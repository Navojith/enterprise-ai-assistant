"""Recursive RLM executor: ties `rlm/planner.py` (what code to run) to `rlm/sandbox.py` (how it
runs) and `rlm/api.py` (what it can call), and is the one place recursion actually happens —
`sub_agent`'s `NestedPlanRunner` (`rlm/api.py`) is bound to `run_research` below, a closure this
module owns so `rlm/api.py` never imports this module back (avoiding a circular import between
"what a plan can call" and "what runs a plan").

```
execute_research(question, ...)                     depth 0: full plan generated + run
  -> plan calls sub_agents([...batches])
       -> each batch's call, depth 1 < max_depth: recurses into run_research again
            -> a new nested plan calls sub_agents([...]) again
                 -> each of those calls, depth 2 >= max_depth: bottoms out to one direct
                    LLM finding (rlm/api.py::_direct_finding), no further plan generation
```

The diagram shows what the mechanism supports at `max_depth=2`; `Settings.rlm_max_depth`
actually **defaults to 1** — every `sub_agent` call bottoms out to a leaf on this project's
hardware, never recursing into a nested plan — for a reason only live verification surfaced:
see `core/config.py`'s `rlm_max_depth`/`rlm_max_concurrent_sub_agents` docstrings for what
happened at the higher defaults (concurrent nested plan-generation calls queuing inside a
single local Ollama instance until they timed out). The recursive path stays fully built and
correct for a deployment with real spare LLM throughput to raise the setting into.

Two independent caps bound this tree, both read from one `RLMBudget` shared by reference across
every recursive call (see `rlm/api.py`'s module docstring for why width needs its own cap, not
just depth): `max_depth` (how many levels of *plan generation* are allowed) and
`max_total_sub_agent_calls` (how many `sub_agent`/`sub_agents` calls are allowed anywhere in the
whole tree, regardless of depth) — the latter is what actually keeps a wide, shallow tree inside
`docs/DECISIONS.md` §3's per-question latency budget.
"""

from __future__ import annotations

import asyncio
import contextvars
import dataclasses
from dataclasses import dataclass
from typing import Any

import structlog
from langgraph.config import get_stream_writer

from backend.app.core.config import Settings
from backend.app.core.errors import SandboxViolationError
from backend.app.core.security.rbac import Principal
from backend.app.llm.provider import LLMProvider
from backend.app.retrieval.pinecone_store import PineconeStore
from backend.app.rlm.api import RLMBudget, RLMContext, build_rlm_globals
from backend.app.rlm.planner import deterministic_fallback_plan, generate_plan
from backend.app.rlm.sandbox import run_sandboxed

logger = structlog.get_logger(__name__)

# A nested plan runs inside the top-level call's own wall-clock budget and concurrently with any
# sibling batches, so a shorter cap than the top-level call is enough headroom without letting
# one slow nested branch dominate the whole turn.
_NESTED_PLAN_TIMEOUT_SECONDS = 45.0


@dataclass
class ResearchResult:
    """What `agents/nodes/research.py` gets back from a completed research turn."""

    result: Any
    used_fallback_plan: bool
    sub_agent_calls_made: int


def _stringify(result: Any) -> str:
    """A nested call's caller (`rlm/api.py::_one_sub_agent_call`) only ever wants one finding
    string back, whatever shape the nested plan's own `result` took — most plans assign
    `aggregate`'s `{"summary": ..., "recurring_themes": [...]}` dict, so that is unwrapped to
    just its summary; anything else is stringified as-is rather than raising on an unexpected
    shape."""
    if isinstance(result, dict) and "summary" in result:
        return str(result["summary"])
    return str(result)


async def _run_plan_at_depth(
    *, question: str, data: Any, context: RLMContext, timeout_seconds: float
) -> tuple[Any, bool]:
    """Generate and run one plan at `context.depth`. Returns `(sandbox_result, used_fallback)`.
    A generated plan that validates but still fails at runtime (a `NameError` from a typo, for
    instance — `rlm/sandbox.py` wraps that as `SandboxViolationError` too) gets exactly one
    fallback attempt with the fixed deterministic plan; the fallback plan itself is never
    retried again if it somehow also fails, since a bug in fixed, hand-written code is a real
    bug to fix, not a degradation to paper over a second time.

    At depth 0 only, the fallback attempt runs against a **fresh** `RLMBudget`, not
    `context.budget` — verified live that reusing the same one lets a generated plan that
    already spent the whole `max_total_sub_agent_calls` budget (successfully or not) before
    failing at some *later* line leave the deterministic fallback with nothing to spend,
    defeating the one guarantee the fallback plan exists to provide. The failed attempt's own
    sub-agent work is discarded along with its code, so the budget it spent should be too. A
    nested call (`context.depth > 0`) keeps the shared budget instead: it must stay a true
    whole-tree cap there, since resetting it for one branch's fallback would let the tree's
    total sub-agent calls exceed the cap the rest of the tree is still counting against.
    """
    code, used_fallback = await generate_plan(question, llm=context.llm)
    sandbox_globals = build_rlm_globals(context)
    sandbox_globals["question"] = question
    sandbox_globals["data"] = data

    try:
        outcome = await run_sandboxed(
            code, injected_globals=sandbox_globals, timeout_seconds=timeout_seconds
        )
    except SandboxViolationError as exc:
        if used_fallback:
            raise
        logger.warning("rlm_generated_plan_failed_at_runtime", depth=context.depth, error=str(exc))
        fallback_context = context
        if context.depth == 0:
            fallback_context = dataclasses.replace(
                context,
                budget=RLMBudget(
                    max_depth=context.budget.max_depth,
                    max_total_sub_agent_calls=context.budget.max_total_sub_agent_calls,
                ),
            )
        code = deterministic_fallback_plan(question)
        outcome = await run_sandboxed(
            code,
            injected_globals={
                **build_rlm_globals(fallback_context),
                "question": question,
                "data": data,
            },
            timeout_seconds=timeout_seconds,
        )
        used_fallback = True

    return outcome.result, used_fallback


async def run_research(*, question: str, data: Any, context: RLMContext) -> str:
    """The `NestedPlanRunner` bound into every `RLMContext.run_nested_plan` (see `rlm/api.py`).
    Runs one full nested plan-generation-and-execution cycle at `context.depth` and returns a
    plain finding string."""
    result, _ = await _run_plan_at_depth(
        question=question, data=data, context=context, timeout_seconds=_NESTED_PLAN_TIMEOUT_SECONDS
    )
    return _stringify(result)


async def execute_research(
    *,
    question: str,
    principal: Principal,
    role: str,
    store: PineconeStore | None,
    llm: LLMProvider,
    settings: Settings,
) -> ResearchResult:
    """Entry point for `agents/nodes/research.py`: depth 0 of the recursive tree.

    Must be called from inside a real LangGraph node invocation — it calls `get_stream_writer()`
    once, up front, exactly like every other node (`agents/nodes/supervisor.py` et al.), and
    captures the current `contextvars.Context` for `rlm/api.py`'s sync/async bridge to reuse on
    every recursive call, so the panel and (from Cycle 7) LangSmith both see sub-agent activity
    nested under this turn instead of detached from it.
    """
    loop = asyncio.get_running_loop()
    captured_vars = contextvars.copy_context()
    writer = get_stream_writer()

    budget = RLMBudget(
        max_depth=settings.rlm_max_depth,
        max_total_sub_agent_calls=settings.rlm_max_total_sub_agent_calls,
    )
    context = RLMContext(
        loop=loop,
        captured_vars=captured_vars,
        principal=principal,
        role=role,
        store=store,
        llm=llm,
        budget=budget,
        depth=0,
        max_concurrent_sub_agents=settings.rlm_max_concurrent_sub_agents,
        run_nested_plan=run_research,
        activity_writer=writer,
    )

    result, used_fallback = await _run_plan_at_depth(
        question=question,
        data=None,
        context=context,
        timeout_seconds=settings.rlm_plan_timeout_seconds,
    )
    return ResearchResult(
        result=result,
        used_fallback_plan=used_fallback,
        sub_agent_calls_made=budget.sub_agent_calls_made,
    )
