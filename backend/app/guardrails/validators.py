"""Input and tool-parameter validation beyond what request/tool schemas already check.

`ChatRequest` (`api/v1/chat.py`) already bounds message length via Pydantic
(`Field(min_length=1, max_length=4000)`), and `tools/registry.py::execute` already validates a
tool call's arguments against its own `ToolSpec.params_schema`. Both checks are purely *shape* —
well-typed, in range — and neither catches a request that is shaped correctly but semantically
hostile: a whitespace-only message that still satisfies `min_length=1`, a control-character
payload, a pathological repeated-character string designed to burn tokens, or a tool argument
that itself smuggles an injection payload past the one place (`guardrails/injection.py`'s
heuristic screen) that otherwise only ever looks at the user's own chat message.
"""

from __future__ import annotations

import re
from typing import Any

from backend.app.core.errors import ValidationFailedError
from backend.app.guardrails.injection import InjectionVerdict, heuristic_screen

_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_PATHOLOGICAL_REPETITION = re.compile(r"(.)\1{199,}")


def validate_user_message(text: str) -> None:
    """Raises `ValidationFailedError` for a message that is well-typed but not well-formed.

    Distinct from injection detection (`guardrails/injection.py`): this checks *shape*, not
    intent, and runs before the graph even starts (`api/v1/chat.py::chat_stream`) — a clean 422
    is appropriate here since headers have not been sent yet, unlike a guardrail block raised
    from inside the graph after the SSE stream has already opened.
    """
    if not text.strip():
        raise ValidationFailedError("The message cannot be empty or whitespace-only.")
    if _CONTROL_CHARACTERS.search(text):
        raise ValidationFailedError("The message contains disallowed control characters.")
    if _PATHOLOGICAL_REPETITION.search(text):
        raise ValidationFailedError(
            "The message contains an excessively long repeated character sequence."
        )


def validate_tool_arguments(arguments: dict[str, Any]) -> None:
    """Recursively screens every string value in a validated tool-call payload against the same
    heuristic patterns applied to chat input (`guardrails/injection.py::heuristic_screen`) —
    defense in depth for a tool argument that itself carries an injection payload, independent
    of whatever screened the chat message that led here. Only a `BLOCK`-tier match raises; an
    `AMBIGUOUS` watchlist hit is let through unescalated, since there is no natural place to run
    the LLM classifier at this boundary without adding a call to every tool invocation
    regardless of role.
    """
    for value in _iter_strings(arguments):
        screen = heuristic_screen(value)
        if screen.verdict is InjectionVerdict.BLOCK:
            raise ValidationFailedError(
                f"Tool argument rejected: {screen.reason}.",
                details={"category": screen.category},
            )


def _iter_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _iter_strings(v)]
    if isinstance(value, list):
        return [s for item in value for s in _iter_strings(item)]
    return []
