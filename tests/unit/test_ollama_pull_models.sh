#!/usr/bin/env bash
# Regression: Railway often stores OLLAMA_PULL_MODELS as a JSON array.
# Word-splitting that value passes ollama pull an invalid name and
# crash-loops the container under set -e.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
dir="$(mktemp -d)"
trap 'rm -rf "$dir"' EXIT

sed -n '/^expand_pull_models()/,/^}/p' "$ROOT/services/ollama/entrypoint.sh" > "$dir/lib.sh"
# shellcheck disable=SC1091
source "$dir/lib.sh"

fail=0
check() {
  local label="$1" input="$2" expected="$3" actual
  actual="$(expand_pull_models "$input")"
  if [[ "$actual" != "$expected" ]]; then
    printf 'FAIL %s: got [%s] expected [%s]\n' "$label" "$actual" "$expected" >&2
    fail=1
  fi
}

check spaces 'mistral qwen3-embedding' 'mistral qwen3-embedding'
check commas 'mistral,qwen3-embedding' 'mistral qwen3-embedding'
check json_compact '["mistral","qwen3-embedding"]' 'mistral qwen3-embedding'
check json_live '["mistral","qwen3-embedding", "deepseek-v4-flash:cloud", "qwen3.8","muse-glimmer"]' \
  'mistral qwen3-embedding  deepseek-v4-flash:cloud  qwen3.8 muse-glimmer'

if [[ "$fail" -ne 0 ]]; then
  exit 1
fi
echo "ok: expand_pull_models"
