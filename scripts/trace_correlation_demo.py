"""Capture a real end-to-end trace of one incident through RA-007.

Runs the checkout/payment scenario on the synthetic path (no cluster needed) with
every relevant logger at DEBUG, and records wall-clock timing per stage by
instrumenting the agent's own functions rather than re-implementing the pipeline —
so the timings describe the real call graph, not a parallel copy of it.

Usage:  uv run python -m scripts.trace_correlation_demo
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import UTC, datetime, timedelta

os.environ.setdefault("AIOPS_LLM_PROVIDER", "stub")

# Configure before importing the agent so module-level loggers inherit it.
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)-7s %(name)-44s %(message)s",
    stream=sys.stdout,
)
for noisy in ("httpx", "httpcore", "urllib3", "kubernetes", "asyncio", "openai", "anthropic"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

from agents.log_correlation import CorrelationInput  # noqa: E402
from agents.log_correlation import agent as lc  # noqa: E402

_TIMINGS: list[tuple[str, float]] = []


def _timed(module, name: str, label: str) -> None:
    """Wrap a pipeline function to record its wall-clock cost.

    Wrapping the real functions keeps the measurement honest: a hand-rolled
    re-implementation would time code that is not what production runs.
    """
    original = getattr(module, name)

    def wrapper(*args, **kwargs):
        started = time.perf_counter()
        try:
            return original(*args, **kwargs)
        finally:
            _TIMINGS.append((label, (time.perf_counter() - started) * 1000.0))

    setattr(module, name, wrapper)


for fn, label in [
    ("_resolve_topology", "1. topology resolution"),
    ("_fetch_logs", "2a. fetch logs"),
    ("_fetch_traces", "2b. fetch traces"),
    ("_fetch_metrics", "2c. fetch metrics"),
    ("_synthesize_signals", "3. synthetic fallback"),
    ("_rank_signatures", "4a. rank signatures"),
    ("_suspects_from_topology", "4b. suspect derivation"),
    ("_generate_summary", "5. summary (LLM/template)"),
    ("build_evidence", "6. structured evidence"),
    ("_confidence_breakdown", "7. confidence scoring"),
    ("_build_incident_timeline", "8. incident timeline"),
]:
    if hasattr(lc, fn):
        _timed(lc, fn, label)


def main() -> None:
    end = datetime.now(UTC)
    payload = CorrelationInput(
        service="checkout",
        window={"start": (end - timedelta(minutes=15)).isoformat(), "end": end.isoformat()},
        triage_verdict={"alert_summary": "checkout error rate elevated", "severity": "high"},
    )

    print("=" * 100)
    print("INPUT CorrelationInput")
    print("=" * 100)
    print(json.dumps(payload.model_dump(mode="json"), indent=2))

    print("\n" + "=" * 100)
    print("LOG OUTPUT (real logger records, DEBUG and above)")
    print("=" * 100)
    t0 = time.perf_counter()
    result = lc.correlate(payload, force_synthetic=True)
    total_ms = (time.perf_counter() - t0) * 1000.0

    print("\n" + "=" * 100)
    print("DECISION TRACE (audit_metadata.decision_trace)")
    print("=" * 100)
    for i, line in enumerate(result.audit_metadata.decision_trace, 1):
        print(f"  {i:2}. {line}")

    print("\n" + "=" * 100)
    print("STAGE TIMING")
    print("=" * 100)
    for label, ms in _TIMINGS:
        share = (ms / total_ms * 100) if total_ms else 0
        print(f"  {label:<32} {ms:8.2f} ms  {share:5.1f}%")
    accounted = sum(ms for _, ms in _TIMINGS)
    print(f"  {'-' * 32} {'-' * 8}")
    print(f"  {'TOTAL correlate()':<32} {total_ms:8.2f} ms")
    print(f"  {'(unaccounted: assembly/validation)':<32} {total_ms - accounted:8.2f} ms")

    print("\n" + "=" * 100)
    print("RESULT CorrelationResult")
    print("=" * 100)
    d = result.model_dump(mode="json")
    print(f"  service                : {d['service']}")
    print(f"  confidence             : {d['confidence']}")
    print(f"  suspected_dependencies : {d['suspected_dependencies']}")
    print(f"  top_signatures         : {d['top_signatures']}")
    print(f"  timeline (raw signals) : {len(d['timeline'])}")
    print(f"  provenance             : {d['audit_metadata']['signal_source']}")
    print(f"  summary                : {d['summary']}")

    print("\n  --- structured evidence ---")
    for e in d.get("evidence") or []:
        tc = e["topology_context"]
        print(
            f"    {e['evidence_id']}  {e['source']:<8} {e['signal_type']:<15} "
            f"conf={e['confidence']}  sev={e['severity']:<8} "
            f"implicates={tc.get('implicated_service')} ({tc.get('relation')})"
        )
        print(f"        signature: {e['normalized_signature']}")

    print("\n  --- confidence derivation ---")
    cb = d.get("confidence_breakdown")
    if cb:
        print(f"    score={cb['score']}  base={cb['base']}  capped={cb['capped']}")
        for c in cb["contributors"]:
            print(f"      +{c['delta']:<5} {c['rule_id']:<26} evidence={len(c['triggered_by'])}")
        for u in cb["unapplied"]:
            print(f"      -     {u['rule_id']:<26} {u['reason'][:58]}")

    print("\n  --- incident timeline ---")
    it = d.get("incident_timeline")
    if it:
        print(f"    sources={it['sources_present']} truncated={it['truncated']}")
        print(f"    coverage_note={it['coverage_note']}")
        for e in it["entries"]:
            print(
                f"      {e['timestamp']}  [{e['source']}/{e['severity']}] {e['service']}: {e['event'][:58]}"
            )

    print("\n  --- similar past incidents (opt-in: AIOPS_INCIDENT_HISTORY) ---")
    si = d.get("similar_incidents")
    if si is None:
        print("    None — not collected (gate off). Absent is not the same as empty.")
    else:
        print(
            f"    provider={si['provider']} attempted={si['providers_attempted']} "
            f"corpus_size={si['corpus_size']} matches={len(si['matches'])}"
        )
        if si.get("coverage_note"):
            print(f"    coverage_note={si['coverage_note']}")
        for m in si["matches"]:
            print(
                f"      {m['incident_id']:<14} sim={m['similarity_score']:.2f}  "
                f"{(m['occurred_at'] or '')[:19]}  services={m['matching_services']}"
            )
            print(f"        {m['title']}")
            print(f"        why: {m['match_explanation']}")
            if m.get("resolution"):
                print(f"        resolution: {m['resolution']}")

    print("\n  --- deployment / configuration context (opt-in: AIOPS_CHANGE_CONTEXT) ---")
    dc = d.get("deployment_context")
    if dc is None:
        print("    None — not collected (gate off). Absent is not the same as empty.")
    else:
        print(
            f"    sources_collected={dc['sources_collected']} "
            f"sources_unavailable={dc['sources_unavailable']}"
        )
        if dc.get("coverage_note"):
            print(f"    coverage_note={dc['coverage_note']}")
        for c in dc["records"]:
            print(
                f"      {(c['timestamp'] or '')[:19]}  [{c['source']}/{c['change_type']}] "
                f"{c['service'] or '-'}"
            )
            print(f"        {c['summary']}")
            print(
                f"        sha={(c.get('commit_sha') or '-')[:12]}  "
                f"author={c.get('author') or '-'}  "
                f"username={c.get('author_username') or 'unresolved (needs GITHUB_API=true)'}"
            )
        if not dc["records"]:
            print("      (no records — sources answered, genuinely zero changes in window)")


if __name__ == "__main__":
    main()
