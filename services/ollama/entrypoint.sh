#!/bin/sh
set -e

# ollama pull is a client of the daemon. Start the server first, wait until it
# answers, then pull. Pulling before serve exits nonzero under set -e and
# crash-loops the container.
#
# OLLAMA_MODELS is reserved by Ollama as the models *directory*. The
# list of models to fetch at boot is OLLAMA_PULL_MODELS: spaces, commas,
# or a JSON string array. A JSON array is a single word to `for`, so
# ollama pull would get '["mistral",...' and 400 as "invalid model name".

# Prints names as IFS words. tr is the whole parser — keep it in sync
# with tests/unit/test_ollama_pull_models.sh.
expand_pull_models() {
  printf '%s' "$1" | tr -d '[]"' | tr ',' ' '
}

ollama serve &
pid=$!

trap 'kill "$pid" 2>/dev/null || true' EXIT INT TERM

i=0
until ollama list >/dev/null 2>&1; do
  i=$((i + 1))
  if [ "$i" -gt 30 ]; then
    echo "error: ollama serve did not become ready" >&2
    exit 1
  fi
  sleep 1
done

if [ -n "$OLLAMA_PULL_MODELS" ]; then
  # Unquoted expansion is intentional: names are whitespace-separated after
  # expand_pull_models. A failed pull must not set -e the daemon (crash-loop).
  # shellcheck disable=SC2046,SC2086
  for model in $(expand_pull_models "$OLLAMA_PULL_MODELS"); do
    [ -n "$model" ] || continue
    echo "Pulling $model..."
    ollama pull "$model" || echo "error: pull failed for $model" >&2
  done
fi

trap - EXIT INT TERM
wait "$pid"
