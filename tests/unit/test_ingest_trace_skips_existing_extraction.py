"""GitHub issue #4: ingest_trace must not discard an existing StrategyItem.

Today ``ingest_trace`` calls ``BACKENDS[backend]`` before it knows whether a
``StrategyItem`` already exists for this ``(task_type, raw_reasoning)`` (or
``strategy_key``) identity. ``strategy_id_for`` never depends on model
output, so on a repeat trace the freshly extracted title/description/
conditions/steps are thrown away and only the Neo4j counters move -- yet the
handler still pays for a real extraction call every time, and the failure
path (``extraction_status = 'failed'``) never records which backend failed.

These tests pin the control flow the fix must implement:

1. The trace MERGE into ReasoningTrace still runs first, unconditionally.
2. An existence *read* (``MATCH``, not ``MERGE``) on StrategyItem runs next,
   keyed by ``strategy_id_for`` on the (redacted) reasoning / strategy_key.
3. A hit (a row with a non-empty string ``strategy_id``) skips the
   extraction backend entirely. If the strategy's embedding is already
   synced, it also skips the embedder and Qdrant. If not yet synced, it
   still skips the backend but re-embeds from the *stored* title/description
   and upserts to Qdrant, healing a first ingest that wrote to Neo4j and
   then died before ``embedding_synced`` was set.
4. A miss (no row, or a row missing/blank ``strategy_id``) still calls the
   extraction backend and runs the existing create path.
5. On extraction failure, the failure write records both
   ``extraction_status = 'failed'`` and ``extraction_backend`` (as the
   backend's ``.value`` string, not its ``repr``).

The fake Neo4j session below only hands a query's ``.single()`` row the
fields that query's own ``RETURN`` clause actually projects. This matters:
a fixture that always contains ``title``/``description``/``embedding_synced``
regardless of what the query asked for would let a handler pass these tests
by accident, without the RETURN clause the real fix needs to have.

Imports main.py directly (not identity.py/redact.py) so this exercises the
real request path, mirroring the harness in test_ingest_trace_redacts.py.
main.py builds its Neo4j/Qdrant/embedder clients at import time, so required
env vars are set with setdefault before the import.
"""

import json
import os
import re
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

# ---------------------------------------------------------------------------
# Shared fake-Neo4j harness
# ---------------------------------------------------------------------------


_RETURN_ITEM_RE = re.compile(
    r"^([A-Za-z_][A-Za-z0-9_.]*)\s*(?:AS\s+([A-Za-z_][A-Za-z0-9_]*))?$",
    re.IGNORECASE,
)


def _parse_return_aliases(query: str) -> list[str] | None:
    """Return the output aliases of a Cypher query's RETURN clause.

    ``None`` means the query has no RETURN at all (a plain write). Each item
    resolves to its ``AS`` alias when present, otherwise the identifier's
    last dotted segment (``s.title`` -> ``title``), matching how the real
    handler would name its row keys.
    """
    match = re.search(r"RETURN\s+(.*)", query, re.DOTALL | re.IGNORECASE)
    if not match:
        return None
    aliases: list[str] = []
    for part in match.group(1).split(","):
        part = part.strip()
        if not part:
            continue
        item_match = _RETURN_ITEM_RE.match(part)
        if item_match:
            base, alias = item_match.groups()
            aliases.append(alias if alias else base.rsplit(".", 1)[-1])
        else:
            # Defensive fallback for a shape this test harness doesn't model
            # (e.g. a map projection) -- take the first token's last segment
            # rather than silently dropping it.
            aliases.append(part.split()[0].rsplit(".", 1)[-1])
    return aliases


def _project_row(query: str, fixture: dict) -> dict | None:
    """Keep only the fixture keys the query's own RETURN actually names.

    A query with no RETURN clause projects *nothing*: real Neo4j has no row
    shape to hand `.single()` in that case, so the fake must not either --
    otherwise an existence MATCH that forgot its RETURN would still be
    handed `strategy_id`/`embedding_synced` from the fixture, making a hit
    look real when production would actually get no row back (and would
    keep calling the extractor on every repeat). Writes that never call
    `.single()` (the trace MERGE, the final `embedding_synced = true` flip)
    are unaffected either way, since nothing reads this value for those.

    A query WITH a RETURN only gets the subset of the fixture that RETURN
    actually lists -- if the implementation forgets to return
    `embedding_synced`, the handler still never sees it.
    """
    aliases = _parse_return_aliases(query)
    if aliases is None:
        return None
    return {alias: fixture[alias] for alias in aliases if alias in fixture}


class _FakeResult:
    def __init__(self, value):
        self._value = value

    def single(self):
        return self._value


def _install_fake_session(monkeypatch, run_calls, router):
    """Route every session.run() call through `router(query, params)`.

    `router` returns the raw fixture dict for that call (or None for "no
    row"). The handler is not required to call `.single()` on every result
    -- in particular, the initial ReasoningTrace MERGE must not be assumed
    to. When a fixture is returned, it is filtered through `_project_row`
    before being handed back, so the handler only ever sees fields its own
    query actually asked for.
    """

    class _FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def run(self, query, params=None):
            params = params or {}
            run_calls.append({"query": query, "params": params})
            fixture = router(query, params)
            if fixture is None:
                return _FakeResult(None)
            return _FakeResult(_project_row(query, fixture))

    monkeypatch.setattr(main.neo4j_driver, "session", lambda: _FakeSession())


def _route(
    query,
    *,
    existence_result=None,
    reuse_result=None,
    miss_create_result=None,
    failure_result=None,
):
    """Classify a Cypher string by shape, not by exact text.

    - Trace MERGE: "MERGE (rt:ReasoningTrace" -> irrelevant to routing here.
    - Failure write: sets extraction_status = 'failed'.
    - Miss/create path (existing behavior): "MERGE (s:StrategyItem".
    - Existence read: "MATCH (s:StrategyItem" with no mutating SET.
    - Reuse update: "MATCH (s:StrategyItem" with a SET (count bump).
    """
    if "extraction_status = 'failed'" in query:
        return failure_result
    if "MERGE (s:StrategyItem" in query:
        return miss_create_result
    if "MATCH (s:StrategyItem" in query:
        if "SET" in query:
            return reuse_result
        return existence_result
    return None


def _find_calls(run_calls, *, contains=(), not_contains=()):
    return [
        c
        for c in run_calls
        if all(s in c["query"] for s in contains)
        and all(s not in c["query"] for s in not_contains)
    ]


def _find_one(run_calls, *, contains=(), not_contains=(), label=""):
    matches = _find_calls(run_calls, contains=contains, not_contains=not_contains)
    assert len(matches) == 1, (
        f"expected exactly one {label or contains!r} call, got {len(matches)}: "
        f"{[c['query'] for c in run_calls]}"
    )
    return matches[0]


def _assignment_segment(query: str, assignment_marker: str) -> str:
    """Return only the RHS text of ONE assignment, not its neighbors.

    Starting at `assignment_marker` (e.g. "s.success_count = s.success_count
    +"), this returns up to that assignment's own closing `END` (inclusive)
    if a CASE expression reaches one before the next top-level comma,
    otherwise up to that next comma. Without this, a later `$success`
    belonging to a *different* assignment in the same SET clause can sit
    inside a naive fixed-size window after this one and be mistaken for
    this assignment's own param reference.
    """
    start = query.index(assignment_marker)
    comma_idx = query.find(",", start)
    end_kw_idx = query.find("END", start)
    if end_kw_idx != -1 and (comma_idx == -1 or end_kw_idx < comma_idx):
        return query[start : end_kw_idx + len("END")]
    if comma_idx != -1:
        return query[start:comma_idx]
    return query[start:]


def _bool_false_param_key(params: dict) -> str:
    """Return the name of the (single) boolean-False-valued param.

    The reuse Cypher must bump failure_count using whatever boolean param
    the request's outcome maps to -- production code names it `success`,
    but this doesn't assume that name, only that some boolean param is
    False for a failure-outcome trace and that it is referenced (as `$name`)
    in the query.
    """
    keys = [k for k, v in params.items() if v is False]
    assert keys, f"expected a boolean False-valued param, got {params!r}"
    return keys[0]


def _extract_upsert_points(upsert_mock):
    call = upsert_mock.call_args
    assert call is not None, "qdrant.upsert was not called"
    if "points" in call.kwargs:
        return call.kwargs["points"]
    for arg in call.args:
        if isinstance(arg, list):
            return arg
    raise AssertionError(f"could not find `points` in upsert call: {call}")


def _point_payload(point):
    payload = getattr(point, "payload", None)
    if payload is None and isinstance(point, dict):
        payload = point.get("payload")
    assert payload is not None, f"could not find a payload on upsert point: {point!r}"
    return payload


def _post(client, **overrides):
    body = {
        "trace_id": "trace-1",
        "task_id": "task-1",
        "task_type": "code_review",
        "raw_reasoning": "checked the diff for missing null checks before approving",
        "outcome": "success",
        "backend": "claude",
    }
    body.update(overrides)
    return client.post("/traces", json=body)


# ---------------------------------------------------------------------------
# 1. Hit, embedding already synced: skip backend, encode, and upsert.
# ---------------------------------------------------------------------------


def test_ingest_trace_existing_strategy_skips_backend(monkeypatch):
    raw_reasoning = "checked the diff for missing null checks before approving"
    task_type = "code_review"
    trace_id = "trace-hit-1"
    strategy_id = main.strategy_id_for(task_type=task_type, raw_reasoning=raw_reasoning)

    run_calls = []
    existence_result = {"strategy_id": strategy_id, "embedding_synced": True}
    reuse_result = {
        "success_rate": 0.5,
        "title": "Stored Title",
        "description": "Stored description",
        "conditions": json.dumps({"when": "x"}),
        "steps": ["step"],
        "embedding_synced": True,
    }
    router = lambda query, params: _route(  # noqa: E731
        query, existence_result=existence_result, reuse_result=reuse_result
    )
    _install_fake_session(monkeypatch, run_calls, router)

    # Not a side_effect raise: main.py's broad except-Exception around the
    # backend call would swallow an AssertionError raised there and turn it
    # into an opaque 502, hiding the real reason this should fail. A benign
    # sentinel return value lets a wrongful call surface as a clear
    # `.called` assertion instead.
    backend_mock = MagicMock(
        return_value={
            "title": "WRONGLY_EXTRACTED",
            "description": "WRONGLY_EXTRACTED",
            "conditions": {},
            "steps": [],
            "success_rate": 0.0,
        }
    )
    monkeypatch.setitem(main.BACKENDS, main.ExtractionBackend.OLLAMA, backend_mock)

    fake_vector = MagicMock()
    fake_vector.tolist.return_value = [0.0] * 384
    encode_mock = MagicMock(return_value=fake_vector)
    monkeypatch.setattr(main.embedder, "encode", encode_mock)

    upsert_mock = MagicMock()
    monkeypatch.setattr(main.qdrant, "upsert", upsert_mock)

    client = TestClient(main.app)
    response = _post(
        client,
        trace_id=trace_id,
        raw_reasoning=raw_reasoning,
        task_type=task_type,
        outcome="failure",
        backend="ollama",
    )

    assert response.status_code == 200

    assert backend_mock.called is False
    assert encode_mock.called is False
    assert upsert_mock.called is False

    data = response.json()
    assert data["title"] == "Stored Title"
    assert data["description"] == "Stored description"
    assert data["conditions"] == {"when": "x"}  # parsed dict, not the JSON string
    assert data["steps"] == ["step"]
    assert data["success_rate"] == 0.5

    trace_merge_call = _find_one(
        run_calls, contains=("ReasoningTrace", "MERGE"), label="trace MERGE"
    )
    assert "raw_reasoning" in trace_merge_call["params"]

    existence_call = _find_one(
        run_calls,
        contains=("MATCH (s:StrategyItem",),
        not_contains=("MERGE", "SET"),
        label="existence read",
    )
    assert "MERGE" not in existence_call["query"]

    # The hit predicate depends on the row having a usable strategy_id.
    # With no RETURN naming it, the fake now correctly refuses to leak it
    # (see _project_row), so require the query to actually ask for it.
    existence_aliases = _parse_return_aliases(existence_call["query"]) or []
    assert "strategy_id" in existence_aliases, (
        "expected the existence read to RETURN s.id AS strategy_id (or "
        f"equivalent); got RETURN aliases {existence_aliases}"
    )

    reuse_call = _find_one(
        run_calls,
        contains=("MATCH (s:StrategyItem", "s.success_count = s.success_count +"),
        not_contains=("MERGE (s:StrategyItem",),
        label="reuse update",
    )
    reuse_query = reuse_call["query"]

    # --- count bump: both counters, using the same boolean outcome param ---
    assert "s.success_count = s.success_count +" in reuse_query
    assert "s.failure_count = s.failure_count +" in reuse_query

    success_param_key = _bool_false_param_key(reuse_call["params"])
    success_bump_idx = reuse_query.index("s.success_count = s.success_count +")

    # Scoped to THIS assignment's own text only (through its own CASE...END,
    # or up to the next comma if it has no CASE) -- a fixed-size character
    # window here would let a `$<param>` reference that actually belongs to
    # a *different*, later assignment in the same SET clause satisfy both
    # checks even though this assignment never references it at all, e.g.:
    #
    #   SET s.success_count = s.success_count + 1,
    #       s.failure_count = s.failure_count + 1,
    #       s.flag = $success
    #
    # must fail here, not pass.
    success_segment = _assignment_segment(
        reuse_query, "s.success_count = s.success_count +"
    )
    failure_segment = _assignment_segment(
        reuse_query, "s.failure_count = s.failure_count +"
    )

    success_arm_re = re.compile(
        rf"CASE\s+WHEN\s+\${re.escape(success_param_key)}\s+THEN\s+1\s+ELSE\s+0\s+END",
        re.IGNORECASE,
    )
    failure_arm_re = re.compile(
        rf"CASE\s+WHEN\s+\${re.escape(success_param_key)}\s+THEN\s+0\s+ELSE\s+1\s+END",
        re.IGNORECASE,
    )
    assert success_arm_re.search(success_segment) is not None, (
        f"expected `CASE WHEN ${success_param_key} THEN 1 ELSE 0 END` inside "
        f"the success_count assignment itself, got: {success_segment!r}"
    )
    assert failure_arm_re.search(failure_segment) is not None, (
        f"expected `CASE WHEN ${success_param_key} THEN 0 ELSE 1 END` inside "
        f"the failure_count assignment itself, got: {failure_segment!r}"
    )

    assert "last_validated" in reuse_query
    assert "DERIVES_STRATEGY" in reuse_query
    assert trace_id in reuse_call["params"].values(), (
        "expected this request's trace_id among the reuse query params "
        "(the DERIVES_STRATEGY link is for this trace)"
    )

    # --- success_rate: a *following* SET after WITH s, using the exact
    # post-bump ratio -- not a single combined SET (which would read the
    # pre-bump counters) and not a constant.
    idx_count_set = success_bump_idx
    idx_rate_set = reuse_query.index("s.success_rate =", idx_count_set)
    with_segment = reuse_query[idx_count_set:idx_rate_set]
    assert re.search(r"WITH\s+s\b", with_segment) is not None, (
        "expected `WITH s` between the count bump SET and the success_rate SET"
    )
    assert (
        "toFloat(s.success_count) / (s.success_count + s.failure_count)" in reuse_query
    ), "success_rate must be the exact post-bump ratio, not a constant or pre-bump formula"

    # --- extraction_status only flips to 'reused' from 'pending' ---
    reused_from_pending = re.search(
        r"CASE\s+WHEN\s+rt\.extraction_status\s*=\s*'pending'"
        r"\s+THEN\s+'reused'\s+ELSE\s+rt\.extraction_status\s+END",
        reuse_query,
        re.IGNORECASE | re.DOTALL,
    )
    assert reused_from_pending is not None, (
        "expected `CASE WHEN rt.extraction_status = 'pending' THEN 'reused' "
        "ELSE rt.extraction_status END` (or equally strict), not merely both "
        "string literals appearing somewhere"
    )

    # --- hit path must not clobber model-authored fields or claim a backend
    # ran; whitespace-insensitive so `s.title=$title` doesn't slip past.
    assert re.search(r"s\.title\s*=", reuse_query) is None
    assert "extraction_backend" not in reuse_query

    # --- RETURN must actually project everything the response body needs;
    # a fixture key the query doesn't ask for never reaches the handler.
    reuse_aliases = _parse_return_aliases(reuse_query) or []
    for expected in ("success_rate", "title", "description", "conditions", "steps"):
        assert expected in reuse_aliases, f"expected {expected!r} in reuse RETURN, got {reuse_aliases}"


# ---------------------------------------------------------------------------
# 2. Hit via explicit strategy_key: identity comes from the key, not the hash
#    of raw_reasoning.
# ---------------------------------------------------------------------------


def test_ingest_trace_strategy_key_hit_skips_backend(monkeypatch):
    strategy_key = "custom-key-42"
    raw_reasoning = "this reasoning text would hash to a completely different id"
    task_type = "incident_response"
    expected_id = main.strategy_id_for(
        task_type=task_type, raw_reasoning=raw_reasoning, strategy_key=strategy_key
    )
    hash_based_id = main.strategy_id_for(task_type=task_type, raw_reasoning=raw_reasoning)
    assert expected_id != hash_based_id  # sanity: the key actually changes identity

    run_calls = []
    existence_result = {"strategy_id": expected_id, "embedding_synced": True}
    reuse_result = {
        "success_rate": 0.9,
        "title": "Stored Title Key",
        "description": "Stored Desc Key",
        "conditions": json.dumps({}),
        "steps": ["only-step"],
        "embedding_synced": True,
    }
    router = lambda query, params: _route(  # noqa: E731
        query, existence_result=existence_result, reuse_result=reuse_result
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
    fake_vector = MagicMock()
    fake_vector.tolist.return_value = [0.0] * 384
    encode_mock = MagicMock(return_value=fake_vector)
    monkeypatch.setattr(main.embedder, "encode", encode_mock)
    upsert_mock = MagicMock()
    monkeypatch.setattr(main.qdrant, "upsert", upsert_mock)

    client = TestClient(main.app)
    response = _post(
        client,
        raw_reasoning=raw_reasoning,
        task_type=task_type,
        strategy_key=strategy_key,
        backend="claude",
    )

    assert response.status_code == 200
    assert backend_mock.called is False
    assert encode_mock.called is False
    assert upsert_mock.called is False
    assert response.json()["title"] == "Stored Title Key"

    existence_call = _find_one(
        run_calls,
        contains=("MATCH (s:StrategyItem",),
        not_contains=("MERGE", "SET"),
        label="existence read",
    )
    assert expected_id in existence_call["params"].values()
    assert hash_based_id not in existence_call["params"].values()


# ---------------------------------------------------------------------------
# 3. Miss: backend still runs, create path unchanged.
# ---------------------------------------------------------------------------


def test_ingest_trace_missing_strategy_calls_backend(monkeypatch):
    run_calls = []
    router = lambda query, params: _route(  # noqa: E731
        query, existence_result=None, miss_create_result={"success_rate": 1.0}
    )
    _install_fake_session(monkeypatch, run_calls, router)

    fake_extraction = {
        "title": "Extracted Title",
        "description": "Extracted description",
        "conditions": {"only": "here"},
        "steps": ["a", "b"],
        "success_rate": 1.0,
    }
    backend_mock = MagicMock(return_value=fake_extraction)
    monkeypatch.setitem(main.BACKENDS, main.ExtractionBackend.CLAUDE, backend_mock)

    fake_vector = MagicMock()
    fake_vector.tolist.return_value = [0.0] * 384
    monkeypatch.setattr(main.embedder, "encode", MagicMock(return_value=fake_vector))
    upsert_mock = MagicMock()
    monkeypatch.setattr(main.qdrant, "upsert", upsert_mock)

    client = TestClient(main.app)
    response = _post(client, backend="claude")

    assert response.status_code == 200
    assert response.json()["title"] == "Extracted Title"
    assert backend_mock.call_count == 1
    assert upsert_mock.call_count == 1

    extracted_calls = _find_calls(run_calls, contains=("extraction_status = 'extracted'",))
    assert len(extracted_calls) >= 1


# ---------------------------------------------------------------------------
# 4. Existence row present but without a usable strategy_id: still a miss.
# ---------------------------------------------------------------------------


def test_ingest_trace_existence_row_without_strategy_id_calls_backend(monkeypatch):
    run_calls = []
    router = lambda query, params: _route(  # noqa: E731
        query,
        existence_result={"success_rate": 1.0},  # no strategy_id key -> not a hit
        miss_create_result={"success_rate": 1.0},
    )
    _install_fake_session(monkeypatch, run_calls, router)

    fake_extraction = {
        "title": "Extracted Title 2",
        "description": "d",
        "conditions": {},
        "steps": ["s"],
        "success_rate": 1.0,
    }
    backend_mock = MagicMock(return_value=fake_extraction)
    monkeypatch.setitem(main.BACKENDS, main.ExtractionBackend.CLAUDE, backend_mock)

    fake_vector = MagicMock()
    fake_vector.tolist.return_value = [0.0] * 384
    monkeypatch.setattr(main.embedder, "encode", MagicMock(return_value=fake_vector))
    monkeypatch.setattr(main.qdrant, "upsert", MagicMock())

    client = TestClient(main.app)
    response = _post(client, backend="claude")

    assert response.status_code == 200
    assert backend_mock.call_count == 1

    assert "ReasoningTrace" in run_calls[0]["query"]
    assert "MERGE" in run_calls[0]["query"]


# ---------------------------------------------------------------------------
# 5. Hit but embedding not yet synced: still skip backend, but re-embed from
#    the *stored* fields and upsert.
# ---------------------------------------------------------------------------


def test_ingest_trace_unsynced_strategy_reembeds_without_backend(monkeypatch):
    raw_reasoning = "reused reasoning whose strategy row exists but never got embedded"
    task_type = "deploy_review"
    strategy_id = main.strategy_id_for(task_type=task_type, raw_reasoning=raw_reasoning)

    run_calls = []
    stored_title = "Stored Title 5"
    stored_description = "Stored Desc 5"
    # None, not False: the healing predicate is "not strictly True" (covers a
    # column that came back null, or one a query forgot to RETURN and so is
    # simply absent). A handler that only special-cases `is False` would
    # wrongly skip the heal here; only `is not True` heals correctly.
    existence_result = {"strategy_id": strategy_id, "embedding_synced": None}
    reuse_result = {
        "success_rate": 0.7,
        "title": stored_title,
        "description": stored_description,
        "conditions": json.dumps({}),
        "steps": ["s5"],
        "embedding_synced": None,
    }
    router = lambda query, params: _route(  # noqa: E731
        query, existence_result=existence_result, reuse_result=reuse_result
    )
    _install_fake_session(monkeypatch, run_calls, router)

    backend_mock = MagicMock(
        return_value={
            "title": "SHOULD_NOT_BE_USED",
            "description": "SHOULD_NOT_BE_USED",
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

    client = TestClient(main.app)
    response = _post(client, raw_reasoning=raw_reasoning, task_type=task_type, backend="claude")

    assert response.status_code == 200
    assert backend_mock.called is False
    assert encode_mock.call_count == 1
    assert upsert_mock.call_count == 1

    existence_call = _find_one(
        run_calls,
        contains=("MATCH (s:StrategyItem",),
        not_contains=("MERGE", "SET"),
        label="existence read",
    )
    reuse_call = _find_one(
        run_calls,
        contains=("MATCH (s:StrategyItem", "s.success_count = s.success_count +"),
        not_contains=("MERGE (s:StrategyItem",),
        label="reuse update",
    )
    existence_aliases = _parse_return_aliases(existence_call["query"]) or []
    reuse_aliases = _parse_return_aliases(reuse_call["query"]) or []

    # The handler can only see embedding_synced/title/description if some
    # query actually RETURNs them -- the fake will not hand these over for
    # free (see _project_row). Require the query that decides to heal, and
    # the one supplying re-embed input, to actually ask for these columns.
    assert "embedding_synced" in existence_aliases or "embedding_synced" in reuse_aliases, (
        "expected embedding_synced to be RETURNed by the existence read or "
        "the reuse update"
    )
    assert "title" in existence_aliases or "title" in reuse_aliases, (
        "expected title to be RETURNed so the handler can re-embed from it"
    )
    assert "description" in existence_aliases or "description" in reuse_aliases, (
        "expected description to be RETURNed so the handler can re-embed from it"
    )

    encode_args = "".join(str(a) for a in encode_mock.call_args[0]) + "".join(
        str(v) for v in encode_mock.call_args[1].values()
    )
    assert stored_title in encode_args
    assert stored_description in encode_args
    assert "SHOULD_NOT_BE_USED" not in encode_args

    points = _extract_upsert_points(upsert_mock)
    assert len(points) == 1
    payload = _point_payload(points[0])
    assert payload["title"] == stored_title
    assert payload["title"] != "SHOULD_NOT_BE_USED"


# ---------------------------------------------------------------------------
# 6. Extraction failure on a miss: 502, and the failure write records the
#    backend that failed.
# ---------------------------------------------------------------------------


def test_ingest_trace_extraction_failure_records_backend(monkeypatch):
    run_calls = []
    router = lambda query, params: _route(  # noqa: E731
        query, existence_result=None, failure_result=None
    )
    _install_fake_session(monkeypatch, run_calls, router)

    backend_mock = MagicMock(side_effect=RuntimeError("boom"))
    monkeypatch.setitem(main.BACKENDS, main.ExtractionBackend.OLLAMA, backend_mock)
    monkeypatch.setattr(main.embedder, "encode", MagicMock(side_effect=AssertionError("no")))
    monkeypatch.setattr(main.qdrant, "upsert", MagicMock(side_effect=AssertionError("no")))

    client = TestClient(main.app)
    response = _post(client, backend="ollama")

    assert response.status_code == 502
    assert backend_mock.call_count == 1

    assert len(run_calls) >= 2
    assert "ReasoningTrace" in run_calls[0]["query"]
    assert "MERGE" in run_calls[0]["query"]

    existence_call = run_calls[1]
    assert "MATCH (s:StrategyItem" in existence_call["query"]
    assert "MERGE" not in existence_call["query"]

    failure_call = _find_one(
        run_calls,
        contains=("extraction_status = 'failed'",),
        label="failure write",
    )

    # Must be a parameter reference (`$name`), not a string literal --
    # otherwise an unused "ollama" param, or a hardcoded 'claude', would
    # slip through unnoticed. The param does not have to be named
    # `extraction_backend`; only that some param is assigned there and that
    # its value is the backend's `.value` string.
    backend_assignment = re.search(
        r"extraction_backend\s*=\s*\$([A-Za-z_][A-Za-z0-9_]*)", failure_call["query"]
    )
    assert backend_assignment is not None, (
        "expected an `extraction_backend = $<param>` assignment in the "
        "failure write, not a literal or an unused param"
    )
    backend_param_name = backend_assignment.group(1)
    backend_param_value = failure_call["params"].get(backend_param_name)
    assert backend_param_value == "ollama"
    assert "ExtractionBackend" not in str(backend_param_value)
