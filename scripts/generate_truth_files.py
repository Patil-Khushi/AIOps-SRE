"""One-shot generator for the 12 missing UI-scenario truth files (issue #64 / D6).

Each scenario in ``demo/scenarios/*.yaml`` that lacks a paired truth file in
``demo/truth_files/`` gets a generated file modelled on
``demo/truth_files/template.yaml``. Content is tailored per scenario family
(error-rate / latency / capacity / infra) because they need different
expected RCA narratives and fix steps.

These are "day one" truth files per the issue body — minimal but valid.
A future PR can flesh out the RCA narrative.

Run:
    uv run python -m scripts.generate_truth_files
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SCENARIOS_DIR = REPO_ROOT / "demo" / "scenarios"
TRUTH_DIR = REPO_ROOT / "demo" / "truth_files"


# ─── Per-scenario content overrides ────────────────────────────────────────
#
# Keys keyed on scenario.id. Each value supplies the scenario-specific
# narrative slots in the truth-file template. Anything not overridden falls
# back to a sensible default derived from the scenario YAML.

SCENARIO_OVERRIDES: dict[str, dict[str, Any]] = {
    "payment_failure": {
        "layer": "application",
        "component": "payment",
        "real_cause": (
            "flagd flag `paymentFailure` is set to `100%`, causing the payment service's "
            "`Charge` gRPC handler to deterministically return an error for every call. "
            "Not a real outage — injected via feature flag."
        ),
        "rca_summary": (
            "Payment service error rate is 100% across all charges. Span metrics for the "
            "payment service show `status_code=STATUS_CODE_ERROR` on every Charge span. The "
            "`paymentFailure` flag in flagd-config is at `100%`. Likely cause: the flag was "
            "flipped on; payment is rejecting every charge. Confirm by reading the flagd "
            "ConfigMap."
        ),
        "evidence": [
            "PaymentErrorRateHigh alert firing",
            "rate(traces_span_metrics_calls_total{service_name=\"payment\",status_code=\"STATUS_CODE_ERROR\"}[2m]) > 0",
            "flagd config: paymentFailure variant `100%` active",
        ],
        "hypotheses": [
            "paymentFailure feature flag is enabled",
            "Recent regression in payment.Charge handler",
            "Downstream dependency (Redis/DB) failure cascading to payment",
        ],
        "fix_low": "Flip paymentFailure variant to `off` in flagd configmap.",
        "fix_med": "Restart payment pods if the flag flip doesn't recover within 60s.",
        "wrong_fixes": [
            ("Scale up payment pods", "Capacity is not the bottleneck — every request still fails 100%."),
            ("Restart the database", "DB is healthy; the error originates in the application layer."),
        ],
        "rca_must_include": ["paymentFailure", "feature flag"],
    },
    "payment_unreachable": {
        "layer": "network",
        "component": "payment",
        "real_cause": (
            "flagd flag `paymentUnreachable` is on, simulating a connection-refused failure "
            "between checkout and payment. Payment is healthy in isolation but unreachable "
            "from upstream callers."
        ),
        "rca_summary": (
            "Checkout's outbound calls to payment fail with connection-refused. Payment "
            "itself is up and serving the few direct requests that reach it. flagd shows "
            "`paymentUnreachable` is on. Likely cause: the flag is simulating a network "
            "partition between checkout and payment."
        ),
        "evidence": [
            "PaymentErrorRateHigh alert firing on the calling side",
            "checkout span error.message contains \"connection refused\"",
            "flagd config: paymentUnreachable=on",
        ],
        "hypotheses": [
            "paymentUnreachable feature flag is enabled",
            "Network policy blocking checkout → payment",
            "Service discovery (DNS) returning stale endpoints",
        ],
        "fix_low": "Flip paymentUnreachable to `off` in flagd configmap.",
        "fix_med": "If unreachable persists after the flag flip, restart frontend-proxy.",
        "wrong_fixes": [
            ("Restart payment pods", "Payment is healthy; restarting it would not change the simulated network failure."),
        ],
        "rca_must_include": ["paymentUnreachable", "feature flag"],
    },
    "cart_failure": {
        "layer": "application",
        "component": "cart",
        "real_cause": (
            "flagd flag `cartFailure` is on. The cart service returns a 5xx on every "
            "request. Customer impact: shopping cart cannot be opened or modified."
        ),
        "rca_summary": (
            "Cart service error rate is 100%. Span metrics show STATUS_CODE_ERROR across "
            "all cart operations. flagd shows `cartFailure=on`. Likely cause: the flag was "
            "flipped on; cart rejects every request."
        ),
        "evidence": [
            "CartErrorRateHigh alert firing",
            "cart span p95 returns 500 across GetCart / AddItem / EmptyCart",
            "flagd config: cartFailure=on",
        ],
        "hypotheses": [
            "cartFailure feature flag is enabled",
            "Cart's valkey backing store is unreachable",
            "Cart deployment is mid-rollback",
        ],
        "fix_low": "Flip cartFailure to `off` in flagd configmap.",
        "fix_med": "If cart errors persist, check valkey-cart pod health (`kubectl -n otel-demo logs deploy/valkey-cart`).",
        "wrong_fixes": [
            ("Restart valkey-cart", "Valkey is healthy. Restarting it would only break sessions."),
        ],
        "rca_must_include": ["cartFailure", "feature flag"],
    },
    "product_catalog_failure": {
        "layer": "application",
        "component": "product-catalog",
        "real_cause": (
            "flagd flag `productCatalogFailure` is on. Product-catalog returns errors on a "
            "subset of products (the upstream OTel demo's documented behaviour for this "
            "flag). Some product pages 5xx; others succeed."
        ),
        "rca_summary": (
            "Product-catalog has a partial error rate — only specific product IDs 5xx. "
            "Span metrics show STATUS_CODE_ERROR clustered on a subset of GetProduct spans. "
            "flagd shows `productCatalogFailure=on`."
        ),
        "evidence": [
            "ProductCatalogErrorRateHigh alert firing",
            "rate(traces_span_metrics_calls_total{service_name=\"product-catalog\",status_code=\"STATUS_CODE_ERROR\"}[1m]) > 0",
            "flagd config: productCatalogFailure=on",
        ],
        "hypotheses": [
            "productCatalogFailure feature flag is enabled",
            "Bad data for specific product IDs in the catalog DB",
            "Partial deployment / canary stuck",
        ],
        "fix_low": "Flip productCatalogFailure to `off` in flagd configmap.",
        "fix_med": "If errors persist, restart product-catalog pods.",
        "wrong_fixes": [
            ("Re-import the entire product catalog", "Data is fine; the flag is the issue."),
        ],
        "rca_must_include": ["productCatalogFailure", "feature flag"],
    },
    "ad_failure": {
        "layer": "application",
        "component": "ad",
        "real_cause": (
            "flagd flag `adFailure` is on. The ad service returns 5xx — homepage banners "
            "render with placeholders or disappear entirely."
        ),
        "rca_summary": (
            "Ad service error rate spike. Frontend banner-fetch calls fail. flagd shows "
            "`adFailure=on`. Likely cause: the flag was flipped on."
        ),
        "evidence": [
            "AdErrorRateHigh alert firing",
            "rate(traces_span_metrics_calls_total{service_name=\"ad\",status_code=\"STATUS_CODE_ERROR\"}[1m]) > 0",
            "flagd config: adFailure=on",
        ],
        "hypotheses": [
            "adFailure feature flag is enabled",
            "Ad service OOM / pod crashloop",
            "Ad's downstream LLM dependency unreachable",
        ],
        "fix_low": "Flip adFailure to `off` in flagd configmap.",
        "fix_med": "If errors persist, check ad pod logs and restart if necessary.",
        "wrong_fixes": [],
        "rca_must_include": ["adFailure", "feature flag"],
    },
    "recommendation_cache_failure": {
        "layer": "application",
        "component": "recommendation",
        "real_cause": (
            "flagd flag `recommendationCacheFailure` is on, causing the recommendation "
            "service to skip its in-memory cache and recompute on every request. p95 "
            "latency climbs above target."
        ),
        "rca_summary": (
            "Recommendation latency p95 is elevated. Span metrics show duration up across "
            "the board. flagd shows `recommendationCacheFailure=on`. Cache is being "
            "bypassed; every request recomputes."
        ),
        "evidence": [
            "RecommendationLatencyP95High alert firing",
            "histogram_quantile(0.95, ...service_name=\"recommendation\") rising",
            "flagd config: recommendationCacheFailure=on",
        ],
        "hypotheses": [
            "recommendationCacheFailure feature flag is enabled",
            "Cache backend (Valkey/Redis) unreachable",
            "Recent recommendation deployment removed caching path",
        ],
        "fix_low": "Flip recommendationCacheFailure to `off` in flagd configmap.",
        "fix_med": "Once flag is off, warm the cache by issuing a handful of GetRecommendations requests.",
        "wrong_fixes": [
            ("Increase recommendation pod CPU limits", "Latency is from cache miss, not CPU saturation."),
        ],
        "rca_must_include": ["recommendationCacheFailure", "cache"],
    },
    "ad_manual_gc": {
        "layer": "application",
        "component": "ad",
        "real_cause": (
            "flagd flag `adManualGc` triggers periodic manual GC pauses in the ad service. "
            "Visible as latency spikes synchronised across all ad-service spans every few seconds."
        ),
        "rca_summary": (
            "Ad service p95 latency is spiky with sharp peaks. Span duration histograms "
            "show bursts of slow spans. flagd shows `adManualGc=on`. The GC pauses are "
            "synthetic, injected by the flag."
        ),
        "evidence": [
            "AdLatencyP95High alert firing",
            "ad span duration histogram shows bimodal distribution",
            "flagd config: adManualGc=on",
        ],
        "hypotheses": [
            "adManualGc feature flag is enabled",
            "JVM tuning regression in ad service",
            "Memory pressure forcing real GC pauses",
        ],
        "fix_low": "Flip adManualGc to `off` in flagd configmap.",
        "fix_med": "If spikes persist, increase ad pod heap size as a temporary mitigation.",
        "wrong_fixes": [
            ("Switch ad-service GC algorithm via env var", "Won't help — the GC is synthetic, triggered by the flag, not by real allocation pressure."),
        ],
        "rca_must_include": ["adManualGc", "GC"],
    },
    "image_slow_load_10s": {
        "layer": "application",
        "component": "frontend",
        "real_cause": (
            "flagd flag `imageSlowLoad` is set to `10sec`, injecting a 10-second delay into "
            "the frontend's image-fetch path. Frontend p95 page-load latency degrades."
        ),
        "rca_summary": (
            "Frontend image latency p95 is at 10s. Span metrics show the delay is inside "
            "the image-fetch span, not in image-provider. flagd shows `imageSlowLoad=10sec`. "
            "Synthetic delay injected by the flag."
        ),
        "evidence": [
            "FrontendImageLatencyHigh alert firing",
            "frontend image-fetch span duration ≈ 10s consistently",
            "flagd config: imageSlowLoad variant `10sec`",
        ],
        "hypotheses": [
            "imageSlowLoad feature flag is set to 10sec",
            "Image-provider service slowness",
            "CDN or upstream image-host degradation",
        ],
        "fix_low": "Flip imageSlowLoad to `off` in flagd configmap.",
        "fix_med": "If the delay persists, check image-provider pod (`kubectl -n otel-demo logs deploy/image-provider`).",
        "wrong_fixes": [
            ("Scale image-provider", "Image-provider is healthy; the delay is injected on the frontend side."),
        ],
        "rca_must_include": ["imageSlowLoad", "frontend"],
    },
    "loadgen_homepage_flood": {
        "layer": "application",
        "component": "frontend",
        "real_cause": (
            "flagd flag `loadGeneratorFloodHomepage` is on. The load generator amplifies "
            "homepage traffic to >10 req/s (vs normal ~3 req/s) for the duration the flag "
            "is on."
        ),
        "rca_summary": (
            "Frontend request rate has jumped 3-4x. Span volume is up across all frontend "
            "operations. flagd shows `loadGeneratorFloodHomepage=on`. Synthetic traffic "
            "surge from the load generator, not real users."
        ),
        "evidence": [
            "FrontendTrafficSurge alert firing",
            "sum(rate(...service_name=\"frontend\")) > 10 req/s",
            "flagd config: loadGeneratorFloodHomepage=on",
        ],
        "hypotheses": [
            "loadGeneratorFloodHomepage feature flag is enabled",
            "Real traffic surge / promotion campaign",
            "Bot or DDoS at the edge",
        ],
        "fix_low": "Flip loadGeneratorFloodHomepage to `off` in flagd configmap.",
        "fix_med": "If real traffic is the cause, scale frontend HPA min replicas up.",
        "wrong_fixes": [
            ("Block frontend ingress in NetworkPolicy", "Would also block legitimate traffic; the cause here is internal load generator, not external traffic."),
        ],
        "rca_must_include": ["loadGeneratorFloodHomepage", "traffic"],
    },
    "kafka_backpressure": {
        "layer": "data",
        "component": "checkout",
        "real_cause": (
            "flagd flag `kafkaQueueProblems` is on, throttling the Kafka consumer in "
            "checkout's downstream chain. Queue depth grows; checkout-fed background "
            "tasks (accounting, shipping) start erroring out."
        ),
        "rca_summary": (
            "Kafka consumer lag is climbing for the `orders` topic. Downstream services "
            "(accounting, shipping) error rates rise as their inputs get stale. flagd "
            "shows `kafkaQueueProblems=on`. The consumer-side slowness is injected by "
            "the flag."
        ),
        "evidence": [
            "CheckoutBackpressureHigh alert firing",
            "kafka consumer lag metric > threshold and rising",
            "flagd config: kafkaQueueProblems=on",
        ],
        "hypotheses": [
            "kafkaQueueProblems feature flag is enabled",
            "Kafka broker under-replicated partitions",
            "Consumer pod CPU-throttled by limits",
        ],
        "fix_low": "Flip kafkaQueueProblems to `off` in flagd configmap.",
        "fix_med": "If consumer lag persists, restart accounting + shipping deployments to reset offsets.",
        "wrong_fixes": [
            ("Increase Kafka broker memory", "Brokers are healthy; the bottleneck is at the consumer side, not the broker."),
        ],
        "rca_must_include": ["kafkaQueueProblems", "consumer"],
    },
    "email_memory_leak": {
        "layer": "application",
        "component": "email",
        "real_cause": (
            "flagd flag `emailMemoryLeak` is set to `100x`, causing the email service to "
            "leak memory ~100× normal rate. RSS grows steadily until the pod OOM-kills."
        ),
        "rca_summary": (
            "Email service RSS is rising linearly over the last 30 minutes. flagd shows "
            "`emailMemoryLeak` variant `100x`. The leak is synthetic, injected by the "
            "flag — RSS will keep rising until OOMKilled and the pod restarts."
        ),
        "evidence": [
            "EmailMemoryHigh alert firing",
            "container_memory_rss_bytes{service_name=\"email\"} rising monotonically",
            "flagd config: emailMemoryLeak variant `100x`",
        ],
        "hypotheses": [
            "emailMemoryLeak feature flag is at 100x",
            "Recent email service deploy introduced a real leak",
            "Garbage collector tuning regression",
        ],
        "fix_low": "Flip emailMemoryLeak to `off` in flagd configmap.",
        "fix_med": "Restart email pod (`kubectl -n otel-demo rollout restart deploy/email`) to reclaim leaked memory.",
        "wrong_fixes": [
            ("Increase email pod memory limit", "Treats the symptom, not the cause; the leak will fill any limit."),
        ],
        "rca_must_include": ["emailMemoryLeak", "memory"],
    },
    "ad_high_cpu": {
        "layer": "application",
        "component": "ad",
        "real_cause": (
            "flagd flag `adHighCpu` is on, causing the ad service to consume CPU above "
            "80% of its limit. Triggers HPA scaling but never relieves because the CPU "
            "spike is synthetic."
        ),
        "rca_summary": (
            "Ad service CPU usage is pegged above 80%. HPA has scaled out additional "
            "replicas but CPU stays high. flagd shows `adHighCpu=on`. The CPU consumption "
            "is injected by the flag, not driven by traffic."
        ),
        "evidence": [
            "AdCpuHigh alert firing",
            "container_cpu_usage{service_name=\"ad\"} > 0.8 sustained",
            "flagd config: adHighCpu=on",
        ],
        "hypotheses": [
            "adHighCpu feature flag is enabled",
            "Real CPU-bound regression in ad service",
            "Background batch job stuck in tight loop",
        ],
        "fix_low": "Flip adHighCpu to `off` in flagd configmap.",
        "fix_med": "If CPU stays high after flag-off, restart ad pods to clear any in-process accumulated work.",
        "wrong_fixes": [
            ("Increase ad pod CPU limit", "Won't help — the synthetic CPU consumption scales to whatever limit is set."),
        ],
        "rca_must_include": ["adHighCpu", "CPU"],
    },
}


def build_truth(scn: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    sid = scn["id"]
    return {
        "scenario_id": sid,
        "title": scn.get("title", sid),
        "last_updated": date.today().isoformat(),
        "owner": "poc-team",
        "real_cause": {
            "layer": over["layer"],
            "component": over["component"],
            "description": over["real_cause"],
        },
        "expected_rca": {
            "cause_summary": over["rca_summary"],
            "evidence_signals": over["evidence"],
            "ranked_hypotheses": over["hypotheses"],
            "confidence_floor": 0.7,
        },
        "expected_fix_steps": [
            {
                "description": over["fix_low"],
                "blast_radius": "low",
                "rollback": "Flip the flag back to its previous variant in the flagd configmap.",
                "requires_hitl": False,
            },
            {
                "description": over["fix_med"],
                "blast_radius": "medium",
                "rollback": "kubectl rollout undo for the affected deployment.",
                "requires_hitl": True,
            },
        ],
        "known_wrong_fixes": [
            {"description": d, "why_wrong": w} for d, w in over.get("wrong_fixes", [])
        ],
        "scoring": {
            "rca_must_include": over["rca_must_include"],
            "fix_step_match": "any-of",
            "max_time_to_rca_seconds": 180,
        },
        "notes": (
            "Generated by scripts/generate_truth_files.py for DEMO-12 (#64). "
            "Refine the cause/fix narrative as the agents' real RCA output matures."
        ),
    }


def main() -> None:
    written: list[str] = []
    skipped: list[str] = []
    for path in sorted(SCENARIOS_DIR.glob("*.yaml")):
        scn = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(scn, dict):
            continue
        sid = scn.get("id")
        if not sid:
            continue
        truth_path = TRUTH_DIR / f"{sid}.yaml"
        if truth_path.exists():
            skipped.append(sid)
            continue
        over = SCENARIO_OVERRIDES.get(sid)
        if over is None:
            print(f"WARN: no override for {sid!r} — skipping")
            continue
        truth = build_truth(scn, over)
        truth_path.write_text(
            yaml.safe_dump(
                truth,
                sort_keys=False,
                allow_unicode=True,
                default_flow_style=False,
                width=100,
            ),
            encoding="utf-8",
        )
        written.append(sid)
    print(f"wrote {len(written)} truth file(s) | skipped {len(skipped)} already-present")
    for s in written:
        print(f"  + {s}")
    for s in skipped:
        print(f"  = {s}")


if __name__ == "__main__":
    main()
