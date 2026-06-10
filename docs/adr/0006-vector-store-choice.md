# ADR-006: Vector store choice

## Status

Proposed — no vector store is deployed in the POC yet. This ADR records the *default to reach
for* when one is first needed, so the choice isn't made ad-hoc under deadline.

## Context

Several future agents are RAG-backed and will need persistent semantic retrieval: the
Knowledge Synthesizer, the RCA corpus, and cross-incident "similar incidents" lookups. CLAUDE.md
lists `pgvector` or `Qdrant` as the options.

Today the only similarity use in the codebase is **Alert Triage dedup**, which computes
sentence-transformers embeddings **in memory** (the optional `embeddings` extra) and falls
back to rule-based dedup when that extra isn't installed. There is no persistent vector store,
and the POC doesn't need one to demo the Reactive flow. The runtime state the platform already
persists (verdicts, classifications) lives in SQLite via SQLModel.

## Decision

**Defer standing up a vector store until an agent needs persistent semantic retrieval**
(expected at the Phase 2 RCA / Knowledge Synthesizer work). When that point arrives, **default
to pgvector**: it rides on the relational store we already model with SQLModel (SQLite locally
→ Postgres later), so it's one fewer service to run and operate on a 16 GiB laptop. **Qdrant
is the documented alternative**, to adopt only if filtered search or scale outgrows what
pgvector comfortably serves. POC dedup stays in-memory.

## Consequences

- **Easier:** no extra infrastructure now; when the store lands, pgvector reuses the SQL
  engine and migration path already in the repo, keeping the operational surface small.
- **Harder:** in-memory dedup doesn't persist across restarts; the eventual migration from
  "no store" to pgvector (schema, embedding pipeline, backfill) is real work that this ADR
  defers rather than removes.
- **We now can't:** do cross-incident semantic recall (e.g. "have we seen this failure
  before?") until a store is in place — a capability gap to flag whenever an agent's value
  proposition depends on it.
