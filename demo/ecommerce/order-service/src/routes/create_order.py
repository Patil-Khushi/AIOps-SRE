from fastapi import APIRouter, Header, HTTPException, status

from ..clients import payment_service_client as payments
from ..clients import user_service_client as users
from ..db import postgres_client as db
from ..models.order import CreateOrderRequest
from ..observability import faults
from ..observability.logging_config import log
from ..observability.metrics import (
    order_latency_seconds,
    orders_created_total,
    orders_failed_total,
    payment_timeout_total,
)

router = APIRouter()


@router.post("/orders", status_code=status.HTTP_201_CREATED)
@order_latency_seconds.time()
def create_order(req: CreateOrderRequest, authorization: str | None = Header(default=None)):
    # Failure 3: forced unhandled 5xx.
    if faults.http_500_enabled():
        orders_failed_total.labels(reason="injected_500").inc()
        log.error("injected HTTP 500 on order creation")
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "internal error")

    # Failure 4: leak memory on each order (eventually OOMKilled).
    faults.maybe_leak_memory()

    # Step 1 — validate the user via the User Service.
    try:
        user = users.validate_user(authorization)
    except users.UserInvalid as exc:
        orders_failed_total.labels(reason="user_invalid").inc()
        log.warning("order rejected: user invalid", extra={"error": str(exc)})
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc

    items = [i.model_dump() for i in req.items]

    # Step 2 — persist the order as PENDING. DB errors = Failure 1.
    try:
        order_id = db.create_order(user["id"], req.amount, items)
    except Exception as exc:
        db.postgres_connection_status.set(0)
        orders_failed_total.labels(reason="db_error").inc()
        log.error("database connection failed", extra={"op": "create_order", "error": str(exc)})
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "database error") from exc

    # Step 3 — call Payment. Timeout/failure marks the order FAILED but the
    # order row still exists (so the client can see the failed attempt).
    try:
        payments.charge(order_id, req.amount)
    except payments.PaymentTimeout as exc:
        payment_timeout_total.inc()
        orders_failed_total.labels(reason="payment_timeout").inc()
        _safe_set_status(order_id, "FAILED")
        log.error("order failed: payment timeout", extra={"order_id": order_id})
        raise HTTPException(status.HTTP_504_GATEWAY_TIMEOUT, "payment timed out") from exc
    except payments.PaymentFailed as exc:
        orders_failed_total.labels(reason="payment_failed").inc()
        _safe_set_status(order_id, "FAILED")
        log.error("order failed: payment failed", extra={"order_id": order_id, "error": str(exc)})
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "payment failed") from exc

    # Step 4 — mark PAID.
    _safe_set_status(order_id, "PAID")
    orders_created_total.inc()
    log.info(
        "order created", extra={"order_id": order_id, "user_id": user["id"], "amount": req.amount}
    )
    return {"id": order_id, "user_id": user["id"], "amount": req.amount, "status": "PAID"}


def _safe_set_status(order_id: int, status_value: str) -> None:
    try:
        db.set_status(order_id, status_value)
    except Exception as exc:
        log.error("could not update order status", extra={"order_id": order_id, "error": str(exc)})
