"""Idempotent ingestion: parse the corpus, skip anything unchanged, upsert the rest.

"Idempotent" means keyed on `Chunk.content_hash` (docs/DECISIONS.md §4 cost guard): a
`ingested_chunks` table in Postgres remembers the content hash last upserted for every
`chunk_id`, so re-running ingestion after a no-op corpus change re-embeds nothing, and after a
small edit re-embeds only the chunks that actually changed — protecting Pinecone's 5M-token/
month integrated-inference allowance from being burned by repeated full-corpus re-ingestion
during development.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import structlog
from sqlalchemy import DateTime, String, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.db import Base, create_all_tables, session_scope
from backend.app.retrieval.chunking import parse_corpus
from backend.app.retrieval.models import Chunk
from backend.app.retrieval.pinecone_store import PineconeStore

logger = structlog.get_logger(__name__)


class IngestedChunk(Base):
    """The idempotency manifest: the content hash last successfully upserted for each chunk.

    `chunk_id` is the primary key (stable identity, per `retrieval/models.py`), not
    `content_hash` — the manifest's question is "does the current content match what's
    already indexed for this identity", not "have we ever seen this exact text before".
    """

    __tablename__ = "ingested_chunks"

    chunk_id: Mapped[str] = mapped_column(String, primary_key=True)
    content_hash: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


@dataclass(frozen=True)
class IngestReport:
    total_chunks: int
    upserted: int
    skipped_unchanged: int
    dense_upserted: int
    sparse_upserted: int


async def _load_existing_hashes() -> dict[str, str]:
    async with session_scope() as session:
        result = await session.execute(select(IngestedChunk.chunk_id, IngestedChunk.content_hash))
        return dict(result.tuples().all())


async def _record_ingested(chunks: list[Chunk]) -> None:
    now = datetime.now(UTC)
    async with session_scope() as session:
        for chunk in chunks:
            statement = pg_insert(IngestedChunk).values(
                chunk_id=chunk.chunk_id, content_hash=chunk.content_hash, updated_at=now
            )
            statement = statement.on_conflict_do_update(
                index_elements=[IngestedChunk.chunk_id],
                set_={
                    "content_hash": statement.excluded.content_hash,
                    "updated_at": statement.excluded.updated_at,
                },
            )
            await session.execute(statement)


async def ingest_corpus(store: PineconeStore, corpus_root: Path) -> IngestReport:
    """Parse every document under `corpus_root`, skip chunks whose content hash already
    matches the manifest, and upsert the rest into both the dense and sparse indexes."""
    await create_all_tables()
    await store.ensure_indexes()

    chunks = parse_corpus(corpus_root)
    existing_hashes = await _load_existing_hashes()
    changed_chunks = [c for c in chunks if existing_hashes.get(c.chunk_id) != c.content_hash]
    skipped = len(chunks) - len(changed_chunks)

    logger.info(
        "ingest_diff_computed",
        total_chunks=len(chunks),
        changed=len(changed_chunks),
        skipped_unchanged=skipped,
    )

    dense_upserted = sparse_upserted = 0
    if changed_chunks:
        dense_upserted = await store.upsert_chunks(changed_chunks, index_kind="dense")
        sparse_upserted = await store.upsert_chunks(changed_chunks, index_kind="sparse")
        await _record_ingested(changed_chunks)

    return IngestReport(
        total_chunks=len(chunks),
        upserted=len(changed_chunks),
        skipped_unchanged=skipped,
        dense_upserted=dense_upserted,
        sparse_upserted=sparse_upserted,
    )
