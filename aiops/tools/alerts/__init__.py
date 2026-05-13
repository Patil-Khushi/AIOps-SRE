"""Alert-source adapters: vendor webhook payload → canonical Alert dict.

One adapter per source so a new monitoring vendor can plug in without
touching agent code or the canonical Alert schema. Adapters are pure
functions (no HTTP, no registry hooks); fetching/polling lives in
``aiops/tools/observability/*`` next door.
"""

from __future__ import annotations
