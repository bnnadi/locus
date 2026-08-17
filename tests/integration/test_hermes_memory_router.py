"""
Integration tests for hermes-memory-router.

Requires a running router instance with live Neo4j + Qdrant connections
(point at a test/staging environment, never production).

Run: pytest tests/integration/test_hermes_memory_router.py --router-url=http://localhost:8000
"""
import os
import time
import uuid
import requests
import pytest

ROUTER_URL = os.environ.get("HERMES_MEMORY_ROUTER_URL", "http://localhost:8000")


@pytest.fixture
def trace_id():
    return f"trace_test_{uuid.uuid4().hex[:12]}"


def test_health():
    resp = requests.get(f"{ROUTER_URL}/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_ingest_trace_success_outcome(trace_id):
    resp = requests.post(
        f"{ROUTER_URL}/traces",
        json={
            "trace_id": trace_id,
            "task_id": "test_task_1",
            "task_type": "code_review",
            "raw_reasoning": "Used a guard clause instead of nested ifs to reduce complexity.",
            "outcome": "success",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["strategy_id"].startswith("strategy_")
    assert body["title"]
    assert 0.0 <= body["success_rate"] <= 1.0


def test_ingest_trace_failure_outcome(trace_id):
    resp = requests.post(
        f"{ROUTER_URL}/traces",
        json={
            "trace_id": trace_id,
            "task_id": "test_task_2",
            "task_type": "code_review",
            "raw_reasoning": "Attempted a regex-based validator that failed on edge cases.",
            "outcome": "failure",
        },
    )
    assert resp.status_code == 200


def test_ingest_trace_bad_backend_fails_gracefully(trace_id):
    resp = requests.post(
        f"{ROUTER_URL}/traces",
        json={
            "trace_id": trace_id,
            "task_id": "test_task_3",
            "task_type": "code_review",
            "raw_reasoning": "test",
            "outcome": "success",
            "backend": "not_a_real_backend",
        },
    )
    # pydantic should 422 on invalid enum value before it ever reaches extraction
    assert resp.status_code == 422


def test_retrieve_after_ingest(trace_id):
    requests.post(
        f"{ROUTER_URL}/traces",
        json={
            "trace_id": trace_id,
            "task_id": "test_task_4",
            "task_type": "debugging",
            "raw_reasoning": "Added a null check before dereferencing the response object.",
            "outcome": "success",
        },
    )
    time.sleep(1)  # allow Qdrant upsert to settle

    resp = requests.post(
        f"{ROUTER_URL}/retrieve",
        json={"query": "null check before using an object", "task_type": "debugging", "k": 1},
    )
    assert resp.status_code == 200
    results = resp.json()["results"]
    assert len(results) >= 1
    assert "audit_path" in results[0]
    assert "strategy" in results[0]


def test_retrieve_respects_min_success_rate():
    resp = requests.post(
        f"{ROUTER_URL}/retrieve",
        json={"query": "anything", "k": 5, "min_success_rate": 1.1},  # impossible threshold
    )
    assert resp.status_code == 200
    assert resp.json()["results"] == []


def test_provenance_for_known_trace(trace_id):
    requests.post(
        f"{ROUTER_URL}/traces",
        json={
            "trace_id": trace_id,
            "task_id": "test_task_5",
            "task_type": "spec_writing",
            "raw_reasoning": "Structured the spec with explicit non-goals to prevent scope creep.",
            "outcome": "success",
        },
    )
    resp = requests.get(f"{ROUTER_URL}/trace/{trace_id}/provenance")
    assert resp.status_code == 200
    body = resp.json()
    assert body["trace"]["id"] == trace_id
    assert len(body["strategies_derived"]) >= 1


def test_provenance_for_unknown_trace_404():
    resp = requests.get(f"{ROUTER_URL}/trace/nonexistent_trace_id/provenance")
    assert resp.status_code == 404
