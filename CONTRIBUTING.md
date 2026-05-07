# Contributing

This is the POC repository for Adaptive AIOps + SRE Ops. Conventions below match the cadence in `docs/poc_aiops_onboarding_guide.docx` §8.

## Branching

- `main` — always demo-ready. Locked 24h before any external demo.
- `phase/<n>-<name>` — long-lived branches per POC phase (`phase/1-reactive`, `phase/2-rca`, …).
- `feat/<area>-<short-desc>` — feature branches off the current phase branch.
- `fix/<short-desc>` — bug fixes.

Every PR targets the active phase branch. Phase branches merge to `main` at end-of-phase, after the dry-run demo passes.

## Commits

- Imperative mood, short subject (≤72 chars), explain *why* in the body if non-obvious.
- One concern per commit. Refactors and behavior changes go in separate commits.
- Reference the agent ID from the catalog when applicable (e.g. `RA-001 alert triage: dedup by fingerprint`).

## Pull requests

A PR must:

1. Pass `ruff check` and `ruff format --check`.
2. Pass `pytest`.
3. Pass the eval gate (`uv run python -m evals.harness --ci`) — pass rate cannot drop more than 2% vs `main`.
4. Touch the agent's truth file if a new failure scenario or expected behavior is added.
5. Not introduce a direct vendor SDK call outside `aiops/llm/` or `aiops/tools/`.
6. Not put HITL logic inside agent code — gates live in `aiops/policy/` and `policies/*.rego`.

## Adding a new agent

See `agents/README.md`. The short version:

1. Find the agent's row in `docs/Adaptive_AIOps_Agent_Catalog.xlsx` — that is the contract.
2. `mkdir agents/<phase>-<id>-<slug>/` with `agent.py`, `prompts/`, `evals/golden.json`, `README.md`.
3. Build the eval set the same week you build the agent.
4. Wire it through `aiops/llm/` and `aiops/tools/`. No direct SDK calls.
5. Set its HITL level in `policies/hitl.rego` to match the catalog.

## Adding a new failure scenario

See `demo/failure_injection/README.md`. Every scenario must ship with a truth file in `demo/truth_files/` — without it the scenario cannot be used for eval scoring.

## Code style

- Python 3.12+. `ruff` for lint and format. `pytest` for tests. Type hints on public functions.
- Avoid premature abstraction. Three similar lines is better than a generic helper that gets used twice.
- No emojis in code, comments, or commit messages unless the user explicitly asks for them.
- Comments explain *why*, not *what*. Default to no comment.

## Secrets

- Never commit credentials. `.env` is git-ignored; use `.env.example` for the template.
- LLM API keys live in environment variables loaded by `aiops/llm/`. Never paste them into prompts or logs.
- See `docs/llm-access.md` for the full data-handling policy.
