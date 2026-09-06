"""Curated Python API injected into `rlm/sandbox.py` for the RLM planner's generated search
plans: `search`, `filter`, `batch`, `sub_agent`, `sub_agents`, `aggregate` — the five functions
`docs/ARCHITECTURE.md` and `docs/DELIVERY_PLAN.md` name explicitly — plus `group_by_document`,
added after live testing found `batch`'s fixed-size, order-agnostic splitting could separate one
document's sections across two batches (see `group_by_document`'s own docstring), and
`count_by_month`, added after live testing found `aggregate()` cannot answer a "how many per
month" style question — it is one LLM call producing prose, never a deterministic count (see
`count_by_month`'s own docstring). Every function
here operates on plain JSON-safe `dict`s, never `RetrievedChunk` model instances or anything else
with attributes to walk — this keeps the sandbox's data surface exactly as constrained as
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
import re
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any, Protocol, cast

import structlog
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from backend.app.core.config import Settings
from backend.app.core.errors import AppError
from backend.app.core.security.rbac import Principal
from backend.app.guardrails.injection import UNTRUSTED_CONTENT_INSTRUCTION, frame_untrusted_content
from backend.app.llm.provider import LLMProvider
from backend.app.observability.events import ActivityEvent, ActivityEventType
from backend.app.retrieval.hybrid import hybrid_search, merge_prioritizing_scoped
from backend.app.retrieval.models import DEPARTMENTS, DocumentType
from backend.app.retrieval.pinecone_store import PineconeStore
from backend.app.retrieval.reranker import rerank_chunks

logger = structlog.get_logger(__name__)


class SubAgentFinding(BaseModel):
    """Schema for the leaf case: one direct, non-recursive analysis of a single batch."""

    reasoning: str = Field(
        description="One sentence on what this batch's evidence shows, written before the finding."
    )
    finding: str = Field(
        description=(
            "A grounded answer to the sub-question, using only the evidence given. If the "
            "evidence describes multiple distinct incidents, dates, or events, identify each "
            "one separately (with its date and root cause) rather than merging them into one "
            "generic statement, and cite each fact using the bracketed [Title] it appeared "
            "under."
        )
    )


class AggregatedFindings(BaseModel):
    """Schema for `aggregate`'s one LLM call combining every sub-agent finding."""

    summary: str = Field(
        description=(
            "A synthesized answer combining every finding below. Preserve every distinct "
            "incident, date, and root cause the findings name and keep their citations intact "
            "— do not merge or average them into vague generalities, and never state a count "
            "or fact that is not actually present in the findings."
        )
    )
    recurring_themes: list[str] = Field(
        default_factory=list,
        description=(
            "Distinct themes or root causes explicitly named in two or more of the findings "
            "below — never a theme invented or inferred beyond what the findings actually "
            "state."
        ),
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
    reranked: bool = False

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

    def try_reserve_rerank(self) -> bool:
        """Reserve this research turn's one allowed rerank call. `CLAUDE.md`'s architecture
        invariant is explicit: "Reranking runs at most once per user turn, never per RLM
        sub-agent — the free tier allows only 500 requests per month." Sharing one `RLMBudget`
        by reference across the whole recursive tree (exactly like `sub_agent_calls_made`) is
        what makes this a true once-per-turn cap: the first `search()` call anywhere in the
        tree — ordinarily the top-level plan's own, highest-value search — gets reranked, and
        every subsequent call, including a recursive sub-agent's own `search()`, reuses the
        plain RRF-fused order instead. `rlm/executor.py::_run_plan_at_depth` hands a depth-0
        fallback attempt a *fresh* `RLMBudget`, so a failed generated plan's rerank spend (if
        any) does not deny the fallback its own one call — the same reasoning already applied
        to `sub_agent_calls_made` there."""
        if self.reranked:
            return False
        self.reranked = True
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
    settings: Settings
    budget: RLMBudget
    depth: int
    max_concurrent_sub_agents: int
    run_nested_plan: NestedPlanRunner
    activity_writer: Callable[[ActivityEvent], None] = field(default=lambda _event: None)
    # The Supervisor's department guess for this turn (`agents/nodes/supervisor.py`'s
    # `search_query`/`department` fields, `docs/ASSUMPTIONS_AND_TRADEOFFS.md` trade-off 26),
    # threaded through from `rlm/executor.py::execute_research` and preserved across every
    # recursive `RLMContext` a nested plan creates (`_one_sub_agent_call` below) — a sub-question
    # a nested plan investigates is still fundamentally about the same department as the turn
    # that spawned it. `None` when the Supervisor could not identify one, in which case `search`
    # behaves exactly as it did before this field existed: a plain all-department search.
    department: str | None = None


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
        # Populated only on the department-scoped path below; stay `None` on the plain path so
        # the completion log can distinguish "not scoped" from "scoped but one side came back
        # empty" — the RLM path has no per-search activity event the way `retrieval_node` gets
        # (`RETRIEVAL_STATUS`, "Found N relevant chunk(s)"), so this is currently the only place
        # a live run's actual chunk counts are ever visible at all.
        scoped_count: int | None = None
        unscoped_count: int | None = None
        try:
            if context.department:
                # Identical fix, identical reason, as `agents/nodes/retrieval.py`'s scoped +
                # all-department merge (`docs/ASSUMPTIONS_AND_TRADEOFFS.md` trade-off 26): a
                # plain all-department `hybrid_search` lets RRF fusion bury the correct
                # department's chunks under several *other* departments' locally-top-ranked but
                # irrelevant ones. Run concurrently so a department-scoped search costs one extra
                # round-trip's latency, not a sequential doubling of it.
                scoped_result, unscoped_result = await asyncio.gather(
                    hybrid_search(
                        context.store,
                        query_text=query,
                        role=context.role,
                        namespaces=[context.department],
                        top_k=top_k,
                    ),
                    hybrid_search(context.store, query_text=query, role=context.role, top_k=top_k),
                    return_exceptions=True,
                )
                if isinstance(scoped_result, BaseException) and isinstance(
                    unscoped_result, BaseException
                ):
                    raise unscoped_result
                scoped = [] if isinstance(scoped_result, BaseException) else scoped_result
                unscoped = [] if isinstance(unscoped_result, BaseException) else unscoped_result
                scoped_count, unscoped_count = len(scoped), len(unscoped)
                # Merge budget is the *sum* of both full lists (`top_k * 2`), matching
                # `agents/nodes/retrieval.py`'s `_MERGED_TOP_K` exactly — not `top_k` alone.
                # This module used to pass `top_k` here specifically to avoid growing a plan's
                # own requested chunk count (and so its downstream `batch`/`sub_agents` count and
                # `RLMBudget` spend). Live-verified that this was a real, worse-than-no-scoping
                # regression, not a harmless economy: `hybrid_search` never returns "no good
                # match" for a namespace, only its nearest neighbors — so a *wrong* department
                # guess still fills all `top_k` scoped slots with irrelevant chunks, and
                # `merge_prioritizing_scoped` keeps every one of them unconditionally, leaving
                # zero room for the correct all-department result. Confirmed against a real
                # question ("payment incidents per month last year", guessed department
                # `product` instead of `payments`): the all-department search alone surfaced one
                # genuine payment-incident chunk in its own top `top_k`, and the pre-fix merge
                # discarded it entirely — strictly worse than running no department-scoping at
                # all. `docs/ASSUMPTIONS_AND_TRADEOFFS.md` trade-off 29 has the full
                # investigation, including why a smaller reserved quota was tried first and
                # found insufficient. The accepted cost, same as `retrieval_node` already pays:
                # a scoped `search()` call can now return up to `top_k * 2` chunks rather than a
                # fixed `top_k`, growing a plan's batch/sub-agent-call count correspondingly.
                chunks = merge_prioritizing_scoped(scoped, unscoped, top_k=top_k * 2)
            else:
                chunks = await hybrid_search(
                    context.store, query_text=query, role=context.role, top_k=top_k
                )
        except AppError as exc:
            # Matches `agents/nodes/retrieval.py`'s precedent: one degraded search source (or
            # Pinecone entirely down) empties the result rather than failing the whole plan.
            logger.warning("rlm_search_degraded", query=query, error=str(exc))
            return []

        # Parity with `agents/nodes/retrieval.py`, gated by `RLMBudget.try_reserve_rerank` —
        # see that method's docstring for the once-per-turn invariant this enforces. Before this
        # fix, every RLM search ran on the raw RRF-fused order with no cross-encoder pass at
        # all, which was a real, measured quality gap against the plain retrieval path (a
        # research-route answer to the spec's own example question came back visibly worse than
        # a single-hop retrieval answer to the identical question).
        #
        # `rerank_attempted` records whether this call *spent* the turn's one reservation, not
        # whether reranking actually reordered anything — `rerank_chunks` itself degrades to a
        # no-op (and logs its own `rerank_skipped`) whenever `settings.rerank_enabled` is off,
        # the monthly budget is exhausted, or the call fails. Naming this field `reranked` read
        # as a stronger claim than it was: live-verified confusing, since this project's own
        # `.env` runs with `RERANK_ENABLED=false` day to day, so every `rlm_search_completed`
        # line during ordinary local testing showed `reranked=True` despite `rerank_chunks`
        # having just no-op'd one line above it in the same log stream.
        rerank_attempted = context.budget.try_reserve_rerank()
        if rerank_attempted:
            chunks = await rerank_chunks(
                context.store, query=query, chunks=chunks, settings=context.settings
            )
        logger.info(
            "rlm_search_completed",
            query=query,
            department=context.department,
            top_k=top_k,
            scoped_count=scoped_count,
            unscoped_count=unscoped_count,
            result_count=len(chunks),
            rerank_attempted=rerank_attempted,
        )
        return [_chunk_to_dict(chunk) for chunk in chunks]

    def search(query: str, top_k: int = 10) -> list[dict[str, Any]]:
        return cast(
            "list[dict[str, Any]]", _run_coroutine_from_sandbox(context, _search(query, top_k))
        )

    return search


_VALID_DOCUMENT_TYPES = frozenset(document_type.value for document_type in DocumentType)
_VALID_DEPARTMENTS = frozenset(DEPARTMENTS)


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
    ordinary comprehensions over plain dicts.

    An unrecognized `document_type` or `department` is logged and *ignored* rather than applied
    literally — live testing found the model repeatedly inventing plausible-sounding values
    ("outage report") that match no real chunk, silently zeroing an otherwise-correct plan's
    entire result with no error anywhere to explain why. `rlm/planner.py`'s system prompt now
    also tells the model the real values up front (the cheaper, more direct half of this same
    fix); this is the defensive half for whatever it still gets wrong.
    """
    if document_type is not None and document_type not in _VALID_DOCUMENT_TYPES:
        logger.warning(
            "rlm_filter_ignored_unknown_document_type",
            document_type=document_type,
            valid_values=sorted(_VALID_DOCUMENT_TYPES),
        )
        document_type = None
    if department is not None and department not in _VALID_DEPARTMENTS:
        logger.warning(
            "rlm_filter_ignored_unknown_department",
            department=department,
            valid_values=sorted(_VALID_DEPARTMENTS),
        )
        department = None

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


def group_by_document(
    chunks: list[dict[str, Any]], max_batch_size: int = 8
) -> list[list[dict[str, Any]]]:
    """Group chunks into batches that never split one document's sections across two
    batches, packing whole documents together up to `max_batch_size` per batch.

    Preferred over `batch()` for a broad question spanning many documents: a fixed-size,
    order-agnostic `batch()` call can (and, live-verified, did) split a single incident's
    Root Cause section into one batch and its Summary/Timeline into another, so no single
    sub-agent ever sees the full incident together — `_direct_finding`'s leaf analysis then
    has no way to answer "what was the root cause?" for that incident correctly, and reported
    it as unspecified even though the corpus stated it plainly, just in a different batch.

    Document order is preserved by each document's first appearance in `chunks` — already
    relevance-ranked by `search` (and, since this fix, reranked) — so if the sub-agent budget
    runs out before every batch is analyzed, the highest-ranked documents are the ones that
    ran. A document larger than `max_batch_size` still gets its own single batch rather than
    being split; respecting document boundaries takes priority over the size target.
    """
    order: list[str] = []
    by_document: dict[str, list[dict[str, Any]]] = {}
    for chunk in chunks:
        document_id = str(chunk.get("document_id", ""))
        if document_id not in by_document:
            order.append(document_id)
            by_document[document_id] = []
        by_document[document_id].append(chunk)

    batches: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for document_id in order:
        document_chunks = by_document[document_id]
        if current and len(current) + len(document_chunks) > max_batch_size:
            batches.append(current)
            current = []
        current.extend(document_chunks)
    if current:
        batches.append(current)
    return batches


def count_by_month(chunks: list[dict[str, Any]]) -> dict[str, int]:
    """Count *distinct documents* per calendar month (`created_date`'s `"YYYY-MM"` prefix),
    sorted chronologically — added specifically for questions asking to count or tally
    evidence over time (e.g. "how many payment incidents per month"), which
    `aggregate()` alone cannot answer: `aggregate` is one LLM call producing a prose synthesis,
    never a deterministic number. `rlm/planner.py`'s system prompt tells the model to prefer
    this (or equivalent plain Python counting) over asking `aggregate` to guess a count.

    Counts by `document_id`, not by chunk: one incident is chunked into several sections
    (Summary, Root Cause, Timeline, ...), each carrying the same `created_date`, so counting
    chunks directly would over-count every incident by its section count. A chunk with a
    missing or malformed `created_date` (shorter than `"YYYY-MM"`) is skipped rather than
    corrupting a bucket with a partial key — the same "degrade, don't crash the plan" posture
    `filter_chunks` and `batch_chunks` already take on bad input.
    """
    documents_seen_per_month: dict[str, set[str]] = {}
    for chunk in chunks:
        document_id = str(chunk.get("document_id", ""))
        created_date = str(chunk.get("created_date", ""))
        if not document_id or len(created_date) < 7:
            continue
        month = created_date[:7]
        documents_seen_per_month.setdefault(month, set()).add(document_id)
    return {
        month: len(document_ids) for month, document_ids in sorted(documents_seen_per_month.items())
    }


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
                "research task. Answer the question below using only the evidence given. If "
                "the evidence covers multiple distinct incidents or events, list each one "
                "separately with its date and root cause rather than collapsing them into one "
                "vague statement, and cite each fact's [Title] exactly as it appears in the "
                f"evidence. {UNTRUSTED_CONTENT_INSTRUCTION}"
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
        settings=context.settings,
        budget=context.budget,
        depth=context.depth + 1,
        max_concurrent_sub_agents=context.max_concurrent_sub_agents,
        run_nested_plan=context.run_nested_plan,
        activity_writer=context.activity_writer,
        department=context.department,
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


# Aggregation should primarily *synthesize* the evidence it is handed, not explore alternative
# phrasings of it — unlike the RLM planner (`rlm/planner.py`, `reasoning=True` at the provider's
# default temperature, where creative variety in *strategy* is worth having), `aggregate` is a
# one-shot reduction over a fixed, already-correct set of findings. Lower temperature is
# unrelated to and does not substitute for the completeness check below: it reduces how often a
# degraded draw happens, the check catches it when one happens anyway.
_AGGREGATE_TEMPERATURE = 0.2

# Matches a bracketed `[Title]` citation exactly like `agents/nodes/response.py`'s own citation
# convention and `guardrails/citations.py`'s verification — every sub-agent finding and the
# aggregate prompt itself are both instructed to cite this way. `\[([^\[\]]+)\]` deliberately
# does not match nested or empty brackets.
_CITATION_PATTERN = re.compile(r"\[([^\[\]]+)\]")


def _extract_citations(text: str) -> set[str]:
    """Every bracketed `[Title]` citation appearing in `text`, ignoring a bracket that is empty
    or purely numeric — a numbered-list marker like `[1]`, never a real citation in this
    codebase's convention, which always cites a document *title*."""
    return {
        citation
        for raw in _CITATION_PATTERN.findall(text)
        if (citation := raw.strip()) and not citation.isdigit()
    }


def _missing_citations(findings_text: str, summary: str) -> set[str]:
    """Citations the raw sub-agent findings actually named that the aggregated `summary`
    dropped entirely — a direct, non-brittle signal of *evidence* loss during synthesis, not a
    comparison of prose or phrasing (which legitimate summarization is expected to change
    freely). The aggregate prompt already instructs the model to "keep their citations intact",
    so this checks that instruction was actually followed rather than trusting it was."""
    return _extract_citations(findings_text) - _extract_citations(summary)


async def _retry_aggregate_once(
    context: RLMContext,
    *,
    messages: list[SystemMessage | HumanMessage],
    findings_text: str,
    original: AggregatedFindings,
    missing: set[str],
) -> AggregatedFindings:
    """Exactly one bounded retry, with the specific missing citations fed back as feedback —
    the same "one retry with concrete feedback" shape `rlm/planner.py::_retry_messages` already
    uses for a validation failure, applied here to an incompleteness signal instead. Never
    raises and never loops: an `AppError` on the retry call, or a retry that does not actually
    reduce how many citations are missing, both fall back to `original` rather than discarding a
    validated (if incomplete) result for nothing."""
    feedback = HumanMessage(
        content=(
            "Your summary above did not mention the following citation(s), even though the "
            f"findings actually contained them: {', '.join(sorted(missing))}. Rewrite the "
            "summary to include every one of them, without dropping anything you already "
            "included correctly."
        )
    )
    try:
        retry_result = await context.llm.astructured(
            [*messages, feedback],
            schema=AggregatedFindings,
            reasoning=True,
            temperature=_AGGREGATE_TEMPERATURE,
        )
    except AppError as exc:
        logger.warning("rlm_aggregate_retry_failed", error=str(exc))
        return original

    retry_missing = _missing_citations(findings_text, retry_result.summary)
    if len(retry_missing) < len(missing):
        logger.info(
            "rlm_aggregate_retry_improved",
            missing_before=len(missing),
            missing_after=len(retry_missing),
        )
        return retry_result
    logger.warning(
        "rlm_aggregate_retry_did_not_improve",
        missing_before=len(missing),
        missing_after=len(retry_missing),
    )
    return original


def build_aggregate(context: RLMContext) -> Callable[[list[str], str], dict[str, Any]]:
    async def _aggregate(findings: list[str], question: str) -> dict[str, Any]:
        findings_text = "\n".join(f"- {finding}" for finding in findings) or "(no findings)"
        messages: list[SystemMessage | HumanMessage] = [
            SystemMessage(
                content=(
                    "Combine the sub-agent findings below into one synthesized answer. "
                    "Preserve every distinct incident, date, and root cause the findings name, "
                    "and keep their citations intact — do not drop, merge, or average them "
                    "into vague generalities, and do not state a count or fact the findings "
                    "below do not actually contain."
                )
            ),
            HumanMessage(content=f"Original question: {question}\n\nFindings:\n{findings_text}"),
        ]
        try:
            result = await context.llm.astructured(
                messages,
                schema=AggregatedFindings,
                reasoning=True,
                temperature=_AGGREGATE_TEMPERATURE,
            )
        except AppError as exc:
            logger.warning("rlm_aggregate_failed", error=str(exc))
            return {"summary": " ".join(findings) or "(no findings)", "recurring_themes": []}

        # A completeness guard, not a correctness one: this can only catch evidence that a
        # sub-agent actually returned and the aggregation step then dropped — it has no way to
        # know about, and does not claim to fix, evidence the search/planning stages never
        # retrieved in the first place (`docs/ASSUMPTIONS_AND_TRADEOFFS.md` trade-off 28's
        # second addendum is explicit about that distinction).
        missing = _missing_citations(findings_text, result.summary)
        if missing:
            logger.warning(
                "rlm_aggregate_incomplete", missing_citations=sorted(missing), question=question
            )
            result = await _retry_aggregate_once(
                context,
                messages=messages,
                findings_text=findings_text,
                original=result,
                missing=missing,
            )
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
        "group_by_document": group_by_document,
        "count_by_month": count_by_month,
        "sub_agent": build_sub_agent(context),
        "sub_agents": build_sub_agents(context),
        "aggregate": build_aggregate(context),
    }
