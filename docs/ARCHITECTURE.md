# Locus Architecture

Locus is the self-hostable service stack behind the MCP ecosystem: workflow
orchestration, local inference, and the datastores that back them. It is
infrastructure, not an application — nothing here owns product logic.

## Services

| Service | Role | Source | Public |
|---|---|---|---|
| `postgres` | Relational store; one database per consuming service | Railway template | No |
| `neo4j` | Graph store; shared with the Knowledge-Graph MCP | Railway template | No |
| `qdrant` | Vector index for semantic recall | `services/qdrant` | No |
| `ollama` | Local LLM inference | `services/ollama` | No |
| `n8n` | Workflow orchestration across MCP services | `services/n8n` | Yes |
| `hermes-memory-router` | Bridges Hermes to Neo4j + Qdrant | `services/hermes-memory-router` | No |

Only `n8n` is intended to have a public domain. Everything else is reachable
on the internal network exclusively — `ollama` and `hermes-memory-router` both
ship without authentication, and the router spends money per request, so a
public domain on either is a standing liability rather than a convenience.

## Deployment model

Two targets are supported deliberately, because the choice is not yet settled:

- **Railway.** Per-service config in `services/<name>/railway.json`, resolved
  from each service's Root Directory. `config/railway.yml` is a hand-maintained
  map of the intended topology — Railway does not read it.
- **Docker Compose.** Base definition at `docker-compose.yml`, development
  overrides at `config/docker-compose.override.yml`, passed explicitly with
  `-f` because Compose only auto-discovers an override sitting beside the base.

`scripts/deploy.sh` drives both and refuses to guess which one you meant.

## Data isolation

One Postgres instance, with a dedicated role and database per consuming
service created by `scripts/setup.sh`. Services share a host but never a
schema or a connection, so one service's migration or credential rotation
cannot reach another's tables.

This is a deliberate narrowing of the broader service-isolation rule (one
Postgres plugin per service). The tradeoff: a single instance means a single
blast radius for host-level failure, in exchange for one thing to back up and
one bill. Revisit if any consumer's load or uptime needs diverge.

## Migrations

`scripts/migration.sh --target postgres|neo4j` applies every pending migration
under `migrations/<target>/` in filename order and records what it applied —
`schema_migrations` in Postgres, `:_SchemaMigration` nodes in Neo4j.

Three properties worth knowing:

- **Re-running is safe.** Applied migrations are skipped by name.
- **Editing an applied migration is a hard error.** A checksum mismatch means
  the live schema and the repository have diverged; replaying the edited file
  would not reconcile them, so the script stops and asks for a new migration.
- **Postgres migrations are atomic with their bookkeeping row.** Both commit
  or neither does. Statements that cannot run in a transaction must declare
  `-- migration:no-transaction`, and those are recorded non-atomically.

`migrations/postgres/` is currently empty: n8n manages its own schema, and
nothing else in Locus owns relational tables yet.

## Hermes Memory Layer

Two services supporting Hermes Agent's self-improving reasoning loop.

```
Hermes Agent
    │  HTTP (plain REST, not MCP — single known caller)
    ▼
hermes-memory-router (FastAPI)
    │                    │
    ▼                    ▼
Neo4j (existing)     Qdrant (new)
deterministic spine   fast semantic recall
```

**Neo4j is the source of truth.** `ReasoningTrace` nodes are immutable — once
a task's reasoning is recorded it is never edited, only linked to from newly
derived `StrategyItem` nodes. Every strategy Hermes acts on traces back through
an explicit graph path to the trace that produced it.

**Qdrant is a derived index, not a second source of truth.** It stores
embeddings of `StrategyItem` text for fast top-k retrieval, and every point
payload carries `neo4j_node_id` so a result can be re-verified against the
graph. If Qdrant were lost entirely it could be rebuilt by re-embedding every
`StrategyItem` in Neo4j.

**Extraction backend is swappable per call.** `ollama` costs nothing beyond
compute and works offline; `claude` gives higher-quality extraction at API
cost; `hermes` keeps extraction inside Hermes' own auditable trace.

**REST, not MCP.** Single-consumer infrastructure, so MCP's discovery and
session machinery would not earn its overhead. Contrast the Knowledge-Graph
MCP, which is genuinely multi-tool and Cursor-facing. If a second client ever
needs these strategies, wrap the three endpoints then — not speculatively.

### Relationship to the Knowledge-Graph MCP

Both live on Neo4j but serve opposite write patterns: the Knowledge-Graph MCP
is human-curated with no auto-linking or scoring, while the Hermes Memory Layer
is auto-extracted from task execution. They may share the `CodePattern` label
on one instance — see the open questions below.

## Known open questions

1. **Deployment target.** Railway and Compose are both scaffolded; neither is
   authoritative yet. Ollama is the forcing function — Railway is CPU-only.
2. **`CodePattern` label sharing** between the Knowledge-Graph MCP and the
   Hermes Memory Layer on one Neo4j instance. Note that this graph has already
   had one index-versus-constraint name collision, which is exactly how a
   shared label bites.
3. **Strategy identity.** `StrategyItem` ids are currently derived from
   LLM-generated titles, which is nondeterministic and defeats the
   reinforce-rather-than-duplicate behavior the layer depends on. Unresolved;
   see `docs/troubleshooting.md`.
4. **Embedding model.** `all-MiniLM-L6-v2` (free, local, 384-dim) is the v0.1
   default. Changing it requires recreating the Qdrant collection, since stored
   vectors carry the old dimensionality.
