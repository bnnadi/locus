# Locus

Self-hostable service stack behind the MCP ecosystem: workflow orchestration
(n8n), local inference (Ollama), and the datastores that back them (Postgres,
Neo4j, Qdrant), plus a router bridging Hermes Agent to its memory layer.

Infrastructure only — no product logic lives here.

## Quick start

```bash
# Bootstrap .env plus per-service Postgres roles and databases
ADMIN_DATABASE_URL=postgres://... scripts/setup.sh

# Bring the stack up locally
scripts/deploy.sh --target compose
```

Both scripts are safe to re-run. See [docs/README.md](docs/README.md) for the
full documentation index and [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for
how the pieces fit together.

## Layout

```
services/     One directory per built service, each with its Dockerfile
scripts/      setup.sh, deploy.sh, migration.sh, and maintenance/
config/       env.example, railway.yml, Compose override
migrations/   postgres/ and neo4j/, applied in filename order
tests/        integration/ and e2e/, both need a live deployment
docs/         Architecture, runbooks, troubleshooting
```

## Status

Pre-deployment. The tree is scaffolded and the Hermes memory layer is written
but unverified against a running stack. There are known open bugs that will
bite on a first deploy — read
[docs/troubleshooting.md](docs/troubleshooting.md) before deploying, in
particular the Ollama entrypoint crash loop and the `N8N_ENCRYPTION_KEY`
requirement.

The Railway-versus-Compose decision is deliberately still open; both paths are
scaffolded and `scripts/deploy.sh` refuses to guess between them.
