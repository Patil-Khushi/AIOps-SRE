from fastapi import APIRouter, Depends, HTTPException, status

from ..auth.jwt_handler import get_current_user
from ..db import mysql_client as db
from ..models.user import ProfileResponse
from ..observability.logging_config import log

router = APIRouter()


@router.get("/profile", response_model=ProfileResponse)
def profile(current=Depends(get_current_user)):
    try:
        user = db.get_user_by_id(current["id"])
    except Exception as exc:  # noqa: BLE001
        db.mysql_connection_status.set(0)
        log.error("database connection failed", extra={"op": "profile", "error": str(exc)})
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "database error")

    if not user:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found")

    return ProfileResponse(id=user["id"], name=user["name"], email=user["email"])