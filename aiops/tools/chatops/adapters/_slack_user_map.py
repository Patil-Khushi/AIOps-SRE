"""Shared Slack name→user-ID resolver for the webhook + bot adapters.

The committed ``slack_users.json`` carries **placeholder** entries
(``UPLACEHOLDER1``…) — committing real Slack member IDs would publish
them in git history forever. Real IDs come from
``AIOPS_SLACK_USER_MAP_JSON`` in the (encrypted) ``.env.shared``;
the env override is merged on top of whatever is in the file.

Both adapters call :func:`load_slack_user_map` so the lookup behaviour
stays in lock-step — when an entry is corrected once, both the team
channel post and the personal DM start pinging the same person.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# Single env var carrying the real handle→user-id mapping for the
# workspace. Format: a JSON object, e.g.
# ``{"alice": "U0123ABCDEF", "alice@example.com": "U0123ABCDEF"}``.
# Lives in ``.env.shared`` (encrypted via git-crypt); see SECRETS.md.
_ENV_VAR = "AIOPS_SLACK_USER_MAP_JSON"


def _load_file_map(path: Path) -> dict[str, str]:
    """Read the committed placeholder map. Permissive on all errors.

    A missing or malformed file degrades to ``{}`` rather than crashing.
    The trade-off is intentional: demo continuity beats hard failure on
    a config file most operators don't groom by hand.
    """
    if not path.exists():
        logger.info(
            "slack user-map: %s not found; mentions will render as plain text",
            path,
        )
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning(
            "slack user-map: %s unreadable (%s); falling back to empty map",
            path,
            exc,
        )
        return {}
    if not isinstance(data, dict):
        logger.warning(
            "slack user-map: %s must be a JSON object (got %s); using empty map",
            path,
            type(data).__name__,
        )
        return {}
    return _filter_entries(data)


def _load_env_overrides() -> dict[str, str]:
    """Read the ``AIOPS_SLACK_USER_MAP_JSON`` env var (encrypted secret).

    Returns ``{}`` when unset, blank, or malformed. A malformed value
    logs a warning instead of raising — the rest of the chatops seam
    must remain functional even when the override is broken.
    """
    raw = os.environ.get(_ENV_VAR, "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning(
            "slack user-map: %s is not valid JSON (%s); ignoring override",
            _ENV_VAR,
            exc,
        )
        return {}
    if not isinstance(data, dict):
        logger.warning(
            "slack user-map: %s must be a JSON object (got %s); ignoring override",
            _ENV_VAR,
            type(data).__name__,
        )
        return {}
    return _filter_entries(data)


def _filter_entries(data: dict) -> dict[str, str]:
    """Keep only ``str→str`` entries; drop documentation keys (``_*``)."""
    return {
        k: v
        for k, v in data.items()
        if isinstance(k, str) and isinstance(v, str) and not k.startswith("_")
    }


def load_slack_user_map(path: Path) -> dict[str, str]:
    """Resolved map = committed placeholders merged with env overrides.

    Real Slack member IDs from the env override win over placeholders
    in the file. Entries that only exist in the env override are added.
    Entries that only exist in the file (handles that haven't been
    assigned a real ID yet) stay as the placeholder so the demo doesn't
    error — the message lands without a real ping until the env is
    updated.
    """
    merged = _load_file_map(path)
    overrides = _load_env_overrides()
    if overrides:
        merged.update(overrides)
        logger.info(
            "slack user-map: merged %d entries from %s on top of %s",
            len(overrides),
            _ENV_VAR,
            path.name,
        )
    return merged


__all__ = ["load_slack_user_map"]
