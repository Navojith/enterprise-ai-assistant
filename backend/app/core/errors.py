"""Application exception hierarchy and FastAPI error handlers.

Every domain error the assistant raises deliberately inherits from `AppError`, which carries
a machine-readable `code`, a human-readable `message`, the HTTP status it maps to, and
optional structured `details`. The handlers registered here turn any `AppError` into one
consistent JSON envelope instead of leaking stack traces or ad-hoc shapes to the client, and
log it at a severity that matches whether it was expected (4xx) or not (5xx).

Category base classes exist for every failure mode ASSESSMENT.md's "Error Handling" section
calls out by name (LLM, vector store, tool execution, MCP, rate limiting) plus authorization,
so later cycles subclass one of these rather than inventing a new shape each time. See
docs/ARCHITECTURE.md "Failure and degradation" for how each maps to graceful degradation, and
docs/DECISIONS.md section 6 for why authorization errors are raised from application code and
never inferred from model output.
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from backend.app.core.logging import get_correlation_id

logger = structlog.get_logger(__name__)


class ErrorDetail(BaseModel):
    code: str
    message: str
    details: dict[str, Any] | None = None


class ErrorResponse(BaseModel):
    error: ErrorDetail
    correlation_id: str | None = None


class AppError(Exception):
    """Base class for every error the assistant raises on purpose.

    Raising `AppError` (or a subclass) is a *handled* failure: `app_error_handler` converts it
    into a structured response rather than a bare 500. Anything that reaches
    `unhandled_exception_handler` instead is, by definition, a bug, not a modeled failure mode.
    """

    code: str = "internal_error"
    http_status: int = status.HTTP_500_INTERNAL_SERVER_ERROR

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details


class ConfigurationError(AppError):
    """Required configuration is missing or invalid. Raised at startup or first use, never
    silently defaulted, because a silent default here would be a security or cost hole."""

    code = "configuration_error"
    http_status = status.HTTP_500_INTERNAL_SERVER_ERROR


class ValidationFailedError(AppError):
    """A domain-level input failed validation beyond what the request schema already checks
    (for example a tool parameter, or retrieved content that fails a guardrail check)."""

    code = "validation_failed"
    http_status = status.HTTP_422_UNPROCESSABLE_CONTENT


class AuthenticationError(AppError):
    """Credentials are missing, malformed, or do not verify."""

    code = "authentication_failed"
    http_status = status.HTTP_401_UNAUTHORIZED


class AuthorizationError(AppError):
    """The authenticated principal lacks permission for the requested action.

    Raised at the tool-execution boundary and the retrieval access-level filter, reading the
    principal from request-scoped context, never inferred from anything the model emits.
    """

    code = "not_authorized"
    http_status = status.HTTP_403_FORBIDDEN


class RateLimitExceededError(AppError):
    """The caller's token bucket is empty. `details` should carry `retry_after_seconds`."""

    code = "rate_limit_exceeded"
    http_status = status.HTTP_429_TOO_MANY_REQUESTS


class RetrievalError(AppError):
    """The retrieval layer (Pinecone dense, sparse, or rerank) failed or degraded."""

    code = "retrieval_error"
    http_status = status.HTTP_502_BAD_GATEWAY


class VectorStoreUnavailableError(RetrievalError):
    """Pinecone is unreachable. The graph should answer with an explicit evidence caveat
    instead of crashing, see docs/ARCHITECTURE.md "Failure and degradation"."""

    code = "vector_store_unavailable"
    http_status = status.HTTP_503_SERVICE_UNAVAILABLE


class LLMError(AppError):
    """The local model failed to respond, load, or produce schema-conformant output."""

    code = "llm_error"
    http_status = status.HTTP_502_BAD_GATEWAY


class LLMTimeoutError(LLMError):
    code = "llm_timeout"
    http_status = status.HTTP_504_GATEWAY_TIMEOUT


class LLMUnavailableError(LLMError):
    code = "llm_unavailable"
    http_status = status.HTTP_503_SERVICE_UNAVAILABLE


class ToolExecutionError(AppError):
    """A tool call failed, timed out, or was rejected before it ran."""

    code = "tool_execution_error"
    http_status = status.HTTP_502_BAD_GATEWAY


class ToolTimeoutError(ToolExecutionError):
    code = "tool_timeout"
    http_status = status.HTTP_504_GATEWAY_TIMEOUT


class ToolNotPermittedError(ToolExecutionError, AuthorizationError):
    """The principal's role does not permit this tool.

    Deliberately both a tool-execution error and an authorization error: it is raised at the
    execution boundary (the registry re-checks on every call, not only at bind time) even if
    the tool was somehow offered to the model, satisfying the requirement that the agent must
    not be able to bypass authorization. See docs/DECISIONS.md section 6.
    """

    code = "tool_not_permitted"
    http_status = status.HTTP_403_FORBIDDEN


class MCPError(AppError):
    """The MCP client or server failed."""

    code = "mcp_error"
    http_status = status.HTTP_502_BAD_GATEWAY


class MCPUnavailableError(MCPError):
    """The MCP server is unreachable. The supervisor should route around the tool rather
    than fail the whole turn, see docs/ARCHITECTURE.md "Failure and degradation"."""

    code = "mcp_unavailable"
    http_status = status.HTTP_503_SERVICE_UNAVAILABLE


class GraphUnavailableError(AppError):
    """The compiled LangGraph graph or its `AsyncPostgresSaver` checkpointer failed to
    initialize at startup (e.g. Postgres was unreachable) — `main.py`'s lifespan degrades this
    to a warning log and a `None` on `app.state` rather than crashing the whole process, so
    liveness/readiness stay servable; `api/v1/chat.py` raises this instead of an `AttributeError`
    the first time a chat request actually needs the graph."""

    code = "graph_unavailable"
    http_status = status.HTTP_503_SERVICE_UNAVAILABLE


class GuardrailViolationError(AppError):
    """A guardrail rejected the input or the output: prompt injection, a hallucinated
    citation, or a brand/safety violation."""

    code = "guardrail_violation"
    http_status = status.HTTP_400_BAD_REQUEST


class SandboxViolationError(AppError):
    """RLM-generated Python failed the AST allowlist or the sandbox's runtime limits
    (wall-clock timeout, recursion depth, fan-out cap)."""

    code = "sandbox_violation"
    http_status = status.HTTP_400_BAD_REQUEST


def _error_response(exc: AppError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.http_status,
        content=ErrorResponse(
            error=ErrorDetail(code=exc.code, message=exc.message, details=exc.details),
            correlation_id=get_correlation_id(),
        ).model_dump(),
    )


async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    is_server_error = exc.http_status >= status.HTTP_500_INTERNAL_SERVER_ERROR
    log = logger.error if is_server_error else logger.warning
    log(
        "app_error",
        code=exc.code,
        path=request.url.path,
        details=exc.details,
        exc_info=is_server_error,
    )
    return _error_response(exc)


async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    logger.warning("request_validation_error", path=request.url.path, errors=exc.errors())
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        content=ErrorResponse(
            error=ErrorDetail(
                code="request_validation_error",
                message="Request failed validation.",
                details={"errors": exc.errors()},
            ),
            correlation_id=get_correlation_id(),
        ).model_dump(),
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.error("unhandled_exception", path=request.url.path, exc_info=True)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content=ErrorResponse(
            error=ErrorDetail(code="internal_error", message="An unexpected error occurred."),
            correlation_id=get_correlation_id(),
        ).model_dump(),
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Register handlers most-specific-first for readability.

    FastAPI/Starlette actually dispatches by exact exception type and then walks the MRO, so
    registration order does not itself determine precedence, but grouping the calls this way
    keeps the intent legible.
    """
    # Starlette's stub types `add_exception_handler`'s callback as `Callable[[Request,
    # Exception], ...]` regardless of which exception class is registered, so a handler typed
    # to its own narrower exception (correct, and enforced at runtime by Starlette's dispatch,
    # which looks up the handler by the registered class) reads as a parameter-contravariance
    # violation to mypy. This is a stub limitation, not a real type-safety bug.
    app.add_exception_handler(RequestValidationError, validation_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(AppError, app_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, unhandled_exception_handler)
