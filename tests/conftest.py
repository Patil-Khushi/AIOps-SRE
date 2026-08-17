"""Shared test fixtures.

The autouse fixture here exists to fix #113 — the full-suite pytest hang.

Earlier tests in the run (notably the HITL UI / approval-flow suites that
boot FastAPI via ``TestClient``) call ``install_default_approver()`` as
part of the app's startup/lifespan hooks. That swaps the gate's approver
from the fail-closed ``_no_approver`` default to a real ``ApprovalRequester``
that waits up to ``AIOPS_HITL_APPROVAL_TIMEOUT`` (600s default) for a
human decision. If the global gate state isn't restored, later tests that
rely on the fail-closed default — `test_hitl_enforcement` and a smoke
test in `test_smoke.py` are the two known cases — block for the full
600s budget instead of failing immediately, stalling the whole suite.

Resetting the gate to ``_no_approver`` at both ends of every test makes
the gate's approver hermetic without forcing every test to write its
own setup/teardown.  Unconditional reset (rather than snapshot/restore)
keeps the fixture simple and side-steps the ordering hazard of trying
to capture and replay whatever the previous test left behind.
"""

from __future__ import annotations

import os

import pytest

# Pin the observability backends to a fast-refusing endpoint before any module
# that snapshots their URL/timeout at import time is loaded.
#
# ``aiops.tools.observability.prometheus`` / ``...jaeger`` read
# ``AIOPS_PROMETHEUS_URL`` / ``AIOPS_JAEGER_URL`` (and their timeouts) into
# module-level constants at import, so a fixture set later cannot redirect them.
# ``alert_triage.triage`` calls both providers for real during
# ``_fetch_metric_context`` / ``_fetch_trace_context``; on a dev box where those
# ports are firewalled (connections hang rather than refuse), the eval-harness
# test that walks every agent stacks the round-trips past the 60s pytest-timeout
# and the thread-method hard-kills the process. Pointing every test at
# ``127.0.0.1:1`` (refused instantly) with a sub-second timeout makes the calls
# fail fast and lets the agent's graceful-degradation path run. ``setdefault``
# so ``@pytest.mark.integration`` runs against a real cluster — which export
# these vars — are never overridden. Runs at conftest import, ahead of the
# jaeger import below, so the module-level snapshots pick them up.
os.environ.setdefault("AIOPS_PROMETHEUS_URL", "http://127.0.0.1:1")
os.environ.setdefault("AIOPS_PROMETHEUS_TIMEOUT", "0.25")
os.environ.setdefault("AIOPS_JAEGER_URL", "http://127.0.0.1:1")
os.environ.setdefault("AIOPS_JAEGER_TIMEOUT", "0.25")
os.environ.setdefault("AIOPS_JAEGER_CONNECT_TIMEOUT", "0.25")
os.environ.setdefault("AIOPS_LOKI_URL", "http://127.0.0.1:1")
os.environ.setdefault("AIOPS_LOKI_TIMEOUT", "0.25")
os.environ.setdefault("AIOPS_LOKI_CONNECT_TIMEOUT", "0.25")

# Disable embeddings in the test suite by default (#113).
#
# Several agents (``alert_triage`` — for both its dedup and classification
# steps — and ``knowledge_synthesizer``) lazily load an 80MB
# ``sentence-transformers`` model the first time a method
# that needs embeddings runs. The load is a hefty HTTPS download on
# cold cache and a multi-second mmap on warm cache, blowing past the
# 60s pytest-timeout in either case. The eval harness in particular
# walks every agent and would otherwise pay the load cost per agent.
#
# Each agent already has a documented fallback (rule-based dedup /
# classification) when ``_get_embed_model()`` returns ``None``. We
# pin that fallback by replacing the ``_get_embed_model`` function
# on each agent module at conftest load time so it unconditionally
# returns ``None`` — the package may be installed (test env has the
# embeddings extra) but each agent treats ``None`` as "unavailable"
# and never tries to load. We override the function rather than the
# ``_EMBED_MODEL`` cache sentinel because ``reset_state()`` paths in
# the classification step reset ``_EMBED_MODEL = None`` between cases and
# would otherwise re-trigger a load.
#
# Tests that specifically need to exercise the embeddings path
# monkeypatch ``_get_embed_model`` back to a fake (see
# ``test_alert_triage_embedding_persistence``); ``monkeypatch.setattr``
# undoes the override per-test without disturbing this default.
from agents.alert_triage import agent as _alert_triage_agent
from agents.alert_triage import classifier as _alert_triage_classifier
from agents.knowledge_synthesizer import agent as _knowledge_synthesizer_agent
from aiops.policy import get_gate
from aiops.tools import topology as _topology
from aiops.tools.observability import jaeger as _jaeger
from aiops.tools.observability import loki as _loki


def _no_embed_model() -> None:
    return None


# Alert Triage lazily loads an embedding model in two places — the dedup path
# (agent.py) and the classification path (classifier.py). Pin both to the
# rule-based / keyword fallback so the test suite never pays the model load.
_alert_triage_agent._get_embed_model = _no_embed_model
_alert_triage_classifier._get_embed_model = _no_embed_model
_knowledge_synthesizer_agent._get_embed_model = _no_embed_model


@pytest.fixture(autouse=True)
def _disable_auto_triage(monkeypatch):
    """Disable the auto-triage loop in tests (#130).

    ``demo/ui/server.py`` spawns an asyncio background task on startup
    that polls Prometheus every 3s and triages new alerts. In tests
    that boot FastAPI via ``TestClient``, the startup hook would
    otherwise spawn a real background task, generate test noise, and
    keep the loop alive for the full test duration. Tests that
    specifically exercise the loop instantiate ``_AutoTriageLoop``
    directly rather than going through the startup hook.
    """
    monkeypatch.setenv("AIOPS_AUTO_TRIAGE_ENABLED", "false")
    # Same hygiene for the SNOW resolved-ticket watcher (#PRS-007): don't let
    # the lifespan spawn a background poller during TestClient-based tests.
    monkeypatch.setenv("SNOW_WATCHER_ENABLED", "false")
    # Don't let the lifespan auto-seed the on-call roster into the hermetic
    # per-test DB — tests that exercise on-call seed their own rows, and the
    # triage-endpoint/orchestrator suites assert against the empty-roster
    # (mock-provider) path. Demo runs leave this unset (auto-seed on).
    monkeypatch.setenv("AIOPS_ONCALL_AUTOSEED", "false")


@pytest.fixture(autouse=True)
def _hermetic_llm_provider(monkeypatch):
    """Pin the LLM provider to the offline ``stub`` around every test (#151).

    ``demo/ui/server.py`` calls ``load_dotenv()`` at import and then
    ``os.environ.setdefault("AIOPS_LLM_PROVIDER", "stub")`` — but ``setdefault``
    cannot override a real provider/key that ``.env`` has already pushed into
    the *process-wide* environment. So once any ``TestClient(srv.app)`` test
    imports the server, ``.env`` leaks a real provider into ``os.environ`` for
    the rest of the session (``test_pagerduty_adapter`` documents the same
    ``load_dotenv`` re-population). A later agent test that doesn't pin the
    provider then makes a *real* LLM call, which hangs on a corporate
    TLS-inspecting proxy — the SDK's default timeout is far longer than the 60s
    pytest-timeout, so the whole suite stalls. That is the full-suite hang in
    #151 (a recurrence of the #113 class of bug).

    Forcing ``stub`` here makes the suite hermetic regardless of ``.env`` or
    test import order. The handful of tests that need a specific provider
    (``test_llm_ping``) override with their own ``monkeypatch.setenv`` inside the
    test body — that runs after this fixture and takes precedence, and is
    unwound afterwards.
    """
    monkeypatch.setenv("AIOPS_LLM_PROVIDER", "stub")


@pytest.fixture(autouse=True)
def _hermetic_state_db(monkeypatch, tmp_path_factory):
    """Give every test its own empty SQLite state DB (#151, mode A).

    ``aiops.state`` caches a process-wide engine, and tests that don't set
    their own ``AIOPS_STATE_DB_URL`` all fall back to the same default
    ``./data/state.db`` file. Clusters and verdicts written by one such test
    then accumulate and leak into the next, which is the order-dependent root
    of ``test_ema_keeps_centroid_anchored_to_origin`` — an embedding-dedup
    test that asserts an exact active-cluster count. Pointing each test at a
    fresh temp DB and rebuilding the engine + clearing the alert_triage dedup
    cache around it makes persisted state hermetic regardless of run order.

    Tests that set their own ``AIOPS_STATE_DB_URL`` (the alert_triage and
    state suites) request their fixtures after this autouse setup and override
    the URL; the duplicate engine resets are idempotent and harmless.
    """
    from agents.alert_triage.agent import reset_dedup_store
    from aiops.state import init_db, reset_engine_for_tests

    db_path = tmp_path_factory.mktemp("state") / "state.db"
    monkeypatch.setenv("AIOPS_STATE_DB_URL", f"sqlite:///{db_path.as_posix()}")
    reset_engine_for_tests()
    reset_dedup_store()
    init_db()
    try:
        yield
    finally:
        reset_engine_for_tests()
        reset_dedup_store()


@pytest.fixture(autouse=True)
def _hermetic_gate_approver():
    """Reset ``HITLGate._approver`` to the fail-closed default around every test.

    Tests that need a custom approver (e.g. the HITL approval flow suite)
    install one inside their own body; the FastAPI lifespan that fires
    inside ``TestClient(...)`` context managers similarly swaps in an
    ``ApprovalRequester``. Either way, this autouse fixture undoes any
    such change after the test exits so the next test starts from the
    same known-good ``_no_approver`` state.

    Resets at both ends (not just teardown) so a leak from a previous
    test that escaped its own cleanup can't taint the next test's setup.
    """
    gate = get_gate()
    gate.reset_approver()
    try:
        yield
    finally:
        gate.reset_approver()


@pytest.fixture(autouse=True)
def _hermetic_slack_user_map_env(monkeypatch):
    """Clear the Slack/on-call identity env overrides around every test (#174).

    ``demo/ui/server.py`` calls ``load_dotenv()`` at import, so once any test
    imports the server (the auto-triage / approval / triage-endpoint suites do)
    a developer's real ``.env`` pushes ``AIOPS_SLACK_USER_MAP_JSON`` (and
    ``AIOPS_ONCALL_ROSTER_JSON``) into the *process-wide* environment for the
    rest of the session. The Slack user-map loader merges that env on top of
    whatever file map a test wrote, so ``tests/test_chatops_slack_adapter.py``
    and ``tests/test_chatops_slack_bot_adapter.py`` start resolving handles to
    real member IDs instead of their fixtures — green in isolation, red in the
    full suite (the order-dependent bleed in #174).

    Clearing it here fixes #174 at the source for every test (not just the one
    file that had its own guard), and protects the bot-adapter suite, which had
    none. Tests that specifically need an override set it themselves via
    ``monkeypatch.setenv`` after this autouse fixture runs.

    ``AIOPS_ONCALL_ROSTER_JSON`` is cleared proactively for the same
    ``.env``-injection class — NOT because the Slack loader reads it (it
    doesn't; that var feeds ``scripts/seed_oncall``). It keeps a developer's
    real roster from leaking real identities into any seed-driven test.
    """
    monkeypatch.delenv("AIOPS_SLACK_USER_MAP_JSON", raising=False)
    monkeypatch.delenv("AIOPS_ONCALL_ROSTER_JSON", raising=False)


@pytest.fixture(autouse=True)
def _hermetic_chatops_sink_env(monkeypatch):
    """Clear the real-sink chatops env vars around every test.

    Same ``.env``-injection class as ``_hermetic_slack_user_map_env`` above,
    but with a worse blast radius: every test that boots the demo server via
    ``TestClient(srv.app)`` runs the lifespan, whose
    ``_register_chatops_adapters()`` reads these vars and registers REAL
    Slack/Teams/PagerDuty adapters on the process-wide ``get_client()``
    singleton — which no fixture resets. Once a developer's ``.env`` webhook
    URL leaks in (via the server's import-time ``load_dotenv``), every
    subsequent ``get_client().send()`` in the session performs an unmocked
    network POST to the developer's live channel: fixture incidents, approval
    prompts, war-room notices. ``ChatOpsClient.send`` swallows adapter
    errors, so nothing fails loudly — the suite just leaks test data to a
    real workspace and silently gains per-send network latency.

    Tests that exercise env-driven registration set these explicitly
    (``monkeypatch.setenv`` runs after this autouse fixture) or pass the
    values as arguments to ``register_env_adapters``.
    """
    monkeypatch.delenv("AIOPS_SLACK_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("AIOPS_SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("AIOPS_PAGERDUTY_INTEGRATION_KEY", raising=False)
    monkeypatch.delenv("AIOPS_TEAMS_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("AIOPS_TEAMS_DM_WEBHOOK_URL", raising=False)
    # Not a chat sink, but the same class of bug with worse blast radius: a
    # developer's real URL here would make the suite create actual Teams
    # meetings and mail calendar invites to real colleagues on every
    # Sev-1/Sev-2 fixture.
    monkeypatch.delenv("AIOPS_TEAMS_MEETING_WEBHOOK_URL", raising=False)


@pytest.fixture(autouse=True)
def _hermetic_observability_circuits():
    """Reset the Jaeger + Loki circuit breakers around every test (#113).

    Both breakers are module-level process state that survives test
    boundaries. A test that trips one (real socket failure or a mocked
    one) would otherwise short-circuit that provider's calls in the next
    30s of tests — including any test that monkeypatches ``httpx.get`` to
    succeed. Reset at both ends so a breaker can't leak in either
    direction.
    """
    _jaeger._reset_circuit_for_tests()
    _loki._reset_circuit_for_tests()
    try:
        yield
    finally:
        _jaeger._reset_circuit_for_tests()
        _loki._reset_circuit_for_tests()


@pytest.fixture(autouse=True)
def _hermetic_resilience():
    """Reset the shared resilience middleware around every test.

    Its breakers, cache and counters are process-global, so without this a test
    that trips a breaker would silently disable that provider for the next 30s of
    unrelated tests — and a cached result would be served to a test that expected
    a fresh call. Same leak class as the observability breakers above, now
    centralised because all three provider seams share this state.

    ``aiops.context.shadow`` and the collector base's in-flight request map are
    reset here rather than in fixtures of their own: all three are the same
    class of process-global state (shadow's counters plus a bounded diff ring;
    the collector's ``threading.Event`` map for in-flight duplicate requests),
    and the context layer's section cache lives in ``resilience``'s cache, so
    they are reset as one unit. Folding them in also keeps the autouse count at
    ten — each of these is a place a regression can hide, and this file already
    documents one case (``_opt_in_enrichment_seams_off``) where a fixture masked
    exactly that. A leaked in-flight entry is a particularly sharp failure mode:
    every later request for that exact (correlation_id, fingerprint) would wait
    on an ``Event`` nothing will ever set, until its timeout.
    """
    from aiops.context import shadow as _context_shadow
    from aiops.context.collectors import base as _collectors_base
    from aiops.tools import resilience as _resilience

    _resilience.reset_for_tests()
    _context_shadow.reset_for_tests()
    _collectors_base.reset_for_tests()
    try:
        yield
    finally:
        _resilience.reset_for_tests()
        _context_shadow.reset_for_tests()
        _collectors_base.reset_for_tests()


@pytest.fixture(autouse=True)
def _hermetic_topology_chain(monkeypatch):
    """Reset the topology resolver's cache + per-provider breakers, and pin the
    chain to its default, around every test.

    Same class of leak as the observability breakers above: the resolver keeps a
    process-global TTL cache and a per-provider circuit-breaker dict, so a test
    that trips a provider or caches a dependency list would otherwise change what
    an unrelated later test resolves — and topology feeds suspect derivation, so
    the bleed would show up as a wrong ``suspected_dependencies`` far from its
    cause.

    ``AIOPS_TOPOLOGY_PROVIDERS`` is deleted rather than set so tests exercise the
    real default chain; a developer's ``.env`` enabling an opt-in tier must not
    silently turn every test into a live-backend test (the same ``.env``-bleed
    class as #151 / #174 above).
    """
    monkeypatch.delenv("AIOPS_TOPOLOGY_PROVIDERS", raising=False)
    _topology.reset_for_tests()
    try:
        yield
    finally:
        _topology.reset_for_tests()


@pytest.fixture(autouse=True)
def _opt_in_enrichment_seams_off(monkeypatch):
    """Pin RA-007's three opt-in enrichment gates off for every test.

    Completes the ``.env``-bleed defence above for a case ``delenv`` cannot reach.
    These gates are module-level constants evaluated at *import*, so by the time
    any fixture runs the value is already baked in — deleting the environment
    variable changes nothing. They have to be patched on the module object.

    Without this, a machine whose ``.env`` sets ``AIOPS_CHANGE_CONTEXT`` or
    ``AIOPS_INCIDENT_HISTORY`` runs correlation stages 9 and 10 for the entire
    suite while CI, which has no ``.env``, does not. That is a green CI and a red
    laptop for the same commit — the most expensive kind of divergence, because
    the failure looks like the developer's change.

    Tests that need a seam on already patch it ``True`` themselves; a test-body
    patch applies after fixture setup, so those are unaffected.

    ``AIOPS_CONTEXT_LAYER`` joins them, but by ``delenv`` rather than ``setattr``:
    ``aiops.context.config.context_mode()`` reads the environment on every call
    precisely so it *can* be reached this way (see that module's docstring). Same
    ``.env``-bleed defence, one line instead of a fourth patched constant — a
    developer running with the context layer on must not silently exercise a
    different code path than CI does.
    """
    from agents.log_correlation import agent as _lc_agent
    from agents.log_correlation import history as _lc_history
    from agents.log_correlation import timeline_sources as _lc_timeline_sources

    monkeypatch.setattr(_lc_agent, "_CHANGE_CONTEXT_ENABLED", False, raising=False)
    monkeypatch.setattr(_lc_history, "_ENABLED", False, raising=False)
    monkeypatch.setattr(_lc_timeline_sources, "_K8S_ENABLED", False, raising=False)
    monkeypatch.delenv("AIOPS_CONTEXT_LAYER", raising=False)


@pytest.fixture(autouse=True)
def _hermetic_chatops_hub():
    """Reset the chatops WebSocket history hub around every test.

    The hub (``demo/ui/chatops_ws._HUB``) keeps a process-global history ring
    that a new ``/ws/chatops`` client replays on connect. A chatops message
    emitted by one test — the chained-demo / reactive-flow / triage suites
    route notifications through ``get_client().send()`` → the WebSocket
    adapter → ``_HUB.push()`` — otherwise lingers in that ring and leaks into
    the next test's replay, which is what makes
    ``test_chatops_ws::test_websocket_endpoint_replays_history_and_streams_new_messages``
    read a stale ``"product-catalog … Prometheus"`` message instead of its own
    ``"first"``. Clearing at both ends, same discipline as the SQLite / gate /
    Jaeger-circuit fixtures, makes the hub hermetic regardless of run order.
    """
    from demo.ui.chatops_ws import get_hub

    get_hub()._reset_for_tests()
    try:
        yield
    finally:
        get_hub()._reset_for_tests()


@pytest.fixture(autouse=True)
def _hermetic_rca_progress_hub():
    """Reset the RCA progress hub (demo/ui/rca_progress.py) around every test.

    Same discipline as ``_hermetic_chatops_hub`` immediately above, for the
    same reason: the hub is a process-global singleton keyed by run_id, and a
    run pushed by one test would otherwise linger and be replayable by a
    later test that happens to reuse (or, worse, never reuses, silently
    inflating the LRU) the same run_id.
    """
    from demo.ui.rca_progress import get_hub as get_rca_progress_hub

    get_rca_progress_hub()._reset_for_tests()
    try:
        yield
    finally:
        get_rca_progress_hub()._reset_for_tests()


@pytest.fixture(autouse=True)
def _hermetic_rca_chat_sessions():
    """Reset the in-memory RCA chat session store around every test.

    Same discipline as ``_hermetic_rca_progress_hub`` immediately above: the
    store (``demo/ui/rca_sessions.py``) is a process-global singleton, so a
    session seeded by one test would otherwise be resolvable by
    ``GET /api/rca/chat/by-incident/{id}`` in a later test that reuses the
    same incident id.
    """
    from demo.ui.rca_sessions import get_session_store

    get_session_store()._reset_for_tests()
    try:
        yield
    finally:
        get_session_store()._reset_for_tests()
