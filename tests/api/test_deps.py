"""Tests for the `require_permission` dependency chain.

Builds a throwaway route behind `require_permission(...)` — the same "real handlers, one
route per case" approach `tests/core/test_errors.py` uses — because `api/v1/auth.py`'s own
endpoints don't happen to gate anything on a specific permission (only on being
authenticated), so this is the only place the full
`current_principal -> enforce_rate_limit -> require_permission` composition gets exercised.
"""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import AsyncMock

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from backend.app.api.deps import require_permission
from backend.app.api.v1.auth import router as auth_router
from backend.app.core.config import get_settings
from backend.app.core.errors import register_exception_handlers
from backend.app.core.security import rate_limit
from backend.app.core.security.rate_limit import _RefillResult
from backend.app.core.security.rbac import Permission, Principal

# Built once at module scope, not inline in a route's `Depends(...)` default — ruff's B008
# flags a function call in an argument default even when it produces a dependency callable,
# and the fix is the same either way: construct it once, not on every request.
_require_admin_tools = require_permission(Permission.ADMIN_TOOLS)


def _build_app() -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(auth_router, prefix="/api/v1")

    @app.get("/api/v1/admin-only")
    async def _admin_only(
        principal: Principal = Depends(_require_admin_tools),
    ) -> dict[str, str]:
        return {"username": principal.username}

    return app


@pytest.fixture(autouse=True)
def _configure_jwt_secret(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("JWT_SECRET_KEY", "test-signing-key-for-deps-endpoint-tests-32chars")
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
    return TestClient(_build_app())


def _token_for(client: TestClient, username: str, password: str) -> str:
    response = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    token: str = response.json()["access_token"]
    return token


def test_viewer_is_forbidden_from_an_admin_only_route(client: TestClient) -> None:
    token = _token_for(client, "viewer", "ViewerPass123!")

    response = client.get("/api/v1/admin-only", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 403
    body = response.json()
    assert body["error"]["code"] == "not_authorized"
    assert body["error"]["details"] == {"role": "viewer", "required_permission": "admin_tools"}


def test_administrator_is_allowed_on_an_admin_only_route(client: TestClient) -> None:
    token = _token_for(client, "admin", "AdminPass123!")

    response = client.get("/api/v1/admin-only", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.json() == {"username": "admin"}


def test_authorization_is_checked_only_after_authentication_succeeds(client: TestClient) -> None:
    """An invalid token fails as `authentication_failed`, not `not_authorized` — the caller
    never even has a role to be checked against a permission yet."""
    response = client.get("/api/v1/admin-only", headers={"Authorization": "Bearer garbage-token"})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_failed"
