from fastapi import APIRouter, HTTPException, status

from ..db import redis_client as store
from ..models.payment import PaymentResponse
from ..observability.logging_config import log

router = APIRouter()


@router.get("/payments/{payment_id}", response_model=PaymentResponse)
def payment_status(payment_id: str):
    try:
        record = store.get_payment(payment_id)
    except Exception as exc:
        store.redis_connection_status.set(0)
        log.error("redis connection error", extra={"op": "get_payment", "error": str(exc)})
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "storage error") from exc

    if not record:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "payment not found")
    return PaymentResponse(**record)
