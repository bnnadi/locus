#!/usr/bin/env bash
# Target design: authentik nests into two Railway root directories,
# services/authentik/server (the "server" ak process, health-gated) and
# services/authentik/worker (the "worker" ak process, no HTTP health check
# of its own). The old single services/authentik/{Dockerfile,railway.json}
# pair collapses away in favor of those two leaves. scripts/deploy.sh needs
# a source_dir_for() mapping (Railway root dirs no longer match
# services/<service> 1:1), SERVICES=(...) needs an authentik-worker entry,
# and the rendered Compose config needs a second "authentik-worker" service
# alongside "authentik" so the worker process actually runs somewhere.
#
# This test is written FAILING-first: on the current tree (authentik is
# still one flat services/authentik/ directory, one Compose service, one
# SERVICES entry) every assertion below must FAIL except no_sibling, which
# already passes today because services/authentik-worker/ was never a
# sibling directory in this repo.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

# Dummy env for the :? required vars in docker-compose.yml. Exported
# explicitly rather than inherited from CI, so this test is self-contained.
export POSTGRES_PASSWORD="ci-not-a-real-password"
export NEO4J_PASSWORD="ci-not-a-real-password"
export N8N_DB_PASSWORD="ci-not-a-real-password"
export N8N_ENCRYPTION_KEY="ci-not-a-real-key"
export AUTHENTIK_SECRET_KEY="ci-not-a-real-authentik-secret"
export AUTHENTIK_DB_PASSWORD="ci-not-a-real-password"
export AUTHENTIK_BOOTSTRAP_PASSWORD="ci-not-a-real-bootstrap"
export AUTHENTIK_BOOTSTRAP_EMAIL="ci@localhost"
export AUTHENTIK_BOOTSTRAP_TOKEN="ci-not-a-real-bootstrap-token"

fail=0
DEPLOY="$ROOT/scripts/deploy.sh"

# Pulls a named "name() { ... }" function verbatim out of scripts/deploy.sh,
# from its declaration line through the first line that is a bare closing
# brace. Used instead of whole-file greps so each check only sees the one
# function it's asserting about.
extract_function() {
  local file="$1" name="$2"
  sed -n "/^${name}()/,/^}/p" "$file"
}

# Pulls one top-level `case "$TARGET" in ... esac` arm verbatim, from its
# `name)` line through the first line that is nothing but the closing `;;`
# at the arm's own indentation. Deliberately not a BSD-sed-hostile range
# like '/name)/,/esac/' (would overshoot into later arms); this range ends
# at the arm's own terminator instead.
extract_case_arm() {
  local file="$1" name="$2"
  sed -n "/^  ${name})/,/^    ;;\$/p" "$file"
}

# --- 1. services/authentik/server must be a real Railway leaf: its own
#        Dockerfile and railway.json, with a non-empty healthcheckPath. ---
check_server_leaf() {
  local label="server_leaf"
  local dir="$ROOT/services/authentik/server"
  if [[ ! -f "$dir/Dockerfile" ]]; then
    echo "FAIL $label: $dir/Dockerfile does not exist" >&2
    fail=1
    return
  fi
  if [[ ! -f "$dir/railway.json" ]]; then
    echo "FAIL $label: $dir/railway.json does not exist" >&2
    fail=1
    return
  fi
  local result
  result="$(python3 - "$dir/railway.json" <<'PYEOF'
import json
import sys

path = sys.argv[1]
try:
    with open(path) as f:
        data = json.load(f)
except Exception as e:
    print(f"could not parse {path}: {e}")
    sys.exit(1)

path_val = data.get("deploy", {}).get("healthcheckPath", "")
if not path_val:
    print("deploy.healthcheckPath is missing or empty")
    sys.exit(1)
sys.exit(0)
PYEOF
)"
  if [[ $? -ne 0 ]]; then
    echo "FAIL $label: $result" >&2
    fail=1
  else
    echo "PASS $label: services/authentik/server has Dockerfile + healthcheckPath"
  fi
}
check_server_leaf

# --- 2. services/authentik/worker must be a real Railway leaf too, but
#        with NO healthcheckPath (worker has no HTTP endpoint to probe) and
#        no healthcheckTimeout (that setting only makes sense paired with
#        a health check). A present-but-empty healthcheckPath still FAILS
#        this -- the key must be entirely absent, not just falsy. ---
check_worker_leaf() {
  local label="worker_leaf"
  local dir="$ROOT/services/authentik/worker"
  if [[ ! -f "$dir/Dockerfile" ]]; then
    echo "FAIL $label: $dir/Dockerfile does not exist" >&2
    fail=1
    return
  fi
  if [[ ! -f "$dir/railway.json" ]]; then
    echo "FAIL $label: $dir/railway.json does not exist" >&2
    fail=1
    return
  fi
  local result
  result="$(python3 - "$dir/railway.json" <<'PYEOF'
import json
import sys

path = sys.argv[1]
try:
    with open(path) as f:
        data = json.load(f)
except Exception as e:
    print(f"could not parse {path}: {e}")
    sys.exit(1)

deploy = data.get("deploy", {})
errors = []
if "healthcheckPath" in deploy:
    errors.append("deploy.healthcheckPath key is present (must be absent for the worker)")
if "healthcheckTimeout" in deploy:
    errors.append("deploy.healthcheckTimeout key is present (must be absent for the worker)")

for e in errors:
    print(e)
sys.exit(1 if errors else 0)
PYEOF
)"
  if [[ $? -ne 0 ]]; then
    echo "FAIL $label:"$'\n'"$result" >&2
    fail=1
  else
    echo "PASS $label: services/authentik/worker has Dockerfile with no health-check keys"
  fi
}
check_worker_leaf

# --- 3. The old flat services/authentik/{Dockerfile,railway.json} pair
#        must be gone once server/ and worker/ own those files. README.md
#        is explicitly allowed to stay at the parent level (it documents
#        both leaves). ---
check_parent_clean() {
  local label="parent_clean"
  local dir="$ROOT/services/authentik"
  local errors=()
  if [[ -e "$dir/railway.json" ]]; then
    errors+=("$dir/railway.json still exists")
  fi
  if [[ -e "$dir/Dockerfile" ]]; then
    errors+=("$dir/Dockerfile still exists")
  fi
  if [[ ${#errors[@]} -eq 0 ]]; then
    echo "PASS $label: services/authentik has no leftover Dockerfile/railway.json"
  else
    local e
    for e in "${errors[@]}"; do
      echo "FAIL $label: $e" >&2
    done
    fail=1
  fi
}
check_parent_clean

# --- 4. services/authentik-worker/ (a sibling directory, as opposed to
#        the services/authentik/worker leaf above) must never exist. This
#        already passes today -- kept as a guard against reintroducing the
#        sibling-directory design instead of the nested one. ---
check_no_sibling() {
  local label="no_sibling"
  if [[ ! -e "$ROOT/services/authentik-worker" ]]; then
    echo "PASS $label: services/authentik-worker does not exist"
  else
    echo "FAIL $label: services/authentik-worker exists as a sibling directory" >&2
    fail=1
  fi
}
check_no_sibling

# --- 5. SERVICES=(...) in scripts/deploy.sh must list both "authentik"
#        and "authentik-worker" as exact, word-split tokens. Extracted
#        from the assignment line via grep (not a BSD-sed '/^SERVICES=(/,
#        /)/' range, which would overshoot past this one-line array to an
#        unrelated ")" later in the file, e.g. in die() {). ---
check_services_array() {
  local label="services_array"
  local block
  block="$(grep -E '^SERVICES=\(' "$DEPLOY")"
  if [[ -z "$block" ]]; then
    echo "FAIL $label: could not locate SERVICES=(...) block in scripts/deploy.sh" >&2
    fail=1
    return
  fi
  local stripped tokens
  stripped="${block#SERVICES=(}"
  stripped="${stripped%)}"
  # shellcheck disable=SC2206
  tokens=($stripped)
  local has_authentik=0 has_worker=0 t
  for t in "${tokens[@]}"; do
    if [[ "$t" == "authentik" ]]; then
      has_authentik=1
    fi
    if [[ "$t" == "authentik-worker" ]]; then
      has_worker=1
    fi
  done
  if [[ "$has_authentik" -eq 1 && "$has_worker" -eq 1 ]]; then
    echo "PASS $label: SERVICES array has both authentik and authentik-worker (${tokens[*]})"
  else
    echo "FAIL $label: SERVICES array missing authentik and/or authentik-worker (${tokens[*]})" >&2
    fail=1
  fi
}
check_services_array

# --- 6. scripts/deploy.sh must define a source_dir_for() function mapping
#        each SERVICES entry to its actual Railway root directory, since
#        that's no longer always services/<name> once authentik nests. ---
check_source_dir_map() {
  local label="source_dir_map"
  local snippet
  snippet="$(extract_function "$DEPLOY" "source_dir_for")"
  if [[ -z "$snippet" ]]; then
    echo "FAIL $label: source_dir_for() is not defined in scripts/deploy.sh" >&2
    fail=1
    return
  fi

  local snippet_file
  snippet_file="$(mktemp)"
  printf '%s\n' "$snippet" > "$snippet_file"

  # Source ONLY the extracted function body, not the rest of deploy.sh
  # (which has top-level side effects like `set -euo pipefail; cd ...`).
  local got_authentik got_worker got_uzora status=0
  # shellcheck disable=SC1090
  source "$snippet_file" 2>/dev/null || status=1
  if [[ "$status" -ne 0 ]] || ! declare -f source_dir_for >/dev/null 2>&1; then
    echo "FAIL $label: extracted source_dir_for() snippet did not define a callable function" >&2
    fail=1
    rm -f "$snippet_file"
    return
  fi

  got_authentik="$(source_dir_for authentik)"
  got_worker="$(source_dir_for authentik-worker)"
  got_uzora="$(source_dir_for uzora)"
  rm -f "$snippet_file"
  unset -f source_dir_for 2>/dev/null || true

  local errors=()
  if [[ "$got_authentik" != "services/authentik/server" ]]; then
    errors+=("source_dir_for authentik => '$got_authentik' (want services/authentik/server)")
  fi
  if [[ "$got_worker" != "services/authentik/worker" ]]; then
    errors+=("source_dir_for authentik-worker => '$got_worker' (want services/authentik/worker)")
  fi
  if [[ "$got_uzora" != "services/uzora" ]]; then
    errors+=("source_dir_for uzora => '$got_uzora' (want services/uzora)")
  fi

  if [[ ${#errors[@]} -eq 0 ]]; then
    echo "PASS $label: source_dir_for maps authentik/authentik-worker/uzora correctly"
  else
    local e
    for e in "${errors[@]}"; do
      echo "FAIL $label: $e" >&2
    done
    fail=1
  fi
}
check_source_dir_map

# --- 7. In the railway) case only, `railway up` must be driven by
#        source_dir_for (so authentik and authentik-worker each get their
#        own root dir) and must never name both authentik and
#        authentik-worker as a paired argument on one up line. ---
check_railway_no_pair() {
  local label="railway_no_pair"
  local block
  block="$(extract_case_arm "$DEPLOY" "railway")"
  if [[ -z "$block" ]]; then
    echo "FAIL $label: could not locate the railway) case arm in scripts/deploy.sh" >&2
    fail=1
    return
  fi

  local uses_source_dir_for=0 has_pair=0
  if echo "$block" | grep -Eq 'up[^#]*source_dir_for'; then
    uses_source_dir_for=1
  fi
  if echo "$block" | grep -Eq 'up[^#]*--service[[:space:]]+"?authentik"?[^#]*\bauthentik-worker\b'; then
    has_pair=1
  fi

  if [[ "$uses_source_dir_for" -eq 1 && "$has_pair" -eq 0 ]]; then
    echo "PASS $label: railway up is driven by source_dir_for with no authentik/authentik-worker pairing"
  else
    echo "FAIL $label: railway up does not use source_dir_for for per-service root dirs (or still pairs authentik with authentik-worker on one up line)" >&2
    fail=1
  fi
}
check_railway_no_pair

# --- 8. Rendered Compose config must define both "authentik" and
#        "authentik-worker" services, and the worker's environment must
#        carry the three bootstrap vars (H1: the worker, not just the
#        server, needs them to run first-boot setup). Docker/Compose being
#        unavailable is a hard FAIL here, not a skip. ---
check_rendered_compose() {
  local label="rendered_compose"
  if ! command -v docker >/dev/null 2>&1; then
    echo "FAIL $label: docker not found on PATH" >&2
    fail=1
    return
  fi
  if ! docker compose version >/dev/null 2>&1; then
    echo "FAIL $label: 'docker compose' not available" >&2
    fail=1
    return
  fi

  local err_file json_file
  err_file="$(mktemp)"
  json_file="$(mktemp)"

  local json status
  json="$(docker compose \
    -f "$ROOT/docker-compose.yml" \
    -f "$ROOT/config/docker-compose.override.yml" \
    --env-file /dev/null \
    config --format json 2>"$err_file")"
  status=$?
  if [[ "$status" -ne 0 ]]; then
    echo "FAIL $label: docker compose config exited $status: $(cat "$err_file" 2>/dev/null)" >&2
    rm -f "$err_file" "$json_file"
    fail=1
    return
  fi
  rm -f "$err_file"

  printf '%s' "$json" > "$json_file"

  local result
  result="$(python3 - "$json_file" <<'PYEOF'
import json
import sys

path = sys.argv[1]
try:
    with open(path) as f:
        data = json.load(f)
except Exception as e:
    print(f"could not parse compose config JSON: {e}")
    sys.exit(1)

services = data.get("services", {})
errors = []
if "authentik" not in services:
    errors.append("services has no key 'authentik'")
if "authentik-worker" not in services:
    errors.append("services has no key 'authentik-worker'")
else:
    env = services["authentik-worker"].get("environment", {})
    if isinstance(env, list):
        env_keys = set()
        for item in env:
            env_keys.add(item.split("=", 1)[0])
    else:
        env_keys = set(env.keys())
    for required in (
        "AUTHENTIK_BOOTSTRAP_PASSWORD",
        "AUTHENTIK_BOOTSTRAP_EMAIL",
        "AUTHENTIK_BOOTSTRAP_TOKEN",
    ):
        if required not in env_keys:
            errors.append(f"authentik-worker environment is missing {required}")

for e in errors:
    print(e)
sys.exit(1 if errors else 0)
PYEOF
)"
  status=$?
  rm -f "$json_file"
  if [[ "$status" -ne 0 ]]; then
    echo "FAIL $label:"$'\n'"$result" >&2
    fail=1
  else
    echo "PASS $label: rendered compose has authentik + authentik-worker with bootstrap env on the worker"
  fi
}
check_rendered_compose

# --- 9. In the compose) case only, starting just --service authentik must
#        also bring up authentik-worker (Compose has no dependency-only
#        "run this other service too" primitive, so deploy.sh has to pair
#        them explicitly on the up command). A commented-out pairing does
#        not count. ---
check_compose_pair() {
  local label="compose_pair"
  local block
  block="$(extract_case_arm "$DEPLOY" "compose")"
  if [[ -z "$block" ]]; then
    echo "FAIL $label: could not locate the compose) case arm in scripts/deploy.sh" >&2
    fail=1
    return
  fi

  local paired=0
  while IFS= read -r line; do
    # Skip comment-only lines so a mention in a comment doesn't count.
    local trimmed="${line#"${line%%[![:space:]]*}"}"
    [[ "$trimmed" == \#* ]] && continue
    if echo "$line" | grep -Eq 'up[^#]*\bauthentik\b[^#]*\bauthentik-worker\b'; then
      paired=1
      break
    fi
  done <<< "$block"

  if [[ "$paired" -eq 1 ]]; then
    echo "PASS $label: compose) --service authentik ups authentik-worker alongside it"
  else
    echo "FAIL $label: compose) case does not pair authentik with authentik-worker on an up line" >&2
    fail=1
  fi
}
check_compose_pair

# --- 10. If both nested Dockerfiles exist, they must pin the exact same
#         upstream image tag -- server and worker are the same ak binary
#         started with a different command, not two different images. ---
check_from_pin() {
  local label="from_pin"
  local want="ghcr.io/goauthentik/server:2026.8.2"
  local server_df="$ROOT/services/authentik/server/Dockerfile"
  local worker_df="$ROOT/services/authentik/worker/Dockerfile"

  if [[ ! -f "$server_df" || ! -f "$worker_df" ]]; then
    echo "FAIL $label: server and/or worker Dockerfile is missing" >&2
    fail=1
    return
  fi

  local server_from worker_from
  server_from="$(grep -E '^FROM ' "$server_df" | head -n1)"
  worker_from="$(grep -E '^FROM ' "$worker_df" | head -n1)"

  if [[ "$server_from" == "FROM $want" && "$worker_from" == "FROM $want" ]]; then
    echo "PASS $label: server and worker Dockerfiles both pin $want"
  else
    echo "FAIL $label: server FROM='$server_from' worker FROM='$worker_from' (want 'FROM $want' on both)" >&2
    fail=1
  fi
}
check_from_pin

# --- 11. health_path_for() and base_url_for() must not grow a dedicated
#         authentik-worker) arm. The worker has no HTTP endpoint of its
#         own to health-check or point a base URL at; it should fall
#         through to each function's default case like every other
#         non-HTTP service, not gain a lookalike branch next to
#         authentik). ---
check_no_worker_health_url() {
  local label="no_worker_health_url"
  local health_snippet url_snippet
  health_snippet="$(extract_function "$DEPLOY" "health_path_for")"
  url_snippet="$(extract_function "$DEPLOY" "base_url_for")"

  if [[ -z "$health_snippet" ]]; then
    echo "FAIL $label: health_path_for() is not defined in scripts/deploy.sh" >&2
    fail=1
    return
  fi
  if [[ -z "$url_snippet" ]]; then
    echo "FAIL $label: base_url_for() is not defined in scripts/deploy.sh" >&2
    fail=1
    return
  fi

  local errors=()
  if echo "$health_snippet" | grep -Eq '^\s*authentik-worker\)'; then
    errors+=("health_path_for() has an authentik-worker) arm")
  fi
  if echo "$url_snippet" | grep -Eq '^\s*authentik-worker\)'; then
    errors+=("base_url_for() has an authentik-worker) arm")
  fi

  if [[ ${#errors[@]} -eq 0 ]]; then
    echo "PASS $label: health_path_for/base_url_for have no authentik-worker) arm"
  else
    local e
    for e in "${errors[@]}"; do
      echo "FAIL $label: $e" >&2
    done
    fail=1
  fi
}
check_no_worker_health_url

if [[ "$fail" -ne 0 ]]; then
  exit 1
fi
echo "ok: authentik nested into services/authentik/{server,worker}"
