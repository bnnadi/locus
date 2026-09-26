"""GitHub issue #11: the miss/create path must return the STORED row, not
the extraction dict it just handed to the ``MERGE (s:StrategyItem`` query.

Today ``ingest_trace``'s miss-success branch (main.py, step 11) builds its
``StrategyOut`` straight from ``extracted`` (the backend's return value) and
embeds/upserts ``f"{extracted['title']}. {extracted['description']}"``. The
``MERGE ... RETURN s.success_rate AS success_rate`` query only ever projects
``success_rate`` -- title/description/conditions/steps never round-trip
through Neo4j at all on this path. That is invisible on a fresh
``ON CREATE`` (the stored row and the extraction are identical, since the
``MERGE`` just wrote the extraction's own values). It stops being invisible
the moment ``ON MATCH`` can fire on a re-run of the *same* strategy identity
with a *different* extraction, or once any other writer has touched the
row -- the response and the embed/upsert must reflect what Neo4j actually
holds, not what this one request's backend call happened to return.

The fix under test: after the ``MERGE``, ``RETURN`` (and read) the stored
``s.title``, ``s.description``, ``s.conditions``, ``s.steps`` alongside
``s.success_rate``, and build the response / embed text / Qdrant payload
from those, not from ``extracted``.

Test A pins the response/embed/payload contract using a stored row that
deliberately disagrees with the extraction on every field. Test B locks the
Cypher: ON CREATE still assigns the model-authored fields (unchanged), the
window from ON MATCH through RETURN must never assign them (a future
ON MATCH overwrite would be exactly the same regression this issue is
about), and RETURN must project them.
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
os.environ.setdefault("HERMES_MEMORY_ROUTER_TOKEN", "router-test-token")

_ROUTER_DIR = str(Path(__file__).resolve().parents[2] / "services" / "hermes-memory-router")
if _ROUTER_DIR not in sys.path:
    sys.path.insert(0, _ROUTER_DIR)

import main  # noqa: E402

# tests/ is not a package (no __init__.py). Pytest prepends tests/unit, so
# the sibling module is importable by filename, not as tests.unit.*. Do not
# import main a second time under a different module name here -- this file
# and the sibling module must both bind sys.modules["main"] to the same
# object, which they do because both resolve the same path via the same
# sys.path insertion above.
from fastapi.testclient import TestClient  # noqa: E402
from test_ingest_trace_skips_existing_extraction import (  # noqa: E402
    _extract_upsert_points,
    _find_calls,
    _install_fake_session,
    _point_payload,
    _post,
    _route,
    _stub_qdrant_startup,
)

# ---------------------------------------------------------------------------
# A. The response body, embed text, and Qdrant payload must all reflect the
#    STORED row, not the extraction dict -- must FAIL today for the right
#    reason (wrong values), not a KeyError.
# ---------------------------------------------------------------------------


def test_miss_merge_response_uses_stored_text_not_extraction(monkeypatch):
    run_calls = []

    stored_title = "Stored Miss Title"
    stored_description = "Stored miss description"
    stored_conditions_json = json.dumps({"lane": "stored"})
    stored_steps = ["stored-step"]
    stored_success_rate = 0.25

    miss_create_result = {
        "success_rate": stored_success_rate,
        "title": stored_title,
        "description": stored_description,
        "conditions": stored_conditions_json,
        "steps": stored_steps,
    }

    def router(query, params):
        return _route(query, existence_result=None, miss_create_result=miss_create_result)

    _install_fake_session(monkeypatch, run_calls, router)

    # Deliberately disagrees with the stored row on every field, including
    # success_rate -- if the handler builds its response from `extracted`
    # instead of the stored row, every assertion below catches it.
    fake_extraction = {
        "title": "Extracted Title",
        "description": "Extracted description",
        "conditions": {"lane": "extracted"},
        "steps": ["extracted-step"],
        "success_rate": 1.0,
    }
    backend_mock = MagicMock(return_value=fake_extraction)
    monkeypatch.setitem(main.BACKENDS, main.ExtractionBackend.CLAUDE, backend_mock)

    fake_vector = MagicMock()
    fake_vector.tolist.return_value = [0.0] * 384
    encode_mock = MagicMock(return_value=fake_vector)
    monkeypatch.setattr(main.embedder, "encode", encode_mock)

    upsert_mock = MagicMock()
    monkeypatch.setattr(main.qdrant, "upsert", upsert_mock)

    _stub_qdrant_startup(monkeypatch)
    with TestClient(main.app) as client:
        response = _post(client, backend="claude")

    assert response.status_code == 200
    data = response.json()

    assert data["title"] == stored_title
    assert data["description"] == stored_description
    assert data["conditions"] == {"lane": "stored"}  # parsed object, not the JSON string
    assert data["steps"] == stored_steps
    assert data["success_rate"] == stored_success_rate

    assert encode_mock.called, "expected embedder.encode to be called"
    encode_args = "".join(str(a) for a in encode_mock.call_args[0]) + "".join(
        str(v) for v in encode_mock.call_args[1].values()
    )
    assert encode_args == f"{stored_title}. {stored_description}"

    points = _extract_upsert_points(upsert_mock)
    assert len(points) == 1
    payload = _point_payload(points[0])
    assert payload["title"] == stored_title


# ---------------------------------------------------------------------------
# B. Cypher lock: ON CREATE keeps assigning the model-authored fields, the
#    window from ON MATCH through RETURN never (re-)assigns them, and
#    RETURN must project them for the handler to read. Must FAIL today only
#    on the RETURN-aliases pin.
# ---------------------------------------------------------------------------


def test_miss_merge_returns_stored_fields_and_does_not_overwrite_them_on_match(monkeypatch):
    run_calls = []

    miss_create_result = {
        "success_rate": 1.0,
        "title": "Stored Lock Title",
        "description": "Stored lock description",
        "conditions": json.dumps({}),
        "steps": [],
    }

    def router(query, params):
        return _route(query, existence_result=None, miss_create_result=miss_create_result)

    _install_fake_session(monkeypatch, run_calls, router)

    fake_extraction = {
        "title": "Extracted Lock Title",
        "description": "Extracted lock description",
        "conditions": {},
        "steps": [],
        "success_rate": 1.0,
    }
    monkeypatch.setitem(
        main.BACKENDS, main.ExtractionBackend.CLAUDE, MagicMock(return_value=fake_extraction)
    )

    fake_vector = MagicMock()
    fake_vector.tolist.return_value = [0.0] * 384
    monkeypatch.setattr(main.embedder, "encode", MagicMock(return_value=fake_vector))
    monkeypatch.setattr(main.qdrant, "upsert", MagicMock())

    _stub_qdrant_startup(monkeypatch)
    with TestClient(main.app) as client:
        response = _post(client, backend="claude")

    assert response.status_code == 200

    merge_calls = _find_calls(run_calls, contains=("MERGE (s:StrategyItem",))
    assert len(merge_calls) == 1, (
        f"expected exactly one MERGE (s:StrategyItem query, got {len(merge_calls)}"
    )
    query = merge_calls[0]["query"]

    # --- Pins that PASS today ---

    on_create_idx = query.index("ON CREATE")
    on_match_idx = query.index("ON MATCH")
    on_create_segment = query[on_create_idx:on_match_idx]
    for field in ("title", "description", "conditions", "steps"):
        assert re.search(rf"s\.{field}\s*=\s*\${field}\b", on_create_segment) is not None, (
            f"expected ON CREATE to assign s.{field} = ${field}; got: "
            f"{on_create_segment!r}"
        )

    # From ON MATCH through RETURN (the entire rest of the query, not
    # merely up to the first WITH) -- a future ON MATCH SET (or a later SET
    # after the success_rate WITH) reassigning any of these from `extracted`
    # would land inside this window and must fail here.
    on_match_through_return_segment = query[on_match_idx:]
    for field in ("title", "description", "conditions", "steps"):
        assert re.search(rf"s\.{field}\s*=", on_match_through_return_segment) is None, (
            f"expected no s.{field} = assignment between ON MATCH and RETURN "
            f"(inclusive); found one in: {on_match_through_return_segment!r}"
        )

    assert "extraction_status = 'extracted'" in query

    # --- Red today: RETURN must project the actual node fields via their
    # own `s.<field> AS <field>` source expression, not merely have an
    # output column that happens to be named e.g. "title" from somewhere
    # else. A query like:
    #
    #   WITH $title AS title, $description AS description, ...
    #   RETURN title, description, conditions, steps, success_rate
    #
    # would satisfy an alias-name-only check (e.g. via
    # _parse_return_aliases) while never reading anything from the stored
    # StrategyItem node -- production would still be free to answer from
    # `extracted` and hardcode success_rate, and this test would wrongly
    # stay green. Requiring the literal `s.<field> AS <field>` source
    # expression is what actually forces a read of the stored row.
    return_match = re.search(r"RETURN\s+(.*)", query, re.DOTALL | re.IGNORECASE)
    assert return_match is not None, f"expected a RETURN clause in: {query!r}"
    return_clause = return_match.group(1)
    for field in ("title", "description", "conditions", "steps", "success_rate"):
        pattern = rf"s\.{field}\s+AS\s+{field}\b"
        assert re.search(pattern, return_clause, re.IGNORECASE) is not None, (
            f"expected RETURN to project s.{field} AS {field} (read from the "
            f"stored StrategyItem node), not merely an output column named "
            f"{field!r} sourced from something else; RETURN clause: "
            f"{return_clause!r}"
        )
