"""Tests for `call_function`, the dispatcher every registered callback goes through, and `run_process_tree`."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from griptape_nodes.utils.async_utils import ProcessTreeResult, call_function, run_process_tree

INPUT = 21
DOUBLED = 42


class TestCallFunction:
    @pytest.mark.asyncio
    async def test_calls_a_sync_function(self) -> None:
        assert await call_function(lambda value: value * 2, INPUT) == DOUBLED

    @pytest.mark.asyncio
    async def test_awaits_an_async_function(self) -> None:
        async def double(value: int) -> int:
            return value * 2

        assert await call_function(double, INPUT) == DOUBLED

    @pytest.mark.asyncio
    async def test_awaits_a_callable_object_with_an_async_call(self) -> None:
        """The shape that broke every worker: a handler that is an instance, not a function.

        `inspect.iscoroutinefunction` inspects the object rather than its `__call__`, so an
        instance like this reads as synchronous. Returning its coroutine unawaited handed a
        coroutine object onward as if it were the result, and the first attribute access on
        the "result" failed far from the cause.
        """

        class Handler:
            async def __call__(self, value: int) -> int:
                return value * 2

        assert await call_function(Handler(), INPUT) == DOUBLED

    @pytest.mark.asyncio
    async def test_passes_keyword_arguments_through(self) -> None:
        def combine(first: str, *, second: str) -> str:
            return first + second

        assert await call_function(combine, "a", second="b") == "ab"

    @pytest.mark.asyncio
    async def test_a_sync_function_returning_a_plain_value_is_not_awaited(self) -> None:
        """Only awaitables are awaited; ordinary values pass straight through."""
        sentinel: dict[str, Any] = {"not": "awaitable"}
        assert await call_function(lambda: sentinel) is sentinel


GRANDCHILD_SCRIPT = """
import subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
open(sys.argv[1], "w").write(str(child.pid))
print("started", flush=True)
time.sleep(60)
"""


def _process_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A killed child of a dead parent is reaped by init; until then it is a zombie, which counts as gone.
    status_path = Path(f"/proc/{pid}/status")
    if status_path.exists():
        return "State:\tZ" not in status_path.read_text()
    return True


def _has_content(path: Path) -> bool:
    return path.exists() and bool(path.read_text())


def _same_directory(first: str, second: Path) -> bool:
    return Path(first).resolve() == second.resolve()


async def _wait_for_file(path: Path) -> None:
    for _ in range(200):
        if _has_content(path):
            return
        await asyncio.sleep(0.05)
    msg = f"{path} was never written"
    raise AssertionError(msg)


class TestRunProcessTree:
    @pytest.mark.asyncio
    async def test_returns_the_exit_code_and_combined_output(self, tmp_path: Path) -> None:
        script = "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"

        result = await run_process_tree([sys.executable, "-c", script], cwd=tmp_path, env=dict(os.environ), timeout=30)

        assert isinstance(result, ProcessTreeResult)
        assert result.returncode == 3  # noqa: PLR2004
        assert result.timed_out is False
        assert "out" in result.output
        assert "err" in result.output

    @pytest.mark.asyncio
    async def test_runs_in_the_given_directory_with_only_the_given_environment(self, tmp_path: Path) -> None:
        script = "import os; print(os.getcwd()); print(os.environ.get('ONLY_THIS')); print(os.environ.get('HOME'))"
        env = {"ONLY_THIS": "yes", "PATH": os.environ.get("PATH", "")}

        result = await run_process_tree([sys.executable, "-c", script], cwd=tmp_path, env=env, timeout=30)

        lines = result.output.splitlines()
        assert _same_directory(lines[0], tmp_path)
        assert lines[1] == "yes"
        assert lines[2] == "None"

    @pytest.mark.asyncio
    async def test_keeps_only_the_end_of_long_output(self, tmp_path: Path) -> None:
        script = "print('a' * 5000 + 'END')"

        result = await run_process_tree(
            [sys.executable, "-c", script], cwd=tmp_path, env=dict(os.environ), timeout=30, output_limit=100
        )

        assert len(result.output) == 100  # noqa: PLR2004
        assert result.output.rstrip().endswith("END")

    @pytest.mark.asyncio
    @pytest.mark.skipif(sys.platform == "win32", reason="checks the process group with POSIX signals")
    async def test_a_timeout_kills_the_child_and_its_children(self, tmp_path: Path) -> None:
        pid_file = tmp_path / "grandchild.pid"

        result = await run_process_tree(
            [sys.executable, "-c", GRANDCHILD_SCRIPT, str(pid_file)], cwd=tmp_path, env=dict(os.environ), timeout=2
        )

        assert result.timed_out is True
        assert result.returncode is None
        assert "started" in result.output
        grandchild_pid = int(pid_file.read_text())
        for _ in range(100):
            if not _process_is_running(grandchild_pid):
                break
            await asyncio.sleep(0.05)
        assert not _process_is_running(grandchild_pid)

    @pytest.mark.asyncio
    @pytest.mark.skipif(sys.platform == "win32", reason="checks the process group with POSIX signals")
    async def test_cancelling_kills_the_tree_and_reraises(self, tmp_path: Path) -> None:
        pid_file = tmp_path / "grandchild.pid"
        task = asyncio.create_task(
            run_process_tree(
                [sys.executable, "-c", GRANDCHILD_SCRIPT, str(pid_file)], cwd=tmp_path, env=dict(os.environ), timeout=60
            )
        )
        await _wait_for_file(pid_file)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        grandchild_pid = int(pid_file.read_text())
        for _ in range(100):
            if not _process_is_running(grandchild_pid):
                break
            await asyncio.sleep(0.05)
        assert not _process_is_running(grandchild_pid)

    @pytest.mark.asyncio
    @pytest.mark.skipif(sys.platform == "win32", reason="checks the process group with POSIX signals")
    async def test_a_leader_that_exits_is_done_even_when_a_descendant_holds_the_output_open(
        self, tmp_path: Path
    ) -> None:
        """The leader's exit ends the run; whatever it left behind in its group is stopped."""
        pid_file = tmp_path / "descendant.pid"
        script = f'sleep 30 & echo $! > "{pid_file}"; echo done; exit 0'

        result = await run_process_tree(["/bin/sh", "-c", script], cwd=tmp_path, env=dict(os.environ), timeout=3)

        assert result.timed_out is False
        assert result.returncode == 0
        assert "done" in result.output
        descendant_pid = int(pid_file.read_text())
        for _ in range(100):
            if not _process_is_running(descendant_pid):
                break
            await asyncio.sleep(0.05)
        assert not _process_is_running(descendant_pid)
