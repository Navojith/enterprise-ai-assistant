"""Tests for JWT issuance and verification.

`Settings(_env_file=None, jwt_secret_key=...)` builds a settings instance in isolation from
whatever `.env` happens to be on disk — the same pattern `tests/retrieval/test_reranker.py`
uses — so these tests exercise the signing/verification logic itself, not the environment.
"""

from __future__ import annotations

from typing import Any

import jwt as pyjwt
import pytest
from freezegun import freeze_time
from pydantic import SecretStr

from backend.app.core.config import Settings
from backend.app.core.errors import AuthenticationError, ConfigurationError
from backend.app.core.security.jwt import decode_access_token, issue_access_token
from backend.app.core.security.rbac import Principal, Role


def _settings(**overrides: Any) -> Settings:
    overrides.setdefault("jwt_secret_key", SecretStr("test-signing-key-at-least-32-bytes-long"))
    return Settings(_env_file=None, **overrides)


def test_issued_token_decodes_back_to_the_same_principal() -> None:
    settings = _settings()
    principal = Principal(username="analyst", role=Role.ANALYST)

    token = issue_access_token(principal, settings=settings)
    decoded = decode_access_token(token, settings=settings)

    assert decoded == principal


def test_issuing_without_a_signing_key_raises_configuration_error() -> None:
    settings = _settings(jwt_secret_key=None)

    with pytest.raises(ConfigurationError, match="JWT_SECRET_KEY"):
        issue_access_token(Principal(username="v", role=Role.VIEWER), settings=settings)


def test_a_token_signed_with_a_different_key_is_rejected() -> None:
    settings = _settings()
    other_settings = _settings(
        jwt_secret_key=SecretStr("a-different-signing-key-at-least-32-bytes")
    )
    token = issue_access_token(Principal(username="v", role=Role.VIEWER), settings=other_settings)

    with pytest.raises(AuthenticationError):
        decode_access_token(token, settings=settings)


def test_an_expired_token_is_rejected() -> None:
    settings = _settings(jwt_expire_minutes=1)
    with freeze_time("2026-01-01 00:00:00"):
        token = issue_access_token(Principal(username="v", role=Role.VIEWER), settings=settings)

    with freeze_time("2026-01-01 00:05:00"), pytest.raises(AuthenticationError):
        decode_access_token(token, settings=settings)


def test_a_token_with_an_unrecognized_role_claim_is_rejected() -> None:
    settings = _settings()
    forged_payload = {"sub": "someone", "role": "super_admin"}
    forged_token = pyjwt.encode(
        forged_payload, "test-signing-key-at-least-32-bytes-long", algorithm="HS256"
    )

    with pytest.raises(AuthenticationError):
        decode_access_token(forged_token, settings=settings)


def test_a_malformed_token_is_rejected() -> None:
    settings = _settings()

    with pytest.raises(AuthenticationError):
        decode_access_token("not-a-jwt-at-all", settings=settings)
