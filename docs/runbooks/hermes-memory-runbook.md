# Hermes Memory Layer — Runbook

## Is this an MCP?

No. It's plain REST infrastructure (FastAPI), same category as n8n or
Ollama in this repo. Hermes is the single known caller with a fixed set of
operations, so a direct HTTP API is simpler and lower-latency than MCP's
discovery/session overhead. If a second client (e.g. Cursor) later needs to
read Hermes' learned strategies, wrap the three endpoints as MCP tools at
that point — don't build the MCP layer speculatively.

## Common operations

### Check router health
```bash
curl "${HERMES_MEMORY_ROUTER_URL}/health"
```

### Inspect a strategy's full provenance
```bash
curl "${HERMES_MEMORY_ROUTER_URL}/trace/<trace_id>/provenance"
```
Or directly in Neo4j Browser:
```cypher
MATCH (rt:ReasoningTrace {id: "<trace_id>"})-[:DERIVES_STRATEGY]->(s:StrategyItem)
OPTIONAL MATCH (s)-[c:CONTRADICTS]->(other:StrategyItem)
RETURN rt, s, c, other
```

### Re-sync a strategy to Qdrant (if `embedding_synced = false`)
```cypher
MATCH (s:StrategyItem) WHERE s.embedding_synced = false RETURN s.id, s.title
```
Then re-POST the originating trace to `/traces`, or write a one-off backfill
script following the embed/upsert block in `main.py`.

### Switch extraction backend for a single call (without redeploying)
```bash
curl -X POST "${HERMES_MEMORY_ROUTER_URL}/traces" \
  -H "Content-Type: application/json" \
  -d '{ ..., "backend": "ollama" }'
```

### Review recently extracted strategies before trusting auto-injection
```cypher
MATCH (s:StrategyItem)
RETURN s.title, s.success_rate, s.last_validated
ORDER BY s.last_validated DESC
LIMIT 20
```

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `POST /traces` returns 502 | Extraction backend unreachable (Claude API down, Ollama not running, Hermes endpoint misconfigured) | Check `extraction_backend` env var and target service health; trace is still saved in Neo4j with `extraction_status: failed` — safe to retry |
| `POST /retrieve` returns empty results | Qdrant collection empty or `task_type` filter too narrow | Check `qdrant.get_collection("hermes_memory").points_count`; drop the `task_type` filter to confirm |
| Qdrant point exists but Neo4j lookup fails in `/retrieve` | `neo4j_node_id` payload drifted from actual Neo4j `id` | Should not happen under normal operation (single write path in `/traces`); if seen, treat as a bug — the point ID generation and Neo4j ID generation must stay in sync (see `strategy_id_from_title`) |
| Router won't start | Neo4j or Qdrant unreachable at startup (`ensure_collection` hook fails) | Confirm `NEO4J_URI`/`QDRANT_URL` and that both dependency services are healthy before router starts (`dependsOn` in railway.yml) |

## Escalation path

For anything touching data correctness (wrong strategy injected into a
Hermes prompt, contradictory strategies not flagged), pull the full
provenance via `/trace/{id}/provenance` before making any manual Neo4j
edits — the audit trail is the point of this system; don't bypass it even
when fixing it.
