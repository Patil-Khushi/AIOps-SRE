# ADR-001: Feature-flag mutation seam

## Status

Accepted — shipped 2026-05-20 (issue #70, branch `feat/arch-1-feature-flags-seam`).
Ports the design doc [`docs/arch_1_feature_flags_seam_design.md`](../arch_1_feature_flags_seam_design.md).

## Context

The demo flips OpenTelemetry-demo failure scenarios by mutating flagd's `flagd-config`
ConfigMap. That ConfigMap is **owned by the `opentelemetry-demo` Helm chart**. Both the
dashboard (`demo/ui/server.py`) and the CLI (`demo/failure_injection/inject.py`) were
`kubectl patch`-ing it directly, from several scattered call sites.

Two problems followed:

1. **Server-Side-Apply field-manager conflicts.** A `kubectl-patch` manager fighting Helm's
   manager over `.data.demo.flagd.json` produces an SSA conflict on every `helm upgrade`,
   recoverable only with `helm upgrade --force`. A per-call-site bandaid
   (`--field-manager=helm`) existed but didn't stop the *next* dev from adding a fourth
   call site and re-introducing the bug.
2. **flagd has no runtime mutation API.** Its ports (8013 gRPC, 8016 HTTP, 4000 ui) are for
   *evaluation*, not mutation; the ConfigMap is the source of truth. So "just use flagd's
   API" was not an option (verified on chart `opentelemetry-demo-0.40.8`, flagd `v0.12.9`).

This directly violated CLAUDE.md non-negotiable #1 (wrap every external dependency behind a
thin internal interface).

## Decision

Route **all** flagd mutation through a single seam, `aiops/tools/feature_flags/`, registered
on the tool registry with the capabilities `feature_flags.set_variant`, `.get_variant`,
`.reset_all`, and `.list_variants`. The adapter uses the official `kubernetes` Python client
(not a `kubectl` shell-out) with `field_manager="helm"` and `force=True`, so it takes
ownership back from any stale manager at call-site granularity. The field manager is **not**
configurable — it is always `"helm"`.

Direct `kubectl patch` of `flagd-config` anywhere outside the seam is forbidden and enforced
in CI by `tests/test_no_kubectl_for_flagd.py`.

## Consequences

- **Easier:** one owner of the mutation path; no SSA conflicts (`helm upgrade` works without
  `--force`); the provider is swappable — a future Unleash/LaunchDarkly backend is a
  one-adapter change behind the same capabilities.
- **Harder:** the adapter needs the `kubernetes` client, so it's a dependency of the `ui`
  extra; the seam must be kept honest by the smoke test (a layering rule, not a convenience).
- **We now can't:** mutate flags by ad-hoc `kubectl` from a script or a new route — that path
  fails CI by design. Use the registry capability instead.
