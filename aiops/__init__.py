"""Cross-cutting platform code shared by all agents.

Three day-one seams live here, per Solution Design principle #1 (vendor-neutrality):

- ``aiops.llm``    — provider-agnostic LLM gateway
- ``aiops.tools``  — registry that wraps every external integration
- ``aiops.policy`` — platform-enforced HITL gate (Required/Optional/None)

Agent code imports from these modules and never calls vendor SDKs directly.

Note: this package is named ``aiops`` (not ``platform``) because Python's standard
library has a module called ``platform`` and shadowing it breaks pytest, uv, and
every library that introspects the runtime.
"""
