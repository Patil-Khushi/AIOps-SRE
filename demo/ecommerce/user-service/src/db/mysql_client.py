"""MySQL access layer.

Uses SQLAlchemy Core with PyMySQL. Kept deliberately small: a single `users`
table plus helpers for the three routes. Connection failures surface as
exceptions so routes can return 500 and flip the mysql_connection_status gauge
(Failure 1: MySQL down).
"""

import os

from passlib.context import CryptContext
from sqlalchemy import (
    Column,
    Integer,
    MetaData,
    String,
    Table,
    create_engine,
    insert,
    select,
    text,
)
from sqlalchemy.exc import IntegrityError

from ..observability.logging_config import log
from ..observability.metrics import mysql_connection_status

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

_metadata = MetaData()
users = Table(
    "users",
    _metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("name", String(255), nullable=False),
    Column("email", String(255), nullable=False, unique=True),
    Column("password_hash", String(255), nullable=False),
)


def _database_url() -> str:
    host = os.environ["MYSQL_HOST"]  # KeyError here = crashloop (Failure 4)
    port = os.getenv("MYSQL_PORT", "3306")
    user = os.getenv("MYSQL_USER", "appuser")
    password = os.getenv("MYSQL_PASSWORD", "apppass")
    db = os.getenv("MYSQL_DATABASE", "users")
    return f"mysql+pymysql://{user}:{password}@{host}:{port}/{db}"


# pool_pre_ping detects dropped connections (e.g. MySQL restarted) up front.
engine = create_engine(_database_url(), pool_pre_ping=True, pool_recycle=1800)


def init_schema() -> None:
    """Create the users table if missing. Called at startup."""
    _metadata.create_all(engine)
    log.info("schema ensured")


def ping() -> bool:
    """Check MySQL reachability and update the gauge. Never raises."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        mysql_connection_status.set(1)
        return True
    except Exception as exc:
        mysql_connection_status.set(0)
        log.error("database connection failed", extra={"error": str(exc)})
        return False


def hash_password(raw: str) -> str:
    return pwd_context.hash(raw)


def verify_password(raw: str, hashed: str) -> bool:
    return pwd_context.verify(raw, hashed)


# Precomputed once at import time so a login against a nonexistent account
# pays the same bcrypt cost as one against a real account with a wrong
# password. Without this, "no such user" short-circuits before bcrypt runs at
# all, and that fast path is measurably faster than a bcrypt mismatch — a
# timing side-channel an attacker can use to enumerate registered emails.
_DUMMY_PASSWORD_HASH = pwd_context.hash("no-such-account-timing-guard")


def verify_login(raw_password: str, user) -> bool:
    """True iff `user` exists and `raw_password` matches its stored hash.

    Always runs the bcrypt comparison, even when `user` is None, to close the
    timing gap described above.
    """
    hashed = user["password_hash"] if user is not None else _DUMMY_PASSWORD_HASH
    matched = pwd_context.verify(raw_password, hashed)
    return matched and user is not None


def create_user(name: str, email: str, password: str) -> int:
    with engine.begin() as conn:
        try:
            result = conn.execute(
                insert(users).values(name=name, email=email, password_hash=hash_password(password))
            )
            return result.inserted_primary_key[0]
        except IntegrityError as exc:
            raise ValueError("email already registered") from exc


def get_user_by_email(email: str):
    with engine.connect() as conn:
        row = conn.execute(select(users).where(users.c.email == email)).first()
        return row._mapping if row else None


def get_user_by_id(user_id: int):
    with engine.connect() as conn:
        row = conn.execute(select(users).where(users.c.id == user_id)).first()
        return row._mapping if row else None
