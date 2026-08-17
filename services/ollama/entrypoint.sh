#!/bin/sh
set -e

# ollama pull is a client of the daemon. Start the server first, wait until it
# answers, then pull. Pulling before serve exits nonzero under set -e and
# crash-loops the container.
#
# OLLAMA_MODELS is reserved by Ollama as the models *directory*. The
# space-separated list of models to fetch at boot is OLLAMA_PULL_MODELS.

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
  for model in $OLLAMA_PULL_MODELS; do
    echo "Pulling $model..."
    ollama pull "$model"
  done
fi

trap - EXIT INT TERM
wait "$pid"
