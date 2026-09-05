"""Tests for `merge_retrieved_chunks`, the reducer `AgentState` uses for concurrent writers to
`retrieved_chunks` (docs: the module docstring on `agents/state.py`)."""

from __future__ import annotations

from backend.app.agents.state import merge_retrieved_chunks
from backend.app.retrieval.models import RetrievedChunk


def _chunk(chunk_id: str, *, score: float) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk_id,
        document_id="doc-1",
        section="root-cause",
        text="some text",
        title="Some Incident",
        department="payments",
        document_type="incident",
        access_level="internal",
        created_date="2026-01-01",
        score=score,
    )


def test_new_chunks_are_appended_to_an_empty_list() -> None:
    result = merge_retrieved_chunks([], [_chunk("a", score=0.5)])

    assert [chunk.chunk_id for chunk in result] == ["a"]


def test_disjoint_chunk_ids_are_concatenated() -> None:
    result = merge_retrieved_chunks([_chunk("a", score=0.5)], [_chunk("b", score=0.9)])

    assert {chunk.chunk_id for chunk in result} == {"a", "b"}


def test_a_repeated_chunk_id_keeps_the_higher_scoring_instance() -> None:
    low = _chunk("a", score=0.2)
    high = _chunk("a", score=0.8)

    result = merge_retrieved_chunks([low], [high])

    assert len(result) == 1
    assert result[0].score == 0.8


def test_a_repeated_chunk_id_with_a_lower_incoming_score_keeps_the_existing_one() -> None:
    high = _chunk("a", score=0.8)
    low = _chunk("a", score=0.2)

    result = merge_retrieved_chunks([high], [low])

    assert len(result) == 1
    assert result[0].score == 0.8


def test_merging_two_empty_lists_is_empty() -> None:
    assert merge_retrieved_chunks([], []) == []
