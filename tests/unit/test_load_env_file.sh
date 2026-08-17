#!/usr/bin/env bash
# Regression: .env values must be stored literally. Passwords and URLs
# commonly contain $, $$, backticks, and $(...).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
dir="$(mktemp -d)"
trap 'rm -rf "$dir"' EXIT

# Isolate the helper without running the rest of setup.sh.
sed -n '/^load_env_file()/,/^}/p' "$ROOT/scripts/setup.sh" > "$dir/lib.sh"
# shellcheck disable=SC1090
source "$dir/lib.sh"

cat > "$dir/.env" <<'ENV'
# comment should be ignored
N8N_DB_PASSWORD=pa$$word
ADMIN_DATABASE_URL=postgres://u:pass$word@host/db
BACKTICK=pre`uname`post
CMDSUB=pre$(echo pwned)post
PIDLIKE=before$$after
QUOTED="keep $HOME and $$"
ENV

unset N8N_DB_PASSWORD ADMIN_DATABASE_URL BACKTICK CMDSUB PIDLIKE QUOTED || true
load_env_file "$dir/.env"

fail=0
check() {
  local name="$1" expected="$2" actual
  actual="${!name}"
  if [[ "$actual" != "$expected" ]]; then
    printf 'FAIL %s: got [%s] expected [%s]\n' "$name" "$actual" "$expected" >&2
    fail=1
  fi
}

check N8N_DB_PASSWORD 'pa$$word'
check ADMIN_DATABASE_URL 'postgres://u:pass$word@host/db'
check BACKTICK 'pre`uname`post'
check CMDSUB 'pre$(echo pwned)post'
check PIDLIKE 'before$$after'
check QUOTED 'keep $HOME and $$'

if [[ "$fail" -ne 0 ]]; then
  exit 1
fi

cat > "$dir/.env.gen" <<'ENV'
API_SERVER_KEY="$(openssl rand -hex 32)"
STILL_LITERAL=pre$(echo pwned)post
ENV

unset API_SERVER_KEY STILL_LITERAL || true
load_env_file "$dir/.env.gen"

if [[ ! "$API_SERVER_KEY" =~ ^[0-9a-f]{64}$ ]]; then
  printf 'FAIL API_SERVER_KEY: expected 64 hex chars, got [%s]\n' "$API_SERVER_KEY" >&2
  exit 1
fi
check STILL_LITERAL 'pre$(echo pwned)post'

# Generator must be replaced in the file so a second load does not rotate.
second="$API_SERVER_KEY"
unset API_SERVER_KEY || true
load_env_file "$dir/.env.gen"
if [[ "$API_SERVER_KEY" != "$second" ]]; then
  printf 'FAIL API_SERVER_KEY rotated on second load\n' >&2
  exit 1
fi
if grep -q 'openssl' "$dir/.env.gen"; then
  printf 'FAIL generator left in file after expansion\n' >&2
  exit 1
fi

echo "load_env_file preserves literal \$, \$\$, backticks, and command substitutions"
echo "load_env_file expands only \$(openssl rand -hex N) and persists it"
