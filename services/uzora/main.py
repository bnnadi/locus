"""Uzora token-gate FastAPI application.

Routes
------
GET  /health  — liveness probe, no auth.
GET  /whoami  — verify Bearer JWT and return decoded claims.
GET  /mcp     — 501 (not implemented).
POST /mcp     — 501 (not implemented).
"""

from __future__ import annotations

import json
from typing import Any

import auth
import httpx
import jwt
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from jwt.algorithms import RSAAlgorithm

app = FastAPI(title="Uzora Token Gate", docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/health")
async def health() -> dict[str, str]:
    """Liveness probe — no authentication required, never touches Authentik."""
    return {"status": "ok"}


@app.get("/whoami")
async def whoami(request: Request) -> JSONResponse:
    """Verify the Bearer JWT and return the decoded claims.

    Priority order (fail-closed):
    1. Missing or malformed Authorization header → 401.
    2. Incomplete Authentik settings              → 503.
    3. JWT verification failure                  → 401.
    4. Verification success                      → 200 with claims JSON.
    """
    # --- 1. Require a well-formed Bearer token header -----------------------
    authorization: str = request.headers.get("Authorization", "")
    if not authorization.startswith("Bearer "):
        return JSONResponse({"detail": "Missing or invalid Authorization header"}, status_code=401)

    token: str = authorization[len("Bearer "):].strip()
    if not token:
        return JSONResponse({"detail": "Missing or invalid Authorization header"}, status_code=401)

    # --- 2. Require complete Authentik settings (fail-closed) ---------------
    if not auth.settings_ready():
        return JSONResponse({"detail": "Service configuration incomplete"}, status_code=503)

    # --- 3. Reject a non-JWT before any Authentik I/O -----------------------
    try:
        unverified_header: dict[str, Any] = jwt.get_unverified_header(token)
    except jwt.PyJWTError:
        return JSONResponse({"detail": "Token verification failed"}, status_code=401)

    kid = unverified_header.get("kid")
    if not isinstance(kid, str) or not kid:
        return JSONResponse({"detail": "Token verification failed"}, status_code=401)

    # --- 4. Fetch JWKS and verify the token ---------------------------------
    try:
        jwks: dict[str, Any] = await auth.jwks_for_kid(  # type: ignore[arg-type]
            kid, auth.AUTHENTIK_URL, auth.AUTHENTIK_ISSUER
        )
        key_data: dict[str, Any] | None = auth.signing_key(jwks, kid)
        if key_data is None:
            return JSONResponse({"detail": "Token verification failed"}, status_code=401)

        public_key = RSAAlgorithm.from_jwk(json.dumps(key_data))
        claims: dict[str, Any] = jwt.decode(
            token,
            public_key,  # type: ignore[arg-type]
            algorithms=["RS256"],
            issuer=auth.AUTHENTIK_ISSUER,
            audience=auth.AUTHENTIK_AUDIENCE,
            options={"require": ["exp", "iss", "aud"]},
        )
        return JSONResponse(
            claims,
            status_code=200,
            headers={"Cache-Control": "no-store"},
        )
    except httpx.HTTPError:
        return JSONResponse({"detail": "Identity provider unavailable"}, status_code=503)
    except (jwt.PyJWTError, ValueError, KeyError, TypeError):
        return JSONResponse({"detail": "Token verification failed"}, status_code=401)


@app.get("/mcp")
async def mcp_get() -> JSONResponse:
    """MCP endpoint — not implemented."""
    return JSONResponse(
        {"detail": "MCP proxying is not implemented"},
        status_code=501,
    )


@app.post("/mcp")
async def mcp_post() -> JSONResponse:
    """MCP endpoint — not implemented."""
    return JSONResponse(
        {"detail": "MCP proxying is not implemented"},
        status_code=501,
    )
