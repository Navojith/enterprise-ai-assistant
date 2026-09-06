"""Shared temporal grounding for every LLM system prompt that reasons about dates.

`qwen3:4b`'s training data has a cutoff well before this project's real present-day setting, and
nothing in any prompt told it otherwise. Live testing found that mismatch leaks directly into
behavior, not just idle chit-chat: asked to analyze "payment incidents... in 2026", the
Supervisor's routing call reasoned "the year 2026 is in the future, so there are no incidents for
that year yet" and routed a real, answerable question to `"direct"` instead of `"retrieval"`
(skipping evidence entirely because it had already, silently, decided the answer must be zero);
the Response node then confidently refused the same real business question ("Payment incidents
for 2026 cannot be analyzed as the year has not yet occurred and no future data is available":
its own captured reasoning trace stated outright, "the current year is 2023, and 2026 hasn't
happened yet") — a confidently wrong premise, not a hedge, and one that a real analyst asking
about the bank's own real, already-occurred incident data would have no way to know was false
without checking the trace themselves. `docs/ASSUMPTIONS_AND_TRADEOFFS.md` trade-off 31 has the
full investigation.

Every system prompt where the model reasons about the user's request in a way a wrong sense of
"now" could distort — the Supervisor's routing/department/search-query decision, the Response
node's final answer, the RLM planner's search plan — includes this line so the model's own stale
internal calendar never overrides the real one. `agents/nodes/response.py::SYSTEM_PROMPT` stays a
fixed constant (`guardrails/brand.py::check_system_prompt_leak` depends on that for its
verbatim-leak check), so this is appended at call time rather than baked into any fixed prompt
string.
"""

from __future__ import annotations

from datetime import datetime


def current_date_context(*, now: datetime) -> str:
    """One line of grounding text for a system prompt. Takes `now` explicitly rather than
    calling `datetime.now()` itself, so every caller stays unit-testable with a fixed date —
    the same pure-logic/real-clock split `llm/chain.py`'s `CircuitBreaker`/`_is_open` already
    uses for the identical reason."""
    return f"Today's date is {now.date().isoformat()}."
