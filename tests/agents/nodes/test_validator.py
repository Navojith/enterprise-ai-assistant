"""Tests for `_validate_structurally`, the Validator node's pure pass/fail check
(`agents/nodes/validator.py`'s docstring on why this is this cycle's whole check, and what
Cycle 6 replaces it with)."""

from __future__ import annotations

from backend.app.agents.nodes.validator import _validate_structurally


def test_an_empty_answer_fails() -> None:
    assert _validate_structurally(answer="", had_evidence=False) == "The answer was empty."


def test_a_whitespace_only_answer_fails() -> None:
    assert _validate_structurally(answer="   \n  ", had_evidence=False) is not None


def test_a_non_empty_answer_with_no_evidence_needed_passes() -> None:
    assert _validate_structurally(answer="Hello there!", had_evidence=False) is None


def test_an_answer_with_evidence_but_no_citation_fails() -> None:
    feedback = _validate_structurally(
        answer="The root cause was a race condition.", had_evidence=True
    )

    assert feedback is not None
    assert "citation" in feedback.lower()


def test_an_answer_with_evidence_and_a_bracketed_citation_passes() -> None:
    answer = "The root cause was a race condition [Incident Report INC-042]."

    assert _validate_structurally(answer=answer, had_evidence=True) is None
