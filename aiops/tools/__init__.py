"""Tool registry — single seam for every external integration agents call.

Why this exists:
    Solution Design §2 — vendor-neutral by default. ServiceNow, Splunk, Dynatrace,
    PagerDuty, Ansible, Jira, Teams, Kubernetes — every one of these has at least
    one alternative documented in the catalog. Agents reference *capabilities*
    (``"itsm.create_incident"``), not vendors, so swapping ServiceNow for Jira
    is a registry edit, not an agent rewrite.

Phase 0 ships the registry plus a single ``mock`` tool family. Real provider
implementations (ServiceNow PDI, PagerDuty dev account, Prometheus, Loki, Tempo,
kubectl wrappers) land in Phase 1+ as separate modules that register themselves.
"""

from .registry import Tool, ToolRegistry, ToolResult, get_registry, tool

__all__ = ["Tool", "ToolRegistry", "ToolResult", "get_registry", "tool"]
