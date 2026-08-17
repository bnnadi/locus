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
echo "load_env_file preserves literal \$, \$\$, backticks, and command substitutions"
