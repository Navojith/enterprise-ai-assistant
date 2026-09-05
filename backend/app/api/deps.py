"""FastAPI dependency-injection wiring for authentication, authorization and rate limiting.

Every endpoint that needs to know "who is calling, and are they allowed to do this" depends on
`current_principal` or `require_permission(...)`, rather than reading the `Authorization`
header or a role string itself. That makes this module the one place request-scoped identity
is constructed — the place to audit when checking that authorization can never be bypassed by
anything upstream of it, including the model (docs/DECISIONS.md §6).

Dependencies compose in a fixed order — `current_principal` -> `enforce_rate_limit` ->
`require_permission` — so an unauthenticated caller gets 401 before its (nonexistent)
identity's bucket is ever touched, and an over-quota caller gets 429 before an authorization
check does any work.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from functools import lru_cache

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from backend.app.core.config import Settings, get_settings
from backend.app.core.security.jwt import decode_access_token
from backend.app.core.security.rate_limit import RateLimiter
from backend.app.core.security.rbac import Permission, Principal

_bearer_scheme = HTTPBearer(
    auto_error=True,
    description="JWT issued by POST /api/v1/auth/login",
)


async def current_principal(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer_scheme),
    settings: Settings = Depends(get_settings),
) -> Principal:
    """Resolve the bearer token into a `Principal` for this request.

    This is the single point where request-scoped identity comes into existence — every
    downstream authorization check (the Cycle 4 tool registry, the retrieval access-level
    filter) is handed the `Principal` this function returns, never a raw token, a role
    string, or anything derived from model output.
    """
    return decode_access_token(credentials.credentials, settings=settings)


@lru_cache(maxsize=1)
def get_rate_limiter() -> RateLimiter:
    """Process-wide limiter instance, built once from settings — same "construct once, reuse
    everywhere" shape as `core/config.get_settings` and `core/db.get_engine`. Bucket *state*
    lives in Postgres (`core/security/rate_limit.RateLimitBucket`), not on this object, so the
    instance being process-local doesn't make the limiter itself process-local."""
    settings = get_settings()
    return RateLimiter(
        capacity=settings.rate_limit_capacity, refill_per_sec=settings.rate_limit_refill_per_sec
    )


async def enforce_rate_limit(
    principal: Principal = Depends(current_principal),
    limiter: RateLimiter = Depends(get_rate_limiter),
) -> Principal:
    """Consume one token from the caller's bucket. Depends on `current_principal` first, so a
    request with no valid token is rejected with 401 before it can consume anything."""
    await limiter.acquire(principal.username)
    return principal


def require_permission(
    permission: Permission,
) -> Callable[[Principal], Awaitable[Principal]]:
    """Dependency factory: `Depends(require_permission(Permission.MCP_TOOLS))` 403s unless the
    caller's role grants `permission`. Layered after `enforce_rate_limit` so authentication and
    rate limiting are both settled before an authorization check runs."""

    async def _check(principal: Principal = Depends(enforce_rate_limit)) -> Principal:
        principal.require_permission(permission)
        return principal

    return _check
