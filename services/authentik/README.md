# authentik

OSS IdP for the Locus stack. Image is `ghcr.io/goauthentik/server:2026.8.2`
(not Enterprise). One folder, two Railway Root Directories:

| Railway service | Root Directory | CMD |
|---|---|---|
| `authentik` | `services/authentik/server` | `server` |
| `authentik-worker` | `services/authentik/worker` | `worker` |

Do not set either service's Root Directory to `services/authentik`. There is
no `railway.json` here — a parent root would ignore both leaf configs.
Do not clone the server service to create the worker (that copies the HTTP
health probe and restart-loops the worker). Create an empty service and
point it at `services/authentik/worker`.

## What this is

Shared identity for later consumers (Hermes dashboard SSO, n8n, Uzora JWT
validation). It does not proxy MCP. Uzora talks to it over JWKS only and
never gets these database credentials.

The server is login, OIDC, and JWKS. The worker runs first-boot bootstrap,
emails, cert jobs, and queued tasks. Both use the same image pin and the
same `authentik` Postgres role — not a second database.

## Compose

`http://127.0.0.1:9000`. `scripts/deploy.sh --target compose --service
authentik` starts the server **and** the worker. No Redis (dropped in
2025.10+). No Docker socket — outposts are out of this slice.

Health is `ak healthcheck` on the server (the image does not ship `wget`).
The worker has no HTTP probe on Railway.

First boot: set `AUTHENTIK_BOOTSTRAP_PASSWORD`, `AUTHENTIK_BOOTSTRAP_EMAIL`,
and `AUTHENTIK_BOOTSTRAP_TOKEN` on **both** services **before** a public
domain is attached. Official docs apply those vars on the worker. A public
domain with `/if/flow/initial-setup/` still open is a race for the IdP
Uzora will trust.

Create an OIDC application and a client-credentials client in the admin
UI, then copy the issuer URL into `AUTHENTIK_ISSUER` for Uzora. Compose
does not require the issuer up front — that would deadlock first boot.

Set the brand / external URL in the admin UI so `iss` stays stable instead
of following whichever Host header hit the login.

## Railway

- Server: Root Directory `services/authentik/server`. Public domain **only
  after** bootstrap vars are set on server and worker and initial-setup is
  closed. Pin `PORT=9000`. Paste hex secrets, not the `$(openssl …)`
  generator.
- Worker: Root Directory `services/authentik/worker`. No public domain. No
  HTTP healthcheck.
- Do not attach a `/data` volume on either service. OIDC state lives in
  Postgres; Railway volumes are root-owned and this image runs as uid 1000
  (the n8n EACCES class). Media is unused in this slice.
- `numReplicas: 1` on both.
- `AUTHENTIK_ERROR_REPORTING__ENABLED=false`.

## Machine clients

Hermes has no TTY. Interactive OAuth parks the connection
(`OAuthNonInteractiveError`, 2026-08-21). Mint a client-credentials token
or an API token on a dedicated user and send it as `Authorization: Bearer`.
