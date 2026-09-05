"""JWT issuance and verification.

Real signing and verification against `Settings.jwt_secret_key`, even though the user store
behind it is static — see docs/DECISIONS.md's Option A note that only the store is hardcoded,
not the authorization mechanism. `jwt_secret_key` is `Optional` at the config layer (Cycle 0
had to boot before this account/secret existed); this module is one of the "responsible for
raising `ConfigurationError` at first use" call sites `core/config.py` describes.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
import structlog

from backend.app.core.config import Settings
from backend.app.core.errors import AuthenticationError, ConfigurationError
from backend.app.core.security.rbac import Principal, Role

logger = structlog.get_logger(__name__)

_ALGORITHM = "HS256"


def _signing_key(settings: Settings) -> str:
    if settings.jwt_secret_key is None:
        raise ConfigurationError(
            "JWT_SECRET_KEY is not set. Generate one (e.g. `openssl rand -hex 32`) and add it "
            "to .env before issuing or verifying tokens — see docs/SETUP.md."
        )
    return settings.jwt_secret_key.get_secret_value()


def issue_access_token(principal: Principal, *, settings: Settings) -> str:
    """Sign a token carrying `principal`'s identity and role as claims."""
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": principal.username,
        "role": principal.role.value,
        "iat": now,
        "exp": now + timedelta(minutes=settings.jwt_expire_minutes),
    }
    return jwt.encode(payload, _signing_key(settings), algorithm=_ALGORITHM)


def decode_access_token(token: str, *, settings: Settings) -> Principal:
    """Verify signature and expiry, then reconstruct the `Principal` from the token's claims.

    Every failure mode — bad signature, expired, malformed payload, an unrecognized role —
    collapses to the same `AuthenticationError`. Reporting *which* check failed would help an
    attacker narrow down how to forge a token; a caller only ever needs to know "log in again".
    """
    try:
        payload = jwt.decode(token, _signing_key(settings), algorithms=[_ALGORITHM])
        return Principal(username=payload["sub"], role=Role(payload["role"]))
    except (jwt.PyJWTError, KeyError, ValueError) as exc:
        logger.warning("jwt_verification_failed", error=str(exc))
        raise AuthenticationError("Invalid or expired token.") from exc
