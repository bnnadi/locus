# Qdrant Service

Vector store for the Hermes memory layer. Stores embeddings of distilled
`StrategyItem` nodes; Neo4j remains the source of truth (see
`hermes-memory-router` and `migrations/neo4j/001_hermes_memory_schema.cypher`).

## Collection

Created once by the router on first boot (see
`services/hermes-memory-router/main.py`), or manually via:

```bash
curl -X PUT "$QDRANT_URL/collections/hermes_memory" \
  -H "api-key: $QDRANT_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"vectors": {"size": 384, "distance": "Cosine"}}'
```

384 dims matches the default embedding model
(`sentence-transformers/all-MiniLM-L6-v2`). If you change embedding models,
the collection must be recreated — dimensions can't change in place.

## Railway config

- Volume: mount `/qdrant/storage`, start at 20GB
- Internal URL for other Locus services: `qdrant.railway.internal:6333`
- Env vars: see `config/hermes-memory.env.example`

## Local dev

```bash
docker build -t locus-qdrant services/qdrant
docker run -p 6333:6333 -p 6334:6334 \
  -e QDRANT__SERVICE__API_KEY=dev-key \
  -v $(pwd)/.qdrant-data:/qdrant/storage \
  locus-qdrant
```
