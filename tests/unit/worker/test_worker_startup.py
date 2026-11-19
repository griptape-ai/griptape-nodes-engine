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
from unittest.mock import AsyncMock, MagicMock

import pytest

from griptape_nodes.app.worker_routing import ActivateProjectRequest
from griptape_nodes.retained_mode.engine import reset_root_engine
from griptape_nodes.retained_mode.events.base_events import EventRequest, EventResultSuccess
from griptape_nodes.retained_mode.events.secrets_events import GetSecretValueRequest, GetSecretValueResultSuccess
from griptape_nodes.retained_mode.events.worker_events import (
    GetCommittedProjectRequest,
    GetCommittedProjectResultSuccess,
)
from griptape_nodes.retained_mode.managers.project_manager import SYSTEM_DEFAULTS_KEY
from griptape_nodes.retained_mode.managers.worker_manager import ENGINE_REQUEST_TIMEOUT_S
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
# Longer than the bound the slow-engine test gives an accepted worker's waits, and short enough to wait out.
_SLOW_ANSWER_S = 0.5
_BOUND_SHORTER_THAN_A_SLOW_ANSWER_S = 0.05
_WORKER_ID = "the-workers-own-id"


class _Engine:
    """The engine the worker works for: answers it, and records what it asked."""

    def __init__(
        self, channel: AsyncChannel, *, accept: bool | str | None, answers_after_s: float, knows_project: bool
    ) -> None:
        self._accept = accept
        self._answers_after_s = answers_after_s
        self._knows_project = knows_project
        self._held: set[str] = set()
        self.hellos: list[dict] = []
        self.secrets: list[str] = []
        self.checkpoints: list[dict] = []
        self.said_hello = threading.Event()
        self.channel = channel
        channel.start(on_request=self._on_request, on_notification=lambda _body: None, on_closed=lambda: None)
        activation = ActivateProjectRequest()
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
            await self._hold_first("hello")
            return {"accepted": self._accept, "details": "the test's engine decided."}
        if body["type"] == AUTHORIZATION_CHECKPOINT:
            self.checkpoints.append(body["payload"])
            return {"denial": None}
        event_request = read_event_request(body)
        if isinstance(event_request.request, GetSecretValueRequest):
            await self._hold_first("secret")
            self.secrets.append(event_request.request.key)
            answer = GetSecretValueResultSuccess(value=None, result_details="No such secret.")
            return result_body(
                EventResultSuccess(request=event_request.request, result=answer, request_id=event_request.request_id)
            )
        if isinstance(event_request.request, GetCommittedProjectRequest):
            await self._hold_first("project")
            if not self._knows_project:
                return request_failure(body, "the test's engine cannot say which project it is on.")
            project = GetCommittedProjectResultSuccess(project_id=SYSTEM_DEFAULTS_KEY, result_details="ok")
            return result_body(
                EventResultSuccess(request=event_request.request, result=project, request_id=event_request.request_id)
            )
        return request_failure(body, "The test's engine answers only secrets and which project it is on.")

    async def _hold_first(self, kind: str) -> None:
        """Answer the first question of each kind late, as an engine busy with something else does."""
        if kind in self._held:
            return
        self._held.add(kind)
        await asyncio.sleep(self._answers_after_s)


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

    def __init__(self, *, accept: bool | str | None, answers_after_s: float, knows_project: bool) -> None:
        self._loop = asyncio.new_event_loop()
        threading.Thread(target=self._loop.run_forever, daemon=True).start()
        self._pending = asyncio.run_coroutine_threadsafe(listen_for_worker(), self._loop).result(_TIMEOUT_S)
        self._accepted = asyncio.run_coroutine_threadsafe(
            self._accept(accept=accept, answers_after_s=answers_after_s, knows_project=knows_project), self._loop
        )

    @property
    def child_environment(self) -> dict[str, str]:
        return self._pending.child_environment

    def engine(self) -> _Engine:
        return self._accepted.result(_TIMEOUT_S)

    def hang_up_once_greeted(self) -> None:
        """Close the engine's end once the worker has said hello, as an engine that stops mid-answer does."""

        def hang_up() -> None:
            engine = self.engine()
            engine.said_hello.wait(_TIMEOUT_S)
            self._loop.call_soon_threadsafe(engine.channel.close)

        threading.Thread(target=hang_up, daemon=True).start()

    def stop(self) -> None:
        """Close the engine's end and let it finish reading, then stop its loop."""
        asyncio.run_coroutine_threadsafe(self._close(), self._loop).result(_TIMEOUT_S)
        self._loop.call_soon_threadsafe(self._loop.stop)

    async def _accept(self, *, accept: bool | str | None, answers_after_s: float, knows_project: bool) -> _Engine:
        channel = await self._pending.accept(_TIMEOUT_S)
        return _Engine(channel, accept=accept, answers_after_s=answers_after_s, knows_project=knows_project)

    async def _close(self) -> None:
        if not self._accepted.done():
            return
        channel = self._accepted.result().channel
        channel.close()
        # A request it never answered is still waiting; nothing will answer it now.
        for task in channel._tasks:
            task.cancel()
        await asyncio.gather(*channel._tasks, return_exceptions=True)


def _listen(
    monkeypatch: pytest.MonkeyPatch,
    *,
    accept: bool | str | None,
    answers_after_s: float = 0.0,
    knows_project: bool = True,
) -> _EngineThread:
    """Point this process's environment at an engine played by the test."""
    engine_thread = _EngineThread(accept=accept, answers_after_s=answers_after_s, knows_project=knows_project)
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
def test_a_worker_whose_engine_goes_away_before_answering_its_hello_exits_saying_why(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """An unanswered hello is waited on for as long as the channel is open, and ends when it closes."""
    engine_thread = _listen(monkeypatch, accept=_NEVER_ANSWERS)
    monkeypatch.setattr(runtime, "_exit_now", lambda: None)
    engine_thread.hang_up_once_greeted()

    try:
        exit_code = run_worker([])
    finally:
        engine_thread.stop()

    assert exit_code == 1
    assert "did not answer its hello: The worker channel is closed." in caplog.text


@pytest.mark.usefixtures("worker_process_state")
def test_a_starting_worker_outwaits_an_engine_slower_than_an_accepted_workers_bound(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A busy engine answers a secret, which project it is on, and the hello late; the worker hears each one."""
    monkeypatch.setattr(runtime, "PARENT_REQUEST_TIMEOUT_S", _BOUND_SHORTER_THAN_A_SLOW_ANSWER_S)
    engine_thread = _listen(monkeypatch, accept=False, answers_after_s=_SLOW_ANSWER_S)
    monkeypatch.setattr(runtime, "_exit_now", lambda: None)

    try:
        exit_code = run_worker([])
        engine = engine_thread.engine()
    finally:
        engine_thread.stop()

    # Refused, which is the engine's late answer to the hello: giving up on any wait would have ended it sooner.
    assert exit_code == 1
    assert len(engine.hellos) == 1
    assert "refused it: the test's engine decided." in caplog.text


@pytest.mark.usefixtures("worker_process_state")
def test_a_worker_that_cannot_adopt_its_engines_project_exits_saying_why(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The failed adoption ends startup with its own reason, and no library loads against the wrong project."""
    engine_thread = _listen(monkeypatch, accept=True, knows_project=False)
    monkeypatch.setattr(runtime, "_exit_now", lambda: None)

    try:
        exit_code = run_worker([])
        engine = engine_thread.engine()
    finally:
        engine_thread.stop()

    assert exit_code == 1
    assert engine.hellos == []
    assert "could not take on the project of the engine that started it" in caplog.text
    assert "the test's engine cannot say which project it is on." in caplog.text


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


def test_a_worker_waits_longer_than_its_parent_waits_on_another_engine() -> None:
    """A parent running the worker for another engine answers by asking it, so the worker must outwait that."""
    assert runtime.PARENT_REQUEST_TIMEOUT_S > ENGINE_REQUEST_TIMEOUT_S


@pytest.mark.asyncio
async def test_a_worker_bounds_what_it_forwards_to_its_engine_only_once_accepted() -> None:
    """Starting, a forwarded request waits for as long as the channel is open."""
    link = MagicMock()
    link.request_async = AsyncMock(return_value={})
    side = WorkerSide(link, on_parent_gone=lambda: None)
    sender = runtime._ChannelRequestSender(side)
    asked = EventRequest(request=GetCommittedProjectRequest())

    await sender.request_from_worker(asked)
    side.begin_serving()
    await sender.request_from_worker(asked)

    waited = [call.args[1] for call in link.request_async.call_args_list]
    assert waited == [None, runtime.PARENT_REQUEST_TIMEOUT_S]


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
