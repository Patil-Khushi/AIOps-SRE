from fastapi import APIRouter, Header, HTTPException, status

from ..clients import user_service_client as users
from ..db import postgres_client as db
from ..observability.logging_config import log

router = APIRouter()


@router.get("/orders")
def get_orders(authorization: str | None = Header(default=None)):
    try:
        user = users.validate_user(authorization)
    except users.UserInvalid as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc

    try:
        return db.get_orders_for_user(user["id"])
    except Exception as exc:
        db.postgres_connection_status.set(0)
        log.error("database connection failed", extra={"op": "get_orders", "error": str(exc)})
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "database error") from exc
