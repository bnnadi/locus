"""Authentication helpers for the Uzora token-gate service.

Reads Authentik configuration from environment variables at import time.
No network I/O occurs at import; all network activity is isolated to
``fetch_jwks``.
"""

from __future__ import annotations

import os
from urllib.parse import urlparse, urlunparse

import httpx

# ---------------------------------------------------------------------------
# Module-level settings — read from env at import time.
# Empty string is treated the same as unset (both become None / falsy).
# ---------------------------------------------------------------------------

AUTHENTIK_URL: str | None = os.environ.get("AUTHENTIK_URL") or None
AUTHENTIK_ISSUER: str | None = os.environ.get("AUTHENTIK_ISSUER") or None
AUTHENTIK_AUDIENCE: str | None = os.environ.get("AUTHENTIK_AUDIENCE") or None


def settings_ready() -> bool:
    """Return True only when all three Authentik settings are non-empty.

    Returns:
        True when ``AUTHENTIK_URL``, ``AUTHENTIK_ISSUER``, and
        ``AUTHENTIK_AUDIENCE`` are all set to non-empty strings.
    """
    return bool(AUTHENTIK_URL and AUTHENTIK_ISSUER and AUTHENTIK_AUDIENCE)


def _rewrite_origin(url: str, authentik_url: str) -> str:
    """Replace ``url``'s scheme + host + port with those of ``authentik_url``.

    Args:
        url: The URL whose origin should be replaced.
        authentik_url: Source of the replacement origin.

    Returns:
        ``url`` with its origin swapped to that of ``authentik_url``.
    """
    parsed = urlparse(url)
    authentik = urlparse(authentik_url)
    rewritten = parsed._replace(scheme=authentik.scheme, netloc=authentik.netloc)
    return urlunparse(rewritten)


def fetch_jwks(authentik_url: str, issuer: str) -> dict:  # type: ignore[type-arg]
    """Fetch the JWKS for ``issuer`` via the internal ``authentik_url``.

    The issuer's path is rewritten onto ``authentik_url``'s origin so the
    request always goes to the internal container, never to the public-facing
    issuer host.  If the first response looks like an OIDC discovery document
    (contains a ``jwks_uri`` but no ``keys``), a second request is made to
    the JWKS endpoint — also rewritten to ``authentik_url``'s origin.

    HTTP calls are made through the module-level ``httpx.get`` so that tests
    can monkeypatch ``httpx.get`` directly to intercept them.

    Args:
        authentik_url: Internal Authentik base URL
            (e.g. ``http://authentik:9000``).
        issuer: OIDC issuer string
            (e.g. ``https://auth.example.com/application/o/locus/``).

    Returns:
        JWKS ``dict`` (``{"keys": [...]}``).

    Raises:
        httpx.HTTPStatusError: When any HTTP response indicates failure.
    """
    issuer_path = urlparse(issuer).path.rstrip("/")
    discovery_path = f"{issuer_path}/.well-known/openid-configuration"

    # Rewrite the discovery URL onto authentik_url's origin.
    authentik_parsed = urlparse(authentik_url)
    discovery_url = urlunparse(
        authentik_parsed._replace(path=discovery_path, query="", fragment="")
    )

    response = httpx.get(discovery_url, timeout=10.0, follow_redirects=False)
    response.raise_for_status()
    document: dict = response.json()  # type: ignore[assignment]

    keys = document.get("keys")
    if isinstance(keys, list):
        return document

    jwks_uri = document.get("jwks_uri")
    if not isinstance(jwks_uri, str) or not jwks_uri:
        raise ValueError("OIDC discovery document has no usable jwks_uri")

    jwks_url = _rewrite_origin(jwks_uri, authentik_url)
    jwks_response = httpx.get(jwks_url, timeout=10.0, follow_redirects=False)
    jwks_response.raise_for_status()
    jwks_document = jwks_response.json()
    if not isinstance(jwks_document, dict) or not isinstance(jwks_document.get("keys"), list):
        raise TypeError("JWKS response is not a keys list")
    return jwks_document
