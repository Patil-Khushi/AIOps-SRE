"""Client for the Payment Service.

The call has an explicit timeout (PAYMENT_TIMEOUT_SECONDS). When the payment
path is slow (e.g. the mock gateway is delayed above this threshold), the call
raises TimeoutException, which the route maps to a timed-out order and bumps
payment_timeout_total (Failure 2).
"""

import os

import httpx

from ..observability.logging_config import log

_BASE = os.getenv("PAYMENT_SERVICE_URL", "http://localhost:8003")
_TIMEOUT = float(os.getenv("PAYMENT_TIMEOUT_SECONDS", "5"))


class PaymentTimeout(Exception):
    pass


class PaymentFailed(Exception):
    pass


def charge(order_id: int, amount: float) -> dict:
    try:
        resp = httpx.post(
            f"{_BASE}/payments",
            json={"order_id": order_id, "amount": amount},
            timeout=_TIMEOUT,
        )
    except httpx.TimeoutException as exc:
        log.error("payment call timed out", extra={"order_id": order_id, "timeout_s": _TIMEOUT})
        raise PaymentTimeout(str(exc)) from exc
    except httpx.HTTPError as exc:
        log.error("payment call failed", extra={"order_id": order_id, "error": str(exc)})
        raise PaymentFailed(str(exc)) from exc

    if resp.status_code >= 500:
        log.error("payment returned 5xx", extra={"order_id": order_id, "status": resp.status_code})
        raise PaymentFailed(f"payment HTTP {resp.status_code}")

    data = resp.json()
    if data.get("status") != "PAID":
        raise PaymentFailed(f"payment not completed: {data.get('status')}")
    return data
