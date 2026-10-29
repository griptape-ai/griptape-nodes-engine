from dataclasses import dataclass, field
from typing import Literal

from griptape_nodes.retained_mode.events.base_events import (
    RequestPayload,
    ResultPayloadFailure,
    ResultPayloadSuccess,
    SkipTheLineMixin,
    WorkflowNotAlteredMixin,
)
from griptape_nodes.retained_mode.events.payload_registry import PayloadRegistry


class WorkerGoneError(Exception):
    """Raised at whatever was awaiting a worker's reply, once that worker will never send one.

    Carries the reason the worker went away, written where it is known -- eviction and a library
    reload are different stories and only their own call sites can tell them. Raised rather than
    cancelling the awaiting task, so a CancelledError keeps its single meaning: the flow was
    cancelled.

    Lives here, with the worker protocol's payloads, because both the manager that raises it and
    the manager that turns it into a node failure import it at runtime, and events are the leaf of
    the import graph.
    """


@dataclass
@PayloadRegistry.register
class RegisterWorkerRequest(RequestPayload):
    """Sent by a worker engine to the orchestrator's session topic to announce itself.

    The orchestrator stores the worker's engine_id and subscribes to the worker's
    dedicated response topic so it can relay results back to the GUI.

    Args:
        worker_engine_id: The engine_id of the registering worker.
        engine_version: The griptape_nodes engine version the worker is running.
            The orchestrator rejects registrations whose engine_version does not
            match its own; workers and orchestrators must share a version because
            the wire shape of every event is tied to the engine build.
        library_name: The library this worker exclusively serves, or None for a
            general-purpose worker that can handle any request.
    """

    worker_engine_id: str
    engine_version: str
    library_name: str | None = None
    broadcast_result: bool = field(default=False, kw_only=True)


@dataclass
@PayloadRegistry.register
class RegisterWorkerResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Worker registration succeeded.

    Carries no project: the orchestrator sends the worker's first activation down the same path a
    mid-session switch uses, so there is one sender and one adoption path rather than two that have
    to be ordered against each other.

    Args:
        worker_engine_id: The engine_id of the worker that was registered.
    """

    worker_engine_id: str


@dataclass
@PayloadRegistry.register
class RegisterWorkerResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Worker registration failed."""


@dataclass
@PayloadRegistry.register
class WorkerHeartbeatRequest(RequestPayload, SkipTheLineMixin):
    """Sent by the orchestrator to a worker to verify it is still alive.

    Uses SkipTheLineMixin so the worker processes it immediately without queuing,
    identical to SessionHeartbeatRequest and EngineHeartbeatRequest.

    Args:
        heartbeat_id: Correlation token to match request with response.
    """

    heartbeat_id: str


@dataclass
@PayloadRegistry.register
class WorkerHeartbeatResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Worker heartbeat succeeded — worker is alive.

    Args:
        heartbeat_id: Correlates with the originating WorkerHeartbeatRequest.
    """

    heartbeat_id: str


@dataclass
@PayloadRegistry.register
class UnregisterWorkerRequest(RequestPayload):
    """Sent by a worker to the orchestrator's session topic on graceful shutdown.

    Args:
        worker_engine_id: The engine_id of the departing worker.
    """

    worker_engine_id: str
    broadcast_result: bool = field(default=False, kw_only=True)


@dataclass
@PayloadRegistry.register
class UnregisterWorkerResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Worker unregistration succeeded.

    Args:
        worker_engine_id: The engine_id of the worker that was removed.
    """

    worker_engine_id: str


@dataclass
@PayloadRegistry.register
class UnregisterWorkerResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Worker unregistration failed."""


@dataclass
@PayloadRegistry.register
class StartWorkerRequest(RequestPayload):
    """Internal request to spawn a worker subprocess for a library that requires one.

    Sent by LibraryManager when a worker-delegated library finishes loading on the
    orchestrator. WorkerManager handles this by waiting for an active session then
    spawning the worker subprocess.

    Args:
        library_name: The library the worker will serve, or None for the general-purpose worker,
            which serves no single library and runs the nodes of Isolated Execution groups.
    """

    library_name: str | None
    broadcast_result: bool = field(default=False, kw_only=True)


@dataclass
@PayloadRegistry.register
class StartWorkerResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Worker spawn was successfully scheduled."""


@dataclass
@PayloadRegistry.register
class StartWorkerResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Worker spawn could not be scheduled."""


@dataclass
@PayloadRegistry.register
class StartWorkerForEngineRequest(RequestPayload):
    """Sent by the artist's engine to the engine its distribution settings assign a library to.

    That engine starts the library's worker under `worker_engine_id` in the session of the engine
    that sent the request, so the worker registers with the artist's engine and serves it.

    Args:
        library_name: The library the worker will serve.
        worker_engine_id: The engine ID the worker takes, chosen by the artist's engine so it can
            recognize the worker's registration, and stop it, before this request is answered.
    """

    library_name: str
    worker_engine_id: str
    broadcast_result: bool = field(default=False, kw_only=True)


@dataclass
@PayloadRegistry.register
class StartWorkerForEngineResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """The engine started the worker."""


@dataclass
@PayloadRegistry.register
class StartWorkerForEngineResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """The engine did not start the worker.

    Args:
        session_unknown: The session that asked has not registered with this engine, as after this
            engine restarts, or registered without naming the library, as after its settings
            changed. It may register and ask again.
        at_capacity: This engine already runs as many workers for other engines as it allows. It may
            ask again later.
    """

    session_unknown: bool = False
    at_capacity: bool = False


@dataclass
@PayloadRegistry.register
class OfferSessionForWorkersRequest(RequestPayload):
    """Sent by the artist's engine to another engine before asking it to start any worker.

    Sent once per session for each engine the distribution settings assign a library to. That
    engine registers the session of the engine that sent it, which the workers it starts for it
    join. It refuses a session whose engine version differs from its own, because the wire shape of
    every event is tied to the engine build, and refuses each library whose version differs or
    that it does not have.

    Args:
        schema_version: The version of the distributed execution contract the artist's engine speaks.
        engine_version: The artist's engine version.
        library_versions: The version of each library the artist's engine will send to that engine.
        static_server_base_url: Where the artist's engine serves the shared workspace, so the asset
            URLs its workers mint are ones the artist's editor can open.
    """

    schema_version: str
    engine_version: str
    library_versions: dict[str, str]
    static_server_base_url: str | None = None
    broadcast_result: bool = field(default=False, kw_only=True)


@dataclass
@PayloadRegistry.register
class OfferSessionForWorkersResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """The engine accepted the artist's session.

    Args:
        engine_id: The ID of the engine that accepted it, so the artist's engine can tell it reached
            the engine its distribution settings name.
        refused_libraries: Each library it will not start workers for, with the reason.
    """

    engine_id: str = ""
    refused_libraries: dict[str, str] = field(default_factory=dict)


@dataclass
@PayloadRegistry.register
class OfferSessionForWorkersResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """The engine refused the artist's session, and will start no workers for it."""


@dataclass
@PayloadRegistry.register
class WithdrawSessionOfferRequest(RequestPayload):
    """Sent by the artist's engine to each engine it offered its session to, once it is done with them.

    Sent when the artist's engine resets its workers, as when its session ends or it shuts down. The
    other engine forgets every session the sender offered and stops the
    workers it runs for them, as it does when the connection to the sender is lost.
    """

    broadcast_result: bool = field(default=False, kw_only=True)


@dataclass
@PayloadRegistry.register
class WithdrawSessionOfferResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """The engine forgot the sender's sessions."""


@dataclass
@PayloadRegistry.register
class WithdrawSessionOfferResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """The engine could not forget the sender's sessions."""


@dataclass
@PayloadRegistry.register
class StopWorkerForEngineRequest(RequestPayload):
    """Sent by the artist's engine to the engine it asked to start a worker, to stop that worker.

    Sent when the start is withdrawn after it was sent, as when the library is reset while the
    other engine has not answered yet. That engine reports the exit with `ReportWorkerExitedRequest`.

    Args:
        worker_engine_id: The worker to stop, as named in the `StartWorkerForEngineRequest`. Only
            the engine that started it may stop it.
    """

    worker_engine_id: str
    broadcast_result: bool = field(default=False, kw_only=True)


@dataclass
@PayloadRegistry.register
class StopWorkerForEngineResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """The engine stopped the worker, or was not running it."""


@dataclass
@PayloadRegistry.register
class StopWorkerForEngineResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """The engine could not stop the worker."""


@dataclass
@PayloadRegistry.register
class ReportWorkerExitedRequest(RequestPayload):
    """Sent by an engine to the artist's engine whenever a worker it runs for that artist exits.

    Sent for every exit, whatever ended the worker, so the artist's engine settles a start that
    never registered and removes a registered worker at once rather than after heartbeat eviction.

    Args:
        worker_engine_id: The worker that exited, as named in the `StartWorkerForEngineRequest`.
        exit_code: The worker process's exit code.
        reason: "stopped" when the engine stopped the worker on purpose, as when it shuts down or a
            new start replaces it, so the worker may be started again. "exited" when the worker
            ended on its own, as when it crashes or fails to start.
    """

    worker_engine_id: str
    exit_code: int
    reason: Literal["stopped", "exited"]
    broadcast_result: bool = field(default=False, kw_only=True)


@dataclass
@PayloadRegistry.register
class ReportWorkerExitedResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """The artist's engine recorded the exit."""


@dataclass
@PayloadRegistry.register
class ReportWorkerExitedResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """The exit was reported by an engine other than the one running the worker."""


@dataclass
@PayloadRegistry.register
class AdmitNodeRequest(RequestPayload):
    """Sent by the artist's engine to the engine whose worker will run a node, before sending the node.

    That engine runs one run at a time, its own and other engines' alike, in the order they asked. It
    answers once the node may run: at once while it is free or already running the node's run,
    otherwise when the runs ahead end. While the node waits, it reports the run's place in line with
    `ReportPlaceInLineRequest`.

    Args:
        admission_id: Names this admission, in `NodeExecutionEndedRequest` and `ReportPlaceInLineRequest`.
        run_id: The run the node belongs to, or None for a node run on its own.
        node_name: The node, named in that engine's log.
    """

    admission_id: str
    run_id: str | None
    node_name: str
    broadcast_result: bool = field(default=False, kw_only=True)


@dataclass
@PayloadRegistry.register
class AdmitNodeResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """The node may run."""


@dataclass
@PayloadRegistry.register
class AdmitNodeResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """The node will not run on that engine."""


@dataclass
@PayloadRegistry.register
class ReportPlaceInLineRequest(RequestPayload):
    """Sent by an engine to the artist's engine while one of its nodes waits there, each time its run's place changes.

    Sent once more with position 0 when the run stops waiting.

    Args:
        admission_id: The `admission_id` of the waiting node's `AdmitNodeRequest`.
        position: The run's place in line, 1 being next, or 0 once it no longer waits.
    """

    admission_id: str
    position: int
    broadcast_result: bool = field(default=False, kw_only=True)


@dataclass
@PayloadRegistry.register
class ReportPlaceInLineResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """The artist's engine passed the place in line on."""


@dataclass
@PayloadRegistry.register
class ReportPlaceInLineResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """The place in line came from an engine other than the one the node waits on."""


@dataclass
@PayloadRegistry.register
class NodeExecutionEndedRequest(RequestPayload):
    """Sent by the artist's engine when a node another engine admitted ends, however it ends.

    Args:
        admission_id: The `admission_id` of the node's `AdmitNodeRequest`.
    """

    admission_id: str
    broadcast_result: bool = field(default=False, kw_only=True)


@dataclass
@PayloadRegistry.register
class NodeExecutionEndedResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """The engine recorded the node as ended."""


@dataclass
@PayloadRegistry.register
class NodeExecutionEndedResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """The engine could not record the node as ended."""


@dataclass
@PayloadRegistry.register
class EndRunRequest(RequestPayload):
    """Sent by the artist's engine to every engine a run used, once the run ends, so they serve the next run.

    Args:
        run_id: The run that ended.
    """

    run_id: str
    broadcast_result: bool = field(default=False, kw_only=True)


@dataclass
@PayloadRegistry.register
class EndRunResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """The engine no longer holds itself for the run."""


@dataclass
@PayloadRegistry.register
class EndRunResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """The engine could not end the run."""


@dataclass
@PayloadRegistry.register
class DropLibraryNamedLocalObjectsRequest(RequestPayload, SkipTheLineMixin):
    """Sent to a worker to release what its libraries keep between runs, such as a loaded model.

    Sent by an engine that runs nodes for other engines, as it passes from one engine's run to another's,
    so the incoming run has the GPU to itself. Values the engine parked for a node's parameters stay: a
    later node of the same run may still need them, and nothing could rebuild them.
    """


@dataclass
@PayloadRegistry.register
class DropLibraryNamedLocalObjectsResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """The worker released what its libraries kept between runs."""


@dataclass
@PayloadRegistry.register
class DropLibraryNamedLocalObjectsResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """The worker failed while releasing what its libraries kept between runs."""


@dataclass
@PayloadRegistry.register
class EngineSessionHeartbeatRequest(RequestPayload, SkipTheLineMixin):
    """Sent on an interval by each of two engines with a session between them, to tell the other is still there.

    The artist's engine sends it to each engine it registered a session with, and that engine to each engine
    that offered it one. One that stops answering, or answers that it knows of no session, is lost.

    Args:
        heartbeat_id: Correlation token, named in the log.
    """

    heartbeat_id: str
    broadcast_result: bool = field(default=False, kw_only=True)


@dataclass
@PayloadRegistry.register
class EngineSessionHeartbeatResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """The engine is there, and a session between the two engines is too."""


@dataclass
@PayloadRegistry.register
class EngineSessionHeartbeatResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """The engine is there but has no session with the engine that asked, as after it restarted."""
