"""Tests for `_latest_user_text` and `_merge_prioritizing_scoped`, the pure,
`get_stream_writer`-free helpers in `agents/nodes/retrieval.py`.

`retrieval_node` itself is not unit-tested directly, matching `tests/agents/nodes/
test_tools.py`'s precedent: `get_stream_writer()` raises `RuntimeError` outside a real graph
invocation, so every node in this codebase is verified live end to end rather than by faking
that context — only the logic a node delegates to a plain function gets a unit test.
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage

from backend.app.agents.nodes.retrieval import _latest_user_text, _merge_prioritizing_scoped
from backend.app.retrieval.models import RetrievedChunk


def _chunk(chunk_id: str, *, department: str = "payments") -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk_id,
        document_id=chunk_id.split("::")[0],
        section="Purpose",
        text="text",
        title="Title",
        department=department,
        document_type="runbook",
        access_level="internal",
        created_date="2026-01-01",
        score=1.0,
    )


class TestLatestUserText:
    def test_returns_the_most_recent_human_message(self) -> None:
        messages: list[AnyMessage] = [
            HumanMessage(content="first question"),
            AIMessage(content="an answer"),
            HumanMessage(content="second question"),
        ]

        assert _latest_user_text(messages) == "second question"

    def test_raises_when_there_is_no_human_message(self) -> None:
        messages: list[AnyMessage] = [AIMessage(content="an answer")]

        with pytest.raises(ValueError, match="no user message"):
            _latest_user_text(messages)


class TestMergePrioritizingScoped:
    """See `docs/ASSUMPTIONS_AND_TRADEOFFS.md` trade-off 26's postscript: a department-scoped
    search must never *replace* the all-department search, only be prioritized within it — a
    wrong department guess must degrade to the same recall as no guess at all, never to zero."""

    def test_scoped_hits_are_kept_even_when_unscoped_would_have_ranked_them_lower(self) -> None:
        scoped = [_chunk("a"), _chunk("b")]
        unscoped = [_chunk("c"), _chunk("d")]

        merged = _merge_prioritizing_scoped(scoped, unscoped, top_k=8)

        assert [chunk.chunk_id for chunk in merged] == ["a", "b", "c", "d"]

    def test_a_chunk_present_in_both_lists_is_not_duplicated(self) -> None:
        scoped = [_chunk("a")]
        unscoped = [_chunk("a"), _chunk("b")]

        merged = _merge_prioritizing_scoped(scoped, unscoped, top_k=8)

        assert [chunk.chunk_id for chunk in merged] == ["a", "b"]

    def test_no_department_identified_falls_back_to_the_unscoped_list_unchanged(self) -> None:
        """The critical safety property: an empty `scoped` list (no department identified, or
        the guessed department's own search came back empty) must never lose recall relative to
        searching every department — it is a pure fallback, not a narrowing."""
        unscoped = [_chunk("a"), _chunk("b"), _chunk("c")]

        merged = _merge_prioritizing_scoped([], unscoped, top_k=8)

        assert merged == unscoped

    def test_result_is_capped_at_top_k_even_when_both_lists_are_full(self) -> None:
        scoped = [_chunk(f"s{i}") for i in range(5)]
        unscoped = [_chunk(f"u{i}") for i in range(5)]

        merged = _merge_prioritizing_scoped(scoped, unscoped, top_k=6)

        assert len(merged) == 6
        assert [chunk.chunk_id for chunk in merged[:5]] == [f"s{i}" for i in range(5)]
        assert merged[5].chunk_id == "u0"

    def test_a_wrong_department_guess_still_surfaces_the_correct_chunk_from_the_safety_net(
        self,
    ) -> None:
        """The exact regression this function fixes: a scoped search into the *wrong*
        department finds nothing relevant, but the correct chunk is still present because the
        all-department search always runs too."""
        scoped = [_chunk("wrong-dept-chunk", department="core_banking")]
        unscoped = [_chunk("correct-chunk", department="security"), _chunk("wrong-dept-chunk")]

        merged = _merge_prioritizing_scoped(scoped, unscoped, top_k=8)

        assert "correct-chunk" in [chunk.chunk_id for chunk in merged]
