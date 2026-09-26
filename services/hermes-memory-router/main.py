"""
Hermes Memory Extraction Router
--------------------------------
FastAPI service bridging Hermes Agent, Neo4j (deterministic/auditable spine),
and Qdrant (fast semantic recall). Extraction backend (claude/ollama/hermes)
is selectable per-request without redeploying.

Part of the Locus stack. See docs/deployment/hermes-memory-layer.md and
docs/runbooks/hermes-memory-runbook.md for operational detail.
"""

import hashlib
import json
import os
import time
from enum import Enum

import requests
from anthropic import Anthropic
from fastapi import FastAPI, HTTPException
from identity import below_min_success_rate, strategy_id_for
from neo4j import GraphDatabase
from pydantic import BaseModel
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchValue,
    PointStruct,
    VectorParams,
)
from redact import redact_reasoning
from sentence_transformers import SentenceTransformer

app = FastAPI(title="Hermes Memory Extraction Router")

COLLECTION = "hermes_memory"
EMBEDDING_DIM = 384  # all-MiniLM-L6-v2


# Not StrEnum, though UP042 asks for it: str(member) differs between the two
# ("ExtractionBackend.CLAUDE" here, "claude" under StrEnum), and the 502
# detail on line ~212 interpolates a member directly, so switching would
# change an API response body. Persistence is unaffected — it uses
# .value explicitly. Worth doing as its own change, not as a lint fix.
class ExtractionBackend(str, Enum):  # noqa: UP042
    CLAUDE = "claude"
    OLLAMA = "ollama"
    HERMES = "hermes"


class ReasoningTraceIn(BaseModel):
    trace_id: str
    task_id: str
    task_type: str
    # Stored field: the redacted string.  When strategy_key is absent, the
    # strategy hash is derived from task_type + normalized raw_reasoning.
    raw_reasoning: str
    outcome: str  # "success" | "failure" | "partial"
    backend: ExtractionBackend | None = None  # override default
    # Optional caller-owned key. When omitted, identity is derived from
    # task_type + normalized raw_reasoning — never from model output.
    strategy_key: str | None = None


class StrategyOut(BaseModel):
    strategy_id: str
    title: str
    description: str
    conditions: dict
    steps: list[str]
    success_rate: float


class RetrieveIn(BaseModel):
    query: str
    task_type: str | None = None
    k: int = 1
    min_success_rate: float | None = None


# --- Clients (initialized once at startup) ---
neo4j_driver = GraphDatabase.driver(
    os.environ["NEO4J_URI"],
    auth=(os.environ["NEO4J_USER"], os.environ["NEO4J_PASSWORD"]),
)
qdrant = QdrantClient(
    url=os.environ["QDRANT_URL"], api_key=os.environ.get("QDRANT_API_KEY")
)
embedder = SentenceTransformer(
    os.environ.get("EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
)
DEFAULT_BACKEND = ExtractionBackend(os.environ.get("EXTRACTION_BACKEND", "claude"))


@app.on_event("startup")
def ensure_collection():
    collections = [c.name for c in qdrant.get_collections().collections]
    if COLLECTION not in collections:
        qdrant.create_collection(
            collection_name=COLLECTION,
            vectors_config=VectorParams(size=EMBEDDING_DIM, distance=Distance.COSINE),
        )
        qdrant.create_payload_index(
            collection_name=COLLECTION, field_name="task_type", field_schema="keyword"
        )
        qdrant.create_payload_index(
            collection_name=COLLECTION, field_name="success_rate", field_schema="float"
        )


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
        thinking={"type": "disabled"},
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(block.text for block in resp.content if block.type == "text")
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
        json={
            "model": "mistral",
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0},
        },
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


def _strategy_id_from_existence_row(record: dict | None) -> str | None:
    """Return the strategy_id string from an existence-read row, or None.

    Returns None for a missing row, a missing key, or a non-string/blank value.
    This prevents the row shape returned by the redacts-test fake (which always
    hands back ``{"success_rate": 1.0}`` regardless of the query) from being
    mis-classified as a hit.
    """
    if record is None:
        return None
    strategy_id = record.get("strategy_id")
    if isinstance(strategy_id, str) and strategy_id:
        return strategy_id
    return None


@app.post("/traces", response_model=StrategyOut)
def ingest_trace(trace: ReasoningTraceIn) -> StrategyOut:
    """Store trace in Neo4j (immutable), extract + store a strategy."""
    # Step 1: redact + resolve backend first.
    trace = trace.model_copy(update={"raw_reasoning": redact_reasoning(trace.raw_reasoning)})
    backend = trace.backend or DEFAULT_BACKEND

    # Step 2: unconditional ReasoningTrace MERGE (do NOT call .single()).
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

    # Step 3: compute identity and outcome from the (already-redacted) reasoning.
    strategy_id = strategy_id_for(
        task_type=trace.task_type,
        raw_reasoning=trace.raw_reasoning,
        strategy_key=trace.strategy_key,
    )
    success = trace.outcome == "success"

    # Step 4: existence read — own session, MATCH not MERGE, call .single().
    with neo4j_driver.session() as session:
        existence_record = session.run(
            """MATCH (s:StrategyItem {id: $id})
RETURN s.id AS strategy_id, s.embedding_synced AS embedding_synced""",
            {"id": strategy_id},
        ).single()

    # Step 5: hit predicate.
    existing_id = _strategy_id_from_existence_row(existence_record)

    if existing_id is not None:
        # Step 6: reuse update — MATCH not MERGE, bump counters, link trace.
        with neo4j_driver.session() as session:
            reuse_record = session.run(
                """MATCH (s:StrategyItem {id: $id})
SET s.success_count = s.success_count + CASE WHEN $success THEN 1 ELSE 0 END,
    s.failure_count = s.failure_count + CASE WHEN $success THEN 0 ELSE 1 END,
    s.last_validated = datetime()
WITH s
SET s.success_rate = toFloat(s.success_count) / (s.success_count + s.failure_count)
WITH s
MATCH (rt {id: $trace_id})
MERGE (rt)-[:DERIVES_STRATEGY]->(s)
SET rt.extraction_status = CASE WHEN rt.extraction_status = 'pending' THEN 'reused' ELSE rt.extraction_status END
RETURN s.title AS title,
       s.description AS description,
       s.conditions AS conditions,
       s.steps AS steps,
       s.success_rate AS success_rate,
       s.embedding_synced AS embedding_synced""",
                {"id": existing_id, "success": success, "trace_id": trace.trace_id},
            ).single()

        # Step 7: if reuse row is absent, fall through to the miss/extract path.
        if reuse_record is not None:
            # Step 8: build StrategyOut from stored fields.
            conditions_raw = reuse_record.get("conditions")
            if not conditions_raw:
                conditions: dict = {}
            else:
                parsed = json.loads(conditions_raw)
                if not isinstance(parsed, dict):
                    raise ValueError(
                        f"conditions must be a JSON object, got {type(parsed).__name__}"
                    )
                conditions = parsed

            steps: list[str] = reuse_record.get("steps") or []
            title: str = reuse_record["title"]
            description: str = reuse_record["description"]
            sr = float(reuse_record["success_rate"])

            # Step 9: embedding heal using the EXISTENCE row's embedding_synced.
            embedding_synced = (
                existence_record.get("embedding_synced")
                if existence_record is not None
                else None
            )
            if embedding_synced is not True:
                text_to_embed = f"{title}. {description}"
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
                                "title": title,
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
                title=title,
                description=description,
                conditions=conditions,
                steps=steps,
                success_rate=sr,
            )

    # Step 10: miss path — call extraction backend (broad except preserved).
    try:
        extracted = BACKENDS[backend](trace)
    # Deliberately broad: this is the API boundary for third-party extraction
    # backends, and an unanticipated exception escaping here would return a
    # 500 and strand the trace on extraction_status='pending' forever. Every
    # path below either records the failure or re-raises, so nothing is
    # swallowed.
    except Exception as e:  # noqa: BLE001
        with neo4j_driver.session() as session:
            session.run(
                "MATCH (rt:ReasoningTrace {id: $id}) SET rt.extraction_status = 'failed', rt.extraction_backend = $backend",
                {"id": trace.trace_id, "backend": backend.value},
            )
        raise HTTPException(
            status_code=502, detail=f"Extraction failed ({backend}): {e}"
        )

    # Step 11: miss success — existing MERGE/create path (unchanged).
    with neo4j_driver.session() as session:
        record = session.run(
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
            RETURN s.success_rate AS success_rate
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
        ).single()
        success_rate = (
            float(record["success_rate"]) if record else (1.0 if success else 0.0)
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
        success_rate=success_rate,
    )


@app.post("/retrieve")
def retrieve_strategy(req: RetrieveIn):
    """Fast path: vector search + Neo4j enrichment. k=1 default per ReasoningBank finding."""
    vector = embedder.encode(req.query).tolist()

    query_filter = None
    conditions = []
    if req.task_type:
        conditions.append(
            FieldCondition(key="task_type", match=MatchValue(value=req.task_type))
        )
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
            if below_min_success_rate(
                strategy.get("success_rate"), req.min_success_rate
            ):
                continue

            enriched.append(
                {
                    "vector_score": r.score,
                    "strategy": strategy,
                    "source_traces": record["source_traces"],
                    "contradictions": [
                        c for c in record["contradictions"] if c["title"]
                    ],
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
