"""Tests for the settings model — in particular the rerank-budget cost guard.

docs/DECISIONS.md §4 requires the reranking budget to stay strictly below Pinecone Starter's
free-tier ceiling of 500 requests/month. `Settings` enforces that as a validator rather than
leaving it as documentation only, so a misconfigured `.env` fails fast at startup instead of
risking a billed request months later. This test is what keeps that guard from silently
regressing if the field is ever refactored.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.app.core.config import Settings


def test_default_settings_load_without_an_env_file() -> None:
    """Cycle 0 must boot with no `.env` file and no external accounts configured yet."""
    settings = Settings(_env_file=None)

    assert settings.environment == "development"
    assert settings.pinecone_api_key is None
    assert settings.langsmith_api_key is None
    assert settings.jwt_secret_key is None


@pytest.mark.parametrize("budget", [500, 501, 10_000])
def test_rerank_budget_at_or_above_free_tier_ceiling_is_rejected(budget: int) -> None:
    with pytest.raises(ValidationError, match="Pinecone Starter"):
        Settings(_env_file=None, rerank_monthly_budget=budget)


def test_rerank_budget_below_free_tier_ceiling_is_accepted() -> None:
    settings = Settings(_env_file=None, rerank_monthly_budget=499)

    assert settings.rerank_monthly_budget == 499
