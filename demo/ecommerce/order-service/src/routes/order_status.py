from fastapi import APIRouter, Header, HTTPException, status

from ..clients import user_service_client as users
from ..db import postgres_client as db
from ..observability.logging_config import log

router = APIRouter()


@router.get("/orders/{order_id}")
def order_status(order_id: int, authorization: str | None = Header(default=None)):
    try:
        user = users.validate_user(authorization)
    except users.UserInvalid as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc))

    try:
        order = db.get_order(order_id)
    except Exception as exc:  # noqa: BLE001
        db.postgres_connection_status.set(0)
        log.error("database connection failed", extra={"op": "order_status", "error": str(exc)})
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "database error")

    if not order:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "order not found")
    # Only the owner may read the order.
    if order["user_id"] != user["id"]:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "not your order")
    return order