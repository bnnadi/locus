"""Authentication helpers for the Uzora token-gate service.

Reads Authentik configuration from environment variables at import time.
No network I/O occurs at import; all network activity is isolated to
``fetch_jwks``.
"""

from __future__ import annotations

import asyncio
import os
import time
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


# ---------------------------------------------------------------------------
# JWKS cache — module-level state, lock, and clock seam.
# ---------------------------------------------------------------------------

_TTL: float = 600.0
_COOLDOWN: float = 60.0

# Cached state: None timestamps mean "never set", never 0.
_jwks_document: dict | None = None  # type: ignore[type-arg]
_fetched_at: float | None = None
_miss_refresh_at: float | None = None

# Single asyncio.Lock created at import; tests reload the module to get a
# fresh lock per test. The lock attaches to the running event loop on first
# acquire (Python 3.10+ — no loop binding at creation time).
_jwks_lock: asyncio.Lock = asyncio.Lock()


class _Clock:
    """Monotonic clock with a replaceable ``now()`` seam.

    Tests replace the module-level ``auth._clock`` instance with a
    ``_FakeClock`` to control time deterministically.
    """

    def now(self) -> float:
        """Return the current monotonic time as a float."""
        return time.monotonic()


_clock: _Clock = _Clock()


def signing_key(jwks: dict, kid: str) -> dict | None:  # type: ignore[type-arg]
    """Find a usable signing key in ``jwks`` for ``kid``.

    A key is accepted when it is a ``dict``, its ``kid`` matches, its
    ``use`` field is ``"sig"`` (or absent, defaulting to ``"sig"``), and its
    ``alg`` field is ``"RS256"`` (or absent, defaulting to ``"RS256"``).

    Args:
        jwks: JWKS document (``{"keys": [...]}``)
        kid: Key ID to look up.

    Returns:
        The matching JWK ``dict``, or ``None`` if no usable key is found.
    """
    for k in jwks.get("keys", []):
        if (
            isinstance(k, dict)
            and k.get("kid") == kid
            and k.get("use", "sig") == "sig"
            and k.get("alg", "RS256") == "RS256"
        ):
            return k
    return None


async def jwks_for_kid(kid: str, authentik_url: str, issuer: str) -> dict:  # type: ignore[type-arg]
    """Return the cached JWKS document, refreshing via ``fetch_jwks`` when needed.

    Implements a 600-second TTL cache with one global 60-second cooldown so
    unknown kids cannot each trigger a fetch.  The asyncio lock serialises
    all decisions so concurrent cold requests coalesce into a single fetch.

    Decision order (all under ``_jwks_lock``):

    1. Re-check all state after acquiring the lock (the coalescing join).
    2. Fresh cache *and* ``signing_key`` finds ``kid``: return the document.
    3. Fresh cache, ``kid`` absent, cooldown active: return the document.
    4. Fresh cache, ``kid`` absent, cooldown inactive: one fetch, store,
       arm cooldown regardless of whether the new document contains ``kid``,
       return the document.
    5. Cache empty or stale: one fetch, store; arm cooldown only when
       ``signing_key`` does *not* find ``kid`` in the new document; return it.

    On exception from ``fetch_jwks`` no state is updated and the exception
    propagates — a failed fetch is never cached and never arms the cooldown.

    Args:
        kid: JWT key ID to locate.
        authentik_url: Internal Authentik base URL.
        issuer: OIDC issuer string.

    Returns:
        The current JWKS document (may or may not contain ``kid``).

    Raises:
        httpx.HTTPError: Propagated unchanged from ``fetch_jwks`` on failure.
    """
    global _jwks_document, _fetched_at, _miss_refresh_at

    async with _jwks_lock:
        now = _clock.now()
        is_fresh = _fetched_at is not None and (now - _fetched_at) < _TTL

        if is_fresh:
            # _fetched_at is set only together with _jwks_document. An explicit
            # raise keeps that invariant under python -O, which strips assert.
            if _jwks_document is None:
                raise RuntimeError("JWKS cache is fresh but has no document")

            # Rule 2: fresh and kid present — no fetch.
            if signing_key(_jwks_document, kid) is not None:
                return _jwks_document

            # Rule 3: fresh, kid absent, cooldown active — no fetch.
            cooldown_active = (
                _miss_refresh_at is not None and (now - _miss_refresh_at) < _COOLDOWN
            )
            if cooldown_active:
                return _jwks_document

            # Rule 4: fresh, kid absent, cooldown inactive — one fetch.
            # Cooldown is armed regardless of whether the new document has kid.
            new_doc = await asyncio.to_thread(fetch_jwks, authentik_url, issuer)
            _jwks_document = new_doc
            _fetched_at = now
            _miss_refresh_at = now
            return _jwks_document

        # Rule 5: cache empty or stale — one fetch.
        # Arm cooldown only when the new document does not contain kid.
        new_doc = await asyncio.to_thread(fetch_jwks, authentik_url, issuer)
        _jwks_document = new_doc
        _fetched_at = now
        if signing_key(new_doc, kid) is None:
            _miss_refresh_at = now
        return _jwks_document
