"""Client for the User Service.

Order creation validates the caller by forwarding their bearer token to the
User Service's /profile endpoint. A 200 means the user is valid and gives us
their id; anything else means the order should be rejected.
"""

import os

import httpx

from ..observability.logging_config import log

_BASE = os.getenv("USER_SERVICE_URL", "http://localhost:8001")


class UserInvalid(Exception):
    """Raised when the token is missing/invalid or the user can't be resolved."""


def validate_user(authorization: str | None) -> dict:
    if not authorization:
        raise UserInvalid("missing authorization header")
    try:
        resp = httpx.get(
            f"{_BASE}/profile",
            headers={"Authorization": authorization},
            timeout=5.0,
        )
    except httpx.HTTPError as exc:
        # User Service unreachable — treat as validation failure (not our DB).
        log.error("user validation call failed", extra={"error": str(exc)})
        raise UserInvalid("user service unreachable") from exc

    if resp.status_code != 200:
        raise UserInvalid(f"user validation failed (HTTP {resp.status_code})")

    data = resp.json()
    return {"id": data["id"], "email": data.get("email")}
