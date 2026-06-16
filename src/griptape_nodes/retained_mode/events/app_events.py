from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from griptape_nodes.retained_mode.events.base_events import (
    AppPayload,
    RequestPayload,
    ResultPayloadFailure,
    ResultPayloadSuccess,
    SkipTheLineMixin,
    WorkflowNotAlteredMixin,
)
from griptape_nodes.retained_mode.events.payload_registry import PayloadRegistry


class InitializationPhase(StrEnum):
    """Initialization phase types for engine startup."""

    LIBRARIES = "libraries"
    WORKFLOWS = "workflows"


class InitializationStatus(StrEnum):
    """Status types for initialization progress."""

    LOADING = "loading"
    COMPLETE = "complete"
    FAILED = "failed"


@dataclass
class OrganizationInfo:
    """Organization information from Griptape Cloud."""

    id: str
    name: str


@dataclass
class UserInfo:
    """User information from Griptape Cloud."""

    id: str
    email: str
    name: str | None = None


@dataclass
@PayloadRegistry.register
class AppStartSessionRequest(RequestPayload):
    """Start a new application session.

    Use when: Initializing client connections, beginning new workflow sessions,
    setting up isolated execution environments, managing session state.

    Results: AppStartSessionResultSuccess (with session ID) | AppStartSessionResultFailure (session creation error)
    """


@dataclass
@PayloadRegistry.register
class AppStartSessionResultSuccess(ResultPayloadSuccess):
    """Session started successfully.

    Args:
        session_id: Unique identifier for the created session
    """

    session_id: str


@dataclass
@PayloadRegistry.register
class AppStartSessionResultFailure(ResultPayloadFailure):
    """Session start failed. Common causes: resource constraints, initialization error."""


@dataclass
@PayloadRegistry.register
class AppGetSessionRequest(RequestPayload):
    """Get the current session information.

    Use when: Checking session status, retrieving session details,
    validating session state, debugging session issues.

    Results: AppGetSessionResultSuccess (with session ID) | AppGetSessionResultFailure (session error)
    """


@dataclass
@PayloadRegistry.register
class AppGetSessionResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Session information retrieved successfully.

    Args:
        session_id: Current session identifier (None if no active session)
    """

    session_id: str | None


@dataclass
@PayloadRegistry.register
class AppGetSessionResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Session information retrieval failed. Common causes: session not found, access error."""


@dataclass
@PayloadRegistry.register
class AppInitializationComplete(AppPayload):
    """Application initialization completed successfully. All subsystems ready."""

    libraries_to_download: list[str] = field(default_factory=list)
    libraries_to_register: list[str] = field(default_factory=list)
    workflows_to_register: list[str] = field(default_factory=list)
    models_to_download: list[str] = field(default_factory=list)
    skip_library_loading: bool = False
    # When True, this process is a dedicated library worker. The library to load is
    # taken from the first entry in libraries_to_register. Multiple workers can run
    # simultaneously for different libraries.
    is_worker: bool = False


@dataclass
@PayloadRegistry.register
class AppConnectionEstablished(AppPayload):
    """Notification that a connection to the API has been established."""


@dataclass
@PayloadRegistry.register
class AppSessionStartedEvent(AppPayload):
    """Notification that a session has started and workers can be spawned."""

    session_id: str


@dataclass
@PayloadRegistry.register
class EngineInitializationProgress(AppPayload):
    """Real-time progress updates during engine initialization (libraries and workflows loading).

    Args:
        phase: Current initialization phase (libraries or workflows)
        item_name: Name of the library or workflow being loaded
        status: Current status of the item (loading, complete, or failed)
        current: Number of items completed so far
        total: Total number of items to load
        error: Error message if status is failed, None otherwise
    """

    phase: InitializationPhase
    item_name: str
    status: InitializationStatus
    current: int
    total: int
    error: str | None = None
    is_worker: bool = False


@dataclass
class WorkerParameterSchema:
    """Serialized parameter from a worker-loaded node.

    Contains the fields needed to reconstruct a Parameter on the orchestrator
    without importing the worker library's Python modules.
    """

    name: str
    type: str
    input_types: list[str]
    output_type: str
    default_value: Any
    tooltip: Any
    tooltip_as_input: Any
    tooltip_as_property: Any
    tooltip_as_output: Any
    mode_allowed_input: bool
    mode_allowed_property: bool
    mode_allowed_output: bool
    user_defined: bool
    settable: bool
    serializable: bool
    private: bool
    ui_options: dict | None


@dataclass
class WorkerNodeSchema:
    """Serialized node from a worker-loaded library."""

    class_name: str
    parameters: list[WorkerParameterSchema]


@dataclass
@PayloadRegistry.register
class LibraryLoadedNotification(AppPayload):
    """Notification that a library has finished loading, including its fitness outcome.

    Emitted after a library reaches LOADED state. The orchestrator re-broadcasts it so
    all listeners (including the GUI) can update their view of library health.

    Args:
        library_name: Name of the library that was loaded.
        fitness: Final fitness value (LibraryManager.LibraryFitness string).
        problem_details: Human-readable summary of problems, or None if there are none.
        node_schemas: Serialized node/parameter schemas from the worker process, or None
            if this notification was not produced by a worker.
    """

    library_name: str
    fitness: str
    problem_details: str | None = None
    node_schemas: list[WorkerNodeSchema] | None = None


@dataclass
@PayloadRegistry.register
class ConfigChanged(AppPayload):
    """Configuration value changed notification.

    Emitted whenever a config value is modified via set_config_value() or
    set_config_category(). Allows managers to respond to specific config changes.

    Args:
        key: The config key that changed (e.g., "workspace_directory").
             Empty string ("") indicates the entire config was replaced.
        old_value: Previous value before the change
        new_value: New value after the change
    """

    key: str
    old_value: Any
    new_value: Any


@dataclass
@PayloadRegistry.register
class SecretChanged(AppPayload):
    """Secret value mutation notification.

    Emitted by the orchestrator's SecretsManager after a secret is set or
    deleted. WorkerManager listens for this and fans out a refresh signal to
    every registered worker so their os.environ shadow picks up the new value
    from the shared .env file.

    The secret value itself is intentionally not carried in this event --
    workers re-read from the shared .env file on the same machine.

    Args:
        key: The secret name (normalized) that changed.
    """

    key: str


@dataclass
@PayloadRegistry.register
class GetEngineVersionRequest(RequestPayload):
    """Get the engine version information.

    Use when: Checking compatibility, displaying version info,
    debugging engine issues, validating engine capabilities.

    Results: GetEngineVersionResultSuccess (with version numbers) | GetEngineVersionResultFailure (version error)
    """


@dataclass
@PayloadRegistry.register
class GetEngineVersionResultSuccess(ResultPayloadSuccess):
    """Engine version retrieved successfully.

    Args:
        major: Major version number
        minor: Minor version number
        patch: Patch version number
    """

    major: int
    minor: int
    patch: int


@dataclass
@PayloadRegistry.register
class GetEngineVersionResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Engine version retrieval failed. Common causes: version not available, system error."""


@dataclass
@PayloadRegistry.register
class AppEndSessionRequest(RequestPayload):
    """End the current application session.

    Use when: Closing client connections, cleaning up session resources,
    terminating workflow sessions, implementing logout functionality.

    Results: AppEndSessionResultSuccess (with session ID) | AppEndSessionResultFailure (cleanup error)
    """


@dataclass
@PayloadRegistry.register
class AppEndSessionResultSuccess(ResultPayloadSuccess):
    """Session ended successfully.

    Args:
        session_id: Identifier of the ended session (None if no session was active)
    """

    session_id: str | None


@dataclass
@PayloadRegistry.register
class AppEndSessionResultFailure(ResultPayloadFailure):
    """Session end failed. Common causes: session not found, cleanup error."""


@dataclass
@PayloadRegistry.register
class SessionHeartbeatRequest(RequestPayload, SkipTheLineMixin):
    """Request clients can use ensure the engine session is still active."""


@dataclass
@PayloadRegistry.register
class SessionHeartbeatResultSuccess(ResultPayloadSuccess):
    """Session heartbeat successful. Session is active and responsive."""


@dataclass
@PayloadRegistry.register
class SessionHeartbeatResultFailure(ResultPayloadFailure):
    """Session heartbeat failed. Common causes: session inactive, network error, timeout."""


@dataclass
@PayloadRegistry.register
class EngineHeartbeatRequest(RequestPayload, SkipTheLineMixin):
    """Request clients can use to discover active engines and their status.

    Attributes:
        heartbeat_id: Unique identifier for the heartbeat request, used to correlate requests and responses.

    """

    heartbeat_id: str


@dataclass
@PayloadRegistry.register
class EngineHeartbeatResultSuccess(ResultPayloadSuccess):
    """Engine heartbeat successful with comprehensive status information.

    Args:
        heartbeat_id: Unique identifier correlating with the request
        engine_version: Current engine version string
        engine_id: Unique engine identifier (None if not set)
        session_id: Current session identifier (None if no session)
        timestamp: Heartbeat timestamp
        instance_type: Cloud instance type (None if not applicable)
        instance_region: Cloud instance region (None if not applicable)
        instance_provider: Cloud provider name (None if not applicable)
        deployment_type: Type of deployment (None if not applicable)
        current_workflow: Name of active workflow (None if none)
        workflow_file_path: Path to workflow file (None if none)
        has_active_flow: Whether there's an active flow running
        engine_name: Human-readable engine name
        user: User information including ID, email, and name (None if not logged in)
        user_organization: User's organization information including ID and name (None if not logged in)
    """

    heartbeat_id: str
    engine_version: str
    engine_id: str | None
    session_id: str | None
    timestamp: str
    instance_type: str | None
    instance_region: str | None
    instance_provider: str | None
    deployment_type: str | None
    current_workflow: str | None
    workflow_file_path: str | None
    has_active_flow: bool
    engine_name: str
    user: UserInfo | None
    user_organization: OrganizationInfo | None
    # Whether the engine is still running its initialization sequence (loading libraries and
    # workflows). Lets a client connecting mid-startup render a loading state instead of an
    # empty workflow list; the live EngineInitializationProgress stream fills in the detail.
    # Defaulted for backward compatibility with older clients.
    is_initializing: bool = False


@dataclass
@PayloadRegistry.register
class EngineHeartbeatResultFailure(ResultPayloadFailure):
    """Engine heartbeat failed.

    Args:
        heartbeat_id: Unique identifier correlating with the request
    """

    heartbeat_id: str


@dataclass
@PayloadRegistry.register
class SetEngineNameRequest(RequestPayload):
    """Set the human-readable engine name.

    Use when: Customizing engine identification, setting up engine instances,
    implementing engine management, branding engine instances.

    Args:
        engine_name: New name for the engine

    Results: SetEngineNameResultSuccess (with name) | SetEngineNameResultFailure (validation error)
    """

    engine_name: str


@dataclass
@PayloadRegistry.register
class SetEngineNameResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Engine name set successfully.

    Args:
        engine_name: The name that was set for the engine
    """

    engine_name: str


@dataclass
@PayloadRegistry.register
class SetEngineNameResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Engine name setting failed.

    Args:
        error_message: Detailed error message describing the failure
    """

    error_message: str


@dataclass
@PayloadRegistry.register
class GetEngineNameRequest(RequestPayload):
    """Get the current engine name.

    Use when: Displaying engine information, checking engine identity,
    implementing engine management UIs, debugging engine issues.

    Results: GetEngineNameResultSuccess (with name) | GetEngineNameResultFailure (retrieval error)
    """


@dataclass
@PayloadRegistry.register
class GetEngineNameResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Engine name retrieved successfully.

    Args:
        engine_name: Current engine name
    """

    engine_name: str


@dataclass
@PayloadRegistry.register
class GetEngineNameResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Engine name retrieval failed.

    Args:
        error_message: Detailed error message describing the failure
    """

    error_message: str
