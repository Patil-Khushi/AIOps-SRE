"""Resolve the runbook for an incident into an attachable file.

RA-005 names a runbook in the notification body, but the reference it
inherits from the CMDB is a placeholder URL (``runbooks.example.com``)
that resolves nowhere — so the on-call engineer gets a dead link at the
moment they most need the procedure. This module turns whatever the
verdict carries into the *actual* markdown from the runbook library
(``aiops.runbooks``, ``data/runbooks/*.md``) so adapters can deliver the
content itself instead of a link.

Resolution order, first hit wins:

1. Explicit id — ``"rb-payment-failure"``, or a URL/path whose last
   segment is one (``".../rb-payment-failure.md"``).
2. Service match — the library's own normalized service key, so
   ``"recommendation"``, ``"recommendationservice"`` and
   ``"recommendation-service"`` all find the same runbook. This is what
   rescues the placeholder-URL case: ``runbooks.example.com/frontend``
   contributes nothing usable as an id, but the verdict's
   ``affected_service`` does.

Returns ``None`` when nothing matches — the caller degrades to the plain
text line rather than attaching a wrong procedure.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

# Runbook ids are "rb-" + kebab words; anchored so a URL segment that
# merely contains the prefix can't smuggle in path traversal.
_RUNBOOK_ID_RE = re.compile(r"^rb-[a-z0-9]+(?:-[a-z0-9]+)*$")

# Share links minted once per runbook by scripts/publish_runbooks.py and
# committed as data. Publishing per incident would upload a fresh copy on
# every alert and hand each sink a different URL; a stable map means the
# channel card and the personal DM point at the SAME file.
_LINKS_PATH_ENV = "AIOPS_RUNBOOK_LINKS_PATH"
_DEFAULT_LINKS_PATH = "data/runbook_links.json"


@lru_cache(maxsize=1)
def _link_map() -> dict[str, dict[str, str]]:
    p = Path(os.environ.get(_LINKS_PATH_ENV) or _DEFAULT_LINKS_PATH)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.debug("runbook link map %s not present; links disabled", p)
        return {}
    except Exception as exc:
        logger.warning("runbook link map %s unreadable (%s); links disabled", p, exc)
        return {}
    return data if isinstance(data, dict) else {}


def _reset_link_cache_for_tests() -> None:
    """Drop the memoized link map. Test seam only — the map is read once
    per process, so a test writing its own fixture file would otherwise see
    whichever map the first test happened to load."""
    _link_map.cache_clear()


@dataclass(frozen=True)
class RunbookAttachment:
    """A runbook rendered as a deliverable file."""

    runbook_id: str
    title: str
    filename: str
    markdown: str
    url: str | None = None
    """Stable share link to the published copy, when one has been minted.
    ``None`` leaves adapters rendering the runbook's name as plain text."""


def _candidate_id(ref: str) -> str | None:
    """Extract a runbook id from a raw reference, or None.

    Handles bare ids, URLs, and paths — everything after the last ``/`` or
    ``\\``, minus a ``.md`` suffix and any query string.
    """
    token = (ref or "").strip()
    if not token:
        return None
    token = token.split("?", 1)[0].split("#", 1)[0]
    token = re.split(r"[\\/]", token)[-1]
    if token.endswith(".md"):
        token = token[: -len(".md")]
    token = token.lower()
    return token if _RUNBOOK_ID_RE.match(token) else None


def resolve_runbook(
    *,
    service: str | None = None,
    runbook_ref: str | None = None,
) -> RunbookAttachment | None:
    """Best-effort runbook lookup for a notification.

    Never raises: a missing library directory or an unparseable file
    degrades to ``None`` so a notification is never lost to an
    attachment problem.
    """
    # Imported lazily: the chatops seam must stay importable in contexts
    # (tests, adapters-only usage) where the runbook library isn't seeded.
    try:
        from aiops.runbooks import get_runbook, search_runbooks
    except Exception as exc:  # pragma: no cover - import guard
        logger.debug("runbook library unavailable: %s", exc)
        return None

    rb = None
    candidate = _candidate_id(runbook_ref or "")
    if candidate:
        try:
            rb = get_runbook(candidate)
        except Exception as exc:
            logger.debug("runbook lookup by id %r failed: %s", candidate, exc)

    if rb is None and service:
        try:
            matches = search_runbooks(service=service)
        except Exception as exc:
            logger.debug("runbook search for service %r failed: %s", service, exc)
            matches = []
        if matches:
            # Prefer a published runbook over a draft when the library has
            # both for one service; otherwise take the first (id-sorted).
            rb = next(
                (r for r in matches if str(getattr(r.status, "value", r.status)) == "published"),
                matches[0],
            )

    if rb is None:
        return None

    entry = _link_map().get(rb.id) or {}
    return RunbookAttachment(
        runbook_id=rb.id,
        title=rb.title,
        filename=entry.get("filename") or f"{rb.id}.md",
        markdown=rb.body or "",
        url=entry.get("url") or None,
    )


__all__ = ["RunbookAttachment", "resolve_runbook"]
