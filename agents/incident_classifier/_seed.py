"""Idempotent seed loader for the RA-002 historical_incidents store.

On first call, embeds and inserts ``data/historical_seed.json`` so the
similarity search has something to match against. On subsequent calls (rows
already present), it's a no-op.

Why this lives next to the agent and not in ``aiops.state``: the seed data
is the agent's contract (which past-incident shapes it expects), so it
belongs with the agent. The persistence layer stays vendor-neutral.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from aiops.state import init_db
from aiops.state.repository import (
    count_historical_incidents,
    save_historical_incident,
)

logger = logging.getLogger(__name__)

_SEED_PATH = Path(__file__).parent / "data" / "historical_seed.json"


def _load_seed() -> list[dict[str, Any]]:
    with _SEED_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)["incidents"]


def _seed_embedding_text(entry: dict[str, Any]) -> str:
    """Canonical text we embed for each seed. Same shape that the agent
    builds for live incidents, so seed and live vectors live in the same
    space."""
    parts = [
        f"service: {entry['affected_service']}",
        f"severity: {entry['severity']}",
        f"summary: {entry['summary']}",
        f"metric: {entry.get('metric', '')}",
    ]
    if entry.get("annotations"):
        parts.append(f"annotations: {entry['annotations']}")
    if entry.get("tags"):
        parts.append(f"tags: {', '.join(entry['tags'])}")
    return " | ".join(parts)


def ensure_seeded(embed_fn: Any) -> int:
    """If the historical store is empty, embed each seed entry and insert it.
    Returns the number of rows inserted (0 if the store was already seeded
    or the seed file is missing).

    The seed file (``agents/incident_classifier/data/historical_seed.json``)
    lives in a gitignored ``data/`` directory by repo convention. When it's
    absent the agent still runs — Tier-3 (LLM cold) and Tier-4 (keyword)
    cover the no-similarity path.

    ``embed_fn`` is the agent's ``_embed`` callable. Pass-through (rather than
    importing it here) avoids a circular import.
    """
    init_db()
    if count_historical_incidents() > 0:
        return 0
    if not _SEED_PATH.exists():
        logger.info(
            "RA-002 seed file not found at %s; classifier will operate without "
            "historical similarity (Tier 3 / Tier 4 paths still functional)",
            _SEED_PATH,
        )
        return 0

    inserted = 0
    for entry in _load_seed():
        text = _seed_embedding_text(entry)
        embedding = embed_fn(text) or []
        if not embedding:
            logger.warning(
                "RA-002 seed: embedding unavailable for %s — row inserted without vector "
                "(will not be retrievable until re-embedded)",
                entry["incident_key"],
            )
        save_historical_incident(
            incident_key=entry["incident_key"],
            incident_type=entry["incident_type"],
            affected_service=entry["affected_service"],
            severity=entry["severity"],
            summary=entry["summary"],
            probable_root_cause=entry["probable_root_cause"],
            recommended_runbook=entry.get("recommended_runbook"),
            tags=entry.get("tags", []),
            embedding=embedding,
            embedding_text=text,
            source="seed",
        )
        inserted += 1
    logger.info("RA-002 seeded historical store with %d incidents", inserted)
    return inserted
