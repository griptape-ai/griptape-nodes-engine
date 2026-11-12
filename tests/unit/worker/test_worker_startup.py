"""A worker's startup against an engine played by the test, over a real channel in this process.

The worker runs the same code its entry point runs: it builds its engine, adopts the project the engine
sends as soon as it connects, loads what it serves, and says hello. The test's engine answers its
secrets and checkpoints and accepts or refuses the hello.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import threading
from typing import TYPE_CHECKING, Any

import pytest

from griptape_nodes.app.worker_routing import ActivateProjectRequest
from griptape_nodes.retained_mode.engine import reset_root_engine
from griptape_nodes.retained_mode.events.base_events import EventRequest, EventResultSuccess
from griptape_nodes.retained_mode.events.secrets_events import GetSecretValueRequest, GetSecretValueResultSuccess
from griptape_nodes.retained_mode.managers.project_manager import SYSTEM_DEFAULTS_KEY
from griptape_nodes.utils.version_utils import engine_version
from griptape_nodes.worker import runtime
from griptape_nodes.worker.__main__ import main
from griptape_nodes.worker.channel import AsyncChannel, WorkerLink, listen_for_worker
from griptape_nodes.worker.runtime import WorkerSide, run_worker
from griptape_nodes.worker.wire import (
    AUTHORIZATION_CHECKPOINT,
    WORKER_ENGINE_ID_ENV,
    WORKER_HELLO,
    event_request_envelope,
    read_event_request,
    request_failure,
    result_body,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

_TIMEOUT_S = 60.0
# An engine that takes a worker's hello and never answers it.
_NEVER_ANSWERS = "never answers"
_WORKER_ID = "the-workers-own-id"


class _Engine:
    """The engine the worker works for: answers it, and records what it asked."""

    def __init__(self, channel: AsyncChannel, *, accept: bool | str | None) -> None:
        self._accept = accept
        self.hellos: list[dict] = []
        self.secrets: list[str] = []
        self.checkpoints: list[dict] = []
        self.said_hello = threading.Event()
        self.channel = channel
        channel.start(on_request=self._on_request, on_notification=lambda _body: None, on_closed=lambda: None)
        activation = ActivateProjectRequest(project_id=SYSTEM_DEFAULTS_KEY, generation=1)
        channel.notify(event_request_envelope(EventRequest(request=activation)))

    async def _on_request(self, body: dict) -> dict:
        if body["type"] == WORKER_HELLO:
            self.hellos.append(body["payload"])
            self.said_hello.set()
            if self._accept is None:
                msg = "the test's engine failed to answer"
                raise RuntimeError(msg)
            if self._accept == _NEVER_ANSWERS:
                await asyncio.Event().wait()
            return {"accepted": self._accept, "details": "the test's engine decided."}
        if body["type"] == AUTHORIZATION_CHECKPOINT:
            self.checkpoints.append(body["payload"])
            return {"denial": None}
        event_request = read_event_request(body)
        if isinstance(event_request.request, GetSecretValueRequest):
            self.secrets.append(event_request.request.key)
            answer = GetSecretValueResultSuccess(value=None, result_details="No such secret.")
            return result_body(
                EventResultSuccess(request=event_request.request, result=answer, request_id=event_request.request_id)
            )
        return request_failure(body, "The test's engine answers only secrets.")


@pytest.fixture
def worker_process_state() -> Iterator[None]:
    """Put back what starting a worker changes for the whole process: the root engine, logging, SIGINT."""
    engine_logger = logging.getLogger("griptape_nodes")
    handlers = list(engine_logger.handlers)
    level = engine_logger.level
    sigint = signal.getsignal(signal.SIGINT)
    reset_root_engine()
    yield
    signal.signal(signal.SIGINT, sigint)
    engine_logger.handlers[:] = handlers
    engine_logger.setLevel(level)
    reset_root_engine()


class _EngineThread:
    """Plays the engine on a loop of its own: listens, and answers the worker once it connects."""

    def __init__(self, *, accept: bool | str | None) -> None:
        self._loop = asyncio.new_event_loop()
        threading.Thread(target=self._loop.run_forever, daemon=True).start()
        self._pending = asyncio.run_coroutine_threadsafe(listen_for_worker(), self._loop).result(_TIMEOUT_S)
        self._accepted = asyncio.run_coroutine_threadsafe(self._accept(accept=accept), self._loop)

    @property
    def child_environment(self) -> dict[str, str]:
        return self._pending.child_environment

    def engine(self) -> _Engine:
        return self._accepted.result(_TIMEOUT_S)

    def stop(self) -> None:
        """Close the engine's end and let it finish reading, then stop its loop."""
        asyncio.run_coroutine_threadsafe(self._close(), self._loop).result(_TIMEOUT_S)
        self._loop.call_soon_threadsafe(self._loop.stop)

    async def _accept(self, *, accept: bool | str | None) -> _Engine:
        return _Engine(await self._pending.accept(_TIMEOUT_S), accept=accept)

    async def _close(self) -> None:
        if not self._accepted.done():
            return
        channel = self._accepted.result().channel
        channel.close()
        # A request it never answered is still waiting; nothing will answer it now.
        for task in channel._tasks:
            task.cancel()
        await asyncio.gather(*channel._tasks, return_exceptions=True)


def _listen(monkeypatch: pytest.MonkeyPatch, *, accept: bool | str | None) -> _EngineThread:
    """Point this process's environment at an engine played by the test."""
    engine_thread = _EngineThread(accept=accept)
    for name, value in engine_thread.child_environment.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv(WORKER_ENGINE_ID_ENV, _WORKER_ID)
    return engine_thread


@pytest.mark.usefixtures("worker_process_state")
def test_a_worker_says_hello_and_exits_when_its_engine_refuses_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """The hello names the version and what it serves; what it read on the way came from its engine."""
    engine_thread = _listen(monkeypatch, accept=False)
    monkeypatch.setattr(runtime, "_exit_now", lambda: None)

    try:
        exit_code = run_worker([])
        engine = engine_thread.engine()
    finally:
        engine_thread.stop()
    [hello] = engine.hellos
    assert exit_code == 1
    assert hello["engine_version"] == engine_version
    assert hello["library_names"] == []
    # Building its engine reads the Griptape Cloud credential, from this engine and nowhere else.
    assert "GRIPTAPE_NODES_LICENSE" in engine.secrets


@pytest.mark.usefixtures("worker_process_state")
def test_a_worker_whose_hello_goes_unanswered_exits_saying_why(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A failed answer ends startup with the reason, not a traceback."""
    engine_thread = _listen(monkeypatch, accept=None)
    monkeypatch.setattr(runtime, "_exit_now", lambda: None)

    try:
        exit_code = run_worker([])
    finally:
        engine_thread.stop()

    assert exit_code == 1
    assert "did not answer its hello" in caplog.text
    assert "the test's engine failed to answer" in caplog.text


@pytest.mark.usefixtures("worker_process_state")
def test_a_worker_whose_hello_times_out_exits_naming_the_timeout(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A timeout's own message is empty, so the reason names what happened instead."""
    engine_thread = _listen(monkeypatch, accept=_NEVER_ANSWERS)
    monkeypatch.setattr(runtime, "_exit_now", lambda: None)
    monkeypatch.setattr(runtime, "_HELLO_TIMEOUT_S", 0.5)

    try:
        exit_code = run_worker([])
    finally:
        engine_thread.stop()

    assert exit_code == 1
    assert "did not answer its hello: TimeoutError" in caplog.text


@pytest.mark.usefixtures("worker_process_state")
@pytest.mark.asyncio
async def test_a_worker_its_engine_accepts_keeps_serving(monkeypatch: pytest.MonkeyPatch) -> None:
    """Accepted, it waits on its channel rather than exiting."""
    engine_thread = _listen(monkeypatch, accept=True)
    gone = threading.Event()
    side = WorkerSide(await asyncio.to_thread(WorkerLink, os.environ), on_parent_gone=gone.set)

    serving = asyncio.create_task(runtime._serve(side, [], _WORKER_ID))
    try:
        engine = await asyncio.to_thread(engine_thread.engine)
        assert await asyncio.to_thread(engine.said_hello.wait, _TIMEOUT_S)
        assert engine.hellos[0]["loaded"] is False
        assert not serving.done(), "an accepted worker keeps serving until its channel closes"
    finally:
        serving.cancel()
        side.link.close()
        engine_thread.stop()


def test_started_without_its_engine_the_entry_point_exits_non_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no engine ID or channel in its environment there is nothing to work for."""
    monkeypatch.delenv(WORKER_ENGINE_ID_ENV, raising=False)
    sigint = signal.getsignal(signal.SIGINT)
    try:
        assert main(["--library-name", "Some Library"]) == 1
    finally:
        signal.signal(signal.SIGINT, sigint)


def test_every_library_named_is_the_workers(mocker: Any) -> None:
    """A repeated --library-name gives the worker every library it names."""
    run = mocker.patch("griptape_nodes.worker.__main__.run_worker", return_value=0)

    assert main(["--library-name", "First Library", "--library-name", "Second Library"]) == 0

    run.assert_called_once_with(["First Library", "Second Library"])


def test_a_worker_naming_no_library_serves_every_library(mocker: Any) -> None:
    """With no --library-name the worker is the general-purpose one."""
    run = mocker.patch("griptape_nodes.worker.__main__.run_worker", return_value=0)

    main([])

    run.assert_called_once_with([])
