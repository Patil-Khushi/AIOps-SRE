"""Unit tests for the ``flagd`` ConfigMap adapter — K8s client mocked.

The adapter's only side effect is one ``patch_namespaced_config_map`` call
per mutation, and one ``read_namespaced_config_map`` per read. We assert on
the call args because the whole point of ARCH-1 is the exact SSA shape:
``field_manager="helm"``, ``force=True``, content-type apply-patch.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from aiops.tools import get_registry
from aiops.tools.feature_flags import adapter as adapter_mod
from aiops.tools.feature_flags.adapter import (
    FlagdConfigMapAdapter,
    FlagNotFound,
    VariantNotValid,
)


def _flagd_doc() -> dict[str, Any]:
    """Realistic-shape flagd JSON with three flags."""
    return {
        "flags": {
            "paymentFailure": {
                "state": "ENABLED",
                "variants": {"off": 0, "100%": 1, "50%": 0.5},
                "defaultVariant": "off",
            },
            "productCatalogFailure": {
                "state": "ENABLED",
                "variants": {"off": 0, "on": 1},
                "defaultVariant": "on",
            },
            "adManualGc": {
                "state": "ENABLED",
                "variants": {"off": 0, "on": 1},
                "defaultVariant": "off",
            },
        }
    }


@pytest.fixture
def adapter(monkeypatch: pytest.MonkeyPatch) -> FlagdConfigMapAdapter:
    """Adapter with mocked CoreV1Api + DynamicClient and stub kube-config loaders.

    Resets ``adapter._adapter`` so each test gets a fresh singleton injection.
    """
    monkeypatch.setattr(adapter_mod.config, "load_incluster_config", lambda: None)
    monkeypatch.setattr(
        adapter_mod.config, "load_kube_config", lambda *a, **kw: None
    )
    monkeypatch.setattr(adapter_mod, "_resolve_kubeconfig_path", lambda: "/fake/kubeconfig")
    # Use lambdas so each constructor call returns a fresh MagicMock without
    # MagicMock(another_mock) tripping spec introspection (InvalidSpecError).
    monkeypatch.setattr(adapter_mod.client, "ApiClient", lambda *a, **kw: MagicMock())
    monkeypatch.setattr(adapter_mod.client, "CoreV1Api", lambda *a, **kw: MagicMock())
    monkeypatch.setattr(adapter_mod.dynamic, "DynamicClient", lambda *a, **kw: MagicMock())

    a = FlagdConfigMapAdapter()
    a._api.read_namespaced_config_map = MagicMock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(data={"demo.flagd.json": json.dumps(_flagd_doc())})
    )
    # The dynamic client's configmap resource — only ``server_side_apply`` is
    # used by the adapter's mutation path.
    a._configmap = MagicMock()
    a._configmap.server_side_apply = MagicMock()

    monkeypatch.setattr(adapter_mod, "_adapter", a)
    return a


# ─── reads ──────────────────────────────────────────────────────────────


def test_get_variant_returns_current_default(adapter: FlagdConfigMapAdapter) -> None:
    assert adapter.get_variant("paymentFailure") == {
        "flag": "paymentFailure",
        "variant": "off",
    }
    assert adapter.get_variant("productCatalogFailure") == {
        "flag": "productCatalogFailure",
        "variant": "on",
    }


def test_get_variant_raises_flag_not_found(adapter: FlagdConfigMapAdapter) -> None:
    with pytest.raises(FlagNotFound) as ei:
        adapter.get_variant("doesNotExist")
    assert ei.value.available == sorted(_flagd_doc()["flags"])


def test_list_variants_returns_full_map(adapter: FlagdConfigMapAdapter) -> None:
    assert adapter.list_variants() == {
        "variants": {
            "paymentFailure": "off",
            "productCatalogFailure": "on",
            "adManualGc": "off",
        }
    }


# ─── set_variant ────────────────────────────────────────────────────────


def test_set_variant_no_op_when_already_at_target(adapter: FlagdConfigMapAdapter) -> None:
    result = adapter.set_variant("paymentFailure", "off")
    assert result["noop"] is True
    assert result["previous_variant"] == "off"
    assert result["new_variant"] == "off"
    adapter._configmap.server_side_apply.assert_not_called()


def test_set_variant_applies_with_helm_field_manager_and_force_conflicts(
    adapter: FlagdConfigMapAdapter,
) -> None:
    result = adapter.set_variant("paymentFailure", "100%")

    assert result == {
        "flag": "paymentFailure",
        "previous_variant": "off",
        "new_variant": "100%",
        "noop": False,
    }

    ssa = adapter._configmap.server_side_apply
    assert ssa.call_count == 1
    kwargs = ssa.call_args.kwargs
    assert kwargs["name"] == "flagd-config"
    assert kwargs["namespace"] == "otel-demo"
    assert kwargs["field_manager"] == "helm"
    assert kwargs["force_conflicts"] is True

    body = kwargs["body"]
    assert body["apiVersion"] == "v1"
    assert body["kind"] == "ConfigMap"
    assert body["metadata"]["name"] == "flagd-config"
    patched_doc = json.loads(body["data"]["demo.flagd.json"])
    assert patched_doc["flags"]["paymentFailure"]["defaultVariant"] == "100%"
    # untouched flags preserved
    assert patched_doc["flags"]["productCatalogFailure"]["defaultVariant"] == "on"


def test_set_variant_unknown_flag(adapter: FlagdConfigMapAdapter) -> None:
    with pytest.raises(FlagNotFound):
        adapter.set_variant("nope", "on")
    adapter._configmap.server_side_apply.assert_not_called()


def test_set_variant_invalid_variant(adapter: FlagdConfigMapAdapter) -> None:
    with pytest.raises(VariantNotValid) as ei:
        adapter.set_variant("paymentFailure", "25%")
    assert sorted(ei.value.valid) == ["100%", "50%", "off"]
    adapter._configmap.server_side_apply.assert_not_called()


# ─── reset_all ──────────────────────────────────────────────────────────


def test_reset_all_skips_flags_already_off(adapter: FlagdConfigMapAdapter) -> None:
    result = adapter.reset_all(["paymentFailure", "adManualGc"])
    assert result == {"reset_count": 0, "touched": []}
    adapter._configmap.server_side_apply.assert_not_called()


def test_reset_all_atomically_resets_non_off(adapter: FlagdConfigMapAdapter) -> None:
    result = adapter.reset_all(["paymentFailure", "productCatalogFailure", "adManualGc"])
    assert result["reset_count"] == 1
    assert result["touched"] == [{"flag": "productCatalogFailure", "from": "on"}]

    ssa = adapter._configmap.server_side_apply
    assert ssa.call_count == 1
    body = ssa.call_args.kwargs["body"]
    patched_doc = json.loads(body["data"]["demo.flagd.json"])
    assert patched_doc["flags"]["productCatalogFailure"]["defaultVariant"] == "off"


def test_reset_all_ignores_unknown_flags(adapter: FlagdConfigMapAdapter) -> None:
    result = adapter.reset_all(["productCatalogFailure", "doesNotExist"])
    assert result["reset_count"] == 1
    assert {e["flag"] for e in result["touched"]} == {"productCatalogFailure"}


# ─── registry wiring ────────────────────────────────────────────────────


def test_registry_dispatches_to_set_variant(adapter: FlagdConfigMapAdapter) -> None:
    res = get_registry().call(
        "feature_flags.set_variant",
        flag="paymentFailure",
        variant="100%",
    )
    assert res.ok
    assert res.data["new_variant"] == "100%"
    assert res.metadata["provider"] == "flagd"


def test_registry_dispatches_to_list_variants(adapter: FlagdConfigMapAdapter) -> None:
    res = get_registry().call("feature_flags.list_variants")
    assert res.ok
    assert res.data["variants"]["productCatalogFailure"] == "on"


def test_registry_returns_error_for_unknown_flag(adapter: FlagdConfigMapAdapter) -> None:
    res = get_registry().call("feature_flags.set_variant", flag="nope", variant="on")
    assert not res.ok
    assert "nope" in (res.error or "")
    assert res.metadata["provider"] == "flagd"


# ─── kubeconfig path resolution (ARCH-1 issue #70 regression guard) ────


def test_resolve_kubeconfig_prefers_explicit_KUBECONFIG_env(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    explicit = tmp_path / "explicit-kube.yaml"
    explicit.write_text("apiVersion: v1\nkind: Config\n")
    monkeypatch.setenv("KUBECONFIG", str(explicit))
    assert adapter_mod._resolve_kubeconfig_path() == str(explicit)


def test_resolve_kubeconfig_falls_back_to_USERPROFILE_when_HOME_missing(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The actual bug from 2026-05-20 PS Start-Job rehearsal — `~/` expansion
    fails inside Start-Job's child process when ``USERPROFILE`` is missing
    from its env. The resolver must use whichever of USERPROFILE/HOME is set."""
    fake_home = tmp_path / "homedir"
    (fake_home / ".kube").mkdir(parents=True)
    (fake_home / ".kube" / "config").write_text("apiVersion: v1\nkind: Config\n")
    monkeypatch.delenv("KUBECONFIG", raising=False)
    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    assert adapter_mod._resolve_kubeconfig_path() == str(fake_home / ".kube" / "config")


def test_resolve_kubeconfig_returns_none_when_nothing_found(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("KUBECONFIG", raising=False)
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "no-such-dir"))
    monkeypatch.setenv("HOME", str(tmp_path / "no-such-dir"))
    assert adapter_mod._resolve_kubeconfig_path() is None
