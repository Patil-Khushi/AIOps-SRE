"""JWT creation and verification.

JWT_SECRET is required — a missing secret raises at import time, which is one of
the intentional CrashLoopBackOff triggers (Failure 4).
"""

import os
from datetime import UTC, datetime, timedelta
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

_SECRET = os.environ["JWT_SECRET"]  # KeyError = crashloop on startup
_ALGO = "HS256"
_EXPIRY_MIN = int(os.getenv("JWT_EXPIRY_MINUTES", "60"))

_bearer = HTTPBearer(auto_error=True)


def create_access_token(user_id: int, email: str) -> str:
    now = datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "email": email,
        "iat": now,
        "exp": now + timedelta(minutes=_EXPIRY_MIN),
    }
    return jwt.encode(payload, _SECRET, algorithm=_ALGO)


def get_current_user(
    creds: Annotated[HTTPAuthorizationCredentials, Depends(_bearer)],
) -> dict:
    """Dependency for protected routes. Returns the decoded claims."""
    try:
        claims = jwt.decode(creds.credentials, _SECRET, algorithms=[_ALGO])
        return {"id": int(claims["sub"]), "email": claims.get("email")}
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "token expired") from exc
    except jwt.PyJWTError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid token") from exc
