"""Tests for `call_function`, the dispatcher every registered callback goes through."""

from __future__ import annotations

import subprocess
import sys
import time
from typing import Any

import pytest

from griptape_nodes.utils.async_utils import call_function, subprocess_run

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


_FAILING_EXIT_CODE = 2
# A child stopped after its output stops being read exits well before its 60 s sleep would.
_PROCESS_STOP_SECONDS = 30


class TestSubprocessRunStreamingStderr:
    @pytest.mark.asyncio
    async def test_hands_each_stderr_line_to_the_callback_and_still_returns_all_output(self) -> None:
        lines: list[str] = []
        script = (
            "import sys\n"
            "sys.stdout.write('out\\n'); sys.stdout.flush()\n"
            "sys.stderr.write('first\\n'); sys.stderr.flush()\n"
            "sys.stderr.write('second\\r\\n'); sys.stderr.flush()\n"
        )

        result = await subprocess_run(
            [sys.executable, "-c", script], capture_output=True, text=True, on_stderr_line=lines.append
        )

        assert lines == ["first", "second"]
        assert result.stdout == "out\n"
        assert result.stderr == "first\nsecond\r\n"

    @pytest.mark.asyncio
    async def test_a_failure_still_carries_the_streamed_stderr(self) -> None:
        lines: list[str] = []
        script = "import sys\nsys.stderr.write('no solution found\\n')\nsys.exit(2)\n"

        with pytest.raises(subprocess.CalledProcessError) as raised:
            await subprocess_run([sys.executable, "-c", script], check=True, text=True, on_stderr_line=lines.append)

        assert lines == ["no solution found"]
        assert raised.value.stderr == "no solution found\n"
        assert raised.value.returncode == _FAILING_EXIT_CODE

    @pytest.mark.asyncio
    async def test_a_line_longer_than_the_stream_limit_is_still_delivered(self) -> None:
        lines: list[str] = []
        long_line_length = 200_000
        script = f"import sys\nsys.stderr.write('x' * {long_line_length} + '\\nend\\n')\n"

        result = await subprocess_run([sys.executable, "-c", script], text=True, on_stderr_line=lines.append)

        assert [len(line) for line in lines] == [long_line_length, len("end")]
        assert result.returncode == 0

    @pytest.mark.asyncio
    async def test_a_callback_that_raises_stops_the_process(self) -> None:
        def explode(_line: str) -> None:
            raise RuntimeError

        started = time.monotonic()
        script = "import sys, time\nsys.stderr.write('go\\n'); sys.stderr.flush()\ntime.sleep(60)\n"

        with pytest.raises(RuntimeError):
            await subprocess_run([sys.executable, "-c", script], capture_output=True, on_stderr_line=explode)

        assert time.monotonic() - started < _PROCESS_STOP_SECONDS
