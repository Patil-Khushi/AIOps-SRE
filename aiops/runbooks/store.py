"""File-backed runbook library — the agent-facing API for runbooks.

Agents and the demo server only import functions from this module; they never
touch the runbook directory directly. That keeps the "agents don't know about
storage" invariant the same way ``aiops.state.repository`` hides SQL and
``aiops.llm`` hides the LLM SDK (CLAUDE.md non-negotiable #1).

Storage is one markdown file per runbook under the library directory:

    <library>/<id>.md

where each file is a YAML frontmatter block (the indexed metadata) followed by
the markdown procedure body. v0 is deliberately file-based — runbooks are
human-authored, human-reviewed, git-diffable prose, so a directory of markdown
is the natural store. Swapping to Confluence / ServiceNow Knowledge later
(the catalog's real targets) becomes a change behind this seam, not an agent
rewrite.

Config:

- ``AIOPS_RUNBOOKS_DIR`` — library directory. Default ``data/runbooks``
  (created on first use). The shipped baseline lives in a *tracked* seed
  directory and is copied into the live (gitignored) library via
  :func:`seed_from_dir` on first boot — mirroring how
  ``agents.incident_classifier._seed`` seeds the historical-incident store.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

import yaml

from aiops.runbooks.models import ReviewStatus, Runbook

logger = logging.getLogger(__name__)

_DEFAULT_DIR = "data/runbooks"

# Frontmatter field order, so serialized files stay stable + diff-friendly
# (yaml.safe_dump with sort_keys would otherwise alphabetize them).
_FRONTMATTER_ORDER = (
    "id",
    "title",
    "service",
    "version",
    "tags",
    "severity",
    "source",
    "source_incident",
    "status",
    "related_kb",
    "last_updated",
)

# A runbook id maps directly to a filename, so it must not be able to escape
# the library directory or contain path separators. Slugs only.
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

# `---\n <yaml> \n---\n <body>`. DOTALL so the yaml/body groups span lines.
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.S)


# ─── paths / ids ─────────────────────────────────────────────────────────────


def _library_dir() -> Path:
    d = os.environ.get("AIOPS_RUNBOOKS_DIR", "").strip() or _DEFAULT_DIR
    p = Path(d)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _validate_id(runbook_id: str) -> str:
    if not _ID_RE.match(runbook_id or ""):
        raise ValueError(
            f"invalid runbook id {runbook_id!r}: must be a slug "
            "(letters/digits/.-_, no path separators)"
        )
    return runbook_id


# ─── (de)serialization ───────────────────────────────────────────────────────


def _parse(text: str) -> tuple[dict, str]:
    """Split a runbook file into (frontmatter dict, body markdown).

    A file with no frontmatter block returns ``({}, text)`` so a stray plain
    markdown file degrades to an all-defaults runbook rather than raising.
    """
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    front = yaml.safe_load(m.group(1)) or {}
    if not isinstance(front, dict):
        front = {}
    return front, m.group(2)


def _runbook_from_file(path: Path) -> Runbook:
    front, body = _parse(path.read_text(encoding="utf-8"))
    front = dict(front)
    # Filename is authoritative for the id — a frontmatter id that disagrees
    # with the filename would make get_runbook(id) and the on-disk name drift.
    front["id"] = path.stem
    front.pop("body", None)  # body comes from the file body, never frontmatter
    return Runbook(**front, body=body.strip() + "\n" if body.strip() else "")


def _serialize(rb: Runbook) -> str:
    fm = {
        "id": rb.id,
        "title": rb.title,
        "service": rb.service,
        "version": rb.version,
        "tags": list(rb.tags),
        "severity": rb.severity,
        "source": rb.source,
        "source_incident": rb.source_incident,
        "status": rb.status.value,
        "related_kb": rb.related_kb,
        "last_updated": rb.last_updated,
    }
    ordered = {k: fm[k] for k in _FRONTMATTER_ORDER if k in fm}
    front = yaml.safe_dump(
        ordered, sort_keys=False, allow_unicode=True, default_flow_style=False
    ).strip()
    body = rb.body.strip()
    return f"---\n{front}\n---\n\n{body}\n"


def _normalize_service(service: str) -> str:
    """Collapse the many spellings of a service to a base key for matching.

    Lower-cases, strips separators, drops a trailing ``service`` suffix, so
    ``"recommendation"``, ``"recommendationservice"`` and
    ``"recommendation-service"`` all match. A local copy of the same idea in
    ``agents.rca_agent.remediation_map`` — duplicated rather than imported so
    the platform seam doesn't depend on an agent package.
    """
    s = (service or "").lower().strip()
    for sep in ("-", "_", " "):
        s = s.replace(sep, "")
    if s.endswith("service") and len(s) > len("service"):
        s = s[: -len("service")]
    return s


# ─── public API ──────────────────────────────────────────────────────────────


def list_runbooks() -> list[Runbook]:
    """All runbooks in the library, sorted by id. Unparseable files are
    skipped with a warning rather than failing the whole listing."""
    out: list[Runbook] = []
    for p in sorted(_library_dir().glob("*.md")):
        try:
            out.append(_runbook_from_file(p))
        except Exception as exc:  # one bad file shouldn't break the library
            logger.warning("skipping unparseable runbook %s: %s", p.name, exc)
    return out


def get_runbook(runbook_id: str) -> Runbook | None:
    """Load one runbook by id, or ``None`` if it isn't in the library."""
    _validate_id(runbook_id)
    p = _library_dir() / f"{runbook_id}.md"
    if not p.exists():
        return None
    return _runbook_from_file(p)


def save_runbook(rb: Runbook, *, bump_version: bool = False) -> Runbook:
    """Write a runbook to the library (create or overwrite ``<id>.md``).

    With ``bump_version=True`` and an existing runbook of the same id, the
    saved version becomes ``existing.version + 1`` — the "update an existing
    runbook" path the synthesizer uses after a human approves a suggestion.
    Returns the runbook as written (with the possibly-bumped version).
    """
    _validate_id(rb.id)
    if bump_version:
        existing = get_runbook(rb.id)
        if existing is not None:
            rb = rb.model_copy(update={"version": existing.version + 1})
    (_library_dir() / f"{rb.id}.md").write_text(_serialize(rb), encoding="utf-8")
    return rb


def search_runbooks(
    *,
    service: str | None = None,
    query: str | None = None,
    status: ReviewStatus | str | None = None,
) -> list[Runbook]:
    """Filter the library. ``service`` matches on the normalized service key;
    ``query`` is a case-insensitive substring over title / tags / body;
    ``status`` filters by review state. All filters combine with AND."""
    items = list_runbooks()
    if service:
        ns = _normalize_service(service)
        items = [r for r in items if _normalize_service(r.service) == ns]
    if status is not None:
        want = ReviewStatus(status) if not isinstance(status, ReviewStatus) else status
        items = [r for r in items if r.status == want]
    if query:
        q = query.lower()
        items = [
            r
            for r in items
            if q in r.title.lower() or any(q in t.lower() for t in r.tags) or q in r.body.lower()
        ]
    return items


def seed_from_dir(source_dir: str | Path, *, overwrite: bool = False) -> int:
    """Copy markdown runbooks from a (tracked) seed directory into the library.

    Idempotent: by default an id already present in the library is left alone,
    so calling this on every boot is safe. Pass ``overwrite=True`` to refresh
    seeds from source. Returns the number of runbooks written.
    """
    src = Path(source_dir)
    if not src.exists():
        logger.info("runbook seed dir %s does not exist; nothing to seed", src)
        return 0
    written = 0
    for p in sorted(src.glob("*.md")):
        try:
            rb = _runbook_from_file(p)
        except Exception as exc:
            logger.warning("skipping unparseable seed runbook %s: %s", p.name, exc)
            continue
        if not overwrite and get_runbook(rb.id) is not None:
            continue
        save_runbook(rb)
        written += 1
    if written:
        logger.info("seeded %d runbook(s) from %s", written, src)
    return written


def ensure_seeded(source_dir: str | Path) -> int:
    """Seed the library from ``source_dir`` only when it is currently empty.

    The boot-time convenience the synthesizer calls: a populated library
    (operator-curated or previously seeded) is never disturbed."""
    if list_runbooks():
        return 0
    return seed_from_dir(source_dir)


def delete_runbook(runbook_id: str) -> bool:
    """Remove one runbook file. Returns True if it existed. Test/admin hook."""
    _validate_id(runbook_id)
    p = _library_dir() / f"{runbook_id}.md"
    if not p.exists():
        return False
    p.unlink()
    return True


def delete_all_runbooks() -> int:
    """Wipe the library. Test hook; not a production path."""
    count = 0
    for p in _library_dir().glob("*.md"):
        p.unlink()
        count += 1
    return count


__all__ = [
    "delete_all_runbooks",
    "delete_runbook",
    "ensure_seeded",
    "get_runbook",
    "list_runbooks",
    "save_runbook",
    "search_runbooks",
    "seed_from_dir",
]
