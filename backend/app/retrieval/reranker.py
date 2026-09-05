"""Budget-gated, allowlisted reranking on top of the RRF-fused candidate set.

Two independent guards protect the zero-cost constraint (docs/DECISIONS.md §4):

1. **Model allowlist.** Pinecone's reranking API exposes `cohere-rerank-3.5` through the same
   call as the free `bge-reranker-v2-m3` — but the Cohere model has zero free requests on
   Starter and bills on the first call. The model name is a private module constant, never a
   config value or function parameter, so it cannot be widened by a settings change; the
   `_assert_model_allowed` check further ahead is what makes that a tested guarantee rather
   than an assertion nobody re-verifies after the next refactor.
2. **Persisted monthly budget.** Starter includes only 500 rerank requests/month — the
   tightest limit in the whole stack. A row in Postgres (`rerank_usage`, one per calendar
   month) is incremented atomically before every call and compared against
   `settings.rerank_monthly_budget` (itself validated to sit below 500 — see
   `core/config.py`); once exceeded, reranking silently degrades to the RRF-fused order rather
   than erroring or risking a billed request. The counter is incremented *before* the budget
   check, not after a successful call, which means an outright race between two concurrent
   requests both reading "499 used" cannot both slip through — the trade-off is that a
   request refused for being over budget still consumes one increment, undercounting headroom
   very slightly. Erring toward stopping early is the correct direction for a cost guard.
"""

from __future__ import annotations

from datetime import UTC, datetime

import structlog
from sqlalchemy import Integer, String
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.config import Settings
from backend.app.core.db import Base, session_scope
from backend.app.core.errors import ConfigurationError
from backend.app.retrieval.models import RetrievedChunk
from backend.app.retrieval.pinecone_store import PineconeStore

logger = structlog.get_logger(__name__)

_ALLOWED_RERANK_MODEL = "bge-reranker-v2-m3"
_BLOCKED_RERANK_MODELS = frozenset({"cohere-rerank-3.5", "pinecone-rerank-v0"})


class RerankUsage(Base):
    """One row per calendar month (`"YYYY-MM"`), tracking attempted rerank calls against the
    Pinecone Starter free-tier budget."""

    __tablename__ = "rerank_usage"

    month: Mapped[str] = mapped_column(String(7), primary_key=True)
    count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


def _assert_model_allowed(model: str) -> None:
    if model in _BLOCKED_RERANK_MODELS:
        raise ConfigurationError(
            f"Reranker model {model!r} is on the billing blocklist (bills on first call under "
            "Pinecone Starter) — see docs/DECISIONS.md §4. This should be unreachable; the "
            "model is a private constant, not user-configurable."
        )
    if model != _ALLOWED_RERANK_MODEL:
        raise ConfigurationError(
            f"Reranker model {model!r} is not the allowlisted model ({_ALLOWED_RERANK_MODEL!r})."
        )


async def _increment_monthly_usage(month: str) -> int:
    """Atomically increment this month's counter and return the new total, in one statement —
    `INSERT ... ON CONFLICT DO UPDATE` is a single atomic operation in Postgres, so this is
    race-free under concurrent requests without needing an explicit row lock."""
    statement = (
        pg_insert(RerankUsage)
        .values(month=month, count=1)
        .on_conflict_do_update(
            index_elements=[RerankUsage.month], set_={"count": RerankUsage.count + 1}
        )
        .returning(RerankUsage.count)
    )
    async with session_scope() as session:
        result = await session.execute(statement)
        return result.scalar_one()


async def rerank_chunks(
    store: PineconeStore,
    *,
    query: str,
    chunks: list[RetrievedChunk],
    settings: Settings,
    top_n: int | None = None,
) -> list[RetrievedChunk]:
    """Rerank `chunks` (already RRF-fused) against `query`, or return them unchanged if
    reranking is disabled, the budget is exhausted, or the call itself fails — reranking is a
    quality improvement, never a correctness dependency, so every non-happy path degrades to
    the input order instead of raising.
    """
    if not chunks:
        return chunks
    if not settings.rerank_enabled:
        logger.info("rerank_skipped", reason="disabled_in_settings")
        return chunks

    _assert_model_allowed(_ALLOWED_RERANK_MODEL)

    month_key = datetime.now(UTC).strftime("%Y-%m")
    used = await _increment_monthly_usage(month_key)
    if used > settings.rerank_monthly_budget:
        logger.warning(
            "rerank_budget_exhausted",
            month=month_key,
            used=used,
            budget=settings.rerank_monthly_budget,
        )
        return chunks

    try:
        ranked = await store.rerank(
            model=_ALLOWED_RERANK_MODEL,
            query=query,
            documents=[chunk.text for chunk in chunks],
            top_n=top_n,
        )
    except Exception as exc:  # noqa: BLE001 - reranking degrades rather than propagating
        logger.warning("rerank_call_failed", error=str(exc))
        return chunks

    return [chunks[index].model_copy(update={"score": score}) for index, score in ranked]
