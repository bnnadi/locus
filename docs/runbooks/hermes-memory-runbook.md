# Hermes Memory Layer — Runbook

## Is this an MCP?

No. It's plain REST infrastructure (FastAPI), same category as n8n or
Ollama in this repo. Hermes is the single known caller with a fixed set of
operations, so a direct HTTP API is simpler and lower-latency than MCP's
discovery/session overhead. If a second client (e.g. Cursor) later needs to
read Hermes' learned strategies, wrap the three endpoints as MCP tools at
that point — don't build the MCP layer speculatively.

## Authentication

All routes except `GET /health` require:

```
Authorization: Bearer <HERMES_MEMORY_ROUTER_TOKEN>
```

A missing header, wrong secret, empty bearer, non-Bearer scheme, or
trailing-whitespace bearer returns `401 Unauthorized`. The token is read
once at startup; the router refuses to start if it is unset or
whitespace-only.

## Common operations

### Check router health (no auth required)
```bash
curl "${HERMES_MEMORY_ROUTER_URL}/health"
```

### Inspect a strategy's full provenance
```bash
curl -H "Authorization: Bearer ${HERMES_MEMORY_ROUTER_TOKEN}" \
  "${HERMES_MEMORY_ROUTER_URL}/trace/<trace_id>/provenance"
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
Then re-POST the originating trace to `/traces` (with the bearer), or write
a one-off backfill script following the embed/upsert block in `main.py`.

> **Note:** Do not re-POST `/traces` to unstick a *quarantined* strategy.
> Quarantined strategies return `409 strategy quarantined` and no extraction
> runs. Remove the quarantine flag via Neo4j directly or delete the strategy
> first.

### Switch extraction backend for a single call (without redeploying)
```bash
curl -X POST "${HERMES_MEMORY_ROUTER_URL}/traces" \
  -H "Authorization: Bearer ${HERMES_MEMORY_ROUTER_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{ ..., "backend": "ollama" }'
```

### Quarantine a strategy (stop it being retrieved or reinforced)
```bash
curl -X POST "${HERMES_MEMORY_ROUTER_URL}/strategies/<strategy_id>/quarantine" \
  -H "Authorization: Bearer ${HERMES_MEMORY_ROUTER_TOKEN}"
```

This sets `quarantined = true` on the Neo4j node and removes the Qdrant
point. A `503` means the Qdrant delete failed; the node is already
quarantined in Neo4j — repeat the call to retry the Qdrant step. The
`ReasoningTrace` nodes are never removed.

### Delete a strategy permanently
```bash
curl -X DELETE "${HERMES_MEMORY_ROUTER_URL}/strategies/<strategy_id>" \
  -H "Authorization: Bearer ${HERMES_MEMORY_ROUTER_TOKEN}"
```

Deletes the Qdrant point first, then `DETACH DELETE`s the `StrategyItem`
node. `ReasoningTrace` nodes are preserved. A `503` means the Qdrant delete
failed and the Neo4j node was not touched — repeat to retry.

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
| Any protected route returns `401` | Missing or wrong `Authorization: Bearer` header | Pass `Authorization: Bearer <HERMES_MEMORY_ROUTER_TOKEN>`; check the token matches what the service started with |
| Router won't start (`RuntimeError: HERMES_MEMORY_ROUTER_TOKEN`) | Token env var unset or whitespace-only | Set `HERMES_MEMORY_ROUTER_TOKEN` to a non-empty value |
| `POST /traces` returns 409 `strategy quarantined` | The strategy or the trace's linked strategy is quarantined | Do not re-POST to unstick; remove the quarantine via Neo4j or delete the strategy |
| `POST /traces` returns 502 | Extraction backend unreachable (Claude API down, Ollama not running, Hermes endpoint misconfigured) | Check `extraction_backend` env var and target service health; trace is still saved in Neo4j with `extraction_status: failed` — safe to retry |
| `POST /retrieve` returns empty results | Qdrant collection empty or `task_type` filter too narrow | Check `qdrant.get_collection("hermes_memory").points_count`; drop the `task_type` filter to confirm |
| Quarantine or delete returns 503 | Qdrant unreachable during point delete | Repeat the call; Qdrant delete is always retried, never skipped |
| Qdrant point exists but Neo4j lookup fails in `/retrieve` | `neo4j_node_id` payload drifted from actual Neo4j `id` | Should not happen under normal operation (single write path in `/traces`); if seen, treat as a bug — the point ID generation and Neo4j ID generation must stay in sync (see `strategy_id_from_title`) |
| Router won't start | Neo4j or Qdrant unreachable at startup (`ensure_collection` fails) | Confirm `NEO4J_URI`/`QDRANT_URL` and that both dependency services are healthy before router starts (`dependsOn` in railway.yml) |

## Escalation path

For anything touching data correctness (wrong strategy injected into a
Hermes prompt, contradictory strategies not flagged), pull the full
provenance via `/trace/{id}/provenance` before making any manual Neo4j
edits — the audit trail is the point of this system; don't bypass it even
when fixing it.
