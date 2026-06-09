# ADR-003: Default LLM provider

## Status

Accepted.

## Context

CLAUDE.md non-negotiable #1 requires every LLM call to go through a thin internal interface —
`import anthropic`/`import openai` outside `aiops/llm/` fails a smoke test. The product is
vendor-neutral, but a POC still needs a sensible default, a documented alternative, and an
offline fallback (data sensitivity varies per agent, and demos happen on flaky Wi-Fi).

The gateway (`aiops/llm`) already supports four providers — `anthropic`, `openai`, `ollama`,
`stub` — dispatched by the `AIOPS_LLM_PROVIDER` env var, and `get_provider()` falls back to
`anthropic` when nothing is set. The headline RCA Agent is pinned to Anthropic (Azure AI
Foundry Claude) regardless of the env var, because its structured-reasoning quality is the
differentiator. Each SDK is an optional extra (`llm-anthropic`, `llm-openai`, `llm-ollama`)
so installs stay lean.

## Decision

**Anthropic Claude is the default provider.** OpenAI is the documented swap-in alternative;
**Ollama** is the offline / sensitive-data fallback; **stub** is the deterministic provider
for tests and CI (no network, no key). All access goes through `aiops.llm.complete` /
`acomplete`; agents never import a vendor SDK. **Model versions are pinned, never `latest`**,
so a provider-side model rotation can't silently change agent behaviour.

## Consequences

- **Easier:** switch provider via one env var; CI and the eval harness run on `stub` with no
  network or API key; per-agent provider choice (e.g. RCA→Anthropic, a noisy classifier→local
  Ollama) is a config change, not a code change.
- **Harder:** each provider is an optional dependency, so a misconfigured `.env` can leave the
  SDK uninstalled and the call falling back silently (the failure mode DEMO-2 surfaced);
  Azure AI Foundry's Anthropic client classes require `anthropic>=0.69`, a version floor we
  carry deliberately.
- **We now can't:** use provider-specific features (e.g. a vendor's bespoke tool-calling
  shape) from inside an agent — they'd have to be modelled in the gateway's provider-agnostic
  request/response types first.
