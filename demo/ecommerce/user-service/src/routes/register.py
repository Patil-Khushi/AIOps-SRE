from fastapi import APIRouter, HTTPException, status

from ..db import mysql_client as db
from ..models.user import RegisterRequest
from ..observability.logging_config import log

router = APIRouter()


@router.post("/register", status_code=status.HTTP_201_CREATED)
def register(req: RegisterRequest):
    try:
        user_id = db.create_user(req.name, str(req.email), req.password)
    except ValueError as exc:
        # Duplicate email — client error, not a fault.
        log.warning("registration rejected", extra={"reason": str(exc), "email": str(req.email)})
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except Exception as exc:
        db.mysql_connection_status.set(0)
        log.error("database connection failed", extra={"op": "register", "error": str(exc)})
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "database error") from exc

    log.info("user registered", extra={"user_id": user_id, "email": str(req.email)})
    return {"id": user_id, "name": req.name, "email": str(req.email)}
