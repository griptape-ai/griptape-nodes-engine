"""A library's worker process: an engine that works for the engine that started it, over their channel.

The worker has no engine identity, session, transport, or license of its own. Everything it needs
answered or authorized goes to its parent:

- Requests node code sends while a node runs, through the `RemoteHandler`s every worker installs.
- Every authorization checkpoint, through a hook installed before any library loads. The hook denies
  whenever the parent cannot answer, so the worker never runs on the engine's allow-by-default.
- Every secret, so a credential its parent did not give it cannot be read from its own environment.

It exits when the channel closes: the parent stopped it, or the parent is gone.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import threading
from dataclasses import dataclass
from multiprocessing import AuthenticationError
from typing import TYPE_CHECKING, Any

from griptape_nodes.app.worker_routing import register_broadcast_handlers, register_remote_handlers
from griptape_nodes.common.node_executor import current_executing_node_name
from griptape_nodes.retained_mode.engine import WorkerProcess, build_root_engine
from griptape_nodes.retained_mode.events import converters
from griptape_nodes.retained_mode.events.app_events import (
    AppInitializationComplete,
    ReportGeneralPurposeWorkerReadyRequest,
    ReportLibraryLoadedRequest,
)
from griptape_nodes.retained_mode.events.base_events import (
    AppEvent,
    EventRequest,
    EventResultFailure,
    ExecutionEvent,
    ExecutionGriptapeNodeEvent,
    GriptapeNodeEvent,
    ProgressEvent,
)
from griptape_nodes.retained_mode.events.execution_events import GriptapeEvent
from griptape_nodes.retained_mode.events.logger_events import LogHandlerEvent
from griptape_nodes.retained_mode.events.project_events import ActivateWorkspaceProjectRequest
from griptape_nodes.retained_mode.events.secrets_events import GetSecretValueRequest, GetSecretValueResultSuccess
from griptape_nodes.retained_mode.managers.authorization_checkpoint import (
    AuthorizationCheckpoint,
    CheckpointDenial,
    CheckpointFailure,
)
from griptape_nodes.retained_mode.managers.event_manager import read_forwarded_result
from griptape_nodes.retained_mode.managers.project_manager import SYSTEM_DEFAULTS_KEY
from griptape_nodes.utils.version_utils import engine_version
from griptape_nodes.worker.channel import ChannelClosedError, connect_from_environment
from griptape_nodes.worker.wire import (
    APP_EVENT,
    AUTHORIZATION_CHECKPOINT,
    CHANNEL_REQUEST_TOPIC,
    EVENT_REQUEST,
    EVENT_RESULT,
    EXECUTION_EVENT,
    RELAYED_EXECUTION_PAYLOADS,
    WORKER_ENGINE_ID_ENV,
    WORKER_HELLO,
    LibraryLoadReport,
    WireFormatError,
    WorkerHello,
    answer_event_request,
    event_body,
    event_request_envelope,
    read_event_request,
    read_payload,
    request_failure,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from concurrent.futures import Future

    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.worker.channel import Channel

logger = logging.getLogger("griptape_nodes")

# How long the worker waits for its parent to answer a request, a checkpoint, or a secret.
PARENT_REQUEST_TIMEOUT_S = 30.0

# How long the worker waits for the project its parent sends as soon as it connects.
_PROJECT_SETTLE_TIMEOUT_S = 30.0

# Longest log message sent on to the editor, as the editor's own engine log handler cuts it.
_LOG_MESSAGE_MAX_LENGTH = 500

# The response topic a forwarded request names. The channel carries the reply, so nothing routes on it.
_CHANNEL_RESPONSE_TOPIC = "worker"


class WorkerStartupError(Exception):
    """The worker cannot run safely, and exits rather than starting."""


@dataclass
class _Flush:
    """Queued behind every event emitted so far; set once the event drain has sent them all."""

    sent: asyncio.Future[None]


class WorkerSide:
    """The worker's end of the channel: what it answers, and what it asks its parent.

    `on_parent_gone` is called once, from the channel's own thread, when the channel ends.
    """

    def __init__(self, channel: Channel, *, on_parent_gone: Callable[[], None]) -> None:
        self._channel = channel
        self._on_parent_gone = on_parent_gone
        self._engine: Engine | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        # Held until `arm`, which comes once the engine can handle them: the parent sends the project
        # as soon as the worker connects, and the worker reads secrets over the channel while its
        # engine is still being built.
        self._armed = threading.Event()
        self._held_lock = threading.Lock()
        self._held_notifications: list[dict[str, Any]] = []
        # Local dispatch of the app events this engine queues, held so the loop does not drop them.
        self._app_event_dispatches: set[asyncio.Task] = set()

    @property
    def channel(self) -> Channel:
        return self._channel

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        """Start the channel. `loop` is the engine loop this worker handles its parent's requests on."""
        self._loop = loop
        self._channel.start(
            on_request=self._on_request, on_notification=self._on_notification, on_closed=self._on_parent_gone
        )

    def arm(self, engine: Engine) -> None:
        """Begin answering the parent on `engine`, starting with whatever arrived while it was being built."""
        self._engine = engine
        with self._held_lock:
            for body in self._held_notifications:
                self._schedule_notification(body)
            self._held_notifications.clear()
            self._armed.set()

    def read_secret(self, name: str) -> str | None:
        """Read the secret `name` from the parent, or None if it has none by that name.

        Raises:
            RuntimeError: The parent could not be asked, or failed to answer.
        """
        event_request: EventRequest = EventRequest(
            request=GetSecretValueRequest(key=name, should_error_on_not_found=False)
        )
        try:
            reply = self._channel.request(event_request_envelope(event_request), timeout_s=PARENT_REQUEST_TIMEOUT_S)
        except ChannelClosedError as e:
            msg = f"Attempted to read the secret '{name}' from the engine this worker works for. Failed because {e}"
            raise RuntimeError(msg) from e
        except TimeoutError as e:
            msg = (
                f"Attempted to read the secret '{name}' from the engine this worker works for. Failed because it "
                f"did not answer within {PARENT_REQUEST_TIMEOUT_S:.0f} seconds."
            )
            raise RuntimeError(msg) from e
        result = read_forwarded_result(event_request.request, reply)
        if not isinstance(result, GetSecretValueResultSuccess):
            msg = (
                f"Attempted to read the secret '{name}' from the engine this worker works for. "
                f"Failed because {result.result_details}"
            )
            raise RuntimeError(msg)  # noqa: TRY004 (a failed answer, not a wrong type)
        return result.value

    def authorize_with_parent(self, checkpoint: AuthorizationCheckpoint) -> CheckpointDenial | None:
        """The authorization hook every worker runs: the parent decides, and anything short of its answer denies."""
        # Every failure denies, including ones nobody anticipated: allowing here is how a worker would
        # run outside its parent's license policy.
        try:
            body = event_body(AUTHORIZATION_CHECKPOINT, checkpoint)
            reply = self._channel.request(body, timeout_s=PARENT_REQUEST_TIMEOUT_S)
            return _read_checkpoint_reply(reply)
        except Exception as e:
            logger.error(
                "Denied the checkpoint '%s' on '%s' because the engine this worker works for could not decide it: %s",
                checkpoint.action,
                checkpoint.subject_id,
                e,
            )
            detail = (
                f"Attempted to check whether '{checkpoint.subject_id}' is permitted. Failed because the engine "
                f"that started this worker could not be asked: {e}"
            )
            return CheckpointDenial(failures=(CheckpointFailure(detail=detail),))

    async def answer_parent_request(self, event_request: EventRequest) -> dict[str, Any]:
        """Handle a request from the parent, and return its result once every event before it has been sent."""
        result = await answer_event_request(self._require_engine(), event_request)
        await self.flush_events()
        return result

    async def flush_events(self) -> None:
        """Wait until every event emitted before now has gone to the parent.

        Scheduled with `call_soon` so that events put from other threads ahead of this call, which
        reach the queue through the loop's own callbacks, are queued before the marker.
        """
        loop = asyncio.get_running_loop()
        sent: asyncio.Future[None] = loop.create_future()
        loop.call_soon(self._require_engine().event_manager.event_queue.put_nowait, _Flush(sent=sent))
        await sent

    async def drain_events(self) -> None:
        """Send the parent, in order, everything this engine would have sent the editor."""
        engine = self._require_engine()
        event_queue = engine.event_manager.event_queue
        while True:
            event = await event_queue.get()
            try:
                await self._send_event(engine, event)
            except ChannelClosedError:
                return
            finally:
                event_queue.task_done()

    async def _send_event(self, engine: Engine, event: Any) -> None:
        if isinstance(event, _Flush):
            event.sent.set_result(None)
            return
        if isinstance(event, AppEvent):
            # Raised here, so it is dispatched here, as the engine's host dispatches what it queues. In
            # a task of its own, so a slow listener holds up neither the events behind it nor a flush.
            dispatch = asyncio.create_task(engine.abroadcast_app_event(event.payload))
            self._app_event_dispatches.add(dispatch)
            dispatch.add_done_callback(self._app_event_dispatches.discard)
            self._notify(APP_EVENT, event)
            return
        if isinstance(event, ExecutionGriptapeNodeEvent):
            if isinstance(event.wrapped_event.payload, RELAYED_EXECUTION_PAYLOADS):
                self._notify(EXECUTION_EVENT, event.wrapped_event)
            return
        if isinstance(event, ProgressEvent):
            if event.node_name:
                payload = GriptapeEvent(
                    node_name=event.node_name,
                    parameter_name=event.parameter_name,
                    type=type(event).__name__,
                    value=event.value,
                )
                self._notify(EXECUTION_EVENT, ExecutionEvent(payload=payload, workflow_id=event.workflow_id))
            return
        if isinstance(event, GriptapeNodeEvent):
            self._notify(EVENT_RESULT, event.wrapped_event)
            return
        logger.warning("Not sending an event of unknown type %s to the engine this worker works for.", type(event))

    def _notify(self, body_type: str, event: Any) -> None:
        try:
            body = event_body(body_type, event)
        except converters.EventSerializationError as e:
            logger.error("Could not send a %s to the engine this worker works for: %s", type(event).__name__, e)
            return
        self._channel.notify(body)

    def _on_request(self, body: dict[str, Any]) -> dict[str, Any]:
        """Answer one request from the parent. Runs on a channel pool thread, so it may wait for the engine."""
        self._armed.wait()
        if body.get("type") != EVENT_REQUEST:
            return {"error": f"A library's worker process does not answer a '{body.get('type')}'."}
        try:
            event_request = read_event_request(body)
        except WireFormatError as e:
            return request_failure(
                body, f"Attempted to handle a request in a library's worker process. Failed because {e}."
            )
        return asyncio.run_coroutine_threadsafe(
            self.answer_parent_request(event_request), self._require_loop()
        ).result()

    def _on_notification(self, body: dict[str, Any]) -> None:
        with self._held_lock:
            if not self._armed.is_set():
                self._held_notifications.append(body)
                return
            self._schedule_notification(body)

    def _schedule_notification(self, body: dict[str, Any]) -> None:
        """Start handling a request the parent sends without waiting for its answer, such as a broadcast or a cancel."""
        if body.get("type") != EVENT_REQUEST:
            logger.error("Ignored a '%s' from the engine this worker works for.", body.get("type"))
            return
        try:
            event_request = read_event_request(body)
        except WireFormatError as e:
            logger.error("Ignored a request from the engine this worker works for: %s.", e)
            return
        handled = asyncio.run_coroutine_threadsafe(self.answer_parent_request(event_request), self._require_loop())
        handled.add_done_callback(_log_unanswered_failure)

    def _require_engine(self) -> Engine:
        if self._engine is None:
            msg = "WorkerSide.arm() must be called before the worker answers anything."
            raise RuntimeError(msg)
        return self._engine

    def _require_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is None:
            msg = "WorkerSide.start() must be called before the channel delivers anything."
            raise RuntimeError(msg)
        return self._loop


class _ChannelRequestSender:
    """Sends the requests `RemoteHandler` forwards over the channel, as the wire envelope a client's request is."""

    def __init__(self, channel: Channel) -> None:
        self._channel = channel

    async def request_from_worker(
        self,
        event_request: EventRequest,
        request_topic: str,
        worker_response_topic: str,  # noqa: ARG002 (the channel carries the reply)
        timeout_ms: int | None = None,
    ) -> dict[str, Any]:
        timeout_s = None
        if timeout_ms:
            timeout_s = timeout_ms / 1000
        return await self._channel.request_async(event_request_envelope(event_request, request_topic), timeout_s)


class _EditorLogHandler(logging.Handler):
    """Queue engine log records for the editor, as the engine's host does for its own."""

    def __init__(self, engine: Engine) -> None:
        super().__init__()
        self._engine = engine

    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        if len(message) > _LOG_MESSAGE_MAX_LENGTH:
            message = message[:_LOG_MESSAGE_MAX_LENGTH] + "... (truncated; full output available in engine logs)"
        self._engine.event_manager.put_event(
            AppEvent(
                payload=LogHandlerEvent(
                    message=message,
                    levelname=record.levelname,
                    created=record.created,
                    node_name=current_executing_node_name.get(None),
                )
            )
        )


def run_worker(library_name: str | None) -> int:
    """Run this process as a library's worker until its parent closes the channel. Returns the exit code."""
    label = library_name or "general-purpose"
    logging.basicConfig(level=logging.INFO, format=f"[worker {label}] %(message)s", stream=sys.stderr)
    # The parent decides when this process ends, by closing the channel. A Ctrl-C in the terminal
    # reaches the whole process group, and is the parent's to act on.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        worker_engine_id = os.environ[WORKER_ENGINE_ID_ENV]
        channel = connect_from_environment(os.environ)
    except (KeyError, ValueError, OSError, AuthenticationError) as e:
        logger.error(
            "Attempted to start a library worker process. Failed because it could not reach the engine that "
            "started it (%s). A worker process is started by Griptape Nodes, not by hand.",
            e,
        )
        return 1
    side = WorkerSide(channel, on_parent_gone=_exit_now)
    try:
        return asyncio.run(_serve(side, library_name, worker_engine_id))
    except WorkerStartupError as e:
        logger.error("%s", e)
        return 1


async def _serve(side: WorkerSide, library_name: str | None, worker_engine_id: str) -> int:
    side.start(asyncio.get_running_loop())
    engine = build_root_engine(worker=WorkerProcess(engine_id=worker_engine_id, read_secret=side.read_secret))
    worker_settled = prepare_worker_engine(engine, side)
    _route_logs_to_parent(engine)
    side.arm(engine)
    drain = asyncio.create_task(side.drain_events())

    try:
        await asyncio.wait_for(worker_settled.wait(), timeout=_PROJECT_SETTLE_TIMEOUT_S)
    except TimeoutError:
        msg = (
            "Attempted to start a library worker process. Failed because the engine that started it did not say "
            f"which project to run against within {_PROJECT_SETTLE_TIMEOUT_S:.0f} seconds."
        )
        raise WorkerStartupError(msg) from None
    adopted_project_id, _ = engine.project_manager.committed_project()
    # Deriving the project from the shared config is what system defaults means; a concrete project
    # is the parent's, and deriving would overwrite it.
    if adopted_project_id == SYSTEM_DEFAULTS_KEY:
        activation = await engine.event_manager.ahandle_request(ActivateWorkspaceProjectRequest())
        if isinstance(activation, EventResultFailure):
            logger.error("Workspace project activation failed: %s", activation.result.result_details)

    reports: list[ReportLibraryLoadedRequest | ReportGeneralPurposeWorkerReadyRequest] = []

    async def record_report(report: ReportLibraryLoadedRequest | ReportGeneralPurposeWorkerReadyRequest) -> None:
        reports.append(report)

    engine.library_manager.register_library_load_reporter(record_report)
    await engine.abroadcast_app_event(
        AppInitializationComplete(
            is_worker=True,
            is_general_purpose_worker=library_name is None,
            libraries_to_register=[library_name] if library_name else [],
        )
    )
    hello = _hello(engine, library_name, reports)
    reply = await side.channel.request_async(event_body(WORKER_HELLO, hello), PARENT_REQUEST_TIMEOUT_S)
    if not reply.get("accepted"):
        msg = f"The engine that started this worker refused it: {reply.get('details', reply)}"
        raise WorkerStartupError(msg)
    logger.debug("Worker accepted: %s", reply.get("details"))
    await drain
    return 0


def prepare_worker_engine(engine: Engine, side: WorkerSide) -> asyncio.Event:
    """Install what makes `engine` a worker for the parent `side` talks to, before any library loads.

    Returns:
        Set once the worker is on the project its parent sent, after which it may load libraries.

    Raises:
        WorkerStartupError: The checkpoint hook did not install, so the worker would run on the engine's
            allow-by-default.
    """
    event_manager = engine.event_manager
    event_manager.initialize_queue()
    event_manager.add_authorization_hook(side.authorize_with_parent)
    if not event_manager.has_authorization_hook(side.authorize_with_parent):
        msg = (
            "Attempted to start a library worker process. Failed because it could not install the hook that "
            "asks the engine that started it about permissions, and it does not run without one."
        )
        raise WorkerStartupError(msg)
    event_manager.configure_worker_forwarding(
        request_client=_ChannelRequestSender(side.channel),
        orchestrator_request_topic=CHANNEL_REQUEST_TOPIC,
        worker_response_topic=_CHANNEL_RESPONSE_TOPIC,
        websocket_event_loop=_start_forwarding_loop(),
        timeout_ms=int(PARENT_REQUEST_TIMEOUT_S * 1000),
    )
    register_remote_handlers(event_manager)
    return register_broadcast_handlers(
        event_manager,
        config_manager=engine.config_manager,
        secrets_manager=engine.secrets_manager,
        project_manager=engine.project_manager,
    )


def _hello(
    engine: Engine,
    library_name: str | None,
    reports: list[ReportLibraryLoadedRequest | ReportGeneralPurposeWorkerReadyRequest],
) -> WorkerHello:
    """What this worker tells its parent once its libraries have loaded."""
    libraries = [
        LibraryLoadReport(
            library_name=report.library_name, fitness=report.fitness, problem_details=report.problem_details
        )
        for report in reports
        if isinstance(report, ReportLibraryLoadedRequest)
    ]
    if library_name is None:
        loaded = any(isinstance(report, ReportGeneralPurposeWorkerReadyRequest) for report in reports)
    else:
        loaded = any(library.library_name == library_name for library in libraries)
    failure_reason = None
    if not loaded:
        if library_name is None:
            failure_reason = "it did not finish loading libraries."
        else:
            failure_reason = _load_failure_reason(engine, library_name)
    return WorkerHello(
        engine_version=engine_version,
        library_name=library_name,
        loaded=loaded,
        libraries=libraries,
        failure_reason=failure_reason,
    )


def _load_failure_reason(engine: Engine, library_name: str) -> str:
    library_info = engine.library_manager.get_library_info_by_library_name(library_name)
    if library_info is None:
        return "the library is not registered in the worker's configuration."
    problems = engine.library_manager.catalog.collate_problems_for_lib_info(library_info)
    if problems:
        return f"the library did not load there ({library_info.lifecycle_state}): {problems}"
    return f"the library did not load there ({library_info.lifecycle_state})."


def _route_logs_to_parent(engine: Engine) -> None:
    level = logging.getLevelNamesMapping()[engine.config_manager.get_config_value("log_level").upper()]
    engine_logger = logging.getLogger("griptape_nodes")
    engine_logger.setLevel(level)
    handler = _EditorLogHandler(engine)
    handler.setLevel(level)
    engine_logger.addHandler(handler)


def _start_forwarding_loop() -> asyncio.AbstractEventLoop:
    """A loop of its own for forwarded requests, so a sync request from code on the engine loop can block it."""
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, name="gtn-worker-forwarding", daemon=True).start()
    return loop


def _read_checkpoint_reply(reply: dict[str, Any]) -> CheckpointDenial | None:
    if "denial" not in reply:
        msg = f"it answered with {reply}"
        raise WireFormatError(msg)
    if reply["denial"] is None:
        return None
    return read_payload({"type": AUTHORIZATION_CHECKPOINT, "payload": reply["denial"]}, CheckpointDenial)


def _log_unanswered_failure(handled: Future[Any]) -> None:
    if handled.cancelled():
        return
    error = handled.exception()
    if error is not None:
        logger.error("A request from the engine this worker works for failed: %s", error)


def _exit_now() -> None:
    """End the process at once: the parent is done with it, and nothing it was running has anyone to answer to."""
    logger.debug("The channel to the engine this worker works for closed; exiting.")
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)
