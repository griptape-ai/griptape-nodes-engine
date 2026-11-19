"""A worker process that exits before it has connected settles the runs waiting on it.

Such a worker never connects -- refused a license session, a crash on import -- so its channel
never opens and it is never taken on. Without the engine watching the process itself, a node
waiting on it would wait out the whole load timeout.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from griptape_nodes.retained_mode.managers.worker_manager import WorkerManager

# The spawn under test runs for real, with its process faked by `asyncio.create_subprocess_exec`.
pytestmark = pytest.mark.worker_processes

_LIBRARY = "My Library"
_ARGS = ["/usr/bin/gtn", "engine"]
_TIMEOUT_S = 5.0


class _FakeProc:
    """A worker process whose exit the test controls."""

    def __init__(self) -> None:
        self.pid = 4242
        self.returncode: int | None = None
        self._exited = asyncio.Event()

    def exit(self, code: int) -> None:
        self.returncode = code
        self._exited.set()

    async def wait(self) -> int | None:
        await self._exited.wait()
        return self.returncode

    def terminate(self) -> None:
        self.exit(-15)

    def kill(self) -> None:
        self.exit(-9)


def _worker_manager() -> WorkerManager:
    engine = MagicMock()
    engine.get_session_id.return_value = "sess-abc"
    engine.config_manager.get_config_value.side_effect = lambda _key, default, cast_type=None: (
        default if cast_type is None else cast_type(default)
    )
    engine.project_manager.get_pre_project_environ.return_value = {}
    engine.library_manager.environment.execution_site_packages.return_value = None
    manager = WorkerManager(engine=engine, event_manager=MagicMock())
    manager.attach_transport(
        send_message=AsyncMock(),
    )
    manager._orchestrator_static_server_base_url = AsyncMock(return_value=None)  # type: ignore[method-assign]
    return manager


async def _until(predicate: Any) -> None:
    """Wait for state a background task writes, giving up after `_TIMEOUT_S`."""
    async with asyncio.timeout(_TIMEOUT_S):
        while not predicate():  # noqa: ASYNC110 (nothing here to set an Event from)
            await asyncio.sleep(0.01)


async def _spawn(manager: WorkerManager, proc: Any) -> asyncio.Task[None]:
    """Start the worker's connect phase in the background, with `proc` standing in for its process.

    Returns once the process is spawned and tracked, parked waiting for it to connect -- the state
    the test needs before it decides whether `proc` connects, exits, or is stopped out from under it.
    """
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        task = asyncio.create_task(manager._spawn_local_worker(_ARGS, _LIBRARY))
        await _until(lambda: manager._managed_worker_processes.get(_LIBRARY) is proc)
    return task


@pytest.mark.asyncio
async def test_an_exit_before_connecting_releases_the_waiters_with_the_reason() -> None:
    """The waiter is released as soon as the process exits, not when the load timeout runs out."""
    manager = _worker_manager()
    manager.expect_worker(_LIBRARY)
    proc = _FakeProc()
    task = await _spawn(manager, proc)
    waiter = asyncio.create_task(manager.wait_until_executable(_LIBRARY))
    await asyncio.sleep(0)
    assert not waiter.done()

    proc.exit(1)
    await asyncio.wait_for(waiter, timeout=_TIMEOUT_S)
    await asyncio.wait_for(task, timeout=_TIMEOUT_S)

    assert manager.has_settled(_LIBRARY)
    reason = manager.worker_unavailable_reason(_LIBRARY)
    assert reason is not None
    assert "stopped while starting up" in reason
    assert "exit code 1" in reason
    assert _LIBRARY not in manager._managed_worker_processes


@pytest.mark.asyncio
async def test_a_process_this_manager_stopped_is_not_a_death() -> None:
    """A reload stops every worker and spawns them again; the exits it causes blame nothing."""
    manager = _worker_manager()
    manager.expect_worker(_LIBRARY)
    proc = _FakeProc()
    task = await _spawn(manager, proc)

    await manager.reset_workers()
    await asyncio.wait_for(task, timeout=_TIMEOUT_S)

    assert manager.worker_unavailable_reason(_LIBRARY) is None


@pytest.mark.asyncio
async def test_an_exit_after_loading_is_left_to_the_channel_ending() -> None:
    """A loaded worker is watched through its channel, not through this early-exit path."""
    manager = _worker_manager()
    manager.expect_worker(_LIBRARY)
    proc = _FakeProc()
    task = await _spawn(manager, proc)
    manager.note_library_loaded(_LIBRARY)

    proc.exit(1)
    await asyncio.wait_for(task, timeout=_TIMEOUT_S)

    assert manager.worker_unavailable_reason(_LIBRARY) is None
    assert manager._managed_worker_processes[_LIBRARY] is proc
