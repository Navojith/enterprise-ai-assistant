"""Tests for `guardrails/citations.py` — the hallucinated-citation check `agents/nodes/
validator.py` layers on top of the pre-existing "cited if evidenced" structural check."""

from __future__ import annotations

from datetime import date

from backend.app.guardrails.citations import extract_citations, verify_citations
from backend.app.retrieval.models import RetrievedChunk


def _chunk(title: str = "Payments Incident Report") -> RetrievedChunk:
    return RetrievedChunk.model_validate(
        {
            "chunk_id": "doc::sec",
            "document_id": "doc",
            "section": "Summary",
            "text": "the gateway timed out",
            "title": title,
            "department": "payments",
            "document_type": "incident",
            "access_level": "internal",
            "created_date": date(2025, 1, 1).isoformat(),
            "score": 1.0,
        }
    )


class TestExtractCitations:
    def test_extracts_every_bracketed_span_in_order(self) -> None:
        answer = "The cause was X [Report A]. It recurred [Report B]."

        assert extract_citations(answer) == ["Report A", "Report B"]

    def test_no_brackets_yields_an_empty_list(self) -> None:
        assert extract_citations("No citations here.") == []


class TestVerifyCitations:
    def test_a_citation_matching_a_retrieved_chunk_is_not_hallucinated(self) -> None:
        answer = "The gateway timed out [Payments Incident Report]."

        assert (
            verify_citations(
                answer=answer, retrieved_chunks=[_chunk()], tool_output=None, research_output=None
            )
            == []
        )

    def test_citation_matching_is_case_insensitive(self) -> None:
        answer = "See [payments incident report] for details."

        assert (
            verify_citations(
                answer=answer, retrieved_chunks=[_chunk()], tool_output=None, research_output=None
            )
            == []
        )

    def test_a_fabricated_title_is_flagged(self) -> None:
        answer = "As shown in [A Report That Was Never Retrieved]."

        hallucinated = verify_citations(
            answer=answer, retrieved_chunks=[_chunk()], tool_output=None, research_output=None
        )

        assert hallucinated == ["A Report That Was Never Retrieved"]

    def test_tool_result_citation_is_allowed_only_when_a_tool_actually_ran(self) -> None:
        answer = "Found the employee [Tool result]."

        assert (
            verify_citations(
                answer=answer, retrieved_chunks=[], tool_output="Jane Doe", research_output=None
            )
            == []
        )
        assert verify_citations(
            answer=answer, retrieved_chunks=[], tool_output=None, research_output=None
        ) == ["Tool result"]

    def test_research_findings_citation_is_allowed_only_when_research_actually_ran(self) -> None:
        answer = "See the summary [Research findings]."

        assert (
            verify_citations(
                answer=answer,
                retrieved_chunks=[],
                tool_output=None,
                research_output="four recurring causes",
            )
            == []
        )
        assert verify_citations(
            answer=answer, retrieved_chunks=[], tool_output=None, research_output=None
        ) == ["Research findings"]

    def test_no_citations_at_all_is_trivially_not_hallucinated(self) -> None:
        assert (
            verify_citations(
                answer="A plain answer with no brackets.",
                retrieved_chunks=[_chunk()],
                tool_output=None,
                research_output=None,
            )
            == []
        )
