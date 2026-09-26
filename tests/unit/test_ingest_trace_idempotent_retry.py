"""GitHub issue #6: retrying a trace_id must not re-count or re-upsert.

Today ``ingest_trace`` never checks whether *this specific trace* already
has a ``DERIVES_STRATEGY`` link before deciding what to do:

1. Redact, then unconditional ``MERGE (rt:ReasoningTrace)``.
2. ``strategy_id_for`` to compute identity.
3. An existence read on ``StrategyItem`` (hit iff a non-empty string
   ``strategy_id`` comes back).
4. Hit: bump ``success_count``/``failure_count``, ``MERGE`` the
   ``DERIVES_STRATEGY`` relationship, return the stored title. If
   ``embedding_synced`` is not ``True``, it re-embeds from the stored title
   and upserts to Qdrant.
5. Miss: call the extraction backend; failure sets
   ``extraction_status = 'failed'``; success ``MERGE``s the
   ``StrategyItem`` and upserts.

If the caller retries the exact same ``trace_id`` (e.g. an HTTP client that
times out waiting for a response but the write actually landed), step 4
runs again in full: the counters bump a second time for a request that
never happened twice, and -- because ``embedding_synced`` on the existence
row is not strictly ``True`` -- the embedding heal fires and upserts again
too.

The fix under test: after the existence read and before the counter bump
or the backend call, a third read --

    MATCH (rt:ReasoningTrace {id: $trace_id})-[:DERIVES_STRATEGY]->(s:StrategyItem)
    RETURN s.id AS strategy_id, s.title AS title, s.description AS description,
           s.conditions AS conditions, s.steps AS steps, s.success_rate AS success_rate

-- and if that row has a non-empty string ``strategy_id``, return a
``StrategyOut`` straight from those stored fields: no counter ``SET``, no
backend call, no encode, no upsert, no embedding heal.

A new trace_id with no ``DERIVES_STRATEGY`` link still takes the existing
hit or miss path. A failed extraction writes no relationship, so retrying
it still calls the backend.

This module builds its own stateful fake-Neo4j router (rather than using
the bare ``_route`` helper from ``test_ingest_trace_skips_existing_extraction``)
because that helper has no notion of "this trace_id is already linked" --
it would return ``None`` for the new read forever, which would make
test 1 fail for the wrong reason (a missing fixture) instead of the right
one (current main.py re-bumps and re-upserts on retry).
"""

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

os.environ.setdefault("NEO4J_URI", "bolt://localhost:7687")
os.environ.setdefault("NEO4J_USER", "neo4j")
os.environ.setdefault("NEO4J_PASSWORD", "ci-not-a-real-password")
os.environ.setdefault("QDRANT_URL", "http://localhost:6333")
os.environ.setdefault("HERMES_MEMORY_ROUTER_TOKEN", "router-test-token")

_ROUTER_DIR = str(Path(__file__).resolve().parents[2] / "services" / "hermes-memory-router")
if _ROUTER_DIR not in sys.path:
    sys.path.insert(0, _ROUTER_DIR)

import main  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

# tests/ is not a package (no __init__.py). Pytest prepends tests/unit, so
# the sibling module is importable by filename, not as tests.unit.*.
from test_ingest_trace_skips_existing_extraction import (  # noqa: E402
    _find_calls,
    _install_fake_session,
    _parse_return_aliases,
    _post,
    _route,
    _stub_qdrant_startup,
)

# ---------------------------------------------------------------------------
# Stateful link-aware router
# ---------------------------------------------------------------------------

_LINK_MERGE_MARKER = "MERGE (rt)-[:DERIVES_STRATEGY]->(s)"


def _is_linked_read(query: str) -> bool:
    """Identify the (not-yet-implemented) per-trace DERIVES_STRATEGY read.

    Shape: contains both ``DERIVES_STRATEGY`` and ``ReasoningTrace``, and is
    a plain read -- no ``MERGE`` in it. This must NOT match the existing
    reuse-update query (which also mentions both, but mutates via MERGE) or
    the miss/create query (same reasoning).
    """
    return "DERIVES_STRATEGY" in query and "ReasoningTrace" in query and "MERGE" not in query


def _make_stateful_router(
    *,
    existence_result=None,
    reuse_result=None,
    miss_create_result=None,
    failure_result=None,
    linked_fixtures: dict | None = None,
):
    """Build a router that tracks, per trace_id, whether it is linked yet.

    Existence / reuse / miss / failure queries delegate to `_route` (the
    shared classifier from the sibling module) unchanged. On top of that,
    this router:

    - Records a trace_id as linked the moment a query containing the exact
      substring ``MERGE (rt)-[:DERIVES_STRATEGY]->(s)`` runs with that
      trace_id among its params. This is the counter-bump/create query --
      the *write* that creates the link, not the read that checks for it.
    - Answers the not-yet-implemented linked read (see `_is_linked_read`)
      by returning the fixture for that trace_id if it is already linked,
      else `None` -- mirroring "hit" vs "miss" for that read.

    Returns `(router, linked_trace_ids)` so a test can also inspect the
    live set if needed.
    """
    linked_trace_ids: set[str] = set()
    linked_fixtures = linked_fixtures or {}

    def router(query, params):
        params = params or {}

        if _LINK_MERGE_MARKER in query:
            trace_id = params.get("trace_id")
            if trace_id is not None:
                linked_trace_ids.add(trace_id)

        if _is_linked_read(query):
            trace_id = params.get("trace_id")
            if trace_id in linked_trace_ids:
                fixture = linked_fixtures.get(trace_id)
                return dict(fixture) if fixture is not None else {"strategy_id": None}
            return None

        return _route(
            query,
            existence_result=existence_result,
            reuse_result=reuse_result,
            miss_create_result=miss_create_result,
            failure_result=failure_result,
        )

    return router, linked_trace_ids


def _upsert_payload(upsert_mock: MagicMock) -> dict:
    call = upsert_mock.call_args
    assert call is not None, "qdrant.upsert was not called"
    points = call.kwargs.get("points")
    if points is None:
        points = next((a for a in call.args if isinstance(a, list)), None)
    assert points, f"could not find `points` in upsert call: {call}"
    payload = getattr(points[0], "payload", None)
    if payload is None and isinstance(points[0], dict):
        payload = points[0].get("payload")
    assert payload is not None, f"could not find a payload on upsert point: {points[0]!r}"
    return payload


# ---------------------------------------------------------------------------
# 1. Retry of an already-linked trace_id must not double-count or
#    double-upsert. THIS MUST FAIL on current main.py.
# ---------------------------------------------------------------------------


def test_same_trace_id_retry_does_not_recount_or_upsert(monkeypatch):
    raw_reasoning = "rolled back the deploy after canary error rate spiked past threshold"
    task_type = "deploy_rollback"
    trace_id = "trace-retry-1"
    strategy_id = main.strategy_id_for(task_type=task_type, raw_reasoning=raw_reasoning)

    run_calls = []
    stored_title = "Stored Retry Title"
    stored_description = "Stored Retry Description"
    stored_conditions = {"canary_error_rate_pct": 5}
    stored_steps = ["pause-rollout", "revert-image"]
    stored_success_rate = 0.6

    existence_result = {"strategy_id": strategy_id, "embedding_synced": False}
    reuse_result = {
        "success_rate": stored_success_rate,
        "title": stored_title,
        "description": stored_description,
        "conditions": json.dumps(stored_conditions),
        "steps": stored_steps,
        "embedding_synced": False,
    }
    linked_fixture = {
        "strategy_id": strategy_id,
        "title": stored_title,
        "description": stored_description,
        "conditions": json.dumps(stored_conditions),
        "steps": stored_steps,
        "success_rate": stored_success_rate,
    }
    router, _linked = _make_stateful_router(
        existence_result=existence_result,
        reuse_result=reuse_result,
        linked_fixtures={trace_id: linked_fixture},
    )
    _install_fake_session(monkeypatch, run_calls, router)

    # A benign sentinel, not a raise: main.py's broad except-Exception
    # around the backend call would otherwise swallow a wrongful call and
    # turn it into an opaque 502 instead of a clear `.called` assertion.
    backend_mock = MagicMock(
        return_value={
            "title": "WRONGLY_EXTRACTED",
            "description": "WRONGLY_EXTRACTED",
            "conditions": {},
            "steps": [],
            "success_rate": 0.0,
        }
    )
    monkeypatch.setitem(main.BACKENDS, main.ExtractionBackend.CLAUDE, backend_mock)

    fake_vector = MagicMock()
    fake_vector.tolist.return_value = [0.0] * 384
    encode_mock = MagicMock(return_value=fake_vector)
    monkeypatch.setattr(main.embedder, "encode", encode_mock)

    upsert_mock = MagicMock()
    monkeypatch.setattr(main.qdrant, "upsert", upsert_mock)

    _stub_qdrant_startup(monkeypatch)
    with TestClient(main.app) as client:
        first = _post(
            client,
            trace_id=trace_id,
            raw_reasoning=raw_reasoning,
            task_type=task_type,
            backend="claude",
        )
        second = _post(
            client,
            trace_id=trace_id,
            raw_reasoning=raw_reasoning,
            task_type=task_type,
            backend="claude",
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["title"] == stored_title
    assert second.json()["title"] == stored_title

    assert backend_mock.called is False

    bump_calls = _find_calls(
        run_calls,
        contains=("MATCH (s:StrategyItem", "s.success_count = s.success_count +"),
        not_contains=("MERGE (s:StrategyItem",),
    )
    assert len(bump_calls) == 1, (
        "expected the counter-bump query to run exactly once across both "
        f"posts (a retry of an already-linked trace must be short-circuited "
        f"before it); got {len(bump_calls)}: {[c['query'] for c in bump_calls]}"
    )

    assert encode_mock.call_count == 1, (
        "expected embedder.encode to run exactly once (only the first "
        f"post's embedding heal); got {encode_mock.call_count} -- a second "
        "post against an already-linked trace must not re-heal"
    )
    assert upsert_mock.call_count == 1, (
        "expected qdrant.upsert to run exactly once (only the first post's "
        f"embedding heal); got {upsert_mock.call_count}"
    )

    payload = _upsert_payload(upsert_mock)
    assert payload["title"] == stored_title

    linked_read_calls = _find_calls(
        run_calls,
        contains=("DERIVES_STRATEGY", "ReasoningTrace"),
        not_contains=("MERGE",),
    )
    assert len(linked_read_calls) >= 1, (
        "expected a read shaped like MATCH (rt:ReasoningTrace {id: "
        "$trace_id})-[:DERIVES_STRATEGY]->(s:StrategyItem) before the bump "
        "or backend decision -- current main.py never issues this read, so "
        "it never short-circuits a retry"
    )
    linked_read_aliases = _parse_return_aliases(linked_read_calls[-1]["query"]) or []
    for expected in (
        "strategy_id",
        "title",
        "description",
        "conditions",
        "steps",
        "success_rate",
    ):
        assert expected in linked_read_aliases, (
            f"expected the linked read's RETURN to include {expected!r} so "
            f"a hit can build StrategyOut straight from it; got "
            f"{linked_read_aliases}"
        )


# ---------------------------------------------------------------------------
# 2. Regression guard: retrying a *failed* extraction still calls the
#    backend again and still only counts/upserts once it succeeds.
# ---------------------------------------------------------------------------


def test_failed_extraction_retry_still_counts_once(monkeypatch):
    trace_id = "trace-retry-fail-1"
    run_calls = []
    router, _linked = _make_stateful_router(
        existence_result=None,
        miss_create_result={"success_rate": 1.0},
    )
    _install_fake_session(monkeypatch, run_calls, router)

    backend_mock = MagicMock(side_effect=RuntimeError("boom"))
    monkeypatch.setitem(main.BACKENDS, main.ExtractionBackend.CLAUDE, backend_mock)

    fake_vector = MagicMock()
    fake_vector.tolist.return_value = [0.0] * 384
    monkeypatch.setattr(main.embedder, "encode", MagicMock(return_value=fake_vector))
    upsert_mock = MagicMock()
    monkeypatch.setattr(main.qdrant, "upsert", upsert_mock)

    _stub_qdrant_startup(monkeypatch)
    with TestClient(main.app) as client:
        first = _post(client, trace_id=trace_id, backend="claude")
        assert first.status_code == 502

        failed_calls = _find_calls(run_calls, contains=("extraction_status = 'failed'",))
        assert len(failed_calls) == 1

        link_merge_calls = _find_calls(run_calls, contains=(_LINK_MERGE_MARKER,))
        assert len(link_merge_calls) == 0, (
            "a failed extraction must not create the DERIVES_STRATEGY link -- "
            f"got {len(link_merge_calls)} link-merge calls after the 502"
        )

        backend_mock.side_effect = None
        backend_mock.return_value = {
            "title": "Recovered Title",
            "description": "Recovered description",
            "conditions": {},
            "steps": ["retry-step"],
            "success_rate": 1.0,
        }

        second = _post(client, trace_id=trace_id, backend="claude")
    assert second.status_code == 200
    assert backend_mock.call_count == 2, (
        "a retry after a failed extraction must call the backend again -- "
        f"got {backend_mock.call_count} total calls"
    )

    create_calls = _find_calls(run_calls, contains=("MERGE (s:StrategyItem",))
    assert len(create_calls) == 1, (
        f"expected exactly one StrategyItem create across both posts, got "
        f"{len(create_calls)}"
    )
    assert upsert_mock.call_count == 1


# ---------------------------------------------------------------------------
# 3. Regression guard: two different trace_ids sharing a strategy identity
#    must both still bump the shared strategy's counters.
# ---------------------------------------------------------------------------


def test_new_trace_id_against_existing_strategy_still_increments(monkeypatch):
    raw_reasoning = "rolled back the migration after seeing lock contention spike"
    task_type = "db_migration"
    strategy_id = main.strategy_id_for(task_type=task_type, raw_reasoning=raw_reasoning)

    run_calls = []
    existence_result = {"strategy_id": strategy_id, "embedding_synced": True}
    reuse_result = {
        "success_rate": 0.8,
        "title": "Shared Strategy Title",
        "description": "Shared Strategy Description",
        "conditions": json.dumps({}),
        "steps": ["shared-step"],
        "embedding_synced": True,
    }
    router, _linked = _make_stateful_router(
        existence_result=existence_result,
        reuse_result=reuse_result,
    )
    _install_fake_session(monkeypatch, run_calls, router)

    backend_mock = MagicMock(
        return_value={
            "title": "WRONGLY_EXTRACTED",
            "description": "WRONGLY_EXTRACTED",
            "conditions": {},
            "steps": [],
            "success_rate": 0.0,
        }
    )
    monkeypatch.setitem(main.BACKENDS, main.ExtractionBackend.CLAUDE, backend_mock)
    monkeypatch.setattr(
        main.embedder,
        "encode",
        MagicMock(side_effect=AssertionError("should not re-embed: already synced")),
    )
    monkeypatch.setattr(
        main.qdrant,
        "upsert",
        MagicMock(side_effect=AssertionError("should not upsert: already synced")),
    )

    _stub_qdrant_startup(monkeypatch)
    with TestClient(main.app) as client:
        first = _post(
            client,
            trace_id="trace-shared-a",
            raw_reasoning=raw_reasoning,
            task_type=task_type,
            backend="claude",
        )
        second = _post(
            client,
            trace_id="trace-shared-b",
            raw_reasoning=raw_reasoning,
            task_type=task_type,
            backend="claude",
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert backend_mock.called is False

    bump_calls = _find_calls(
        run_calls,
        contains=("MATCH (s:StrategyItem", "s.success_count = s.success_count +"),
        not_contains=("MERGE (s:StrategyItem",),
    )
    assert len(bump_calls) == 2, (
        "each distinct trace_id must bump the shared strategy's counters "
        f"on its own first post; got {len(bump_calls)} bump calls across "
        "two different trace_ids -- a short-circuit keyed off the strategy "
        "node instead of this trace's own DERIVES_STRATEGY link would "
        "wrongly skip the second one"
    )
