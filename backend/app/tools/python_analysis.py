"""The Python Analysis tool: ASSESSMENT.md's "perform structured analysis on retrieved data",
running on `rlm/sandbox.py`.

Deliberately the same sandbox Cycle 5's RLM planner will execute generated search plans in
(`docs/ARCHITECTURE.md`: "The same sandbox backs the Python Analysis tool, so the one
security-critical component is written and audited once") — this tool is, in effect, a
single-shot, human-or-LLM-authored use of exactly the execution path Cycle 5 will drive
programmatically many times per turn.
"""

from __future__ import annotations

from typing import Any, cast

from pydantic import BaseModel, Field

from backend.app.core.security.rbac import Permission, Principal
from backend.app.rlm.sandbox import run_sandboxed
from backend.app.tools.registry import ToolResult, ToolSpec

TOOL_NAME = "python_analysis"


class PythonAnalysisParams(BaseModel):
    """`code` runs inside `rlm/sandbox.py`'s AST-allowlisted, stripped-builtins environment;
    `data` is the only input it can see, bound to the name `data` in the sandbox's globals — a
    tool call cannot smuggle in anything else."""

    code: str = Field(
        description=(
            "Python code to run. No imports, no dunder attribute access, no file I/O. "
            "The value to return must be assigned to a variable named `result`. "
            "The tool's input, if any, is available as a variable named `data`."
        )
    )
    data: Any = Field(default=None, description="JSON-serializable data the code can analyze.")


def build_python_analysis_tool(*, timeout_seconds: float) -> ToolSpec:
    async def _handle(principal: Principal, raw_params: BaseModel) -> ToolResult:
        params = cast(PythonAnalysisParams, raw_params)
        outcome = await run_sandboxed(
            params.code, injected_globals={"data": params.data}, timeout_seconds=timeout_seconds
        )
        summary = f"Analysis produced: {outcome.result!r}"
        if outcome.stdout.strip():
            summary += f"\nOutput:\n{outcome.stdout.strip()}"
        return ToolResult(summary=summary, data=outcome.result)

    return ToolSpec(
        name=TOOL_NAME,
        description=(
            "Run a short, sandboxed Python snippet to compute or summarize structured data "
            "(counts, groupings, simple statistics). No imports, no file access."
        ),
        required_permission=Permission.ANALYTICS_TOOLS,
        params_schema=PythonAnalysisParams,
        handler=_handle,
        timeout_seconds=timeout_seconds,
    )
