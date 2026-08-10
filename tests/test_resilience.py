"""Tests for the shared resilience middleware.

This exists to close two production blockers found in the Phase 9 audit: no retry
policy anywhere, and uneven protection across the three provider seams. The tests
that matter most are the ones proving it is a real fix rather than a wrapper:

- **Retry happens before the breaker trips.** The original arrangement broke on the
  first failure, so one dropped packet disabled a tier for 30 seconds.
- **The timeout is enforced, not advertised.** Passing a deadline to a provider
  that ignores it is not a timeout — a topology tier once burned a whole 3-second
  budget on a single call that way.
- **Failures are never cached.** Caching an error serves a transient blip for the
  full TTL, converting a blip into an outage.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from aiops.tools import resilience
from aiops.tools.resilience import ResiliencePolicy, guard


@pytest.fixture(autouse=True)
def _reset():
    resilience.reset_for_tests()
    yield
    resilience.reset_for_tests()


_FAST = ResiliencePolicy(timeout=2.0, retries=2, backoff=0.001, breaker_seconds=30)


# ─── retry before breaking ───────────────────────────────────────────────────


def test_transient_failure_is_retried_then_succeeds():
    """The core fix: a blip must not cost the call."""
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        return "fail" if calls["n"] < 3 else "ok"

    out = guard("t1", flaky, policy=_FAST, is_transient=lambda v: v == "fail")

    assert out.ok is True
    assert out.value == "ok"
    assert out.attempts == 3


def test_breaker_does_not_trip_while_retries_remain():
    """A single failure must not disable the tier — that over-reaction is exactly
    what the old breaker-without-retry arrangement did."""
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        return "fail" if calls["n"] == 1 else "ok"

    guard("t2", flaky, policy=_FAST, is_transient=lambda v: v == "fail")
    assert resilience.breaker_open("t2") is False


def test_breaker_trips_only_after_retries_are_exhausted():
    out = guard("t3", lambda: "fail", policy=_FAST, is_transient=lambda v: v == "fail")

    assert out.ok is False
    assert out.attempts == 3, "1 initial + 2 retries"
    assert resilience.breaker_open("t3") is True


def test_open_breaker_skips_the_call_entirely():
    guard("t4", lambda: "fail", policy=_FAST, is_transient=lambda v: v == "fail")

    calls = {"n": 0}

    def counted():
        calls["n"] += 1
        return "ok"

    out = guard("t4", counted, policy=_FAST)
    assert calls["n"] == 0, "an open breaker must not reach the provider"
    assert out.breaker_open is True
    assert out.ok is False


def test_retries_are_disabled_when_configured_to_zero():
    policy = ResiliencePolicy(timeout=1, retries=0, backoff=0.001)
    out = guard("t5", lambda: "fail", policy=policy, is_transient=lambda v: v == "fail")
    assert out.attempts == 1


def test_non_transient_result_is_not_retried():
    """Retrying "not configured" or "found nothing" is pure latency — the answer
    will not change."""
    calls = {"n": 0}

    def once():
        calls["n"] += 1
        return "unavailable"

    out = guard("t6", once, policy=_FAST, is_transient=lambda v: v == "fail")
    assert calls["n"] == 1
    assert out.ok is True


def test_raised_exception_is_retried_and_contained():
    calls = {"n": 0}

    def boom():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("transient")
        return "ok"

    out = guard("t7", boom, policy=_FAST)
    assert out.ok is True and out.value == "ok" and calls["n"] == 3


def test_persistent_exception_never_escapes():
    """Every caller is an enrichment path; a raise here would cost a verdict."""
    out = guard("t8", lambda: (_ for _ in ()).throw(RuntimeError("always")), policy=_FAST)
    assert out.ok is False
    assert "RuntimeError" in (out.error or "")


# ─── enforced timeout ────────────────────────────────────────────────────────


def test_timeout_is_enforced_even_when_the_callee_ignores_it():
    """A provider that never checks a deadline must still be bounded — this is the
    bug class where one slow tier consumed an entire budget."""
    policy = ResiliencePolicy(timeout=0.2, retries=0, backoff=0.001)
    started = time.monotonic()
    out = guard("t9", lambda: time.sleep(5) or "never", policy=policy)
    elapsed = time.monotonic() - started

    assert out.timed_out is True
    assert out.ok is False
    assert elapsed < 2.0, f"caller was not released promptly ({elapsed:.1f}s)"


def test_timeout_counts_as_a_transient_failure_and_is_retried():
    policy = ResiliencePolicy(timeout=0.1, retries=1, backoff=0.001)
    out = guard("t10", lambda: time.sleep(5) or "never", policy=policy)
    assert out.attempts == 2
    assert resilience.breaker_open("t10") is True


# ─── caching ─────────────────────────────────────────────────────────────────


def test_successful_result_is_cached():
    calls = {"n": 0}

    def counted():
        calls["n"] += 1
        return "value"

    guard("t11", counted, policy=_FAST, cache_key="k1")
    out = guard("t11", counted, policy=_FAST, cache_key="k1")

    assert calls["n"] == 1
    assert out.from_cache is True
    assert out.value == "value"


def test_failures_are_never_cached():
    """Caching an error would serve a transient blip for the whole TTL."""
    out = guard(
        "t12",
        lambda: "fail",
        policy=_FAST,
        cache_key="k2",
        is_transient=lambda v: v == "fail",
        is_cacheable=lambda v: v != "fail",
    )
    assert out.ok is False
    hit, _ = resilience.cache_get("k2")
    assert hit is False


def test_empty_results_use_a_shorter_ttl():
    """An empty answer is likelier to change soon than a positive one, so it is
    re-checked sooner without being treated as a failure."""
    policy = ResiliencePolicy(timeout=1, retries=0, cache_ttl=100, cache_empty_ttl=0.05)
    guard("t13", lambda: "empty", policy=policy, cache_key="k3", is_empty=lambda v: v == "empty")

    assert resilience.cache_get("k3")[0] is True
    time.sleep(0.1)
    assert resilience.cache_get("k3")[0] is False, "empty entry should have expired"


def test_cache_distinguishes_a_stored_none_from_a_miss():
    """Returning a bare value would make a legitimately cached ``None`` look like a
    cache miss and re-query forever."""
    resilience.cache_put("k4", None, 10)
    hit, value = resilience.cache_get("k4")
    assert hit is True and value is None


def test_zero_ttl_disables_caching():
    resilience.cache_put("k5", "v", 0)
    assert resilience.cache_get("k5")[0] is False


# ─── observability ───────────────────────────────────────────────────────────


def test_counters_make_the_protections_visible():
    """Without counters nobody can tell whether a breaker is saving the system or
    hiding a backend that has been down for a week."""
    guard("t14", lambda: "fail", policy=_FAST, is_transient=lambda v: v == "fail")
    s = resilience.stats()["t14"]

    assert s["calls"] == 1
    assert s["retries"] == 2
    assert s["breaks"] == 1


# ─── thread safety ───────────────────────────────────────────────────────────


def test_concurrent_guards_do_not_corrupt_shared_state():
    """Breakers, cache and counters are process-global; the original seams mutated
    equivalents without a lock."""
    errors: list[str] = []

    def worker(i: int):
        try:
            guard(f"tc{i % 4}", lambda: "ok", policy=_FAST, cache_key=f"ck{i % 4}")
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(40)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert sum(v.get("calls", 0) for v in resilience.stats().values()) == 40


# ─── seam integration ────────────────────────────────────────────────────────


def test_incident_history_delegates_its_breaker_to_the_middleware():
    """Two breakers for one provider would disagree the first time one tripped."""
    from aiops.tools.incident_history import retriever

    assert retriever._breaker_open("mock") is False
    resilience.trip_breaker("incident_history.mock", 30)
    assert retriever._breaker_open("mock") is True


def test_all_three_seams_now_have_the_full_protection_set():
    """The Phase 9 finding was an uneven table, not one missing feature. This
    asserts the two gap seams route through the middleware that supplies all four.
    """
    from aiops.tools.change_context import collector
    from aiops.tools.incident_history import retriever

    assert hasattr(retriever, "_POLICY")
    assert hasattr(collector, "_POLICY")
    for pol in (retriever._POLICY, collector._POLICY):
        assert pol.timeout > 0, "timeout"
        assert pol.retries >= 1, "retry"
        assert pol.breaker_seconds > 0, "breaker"
        assert pol.cache_ttl > 0, "cache"


def test_reset_clears_breakers_cache_and_counters():
    guard("t15", lambda: "fail", policy=_FAST, is_transient=lambda v: v == "fail")
    resilience.cache_put("k6", "v", 60)

    resilience.reset_for_tests()

    assert resilience.breaker_open("t15") is False
    assert resilience.cache_get("k6")[0] is False
    assert resilience.stats() == {}


# ─── pool starvation vs provider timeout (PR #235 review, non-blocking) ───────


def test_starved_call_does_not_trip_the_providers_breaker(monkeypatch):
    """A saturated pool must not be reported as the waiting provider's failure.

    The executor is shared by every guarded seam and a hung provider holds its
    thread until it returns, so one wedged backend can fill the pool. A queued call
    that never started was counted as *its own* provider timing out, which tripped
    that provider's breaker and hid a perfectly healthy backend for 30s. The cause
    lived in a different seam entirely, which makes it a genuinely hard bug to read
    from the outside — hence the assertion.
    """
    released = threading.Event()

    def _hog():
        released.wait(timeout=5)
        return "done"

    # One worker, already occupied: anything else submitted can only queue.
    monkeypatch.setattr(resilience, "_executor", ThreadPoolExecutor(max_workers=1))
    monkeypatch.setattr(resilience, "_MAX_WORKERS", 1)

    hog = threading.Thread(target=lambda: guard("hog", _hog, policy=_FAST))
    hog.start()
    try:
        # Give the hog its slot before the victim competes for one.
        time.sleep(0.05)
        out = guard("victim", lambda: "never runs", policy=_FAST)

        assert out.ok is False
        assert out.starved is True, "a queued call must be reported as starved"
        assert out.timed_out is False, "the provider was never called, so it did not time out"
        assert resilience.breaker_open("victim") is False, (
            "one seam's hung backend must not disable another seam's provider"
        )
        assert "never called" in " ".join(out.notes)
    finally:
        released.set()
        hog.join(timeout=5)


def test_timeout_then_starved_retry_still_trips_the_breaker(monkeypatch):
    """A provider that was called and hung must not be exonerated by a starved retry.

    The starvation branch is self-reinforcing without this: attempt 1 reaches the
    provider and hangs, its thread keeps the only worker, so attempt 2 is starved —
    by that same provider. Reporting starvation-only overwrote the real timeout,
    claimed in the notes that "the provider was never called", and left the breaker
    closed on a backend that is demonstrably broken. Exactly inverting the bug the
    starvation handling was added to fix.
    """
    released = threading.Event()

    def _hang():
        released.wait(timeout=5)
        return "eventually"

    monkeypatch.setattr(resilience, "_executor", ThreadPoolExecutor(max_workers=1))
    monkeypatch.setattr(resilience, "_MAX_WORKERS", 1)

    try:
        out = guard("hangs", _hang, policy=ResiliencePolicy(timeout=0.2, retries=2, backoff=0.0))

        assert out.ok is False
        assert out.timed_out is True, "the provider was called and did not answer"
        assert out.starved is False, "starvation must not mask a real timeout"
        assert "timed out" in (out.error or ""), "the real failure must survive"
        assert resilience.breaker_open("hangs") is True, "a hung provider must be broken open"
        notes = " ".join(out.notes)
        assert "never called" not in notes, "it was called — saying otherwise is false"
        assert "retry starved" in notes, "the starved retry is still worth recording"
    finally:
        released.set()


def test_starved_outcome_does_not_count_the_uncalled_attempt(monkeypatch):
    """``attempts`` must reflect calls actually made, since it is public and a
    consumer reporting "N attempts failed" would otherwise overstate contact."""
    released = threading.Event()

    def _hog():
        released.wait(timeout=5)
        return "done"

    monkeypatch.setattr(resilience, "_executor", ThreadPoolExecutor(max_workers=1))
    monkeypatch.setattr(resilience, "_MAX_WORKERS", 1)

    hog = threading.Thread(target=lambda: guard("hog2", _hog, policy=_FAST))
    hog.start()
    try:
        time.sleep(0.05)
        out = guard("victim2", lambda: "never runs", policy=_FAST)

        assert out.starved is True
        assert out.attempts == 0, "no attempt reached the provider"
    finally:
        released.set()
        hog.join(timeout=5)
