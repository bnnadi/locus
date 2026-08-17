#!/usr/bin/env bash
# Deploys the Hermes Memory Layer (Qdrant + hermes-memory-router) to Railway.
# Assumes Neo4j and Postgres are already running as existing Locus services.
#
# Usage: ./scripts/maintenance/deploy_hermes_memory.sh [--skip-migration]
#
# NOTE: This is a standalone script for this component. Once reviewed, fold
# the relevant steps into the top-level scripts/deploy.sh so this component
# deploys as part of the normal Locus deploy flow rather than separately.

set -euo pipefail

SKIP_MIGRATION=false
if [[ "${1:-}" == "--skip-migration" ]]; then
  SKIP_MIGRATION=true
fi

echo "== Hermes Memory Layer: deploy =="

echo "-- Deploying Qdrant service"
railway up --service qdrant --detach services/qdrant

echo "-- Deploying hermes-memory-router service"
railway up --service hermes-memory-router --detach services/hermes-memory-router

if [[ "$SKIP_MIGRATION" == false ]]; then
  echo "-- Running Neo4j schema migration (001_hermes_memory_schema.cypher)"
  ./scripts/migration.sh --target neo4j --file migrations/neo4j/001_hermes_memory_schema.cypher
else
  echo "-- Skipping migration (--skip-migration set)"
fi

echo "-- Waiting for router health check"
ROUTER_URL="${HERMES_MEMORY_ROUTER_URL:?Set HERMES_MEMORY_ROUTER_URL before running}"
for i in $(seq 1 12); do
  if curl -sf "${ROUTER_URL}/health" > /dev/null; then
    echo "Router healthy."
    exit 0
  fi
  echo "  waiting (${i}/12)..."
  sleep 5
done

echo "ERROR: router did not report healthy after 60s" >&2
exit 1
