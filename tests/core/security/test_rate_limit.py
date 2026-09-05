"""Tests for the token-bucket rate limiter.

Split to match the module's own split: `_apply_refill` is pure math, tested directly with
plain `datetime`s and no database or event loop. `RateLimiter.acquire`'s wiring — does it
raise `RateLimitExceededError` with the right `retry_after_seconds`, does it stay silent on
success — is tested with `_consume_token` monkeypatched, the same pattern
`tests/retrieval/test_reranker.py` uses for `_increment_monthly_usage`: these are unit tests
of the limiter's decision logic, not an integration test of Postgres locking, which was
exercised manually against a live database.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from backend.app.core.errors import RateLimitExceededError
from backend.app.core.security import rate_limit
from backend.app.core.security.rate_limit import RateLimiter, _apply_refill, _RefillResult


class TestApplyRefill:
    def test_a_full_bucket_allows_and_consumes_one_token(self) -> None:
        now = datetime(2026, 1, 1, tzinfo=UTC)
        result = _apply_refill(
            stored_tokens=5.0, last_updated=now, now=now, capacity=5, refill_per_sec=1.0
        )
        assert result.allowed
        assert result.tokens_after == 4.0
        assert result.retry_after_seconds is None

    def test_an_empty_bucket_with_no_elapsed_time_is_denied(self) -> None:
        now = datetime(2026, 1, 1, tzinfo=UTC)
        result = _apply_refill(
            stored_tokens=0.0, last_updated=now, now=now, capacity=5, refill_per_sec=1.0
        )
        assert not result.allowed
        assert result.tokens_after == 0.0
        assert result.retry_after_seconds == pytest.approx(1.0)

    def test_partial_refill_over_elapsed_time_is_applied_before_the_decision(self) -> None:
        last_updated = datetime(2026, 1, 1, tzinfo=UTC)
        now = last_updated + timedelta(seconds=3)
        # 0 stored + 3s * 0.5/s = 1.5 tokens available -> allowed, 0.5 left after consuming one.
        result = _apply_refill(
            stored_tokens=0.0, last_updated=last_updated, now=now, capacity=5, refill_per_sec=0.5
        )
        assert result.allowed
        assert result.tokens_after == pytest.approx(0.5)

    def test_refill_is_capped_at_capacity_not_allowed_to_accumulate_unbounded(self) -> None:
        last_updated = datetime(2026, 1, 1, tzinfo=UTC)
        now = last_updated + timedelta(hours=1)  # far more than enough to overflow capacity
        result = _apply_refill(
            stored_tokens=0.0, last_updated=last_updated, now=now, capacity=5, refill_per_sec=1.0
        )
        assert result.allowed
        assert result.tokens_after == 4.0  # capped at 5, then one consumed

    def test_exact_threshold_of_one_token_is_allowed(self) -> None:
        now = datetime(2026, 1, 1, tzinfo=UTC)
        result = _apply_refill(
            stored_tokens=1.0, last_updated=now, now=now, capacity=5, refill_per_sec=1.0
        )
        assert result.allowed
        assert result.tokens_after == 0.0

    def test_just_under_one_token_is_denied(self) -> None:
        now = datetime(2026, 1, 1, tzinfo=UTC)
        result = _apply_refill(
            stored_tokens=0.999, last_updated=now, now=now, capacity=5, refill_per_sec=1.0
        )
        assert not result.allowed
        assert result.retry_after_seconds == pytest.approx(0.001)


class TestRateLimiterAcquire:
    async def test_an_allowed_outcome_does_not_raise(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            rate_limit,
            "_consume_token",
            AsyncMock(
                return_value=_RefillResult(allowed=True, tokens_after=4.0, retry_after_seconds=None)
            ),
        )
        limiter = RateLimiter(capacity=5, refill_per_sec=1.0)

        await limiter.acquire("viewer")  # must not raise

    async def test_a_denied_outcome_raises_with_the_retry_after_seconds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            rate_limit,
            "_consume_token",
            AsyncMock(
                return_value=_RefillResult(
                    allowed=False, tokens_after=0.2, retry_after_seconds=2.345
                )
            ),
        )
        limiter = RateLimiter(capacity=5, refill_per_sec=1.0)

        with pytest.raises(RateLimitExceededError) as exc_info:
            await limiter.acquire("viewer")

        assert exc_info.value.details == {"retry_after_seconds": 2.35}

    async def test_acquire_is_scoped_per_username(self, monkeypatch: pytest.MonkeyPatch) -> None:
        consume = AsyncMock(
            return_value=_RefillResult(allowed=True, tokens_after=4.0, retry_after_seconds=None)
        )
        monkeypatch.setattr(rate_limit, "_consume_token", consume)
        limiter = RateLimiter(capacity=5, refill_per_sec=1.0)

        await limiter.acquire("viewer")
        await limiter.acquire("admin")

        called_usernames = [call.args[0] for call in consume.call_args_list]
        assert called_usernames == ["viewer", "admin"]
