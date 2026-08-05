"""Redis access layer for the Payment Service.

Stores each payment record as a JSON string under key `payment:{id}`.
Connection failures raise so routes return 500 and flip
redis_connection_status (Failure 1: Redis down).
"""

import json
import os

import redis

from ..observability.logging_config import log
from ..observability.metrics import redis_connection_status

_HOST = os.environ["REDIS_HOST"]  # KeyError = crashloop on boot
_PORT = int(os.getenv("REDIS_PORT", "6379"))

# socket timeouts keep a dead Redis from hanging the request forever.
client = redis.Redis(
    host=_HOST,
    port=_PORT,
    decode_responses=True,
    socket_connect_timeout=2,
    socket_timeout=2,
)


def ping() -> bool:
    try:
        client.ping()
        redis_connection_status.set(1)
        return True
    except Exception as exc:
        redis_connection_status.set(0)
        log.error("redis connection error", extra={"error": str(exc)})
        return False


def save_payment(record: dict) -> None:
    client.set(f"payment:{record['id']}", json.dumps(record))


def get_payment(payment_id: str):
    raw = client.get(f"payment:{payment_id}")
    return json.loads(raw) if raw else None
