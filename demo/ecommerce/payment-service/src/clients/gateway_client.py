"""Client for the mock external payment gateway.

The call has an explicit timeout (GATEWAY_TIMEOUT_SECONDS). When the gateway is
delayed past that threshold, the call raises TimeoutException -> the route maps
it to a gateway timeout (Failure 2).
"""
import os

import httpx

from ..observability.logging_config import log

_BASE = os.getenv("GATEWAY_URL", "http://localhost:8004")
_TIMEOUT = float(os.getenv("GATEWAY_TIMEOUT_SECONDS", "5"))


class GatewayTimeout(Exception):
    pass


class GatewayError(Exception):
    pass


def charge(order_id: int, amount: float) -> dict:
    try:
        resp = httpx.post(
            f"{_BASE}/charge",
            json={"order_id": order_id, "amount": amount},
            timeout=_TIMEOUT,
        )
    except httpx.TimeoutException as exc:
        log.error("gateway timeout", extra={"order_id": order_id, "timeout_s": _TIMEOUT})
        raise GatewayTimeout(str(exc)) from exc
    except httpx.HTTPError as exc:
        log.error("gateway call failed", extra={"order_id": order_id, "error": str(exc)})
        raise GatewayError(str(exc)) from exc

    if resp.status_code >= 400:
        raise GatewayError(f"gateway HTTP {resp.status_code}")

    data = resp.json()
    if data.get("status") != "approved":
        raise GatewayError(f"gateway declined: {data.get('status')}")
    return data