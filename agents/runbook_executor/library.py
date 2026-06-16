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

from agents.runbook_executor.models import RunbookStep

logger = logging.getLogger(__name__)

_SHIPPED_DIR = Path(__file__).resolve().parent / "runbooks"

# `---\n <yaml> \n---\n <body>` — DOTALL so yaml/body groups span lines.
# Same shape as aiops.runbooks.store._FRONTMATTER_RE.
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.S)


class ExecutableRunbook(BaseModel):
    """A selectable, runnable runbook: selection metadata + ordered steps."""

    id: str
    title: str
    service: str
    severity: str | None = None
    tags: list[str] = Field(default_factory=list)
    steps: list[RunbookStep] = Field(default_factory=list)
    body: str = ""


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


def _runbook_from_file(path: Path) -> ExecutableRunbook:
    front, body = _parse(path.read_text(encoding="utf-8"))
    front = dict(front)
    # Filename is authoritative for the id (same rule as the platform store).
    front["id"] = path.stem
    front.pop("body", None)
    steps = [RunbookStep(**s) for s in (front.pop("steps", None) or [])]
    return ExecutableRunbook(**front, steps=steps, body=body.strip())


def load_runbooks(directory: str | Path | None = None) -> list[ExecutableRunbook]:
    """All runbooks in the library, sorted by id. Unparseable files are skipped
    with a warning rather than breaking the whole listing (defensive parsing,
    same as the platform store)."""
    out: list[ExecutableRunbook] = []
    for p in sorted(_library_dir(directory).glob("*.md")):
        try:
            out.append(_runbook_from_file(p))
        except Exception as exc:  # one bad file shouldn't sink the library
            logger.warning("skipping unparseable runbook %s: %s", p.name, exc)
    return out


def get_runbook(runbook_id: str, directory: str | Path | None = None) -> ExecutableRunbook | None:
    """Load one runbook by id (filename stem), or None if absent."""
    p = _library_dir(directory) / f"{runbook_id}.md"
    if not p.exists():
        return None
    return _runbook_from_file(p)
