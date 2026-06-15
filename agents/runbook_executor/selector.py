"""Runbook selection policy — *which* runbook fits an incident.

This is deliberately dumb: substring matching on service + tags + severity, no
RAG / embeddings / vector DB (catalog scope for RA-004 v0, and the same
"start simple" stance as ``aiops.runbooks.search_runbooks``). The selection
*policy* lives here in the agent, never in the execution core (CLAUDE.md #2 /
#3): the platform mechanics don't get an opinion about which runbook runs.
"""

from __future__ import annotations

from agents.runbook_executor.library import ExecutableRunbook


def _norm(s: str | None) -> str:
    return (s or "").strip().lower()


def _norm_token(s: str | None) -> str:
    """Lower-case and strip separators, so 'Sev-1' / 'sev_1' / 'sev 1' all
    compare equal to 'sev1'."""
    t = _norm(s)
    for sep in ("-", "_", " "):
        t = t.replace(sep, "")
    return t


def _normalize_service(service: str | None) -> str:
    """Collapse spellings to a base key, mirroring
    ``aiops.runbooks.store._normalize_service`` so 'payment',
    'payment-service' and 'paymentservice' all match."""
    s = _norm(service)
    for sep in ("-", "_", " "):
        s = s.replace(sep, "")
    if s.endswith("service") and len(s) > len("service"):
        s = s[: -len("service")]
    return s


def _service_matches(runbook: ExecutableRunbook, service: str | None) -> bool:
    a, b = _normalize_service(runbook.service), _normalize_service(service)
    if not a or not b:
        return False
    return a in b or b in a  # substring either direction


def score(
    runbook: ExecutableRunbook,
    *,
    service: str | None,
    tags: list[str] | None = None,
    severity: str | None = None,
) -> int:
    """Match score, or ``-1`` to disqualify. Service is mandatory; tag overlap
    and a severity match raise the score so the most specific runbook wins."""
    if not _service_matches(runbook, service):
        return -1
    pts = 10
    rb_tags = {_norm(t) for t in runbook.tags}
    want = {_norm(t) for t in (tags or [])}
    # Substring tag overlap (so 'oom' matches 'oom-kill', 'latency' matches
    # 'high-latency') — count each wanted tag that hits any runbook tag.
    pts += 3 * sum(1 for w in want if w and any(w in t or t in w for t in rb_tags))
    if severity and runbook.severity and _norm_token(runbook.severity) == _norm_token(severity):
        pts += 5
    return pts


def select_runbook(
    runbooks: list[ExecutableRunbook],
    *,
    service: str | None,
    tags: list[str] | None = None,
    severity: str | None = None,
) -> ExecutableRunbook | None:
    """Return the best-matching runbook, or ``None`` when nothing matches the
    service. Ties break deterministically by runbook id so selection is
    reproducible in evals."""
    best: ExecutableRunbook | None = None
    best_score = 0  # a disqualified (-1) or zero-point runbook is never chosen
    for rb in sorted(runbooks, key=lambda r: r.id):
        s = score(rb, service=service, tags=tags, severity=severity)
        if s > best_score:
            best_score, best = s, rb
    return best
