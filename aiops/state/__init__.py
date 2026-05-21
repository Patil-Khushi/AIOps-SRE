"""Persistent state seam — the only place SQLModel/SQLAlchemy is imported.

Agents and the demo server speak to the database through
:mod:`aiops.state.repository`. Direct ``Session`` use anywhere else in the
codebase defeats the seam (CLAUDE.md non-negotiable #1).

Config:

- ``AIOPS_STATE_DB_URL`` — SQLAlchemy URL. Default
  ``sqlite:///./data/state.db`` (file auto-created on first ``init_db``).
  Swap to ``postgresql+psycopg://…`` for Phase 2 without touching agents.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from threading import Lock

from sqlalchemy import Engine
from sqlmodel import SQLModel, create_engine

from aiops.state import models as _models  # noqa: F401 — registers tables

logger = logging.getLogger(__name__)

_DEFAULT_URL = "sqlite:///./data/state.db"
_engine: Engine | None = None
_engine_lock = Lock()


def _resolve_url() -> str:
    return os.environ.get("AIOPS_STATE_DB_URL", _DEFAULT_URL).strip() or _DEFAULT_URL


def _ensure_sqlite_dir(url: str) -> None:
    if not url.startswith("sqlite"):
        return
    # Format: sqlite:///relative/path.db  or  sqlite:////abs/path.db
    path_part = url.split("///", 1)[1] if "///" in url else ""
    if not path_part or path_part == ":memory:":
        return
    Path(path_part).parent.mkdir(parents=True, exist_ok=True)


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                url = _resolve_url()
                _ensure_sqlite_dir(url)
                connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
                _engine = create_engine(url, echo=False, connect_args=connect_args)
                logger.info("aiops.state engine ready (%s)", url)
    return _engine


def init_db() -> None:
    """Create tables if they don't exist. Idempotent. Safe to call on every
    server boot — SQLite's ``CREATE TABLE IF NOT EXISTS`` is a no-op when the
    schema already matches."""
    SQLModel.metadata.create_all(get_engine())
    _migrate_add_columns_if_missing()


def _migrate_add_columns_if_missing() -> None:
    """Tiny additive-only migrator. ``metadata.create_all`` is a no-op against
    existing tables, so columns added to a model after first boot never reach
    the DB without this. Idempotent — checks ``PRAGMA table_info`` (SQLite)
    or ``information_schema`` (Postgres) before issuing ``ALTER TABLE``.

    Scope: ``clusters.embedding`` (added when persistent dedup landed). When
    the next column-add happens, extend ``_NEEDED`` rather than adding a
    second function. For schema rewrites (column type change, drop) bring in
    Alembic — this helper is intentionally additive-only.
    """
    from sqlalchemy import inspect, text

    _NEEDED: list[tuple[str, str, str]] = [
        # (table, column, SQL type)
        ("clusters", "embedding", "JSON"),
        ("verdicts", "alert_id", "VARCHAR"),
        # CHAT-2 (#82): reshape of the RA-005 placeholder table into the
        # RoutingDecision-aligned schema. ``create_all`` is a no-op on the
        # existing ``notifications`` table, so additive columns need this.
        # The old placeholder columns (target/status/sent_at/detail) had no
        # writers, so we don't migrate them — they linger harmlessly until
        # an Alembic rewrite.
        ("notifications", "routed_at", "TIMESTAMP"),
        ("notifications", "chat_severity", "VARCHAR"),
        ("notifications", "title", "VARCHAR"),
        ("notifications", "body", "VARCHAR"),
        ("notifications", "service", "VARCHAR"),
        ("notifications", "actions", "JSON"),
        ("notifications", "reason", "VARCHAR"),
        ("notifications", "audit_trace", "JSON"),
    ]

    engine = get_engine()
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    with engine.begin() as conn:
        for table, column, sql_type in _NEEDED:
            if table not in existing_tables:
                continue  # create_all just made it with the right schema
            cols = {c["name"] for c in inspector.get_columns(table)}
            if column in cols:
                continue
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}"))
            logger.info("migrated: added %s.%s (%s)", table, column, sql_type)


def reset_engine_for_tests() -> None:
    """Drop the cached engine. Tests that swap ``AIOPS_STATE_DB_URL`` need
    this so the next ``get_engine()`` rebuilds against the new URL."""
    global _engine
    with _engine_lock:
        _engine = None


__all__ = ["get_engine", "init_db", "reset_engine_for_tests"]
