"""The Engine: the object graph that owns every manager.

``Engine`` is a plain class. Construct as many as you like -- each one owns its own
``EventManager``, ``FlowManager``, ``ObjectManager``, and so on, with no shared state
between instances. This is what makes isolated tests and multiple in-process engines
possible.

Exactly one piece of global state lives here: the process-wide root engine, resolved by
``current_engine()``. The ``GriptapeNodes`` facade in ``griptape_nodes.py`` is the only
thing that should read it. Everything else receives its dependencies.
"""

from __future__ import annotations

import logging
import os
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import semver

from griptape_nodes.exe_types.flow import ControlFlow
from griptape_nodes.node_library.workflow_registry import WorkflowRegistry
from griptape_nodes.retained_mode.events.app_events import (
    EngineHeartbeatRequest,
    EngineHeartbeatResultFailure,
    EngineHeartbeatResultSuccess,
    GetEngineVersionRequest,
    GetEngineVersionResultFailure,
    GetEngineVersionResultSuccess,
)
from griptape_nodes.retained_mode.events.base_events import (
    GriptapeNodeEvent,
    ResultPayloadFailure,
)
from griptape_nodes.retained_mode.events.execution_events import (
    CancelFlowRequest,
)
from griptape_nodes.retained_mode.events.flow_events import (
    DeleteFlowRequest,
)
from griptape_nodes.utils.ffmpeg_cache import redirect_ffmpeg_cache, resolve_ffmpeg_directory
from griptape_nodes.utils.version_utils import engine_version

if TYPE_CHECKING:
    from collections.abc import Iterator

    from griptape_nodes.retained_mode.events.base_events import (
        AppPayload,
        RequestPayload,
        ResultPayload,
    )
    from griptape_nodes.retained_mode.managers.access_manager import AccessManager
    from griptape_nodes.retained_mode.managers.agent_manager import AgentManager
    from griptape_nodes.retained_mode.managers.arbitrary_code_exec_manager import (
        ArbitraryCodeExecManager,
    )
    from griptape_nodes.retained_mode.managers.artifact_manager import ArtifactManager
    from griptape_nodes.retained_mode.managers.config_manager import ConfigManager
    from griptape_nodes.retained_mode.managers.context_manager import ContextManager
    from griptape_nodes.retained_mode.managers.engine_identity_manager import EngineIdentityManager
    from griptape_nodes.retained_mode.managers.event_manager import EventManager
    from griptape_nodes.retained_mode.managers.flow_manager import FlowManager
    from griptape_nodes.retained_mode.managers.library_manager import LibraryManager
    from griptape_nodes.retained_mode.managers.manifest_manager import ManifestManager
    from griptape_nodes.retained_mode.managers.mcp_manager import MCPManager
    from griptape_nodes.retained_mode.managers.model_manager import ModelManager
    from griptape_nodes.retained_mode.managers.node_manager import NodeManager
    from griptape_nodes.retained_mode.managers.object_manager import ObjectManager
    from griptape_nodes.retained_mode.managers.operation_manager import (
        OperationDepthManager,
    )
    from griptape_nodes.retained_mode.managers.os_manager import OSManager
    from griptape_nodes.retained_mode.managers.project_manager import ProjectManager
    from griptape_nodes.retained_mode.managers.resource_manager import ResourceManager
    from griptape_nodes.retained_mode.managers.secrets_manager import SecretsManager
    from griptape_nodes.retained_mode.managers.session_manager import SessionManager
    from griptape_nodes.retained_mode.managers.static_files_manager import (
        StaticFilesManager,
    )
    from griptape_nodes.retained_mode.managers.sync_manager import SyncManager
    from griptape_nodes.retained_mode.managers.user_manager import UserManager
    from griptape_nodes.retained_mode.managers.variable_manager import (
        VariablesManager,
    )
    from griptape_nodes.retained_mode.managers.version_compatibility_manager import (
        VersionCompatibilityManager,
    )
    from griptape_nodes.retained_mode.managers.worker_manager import WorkerManager
    from griptape_nodes.retained_mode.managers.workflow_manager import WorkflowManager


logger = logging.getLogger("griptape_nodes")

# Scoped override of the root engine. Set by `engine_scope`; `None` means "use the
# process root". A ContextVar rather than a plain global so a test (or an embedder
# running two engines) can rebind for a block and have asyncio tasks created inside
# that block inherit the binding.
_scoped_engine: ContextVar[Engine | None] = ContextVar("griptape_nodes_scoped_engine", default=None)

# The process root engine, created on first `current_engine()` call. Deliberately not a
# ContextVar: lazy creation can happen inside an arbitrary task, and a ContextVar set
# there would evaporate when that task finished.
_root_engine: Engine | None = None

# Serializes the lazy build so two threads racing the first `current_engine()` cannot each
# construct an engine and leave one of them orphaned mid-use. Reentrant so a nested call on
# the building thread reaches the guard in `current_engine` instead of deadlocking.
_root_engine_lock = threading.RLock()
_root_engine_building = False


class EngineScoped:
    """Base for objects that belong to a single `Engine`.

    Managers reach their peers through `self.engine` rather than through process-wide
    state, so an engine's object graph is self-contained.

    `engine` is optional only so a unit test can build a manager on its own without
    standing up a whole engine. `Engine` always injects itself, so the fallback to
    `current_engine()` never runs in production. It is the one implicit global read left
    outside the `GriptapeNodes` facade; make `engine` required once no test constructs a
    manager bare.
    """

    def __init__(self, engine: Engine | None = None) -> None:
        self._engine = engine

    @property
    def engine(self) -> Engine:
        if self._engine is None:
            return current_engine()
        return self._engine


class Engine:
    """Owns one complete set of managers.

    Managers are created here and hold a reference back to this engine, so they resolve
    peers through `self._engine` rather than reaching for process-wide state.
    """

    _event_manager: EventManager
    _os_manager: OSManager
    _config_manager: ConfigManager
    _secrets_manager: SecretsManager
    _object_manager: ObjectManager
    _node_manager: NodeManager
    _flow_manager: FlowManager
    _context_manager: ContextManager
    _library_manager: LibraryManager
    _model_manager: ModelManager
    _access_manager: AccessManager
    _workflow_manager: WorkflowManager
    _workflow_variables_manager: VariablesManager
    _arbitrary_code_exec_manager: ArbitraryCodeExecManager
    _operation_depth_manager: OperationDepthManager
    _static_files_manager: StaticFilesManager
    _agent_manager: AgentManager
    _version_compatibility_manager: VersionCompatibilityManager
    _session_manager: SessionManager
    _engine_identity_manager: EngineIdentityManager
    _mcp_manager: MCPManager
    _resource_manager: ResourceManager
    _sync_manager: SyncManager
    _user_manager: UserManager
    _project_manager: ProjectManager
    _artifact_manager: ArtifactManager
    _manifest_manager: ManifestManager
    _worker_manager: WorkerManager

    def __init__(self) -> None:  # noqa: PLR0915
        from griptape_nodes.retained_mode.managers.access_manager import AccessManager
        from griptape_nodes.retained_mode.managers.agent_manager import AgentManager
        from griptape_nodes.retained_mode.managers.arbitrary_code_exec_manager import (
            ArbitraryCodeExecManager,
        )
        from griptape_nodes.retained_mode.managers.artifact_manager import ArtifactManager
        from griptape_nodes.retained_mode.managers.config_manager import ConfigManager
        from griptape_nodes.retained_mode.managers.context_manager import ContextManager
        from griptape_nodes.retained_mode.managers.engine_identity_manager import EngineIdentityManager
        from griptape_nodes.retained_mode.managers.event_manager import EventManager
        from griptape_nodes.retained_mode.managers.flow_manager import FlowManager
        from griptape_nodes.retained_mode.managers.library_manager import LibraryManager
        from griptape_nodes.retained_mode.managers.manifest_manager import ManifestManager
        from griptape_nodes.retained_mode.managers.mcp_manager import MCPManager
        from griptape_nodes.retained_mode.managers.model_manager import ModelManager
        from griptape_nodes.retained_mode.managers.node_manager import NodeManager
        from griptape_nodes.retained_mode.managers.object_manager import ObjectManager
        from griptape_nodes.retained_mode.managers.operation_manager import (
            OperationDepthManager,
        )
        from griptape_nodes.retained_mode.managers.os_manager import OSManager
        from griptape_nodes.retained_mode.managers.project_manager import ProjectManager
        from griptape_nodes.retained_mode.managers.resource_manager import ResourceManager
        from griptape_nodes.retained_mode.managers.secrets_manager import SecretsManager
        from griptape_nodes.retained_mode.managers.session_manager import SessionManager
        from griptape_nodes.retained_mode.managers.static_files_manager import (
            StaticFilesManager,
        )
        from griptape_nodes.retained_mode.managers.sync_manager import SyncManager
        from griptape_nodes.retained_mode.managers.user_manager import UserManager
        from griptape_nodes.retained_mode.managers.variable_manager import (
            VariablesManager,
        )
        from griptape_nodes.retained_mode.managers.version_compatibility_manager import (
            VersionCompatibilityManager,
        )
        from griptape_nodes.retained_mode.managers.worker_manager import WorkerManager
        from griptape_nodes.retained_mode.managers.workflow_manager import (
            WorkflowManager,
        )

        self._event_manager = EventManager(engine=self)
        self._resource_manager = ResourceManager(self._event_manager)
        self._config_manager = ConfigManager(self._event_manager, engine=self)

        # Move ffmpeg's lock file and downloaded binaries out of `static_ffmpeg`'s own package
        # directory, which is read-only when the engine runs from a packaged app (notably the
        # Linux AppImage's FUSE mount). Has to happen after ConfigManager so
        # GTN_CONFIG_FFMPEG_DIRECTORY is honored, and before any node resolves ffmpeg -- which
        # can only happen once boot is complete. See utils/ffmpeg_cache.py.
        redirect_ffmpeg_cache(
            resolve_ffmpeg_directory(self._config_manager.get_config_value("ffmpeg_directory", default=""))
        )

        self._os_manager = OSManager(self._event_manager, engine=self)
        self._secrets_manager = SecretsManager(self._config_manager, self._event_manager)
        self._object_manager = ObjectManager(self._event_manager, engine=self)
        self._node_manager = NodeManager(self._event_manager, engine=self)
        self._flow_manager = FlowManager(self._event_manager, engine=self)
        self._context_manager = ContextManager(self._event_manager, engine=self)
        self._worker_manager = WorkerManager(engine=self, event_manager=self._event_manager)
        self._library_manager = LibraryManager(self._event_manager, worker_manager=self._worker_manager, engine=self)
        self._model_manager = ModelManager(self._event_manager, engine=self)
        self._access_manager = AccessManager(self._event_manager, engine=self)
        self._workflow_manager = WorkflowManager(self._event_manager, engine=self)
        self._workflow_variables_manager = VariablesManager(self._event_manager, engine=self)
        self._arbitrary_code_exec_manager = ArbitraryCodeExecManager(self._event_manager)
        self._operation_depth_manager = OperationDepthManager(self._config_manager)
        self._static_files_manager = StaticFilesManager(
            self._config_manager, self._secrets_manager, self._event_manager, engine=self
        )
        self._agent_manager = AgentManager(self._static_files_manager, self._event_manager, engine=self)
        self._version_compatibility_manager = VersionCompatibilityManager(self._event_manager, engine=self)
        self._engine_identity_manager = EngineIdentityManager(self._event_manager)
        self._session_manager = SessionManager(self._engine_identity_manager, self._event_manager)
        self._mcp_manager = MCPManager(self._event_manager, self._config_manager)
        self._sync_manager = SyncManager(self._event_manager, self._config_manager, engine=self)
        self._user_manager = UserManager(self._secrets_manager)
        self._project_manager = ProjectManager(
            self._event_manager, self._config_manager, self._secrets_manager, engine=self
        )
        self._artifact_manager = ArtifactManager(self._event_manager, engine=self)
        self._manifest_manager = ManifestManager(self._event_manager, engine=self)

        # Assign handlers now that these are created.
        self._event_manager.assign_manager_to_request_type(GetEngineVersionRequest, self.handle_engine_version_request)
        self._event_manager.assign_manager_to_request_type(EngineHeartbeatRequest, self.handle_engine_heartbeat_request)

    @property
    def event_manager(self) -> EventManager:
        return self._event_manager

    @property
    def os_manager(self) -> OSManager:
        return self._os_manager

    @property
    def config_manager(self) -> ConfigManager:
        return self._config_manager

    @property
    def secrets_manager(self) -> SecretsManager:
        return self._secrets_manager

    @property
    def object_manager(self) -> ObjectManager:
        return self._object_manager

    @property
    def node_manager(self) -> NodeManager:
        return self._node_manager

    @property
    def flow_manager(self) -> FlowManager:
        return self._flow_manager

    @property
    def context_manager(self) -> ContextManager:
        return self._context_manager

    @property
    def library_manager(self) -> LibraryManager:
        return self._library_manager

    @property
    def model_manager(self) -> ModelManager:
        return self._model_manager

    @property
    def access_manager(self) -> AccessManager:
        return self._access_manager

    @property
    def workflow_manager(self) -> WorkflowManager:
        return self._workflow_manager

    @property
    def variables_manager(self) -> VariablesManager:
        return self._workflow_variables_manager

    @property
    def arbitrary_code_exec_manager(self) -> ArbitraryCodeExecManager:
        return self._arbitrary_code_exec_manager

    @property
    def operation_depth_manager(self) -> OperationDepthManager:
        return self._operation_depth_manager

    @property
    def static_files_manager(self) -> StaticFilesManager:
        return self._static_files_manager

    @property
    def agent_manager(self) -> AgentManager:
        return self._agent_manager

    @property
    def version_compatibility_manager(self) -> VersionCompatibilityManager:
        return self._version_compatibility_manager

    @property
    def session_manager(self) -> SessionManager:
        return self._session_manager

    @property
    def engine_identity_manager(self) -> EngineIdentityManager:
        return self._engine_identity_manager

    @property
    def mcp_manager(self) -> MCPManager:
        return self._mcp_manager

    @property
    def resource_manager(self) -> ResourceManager:
        return self._resource_manager

    @property
    def sync_manager(self) -> SyncManager:
        return self._sync_manager

    @property
    def user_manager(self) -> UserManager:
        return self._user_manager

    @property
    def project_manager(self) -> ProjectManager:
        return self._project_manager

    @property
    def artifact_manager(self) -> ArtifactManager:
        return self._artifact_manager

    @property
    def manifest_manager(self) -> ManifestManager:
        return self._manifest_manager

    @property
    def worker_manager(self) -> WorkerManager:
        return self._worker_manager

    # Node libraries and saved workflows do `app = GriptapeNodes()` and then call these
    # PascalCase accessors on the result. `GriptapeNodes()` hands back an `Engine`, so
    # the compat surface has to live here. Engine-internal code uses the snake_case
    # properties above; nothing in this repo should call these.

    def EventManager(self) -> EventManager:
        return self._event_manager

    def LibraryManager(self) -> LibraryManager:
        return self._library_manager

    def ModelManager(self) -> ModelManager:
        return self._model_manager

    def AccessManager(self) -> AccessManager:
        return self._access_manager

    def ObjectManager(self) -> ObjectManager:
        return self._object_manager

    def FlowManager(self) -> FlowManager:
        return self._flow_manager

    def NodeManager(self) -> NodeManager:
        return self._node_manager

    def ContextManager(self) -> ContextManager:
        return self._context_manager

    def WorkflowManager(self) -> WorkflowManager:
        return self._workflow_manager

    def ArbitraryCodeExecManager(self) -> ArbitraryCodeExecManager:
        return self._arbitrary_code_exec_manager

    def ConfigManager(self) -> ConfigManager:
        return self._config_manager

    def OSManager(self) -> OSManager:
        return self._os_manager

    def SecretsManager(self) -> SecretsManager:
        return self._secrets_manager

    def OperationDepthManager(self) -> OperationDepthManager:
        return self._operation_depth_manager

    def StaticFilesManager(self) -> StaticFilesManager:
        return self._static_files_manager

    def AgentManager(self) -> AgentManager:
        return self._agent_manager

    def VersionCompatibilityManager(self) -> VersionCompatibilityManager:
        return self._version_compatibility_manager

    def SessionManager(self) -> SessionManager:
        return self._session_manager

    def MCPManager(self) -> MCPManager:
        return self._mcp_manager

    def EngineIdentityManager(self) -> EngineIdentityManager:
        return self._engine_identity_manager

    def ResourceManager(self) -> ResourceManager:
        return self._resource_manager

    def SyncManager(self) -> SyncManager:
        return self._sync_manager

    def VariablesManager(self) -> VariablesManager:
        return self._workflow_variables_manager

    def UserManager(self) -> UserManager:
        return self._user_manager

    def ProjectManager(self) -> ProjectManager:
        return self._project_manager

    def ArtifactManager(self) -> ArtifactManager:
        return self._artifact_manager

    def ManifestManager(self) -> ManifestManager:
        return self._manifest_manager

    def WorkerManager(self) -> WorkerManager:
        return self._worker_manager

    def get_instance(self) -> Engine:
        """Back-compat for callers that reach for the instance through an instance."""
        return self

    def handle_request(self, request: RequestPayload) -> ResultPayload:
        """Synchronous request handler."""
        event_mgr = self._event_manager

        try:
            result_event = event_mgr.handle_request(request=request)
            # Only queue result event if broadcasting is enabled and not suppressed
            if request.broadcast_result and not event_mgr.should_suppress_event(result_event):
                event_mgr.put_event(GriptapeNodeEvent(wrapped_event=result_event))
        except Exception as e:
            logger.exception(
                "Unhandled exception while processing request of type %s. "
                "Consider saving your work and restarting the engine if issues persist."
                "Request: %s",
                type(request).__name__,
                request,
            )
            return ResultPayloadFailure(
                exception=e, result_details=f"Unhandled exception while processing {type(request).__name__}: {e}"
            )
        else:
            return result_event.result

    async def ahandle_request(self, request: RequestPayload) -> ResultPayload:
        """Asynchronous request handler.

        Args:
            request: The request payload to handle.
        """
        event_mgr = self._event_manager

        try:
            result_event = await event_mgr.ahandle_request(request=request)
            # Only queue result event if broadcasting is enabled and not suppressed
            if request.broadcast_result and not event_mgr.should_suppress_event(result_event):
                await event_mgr.aput_event(GriptapeNodeEvent(wrapped_event=result_event))
        except Exception as e:
            logger.exception(
                "Unhandled exception while processing async request of type %s. "
                "Consider saving your work and restarting the engine if issues persist."
                "Request: %s",
                type(request).__name__,
                request,
            )
            return ResultPayloadFailure(
                exception=e, result_details=f"Unhandled exception while processing async {type(request).__name__}: {e}"
            )
        else:
            return result_event.result

    def broadcast_app_event(self, app_event: AppPayload) -> None:
        self._event_manager.broadcast_app_event(app_event)

    async def abroadcast_app_event(self, app_event: AppPayload) -> None:
        await self._event_manager.abroadcast_app_event(app_event)

    def get_session_id(self) -> str | None:
        return self._session_manager.active_session_id

    def get_engine_id(self) -> str | None:
        return self._engine_identity_manager.active_engine_id

    def clear_current_workflow_data(self) -> None:  # noqa: C901
        """Tear down the active workflow: cancel running flows, delete its orphan flows, then pop it.

        Requires an active workflow on the ContextManager stack. Order matters:
        `on_delete_flow_request` pushes a flow context via `ContextManager().flow(...)`,
        which raises `NoActiveWorkflowError` if the workflow has already been popped.
        So we delete flows first and pop the workflow last.
        """
        context_manager = self._context_manager
        if not context_manager.has_current_workflow():
            msg = "Cannot clear current workflow data without an active workflow context."
            raise RuntimeError(msg)

        # Cancel any running flow so the delete path doesn't race with execution.
        flow_manager = self._flow_manager
        for flow_name in self._object_manager.get_filtered_subset(type=ControlFlow):
            if flow_manager.check_for_existing_running_flow():
                self.handle_request(CancelFlowRequest(flow_name=flow_name))

        # Delete all orphan (top-level) flows. We can't rely on
        # `ListFlowsInCurrentContextRequest` here because the caller may only have a
        # workflow on the context stack (no child flow), and that request requires a
        # current flow. Instead, repeatedly find a flow with no parent and delete it;
        # `on_delete_flow_request` cascades into its children and nodes.
        more_flows = True
        while more_flows:
            flows = self._object_manager.get_filtered_subset(type=ControlFlow)
            found_orphan = False
            for flow_name in flows:
                parent = flow_manager.get_parent_flow(flow_name)
                if not parent:
                    self.handle_request(DeleteFlowRequest(flow_name=flow_name))
                    found_orphan = True
                    break
            if not flows or not found_orphan:
                more_flows = False

        # Drain this workflow's context substack before popping the workflow itself.
        while context_manager.has_current_flow():
            while context_manager.has_current_node():
                while context_manager.has_current_element():
                    context_manager.pop_element()
                context_manager.pop_node()
            context_manager.pop_flow()
        context_manager.pop_workflow()

    def handle_engine_version_request(self, request: GetEngineVersionRequest) -> ResultPayload:  # noqa: ARG002
        try:
            engine_ver = semver.VersionInfo.parse(engine_version)
            return GetEngineVersionResultSuccess(
                major=engine_ver.major,
                minor=engine_ver.minor,
                patch=engine_ver.patch,
                result_details="Engine version retrieved successfully.",
            )
        except Exception as err:
            details = f"Attempted to get engine version. Failed due to '{err}'."
            logger.error(details)
            return GetEngineVersionResultFailure(result_details=details)

    def handle_engine_heartbeat_request(self, request: EngineHeartbeatRequest) -> ResultPayload:
        """Handle engine heartbeat requests.

        Returns engine status information including version, session state, and system metrics.
        """
        try:
            # Get instance information based on environment variables
            instance_info = self._get_instance_info()

            # Get current workflow information
            workflow_info = self._get_current_workflow_info()

            # Get engine name
            engine_name = self._engine_identity_manager.engine_name

            # Get user and organization
            user = self._user_manager.user
            user_organization = self._user_manager.user_organization

            return EngineHeartbeatResultSuccess(
                heartbeat_id=request.heartbeat_id,
                engine_version=engine_version,
                engine_name=engine_name,
                engine_id=self._engine_identity_manager.active_engine_id,
                session_id=self._session_manager.active_session_id,
                timestamp=datetime.now(tz=UTC).isoformat(),
                user=user,
                user_organization=user_organization,
                is_initializing=self._library_manager.is_initializing(),
                # Set on worker processes (injected at spawn by WorkerManager), unset on the
                # orchestrator. Lets a discovery client tell workers apart and nest them.
                orchestrator_engine_id=os.getenv("GTN_ORCHESTRATOR_ENGINE_ID"),
                result_details="Engine heartbeat successful",
                **instance_info,
                **workflow_info,
            )
        except Exception as err:
            details = f"Failed to handle engine heartbeat: {err}"
            logger.error(details)
            return EngineHeartbeatResultFailure(heartbeat_id=request.heartbeat_id, result_details=details)

    def _get_instance_info(self) -> dict[str, str | None]:
        """Get instance information from environment variables.

        Returns instance type, region, provider, and public IP information if available.
        """
        instance_info: dict[str, str | None] = {
            "instance_type": os.getenv("GTN_INSTANCE_TYPE"),
            "instance_region": os.getenv("GTN_INSTANCE_REGION"),
            "instance_provider": os.getenv("GTN_INSTANCE_PROVIDER"),
        }

        # Determine deployment type based on presence of instance environment variables
        instance_info["deployment_type"] = "griptape_hosted" if any(instance_info.values()) else "local"

        return instance_info

    def _get_current_workflow_info(self) -> dict[str, Any]:
        """Get information about the currently loaded workflow.

        Returns workflow name, file path, and status information if available.
        """
        workflow_info = {
            "current_workflow": None,
            "workflow_file_path": None,
            "has_active_flow": False,
        }

        try:
            context_manager = self._context_manager

            # Check if there's an active workflow
            if context_manager.has_current_workflow():
                workflow_name = context_manager.get_current_workflow_name()
                workflow_info["current_workflow"] = workflow_name
                workflow_info["has_active_flow"] = context_manager.has_current_flow()

                # Get workflow file path from registry (None for unsaved workflows).
                if WorkflowRegistry.has_workflow_with_name(workflow_name):
                    workflow = WorkflowRegistry.get_workflow_by_name(workflow_name)
                    if workflow.file_path is not None:
                        absolute_path = WorkflowRegistry.get_complete_file_path(workflow.file_path)
                        workflow_info["workflow_file_path"] = absolute_path

        except Exception as err:
            logger.warning("Failed to get current workflow info: %s", err)

        return workflow_info


def current_engine() -> Engine:
    """Return the engine bound to this context, creating the process root on first use.

    Prefer an injected `Engine` reference. Reach for this only where no reference can be
    threaded through -- the `GriptapeNodes` facade, saved workflow files, and node
    libraries.
    """
    global _root_engine, _root_engine_building  # noqa: PLW0603

    scoped = _scoped_engine.get()
    if scoped is not None:
        return scoped
    if _root_engine is not None:
        return _root_engine

    with _root_engine_lock:
        # Another thread may have finished the build while this one waited for the lock.
        if _root_engine is not None:
            return _root_engine
        if _root_engine_building:
            msg = (
                "Something asked for the current engine while that engine was still being built. "
                "A manager's constructor is reaching for the engine instead of using the one "
                "passed to it."
            )
            raise RuntimeError(msg)
        _root_engine_building = True
        try:
            _root_engine = Engine()
        finally:
            _root_engine_building = False
        return _root_engine


def has_current_engine() -> bool:
    """Whether an engine exists yet, without creating one.

    Lets boot-time and broadcast code skip work when no engine has been built, which is
    the normal state in unit tests that construct managers on their own.
    """
    return _scoped_engine.get() is not None or _root_engine is not None


@contextmanager
def engine_scope(engine: Engine | None = None) -> Iterator[Engine]:
    """Bind `engine` (or a fresh one) as the current engine for the duration of the block.

    Tests use this to get a clean object graph without mutating process-wide state.
    asyncio tasks created inside the block inherit the binding.
    """
    if engine is None:
        engine = Engine()
    token = _scoped_engine.set(engine)
    try:
        yield engine
    finally:
        _scoped_engine.reset(token)


def reset_root_engine() -> None:
    """Drop the process root engine so the next `current_engine()` builds a fresh one.

    Test-only. Lets a test suite hand each test a clean object graph without paying to
    construct an engine the test may never touch. Prefer `engine_scope` when a test wants
    an engine it can hold.
    """
    global _root_engine  # noqa: PLW0603

    with _root_engine_lock:
        _root_engine = None
