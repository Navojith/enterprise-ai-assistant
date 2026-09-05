"""Async Pinecone client: idempotent dense + sparse index creation, upsert, and search.

Both indexes use **integrated inference** (`docs/DECISIONS.md` §2): Pinecone embeds text
server-side on upsert and on query, so this module never computes an embedding itself, never
holds an embedding model in memory, and consumes none of the 4 GB of VRAM reserved for the
LLM. Every namespace is a department (`retrieval/models.py`'s `DEPARTMENTS`), matching the
metadata schema ASSESSMENT.md specifies.

This module owns index lifecycle and raw search; `retrieval/hybrid.py` is what fans out to
both indexes concurrently and fuses the results, and `retrieval/reranker.py` is what
optionally reranks the fused list — kept separate because index management, search, fusion,
and reranking are each independently testable without the other three.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

import structlog
from pinecone import AsyncIndex, AsyncPinecone

from backend.app.core.config import Settings, get_settings
from backend.app.core.errors import RetrievalError, VectorStoreUnavailableError
from backend.app.retrieval.models import Chunk, RetrievedChunk

logger = structlog.get_logger(__name__)

IndexKind = Literal["dense", "sparse"]

_DENSE_EMBED_MODEL = "llama-text-embed-v2"
_SPARSE_EMBED_MODEL = "pinecone-sparse-english-v0"
_TEXT_FIELD = "chunk_text"  # the field integrated inference embeds, on both indexes
_PINECONE_REGION = "us-east-1"  # Starter tier is AWS us-east-1 only — docs/DECISIONS.md §4
_UPSERT_BATCH_SIZE = 96  # comfortably under Pinecone's per-request record limit


class PineconeStore:
    """Owns one `AsyncPinecone` client and the two index connections derived from it.

    Index *host* URLs (not just names) are required to open an `AsyncIndexAsyncio` handle, and
    a host is only known after the index exists — so `ensure_indexes()` must run before
    `dense_index`/`sparse_index` are accessed. That ordering is enforced by raising rather than
    lazily creating on first access, so a caller that forgets the setup step gets a clear error
    instead of a surprising first-request delay.
    """

    def __init__(self, settings: Settings) -> None:
        if settings.pinecone_api_key is None:
            raise RetrievalError(
                "PINECONE_API_KEY is not set — see docs/SETUP.md to create a Starter account."
            )
        self._settings = settings
        self._client = AsyncPinecone(api_key=settings.pinecone_api_key.get_secret_value())
        self._dense_index: AsyncIndex | None = None
        self._sparse_index: AsyncIndex | None = None

    @property
    def dense_index(self) -> AsyncIndex:
        if self._dense_index is None:
            raise RetrievalError("Pinecone indexes not initialized — call ensure_indexes() first.")
        return self._dense_index

    @property
    def sparse_index(self) -> AsyncIndex:
        if self._sparse_index is None:
            raise RetrievalError("Pinecone indexes not initialized — call ensure_indexes() first.")
        return self._sparse_index

    async def close(self) -> None:
        await self._client.close()

    async def ensure_indexes(self) -> None:
        """Create the dense and sparse indexes if they don't already exist, then open a
        connected handle to each. Safe to call on every startup/ingestion run — `has_index`
        makes creation idempotent, matching the idempotent-ingestion requirement one level up
        the stack (docs/DELIVERY_PLAN.md Cycle 1)."""
        dense_host = await self._ensure_index(
            self._settings.pinecone_dense_index, _DENSE_EMBED_MODEL
        )
        sparse_host = await self._ensure_index(
            self._settings.pinecone_sparse_index, _SPARSE_EMBED_MODEL
        )
        self._dense_index = self._client.IndexAsyncio(host=dense_host)
        self._sparse_index = self._client.IndexAsyncio(host=sparse_host)

    async def _ensure_index(self, name: str, embed_model: str) -> str:
        try:
            if not await self._client.has_index(name):
                logger.info("pinecone_index_creating", index=name, model=embed_model)
                await self._client.create_index_for_model(
                    name=name,
                    cloud="aws",
                    region=_PINECONE_REGION,
                    embed={"model": embed_model, "field_map": {"text": _TEXT_FIELD}},
                )
            index_model = await self._client.describe_index(name)
        except Exception as exc:
            raise VectorStoreUnavailableError(
                f"Could not create/describe index {name!r}: {exc}"
            ) from exc
        if not index_model.host:
            raise VectorStoreUnavailableError(f"Pinecone returned no host for index {name!r}")
        return index_model.host

    async def upsert_chunks(self, chunks: list[Chunk], *, index_kind: IndexKind) -> int:
        """Upsert `chunks` into the given index, one namespace per department, batched to stay
        under Pinecone's per-request record limit. Returns the number of records upserted."""
        index = self.dense_index if index_kind == "dense" else self.sparse_index
        by_namespace: dict[str, list[dict[str, str | int]]] = {}
        for chunk in chunks:
            by_namespace.setdefault(chunk.metadata.department, []).append(
                chunk.to_pinecone_record()
            )

        total = 0
        try:
            for namespace, records in by_namespace.items():
                for start in range(0, len(records), _UPSERT_BATCH_SIZE):
                    batch = records[start : start + _UPSERT_BATCH_SIZE]
                    await index.upsert_records(records=batch, namespace=namespace)
                    total += len(batch)
        except Exception as exc:
            raise VectorStoreUnavailableError(
                f"Upsert to {index_kind} index failed: {exc}"
            ) from exc
        return total

    async def search(
        self,
        *,
        index_kind: IndexKind,
        namespace: str,
        query_text: str,
        top_k: int,
        access_levels: list[str],
    ) -> list[RetrievedChunk]:
        """Search one index/namespace, filtered to the given `access_level`s. Integrated
        inference means `query_text` is sent as-is — Pinecone embeds it server-side with the
        same model that embedded the stored records."""
        index = self.dense_index if index_kind == "dense" else self.sparse_index
        try:
            response = await index.search(
                namespace=namespace,
                inputs={"text": query_text},
                top_k=top_k,
                filter={"access_level": {"$in": access_levels}},
            )
        except Exception as exc:
            raise VectorStoreUnavailableError(
                f"Search on {index_kind} index failed: {exc}"
            ) from exc

        return [
            RetrievedChunk(
                chunk_id=hit.id,
                document_id=hit.fields["document_id"],
                section=hit.fields["section"],
                text=hit.fields[_TEXT_FIELD],
                title=hit.fields["title"],
                department=hit.fields["department"],
                document_type=hit.fields["document_type"],
                access_level=hit.fields["access_level"],
                created_date=hit.fields["created_date"],
                score=hit.score,
            )
            for hit in response.result.hits
        ]

    async def rerank(
        self, *, model: str, query: str, documents: list[str], top_n: int | None = None
    ) -> list[tuple[int, float]]:
        """Rerank `documents` (plain text) against `query`, returning `(original_index, score)`
        pairs in descending relevance order. The allowlist and budget guard live in
        `retrieval/reranker.py`, one layer up — this method only wraps the raw API call so
        every Pinecone request in the codebase goes through this one client."""
        try:
            result = await self._client.inference.rerank(
                model=model, query=query, documents=documents, top_n=top_n, return_documents=False
            )
        except Exception as exc:
            raise RetrievalError(f"Rerank call failed: {exc}") from exc
        return [(ranked.index, ranked.score) for ranked in result.data]

    async def describe_stats(self, *, index_kind: IndexKind) -> dict[str, int]:
        """Per-namespace vector counts for one index — what
        `docs/DELIVERY_PLAN.md`'s acceptance criteria call "assert dense and sparse vector
        counts and namespace distribution", surfaced for `scripts/ingest.py` to print rather
        than left as something only checkable by hand in the Pinecone console."""
        index = self.dense_index if index_kind == "dense" else self.sparse_index
        try:
            stats = await index.describe_index_stats()
        except Exception as exc:
            raise VectorStoreUnavailableError(
                f"describe_index_stats failed for {index_kind}: {exc}"
            ) from exc
        return {namespace: summary.vector_count for namespace, summary in stats.namespaces.items()}


@lru_cache(maxsize=1)
def get_pinecone_store() -> PineconeStore:
    """Cached factory for FastAPI dependency injection — mirrors `get_settings`/`get_engine`."""
    return PineconeStore(get_settings())
