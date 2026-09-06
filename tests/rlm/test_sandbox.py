"""Tests for `rlm/sandbox.py`'s AST allowlist, restricted execution, and timeout — the security-
critical component `docs/ARCHITECTURE.md` says backs both the Python Analysis tool (Cycle 4)
and the RLM planner's generated plans (Cycle 5). `docs/DELIVERY_PLAN.md`'s acceptance criterion
1 names exactly the AST rejections tested here: `import os`, `__class__` access, and file I/O.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from backend.app.core.errors import SandboxViolationError
from backend.app.rlm.sandbox import _SANDBOX_EXECUTOR, run_sandboxed, validate_ast


def _violations(code: str) -> list[str]:
    """`SandboxViolationError.message` is a fixed, generic string on purpose (see
    `validate_ast`'s docstring) — the specifics live in `details["violations"]`, which is what
    every test below actually asserts against."""
    with pytest.raises(SandboxViolationError) as exc_info:
        validate_ast(code)
    details = exc_info.value.details
    assert details is not None
    violations = details["violations"]
    assert isinstance(violations, list)
    return violations


class TestValidateAst:
    def test_import_statement_is_rejected(self) -> None:
        assert any("import" in v for v in _violations("import os"))

    def test_import_from_is_rejected(self) -> None:
        assert any("import" in v for v in _violations("from os import path"))

    def test_dunder_attribute_access_is_rejected(self) -> None:
        assert any("dunder" in v for v in _violations("().__class__"))

    def test_globals_dunder_access_is_rejected(self) -> None:
        assert any("dunder" in v for v in _violations("(lambda: 1).__globals__"))

    def test_a_bare_dunder_name_is_rejected(self) -> None:
        """Found by live security testing: `result = __builtins__` (a `Name` node, not an
        `Attribute` node) leaked the sandbox's restricted builtins dict past a version of
        `visit_Name` that only checked `_FORBIDDEN_NAMES`, never dunder-shaped identifiers in
        general — `visit_Attribute`'s dunder check only ever saw `.__dunder__` access, not a
        bare dunder name used directly."""
        assert any("dunder" in v for v in _violations("result = __builtins__"))

    def test_other_bare_dunder_names_exec_populates_are_also_rejected(self) -> None:
        """`exec()` auto-populates more than just `__builtins__` into the globals dict it is
        given — `__name__`, `__loader__`, `__spec__`, `__package__`, `__doc__` are all
        candidates for the same leak, so the fix checks dunder *shape*, not one specific name."""
        for name in ("__name__", "__loader__", "__package__", "__doc__"):
            assert any("dunder" in v for v in _violations(f"result = {name}")), name

    def test_open_is_rejected(self) -> None:
        assert any("open" in v for v in _violations("open('/etc/passwd')"))

    def test_exec_is_rejected(self) -> None:
        assert any("exec" in v for v in _violations("exec('1')"))

    def test_eval_is_rejected(self) -> None:
        assert any("eval" in v for v in _violations("eval('1')"))

    def test_with_statement_is_rejected(self) -> None:
        assert any("with" in v for v in _violations("with x as y:\n    pass"))

    def test_syntax_error_is_reported_as_a_violation(self) -> None:
        with pytest.raises(SandboxViolationError, match="does not parse"):
            validate_ast("def (:")

    def test_multiple_violations_are_all_reported(self) -> None:
        assert len(_violations("import os\nopen('x')")) == 2

    def test_ordinary_analysis_code_passes(self) -> None:
        validate_ast("result = sum(x for x in data if x > 0)")


class TestRunSandboxed:
    async def test_result_variable_is_returned(self) -> None:
        outcome = await run_sandboxed("result = 1 + 1", timeout_seconds=1.0)

        assert outcome.result == 2

    async def test_injected_data_is_visible_to_the_code(self) -> None:
        outcome = await run_sandboxed(
            "result = sum(data)", injected_globals={"data": [1, 2, 3]}, timeout_seconds=1.0
        )

        assert outcome.result == 6

    async def test_print_output_is_captured_as_stdout(self) -> None:
        outcome = await run_sandboxed("print('hello')", timeout_seconds=1.0)

        assert "hello" in outcome.stdout

    async def test_no_result_assignment_yields_none(self) -> None:
        outcome = await run_sandboxed("x = 1", timeout_seconds=1.0)

        assert outcome.result is None

    async def test_code_failing_the_ast_allowlist_never_runs(self) -> None:
        with pytest.raises(SandboxViolationError):
            await run_sandboxed("import os\nresult = os.getcwd()", timeout_seconds=1.0)

    async def test_a_runtime_error_inside_the_sandbox_is_wrapped(self) -> None:
        with pytest.raises(SandboxViolationError, match="ZeroDivisionError"):
            await run_sandboxed("result = 1 / 0", timeout_seconds=1.0)

    async def test_exceeding_the_wall_clock_budget_raises(self) -> None:
        # `rlm/sandbox.py`'s module docstring explains why the abandoned thread cannot be
        # killed once `wait_for` gives up: it keeps running to completion in the background,
        # and Python's own `atexit` machinery joins every `ThreadPoolExecutor` worker before
        # the process (here, the test run) can exit — so this workload is deliberately small
        # (a few million iterations, comfortably over the 0.01s budget but done in well under a
        # second) rather than the kind of runaway loop a real sandbox violation might contain.
        with pytest.raises(SandboxViolationError, match="wall-clock"):
            await run_sandboxed("result = sum(i for i in range(5_000_000))", timeout_seconds=0.01)

    async def test_builtins_outside_the_safe_allowlist_are_unreachable_even_when_ast_allows_the_name(
        self,
    ) -> None:
        """Defense-in-depth: `type` is not in `_FORBIDDEN_NAMES` (the AST allowlist has no
        opinion on it), yet it is still unusable, because `_SAFE_BUILTINS` never exposed it in
        the first place — proving the second layer (stripped builtins) holds independently of
        the first (the AST check)."""
        with pytest.raises(SandboxViolationError, match="NameError"):
            await run_sandboxed("result = type(1)", timeout_seconds=1.0)


class TestSandboxExecutorIsolation:
    """Found by live security testing (docs/ASSUMPTIONS_AND_TRADEOFFS.md): a sandboxed
    CPU-bound infinite loop cannot be killed once `asyncio.wait_for` gives up on it (see the
    module docstring), so it permanently occupies one worker thread for the process's life.
    Pins that this no longer happens against the same shared pool every other
    `run_in_executor(None, ...)` call in the process would draw from."""

    def test_the_sandbox_uses_its_own_dedicated_executor_not_the_process_default(self) -> None:
        assert isinstance(_SANDBOX_EXECUTOR, ThreadPoolExecutor)
        assert _SANDBOX_EXECUTOR._max_workers > 0
