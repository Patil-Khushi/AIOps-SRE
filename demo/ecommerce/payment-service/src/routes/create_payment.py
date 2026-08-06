import contextlib
import uuid

from fastapi import APIRouter, HTTPException, status

from ..clients import gateway_client as gateway
from ..db import redis_client as store
from ..models.payment import PaymentRequest, PaymentResponse
from ..observability import faults
from ..observability.logging_config import log
from ..observability.metrics import (
    payment_failures_total,
    payment_latency_seconds,
    payment_requests_total,
)

router = APIRouter()


@router.post("/payments", response_model=PaymentResponse)
@payment_latency_seconds.time()
def create_payment(req: PaymentRequest):
    payment_requests_total.inc()

    # Failure 4: forced 5xx.
    if faults.http_500_enabled():
        payment_failures_total.labels(reason="injected_500").inc()
        log.error("injected HTTP 500 on payment")
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "internal error")

    # Failure 3: burn CPU.
    faults.maybe_burn_cpu()

    payment_id = uuid.uuid4().hex[:16]

    # Call the external gateway. Timeout/error = Failure 2.
    try:
        result = gateway.charge(req.order_id, req.amount)
    except gateway.GatewayTimeout as exc:
        payment_failures_total.labels(reason="gateway_timeout").inc()
        _safe_save(payment_id, req, "FAILED", None)
        log.error("payment failed: gateway timeout", extra={"order_id": req.order_id})
        raise HTTPException(status.HTTP_504_GATEWAY_TIMEOUT, "gateway timeout") from exc
    except gateway.GatewayError as exc:
        payment_failures_total.labels(reason="gateway_error").inc()
        _safe_save(payment_id, req, "FAILED", None)
        log.error(
            "payment failed: gateway error", extra={"order_id": req.order_id, "error": str(exc)}
        )
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "gateway error") from exc

    # Persist success in Redis. Redis errors = Failure 1.
    record = {
        "id": payment_id,
        "order_id": req.order_id,
        "amount": req.amount,
        "status": "PAID",
        "txn_id": result.get("txn_id"),
    }
    try:
        store.save_payment(record)
    except Exception as exc:
        store.redis_connection_status.set(0)
        payment_failures_total.labels(reason="redis_error").inc()
        log.error("redis connection error", extra={"op": "save_payment", "error": str(exc)})
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "storage error") from exc

    log.info("payment successful", extra={"payment_id": payment_id, "order_id": req.order_id})
    return PaymentResponse(**record)


def _safe_save(payment_id: str, req: PaymentRequest, status_value: str, txn_id):
    """Best-effort record of a failed payment; never masks the original error."""
    with contextlib.suppress(Exception):
        store.save_payment(
            {
                "id": payment_id,
                "order_id": req.order_id,
                "amount": req.amount,
                "status": status_value,
                "txn_id": txn_id,
            }
        )
