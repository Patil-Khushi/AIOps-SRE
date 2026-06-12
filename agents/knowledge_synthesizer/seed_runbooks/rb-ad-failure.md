---
id: rb-ad-failure
title: Ad service — 5xx errors, homepage banners missing
service: ad
version: 1
tags: [error-rate, flagd, feature-flag, ad, frontend]
severity: Sev-2
source: seed
source_incident: null
status: published
related_kb: null
last_updated: 2026-06-11
---

## Symptoms
- `AdErrorRateHigh` alert firing.
- `rate(traces_span_metrics_calls_total{service_name="ad",status_code="STATUS_CODE_ERROR"}[1m]) > 0`.
- Frontend banner-fetch calls fail; homepage banners render as placeholders or disappear.

## Affected service & blast radius
`ad`. Blast radius of the fix: **LOW** — single feature flag, instant rollback.

## Diagnosis
1. Confirm ad service error-rate spike on banner-fetch spans.
2. Read flagd `flagd-config`: `adFailure == on` returns 5xx from the ad service.
3. Rule out ad pod OOM/crashloop and an unreachable downstream LLM dependency.

## Resolution steps
1. **[set_flag · low]** Flip `adFailure` to `off` in the flagd ConfigMap (via the feature-flags seam).
2. **[manual · medium]** If errors persist after the flip, check ad pod logs and restart if necessary.

## Verification
- Ad service error rate returns to baseline within ~60s of the flag flip.
- `AdErrorRateHigh` clears; homepage banners render normally.

## Rollback
1. Flip `adFailure` back to its previous variant in the flagd ConfigMap.
2. `kubectl rollout undo` for the ad deployment.

## References
- Scenario: `ad_failure`
- flagd flag: `adFailure`
