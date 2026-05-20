"""Feature-flag tool seam (ARCH-1).

The single chokepoint for mutating the flagd-config ConfigMap. Importing this
package side-effect-registers four capabilities with the global ``aiops.tools``
registry via ``@tool`` decorators in ``adapter.py``:

- ``feature_flags.set_variant``    — flip one flag's defaultVariant
- ``feature_flags.get_variant``    — read one flag's defaultVariant
- ``feature_flags.list_variants``  — read all flags' defaultVariants (bulk)
- ``feature_flags.reset_all``      — set a list of flags back to ``off`` atomically

The current concrete provider is ``flagd`` (configmap-backed); the capability
names are vendor-neutral so a future ``unleash`` / ``launchdarkly`` adapter
swaps via the registry without touching callers.

Why this exists: see ``docs/arch_1_feature_flags_seam_design.md``. The short
version — ``kubectl patch`` shell-out from random call sites collides with
helm's server-side-apply field manager on every ``helm upgrade``. Routing
through one adapter that uses the official ``kubernetes`` Python client with
``field_manager="helm"`` and ``force=True`` eliminates the class of bug, and
the no-kubectl smoke test (``tests/test_no_kubectl_for_flagd.py``) keeps it
eliminated.
"""

from __future__ import annotations

from aiops.tools.feature_flags import adapter

__all__ = ["adapter"]
