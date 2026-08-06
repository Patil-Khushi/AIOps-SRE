"""Tests for deployment and configuration change context (Phase 8).

Two properties carry most of the weight.

**No inferred causality.** Most outages follow a change, which makes the
temptation to editorialise strong — so the model has nowhere to record blame and
the collector sorts chronologically rather than by suspicion. Tests assert both,
because a collector that ranks changes by likely culprit has made the RCA agent's
decision invisibly.

**Attribution honesty.** ``git`` yields a locally configured author name, not a
platform account. ``author_username`` must stay ``None`` unless resolved from an
API, since attributing a change to the wrong person during an incident is a real
harm rather than a cosmetic one.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from aiops.tools.change_context import (
    ChangeContext,
    ChangeRecord,
    ChangeType,
    ProviderStatus,
    RollbackStatus,
    collect_change_context,
)
from aiops.tools.change_context.providers import (
    ArgoCDChangeProvider,
    GitHubChangeProvider,
    GitLabChangeProvider,
    JenkinsChangeProvider,
)

_T0 = datetime(2026, 7, 31, 10, 0, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("AIOPS_CHANGE_CONTEXT_PROVIDERS", raising=False)
    yield


# ─── no inferred causality ───────────────────────────────────────────────────


def test_record_has_no_field_for_blame():
    """Structural guarantee: there is nowhere to assert a change caused the
    incident, so the collector cannot drift into inference."""
    fields = set(ChangeRecord.model_fields)
    for forbidden in (
        "caused_incident",
        "is_culprit",
        "suspicion_score",
        "likely_cause",
        "root_cause",
        "blame",
    ):
        assert forbidden not in fields, f"{forbidden} would make this an inference engine"


def test_context_has_no_ranking_field():
    fields = set(ChangeContext.model_fields)
    for forbidden in ("ranked_changes", "most_likely_change", "suspects"):
        assert forbidden not in fields


def test_records_are_sorted_chronologically_not_by_suspicion():
    """Time is a fact; relevance is a judgement. Ordering must reflect the former."""
    ctx = collect_change_context("checkout", _T0 - timedelta(days=30), _T0)
    stamps = [r.timestamp for r in ctx.records if r.timestamp]
    assert stamps == sorted(stamps)


# ─── attribution honesty ─────────────────────────────────────────────────────


def test_git_commits_do_not_populate_author_username():
    """``%an`` is a git config string. Presenting it as a platform account would be
    a false attribution, so the field stays empty until an API resolves it."""
    provider = GitHubChangeProvider()
    records = provider._git_commits(_T0 - timedelta(days=365), datetime.now(UTC))
    assert records, "this repo should have commits in the last year"
    for r in records:
        assert r.author, "git author name should be captured"
        assert r.author_username is None, "must not be inferred from the git name"


def test_git_commits_capture_sha_and_email():
    records = GitHubChangeProvider()._git_commits(_T0 - timedelta(days=365), datetime.now(UTC))
    r = records[0]
    assert r.commit_sha and len(r.commit_sha) >= 12
    assert r.author_email
    assert r.change_type is ChangeType.COMMIT


def test_github_reports_when_api_enrichment_is_off(monkeypatch):
    """A caller must know ``author_username`` is absent by configuration rather
    than because the commit had no author."""
    monkeypatch.setenv("GITHUB_PAT", "x" * 40)
    monkeypatch.setenv("GITHUB_OWNER", "someone")
    monkeypatch.setenv("GITHUB_REPO", "somerepo")
    monkeypatch.delenv("AIOPS_CHANGE_CONTEXT_GITHUB_API", raising=False)

    result = GitHubChangeProvider().collect(
        "checkout", _T0 - timedelta(days=365), datetime.now(UTC)
    )
    assert result.note and "API enrichment disabled" in result.note


def test_github_never_puts_the_token_in_output(monkeypatch):
    """A leaked credential in a note or error would end up in a decision trace and
    then in a ticket."""
    secret = "ghp_" + "s" * 36
    monkeypatch.setenv("GITHUB_PAT", secret)
    monkeypatch.setenv("GITHUB_OWNER", "someone")

    result = GitHubChangeProvider().collect(
        "checkout", _T0 - timedelta(days=365), datetime.now(UTC)
    )
    blob = f"{result.note}|{result.error}|{[r.raw_detail for r in result.records]}"
    assert secret not in blob


# ─── rollback status ─────────────────────────────────────────────────────────


def test_rollback_status_defaults_to_unknown_not_none():
    """``NONE`` asserts a change is still live. A provider that cannot see rollback
    state must not make that claim."""
    r = ChangeRecord(change_id="x", change_type=ChangeType.COMMIT, source="github")
    assert r.rollback_status is RollbackStatus.UNKNOWN


def test_rollback_status_has_an_explicit_unknown_member():
    assert RollbackStatus.UNKNOWN.value == "unknown"
    assert {s.value for s in RollbackStatus} >= {"none", "in_progress", "rolled_back", "unknown"}


# ─── absent platforms report unavailable, never empty ────────────────────────


@pytest.mark.parametrize(
    ("provider", "env"),
    [
        (GitLabChangeProvider(), "AIOPS_GITLAB_URL"),
        (ArgoCDChangeProvider(), "AIOPS_ARGOCD_URL"),
        (JenkinsChangeProvider(), "AIOPS_JENKINS_URL"),
    ],
)
def test_absent_platform_is_unavailable_never_empty(provider, env, monkeypatch):
    """ "No GitLab deployment happened" and "there is no GitLab" are opposite
    conclusions during an incident."""
    monkeypatch.delenv(env, raising=False)
    result = provider.collect("checkout", _T0, _T0 + timedelta(hours=1))

    assert result.status is ProviderStatus.UNAVAILABLE
    assert result.status is not ProviderStatus.EMPTY
    assert env in (result.note or "")

    healthy, detail = provider.health()
    assert healthy is False
    assert env in detail


# ─── union semantics ─────────────────────────────────────────────────────────


def test_collector_is_a_union_not_a_first_wins_chain(monkeypatch):
    """A commit and a flag flip are both true; stopping at the first answer would
    discard most of the change picture."""
    from aiops.tools.change_context import collector

    class _A:
        name = "a"
        source = "a"

        def health(self):
            return True, "ok"

        def collect(self, service, start, end):
            from aiops.tools.change_context.base import ChangeContextResult

            return ChangeContextResult(
                provider="a",
                status=ProviderStatus.COLLECTED,
                records=[
                    ChangeRecord(
                        change_id="a1", change_type=ChangeType.COMMIT, source="a", timestamp=_T0
                    )
                ],
            )

    class _B(_A):
        name = "b"
        source = "b"

        def collect(self, service, start, end):
            from aiops.tools.change_context.base import ChangeContextResult

            return ChangeContextResult(
                provider="b",
                status=ProviderStatus.COLLECTED,
                records=[
                    ChangeRecord(
                        change_id="b1",
                        change_type=ChangeType.FEATURE_FLAG,
                        source="b",
                        timestamp=_T0 + timedelta(minutes=1),
                    )
                ],
            )

    collector.register_provider(_A())
    collector.register_provider(_B())
    monkeypatch.setenv("AIOPS_CHANGE_CONTEXT_PROVIDERS", "a,b")

    ctx = collect_change_context("checkout", _T0 - timedelta(hours=1), _T0 + timedelta(hours=1))
    assert {r.change_id for r in ctx.records} == {"a1", "b1"}, "both providers must contribute"
    assert ctx.sources_collected == ["a", "b"]


def test_unavailable_sources_are_named(monkeypatch):
    """An empty record list means "nothing changed" only if every source answered."""
    monkeypatch.setenv("AIOPS_CHANGE_CONTEXT_PROVIDERS", "gitlab,argocd")
    monkeypatch.delenv("AIOPS_GITLAB_URL", raising=False)
    monkeypatch.delenv("AIOPS_ARGOCD_URL", raising=False)

    ctx = collect_change_context("checkout", _T0, _T0 + timedelta(hours=1))
    assert ctx.records == []
    assert set(ctx.sources_unavailable) == {"gitlab", "argocd"}
    assert ctx.complete is False, "an incomplete picture must not look complete"


def test_complete_is_true_only_when_nothing_is_missing(monkeypatch):
    from aiops.tools.change_context import collector
    from aiops.tools.change_context.base import ChangeContextResult

    class _Ok:
        name = "ok"
        source = "ok"

        def health(self):
            return True, "ok"

        def collect(self, service, start, end):
            return ChangeContextResult(
                provider="ok",
                status=ProviderStatus.COLLECTED,
                records=[
                    ChangeRecord(
                        change_id="1", change_type=ChangeType.COMMIT, source="ok", timestamp=_T0
                    )
                ],
            )

    collector.register_provider(_Ok())
    monkeypatch.setenv("AIOPS_CHANGE_CONTEXT_PROVIDERS", "ok")

    ctx = collect_change_context("checkout", _T0 - timedelta(hours=1), _T0 + timedelta(hours=1))
    assert ctx.complete is True


def test_provider_exception_is_contained(monkeypatch):
    from aiops.tools.change_context import collector

    class _Boom:
        name = "boom"
        source = "boom"

        def health(self):
            return True, "ok"

        def collect(self, service, start, end):
            raise RuntimeError("exploded")

    collector.register_provider(_Boom())
    monkeypatch.setenv("AIOPS_CHANGE_CONTEXT_PROVIDERS", "boom")

    ctx = collect_change_context("checkout", _T0, _T0 + timedelta(hours=1))
    assert "boom" in ctx.sources_unavailable
    assert "RuntimeError" in (ctx.coverage_note or "")


def test_unknown_provider_name_is_skipped(monkeypatch):
    monkeypatch.setenv("AIOPS_CHANGE_CONTEXT_PROVIDERS", "nonexistent")
    ctx = collect_change_context("checkout", _T0, _T0 + timedelta(hours=1))
    assert ctx.records == []


def test_all_unknown_chain_is_not_reported_as_complete(monkeypatch):
    """A typo'd chain must not read as "checked everything, nothing changed".

    Every configured name being unrecognised used to leave ``attempts`` empty, so
    ``sources_unavailable`` was empty and ``complete`` came back True — an
    authoritative all-clear from a collector that asked nobody. That is the exact
    false-completeness this module's docstrings warn against, so it is asserted
    rather than left to a log warning nobody reads.
    """
    monkeypatch.setenv("AIOPS_CHANGE_CONTEXT_PROVIDERS", "githbu,kubernets")
    ctx = collect_change_context("checkout", _T0, _T0 + timedelta(hours=1))

    assert ctx.records == []
    assert ctx.complete is False, "a chain that resolved no providers is not complete"
    assert sorted(ctx.sources_unavailable) == ["githbu", "kubernets"]
    assert ctx.coverage_note is not None
    assert "unknown provider name" in ctx.coverage_note


def test_default_chain_is_the_three_real_sources():
    from aiops.tools.change_context import collector

    assert collector._chain() == (["github", "feature_flags", "kubernetes"], [])


# ─── model contract ──────────────────────────────────────────────────────────


def test_records_are_immutable():
    r = ChangeRecord(change_id="x", change_type=ChangeType.COMMIT, source="github")
    with pytest.raises(Exception):
        r.author = "someone else"


def test_all_required_evidence_fields_exist():
    """The brief's list: deployment id, commit, author, timestamp, rollback status,
    feature flags, configuration version."""
    fields = set(ChangeRecord.model_fields)
    for required in (
        "deployment_id",
        "commit_sha",
        "author",
        "timestamp",
        "rollback_status",
        "feature_flags",
        "configuration_version",
    ):
        assert required in fields


def test_context_is_json_serializable():
    ctx = collect_change_context("checkout", _T0 - timedelta(days=7), datetime.now(UTC))
    dumped = ctx.model_dump(mode="json")
    assert "records" in dumped
    assert "sources_unavailable" in dumped


# ─── agent integration ───────────────────────────────────────────────────────


def test_collection_is_opt_in(monkeypatch):
    """Disabled means ``None`` — not attempted — distinct from an empty record list
    meaning nothing changed.

    Asserts the parsing rule with the variable explicitly absent, not the ambient
    value of ``_CHANGE_CONTEXT_ENABLED``. That constant is evaluated at import, so
    a developer whose ``.env`` sets ``AIOPS_CHANGE_CONTEXT`` would fail this test
    for a reason unrelated to the code, and no fixture could undo it — the import
    has already happened by the time one runs.
    """
    from agents.log_correlation import agent as lc_agent

    monkeypatch.delenv("AIOPS_CHANGE_CONTEXT", raising=False)
    assert lc_agent._flag_enabled("AIOPS_CHANGE_CONTEXT") is False, "must default to off"

    monkeypatch.setenv("AIOPS_CHANGE_CONTEXT", "true")
    assert lc_agent._flag_enabled("AIOPS_CHANGE_CONTEXT") is True, "opt-in must be honoured"


def test_existing_outputs_unchanged_when_enabled(monkeypatch):
    from agents.log_correlation import CorrelationInput
    from agents.log_correlation import agent as lc_agent
    from agents.log_correlation.agent import correlate

    monkeypatch.setattr(lc_agent, "_CHANGE_CONTEXT_ENABLED", True)
    end = datetime.now(UTC)
    r = correlate(
        CorrelationInput(
            service="checkout",
            window={"start": (end - timedelta(minutes=15)).isoformat(), "end": end.isoformat()},
        ),
        force_synthetic=True,
    )

    assert r.confidence == 0.9, "the eval-asserted score must not move"
    assert r.suspected_dependencies == ["payment"]
    assert len(r.timeline) == 3
    assert r.evidence


def test_collection_failure_does_not_lose_the_verdict(monkeypatch):
    from agents.log_correlation import CorrelationInput
    from agents.log_correlation import agent as lc_agent
    from agents.log_correlation.agent import correlate

    def _boom(*_a, **_kw):
        raise RuntimeError("collector exploded")

    monkeypatch.setattr(lc_agent, "_CHANGE_CONTEXT_ENABLED", True)
    monkeypatch.setattr(lc_agent, "collect_change_context", _boom)

    end = datetime.now(UTC)
    r = correlate(
        CorrelationInput(
            service="checkout",
            window={"start": (end - timedelta(minutes=15)).isoformat(), "end": end.isoformat()},
        ),
        force_synthetic=True,
    )

    assert r.deployment_context is None
    assert r.suspected_dependencies == ["payment"], "verdict survives"
    assert any("change context: collection failed" in t for t in r.audit_metadata.decision_trace)


def test_empty_parsed_chain_is_not_reported_as_complete(monkeypatch):
    """A chain that parses to nothing is the same false-completeness, other route.

    ``","`` is non-empty so the default chain is not substituted, yet every element
    is dropped as blank — leaving zero attempts, zero unavailable sources and so
    ``complete=True`` from a collector that asked nobody. Distinct from the
    all-unknown case: there the names exist and are unrecognised; here there are no
    names at all.
    """
    monkeypatch.setenv("AIOPS_CHANGE_CONTEXT_PROVIDERS", " , , ")
    ctx = collect_change_context("checkout", _T0, _T0 + timedelta(hours=1))

    assert ctx.records == []
    assert ctx.complete is False, "a chain that resolved no providers is not complete"
    assert ctx.coverage_note is not None
    assert "empty chain" in ctx.coverage_note
