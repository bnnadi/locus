"""GitHub issue #7: a StrategyItem must be quarantinable and deletable.

Today ``main.py`` has no quarantine flag, no ``POST
/strategies/{strategy_id}/quarantine`` route, no ``DELETE
/strategies/{strategy_id}`` route, and ``ingest_trace``/``retrieve_strategy``
never check a ``quarantined`` flag at all. This module pins the contract the
fix must implement:

1. Ingest refuses to reuse or heal a quarantined StrategyItem (409, no
   counter bump, no backend call, no embed, no upsert) whether the
   quarantine is found via the existence-by-identity read OR via the
   per-trace DERIVES_STRATEGY link -- the second case matters because a
   retried trace_id can be linked to a *different* strategy than the one
   this request's own (task_type, raw_reasoning) would hash to.
2. ``POST .../quarantine``: 404 (MATCH+SET, no MERGE) when the node is
   missing; on a hit, SET a real boolean `quarantined = true` in Neo4j and
   delete the *current* (uuid5) Qdrant point -- never the legacy truncated
   int id, except when a legacy point exists AND its own payload
   strategy_id still matches this strategy. A failed Qdrant delete is 503
   and must be retried (not skipped) on the next call.
3. ``DELETE /strategies/{strategy_id}``: 404 when missing; on a hit, delete
   the Qdrant point FIRST, then DETACH DELETE the StrategyItem only --
   never the ReasoningTrace. A failed Qdrant delete is 503 and the node
   must not be deleted.
4. ``/retrieve`` excludes a search hit whose Neo4j row is quarantined, and
   its own Qdrant filter carries a `must_not` on payload `quarantined ==
   true` alongside (not instead of) any `must` condition on task_type.
5. The ingest miss-path Qdrant point id is
   ``str(uuid.uuid5(uuid.NAMESPACE_URL, "hermes-strategy:" + strategy_id))``,
   not the legacy ``int(sha256(strategy_id).hexdigest()[:8], 16)``.

Every protected call below sends the fixed fake bearer
(``HERMES_MEMORY_ROUTER_TOKEN`` / ``ROUTER_TOKEN``, imported from the sibling
module) so a 401 never masks the behavior actually under test here --
auth-rejection itself is covered in test_router_inbound_auth.py.

FastAPI 0.115 only runs the startup/lifespan hook inside `with
TestClient(app)`; every test enters the client that way and stubs Qdrant's
startup calls first via `_stub_qdrant_startup`.
"""

import hashlib
import os
import re
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
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
    ROUTER_TOKEN,
    _find_calls,
    _install_fake_session,
    _parse_return_aliases,
    _project_row,
    _route,
    _stub_qdrant_startup,
)

AUTH_HEADER = {"Authorization": f"Bearer {ROUTER_TOKEN}"}

# Same class of secret as tests/unit/test_ingest_trace_redacts.py's
# BEARER_TOKEN (20 chars, class [A-Za-z0-9._~+/-]) -- deliberately NOT the
# router token, since this one is planted inside raw_reasoning to prove
# redaction still runs ahead of the (unimplemented) quarantine check, not to
# exercise inbound auth.
PLANTED_BEARER_SECRET = "QwertyUiopAsdfghJk12"

# An arbitrary, already-existing StrategyItem id used by the quarantine/
# delete endpoint tests, which act on a strategy by id directly rather than
# deriving identity from (task_type, raw_reasoning).
QUARANTINE_STRATEGY_ID = "strategy_11112222aaaa"


def _uuid5_point_id(strategy_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"hermes-strategy:{strategy_id}"))


def _legacy_point_id(strategy_id: str) -> int:
    return int(hashlib.sha256(strategy_id.encode()).hexdigest()[:8], 16)


def _extract_point_ids(call) -> set:
    """Pull point ids out of a qdrant.delete/upsert call, whatever shape it used.

    ``points_selector`` can legitimately be a plain list of ids or a
    ``PointIdsList`` (``.points``); this test suite only asserts on the ids
    themselves, not which shape a future implementation picks.
    """
    ids: set = set()
    candidates = list(call.args) + list(call.kwargs.values())
    for candidate in candidates:
        points = getattr(candidate, "points", None)
        if points is not None:
            ids.update(points)
        elif isinstance(candidate, (list, tuple, set)) and not isinstance(candidate, str):
            ids.update(c for c in candidate if isinstance(c, (str, int)))
    return ids


def _assert_detach_delete_targets_strategy_item(query: str) -> None:
    """Assert DETACH DELETE removes exactly the variable bound to :StrategyItem.

    Checking only "ReasoningTrace not in query" would still accept e.g.
    ``MATCH (s:StrategyItem {id: $id}), (rt) DETACH DELETE rt`` -- an
    unlabeled variable that happens not to spell out "ReasoningTrace" but
    could still be bound to any node, ReasoningTrace included, by the
    surrounding MATCH. This requires the deleted variable to be labeled
    :StrategyItem in THIS query, and never bound to any other label or left
    unlabeled elsewhere in it.
    """
    detach_match = re.search(r"DETACH\s+DELETE\s+([A-Za-z_]\w*)", query, re.IGNORECASE)
    assert detach_match is not None, f"expected a DETACH DELETE clause in: {query!r}"
    deleted_var = detach_match.group(1)

    bound_to_strategy_item = re.search(
        rf"\(\s*{re.escape(deleted_var)}\s*:\s*StrategyItem\b", query
    )
    assert bound_to_strategy_item is not None, (
        f"DETACH DELETE {deleted_var} -- {deleted_var!r} is never bound to "
        f":StrategyItem anywhere in this query: {query!r}"
    )

    bound_to_other_label = re.search(
        rf"\(\s*{re.escape(deleted_var)}\s*:\s*(?!StrategyItem\b)\w+", query
    )
    assert bound_to_other_label is None, (
        f"variable {deleted_var!r} used in DETACH DELETE is ALSO bound to a "
        f"different label elsewhere in the same query (e.g. ReasoningTrace): "
        f"{query!r}"
    )

    bound_unlabeled = re.search(rf"\(\s*{re.escape(deleted_var)}\s*\)", query)
    assert bound_unlabeled is None, (
        f"variable {deleted_var!r} used in DETACH DELETE also appears with "
        f"no label at all elsewhere in the query, which could match a "
        f"ReasoningTrace or any other node: {query!r}"
    )


def _assert_boolean_true_assignment(query: str, params: dict, field: str) -> None:
    """Assert `field` is bound to the Python bool True, not a string.

    Accepts either a Cypher `true` literal or a `$param` reference whose
    bound value is `True` -- never the string "true"/"True" (which Neo4j
    would happily accept as a truthy-looking value that isn't a boolean).
    """
    match = re.search(rf"{field}\s*=\s*(true|\$[A-Za-z_]\w*)", query, re.IGNORECASE)
    assert match is not None, f"expected `{field} = true` or `{field} = $<param>` in: {query!r}"
    token = match.group(1)
    if token.lower() == "true":
        return
    value = params.get(token[1:])
    assert value is True, f"expected {field}'s bound param to be Python bool True, got {value!r}"


# ---------------------------------------------------------------------------
# 1. Ingest must refuse a quarantined strategy -- both via the
#    existence-by-identity read and via the per-trace linked read.
# ---------------------------------------------------------------------------


def _assert_refused_from_pending(run_calls: list, *, after_index: int) -> None:
    """Assert exactly one refused_quarantine write, strictly after `after_index`.

    `after_index` is the position of the last read the quarantine decision
    depends on (the existence MATCH for a fresh trace, or the linked-trace
    read for a retry) -- the write must come after it, never be folded into
    the unconditional ReasoningTrace MERGE at run_calls[0] itself (which
    runs before quarantine status is even known).
    """
    assert "refused_quarantine" not in run_calls[0]["query"], (
        "the trace MERGE at run_calls[0] must not itself set "
        "extraction_status = 'refused_quarantine' -- that decision can only "
        "be made after reading the strategy's quarantined flag"
    )

    refused_indices = [
        i for i, c in enumerate(run_calls) if "refused_quarantine" in c["query"]
    ]
    assert len(refused_indices) == 1, (
        "expected exactly one write setting extraction_status = "
        f"'refused_quarantine', got {len(refused_indices)}: "
        f"{[c['query'] for c in run_calls]}"
    )
    refused_index = refused_indices[0]
    assert refused_index > after_index, (
        f"expected the refused_quarantine write (run_calls[{refused_index}]) "
        f"to come strictly after run_calls[{after_index}] (the read that "
        "actually revealed the quarantine), not before or at the same call"
    )

    refused_query = run_calls[refused_index]["query"]
    predicate = re.search(
        r"CASE\s+WHEN\s+rt\.extraction_status\s*=\s*'pending'"
        r"\s+THEN\s+'refused_quarantine'\s+ELSE\s+rt\.extraction_status\s+END",
        refused_query,
        re.IGNORECASE | re.DOTALL,
    )
    assert predicate is not None, (
        "expected `CASE WHEN rt.extraction_status = 'pending' THEN "
        "'refused_quarantine' ELSE rt.extraction_status END` (only flips "
        f"from pending), got: {refused_query!r}"
    )


def test_ingest_new_trace_against_quarantined_existing_strategy_returns_409(monkeypatch):
    raw_reasoning = (
        "reused a well known but since-quarantined strategy pattern; "
        f"Authorization: Bearer {PLANTED_BEARER_SECRET}"
    )
    task_type = "code_review"
    trace_id = "trace-quarantine-existence-1"
    strategy_id = main.strategy_id_for(task_type=task_type, raw_reasoning=raw_reasoning)
    stored_title = "Stored Title Before Quarantine"

    run_calls = []
    # embedding_synced is None (unsynced), matching the sibling heal test
    # (test_ingest_trace_unsynced_strategy_reembeds_without_backend) exactly.
    # Today's hit path already skips encode/upsert whenever embedding_synced
    # is True, so a `True` value here would let a handler that 409s only
    # because "already synced" masquerade as a quarantine check. With
    # embedding_synced None, the ONLY thing that can explain skipping
    # encode/upsert and returning 409 is the quarantine check itself -- a
    # handler that heals whenever embedding_synced is not True (ignoring
    # quarantined) must fail here.
    existence_result = {
        "strategy_id": strategy_id,
        "embedding_synced": None,
        "quarantined": True,
    }
    reuse_result = {
        "success_rate": 0.5,
        "title": stored_title,
        "description": "d",
        "conditions": "{}",
        "steps": [],
        "embedding_synced": None,
    }

    def router(query, params):
        return _route(query, existence_result=existence_result, reuse_result=reuse_result)

    _install_fake_session(monkeypatch, run_calls, router)

    # Benign, not a raise: main.py's broad except-Exception around the
    # backend call would otherwise swallow a wrongful call and turn it into
    # an opaque 502 instead of a clear `.called` assertion below. Same
    # reasoning for encode/upsert being harmless mocks rather than raises --
    # this lets a genuine miss/heal path (today's actual, wrong, behavior)
    # complete and return a clean response instead of crashing the request.
    backend_mock = MagicMock(
        return_value={
            "title": "WRONGLY_EXTRACTED",
            "description": "x",
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
        response = client.post(
            "/traces",
            json={
                "trace_id": trace_id,
                "task_id": "task-1",
                "task_type": task_type,
                "raw_reasoning": raw_reasoning,
                "outcome": "success",
                "backend": "claude",
            },
            headers=AUTH_HEADER,
        )

    assert response.status_code == 409
    assert response.json()["detail"] == "strategy quarantined"
    assert stored_title not in response.text, (
        "a 409 must not leak the quarantined strategy's stored title"
    )
    assert backend_mock.called is False
    assert encode_mock.called is False
    assert upsert_mock.called is False

    assert len(run_calls) >= 2, "expected at least the trace MERGE and the existence MATCH"
    assert "ReasoningTrace" in run_calls[0]["query"] and "MERGE" in run_calls[0]["query"]
    assert "raw_reasoning" in run_calls[0]["params"]
    assert PLANTED_BEARER_SECRET not in run_calls[0]["params"]["raw_reasoning"], (
        "redaction must still run ahead of the MERGE regardless of the "
        "quarantine outcome -- the planted bearer secret must never reach "
        "the ReasoningTrace MERGE params"
    )
    assert not any(
        PLANTED_BEARER_SECRET in str(v) for v in run_calls[0]["params"].values()
    ), "the planted secret must not appear in ANY MERGE param, not just raw_reasoning"

    existence_call = run_calls[1]
    assert "MATCH (s:StrategyItem" in existence_call["query"]
    assert "MERGE" not in existence_call["query"]
    existence_aliases = _parse_return_aliases(existence_call["query"]) or []
    assert "quarantined" in existence_aliases, (
        "expected the existence read to RETURN s.quarantined AS quarantined "
        f"so a hit can be quarantine-checked; got {existence_aliases}"
    )

    bump_calls = _find_calls(run_calls, contains=("s.success_count = s.success_count +",))
    assert len(bump_calls) == 0, "a quarantined hit must never reach the counter-bump query"

    _assert_refused_from_pending(run_calls, after_index=1)


def test_ingest_retry_of_linked_quarantined_strategy_returns_409_even_when_identity_hash_differs(
    monkeypatch,
):
    """A retried trace_id linked to a quarantined strategy must 409 -- even
    though *this body's own* (task_type, raw_reasoning) hashes to an
    unrelated strategy the existence read reports as a plain miss.
    """
    trace_id = "trace-quarantine-linked-1"
    linked_strategy_id = "strategy_linked_and_quarantined"
    stored_title = "Quarantined Stored Title"

    task_type = "incident_response"
    raw_reasoning = "wholly unrelated reasoning text for this retry body"

    run_calls = []

    def router(query, params):
        if "DERIVES_STRATEGY" in query and "ReasoningTrace" in query and "MERGE" not in query:
            return {
                "strategy_id": linked_strategy_id,
                "title": stored_title,
                "description": "Quarantined stored description",
                "conditions": "{}",
                "steps": [],
                "success_rate": 1.0,
                "quarantined": True,
            }
        return _route(query, existence_result=None, miss_create_result={"success_rate": 1.0})

    _install_fake_session(monkeypatch, run_calls, router)

    # Benign, not a raise -- see the comment in the existence-row variant of
    # this test above for why: today's actual (wrong) behavior is to fall
    # through to the miss/extract path, and it must be allowed to complete
    # cleanly so the assertions below report a clear `.called` mismatch
    # instead of an unhandled exception.
    backend_mock = MagicMock(
        return_value={
            "title": "WRONGLY_EXTRACTED",
            "description": "x",
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
        response = client.post(
            "/traces",
            json={
                "trace_id": trace_id,
                "task_id": "task-1",
                "task_type": task_type,
                "raw_reasoning": raw_reasoning,
                "outcome": "success",
                "backend": "claude",
            },
            headers=AUTH_HEADER,
        )

    assert response.status_code == 409
    assert response.json()["detail"] == "strategy quarantined"
    assert stored_title not in response.text
    assert backend_mock.called is False
    assert encode_mock.called is False
    assert upsert_mock.called is False

    assert len(run_calls) >= 2
    assert "ReasoningTrace" in run_calls[0]["query"] and "MERGE" in run_calls[0]["query"]
    existence_call = run_calls[1]
    assert "MATCH (s:StrategyItem" in existence_call["query"]
    assert "MERGE" not in existence_call["query"]

    bump_calls = _find_calls(run_calls, contains=("s.success_count = s.success_count +",))
    assert len(bump_calls) == 0, (
        "a linked-but-quarantined trace must never reach the counter-bump "
        "query -- a handler that only checks quarantine on the "
        "existence-by-identity path (and bumps first) would wrongly pass "
        "here otherwise"
    )

    linked_read_calls = _find_calls(
        run_calls, contains=("DERIVES_STRATEGY", "ReasoningTrace"), not_contains=("MERGE",)
    )
    assert len(linked_read_calls) >= 1
    linked_index = run_calls.index(linked_read_calls[-1])
    linked_query = linked_read_calls[-1]["query"]
    linked_aliases = _parse_return_aliases(linked_query) or []
    assert "quarantined" in linked_aliases, (
        "expected the linked-trace read to RETURN s.quarantined AS quarantined "
        f"so a hit can be quarantine-checked; got {linked_aliases}"
    )
    # Sanity check on the harness itself: _project_row only lets `quarantined`
    # through when the query's own RETURN names it -- confirming the alias
    # assertion above is the thing actually gating what the handler can see,
    # not an incidental fixture leak.
    assert _project_row(linked_query, {"quarantined": True}).get("quarantined") is True

    _assert_refused_from_pending(run_calls, after_index=linked_index)


# ---------------------------------------------------------------------------
# 2. POST /strategies/{strategy_id}/quarantine
# ---------------------------------------------------------------------------


def test_quarantine_requires_bearer(monkeypatch):
    _stub_qdrant_startup(monkeypatch)
    with TestClient(main.app) as client:
        response = client.post(f"/strategies/{QUARANTINE_STRATEGY_ID}/quarantine")
    assert response.status_code == 401


def test_quarantine_missing_strategy_returns_404_and_does_not_merge(monkeypatch):
    run_calls = []
    _install_fake_session(monkeypatch, run_calls, lambda query, params: None)
    _stub_qdrant_startup(monkeypatch)
    delete_mock = MagicMock(side_effect=AssertionError("must not touch qdrant for a missing strategy"))
    monkeypatch.setattr(main.qdrant, "delete", delete_mock)

    with TestClient(main.app) as client:
        response = client.post(
            f"/strategies/{QUARANTINE_STRATEGY_ID}/quarantine", headers=AUTH_HEADER
        )

    assert response.status_code == 404
    assert delete_mock.called is False
    assert len(run_calls) >= 1, "expected the handler to attempt a MATCH+SET on StrategyItem"
    call = run_calls[0]
    assert "MATCH" in call["query"]
    assert "SET" in call["query"]
    assert "MERGE" not in call["query"]


def test_quarantine_existing_strategy_sets_boolean_flag_and_deletes_uuid5_point(monkeypatch):
    strategy_id = QUARANTINE_STRATEGY_ID
    expected_point_id = _uuid5_point_id(strategy_id)

    run_calls = []

    def router(query, params):
        if "StrategyItem" in query and "MATCH" in query and "SET" in query:
            return {"strategy_id": strategy_id, "quarantined": True}
        return None

    _install_fake_session(monkeypatch, run_calls, router)
    _stub_qdrant_startup(monkeypatch)
    monkeypatch.setattr(main.qdrant, "retrieve", MagicMock(return_value=[]))  # no legacy point
    delete_mock = MagicMock()
    monkeypatch.setattr(main.qdrant, "delete", delete_mock)

    with TestClient(main.app) as client:
        response = client.post(f"/strategies/{strategy_id}/quarantine", headers=AUTH_HEADER)

    assert response.status_code == 200
    assert response.json() == {"strategy_id": strategy_id, "quarantined": True}, (
        "response body must be ONLY strategy_id and quarantined -- no title, "
        "description, steps, or reasoning"
    )

    assert delete_mock.called
    deleted_ids: set = set()
    for call in delete_mock.call_args_list:
        deleted_ids.update(_extract_point_ids(call))
    assert expected_point_id in deleted_ids
    assert _legacy_point_id(strategy_id) not in deleted_ids  # no legacy point existed to delete

    set_call = _find_calls(run_calls, contains=("MATCH", "SET", "StrategyItem"))
    assert len(set_call) == 1
    _assert_boolean_true_assignment(set_call[0]["query"], set_call[0]["params"], "quarantined")


def test_quarantine_qdrant_delete_failure_returns_503_and_neo4j_already_quarantined(monkeypatch):
    strategy_id = QUARANTINE_STRATEGY_ID
    run_calls = []

    def router(query, params):
        if "StrategyItem" in query and "MATCH" in query and "SET" in query:
            return {"strategy_id": strategy_id, "quarantined": True, "title": "Secret Title"}
        return None

    _install_fake_session(monkeypatch, run_calls, router)
    _stub_qdrant_startup(monkeypatch)
    monkeypatch.setattr(main.qdrant, "retrieve", MagicMock(return_value=[]))
    delete_mock = MagicMock(side_effect=RuntimeError("qdrant unavailable"))
    monkeypatch.setattr(main.qdrant, "delete", delete_mock)

    with TestClient(main.app) as client:
        response = client.post(f"/strategies/{strategy_id}/quarantine", headers=AUTH_HEADER)

    assert response.status_code == 503
    assert "Secret Title" not in response.text
    assert delete_mock.called
    # The Neo4j SET already ran (and, per the contract, the node stays
    # quarantined) before the Qdrant attempt failed.
    assert len(_find_calls(run_calls, contains=("MATCH", "SET", "StrategyItem"))) == 1


def test_quarantine_retry_after_qdrant_failure_calls_delete_again_and_succeeds_when_point_absent(
    monkeypatch,
):
    strategy_id = QUARANTINE_STRATEGY_ID
    run_calls = []

    def router(query, params):
        if "StrategyItem" in query and "MATCH" in query and "SET" in query:
            return {"strategy_id": strategy_id, "quarantined": True}
        return None

    _install_fake_session(monkeypatch, run_calls, router)
    _stub_qdrant_startup(monkeypatch)
    monkeypatch.setattr(main.qdrant, "retrieve", MagicMock(return_value=[]))

    delete_mock = MagicMock(side_effect=[RuntimeError("qdrant unavailable"), None])
    monkeypatch.setattr(main.qdrant, "delete", delete_mock)

    with TestClient(main.app) as client:
        first = client.post(f"/strategies/{strategy_id}/quarantine", headers=AUTH_HEADER)
        second = client.post(f"/strategies/{strategy_id}/quarantine", headers=AUTH_HEADER)

    assert first.status_code == 503
    assert second.status_code == 200
    assert delete_mock.call_count == 2, (
        "a repeat quarantine after a failed delete must call qdrant.delete "
        "again -- 'already quarantined in Neo4j' is not a reason to skip "
        "Qdrant; a missing point on retry is what makes it a 200"
    )
    assert second.json() == {"strategy_id": strategy_id, "quarantined": True}


def test_quarantine_deletes_legacy_point_when_payload_strategy_id_matches(monkeypatch):
    strategy_id = QUARANTINE_STRATEGY_ID
    legacy_id = _legacy_point_id(strategy_id)
    run_calls = []

    def router(query, params):
        if "StrategyItem" in query and "MATCH" in query and "SET" in query:
            return {"strategy_id": strategy_id, "quarantined": True}
        return None

    _install_fake_session(monkeypatch, run_calls, router)
    _stub_qdrant_startup(monkeypatch)

    legacy_point = SimpleNamespace(id=legacy_id, payload={"strategy_id": strategy_id})
    monkeypatch.setattr(main.qdrant, "retrieve", MagicMock(return_value=[legacy_point]))
    delete_mock = MagicMock()
    monkeypatch.setattr(main.qdrant, "delete", delete_mock)

    with TestClient(main.app) as client:
        response = client.post(f"/strategies/{strategy_id}/quarantine", headers=AUTH_HEADER)

    assert response.status_code == 200
    deleted_ids: set = set()
    for call in delete_mock.call_args_list:
        deleted_ids.update(_extract_point_ids(call))
    assert legacy_id in deleted_ids, (
        "expected the legacy truncated-int point to be deleted when its own "
        "payload strategy_id matches this strategy"
    )
    assert _uuid5_point_id(strategy_id) in deleted_ids


def test_quarantine_does_not_delete_legacy_point_when_payload_strategy_id_differs(monkeypatch):
    strategy_id = QUARANTINE_STRATEGY_ID
    legacy_id = _legacy_point_id(strategy_id)
    run_calls = []

    def router(query, params):
        if "StrategyItem" in query and "MATCH" in query and "SET" in query:
            return {"strategy_id": strategy_id, "quarantined": True}
        return None

    _install_fake_session(monkeypatch, run_calls, router)
    _stub_qdrant_startup(monkeypatch)

    other_point = SimpleNamespace(
        id=legacy_id, payload={"strategy_id": "strategy_completely_different"}
    )
    monkeypatch.setattr(main.qdrant, "retrieve", MagicMock(return_value=[other_point]))
    delete_mock = MagicMock()
    monkeypatch.setattr(main.qdrant, "delete", delete_mock)

    with TestClient(main.app) as client:
        response = client.post(f"/strategies/{strategy_id}/quarantine", headers=AUTH_HEADER)

    assert response.status_code == 200
    deleted_ids: set = set()
    for call in delete_mock.call_args_list:
        deleted_ids.update(_extract_point_ids(call))
    assert legacy_id not in deleted_ids, (
        "the legacy point's payload strategy_id belongs to a different "
        "strategy -- it must not be deleted by this quarantine call"
    )
    assert _uuid5_point_id(strategy_id) in deleted_ids


# ---------------------------------------------------------------------------
# 3. DELETE /strategies/{strategy_id}
# ---------------------------------------------------------------------------


def test_delete_requires_bearer(monkeypatch):
    _stub_qdrant_startup(monkeypatch)
    with TestClient(main.app) as client:
        response = client.delete(f"/strategies/{QUARANTINE_STRATEGY_ID}")
    assert response.status_code == 401


def test_delete_missing_strategy_returns_404_and_does_not_touch_qdrant_or_merge(monkeypatch):
    run_calls = []
    _install_fake_session(monkeypatch, run_calls, lambda query, params: None)
    _stub_qdrant_startup(monkeypatch)
    delete_mock = MagicMock(side_effect=AssertionError("must not touch qdrant for a missing strategy"))
    monkeypatch.setattr(main.qdrant, "delete", delete_mock)

    with TestClient(main.app) as client:
        response = client.delete(f"/strategies/{QUARANTINE_STRATEGY_ID}", headers=AUTH_HEADER)

    assert response.status_code == 404
    assert delete_mock.called is False
    assert len(run_calls) >= 1
    assert "MERGE" not in run_calls[0]["query"]


def test_delete_existing_strategy_deletes_qdrant_point_before_detach_deleting_node(monkeypatch):
    strategy_id = QUARANTINE_STRATEGY_ID
    expected_point_id = _uuid5_point_id(strategy_id)

    order: list = []
    run_calls = []

    def router(query, params):
        order.append(("neo4j", query))
        if "StrategyItem" in query:
            return {"strategy_id": strategy_id}
        return None

    _install_fake_session(monkeypatch, run_calls, router)
    _stub_qdrant_startup(monkeypatch)
    monkeypatch.setattr(main.qdrant, "retrieve", MagicMock(return_value=[]))

    def _record_delete(*args, **kwargs):
        order.append(("qdrant_delete", None))

    delete_mock = MagicMock(side_effect=_record_delete)
    monkeypatch.setattr(main.qdrant, "delete", delete_mock)

    with TestClient(main.app) as client:
        response = client.delete(f"/strategies/{strategy_id}", headers=AUTH_HEADER)

    assert response.status_code == 200
    assert response.json() == {"strategy_id": strategy_id, "deleted": True}

    assert delete_mock.called
    deleted_ids: set = set()
    for call in delete_mock.call_args_list:
        deleted_ids.update(_extract_point_ids(call))
    assert expected_point_id in deleted_ids

    detach_calls = _find_calls(run_calls, contains=("DETACH DELETE",))
    assert len(detach_calls) == 1, "expected exactly one DETACH DELETE"
    detach_query = detach_calls[0]["query"]
    assert "ReasoningTrace" not in detach_query, (
        "DETACH DELETE must target the StrategyItem only, never ReasoningTrace"
    )
    assert "MERGE" not in detach_query
    _assert_detach_delete_targets_strategy_item(detach_query)

    qdrant_positions = [i for i, (kind, _) in enumerate(order) if kind == "qdrant_delete"]
    detach_positions = [
        i for i, (kind, q) in enumerate(order) if kind == "neo4j" and "DETACH DELETE" in q
    ]
    assert qdrant_positions and detach_positions
    assert qdrant_positions[0] < detach_positions[0], (
        "the Qdrant point must be deleted BEFORE the Neo4j DETACH DELETE"
    )


def test_delete_qdrant_failure_returns_503_and_node_is_not_deleted(monkeypatch):
    strategy_id = QUARANTINE_STRATEGY_ID
    run_calls = []

    def router(query, params):
        if "StrategyItem" in query:
            return {"strategy_id": strategy_id}
        return None

    _install_fake_session(monkeypatch, run_calls, router)
    _stub_qdrant_startup(monkeypatch)
    monkeypatch.setattr(main.qdrant, "retrieve", MagicMock(return_value=[]))
    delete_mock = MagicMock(side_effect=RuntimeError("qdrant unavailable"))
    monkeypatch.setattr(main.qdrant, "delete", delete_mock)

    with TestClient(main.app) as client:
        response = client.delete(f"/strategies/{strategy_id}", headers=AUTH_HEADER)

    assert response.status_code == 503
    assert delete_mock.called, (
        "a 503 must mean Qdrant was actually attempted and failed -- not "
        "that the handler never touched Qdrant at all"
    )
    detach_calls = _find_calls(run_calls, contains=("DETACH DELETE",))
    assert len(detach_calls) == 0, "a failed Qdrant delete must leave the node undeleted"


# ---------------------------------------------------------------------------
# 4. /retrieve must exclude quarantined strategies.
# ---------------------------------------------------------------------------


def test_retrieve_requires_bearer(monkeypatch):
    # Benign, not a raise: today's main.py has no auth check at all, so an
    # unauthenticated call actually reaches the embedder and Qdrant. Letting
    # that complete cleanly (returning 200) makes the 401 assertion below
    # fail with a clear status-code mismatch instead of an unhandled
    # exception mid-request.
    fake_vector = MagicMock()
    fake_vector.tolist.return_value = [0.0] * 384
    encode_mock = MagicMock(return_value=fake_vector)
    search_mock = MagicMock(return_value=[])
    monkeypatch.setattr(main.embedder, "encode", encode_mock)
    monkeypatch.setattr(main.qdrant, "search", search_mock)
    _install_fake_session(monkeypatch, [], lambda query, params: None)
    _stub_qdrant_startup(monkeypatch)

    with TestClient(main.app) as client:
        response = client.post("/retrieve", json={"query": "anything", "k": 1})

    assert response.status_code == 401
    assert encode_mock.called is False
    assert search_mock.called is False


def test_retrieve_excludes_result_whose_neo4j_row_is_quarantined(monkeypatch):
    """A quarantined hit is excluded; a hit whose row omits the key at all
    (the shape of every current, pre-quarantine StrategyItem) is NOT.

    A single quarantined-only fixture would let `strategy["quarantined"]`
    (a plain dict index, no default) satisfy `results == []` -- and that
    same expression would KeyError on every row that predates this field
    entirely. Pairing it with a hit that omits the key is what forces the
    real implementation to use `.get("quarantined") is True`, not a bare
    index or a truthiness check that blows up on a missing key.
    """
    quarantined_strategy_id = "strategy_quarantined_hit"
    clean_strategy_id = "strategy_predates_quarantine_field"

    quarantined_point = SimpleNamespace(
        id="qdrant-point-quarantined", score=0.95, payload={"strategy_id": quarantined_strategy_id}
    )
    clean_point = SimpleNamespace(
        id="qdrant-point-clean", score=0.80, payload={"strategy_id": clean_strategy_id}
    )
    search_mock = MagicMock(return_value=[quarantined_point, clean_point])
    monkeypatch.setattr(main.qdrant, "search", search_mock)

    fake_vector = MagicMock()
    fake_vector.tolist.return_value = [0.0] * 384
    monkeypatch.setattr(main.embedder, "encode", MagicMock(return_value=fake_vector))

    rows_by_strategy_id = {
        quarantined_strategy_id: {
            "strategy": {
                "id": quarantined_strategy_id,
                "success_rate": 1.0,
                "quarantined": True,
            },
            "source_traces": ["trace-x"],
            "contradictions": [{"title": None, "weight": None}],
        },
        # Deliberately no "quarantined" key at all -- not False, absent.
        clean_strategy_id: {
            "strategy": {"id": clean_strategy_id, "success_rate": 1.0},
            "source_traces": ["trace-y"],
            "contradictions": [{"title": None, "weight": None}],
        },
    }

    class _FakeResult:
        def __init__(self, value):
            self._value = value

        def single(self):
            return self._value

    class _FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def run(self, query, params=None):
            strategy_id = (params or {}).get("id")
            return _FakeResult(rows_by_strategy_id.get(strategy_id))

    monkeypatch.setattr(main.neo4j_driver, "session", lambda: _FakeSession())
    _stub_qdrant_startup(monkeypatch)

    with TestClient(main.app) as client:
        response = client.post(
            "/retrieve", json={"query": "anything", "k": 2}, headers=AUTH_HEADER
        )

    assert response.status_code == 200
    results = response.json()["results"]
    result_ids = [r["strategy"]["id"] for r in results]

    assert quarantined_strategy_id not in result_ids, (
        "a search hit whose Neo4j row is quarantined must be excluded from "
        "/retrieve results, not merely deprioritized"
    )
    assert clean_strategy_id in result_ids, (
        "a hit whose Neo4j row simply has no `quarantined` key (every "
        "pre-existing StrategyItem) must still be returned -- excluding it "
        "too means the filter isn't using `.get(...) is True`"
    )


def _has_condition(conditions, key, value):
    for condition in conditions:
        if getattr(condition, "key", None) != key:
            continue
        match = getattr(condition, "match", None)
        if getattr(match, "value", None) == value:
            return True
    return False


def test_retrieve_qdrant_filter_excludes_quarantined_via_must_not_while_task_type_stays_in_must(
    monkeypatch,
):
    search_mock = MagicMock(return_value=[])
    monkeypatch.setattr(main.qdrant, "search", search_mock)
    fake_vector = MagicMock()
    fake_vector.tolist.return_value = [0.0] * 384
    monkeypatch.setattr(main.embedder, "encode", MagicMock(return_value=fake_vector))
    _install_fake_session(monkeypatch, [], lambda query, params: None)
    _stub_qdrant_startup(monkeypatch)

    with TestClient(main.app) as client:
        response = client.post(
            "/retrieve",
            json={"query": "anything", "task_type": "code_review", "k": 3},
            headers=AUTH_HEADER,
        )

    assert response.status_code == 200
    assert search_mock.called
    query_filter = search_mock.call_args.kwargs.get("query_filter")
    assert query_filter is not None, "expected a query_filter to be passed to qdrant.search"

    must = list(getattr(query_filter, "must", None) or [])
    must_not = list(getattr(query_filter, "must_not", None) or [])

    assert _has_condition(must, "task_type", "code_review"), (
        "expected task_type to remain a `must` condition, not be dropped or "
        "moved when quarantine filtering is added"
    )
    assert not _has_condition(must, "quarantined", False), (
        "quarantine exclusion must be a `must_not quarantined == true` "
        "condition, not a `must quarantined == false` one -- the latter "
        "would reject every point that predates this field entirely "
        "(missing key, not False)"
    )
    assert _has_condition(must_not, "quarantined", True), (
        "expected a `must_not` condition excluding payload quarantined == "
        "true -- not a `must` condition requiring quarantined == false"
    )


def test_retrieve_qdrant_filter_still_excludes_quarantined_without_task_type(monkeypatch):
    """The quarantine exclusion is not conditional on task_type being set."""
    search_mock = MagicMock(return_value=[])
    monkeypatch.setattr(main.qdrant, "search", search_mock)
    fake_vector = MagicMock()
    fake_vector.tolist.return_value = [0.0] * 384
    monkeypatch.setattr(main.embedder, "encode", MagicMock(return_value=fake_vector))
    _install_fake_session(monkeypatch, [], lambda query, params: None)
    _stub_qdrant_startup(monkeypatch)

    with TestClient(main.app) as client:
        response = client.post(
            "/retrieve",
            json={"query": "anything", "k": 3},  # no task_type
            headers=AUTH_HEADER,
        )

    assert response.status_code == 200
    assert search_mock.called
    query_filter = search_mock.call_args.kwargs.get("query_filter")
    assert query_filter is not None, (
        "expected a query_filter carrying the quarantine exclusion even "
        "when there is no task_type to filter on"
    )

    must = list(getattr(query_filter, "must", None) or [])
    must_not = list(getattr(query_filter, "must_not", None) or [])
    assert _has_condition(must_not, "quarantined", True), (
        "the quarantine `must_not` condition must be present regardless of "
        "whether task_type is set -- it must not be attached only "
        "alongside a task_type `must` condition"
    )
    assert not _has_condition(must, "quarantined", False), (
        "quarantine exclusion must be a `must_not quarantined == true` "
        "condition -- not a `must` condition requiring quarantined == false "
        "-- even when task_type is absent and `must` would otherwise be "
        "empty"
    )


# ---------------------------------------------------------------------------
# 5. Ingest miss-path upsert must use the uuid5 point id, not the legacy
#    truncated int hash.
# ---------------------------------------------------------------------------


def test_ingest_miss_path_upserts_with_uuid5_point_id_not_truncated_int(monkeypatch):
    raw_reasoning = "learned to always check for race conditions in the queue drain path"
    task_type = "concurrency_review"
    trace_id = "trace-uuid5-miss-1"
    strategy_id = main.strategy_id_for(task_type=task_type, raw_reasoning=raw_reasoning)
    expected_point_id = _uuid5_point_id(strategy_id)
    legacy_point_id = _legacy_point_id(strategy_id)

    run_calls = []

    def router(query, params):
        return _route(query, existence_result=None, miss_create_result={"success_rate": 1.0})

    _install_fake_session(monkeypatch, run_calls, router)

    fake_extraction = {
        "title": "Queue Drain Guard",
        "description": "Check for race conditions before draining.",
        "conditions": {},
        "steps": ["check", "drain"],
        "success_rate": 1.0,
    }
    monkeypatch.setitem(
        main.BACKENDS, main.ExtractionBackend.CLAUDE, MagicMock(return_value=fake_extraction)
    )

    fake_vector = MagicMock()
    fake_vector.tolist.return_value = [0.0] * 384
    monkeypatch.setattr(main.embedder, "encode", MagicMock(return_value=fake_vector))
    upsert_mock = MagicMock()
    monkeypatch.setattr(main.qdrant, "upsert", upsert_mock)
    _stub_qdrant_startup(monkeypatch)

    with TestClient(main.app) as client:
        response = client.post(
            "/traces",
            json={
                "trace_id": trace_id,
                "task_id": "task-1",
                "task_type": task_type,
                "raw_reasoning": raw_reasoning,
                "outcome": "success",
                "backend": "claude",
            },
            headers=AUTH_HEADER,
        )

    assert response.status_code == 200
    assert upsert_mock.called
    call = upsert_mock.call_args
    points = call.kwargs.get("points") or next(
        (a for a in call.args if isinstance(a, list)), None
    )
    assert points, f"could not find `points` in upsert call: {call}"
    point_id = getattr(points[0], "id", None)
    if point_id is None and isinstance(points[0], dict):
        point_id = points[0].get("id")

    assert point_id == expected_point_id, (
        f"expected the uuid5 point id {expected_point_id!r}, got "
        f"{point_id!r} (the legacy truncated int would be {legacy_point_id!r})"
    )
