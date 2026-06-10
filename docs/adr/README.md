# Architecture Decision Records

This directory records the **why** behind the architectural choices in this repo, so that
future hires and post-POC customers don't have to reverse-engineer intent from the code or
dig through slide decks. Each record is one decision, in the
[Michael Nygard format](https://github.com/joelparkerhenderson/architecture-decision-record)
(Status / Context / Decision / Consequences).

## Conventions

- One file per decision: `NNNN-short-title.md`, numbered in the order decided.
- A record is **immutable once Accepted**. To change a decision, write a *new* ADR and mark
  the old one `Superseded by ADR-MMM`.
- `Status` values: `Proposed` · `Accepted` · `Deprecated` · `Superseded by ADR-MMM`.
- Each ADR is reviewed by dev-a (Chinmay) before it moves to `Accepted`.

## Index

| ADR | Title | Status |
|---|---|---|
| [ADR-001](0001-feature-flag-mutation-seam.md) | Feature-flag mutation seam | Accepted |
| [ADR-002](0002-agent-framework-choice.md) | Agent framework choice (none, for the POC) | Accepted |
| [ADR-003](0003-default-llm-provider.md) | Default LLM provider | Accepted |
| [ADR-004](0004-hitl-approval-surfaces.md) | HITL approval surfaces | Accepted |
| [ADR-005](0005-policy-engine.md) | Policy engine | Accepted |
| [ADR-006](0006-vector-store-choice.md) | Vector store choice | Proposed |
| [ADR-007](0007-truth-files-vs-db.md) | Truth files vs database for scenarios | Accepted |

## Sources

These ADRs back-fill decisions that until now lived only in
[`CLAUDE.md`](../../CLAUDE.md) (the reference POC stack table and non-negotiable principles),
the Solution Design deck, and one design doc
([`docs/arch_1_feature_flags_seam_design.md`](../arch_1_feature_flags_seam_design.md), the
ADR-001 source).
