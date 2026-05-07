# `aiops.llm` — provider-agnostic LLM gateway

Every LLM call from agent code goes through this module. **No agent file should import `anthropic`, `openai`, or `ollama` directly.** This is the single most-cited rule in `docs/poc_aiops_onboarding_guide.docx` §9.6.

## Usage

```python
from aiops.llm import Message, complete

resp = complete(
    [Message("system", "You are a triage assistant."),
     Message("user", "Classify this alert: ...")],
    max_tokens=512,
)
print(resp.text, resp.input_tokens, resp.output_tokens)
```

Async variant: `acomplete(...)`.

## Provider selection

Set `AIOPS_LLM_PROVIDER` (or pass `provider="..."`):

| Value | When to use | Setup |
|---|---|---|
| `anthropic` | Default for the POC | `uv sync --extra llm-anthropic` + `ANTHROPIC_API_KEY` |
| `openai` | When the team wants a comparison run | `uv sync --extra llm-openai` + `OPENAI_API_KEY` |
| `ollama` | Air-gapped demo / data must not leave cluster | `uv sync --extra llm-ollama` + run `ollama serve` |
| `stub` | Tests; never hits a real API | nothing — always available |

Pin the model with `AIOPS_LLM_MODEL`. Never use `latest`.

## Adding a new provider

1. Create `aiops/llm/<name>_provider.py` with a class decorated `@register_provider("<name>")` implementing `LLMProvider`.
2. Add a lazy import branch in `aiops/llm/base.py::get_provider`.
3. Add an optional-deps group in `pyproject.toml`.
4. Add a row to the table above.

## Caps and safety

- `AIOPS_LLM_MAX_TOKENS_PER_CALL` clamps `max_tokens` before any request leaves the process.
- `AIOPS_LLM_MAX_CALLS_PER_INCIDENT` is read by orchestrators (Phase 2+) — not enforced here.
- PII redaction is **not** the gateway's job. Run inputs through the redactor (Microsoft Presidio is the default) at the agent boundary before they reach `complete()`.
