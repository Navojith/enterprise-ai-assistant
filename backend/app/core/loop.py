"""Custom uvicorn event-loop factory that forces `SelectorEventLoop` on Windows.

psycopg's async mode — used by the health readiness check below, and by
`langgraph-checkpoint-postgres`'s `AsyncPostgresSaver` from Cycle 3 onward — needs
`loop.add_reader`/`add_writer`, which Windows' `ProactorEventLoop` does not implement.
Uvicorn's own loop selection (`uvicorn.loops.asyncio.asyncio_loop_factory`) returns
`ProactorEventLoop` on Windows unless `use_subprocess` is set, which uvicorn only does when
`--reload` or `--workers > 1` is passed. That made the bug appear and disappear depending on
which flags happened to be on the command line — worth fixing explicitly rather than relying
on that coincidence holding forever (a demo run without `--reload` would silently break every
Postgres-backed feature).

Point uvicorn at this module instead of guessing:

    uvicorn backend.app.main:app --loop backend.app.core.loop:selector_loop_factory

Uvicorn resolves a *named* loop (`asyncio`, `uvloop`, `auto`) through an extra indirection —
it imports a "factory of factories" and calls it with `use_subprocess` to get the actual loop
constructor. A *custom* `--loop module:attr` string skips that indirection entirely: uvicorn
imports the attribute and calls it directly with zero arguments, so this function must return
a loop *instance*, not a loop class, and must not take `use_subprocess` — there is nothing to
pass it.

`SelectorEventLoop` cannot spawn subprocesses on Windows, which is a real limitation of the
selector loop in general — nothing in this application spawns one, so it does not apply here.
"""

from __future__ import annotations

import asyncio
import sys


def selector_loop_factory() -> asyncio.AbstractEventLoop:
    if sys.platform == "win32":
        return asyncio.SelectorEventLoop()
    # Non-Windows platforms never hit this bug; the stdlib default is fine here. (uvloop, when
    # installed, is picked up automatically by uvicorn's own "auto" loop instead — this custom
    # factory is only ever selected explicitly, for the Windows fix.)
    return asyncio.new_event_loop()
