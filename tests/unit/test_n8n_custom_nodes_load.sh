#!/usr/bin/env bash
# The Locus nodes must load through n8n's directory loader. A bare require
# of the class files does not prove that: a throw inside loadAll aborts
# startup before /healthz/readiness exists. The empty off directory must
# still become ready, so rollback is an env change.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
IMAGE="locus-n8n-nodes-load:local"

if ! command -v docker >/dev/null 2>&1; then
  echo "skip: docker not on PATH"
  exit 0
fi
if ! docker info >/dev/null 2>&1; then
  echo "skip: docker daemon not reachable"
  exit 0
fi

docker build -t "$IMAGE" "$ROOT/services/n8n"

wait_ready() {
  local name="$1"
  local i
  for i in $(seq 1 60); do
    if docker exec "$name" wget -qO- http://127.0.0.1:5678/healthz/readiness >/dev/null 2>&1; then
      return 0
    fi
    if ! docker inspect -f '{{.State.Running}}' "$name" | grep -q true; then
      echo "container $name exited before readiness" >&2
      docker logs "$name" >&2 || true
      return 1
    fi
    sleep 2
  done
  echo "timed out waiting for $name readiness" >&2
  docker logs "$name" >&2 || true
  return 1
}

run_ready() {
  local name="$1"
  shift
  docker rm -f "$name" >/dev/null 2>&1 || true
  docker run -d --name "$name" \
    -e N8N_ENCRYPTION_KEY=ci-not-a-real-key \
    -e N8N_DIAGNOSTICS_ENABLED=false \
    -e N8N_PORT=5678 \
    "$@" \
    "$IMAGE" >/dev/null
  wait_ready "$name"
  docker rm -f "$name" >/dev/null
}

run_ready locus-n8n-nodes-on
run_ready locus-n8n-nodes-off -e N8N_CUSTOM_EXTENSIONS=/opt/locus-n8n-nodes-off
echo "n8n loaded Locus nodes and the empty off directory"
