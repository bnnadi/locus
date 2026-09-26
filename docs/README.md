# Locus Documentation

| Document | What it covers |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | Services, deployment model, data isolation, migrations, and the Hermes Memory Layer design |
| [troubleshooting.md](troubleshooting.md) | Known issues and symptom-to-cause tables across the stack |
| [deployment/hermes-memory-layer.md](deployment/hermes-memory-layer.md) | Deploy guide for Qdrant + `hermes-memory-router` |
| [runbooks/hermes-memory-runbook.md](runbooks/hermes-memory-runbook.md) | Day-to-day operations for the memory layer |
| [../services/hermes/README.md](../services/hermes/README.md) | Hermes gateway and shadow-team profiles (`researcher`, `spec`, `engineer`, `qa`) |
| [../services/authentik/README.md](../services/authentik/README.md) | Authentik IdP: nested `server/` and `worker/` Railway roots |
| [../services/uzora/README.md](../services/uzora/README.md) | Uzora token gate (JWT validation; `/mcp` is 501) |

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
                 and railway.json. Authentik nests server/ and worker/
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
- **Keep `ollama` and `hermes-memory-router` off public domains.** Ollama has
  no authentication. The router requires
  `Authorization: Bearer <HERMES_MEMORY_ROUTER_TOKEN>` on every route except
  `GET /health`, and `POST /traces` spends Anthropic credit per call. The
  Hermes dashboard is public and must have basic-auth (or OAuth/OIDC) set
  before the first deploy; the API on 8642 stays internal.
- **Keep `authentik-worker` and `uzora` off public domains.** The authentik
  **server** is public (login / OIDC / JWKS) only after
  `AUTHENTIK_BOOTSTRAP_PASSWORD`, `AUTHENTIK_BOOTSTRAP_EMAIL`, and
  `AUTHENTIK_BOOTSTRAP_TOKEN` are set on both processes — otherwise
  first-boot setup is an internet race. Confirm `/if/flow/initial-setup/`
  is closed before trusting the domain. Uzora is an internal token stub,
  not a cross-project MCP hop.

See [troubleshooting.md](troubleshooting.md) for the current list of known
bugs, several of which will bite on a first deploy.
