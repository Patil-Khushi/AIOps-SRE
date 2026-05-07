# `demo/` — the pretend customer

The POC's "customer" is the [OpenTelemetry Demo (Astronomy Shop)](https://opentelemetry.io/docs/demo/) running in a local kind cluster. We instrument it, break it on purpose, watch agents respond, and score against truth files.

## Layout

```
demo/
├── otel-demo/             # Helm values for the upstream OTel demo chart
├── failure_injection/     # One-command scenario runner + 3 starter scenarios
├── truth_files/           # Ground truth per scenario (cause + expected fix)
└── load/                  # k6 load scripts for steady-state traffic
```

The chart itself is upstream. We do not vendor it — `infra/bootstrap.ps1` adds the Helm repo and installs from the registry.

## Why this app

The OTel demo is the right Phase-0 default because:

- already instrumented with metrics, logs, and traces (no work to do);
- ships a feature-flag service so you can turn failures on with a single API call (no bespoke chaos for the easy scenarios);
- microservices in a mix of languages — close enough to a "real" portfolio.

For the harder failures (pod kill, network partition, packet loss) we layer Chaos Mesh on top.

## Running

```powershell
.\infra\bootstrap.ps1                                              # one-time, ~10 min
uv run python -m demo.failure_injection.inject --list              # see scenarios
uv run python -m demo.failure_injection.inject slow-product-catalog
uv run python -m demo.failure_injection.inject --clear             # turn everything off
```

For continuous load that gives anomaly detectors a baseline:

```powershell
k6 run demo/load/baseline.js
```

## Adding a scenario

See `demo/failure_injection/README.md`. Every scenario must ship with a truth file in `demo/truth_files/`.
