from dataclasses import dataclass, field

from griptape_nodes.retained_mode.events.base_events import (
    RequestPayload,
    ResultPayloadFailure,
    ResultPayloadSuccess,
    SkipTheLineMixin,
    WorkflowNotAlteredMixin,
)
from griptape_nodes.retained_mode.events.payload_registry import PayloadRegistry


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

    Args:
        worker_engine_id: The engine_id of the worker that was registered.
        current_project_id: The project the orchestrator has active, or None when it is on system
            defaults. The worker adopts this BEFORE loading libraries, which is what keeps the two
            processes on one workspace: a project decides the workspace, libraries resolve against
            the workspace, and a worker that loaded first would resolve them against a different
            one. Answered here rather than pushed afterwards so the ordering is a consequence of
            the reply the worker already waits for, not of two messages racing.
    """

    worker_engine_id: str
    current_project_id: str | None = None
    project_generation: int = 0


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
        library_name: The library the worker will serve.
    """

    library_name: str
    broadcast_result: bool = field(default=False, kw_only=True)


@dataclass
@PayloadRegistry.register
class StartWorkerResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Worker spawn was successfully scheduled."""


@dataclass
@PayloadRegistry.register
class StartWorkerResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Worker spawn could not be scheduled."""
