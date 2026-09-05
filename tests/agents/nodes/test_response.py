"""Tests for the pure, `get_stream_writer`-free helpers in `agents/nodes/response.py`.

`response_node` itself is not unit-tested directly, matching `tests/agents/nodes/test_tools.py`'s
precedent. `_build_system_prompt` exists specifically because of a live-verified bug: an earlier
version baked "if no evidence, say no documents were consulted" unconditionally into the system
prompt, which actively misled the model on a `"research"`/`"tools"` turn where `retrieved_chunks`
is legitimately empty but `research_output`/`tool_output` carries a real answer — the model
followed the unconditional instruction and discarded genuine research findings. These tests pin
the three-way condition that fixes it.
"""

from __future__ import annotations

from datetime import date

from backend.app.agents.nodes.response import _build_system_prompt, _format_evidence
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


class TestFormatEvidence:
    def test_empty_chunks_produce_the_explicit_no_evidence_marker(self) -> None:
        assert _format_evidence([]) == "(no evidence retrieved for this turn)"

    def test_chunks_are_rendered_with_title_and_section(self) -> None:
        text = _format_evidence([_chunk()])

        assert "[Payments Incident Report]" in text
        assert "the gateway timed out" in text


class TestBuildSystemPrompt:
    def test_says_no_documents_consulted_when_nothing_at_all_was_found(self) -> None:
        prompt = _build_system_prompt(
            chunks=[], tool_output=None, research_output=None, validation_feedback=None
        )

        assert "no internal documents were consulted" in prompt

    def test_does_not_say_no_documents_consulted_when_research_output_is_present(self) -> None:
        """The regression this module exists to prevent: `retrieved_chunks` is empty on the
        `"research"` route, but `research_output` carries a real finding."""
        prompt = _build_system_prompt(
            chunks=[],
            tool_output=None,
            research_output="Root cause: gateway timeouts recurred in 4 of 5 incidents.",
            validation_feedback=None,
        )

        assert "no internal documents were consulted" not in prompt
        assert "Root cause: gateway timeouts recurred" in prompt

    def test_does_not_say_no_documents_consulted_when_tool_output_is_present(self) -> None:
        prompt = _build_system_prompt(
            chunks=[],
            tool_output="Found employee: Jane Doe, Payments team.",
            research_output=None,
            validation_feedback=None,
        )

        assert "no internal documents were consulted" not in prompt
        assert "Jane Doe" in prompt

    def test_does_not_say_no_documents_consulted_when_chunks_are_present(self) -> None:
        prompt = _build_system_prompt(
            chunks=[_chunk()], tool_output=None, research_output=None, validation_feedback=None
        )

        assert "no internal documents were consulted" not in prompt

    def test_validation_feedback_is_appended_as_a_revision_instruction(self) -> None:
        prompt = _build_system_prompt(
            chunks=[],
            tool_output=None,
            research_output=None,
            validation_feedback="The answer was empty.",
        )

        assert "The answer was empty." in prompt
        assert "Revise the answer" in prompt
