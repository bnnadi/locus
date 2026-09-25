#!/usr/bin/env bash
#
# Installs the shadow dev team profiles into a running Hermes instance.
# Safe to re-run.
#
# Usage (from inside the hermes container, repo checked out somewhere readable):
#   services/hermes/install-profiles.sh
#
# What it does:
#   1. Installs the researcher / spec / engineer / qa profile distributions
#      from this repo.
#   2. Merges the orchestrator config into the default profile, preserving the
#      model and provider settings already on the volume.
#   3. Copies each worker's OpenCode config into that profile's own HOME.
#
# Step 3 is the load-bearing one. Each worker profile sets
# terminal.home_mode: profile, so its tool subprocesses run with
# HOME={HERMES_HOME}/home and OpenCode reads ~/.config/opencode from there.
# That is what scopes each lane to exactly one OpenCode agent: the qa profile
# cannot run `opencode run --agent engineer`, because that agent does not exist
# in its roster. Putting these files in the target repo's .opencode/ instead
# would make one shared roster visible to all four lanes and undo the split.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly REPO_DIR
readonly PROFILES_DIR="${REPO_DIR}/profiles"

# /opt/data is the Hermes home inside the official image, and the only
# writable state. Named profiles live under profiles/<name> beneath it.
readonly HERMES_ROOT="${HERMES_HOME:-/opt/data}"

readonly WORKERS=(researcher spec engineer qa)

die() {
  echo "error: $*" >&2
  exit 1
}

require() {
  command -v "$1" >/dev/null 2>&1 || die "$1 is not on PATH"
}

# The orchestrator's config cannot be installed as a distribution: `hermes
# profile install` always creates a new profile, and `default` is the
# installation root rather than a profile directory. Copying over it would
# discard the model, provider, and base_url already configured on the volume,
# so merge instead and keep a timestamped backup.
merge_default_config() {
  local src="${PROFILES_DIR}/default/config.yaml"
  local dest="${HERMES_ROOT}/config.yaml"

  [ -f "$src" ] || die "missing $src"

  if [ ! -f "$dest" ]; then
    install -m 600 "$src" "$dest"
    echo "default: wrote $dest"
    return
  fi

  cp -p "$dest" "${dest}.bak.$(date -u +%Y%m%dT%H%M%SZ)"

  python3 - "$src" "$dest" <<'PY'
import sys

try:
    import yaml
except ImportError:
    sys.exit("error: PyYAML unavailable; merge %s into %s by hand" % tuple(sys.argv[1:3]))

src_path, dest_path = sys.argv[1], sys.argv[2]

with open(src_path) as fh:
    incoming = yaml.safe_load(fh) or {}
with open(dest_path) as fh:
    existing = yaml.safe_load(fh) or {}


def merge(base, new):
    """Recursive merge where the repo wins on conflict.

    Scalars and lists are replaced wholesale rather than combined: a toolset
    allowlist that merged with whatever was already on the volume would silently
    grant the orchestrator back the terminal access this config exists to remove.
    """
    for key, value in new.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            merge(base[key], value)
        else:
            base[key] = value
    return base


with open(dest_path, "w") as fh:
    yaml.safe_dump(merge(existing, incoming), fh, sort_keys=False)
PY

  echo "default: merged orchestrator config into $dest (backup alongside)"
}

install_worker_profile() {
  local name="$1"
  local src="${PROFILES_DIR}/${name}"

  [ -d "$src" ] || die "missing profile directory $src"

  if hermes profile list 2>/dev/null | grep -qw "$name"; then
    hermes profile update "$name" --force-config --yes
    echo "${name}: updated from ${src}"
  else
    hermes profile install "$src" --alias
    echo "${name}: installed from ${src}"
  fi
}

install_opencode_config() {
  local name="$1"
  local src="${PROFILES_DIR}/${name}/opencode"
  local dest="${HERMES_ROOT}/profiles/${name}/home/.config/opencode"

  [ -d "$src" ] || die "missing OpenCode config at $src"

  mkdir -p "${dest}/agents"

  # Read-only, and a tripwire when read-only is all we have.
  #
  # These two files ARE the lane's permission model, and they sit on the
  # mounted volume. A bash-level write from a compromised lane would rewrite
  # them, and the change would survive the card, the session, and a container
  # restart — every later card in that lane then runs under permissions an
  # attacker chose.
  #
  # Mode 444 does not prevent that. A file's owner can always chmod it back,
  # so this is only a real control when the owner is a uid the lane's tools do
  # not run as. The stock hermes-agent image runs everything as root, which
  # means today it is a speed bump and an audit signal, not a boundary: if a
  # 444 file has changed, something wrote it deliberately. Compare hashes on a
  # schedule and treat a mismatch as a compromised lane.
  install -m 444 "${src}/opencode.json" "${dest}/opencode.json"
  install -m 444 "${src}/agents/${name}.md" "${dest}/agents/${name}.md"

  if [ "$(id -u)" -eq 0 ]; then
    chown root:root "${dest}/opencode.json" "${dest}/agents/${name}.md"
    if [ -z "${HERMES_TOOL_UID:-}" ]; then
      echo "note: ${name} permission files are root-owned, but tools also run" >&2
      echo "      as root here, so the lane can chmod and rewrite them. This" >&2
      echo "      is gap 1 in services/hermes/SECURITY.md. Bound it at the" >&2
      echo "      PAT scope until the tool uid is separated." >&2
    fi
  else
    echo "warning: ${name} permission files are owned by $(id -un), the same" >&2
    echo "         uid that runs the lane's tools, so the lane can rewrite" >&2
    echo "         its own permissions. See services/hermes/SECURITY.md." >&2
  fi

  # A stale agent from an earlier layout would still be a reachable roster
  # entry, and reachable means its permissions apply.
  find "${dest}/agents" -name '*.md' ! -name "${name}.md" -delete

  echo "${name}: OpenCode roster scoped to agent '${name}' at ${dest}"
}

main() {
  require hermes
  require python3
  command -v opencode >/dev/null 2>&1 ||
    echo "warning: opencode is not on PATH; worker lanes will block on every card" >&2

  [ -d "$HERMES_ROOT" ] || die "$HERMES_ROOT does not exist; is the volume mounted?"

  merge_default_config

  for name in "${WORKERS[@]}"; do
    install_worker_profile "$name"
    install_opencode_config "$name"
  done

  # The board has to exist before the dispatcher can promote anything onto it.
  hermes kanban init >/dev/null 2>&1 || true

  cat <<'EOF'

Installed. Before the first card:
  - Set a model on each profile (hermes -p <name> model). The distributions
    deliberately ship no model pin so installing cannot blank an existing one.
  - Confirm the orchestrator really is inert:
      hermes -p default tools --summary   # expect no terminal, file, or code tools
  - Confirm each lane sees exactly one OpenCode agent:
      hermes -p qa chat -q 'run: opencode agent list'
  - Create cards with a shared worktree or dir workspace. The default scratch
    workspace is deleted per card, so the engineer would never see the tests.
EOF
}

main "$@"
