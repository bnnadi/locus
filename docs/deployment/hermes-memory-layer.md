# Hermes Memory Layer — Deployment Guide

Neo4j + Qdrant hybrid memory for Hermes Agent's task-execution learning loop.
Deterministic/auditable spine (Neo4j) + fast semantic recall (Qdrant), with a
backend-swappable extraction router (Claude API / Ollama / Hermes subagent).

## Components added to Locus

| Component | Type | New/Existing |
|---|---|---|
| Qdrant | Vector store service | New |
| hermes-memory-router | FastAPI service | New |
| Neo4j | Graph DB | Existing (schema extended via migration) |
| Postgres | — | Not used by this component |

## Prerequisites

- Existing Neo4j instance reachable at `neo4j.railway.internal:7687`
- Existing Ollama instance (if using `ollama` extraction backend)
- Anthropic API key (if using `claude` extraction backend)

## Deploy steps

1. Merge `config/hermes-memory.env.example` into `config/env.example`, fill
   in real values in Railway's env var UI (never commit secrets).
2. Merge `config/hermes-memory.railway.yml` service definitions into
   `config/railway.yml`.
3. Run the Neo4j migration:
   ```bash
   ./scripts/migration.sh --target neo4j --file migrations/neo4j/001_hermes_memory_schema.cypher
   ```
4. Deploy:
   ```bash
   ./scripts/maintenance/deploy_hermes_memory.sh
   ```
   (Once verified, fold these steps into the top-level `scripts/deploy.sh`.)
5. Smoke test:
   ```bash
   HERMES_MEMORY_ROUTER_URL=https://<router-public-url> \
     ./scripts/maintenance/smoke_test_hermes_memory.sh
   ```

## Cost

~$35–45/mo incremental (Qdrant ~$25–30/mo, router ~$8–12/mo). Neo4j and
Postgres are already provisioned Locus services — no new cost there.

## Schema

Full Neo4j node/relationship schema, Qdrant collection schema, and API
contract for `hermes-memory-router` are documented in the router's
`main.py` docstrings and in `migrations/neo4j/001_hermes_memory_schema.cypher`.
Summary:

- **`ReasoningTrace`** (Neo4j, immutable) — one node per task execution
- **`StrategyItem`** (Neo4j, append-only counts) — distilled reusable strategy
- **`DERIVES_STRATEGY`** edge links a trace to the strategy it produced/reinforced
- **`CONTRADICTS`** edge flags conflicting strategies for manual review
- Qdrant `hermes_memory` collection embeds `StrategyItem` text for fast
  top-k recall; every point payload carries `neo4j_node_id` for enrichment

## Hermes Agent integration

Hermes calls the router over plain HTTP (not MCP — see
`docs/runbooks/hermes-memory-runbook.md` for the rationale):

- `POST /traces` — post-task, ingest a reasoning trace + trigger extraction
- `POST /retrieve` — pre-task, fetch the top-k (default k=1) relevant
  strategy with full Neo4j provenance
- `GET /trace/{id}/provenance` — on-demand full audit trail for one trace

## Open questions (resolve before broad rollout)

1. **`CodePattern` node sharing** with the Cursor-facing Knowledge-Graph MCP:
   same Neo4j label/instance (recommended) or namespaced/duplicated?
2. **Contradiction resolution**: no auto-override — `CONTRADICTS` edges are
   surfaced for manual review, consistent with the Knowledge-Graph MCP's
   "store what you put in, no auto-scoring" design.
3. **Embedding model**: `all-MiniLM-L6-v2` (free, local, 384-dim) is the v0.1
   default. Revisit after real usage data if retrieval quality is
   insufficient — note this requires recreating the Qdrant collection.
