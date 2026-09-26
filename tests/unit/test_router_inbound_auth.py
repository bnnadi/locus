"""GitHub issue #7: hermes-memory-router has no inbound authentication.

Today every route in ``main.py`` is reachable by any client on the network:
``POST /traces``, ``POST /retrieve``, ``GET /trace/{trace_id}/provenance``,
and the not-yet-implemented quarantine/delete routes have no auth dependency
at all. This module pins the auth contract the fix must implement:

1. A shared secret, ``HERMES_MEMORY_ROUTER_TOKEN``, read from env once at
   startup (stored on ``app.state`` during lifespan -- not re-read per
   request).
2. Every route except ``GET /health`` requires ``Authorization: Bearer
   <token>``. A missing header, a wrong secret, an empty bearer, a
   non-Bearer scheme, or a bearer that differs only by trailing whitespace
   all return ``401`` with ``detail`` exactly ``"Unauthorized"`` -- never
   ``403``, and the presented secret never appears in the response body.
3. An unset or whitespace-only token is a startup-time misconfiguration:
   entering ``with TestClient(main.app)`` must raise ``RuntimeError``
   *before* touching Qdrant or Neo4j, not fail lazily on first request.
4. ``ensure_collection`` (create the collection + both payload indexes when
   absent) must keep running during lifespan no matter how auth is wired in
   -- a custom lifespan that swaps in the auth setup must not drop it.

FastAPI 0.115 only runs the startup/lifespan hook inside ``with
TestClient(app)``, never on bare ``TestClient(app)`` construction or on
``.post()``/``.get()`` alone. Every test below that expects the app to be
usable enters the client as a context manager; the unset-token tests rely
on that entry itself raising.

Imports main.py directly (not a hypothetical auth module) so this exercises
the real request path, mirroring the harness in
test_ingest_trace_skips_existing_extraction.py. Required env vars are set
with setdefault before the import so CI (which has no env configured for
this module) fails for the right reason if it ever does.
"""

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("NEO4J_URI", "bolt://localhost:7687")
os.environ.setdefault("NEO4J_USER", "neo4j")
os.environ.setdefault("NEO4J_PASSWORD", "ci-not-a-real-password")
os.environ.setdefault("QDRANT_URL", "http://localhost:6333")
os.environ.setdefault("HERMES_MEMORY_ROUTER_TOKEN", "router-test-token")

_ROUTER_DIR = str(Path(__file__).resolve().parents[2] / "services" / "hermes-memory-router")
if _ROUTER_DIR not in sys.path:
    sys.path.insert(0, _ROUTER_DIR)

import main  # noqa: E402  (must follow env setup and sys.path insert)
from fastapi.testclient import TestClient  # noqa: E402

# tests/ is not a package (no __init__.py). Pytest prepends tests/unit, so
# the sibling module is importable by filename, not as tests.unit.*.
from test_ingest_trace_skips_existing_extraction import (  # noqa: E402
    ROUTER_TOKEN,
    _install_fake_session,
    _route,
    _stub_qdrant_startup,
)

TRACE_BODY = {
    "trace_id": "auth-trace-1",
    "task_id": "task-1",
    "task_type": "code_review",
    "raw_reasoning": "checked the diff for missing null checks before approving",
    "outcome": "success",
    "backend": "claude",
}
_TRACE_STRATEGY_ID = main.strategy_id_for(
    task_type=TRACE_BODY["task_type"], raw_reasoning=TRACE_BODY["raw_reasoning"]
)


# ---------------------------------------------------------------------------
# Fixtures: make each protected route safe to call without touching a real
# Neo4j, Qdrant, or embedding model -- so a missing/failed auth check shows
# up as the wrong status code, not a connection error or a real model
# download.
# ---------------------------------------------------------------------------


@pytest.fixture
def safe_traces(monkeypatch):
    """POST /traces via the existing-hit path: no backend, encode, or upsert.

    Absent an auth check, this returns 200 -- a clean baseline for proving a
    missing/failed check let the request through.
    """
    existence_result = {"strategy_id": _TRACE_STRATEGY_ID, "embedding_synced": True}
    reuse_result = {
        "success_rate": 1.0,
        "title": "Stored Title",
        "description": "Stored Description",
        "conditions": "{}",
        "steps": [],
        "embedding_synced": True,
    }
    run_calls = []

    def router(query, params):
        return _route(query, existence_result=existence_result, reuse_result=reuse_result)

    _install_fake_session(monkeypatch, run_calls, router)
    encode_mock = MagicMock(side_effect=AssertionError("must not encode on a hit path"))
    upsert_mock = MagicMock(side_effect=AssertionError("must not upsert on a hit path"))
    monkeypatch.setattr(main.embedder, "encode", encode_mock)
    monkeypatch.setattr(main.qdrant, "upsert", upsert_mock)
    _stub_qdrant_startup(monkeypatch)
    return SimpleNamespace(run_calls=run_calls, encode_mock=encode_mock, upsert_mock=upsert_mock)


@pytest.fixture
def safe_retrieve(monkeypatch):
    """POST /retrieve with a mocked embedder and an empty Qdrant search.

    Absent an auth check, this returns 200 with an empty result list -- it
    never needs a real model download or a live Qdrant/Neo4j.
    """
    fake_vector = MagicMock()
    fake_vector.tolist.return_value = [0.0] * 384
    encode_mock = MagicMock(return_value=fake_vector)
    search_mock = MagicMock(return_value=[])
    monkeypatch.setattr(main.embedder, "encode", encode_mock)
    monkeypatch.setattr(main.qdrant, "search", search_mock)
    _install_fake_session(monkeypatch, [], lambda query, params: None)
    _stub_qdrant_startup(monkeypatch)
    return SimpleNamespace(encode_mock=encode_mock, search_mock=search_mock)


@pytest.fixture
def safe_provenance(monkeypatch):
    """GET /trace/{id}/provenance where Neo4j has no row for this trace.

    Absent an auth check, this returns 404 (a real "not found") -- proving
    that a missing token must not be indistinguishable from a Neo4j miss.
    """
    run_calls = []
    _install_fake_session(monkeypatch, run_calls, lambda query, params: None)
    _stub_qdrant_startup(monkeypatch)
    return SimpleNamespace(run_calls=run_calls)


def _auth_header(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Regression guards: these must keep passing before and after the fix.
# ---------------------------------------------------------------------------


def test_health_returns_ok_without_bearer_when_token_configured(monkeypatch):
    _stub_qdrant_startup(monkeypatch)
    with TestClient(main.app) as client:
        resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_ensure_collection_still_runs_when_token_configured_and_collection_absent(
    monkeypatch,
):
    """A custom lifespan wiring up auth must not drop ensure_collection."""
    monkeypatch.setattr(main.qdrant, "get_collections", lambda: SimpleNamespace(collections=[]))
    create_collection_mock = MagicMock()
    create_payload_index_mock = MagicMock()
    monkeypatch.setattr(main.qdrant, "create_collection", create_collection_mock)
    monkeypatch.setattr(main.qdrant, "create_payload_index", create_payload_index_mock)

    with TestClient(main.app):
        pass

    create_collection_mock.assert_called_once()
    assert create_collection_mock.call_args.kwargs.get("collection_name") == main.COLLECTION

    assert create_payload_index_mock.call_count == 2
    field_names = {
        call.kwargs.get("field_name") for call in create_payload_index_mock.call_args_list
    }
    assert field_names == {"task_type", "success_rate"}


# ---------------------------------------------------------------------------
# Startup-time misconfiguration: unset or whitespace-only token.
# ---------------------------------------------------------------------------


def test_unset_token_raises_runtimeerror_on_lifespan_entry(monkeypatch):
    monkeypatch.delenv("HERMES_MEMORY_ROUTER_TOKEN", raising=False)
    # A recorder that WOULD succeed if called, so a passing test proves the
    # RuntimeError fired before any Qdrant call -- not that Qdrant itself
    # was unreachable for an unrelated reason.
    recorder = MagicMock(
        return_value=SimpleNamespace(collections=[SimpleNamespace(name=main.COLLECTION)])
    )
    monkeypatch.setattr(main.qdrant, "get_collections", recorder)

    with pytest.raises(RuntimeError, match="HERMES_MEMORY_ROUTER_TOKEN"):
        with TestClient(main.app):
            pass

    assert recorder.called is False


def test_whitespace_only_token_is_treated_as_unset(monkeypatch):
    monkeypatch.setenv("HERMES_MEMORY_ROUTER_TOKEN", "   ")  # three spaces
    recorder = MagicMock(
        return_value=SimpleNamespace(collections=[SimpleNamespace(name=main.COLLECTION)])
    )
    monkeypatch.setattr(main.qdrant, "get_collections", recorder)

    with pytest.raises(RuntimeError, match="HERMES_MEMORY_ROUTER_TOKEN"):
        with TestClient(main.app):
            pass

    assert recorder.called is False


# ---------------------------------------------------------------------------
# POST /traces: the full matrix of bad/missing bearer presentations.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer wrong-secret"},
        {"Authorization": "Bearer "},
        {"Authorization": f"Basic {ROUTER_TOKEN}"},
        {"Authorization": f"Bearer {ROUTER_TOKEN} "},
    ],
    ids=[
        "missing-header",
        "wrong-secret",
        "empty-bearer",
        "non-bearer-scheme",
        "trailing-whitespace",
    ],
)
def test_traces_rejects_every_bad_or_missing_bearer_with_exact_401(
    safe_traces, headers
):
    with TestClient(main.app) as client:
        resp = client.post("/traces", json=TRACE_BODY, headers=headers)

    assert resp.status_code == 401  # never 403
    assert resp.json()["detail"] == "Unauthorized"
    assert ROUTER_TOKEN not in resp.text
    assert "wrong-secret" not in resp.text
    # A handler that writes the ReasoningTrace (or anything else) before
    # checking auth, then returns 401 anyway, must still fail here -- auth
    # rejection must happen before any Neo4j session is opened at all.
    assert safe_traces.run_calls == [], (
        "a rejected request must never call neo4j session.run -- got "
        f"{len(safe_traces.run_calls)} call(s): "
        f"{[c['query'] for c in safe_traces.run_calls]}"
    )


def test_traces_succeeds_with_the_correct_bearer(safe_traces):
    with TestClient(main.app) as client:
        resp = client.post("/traces", json=TRACE_BODY, headers=_auth_header(ROUTER_TOKEN))
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# POST /retrieve
# ---------------------------------------------------------------------------


def test_retrieve_requires_bearer(safe_retrieve):
    with TestClient(main.app) as client:
        resp = client.post("/retrieve", json={"query": "anything", "k": 1})

    assert resp.status_code == 401
    assert resp.json()["detail"] == "Unauthorized"
    # A rejected request must never reach the embedder or Qdrant.
    assert safe_retrieve.encode_mock.called is False
    assert safe_retrieve.search_mock.called is False


def test_retrieve_succeeds_with_the_correct_bearer(safe_retrieve):
    with TestClient(main.app) as client:
        resp = client.post(
            "/retrieve",
            json={"query": "anything", "k": 1},
            headers=_auth_header(ROUTER_TOKEN),
        )
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# GET /trace/{trace_id}/provenance
# ---------------------------------------------------------------------------


def test_provenance_requires_bearer_even_when_neo4j_would_404(safe_provenance):
    """A missing token must be 401, never indistinguishable from a Neo4j miss."""
    with TestClient(main.app) as client:
        resp = client.get("/trace/no-such-trace/provenance")

    assert resp.status_code == 401
    assert resp.json()["detail"] == "Unauthorized"


def test_provenance_with_valid_token_and_neo4j_miss_is_404(safe_provenance):
    with TestClient(main.app) as client:
        resp = client.get(
            "/trace/no-such-trace/provenance", headers=_auth_header(ROUTER_TOKEN)
        )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# The not-yet-implemented quarantine/delete routes must also be behind auth.
# Today these 404 because the route doesn't exist at all; once implemented,
# an unauthenticated call must 401 before reaching any handler logic.
# ---------------------------------------------------------------------------


def test_quarantine_route_requires_bearer(monkeypatch):
    _stub_qdrant_startup(monkeypatch)
    with TestClient(main.app) as client:
        resp = client.post("/strategies/strategy_deadbeef/quarantine")
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Unauthorized"


def test_delete_route_requires_bearer(monkeypatch):
    _stub_qdrant_startup(monkeypatch)
    with TestClient(main.app) as client:
        resp = client.delete("/strategies/strategy_deadbeef")
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Unauthorized"
