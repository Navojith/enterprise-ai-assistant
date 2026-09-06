"""Tests for LangSmith environment-variable wiring and the startup connectivity check.

`Settings(_env_file=None, ...)` builds a settings instance in isolation from whatever `.env`
happens to be on disk — the same pattern `tests/core/security/test_jwt.py` uses. `monkeypatch.
delenv`/`setenv` on the specific `LANGSMITH_*` names keeps these tests independent of whatever
tracing state a developer's own shell happens to have, and restores it afterward automatically.
"""

from __future__ import annotations

import os
from typing import Any

import pytest
from pydantic import SecretStr

from backend.app.core.config import Settings
from backend.app.observability import langsmith as langsmith_module
from backend.app.observability.langsmith import configure_langsmith, verify_langsmith_connectivity

_TRACING_ENV_VARS = ("LANGSMITH_TRACING", "LANGSMITH_API_KEY", "LANGSMITH_PROJECT")


@pytest.fixture(autouse=True)
def _clean_tracing_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _TRACING_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def _settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **overrides)


def test_disabled_tracing_sets_no_environment_variables() -> None:
    configure_langsmith(_settings(langsmith_tracing=False))

    for name in _TRACING_ENV_VARS:
        assert name not in os.environ


def test_enabled_tracing_without_an_api_key_sets_no_environment_variables() -> None:
    configure_langsmith(_settings(langsmith_tracing=True, langsmith_api_key=None))

    for name in _TRACING_ENV_VARS:
        assert name not in os.environ


def test_enabled_tracing_with_a_key_sets_the_variables_the_global_tracer_reads() -> None:
    settings = _settings(
        langsmith_tracing=True,
        langsmith_api_key=SecretStr("test-key"),
        langsmith_project="my-project",
    )

    configure_langsmith(settings)

    assert os.environ["LANGSMITH_TRACING"] == "true"
    assert os.environ["LANGSMITH_API_KEY"] == "test-key"
    assert os.environ["LANGSMITH_PROJECT"] == "my-project"


async def test_connectivity_check_is_a_no_op_when_tracing_is_disabled() -> None:
    settings = _settings(langsmith_tracing=False, langsmith_api_key=SecretStr("test-key"))

    result = await verify_langsmith_connectivity(settings)

    assert result is False


async def test_connectivity_check_fails_closed_but_does_not_raise_on_a_broken_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `Client` itself is monkeypatched, not left to make a real HTTP call — this test is about
    # `verify_langsmith_connectivity`'s own degradation, not LangSmith's API or network
    # conditions, and a real call to a bad key could hang on the SDK's own retry policy.
    def _raise_unauthorized(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("401 Unauthorized")

    monkeypatch.setattr(langsmith_module, "Client", _raise_unauthorized)
    settings = _settings(langsmith_tracing=True, langsmith_api_key=SecretStr("not-a-real-key"))

    result = await verify_langsmith_connectivity(settings)

    assert result is False


async def test_connectivity_check_succeeds_when_the_client_authenticates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeClient:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def list_projects(self, limit: int) -> Any:
            yield object()

    monkeypatch.setattr(langsmith_module, "Client", _FakeClient)
    settings = _settings(langsmith_tracing=True, langsmith_api_key=SecretStr("test-key"))

    result = await verify_langsmith_connectivity(settings)

    assert result is True
