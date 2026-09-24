"""Uzora JWT verification contract (services/uzora/auth.py + main.py).

Covers GET /whoami with a Bearer token once AUTHENTIK_URL / AUTHENTIK_ISSUER /
AUTHENTIK_AUDIENCE are all present (the 503 fail-closed-on-missing-settings
path is covered in test_uzora_http.py):

- A valid token (matching iss, aud, signature, not expired) -> 200 with a
  claims subset in the body, pinned to both "sub" == "user-42" and
  "iss" == the test issuer constant (not a soft "sub" or "iss" check).
- Wrong iss, wrong aud, expired, and bad signature -> 401 each. The aud check
  is never skipped.

No live Authentik: JWKS/discovery must be fetched via AUTHENTIK_URL, not by
hitting AUTHENTIK_ISSUER over the network. The route-level tests mock that
fetch seam directly (``auth.fetch_jwks``) rather than the real network call,
and pin a local RSA keypair + JWKS built in-test so nothing here depends on
a running IdP. ``test_fetch_jwks_requests_authentik_url_not_issuer_host``
below goes one level deeper and invokes ``auth.fetch_jwks`` itself with the
HTTP client mocked, to pin the seam's own contract (that it targets
AUTHENTIK_URL, never the issuer's host) rather than only asserting that
main.py calls *some* mockable function named ``fetch_jwks``.

Assumed contract (see test_uzora_config.py for the settings side):
- ``auth.py`` exposes module-level ``AUTHENTIK_URL`` / ``AUTHENTIK_ISSUER`` /
  ``AUTHENTIK_AUDIENCE`` and ``settings_ready() -> bool``.
- ``auth.py`` exposes ``fetch_jwks(authentik_url: str, issuer: str) -> dict``
  as the single network-touching function used during token verification;
  main.py's /whoami handler calls it (directly, or via a verify_token
  helper) only when settings_ready() is True and a Bearer token was
  supplied. Tests monkeypatch this function so no network call is ever made.
- GET /whoami returns the decoded claims (a dict/JSON object) on success,
  containing at least "sub" or "iss".

Expiry is tested with an explicit already-expired ``exp`` timestamp built at
token-creation time -- no wall-clock sleeping.
"""

from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any

import pytest
from fastapi.testclient import TestClient

# NOTE (CI collection, plan-critic C5): fastapi is installed in the CI
# `python` job's environment (via services/hermes-memory-router's
# requirements.txt) but `jwt` (PyJWT) and `cryptography` are NOT, so
# `pytest --collect-only` / `pytest tests/unit` must be able to import this
# module without them. `cryptography.hazmat...rsa`, `jwt.encode`, and
# `jwt.algorithms.RSAAlgorithm` are therefore imported lazily inside the
# helper functions that actually need them (_generate_rsa_key, mock_jwks,
# make_token) rather than at module level. The `RSAPrivateKey` type hint
# below is only imported under `TYPE_CHECKING` (never at runtime) so static
# analysis (ruff/mypy) still resolves it without requiring `cryptography` to
# actually be installed to import this module.
if TYPE_CHECKING:
    from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

_UZORA_DIR = Path(__file__).resolve().parents[2] / "services" / "uzora"

_ISSUER = "https://auth.example.com/application/o/locus/"
_WRONG_ISSUER = "https://auth.example.com/application/o/some-other-app/"
_AUDIENCE = "locus-client"
_WRONG_AUDIENCE = "some-other-client"
_KID = "test-signing-key-1"


def _load(name: str, filename: str) -> ModuleType:
    path = _UZORA_DIR / filename
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None, f"could not build spec for {path}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def load_uzora() -> tuple[ModuleType, ModuleType]:
    """(Re)load services/uzora/auth.py then main.py fresh from disk so
    main.py's `import auth` resolves to the freshly-loaded module and
    os.environ changes made in a test aren't shadowed by a cached import."""
    if str(_UZORA_DIR) not in sys.path:
        sys.path.insert(0, str(_UZORA_DIR))
    auth = _load("auth", "auth.py")
    main = _load("main", "main.py")
    return auth, main


@pytest.fixture
def uzora_env(monkeypatch: pytest.MonkeyPatch):
    """Configure a complete, valid Authentik settings triple for every test
    in this file (the incomplete-settings/503 path lives in
    test_uzora_http.py) and clean up loaded modules afterward."""
    monkeypatch.setenv("AUTHENTIK_URL", "http://authentik:9000")
    monkeypatch.setenv("AUTHENTIK_ISSUER", _ISSUER)
    monkeypatch.setenv("AUTHENTIK_AUDIENCE", _AUDIENCE)
    yield monkeypatch
    sys.modules.pop("auth", None)
    sys.modules.pop("main", None)


def _generate_rsa_key() -> RSAPrivateKey:
    from cryptography.hazmat.primitives.asymmetric import rsa

    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def mock_jwks(private_key: RSAPrivateKey, kid: str = _KID) -> dict[str, Any]:
    """Build a JWKS document (as Authentik would serve one) containing the
    public half of ``private_key`` under key id ``kid``."""
    import json

    from jwt.algorithms import RSAAlgorithm

    public_jwk = json.loads(RSAAlgorithm.to_jwk(private_key.public_key()))
    public_jwk["kid"] = kid
    public_jwk["use"] = "sig"
    public_jwk["alg"] = "RS256"
    return {"keys": [public_jwk]}


def make_token(
    private_key: RSAPrivateKey,
    *,
    iss: str = _ISSUER,
    aud: str = _AUDIENCE,
    sub: str = "user-42",
    kid: str = _KID,
    exp: int | None = None,
    extra_claims: dict[str, Any] | None = None,
) -> str:
    """Sign a JWT with ``private_key``. ``exp`` defaults to one hour from now;
    pass an explicit past timestamp to build an already-expired token."""
    from jwt import encode as jwt_encode

    now = int(time.time())
    payload: dict[str, Any] = {
        "iss": iss,
        "aud": aud,
        "sub": sub,
        "iat": now,
        "exp": exp if exp is not None else now + 3600,
    }
    if extra_claims:
        payload.update(extra_claims)
    return jwt_encode(payload, private_key, algorithm="RS256", headers={"kid": kid})


def _client_with_mocked_jwks(
    auth: ModuleType,
    main: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    jwks: dict[str, Any],
) -> TestClient:
    monkeypatch.setattr(auth, "fetch_jwks", lambda *args, **kwargs: jwks)
    return TestClient(main.app)


def test_whoami_valid_token_returns_200(
    uzora_env: pytest.MonkeyPatch, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = _generate_rsa_key()
    jwks = mock_jwks(key)
    token = make_token(key, sub="user-42")
    auth, main = load_uzora()
    client = _client_with_mocked_jwks(auth, main, monkeypatch, jwks)

    response = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200


def test_whoami_valid_token_returns_claims_subset(
    uzora_env: pytest.MonkeyPatch, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = _generate_rsa_key()
    jwks = mock_jwks(key)
    token = make_token(key, sub="user-42")
    auth, main = load_uzora()
    client = _client_with_mocked_jwks(auth, main, monkeypatch, jwks)

    response = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})

    body = response.json()
    assert body["sub"] == "user-42"
    assert body["iss"] == _ISSUER


def test_whoami_missing_bearer_returns_401(
    uzora_env: pytest.MonkeyPatch, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = _generate_rsa_key()
    jwks = mock_jwks(key)
    auth, main = load_uzora()
    client = _client_with_mocked_jwks(auth, main, monkeypatch, jwks)

    response = client.get("/whoami")

    assert response.status_code == 401


def test_whoami_malformed_authorization_header_returns_401(
    uzora_env: pytest.MonkeyPatch, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = _generate_rsa_key()
    jwks = mock_jwks(key)
    token = make_token(key)
    auth, main = load_uzora()
    client = _client_with_mocked_jwks(auth, main, monkeypatch, jwks)

    # Missing the "Bearer " scheme prefix entirely.
    response = client.get("/whoami", headers={"Authorization": token})

    assert response.status_code == 401


def test_whoami_wrong_issuer_returns_401(
    uzora_env: pytest.MonkeyPatch, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = _generate_rsa_key()
    jwks = mock_jwks(key)
    token = make_token(key, iss=_WRONG_ISSUER)
    auth, main = load_uzora()
    client = _client_with_mocked_jwks(auth, main, monkeypatch, jwks)

    response = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 401


def test_whoami_wrong_audience_returns_401(
    uzora_env: pytest.MonkeyPatch, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = _generate_rsa_key()
    jwks = mock_jwks(key)
    token = make_token(key, aud=_WRONG_AUDIENCE)
    auth, main = load_uzora()
    client = _client_with_mocked_jwks(auth, main, monkeypatch, jwks)

    response = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 401


def test_whoami_expired_token_returns_401(
    uzora_env: pytest.MonkeyPatch, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = _generate_rsa_key()
    jwks = mock_jwks(key)
    # Explicit past timestamp -- no sleeping to induce expiry.
    already_expired = int(time.time()) - 3600
    token = make_token(key, exp=already_expired)
    auth, main = load_uzora()
    client = _client_with_mocked_jwks(auth, main, monkeypatch, jwks)

    response = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 401


def test_whoami_bad_signature_returns_401(
    uzora_env: pytest.MonkeyPatch, monkeypatch: pytest.MonkeyPatch
) -> None:
    signing_key = _generate_rsa_key()
    other_key = _generate_rsa_key()
    # JWKS only advertises `signing_key`'s public half...
    jwks = mock_jwks(signing_key)
    # ...but the token is signed by a different, untrusted key under the same kid.
    token = make_token(other_key, kid=_KID)
    auth, main = load_uzora()
    client = _client_with_mocked_jwks(auth, main, monkeypatch, jwks)

    response = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 401


def test_whoami_unknown_kid_returns_401(
    uzora_env: pytest.MonkeyPatch, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = _generate_rsa_key()
    jwks = mock_jwks(key, kid=_KID)
    token = make_token(key, kid="a-kid-not-in-the-jwks")
    auth, main = load_uzora()
    client = _client_with_mocked_jwks(auth, main, monkeypatch, jwks)

    response = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 401


def test_whoami_does_not_fetch_jwks_over_the_network(
    uzora_env: pytest.MonkeyPatch, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verification must go through the fetch_jwks seam (mockable) rather
    than making a real network call directly against AUTHENTIK_ISSUER."""
    key = _generate_rsa_key()
    jwks = mock_jwks(key)
    token = make_token(key)
    auth, main = load_uzora()

    calls: list[tuple[str, str]] = []

    def _spy_fetch_jwks(authentik_url: str, issuer: str) -> dict[str, Any]:
        calls.append((authentik_url, issuer))
        return jwks

    monkeypatch.setattr(auth, "fetch_jwks", _spy_fetch_jwks)
    client = TestClient(main.app)

    response = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert calls, "fetch_jwks was never called during verification"
    called_authentik_url, _called_issuer = calls[0]
    assert called_authentik_url == "http://authentik:9000"


def test_fetch_jwks_requests_authentik_url_not_issuer_host(
    uzora_env: pytest.MonkeyPatch,
) -> None:
    """Pins the ``fetch_jwks`` seam itself, not just that main.py calls a
    mockable function of that name (the other tests in this file monkeypatch
    ``auth.fetch_jwks`` wholesale, so a real implementation that GETs
    AUTHENTIK_ISSUER instead of AUTHENTIK_URL would still pass every one of
    them).

    Assumed HTTP seam: ``fetch_jwks`` performs its request via
    ``httpx.get(url, ...)`` (module-level, not an ``httpx.Client`` instance).
    If the implementation instead uses ``httpx.Client(...).get(...)``, this
    test's mock of ``httpx.get`` will not intercept the call and the test
    will fail with "fetch_jwks did not perform an HTTP GET via httpx.get" --
    that failure is the signal to either switch this mock to
    ``httpx.Client.get`` or align the implementation, not to weaken this
    assertion.

    ``httpx`` is imported lazily here (see the CI-collection note near the
    top of this file) since it is not guaranteed to be installed at
    collection time.
    """
    import httpx

    authentik_url = "http://authentik:9000"
    # Deliberately a different host than authentik_url, matching how
    # Authentik's internal container URL and its public-facing issuer
    # commonly differ.
    issuer = "https://auth.example.com/application/o/locus/"
    requested_urls: list[str] = []

    class _FakeResponse:
        def json(self) -> dict[str, Any]:
            return {"keys": []}

        def raise_for_status(self) -> None:
            return None

    def _fake_httpx_get(url: str, *args: Any, **kwargs: Any) -> _FakeResponse:
        requested_urls.append(url)
        return _FakeResponse()

    uzora_env.setattr(httpx, "get", _fake_httpx_get)
    auth, _main = load_uzora()

    auth.fetch_jwks(authentik_url, issuer)

    assert requested_urls, "fetch_jwks did not perform an HTTP GET via httpx.get"
    requested_url = requested_urls[0]
    assert "authentik:9000" in requested_url, (
        f"expected the JWKS request host to come from AUTHENTIK_URL "
        f"({authentik_url!r}), got {requested_url!r}"
    )
    assert "auth.example.com" not in requested_url, (
        f"fetch_jwks must not derive its request host from the issuer "
        f"({issuer!r}) -- got {requested_url!r}"
    )
