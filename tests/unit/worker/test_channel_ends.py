"""The two ends of a local worker's channel: what the engine screens, and what the worker asks it for.

Each test joins the engine's end (`ParentEnd`) and the worker's end over a real channel (loopback TCP)
in this process, with a real `Engine` behind whichever end the test exercises.
These are the security guarantees a worker relies on to never get more than the engine that started it:

- every request a worker sends passes the engine's worker request gates, and one they deny never runs
- a gate that raises denies
- every checkpoint is decided by the engine, and denied whenever it cannot be
- a worker refuses to start without that checkpoint hook
- a worker reads secrets from the engine, never from its own environment or `.env` files
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, NamedTuple
from unittest.mock import MagicMock, patch

import pytest
import pytest_asyncio

from griptape_nodes.retained_mode.engine import Engine, WorkerProcess
from griptape_nodes.retained_mode.events.app_events import (
    CurrentProjectChanged,
    ReportGeneralPurposeWorkerReadyRequest,
    ReportLibraryLoadedRequest,
)
from griptape_nodes.retained_mode.events.base_events import (
    AppEvent,
    EventRequest,
    EventResultSuccess,
    ExecutionEvent,
    ExecutionGriptapeNodeEvent,
    GriptapeNodeEvent,
    ProgressEvent,
    RequestPayload,
    ResultPayloadSuccess,
    WorkflowNotAlteredMixin,
)
from griptape_nodes.retained_mode.events.execution_events import NodeResolvedEvent, ParameterValueUpdateEvent
from griptape_nodes.retained_mode.events.generic_events import GenericResultFailure
from griptape_nodes.retained_mode.events.logger_events import LogHandlerEvent
from griptape_nodes.retained_mode.events.payload_registry import PayloadRegistry
from griptape_nodes.retained_mode.events.secrets_events import (
    GetSecretValueRequest,
    GetSecretValueResultFailure,
    GetSecretValueResultSuccess,
)
from griptape_nodes.retained_mode.managers.authorization_checkpoint import (
    AuthorizationCheckpoint,
    CheckpointAction,
    CheckpointDenial,
    CheckpointFailure,
    CheckpointSubjectType,
)
from griptape_nodes.retained_mode.managers.event_manager import WorkerRequestGateDecision, read_forwarded_result
from griptape_nodes.retained_mode.managers.read_through_secrets_manager import ReadThroughSecretsManager
from griptape_nodes.worker import runtime as runtime_module
from griptape_nodes.worker.channel import (
    AsyncChannel,
    ChannelRequestError,
    WorkerLink,
    listen_for_worker,
)
from griptape_nodes.worker.parent import ParentEnd
from griptape_nodes.worker.runtime import WorkerSide, WorkerStartupError, prepare_worker_engine
from griptape_nodes.worker.wire import APP_EVENT, EVENT_RESULT, EXECUTION_EVENT, event_body, event_request_envelope

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from griptape_nodes.retained_mode.managers.secrets_manager import SecretsManager

# Ceilings for a loaded CI box; every wait is on a reply or an event, not on time passing.
_TIMEOUT_S = 10.0
_FORBIDDEN_NODE_TYPE = "ForbiddenNode"


@dataclass
@PayloadRegistry.register
class WhoAnswersRequest(RequestPayload):
    """A request whose answer names the engine that handled it."""


@dataclass
@PayloadRegistry.register
class WhoAnswersResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    answered_by: str


class _Pair(NamedTuple):
    parent: AsyncChannel
    worker: WorkerLink


@pytest_asyncio.fixture
async def pair() -> AsyncIterator[_Pair]:
    """Two connected channel ends, neither started: the engine's on this loop, the worker's on its own thread."""
    pending = await listen_for_worker()
    accepting = asyncio.ensure_future(pending.accept(_TIMEOUT_S))
    worker = await asyncio.to_thread(WorkerLink, pending.child_environment)
    parent = await accepting
    yield _Pair(parent=parent, worker=worker)
    await _close(_Pair(parent=parent, worker=worker))


@pytest.fixture
def parent_engine() -> Engine:
    """The engine behind the engine's end of the channel."""
    return Engine()


def _answer_as(name: str) -> Any:
    def handle(request: WhoAnswersRequest) -> WhoAnswersResultSuccess:  # noqa: ARG001
        return WhoAnswersResultSuccess(answered_by=name, result_details=f"Answered by {name}.")

    return handle


def _parent_end(engine: Engine) -> ParentEnd:
    async def unexpected(*_args: Any) -> Any:
        msg = "Not part of this test."
        raise AssertionError(msg)

    return ParentEnd(engine, on_hello=unexpected, on_closed=lambda: None, relay_to_editor=unexpected)


def _start_plain(channel: AsyncChannel | WorkerLink, on_request: Any = None) -> threading.Event:
    """Start a channel end that answers with `on_request`; returns an event set once it closes."""
    closed = threading.Event()
    answer = on_request or (lambda _body: {})

    async def handle(body: dict) -> dict:
        return answer(body)

    channel.start(on_request=handle, on_notification=lambda _body: None, on_closed=closed.set)
    return closed


def _checkpoint(subject_id: str) -> AuthorizationCheckpoint:
    return AuthorizationCheckpoint(
        action=CheckpointAction.INSTANTIATE_NODE, subject_type=CheckpointSubjectType.NODE_TYPE, subject_id=subject_id
    )


def _deny_forbidden(checkpoint: AuthorizationCheckpoint) -> CheckpointDenial | None:
    if checkpoint.subject_id == _FORBIDDEN_NODE_TYPE:
        return CheckpointDenial(failures=(CheckpointFailure(detail="Your license does not include this node."),))
    return None


class TestWorkerRequestGate:
    @pytest.mark.asyncio
    async def test_a_denied_request_never_reaches_its_handler_and_says_the_license_refused_it(
        self, pair: _Pair, parent_engine: Engine
    ) -> None:
        handled: list[RequestPayload] = []

        def handle(request: WhoAnswersRequest) -> WhoAnswersResultSuccess:
            handled.append(request)
            return WhoAnswersResultSuccess(answered_by="parent", result_details="ok")

        parent_engine.event_manager.assign_manager_to_request_type(WhoAnswersRequest, handle)
        parent_engine.event_manager.add_worker_request_gate(
            lambda _envelope: WorkerRequestGateDecision(forward=None, denials=["not on this license"])
        )
        _parent_end(parent_engine).attach(pair.parent)
        _start_plain(pair.worker)

        request = WhoAnswersRequest()
        reply = await asyncio.to_thread(
            pair.worker.request, event_request_envelope(EventRequest(request=request)), _TIMEOUT_S
        )

        assert handled == []
        result = read_forwarded_result(request, reply)
        assert isinstance(result, GenericResultFailure)
        assert "WhoAnswersRequest" in str(result.result_details)
        assert "license policy refused it" in str(result.result_details)

    @pytest.mark.asyncio
    async def test_the_gate_sees_the_wire_envelope_and_an_allowed_request_runs(
        self, pair: _Pair, parent_engine: Engine
    ) -> None:
        """The app's license wire policy evaluates exactly the envelope a client's request arrives in."""
        seen: list[dict] = []

        def allow(envelope: dict) -> WorkerRequestGateDecision:
            seen.append(envelope)
            return WorkerRequestGateDecision(forward=envelope, denials=[])

        parent_engine.event_manager.assign_manager_to_request_type(WhoAnswersRequest, _answer_as("parent"))
        parent_engine.event_manager.add_worker_request_gate(allow)
        _parent_end(parent_engine).attach(pair.parent)
        _start_plain(pair.worker)

        request = WhoAnswersRequest()
        reply = await asyncio.to_thread(
            pair.worker.request, event_request_envelope(EventRequest(request=request)), _TIMEOUT_S
        )

        result = read_forwarded_result(request, reply)
        assert isinstance(result, WhoAnswersResultSuccess)
        assert result.answered_by == "parent"
        [envelope] = seen
        assert envelope["type"] == "EventRequest"
        assert envelope["payload"]["event_type"] == "EventRequest"
        assert envelope["payload"]["request_type"] == "WhoAnswersRequest"
        assert isinstance(envelope["payload"]["request"], dict)

    def test_a_gate_that_raises_denies_and_says_so_at_error(
        self, parent_engine: Engine, caplog: pytest.LogCaptureFixture
    ) -> None:
        def broken(_envelope: dict) -> WorkerRequestGateDecision:
            msg = "policy failed to load"
            raise RuntimeError(msg)

        parent_engine.event_manager.add_worker_request_gate(broken)
        envelope = event_request_envelope(EventRequest(request=WhoAnswersRequest()))

        with caplog.at_level(logging.ERROR, logger="griptape_nodes"):
            decision = _parent_end(parent_engine).gate(envelope)

        assert decision.dispatch is None
        assert decision.reply is not None
        assert "license policy refused it" in str(decision.reply["result"])
        assert any(record.levelno == logging.ERROR and "broken" in record.getMessage() for record in caplog.records)

    def test_no_gate_installed_allows(self, parent_engine: Engine) -> None:
        envelope = event_request_envelope(EventRequest(request=WhoAnswersRequest()))

        decision = _parent_end(parent_engine).gate(envelope)

        assert decision.dispatch == envelope

    def test_something_that_is_not_a_single_request_is_refused_before_any_gate_runs(
        self, parent_engine: Engine
    ) -> None:
        """The wire policy passes what does not declare itself a request, so it must never be read as one."""
        gate = MagicMock(side_effect=AssertionError("must not run"))
        parent_engine.event_manager.add_worker_request_gate(gate)
        envelope = event_request_envelope(EventRequest(request=WhoAnswersRequest()))
        envelope["payload"]["event_type"] = "EventRequestBatch"

        decision = _parent_end(parent_engine).gate(envelope)

        assert decision.dispatch is None
        gate.assert_not_called()

    def test_a_body_of_no_known_type_is_refused(self, parent_engine: Engine) -> None:
        decision = _parent_end(parent_engine).gate({"type": "RunAnything", "payload": {}})

        assert decision.dispatch is None


class TestCheckpoints:
    @pytest.mark.asyncio
    async def test_the_engine_decides_a_workers_checkpoint(self, pair: _Pair, parent_engine: Engine) -> None:
        parent_engine.event_manager.add_authorization_hook(_deny_forbidden)
        _parent_end(parent_engine).attach(pair.parent)
        side = WorkerSide(pair.worker, on_parent_gone=lambda: None)
        side.start(asyncio.get_running_loop())

        denied = await asyncio.to_thread(side.authorize_with_parent, _checkpoint(_FORBIDDEN_NODE_TYPE))
        allowed = await asyncio.to_thread(side.authorize_with_parent, _checkpoint("AllowedNode"))

        assert denied is not None
        assert denied.messages() == ["Your license does not include this node."]
        assert allowed is None

    @pytest.mark.asyncio
    async def test_a_checkpoint_is_denied_once_the_channel_is_down(self, pair: _Pair) -> None:
        gone = threading.Event()
        side = WorkerSide(pair.worker, on_parent_gone=gone.set)
        side.start(asyncio.get_running_loop())
        pair.parent.close()
        assert await asyncio.to_thread(gone.wait, _TIMEOUT_S)

        denial = await asyncio.to_thread(side.authorize_with_parent, _checkpoint("AllowedNode"))

        assert denial is not None
        assert "could not be asked" in denial.reason()

    @pytest.mark.asyncio
    async def test_a_checkpoint_is_denied_when_the_engine_does_not_answer_with_a_verdict(self, pair: _Pair) -> None:
        def fail(_body: dict) -> dict:
            msg = "the engine's handler failed"
            raise RuntimeError(msg)

        _start_plain(pair.parent, on_request=fail)
        side = WorkerSide(pair.worker, on_parent_gone=lambda: None)
        side.start(asyncio.get_running_loop())

        denial = await asyncio.to_thread(side.authorize_with_parent, _checkpoint("AllowedNode"))

        assert denial is not None

    def test_a_checkpoint_the_engine_cannot_read_is_denied(self, parent_engine: Engine) -> None:
        reply = _parent_end(parent_engine).answer_checkpoint({"type": "AuthorizationCheckpoint", "payload": {"x": 1}})

        assert reply["denial"] is not None

    def test_a_worker_refuses_to_start_without_its_checkpoint_hook(self) -> None:
        engine = Engine()
        side = WorkerSide(MagicMock(), on_parent_gone=lambda: None)

        with (
            patch.object(engine.event_manager, "add_authorization_hook"),
            pytest.raises(WorkerStartupError, match="permissions"),
        ):
            prepare_worker_engine(engine, side)


class TestSecrets:
    @pytest.mark.asyncio
    async def test_a_worker_reads_the_engines_secret_and_never_its_own(
        self, pair: _Pair, parent_engine: Engine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        def parent_secret(request: GetSecretValueRequest) -> GetSecretValueResultSuccess:
            return GetSecretValueResultSuccess(value=f"{request.key} from the engine", result_details="ok")

        parent_engine.event_manager.replace_manager_for_request_type(GetSecretValueRequest, parent_secret)
        _parent_end(parent_engine).attach(pair.parent)
        side = WorkerSide(pair.worker, on_parent_gone=lambda: None)
        side.start(asyncio.get_running_loop())

        monkeypatch.setenv("GTN_TEST_PLANTED_SECRET", "planted in the worker's environment")
        global_env = tmp_path / "global.env"
        global_env.write_text("GTN_TEST_PLANTED_SECRET=planted in .env\nGTN_TEST_DOTENV_ONLY=planted\n")
        monkeypatch.setattr("griptape_nodes.retained_mode.managers.secrets_manager.ENV_VAR_PATH", global_env)
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / ".env").write_text("GTN_TEST_PLANTED_SECRET=planted in the workspace .env\n")
        monkeypatch.delenv("GTN_TEST_DOTENV_ONLY", raising=False)
        config_manager = MagicMock(workspace_path=workspace)

        manager = ReadThroughSecretsManager(config_manager, read_secret=side.read_secret)
        value = await asyncio.to_thread(manager.get_secret, "GTN_TEST_PLANTED_SECRET")

        assert value == "GTN_TEST_PLANTED_SECRET from the engine"
        # The worker's .env files never reach its environment, where library code could read them.
        assert "GTN_TEST_DOTENV_ONLY" not in os.environ


class TestAWorkerNeverWritesSecrets:
    """A worker's secrets are its engine's: it writes neither the `.env` nor the environment of its machine."""

    def _worker_secrets(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[SecretsManager, Path]:
        global_env = tmp_path / "global.env"
        global_env.write_text("")
        monkeypatch.setattr("griptape_nodes.retained_mode.managers.secrets_manager.ENV_VAR_PATH", global_env)
        monkeypatch.delenv("GTN_TEST_TO_REGISTER", raising=False)
        config_manager = MagicMock(workspace_path=tmp_path)
        config_manager.get_config_value.return_value = {"GTN_TEST_TO_REGISTER": "a default"}
        return ReadThroughSecretsManager(config_manager, read_secret=lambda _name: None), global_env

    def test_registering_secrets_writes_nothing(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        manager, global_env = self._worker_secrets(monkeypatch, tmp_path)

        manager.register_all_secrets()

        assert global_env.read_text() == ""
        assert "GTN_TEST_TO_REGISTER" not in os.environ

    def test_setting_a_secret_is_refused_and_writes_nothing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        manager, global_env = self._worker_secrets(monkeypatch, tmp_path)

        with pytest.raises(RuntimeError, match="reads its secrets from the engine that started it"):
            manager.set_secret("GTN_TEST_TO_REGISTER", "a value")

        assert global_env.read_text() == ""
        assert "GTN_TEST_TO_REGISTER" not in os.environ


class TestOrdering:
    @pytest.mark.asyncio
    async def test_a_nodes_events_reach_the_engines_queue_before_its_result(
        self, pair: _Pair, parent_engine: Engine
    ) -> None:
        queued: list[Any] = []
        parent_engine.event_manager.put_event = queued.append  # type: ignore[method-assign]
        _parent_end(parent_engine).attach(pair.parent)

        def run_node(_body: dict) -> dict:
            for value in range(3):
                event = ExecutionEvent(
                    payload=ParameterValueUpdateEvent(
                        node_name="node", parameter_name="out", data_type="int", value=value
                    ),
                    workflow_id="workflow",
                )
                # Written now, on the I/O loop this handler runs on, as the worker's own sends are.
                pair.worker.channel.notify(event_body(EXECUTION_EVENT, event))
            return {"done": True}

        _start_plain(pair.worker, on_request=run_node)

        reply = await pair.parent.request({"type": "EventRequest"}, _TIMEOUT_S)

        assert reply == {"done": True}
        assert [event.wrapped_event.payload.value for event in queued] == [0, 1, 2]
        assert all(isinstance(event, ExecutionGriptapeNodeEvent) for event in queued)

    @pytest.mark.asyncio
    async def test_a_worker_answers_once_every_event_before_its_answer_has_been_sent(self) -> None:
        engine = Engine()
        engine.event_manager.initialize_queue()
        engine.event_manager.assign_manager_to_request_type(WhoAnswersRequest, _answer_as("worker"))
        channel = MagicMock()
        side = WorkerSide(channel, on_parent_gone=lambda: None)
        side.arm(engine)
        drain = asyncio.create_task(side.drain_events())
        for value in range(2):
            engine.event_manager.put_event(
                ExecutionGriptapeNodeEvent(
                    wrapped_event=ExecutionEvent(
                        payload=ParameterValueUpdateEvent(
                            node_name="node", parameter_name="out", data_type="int", value=value
                        )
                    )
                )
            )

        try:
            await asyncio.wait_for(side.answer_parent_request(EventRequest(request=WhoAnswersRequest())), _TIMEOUT_S)
            sent = [call.args[0] for call in channel.notify.call_args_list]
        finally:
            drain.cancel()

        assert [body["payload"]["payload"]["value"] for body in sent] == [0, 1]


class TestAppEvents:
    @pytest.mark.asyncio
    async def test_telemetry_is_queued_and_any_other_app_event_goes_to_the_editor_unacted_on(
        self, pair: _Pair, parent_engine: Engine
    ) -> None:
        queued: list[Any] = []
        relayed: list[tuple[str, dict]] = []
        relay_done = asyncio.Event()
        parent_engine.event_manager.put_event = queued.append  # type: ignore[method-assign]

        async def relay(event_type: str, payload: dict) -> None:
            relayed.append((event_type, payload))
            relay_done.set()

        async def unexpected(*_args: Any) -> Any:
            msg = "Not part of this test."
            raise AssertionError(msg)

        ParentEnd(parent_engine, on_hello=unexpected, on_closed=lambda: None, relay_to_editor=relay).attach(pair.parent)
        _start_plain(pair.worker)

        pair.worker.notify(
            event_body(APP_EVENT, AppEvent(payload=CurrentProjectChanged(project_id="the-workers-project")))
        )
        pair.worker.notify(
            event_body(APP_EVENT, AppEvent(payload=LogHandlerEvent(message="m", levelname="INFO", created=1.0)))
        )
        await asyncio.wait_for(relay_done.wait(), _TIMEOUT_S)
        await _until(lambda: queued)

        [(event_type, payload)] = relayed
        assert event_type == APP_EVENT
        assert payload["payload_type"] == "CurrentProjectChanged"
        [log_event] = queued
        assert isinstance(log_event.payload, LogHandlerEvent)


async def _until(predicate: Any) -> None:
    """Wait for state that a callback on another loop writes."""
    async with asyncio.timeout(_TIMEOUT_S):
        while not predicate():  # noqa: ASYNC110 (nothing here to set an Event from)
            await asyncio.sleep(0.01)


class TestSyncWaits:
    @pytest.mark.asyncio
    async def test_node_code_waiting_synchronously_on_the_engine_gets_its_answers(
        self, pair: _Pair, parent_engine: Engine
    ) -> None:
        """A forwarded request and a checkpoint, both blocking the worker's engine loop, as node code does."""
        parent_engine.event_manager.assign_manager_to_request_type(WhoAnswersRequest, _answer_as("parent"))
        parent_engine.event_manager.add_authorization_hook(_deny_forbidden)
        _parent_end(parent_engine).attach(pair.parent)

        worker_loop = asyncio.new_event_loop()
        worker_thread = threading.Thread(target=worker_loop.run_forever, daemon=True)
        worker_thread.start()
        side = WorkerSide(pair.worker, on_parent_gone=lambda: None)
        side.start(worker_loop)

        async def build_worker() -> Engine:
            engine = Engine(worker=WorkerProcess(engine_id="the-workers-id", read_secret=side.read_secret))
            engine.event_manager.assign_manager_to_request_type(WhoAnswersRequest, _answer_as("worker"))
            prepare_worker_engine(engine, side)
            side.arm(engine)
            return engine

        async def run_node_code(engine: Engine) -> tuple[Any, CheckpointDenial | None]:
            # Deliberately synchronous, holding this loop the whole time, as node code calling the facade does.
            with engine.event_manager.node_execution_scope():
                result = engine.handle_request(WhoAnswersRequest())
                denial = engine.event_manager.evaluate_authorization_checkpoint(_checkpoint(_FORBIDDEN_NODE_TYPE))
            return result, denial

        try:
            worker_engine = await asyncio.wait_for(
                asyncio.wrap_future(asyncio.run_coroutine_threadsafe(build_worker(), worker_loop)), _TIMEOUT_S
            )
            result, denial = await asyncio.wait_for(
                asyncio.wrap_future(asyncio.run_coroutine_threadsafe(run_node_code(worker_engine), worker_loop)),
                _TIMEOUT_S,
            )
        finally:
            worker_loop.call_soon_threadsafe(worker_loop.stop)
            worker_thread.join(_TIMEOUT_S)

        assert isinstance(result, WhoAnswersResultSuccess)
        assert result.answered_by == "parent"
        assert denial is not None
        assert denial.messages() == ["Your license does not include this node."]


class TestWhatAWorkerSends:
    """The worker sends its engine only what the editor would have seen, as the editor would have seen it."""

    @pytest.mark.asyncio
    async def test_progress_results_and_app_events_go_to_the_engine_and_element_events_do_not(self) -> None:
        engine = Engine()
        engine.event_manager.initialize_queue()
        channel = MagicMock()
        side = WorkerSide(channel, on_parent_gone=lambda: None)
        side.arm(engine)
        drain = asyncio.create_task(side.drain_events())
        engine.event_manager.put_event(ProgressEvent(value="a token", node_name="node", parameter_name="out"))
        engine.event_manager.put_event(
            ExecutionGriptapeNodeEvent(
                wrapped_event=ExecutionEvent(
                    payload=NodeResolvedEvent(node_name="node", parameter_output_values={}, node_type="Node")
                )
            )
        )
        engine.event_manager.put_event(
            GriptapeNodeEvent(
                wrapped_event=EventResultSuccess(
                    request=WhoAnswersRequest(),
                    result=WhoAnswersResultSuccess(answered_by="worker", result_details="ok"),
                )
            )
        )
        engine.event_manager.put_event(AppEvent(payload=CurrentProjectChanged(project_id="the-workers-project")))

        try:
            await asyncio.wait_for(side.flush_events(), _TIMEOUT_S)
        finally:
            drain.cancel()

        sent = [call.args[0] for call in channel.notify.call_args_list]
        assert [body["type"] for body in sent] == [EXECUTION_EVENT, EVENT_RESULT, APP_EVENT]
        assert sent[0]["payload"]["payload_type"] == "GriptapeEvent"
        assert sent[0]["payload"]["payload"]["value"] == "a token"
        assert sent[1]["payload"]["result_type"] == "WhoAnswersResultSuccess"


class TestWhatAWorkerSendsAsItsChannelEnds:
    @pytest.mark.asyncio
    async def test_the_drain_stops_quietly_once_the_channel_closes_mid_stream(self, pair: _Pair) -> None:
        received: list[Any] = []
        pair.parent.start(on_request=_never_asked, on_notification=received.append, on_closed=lambda: None)
        gone = threading.Event()
        engine = Engine()
        engine.event_manager.initialize_queue()
        side = WorkerSide(pair.worker, on_parent_gone=gone.set)
        side.start(asyncio.get_running_loop())
        side.arm(engine)
        drain = asyncio.create_task(side.drain_events())
        engine.event_manager.put_event(_value_update(0))
        await asyncio.wait_for(side.flush_events(), _TIMEOUT_S)
        await _until(lambda: len(received) == 1)

        pair.parent.close()
        assert await asyncio.to_thread(gone.wait, _TIMEOUT_S)
        engine.event_manager.put_event(_value_update(1))
        engine.event_manager.put_event(_value_update(2))

        await asyncio.wait_for(drain, _TIMEOUT_S)
        assert [body["payload"]["payload"]["value"] for body in received] == [0]

    @pytest.mark.asyncio
    async def test_an_event_the_channel_cannot_encode_is_dropped_and_the_rest_still_go(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        engine = Engine()
        engine.event_manager.initialize_queue()
        link = MagicMock()
        sent: list[dict] = []

        def notify(body: dict) -> None:
            if body["payload"]["payload"]["value"] == 1:
                msg = "Object of type set is not JSON serializable"
                raise TypeError(msg)
            sent.append(body)

        link.notify.side_effect = notify
        side = WorkerSide(link, on_parent_gone=lambda: None)
        side.arm(engine)
        drain = asyncio.create_task(side.drain_events())
        for value in range(3):
            engine.event_manager.put_event(_value_update(value))

        try:
            await asyncio.wait_for(side.flush_events(), _TIMEOUT_S)
        finally:
            drain.cancel()

        assert [body["payload"]["payload"]["value"] for body in sent] == [0, 2]
        assert "not JSON serializable" in caplog.text


async def _never_asked(_body: dict) -> dict:
    msg = "Not part of this test."
    raise AssertionError(msg)


def _value_update(value: int) -> ExecutionGriptapeNodeEvent:
    return ExecutionGriptapeNodeEvent(
        wrapped_event=ExecutionEvent(
            payload=ParameterValueUpdateEvent(node_name="node", parameter_name="out", data_type="int", value=value)
        )
    )


class TestWhatAWorkerAnswers:
    @pytest.mark.asyncio
    async def test_the_engines_requests_and_one_way_sends_are_handled_by_the_worker(self, pair: _Pair) -> None:
        handled: list[str] = []

        def handle(request: WhoAnswersRequest) -> WhoAnswersResultSuccess:  # noqa: ARG001
            handled.append("handled")
            return WhoAnswersResultSuccess(answered_by="worker", result_details="ok")

        engine = Engine()
        engine.event_manager.initialize_queue()
        engine.event_manager.assign_manager_to_request_type(WhoAnswersRequest, handle)
        side = WorkerSide(pair.worker, on_parent_gone=lambda: None)
        side.start(asyncio.get_running_loop())
        _start_plain(pair.parent)
        # Sent before the worker can answer it: held, then handled once the worker's engine exists.
        pair.parent.notify(event_request_envelope(EventRequest(request=WhoAnswersRequest())))
        side.arm(engine)
        drain = asyncio.create_task(side.drain_events())

        try:
            request = WhoAnswersRequest()
            reply = await pair.parent.request(event_request_envelope(EventRequest(request=request)), _TIMEOUT_S)
            await _until(lambda: len(handled) == 2)  # noqa: PLR2004 (the one-way send, and the request)
            unreadable = event_request_envelope(EventRequest(request=WhoAnswersRequest()))
            unreadable["payload"]["request_type"] = "NoSuchRequest"
            refused = await pair.parent.request(unreadable, _TIMEOUT_S)
            with pytest.raises(ChannelRequestError, match="does not answer a 'WorkerHello'"):
                await pair.parent.request({"type": "WorkerHello"}, _TIMEOUT_S)
        finally:
            drain.cancel()

        result = read_forwarded_result(request, reply)
        assert isinstance(result, WhoAnswersResultSuccess)
        assert result.answered_by == "worker"
        assert refused["result_type"] == "GenericResultFailure"
        assert "NoSuchRequest" in str(refused["result"])

    @pytest.mark.asyncio
    async def test_a_secret_the_engine_cannot_give_fails_loudly(self, pair: _Pair, parent_engine: Engine) -> None:
        def refuse(request: GetSecretValueRequest) -> GetSecretValueResultFailure:
            return GetSecretValueResultFailure(result_details=f"Secret '{request.key}' not found.")

        parent_engine.event_manager.replace_manager_for_request_type(GetSecretValueRequest, refuse)
        _parent_end(parent_engine).attach(pair.parent)
        side = WorkerSide(pair.worker, on_parent_gone=lambda: None)
        side.start(asyncio.get_running_loop())

        with pytest.raises(RuntimeError, match="Secret 'MISSING' not found"):
            await asyncio.to_thread(side.read_secret, "MISSING")

        pair.parent.close()
        with pytest.raises(RuntimeError, match="from the engine this worker works for"):
            await asyncio.to_thread(side.read_secret, "ANY")


class TestWhatTheEngineRefuses:
    @pytest.mark.asyncio
    async def test_unreadable_or_unexpected_bodies_are_refused_or_dropped(
        self, pair: _Pair, parent_engine: Engine
    ) -> None:
        queued: list[Any] = []
        parent_engine.event_manager.put_event = queued.append  # type: ignore[method-assign]
        _parent_end(parent_engine).attach(pair.parent)
        _start_plain(pair.worker)

        unreadable = event_request_envelope(EventRequest(request=WhoAnswersRequest()))
        unreadable["payload"]["request_type"] = "NoSuchRequest"
        refused = await asyncio.to_thread(pair.worker.request, unreadable, _TIMEOUT_S)
        element_event = ExecutionEvent(
            payload=NodeResolvedEvent(node_name="node", parameter_output_values={}, node_type="Node")
        )
        pair.worker.notify(event_body(EXECUTION_EVENT, element_event))
        pair.worker.notify({"type": APP_EVENT})
        pair.worker.notify({"type": "Anything"})
        # Ordered behind the notifications above, so they have all been handled once it is answered.
        await asyncio.to_thread(pair.worker.request, {"type": "AuthorizationCheckpoint", "payload": {}}, _TIMEOUT_S)
        # Last, since a refused hello ends the channel once it is answered.
        bad_hello = await asyncio.to_thread(pair.worker.request, {"type": "WorkerHello", "payload": {}}, _TIMEOUT_S)

        assert refused["result_type"] == "GenericResultFailure"
        assert bad_hello["accepted"] is False
        assert queued == []


async def _close(pair: _Pair) -> None:
    """Close both ends, and let the engine's end finish reading before this loop ends."""
    pair.parent.close()
    pair.worker.close()
    await asyncio.wait_for(asyncio.gather(*pair.parent._tasks, return_exceptions=True), _TIMEOUT_S)


class TestAWorkersHelloCoversEveryLibrary:
    def test_one_library_not_loading_fails_the_hello_and_names_it(self) -> None:
        engine = MagicMock()
        engine.library_manager.get_library_info_by_library_name.return_value = None
        reports: list[ReportLibraryLoadedRequest | ReportGeneralPurposeWorkerReadyRequest] = [
            ReportLibraryLoadedRequest(library_name="First Library", fitness="GOOD")
        ]

        hello = runtime_module._hello(engine, ["First Library", "Second Library"], reports)

        assert hello.library_names == ["First Library", "Second Library"]
        assert hello.loaded is False
        assert hello.failure_reason is not None
        assert "'Second Library'" in hello.failure_reason
        assert "'First Library'" not in hello.failure_reason
