"""A worker one engine runs for another: the serving engine carries it, and the artist's engine decides everything it asks for.

The artist's engine (`artist`) and the serving engine (`serving`) are real engines in this process. The
link between them is modeled by handing a request from one straight to the other's dispatch, with the
sender and envelope fields the app's transport sets. The worker is played by the other end of a real
channel. These are the escalation rows for a remote worker:

- a request the worker sends reaches the artist's engine as its own type, and the artist's gate decides it
- the worker's checkpoints are decided by the artist's authorization hooks
- the serving engine applies no gate or hook of its own to the worker's traffic
- the worker's Griptape Cloud credential is the artist's license session when the artist's engine is
  licensed, and none while that engine has no session; an artist's engine with no license lets the
  worker use the serving engine's API key. The serving engine's license is never used.
"""

from __future__ import annotations

import asyncio
import functools
import threading
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, NamedTuple, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from griptape_nodes.drivers.cloud_credentials import API_KEY_SECRET_NAME, LICENSE_SECRET_NAME
from griptape_nodes.retained_mode.engine import Engine
from griptape_nodes.retained_mode.engine_transports import EngineNotFoundError
from griptape_nodes.retained_mode.events import worker_events
from griptape_nodes.retained_mode.events.arbitrary_python_events import RunArbitraryPythonStringRequest
from griptape_nodes.retained_mode.events.base_events import (
    EventRequest,
    ExecutionEvent,
    ExecutionGriptapeNodeEvent,
    RequestPayload,
    RequestSender,
    ResultPayload,
    ResultPayloadSuccess,
    WorkflowNotAlteredMixin,
)
from griptape_nodes.retained_mode.events.execution_events import (
    CancelExecuteNodeRequest,
    ExecuteNodeRequest,
    ParameterValueUpdateEvent,
)
from griptape_nodes.retained_mode.events.generic_events import GenericResultFailure
from griptape_nodes.retained_mode.events.payload_registry import PayloadRegistry
from griptape_nodes.retained_mode.events.secrets_events import GetSecretValueRequest, GetSecretValueResultSuccess
from griptape_nodes.retained_mode.managers.authorization_checkpoint import (
    AuthorizationCheckpoint,
    CheckpointAction,
    CheckpointDenial,
    CheckpointFailure,
    CheckpointSubjectType,
)
from griptape_nodes.retained_mode.managers.event_manager import (
    RemoteWorkerSecretAnswer,
    WorkerRequestGateDecision,
    read_forwarded_result,
)
from griptape_nodes.retained_mode.managers.worker_manager import (
    WorkerRegistration,
    _EngineRun,
    _ServedFor,
    _StartAskedOfHost,
)
from griptape_nodes.worker.channel import AsyncChannel, WorkerLink, listen_for_worker
from griptape_nodes.worker.remote import RemoteWorkerEnd, _EventBatches
from griptape_nodes.worker.runtime import WorkerSide
from griptape_nodes.worker.wire import (
    EXECUTION_EVENT,
    WORKER_HELLO,
    WorkerHello,
    event_body,
    event_request_envelope,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

# Ceilings for a loaded CI box; every wait is on a reply or an event, not on time passing.
_TIMEOUT_S = 10.0
_ARTIST = "artist-workstation"
_SERVING = "gpu-01"
_WORKER = "remote-worker-1"
_LIBRARY = "Exec Library"
_LEASE = "lease-1"
_SESSION_JWT = "session-jwt-of-the-artist"  # a stand-in, not a secret
_FORBIDDEN_NODE_TYPE = "ForbiddenNode"


@dataclass
@PayloadRegistry.register
class RemoteProbeRequest(RequestPayload):
    """A request whose answer names the engine that handled it."""


@dataclass
@PayloadRegistry.register
class RemoteProbeResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    answered_by: str


class _Pair(NamedTuple):
    serving: AsyncChannel
    worker: WorkerLink


@pytest_asyncio.fixture
async def pair() -> AsyncIterator[_Pair]:
    """The serving engine's end of the worker's channel, on this loop, and the worker's own, neither started."""
    pending = await listen_for_worker()
    accepting = asyncio.ensure_future(pending.accept(_TIMEOUT_S))
    worker = await asyncio.to_thread(WorkerLink, pending.child_environment)
    serving = await accepting
    yield _Pair(serving=serving, worker=worker)
    serving.close()
    worker.close()
    await asyncio.wait_for(asyncio.gather(*serving._tasks, return_exceptions=True), _TIMEOUT_S)


def _start_worker_end(link: WorkerLink, answer: Callable[[dict], dict]) -> None:
    """Start the worker's end of the channel answering every request with `answer`, on its I/O thread."""

    async def on_request(body: dict) -> dict:
        return answer(body)

    link.start(on_request=on_request, on_notification=lambda _body: None, on_closed=lambda: None)


@pytest.fixture
def artist() -> Engine:
    """The artist's engine, which has asked `_SERVING` to run `_WORKER` for it."""
    engine = Engine()
    engine.worker_manager._workers[_WORKER] = WorkerRegistration(
        request_topic="", worker_key=_LIBRARY, host_engine_id=_SERVING
    )
    return engine


@pytest.fixture
def serving() -> Engine:
    """The serving engine. Its own gate and hook deny everything, so any use of them would show."""
    engine = Engine()
    engine.event_manager.add_worker_request_gate(_refuse_on_the_serving_engine)
    engine.event_manager.add_authorization_hook(_deny_on_the_serving_engine)
    return engine


def _refuse_on_the_serving_engine(_envelope: dict) -> WorkerRequestGateDecision:
    msg = "The serving engine's gate must not screen a remote worker's request."
    raise AssertionError(msg)


def _deny_on_the_serving_engine(_checkpoint: AuthorizationCheckpoint) -> CheckpointDenial | None:
    msg = "The serving engine's hooks must not decide a remote worker's checkpoint."
    raise AssertionError(msg)


def _answer_as(name: str) -> Callable[[RemoteProbeRequest], RemoteProbeResultSuccess]:
    def handle(request: RemoteProbeRequest) -> RemoteProbeResultSuccess:  # noqa: ARG001
        return RemoteProbeResultSuccess(answered_by=name, result_details=f"Answered by {name}.")

    return handle


def _checkpoint(subject_id: str) -> AuthorizationCheckpoint:
    return AuthorizationCheckpoint(
        action=CheckpointAction.INSTANTIATE_NODE, subject_type=CheckpointSubjectType.NODE_TYPE, subject_id=subject_id
    )


def _deny_forbidden(checkpoint: AuthorizationCheckpoint) -> CheckpointDenial | None:
    if checkpoint.subject_id == _FORBIDDEN_NODE_TYPE:
        return CheckpointDenial(failures=(CheckpointFailure(detail="Your license does not include this node."),))
    return None


def _answering(answers: dict[str, RemoteWorkerSecretAnswer]) -> Callable[[str], RemoteWorkerSecretAnswer | None]:
    """A remote worker secret policy that answers the secrets in `answers` and leaves every other one alone."""
    return answers.get


def _link(to: Engine, *, sender_engine_id: str) -> Callable[..., Awaitable[ResultPayload]]:
    """`Engine.request` from `sender_engine_id` to `to`, as the app's transport delivers it."""

    async def request(
        _engine_id: str,
        request: RequestPayload,
        *,
        timeout_s: float | None,  # noqa: ARG001
        open_workflow_id: str | None = None,
        origin_engine_id: str | None = None,
    ) -> ResultPayload:
        result_event = await to.event_manager.ahandle_request(
            request,
            result_context={
                "request_id": uuid.uuid4().hex,
                "response_topic": f"engines/{sender_engine_id}/response",
                "sender": RequestSender(engine_id=sender_engine_id),
                "open_workflow_id": open_workflow_id,
                "origin_engine_id": origin_engine_id,
            },
        )
        return result_event.result

    return request


def _carry(serving: Engine, channel: AsyncChannel, artist: Engine) -> RemoteWorkerEnd:
    """Have `serving` carry the worker on `channel` to `artist`, over a modeled link."""

    async def unexpected(*_args: Any) -> Any:
        msg = "Not part of this test."
        raise AssertionError(msg)

    serving.request = _link(artist, sender_engine_id=_SERVING)  # type: ignore[method-assign]
    end = RemoteWorkerEnd(
        serving,
        worker_engine_id=_WORKER,
        artist_engine_id=_ARTIST,
        request_timeout_s=_TIMEOUT_S,
        on_hello=unexpected,
        on_closed=lambda: None,
    )
    end.attach(channel)
    return end


def _start_worker(channel: WorkerLink) -> WorkerSide:
    side = WorkerSide(channel, on_parent_gone=lambda: None)
    side.start(asyncio.get_running_loop())
    return side


async def _from_the_serving_engine(artist: Engine, request: RequestPayload, *, sent_by: str = _SERVING) -> Any:
    """Dispatch `request` on the artist's engine as `sent_by` sends it on its own behalf."""
    result_event = await artist.event_manager.ahandle_request(
        request,
        result_context={
            "request_id": "r1",
            "response_topic": f"engines/{sent_by}/response",
            "sender": RequestSender(engine_id=sent_by),
        },
    )
    return result_event.result


async def _from_the_worker(artist: Engine, request: RequestPayload, *, sent_by: str = _SERVING) -> Any:
    """Dispatch `request` on the artist's engine as `sent_by` relays it from `_WORKER`."""
    result_event = await artist.event_manager.ahandle_request(
        request,
        result_context={
            "request_id": "r1",
            "response_topic": f"engines/{sent_by}/response",
            "sender": RequestSender(engine_id=sent_by),
            "open_workflow_id": None,
            "origin_engine_id": _WORKER,
        },
    )
    return result_event.result


class TestTheArtistsEngineAdmitsTheWorkersRequests:
    @pytest.mark.asyncio
    async def test_a_request_reaches_the_artists_gate_as_its_own_type_and_runs_there(self, artist: Engine) -> None:
        seen: list[dict] = []

        def allow(envelope: dict) -> WorkerRequestGateDecision:
            seen.append(envelope)
            return WorkerRequestGateDecision(forward=envelope, denials=[])

        artist.event_manager.assign_manager_to_request_type(RemoteProbeRequest, _answer_as("artist"))
        artist.event_manager.add_worker_request_gate(allow)

        result = await _from_the_worker(artist, RemoteProbeRequest())

        assert isinstance(result, RemoteProbeResultSuccess)
        assert result.answered_by == "artist"
        [envelope] = seen
        assert envelope["type"] == "EventRequest"
        assert envelope["payload"]["request_type"] == "RemoteProbeRequest"

    @pytest.mark.asyncio
    async def test_a_request_the_artists_gate_denies_never_runs(self, artist: Engine) -> None:
        handled: list[RequestPayload] = []

        def handle(request: RemoteProbeRequest) -> RemoteProbeResultSuccess:
            handled.append(request)
            return RemoteProbeResultSuccess(answered_by="artist", result_details="ok")

        artist.event_manager.assign_manager_to_request_type(RemoteProbeRequest, handle)
        artist.event_manager.add_worker_request_gate(
            lambda _envelope: WorkerRequestGateDecision(forward=None, denials=["not on this license"])
        )

        result = await _from_the_worker(artist, RemoteProbeRequest())

        assert handled == []
        assert isinstance(result, GenericResultFailure)
        assert "license policy refused it" in str(result.result_details)

    @pytest.mark.asyncio
    async def test_a_request_relayed_by_an_engine_that_does_not_run_the_worker_is_refused(self, artist: Engine) -> None:
        gate = MagicMock(side_effect=AssertionError("must not run"))
        artist.event_manager.assign_manager_to_request_type(RemoteProbeRequest, _answer_as("artist"))
        artist.event_manager.add_worker_request_gate(gate)

        result = await _from_the_worker(artist, RemoteProbeRequest(), sent_by="some-other-engine")

        assert isinstance(result, GenericResultFailure)
        assert _WORKER in str(result.result_details)
        gate.assert_not_called()

    def test_a_synchronous_dispatch_never_admits_a_workers_request(self, artist: Engine) -> None:
        artist.event_manager.assign_manager_to_request_type(RemoteProbeRequest, _answer_as("artist"))

        result_event = artist.event_manager.handle_request(
            RemoteProbeRequest(),
            result_context={"sender": RequestSender(engine_id=_SERVING), "origin_engine_id": _WORKER},
        )

        assert isinstance(result_event.result, GenericResultFailure)

    @pytest.mark.asyncio
    async def test_the_artists_hooks_decide_the_workers_checkpoint(self, artist: Engine) -> None:
        artist.event_manager.add_authorization_hook(_deny_forbidden)
        forbidden = event_body("AuthorizationCheckpoint", _checkpoint(_FORBIDDEN_NODE_TYPE))["payload"]
        allowed = event_body("AuthorizationCheckpoint", _checkpoint("AllowedNode"))["payload"]

        denied = await _from_the_serving_engine(
            artist, worker_events.DecideWorkerCheckpointRequest(worker_engine_id=_WORKER, checkpoint=forbidden)
        )
        passed = await _from_the_serving_engine(
            artist, worker_events.DecideWorkerCheckpointRequest(worker_engine_id=_WORKER, checkpoint=allowed)
        )

        assert isinstance(denied, worker_events.DecideWorkerCheckpointResultSuccess)
        assert denied.denial is not None
        assert isinstance(passed, worker_events.DecideWorkerCheckpointResultSuccess)
        assert passed.denial is None

    @pytest.mark.asyncio
    async def test_a_checkpoint_from_an_engine_that_does_not_run_the_worker_is_refused(self, artist: Engine) -> None:
        allowed = event_body("AuthorizationCheckpoint", _checkpoint("AllowedNode"))["payload"]

        result = await _from_the_serving_engine(
            artist,
            worker_events.DecideWorkerCheckpointRequest(worker_engine_id=_WORKER, checkpoint=allowed),
            sent_by="some-other-engine",
        )

        assert not result.succeeded()


class TestTheRemoteWorkerSecretPolicy:
    @pytest.mark.asyncio
    async def test_a_secret_the_policy_decides_is_answered_with_its_value(self, artist: Engine) -> None:
        artist.event_manager.set_remote_worker_secret_policy(
            _answering({LICENSE_SECRET_NAME: RemoteWorkerSecretAnswer(value=_SESSION_JWT)})
        )

        result = await _from_the_worker(artist, GetSecretValueRequest(key=LICENSE_SECRET_NAME))

        assert isinstance(result, worker_events.RemoteWorkerSecretResultSuccess)
        assert result.value == _SESSION_JWT
        assert result.from_serving_engine is False
        assert _SESSION_JWT not in repr(result)

    @pytest.mark.asyncio
    async def test_a_secret_the_policy_leaves_alone_is_the_artists_own(
        self, artist: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GTN_TEST_REMOTE_SECRET", "the artist's value")
        artist.event_manager.set_remote_worker_secret_policy(_answering({}))

        result = await _from_the_worker(artist, GetSecretValueRequest(key="GTN_TEST_REMOTE_SECRET"))

        assert isinstance(result, GetSecretValueResultSuccess)
        assert result.value == "the artist's value"

    @pytest.mark.asyncio
    async def test_with_no_policy_every_secret_is_the_artists_own(
        self, artist: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GTN_TEST_REMOTE_SECRET", "the artist's value")

        result = await _from_the_worker(artist, GetSecretValueRequest(key="GTN_TEST_REMOTE_SECRET"))

        assert isinstance(result, GetSecretValueResultSuccess)
        assert result.value == "the artist's value"

    @pytest.mark.asyncio
    async def test_a_policy_that_raises_answers_no_value(self, artist: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
        def broken(_secret_name: str) -> RemoteWorkerSecretAnswer | None:
            msg = "no session"
            raise RuntimeError(msg)

        monkeypatch.setenv(LICENSE_SECRET_NAME, "license-jwt")
        artist.event_manager.set_remote_worker_secret_policy(broken)

        result = await _from_the_worker(artist, GetSecretValueRequest(key=LICENSE_SECRET_NAME))

        assert isinstance(result, worker_events.RemoteWorkerSecretResultSuccess)
        assert result.value is None
        assert result.from_serving_engine is False

    def test_an_answers_value_is_kept_out_of_its_repr(self) -> None:
        assert _SESSION_JWT not in repr(RemoteWorkerSecretAnswer(value=_SESSION_JWT))


class TestTheServingEngineCarriesTheWorker:
    @pytest.mark.asyncio
    async def test_a_request_reaches_the_artist_as_itself_and_the_serving_engine_screens_nothing(
        self, pair: _Pair, serving: Engine, artist: Engine
    ) -> None:
        seen: list[str] = []

        def allow(envelope: dict) -> WorkerRequestGateDecision:
            seen.append(envelope["payload"]["request_type"])
            return WorkerRequestGateDecision(forward=envelope, denials=[])

        artist.event_manager.assign_manager_to_request_type(RemoteProbeRequest, _answer_as("artist"))
        serving.event_manager.assign_manager_to_request_type(RemoteProbeRequest, _answer_as("serving"))
        artist.event_manager.add_worker_request_gate(allow)
        _carry(serving, pair.serving, artist)
        _start_worker(pair.worker)

        request = RemoteProbeRequest()
        reply = await asyncio.to_thread(
            pair.worker.request, event_request_envelope(EventRequest(request=request)), _TIMEOUT_S
        )

        result = read_forwarded_result(request, reply)
        assert isinstance(result, RemoteProbeResultSuccess)
        assert result.answered_by == "artist"
        assert seen == ["RemoteProbeRequest"]

    @pytest.mark.asyncio
    async def test_a_checkpoint_is_decided_by_the_artist_and_not_the_serving_engine(
        self, pair: _Pair, serving: Engine, artist: Engine
    ) -> None:
        artist.event_manager.add_authorization_hook(_deny_forbidden)
        _carry(serving, pair.serving, artist)
        side = _start_worker(pair.worker)

        denied = await asyncio.to_thread(side.authorize_with_parent, _checkpoint(_FORBIDDEN_NODE_TYPE))
        allowed = await asyncio.to_thread(side.authorize_with_parent, _checkpoint("AllowedNode"))

        assert denied is not None
        assert denied.messages() == ["Your license does not include this node."]
        assert allowed is None

    @pytest.mark.asyncio
    async def test_a_checkpoint_the_artist_cannot_be_asked_about_is_denied(self, pair: _Pair, serving: Engine) -> None:
        async def lost(*_args: Any, **_kwargs: Any) -> ResultPayload:
            msg = "the connection was lost"
            raise EngineNotFoundError(msg)

        end = RemoteWorkerEnd(
            serving,
            worker_engine_id=_WORKER,
            artist_engine_id=_ARTIST,
            request_timeout_s=_TIMEOUT_S,
            on_hello=MagicMock(),
            on_closed=lambda: None,
        )
        serving.request = lost  # type: ignore[method-assign]
        end.attach(pair.serving)
        side = _start_worker(pair.worker)

        denial = await asyncio.to_thread(side.authorize_with_parent, _checkpoint("AllowedNode"))

        assert denial is not None

    @pytest.mark.asyncio
    async def test_the_worker_reads_the_value_the_artists_policy_answers_and_never_the_serving_engines(
        self, pair: _Pair, serving: Engine, artist: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Both engines share this process's environment, so this is also what the serving engine would read.
        monkeypatch.setenv(LICENSE_SECRET_NAME, "the serving engine's license")
        monkeypatch.setenv(API_KEY_SECRET_NAME, "the serving engine's api key")
        session = RemoteWorkerSecretAnswer(value=_SESSION_JWT)
        artist.event_manager.set_remote_worker_secret_policy(
            _answering({LICENSE_SECRET_NAME: session, API_KEY_SECRET_NAME: session})
        )
        _carry(serving, pair.serving, artist)
        side = _start_worker(pair.worker)

        license_value = await asyncio.to_thread(side.read_secret, LICENSE_SECRET_NAME)
        api_key_value = await asyncio.to_thread(side.read_secret, API_KEY_SECRET_NAME)

        assert license_value == _SESSION_JWT
        assert api_key_value == _SESSION_JWT

    @pytest.mark.asyncio
    async def test_an_answer_with_no_value_gives_the_worker_none(
        self, pair: _Pair, serving: Engine, artist: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(LICENSE_SECRET_NAME, "the serving engine's license")
        monkeypatch.setenv(API_KEY_SECRET_NAME, "the serving engine's api key")
        artist.event_manager.set_remote_worker_secret_policy(
            _answering(
                {LICENSE_SECRET_NAME: RemoteWorkerSecretAnswer(), API_KEY_SECRET_NAME: RemoteWorkerSecretAnswer()}
            )
        )
        _carry(serving, pair.serving, artist)
        side = _start_worker(pair.worker)

        license_value = await asyncio.to_thread(side.read_secret, LICENSE_SECRET_NAME)
        api_key_value = await asyncio.to_thread(side.read_secret, API_KEY_SECRET_NAME)

        assert license_value is None
        assert api_key_value is None

    @pytest.mark.asyncio
    async def test_an_answer_from_the_serving_engine_gives_the_worker_the_serving_engines_value(
        self, pair: _Pair, serving: Engine, artist: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(LICENSE_SECRET_NAME, "the serving engine's license")
        monkeypatch.setenv(API_KEY_SECRET_NAME, "the serving engine's api key")
        artist.event_manager.set_remote_worker_secret_policy(
            _answering(
                {
                    LICENSE_SECRET_NAME: RemoteWorkerSecretAnswer(),
                    API_KEY_SECRET_NAME: RemoteWorkerSecretAnswer(from_serving_engine=True),
                }
            )
        )
        _carry(serving, pair.serving, artist)
        side = _start_worker(pair.worker)

        license_value = await asyncio.to_thread(side.read_secret, LICENSE_SECRET_NAME)
        api_key_value = await asyncio.to_thread(side.read_secret, API_KEY_SECRET_NAME)

        assert license_value is None
        assert api_key_value == "the serving engine's api key"

    @pytest.mark.asyncio
    async def test_events_reach_the_artist_in_order_one_batch_at_a_time(self, pair: _Pair, serving: Engine) -> None:
        batches: list[list[int]] = []
        first_batch_held = asyncio.Event()
        release_first_batch = asyncio.Event()

        async def deliver(_engine_id: str, request: RequestPayload, **_kwargs: Any) -> ResultPayload:
            assert isinstance(request, worker_events.DeliverWorkerEventsRequest)
            batches.append([body["payload"]["payload"]["value"] for body in request.events])
            if len(batches) == 1:
                first_batch_held.set()
                await release_first_batch.wait()
            return worker_events.DeliverWorkerEventsResultSuccess(result_details="ok")

        end = RemoteWorkerEnd(
            serving,
            worker_engine_id=_WORKER,
            artist_engine_id=_ARTIST,
            request_timeout_s=_TIMEOUT_S,
            on_hello=MagicMock(),
            on_closed=lambda: None,
        )
        serving.request = deliver  # type: ignore[method-assign]
        end.attach(pair.serving)
        _start_worker_end(pair.worker, lambda _body: {})

        later = (1, 2, 3)
        pair.worker.notify(_value_event(0))
        await asyncio.wait_for(first_batch_held.wait(), _TIMEOUT_S)
        for value in later:
            pair.worker.notify(_value_event(value))
        await _until(lambda: end._require_events()._added == 1 + len(later))
        release_first_batch.set()
        await asyncio.wait_for(end.flush_events(), _TIMEOUT_S)

        assert batches == [[0], [1, 2, 3]]


class TestEventBatches:
    @pytest.mark.asyncio
    async def test_a_batch_that_fails_unexpectedly_is_dropped_and_the_next_still_goes(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        sent: list[list[int]] = []
        first_failing = asyncio.Event()
        release_failure = asyncio.Event()

        async def send(batch: list[dict]) -> None:
            if not sent and not first_failing.is_set():
                first_failing.set()
                await release_failure.wait()
                msg = "the link broke mid-send"
                raise RuntimeError(msg)
            sent.append([body["value"] for body in batch])

        batches = _EventBatches(send, asyncio.get_running_loop())
        batches.add({"value": 0})
        await asyncio.wait_for(first_failing.wait(), _TIMEOUT_S)
        batches.add({"value": 1})
        release_failure.set()

        await asyncio.wait_for(batches.flush(), _TIMEOUT_S)

        assert sent == [[1]]
        assert "the link broke mid-send" in caplog.text


class TestARefusedRemoteWorker:
    @pytest.mark.asyncio
    async def test_the_artist_hears_why_the_serving_engine_refused_its_worker(
        self, serving: Engine, artist: Engine
    ) -> None:
        serving.request = _link(artist, sender_engine_id=_SERVING)  # type: ignore[method-assign]
        remote = _served()
        serving.worker_manager._workers[_WORKER] = remote
        proc = MagicMock(pid=4242, returncode=1)
        proc.wait = AsyncMock(return_value=1)
        remote.process = proc
        # A refused worker exits on its own, so its process is still this engine's when it does.
        serving.worker_manager._managed_worker_processes[remote.worker_key] = proc

        reply = await serving.worker_manager._accept_served_hello(
            _WORKER, WorkerHello(engine_version="0.0.1", library_names=[_LIBRARY], loaded=True)
        )
        await serving.worker_manager._report_when_exited(_WORKER, remote, proc)

        assert reply["accepted"] is False
        reason = artist.worker_manager.worker_unavailable_reason(_LIBRARY)
        assert reason is not None
        assert reason.startswith(f"the worker process that runs it was refused by {_SERVING}: ")
        assert "0.0.1" in reason

    @pytest.mark.asyncio
    async def test_a_refused_worker_is_cut_off_and_ended_even_if_its_process_stays_up(
        self, pair: _Pair, serving: Engine, artist: Engine
    ) -> None:
        serving.request = _link(artist, sender_engine_id=_SERVING)  # type: ignore[method-assign]
        manager = serving.worker_manager
        remote = _served()
        manager._workers[_WORKER] = remote
        proc = MagicMock(pid=4242, returncode=None)
        remote.process = proc
        manager._managed_worker_processes[remote.worker_key] = proc
        terminate = AsyncMock()
        manager._terminate_via_spawn_loop = terminate  # type: ignore[method-assign]
        end = RemoteWorkerEnd(
            serving,
            worker_engine_id=_WORKER,
            artist_engine_id=_ARTIST,
            request_timeout_s=_TIMEOUT_S,
            on_hello=functools.partial(manager._accept_served_hello, _WORKER),
            on_closed=functools.partial(manager._start_end_served_worker, remote),
        )
        end.attach(pair.serving)
        closed = threading.Event()
        pair.worker.start(on_request=AsyncMock(), on_notification=lambda _body: None, on_closed=closed.set)

        reply = await asyncio.to_thread(
            pair.worker.request,
            event_body(WORKER_HELLO, WorkerHello(engine_version="0.0.1", library_names=[_LIBRARY], loaded=True)),
            _TIMEOUT_S,
        )

        assert reply["accepted"] is False
        assert await asyncio.to_thread(closed.wait, _TIMEOUT_S)
        await asyncio.wait_for(asyncio.gather(*manager._exit_watchers), _TIMEOUT_S)
        terminate.assert_awaited_once_with(remote.worker_key, proc)
        assert remote.worker_key not in manager._managed_worker_processes


class TestTheServingEngineRunsANode:
    @pytest.mark.asyncio
    async def test_a_node_runs_while_the_lease_holds_and_its_events_reach_the_artist_before_its_result(
        self, pair: _Pair, serving: Engine
    ) -> None:
        delivered: list[int] = []

        async def deliver(_engine_id: str, request: RequestPayload, **_kwargs: Any) -> ResultPayload:
            assert isinstance(request, worker_events.DeliverWorkerEventsRequest)
            # Yield first, so a result sent without awaiting the flush gets ahead of the events.
            await asyncio.sleep(0.05)
            delivered.extend(body["payload"]["payload"]["value"] for body in request.events)
            return worker_events.DeliverWorkerEventsResultSuccess(result_details="ok")

        def run_node(_body: dict) -> dict:
            for value in range(3):
                # Written now, on the I/O loop this handler runs on, as the worker's own sends are.
                pair.worker.channel.notify(_value_event(value))
            return {"result_type": "ExecuteNodeResultSuccess", "result": {"result_details": "ran"}}

        _host(serving, pair.serving, request=deliver)
        _hold_lease(serving)
        _start_worker_end(pair.worker, run_node)

        result = await _from_the_artist(serving, _execute_node())

        assert isinstance(result, worker_events.ExecuteNodeOnRemoteWorkerResultSuccess)
        assert result.result["result"]["result_details"] == "ran"
        assert delivered == [0, 1, 2]

    @pytest.mark.asyncio
    async def test_something_other_than_a_node_under_a_valid_lease_is_refused_and_never_reaches_the_worker(
        self, pair: _Pair, serving: Engine
    ) -> None:
        received: list[dict] = []

        def answer(body: dict) -> dict:
            received.append(body)
            return {}

        _host(serving, pair.serving)
        _hold_lease(serving)
        _start_worker_end(pair.worker, answer)
        smuggled = event_request_envelope(EventRequest(request=RunArbitraryPythonStringRequest(python_string="1")))

        result = await _from_the_artist(
            serving,
            worker_events.ExecuteNodeOnRemoteWorkerRequest(
                worker_engine_id=_WORKER, lease_id=_LEASE, execute_node=smuggled
            ),
        )

        assert isinstance(result, worker_events.ExecuteNodeOnRemoteWorkerResultFailure)
        assert "RunArbitraryPythonStringRequest" in str(result.result_details)
        assert received == []

    @pytest.mark.asyncio
    async def test_a_node_sent_under_a_lease_that_does_not_hold_the_engine_does_not_run(
        self, pair: _Pair, serving: Engine
    ) -> None:
        ran = threading.Event()

        def run_node(_body: dict) -> dict:
            ran.set()
            return {}

        _host(serving, pair.serving)
        _hold_lease(serving, held=False)
        _start_worker_end(pair.worker, run_node)

        result = await _from_the_artist(serving, _execute_node())

        assert isinstance(result, worker_events.ExecuteNodeOnRemoteWorkerResultFailure)
        assert "not holding itself" in str(result.result_details)
        assert not result.worker_gone
        assert not ran.is_set()

    @pytest.mark.asyncio
    async def test_another_engines_lease_does_not_run_this_engines_node(self, pair: _Pair, serving: Engine) -> None:
        _host(serving, pair.serving)
        _hold_lease(serving, engine_id="some-other-engine")

        result = await _from_the_artist(serving, _execute_node())

        assert isinstance(result, worker_events.ExecuteNodeOnRemoteWorkerResultFailure)

    @pytest.mark.asyncio
    async def test_a_node_for_a_worker_this_engine_does_not_run_says_it_is_gone(self, serving: Engine) -> None:
        result = await _from_the_artist(serving, _execute_node())

        assert isinstance(result, worker_events.ExecuteNodeOnRemoteWorkerResultFailure)
        assert result.worker_gone

    @pytest.mark.asyncio
    async def test_a_worker_that_goes_away_mid_node_says_it_is_gone(self, pair: _Pair, serving: Engine) -> None:
        def die(_body: dict) -> dict:
            pair.worker.channel.close()
            return {}

        _host(serving, pair.serving)
        _hold_lease(serving)
        _start_worker_end(pair.worker, die)

        result = await _from_the_artist(serving, _execute_node())

        assert isinstance(result, worker_events.ExecuteNodeOnRemoteWorkerResultFailure)
        assert result.worker_gone

    @pytest.mark.asyncio
    async def test_a_request_the_artist_passes_on_reaches_the_worker_and_its_answer_comes_back(
        self, pair: _Pair, serving: Engine
    ) -> None:
        received: list[dict] = []

        def answer(body: dict) -> dict:
            received.append(body)
            return {"result_type": "RemoteProbeResultSuccess", "result": {"answered_by": "worker"}}

        _host(serving, pair.serving)
        _start_worker_end(pair.worker, answer)
        envelope = event_request_envelope(EventRequest(request=CancelExecuteNodeRequest(target_request_id="r1")))

        result = await _from_the_artist(
            serving,
            worker_events.SendToRemoteWorkerRequest(worker_engine_id=_WORKER, request=envelope, await_reply=True),
        )

        assert isinstance(result, worker_events.SendToRemoteWorkerResultSuccess)
        assert result.result == {"result_type": "RemoteProbeResultSuccess", "result": {"answered_by": "worker"}}
        [body] = received
        assert body["payload"]["request_type"] == "CancelExecuteNodeRequest"

    @pytest.mark.asyncio
    async def test_a_node_passed_on_without_its_lease_is_refused_and_never_reaches_the_worker(
        self, pair: _Pair, serving: Engine
    ) -> None:
        received: list[dict] = []

        def answer(body: dict) -> dict:
            received.append(body)
            return {"result_type": "ExecuteNodeResultSuccess", "result": {"result_details": "ran"}}

        _host(serving, pair.serving)
        _start_worker_end(pair.worker, answer)

        result = await _from_the_artist(
            serving,
            worker_events.SendToRemoteWorkerRequest(
                worker_engine_id=_WORKER, request=_execute_node().execute_node, await_reply=True
            ),
        )

        assert isinstance(result, worker_events.SendToRemoteWorkerResultFailure)
        assert "only ActivateProjectRequest" in str(result.result_details)
        assert received == []

    @pytest.mark.asyncio
    async def test_a_request_outside_what_the_artist_sends_this_way_is_refused(
        self, pair: _Pair, serving: Engine
    ) -> None:
        received: list[dict] = []

        def answer(body: dict) -> dict:
            received.append(body)
            return {}

        _host(serving, pair.serving)
        _start_worker_end(pair.worker, answer)
        envelope = event_request_envelope(EventRequest(request=RunArbitraryPythonStringRequest(python_string="1")))

        result = await _from_the_artist(
            serving,
            worker_events.SendToRemoteWorkerRequest(worker_engine_id=_WORKER, request=envelope, await_reply=True),
        )

        assert isinstance(result, worker_events.SendToRemoteWorkerResultFailure)
        assert "RunArbitraryPythonStringRequest" in str(result.result_details)
        assert received == []

    @pytest.mark.asyncio
    async def test_only_the_engine_the_worker_runs_for_may_send_it_anything(self, pair: _Pair, serving: Engine) -> None:
        _host(serving, pair.serving)
        _hold_lease(serving)
        envelope = event_request_envelope(EventRequest(request=CancelExecuteNodeRequest(target_request_id="r1")))

        node = await _from_the_artist(serving, _execute_node(), sent_by="some-other-engine")
        passed = await _from_the_artist(
            serving,
            worker_events.SendToRemoteWorkerRequest(worker_engine_id=_WORKER, request=envelope),
            sent_by="some-other-engine",
        )

        assert not node.succeeded()
        assert not passed.succeeded()


class TestTheArtistsEngineRoutesToTheWorker:
    @pytest.mark.asyncio
    async def test_a_node_goes_to_the_serving_engine_with_its_lease_and_its_result_comes_back(
        self, artist: Engine
    ) -> None:
        sent: list[tuple[str, RequestPayload]] = []

        async def link(engine_id: str, request: RequestPayload, **_kwargs: Any) -> ResultPayload:
            sent.append((engine_id, request))
            return worker_events.ExecuteNodeOnRemoteWorkerResultSuccess(
                result={"result_type": "ExecuteNodeResultSuccess", "result": {}}, result_details="ok"
            )

        artist.request = link  # type: ignore[method-assign]
        node = EventRequest(request=_execute_node_request(), request_id="node-request")

        raw = await artist.worker_manager.route_to_worker(node, _WORKER, lease_id=_LEASE)

        assert raw == {"result_type": "ExecuteNodeResultSuccess", "result": {}}
        [(engine_id, request)] = sent
        assert engine_id == _SERVING
        assert isinstance(request, worker_events.ExecuteNodeOnRemoteWorkerRequest)
        assert request.lease_id == _LEASE
        assert request.worker_engine_id == _WORKER
        assert request.execute_node["payload"]["request_type"] == "ExecuteNodeRequest"
        assert request.execute_node["payload"]["request_id"] == "node-request"

    @pytest.mark.asyncio
    async def test_a_node_waiting_on_a_worker_that_goes_away_fails_with_why(self, artist: Engine) -> None:
        answered = asyncio.Event()

        async def never(*_args: Any, **_kwargs: Any) -> ResultPayload:
            await answered.wait()
            msg = "unreachable"
            raise AssertionError(msg)

        artist.request = never  # type: ignore[method-assign]
        node = EventRequest(request=_execute_node_request())
        routing = asyncio.create_task(artist.worker_manager.route_to_worker(node, _WORKER, lease_id=_LEASE))
        await asyncio.sleep(0)

        await artist.worker_manager.on_engine_lost(_SERVING)

        with pytest.raises(worker_events.WorkerGoneError, match=r"connection to gpu-01.*Reload your libraries"):
            await asyncio.wait_for(routing, _TIMEOUT_S)

    @pytest.mark.asyncio
    async def test_a_worker_that_stops_mid_node_names_the_computer_running_it(self, artist: Engine) -> None:
        async def stopped(*_args: Any, **_kwargs: Any) -> ResultPayload:
            return worker_events.ExecuteNodeOnRemoteWorkerResultFailure(
                result_details="the worker process running it stopped.", worker_gone=True
            )

        artist.request = stopped  # type: ignore[method-assign]
        node = EventRequest(request=_execute_node_request())

        with pytest.raises(worker_events.WorkerGoneError) as gone:
            await artist.worker_manager.route_to_worker(node, _WORKER, lease_id=_LEASE)

        assert str(gone.value) == "gpu-01, the computer running this library's nodes, stopped running them."

    @pytest.mark.asyncio
    async def test_the_workers_events_are_acted_on_as_a_local_workers_are(self, artist: Engine) -> None:
        queued: list[Any] = []
        artist.event_manager.put_event = queued.append  # type: ignore[method-assign]

        result = await _from_the_serving_engine(
            artist,
            worker_events.DeliverWorkerEventsRequest(
                worker_engine_id=_WORKER, events=[_value_event(0), {"type": "RunAnything"}, _value_event(1)]
            ),
        )

        assert result.succeeded()
        assert [event.wrapped_event.payload.value for event in queued] == [0, 1]
        assert all(isinstance(event, ExecutionGriptapeNodeEvent) for event in queued)

    @pytest.mark.asyncio
    async def test_events_from_an_engine_that_does_not_run_the_worker_are_refused(self, artist: Engine) -> None:
        queued: list[Any] = []
        artist.event_manager.put_event = queued.append  # type: ignore[method-assign]

        result = await _from_the_serving_engine(
            artist,
            worker_events.DeliverWorkerEventsRequest(worker_engine_id=_WORKER, events=[_value_event(0)]),
            sent_by="some-other-engine",
        )

        assert not result.succeeded()
        assert queued == []

    @pytest.mark.asyncio
    async def test_a_worker_reported_ready_is_taken_into_service_on_its_engine(self) -> None:
        artist = Engine()
        artist.worker_manager._starts_asked_of_hosts[_WORKER] = _remote_start()
        artist.request = _record_requests()  # type: ignore[method-assign]

        result = await _from_the_serving_engine(
            artist,
            worker_events.ReportRemoteWorkerReadyRequest(
                worker_engine_id=_WORKER,
                libraries=[worker_events.LibraryLoadReport(library_name=_LIBRARY, fitness="GOOD")],
            ),
        )

        assert isinstance(result, worker_events.ReportRemoteWorkerReadyResultSuccess)
        registration = artist.worker_manager._workers[_WORKER]
        assert registration.host_engine_id == _SERVING
        assert registration.worker_key == _LIBRARY
        assert _WORKER not in artist.worker_manager._starts_asked_of_hosts

    @pytest.mark.asyncio
    async def test_a_worker_this_engine_did_not_ask_for_is_refused(self) -> None:
        artist = Engine()

        result = await _from_the_serving_engine(
            artist, worker_events.ReportRemoteWorkerReadyRequest(worker_engine_id=_WORKER)
        )

        assert not result.succeeded()
        assert _WORKER not in artist.worker_manager._workers


def _remote_start() -> _StartAskedOfHost:
    return _StartAskedOfHost(host_engine_id=_SERVING, library_name=_LIBRARY)


def _record_requests() -> Callable[..., Awaitable[ResultPayload]]:
    async def request(_engine_id: str, request: RequestPayload, **_kwargs: Any) -> ResultPayload:
        return worker_events.SendToRemoteWorkerResultSuccess(result_details=f"took {type(request).__name__}")

    return request


def _value_event(value: int) -> dict:
    event = ExecutionEvent(
        payload=ParameterValueUpdateEvent(node_name="node", parameter_name="out", data_type="int", value=value),
        workflow_id="workflow",
    )
    return event_body(EXECUTION_EVENT, event)


def _execute_node_request() -> ExecuteNodeRequest:
    return ExecuteNodeRequest(node_name="node")


def _execute_node() -> worker_events.ExecuteNodeOnRemoteWorkerRequest:
    envelope = event_request_envelope(EventRequest(request=_execute_node_request()))
    return worker_events.ExecuteNodeOnRemoteWorkerRequest(
        worker_engine_id=_WORKER, lease_id=_LEASE, execute_node=envelope
    )


def _host(
    serving: Engine, channel: AsyncChannel, *, request: Callable[..., Awaitable[ResultPayload]] | None = None
) -> WorkerRegistration:
    """Record `channel` as the serving engine's connected worker for `_ARTIST`."""
    if request is not None:
        serving.request = request  # type: ignore[method-assign]
    remote = _served()
    served = cast("_ServedFor", remote.serves)
    end = RemoteWorkerEnd(
        serving,
        worker_engine_id=_WORKER,
        artist_engine_id=_ARTIST,
        request_timeout_s=_TIMEOUT_S,
        on_hello=MagicMock(),
        on_closed=lambda: None,
    )
    end.attach(channel)
    remote.channel = channel
    served.end = end
    served.started.set()
    serving.worker_manager._workers[_WORKER] = remote
    return remote


def _served() -> WorkerRegistration:
    """A worker the serving engine runs for `_ARTIST`'s session, not yet recorded."""
    return WorkerRegistration(
        request_topic="",
        worker_key=f"session/{_LIBRARY}",
        serves=_ServedFor(owning_engine_id=_ARTIST, session_id="session", library_name=_LIBRARY),
    )


def _hold_lease(serving: Engine, *, held: bool = True, engine_id: str = _ARTIST) -> None:
    granted: asyncio.Future[str | None] = asyncio.get_running_loop().create_future()
    if held:
        granted.set_result(None)
    engine_run = _EngineRun(engine_id=engine_id, lease_id=_LEASE, run_id=None, granted=granted)
    serving.worker_manager._engine_runs[engine_run.key] = engine_run


async def _from_the_artist(serving: Engine, request: RequestPayload, *, sent_by: str = _ARTIST) -> Any:
    """Dispatch `request` on the serving engine as `sent_by` sends it."""
    result_event = await serving.event_manager.ahandle_request(
        request,
        result_context={
            "request_id": "r1",
            "response_topic": f"engines/{sent_by}/response",
            "sender": RequestSender(engine_id=sent_by),
        },
    )
    return result_event.result


async def _until(predicate: Callable[[], bool]) -> None:
    """Wait for state that a callback scheduled from the channel's own thread writes."""
    async with asyncio.timeout(_TIMEOUT_S):
        while not predicate():  # noqa: ASYNC110 (nothing here to set an Event from)
            await asyncio.sleep(0.01)
