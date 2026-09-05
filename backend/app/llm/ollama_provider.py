"""`ChatOllama`-backed `LLMProvider`, encoding two findings verified live against this exact
Ollama install (`docs/ASSUMPTIONS_AND_TRADEOFFS.md` trade-off 13) rather than assumed from the
library's defaults:

1. **Force full GPU residency.** Ollama's automatic layer-placement heuristic left a third of
   `qwen3:4b` on the CPU by default, cutting throughput from a measured ~57 tok/s to ~18 tok/s
   even though the whole model fits in 4 GB of VRAM. `_FORCE_FULL_GPU_OFFLOAD` is passed as
   `num_gpu` on every request — never left to the default.
2. **Never disable reasoning without also constraining the schema.** `think: false` only behaves
   correctly on `/api/chat` when a `format` (JSON schema) is also set in the same call; without
   `format` it leaves the raw `<think>...</think>` block concatenated into the answer instead of
   suppressing it. `astructured()` always sets both together; `astream()` never sets `think:
   false` at all — it takes `reasoning` as a plain on/off switch on the *default* (already-clean)
   splitting behavior instead of fighting it.

Uses `ChatOllama` (from `langchain-ollama`, already a pinned dependency) rather than the raw
`ollama` client: it already implements exactly these request shapes (`with_structured_output`'s
`method="json_schema"` maps straight to Ollama's `format` parameter), and — because it is a
LangChain chat model — a call made inside a LangGraph node is traced to LangSmith and visible to
`astream_events` automatically, with no separate instrumentation to write or keep in sync.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence

import structlog
from langchain_core.messages import BaseMessage, BaseMessageChunk
from langchain_ollama import ChatOllama
from pydantic import ValidationError

from backend.app.core.config import Settings
from backend.app.core.errors import LLMError, LLMTimeoutError, LLMUnavailableError
from backend.app.llm.provider import SchemaT

logger = structlog.get_logger(__name__)

# Verified live at the start of Cycle 3 — see the module docstring. 99 exceeds any real layer
# count, which Ollama treats as "as many layers as fit" rather than an error.
_FORCE_FULL_GPU_OFFLOAD = 99

# A single malformed-JSON retry is enough for a schema grammar that already constrains every
# token — a second consecutive failure is far more likely a genuinely unavailable model than a
# one-off sampling fluke, and is left to the fallback chain (`llm/chain.py`) instead.
_STRUCTURED_OUTPUT_ATTEMPTS = 2


class OllamaProvider:
    """`LLMProvider` for one local Ollama model. See module docstring for the two request-shape
    rules this class exists to enforce on every call, not just the happy path."""

    def __init__(self, settings: Settings) -> None:
        self._request_timeout = settings.llm_request_timeout_seconds
        self._stream_stall_timeout = settings.llm_stream_stall_timeout_seconds
        self._model = ChatOllama(
            model=settings.ollama_model,
            base_url=settings.ollama_base_url,
            num_gpu=_FORCE_FULL_GPU_OFFLOAD,
            # A per-call `reasoning=` kwarg (passed at every call site below) overrides this;
            # the instance-level default only matters for a call site that forgets to set one,
            # so it is deliberately the *safe* choice (reasoning on, format absent) rather than
            # the fast one — see trade-off 13's "broken" row for what the fast choice does when
            # a call site forgets to pair it with `format`.
            reasoning=None,
        )

    async def astructured(
        self,
        messages: Sequence[BaseMessage],
        *,
        schema: type[SchemaT],
        reasoning: bool = False,
    ) -> SchemaT:
        structured_model = self._model.with_structured_output(schema, method="json_schema")
        last_error: Exception | None = None
        for attempt in range(1, _STRUCTURED_OUTPUT_ATTEMPTS + 1):
            try:
                result = await asyncio.wait_for(
                    structured_model.ainvoke(list(messages), reasoning=reasoning),
                    timeout=self._request_timeout,
                )
            except TimeoutError as exc:
                raise LLMTimeoutError(
                    f"Ollama did not respond within {self._request_timeout}s."
                ) from exc
            except ConnectionError as exc:
                raise LLMUnavailableError(f"Cannot reach Ollama: {exc}") from exc
            except Exception as exc:  # noqa: BLE001 - a parse/validation failure is retried below
                last_error = exc
                logger.warning(
                    "structured_output_attempt_failed",
                    attempt=attempt,
                    schema=schema.__name__,
                    error=str(exc),
                )
                continue

            if isinstance(result, schema):
                return result
            # `with_structured_output` without `include_raw` should always return a validated
            # instance of `schema` for a Pydantic schema — but re-validating a plain dict here
            # is cheap insurance against a version drift changing that contract silently.
            try:
                return schema.model_validate(result)
            except ValidationError as exc:
                last_error = exc
                logger.warning(
                    "structured_output_validation_failed",
                    attempt=attempt,
                    schema=schema.__name__,
                    error=str(exc),
                )

        raise LLMError(
            f"Model produced no schema-conformant {schema.__name__} after "
            f"{_STRUCTURED_OUTPUT_ATTEMPTS} attempts: {last_error}"
        ) from last_error

    async def astream(
        self, messages: Sequence[BaseMessage], *, reasoning: bool = True
    ) -> AsyncIterator[BaseMessageChunk]:
        # `.astream()` on a LangChain `Runnable` is itself lazy — no request is sent until the
        # first `__anext__()` — so every failure mode (connection refused, stall, mid-stream
        # drop) surfaces from the loop below, not from this call.
        stream_iter = self._model.astream(list(messages), reasoning=reasoning).__aiter__()
        while True:
            try:
                chunk = await asyncio.wait_for(
                    stream_iter.__anext__(), timeout=self._stream_stall_timeout
                )
            except StopAsyncIteration:
                return
            except TimeoutError as exc:
                raise LLMTimeoutError(
                    f"Ollama produced no token for {self._stream_stall_timeout}s mid-stream."
                ) from exc
            except ConnectionError as exc:
                raise LLMUnavailableError(f"Ollama connection dropped mid-stream: {exc}") from exc
            yield chunk
