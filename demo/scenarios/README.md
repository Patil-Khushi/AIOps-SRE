# Failure-injection scenarios

One YAML file per scenario. `demo/ui/server.py` reads this directory at
startup (D5) and exposes the result via `/api/scenarios`. PMs, SREs, and
test writers can add or edit a scenario by dropping a YAML file here — no
Python edit required.

## Schema

| Field | Type | Required | Notes |
|---|---|---|---|
| `id` | string (snake_case) | yes | Must equal the filename stem (e.g. `payment_failure.yaml` → `id: payment_failure`). Used by `/api/scenarios/{id}/inject` and as the truth-file key. |
| `category` | enum | yes | One of `errors`, `latency`, `capacity`, `infra`. Drives UI grouping. |
| `flag` | string | yes | flagd feature-flag name (matches a key under `flags.*` in the `flagd-config` configmap). |
| `variant_on` | string | no | Variant value flagd sets when the scenario is injected. Defaults to `"on"`. For intensity flags use the variant from flagd-config (e.g. `"100%"`, `"10sec"`, `"100x"`). |
| `alert` | string | yes | Prometheus alert rule name expected to fire. Must match a rule defined in `demo/otel-demo/values.yaml` under `prometheus.serverFiles`. |
| `service` | string | yes | OTel demo service whose telemetry the alert reads. |
| `title` | string | yes | Short human label shown in the UI. |
| `description` | string | yes | One-sentence description shown in the UI. |
| `eta_seconds` | int | yes | Approximate seconds until the alert is expected to fire after injection. The UI polls `/api/live-alerts` for at least this long. |

## Adding a new scenario

1. Pick a unique `id` (snake_case).
2. Confirm the matching flagd flag exists in `demo/otel-demo/values.yaml`
   (under `featureflagservice.flagdConfig` or wherever flagd-config lives).
3. Confirm or add the matching Prometheus alert rule.
4. Copy an existing file as a template, edit fields.
5. Add a matching truth file at `demo/truth_files/<id>.yaml` (D6 enforces this).
6. Restart the server. The scenario auto-appears on the Overview page.

## Single source of truth

This directory is the source of truth. `demo/ui/server.py` is a consumer.
If you change a value, do it here — not in Python. A smoke test
(`tests/test_chatops_seam.py` and friends) verifies every scenario file
parses against this schema and that every scenario has a truth file.
