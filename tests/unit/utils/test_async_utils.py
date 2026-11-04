"""Tests for `call_function`, the dispatcher every registered callback goes through."""

from __future__ import annotations

from typing import Any

import pytest

from griptape_nodes.utils.async_utils import call_function

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
