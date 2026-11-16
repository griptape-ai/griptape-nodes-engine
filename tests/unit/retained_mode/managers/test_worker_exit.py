"""A worker process that exits on its own settles the runs waiting on it, with its exit code.

One that exits before loading never registered -- refused a license session, a crash on import --
so it never unregisters and is never evicted. Without the orchestrator watching the process itself,
a node waiting on it would wait out the whole load timeout.
"""

from __future__ import annotations

import asyncio
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from griptape_nodes.retained_mode.events.worker_events import WorkerGoneError
from griptape_nodes.retained_mode.managers.worker_manager import WorkerManager, WorkerRegistration, describe_exit_code

_LIBRARY = "My Library"
_ARGS = ["/usr/bin/gtn", "engine"]
_WORKER = "worker-1"


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
        subscribe_to_topic=AsyncMock(),
        unsubscribe_from_topic=AsyncMock(),
        request_client=MagicMock(fail_requests_by_tag=AsyncMock()),
    )
    manager._orchestrator_static_server_base_url = AsyncMock(return_value=None)  # type: ignore[method-assign]
    return manager


async def _spawn(manager: WorkerManager, proc: Any) -> None:
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        await manager._spawn_watched_worker(_ARGS, _LIBRARY)


@pytest.mark.asyncio
async def test_an_exit_before_loading_releases_the_waiters_with_the_reason() -> None:
    """The waiter is released as soon as the process exits, not when the load timeout runs out."""
    manager = _worker_manager()
    manager.expect_worker(_LIBRARY)
    proc = _FakeProc()
    await _spawn(manager, proc)
    waiter = asyncio.create_task(manager.wait_until_executable(_LIBRARY))
    await asyncio.sleep(0)
    assert not waiter.done()

    proc.exit(1)
    await asyncio.wait_for(waiter, timeout=1)

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
    await _spawn(manager, _FakeProc())

    await manager.reset_workers()
    await asyncio.sleep(0)

    assert manager.worker_unavailable_reason(_LIBRARY) is None


@pytest.mark.asyncio
async def test_an_exit_after_loading_removes_the_worker_with_its_exit_code() -> None:
    """A loaded worker that dies is removed at once, blamed on how it ended rather than on missed heartbeats."""
    manager = _worker_manager()
    manager.expect_worker(_LIBRARY)
    proc = _FakeProc()
    await _spawn(manager, proc)
    manager._workers[_WORKER] = WorkerRegistration(request_topic="worker-topic", worker_key=_LIBRARY)
    manager.note_library_loaded(_LIBRARY)

    proc.exit(-9)
    await asyncio.sleep(0)

    assert _WORKER not in manager._workers
    assert _LIBRARY not in manager._managed_worker_processes
    fail = cast("AsyncMock", manager._tx.request_client.fail_requests_by_tag)
    fail.assert_awaited_once()
    assert fail.await_args is not None
    tag, error = fail.await_args.args
    assert tag == _WORKER
    assert isinstance(error, WorkerGoneError)
    assert "exited unexpectedly (signal 9: the system killed it" in str(error)
    reason = manager.worker_unavailable_reason(_LIBRARY)
    assert reason is not None
    assert "exited unexpectedly (signal 9" in reason


@pytest.mark.parametrize(
    ("exit_code", "description"),
    [
        (0, "exit code 0"),
        (1, "exit code 1"),
        (-9, "signal 9: the system killed it, which usually means it ran out of memory"),
        (-11, "signal 11: it crashed"),
        (-7, "signal 7"),
        (137, "exit code 137, signal 9: the system killed it, which usually means it ran out of memory"),
        (0xC0000005, "exit code 0xC0000005: it crashed"),
        (0xC0000017, "exit code 0xC0000017: it ran out of memory"),
        (0xC0000135, "exit code 0xC0000135"),
    ],
)
def test_describe_exit_code(exit_code: int, description: str) -> None:
    """An exit code is decoded by its range, so a code from another OS still reads right."""
    assert describe_exit_code(exit_code) == description
