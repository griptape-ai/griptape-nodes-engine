"""A worker one engine runs for another: the serving engine carries it, and the artist's engine decides everything it asks for.

The artist's engine (`artist`) and the serving engine (`serving`) are real engines in this process. The
link between them is modeled by handing a request from one straight to the other's dispatch, with the
sender and envelope fields the app's transport sets. The worker is played by the other end of a real
channel. These are the escalation rows for a hosted worker:

- a request the worker sends reaches the artist's engine as its own type, and the artist's gate decides it
- the worker's checkpoints are decided by the artist's authorization hooks
- the serving engine applies no gate or hook of its own to the worker's traffic
- the worker's Griptape Cloud credential is the artist's license session, never a license or API key,
  and with no session it gets none
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, NamedTuple
from unittest.mock import MagicMock

import pytest

from griptape_nodes.drivers.cloud_credentials import API_KEY_SECRET_NAME, LICENSE_SECRET_NAME
from griptape_nodes.retained_mode.engine import Engine
from griptape_nodes.retained_mode.engine_transports import EngineNotFoundError
from griptape_nodes.retained_mode.events import worker_events
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
from griptape_nodes.retained_mode.events.execution_events import ExecuteNodeRequest, ParameterValueUpdateEvent
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
    SessionCredential,
    WorkerRequestGateDecision,
    read_forwarded_result,
)
from griptape_nodes.retained_mode.managers.worker_manager import (
    WorkerRegistration,
    _EngineRun,
    _HostedWorker,
    _RemoteStart,
)
from griptape_nodes.worker.channel import Channel, ChannelListener, connect_from_environment
from griptape_nodes.worker.hosted import HostedWorkerEnd
from griptape_nodes.worker.runtime import WorkerSide
from griptape_nodes.worker.wire import EXECUTION_EVENT, event_body, event_request_envelope

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator

# Ceilings for a loaded CI box; every wait is on a reply or an event, not on time passing.
_TIMEOUT_S = 10.0
_ARTIST = "artist-workstation"
_SERVING = "gpu-01"
_WORKER = "hosted-worker-1"
_LIBRARY = "Exec Library"
_LEASE = "lease-1"
_SESSION_JWT = "session-jwt-of-the-artist"  # a stand-in, not a secret
_FORBIDDEN_NODE_TYPE = "ForbiddenNode"


@dataclass
@PayloadRegistry.register
class HostedProbeRequest(RequestPayload):
    """A request whose answer names the engine that handled it."""


@dataclass
@PayloadRegistry.register
class HostedProbeResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    answered_by: str


class _Pair(NamedTuple):
    serving: Channel
    worker: Channel


@pytest.fixture
def pair() -> Iterator[_Pair]:
    """The serving engine's end of the worker's channel and the worker's own, neither started."""
    listener = ChannelListener()
    accepted: list[Channel] = []
    thread = threading.Thread(target=lambda: accepted.append(listener.accept(_TIMEOUT_S)))
    thread.start()
    worker = connect_from_environment(listener.child_environment())
    thread.join(_TIMEOUT_S)
    listener.close()
    yield _Pair(serving=accepted[0], worker=worker)
    accepted[0].close()
    worker.close()


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
    msg = "The serving engine's gate must not screen a hosted worker's request."
    raise AssertionError(msg)


def _deny_on_the_serving_engine(_checkpoint: AuthorizationCheckpoint) -> CheckpointDenial | None:
    msg = "The serving engine's hooks must not decide a hosted worker's checkpoint."
    raise AssertionError(msg)


def _answer_as(name: str) -> Callable[[HostedProbeRequest], HostedProbeResultSuccess]:
    def handle(request: HostedProbeRequest) -> HostedProbeResultSuccess:  # noqa: ARG001
        return HostedProbeResultSuccess(answered_by=name, result_details=f"Answered by {name}.")

    return handle


def _checkpoint(subject_id: str) -> AuthorizationCheckpoint:
    return AuthorizationCheckpoint(
        action=CheckpointAction.INSTANTIATE_NODE, subject_type=CheckpointSubjectType.NODE_TYPE, subject_id=subject_id
    )


def _deny_forbidden(checkpoint: AuthorizationCheckpoint) -> CheckpointDenial | None:
    if checkpoint.subject_id == _FORBIDDEN_NODE_TYPE:
        return CheckpointDenial(failures=(CheckpointFailure(detail="Your license does not include this node."),))
    return None


def _session(token: str = _SESSION_JWT) -> Callable[[], SessionCredential | None]:
    return lambda: SessionCredential(token=token, expires_at=datetime(2030, 1, 1, tzinfo=UTC))


def _link(to: Engine, *, sender_engine_id: str) -> Callable[..., Awaitable[ResultPayload]]:
    """`Engine.request` from `sender_engine_id` to `to`, as the app's transport delivers it."""

    async def request(
        _engine_id: str,
        request: RequestPayload,
        *,
        timeout_s: float | None,  # noqa: ARG001
        open_workflow_id: str | None = None,
        worker_engine_id: str | None = None,
    ) -> ResultPayload:
        result_event = await to.event_manager.ahandle_request(
            request,
            result_context={
                "request_id": uuid.uuid4().hex,
                "response_topic": f"engines/{sender_engine_id}/response",
                "sender": RequestSender(engine_id=sender_engine_id),
                "open_workflow_id": open_workflow_id,
                "worker_engine_id": worker_engine_id,
            },
        )
        return result_event.result

    return request


def _carry(serving: Engine, channel: Channel, artist: Engine) -> HostedWorkerEnd:
    """Have `serving` carry the worker on `channel` to `artist`, over a modeled link."""

    async def unexpected(*_args: Any) -> Any:
        msg = "Not part of this test."
        raise AssertionError(msg)

    serving.request = _link(artist, sender_engine_id=_SERVING)  # type: ignore[method-assign]
    end = HostedWorkerEnd(
        serving,
        worker_engine_id=_WORKER,
        artist_engine_id=_ARTIST,
        request_timeout_s=_TIMEOUT_S,
        on_hello=unexpected,
        on_closed=lambda: None,
    )
    end.attach(channel, asyncio.get_running_loop())
    return end


def _start_worker(channel: Channel) -> WorkerSide:
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
            "worker_engine_id": _WORKER,
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

        artist.event_manager.assign_manager_to_request_type(HostedProbeRequest, _answer_as("artist"))
        artist.event_manager.add_worker_request_gate(allow)

        result = await _from_the_worker(artist, HostedProbeRequest())

        assert isinstance(result, HostedProbeResultSuccess)
        assert result.answered_by == "artist"
        [envelope] = seen
        assert envelope["type"] == "EventRequest"
        assert envelope["payload"]["request_type"] == "HostedProbeRequest"

    @pytest.mark.asyncio
    async def test_a_request_the_artists_gate_denies_never_runs(self, artist: Engine) -> None:
        handled: list[RequestPayload] = []

        def handle(request: HostedProbeRequest) -> HostedProbeResultSuccess:
            handled.append(request)
            return HostedProbeResultSuccess(answered_by="artist", result_details="ok")

        artist.event_manager.assign_manager_to_request_type(HostedProbeRequest, handle)
        artist.event_manager.add_worker_request_gate(
            lambda _envelope: WorkerRequestGateDecision(forward=None, denials=["not on this license"])
        )

        result = await _from_the_worker(artist, HostedProbeRequest())

        assert handled == []
        assert isinstance(result, GenericResultFailure)
        assert "license policy refused it" in str(result.result_details)

    @pytest.mark.asyncio
    async def test_a_request_relayed_by_an_engine_that_does_not_run_the_worker_is_refused(self, artist: Engine) -> None:
        gate = MagicMock(side_effect=AssertionError("must not run"))
        artist.event_manager.assign_manager_to_request_type(HostedProbeRequest, _answer_as("artist"))
        artist.event_manager.add_worker_request_gate(gate)

        result = await _from_the_worker(artist, HostedProbeRequest(), sent_by="some-other-engine")

        assert isinstance(result, GenericResultFailure)
        assert _WORKER in str(result.result_details)
        gate.assert_not_called()

    def test_a_synchronous_dispatch_never_admits_a_workers_request(self, artist: Engine) -> None:
        artist.event_manager.assign_manager_to_request_type(HostedProbeRequest, _answer_as("artist"))

        result_event = artist.event_manager.handle_request(
            HostedProbeRequest(),
            result_context={"sender": RequestSender(engine_id=_SERVING), "worker_engine_id": _WORKER},
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


class TestTheWorkersCloudCredential:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("secret_name", [LICENSE_SECRET_NAME, API_KEY_SECRET_NAME])
    async def test_is_the_artists_session_never_a_license_or_api_key(
        self, artist: Engine, secret_name: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(LICENSE_SECRET_NAME, "license-jwt")
        monkeypatch.setenv(API_KEY_SECRET_NAME, "api-key")
        artist.event_manager.set_session_credential_provider(_session())

        result = await _from_the_worker(artist, GetSecretValueRequest(key=secret_name))

        assert isinstance(result, GetSecretValueResultSuccess)
        assert result.value == _SESSION_JWT

    @pytest.mark.asyncio
    async def test_with_no_session_provider_there_is_none(
        self, artist: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(LICENSE_SECRET_NAME, "license-jwt")

        result = await _from_the_worker(
            artist, GetSecretValueRequest(key=LICENSE_SECRET_NAME, should_error_on_not_found=False)
        )

        assert isinstance(result, GetSecretValueResultSuccess)
        assert result.value is None

    @pytest.mark.asyncio
    async def test_a_provider_that_raises_gives_none(self, artist: Engine) -> None:
        def broken() -> SessionCredential | None:
            msg = "no session"
            raise RuntimeError(msg)

        artist.event_manager.set_session_credential_provider(broken)

        result = await _from_the_worker(
            artist, GetSecretValueRequest(key=LICENSE_SECRET_NAME, should_error_on_not_found=False)
        )

        assert isinstance(result, GetSecretValueResultSuccess)
        assert result.value is None

    @pytest.mark.asyncio
    async def test_any_other_secret_is_the_artists_own(self, artist: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GTN_TEST_HOSTED_SECRET", "the artist's value")

        result = await _from_the_worker(artist, GetSecretValueRequest(key="GTN_TEST_HOSTED_SECRET"))

        assert isinstance(result, GetSecretValueResultSuccess)
        assert result.value == "the artist's value"

    def test_the_session_credential_is_kept_out_of_its_repr(self) -> None:
        credential = _session()()

        assert _SESSION_JWT not in repr(credential)


class TestTheServingEngineCarriesTheWorker:
    @pytest.mark.asyncio
    async def test_a_request_reaches_the_artist_as_itself_and_the_serving_engine_screens_nothing(
        self, pair: _Pair, serving: Engine, artist: Engine
    ) -> None:
        seen: list[str] = []

        def allow(envelope: dict) -> WorkerRequestGateDecision:
            seen.append(envelope["payload"]["request_type"])
            return WorkerRequestGateDecision(forward=envelope, denials=[])

        artist.event_manager.assign_manager_to_request_type(HostedProbeRequest, _answer_as("artist"))
        serving.event_manager.assign_manager_to_request_type(HostedProbeRequest, _answer_as("serving"))
        artist.event_manager.add_worker_request_gate(allow)
        _carry(serving, pair.serving, artist)
        _start_worker(pair.worker)

        request = HostedProbeRequest()
        reply = await asyncio.to_thread(
            pair.worker.request, event_request_envelope(EventRequest(request=request)), _TIMEOUT_S
        )

        result = read_forwarded_result(request, reply)
        assert isinstance(result, HostedProbeResultSuccess)
        assert result.answered_by == "artist"
        assert seen == ["HostedProbeRequest"]

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

        end = HostedWorkerEnd(
            serving,
            worker_engine_id=_WORKER,
            artist_engine_id=_ARTIST,
            request_timeout_s=_TIMEOUT_S,
            on_hello=MagicMock(),
            on_closed=lambda: None,
        )
        serving.request = lost  # type: ignore[method-assign]
        end.attach(pair.serving, asyncio.get_running_loop())
        side = _start_worker(pair.worker)

        denial = await asyncio.to_thread(side.authorize_with_parent, _checkpoint("AllowedNode"))

        assert denial is not None

    @pytest.mark.asyncio
    async def test_the_workers_cloud_credential_is_the_artists_session_and_never_the_serving_engines_license(
        self, pair: _Pair, serving: Engine, artist: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Both engines share this process's environment, so this is also what the serving engine would read.
        monkeypatch.setenv(LICENSE_SECRET_NAME, "the serving engine's license")
        artist.event_manager.set_session_credential_provider(_session())
        _carry(serving, pair.serving, artist)
        side = _start_worker(pair.worker)

        license_value = await asyncio.to_thread(side.read_secret, LICENSE_SECRET_NAME)
        api_key_value = await asyncio.to_thread(side.read_secret, API_KEY_SECRET_NAME)

        assert license_value == _SESSION_JWT
        assert api_key_value == _SESSION_JWT

    @pytest.mark.asyncio
    async def test_with_no_session_on_the_artists_engine_the_worker_gets_no_cloud_credential(
        self, pair: _Pair, serving: Engine, artist: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(LICENSE_SECRET_NAME, "the serving engine's license")
        _carry(serving, pair.serving, artist)
        side = _start_worker(pair.worker)

        value = await asyncio.to_thread(side.read_secret, LICENSE_SECRET_NAME)

        assert value is None

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

        end = HostedWorkerEnd(
            serving,
            worker_engine_id=_WORKER,
            artist_engine_id=_ARTIST,
            request_timeout_s=_TIMEOUT_S,
            on_hello=MagicMock(),
            on_closed=lambda: None,
        )
        serving.request = deliver  # type: ignore[method-assign]
        end.attach(pair.serving, asyncio.get_running_loop())
        pair.worker.start(on_request=lambda _body: {}, on_notification=lambda _body: None, on_closed=lambda: None)

        later = (1, 2, 3)
        pair.worker.notify(_value_event(0))
        await asyncio.wait_for(first_batch_held.wait(), _TIMEOUT_S)
        for value in later:
            pair.worker.notify(_value_event(value))
        await _until(lambda: end._require_events()._added == 1 + len(later))
        release_first_batch.set()
        await asyncio.wait_for(end.flush_events(), _TIMEOUT_S)

        assert batches == [[0], [1, 2, 3]]


class TestTheServingEngineRunsANode:
    @pytest.mark.asyncio
    async def test_a_node_runs_while_the_lease_holds_and_its_events_reach_the_artist_before_its_result(
        self, pair: _Pair, serving: Engine
    ) -> None:
        delivered: list[int] = []

        async def deliver(_engine_id: str, request: RequestPayload, **_kwargs: Any) -> ResultPayload:
            assert isinstance(request, worker_events.DeliverWorkerEventsRequest)
            delivered.extend(body["payload"]["payload"]["value"] for body in request.events)
            return worker_events.DeliverWorkerEventsResultSuccess(result_details="ok")

        def run_node(_body: dict) -> dict:
            for value in range(3):
                pair.worker.notify(_value_event(value))
            return {"result_type": "ExecuteNodeResultSuccess", "result": {"result_details": "ran"}}

        _host(serving, pair.serving, request=deliver)
        _hold_lease(serving)
        pair.worker.start(on_request=run_node, on_notification=lambda _body: None, on_closed=lambda: None)

        result = await _from_the_artist(serving, _execute_node())

        assert isinstance(result, worker_events.ExecuteNodeOnHostedWorkerResultSuccess)
        assert result.result["result"]["result_details"] == "ran"
        assert delivered == [0, 1, 2]

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
        pair.worker.start(on_request=run_node, on_notification=lambda _body: None, on_closed=lambda: None)

        result = await _from_the_artist(serving, _execute_node())

        assert isinstance(result, worker_events.ExecuteNodeOnHostedWorkerResultFailure)
        assert "not holding itself" in str(result.result_details)
        assert not result.worker_gone
        assert not ran.is_set()

    @pytest.mark.asyncio
    async def test_another_engines_lease_does_not_run_this_engines_node(self, pair: _Pair, serving: Engine) -> None:
        _host(serving, pair.serving)
        _hold_lease(serving, engine_id="some-other-engine")

        result = await _from_the_artist(serving, _execute_node())

        assert isinstance(result, worker_events.ExecuteNodeOnHostedWorkerResultFailure)

    @pytest.mark.asyncio
    async def test_a_node_for_a_worker_this_engine_does_not_run_says_it_is_gone(self, serving: Engine) -> None:
        result = await _from_the_artist(serving, _execute_node())

        assert isinstance(result, worker_events.ExecuteNodeOnHostedWorkerResultFailure)
        assert result.worker_gone

    @pytest.mark.asyncio
    async def test_a_worker_that_goes_away_mid_node_says_it_is_gone(self, pair: _Pair, serving: Engine) -> None:
        def die(_body: dict) -> dict:
            pair.worker.close()
            return {}

        _host(serving, pair.serving)
        _hold_lease(serving)
        pair.worker.start(on_request=die, on_notification=lambda _body: None, on_closed=lambda: None)

        result = await _from_the_artist(serving, _execute_node())

        assert isinstance(result, worker_events.ExecuteNodeOnHostedWorkerResultFailure)
        assert result.worker_gone

    @pytest.mark.asyncio
    async def test_a_request_the_artist_passes_on_reaches_the_worker_and_its_answer_comes_back(
        self, pair: _Pair, serving: Engine
    ) -> None:
        received: list[dict] = []

        def answer(body: dict) -> dict:
            received.append(body)
            return {"result_type": "HostedProbeResultSuccess", "result": {"answered_by": "worker"}}

        _host(serving, pair.serving)
        pair.worker.start(on_request=answer, on_notification=lambda _body: None, on_closed=lambda: None)
        envelope = event_request_envelope(EventRequest(request=HostedProbeRequest()))

        result = await _from_the_artist(
            serving,
            worker_events.SendToHostedWorkerRequest(worker_engine_id=_WORKER, request=envelope, await_reply=True),
        )

        assert isinstance(result, worker_events.SendToHostedWorkerResultSuccess)
        assert result.result == {"result_type": "HostedProbeResultSuccess", "result": {"answered_by": "worker"}}
        [body] = received
        assert body["payload"]["request_type"] == "HostedProbeRequest"

    @pytest.mark.asyncio
    async def test_only_the_engine_the_worker_runs_for_may_send_it_anything(self, pair: _Pair, serving: Engine) -> None:
        _host(serving, pair.serving)
        _hold_lease(serving)
        envelope = event_request_envelope(EventRequest(request=HostedProbeRequest()))

        node = await _from_the_artist(serving, _execute_node(), sent_by="some-other-engine")
        passed = await _from_the_artist(
            serving,
            worker_events.SendToHostedWorkerRequest(worker_engine_id=_WORKER, request=envelope),
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
            return worker_events.ExecuteNodeOnHostedWorkerResultSuccess(
                result={"result_type": "ExecuteNodeResultSuccess", "result": {}}, result_details="ok"
            )

        artist.request = link  # type: ignore[method-assign]
        node = EventRequest(request=_execute_node_request(), request_id="node-request")

        raw = await artist.worker_manager.route_to_worker(node, _WORKER, lease_id=_LEASE)

        assert raw == {"result_type": "ExecuteNodeResultSuccess", "result": {}}
        [(engine_id, request)] = sent
        assert engine_id == _SERVING
        assert isinstance(request, worker_events.ExecuteNodeOnHostedWorkerRequest)
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

        with pytest.raises(worker_events.WorkerGoneError, match="connection to gpu-01"):
            await asyncio.wait_for(routing, _TIMEOUT_S)

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
        artist.worker_manager._remote_starts[_WORKER] = _remote_start()
        artist.request = _record_requests()  # type: ignore[method-assign]

        result = await _from_the_serving_engine(
            artist,
            worker_events.ReportHostedWorkerReadyRequest(
                worker_engine_id=_WORKER,
                libraries=[worker_events.LibraryLoadReport(library_name=_LIBRARY, fitness="GOOD")],
            ),
        )

        assert isinstance(result, worker_events.ReportHostedWorkerReadyResultSuccess)
        registration = artist.worker_manager._workers[_WORKER]
        assert registration.host_engine_id == _SERVING
        assert registration.worker_key == _LIBRARY
        assert _WORKER not in artist.worker_manager._remote_starts

    @pytest.mark.asyncio
    async def test_a_worker_this_engine_did_not_ask_for_is_refused(self) -> None:
        artist = Engine()

        result = await _from_the_serving_engine(
            artist, worker_events.ReportHostedWorkerReadyRequest(worker_engine_id=_WORKER)
        )

        assert not result.succeeded()
        assert _WORKER not in artist.worker_manager._workers


def _remote_start() -> _RemoteStart:
    return _RemoteStart(host_engine_id=_SERVING, library_name=_LIBRARY)


def _record_requests() -> Callable[..., Awaitable[ResultPayload]]:
    async def request(_engine_id: str, request: RequestPayload, **_kwargs: Any) -> ResultPayload:
        return worker_events.SendToHostedWorkerResultSuccess(result_details=f"took {type(request).__name__}")

    return request


def _value_event(value: int) -> dict:
    event = ExecutionEvent(
        payload=ParameterValueUpdateEvent(node_name="node", parameter_name="out", data_type="int", value=value),
        workflow_id="workflow",
    )
    return event_body(EXECUTION_EVENT, event)


def _execute_node_request() -> ExecuteNodeRequest:
    return ExecuteNodeRequest(node_name="node")


def _execute_node() -> worker_events.ExecuteNodeOnHostedWorkerRequest:
    envelope = event_request_envelope(EventRequest(request=_execute_node_request()))
    return worker_events.ExecuteNodeOnHostedWorkerRequest(
        worker_engine_id=_WORKER, lease_id=_LEASE, execute_node=envelope
    )


def _host(
    serving: Engine, channel: Channel, *, request: Callable[..., Awaitable[ResultPayload]] | None = None
) -> _HostedWorker:
    """Record `channel` as the serving engine's connected worker for `_ARTIST`."""
    if request is not None:
        serving.request = request  # type: ignore[method-assign]
    hosted = _HostedWorker(
        worker_key=f"session/{_LIBRARY}", engine_id=_ARTIST, session_id="session", library_name=_LIBRARY
    )
    end = HostedWorkerEnd(
        serving,
        worker_engine_id=_WORKER,
        artist_engine_id=_ARTIST,
        request_timeout_s=_TIMEOUT_S,
        on_hello=MagicMock(),
        on_closed=lambda: None,
    )
    end.attach(channel, asyncio.get_running_loop())
    hosted.channel = channel
    hosted.end = end
    hosted.started.set()
    serving.worker_manager._hosted_workers[_WORKER] = hosted
    return hosted


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
