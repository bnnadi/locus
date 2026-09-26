#!/usr/bin/env bash
# Smoke test for the Hermes Memory Layer router.
# Usage:
#   HERMES_MEMORY_ROUTER_URL=https://... \
#   HERMES_MEMORY_ROUTER_TOKEN=<secret> \
#   ./scripts/maintenance/smoke_test_hermes_memory.sh

set -euo pipefail

ROUTER_URL="${HERMES_MEMORY_ROUTER_URL:?Set HERMES_MEMORY_ROUTER_URL}"
# Token is required for all protected routes. Never echoed.
: "${HERMES_MEMORY_ROUTER_TOKEN:?Set HERMES_MEMORY_ROUTER_TOKEN}"
TRACE_ID="trace_smoketest_$(date +%s)"

echo "== Checking router health (no auth required) =="
curl -sf "${ROUTER_URL}/health"
echo

echo "== Ingesting a test trace =="
curl -sf -X POST "${ROUTER_URL}/traces" \
  -H "Authorization: Bearer ${HERMES_MEMORY_ROUTER_TOKEN}" \
  -H "Content-Type: application/json" \
  -d "{
    \"trace_id\": \"${TRACE_ID}\",
    \"task_id\": \"smoketest\",
    \"task_type\": \"code_review\",
    \"raw_reasoning\": \"Used a guard clause instead of nested if statements to reduce cyclomatic complexity.\",
    \"outcome\": \"success\"
  }" | tee /tmp/hermes_smoke_ingest.json
echo

echo "== Retrieving via vector search =="
curl -sf -X POST "${ROUTER_URL}/retrieve" \
  -H "Authorization: Bearer ${HERMES_MEMORY_ROUTER_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"query": "how to reduce complexity in validation code", "k": 1}' | tee /tmp/hermes_smoke_retrieve.json
echo

echo "== Fetching provenance (deterministic path) =="
curl -sf "${ROUTER_URL}/trace/${TRACE_ID}/provenance" \
  -H "Authorization: Bearer ${HERMES_MEMORY_ROUTER_TOKEN}" | tee /tmp/hermes_smoke_provenance.json
echo

echo "Smoke test complete. Verify /tmp/hermes_smoke_*.json for correctness."
