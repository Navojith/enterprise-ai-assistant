"""`LLMProvider`: the one interface every graph node uses to talk to a model.

Nodes depend on this `Protocol`, never on `ChatOllama` or any other concrete client — so a node
is testable against a fake provider with no network or GPU involved, and so
`docs/DECISIONS.md` §7's "swap the provider is a configuration change, not a rewrite" claim is
actually true rather than aspirational. `llm/ollama_provider.py` is the only implementation
today; `llm/chain.py` composes one or more implementations behind the same interface.

Two methods, matching the two ways a node ever needs a model:

- `astructured` — one-shot, JSON-schema-constrained decoding. The Supervisor's routing decision
  and the Validator's verdict both need a value that conforms to a schema, not prose to parse
  (`docs/DECISIONS.md` §5) — this returns a validated instance of the caller's own Pydantic model
  directly, so a malformed response is a raised exception, never a value the caller must
  remember to re-validate itself.
- `astream` — free-text token streaming, for the Response node's final answer. It yields
  LangChain `BaseMessageChunk`s (not a bespoke chunk type) because that is exactly what
  `ChatOllama.astream()` already produces and what `agents/nodes/response.py` needs to
  accumulate into a final answer — wrapping it in a new type would only add a translation step
  with nothing to translate. A chunk's `.content` is the answer delta; `.additional_kwargs.get(
  "reasoning_content")` is the model's thinking delta, present only when `reasoning=True` (see
  the module docstring on `ollama_provider.py` for why that flag matters).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Protocol, TypeVar

from langchain_core.messages import BaseMessage, BaseMessageChunk
from pydantic import BaseModel

SchemaT = TypeVar("SchemaT", bound=BaseModel)


class LLMProvider(Protocol):
    """Structural interface for a chat-completion backend. See module docstring."""

    async def astructured(
        self,
        messages: Sequence[BaseMessage],
        *,
        schema: type[SchemaT],
        reasoning: bool = False,
        temperature: float | None = None,
    ) -> SchemaT:
        """Return one validated `schema` instance, decoded under a JSON-schema grammar.

        `reasoning` defaults to `False` — the Supervisor and Validator want the fastest,
        cheapest decision (`docs/DECISIONS.md` §5) — but the signature accepts `True` for
        Cycle 5's RLM planner, where reasoning quality is worth the extra tokens.

        `temperature` defaults to `None` — the provider's own default sampling, unchanged from
        every call site that existed before this parameter did. A caller passes an explicit
        value only when lower variance is worth more than it costs: `rlm/api.py::build_aggregate`
        is the first such caller, since synthesizing several findings into one summary is a
        task where the model should faithfully combine what it was given rather than explore
        alternative phrasings (`docs/ASSUMPTIONS_AND_TRADEOFFS.md` trade-off 28's second
        addendum).
        """
        ...

    def astream(
        self, messages: Sequence[BaseMessage], *, reasoning: bool = True
    ) -> AsyncIterator[BaseMessageChunk]:
        """Stream a free-text completion. `reasoning` defaults to `True` here — the opposite
        default from `astructured` — because free-text generation is the one call shape where
        Ollama cannot reliably suppress `qwen3`'s thinking mode at all (see
        `docs/ASSUMPTIONS_AND_TRADEOFFS.md` trade-off 13); the Response node embraces it and
        forwards the separated reasoning to the Agent Activity Panel instead of fighting it.
        """
        ...
