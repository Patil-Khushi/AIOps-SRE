"""Locate a kubectl binary that actually works from ``subprocess``.

Lifted from ``demo/failure_injection/inject.py`` rather than imported: this
package is deliberately standalone (it ships inside ``demo/ecommerce/`` and is
runnable without the rest of the repo on ``sys.path``). Keep the two in sync if
the probe logic changes.

Background: Rancher Desktop ships a ``kuberlr`` wrapper at
``C:\\Program Files\\Rancher Desktop\\resources\\resources\\win32\\bin\\kubectl.exe``
that rejects standard flags like ``-n`` when invoked from subprocess. It works
fine typed into PowerShell, which makes the failure mode confusing. We probe
each candidate and keep the first real one.
"""

from __future__ import annotations

import os
import subprocess
import sys

_CACHE: str | None = None


def _looks_real(path: str) -> bool:
    """Real kubectl emits ``clientVersion`` JSON; the kuberlr wrapper errors."""
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


def resolve() -> str:
    """Return a path to a working kubectl, or raise with actionable advice."""
    global _CACHE
    if _CACHE:
        return _CACHE

    candidates: list[str] = []

    # 1. Explicit override always wins.
    if (env_path := os.environ.get("KUBECTL")) and os.path.exists(env_path):
        candidates.append(env_path)

    # 2. winget-installed kubectl — the standalone binary the repo README tells
    #    contributors to install precisely because of the wrapper problem.
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

    # 3. Everything on PATH, in PATH order.
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
    else:
        candidates.append("kubectl")

    for candidate in candidates:
        if _looks_real(candidate):
            _CACHE = candidate
            return candidate

    raise RuntimeError(
        "No working kubectl found. Rancher Desktop's bundled kubectl is a "
        "kuberlr wrapper that rejects flags from subprocess calls. Install a "
        "standalone one:\n"
        "    winget install --scope user Kubernetes.kubectl\n"
        "or point the KUBECTL env var at a real binary."
    )
