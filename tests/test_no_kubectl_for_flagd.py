"""ARCH-1 guard rail (issue #70).

Only ``aiops/tools/feature_flags/`` may mutate the flagd-config ConfigMap.
Any other code path that shells out to ``kubectl patch ... flagd-config`` is
a layering violation: it re-creates the field-manager conflict against
helm's server-side-apply manager that ARCH-1 set out to eliminate.

If this test fails, route the offending caller through the registry —
``aiops.tools.get_registry().call('feature_flags.set_variant', ...)`` etc.
— rather than adding ``--field-manager=helm`` to a new shell-out site.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ALLOWED = (
    REPO / "aiops" / "tools" / "feature_flags",
    REPO / "docs",
    REPO / "tests" / "test_no_kubectl_for_flagd.py",  # this file
)
PATTERN = re.compile(r"""kubectl[^"']*patch[^"']*flagd-config""")


def test_no_kubectl_patch_for_flagd_outside_seam() -> None:
    offenders: list[str] = []
    for f in REPO.rglob("*.py"):
        rel = f.relative_to(REPO).as_posix()
        if any(str(f).startswith(str(a)) for a in ALLOWED):
            continue
        if rel.startswith((".venv/", ".claude/", "build/", "dist/", ".tmp_")):
            continue
        text = f.read_text(encoding="utf-8", errors="ignore")
        for m in PATTERN.finditer(text):
            line_no = text[: m.start()].count("\n") + 1
            offenders.append(f"{rel}:{line_no}")
    assert not offenders, (
        "kubectl patch of flagd-config outside the feature_flags seam:\n  "
        + "\n  ".join(offenders)
        + "\n\nRoute the call through aiops.tools.get_registry().call("
        + "'feature_flags.set_variant', ...) instead."
    )
