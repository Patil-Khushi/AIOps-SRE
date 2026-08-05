"""Tests for the SCM (GitHub) seam.

Focus on the properties that matter for a seam agents depend on:
  * it degrades instead of raising when unconfigured or unreachable,
  * it never issues a write,
  * secrets in file contents and diffs are scrubbed,
  * a 404 does not trip the circuit breaker (it is a normal answer).

No network: httpx is monkeypatched throughout.
"""

from __future__ import annotations

import base64

import httpx
import pytest

from aiops.tools import get_registry
from aiops.tools.scm import github
from aiops.tools.scm._secrets import scrub


@pytest.fixture(autouse=True)
def _fresh_circuit():
    github._reset_circuit_for_tests()
    yield
    github._reset_circuit_for_tests()


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(github, "_REPO", "acme/widgets")
    monkeypatch.setattr(github, "_TOKEN", "ghp_" + "x" * 36)
    monkeypatch.setattr(github, "_DEFAULT_REF", "main")


def _resp(payload, status=200, headers=None):
    return httpx.Response(
        status_code=status,
        json=payload,
        headers=headers or {},
        request=httpx.Request("GET", "https://api.github.com/x"),
    )


# ─── registration ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "capability",
    ["scm.file.read", "scm.repo.tree", "scm.commit.history", "scm.diff", "scm.pr.list"],
)
def test_capability_is_registered(capability):
    assert capability in {t.capability for t in get_registry().list()}


def test_capabilities_are_vendor_neutral():
    """Names must be scm.*, not github.* — a GitLab provider must be able to
    register against the same capability without agents changing."""
    for t in get_registry().list():
        if t.provider == "github":
            assert t.capability.startswith("scm."), (
                f"{t.name} registers capability {t.capability!r}; agents would "
                "become coupled to GitHub"
            )


# ─── degradation ─────────────────────────────────────────────────────────────


def test_unconfigured_repo_degrades_not_raises(monkeypatch):
    monkeypatch.setattr(github, "_REPO", "")
    res = github.read_file("README.md")
    assert res.ok is False
    assert "AIOPS_GITHUB_REPO" in res.error


def test_transport_failure_degrades(monkeypatch, configured):
    def boom(*a, **k):
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(httpx, "get", boom)
    res = github.read_file("README.md")
    assert res.ok is False
    assert "github request failed" in res.error


def test_transport_failure_opens_circuit(monkeypatch, configured):
    calls = {"n": 0}

    def boom(*a, **k):
        calls["n"] += 1
        raise httpx.ConnectError("down")

    monkeypatch.setattr(httpx, "get", boom)
    github.read_file("a.py")
    second = github.read_file("b.py")
    assert calls["n"] == 1, "circuit should short-circuit the second call"
    assert second.metadata.get("circuit_open") is True


def test_404_does_not_open_circuit(monkeypatch, configured):
    """A missing path is a legitimate answer, not a transport fault. Tripping
    the breaker here would block real lookups for a minute after any typo."""
    calls = {"n": 0}

    def missing(*a, **k):
        calls["n"] += 1
        return _resp({"message": "Not Found"}, status=404)

    monkeypatch.setattr(httpx, "get", missing)
    first = github.read_file("nope.py")
    second = github.read_file("also-nope.py")
    assert first.ok is False and second.ok is False
    assert calls["n"] == 2, "404 must not short-circuit subsequent calls"


# ─── read-only ───────────────────────────────────────────────────────────────


def test_only_issues_get_requests(monkeypatch, configured):
    """The seam must be incapable of mutating the repo."""
    seen: list[str] = []

    def record(url, **kwargs):
        seen.append("GET")
        # /commits and /pulls return arrays; /git/trees returns an object.
        # A one-size payload would exercise the shape guards instead of the
        # happy path this test is about.
        payload = (
            []
            if ("/commits" in url or "/pulls" in url)
            else {"sha": "abc", "tree": [], "truncated": False}
        )
        return _resp(payload)

    monkeypatch.setattr(httpx, "get", record)
    for verb in ("post", "put", "patch", "delete"):
        monkeypatch.setattr(httpx, verb, lambda *a, **k: pytest.fail("SCM seam must never write"))

    github.repo_tree()
    github.commit_history()
    github.list_prs()
    assert seen and set(seen) == {"GET"}


# ─── content handling ────────────────────────────────────────────────────────


def test_read_file_decodes_and_scrubs(monkeypatch, configured):
    body = "DB_PASSWORD=sup3rs3cretvalue\nprint('ok')\n"
    monkeypatch.setattr(
        httpx,
        "get",
        lambda *a, **k: _resp(
            {"size": len(body), "content": base64.b64encode(body.encode()).decode()}
        ),
    )
    res = github.read_file("app/config.py")
    assert res.ok
    assert "sup3rs3cretvalue" not in res.data["content"]
    assert "DB_PASSWORD" in res.data["content"], "key name should survive; only the value goes"
    assert res.metadata["redactions"]


def test_read_file_rejects_oversized(monkeypatch, configured):
    monkeypatch.setattr(github, "_MAX_FILE_BYTES", 10)
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _resp({"size": 5000, "content": ""}))
    res = github.read_file("package-lock.json")
    assert res.ok is False
    assert "over the" in res.error


def test_read_file_on_directory_is_an_error(monkeypatch, configured):
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _resp([{"name": "a.py"}]))
    res = github.read_file("src")
    assert res.ok is False
    assert "directory" in res.error


def test_tree_reports_truncation(monkeypatch, configured):
    monkeypatch.setattr(
        httpx,
        "get",
        lambda *a, **k: _resp(
            {
                "tree": [
                    {"type": "blob", "path": "a.py"},
                    {"type": "tree", "path": "src"},
                    {"type": "blob", "path": "src/b.py"},
                ],
                "truncated": True,
            }
        ),
    )
    res = github.repo_tree(prefix="src/")
    assert res.ok
    assert res.data["paths"] == ["src/b.py"], "trees and non-matching prefixes filtered out"
    assert res.metadata["truncated"] is True


def test_diff_scrubs_patches_and_flags_truncation(monkeypatch, configured):
    files = [
        {
            "filename": f"f{i}.py",
            "status": "modified",
            "additions": 1,
            "deletions": 0,
            "patch": "+API_KEY=abcdef123456789",
        }
        for i in range(5)
    ]
    monkeypatch.setattr(
        httpx, "get", lambda *a, **k: _resp({"files": files, "ahead_by": 5, "behind_by": 0})
    )
    res = github.diff("main", "feature", max_files=2)
    assert res.ok
    assert len(res.data["files"]) == 2
    assert res.data["total_files"] == 5
    assert res.metadata["truncated"] is True
    assert all("abcdef123456789" not in f["patch"] for f in res.data["files"])


def test_commit_history_shape(monkeypatch, configured):
    monkeypatch.setattr(
        httpx,
        "get",
        lambda *a, **k: _resp(
            [
                {
                    "sha": "a1c5cda1234567890",
                    "html_url": "u",
                    "commit": {
                        "message": "fix: thing\n\nbody",
                        "author": {"name": "K", "date": "d"},
                    },
                }
            ]
        ),
    )
    res = github.commit_history(path="demo/ecommerce", since="2026-08-03T10:00:00Z")
    assert res.ok
    c = res.data["commits"][0]
    assert c["sha"] == "a1c5cda12345", "sha is shortened for prompt economy"
    assert c["message"] == "fix: thing", "only the subject line, not the whole body"


def test_list_prs_rejects_bad_state(configured):
    assert github.list_prs(state="bogus").ok is False


# ─── scrubber ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "secret",
    [
        "ghp_" + "a" * 36,
        "AKIAIOSFODNN7EXAMPLE",
        "xoxb-1111111111-abcdefghij",
        "sk-ant-" + "b" * 32,
        "https://hooks.slack.com/services/T00/B00/XXXXXXXX",
    ],
)
def test_scrub_catches_known_secret_shapes(secret):
    out, findings = scrub(f"value = {secret}")
    assert secret not in out
    assert findings


def test_scrub_redacts_url_credentials():
    out, _ = scrub("postgresql+psycopg://appuser:apppass@postgres:5432/orders")
    assert "apppass" not in out
    assert "postgres:5432/orders" in out, "host and db should survive for context"


def test_scrub_does_not_touch_ordinary_code():
    """Over-redaction makes source useless for RCA."""
    code = "def handler(request):\n    return {'status': 'ok', 'latency_ms': 42}\n"
    out, findings = scrub(code)
    assert out == code
    assert findings == {}


def test_scrub_ignores_empty_placeholder_values():
    """Template files with `PASSWORD=` must not be reported as findings."""
    out, findings = scrub('PASSWORD=\nTOKEN: ""\n')
    assert findings == {}
    assert out == 'PASSWORD=\nTOKEN: ""\n'


def test_commit_history_degrades_on_unexpected_shape(monkeypatch, configured):
    """A 2xx carrying an object instead of an array must not raise into the agent."""
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _resp({"message": "weird"}))
    res = github.commit_history()
    assert res.ok is False
    assert "expected a list of commits" in res.error


def test_list_prs_degrades_on_unexpected_shape(monkeypatch, configured):
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _resp({"message": "weird"}))
    res = github.list_prs()
    assert res.ok is False
    assert "expected a list of pull requests" in res.error


# ─── policy ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "capability",
    ["scm.file.read", "scm.repo.tree", "scm.commit.history", "scm.diff", "scm.pr.list"],
)
def test_scm_capabilities_are_autonomy_none(capability):
    """Reads must not be HITL-gated.

    Left to the registry default these map to OPTIONAL, which implies a human
    might be asked to approve reading a file — wrong semantics, and it would
    add an approval hop to every RCA. A write path (scm.pr.create) must be a
    separate capability at REQUIRED, never a relaxation of these.
    """
    from aiops.policy import AutonomyLevel, get_gate

    assert get_gate().level_for(capability) is AutonomyLevel.NONE


def test_commit_history_pins_the_ref(monkeypatch, configured):
    """Must send `sha`, else GitHub silently answers for the default branch.

    Change correlation against a feature branch would then return an empty
    list — indistinguishable from "nothing changed", which is the worst
    possible wrong answer for an RCA.
    """
    captured: dict = {}

    def record(url, params=None, **kwargs):
        captured.update(params or {})
        return _resp([])

    monkeypatch.setattr(httpx, "get", record)

    github.commit_history()
    assert captured["sha"] == "main", "must default to AIOPS_GITHUB_REF, not GitHub's default"

    github.commit_history(ref="feature/x")
    assert captured["sha"] == "feature/x"
