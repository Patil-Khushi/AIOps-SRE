# `demo/truth_files/` — ground truth for every demo scenario

Why these exist (POC guide §2.3.6):

> How do you know your RCA Agent's answer is correct if there is no real
> incident with a confirmed real cause? You don't — unless you build labelled
> scenarios. Every demo failure scenario must have a written truth file
> stating the real root cause, the correct remediation, and the expected
> agent behaviour. Otherwise the team will silently grade itself on vibes.

## Layout

One file per scenario: `<scenario_id>.yaml`. The `scenario_id` MUST match the YAML in `demo/failure_injection/scenarios/`.

`template.yaml` is the schema; copy it for new scenarios.

## How agents use these

The eval harness (`evals/`) reads truth files when scoring multi-agent runs against a real cluster failure. Each truth file declares:

- `expected_rca.cause_summary` — what the RCA Agent's prose output should resemble.
- `expected_rca.ranked_hypotheses` — the hypothesis list the Root-Cause Predictor should rank.
- `expected_fix_steps` — what the RCA Agent + Remediation Recommender should propose.
- `known_wrong_fixes` — *negative* examples. The agent should not propose these.
- `scoring.rca_must_include` — substrings that must appear in RCA output.

## Owning a truth file

Whoever introduces a scenario owns its truth file. When the scenario changes (different flag, different mechanism), the truth file must change in the same PR. CI does not strictly enforce this yet — it's a culture rule.
