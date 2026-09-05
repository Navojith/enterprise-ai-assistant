"""Tests for the liveness/readiness endpoints.

Readiness is tested with the database check monkeypatched rather than against a real Postgres,
so the suite proves the endpoint's own branching (200 when every check is "ok", 503 with the
failing check named otherwise) without requiring Docker to be running in CI. The real check
against a live container is exercised manually via `docs/SETUP.md`'s run instructions.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.app.api.v1 import health as health_module
from backend.app.main import create_app


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


def test_liveness_never_touches_a_dependency(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _fail(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("liveness must not call the database check")

    monkeypatch.setattr(health_module, "_check_database", _fail)

    response = client.get("/api/v1/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readiness_reports_ready_when_the_database_check_passes(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _ok(_database_url: str) -> str:
        return "ok"

    monkeypatch.setattr(health_module, "_check_database", _ok)

    response = client.get("/api/v1/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready", "checks": {"database": "ok"}}


def test_readiness_degrades_instead_of_crashing_when_the_database_is_unreachable(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _unreachable(_database_url: str) -> str:
        return "error: connection refused"

    monkeypatch.setattr(health_module, "_check_database", _unreachable)

    response = client.get("/api/v1/health/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["checks"]["database"] == "error: connection refused"
