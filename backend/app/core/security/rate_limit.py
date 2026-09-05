"""Async, Postgres-backed per-user token-bucket rate limiter.

ASSESSMENT.md requires Token Bucket rate limiting, per-user, with configurable thresholds and
graceful error handling. The design splits into two layers, mirroring
`retrieval/reranker.py`'s split between an atomic database primitive and the business logic
layered on top of it:

- `_apply_refill` is the token-bucket math — pure, synchronous, no I/O. Every edge case (an
  empty bucket, a partial refill, a refill that would exceed capacity, the exact threshold at
  1.0 token) is a plain unit test with no database or event loop involved.
- `_consume_token` is the async shell: it locks the caller's row for the rest of its
  transaction (`SELECT ... FOR UPDATE`), applies the pure function, and persists the result in
  the same transaction. The lock matters because two concurrent requests from the same user
  must not both read the same pre-consumption token count — without it, both could be
  admitted when only one token remained.

State is persisted rather than kept in-process so a bucket survives an app restart, unlike a
plain in-memory dict. True multi-instance correctness would need a shared store like Redis
(docs/ASSUMPTIONS_AND_TRADEOFFS.md assumption 2 — single-node deployment is assumed); Postgres
is enough here because the deployment target is one process.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import structlog
from sqlalchemy import DateTime, Float, String, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.db import Base, session_scope
from backend.app.core.errors import RateLimitExceededError

logger = structlog.get_logger(__name__)


class RateLimitBucket(Base):
    """One row per user: the token bucket's persisted state."""

    __tablename__ = "rate_limit_buckets"

    username: Mapped[str] = mapped_column(String(255), primary_key=True)
    tokens: Mapped[float] = mapped_column(Float, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


@dataclass(frozen=True)
class _RefillResult:
    allowed: bool
    tokens_after: float
    retry_after_seconds: float | None


def _apply_refill(
    *,
    stored_tokens: float,
    last_updated: datetime,
    now: datetime,
    capacity: int,
    refill_per_sec: float,
) -> _RefillResult:
    """Refill `stored_tokens` for the time elapsed since `last_updated` (capped at
    `capacity`), then attempt to consume one token.

    Persisting `tokens_after` and `now` even when denied is deliberate and safe, not just
    convenient: because refill is linear, recomputing from an older `last_updated` over a
    longer elapsed window gives the same result as recomputing from a persisted-but-unchanged
    intermediate point (unless capacity was reached in between, but a denial means the bucket
    was below one token — nowhere near the cap). This means the caller never needs a
    conditional write.
    """
    elapsed = max(0.0, (now - last_updated).total_seconds())
    tokens = min(float(capacity), stored_tokens + elapsed * refill_per_sec)
    if tokens < 1.0:
        retry_after = (1.0 - tokens) / refill_per_sec
        return _RefillResult(allowed=False, tokens_after=tokens, retry_after_seconds=retry_after)
    return _RefillResult(allowed=True, tokens_after=tokens - 1.0, retry_after_seconds=None)


async def _consume_token(username: str, *, capacity: int, refill_per_sec: float) -> _RefillResult:
    """Load-lock-compute-persist a user's bucket in one transaction.

    `INSERT ... ON CONFLICT DO NOTHING` seeds a full bucket for a first-ever caller before the
    locking `SELECT`, so that select always finds exactly one row to lock instead of racing
    two concurrent first requests for the same brand-new user into both trying to insert the
    same primary key.
    """
    now = datetime.now(UTC)
    async with session_scope() as session:
        await session.execute(
            pg_insert(RateLimitBucket)
            .values(username=username, tokens=float(capacity), updated_at=now)
            .on_conflict_do_nothing(index_elements=[RateLimitBucket.username])
        )
        result = await session.execute(
            select(RateLimitBucket).where(RateLimitBucket.username == username).with_for_update()
        )
        bucket = result.scalar_one()

        outcome = _apply_refill(
            stored_tokens=bucket.tokens,
            last_updated=bucket.updated_at,
            now=now,
            capacity=capacity,
            refill_per_sec=refill_per_sec,
        )
        bucket.tokens = outcome.tokens_after
        bucket.updated_at = now
        return outcome


class RateLimiter:
    """Per-user token bucket, configured from `Settings.rate_limit_capacity` and
    `Settings.rate_limit_refill_per_sec` (see `api/deps.py` for the request-scoped wiring)."""

    def __init__(self, *, capacity: int, refill_per_sec: float) -> None:
        self._capacity = capacity
        self._refill_per_sec = refill_per_sec

    async def acquire(self, username: str) -> None:
        """Consume one token from `username`'s bucket, or raise `RateLimitExceededError`
        with `retry_after_seconds` in `details` — the graceful-429 behaviour ASSESSMENT.md's
        rate-limiting section requires."""
        outcome = await _consume_token(
            username, capacity=self._capacity, refill_per_sec=self._refill_per_sec
        )
        if not outcome.allowed:
            retry_after = round(outcome.retry_after_seconds or 0.0, 2)
            logger.warning(
                "rate_limit_exceeded", username=username, retry_after_seconds=retry_after
            )
            raise RateLimitExceededError(
                f"Rate limit exceeded for {username!r}. Try again shortly.",
                details={"retry_after_seconds": retry_after},
            )
        logger.debug(
            "rate_limit_token_consumed",
            username=username,
            tokens_remaining=round(outcome.tokens_after, 2),
        )
