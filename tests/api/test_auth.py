"""Integration tests for the login and identity endpoints.

The rate limiter's Postgres-backed primitive (`_consume_token`) is monkeypatched to a fixed
outcome rather than hit for real — the same "test the wiring, not Postgres" split used in
`tests/core/security/test_rate_limit.py`. A real login-through-rate-limited-request flow
against a live database was exercised manually; see docs/PROGRESS.md's session log.

`JWT_SECRET_KEY` is set via `monkeypatch.setenv` + `get_settings.cache_clear()` rather than a
FastAPI dependency override — `core/config.py`'s docstring on `get_settings` names this as the
intended way for a test to force a fresh settings read.
"""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from backend.app.core.config import get_settings
from backend.app.core.security import rate_limit
from backend.app.core.security.rate_limit import _RefillResult
from backend.app.main import create_app


@pytest.fixture(autouse=True)
def _configure_jwt_secret(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("JWT_SECRET_KEY", "test-signing-key-for-auth-endpoint-tests")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _allow_every_rate_limit_check(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        rate_limit,
        "_consume_token",
        AsyncMock(
            return_value=_RefillResult(allowed=True, tokens_after=4.0, retry_after_seconds=None)
        ),
    )


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


def test_login_with_correct_credentials_issues_a_token(client: TestClient) -> None:
    response = client.post(
        "/api/v1/auth/login", json={"username": "viewer", "password": "ViewerPass123!"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["role"] == "viewer"
    assert body["token_type"] == "bearer"
    assert body["access_token"]


def test_login_with_wrong_password_is_rejected(client: TestClient) -> None:
    response = client.post("/api/v1/auth/login", json={"username": "viewer", "password": "wrong"})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_failed"


def test_login_with_unknown_username_gets_the_same_error_as_wrong_password(
    client: TestClient,
) -> None:
    """Same status and code either way — distinguishing "no such user" from "wrong password"
    would let a caller enumerate valid usernames."""
    response = client.post("/api/v1/auth/login", json={"username": "ghost", "password": "x"})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_failed"


def test_whoami_without_a_token_is_rejected(client: TestClient) -> None:
    response = client.get("/api/v1/auth/me")

    # HTTPBearer's own `auto_error` raises this — a plain `{"detail": ...}` via Starlette's
    # default HTTPException handler, not our AppError envelope, because it fires before our
    # JWT verification code ever runs.
    assert response.status_code == 401
    assert response.json() == {"detail": "Not authenticated"}


def test_whoami_with_a_valid_token_returns_the_principal(client: TestClient) -> None:
    login_response = client.post(
        "/api/v1/auth/login", json={"username": "analyst", "password": "AnalystPass123!"}
    )
    token = login_response.json()["access_token"]

    response = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.json() == {"username": "analyst", "role": "analyst"}


def test_whoami_with_a_malformed_token_is_rejected(client: TestClient) -> None:
    response = client.get("/api/v1/auth/me", headers={"Authorization": "Bearer not-a-real-token"})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_failed"


def test_whoami_when_rate_limited_returns_a_graceful_429(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    login_response = client.post(
        "/api/v1/auth/login", json={"username": "viewer", "password": "ViewerPass123!"}
    )
    token = login_response.json()["access_token"]
    monkeypatch.setattr(
        rate_limit,
        "_consume_token",
        AsyncMock(
            return_value=_RefillResult(allowed=False, tokens_after=0.0, retry_after_seconds=3.0)
        ),
    )

    response = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 429
    body = response.json()
    assert body["error"]["code"] == "rate_limit_exceeded"
    assert body["error"]["details"] == {"retry_after_seconds": 3.0}
