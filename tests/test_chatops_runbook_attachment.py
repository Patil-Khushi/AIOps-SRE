"""Tests for runbook resolution behind the chatops attachment seam.

The on-call engineer's ``Runbook:`` line inherits a CMDB reference that is
often a placeholder URL resolving nowhere. ``resolve_runbook`` turns
whatever the verdict carries into the library's actual markdown so
file-capable adapters can deliver the procedure itself.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aiops.tools.chatops.runbook_attachment import (
    _candidate_id,
    _reset_link_cache_for_tests,
    resolve_runbook,
)

_RUNBOOK = """---
id: rb-ad-failure
title: Ad service — 5xx errors
service: ad
version: 1
tags:
- error-rate
severity: Sev-2
source: seed
status: published
---

## Symptoms
- `AdErrorRateHigh` alert firing.

## Resolution steps
1. Flip `adFailure` to off.
"""

_OTHER = """---
id: rb-payment-failure
title: Payment service — gateway 5xx
service: payment
version: 1
status: published
---

## Resolution steps
1. Check the gateway.
"""


@pytest.fixture(autouse=True)
def _fresh_link_cache() -> None:
    # The link map is memoized per process, so without this a test writing
    # its own fixture map would see whichever map loaded first.
    _reset_link_cache_for_tests()
    yield
    _reset_link_cache_for_tests()


@pytest.fixture
def library(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "rb-ad-failure.md").write_text(_RUNBOOK, encoding="utf-8")
    (tmp_path / "rb-payment-failure.md").write_text(_OTHER, encoding="utf-8")
    monkeypatch.setenv("AIOPS_RUNBOOKS_DIR", str(tmp_path))
    # Point the link map at a path that does not exist unless a test writes
    # one, so resolution never picks up the repo's real published links.
    monkeypatch.setenv("AIOPS_RUNBOOK_LINKS_PATH", str(tmp_path / "links.json"))
    return tmp_path


# ─── id extraction ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        ("rb-ad-failure", "rb-ad-failure"),
        ("rb-product-catalog-latency", "rb-product-catalog-latency"),
        ("https://runbooks.example.com/rb-ad-failure", "rb-ad-failure"),
        ("https://runbooks.example.com/rb-ad-failure.md", "rb-ad-failure"),
        ("data/runbooks/rb-ad-failure.md", "rb-ad-failure"),
        ("RB-AD-FAILURE", "rb-ad-failure"),
        ("https://runbooks.example.com/rb-ad-failure?v=2", "rb-ad-failure"),
        # Not runbook ids — the placeholder-URL case the CMDB actually emits.
        ("https://runbooks.example.com/frontend", None),
        ("payment-cpu", None),
        ("", None),
    ],
)
def test_candidate_id_extraction(ref: str, expected: str | None) -> None:
    assert _candidate_id(ref) == expected


def test_candidate_id_rejects_path_traversal() -> None:
    # A crafted ref must never escape the library directory.
    assert _candidate_id("../../etc/passwd") is None
    assert _candidate_id("https://evil.example/../../rb-x/../../secret") is None


# ─── resolution ────────────────────────────────────────────────────────


def test_resolves_by_explicit_id(library: Path) -> None:
    rb = resolve_runbook(service="anything-else", runbook_ref="rb-ad-failure")
    assert rb is not None
    assert rb.runbook_id == "rb-ad-failure"
    assert rb.filename == "rb-ad-failure.md"
    assert "AdErrorRateHigh" in rb.markdown
    assert rb.title == "Ad service — 5xx errors"


def test_placeholder_url_falls_back_to_service_match(library: Path) -> None:
    # The real-world case: CMDB hands RA-005 a dead URL whose last segment
    # is a service name, not a runbook id. The service must rescue it.
    rb = resolve_runbook(service="ad", runbook_ref="https://runbooks.example.com/ad")
    assert rb is not None
    assert rb.runbook_id == "rb-ad-failure"


def test_service_match_tolerates_naming_variants(library: Path) -> None:
    for spelling in ("ad", "adservice", "ad-service", "AD"):
        rb = resolve_runbook(service=spelling, runbook_ref=None)
        assert rb is not None, spelling
        assert rb.runbook_id == "rb-ad-failure"


def test_explicit_id_wins_over_service(library: Path) -> None:
    rb = resolve_runbook(service="ad", runbook_ref="rb-payment-failure")
    assert rb is not None
    assert rb.runbook_id == "rb-payment-failure"


def test_unknown_service_and_ref_returns_none(library: Path) -> None:
    assert resolve_runbook(service="weather-forecast", runbook_ref=None) is None
    assert resolve_runbook(service=None, runbook_ref=None) is None


def test_missing_library_dir_degrades_to_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A notification must never be lost to an attachment problem.
    monkeypatch.setenv("AIOPS_RUNBOOKS_DIR", str(tmp_path / "does-not-exist"))
    assert resolve_runbook(service="ad", runbook_ref="rb-ad-failure") is None


# ─── published share links ─────────────────────────────────────────────


def test_published_link_is_attached(library: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    links = library / "links.json"
    links.write_text(
        json.dumps(
            {
                "rb-ad-failure": {
                    "filename": "rb-ad-failure.md",
                    "url": "https://tenant.sharepoint.com/:t:/p/x/abc",
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AIOPS_RUNBOOK_LINKS_PATH", str(links))
    _reset_link_cache_for_tests()

    rb = resolve_runbook(service="ad", runbook_ref=None)
    assert rb is not None
    assert rb.url == "https://tenant.sharepoint.com/:t:/p/x/abc"
    assert rb.filename == "rb-ad-failure.md"


def test_unpublished_runbook_has_no_url(library: Path) -> None:
    # Library hit, but nothing published: adapters must render no button
    # rather than a dead one.
    rb = resolve_runbook(service="ad", runbook_ref=None)
    assert rb is not None
    assert rb.url is None
    assert rb.filename == "rb-ad-failure.md"


def test_malformed_link_map_degrades_to_no_url(
    library: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    links = library / "links.json"
    links.write_text("{not valid json", encoding="utf-8")
    monkeypatch.setenv("AIOPS_RUNBOOK_LINKS_PATH", str(links))
    _reset_link_cache_for_tests()

    rb = resolve_runbook(service="ad", runbook_ref=None)
    assert rb is not None
    assert rb.url is None


def test_unparseable_file_does_not_raise(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "rb-broken.md").write_text("not: [valid: frontmatter", encoding="utf-8")
    monkeypatch.setenv("AIOPS_RUNBOOKS_DIR", str(tmp_path))
    assert resolve_runbook(service="ad", runbook_ref="rb-broken") is None
