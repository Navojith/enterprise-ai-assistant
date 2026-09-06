"""Tests for `guardrails/brand.py` — the persona and system-prompt-leak checks
`agents/nodes/validator.py` runs against every draft answer."""

from __future__ import annotations

from backend.app.guardrails.brand import (
    check_brand_violation,
    check_persona_violation,
    check_system_prompt_leak,
)

_SYSTEM_PROMPT = (
    "You are the AI assistant for a commercial bank's internal staff. Answer clearly and "
    "concisely, using only the evidence below when it is present."
)


class TestCheckPersonaViolation:
    def test_an_ordinary_answer_passes(self) -> None:
        assert check_persona_violation("The gateway timed out due to a race condition.") is None

    def test_a_generic_ai_model_disclaimer_is_flagged(self) -> None:
        feedback = check_persona_violation("As an AI language model, I cannot access that.")

        assert feedback is not None

    def test_claiming_to_be_a_different_provider_is_flagged(self) -> None:
        assert check_persona_violation("I am ChatGPT, here to help.") is not None

    def test_claiming_to_be_trained_by_a_different_company_is_flagged(self) -> None:
        assert check_persona_violation("I was created by OpenAI.") is not None


class TestCheckSystemPromptLeak:
    def test_an_unrelated_answer_passes(self) -> None:
        assert (
            check_system_prompt_leak("The root cause was a race condition.", _SYSTEM_PROMPT) is None
        )

    def test_a_verbatim_run_of_prompt_words_is_flagged(self) -> None:
        answer = "Sure! You are the AI assistant for a commercial bank's internal staff."

        assert check_system_prompt_leak(answer, _SYSTEM_PROMPT) is not None

    def test_matching_is_case_insensitive(self) -> None:
        answer = "you are the ai assistant for a commercial bank's internal staff, apparently."

        assert check_system_prompt_leak(answer, _SYSTEM_PROMPT) is not None

    def test_a_short_incidental_word_overlap_is_not_flagged(self) -> None:
        answer = "You are correct that the bank's internal team resolved it quickly."

        assert check_system_prompt_leak(answer, _SYSTEM_PROMPT) is None


class TestCheckBrandViolation:
    def test_combines_both_checks_persona_first(self) -> None:
        assert (
            check_brand_violation("As an AI language model, I can help.", _SYSTEM_PROMPT)
            is not None
        )

    def test_a_clean_answer_passes_both(self) -> None:
        assert check_brand_violation("The root cause was a timeout.", _SYSTEM_PROMPT) is None
