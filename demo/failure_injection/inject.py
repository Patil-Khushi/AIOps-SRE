"""One-command failure-injection runner.

Reads YAML scenario specs from ``demo/failure_injection/scenarios/`` and applies
the failure to the running demo cluster. Two mechanisms supported in Phase 0:

- ``flagd``    — POST to flagd's HTTP endpoint inside the cluster (port-forwarded).
- ``kubectl``  — delete a pod by selector. Used for hard-failure scenarios.

Phase 1+ adds Chaos Mesh experiments as a third mechanism.

Usage::

    uv run python -m demo.failure_injection.inject --list
    uv run python -m demo.failure_injection.inject slow-product-catalog
    uv run python -m demo.failure_injection.inject --clear
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

SCENARIOS_DIR = Path(__file__).parent / "scenarios"
DEFAULT_NAMESPACE = "otel-demo"


@dataclass
class Scenario:
    id: str
    title: str
    description: str
    mechanism: str
    spec: dict[str, Any]


def load_scenarios() -> dict[str, Scenario]:
    out: dict[str, Scenario] = {}
    for p in sorted(SCENARIOS_DIR.glob("*.yaml")):
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
        out[data["id"]] = Scenario(
            id=data["id"],
            title=data["title"],
            description=data.get("description", "").strip(),
            mechanism=data["mechanism"],
            spec=data,
        )
    return out


_KUBECTL_CACHE: str | None = None


def _looks_like_real_kubectl(path: str) -> bool:
    """Probe a candidate binary by running ``version --client=true``.

    Rancher Desktop ships a wrapper at
    ``C:\\Program Files\\Rancher Desktop\\resources\\resources\\win32\\bin\\kubectl.exe``
    that intercepts arg parsing and rejects standard kubectl flags like
    ``-n`` and ``--client`` from subprocess invocations. It still works when
    invoked from PowerShell directly, but breaks under ``subprocess``. Real
    kubectl always returns a usage block containing "clientVersion" for the
    probe below; the wrapper fails with "unknown flag".
    """
    try:
        result = subprocess.run(
            [path, "version", "--client=true", "--output=json"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and "clientVersion" in (result.stdout or "")


def _require_kubectl() -> str:
    """Return a path to a real kubectl binary, probing candidates if needed."""
    global _KUBECTL_CACHE
    if _KUBECTL_CACHE:
        return _KUBECTL_CACHE

    candidates: list[str] = []

    # 1. Explicit override.
    if (env_path := os.environ.get("KUBECTL")) and os.path.exists(env_path):
        candidates.append(env_path)

    # 2. winget-installed real kubectl on Windows. The package cache path
    #    is stable enough that we hard-code it as a high-priority candidate.
    if home := os.environ.get("USERPROFILE"):
        candidates.append(
            os.path.join(
                home,
                "AppData",
                "Local",
                "Microsoft",
                "WinGet",
                "Packages",
                "Kubernetes.kubectl_Microsoft.Winget.Source_8wekyb3d8bbwe",
                "kubectl.exe",
            )
        )

    # 3. ``where.exe kubectl`` results, in PATH order. Falls back to PATH
    #    lookup on non-Windows.
    if sys.platform == "win32":
        try:
            where = subprocess.run(
                ["where.exe", "kubectl"], capture_output=True, text=True, timeout=5
            )
            if where.returncode == 0:
                candidates.extend(
                    line.strip() for line in where.stdout.splitlines() if line.strip()
                )
        except (OSError, subprocess.SubprocessError):
            pass
    if which_path := shutil.which("kubectl"):
        candidates.append(which_path)

    # Probe in order; first one that responds like real kubectl wins.
    seen: set[str] = set()
    for path in candidates:
        if path in seen or not os.path.exists(path):
            continue
        seen.add(path)
        if _looks_like_real_kubectl(path):
            _KUBECTL_CACHE = path
            return path

    sys.exit(
        "No working kubectl found. Tried:\n  "
        + "\n  ".join(sorted(seen))
        + "\nSet $env:KUBECTL to point to a real kubectl, or install one via "
        "`winget install --scope user --id Kubernetes.kubectl`."
    )


def _flagd_set(flag_key: str, variant: str, *, namespace: str = DEFAULT_NAMESPACE) -> None:
    """Patch the flagd ConfigMap to flip a default variant.

    The OTel demo's flagd is sourced from a ConfigMap. We patch the JSON in
    place and restart flagd so the change picks up. This avoids needing to
    port-forward and call flagd's HTTP API directly.
    """
    kubectl = _require_kubectl()
    print(f"[flagd] reading ConfigMap flagd-config in ns={namespace}")
    # Put -n BEFORE the subcommand. Some kubectl builds (e.g. the one shipped
    # by Rancher Desktop) reject `-n` after the subcommand with
    # "unknown shorthand flag: 'n' in -n" because of how flag interspersion is
    # configured. Long form also works: --namespace=<ns>.
    raw = subprocess.check_output(
        [kubectl, "-n", namespace, "get", "configmap", "flagd-config", "-o", "json"]
    )
    cm = json.loads(raw)
    # The ConfigMap key holding the flag JSON varies between chart versions —
    # try the known names.
    data_key = next(
        (k for k in ("demo.flagd.json", "flagd-config.json") if k in cm.get("data", {})),
        None,
    )
    if data_key is None:
        sys.exit(f"flagd-config ConfigMap has unexpected shape: keys={list(cm.get('data', {}))}")
    flag_doc = json.loads(cm["data"][data_key])
    if "flags" not in flag_doc or flag_key not in flag_doc["flags"]:
        sys.exit(f"flagd: unknown flag {flag_key!r}. Known: {list(flag_doc.get('flags', {}))}")
    flag_doc["flags"][flag_key]["defaultVariant"] = variant
    cm["data"][data_key] = json.dumps(flag_doc)
    payload = json.dumps({"data": cm["data"]})
    # --field-manager=helm so future helm upgrades / rollbacks don't conflict
    # with our patches. Without this, kubectl uses field manager
    # 'kubectl-patch' which Helm's server-side apply refuses to override and
    # the next bootstrap.ps1 run fails with a managedFields conflict.
    subprocess.check_call(
        [
            kubectl,
            "-n",
            namespace,
            "patch",
            "configmap",
            "flagd-config",
            "--patch",
            payload,
            "--field-manager=helm",
        ]
    )
    print(f"[flagd] set {flag_key}.defaultVariant = {variant!r}")
    # flagd watches the file but we kick the deployment to be safe.
    subprocess.call(
        [kubectl, "-n", namespace, "rollout", "restart", "deployment/flagd"],
        stderr=subprocess.DEVNULL,
    )


def _flagd_clear(*, namespace: str = DEFAULT_NAMESPACE) -> None:
    """Reset every flag we touched back to ``off``.

    Phase 0 keeps a tiny static list. If you add a flag-driven scenario, append
    its key here so ``--clear`` resets it.
    """
    for flag in ("productCatalogFailure", "kafkaQueueProblems"):
        try:
            _flagd_set(flag, "off", namespace=namespace)
        except subprocess.CalledProcessError as exc:
            print(f"[flagd] WARN: could not reset {flag}: {exc}", file=sys.stderr)


def _kubectl_delete_pod(selector: str, *, namespace: str = DEFAULT_NAMESPACE) -> None:
    kubectl = _require_kubectl()
    print(f"[kubectl] deleting pod with selector {selector!r} in ns={namespace}")
    # -n before the subcommand for the same flag-interspersion reason as above.
    subprocess.check_call(
        [kubectl, "-n", namespace, "delete", "pod", "-l", selector, "--grace-period=0", "--force"]
    )


def apply(scenario: Scenario) -> None:
    print(f"--- {scenario.id} ---")
    print(scenario.title)
    print()
    print(scenario.description)
    print()
    if scenario.mechanism == "flagd":
        cfg = scenario.spec["flagd"]
        _flagd_set(cfg["flag_key"], cfg["variant"])
    elif scenario.mechanism == "kubectl":
        cfg = scenario.spec["kubectl"]
        _kubectl_delete_pod(cfg["selector"], namespace=cfg.get("namespace", DEFAULT_NAMESPACE))
    else:
        sys.exit(f"unknown mechanism: {scenario.mechanism!r}")
    print()
    print(f"Failure injected at {time.strftime('%Y-%m-%dT%H:%M:%S%z')}")
    truth = Path(__file__).resolve().parent.parent / "truth_files" / f"{scenario.id}.yaml"
    if truth.exists():
        print(f"Truth file: {truth.relative_to(Path.cwd())}")
    print("Run `python -m demo.failure_injection.inject --clear` to reset all flags.")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Inject demo failures into the OTel demo cluster")
    p.add_argument("scenario", nargs="?", help="Scenario id (omit when using --list / --clear)")
    p.add_argument("--list", action="store_true", help="List available scenarios")
    p.add_argument("--clear", action="store_true", help="Reset all flag-driven failures")
    args = p.parse_args(argv)

    scenarios = load_scenarios()

    if args.list:
        for s in scenarios.values():
            print(f"{s.id:30s} {s.title}")
        return 0
    if args.clear:
        _flagd_clear()
        print("All known flag-driven failures cleared.")
        return 0
    if not args.scenario:
        p.print_help()
        return 2
    if args.scenario not in scenarios:
        print(f"Unknown scenario: {args.scenario!r}", file=sys.stderr)
        print("Available:", ", ".join(scenarios), file=sys.stderr)
        return 2
    apply(scenarios[args.scenario])
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
