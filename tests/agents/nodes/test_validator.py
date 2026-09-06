"""Tests for `_validate_answer`, the Validator node's pure pass/fail check
(`agents/nodes/validator.py`'s docstring on what Cycle 6 added on top of Cycle 3's
structural-only check: citation verification and the brand/persona guardrail)."""

from __future__ import annotations

from datetime import date

from backend.app.agents.nodes.validator import _validate_answer
from backend.app.retrieval.models import RetrievedChunk


def _chunk(title: str = "Incident Report INC-042") -> RetrievedChunk:
    return RetrievedChunk.model_validate(
        {
            "chunk_id": "doc::sec",
            "document_id": "doc",
            "section": "Summary",
            "text": "a race condition caused the outage",
            "title": title,
            "department": "payments",
            "document_type": "incident",
            "access_level": "internal",
            "created_date": date(2025, 1, 1).isoformat(),
            "score": 1.0,
        }
    )


class TestStructuralChecks:
    def test_an_empty_answer_fails(self) -> None:
        feedback = _validate_answer(
            answer="", retrieved_chunks=[], tool_output=None, research_output=None
        )

        assert feedback == "The answer was empty."

    def test_a_whitespace_only_answer_fails(self) -> None:
        feedback = _validate_answer(
            answer="   \n  ", retrieved_chunks=[], tool_output=None, research_output=None
        )

        assert feedback is not None

    def test_a_non_empty_answer_with_no_evidence_needed_passes(self) -> None:
        feedback = _validate_answer(
            answer="Hello there!", retrieved_chunks=[], tool_output=None, research_output=None
        )

        assert feedback is None

    def test_an_answer_with_evidence_but_no_citation_fails(self) -> None:
        feedback = _validate_answer(
            answer="The root cause was a race condition.",
            retrieved_chunks=[_chunk()],
            tool_output=None,
            research_output=None,
        )

        assert feedback is not None
        assert "citation" in feedback.lower()


class TestCitationVerification:
    def test_a_real_citation_passes(self) -> None:
        answer = "The root cause was a race condition [Incident Report INC-042]."

        feedback = _validate_answer(
            answer=answer, retrieved_chunks=[_chunk()], tool_output=None, research_output=None
        )

        assert feedback is None

    def test_a_fabricated_citation_fails(self) -> None:
        answer = "The root cause was a race condition [A Report That Does Not Exist]."

        feedback = _validate_answer(
            answer=answer, retrieved_chunks=[_chunk()], tool_output=None, research_output=None
        )

        assert feedback is not None
        assert "does not match" in feedback


class TestBrandGuardrail:
    def test_a_persona_violation_fails_even_with_no_evidence_involved(self) -> None:
        feedback = _validate_answer(
            answer="As an AI language model, I cannot help with that.",
            retrieved_chunks=[],
            tool_output=None,
            research_output=None,
        )

        assert feedback is not None

    def test_an_answer_that_passes_every_check_returns_none(self) -> None:
        answer = "The root cause was a race condition [Incident Report INC-042]."

        feedback = _validate_answer(
            answer=answer, retrieved_chunks=[_chunk()], tool_output=None, research_output=None
        )

        assert feedback is None
