"""The Python Analysis tool: ASSESSMENT.md's "perform structured analysis on retrieved data",
running on `rlm/sandbox.py`.

Deliberately the same sandbox Cycle 5's RLM planner will execute generated search plans in
(`docs/ARCHITECTURE.md`: "The same sandbox backs the Python Analysis tool, so the one
security-critical component is written and audited once") — this tool is, in effect, a
single-shot, human-or-LLM-authored use of exactly the execution path Cycle 5 will drive
programmatically many times per turn.

This tool has no retrieval of its own — `agents/nodes/tools.py::tools_node` never populates
`data` from `state["retrieved_chunks"]` or anything else; the model must fill both `code` and
`data` from the conversation alone. Live testing found this is a real fabrication risk, not a
theoretical one: routed here for a question needing real corpus records it did not have, the
model either echoed the tool's own name back as literal (non-runnable) code, or invented an
entirely fictional dataset and confidently computed over it as if real
(`docs/ASSUMPTIONS_AND_TRADEOFFS.md` trade-off 29's second finding). `PythonAnalysisParams`'s
field descriptions now say explicitly what to do instead of inventing data; the Supervisor's
own routing prompt (`agents/nodes/supervisor.py`) is the first line of defense against reaching
this tool at all for that shape of question, this is the safety net for whenever it still does.
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
    tool call cannot smuggle in anything else.

    This tool has no way to look up or retrieve anything — it can only compute over values
    already present in this conversation. Live testing found the model routed here anyway for
    a question needing real corpus records it did not have, and, having no data to work with,
    either wrote `code = "python_analysis"` (echoing the tool's own name back as if it were a
    callable, producing a `NameError`) or fabricated a plausible-looking but entirely invented
    dataset and confidently computed over it — the second failure mode is the more serious one,
    since a run that doesn't also trip the sandbox's own restrictions (e.g. an import) would
    return a fabricated numeric answer with nothing flagging it as invented.
    `docs/ASSUMPTIONS_AND_TRADEOFFS.md` trade-off 29's second finding has the full
    investigation. Both field descriptions below now say explicitly what to do instead.
    """

    code: str = Field(
        description=(
            "Python code to run. No imports, no dunder attribute access, no file I/O. "
            "The value to return must be assigned to a variable named `result`. "
            "The tool's input, if any, is available as a variable named `data`. If the "
            "specific values to analyze are not already stated in `data` or elsewhere in this "
            "conversation, do NOT invent example, placeholder, or otherwise made-up data to "
            "compute over — instead write code that assigns a plain explanation to `result`, "
            'e.g. `result = "No data available in this conversation for this analysis."`'
        )
    )
    data: Any = Field(
        default=None,
        description=(
            "JSON-serializable data the code can analyze, taken only from values already "
            "explicitly present in this conversation (e.g. numbers or records the user typed, "
            "or a previous tool result shown earlier). Leave this unset rather than inventing "
            "data that was never actually stated — this tool cannot search or retrieve records "
            "on its own."
        ),
    )


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
            "(counts, groupings, simple statistics) that is already explicitly present in this "
            "conversation. No imports, no file access. Cannot search, retrieve, or look up "
            "anything on its own — never choose this for a question that requires finding "
            "records from the document corpus rather than computing over values already "
            "stated here."
        ),
        required_permission=Permission.ANALYTICS_TOOLS,
        params_schema=PythonAnalysisParams,
        handler=_handle,
        timeout_seconds=timeout_seconds,
    )
