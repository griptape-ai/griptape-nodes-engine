from dataclasses import dataclass, field

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

    Also sent by an orchestrator to the machine its distribution settings assign the library
    to, with `originator_session_id` set. That machine's orchestrator then starts the worker
    in the originator's session, so the worker registers with the originator and serves it.

    Args:
        library_name: The library the worker will serve.
        originator_session_id: The session of the orchestrator on another machine that the
            worker serves, or None for a worker of this orchestrator's own session.
    """

    library_name: str
    originator_session_id: str | None = None
    broadcast_result: bool = field(default=False, kw_only=True)


@dataclass
@PayloadRegistry.register
class StartWorkerResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Worker spawn was successfully scheduled."""


@dataclass
@PayloadRegistry.register
class StartWorkerResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Worker spawn could not be scheduled.

    Args:
        originator_unknown: The request came from an orchestrator on another machine that has not
            registered with this one, as after this machine restarts. It may register and ask again.
    """

    originator_unknown: bool = False


@dataclass
@PayloadRegistry.register
class RegisterOriginatorRequest(RequestPayload):
    """Sent by an orchestrator to another machine's orchestrator before asking it to start any worker.

    Sent once per session for each machine the distribution settings assign a library to. The
    serving orchestrator refuses an originator whose engine version differs from its own, because
    the wire shape of every event is tied to the engine build, and refuses each library whose
    version differs or that it does not have.

    Args:
        originator_session_id: The originating orchestrator's session, which its workers there join.
        schema_version: The version of the distributed execution contract the originator speaks.
        engine_version: The originator's engine version.
        library_versions: The version of each library the originator will send to that machine.
        license_token: The originator's Griptape Nodes license, which workers started for it run
            under, or None when it has none.
        static_server_base_url: Where the originator serves the shared workspace, so the asset URLs
            its workers mint are ones the originator's editor can open.
    """

    originator_session_id: str
    schema_version: str
    engine_version: str
    library_versions: dict[str, str]
    license_token: str | None = None
    static_server_base_url: str | None = None
    broadcast_result: bool = field(default=False, kw_only=True)


@dataclass
@PayloadRegistry.register
class RegisterOriginatorResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """The serving orchestrator accepted the originator.

    Args:
        engine_id: The serving orchestrator's engine ID, so the originator can tell it reached the
            machine its distribution settings name.
        refused_libraries: Each library it will not start workers for, with the reason.
    """

    engine_id: str = ""
    refused_libraries: dict[str, str] = field(default_factory=dict)


@dataclass
@PayloadRegistry.register
class RegisterOriginatorResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """The serving orchestrator refused the originator, and will start no workers for it."""


@dataclass
@PayloadRegistry.register
class AdmitNodeRequest(RequestPayload):
    """Sent by an orchestrator to the machine whose worker will run a node, before sending the node.

    The machine answers once the node may run: at once while it is free or already running this run,
    otherwise when the run holding it ends. Waiting nodes are told their place in line.

    Args:
        originator_session_id: The session of the orchestrator the node belongs to.
        admission_id: Names this admission, so the node's NodeExecutionEndedRequest can say which ended.
        run_id: The run the node belongs to, or None for a node run on its own.
        node_name: The node, named in the waiting status the artist sees.
    """

    originator_session_id: str
    admission_id: str
    run_id: str | None
    node_name: str
    broadcast_result: bool = field(default=False, kw_only=True)


@dataclass
@PayloadRegistry.register
class AdmitNodeResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """The node may run.

    Args:
        run_id: The run the machine counts it in, minted for a node sent without one.
    """

    run_id: str


@dataclass
@PayloadRegistry.register
class AdmitNodeResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """The node will not run on that machine."""


@dataclass
@PayloadRegistry.register
class NodeExecutionEndedRequest(RequestPayload):
    """Sent by an orchestrator when a node it was admitted to run on another machine finishes, or is cancelled.

    Args:
        originator_session_id: The session of the orchestrator the node belongs to.
        admission_id: The `admission_id` of the node's AdmitNodeRequest.
    """

    originator_session_id: str
    admission_id: str
    broadcast_result: bool = field(default=False, kw_only=True)


@dataclass
@PayloadRegistry.register
class NodeExecutionEndedResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """The machine recorded the node as ended."""


@dataclass
@PayloadRegistry.register
class EndRunRequest(RequestPayload):
    """Sent by an orchestrator to every machine a run used, when the run finishes, fails, or is cancelled.

    Args:
        originator_session_id: The session of the orchestrator the run belongs to.
        run_id: The run that ended.
    """

    originator_session_id: str
    run_id: str
    broadcast_result: bool = field(default=False, kw_only=True)


@dataclass
@PayloadRegistry.register
class EndRunResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """The machine no longer holds itself for the run."""


@dataclass
@PayloadRegistry.register
class ReleaseOriginatorWorkersRequest(RequestPayload):
    """Sent by an orchestrator to each machine it uses when it resets, so their workers for it stop.

    Args:
        originator_session_id: The session whose workers stop.
    """

    originator_session_id: str
    broadcast_result: bool = field(default=False, kw_only=True)


@dataclass
@PayloadRegistry.register
class ReleaseOriginatorWorkersResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """The machine stopped the originator's workers."""
