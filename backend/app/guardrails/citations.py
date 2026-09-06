"""Citation verification: does every bracketed `[Title]` marker in a draft answer correspond to
something the turn actually retrieved?

`agents/nodes/response.py`'s system prompt instructs the model to cite every factual claim by
document title in square brackets. Cycle 3's Validator only checked *that a citation existed at
all* when evidence was retrieved; it never checked whether the citation was *real*. A 4B model
under pressure to look well-sourced is exactly the kind of model prone to fabricating a
plausible-looking title — this is ASSESSMENT.md's "Guardrails: hallucinated citations" bullet,
addressed with a plain string comparison rather than a second LLM call, since verifying a
citation is real or not is a membership check, not a judgment call.
"""

from __future__ import annotations

import re

from backend.app.retrieval.models import RetrievedChunk

_CITATION_PATTERN = re.compile(r"\[([^\[\]]{1,200})\]")


def extract_citations(answer: str) -> list[str]:
    """Every `[...]` bracketed span in `answer`, in order, including duplicates."""
    return _CITATION_PATTERN.findall(answer)


def verify_citations(
    *,
    answer: str,
    retrieved_chunks: list[RetrievedChunk],
    tool_output: str | None,
    research_output: str | None,
) -> list[str]:
    """Returns every citation in `answer` that is hallucinated — i.e. does not correspond to
    real evidence this turn actually had. Empty means every citation in the answer is real.

    **Retrieved chunks get an exact-title check** because they are several distinct, nameable
    sources: citing one that was never actually retrieved is a real fabrication, and the model
    cannot game this by choosing a different label — a chunk's title is what it is.

    **A tool result or research summary does not**, and this is a live-verified correction, not
    the original design: `response.py`'s system prompt labels that section "Tool result" /
    "Research findings" as a generic header, but a real MCP tool call naturally produced a
    citation naming the *tool* instead (`[Employee Directory]`, not `[Tool result]`) — and
    rejecting that as "hallucinated" repeatedly exhausted the Validator's retry budget for an
    answer that was never actually wrong (`docs/ASSUMPTIONS_AND_TRADEOFFS.md` trade-off 22).
    Unlike retrieved chunks, a tool call or a research turn is exactly *one* piece of real
    evidence, not several distinct sources to pick the wrong one from — there is no meaningful
    "which of several real things did this actually come from" check to make, so once either is
    present, no citation on that turn is flagged: the underlying evidence is genuinely real
    either way, and only the exact label text was ever in question.
    """
    citations = extract_citations(answer)
    if not citations:
        return []
    if tool_output or research_output:
        return []
    allowed_titles = {chunk.title.lower() for chunk in retrieved_chunks}
    return [citation for citation in citations if citation.lower() not in allowed_titles]
