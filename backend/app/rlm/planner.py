"""Schema-constrained RLM plan generation: turns a natural-language research question into a
short, real Python program against `rlm/api.py`'s curated API — `search`/`filter`/`batch`/
`sub_agent`/`sub_agents`/`aggregate` — validated by `rlm/sandbox.py`'s AST allowlist before it is
ever considered runnable.

Three-stage generation, matching `docs/DELIVERY_PLAN.md`'s "deterministic fallback plan when
generated code fails validation":

1. Ask the model once for a plan, with reasoning **on** (`reasoning=True` —
   `docs/DECISIONS.md` §5: plan-generation quality is worth the extra tokens the Supervisor and
   Validator deliberately skip).
2. If the generated code fails the AST allowlist, retry exactly once, feeding the specific
   violations back as feedback — the same "one retry with concrete feedback" shape
   `agents/nodes/validator.py`'s retry loop already uses, applied here to code instead of prose.
3. If the retry also fails, or the model call itself errors (LLM unavailable, timeout), fall back
   to `deterministic_fallback_plan` — fixed, hand-written Python, never model output, so it
   cannot fail the AST allowlist it was written against. A research turn should never fail
   outright just because a 4B model produced unparseable Python this time.
"""

from __future__ import annotations

from langchain_core.messages import AnyMessage, HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from backend.app.core.errors import AppError, SandboxViolationError
from backend.app.llm.provider import LLMProvider
from backend.app.rlm.sandbox import validate_ast

_PLAN_GENERATION_ATTEMPTS = 2

_SYSTEM_PROMPT = (
    "You are generating a short Python search plan to research a question over a large "
    "internal document collection, without loading every document into context.\n\n"
    "You may call ONLY these functions, already available in your execution environment:\n"
    "- search(query: str, top_k: int = 10) -> list[dict]: hybrid dense+sparse search, "
    "returns chunk dicts with keys text, title, section, department, document_type, "
    "created_date, score.\n"
    "- filter(chunks: list[dict], contains: str = None, document_type: str = None, "
    "department: str = None) -> list[dict]: narrow a list of chunk dicts.\n"
    "- batch(chunks: list[dict], size: int) -> list[list[dict]]: split into fixed-size "
    "batches.\n"
    "- sub_agent(question: str, chunks: list[dict]) -> str: recursively analyze ONE batch "
    "and return a finding.\n"
    "- sub_agents(question: str, batches: list[list[dict]]) -> list[str]: analyze MULTIPLE "
    "batches concurrently, one finding per batch — prefer this over a loop of sub_agent "
    "calls so batches are analyzed concurrently, not one at a time.\n"
    "- aggregate(findings: list[str], question: str) -> dict: combine findings into "
    '{"summary": str, "recurring_themes": list[str]}.\n\n'
    "The variable `question` already holds the exact user question text — use it directly "
    "instead of retyping it.\n\n"
    "Rules: no imports, no file access, no dunder attribute access, no with/global/nonlocal "
    "statements. Assign your final answer to a variable named `result` (the dict aggregate "
    "returns is a good choice). Keep the plan short."
)


class ResearchPlan(BaseModel):
    """Field order mirrors `RoutingDecision` (`agents/nodes/supervisor.py`) for the same
    reason: under schema-constrained decoding the model commits to fields in declared order, so
    `reasoning` — a short description of the *strategy*, not the code itself — comes before
    `code` to force deliberation before commitment."""

    reasoning: str = Field(
        description="One or two sentences on the search strategy, written before writing the code."
    )
    code: str = Field(description="The Python plan, using only the functions described above.")


def deterministic_fallback_plan(question: str) -> str:
    """Fixed, hand-written fallback: search once, batch generously, analyze concurrently,
    aggregate. Never fails the AST allowlist — it is not model output — and is the floor this
    codebase's error-handling philosophy asks for everywhere else: a degraded answer, never a
    hard failure, when the fast path (a 4B model writing correct Python on the first or second
    try) does not pan out.
    """
    return (
        "chunks = search(question, top_k=20)\n"
        "batches = batch(chunks, 5)\n"
        "findings = sub_agents(question, batches)\n"
        "result = aggregate(findings, question)\n"
    )


def _retry_messages(
    question: str, *, previous_code: str, violation: SandboxViolationError
) -> list[AnyMessage]:
    return [
        SystemMessage(content=_SYSTEM_PROMPT),
        HumanMessage(content=question),
        HumanMessage(
            content=(
                f"Your previous plan:\n{previous_code}\n\n"
                f"That plan failed validation: {violation.message} "
                f"Violations: {(violation.details or {}).get('violations')}. "
                "Write a corrected plan using only the allowed functions, no imports, no "
                "dunder access, no with/global/nonlocal statements."
            )
        ),
    ]


async def generate_plan(question: str, *, llm: LLMProvider) -> tuple[str, bool]:
    """Returns `(code, used_fallback)`. `used_fallback=True` tells `rlm/executor.py` (and,
    through it, the Agent Activity Panel) that the deterministic plan ran instead of a generated
    one — a real degradation worth showing, not silently swallowed."""
    messages: list[AnyMessage] = [
        SystemMessage(content=_SYSTEM_PROMPT),
        HumanMessage(content=question),
    ]

    for _attempt in range(1, _PLAN_GENERATION_ATTEMPTS + 1):
        try:
            plan = await llm.astructured(messages, schema=ResearchPlan, reasoning=True)
        except AppError:
            # An unavailable/timed-out model is not worth a second attempt at prompting —
            # go straight to the deterministic plan, matching `llm/ollama_provider.py`'s own
            # one-retry-then-give-up shape for a different failure class.
            break
        try:
            validate_ast(plan.code)
        except SandboxViolationError as exc:
            messages = _retry_messages(question, previous_code=plan.code, violation=exc)
            continue
        return plan.code, False

    return deterministic_fallback_plan(question), True
