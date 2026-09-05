"""Tests for the graph's conditional-edge routing functions, kept plain `state -> str`
functions specifically so they are testable without a compiled graph, a checkpointer, or a
`Runtime` — see `_make_after_validator`'s docstring in `agents/graph.py` for why."""

from __future__ import annotations

from langgraph.graph import END

from backend.app.agents.graph import _after_supervisor, _make_after_validator
from backend.app.agents.state import AgentState


def _state(**overrides: object) -> AgentState:
    base: AgentState = {"retry_count": 0, "validation_passed": None}
    base.update(overrides)  # type: ignore[typeddict-item]
    return base


class TestAfterSupervisor:
    def test_routes_to_retrieval_when_the_supervisor_chose_it(self) -> None:
        assert _after_supervisor(_state(route="retrieval")) == "retrieval"

    def test_routes_to_tools_when_the_supervisor_chose_it(self) -> None:
        assert _after_supervisor(_state(route="tools")) == "tools"

    def test_routes_to_research_when_the_supervisor_chose_it(self) -> None:
        assert _after_supervisor(_state(route="research")) == "research"

    def test_routes_to_response_for_the_direct_route(self) -> None:
        assert _after_supervisor(_state(route="direct")) == "response"

    def test_an_unset_route_falls_back_to_response(self) -> None:
        assert _after_supervisor(_state()) == "response"


class TestAfterValidator:
    def test_ends_once_validation_has_passed(self) -> None:
        after_validator = _make_after_validator(max_validator_retries=1)

        assert after_validator(_state(validation_passed=True, retry_count=0)) == END

    def test_retries_when_validation_failed_and_budget_remains(self) -> None:
        after_validator = _make_after_validator(max_validator_retries=1)

        assert after_validator(_state(validation_passed=False, retry_count=1)) == "response"

    def test_ends_once_the_retry_budget_is_exhausted(self) -> None:
        after_validator = _make_after_validator(max_validator_retries=1)

        assert after_validator(_state(validation_passed=False, retry_count=2)) == END

    def test_retry_count_exactly_at_the_budget_still_retries(self) -> None:
        """The bound is `>`, not `>=` — a `retry_count` equal to the budget means that many
        retries have been *allowed*, not yet *exhausted*."""
        after_validator = _make_after_validator(max_validator_retries=2)

        assert after_validator(_state(validation_passed=False, retry_count=2)) == "response"
