# Troubleshooting

## Known open bugs

These are found and unfixed as of the last review. They are listed first
because several will bite on a first deploy and present as something else.

### Ollama crash-loops as soon as you ask it to preload a model

`services/ollama/entrypoint.sh` runs `ollama pull` before `ollama serve`. Pull
is a client command that talks to the daemon over `OLLAMA_HOST`, so with no
server running every pull fails to connect, and `set -e` exits the script on
the first one. The container exits nonzero and retries until it gives up.

It looks healthy while no models are configured, because the `if` guard skips
the loop entirely — so this surfaces the first time the feature is used.

Compounding: the variable it reads is `OLLAMA_MODELS`, which Ollama reserves
for the *path to the models directory*. `config/env.example` uses
`OLLAMA_PULL_MODELS` instead; the entrypoint has not been updated to match.

**Fix:** background `ollama serve`, poll until it answers, pull, then wait on
the server PID. Rename the variable to `OLLAMA_PULL_MODELS`.

### `POST /traces` creates a duplicate strategy every time

`strategy_id` is a hash of the LLM-generated title, and the Claude call sets no
temperature. Identical input produces a different title, a different id, and a
`MERGE` that inserts instead of matching. `success_count` never increments and
the accumulated `success_rate` fragments across near-identical nodes.

**Symptom you will actually see:** `tests/e2e/test_memory_loop_e2e.py` fails
intermittently on the assertion that a second trace reinforces the same
strategy. It looks like Qdrant lag; it is not.

### `success_rate` in the API response is always 1.0 or 0.0

`ingest_trace` returns the boolean `outcome == "success"` in the `success_rate`
field and Pydantic coerces it. The correct value is computed in Cypher and
written to Neo4j, but never read back. Query Neo4j directly for the real rate
until this is fixed.

### `min_success_rate=0.0` is silently ignored

The retrieval filter guards with `if req.min_success_rate`, which is falsy at
zero. Pass any nonzero threshold, or read the field from the returned strategy.

### Ollama port disagreement

`services/ollama/Dockerfile` sets `OLLAMA_HOST=0.0.0.0:8080`, not the upstream
default `11434`. Anything pointed at `11434` will get connection-refused.
`config/env.example` and `docker-compose.yml` both use `8080`; check any config
that predates that.

### Editing `services/ollama/entrypoint.sh` deploys without rebuilding

`services/ollama/railway.json` sets `watchPatterns: ["Dockerfile"]`, so changes
to the entrypoint are not a build trigger. You will deploy, see no error, and
keep running the old script. Set it to `["**"]`.

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
| Container runs as root | `services/n8n/Dockerfile` ends on `USER root` | Add the intended install steps and switch back to `USER node`, or drop both lines |

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
