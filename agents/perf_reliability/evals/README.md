# perf_reliability evals

Golden cases for the UC3 optimizer. Scored by `evals/scoring.py`'s flat-key
grammar (`min_<field>`, `<field>_contains`, `<field>_in`, exact).

- The v0 cases target the **offline heuristic path** (LLM stub in CI), so they
  pass deterministically and keep the CI eval gate green with no Azure/LLM access.
- When a live LLM provider is wired, add cases that assert on richer findings
  (specific `recommendation` substrings, `implementation_complexity`, etc.).
  A prompt change is a model change — re-run: `uv run python -m evals.harness --agent perf_reliability`.

Keep at least the deterministic cases forever: they are the offline safety net.
