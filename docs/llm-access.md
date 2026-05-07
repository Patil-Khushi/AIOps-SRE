# LLM access — rate limits, costs, and security

Phase 0 deliverable from POC guide §8.1: "LLM API access provisioned. Rate-limit, cost, and security policy documented."

This document covers what to do today; it'll need updating once the company LLM gateway (if one exists) is wired in.

## Default provider

Anthropic Claude is the POC default. The catalog and onboarding guide both use Claude as the reference. Set in `.env`:

```
AIOPS_LLM_PROVIDER=anthropic
AIOPS_LLM_MODEL=claude-sonnet-4-6      # PIN exact model. Never "latest".
ANTHROPIC_API_KEY=...
```

Alternative providers are wired in already (`openai`, `ollama`, `stub`). Switch via the env var; agent code does not change.

## How agents call LLMs

**Always** through `aiops.llm.complete` / `acomplete`. Never import `anthropic` / `openai` / `ollama` directly. This is enforced by code review (CONTRIBUTING.md PR rule #5) and tested in `tests/test_smoke.py`.

```python
from aiops.llm import Message, complete

resp = complete([
    Message("system", "You are a triage assistant."),
    Message("user", payload),
])
```

## Rate limits and cost caps

Two env-driven caps apply at the gateway:

| Variable | Default | What it does |
|---|---|---|
| `AIOPS_LLM_MAX_TOKENS_PER_CALL` | 4096 | Clamps `max_tokens` on every request. |
| `AIOPS_LLM_MAX_CALLS_PER_INCIDENT` | 100 | Read by orchestrators (Phase 2+). Not enforced by the gateway. |

Anthropic's own rate limits are tier-dependent. For the POC:

- Use a separate API key per developer where possible.
- If a demo runs hot and hits the limit, switch to Ollama for the demo (`AIOPS_LLM_PROVIDER=ollama`). Document the swap in the demo notes.
- Track spend via the Anthropic console weekly. If monthly spend looks unsustainable, escalate per POC guide §4.3 (partner-tier production access).

## Security policy

These rules apply to **every** LLM call from this repo. They map directly to POC guide §9.7.

1. **No real customer data into a hosted LLM API.** Phase 0 has none anyway, but assume sanitised customer data may arrive in Phase 1 — keep the rule.
2. **Run inputs through a redactor** before the LLM call. Microsoft Presidio is the default. Wire it at the agent boundary, not inside `aiops/llm/`.
3. **Do not log full prompts or completions** outside development. Audit logs record metadata (model, tokens, latency) and a content hash, not the content itself.
4. **API keys are environment variables only.** Never commit a `.env` file. `.env.example` is the template.
5. **Pin model versions.** A model upgrade is a deployment — runs through the eval harness in shadow mode before promotion.
6. **Watch for prompt-injection** in any text that came from the cluster (logs, alerts, ticket bodies). Hostile log lines that say "ignore previous instructions" are real attacks.
7. **Treat the redaction layer as a guarded service** — its own tests, its own evals. Failures here are silent until they aren't.

## What's NOT here yet

- A company-internal LLM gateway. If one exists in the org, wire it as a new provider in `aiops/llm/` and make it the default.
- A budget alarm. Track spend by hand for now.
- Per-tenant key isolation. Phase 1+, when there's more than one tenant.
