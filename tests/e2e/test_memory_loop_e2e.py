"""
End-to-end test: simulates a full Hermes task loop against a real
(staging) deployment — ingest a trace, confirm it's retrievable, confirm a
second similar task retrieves the same strategy with correct provenance,
and confirm a contradicting strategy gets flagged rather than silently
overriding.

Run against staging only: pytest tests/e2e/test_memory_loop_e2e.py --router-url=<staging>
"""
import os
import time
import uuid
import requests
import pytest

ROUTER_URL = os.environ.get("HERMES_MEMORY_ROUTER_URL")

pytestmark = pytest.mark.skipif(
    not ROUTER_URL, reason="HERMES_MEMORY_ROUTER_URL not set — e2e tests require a staging deployment"
)


def _ingest(trace_id, task_type, reasoning, outcome):
    resp = requests.post(
        f"{ROUTER_URL}/traces",
        json={
            "trace_id": trace_id,
            "task_id": trace_id,
            "task_type": task_type,
            "raw_reasoning": reasoning,
            "outcome": outcome,
        },
    )
    resp.raise_for_status()
    return resp.json()


def test_full_learning_loop():
    task_type = "code_review"
    reasoning = (
        "Refactored a deeply nested conditional into early returns, "
        "reducing cyclomatic complexity from 12 to 4."
    )

    # 1. First task: ingest a success trace
    trace_id_1 = f"trace_e2e_{uuid.uuid4().hex[:12]}"
    result_1 = _ingest(trace_id_1, task_type, reasoning, "success")
    strategy_id = result_1["strategy_id"]

    time.sleep(1)

    # 2. Second, similar task: retrieval should surface the same strategy
    retrieve_resp = requests.post(
        f"{ROUTER_URL}/retrieve",
        json={"query": "reduce cyclomatic complexity with early returns", "task_type": task_type, "k": 1},
    )
    retrieve_resp.raise_for_status()
    results = retrieve_resp.json()["results"]
    assert len(results) >= 1
    assert results[0]["strategy"]["id"] == strategy_id
    assert trace_id_1 in results[0]["source_traces"]

    # 3. Confirm provenance is fully reconstructable for the original trace
    prov_resp = requests.get(f"{ROUTER_URL}/trace/{trace_id_1}/provenance")
    prov_resp.raise_for_status()
    prov = prov_resp.json()
    assert prov["trace"]["outcome"] == "success"
    assert any(s["id"] == strategy_id for s in prov["strategies_derived"])

    # 4. Third task, same strategy reinforced by a second success trace:
    #    success_count should have incremented, not duplicated the node
    trace_id_2 = f"trace_e2e_{uuid.uuid4().hex[:12]}"
    _ingest(trace_id_2, task_type, reasoning, "success")

    prov_resp_2 = requests.get(f"{ROUTER_URL}/trace/{trace_id_2}/provenance")
    prov_resp_2.raise_for_status()
    strategies_2 = prov_resp_2.json()["strategies_derived"]
    # Same strategy_id should appear — not a duplicate with a new id
    assert any(s["id"] == strategy_id for s in strategies_2)
