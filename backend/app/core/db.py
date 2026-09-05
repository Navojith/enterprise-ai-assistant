"""Shared async SQLAlchemy engine, session factory, and declarative base.

One engine for every table the application owns directly — the ingestion idempotency
manifest (`retrieval/ingest.py`), the rerank budget counter (`retrieval/reranker.py`), and
Cycle 2's rate-limit state and user store. This is deliberately separate from
`langgraph-checkpoint-postgres`'s own connection handling in Cycle 3: the checkpointer manages
its own schema and pool against the same database, and there is no need for the two to share
a session — they own disjoint tables.

Uses the `postgresql+psycopg` (v3, async) dialect — see `backend/app/core/loop.py` for why
that specific driver, and the Windows event-loop fix it requires.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import lru_cache

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from backend.app.core.config import Settings, get_settings


class Base(DeclarativeBase):
    """Declarative base for every ORM model the application defines directly."""


@lru_cache(maxsize=1)
def get_engine() -> AsyncEngine:
    """Process-wide async engine, built once. `lru_cache` gives it the same "construct once,
    reuse everywhere" shape as `get_settings` — see that function's docstring for why a cached
    factory is preferred here over a bare module-level global."""
    settings: Settings = get_settings()
    return create_async_engine(settings.database_url, pool_pre_ping=True)


_session_factory: async_sessionmaker[AsyncSession] | None = None


def _get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _session_factory


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """One transaction per `async with` block: commits on success, rolls back on any
    exception, always closes. This is the only way application code should touch a session —
    it makes "did this commit or roll back" a property of the code's control flow rather than
    something every call site has to remember to do correctly."""
    session_factory = _get_session_factory()
    async with session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


def _import_all_orm_models() -> None:
    """`Base.metadata` only knows about a `Base` subclass once its module has actually been
    imported — a table whose module nothing else happened to import yet is invisible to
    `create_all`, and that failure mode is a `psycopg.errors.UndefinedTable` at the first query
    against it, not at startup where it would be obvious. Importing every module that defines
    one here, in the one function responsible for creating tables, means a caller of
    `create_all_tables()` never needs to know which modules define what — this list is the
    single place that has to stay in sync as new tables are added.
    """
    from backend.app.core.security import rate_limit as _rate_limit  # noqa: F401
    from backend.app.retrieval import ingest as _ingest  # noqa: F401
    from backend.app.retrieval import reranker as _reranker  # noqa: F401


async def create_all_tables() -> None:
    """Create every table registered on `Base.metadata` that does not already exist.

    A plain `create_all` rather than a migration tool (Alembic) — there is exactly one schema
    version in this project's lifetime, so a migration framework would add ceremony without
    solving a problem that exists yet. `create_all` is itself idempotent (`IF NOT EXISTS`
    semantics), so this is safe to call on every ingestion run and app startup.
    """
    _import_all_orm_models()
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
