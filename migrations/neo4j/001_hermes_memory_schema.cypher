// Migration: 001_hermes_memory_schema
// Target: existing Neo4j Community instance (Locus stack)
// Idempotent — safe to run multiple times (IF NOT EXISTS on all constraints/indexes)
// Run via: scripts/migration.sh --target neo4j --file migrations/neo4j/001_hermes_memory_schema.cypher

CREATE CONSTRAINT reasoning_trace_id IF NOT EXISTS
  FOR (rt:ReasoningTrace) REQUIRE rt.id IS UNIQUE;

CREATE CONSTRAINT strategy_item_id IF NOT EXISTS
  FOR (s:StrategyItem) REQUIRE s.id IS UNIQUE;

CREATE INDEX strategy_by_success IF NOT EXISTS
  FOR (s:StrategyItem) ON (s.success_rate, s.last_validated);

CREATE INDEX trace_by_task_type IF NOT EXISTS
  FOR (rt:ReasoningTrace) ON (rt.task_type, rt.timestamp);

CREATE INDEX trace_extraction_status IF NOT EXISTS
  FOR (rt:ReasoningTrace) ON (rt.extraction_status);

// Node labels introduced by this migration (no CREATE needed, Neo4j is schema-optional):
//   ReasoningTrace  - immutable record of one task execution (write-once)
//   StrategyItem    - distilled, reusable strategy (append-only counts)
//   Decision        - optional fine-grained decision within a trace (write-once)
//
// Relationship types introduced:
//   DERIVES_STRATEGY  (ReasoningTrace -> StrategyItem)
//   CONTRADICTS        (StrategyItem -> StrategyItem, carries evidence_weight)
//   SUPPORTS            (ReasoningTrace -> StrategyItem)
//   IMPLEMENTS_PATTERN  (StrategyItem -> CodePattern; CodePattern is shared with
//                        the Knowledge-Graph MCP's existing label — see
//                        docs/deployment/hermes-memory-layer.md open question #1)
//   LED_TO              (Decision -> ReasoningTrace)
//   PRECEDED_BY         (ReasoningTrace -> ReasoningTrace, optional task chaining)
//
// Full property schema documented in docs/deployment/hermes-memory-layer.md §3.
