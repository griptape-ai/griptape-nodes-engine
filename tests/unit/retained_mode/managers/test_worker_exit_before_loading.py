"""A worker process that exits before it has loaded settles the runs waiting on it.

Such a worker never registered -- refused a license session, a crash on import -- so it never
unregisters and is never evicted. Without the orchestrator watching the process itself, a node
waiting on it would wait out the whole load timeout.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from griptape_nodes.retained_mode.managers.worker_manager import WorkerManager

_LIBRARY = "My Library"
_ARGS = ["/usr/bin/gtn", "engine"]


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
        await manager.spawn_worker(_ARGS, _LIBRARY)


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
async def test_an_exit_after_loading_is_left_to_the_heartbeat() -> None:
    """A loaded worker that dies is evicted when it stops answering, and that eviction says so."""
    manager = _worker_manager()
    manager.expect_worker(_LIBRARY)
    proc = _FakeProc()
    await _spawn(manager, proc)
    manager.note_library_loaded(_LIBRARY)

    proc.exit(1)
    await asyncio.sleep(0)

    assert manager.worker_unavailable_reason(_LIBRARY) is None
    assert manager._managed_worker_processes[_LIBRARY] is proc
