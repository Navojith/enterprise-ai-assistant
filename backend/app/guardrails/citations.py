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
    """Returns every citation in `answer` that names neither a retrieved chunk's title nor a
    non-chunk evidence channel that was actually present this turn (`response.py::
    _build_system_prompt` labels those sections literally "Tool result" / "Research findings")
    — i.e. every hallucinated citation. Empty means every citation in the answer is real.

    A label is only accepted when the corresponding evidence was actually present this turn: a
    `[Tool result]` citation on a turn where no tool ran is just as hallucinated as citing a
    document that was never retrieved.
    """
    allowed = {chunk.title.lower() for chunk in retrieved_chunks}
    if tool_output:
        allowed.add("tool result")
    if research_output:
        allowed.add("research findings")

    return [citation for citation in extract_citations(answer) if citation.lower() not in allowed]
