"""POST /traces must not persist or hash raw secrets from raw_reasoning.

Imports main.py directly (not redact.py) so this exercises the actual
request path. main.py builds its Neo4j/Qdrant/embedder clients at import
time, so the required env vars are set with setdefault before the import --
otherwise the import fails for the wrong reason under CI, which has no env
configured for this test module.
"""

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

os.environ.setdefault("NEO4J_URI", "bolt://localhost:7687")
os.environ.setdefault("NEO4J_USER", "neo4j")
os.environ.setdefault("NEO4J_PASSWORD", "ci-not-a-real-password")
os.environ.setdefault("QDRANT_URL", "http://localhost:6333")

_ROUTER_DIR = str(Path(__file__).resolve().parents[2] / "services" / "hermes-memory-router")
if _ROUTER_DIR not in sys.path:
    sys.path.insert(0, _ROUTER_DIR)

import main  # noqa: E402  (must follow env setup and sys.path insert)
from fastapi.testclient import TestClient  # noqa: E402

BEARER_TOKEN = "AbcdEfghIjklMnop0123"  # 20 chars, class [A-Za-z0-9._~+/-]


def test_ingest_trace_does_not_persist_or_hash_the_bearer_token(monkeypatch):
    raw_reasoning = f"called the API with Authorization: Bearer {BEARER_TOKEN} and it worked"

    # --- Neo4j: mock session() as a context manager, .run() records calls,
    # .single() returns a plausible success_rate row for every call.
    run_calls = []

    class _FakeResult:
        def single(self):
            return {"success_rate": 1.0}

    class _FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def run(self, query, params=None):
            run_calls.append({"query": query, "params": params or {}})
            return _FakeResult()

    monkeypatch.setattr(main.neo4j_driver, "session", lambda: _FakeSession())

    # --- Extraction backend: skip the real Claude call entirely.
    fake_extraction = {
        "title": "t",
        "description": "d",
        "conditions": {},
        "steps": ["s"],
        "success_rate": 1.0,
    }
    monkeypatch.setitem(
        main.BACKENDS, main.ExtractionBackend.CLAUDE, lambda trace: fake_extraction
    )

    # --- Embedder: skip the real sentence-transformers model.
    fake_vector = MagicMock()
    fake_vector.tolist.return_value = [0.0] * 384
    monkeypatch.setattr(main.embedder, "encode", lambda text: fake_vector)

    # --- Qdrant: no-op upsert.
    monkeypatch.setattr(main.qdrant, "upsert", lambda **kwargs: None)

    # --- strategy_id_for: wrap it to record the raw_reasoning it was called
    # with, while still returning a real id so the rest of the handler runs.
    strategy_id_calls = []
    original_strategy_id_for = main.strategy_id_for

    def _recording_strategy_id_for(*, task_type, raw_reasoning, strategy_key=None):
        strategy_id_calls.append(raw_reasoning)
        return original_strategy_id_for(
            task_type=task_type,
            raw_reasoning=raw_reasoning,
            strategy_key=strategy_key,
        )

    monkeypatch.setattr(main, "strategy_id_for", _recording_strategy_id_for)

    client = TestClient(main.app)
    response = client.post(
        "/traces",
        json={
            "trace_id": "trace-1",
            "task_id": "task-1",
            "task_type": "code_review",
            "raw_reasoning": raw_reasoning,
            "outcome": "success",
            "backend": "claude",
        },
    )

    assert response.status_code == 200

    # The first session.run call is the MERGE into ReasoningTrace, carrying
    # raw_reasoning as a query param. Today ingest_trace writes the original
    # string there -- this assertion is expected to fail until raw_reasoning
    # is redacted before being persisted.
    #
    # `.get("raw_reasoning", "")` would let a missing key pass silently
    # (an empty string trivially "lacks" the token). Require the key to
    # actually be present on the MERGE params before checking its value.
    assert len(run_calls) >= 1
    first_call_params = run_calls[0]["params"]
    assert "raw_reasoning" in first_call_params
    assert BEARER_TOKEN not in first_call_params["raw_reasoning"]

    # strategy_id_for must also be called with the redacted reasoning, not
    # the raw secret, since identity is derived from raw_reasoning.
    assert len(strategy_id_calls) >= 1
    assert all(BEARER_TOKEN not in call for call in strategy_id_calls)
