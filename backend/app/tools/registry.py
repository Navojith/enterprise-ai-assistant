"""RBAC-aware tool registry: the one place a tool name and a set of arguments become an
executed call, or a rejected one.

`docs/DECISIONS.md` §6 requires authorization at "the tool-execution boundary", never in a
prompt — this module *is* that boundary. It enforces the role check **twice**, deliberately:

1. **Bind time** (`available_to`) — narrows what a caller's role even sees on offer. The
   Supervisor's tool-choice prompt (`agents/nodes/tools.py`) is built from this list, so a
   Viewer's routing decision is never even shown an analytics or MCP tool name to choose.
2. **Execution boundary** (`execute`) — re-checks independently of whatever narrowed the
   candidate list, using nothing but the `Principal` passed in. This is what makes the
   assessment's "the agent should not be able to bypass authorization" requirement true even if
   bind-time filtering is buggy, or a future caller invokes `execute` directly without going
   through step 1 at all (exactly how `docs/DELIVERY_PLAN.md`'s acceptance criterion 3 is
   verified: a Viewer principal handed straight to `execute("python_analysis", ...)` denied
   right there, with no LLM or graph involved).

Tool parameters are validated against each `ToolSpec`'s own Pydantic model before the handler
ever runs — ASSESSMENT.md's "Validate: tool parameters" requirement — so a handler can assume
well-typed input the same way a FastAPI route handler can.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

import structlog
from pydantic import BaseModel, ValidationError

from backend.app.core.errors import (
    MCPError,
    ToolExecutionError,
    ToolNotPermittedError,
    ToolTimeoutError,
)
from backend.app.core.errors import ValidationFailedError as AppValidationFailedError
from backend.app.core.security.rbac import Permission, Principal

logger = structlog.get_logger(__name__)


class ToolResult(BaseModel):
    """What every tool hands back, regardless of what it does internally — a short
    human-readable `summary` for the Response node and the Agent Activity Panel to show
    directly, plus optional structured `data` for anything that wants to inspect it further."""

    summary: str
    data: Any = None


ToolHandler = Callable[[Principal, BaseModel], Awaitable[ToolResult]]


@dataclass(frozen=True)
class ToolSpec:
    """One registrable tool. `params_schema` doubles as the validation contract and, via
    `.model_json_schema()`, the description handed to the LLM when it is asked to fill in a
    chosen tool's arguments (`agents/nodes/tools.py`) — one schema, not two definitions that
    could drift apart."""

    name: str
    description: str
    required_permission: Permission
    params_schema: type[BaseModel]
    handler: ToolHandler
    timeout_seconds: float = 10.0


class ToolRegistry:
    """Holds every tool the process knows about and is the sole path to running one."""

    def __init__(self, specs: Sequence[ToolSpec]) -> None:
        by_name = {spec.name: spec for spec in specs}
        if len(by_name) != len(specs):
            raise ValueError("Duplicate tool name in registry construction.")
        self._specs = by_name

    def available_to(self, principal: Principal) -> list[ToolSpec]:
        """Bind-time filter — layer 1 of the module docstring. Order matches construction
        order, which matters only for prompt readability, not for correctness."""
        return [
            spec
            for spec in self._specs.values()
            if principal.has_permission(spec.required_permission)
        ]

    async def execute(
        self, name: str, arguments: dict[str, Any], *, principal: Principal
    ) -> ToolResult:
        spec = self._specs.get(name)
        if spec is None:
            raise ToolExecutionError(f"Unknown tool {name!r}.", details={"tool": name})

        # Layer 2 — the execution-boundary re-check. Independent of `available_to`: even if a
        # caller skipped bind-time filtering entirely, this line is what actually stops the call.
        if not principal.has_permission(spec.required_permission):
            logger.warning(
                "tool_denied",
                tool=name,
                role=principal.role.value,
                required_permission=spec.required_permission.value,
            )
            raise ToolNotPermittedError(
                f"Role {principal.role.value!r} does not grant "
                f"{spec.required_permission.value!r}, required for tool {name!r}.",
                details={
                    "tool": name,
                    "role": principal.role.value,
                    "required_permission": spec.required_permission.value,
                },
            )

        try:
            params = spec.params_schema.model_validate(arguments)
        except ValidationError as exc:
            raise AppValidationFailedError(
                f"Invalid arguments for tool {name!r}.",
                details={"tool": name, "errors": exc.errors()},
            ) from exc

        logger.info("tool_call_started", tool=name, role=principal.role.value)
        try:
            result = await asyncio.wait_for(
                spec.handler(principal, params), timeout=spec.timeout_seconds
            )
        except TimeoutError as exc:
            logger.warning("tool_call_timed_out", tool=name, timeout_seconds=spec.timeout_seconds)
            raise ToolTimeoutError(
                f"Tool {name!r} exceeded its {spec.timeout_seconds}s timeout.",
                details={"tool": name},
            ) from exc
        except (ToolExecutionError, MCPError):
            # Both are already well-typed `AppError`s a caller can branch on (e.g.
            # `agents/nodes/tools.py` treats `MCPUnavailableError` as "route around the tool",
            # per docs/ARCHITECTURE.md's failure table, not a generic tool failure) — passed
            # through unchanged rather than flattened into `ToolExecutionError` below.
            raise
        except Exception as exc:
            logger.warning("tool_call_failed", tool=name, error=str(exc))
            raise ToolExecutionError(
                f"Tool {name!r} failed: {exc}", details={"tool": name}
            ) from exc

        logger.info("tool_call_succeeded", tool=name)
        return result
