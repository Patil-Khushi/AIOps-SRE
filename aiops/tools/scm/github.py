"""GitHub provider for the ``scm.*`` capabilities.

Gives agents read-only access to the application's source, infrastructure
manifests and deployment config, so an RCA can correlate an incident against
what actually changed. "p95 crossed 2s at 14:32; commit a1c5cda touching
order-service/src/db/ merged at 14:28" is a fundamentally stronger root cause
than any metric pattern alone.

Mirrors the ``loki`` / ``jaeger`` providers: env-configured, short connect
timeout, process-local circuit breaker so an unreachable or rate-limited GitHub
degrades fast instead of adding latency to every agent call.

READ-ONLY BY CONSTRUCTION. Every function issues GET only; nothing here can
create, update or delete. Use a fine-grained PAT scoped to the single repo with
Contents:Read + Metadata:Read and nothing else. The seam cannot enforce the
token's scope — that is the operator's job — but it will never *ask* to write.

Configuration::

    AIOPS_GITHUB_REPO      owner/name        (required; e.g. Patil-Khushi/AIOps-SRE)
    AIOPS_GITHUB_TOKEN     read-only PAT     (required for private repos)
    AIOPS_GITHUB_REF       default branch    (default: main)
    AIOPS_GITHUB_API_URL   API base          (default: https://api.github.com;
                                              set for GitHub Enterprise)

Every call degrades to ``ToolResult(ok=False, ...)`` rather than raising, so a
missing token disables source correlation without breaking the agent.
"""

from __future__ import annotations

import base64
import os
import time
from typing import Any

import httpx

from aiops.tools.registry import ToolResult, tool

from ._secrets import scrub

_API = os.environ.get("AIOPS_GITHUB_API_URL", "https://api.github.com").rstrip("/")
_REPO = os.environ.get("AIOPS_GITHUB_REPO", "")
_TOKEN = os.environ.get("AIOPS_GITHUB_TOKEN", "")
_DEFAULT_REF = os.environ.get("AIOPS_GITHUB_REF", "main")

_TIMEOUT = float(os.environ.get("AIOPS_GITHUB_TIMEOUT", "10"))
_CONNECT_TIMEOUT = float(os.environ.get("AIOPS_GITHUB_CONNECT_TIMEOUT", "3"))

# Circuit breaker, same rationale as loki.py: after a failure, short-circuit for
# this long instead of re-paying the connect cost on every call. GitHub adds a
# second reason — secondary rate limits. Hammering after a 403 makes the block
# worse, so backing off is the correct behaviour, not just the fast one.
_CIRCUIT_OPEN_SECONDS = float(os.environ.get("AIOPS_GITHUB_CIRCUIT_OPEN_SECONDS", "60"))
_circuit_open_until: float = 0.0

# Cap file size we will pull into an LLM context. GitHub's contents API itself
# refuses >1 MB, but a 400 KB lockfile is already useless context and expensive.
_MAX_FILE_BYTES = int(os.environ.get("AIOPS_GITHUB_MAX_FILE_BYTES", "131072"))


def _reset_circuit_for_tests() -> None:
    """Reset the breaker. Test seam only — mirrors ``loki._reset_circuit_for_tests``.

    Module state survives across pytest boundaries, so one test tripping the
    breaker would short-circuit the next 60s of unrelated tests.
    """
    global _circuit_open_until
    _circuit_open_until = 0.0


def _headers() -> dict[str, str]:
    h = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if _TOKEN:
        h["Authorization"] = f"Bearer {_TOKEN}"
    return h


def _get(path: str, params: dict[str, Any] | None = None) -> ToolResult:
    """GET against the configured repo. Never raises."""
    global _circuit_open_until

    if not _REPO:
        return ToolResult(
            ok=False,
            error="AIOPS_GITHUB_REPO is not set (expected 'owner/name')",
            metadata={"provider": "github"},
        )

    now = time.monotonic()
    if now < _circuit_open_until:
        return ToolResult(
            ok=False,
            error="github circuit open (recent failure); skipping call",
            metadata={"provider": "github", "circuit_open": True},
        )

    url = f"{_API}/repos/{_REPO}{path}"
    try:
        resp = httpx.get(
            url,
            params=params,
            headers=_headers(),
            timeout=httpx.Timeout(_TIMEOUT, connect=_CONNECT_TIMEOUT),
        )
    except Exception as exc:
        _circuit_open_until = now + _CIRCUIT_OPEN_SECONDS
        return ToolResult(
            ok=False, error=f"github request failed: {exc}", metadata={"provider": "github"}
        )

    # Rate-limit headers are worth surfacing: an agent that suddenly stops
    # getting source context should be diagnosable without a packet capture.
    meta = {
        "provider": "github",
        "repo": _REPO,
        "status": resp.status_code,
        "rate_limit_remaining": resp.headers.get("x-ratelimit-remaining"),
    }

    if resp.status_code == 404:
        # Not a transport failure — do NOT trip the breaker. A missing path is
        # a normal answer to "does this file exist at this ref?".
        return ToolResult(ok=False, error=f"not found: {path}", metadata=meta)

    if resp.status_code in (401, 403):
        _circuit_open_until = now + _CIRCUIT_OPEN_SECONDS
        hint = "check AIOPS_GITHUB_TOKEN scope" if resp.status_code == 401 else "rate limited?"
        return ToolResult(ok=False, error=f"github {resp.status_code} ({hint})", metadata=meta)

    if resp.status_code >= 400:
        _circuit_open_until = now + _CIRCUIT_OPEN_SECONDS
        return ToolResult(ok=False, error=f"github {resp.status_code}", metadata=meta)

    try:
        return ToolResult(ok=True, data=resp.json(), metadata=meta)
    except Exception as exc:
        return ToolResult(ok=False, error=f"github returned non-JSON: {exc}", metadata=meta)


# --- capabilities -----------------------------------------------------------


@tool(
    name="github.file.read",
    capability="scm.file.read",
    provider="github",
    description="Read one file's contents at a ref (secrets scrubbed).",
)
def read_file(path: str, ref: str | None = None) -> ToolResult:
    """Return the decoded contents of ``path`` at ``ref``."""
    res = _get(f"/contents/{path.lstrip('/')}", {"ref": ref or _DEFAULT_REF})
    if not res.ok:
        return res

    body = res.data or {}
    if isinstance(body, list):
        return ToolResult(
            ok=False,
            error=f"{path} is a directory; use scm.repo.tree",
            metadata=res.metadata,
        )

    size = int(body.get("size") or 0)
    if size > _MAX_FILE_BYTES:
        return ToolResult(
            ok=False,
            error=f"{path} is {size} bytes, over the {_MAX_FILE_BYTES} limit",
            metadata={**res.metadata, "size": size},
        )

    raw = body.get("content") or ""
    try:
        text = base64.b64decode(raw).decode("utf-8", errors="replace")
    except Exception as exc:
        return ToolResult(ok=False, error=f"could not decode {path}: {exc}", metadata=res.metadata)

    text, findings = scrub(text)
    return ToolResult(
        ok=True,
        data={"path": path, "ref": ref or _DEFAULT_REF, "size": size, "content": text},
        metadata={**res.metadata, "redactions": findings},
    )


@tool(
    name="github.repo.tree",
    capability="scm.repo.tree",
    provider="github",
    description="List file paths in the repo at a ref.",
)
def repo_tree(ref: str | None = None, prefix: str = "") -> ToolResult:
    """List tracked file paths, optionally filtered to those under ``prefix``."""
    res = _get(f"/git/trees/{ref or _DEFAULT_REF}", {"recursive": "1"})
    if not res.ok:
        return res

    body = res.data or {}
    paths = [
        item["path"]
        for item in body.get("tree", [])
        if item.get("type") == "blob" and item.get("path", "").startswith(prefix)
    ]
    return ToolResult(
        ok=True,
        data={"ref": ref or _DEFAULT_REF, "prefix": prefix, "paths": paths, "count": len(paths)},
        # GitHub silently truncates very large trees; a caller that gets a
        # short list needs to know it was cut rather than assume completeness.
        metadata={**res.metadata, "truncated": bool(body.get("truncated"))},
    )


@tool(
    name="github.commit.history",
    capability="scm.commit.history",
    provider="github",
    description="Recent commits, optionally scoped to a path and time window.",
)
def commit_history(
    path: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 20,
    ref: str | None = None,
) -> ToolResult:
    """Commits touching ``path``, newest first.

    This is the change-correlation primitive: given an incident onset time,
    ``since`` a little before it and a ``path`` for the affected service
    answers "what changed just before this broke?".
    ``since``/``until`` are ISO-8601 (e.g. ``2026-08-03T10:00:00Z``).

    ``ref`` selects the branch (GitHub's ``sha`` parameter). It defaults to
    ``AIOPS_GITHUB_REF`` rather than letting GitHub fall back to the default
    branch: when the deployed code lives on a feature branch, querying the
    default branch returns an empty list and looks exactly like "nothing
    changed" — the most dangerous possible wrong answer for change correlation.
    """
    params: dict[str, Any] = {
        "per_page": max(1, min(int(limit), 100)),
        "sha": ref or _DEFAULT_REF,
    }
    if path:
        params["path"] = path
    if since:
        params["since"] = since
    if until:
        params["until"] = until

    res = _get("/commits", params)
    if not res.ok:
        return res

    # Shape guard: this endpoint returns a JSON array. If GitHub answers with an
    # object instead (an error body that still carried a 2xx, or a proxy in the
    # path), iterating it yields strings and the comprehension below dies with
    # AttributeError — raising into the agent and breaking this seam's
    # never-raise contract. Degrade instead.
    if not isinstance(res.data, list):
        return ToolResult(
            ok=False,
            error=f"expected a list of commits, got {type(res.data).__name__}",
            metadata=res.metadata,
        )

    commits = [
        {
            "sha": c.get("sha", "")[:12],
            "message": ((c.get("commit") or {}).get("message") or "").split("\n")[0],
            "author": ((c.get("commit") or {}).get("author") or {}).get("name"),
            "date": ((c.get("commit") or {}).get("author") or {}).get("date"),
            "url": c.get("html_url"),
        }
        for c in (res.data or [])
    ]
    return ToolResult(
        ok=True,
        data={
            "path": path,
            "ref": ref or _DEFAULT_REF,
            "since": since,
            "until": until,
            "commits": commits,
            "count": len(commits),
        },
        metadata=res.metadata,
    )


@tool(
    name="github.diff",
    capability="scm.diff",
    provider="github",
    description="Files and patch hunks changed between two refs.",
)
def diff(base: str, head: str, max_files: int = 50) -> ToolResult:
    """Compare two refs and return the changed files.

    Patches are scrubbed for secrets — a diff is one of the likelier places a
    credential shows up, precisely because it is the moment one gets added.
    """
    res = _get(f"/compare/{base}...{head}")
    if not res.ok:
        return res

    body = res.data or {}
    files_in = body.get("files") or []
    files = []
    total_redactions: dict[str, int] = {}
    for f in files_in[:max_files]:
        patch, findings = scrub(f.get("patch") or "")
        for k, v in findings.items():
            total_redactions[k] = total_redactions.get(k, 0) + v
        files.append(
            {
                "filename": f.get("filename"),
                "status": f.get("status"),
                "additions": f.get("additions"),
                "deletions": f.get("deletions"),
                "patch": patch,
            }
        )

    return ToolResult(
        ok=True,
        data={
            "base": base,
            "head": head,
            "ahead_by": body.get("ahead_by"),
            "behind_by": body.get("behind_by"),
            "files": files,
            "total_files": len(files_in),
        },
        metadata={
            **res.metadata,
            "redactions": total_redactions,
            # Explicit, so a caller never mistakes a capped list for the whole diff.
            "truncated": len(files_in) > max_files,
        },
    )


@tool(
    name="github.pr.list",
    capability="scm.pr.list",
    provider="github",
    description="Recent pull requests, newest first.",
)
def list_prs(state: str = "closed", limit: int = 20) -> ToolResult:
    """Recent PRs. ``state`` is one of open / closed / all.

    Useful for "was anything merged around the incident window?" when commits
    alone are ambiguous (squash merges collapse a branch into one commit).
    """
    if state not in ("open", "closed", "all"):
        return ToolResult(
            ok=False, error=f"invalid state {state!r}", metadata={"provider": "github"}
        )

    res = _get(
        "/pulls",
        {
            "state": state,
            "sort": "updated",
            "direction": "desc",
            "per_page": max(1, min(int(limit), 100)),
        },
    )
    if not res.ok:
        return res

    # Same shape guard as commit_history — see the comment there.
    if not isinstance(res.data, list):
        return ToolResult(
            ok=False,
            error=f"expected a list of pull requests, got {type(res.data).__name__}",
            metadata=res.metadata,
        )

    prs = [
        {
            "number": p.get("number"),
            "title": p.get("title"),
            "state": p.get("state"),
            "merged_at": p.get("merged_at"),
            "user": (p.get("user") or {}).get("login"),
            "url": p.get("html_url"),
        }
        for p in (res.data or [])
    ]
    return ToolResult(
        ok=True,
        data={"state": state, "pull_requests": prs, "count": len(prs)},
        metadata=res.metadata,
    )
