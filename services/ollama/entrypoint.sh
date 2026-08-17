#!/bin/sh
set -e

# Pull models specified in OLLAMA_MODELS env var
if [ -n "$OLLAMA_MODELS" ]; then
  for model in $OLLAMA_MODELS; do
    echo "Pulling $model..."
    ollama pull "$model"
  done
fi

# Start Ollama server
exec ollama serve