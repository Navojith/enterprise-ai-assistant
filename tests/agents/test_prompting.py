"""Tests for `agents/prompting.py`'s shared temporal-grounding helper.

`docs/ASSUMPTIONS_AND_TRADEOFFS.md` trade-off 31: `qwen3:4b`'s own stale, pre-cutoff sense of
"now" leaked into both routing decisions and final answers once a real question mentioned a
year the model's training data predates. `current_date_context` is the fix every affected
prompt (`agents/nodes/supervisor.py`, `agents/nodes/response.py`, `rlm/planner.py`) now leads
with.
"""

from __future__ import annotations

from datetime import UTC, datetime

from backend.app.agents.prompting import current_date_context


class TestCurrentDateContext:
    def test_renders_an_iso_date(self) -> None:
        text = current_date_context(now=datetime(2026, 9, 6, tzinfo=UTC))

        assert text == "Today's date is 2026-09-06."

    def test_drops_the_time_of_day(self) -> None:
        """Only the date matters for grounding "this year"/"last year" reasoning — including a
        timestamp would just be one more thing for the model to (mis)parse."""
        text = current_date_context(now=datetime(2026, 1, 1, 23, 59, 59, tzinfo=UTC))

        assert text == "Today's date is 2026-01-01."
