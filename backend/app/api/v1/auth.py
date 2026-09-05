"""Login and identity endpoints.

`POST /auth/login` exchanges credentials checked against the static store
(`core/security/users.py`) for a signed JWT. `GET /auth/me` exists mainly so the whole chain —
token verification, rate limiting, `Principal` construction — is exercisable and observable
end to end before Cycle 3's chat endpoint exists to exercise it implicitly; the Agent Activity
Panel and any later admin surface can reuse it to show "who is this token for" without
decoding a JWT by hand.
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field

from backend.app.api.deps import enforce_rate_limit
from backend.app.core.config import Settings, get_settings
from backend.app.core.errors import AuthenticationError
from backend.app.core.security.jwt import issue_access_token
from backend.app.core.security.rbac import Principal
from backend.app.core.security.users import get_user, verify_password

logger = structlog.get_logger(__name__)

router = APIRouter()


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=200)


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: str
    expires_in_minutes: int


class WhoAmIResponse(BaseModel):
    username: str
    role: str


@router.post("/auth/login", response_model=LoginResponse, status_code=status.HTTP_200_OK)
async def login(payload: LoginRequest, settings: Settings = Depends(get_settings)) -> LoginResponse:
    user = get_user(payload.username)
    if user is None or not verify_password(user, payload.password):
        # Identical error for "no such user" and "wrong password" — distinguishing them would
        # let a caller enumerate valid usernames by observing which failure they get back.
        logger.warning("login_failed", username=payload.username)
        raise AuthenticationError("Invalid username or password.")

    token = issue_access_token(Principal(username=user.username, role=user.role), settings=settings)
    logger.info("login_succeeded", username=user.username, role=user.role.value)
    return LoginResponse(
        access_token=token,
        role=user.role.value,
        expires_in_minutes=settings.jwt_expire_minutes,
    )


@router.get("/auth/me", response_model=WhoAmIResponse)
async def whoami(principal: Principal = Depends(enforce_rate_limit)) -> WhoAmIResponse:
    return WhoAmIResponse(username=principal.username, role=principal.role.value)
