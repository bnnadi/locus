#!/usr/bin/env bash
# The sed-extracted unit test (test_n8n_port_align.sh) proves
# align_n8n_port's logic in isolation, but it cannot prove the value
# actually survives `su node -s /bin/sh -c '...'` the way
# N8N_UPSTREAM_ENTRYPOINT needed to. This test boots the real image and
# reads the env the node user sees after su, using the same
# N8N_UPSTREAM_ENTRYPOINT override hook test_n8n_volume_permissions.sh
# uses to exercise the entrypoint without booting n8n itself.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
IMAGE="locus-n8n-port-align-test:local"

if ! command -v docker >/dev/null 2>&1; then
  echo "skip: docker not on PATH"
  exit 0
fi
if ! docker info >/dev/null 2>&1; then
  echo "skip: docker daemon not reachable"
  exit 0
fi

dir="$(mktemp -d)"
cleanup() {
  docker rmi "$IMAGE" >/dev/null 2>&1 || true
  rm -rf "$dir"
}
trap cleanup EXIT

docker build -t "$IMAGE" "$ROOT/services/n8n"

fail=0

# run_case <label> <expect_exit_zero: 0|1> [env...]
#
# Captures stdout and stderr to SEPARATE files. If the printf'd stdout
# were concatenated with entrypoint stderr (e.g. via `2>&1`), a correct
# align_n8n_port that logs anything to stderr on the happy path would
# corrupt the exact-match stdout assertions below.
run_case() {
  local label="$1" expect_zero="$2"
  shift 2
  local -a env_args=()
  for e in "$@"; do
    env_args+=(-e "$e")
  done
  local stdout_file="$dir/stdout.$label" stderr_file="$dir/stderr.$label"
  local status=0
  docker run --rm \
    -e N8N_UPSTREAM_ENTRYPOINT=/bin/sh \
    "${env_args[@]}" \
    "$IMAGE" -c 'printf %s "${N8N_PORT:-<unset>}"' \
    >"$stdout_file" 2>"$stderr_file"
  status=$?
  echo "$label: exit=$status stdout=[$(cat "$stdout_file")] stderr=[$(cat "$stderr_file")]" >&2
  if [[ "$expect_zero" == "0" && "$status" -ne 0 ]]; then
    printf 'FAIL %s: expected exit 0, got %s. stderr: %s\n' "$label" "$status" "$(cat "$stderr_file")" >&2
    fail=1
  fi
  if [[ "$expect_zero" == "1" && "$status" -eq 0 ]]; then
    printf 'FAIL %s: expected non-zero exit, got 0. stdout: %s\n' "$label" "$(cat "$stdout_file")" >&2
    fail=1
  fi
}

# --- PORT unset, N8N_PORT=5678 -> stdout 5678, exit 0 ---
run_case compose 0 N8N_PORT=5678
stdout_compose="$(cat "$dir/stdout.compose")"
if [[ "$stdout_compose" != "5678" ]]; then
  printf 'FAIL compose: stdout got [%s] expected [5678]\n' "$stdout_compose" >&2
  fail=1
fi

# --- PORT=8080, N8N_PORT unset -> stdout 8080, exit 0 (export survived su) ---
run_case railway_default 0 PORT=8080
stdout_railway="$(cat "$dir/stdout.railway_default")"
if [[ "$stdout_railway" != "8080" ]]; then
  printf 'FAIL railway_default: stdout got [%s] expected [8080] (export may not have survived su)\n' "$stdout_railway" >&2
  fail=1
fi

# --- PORT=8080, N8N_PORT=5678 -> container exits non-zero; stdout must not be 8080 ---
run_case mismatch 1 PORT=8080 N8N_PORT=5678
stdout_mismatch="$(cat "$dir/stdout.mismatch")"
if [[ "$stdout_mismatch" == "8080" ]]; then
  printf 'FAIL mismatch: N8N_PORT was overwritten with PORT, stdout was [8080]\n' >&2
  fail=1
fi

# --- PORT=5678, N8N_PORT=5678 -> already aligned, stdout 5678, exit 0 ---
run_case aligned 0 PORT=5678 N8N_PORT=5678
stdout_aligned="$(cat "$dir/stdout.aligned")"
if [[ "$stdout_aligned" != "5678" ]]; then
  printf 'FAIL aligned: stdout got [%s] expected [5678]\n' "$stdout_aligned" >&2
  fail=1
fi

if [[ "$fail" -ne 0 ]]; then
  exit 1
fi
echo "ok: N8N_PORT alignment survives su node"
