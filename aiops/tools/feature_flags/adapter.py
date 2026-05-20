"""Flagd ConfigMap adapter (ARCH-1).

The one place in the codebase that mutates the flagd-config ConfigMap. Uses
server-side apply via the official ``kubernetes`` Python client with
``field_manager="helm"`` and ``force=True`` so:

1. Every patch is labelled as ``helm`` in managedFields — subsequent
   ``helm upgrade`` calls don't hit an SSA conflict on
   ``.data.demo.flagd.json``.
2. ``force=True`` re-acquires ownership from any stray ``kubectl-patch``
   manager left over from pre-ARCH-1 code paths or ad-hoc ``kubectl patch``
   from a human. No more one-off ``helm upgrade --force`` to recover.

Field manager is a module constant on purpose — see anti-pattern §6.2 of
``docs/arch_1_feature_flags_seam_design.md``. Anything other than ``"helm"``
re-opens the conflict.
"""

from __future__ import annotations

import json
import os
from typing import Any

from kubernetes import client, config, dynamic
from kubernetes.client.rest import ApiException

from aiops.tools.registry import ToolResult, tool

_NAMESPACE = os.environ.get("AIOPS_FLAGD_NAMESPACE", "otel-demo")
_CONFIGMAP_NAME = "flagd-config"
_KEY = "demo.flagd.json"
_FIELD_MANAGER = "helm"


class FlagNotFound(Exception):
    def __init__(self, flag: str, available: list[str]) -> None:
        self.flag = flag
        self.available = available
        super().__init__(f"flag {flag!r} not present in flagd config; available: {available}")


class VariantNotValid(Exception):
    def __init__(self, flag: str, variant: str, valid: list[str]) -> None:
        self.flag = flag
        self.variant = variant
        self.valid = valid
        super().__init__(f"variant {variant!r} not valid for flag {flag!r}; choose one of {valid}")


def _resolve_kubeconfig_path() -> str | None:
    """Find a kubeconfig file without relying on ``~/`` expansion.

    Python's ``os.path.expanduser('~')`` depends on ``USERPROFILE`` (Windows)
    or ``HOME`` (POSIX) being set. Both can be absent inside a PowerShell
    ``Start-Job`` child process — that's the bug
    [ARCH-1 issue #70] caught after the first rehearsal. Resolve explicitly
    against the same env vars and fail fast with a useful message rather
    than the cryptic ``Invalid kube-config file. No configuration found.``
    """
    if (path := os.environ.get("KUBECONFIG")) and os.path.exists(path):
        return path
    home = (
        os.environ.get("USERPROFILE")
        or os.environ.get("HOME")
        or os.path.expanduser("~")
    )
    candidate = os.path.join(home, ".kube", "config") if home else None
    if candidate and os.path.exists(candidate):
        return candidate
    return None


class FlagdConfigMapAdapter:
    """Read + mutate the flagd ConfigMap via the K8s API.

    Construction loads kube config lazily — in-cluster first (for pods that
    run with a service account token), falling back to the local kubeconfig
    (typical for dev / CI). This means ``import aiops.tools.feature_flags``
    is safe in environments with no kube config; only the first capability
    call hits the cluster.
    """

    def __init__(self) -> None:
        try:
            config.load_incluster_config()
        except config.ConfigException:
            kube_path = _resolve_kubeconfig_path()
            if kube_path is None:
                raise RuntimeError(
                    "feature_flags adapter: no kubeconfig found. "
                    f"KUBECONFIG={os.environ.get('KUBECONFIG')!r}, "
                    f"USERPROFILE={os.environ.get('USERPROFILE')!r}, "
                    f"HOME={os.environ.get('HOME')!r}. "
                    "Set KUBECONFIG or restart the UI server from a shell "
                    "where USERPROFILE points at a profile containing "
                    ".kube/config."
                ) from None
            config.load_kube_config(config_file=kube_path)
        api_client = client.ApiClient()
        self._api = client.CoreV1Api(api_client)
        # The dynamic client is the supported path for server-side apply on
        # kubernetes>=29 — the typed CoreV1Api dropped the private
        # `_content_type` kwarg around v32, so trying to force apply-patch
        # via the typed client fails with ApiTypeError on v35.
        self._dyn = dynamic.DynamicClient(api_client)
        self._configmap = self._dyn.resources.get(api_version="v1", kind="ConfigMap")

    # ─── reads ──────────────────────────────────────────────────────────

    def _read_flagd_json(self) -> dict[str, Any]:
        cm = self._api.read_namespaced_config_map(_CONFIGMAP_NAME, _NAMESPACE)
        raw = (cm.data or {}).get(_KEY) or ""
        if not raw:
            raise RuntimeError(f"flagd-config ConfigMap key {_KEY!r} is empty or missing")
        return json.loads(raw)

    def get_variant(self, flag: str) -> dict[str, str]:
        cfg = self._read_flagd_json()
        flags = cfg.get("flags") or {}
        if flag not in flags:
            raise FlagNotFound(flag, available=sorted(flags))
        return {"flag": flag, "variant": flags[flag].get("defaultVariant", "off")}

    def list_variants(self) -> dict[str, dict[str, str]]:
        cfg = self._read_flagd_json()
        flags = cfg.get("flags") or {}
        return {
            "variants": {name: fdef.get("defaultVariant", "off") for name, fdef in flags.items()}
        }

    # ─── mutations ──────────────────────────────────────────────────────

    def _apply_flagd_json(self, cfg: dict[str, Any]) -> None:
        """Server-side apply the full flagd JSON as the ``helm`` field manager.

        Body is a partial object that names only the field we own. SSA
        preserves fields we don't mention (managed by other field managers).
        ``force_conflicts=True`` re-acquires ownership from any stray
        ``kubectl-patch`` manager left over from pre-ARCH-1 ad-hoc patches.
        """
        body = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": _CONFIGMAP_NAME},
            "data": {_KEY: json.dumps(cfg)},
        }
        self._configmap.server_side_apply(
            body=body,
            namespace=_NAMESPACE,
            name=_CONFIGMAP_NAME,
            field_manager=_FIELD_MANAGER,
            force_conflicts=True,
        )

    def set_variant(self, flag: str, variant: str) -> dict[str, Any]:
        cfg = self._read_flagd_json()
        flags = cfg.get("flags") or {}
        if flag not in flags:
            raise FlagNotFound(flag, available=sorted(flags))
        valid = list((flags[flag].get("variants") or {}).keys())
        if variant not in valid:
            raise VariantNotValid(flag, variant, valid)
        prev = flags[flag].get("defaultVariant", "off")
        if prev == variant:
            return {
                "flag": flag,
                "previous_variant": prev,
                "new_variant": variant,
                "noop": True,
            }
        flags[flag]["defaultVariant"] = variant
        cfg["flags"] = flags
        self._apply_flagd_json(cfg)
        return {
            "flag": flag,
            "previous_variant": prev,
            "new_variant": variant,
            "noop": False,
        }

    def reset_all(self, flags: list[str]) -> dict[str, Any]:
        """Set each flag in ``flags`` back to ``off`` in a single SSA patch.

        Atomic vs N round-trips: flagd reloads once. Flags already at ``off``
        or not in the configmap are skipped. ``touched`` lists only the ones
        that actually changed.
        """
        cfg = self._read_flagd_json()
        current = cfg.get("flags") or {}
        touched: list[dict[str, str]] = []
        for fname in flags:
            if fname not in current:
                continue
            prev = current[fname].get("defaultVariant", "off")
            if prev != "off":
                current[fname]["defaultVariant"] = "off"
                touched.append({"flag": fname, "from": prev})
        if not touched:
            return {"reset_count": 0, "touched": []}
        cfg["flags"] = current
        self._apply_flagd_json(cfg)
        return {"reset_count": len(touched), "touched": touched}


# ─── singleton wiring ────────────────────────────────────────────────────

_adapter: FlagdConfigMapAdapter | None = None


def _get_adapter() -> FlagdConfigMapAdapter:
    """Lazy singleton. Tests can monkeypatch this module's ``_adapter`` to
    inject a mock without forcing kube-config loading at import time."""
    global _adapter
    if _adapter is None:
        _adapter = FlagdConfigMapAdapter()
    return _adapter


def _api_error(exc: ApiException) -> str:
    reason = exc.reason or f"HTTP {exc.status}"
    body = exc.body or ""
    if isinstance(body, bytes):
        body = body.decode("utf-8", errors="replace")
    return f"K8s API {reason}: {body[:400]}" if body else f"K8s API {reason}"


# ─── @tool registrations — 4 capabilities, provider "flagd" ─────────────


@tool(
    name="flagd.feature_flags.set_variant",
    capability="feature_flags.set_variant",
    provider="flagd",
    description="Set the defaultVariant of one flagd flag via SSA as field_manager=helm.",
)
def set_variant(flag: str, variant: str) -> ToolResult:
    try:
        data = _get_adapter().set_variant(flag, variant)
    except FlagNotFound as exc:
        return ToolResult(
            ok=False,
            error=str(exc),
            metadata={"provider": "flagd", "available_flags": exc.available},
        )
    except VariantNotValid as exc:
        return ToolResult(
            ok=False,
            error=str(exc),
            metadata={"provider": "flagd", "valid_variants": exc.valid},
        )
    except ApiException as exc:
        return ToolResult(ok=False, error=_api_error(exc), metadata={"provider": "flagd"})
    return ToolResult(ok=True, data=data, metadata={"provider": "flagd"})


@tool(
    name="flagd.feature_flags.get_variant",
    capability="feature_flags.get_variant",
    provider="flagd",
    description="Read the defaultVariant of one flagd flag.",
)
def get_variant(flag: str) -> ToolResult:
    try:
        data = _get_adapter().get_variant(flag)
    except FlagNotFound as exc:
        return ToolResult(
            ok=False,
            error=str(exc),
            metadata={"provider": "flagd", "available_flags": exc.available},
        )
    except ApiException as exc:
        return ToolResult(ok=False, error=_api_error(exc), metadata={"provider": "flagd"})
    return ToolResult(ok=True, data=data, metadata={"provider": "flagd"})


@tool(
    name="flagd.feature_flags.list_variants",
    capability="feature_flags.list_variants",
    provider="flagd",
    description="Read all flagd flags' current defaultVariants in one round-trip.",
)
def list_variants() -> ToolResult:
    try:
        data = _get_adapter().list_variants()
    except ApiException as exc:
        return ToolResult(ok=False, error=_api_error(exc), metadata={"provider": "flagd"})
    return ToolResult(ok=True, data=data, metadata={"provider": "flagd"})


@tool(
    name="flagd.feature_flags.reset_all",
    capability="feature_flags.reset_all",
    provider="flagd",
    description="Set each given flag back to 'off' in a single SSA patch.",
)
def reset_all(flags: list[str]) -> ToolResult:
    try:
        data = _get_adapter().reset_all(flags)
    except ApiException as exc:
        return ToolResult(ok=False, error=_api_error(exc), metadata={"provider": "flagd"})
    return ToolResult(ok=True, data=data, metadata={"provider": "flagd"})
