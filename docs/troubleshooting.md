# Troubleshooting

## Remaining known issues

These are found and unfixed. They will not block a first Compose boot, but
they will bite under load or on Railway if ignored.

### Ollama port disagreement

`services/ollama/Dockerfile` sets `OLLAMA_HOST=0.0.0.0:8080`, not the upstream
default `11434`. Anything pointed at `11434` will get connection-refused.
`config/env.example` and `docker-compose.yml` both use `8080`; check any config
that predates that.

## Deploys

| Symptom | Likely cause | Fix |
|---|---|---|
| `railway.json` settings appear ignored | Service Root Directory is not `services/<name>` | Railway resolves config-as-code from the service root; set it in the dashboard |
| `scripts/deploy.sh` exits "target is required" | Neither `--target` nor `LOCUS_DEPLOY_TARGET` set | Deliberate — the script will not guess between Railway and Compose |
| Compose ignores the override file | It lives in `config/`, so it is not auto-discovered | Pass both files: `-f docker-compose.yml -f config/docker-compose.override.yml`, or use `deploy.sh` |
| Compose bind mount resolves to the wrong path | Relative paths resolve against the project directory, not the file's directory | Write override paths as `./services/...` from the repo root |
| Ollama inference is unusably slow on Railway | Railway has no GPU | Expected; CPU-only. Use small quantized models or self-host Ollama |

## Migrations

| Symptom | Likely cause | Fix |
|---|---|---|
| "changed after it was applied" | A migration file was edited after being applied | Intentional guard. Write a new migration to reconcile; do not edit history |
| "no `*.sql` migrations" but exit 0 | `migrations/postgres/` is legitimately empty | n8n owns its own schema; nothing else has relational tables yet |
| `CREATE DATABASE` fails inside a migration | Postgres cannot run it in a transaction | Add `-- migration:no-transaction` on its own line, and accept that it records non-atomically |
| Neo4j migration partially applied | `cypher-shell` runs statements individually, with no wrapping transaction | Each Neo4j migration must be individually idempotent — `IF NOT EXISTS` on everything |
| Constraint creation collides with an existing index | A constraint's backing index shares a name with one already present | Drop the index first, in the same migration |

## n8n

| Symptom | Likely cause | Fix |
|---|---|---|
| All credentials show as invalid after a deploy | `N8N_ENCRYPTION_KEY` unset, so a new key was generated | Set it explicitly. Credentials saved under a lost key are unrecoverable — re-enter them |
| Every scheduled workflow fires twice | More than one replica in non-queue mode | Keep `numReplicas: 1`; n8n runs its scheduler in-process |
| Healthcheck never passes | Checking `/healthz/readiness` while the database is unreachable | Correct behavior — readiness gates on a connected, migrated database. Fix the DB connection |

## Hermes memory layer

| Symptom | Likely cause | Fix |
|---|---|---|
| `POST /traces` returns 502 | Extraction backend unreachable, or an invalid Anthropic model id | Trace is still saved with `extraction_status: failed`, so it is retryable. Verify the model id before assuming the API is down |
| `POST /retrieve` returns empty | Collection empty, or the `task_type` filter is too narrow | Check `points_count`; retry without the filter |
| A strategy exists in Neo4j but has no vector | The Qdrant upsert failed after the Neo4j write committed | Look for `embedding_synced = false`. Nothing reconciles this automatically yet — re-POST the originating trace |
| Two strategies share one Qdrant point | Point IDs are a 32-bit hash; collisions start mattering in the low thousands | Migrate point IDs to `uuid5` over `strategy_id` and re-index |
| `conditions` is a dict from one endpoint and a string from another | Neo4j cannot store nested maps, so it is stored JSON-encoded and returned raw by `/retrieve` | Parse on read, or decode it in the retrieve handler |
| Router is slow to become healthy on a cold start | `SentenceTransformer` downloads the embedding model at import | Bake the model into the image, or mount a cache volume |

## Escalation

For anything touching data correctness — a wrong strategy injected into a
Hermes prompt, contradictory strategies not flagged — pull the full provenance
via `GET /trace/{id}/provenance` before making any manual Neo4j edit. The audit
trail is the point of the system; do not bypass it even while fixing it.
