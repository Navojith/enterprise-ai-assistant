"""CLI entrypoint: ingest `data/seed/` into Pinecone. Run with `python -m scripts.ingest`.

Requires `PINECONE_API_KEY` in `.env` and Postgres running (`docker compose up -d postgres`) —
the ingestion manifest that makes re-runs idempotent lives there. See docs/SETUP.md.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from backend.app.core.config import get_settings
from backend.app.core.logging import configure_logging
from backend.app.core.loop import install_selector_event_loop_policy
from backend.app.retrieval.ingest import ingest_corpus
from backend.app.retrieval.pinecone_store import IndexKind, PineconeStore

_INDEX_KINDS: tuple[IndexKind, ...] = ("dense", "sparse")

_CORPUS_ROOT = Path(__file__).resolve().parent.parent / "data" / "seed"


async def main() -> int:
    settings = get_settings()
    configure_logging(settings)

    if settings.pinecone_api_key is None:
        print(
            "PINECONE_API_KEY is not set in .env — see docs/SETUP.md to create a free "
            "Pinecone Starter account before running ingestion.",
            file=sys.stderr,
        )
        return 1

    if not _CORPUS_ROOT.exists() or not any(_CORPUS_ROOT.rglob("*.md")):
        print(
            f"No documents found under {_CORPUS_ROOT}. Run "
            "`python -m scripts.generate_seed_corpus` first.",
            file=sys.stderr,
        )
        return 1

    store = PineconeStore(settings)
    try:
        report = await ingest_corpus(store, _CORPUS_ROOT)

        print(f"Parsed {report.total_chunks} chunks from {_CORPUS_ROOT}")
        print(f"  Unchanged (skipped): {report.skipped_unchanged}")
        print(f"  Upserted:            {report.upserted}")
        print(f"    dense records:     {report.dense_upserted}")
        print(f"    sparse records:    {report.sparse_upserted}")

        for kind in _INDEX_KINDS:
            namespace_counts = await store.describe_stats(index_kind=kind)
            total = sum(namespace_counts.values())
            # A plain hyphen, not an em dash: this prints to a plain console (cmd.exe's default
            # codepage mangles non-ASCII), unlike the docstrings/log messages elsewhere.
            print(f"\n{kind.capitalize()} index - {total} vectors total")
            for namespace, count in sorted(namespace_counts.items()):
                print(f"    {namespace}: {count}")
    finally:
        await store.close()

    return 0


if __name__ == "__main__":
    install_selector_event_loop_policy()
    raise SystemExit(asyncio.run(main()))
