# ADR-007: Truth files vs database for scenarios

## Status

Accepted.

## Context

CLAUDE.md non-negotiable #8: every failure scenario the team injects must have a written
**truth file** — what is broken, the real cause, the correct fix — so the eval harness has
ground truth and the team doesn't "grade itself on vibes." The question is *where* that ground
truth lives: as YAML files in the repo, or as rows in a database/table.

Ground truth is a **specification**, not runtime data. It changes by deliberate human decision
(when a scenario's expected behaviour is defined or revised), it must be reviewable, and it
must be present on a fresh checkout with zero infrastructure so a new contributor — or CI —
can run evals immediately. Runtime *state* (verdicts, classifications, clusters) is a
different lifecycle and already lives in SQLite (`state.db`).

## Decision

**Truth files are YAML in the repo** (`demo/truth_files/<scenario>.yaml`), one per scenario —
not rows in a database. They are versioned with the scenario that produces them, diffable in
pull requests, reviewed like code, and available offline on any checkout. The smoke test
`test_every_scenario_has_a_truth_file` enforces a paired truth file for every scenario.
Runtime state stays in SQLite; the two stores are intentionally separate (static spec vs.
mutable runtime).

## Consequences

- **Easier:** ground truth travels with the code and is reviewed in the same PR as the
  scenario; no database to provision; evals read fixtures straight from disk; a fresh clone is
  immediately gradable.
- **Harder:** no query/aggregation across truth files (acceptable at POC scenario counts); two
  stores with different lifecycles to keep straight (truth = static YAML, state = SQLite).
- **We now can't:** edit ground truth at runtime — a change is a commit, by design (that's the
  point: ground truth shouldn't drift silently).
- **Known gap:** the eval harness currently reads `agents/<name>/evals/golden.json`, **not**
  the truth files (architect retro §2.3). Wiring `demo/truth_files/*.yaml` in as eval inputs
  is tracked Phase 2 work; this ADR records the storage decision, not that the loop is closed.
