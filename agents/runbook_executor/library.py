"""The executable-runbook library for RA-004.

A runbook is a markdown file with a YAML frontmatter block. RA-004 needs more
than the platform ``aiops.runbooks`` library carries today — it needs the
**structured, executable steps** (with per-step ``destructive`` / ``rollback``
metadata). Until those land on the shared ``aiops.runbooks.Runbook`` model, the
executable definitions live here, parsed with the same frontmatter idiom the
platform store uses (``aiops/runbooks/store.py``). Promoting them into the
shared library (adding a ``steps`` field) is the post-POC integration step.

Library directory resolution (mirrors ``AIOPS_RUNBOOKS_DIR``):

- ``AIOPS_RUNBOOK_EXECUTOR_DIR`` env var, else
- the shipped ``runbooks/`` directory next to this module.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from agents.runbook_executor.models import (
    ApplicabilityScope,
    Prerequisite,
    RunbookStatus,
    RunbookStep,
)

logger = logging.getLogger(__name__)

# Executable runbooks (with a ``steps:`` block) are version-controlled here,
# next to this module. This is the source of truth for what the executor can
# run — distinct from the runtime ``data/runbooks`` library the platform store /
# Knowledge Synthesizer write descriptive runbooks into (that dir is gitignored).
_SHIPPED_DIR = Path(__file__).resolve().parent / "runbooks"

# `---\n <yaml> \n---\n <body>` — DOTALL so yaml/body groups span lines.
# Same shape as aiops.runbooks.store._FRONTMATTER_RE.
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.S)


class ExecutableRunbook(BaseModel):
    """A selectable, runnable runbook: selection metadata + ordered steps.

    Version/lifecycle and applicability metadata are additive: a file that predates
    them still parses and is still *listed* (the read-only viewer and the picker keep
    working), but it is not *executable* — see :attr:`is_executable`.
    """

    id: str
    title: str
    service: str
    severity: str | None = None
    tags: list[str] = Field(default_factory=list)
    steps: list[RunbookStep] = Field(default_factory=list)
    body: str = ""

    # ── version + review lifecycle (§9/§10) ─────────────────────────────────
    # ``status`` has no default value that can execute: an omitted status parses as
    # DRAFT, which is refused. Fail-closed, matching RunbookStep.destructive=True.
    version: int = 1
    status: RunbookStatus = RunbookStatus.DRAFT
    owner: str = ""
    approved_by: str = ""
    created_at: str | None = None
    updated_at: str | None = None
    source_incident: str | None = None
    change_reason: str = ""
    previous_version: int | None = None

    # ── applicability + preconditions (§8) ──────────────────────────────────
    applicability: ApplicabilityScope = Field(default_factory=ApplicabilityScope)
    prerequisites: list[Prerequisite] = Field(default_factory=list)

    @property
    def ref(self) -> str:
        """Stable audit reference — id plus the version that actually ran."""
        return f"{self.id}@v{self.version}"

    @property
    def is_executable(self) -> bool:
        """True only for an ACTIVE version with a recorded approver and ≥1 step.

        This is the §9 gate ("ACTIVE + APPROVED"), expressed so that neither half
        can be satisfied by omission. Callers that need to explain a refusal use
        :meth:`executability_reason` instead of re-deriving the condition.
        """
        return (
            bool(self.steps)
            and not self.duplicate_step_names
            and self.status is RunbookStatus.ACTIVE
            and bool(self.approved_by)
        )

    @property
    def duplicate_step_names(self) -> list[str]:
        """Step names that appear more than once, in first-seen order.

        Step names are the key for per-step parameter overrides, per-step approval ids
        and the UI's list keys, so a duplicate is genuinely ambiguous — an override or an
        approval cannot say *which* step it means. The execution core no longer depends on
        name uniqueness for correctness (it pairs by position), but the operator-facing
        contract still does, so a colliding runbook is refused rather than silently
        interpreted.
        """
        seen: set[str] = set()
        duplicates: list[str] = []
        for step in self.steps:
            if step.name in seen and step.name not in duplicates:
                duplicates.append(step.name)
            seen.add(step.name)
        return duplicates

    def executability_reason(self) -> str:
        """Why this runbook may not execute, or ``""`` when it may.

        Returned verbatim to the operator as a blocking reason, so it names the
        field that has to change rather than saying "not allowed".
        """
        if not self.steps:
            return f"{self.id}: descriptive runbook — it declares no executable steps"
        duplicates = self.duplicate_step_names
        if duplicates:
            return (
                f"{self.id}: step name(s) {duplicates} appear more than once — a per-step "
                "override or approval could not say which step it meant"
            )
        if self.status is not RunbookStatus.ACTIVE:
            return (
                f"{self.id}: status is {self.status.value!r}; only "
                f"{RunbookStatus.ACTIVE.value!r} runbooks may execute"
            )
        if not self.approved_by:
            return f"{self.id}: no approved_by recorded — an ACTIVE version must name its approver"
        return ""


def _library_dir(directory: str | Path | None = None) -> Path:
    if directory is not None:
        return Path(directory)
    env = os.environ.get("AIOPS_RUNBOOK_EXECUTOR_DIR", "").strip()
    return Path(env) if env else _SHIPPED_DIR


def _parse(text: str) -> tuple[dict, str]:
    """Split a runbook file into (frontmatter dict, body markdown). A file with
    no frontmatter degrades to ``({}, text)`` rather than raising."""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    front = yaml.safe_load(m.group(1)) or {}
    if not isinstance(front, dict):
        front = {}
    return front, m.group(2)


def _normalize_status(front: dict) -> None:
    """Coerce an unrecognised ``status:`` to DRAFT in place, with a warning.

    Letting pydantic reject it would make the file unparseable, and an unparseable
    file is *skipped* — so a typo'd status would remove the runbook from the library
    listing entirely instead of showing it as not-executable. Refusing loudly beats
    disappearing silently, and DRAFT is the fail-closed landing spot.
    """
    raw = front.get("status")
    if raw is None:
        return
    try:
        front["status"] = RunbookStatus(str(raw).strip().lower())
    except ValueError:
        logger.warning(
            "runbook %r: unknown status %r — treating as %r (not executable); valid values: %s",
            front.get("id", "<unknown>"),
            raw,
            RunbookStatus.DRAFT.value,
            ", ".join(s.value for s in RunbookStatus),
        )
        front["status"] = RunbookStatus.DRAFT


def _runbook_from_file(path: Path) -> ExecutableRunbook:
    front, body = _parse(path.read_text(encoding="utf-8"))
    front = dict(front)
    # Filename is authoritative for the id (same rule as the platform store).
    front["id"] = path.stem
    front.pop("body", None)
    _normalize_status(front)
    steps = [RunbookStep(**s) for s in (front.pop("steps", None) or [])]
    return ExecutableRunbook(**front, steps=steps, body=body.strip())


def load_runbooks(directory: str | Path | None = None) -> list[ExecutableRunbook]:
    """All *executable* runbooks in the library, sorted by id. Files with no
    ``steps:`` block (the descriptive-only runbooks served by the platform
    store / dashboard viewer) are skipped — there's nothing for the executor to
    run. Unparseable files are skipped with a warning rather than breaking the
    whole listing (defensive parsing, same as the platform store)."""
    out: list[ExecutableRunbook] = []
    for p in sorted(_library_dir(directory).glob("*.md")):
        try:
            rb = _runbook_from_file(p)
        except Exception as exc:  # one bad file shouldn't sink the library
            logger.warning("skipping unparseable runbook %s: %s", p.name, exc)
            continue
        if rb.steps:  # executable only — ignore descriptive-only runbooks
            out.append(rb)
    return out


def get_runbook(runbook_id: str, directory: str | Path | None = None) -> ExecutableRunbook | None:
    """Load one runbook by id (filename stem), or None if absent."""
    p = _library_dir(directory) / f"{runbook_id}.md"
    if not p.exists():
        return None
    return _runbook_from_file(p)
