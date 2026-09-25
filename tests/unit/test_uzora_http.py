"""Uzora HTTP routing contract (services/uzora/main.py).

Uzora is a FastAPI token gate, not an MCP proxy. This file pins the
route-level contract from the amended plan (plan-critic C1/C5/C7):

- GET /health: 200, no auth, must not touch Authentik / fetch JWKS, and must
  work even when AUTHENTIK_URL / AUTHENTIK_ISSUER / AUTHENTIK_AUDIENCE are
  all unset.
- GET /whoami without a Bearer token: 401, even when AUTHENTIK_* settings are
  also missing -- the missing-header check wins over the settings check.
- GET /whoami with a Bearer token but incomplete Authentik settings
  (AUTHENTIK_URL / AUTHENTIK_ISSUER / AUTHENTIK_AUDIENCE missing/empty): 503
  (fail-closed), never 200 and never an empty-success response.
- GET and POST /mcp: 501, with a response body that says MCP proxying is not
  implemented (an honest refusal, not an empty 200).

Full JWT verification success/failure paths (valid token, wrong iss/aud,
expired, bad signature) live in test_uzora_jwt.py.

Assumed contract (see test_uzora_config.py for the settings side):
- ``auth.py`` exposes module-level ``AUTHENTIK_URL`` / ``AUTHENTIK_ISSUER`` /
  ``AUTHENTIK_AUDIENCE`` and ``settings_ready() -> bool``.
- ``auth.py`` exposes ``fetch_jwks(authentik_url: str, issuer: str) -> dict``
  as the *only* network-touching function used to verify a token; nothing
  else in the request path performs I/O.

Modules are loaded fresh via importlib.util.spec_from_file_location (per
tests/unit/test_strategy_identity.py's pattern), never via a top-level
`from main import app`, so collecting this file never requires AUTHENTIK_*
env vars to be present.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest
from fastapi.testclient import TestClient

_UZORA_DIR = Path(__file__).resolve().parents[2] / "services" / "uzora"


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
    for key in ("AUTHENTIK_URL", "AUTHENTIK_ISSUER", "AUTHENTIK_AUDIENCE"):
        monkeypatch.delenv(key, raising=False)
    yield monkeypatch
    sys.modules.pop("auth", None)
    sys.modules.pop("main", None)


def test_health_all_env_unset_returns_200(uzora_env: pytest.MonkeyPatch) -> None:
    _, main = load_uzora()
    client = TestClient(main.app)

    response = client.get("/health")

    assert response.status_code == 200


def test_health_does_not_require_bearer_token(uzora_env: pytest.MonkeyPatch) -> None:
    uzora_env.setenv("AUTHENTIK_URL", "http://authentik:9000")
    uzora_env.setenv("AUTHENTIK_ISSUER", "https://auth.example.com/application/o/locus/")
    uzora_env.setenv("AUTHENTIK_AUDIENCE", "locus-client")
    _, main = load_uzora()
    client = TestClient(main.app)

    response = client.get("/health")

    assert response.status_code == 200


def test_health_all_env_empty_string_returns_200(uzora_env: pytest.MonkeyPatch) -> None:
    """A process where every AUTHENTIK_* var is explicitly set to the empty
    string (not merely absent -- see test_uzora_config.py's empty-string
    process-start tests) must still serve /health as 200."""
    uzora_env.setenv("AUTHENTIK_URL", "")
    uzora_env.setenv("AUTHENTIK_ISSUER", "")
    uzora_env.setenv("AUTHENTIK_AUDIENCE", "")
    _, main = load_uzora()
    client = TestClient(main.app)

    response = client.get("/health")

    assert response.status_code == 200


def test_health_does_not_call_fetch_jwks(
    uzora_env: pytest.MonkeyPatch, monkeypatch: pytest.MonkeyPatch
) -> None:
    """/health must never touch Authentik, even when settings are complete."""
    uzora_env.setenv("AUTHENTIK_URL", "http://authentik:9000")
    uzora_env.setenv("AUTHENTIK_ISSUER", "https://auth.example.com/application/o/locus/")
    uzora_env.setenv("AUTHENTIK_AUDIENCE", "locus-client")
    auth, main = load_uzora()

    calls: list[tuple[str, str]] = []

    def _spy_fetch_jwks(authentik_url: str, issuer: str) -> dict[str, object]:
        calls.append((authentik_url, issuer))
        raise AssertionError("fetch_jwks must not be called for /health")

    monkeypatch.setattr(auth, "fetch_jwks", _spy_fetch_jwks)
    client = TestClient(main.app)

    response = client.get("/health")

    assert response.status_code == 200
    assert calls == []


def test_whoami_without_bearer_returns_401(uzora_env: pytest.MonkeyPatch) -> None:
    uzora_env.setenv("AUTHENTIK_URL", "http://authentik:9000")
    uzora_env.setenv("AUTHENTIK_ISSUER", "https://auth.example.com/application/o/locus/")
    uzora_env.setenv("AUTHENTIK_AUDIENCE", "locus-client")
    _, main = load_uzora()
    client = TestClient(main.app)

    response = client.get("/whoami")

    assert response.status_code == 401


def test_whoami_no_bearer_and_no_settings_returns_401(uzora_env: pytest.MonkeyPatch) -> None:
    """The missing-Authorization-header check must win over the
    incomplete-settings check: no Bearer token at all is a client error
    (401), never the fail-closed 503 that applies once a token is actually
    presented against incomplete settings (see
    test_whoami_no_settings_returns_503_not_401 below)."""
    _, main = load_uzora()
    client = TestClient(main.app)

    response = client.get("/whoami")

    assert response.status_code == 401


def test_whoami_missing_authentik_url_returns_503(uzora_env: pytest.MonkeyPatch) -> None:
    uzora_env.setenv("AUTHENTIK_ISSUER", "https://auth.example.com/application/o/locus/")
    uzora_env.setenv("AUTHENTIK_AUDIENCE", "locus-client")
    _, main = load_uzora()
    client = TestClient(main.app)

    response = client.get("/whoami", headers={"Authorization": "Bearer anything.at.all"})

    assert response.status_code == 503


def test_whoami_missing_issuer_returns_503(uzora_env: pytest.MonkeyPatch) -> None:
    uzora_env.setenv("AUTHENTIK_URL", "http://authentik:9000")
    uzora_env.setenv("AUTHENTIK_AUDIENCE", "locus-client")
    _, main = load_uzora()
    client = TestClient(main.app)

    response = client.get("/whoami", headers={"Authorization": "Bearer anything.at.all"})

    assert response.status_code == 503


def test_whoami_missing_audience_returns_503(uzora_env: pytest.MonkeyPatch) -> None:
    uzora_env.setenv("AUTHENTIK_URL", "http://authentik:9000")
    uzora_env.setenv("AUTHENTIK_ISSUER", "https://auth.example.com/application/o/locus/")
    _, main = load_uzora()
    client = TestClient(main.app)

    response = client.get("/whoami", headers={"Authorization": "Bearer anything.at.all"})

    assert response.status_code == 503


def test_whoami_no_settings_returns_503_not_401(uzora_env: pytest.MonkeyPatch) -> None:
    """When settings are incomplete, the response must be 503 (fail-closed)
    even though a Bearer header is present and would otherwise pass the
    401-for-missing-auth check."""
    _, main = load_uzora()
    client = TestClient(main.app)

    response = client.get("/whoami", headers={"Authorization": "Bearer anything.at.all"})

    assert response.status_code == 503
    assert response.status_code != 200


def test_mcp_get_returns_501(uzora_env: pytest.MonkeyPatch) -> None:
    _, main = load_uzora()
    client = TestClient(main.app)

    response = client.get("/mcp")

    assert response.status_code == 501


def test_mcp_post_returns_501(uzora_env: pytest.MonkeyPatch) -> None:
    _, main = load_uzora()
    client = TestClient(main.app)

    response = client.post("/mcp", json={"anything": "goes"})

    assert response.status_code == 501


def test_mcp_get_body_states_not_implemented(uzora_env: pytest.MonkeyPatch) -> None:
    _, main = load_uzora()
    client = TestClient(main.app)

    response = client.get("/mcp")

    body = response.text.lower()
    assert "not implemented" in body


def test_mcp_post_body_states_not_implemented(uzora_env: pytest.MonkeyPatch) -> None:
    _, main = load_uzora()
    client = TestClient(main.app)

    response = client.post("/mcp", json={"anything": "goes"})

    body = response.text.lower()
    assert "not implemented" in body


def test_mcp_get_response_is_not_an_empty_success(uzora_env: pytest.MonkeyPatch) -> None:
    """501 must be an honest refusal, not a disguised empty 200."""
    _, main = load_uzora()
    client = TestClient(main.app)

    response = client.get("/mcp")

    assert response.status_code != 200
    assert response.text.strip() != ""
