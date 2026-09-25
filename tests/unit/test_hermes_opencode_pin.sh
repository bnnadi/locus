#!/usr/bin/env bash
# Regression: profiles under services/hermes/profiles/ shell out to the
# opencode CLI, but nothing installs it yet. A declared ARG/ENV
# OPENCODE_VERSION that the install step never expands, a floating tag
# (latest/main/stable/edge/master/nightly/dev/HEAD/*), or a version
# documented in config/env.example that drifts from the one actually baked
# into the image are the same "unpinned" failure mode wearing different
# disguises -- a rebuild can silently change agent behavior in every one of
# them, and a documented-but-unused ARG is the most deceptive disguise
# because it looks pinned on inspection.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DOCKERFILE="$ROOT/services/hermes/Dockerfile"
ENV_EXAMPLE="$ROOT/config/env.example"

# Exact version only: v?MAJOR.MINOR[.PATCH][-+.prerelease/build]. Floating
# tags like latest/main/stable/edge/master/nightly/dev/HEAD/* never match.
VERSION_SHAPE_RE='^v?[0-9]+\.[0-9]+(\.[0-9]+)?([-.+][0-9A-Za-z.-]+)?$'

fail=0
fail_with() {
  printf 'FAIL %s: %s\n' "$1" "$2" >&2
  fail=1
}

strip_quotes() {
  # Mirrors the quote-stripping in scripts/setup.sh's load_env_file: quotes
  # are semantically irrelevant to Docker and to that loader, so normalize
  # them away rather than treating a quoted pin as a different value.
  local v="$1"
  case "$v" in
    \"*\") v="${v#\"}"; v="${v%\"}" ;;
    \'*\') v="${v#\'}"; v="${v%\'}" ;;
  esac
  printf '%s' "$v"
}

join_continuations() {
  # Reads a Dockerfile on stdin and prints one logical line per output
  # line, joining any line ending in a backslash with the next line. A real
  # `RUN npm install ... \` almost always spans multiple physical lines.
  local line pending=""
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%$'\r'}"
    if [[ -n "$pending" ]]; then
      line="$pending $line"
    fi
    if [[ "$line" == *\\ ]]; then
      pending="${line%\\}"
    else
      printf '%s\n' "$line"
      pending=""
    fi
  done
  if [[ -n "$pending" ]]; then
    printf '%s\n' "$pending"
  fi
}

# --- read the Dockerfile, joined, reporting a labeled failure instead of
# aborting under set -e if it has been moved or renamed ---
dockerfile_available=1
dockerfile_lines=()
if [[ ! -r "$DOCKERFILE" ]]; then
  fail_with dockerfile_readable "cannot read $DOCKERFILE"
  dockerfile_available=0
else
  while IFS= read -r line || [[ -n "$line" ]]; do
    dockerfile_lines+=("$line")
  done < <(join_continuations < "$DOCKERFILE")
fi

# --- collect every ARG/ENV OPENCODE_VERSION declaration, in order ---
declared_values=()
declared_indexes=()
if [[ "$dockerfile_available" -eq 1 ]]; then
  idx=0
  for line in "${dockerfile_lines[@]}"; do
    if [[ "$line" =~ ^[[:space:]]*(ARG|ENV)[[:space:]]+OPENCODE_VERSION(=|[[:space:]])(.*)$ ]]; then
      raw="${BASH_REMATCH[3]}"
      raw="${raw#"${raw%%[![:space:]]*}"}" # trim leading space (space-form ENV)
      declared_values+=("$(strip_quotes "$raw")")
      declared_indexes+=("$idx")
    fi
    idx=$((idx + 1))
  done
fi

# --- 1/2/3 (Dockerfile side): declared exactly once, and its shape is an
# exact version, not a floating tag ---
dockerfile_opencode_version=""
dockerfile_value_ok=0
if [[ "$dockerfile_available" -eq 1 ]]; then
  if [[ ${#declared_values[@]} -eq 0 ]]; then
    fail_with opencode_declared \
      "no 'ARG OPENCODE_VERSION=<value>' or 'ENV OPENCODE_VERSION=<value>' found in $DOCKERFILE"
  else
    unique_count="$(printf '%s\n' "${declared_values[@]}" | sort -u | wc -l | tr -d ' ')"
    if [[ "$unique_count" -gt 1 ]]; then
      fail_with opencode_declared \
        "multiple distinct OPENCODE_VERSION values declared (multi-stage drift): $(printf '%s ' "${declared_values[@]}")"
    else
      dockerfile_opencode_version="${declared_values[0]}"
      if [[ "$dockerfile_opencode_version" =~ $VERSION_SHAPE_RE ]]; then
        dockerfile_value_ok=1
      else
        fail_with opencode_arg_shape \
          "ARG/ENV OPENCODE_VERSION=[$dockerfile_opencode_version] is not an exact version (e.g. 1.18.32 or v1.18.32); floating tags such as latest/main/stable/edge/master/nightly/dev/HEAD/* are not pins"
      fi
    fi
  fi
fi

# --- find every RUN/ADD step that mentions opencode, and whether any of
# them actually expands $OPENCODE_VERSION / ${OPENCODE_VERSION} ---
opencode_run_lines=()
opencode_run_indexes=()
if [[ "$dockerfile_available" -eq 1 ]]; then
  idx=0
  for line in "${dockerfile_lines[@]}"; do
    if [[ "$line" =~ ^[[:space:]]*(RUN|ADD)[[:space:]] ]]; then
      lower="$(printf '%s' "$line" | tr '[:upper:]' '[:lower:]')"
      if [[ "$lower" == *opencode* ]]; then
        opencode_run_lines+=("$line")
        opencode_run_indexes+=("$idx")
      fi
    fi
    idx=$((idx + 1))
  done
fi

# --- 1 (the whole ballgame): an install step must exist, and it must
# actually reference the pinned variable. These are different bugs and get
# different messages. ---
version_referenced=0
referencing_indexes=()
if [[ "$dockerfile_available" -eq 1 ]]; then
  if [[ ${#opencode_run_lines[@]} -eq 0 ]]; then
    fail_with opencode_install_step \
      "no RUN/ADD step in $DOCKERFILE installs opencode -- nothing installs the CLI the profiles under services/hermes/profiles/ shell out to"
  else
    for i in "${!opencode_run_lines[@]}"; do
      l="${opencode_run_lines[$i]}"
      if [[ "$l" == *'$OPENCODE_VERSION'* || "$l" == *'${OPENCODE_VERSION}'* ]]; then
        version_referenced=1
        referencing_indexes+=("${opencode_run_indexes[$i]}")
      fi
    done
    if [[ "$version_referenced" -eq 0 ]]; then
      fail_with opencode_install_pinned \
        "an opencode install step exists but none of them expand \$OPENCODE_VERSION / \${OPENCODE_VERSION} -- the ARG is declared and never used, so a rebuild installs whatever is current: $(printf '%s | ' "${opencode_run_lines[@]}")"
    fi
  fi
fi

# --- 9: the declaration must precede the install step that uses it, or it
# expands to empty at build time. Cheap now that lines are joined. ---
if [[ "$version_referenced" -eq 1 && ${#declared_indexes[@]} -gt 0 && ${#referencing_indexes[@]} -gt 0 ]]; then
  min_declared="${declared_indexes[0]}"
  for d in "${declared_indexes[@]}"; do
    if [[ "$d" -lt "$min_declared" ]]; then
      min_declared="$d"
    fi
  done
  min_referencing="${referencing_indexes[0]}"
  for r in "${referencing_indexes[@]}"; do
    if [[ "$r" -lt "$min_referencing" ]]; then
      min_referencing="$r"
    fi
  done
  if [[ "$min_declared" -gt "$min_referencing" ]]; then
    fail_with opencode_declared_before_use \
      "OPENCODE_VERSION is declared after the install step that expands it (declared at joined line $min_declared, used at joined line $min_referencing); it would expand to empty at build time"
  fi
fi

# --- read config/env.example, same missing-file guard ---
env_available=1
env_content=""
if [[ ! -r "$ENV_EXAMPLE" ]]; then
  fail_with env_readable "cannot read $ENV_EXAMPLE"
  env_available=0
else
  env_content="$(tr -d '\r' < "$ENV_EXAMPLE")"
fi

# --- 2 (env.example side): documented, and its shape is clean -- trailing
# whitespace or an inline comment would really be exported verbatim, since
# load_env_file strips neither. ---
env_opencode_version=""
env_value_ok=0
if [[ "$env_available" -eq 1 ]]; then
  env_declared_values=()
  while IFS= read -r v; do
    env_declared_values+=("$v")
  done < <(printf '%s\n' "$env_content" | sed -n 's/^OPENCODE_VERSION=\(.*\)$/\1/p')

  if [[ ${#env_declared_values[@]} -eq 0 ]]; then
    fail_with opencode_env_documented \
      "no 'OPENCODE_VERSION=<value>' found in $ENV_EXAMPLE"
  else
    env_unique_count="$(printf '%s\n' "${env_declared_values[@]}" | sort -u | wc -l | tr -d ' ')"
    if [[ "$env_unique_count" -gt 1 ]]; then
      fail_with opencode_env_documented \
        "multiple distinct OPENCODE_VERSION lines in $ENV_EXAMPLE: $(printf '%s ' "${env_declared_values[@]}")"
    else
      env_opencode_version="${env_declared_values[0]}"
      if [[ "$env_opencode_version" =~ $VERSION_SHAPE_RE ]]; then
        env_value_ok=1
      else
        fail_with opencode_env_shape \
          "OPENCODE_VERSION=[$env_opencode_version] in $ENV_EXAMPLE is not a clean exact version -- trailing whitespace or an inline comment (e.g. '0.1.0 # keep in sync') is exported verbatim at runtime because load_env_file strips neither"
      fi
    fi
  fi
fi

# --- 3/10: compare, gated on both sides being individually valid so one
# root cause (a missing ARG, say) reports once instead of cascading into a
# second, misleading "they don't match" failure. ---
if [[ "$dockerfile_value_ok" -eq 1 && "$env_value_ok" -eq 1 ]]; then
  if [[ "$dockerfile_opencode_version" != "$env_opencode_version" ]]; then
    fail_with opencode_versions_match \
      "Dockerfile OPENCODE_VERSION=[$dockerfile_opencode_version] != env.example OPENCODE_VERSION=[$env_opencode_version]"
  fi
fi

if [[ "$fail" -ne 0 ]]; then
  exit 1
fi
echo "ok: opencode is declared, installed with its pin expanded, and documented identically in config/env.example"
