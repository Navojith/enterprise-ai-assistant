"""Structured JSON logging with request-scoped correlation IDs.

Every log line — from application code and from third-party libraries using plain stdlib
`logging` — is rendered as a single JSON object carrying `timestamp`, `level`, `event`,
`correlation_id`, and whatever key-value context a call site binds. The correlation ID ties a
backend log line to the LangSmith trace run for the same request once tracing lands in
Cycle 3, and to the request/response pair in the Agent Activity Panel.

This wires structlog's processors into stdlib `logging` (the pattern documented at
https://www.structlog.org/en/stable/standard-library.html) rather than using structlog
standalone, so uvicorn's own access/error logs are captured in the same JSON format instead
of being a differently-shaped stream a log aggregator has to handle specially.
"""

from __future__ import annotations

import logging
import sys
import uuid
from typing import cast

import structlog

from backend.app.core.config import Settings

CORRELATION_ID_HEADER = "X-Request-ID"


def new_correlation_id() -> str:
    return uuid.uuid4().hex


def bind_correlation_id(correlation_id: str | None = None) -> str:
    """Bind a correlation id to the current task's log context, generating one if absent.

    Starlette runs each request's middleware chain and handler inside a single asyncio Task,
    so contextvars bound here are isolated per request without any manual cleanup at the end
    of the request — the next task simply starts with an empty context.
    """
    correlation_id = correlation_id or new_correlation_id()
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(correlation_id=correlation_id)
    return correlation_id


def get_correlation_id() -> str | None:
    value = structlog.contextvars.get_contextvars().get("correlation_id")
    return value if isinstance(value, str) else None


def configure_logging(settings: Settings) -> None:
    """Configure structlog + stdlib logging. Call once, at process startup (see `main.py`)."""
    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    structlog.configure(
        processors=[*shared_processors, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # Console rendering in development for human readability; JSON everywhere else, because
    # that is what a log aggregator (and the correlation-id-to-trace join) actually needs.
    renderer: structlog.types.Processor = (
        structlog.dev.ConsoleRenderer()
        if settings.environment == "development"
        else structlog.processors.JSONRenderer()
    )
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, renderer],
    )

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(formatter)
    root_logger = logging.getLogger()
    root_logger.handlers = [handler]
    root_logger.setLevel(settings.log_level)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    # structlog.get_logger() is typed to return Any (it's a lazy proxy resolved at first use);
    # `wrapper_class=structlog.stdlib.BoundLogger` in configure_logging() is what actually
    # makes that true at runtime, so the cast documents a guarantee mypy cannot see itself.
    return cast(structlog.stdlib.BoundLogger, structlog.get_logger(name))
