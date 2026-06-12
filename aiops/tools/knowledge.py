"""Knowledge publication executor — the KB-draft → approve → publish loop.

The Knowledge Synthesizer (PRS-007) only *drafts* KB articles and *suggests*
runbooks; it never publishes. This module is the platform-side executor that
carries out an approved publication, gated by the REQUIRED-HITL capability
``knowledge.publish`` (registered in ``aiops/policy/gate.py`` and mirrored in
``policies/hitl.rego``). Because the capability is REQUIRED,
``ToolRegistry.call`` runs the approval flow *before* the tool body executes:
it posts an approve/deny prompt through the chatops/Slack seam, blocks until a
human resolves it, and only then flips the article to ``published``. This is
the same machinery the RCA fix-step and auto-heal demos use — reused, not
reinvented.

Publishing has two effects, both inside the gated body so a buggy or
compromised agent physically cannot publish without approval:

1. The KB article transitions ``pending_review`` → ``published``.
2. The suggested runbook (if any) is written to the runbook library — a new
   file, or a version bump of the existing one for an ``update`` suggestion.
"""

from __future__ import annotations

from typing import Any

from aiops.runbooks import ReviewStatus, Runbook, save_runbook
from aiops.state import repository as repo
from aiops.tools.registry import ToolResult, get_registry, tool

_CAPABILITY = "knowledge.publish"


@tool(
    name="seam.knowledge.publish",
    capability=_CAPABILITY,
    provider="seam",
    description="Publish an approved KB article and write its suggested runbook.",
)
def publish_kb_article(
    article_id: int = 0,
    runbook_id: str = "",
    runbook_title: str = "",
    runbook_body: str = "",
    runbook_service: str = "",
    runbook_mode: str = "",
    **_: Any,
) -> ToolResult:
    """Publish one approved KB article. Only reached *after* the registry's
    HITL gate approves — REQUIRED-level ``knowledge.publish`` blocks upstream
    until a human approves. ``**_`` swallows any extra context the caller
    forwards (e.g. approver metadata stamped separately)."""
    if not article_id:
        return ToolResult(ok=False, error="publish requires an 'article_id'")
    row = repo.update_kb_status(int(article_id), ReviewStatus.PUBLISHED.value)
    if row is None:
        return ToolResult(ok=False, error=f"KB article {article_id!r} not found")

    runbook_written = False
    if runbook_id and runbook_body:
        save_runbook(
            Runbook(
                id=runbook_id,
                title=runbook_title or runbook_id,
                service=runbook_service or row.get("service") or "unknown",
                body=runbook_body,
                status=ReviewStatus.PUBLISHED,
                source="live",
                source_incident=row.get("incident_id"),
                related_kb=str(article_id),
            ),
            bump_version=(runbook_mode == "update"),
        )
        runbook_written = True

    return ToolResult(
        ok=True,
        data={
            "article_id": int(article_id),
            "status": ReviewStatus.PUBLISHED.value,
            "runbook_written": runbook_written,
            "runbook_id": runbook_id or None,
        },
    )


def request_publish(
    *,
    article_id: int,
    runbook: dict[str, Any] | None = None,
    hitl_context: dict[str, Any],
) -> dict[str, Any]:
    """Request approval to publish a KB article; return a JSON-able outcome.

    Blocks inside the registry call until the human approves / denies / the
    request expires — the platform HITL gate owns that wait — then maps the
    result to a status the UI can render: ``published`` / ``denied`` /
    ``expired`` / ``blocked`` / ``error``.

    ``runbook`` is the optional suggestion to apply on publish: a dict with
    ``target_id`` / ``title`` / ``body_markdown`` / ``service`` / ``mode``.
    """
    rb = runbook or {}
    result = get_registry().call(
        _CAPABILITY,
        hitl_context=hitl_context,
        article_id=article_id,
        runbook_id=rb.get("target_id", ""),
        runbook_title=rb.get("title", ""),
        runbook_body=rb.get("body_markdown", ""),
        runbook_service=rb.get("service", ""),
        runbook_mode=rb.get("mode", ""),
    )

    approval_id = hitl_context.get("pending_approval_id")
    approver: str | None = None
    req = None
    if approval_id:
        try:
            from aiops.policy import get_approval_registry

            req = get_approval_registry().get(approval_id)
            approver = req.approver
        except Exception:
            req = None

    if result.ok:
        # Stamp the audit fields (who approved, which request) onto the row.
        # Best-effort: the gated transition already happened in the body.
        repo.update_kb_status(
            int(article_id),
            ReviewStatus.PUBLISHED.value,
            approval_id=approval_id,
            approved_by=approver,
        )
        out = {"status": "published", "approval_id": approval_id, "approver": approver}
        out.update(result.data if isinstance(result.data, dict) else {})
        return out

    status = "error"
    if (result.metadata or {}).get("blocked_by") == "hitl_gate":
        status = "blocked"
        if req is not None:
            try:
                from aiops.policy import ApprovalStatus

                if req.status is ApprovalStatus.DENIED:
                    status = "denied"
                elif req.status is ApprovalStatus.EXPIRED:
                    status = "expired"
            except Exception:
                pass
    return {
        "status": status,
        "approval_id": approval_id,
        "approver": approver,
        "article_id": int(article_id),
        "error": result.error,
    }
