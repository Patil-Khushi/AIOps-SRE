from fastapi import APIRouter, HTTPException, status

from ..auth.jwt_handler import create_access_token
from ..db import mysql_client as db
from ..models.user import LoginRequest, TokenResponse
from ..observability.faults import maybe_burn_cpu, maybe_inject_latency
from ..observability.logging_config import log
from ..observability.metrics import (
    login_failure_total,
    login_latency_seconds,
    login_requests_total,
)

router = APIRouter()


@router.post("/login", response_model=TokenResponse)
@login_latency_seconds.time()  # records login_latency_seconds histogram
def login(req: LoginRequest):
    login_requests_total.inc()

    # Injected faults (default inert).
    maybe_inject_latency()  # Failure 2: high latency
    maybe_burn_cpu()        # Failure 3: high CPU

    # Look up the user; DB errors here are Failure 1 (MySQL down).
    try:
        user = db.get_user_by_email(str(req.email))
    except Exception as exc:  # noqa: BLE001
        db.mysql_connection_status.set(0)
        login_failure_total.labels(reason="db_error").inc()
        log.error("database connection failed", extra={"op": "login", "error": str(exc)})
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "database error")

    if not user or not db.verify_password(req.password, user["password_hash"]):
        login_failure_total.labels(reason="invalid_credentials").inc()
        log.warning("invalid credentials", extra={"email": str(req.email)})
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")

    token = create_access_token(user["id"], user["email"])
    log.info("login successful", extra={"user_id": user["id"], "email": user["email"]})
    return TokenResponse(access_token=token)