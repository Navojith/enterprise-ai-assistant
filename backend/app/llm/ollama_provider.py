"""`ChatOllama`-backed `LLMProvider`, encoding three findings verified live against this exact
Ollama install rather than assumed from the library's defaults:

1. **Force full GPU residency** (`docs/ASSUMPTIONS_AND_TRADEOFFS.md` trade-off 13). Ollama's
   automatic layer-placement heuristic left a third of `qwen3:4b` on the CPU by default, cutting
   throughput from a measured ~57 tok/s to ~18 tok/s even though the whole model fits in 4 GB of
   VRAM. `_FORCE_FULL_GPU_OFFLOAD` is passed as `num_gpu` on every request — never left to the
   default.
2. **Never disable reasoning without also constraining the schema** (same trade-off). `think:
   false` only behaves correctly on `/api/chat` when a `format` (JSON schema) is also set in the
   same call; without `format` it leaves the raw `<think>...</think>` block concatenated into the
   answer instead of suppressing it. `astructured()` always sets both together; `astream()` never
   sets `think: false` at all — it takes `reasoning` as a plain on/off switch on the *default*
   (already-clean) splitting behavior instead of fighting it.
3. **`astructured()` bypasses `ChatOllama`'s own request path for a non-streaming one** (trade-off
   24, found containerizing this project). `ChatOllama.ainvoke()` always sends Ollama's *streamed*
   chat API internally, even for what looks like a single request-response call, aggregating the
   chunks itself. Verified live and reproduced directly against the raw Ollama HTTP API: a
   container reaching this project's intentionally-native Ollama over Docker Desktop's
   `host.docker.internal` NAT hit a reproducible, high (~80%) chance that a *streamed* response
   stalled after its headers arrived, which a non-streaming call to the identical endpoint mostly
   did not exhibit in the same testing. `astructured()` therefore calls Ollama's non-streaming API
   directly (still through `self._model`'s own configured client and parameter-building, so
   GPU/format/timeout behavior stays identical — see `_call_non_streaming` below), with a
   hand-rolled LangSmith trace so this path keeps the same observability coverage a traced
   `Runnable` call gets for free. **This is a real improvement, not a complete fix**: further
   testing also found `host.docker.internal` going through bursty stretches — lasting several
   consecutive requests — where even non-streaming calls stall, independent of request shape.
   That residual, unresolved intermittency is Docker Desktop's own NAT behavior, not something
   this provider's request shape controls; `docs/ASSUMPTIONS_AND_TRADEOFFS.md` trade-off 24 has
   the full, honest reliability picture rather than an overclaim. `astream()` is untouched:
   token-by-token streaming to the UI is supposed to stream, and native (non-containerized)
   deployments never cross the NAT boundary that causes any of this in the first place, so
   nothing here changes their behavior.

Uses `ChatOllama` (from `langchain-ollama`, already a pinned dependency) rather than the raw
`ollama` client for construction and parameter-building even in the bypassed path — one place
builds every request's GPU/format/timeout shape, so the two call paths (`astream`/`astructured`)
can never quietly drift apart on those settings.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Sequence

import structlog
from langchain_core.callbacks.manager import AsyncCallbackManager
from langchain_core.messages import AIMessage, BaseMessage, BaseMessageChunk
from langchain_core.outputs import ChatGeneration, LLMResult
from langchain_core.runnables.config import ensure_config
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
#
# The same budget also covers a timeout, for a distinct reason found live while containerizing
# this project (docs/ASSUMPTIONS_AND_TRADEOFFS.md trade-off 24): a container reaching this
# project's intentionally-native Ollama over Docker Desktop's `host.docker.internal` NAT can hit
# stretches where requests stall well past this project's timeout, streamed or not — root-caused
# to `ChatOllama` always using Ollama's *streamed* chat API internally (module docstring, finding
# 3), which measurably raises how often this happens, but a non-streaming call is not immune to
# every bad stretch either. This retry is a second line of defense on top of the non-streaming
# fix, not the primary mitigation — see `docs/ASSUMPTIONS_AND_TRADEOFFS.md` trade-off 24 for the
# honest reliability picture, including the residual, unresolved intermittency this doesn't fully
# eliminate. This helps the native (non-containerized) deployment not at all, since it never
# crosses that NAT boundary, and costs it nothing either.
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
        temperature: float | None = None,
    ) -> SchemaT:
        message_list = list(messages)
        last_error: Exception | None = None
        for attempt in range(1, _STRUCTURED_OUTPUT_ATTEMPTS + 1):
            try:
                raw_content = await asyncio.wait_for(
                    self._call_non_streaming(
                        message_list, schema=schema, reasoning=reasoning, temperature=temperature
                    ),
                    timeout=self._request_timeout,
                )
            except TimeoutError as exc:
                last_error = exc
                if attempt == _STRUCTURED_OUTPUT_ATTEMPTS:
                    raise LLMTimeoutError(
                        f"Ollama did not respond within {self._request_timeout}s "
                        f"(after {attempt} attempts)."
                    ) from exc
                logger.warning(
                    "structured_output_attempt_timed_out",
                    attempt=attempt,
                    schema=schema.__name__,
                    timeout_seconds=self._request_timeout,
                )
                continue
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

            # Fail safely on malformed output: a schema-constrained grammar should always return
            # conformant JSON, but re-parsing and re-validating here rather than trusting the raw
            # string is cheap insurance against a stray token or a version drift breaking that
            # contract silently — the same posture `with_structured_output` gave us for free
            # before, now made explicit since this path builds the object itself.
            try:
                return schema.model_validate(json.loads(raw_content))
            except (json.JSONDecodeError, ValidationError) as exc:
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

    async def _call_non_streaming(
        self,
        messages: list[BaseMessage],
        *,
        schema: type[SchemaT],
        reasoning: bool,
        temperature: float | None = None,
    ) -> str:
        """One non-streaming `/api/chat` call, returning the raw (unparsed, unvalidated) content
        string — `astructured()` owns parsing, validation, retry, and the timeout wrapper so this
        method has exactly one job. Traced manually as one LLM run so it shows up in LangSmith
        exactly like the `ChatOllama.ainvoke()` call it replaces: LangGraph threads the active
        run's callbacks through an ambient `RunnableConfig` that `ensure_config()` reads even
        though this node function never receives `config` explicitly (`docs/DECISIONS.md` §11) —
        the same mechanism a real `Runnable`'s `.ainvoke()` relies on internally.
        """
        # `_chat_params` is `ChatOllama`'s own request-shape builder (message conversion, model
        # name, `num_gpu`, `keep_alive`, ...) — reused rather than reimplemented so this path can
        # never quietly drift from `astream()`'s GPU/timeout/model settings. Passing `options`
        # explicitly (only when a caller asked for a non-default `temperature`) replaces
        # `_chat_params`'s own instance-attribute-derived dict wholesale rather than merging into
        # it, so `num_gpu` is repeated here to avoid silently losing trade-off 13's forced full
        # GPU residency the moment a caller wants a non-default temperature.
        options_override = (
            {"num_gpu": _FORCE_FULL_GPU_OFFLOAD, "temperature": temperature}
            if temperature is not None
            else None
        )
        chat_params = self._model._chat_params(
            messages,
            stop=None,
            stream=False,
            reasoning=reasoning,
            format=schema.model_json_schema(),
            **({"options": options_override} if options_override is not None else {}),
        )

        config = ensure_config()
        callback_manager = AsyncCallbackManager.configure(
            inheritable_callbacks=config.get("callbacks"),
            inheritable_tags=config.get("tags"),
            inheritable_metadata=config.get("metadata"),
        )
        [run_manager] = await callback_manager.on_chat_model_start(
            {"name": "OllamaProvider._call_non_streaming"},
            [messages],
            invocation_params={
                "model": chat_params["model"],
                "think": chat_params["think"],
                "stream": False,
            },
        )
        try:
            response = await self._model._async_client.chat(**chat_params)
        except Exception as exc:
            await run_manager.on_llm_error(exc)
            raise
        content: str = response["message"]["content"]
        await run_manager.on_llm_end(
            LLMResult(generations=[[ChatGeneration(message=AIMessage(content=content))]])
        )
        return content

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
