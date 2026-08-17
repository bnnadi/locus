# Locus Documentation

| Document | What it covers |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | Services, deployment model, data isolation, migrations, and the Hermes Memory Layer design |
| [troubleshooting.md](troubleshooting.md) | Known issues and symptom-to-cause tables across the stack |
| [deployment/hermes-memory-layer.md](deployment/hermes-memory-layer.md) | Deploy guide for Qdrant + `hermes-memory-router` |
| [runbooks/hermes-memory-runbook.md](runbooks/hermes-memory-runbook.md) | Day-to-day operations for the memory layer |

## Getting started

```bash
# 1. Create .env and bootstrap per-service roles and databases
ADMIN_DATABASE_URL=postgres://... scripts/setup.sh

# 2. Bring the stack up locally
scripts/deploy.sh --target compose

# 3. Or deploy to Railway
scripts/deploy.sh --target railway
```

`scripts/setup.sh` is safe to re-run: it leaves an existing `.env` alone,
creates roles and databases only when absent, and skips migrations that have
already been applied.

## Repository layout

```
services/        One directory per built service; each carries its Dockerfile
                 and, for Railway, its railway.json
scripts/         setup.sh, deploy.sh, migration.sh, plus maintenance/
config/          env.example, railway.yml (reference only), Compose override
migrations/      postgres/ and neo4j/, applied in filename order
tests/           unit/ (no live stack), plus integration/ and e2e/ against a deployment
docs/            This directory
```

## Before you deploy anything

Two settings will cost you data or money if missed, and neither is enforced by
code:

- **`N8N_ENCRYPTION_KEY` must be set before you create any n8n credential.**
  n8n generates a new key whenever its data directory is empty, which makes
  every credential saved under the old key permanently undecryptable. Using
  Postgres as the backend does not protect you — the key lives on disk.
- **Keep `ollama` and `hermes-memory-router` off public domains.** Neither has
  authentication, and `POST /traces` on the router spends Anthropic credit per
  call.

See [troubleshooting.md](troubleshooting.md) for the current list of known
bugs, several of which will bite on a first deploy.
