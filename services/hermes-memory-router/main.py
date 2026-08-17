"""
Hermes Memory Extraction Router
--------------------------------
FastAPI service bridging Hermes Agent, Neo4j (deterministic/auditable spine),
and Qdrant (fast semantic recall). Extraction backend (claude/ollama/hermes)
is selectable per-request without redeploying.

Part of the Locus stack. See docs/deployment/hermes-memory-layer.md and
docs/runbooks/hermes-memory-runbook.md for operational detail.
"""

import os
import json
import hashlib
import time
from enum import Enum
from typing import Optional
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from neo4j import GraphDatabase
from qdrant_client import QdrantClient
from qdrant_client.models import (
    PointStruct, Filter, FieldCondition, MatchValue,
    VectorParams, Distance,
)
from sentence_transformers import SentenceTransformer
from anthropic import Anthropic
import requests

app = FastAPI(title="Hermes Memory Extraction Router")

COLLECTION = "hermes_memory"
EMBEDDING_DIM = 384  # all-MiniLM-L6-v2


class ExtractionBackend(str, Enum):
    CLAUDE = "claude"
    OLLAMA = "ollama"
    HERMES = "hermes"


class ReasoningTraceIn(BaseModel):
    trace_id: str
    task_id: str
    task_type: str
    raw_reasoning: str
    outcome: str  # "success" | "failure" | "partial"
    backend: Optional[ExtractionBackend] = None  # override default


class StrategyOut(BaseModel):
    strategy_id: str
    title: str
    description: str
    conditions: dict
    steps: list[str]
    success_rate: float


class RetrieveIn(BaseModel):
    query: str
    task_type: Optional[str] = None
    k: int = 1
    min_success_rate: Optional[float] = None


# --- Clients (initialized once at startup) ---
neo4j_driver = GraphDatabase.driver(
    os.environ["NEO4J_URI"],
    auth=(os.environ["NEO4J_USER"], os.environ["NEO4J_PASSWORD"]),
)
qdrant = QdrantClient(url=os.environ["QDRANT_URL"], api_key=os.environ.get("QDRANT_API_KEY"))
embedder = SentenceTransformer(os.environ.get("EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2"))
DEFAULT_BACKEND = ExtractionBackend(os.environ.get("EXTRACTION_BACKEND", "claude"))


@app.on_event("startup")
def ensure_collection():
    collections = [c.name for c in qdrant.get_collections().collections]
    if COLLECTION not in collections:
        qdrant.create_collection(
            collection_name=COLLECTION,
            vectors_config=VectorParams(size=EMBEDDING_DIM, distance=Distance.COSINE),
        )
        qdrant.create_payload_index(collection_name=COLLECTION, field_name="task_type", field_schema="keyword")
        qdrant.create_payload_index(collection_name=COLLECTION, field_name="success_rate", field_schema="float")


def strategy_id_from_title(title: str) -> str:
    h = hashlib.sha256(title.strip().lower().encode()).hexdigest()[:16]
    return f"strategy_{h}"


# --- Extraction backends ---

def extract_claude(trace: ReasoningTraceIn) -> dict:
    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    prompt = f"""Given this agent reasoning trace, extract ONE reusable strategy.

Task type: {trace.task_type}
Outcome: {trace.outcome}
Reasoning: {trace.raw_reasoning}

Reply with JSON only, no preamble:
{{"title": "...", "description": "...", "conditions": {{}}, "steps": ["..."], "success_rate": 0.0}}
"""
    resp = client.messages.create(
        model="claude-sonnet-5",
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}],
    )
    text = resp.content[0].text
    start, end = text.find("{"), text.rfind("}") + 1
    return json.loads(text[start:end])


def extract_ollama(trace: ReasoningTraceIn) -> dict:
    prompt = f"""Extract ONE reusable strategy from this trace. Reply with JSON only.

Task type: {trace.task_type}
Outcome: {trace.outcome}
Reasoning: {trace.raw_reasoning}

{{"title": "...", "description": "...", "conditions": {{}}, "steps": ["..."], "success_rate": 0.0}}
"""
    resp = requests.post(
        f"{os.environ['OLLAMA_URL']}/api/generate",
        json={"model": "mistral", "prompt": prompt, "stream": False, "temperature": 0.2},
        timeout=60,
    )
    text = resp.json()["response"]
    start, end = text.find("{"), text.rfind("}") + 1
    return json.loads(text[start:end])


def extract_hermes(trace: ReasoningTraceIn) -> dict:
    resp = requests.post(
        os.environ["HERMES_EXTRACT_ENDPOINT"],
        json={"reasoning_trace": trace.model_dump()},
        headers={"Authorization": f"Bearer {os.environ['HERMES_TOKEN']}"},
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()["strategy"]


BACKENDS = {
    ExtractionBackend.CLAUDE: extract_claude,
    ExtractionBackend.OLLAMA: extract_ollama,
    ExtractionBackend.HERMES: extract_hermes,
}


# --- Endpoints ---

@app.post("/traces", response_model=StrategyOut)
def ingest_trace(trace: ReasoningTraceIn):
    """Store trace in Neo4j (immutable), extract + store a strategy."""
    backend = trace.backend or DEFAULT_BACKEND

    with neo4j_driver.session() as session:
        session.run(
            """
            MERGE (rt:ReasoningTrace {id: $id})
            ON CREATE SET
                rt.task_id = $task_id,
                rt.task_type = $task_type,
                rt.raw_reasoning = $raw_reasoning,
                rt.outcome = $outcome,
                rt.success = $success,
                rt.timestamp = datetime(),
                rt.extraction_status = 'pending'
            """,
            {
                "id": trace.trace_id,
                "task_id": trace.task_id,
                "task_type": trace.task_type,
                "raw_reasoning": trace.raw_reasoning,
                "outcome": trace.outcome,
                "success": trace.outcome == "success",
            },
        )

    try:
        extracted = BACKENDS[backend](trace)
    except Exception as e:
        with neo4j_driver.session() as session:
            session.run(
                "MATCH (rt:ReasoningTrace {id: $id}) SET rt.extraction_status = 'failed'",
                {"id": trace.trace_id},
            )
        raise HTTPException(status_code=502, detail=f"Extraction failed ({backend}): {e}")

    strategy_id = strategy_id_from_title(extracted["title"])
    success = trace.outcome == "success"

    with neo4j_driver.session() as session:
        session.run(
            """
            MERGE (s:StrategyItem {id: $id})
            ON CREATE SET
                s.title = $title,
                s.description = $description,
                s.conditions = $conditions,
                s.steps = $steps,
                s.success_count = CASE WHEN $success THEN 1 ELSE 0 END,
                s.failure_count = CASE WHEN $success THEN 0 ELSE 1 END,
                s.first_seen = datetime(),
                s.last_validated = datetime(),
                s.embedding_synced = false
            ON MATCH SET
                s.success_count = s.success_count + CASE WHEN $success THEN 1 ELSE 0 END,
                s.failure_count = s.failure_count + CASE WHEN $success THEN 0 ELSE 1 END,
                s.last_validated = datetime()
            WITH s
            SET s.success_rate = toFloat(s.success_count) / (s.success_count + s.failure_count)
            WITH s
            MATCH (rt:ReasoningTrace {id: $trace_id})
            MERGE (rt)-[:DERIVES_STRATEGY]->(s)
            SET rt.extraction_status = 'extracted', rt.extraction_backend = $backend
            """,
            {
                "id": strategy_id,
                "title": extracted["title"],
                "description": extracted["description"],
                "conditions": json.dumps(extracted.get("conditions", {})),
                "steps": extracted.get("steps", []),
                "success": success,
                "trace_id": trace.trace_id,
                "backend": backend.value,
            },
        )

    text_to_embed = f"{extracted['title']}. {extracted['description']}"
    vector = embedder.encode(text_to_embed).tolist()
    point_id = int(hashlib.sha256(strategy_id.encode()).hexdigest()[:8], 16)

    qdrant.upsert(
        collection_name=COLLECTION,
        points=[
            PointStruct(
                id=point_id,
                vector=vector,
                payload={
                    "strategy_id": strategy_id,
                    "neo4j_node_id": strategy_id,
                    "trace_id": trace.trace_id,
                    "title": extracted["title"],
                    "task_type": trace.task_type,
                    "type": "strategy",
                    "created_at": int(time.time()),
                },
            )
        ],
    )
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (s:StrategyItem {id: $id}) SET s.embedding_synced = true, s.qdrant_point_id = $pid",
            {"id": strategy_id, "pid": point_id},
        )

    return StrategyOut(
        strategy_id=strategy_id,
        title=extracted["title"],
        description=extracted["description"],
        conditions=extracted.get("conditions", {}),
        steps=extracted.get("steps", []),
        success_rate=success,
    )


@app.post("/retrieve")
def retrieve_strategy(req: RetrieveIn):
    """Fast path: vector search + Neo4j enrichment. k=1 default per ReasoningBank finding."""
    vector = embedder.encode(req.query).tolist()

    query_filter = None
    conditions = []
    if req.task_type:
        conditions.append(FieldCondition(key="task_type", match=MatchValue(value=req.task_type)))
    if conditions:
        query_filter = Filter(must=conditions)

    results = qdrant.search(
        collection_name=COLLECTION,
        query_vector=vector,
        query_filter=query_filter,
        limit=req.k,
    )

    enriched = []
    with neo4j_driver.session() as session:
        for r in results:
            sid = r.payload["strategy_id"]
            record = session.run(
                """
                MATCH (s:StrategyItem {id: $id})
                OPTIONAL MATCH (rt:ReasoningTrace)-[:DERIVES_STRATEGY]->(s)
                OPTIONAL MATCH (s)-[c:CONTRADICTS]->(s2:StrategyItem)
                RETURN s { .* } as strategy,
                       collect(DISTINCT rt.id) as source_traces,
                       collect(DISTINCT {title: s2.title, weight: c.evidence_weight}) as contradictions
                """,
                {"id": sid},
            ).single()

            if not record:
                continue
            strategy = record["strategy"]
            if req.min_success_rate and strategy.get("success_rate", 0) < req.min_success_rate:
                continue

            enriched.append(
                {
                    "vector_score": r.score,
                    "strategy": strategy,
                    "source_traces": record["source_traces"],
                    "contradictions": [c for c in record["contradictions"] if c["title"]],
                    "audit_path": f"Qdrant point {r.id} -> Neo4j StrategyItem {sid} -> DERIVES_STRATEGY <- ReasoningTrace",
                }
            )

    return {"results": enriched}


@app.get("/trace/{trace_id}/provenance")
def get_provenance(trace_id: str):
    """Deterministic path: full Cypher-traced audit trail for one trace."""
    with neo4j_driver.session() as session:
        record = session.run(
            """
            MATCH (rt:ReasoningTrace {id: $id})
            OPTIONAL MATCH (rt)-[:DERIVES_STRATEGY]->(s:StrategyItem)
            OPTIONAL MATCH (d:Decision)-[:LED_TO]->(rt)
            OPTIONAL MATCH (s)-[c:CONTRADICTS]->(other:StrategyItem)
            RETURN rt { .* } as trace,
                   collect(DISTINCT s { .* }) as strategies,
                   collect(DISTINCT d { .* }) as decisions,
                   collect(DISTINCT {title: other.title, weight: c.evidence_weight}) as contradictions
            """,
            {"id": trace_id},
        ).single()

        if not record:
            raise HTTPException(status_code=404, detail="Trace not found")

        return {
            "trace": record["trace"],
            "strategies_derived": record["strategies"],
            "decisions": record["decisions"],
            "contradictions": [c for c in record["contradictions"] if c["title"]],
        }


@app.get("/health")
def health():
    return {"status": "ok"}
