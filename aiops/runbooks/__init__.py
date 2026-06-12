"""Runbook library seam — file-backed store of incident runbooks.

The runbook-library counterpart to ``aiops.state``: agents and the demo server
call the functions re-exported here and never read the runbook directory
directly. v0 stores one markdown-with-frontmatter file per runbook under
``data/runbooks`` (configurable via ``AIOPS_RUNBOOKS_DIR``); the shipped
baseline is seeded from a tracked seed directory on first boot.
"""

from __future__ import annotations

from aiops.runbooks.models import ReviewStatus, Runbook
from aiops.runbooks.store import (
    delete_all_runbooks,
    delete_runbook,
    ensure_seeded,
    get_runbook,
    list_runbooks,
    save_runbook,
    search_runbooks,
    seed_from_dir,
)

__all__ = [
    "ReviewStatus",
    "Runbook",
    "delete_all_runbooks",
    "delete_runbook",
    "ensure_seeded",
    "get_runbook",
    "list_runbooks",
    "save_runbook",
    "search_runbooks",
    "seed_from_dir",
]
