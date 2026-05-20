# ARCH-1 — Feature-flag seam: kill the kubectl-patch shell-out

**Status:** ✅ **Shipped 2026-05-20** on branch `feat/arch-1-feature-flags-seam` (issue #70). The bandaid (`--field-manager=helm` at every call site) is gone — the constant lives in the adapter, and `tests/test_no_kubectl_for_flagd.py` enforces the seam. This document is preserved as historical context; the §3.3 sketch matches the shipped `aiops/tools/feature_flags/adapter.py`. One in-scope correction: a fourth capability `feature_flags.list_variants` was added beyond §3.2's original three, to keep the UI's `/metrics` gauge refresh to one K8s round-trip instead of N.

**For the next Claude session (or human) picking this up:** read [§1](#1-what-youre-fixing-and-why) first. Don't start coding until you've also read [§3](#3-the-design) and [§6](#6-anti-patterns-do-not-do-this). The whole point of this doc is to prevent the architectural drift this fix is supposed to correct.

---

## 1. What you're fixing and why

The dashboard's *Failure Injection* panel and the CLI `inject.py` both flip flagd flags by `kubectl patch`-ing the `flagd-config` ConfigMap. That ConfigMap is **owned by the `opentelemetry-demo` Helm chart**. Two field managers fighting over `.data.demo.flagd.json` produces an SSA conflict on every `helm upgrade`:

```
conflict with "kubectl-patch" using v1: .data.demo.flagd.json
```

PR #42 hardened `demo/failure_injection/inject.py` by passing `--field-manager=helm`. **But `demo/ui/server.py` has two parallel call sites — `_toggle_flagd_flag` and `reset_all_scenarios` — that PR #42 didn't touch.** Every dashboard inject/reset and every `.\reset.ps1` re-poisons the ConfigMap. The trap is now mostly the UI server's, not the CLI's.

**The bandaid that's currently in the codebase:**

- `--field-manager=helm` added to the two call sites in `demo/ui/server.py` (look for the lines around `_toggle_flagd_flag` and `reset_all_scenarios` patching `flagd-config`).
- One-off `helm upgrade --force` ran 2026-05-15 to clear the existing poisoning.

That bandaid prevents the next recurrence on the SAME two call sites. It does **not** prevent the next dev from adding a third call site, forgetting the flag, and re-creating the bug. The architectural fix is this document.

**Companion reading before you start coding:**
- [docs/plan_b_alert_pipeline_repair.md](plan_b_alert_pipeline_repair.md) — context on the demo crisis that exposed this.
- [docs/architect_retrospective_phase1.md](architect_retrospective_phase1.md) — §2 lists this class of mistake.
- CLAUDE.md §"Non-negotiable design principles" #1 — vendor neutrality and seam discipline; the rule this fix restores.
- [demo/failure_injection/inject.py](../demo/failure_injection/inject.py) — the *only* file in the repo that gets the field-manager dance right today; useful reference for the patch payload shape.

---

## 2. Why not "just use flagd's API"?

I considered (and originally recommended) routing through flagd's HTTP/gRPC endpoints to avoid touching the ConfigMap entirely. **flagd has no runtime flag-management API.** Its data model is declarative: the ConfigMap (or HTTP source, or file) IS the source of truth, and flagd evaluates from it. The endpoints on ports 8013 (gRPC), 8016 (HTTP), and 4000 (flagd-ui) are for *evaluation*, not for *mutation*. flagd-ui mutates by writing to the same ConfigMap underneath.

So the fix can't be "use flagd's API". It has to be "stop shelling out to kubectl from random places in the codebase." Same destination (the ConfigMap), better path to it.

If you find a flagd version that exposes a management API in the future, revisit this — but as of chart `opentelemetry-demo-0.40.8` (flagd `v0.12.9`), there isn't one.

---

## 3. The design

### 3.1 Package layout (new)

```
aiops/
  tools/
    feature_flags/
      __init__.py                # exports `set_variant`, `get_variant`, `reset_all`
      adapter.py                 # actual K8s API calls
      models.py                  # FlagVariant, FlagState, etc.
      tests/
        test_adapter.py          # unit tests w/ k8s client mocked
        test_no_shellout.py      # smoke test: greps codebase for kubectl-patch
```

### 3.2 Capability surface

Register three capabilities on the tool registry, matching the existing pattern in `aiops/tools/itsm/` and `aiops/tools/observability/`:

| Capability | Input | Output | Idempotency |
|---|---|---|---|
| `feature_flags.set_variant` | `{flag: str, variant: str}` | `ToolResult({previous_variant, new_variant, applied_at})` | Yes — repeated call with same args is a no-op |
| `feature_flags.get_variant` | `{flag: str}` | `ToolResult({variant: str})` | Read-only |
| `feature_flags.reset_all` | `{}` (or `{scenarios: list[str]}` to scope) | `ToolResult({reset_count, touched: list[{flag, from}]})` | Yes |

**Provider:** `flagd` (configmap-backed). The provider name is part of the capability metadata so a future provider (Unleash, LaunchDarkly) is a one-adapter swap.

### 3.3 The adapter (key points only — full code goes in `adapter.py`)

```python
# Pseudo-code — do NOT copy verbatim; this is the SHAPE, not the implementation.
from kubernetes import client, config

_NAMESPACE = "otel-demo"
_CONFIGMAP_NAME = "flagd-config"
_KEY = "demo.flagd.json"
_FIELD_MANAGER = "helm"  # NEVER make this configurable — match what the chart uses.

class FlagdConfigMapAdapter:
    def __init__(self):
        config.load_kube_config()  # in-cluster: config.load_incluster_config()
        self._api = client.CoreV1Api()

    def set_variant(self, flag: str, variant: str) -> dict:
        cfg = self._read_flagd_json()
        if flag not in cfg.get("flags", {}):
            raise FlagNotFound(flag, available=list(cfg.get("flags", {})))
        prev = cfg["flags"][flag].get("defaultVariant", "off")
        if prev == variant:
            return {"previous_variant": prev, "new_variant": variant, "noop": True}
        cfg["flags"][flag]["defaultVariant"] = variant
        self._patch_flagd_json(cfg)
        return {"previous_variant": prev, "new_variant": variant, "noop": False}

    def _patch_flagd_json(self, cfg: dict) -> None:
        body = {"data": {_KEY: json.dumps(cfg)}}
        self._api.patch_namespaced_config_map(
            name=_CONFIGMAP_NAME,
            namespace=_NAMESPACE,
            body=body,
            field_manager=_FIELD_MANAGER,
            force=True,   # take ownership back from any stale kubectl-patch manager
        )
```

**Why `force=True` matters:** if a previous-version code path (or a human running `kubectl patch` ad-hoc) left a `kubectl-patch` field manager on the ConfigMap, our patch will hit the same SSA conflict that bandaid `helm upgrade --force` fixes. `force=True` on the SSA call says "I am taking ownership of this field" — exactly equivalent to helm's `--force` flag but at the call-site granularity.

### 3.4 Migration plan

| Step | File | Change |
|---|---|---|
| 1 | `pyproject.toml` | Add `kubernetes>=29` to the `ui` extra (the adapter only needs the K8s client when running in the UI server or CLI). |
| 2 | `aiops/tools/feature_flags/` | Create the package per §3.1. |
| 3 | `aiops/tools/registry/__init__.py` (or wherever capabilities are wired) | Register the three new capabilities. |
| 4 | `demo/ui/server.py` | Replace both `_run_kubectl(["patch", "cm", "flagd-config", ...])` call sites with `registry.call("feature_flags.set_variant", ...)`. Delete `_run_kubectl` if no other caller remains (currently `/api/system/pods` uses it — leave that alone). |
| 5 | `demo/failure_injection/inject.py` | Replace its kubectl shell-out with `registry.call("feature_flags.set_variant", ...)` / `reset_all`. Keep the CLI surface (`--list`, `--clear`, scenario id arg) identical so RUNNING.md and team muscle memory don't change. |
| 6 | `tests/test_no_kubectl_for_flagd.py` | Add the smoke test in §5. |
| 7 | `CLAUDE.md` | **Delete** the `flagd-config` field-manager-trap gotcha from §"Local environment constraints". Replace with one line: "flagd flag mutation goes through `aiops.tools.get_registry().call('feature_flags.set_variant', ...)`." |
| 8 | `reset.ps1` | Unchanged. It calls the UI's `/api/scenarios/reset-all` which calls the new adapter. |
| 9 | Memory | Update [feedback_helm_over_kubectl_patch.md](file:///C:/Users/CK115382/.claude/projects/c--Projects-AIops/memory/feedback_helm_over_kubectl_patch.md) and [project_alert_pipeline_synthetic_gauge_bandaid.md](file:///C:/Users/CK115382/.claude/projects/c--Projects-AIops/memory/project_alert_pipeline_synthetic_gauge_bandaid.md) to reflect ARCH-1 landed; mark the trap as historical. |

### 3.5 Verification

1. `uv run pytest tests/test_no_kubectl_for_flagd.py` — passes (no shellout outside `aiops/tools/feature_flags/`).
2. `uv run python -m demo.failure_injection.inject slow-product-catalog` — flips flag, no field-manager surprises.
3. `Invoke-RestMethod -Method POST http://localhost:8765/api/scenarios/ad_manual_gc/inject` — same.
4. `kubectl get cm flagd-config --show-managed-fields=true -o yaml | grep -B1 -A1 manager:` — only `helm` should own `.data.demo.flagd.json`.
5. `helm upgrade otel-demo open-telemetry/opentelemetry-demo --version 0.40.8 --namespace otel-demo --values demo/otel-demo/values.yaml --wait` — succeeds WITHOUT `--force`.
6. Eval harness pass rate ≥ 0.85 (no agent regression).

---

## 4. What does NOT need to change

To avoid scope creep, here's what stays as-is:

- **The synthetic `aiops_scenario_active` gauge** in `demo/ui/server.py:/metrics`. Bandaid for a *different* problem (the OTel demo's `STATUS_CODE_UNSET` gap). Lives until upstream instrumentation is fixed. See [reference_otel_demo_span_status_gap](file:///C:/Users/CK115382/.claude/projects/c--Projects-AIops/memory/reference_otel_demo_span_status_gap.md).
- **The existing `*ErrorRateHigh` alert rules** in `values.yaml`. Correct rule shape; they'll start firing once upstream emits status correctly. Don't delete them.
- **`reset.ps1`**. It already calls the UI's `/api/scenarios/reset-all` — once that endpoint uses the new adapter, reset.ps1 transparently benefits.
- **The `--field-manager=helm` arg passed by the current bandaid** in server.py. Once the new adapter replaces those call sites, the arg becomes dead code — remove it as part of the migration, not before.

---

## 5. The smoke test (mandatory)

`tests/test_no_kubectl_for_flagd.py`:

```python
"""Prevent regression: only aiops/tools/feature_flags/ may shell out to kubectl
for flagd-config mutation. Any other code path is a layering violation; the
SSA conflict it produces against helm's field manager is non-recoverable
without `helm upgrade --force`."""
from pathlib import Path
import re

REPO = Path(__file__).resolve().parent.parent
ALLOWED = {REPO / "aiops" / "tools" / "feature_flags"}
PATTERN = re.compile(r"""kubectl[^"']*patch[^"']*flagd-config""")

def test_no_kubectl_patch_for_flagd_outside_seam() -> None:
    offenders = []
    for f in REPO.rglob("*.py"):
        if any(str(f).startswith(str(a)) for a in ALLOWED): continue
        if "/.venv/" in str(f) or "/.claude/" in str(f): continue
        text = f.read_text(encoding="utf-8", errors="ignore")
        for m in PATTERN.finditer(text):
            offenders.append(f"{f}:{text[:m.start()].count(chr(10))+1}")
    assert not offenders, (
        "kubectl patch of flagd-config outside aiops/tools/feature_flags/:\n"
        + "\n".join(offenders)
    )
```

This is the gate. If it fails, the offending file goes through the seam — no exceptions.

---

## 6. Anti-patterns (do NOT do this)

1. **Do not** "fix" this by adding `--field-manager=helm` at every existing call site and calling it done. That's the bandaid. The next call site somebody adds will forget the flag.
2. **Do not** make `field_manager` configurable in the adapter. It's `"helm"`, full stop. Anything else re-opens the SSA conflict.
3. **Do not** replace `kubectl` shell-out with `subprocess` calls to `python-kubectl` or any wrapper. Use the official `kubernetes` Python client library directly. That's what gives us SSA semantics, structured errors, and no shell-quoting traps.
4. **Do not** try to use flagd's HTTP API at `:8016` for mutation — it doesn't exist (see §2).
5. **Do not** delete the existing `*ErrorRateHigh` rules in `values.yaml` thinking they're redundant with the synthetic gauge. They're orthogonal; they cover *real* error data once upstream is fixed. See §4.
6. **Do not** scope-creep into "while we're at it, refactor `aiops/tools/registry`." This change is one adapter + the smoke test. Anything more is a separate PR.
7. **Do not** assume CLAUDE.md's field-manager-trap entry is still accurate after this lands — delete it as part of the PR (per §3.4 step 7). Stale doc is worse than no doc.

---

## 7. Estimated cost

- **Focused work:** 2–3 hours (per the time-budget table in the user-facing planning conversation 2026-05-15).
- **Code review + merge:** 4–8 hours calendar, team-dependent.
- **Total calendar:** ~1 dev-day from PR-open to merged.

---

## 8. Definition of done

- [ ] `aiops/tools/feature_flags/` exists with 3 registered capabilities.
- [ ] Both call sites in `demo/ui/server.py` and the one in `demo/failure_injection/inject.py` use the registry.
- [ ] `tests/test_no_kubectl_for_flagd.py` passes.
- [ ] `helm upgrade otel-demo ... --values demo/otel-demo/values.yaml` succeeds WITHOUT `--force` on a cluster that has been through 100+ inject/reset cycles via the new path.
- [ ] CLAUDE.md no longer mentions the flagd-config field-manager trap.
- [ ] Eval harness overall_pass_rate ≥ 0.85.
- [ ] Smoke tests pass: `uv run pytest`.

---

*Drafted 2026-05-15 the night before the demo. Implementation deferred to the post-demo sprint. Read this file BEFORE writing any feature-flag-related code.*
