#!/usr/bin/env bash
# Regression: Railway injects PORT for its healthcheck/proxy, while n8n
# itself listens on N8N_PORT (Compose sets N8N_PORT=5678 directly, with
# no PORT). If the entrypoint silently prefers one over the other, n8n
# can end up listening on a port nothing checks — same class of bug as
# the unexported N8N_UPSTREAM_ENTRYPOINT that dies under `su node`.
#
# align_n8n_port must:
#   1. PORT unset/empty            -> no-op, N8N_PORT untouched
#   2. PORT set, N8N_PORT unset/empty -> export N8N_PORT="$PORT"
#   3. both set and equal          -> no-op, success
#   4. both set and differ         -> fail loud (non-zero, stderr names
#                                      both values), do NOT overwrite
#                                      N8N_PORT
#
# IMPORTANT: align_n8n_port is called in the CURRENT shell for every case
# below, never inside `$(...)`. A correct `export N8N_PORT=...` mutates
# only the subshell command substitution runs in, so capturing it via
# `out="$(align_n8n_port)"` would make a correct implementation look
# broken (and would make the "must not overwrite" assertion on mismatch
# untestable, since the parent's N8N_PORT could never have changed
# either way). stdout/stderr are captured to files instead.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
dir="$(mktemp -d)"
trap 'rm -rf "$dir"' EXIT

sed -n '/^align_n8n_port()/,/^}/p' "$ROOT/services/n8n/entrypoint.sh" > "$dir/lib.sh"

fail=0
fn_available=1
if [[ ! -s "$dir/lib.sh" ]]; then
  echo "FAIL: align_n8n_port() not found in services/n8n/entrypoint.sh" >&2
  fail=1
  fn_available=0
else
  # shellcheck disable=SC1091
  source "$dir/lib.sh"
fi

reset_env() {
  unset PORT N8N_PORT || true
}

# Runs align_n8n_port in *this* shell (no subshell) so a correct
# `export N8N_PORT=...` actually lands in our environment, and captures
# its streams to files rather than via command substitution.
run_align() {
  local out_file="$1" err_file="$2"
  align_n8n_port >"$out_file" 2>"$err_file"
}

# --- case: PORT unset, N8N_PORT already set (Compose default) ---
check_compose() {
  local label="compose" status=0
  if [[ "$fn_available" -eq 0 ]]; then
    printf 'FAIL %s: align_n8n_port not available (function missing)\n' "$label" >&2
    fail=1
    return
  fi
  reset_env
  N8N_PORT=5678
  run_align "$dir/out.$label" "$dir/err.$label"
  status=$?
  if [[ "$status" -ne 0 ]]; then
    printf 'FAIL %s: align_n8n_port returned %s, expected 0. stderr: %s\n' \
      "$label" "$status" "$(cat "$dir/err.$label")" >&2
    fail=1
  fi
  if [[ "${N8N_PORT:-}" != "5678" ]]; then
    printf 'FAIL %s: N8N_PORT got [%s] expected [5678]\n' "$label" "${N8N_PORT:-}" >&2
    fail=1
  fi
}
check_compose

# --- case: PORT="" (empty, not just unset) also counts as no-op ---
check_compose_empty_port() {
  local label="compose_empty_port" status=0
  if [[ "$fn_available" -eq 0 ]]; then
    printf 'FAIL %s: align_n8n_port not available (function missing)\n' "$label" >&2
    fail=1
    return
  fi
  reset_env
  PORT=""
  N8N_PORT=5678
  run_align "$dir/out.$label" "$dir/err.$label"
  status=$?
  if [[ "$status" -ne 0 ]]; then
    printf 'FAIL %s: align_n8n_port returned %s, expected 0. stderr: %s\n' \
      "$label" "$status" "$(cat "$dir/err.$label")" >&2
    fail=1
  fi
  if [[ "${N8N_PORT:-}" != "5678" ]]; then
    printf 'FAIL %s: N8N_PORT got [%s] expected [5678]\n' "$label" "${N8N_PORT:-}" >&2
    fail=1
  fi
}
check_compose_empty_port

# --- case: PORT set, N8N_PORT unset (Railway default) ---
check_railway_default() {
  local label="railway_default" status=0
  if [[ "$fn_available" -eq 0 ]]; then
    printf 'FAIL %s: align_n8n_port not available (function missing)\n' "$label" >&2
    fail=1
    return
  fi
  reset_env
  PORT=8080
  run_align "$dir/out.$label" "$dir/err.$label"
  status=$?
  if [[ "$status" -ne 0 ]]; then
    printf 'FAIL %s: align_n8n_port returned %s, expected 0. stderr: %s\n' \
      "$label" "$status" "$(cat "$dir/err.$label")" >&2
    fail=1
    return
  fi
  if [[ "${N8N_PORT:-}" != "8080" ]]; then
    printf 'FAIL %s: N8N_PORT got [%s] expected [8080]\n' "$label" "${N8N_PORT:-}" >&2
    fail=1
    return
  fi
  # Must actually be exported, or a child shell (the same mechanism as
  # `su node -c '...'`) will not see it.
  local child_seen
  child_seen="$(sh -c 'printf %s "${N8N_PORT:-<unset>}"')"
  if [[ "$child_seen" != "8080" ]]; then
    printf 'FAIL %s: N8N_PORT not exported, child saw [%s] expected [8080]\n' "$label" "$child_seen" >&2
    fail=1
  fi
}
check_railway_default

# --- case: PORT set, N8N_PORT="" (empty, not just unset) ---
check_railway_default_empty_n8n_port() {
  local label="railway_default_empty_n8n_port" status=0
  if [[ "$fn_available" -eq 0 ]]; then
    printf 'FAIL %s: align_n8n_port not available (function missing)\n' "$label" >&2
    fail=1
    return
  fi
  reset_env
  PORT=8080
  N8N_PORT=""
  run_align "$dir/out.$label" "$dir/err.$label"
  status=$?
  if [[ "$status" -ne 0 ]]; then
    printf 'FAIL %s: align_n8n_port returned %s, expected 0. stderr: %s\n' \
      "$label" "$status" "$(cat "$dir/err.$label")" >&2
    fail=1
    return
  fi
  if [[ "${N8N_PORT:-}" != "8080" ]]; then
    printf 'FAIL %s: N8N_PORT got [%s] expected [8080]\n' "$label" "${N8N_PORT:-}" >&2
    fail=1
    return
  fi
  local child_seen
  child_seen="$(sh -c 'printf %s "${N8N_PORT:-<unset>}"')"
  if [[ "$child_seen" != "8080" ]]; then
    printf 'FAIL %s: N8N_PORT not exported, child saw [%s] expected [8080]\n' "$label" "$child_seen" >&2
    fail=1
  fi
}
check_railway_default_empty_n8n_port

# --- case: both set and differ -> fail loud, do not overwrite ---
check_mismatch() {
  local label="mismatch" status=0
  if [[ "$fn_available" -eq 0 ]]; then
    printf 'FAIL %s: align_n8n_port not available (function missing)\n' "$label" >&2
    fail=1
    return
  fi
  reset_env
  PORT=8080
  N8N_PORT=5678
  run_align "$dir/out.$label" "$dir/err.$label"
  status=$?
  if [[ "$status" -eq 0 ]]; then
    printf 'FAIL %s: align_n8n_port returned 0, expected non-zero on mismatch\n' "$label" >&2
    fail=1
  fi
  # Real assertion: align_n8n_port ran in *this* shell above, so if it had
  # overwritten N8N_PORT with PORT, we would see that here.
  if [[ "${N8N_PORT:-}" != "5678" ]]; then
    printf 'FAIL %s: N8N_PORT got [%s] expected [5678] (must not be overwritten)\n' "$label" "${N8N_PORT:-}" >&2
    fail=1
  fi
  local err_content
  err_content="$(cat "$dir/err.$label")"
  if [[ "$err_content" != *"8080"* || "$err_content" != *"5678"* ]]; then
    printf 'FAIL %s: stderr did not mention both values, got: %s\n' "$label" "$err_content" >&2
    fail=1
  fi
}
check_mismatch

# --- case: both set and equal -> no-op, success ---
check_aligned() {
  local label="aligned" status=0
  if [[ "$fn_available" -eq 0 ]]; then
    printf 'FAIL %s: align_n8n_port not available (function missing)\n' "$label" >&2
    fail=1
    return
  fi
  reset_env
  PORT=5678
  N8N_PORT=5678
  run_align "$dir/out.$label" "$dir/err.$label"
  status=$?
  if [[ "$status" -ne 0 ]]; then
    printf 'FAIL %s: align_n8n_port returned %s, expected 0. stderr: %s\n' \
      "$label" "$status" "$(cat "$dir/err.$label")" >&2
    fail=1
    return
  fi
  if [[ "${N8N_PORT:-}" != "5678" ]]; then
    printf 'FAIL %s: N8N_PORT got [%s] expected [5678]\n' "$label" "${N8N_PORT:-}" >&2
    fail=1
  fi
}
check_aligned

# --- railway.json: healthcheckPath and healthcheckTimeout=600 ---
# Runs independently of the function checks above (never gated on
# fn_available, never exits early) so a missing function and a stale
# timeout both show up as failures in the same run.
check_railway_json() {
  local label="railway_json"
  local json="$ROOT/services/n8n/railway.json"
  if ! command -v python3 >/dev/null 2>&1; then
    printf 'FAIL %s: python3 not on PATH, cannot verify railway.json\n' "$label" >&2
    fail=1
    return
  fi
  local result status
  result="$(python3 - "$json" <<'PYEOF'
import json
import sys

path = sys.argv[1]
with open(path) as f:
    data = json.load(f)

deploy = data.get("deploy", {})
path_val = deploy.get("healthcheckPath")
timeout_val = deploy.get("healthcheckTimeout")

errors = []
if path_val != "/healthz/readiness":
    errors.append(f"healthcheckPath got [{path_val!r}] expected ['/healthz/readiness']")
if not isinstance(timeout_val, int) or isinstance(timeout_val, bool) or timeout_val != 600:
    errors.append(f"healthcheckTimeout got [{timeout_val!r}] expected [600]")

for e in errors:
    print(e)
sys.exit(1 if errors else 0)
PYEOF
)"
  status=$?
  if [[ "$status" -ne 0 ]]; then
    printf 'FAIL %s:\n%s\n' "$label" "$result" >&2
    fail=1
  fi
}
check_railway_json

if [[ "$fail" -ne 0 ]]; then
  exit 1
fi
echo "ok: align_n8n_port and railway.json healthcheck timeout"
