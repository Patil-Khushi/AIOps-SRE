"""Runbook model for the file-backed runbook library seam.

A *runbook* is a human-authored, human-reviewed, git-diffable procedure for
resolving a class of incident. It is stored as a markdown file with a YAML
frontmatter block (the machine-readable metadata we index/search/dedup on)
followed by the procedure body.

This model is the seam's storage contract — the runbook-library counterpart to
``aiops.state.models.*Row``. ``aiops.runbooks.store`` is responsible for
mapping between this model and the on-disk ``<id>.md`` file. Agents speak to
the library through ``aiops.runbooks`` functions and never read the directory
directly (CLAUDE.md non-negotiable #1).
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ReviewStatus(StrEnum):
    """Review lifecycle of a runbook (mirrors the KB-article review states).

    - ``DRAFT``          — synthesizer-generated, not yet submitted.
    - ``PENDING_REVIEW`` — submitted; awaiting the platform HITL approval.
    - ``PUBLISHED``      — approved by a human; this is the live library entry.
    - ``REJECTED``       — a human declined it; kept for audit, not served.

    Seed runbooks ship as ``PUBLISHED`` so the library has a usable baseline.
    """

    DRAFT = "draft"
    PENDING_REVIEW = "pending_review"
    PUBLISHED = "published"
    REJECTED = "rejected"


class Runbook(BaseModel):
    """One runbook = frontmatter metadata + a markdown procedure body.

    ``extra="ignore"`` so an unexpected frontmatter key in a hand-edited file
    doesn't blow up the loader — we round-trip the known fields and drop the
    rest, matching the defensive parsing the rest of the repo uses for
    externally-authored content.
    """

    model_config = ConfigDict(extra="ignore")

    id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    service: str = Field(min_length=1)
    version: int = 1
    tags: list[str] = Field(default_factory=list)
    severity: str | None = None
    # "seed" for the shipped baseline library; "live" for runtime-written
    # (synthesizer-suggested, human-approved) runbooks.
    source: str = "live"
    # Incident that produced or last updated this runbook. None for seeds.
    source_incident: str | None = None
    status: ReviewStatus = ReviewStatus.DRAFT
    # Linked KB article id from the same synthesis pass, when there is one.
    related_kb: str | None = None
    last_updated: str | None = None
    # The markdown procedure (everything after the frontmatter block).
    body: str = ""

    @field_validator("last_updated", mode="before")
    @classmethod
    def _coerce_date(cls, v: Any) -> Any:
        """Unquoted ``last_updated: 2026-06-11`` is parsed by YAML as a
        ``date``, not a string. Coerce date/datetime to an ISO string so
        hand-edited frontmatter doesn't have to remember to quote it."""
        if isinstance(v, datetime | date):
            return v.isoformat()
        return v


__all__ = ["ReviewStatus", "Runbook"]
