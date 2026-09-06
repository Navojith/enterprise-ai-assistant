"""AST-validated sandbox for executing untrusted, LLM- or user-supplied Python.

This is the one place in the codebase that runs code it did not write, so it backs both the
`python_analysis` tool (Cycle 4) and the RLM planner's generated search plans (Cycle 5,
`docs/ARCHITECTURE.md`: "The same sandbox backs the Python Analysis tool, so the one
security-critical component is written and audited once"). Three independent layers, all
required, none sufficient alone:

1. **AST allowlist** (`validate_ast`) — the code is parsed, never executed, and walked before
   any decision to run it is made. Imports, dunder attribute access (`.__class__`,
   `.__globals__`, ...) *and bare dunder names* (`__builtins__`, `__name__`, ... — live
   security testing found `result = __builtins__` leaking the sandbox's own restricted builtins
   dict past a version of this check that only looked at `.dunder` attribute access, never a
   bare dunder identifier), and the handful of builtins that reach the filesystem, the
   interpreter, or process state (`open`, `exec`, `eval`, `compile`, `__import__`, `input`) are
   rejected outright. This catches the large majority of "escape the sandbox" attempts, and does
   so *before* a single line runs — a rejected plan costs nothing.
2. **Stripped builtins** (`_SAFE_BUILTINS`) — even code that passes the AST check runs against
   a `__builtins__` mapping containing only pure, side-effect-free functions. This is
   defense-in-depth against an AST-allowlist gap, not the primary control.
3. **Wall-clock timeout** — `run_sandboxed` is a coroutine that hands the actual (synchronous,
   blocking) `exec()` call to a worker thread via `asyncio.wait_for`, so a runaway loop cannot
   stall the event loop and cannot run past its budget from the caller's point of view.

**What this does not do, and why that is an accepted trade-off, not an oversight:** this is
in-process isolation, not OS-level sandboxing (a subprocess, a container, a gVisor-style
runtime). The obvious next step up — running the code in a subprocess — is not available here:
this project's Windows event loop is pinned to `SelectorEventLoop`
(`docs/ASSUMPTIONS_AND_TRADEOFFS.md` trade-off 8, chosen because psycopg's async mode cannot
run on Windows' default `ProactorEventLoop`), and `SelectorEventLoop` cannot spawn subprocesses
on Windows at all. A wall-clock timeout also cannot forcibly kill a CPU-bound `exec()` already
running in a thread — `asyncio.wait_for` stops *waiting* for it, but the thread is not
interrupted and is abandoned to finish (or spin) on its own; this is acceptable for a bounded
assessment sandbox handling small analysis snippets, not for arbitrary untrusted code at
production scale. A production deployment would move this to a subprocess or container with a
real OS-enforced CPU/memory/time limit instead.

**Found by live security testing, fixed before it could compound:** an abandoned CPU-bound
thread does not just sit harmlessly — it permanently occupies one worker slot for the rest of
the process's life. `run_sandboxed` therefore hands `exec()` to a small, **dedicated**
`ThreadPoolExecutor` (`_SANDBOX_EXECUTOR`) rather than `loop.run_in_executor(None, ...)`'s
process-wide default pool. Without this, repeated abusive calls (or a buggy generated RLM plan
that happens to loop forever) would each permanently consume one slot of the *same* shared pool
every other `run_in_executor(None, ...)`/`asyncio.to_thread` call in the process draws from —
`min(32, os.cpu_count() + 4)` calls in and the entire application's blocking work would start
queuing indefinitely, not just `python_analysis`/research. A dedicated pool contains that
blast radius to sandbox calls alone: exhausting it degrades only analytics and research, never
the rest of the assistant, and is still a config-sized, documented limit rather than an
unbounded one growing silently.
"""

from __future__ import annotations

import ast
import asyncio
import builtins as _builtins_module
import io
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from dataclasses import dataclass, field
from functools import partial
from typing import Any

from backend.app.core.errors import SandboxViolationError

# Dedicated, bounded pool for sandboxed `exec()` calls only — see the module docstring's "Found
# by live security testing" note. Sized generously above this project's realistic concurrent
# sandbox usage (RLM sub-agent fan-out defaults to sequential, `docs/DECISIONS.md` §9) rather
# than tightly, since the goal is containing an abusive/runaway call's blast radius to sandbox
# work, not rationing ordinary concurrent use.
_SANDBOX_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="rlm-sandbox")

# Builtins reaching the filesystem, the interpreter, process state, or import machinery.
# Blocked by name regardless of how they are referenced (`open(...)`, `__builtins__.open(...)`).
_FORBIDDEN_NAMES: frozenset[str] = frozenset(
    {
        "open",
        "exec",
        "eval",
        "compile",
        "__import__",
        "input",
        "vars",
        "globals",
        "locals",
        "getattr",
        "setattr",
        "delattr",
        "breakpoint",
        "help",
        "exit",
        "quit",
    }
)

# The pure, side-effect-free subset of builtins the sandbox actually exposes at runtime — layer
# 2 of the module docstring's three. Deliberately a short allowlist rather than "everything
# except `_FORBIDDEN_NAMES`", so a new dangerous builtin added to a future Python version is
# excluded by default instead of silently let through.
_SAFE_BUILTINS: dict[str, Any] = {
    name: getattr(_builtins_module, name)
    for name in (
        "abs",
        "all",
        "any",
        "bool",
        "dict",
        "enumerate",
        "filter",
        "float",
        "frozenset",
        "int",
        "len",
        "list",
        "map",
        "max",
        "min",
        "range",
        "reversed",
        "round",
        "set",
        "sorted",
        "str",
        "sum",
        "tuple",
        "zip",
        "print",
        "Exception",
        "ValueError",
        "TypeError",
        "KeyError",
        "IndexError",
        "StopIteration",
        "True",
        "False",
        "None",
    )
}


class _ForbiddenNodeVisitor(ast.NodeVisitor):
    """Walks a parsed module and collects every AST-allowlist violation it finds, rather than
    raising on the first one — a single readable error listing everything wrong is more useful
    to whatever generated the code (a human, or Cycle 5's planner deciding whether to retry)
    than a sequence of one-at-a-time failures."""

    def __init__(self) -> None:
        self.violations: list[str] = []

    def visit_Import(self, node: ast.Import) -> None:
        self.violations.append("import statements are not allowed")

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        self.violations.append("import statements are not allowed")

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr.startswith("__") and node.attr.endswith("__"):
            self.violations.append(f"dunder attribute access is not allowed: .{node.attr}")
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if node.id in _FORBIDDEN_NAMES:
            self.violations.append(f"use of {node.id!r} is not allowed")
        # A *bare* dunder identifier (`__builtins__`, `__name__`, `__loader__`, ...) is a
        # separate escape route from `.dunder` attribute access, and was not covered by
        # `visit_Attribute` above until live security testing found `result = __builtins__`
        # leaking the sandbox's own restricted builtins dict straight past the AST check — a
        # `Name` node, never an `Attribute` node, so the existing dunder check never saw it.
        # `exec()` always populates `__builtins__` (and other dunders) into the globals dict it
        # is given, regardless of what `injected_globals` supplies, so this cannot be closed by
        # controlling what the sandbox hands in — it has to be rejected in the code itself.
        if node.id.startswith("__") and node.id.endswith("__"):
            self.violations.append(f"use of dunder name {node.id!r} is not allowed")
        self.generic_visit(node)

    def visit_With(self, node: ast.With) -> None:
        # `with` is how sandboxed code would reach file I/O even with `open` itself blocked
        # (e.g. a context manager built some other way) — blocked wholesale since the sandbox's
        # own `search`/`filter`/`batch` API (Cycle 5) never needs one.
        self.violations.append("'with' statements are not allowed")

    def visit_Global(self, node: ast.Global) -> None:
        self.violations.append("'global' statements are not allowed")

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
        self.violations.append("'nonlocal' statements are not allowed")


def validate_ast(code: str) -> None:
    """Parse `code` and raise `SandboxViolationError` if it contains anything the allowlist
    rejects. Raises the same error for code that does not even parse — a syntax error is not
    this function's concern to explain, only whether the code is safe to hand to `exec`."""
    try:
        tree = ast.parse(code, mode="exec")
    except SyntaxError as exc:
        raise SandboxViolationError(f"Code does not parse: {exc}") from exc

    visitor = _ForbiddenNodeVisitor()
    visitor.visit(tree)
    if visitor.violations:
        raise SandboxViolationError(
            "Code failed the sandbox AST allowlist.", details={"violations": visitor.violations}
        )


@dataclass
class SandboxResult:
    """What one sandboxed execution produced. `result` is whatever the code assigned to a
    variable literally named `result` — the one contract between sandboxed code and its
    caller, chosen over "the value of the last expression" because `exec()` (unlike `eval()`)
    has no such notion for a multi-statement module. `stdout` is captured separately so a
    `print()`-based script (the natural way to write analysis code) is not silently discarded."""

    result: Any = None
    stdout: str = ""
    variables: dict[str, Any] = field(default_factory=dict)


def _run_sync(code: str, sandbox_globals: dict[str, Any]) -> SandboxResult:
    """The actual blocking `exec()` call — run on a worker thread by `run_sandboxed`, never
    called directly from async code (`ASYNC` lint rules on this codebase would catch that)."""
    stdout = io.StringIO()
    local_vars: dict[str, Any] = {}
    with redirect_stdout(stdout):
        exec(compile(code, "<sandbox>", "exec"), sandbox_globals, local_vars)
    return SandboxResult(
        result=local_vars.get("result"), stdout=stdout.getvalue(), variables=local_vars
    )


async def run_sandboxed(
    code: str, *, injected_globals: dict[str, Any] | None = None, timeout_seconds: float
) -> SandboxResult:
    """Validate, then execute, `code` under the sandbox's restricted builtins, bounded to
    `timeout_seconds` of wall-clock time.

    `injected_globals` is the sandbox's only sanctioned way to receive input data (e.g. the
    `python_analysis` tool's `data` parameter, or Cycle 5's `search`/`filter`/`batch`/
    `sub_agent`/`aggregate` API functions) — the code cannot reach anything outside this
    dict and `_SAFE_BUILTINS`, by construction.
    """
    validate_ast(code)

    sandbox_globals: dict[str, Any] = {"__builtins__": _SAFE_BUILTINS}
    sandbox_globals.update(injected_globals or {})

    loop = asyncio.get_running_loop()
    try:
        return await asyncio.wait_for(
            loop.run_in_executor(_SANDBOX_EXECUTOR, partial(_run_sync, code, sandbox_globals)),
            timeout=timeout_seconds,
        )
    except TimeoutError as exc:
        raise SandboxViolationError(
            f"Sandbox execution exceeded its {timeout_seconds}s wall-clock budget."
        ) from exc
    except SandboxViolationError:
        raise
    except Exception as exc:
        # Any runtime error *inside* the sandboxed code (a NameError from a typo, a ZeroDivisionError,
        # ...) is the caller's problem to see and react to, not this module's — wrapped in the
        # same exception type as a validation failure so every caller has one type to catch,
        # with the original error preserved in `details` for whoever is debugging it.
        raise SandboxViolationError(
            f"Sandbox execution raised {type(exc).__name__}: {exc}"
        ) from exc
