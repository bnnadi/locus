# uzora

Token gate in front of authentik-issued JWTs. v0 is **not** an MCP proxy:
`/mcp` returns 501. Nothing in Locus presents these tokens yet; this service
is a documented stub, not a cross-project hop. Locus private DNS cannot
reach MCP servers in other Railway projects.

## Endpoints

| Path | Auth | Behavior |
|---|---|---|
| `GET /health` | none | 200; does not contact authentik |
| `GET /whoami` | Bearer JWT | 200 + claims, or 401 / 503 |
| `GET`/`POST /mcp` | none | 501, body says not implemented |

Missing `Authorization` is always 401. A Bearer token with
`AUTHENTIK_URL` / `AUTHENTIK_ISSUER` / `AUTHENTIK_AUDIENCE` unset is 503
(fail-closed at request time so Compose can boot authentik first).

JWKS is fetched from `AUTHENTIK_URL` (in-network). `iss` / `aud` are
checked against `AUTHENTIK_ISSUER` / `AUTHENTIK_AUDIENCE`. Uzora never
receives authentik database credentials.

## Env

| Variable | Role |
|---|---|
| `AUTHENTIK_URL` | Origin used to fetch discovery/JWKS. Compose: `http://authentik:9000`. Railway: `http://authentik.railway.internal:9000` |
| `AUTHENTIK_ISSUER` | Full issuer; must match token `iss`. Empty until the OIDC app exists |
| `AUTHENTIK_AUDIENCE` | Must match token `aud` |

Machine clients: authentik client-credentials or an API token, sent as
`Authorization: Bearer`. Do not send Hermes through interactive OAuth
(`OAuthNonInteractiveError`, 2026-08-21).

Keep this service off a public domain. Pin Railway `PORT=8100`.
