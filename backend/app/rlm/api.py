"""Curated Python API injected into `rlm/sandbox.py` for the RLM planner's generated search
plans: `search`, `filter`, `batch`, `sub_agent`, `sub_agents`, `aggregate` — the five functions
`docs/ARCHITECTURE.md` and `docs/DELIVERY_PLAN.md` name explicitly. Every function here operates
on plain JSON-safe `dict`s, never `RetrievedChunk` model instances or anything else with
attributes to walk — this keeps the sandbox's data surface exactly as constrained as
`python_analysis`'s `data: Any` contract, just populated by `search` instead of a caller-supplied
argument.

`filter` deliberately shadows the `filter` *builtin* `rlm/sandbox.py`'s `_SAFE_BUILTINS` exposes:
inside the sandbox's globals, a plan-level name always wins over a builtin of the same name, and
the RLM API's `filter(chunks, ...)` — a keyword-argument narrowing helper over chunk metadata —
is far more useful to a plan than the lazy predicate-over-iterable builtin it replaces. Anything
the builtin could do is still reachable through an ordinary list comprehension, which the AST
allowlist already permits.

## The sync/async bridge

`rlm/sandbox.py::run_sandboxed` runs generated code's `exec()` synchronously on a worker thread
(see its module docstring) — but `search` needs Pinecone and `sub_agent`/`sub_agents`/`aggregate`
need the local LLM, both genuinely async. Every function this module builds looks like an
ordinary blocking Python function to the sandboxed code, but internally hands its real async work
back to the *main* event loop via `_submit_to_loop` and blocks only the sandbox's own worker
thread on `.result()` until it completes. The loop itself is never blocked, so a `search` or
`sub_agent` call made this way is exactly as concurrent as if a graph node had awaited it
directly — only the one worker thread waits.

This is a different risk profile from `docs/ASSUMPTIONS_AND_TRADEOFFS.md` trade-off 16's
`asyncio.wait_for`/`AsyncExitStack` bug: that failure came from wrapping a *stack-tracked context
manager's entry* in `wait_for`, which broke `anyio`'s cancel-scope task affinity.
`asyncio.run_coroutine_threadsafe` (which `_submit_to_loop` builds on) is purpose-built for
cross-thread submission to an already-running loop and has no cancel-scope involvement.

**A second, non-obvious finding this bridge forces**: the stdlib `run_coroutine_threadsafe`
schedules its coroutine via `loop.call_soon_threadsafe`, whose callback runs in whatever
`contextvars.Context` is already active *on the loop thread* — not the submitting (worker)
thread's context. Every graph node's `get_stream_writer()` result and (from Cycle 7) LangSmith's
run-tree nesting are both contextvar-based, so a naive bridge would silently detach every
sandboxed LLM call from the current trace and silently drop it from `astream_events`. `RLMContext`
captures `contextvars.copy_context()` once, on the real event-loop thread, at the top of
`rlm/executor.py::execute_research` — before any thread-bridging happens — and every bridged call,
including recursive ones, reuses that same captured context via `loop.create_task(coro,
context=...)` (the `context=` parameter Python 3.11 added to `create_task` specifically to make
this possible, verified against this project's installed 3.11.5 rather than assumed).

## Recursion and the caps that bound it

`sub_agent` is the one function that can recurse: analyzing one batch of chunks either (a) bottoms
out in a single direct, non-recursive LLM call once the recursion has reached `RLMBudget.max_depth`,
or (b) recursively runs a **new, full RLM cycle** — the planner generates another Python plan
scoped to the sub-question and that batch's chunks, executed in its own sandboxed call, which may
itself call any of these five functions again. `docs/DELIVERY_PLAN.md` asks for "recursive
executor with depth and fan-out caps under a bounded semaphore"; this module is the fan-out half
(`sub_agents`'s concurrent `asyncio.gather`, bounded by a semaphore sized
`RLMContext.max_concurrent_sub_agents`) and `rlm/executor.py` is the depth half. Both share one
`RLMBudget` instance by reference across the *whole* recursive tree, because depth alone does not
bound *width* — a plan that fans out to many batches at a shallow depth could still exhaust the
per-question latency budget `docs/DECISIONS.md` §3 sets. `RLMBudget.max_total_sub_agent_calls` is
what actually keeps that in check: once exhausted, anywhere in the tree, further `sub_agent` calls
degrade to a plain-English "budget exhausted" finding rather than raising — a plan that hits the
ceiling still completes and produces an answer, just a partial one, matching every other
degradation in this codebase.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any, Protocol, cast

import structlog
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from backend.app.core.errors import AppError
from backend.app.core.security.rbac import Principal
from backend.app.guardrails.injection import UNTRUSTED_CONTENT_INSTRUCTION, frame_untrusted_content
from backend.app.llm.provider import LLMProvider
from backend.app.observability.events import ActivityEvent, ActivityEventType
from backend.app.retrieval.hybrid import hybrid_search
from backend.app.retrieval.pinecone_store import PineconeStore

logger = structlog.get_logger(__name__)


class SubAgentFinding(BaseModel):
    """Schema for the leaf case: one direct, non-recursive analysis of a single batch."""

    reasoning: str = Field(
        description="One sentence on what this batch's evidence shows, written before the finding."
    )
    finding: str = Field(
        description="A concise answer to the sub-question, grounded only in the evidence given."
    )


class AggregatedFindings(BaseModel):
    """Schema for `aggregate`'s one LLM call combining every sub-agent finding."""

    summary: str = Field(description="A synthesized answer combining every finding below.")
    recurring_themes: list[str] = Field(
        default_factory=list,
        description="Distinct themes or root causes that recur across two or more findings, if any.",
    )


@dataclass
class RLMBudget:
    """Shared by reference across one research turn's entire recursive tree — see the module
    docstring's "Recursion and the caps that bound it" section for why width needs its own cap,
    not just depth. Constructed once by `rlm/executor.py::execute_research`; every recursive
    `RLMContext` carries the *same* instance rather than a fresh one, which is what makes
    `max_total_sub_agent_calls` a whole-tree limit instead of a per-level one.
    """

    max_depth: int
    max_total_sub_agent_calls: int
    sub_agent_calls_made: int = 0

    def try_reserve_sub_agent_call(self) -> bool:
        """Reserve one call against the whole-tree budget, returning `False` once exhausted
        instead of raising. Safe without a lock: every caller runs as a coroutine on the single
        event-loop thread (see the module docstring's context-preservation note), and this
        check-then-increment contains no `await` between the two steps, so no other task can
        interleave with it."""
        if self.sub_agent_calls_made >= self.max_total_sub_agent_calls:
            return False
        self.sub_agent_calls_made += 1
        return True


class NestedPlanRunner(Protocol):
    """What `sub_agent` calls to recurse into a full nested RLM cycle — implemented by
    `rlm/executor.py::run_research`, injected here rather than imported, so this module never
    imports `rlm/executor.py` back (it builds the API this module exposes)."""

    async def __call__(self, *, question: str, data: Any, context: RLMContext) -> str: ...


@dataclass
class RLMContext:
    """Everything one research turn's injected API functions need, bound once by
    `rlm/executor.py` before each sandboxed call — analogous to `agents/context.GraphContext`,
    but scoped to a single research turn (and, recursively, to one node of its call tree) rather
    than the whole process's lifetime."""

    loop: asyncio.AbstractEventLoop
    captured_vars: contextvars.Context
    principal: Principal
    role: str
    store: PineconeStore | None
    llm: LLMProvider
    budget: RLMBudget
    depth: int
    max_concurrent_sub_agents: int
    run_nested_plan: NestedPlanRunner
    activity_writer: Callable[[ActivityEvent], None] = field(default=lambda _event: None)


def _submit_to_loop(
    context: RLMContext, coro: Coroutine[Any, Any, Any]
) -> concurrent.futures.Future[Any]:
    """Schedule `coro` on `context.loop`, preserving `context.captured_vars` — see the module
    docstring's context-preservation finding for why this cannot be plain
    `asyncio.run_coroutine_threadsafe`. Returns a `concurrent.futures.Future` whose `.result()`
    blocks the *calling* (worker) thread, never the loop."""
    future: concurrent.futures.Future[Any] = concurrent.futures.Future()

    def _start() -> None:
        try:
            task: asyncio.Task[Any] = context.loop.create_task(coro, context=context.captured_vars)
        except Exception as exc:  # noqa: BLE001 - surface any scheduling failure to the caller
            future.set_exception(exc)
            return

        def _done(finished: asyncio.Task[Any]) -> None:
            if finished.cancelled():
                future.cancel()
                return
            error = finished.exception()
            if error is not None:
                future.set_exception(error)
            else:
                future.set_result(finished.result())

        task.add_done_callback(_done)

    context.loop.call_soon_threadsafe(_start)
    return future


def _run_coroutine_from_sandbox(context: RLMContext, coro: Coroutine[Any, Any, Any]) -> Any:
    """The one place every bridged call goes through. Runs on the sandbox's worker thread;
    blocks it, never the event loop, until `coro` finishes on the loop thread."""
    return _submit_to_loop(context, coro).result()


def _chunk_to_dict(chunk: Any) -> dict[str, Any]:
    return chunk.model_dump(mode="json")  # type: ignore[no-any-return]


def build_search(context: RLMContext) -> Callable[..., list[dict[str, Any]]]:
    async def _search(query: str, top_k: int) -> list[dict[str, Any]]:
        if context.store is None:
            return []
        try:
            chunks = await hybrid_search(
                context.store, query_text=query, role=context.role, top_k=top_k
            )
        except AppError as exc:
            # Matches `agents/nodes/retrieval.py`'s precedent: one degraded search source (or
            # Pinecone entirely down) empties the result rather than failing the whole plan.
            logger.warning("rlm_search_degraded", query=query, error=str(exc))
            return []
        return [_chunk_to_dict(chunk) for chunk in chunks]

    def search(query: str, top_k: int = 10) -> list[dict[str, Any]]:
        return cast(
            "list[dict[str, Any]]", _run_coroutine_from_sandbox(context, _search(query, top_k))
        )

    return search


def filter_chunks(
    chunks: list[dict[str, Any]],
    *,
    contains: str | None = None,
    document_type: str | None = None,
    department: str | None = None,
) -> list[dict[str, Any]]:
    """Pure and synchronous — no bridge needed. A convenience over the metadata fields every
    chunk dict already carries; anything not covered here (arbitrary predicates, date ranges) is
    still just a list comprehension away, since the sandbox's AST allowlist already permits
    ordinary comprehensions over plain dicts."""

    def _matches(chunk: dict[str, Any]) -> bool:
        contains_ok = contains is None or contains.lower() in str(chunk.get("text", "")).lower()
        type_ok = document_type is None or chunk.get("document_type") == document_type
        department_ok = department is None or chunk.get("department") == department
        return contains_ok and type_ok and department_ok

    return [chunk for chunk in chunks if _matches(chunk)]


def batch_chunks(chunks: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    """Pure and synchronous. A non-positive `size` is treated as `1` rather than raising or
    looping forever on a zero-length step — a plan's mistake here should degrade to "many tiny
    batches", not crash the whole turn."""
    step = max(1, size)
    return [chunks[i : i + step] for i in range(0, len(chunks), step)]


def _format_evidence(chunks: list[dict[str, Any]]) -> str:
    if not chunks:
        return "(no evidence in this batch)"
    return "\n\n".join(f"[{c.get('title', 'untitled')}] {c.get('text', '')}" for c in chunks)


async def _direct_finding(*, question: str, chunks: list[dict[str, Any]], llm: LLMProvider) -> str:
    """The leaf case: one direct, non-recursive LLM call. No plan, no further tool access.

    The evidence batch is framed as untrusted data (`guardrails/injection.py`) exactly like
    `agents/nodes/response.py`'s own evidence section: a sub-agent reads whatever a generated
    search plan handed it, which can include an entire document's text, so the same defense
    against a document carrying injection-shaped text applies here too.
    """
    messages = [
        SystemMessage(
            content=(
                "You are a sub-agent analyzing one batch of internal documents for a larger "
                "research task. Answer the question below using only the evidence given. "
                f"{UNTRUSTED_CONTENT_INSTRUCTION}"
            )
        ),
        HumanMessage(
            content=(
                f"Question: {question}\n\nEvidence:\n{frame_untrusted_content(_format_evidence(chunks))}"
            )
        ),
    ]
    try:
        result = await llm.astructured(messages, schema=SubAgentFinding, reasoning=True)
    except AppError as exc:
        logger.warning("rlm_sub_agent_finding_failed", question=question, error=str(exc))
        return f"(analysis unavailable for this batch: {exc.message})"
    return result.finding


async def _one_sub_agent_call(
    *, question: str, chunks: list[dict[str, Any]], context: RLMContext
) -> str:
    if not context.budget.try_reserve_sub_agent_call():
        return (
            "(skipped: this research turn's sub-agent budget "
            f"({context.budget.max_total_sub_agent_calls}) was already used by other batches)"
        )

    context.activity_writer(
        ActivityEvent(
            event_type=ActivityEventType.TOOL_CALL,
            node="research",
            message=f"Sub-agent analyzing ({len(chunks)} chunk(s)): {question[:80]}",
            data={"depth": context.depth, "batch_size": len(chunks)},
        )
    )

    if context.depth + 1 >= context.budget.max_depth:
        return await _direct_finding(question=question, chunks=chunks, llm=context.llm)

    nested_context = RLMContext(
        loop=context.loop,
        captured_vars=context.captured_vars,
        principal=context.principal,
        role=context.role,
        store=context.store,
        llm=context.llm,
        budget=context.budget,
        depth=context.depth + 1,
        max_concurrent_sub_agents=context.max_concurrent_sub_agents,
        run_nested_plan=context.run_nested_plan,
        activity_writer=context.activity_writer,
    )
    try:
        return await context.run_nested_plan(question=question, data=chunks, context=nested_context)
    except AppError as exc:
        # A nested plan failing outright is this sub-agent's problem to explain, not the whole
        # research turn's — degrade to the same leaf analysis rather than losing this batch's
        # evidence entirely.
        logger.warning("rlm_nested_plan_failed", depth=nested_context.depth, error=str(exc))
        return await _direct_finding(question=question, chunks=chunks, llm=context.llm)


def build_sub_agent(context: RLMContext) -> Callable[[str, list[dict[str, Any]]], str]:
    def sub_agent(question: str, chunks: list[dict[str, Any]]) -> str:
        return cast(
            str,
            _run_coroutine_from_sandbox(
                context, _one_sub_agent_call(question=question, chunks=chunks, context=context)
            ),
        )

    return sub_agent


def build_sub_agents(
    context: RLMContext,
) -> Callable[[str, list[list[dict[str, Any]]]], list[str]]:
    """The concurrent fan-out primitive: one blocking call from the sandboxed code's point of
    view, but every batch's analysis runs concurrently on the event loop, bounded by a semaphore
    sized `context.max_concurrent_sub_agents` — `docs/ARCHITECTURE.md`'s "bounded semaphore that
    prevents the RLM from saturating a single local model". Plans should prefer this over a loop
    of individual `sub_agent` calls (the planner's system prompt says so explicitly), since a
    sandboxed `for` loop calling `sub_agent` one at a time would block on each `.result()` in
    turn and never actually run concurrently."""
    semaphore = asyncio.Semaphore(context.max_concurrent_sub_agents)

    async def _bounded(question: str, chunks: list[dict[str, Any]]) -> str:
        async with semaphore:
            return await _one_sub_agent_call(question=question, chunks=chunks, context=context)

    async def _gather(question: str, batches: list[list[dict[str, Any]]]) -> list[str]:
        return list(await asyncio.gather(*(_bounded(question, batch) for batch in batches)))

    def sub_agents(question: str, batches: list[list[dict[str, Any]]]) -> list[str]:
        return cast("list[str]", _run_coroutine_from_sandbox(context, _gather(question, batches)))

    return sub_agents


def build_aggregate(context: RLMContext) -> Callable[[list[str], str], dict[str, Any]]:
    async def _aggregate(findings: list[str], question: str) -> dict[str, Any]:
        findings_text = "\n".join(f"- {finding}" for finding in findings) or "(no findings)"
        messages = [
            SystemMessage(
                content="Combine the sub-agent findings below into one synthesized answer."
            ),
            HumanMessage(content=f"Original question: {question}\n\nFindings:\n{findings_text}"),
        ]
        try:
            result = await context.llm.astructured(
                messages, schema=AggregatedFindings, reasoning=True
            )
        except AppError as exc:
            logger.warning("rlm_aggregate_failed", error=str(exc))
            return {"summary": " ".join(findings) or "(no findings)", "recurring_themes": []}
        return {"summary": result.summary, "recurring_themes": result.recurring_themes}

    def aggregate(findings: list[str], question: str) -> dict[str, Any]:
        return cast(
            "dict[str, Any]", _run_coroutine_from_sandbox(context, _aggregate(findings, question))
        )

    return aggregate


def build_rlm_globals(context: RLMContext) -> dict[str, Any]:
    """Assembles the sandbox globals `rlm/executor.py` injects for one plan execution: the five
    curated functions this module builds, bound to `context`."""
    return {
        "search": build_search(context),
        "filter": filter_chunks,
        "batch": batch_chunks,
        "sub_agent": build_sub_agent(context),
        "sub_agents": build_sub_agents(context),
        "aggregate": build_aggregate(context),
    }
