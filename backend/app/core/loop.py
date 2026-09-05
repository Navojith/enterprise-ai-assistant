"""Two ways to force `SelectorEventLoop` on Windows, for the two ways this codebase starts an
event loop: uvicorn (its own loop-construction path) and plain scripts (`asyncio.run`).

psycopg's async mode — used by the health readiness check, `core/db.py`'s engine, and
`langgraph-checkpoint-postgres`'s `AsyncPostgresSaver` from Cycle 3 onward — needs
`loop.add_reader`/`add_writer`, which Windows' `ProactorEventLoop` does not implement.

**Under uvicorn**, point it at this module's factory instead of guessing:

    uvicorn backend.app.main:app --loop backend.app.core.loop:selector_loop_factory

Uvicorn's own loop selection (`uvicorn.loops.asyncio.asyncio_loop_factory`) returns
`ProactorEventLoop` on Windows unless `use_subprocess` is set, which uvicorn only does when
`--reload` or `--workers > 1` is passed — so the bug would otherwise appear and disappear
depending on which flags happened to be on the command line. Uvicorn resolves a *named* loop
(`asyncio`, `uvloop`, `auto`) through an extra indirection — it imports a "factory of
factories" and calls it with `use_subprocess` to get the actual loop constructor. A *custom*
`--loop module:attr` string skips that indirection entirely: uvicorn imports the attribute and
calls it directly with zero arguments, so `selector_loop_factory` returns a loop *instance*,
not a loop class, and takes no `use_subprocess` argument — there is nothing to pass it.

**In a plain script** (`scripts/ingest.py` and anything else calling `asyncio.run()` directly,
outside of uvicorn), there is no `--loop` flag to reach for — call
`install_selector_event_loop_policy()` once, before `asyncio.run(...)`. Unlike uvicorn's custom
loop factory, `asyncio.run()` builds its loop through the *event-loop policy*
(`asyncio.get_event_loop_policy().new_event_loop()`), so setting the policy is what works here;
it would not have worked for uvicorn, which bypasses the policy entirely.

`SelectorEventLoop` cannot spawn subprocesses on Windows, which is a real limitation of the
selector loop in general — nothing in this application spawns one, so it does not apply here.
"""

from __future__ import annotations

import asyncio
import sys


def selector_loop_factory() -> asyncio.AbstractEventLoop:
    """For uvicorn's `--loop module:attr` custom-callable form. See module docstring."""
    if sys.platform == "win32":
        return asyncio.SelectorEventLoop()
    # Non-Windows platforms never hit this bug; the stdlib default is fine here. (uvloop, when
    # installed, is picked up automatically by uvicorn's own "auto" loop instead — this custom
    # factory is only ever selected explicitly, for the Windows fix.)
    return asyncio.new_event_loop()


def install_selector_event_loop_policy() -> None:
    """For plain scripts calling `asyncio.run()` directly. See module docstring. A no-op on
    non-Windows platforms, so scripts can call this unconditionally."""
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
