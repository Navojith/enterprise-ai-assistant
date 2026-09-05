"""Tests for the settings model — in particular the rerank-budget cost guard.

docs/DECISIONS.md §4 requires the reranking budget to stay strictly below Pinecone Starter's
free-tier ceiling of 500 requests/month. `Settings` enforces that as a validator rather than
leaving it as documentation only, so a misconfigured `.env` fails fast at startup instead of
risking a billed request months later. This test is what keeps that guard from silently
regressing if the field is ever refactored.
"""

from __future__ import annotations

from pathlib import Path

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


def test_database_url_is_assembled_from_the_granular_db_fields() -> None:
    settings = Settings(
        _env_file=None,
        db_host="db.internal",
        db_port=6543,
        db_name="assistant",
        db_user="app",
        db_password="hunter2",
    )

    assert settings.database_url == "postgresql+psycopg://app:hunter2@db.internal:6543/assistant"


def test_database_url_percent_encodes_special_characters_in_credentials() -> None:
    """A `:` or `@` in a password would otherwise be parsed as a DSN delimiter instead of part
    of the credential — a real risk since generated or rotated passwords are not guaranteed to
    avoid those characters."""
    settings = Settings(_env_file=None, db_user="a@b", db_password="p:w@rd")

    assert settings.database_url.startswith("postgresql+psycopg://a%40b:p%3Aw%40rd@")


def test_psycopg_dsn_strips_the_sqlalchemy_driver_suffix() -> None:
    """`psycopg.AsyncConnection.connect()` and `AsyncPostgresSaver` (Cycle 3) speak a plain
    libpq conninfo string, not SQLAlchemy's `dialect+driver://` syntax — see `api/v1/health.py`
    and `main.py`'s checkpointer setup, both of which use this property so the two connection
    strings can never drift apart."""
    settings = Settings(
        _env_file=None,
        db_host="db.internal",
        db_port=6543,
        db_name="assistant",
        db_user="app",
        db_password="hunter2",
    )

    assert settings.psycopg_dsn == "postgresql://app:hunter2@db.internal:6543/assistant"
    assert (
        settings.database_url
        == f"postgresql+psycopg://{settings.psycopg_dsn.removeprefix('postgresql://')}"
    )


def test_blank_env_values_fall_back_to_defaults_instead_of_becoming_empty(tmp_path: Path) -> None:
    """A variable present but left blank in `.env` (`DB_HOST=`) must behave like an absent one,
    not like an explicit empty string — otherwise an unfilled `.env` produces a broken DSN and
    silently turns `Optional` secrets into `SecretStr("")` instead of `None`."""
    env_file = tmp_path / ".env"
    env_file.write_text("DB_HOST=\nDB_PORT=\nPINECONE_API_KEY=\n", encoding="utf-8")

    settings = Settings(_env_file=env_file)

    assert settings.db_host == "localhost"
    assert settings.db_port == 5433
    assert settings.pinecone_api_key is None
