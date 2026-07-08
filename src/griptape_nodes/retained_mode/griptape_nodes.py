from __future__ import annotations

import logging
import os
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
from griptape_nodes.utils.metaclasses import SingletonMeta
from griptape_nodes.utils.version_utils import engine_version

if TYPE_CHECKING:
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
    from griptape_nodes.retained_mode.managers.undo_manager import UndoManager
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


class GriptapeNodes(metaclass=SingletonMeta):
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
    _undo_manager: UndoManager

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
        from griptape_nodes.retained_mode.managers.undo_manager import UndoManager
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

        # Initialize only if our managers haven't been created yet
        if not hasattr(self, "_event_manager"):
            self._event_manager = EventManager()
            # Created early so domain managers (NodeManager, FlowManager, ...) can register their
            # undo recorders and non-undoable request types with it from their own __init__.
            self._undo_manager = UndoManager(self._event_manager)
            self._resource_manager = ResourceManager(self._event_manager)
            self._config_manager = ConfigManager(self._event_manager)
            self._os_manager = OSManager(self._event_manager)
            self._secrets_manager = SecretsManager(self._config_manager, self._event_manager)
            self._object_manager = ObjectManager(self._event_manager)
            self._node_manager = NodeManager(self._event_manager, self._undo_manager)
            self._flow_manager = FlowManager(self._event_manager, self._undo_manager)
            self._context_manager = ContextManager(self._event_manager)
            self._worker_manager = WorkerManager(griptape_nodes=self, event_manager=self._event_manager)
            self._library_manager = LibraryManager(self._event_manager, worker_manager=self._worker_manager)
            self._model_manager = ModelManager(self._event_manager)
            self._access_manager = AccessManager(self._event_manager)
            self._workflow_manager = WorkflowManager(self._event_manager)
            self._workflow_variables_manager = VariablesManager(self._event_manager)
            self._arbitrary_code_exec_manager = ArbitraryCodeExecManager(self._event_manager)
            self._operation_depth_manager = OperationDepthManager(self._config_manager)
            self._static_files_manager = StaticFilesManager(
                self._config_manager, self._secrets_manager, self._event_manager
            )
            self._agent_manager = AgentManager(self._static_files_manager, self._event_manager)
            self._version_compatibility_manager = VersionCompatibilityManager(self._event_manager)
            self._engine_identity_manager = EngineIdentityManager(self._event_manager)
            self._session_manager = SessionManager(self._engine_identity_manager, self._event_manager)
            self._mcp_manager = MCPManager(self._event_manager, self._config_manager)
            self._sync_manager = SyncManager(self._event_manager, self._config_manager)
            self._user_manager = UserManager(self._secrets_manager)
            self._project_manager = ProjectManager(self._event_manager, self._config_manager, self._secrets_manager)
            self._artifact_manager = ArtifactManager(self._event_manager)
            self._manifest_manager = ManifestManager(self._event_manager)

            # Assign handlers now that these are created.
            self._event_manager.assign_manager_to_request_type(
                GetEngineVersionRequest, self.handle_engine_version_request
            )
            self._event_manager.assign_manager_to_request_type(
                EngineHeartbeatRequest, self.handle_engine_heartbeat_request
            )

    @classmethod
    def get_instance(cls) -> GriptapeNodes:
        """Helper method to get the singleton instance."""
        return cls()

    @classmethod
    def handle_request(
        cls,
        request: RequestPayload,
    ) -> ResultPayload:
        """Synchronous request handler."""
        event_mgr = GriptapeNodes.EventManager()

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

    @classmethod
    async def ahandle_request(
        cls,
        request: RequestPayload,
    ) -> ResultPayload:
        """Asynchronous request handler.

        Args:
            request: The request payload to handle.
        """
        event_mgr = GriptapeNodes.EventManager()

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

    @classmethod
    def broadcast_app_event(cls, app_event: AppPayload) -> None:
        event_mgr = GriptapeNodes.get_instance()._event_manager
        event_mgr.broadcast_app_event(app_event)

    @classmethod
    async def abroadcast_app_event(cls, app_event: AppPayload) -> None:
        event_mgr = GriptapeNodes.get_instance()._event_manager
        await event_mgr.abroadcast_app_event(app_event)

    @classmethod
    def get_session_id(cls) -> str | None:
        return GriptapeNodes.SessionManager().active_session_id

    @classmethod
    def get_engine_id(cls) -> str | None:
        return GriptapeNodes.EngineIdentityManager().active_engine_id

    @classmethod
    def EventManager(cls) -> EventManager:
        return GriptapeNodes.get_instance()._event_manager

    @classmethod
    def LibraryManager(cls) -> LibraryManager:
        return GriptapeNodes.get_instance()._library_manager

    @classmethod
    def ModelManager(cls) -> ModelManager:
        return GriptapeNodes.get_instance()._model_manager

    @classmethod
    def AccessManager(cls) -> AccessManager:
        return GriptapeNodes.get_instance()._access_manager

    @classmethod
    def ObjectManager(cls) -> ObjectManager:
        return GriptapeNodes.get_instance()._object_manager

    @classmethod
    def FlowManager(cls) -> FlowManager:
        return GriptapeNodes.get_instance()._flow_manager

    @classmethod
    def NodeManager(cls) -> NodeManager:
        return GriptapeNodes.get_instance()._node_manager

    @classmethod
    def ContextManager(cls) -> ContextManager:
        return GriptapeNodes.get_instance()._context_manager

    @classmethod
    def WorkflowManager(cls) -> WorkflowManager:
        return GriptapeNodes.get_instance()._workflow_manager

    @classmethod
    def ArbitraryCodeExecManager(cls) -> ArbitraryCodeExecManager:
        return GriptapeNodes.get_instance()._arbitrary_code_exec_manager

    @classmethod
    def ConfigManager(cls) -> ConfigManager:
        return GriptapeNodes.get_instance()._config_manager

    @classmethod
    def OSManager(cls) -> OSManager:
        return GriptapeNodes.get_instance()._os_manager

    @classmethod
    def SecretsManager(cls) -> SecretsManager:
        return GriptapeNodes.get_instance()._secrets_manager

    @classmethod
    def OperationDepthManager(cls) -> OperationDepthManager:
        return GriptapeNodes.get_instance()._operation_depth_manager

    @classmethod
    def StaticFilesManager(cls) -> StaticFilesManager:
        return GriptapeNodes.get_instance()._static_files_manager

    @classmethod
    def AgentManager(cls) -> AgentManager:
        return GriptapeNodes.get_instance()._agent_manager

    @classmethod
    def VersionCompatibilityManager(cls) -> VersionCompatibilityManager:
        return GriptapeNodes.get_instance()._version_compatibility_manager

    @classmethod
    def SessionManager(cls) -> SessionManager:
        return GriptapeNodes.get_instance()._session_manager

    @classmethod
    def MCPManager(cls) -> MCPManager:
        return GriptapeNodes.get_instance()._mcp_manager

    @classmethod
    def EngineIdentityManager(cls) -> EngineIdentityManager:
        return GriptapeNodes.get_instance()._engine_identity_manager

    @classmethod
    def ResourceManager(cls) -> ResourceManager:
        return GriptapeNodes.get_instance()._resource_manager

    @classmethod
    def SyncManager(cls) -> SyncManager:
        return GriptapeNodes.get_instance()._sync_manager

    @classmethod
    def VariablesManager(cls) -> VariablesManager:
        return GriptapeNodes.get_instance()._workflow_variables_manager

    @classmethod
    def UserManager(cls) -> UserManager:
        return GriptapeNodes.get_instance()._user_manager

    @classmethod
    def ProjectManager(cls) -> ProjectManager:
        return GriptapeNodes.get_instance()._project_manager

    @classmethod
    def ArtifactManager(cls) -> ArtifactManager:
        return GriptapeNodes.get_instance()._artifact_manager

    @classmethod
    def ManifestManager(cls) -> ManifestManager:
        return GriptapeNodes.get_instance()._manifest_manager

    @classmethod
    def WorkerManager(cls) -> WorkerManager:
        return GriptapeNodes.get_instance()._worker_manager

    @classmethod
    def UndoManager(cls) -> UndoManager:
        return GriptapeNodes.get_instance()._undo_manager

    @classmethod
    def clear_current_workflow_data(cls) -> None:  # noqa: C901
        """Tear down the active workflow: cancel running flows, delete its orphan flows, then pop it.

        Requires an active workflow on the ContextManager stack. Order matters:
        `on_delete_flow_request` pushes a flow context via `ContextManager().flow(...)`,
        which raises `NoActiveWorkflowError` if the workflow has already been popped.
        So we delete flows first and pop the workflow last.
        """
        context_manager = GriptapeNodes.ContextManager()
        if not context_manager.has_current_workflow():
            msg = "Cannot clear current workflow data without an active workflow context."
            raise RuntimeError(msg)

        # Cancel any running flow so the delete path doesn't race with execution.
        flow_manager = GriptapeNodes.FlowManager()
        for flow_name in GriptapeNodes.ObjectManager().get_filtered_subset(type=ControlFlow):
            if flow_manager.check_for_existing_running_flow():
                GriptapeNodes.handle_request(CancelFlowRequest(flow_name=flow_name))

        # Delete all orphan (top-level) flows. We can't rely on
        # `ListFlowsInCurrentContextRequest` here because the caller may only have a
        # workflow on the context stack (no child flow), and that request requires a
        # current flow. Instead, repeatedly find a flow with no parent and delete it;
        # `on_delete_flow_request` cascades into its children and nodes.
        more_flows = True
        while more_flows:
            flows = GriptapeNodes.ObjectManager().get_filtered_subset(type=ControlFlow)
            found_orphan = False
            for flow_name in flows:
                parent = flow_manager.get_parent_flow(flow_name)
                if not parent:
                    GriptapeNodes.handle_request(DeleteFlowRequest(flow_name=flow_name))
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
            engine_name = GriptapeNodes.EngineIdentityManager().engine_name

            # Get user and organization
            user = GriptapeNodes.UserManager().user
            user_organization = GriptapeNodes.UserManager().user_organization

            return EngineHeartbeatResultSuccess(
                heartbeat_id=request.heartbeat_id,
                engine_version=engine_version,
                engine_name=engine_name,
                engine_id=GriptapeNodes.EngineIdentityManager().active_engine_id,
                session_id=GriptapeNodes.SessionManager().active_session_id,
                timestamp=datetime.now(tz=UTC).isoformat(),
                user=user,
                user_organization=user_organization,
                is_initializing=GriptapeNodes.LibraryManager().is_initializing(),
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
