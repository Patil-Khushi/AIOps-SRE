"""PostgreSQL access layer for the Order Service.

SQLAlchemy Core over psycopg3. A single `orders` table. Connection failures
raise so routes return 500 and flip postgres_connection_status (Failure 1).
"""

import contextlib
import json
import os

from sqlalchemy import (
    Column,
    DateTime,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    func,
    insert,
    select,
    text,
    update,
)

from ..observability.logging_config import log
from ..observability.metrics import postgres_connection_status

_metadata = MetaData()
orders = Table(
    "orders",
    _metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("user_id", Integer, nullable=False),
    Column("amount", Float, nullable=False),
    Column("status", String(16), nullable=False, default="PENDING"),
    Column("items", Text, nullable=True),  # JSON-encoded line items
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
)


def _database_url() -> str:
    host = os.environ["POSTGRES_HOST"]  # KeyError = crashloop on boot
    port = os.getenv("POSTGRES_PORT", "5432")
    user = os.getenv("POSTGRES_USER", "appuser")
    password = os.getenv("POSTGRES_PASSWORD", "apppass")
    db = os.getenv("POSTGRES_DB", "orders")
    return f"postgresql+psycopg://{user}:{password}@{host}:{port}/{db}"


engine = create_engine(_database_url(), pool_pre_ping=True, pool_recycle=1800)


def init_schema() -> None:
    _metadata.create_all(engine)
    log.info("schema ensured")


def ping() -> bool:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        postgres_connection_status.set(1)
        return True
    except Exception as exc:
        postgres_connection_status.set(0)
        log.error("database connection failed", extra={"error": str(exc)})
        return False


# def create_order(user_id: int, amount: float, items: list) -> int:
def _create_order_impl(user_id: int, amount: float, items: list) -> int:
    with engine.begin() as conn:
        result = conn.execute(
            insert(orders).values(
                user_id=user_id,
                amount=amount,
                status="PENDING",
                items=json.dumps(items),
            )
        )
        return result.inserted_primary_key[0]


def set_status(order_id: int, status: str) -> None:
    with engine.begin() as conn:
        conn.execute(update(orders).where(orders.c.id == order_id).values(status=status))


def _row_to_dict(row) -> dict:
    m = dict(row._mapping)
    if m.get("items"):
        with contextlib.suppress(TypeError, ValueError):
            m["items"] = json.loads(m["items"])
    if m.get("created_at") is not None:
        m["created_at"] = m["created_at"].isoformat()
    return m


def get_orders_for_user(user_id: int) -> list[dict]:
    with engine.connect() as conn:
        rows = conn.execute(
            select(orders).where(orders.c.user_id == user_id).order_by(orders.c.id.desc())
        ).all()
        return [_row_to_dict(r) for r in rows]


def get_order(order_id: int):
    with engine.connect() as conn:
        row = conn.execute(select(orders).where(orders.c.id == order_id)).first()
        return _row_to_dict(row) if row else None
