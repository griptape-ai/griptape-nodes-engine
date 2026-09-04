"""Process-wide facade over the current `Engine`.

`GriptapeNodes` is the one intentionally global entry point into the engine. It exists
for callers that cannot be handed a reference:

- saved workflow `.py` files, which are generated code carrying a `schema_version`
- node libraries, which are versioned and distributed separately from the engine
- CLI commands and servers, at the point where they first reach into the engine

Engine-internal code should not use it. Managers receive an `Engine` and resolve peers
through it; see `griptape_nodes.retained_mode.engine`.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from griptape_nodes.retained_mode.engine import Engine, current_engine

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


class _EngineRootMeta(type):
    """Makes `GriptapeNodes()` resolve the current engine instead of building a facade.

    Saved workflows and node libraries call `GriptapeNodes()` and then use the result as
    the engine (`app = GriptapeNodes(); app.MCPManager()`), so instantiating the facade
    has to hand back the real object.
    """

    def __call__(cls) -> Engine:
        return current_engine()


def _forbid_manager_during_worker_execution(manager_name: str, request_hint: str) -> None:
    """Raise when node code reaches for a manager while executing in a worker process.

    A worker holds no authoritative state: config, secrets, and the filesystem all belong to
    the orchestrator, and reads that look local would be answered by a process that is not
    the source of truth. Node code gets there with ``GriptapeNodes.handle_request(...)``,
    which forwards to the orchestrator, so the value is always the real one.

    Scoped to node execution deliberately: engine boot and library load run in the worker
    too, and they legitimately need their own managers.

    Adding a manager here is only safe once EVERY engine-internal path to it is off the facade.
    The guard cannot tell library code from engine code -- both arrive as the same classmethod --
    so a manager still reached internally through the facade raises on the engine's own plumbing
    rather than on the node author it is meant to teach. That is not hypothetical: the storage
    driver read the workspace path this way, which made a worker unable to write a single file.

    Every manager accessor carries this guard except StaticFilesManager, which is a decided
    exception: a worker shares the workspace on disk, so its static-file answers are real.
    Everything else either answers from worker state that can diverge from the orchestrator's
    (silently wrong, which is worse than an error) or is orchestrator bookkeeping a node has
    no business touching mid-execution.
    """
    engine = current_engine()
    if not engine.library_manager.is_worker:
        return
    if not engine.event_manager.in_node_execution():
        return
    msg = (
        f"Attempted to use {manager_name} while running in an isolated library process, whose "
        f"copy of that state is not the source of truth. Use "
        f"GriptapeNodes.handle_request({request_hint}) instead: requests made while a node is "
        f"executing are answered by the main engine, so the value is always the real one."
    )
    raise RuntimeError(msg)


class GriptapeNodes(metaclass=_EngineRootMeta):
    """Static accessors for the current engine's managers and request handlers."""

    @classmethod
    def get_instance(cls) -> Engine:
        """Return the current engine."""
        return current_engine()

    @classmethod
    def handle_request(cls, request: RequestPayload) -> ResultPayload:
        return current_engine().handle_request(request)

    @classmethod
    async def ahandle_request(cls, request: RequestPayload) -> ResultPayload:
        return await current_engine().ahandle_request(request)

    @classmethod
    def broadcast_app_event(cls, app_event: AppPayload) -> None:
        current_engine().broadcast_app_event(app_event)

    @classmethod
    async def abroadcast_app_event(cls, app_event: AppPayload) -> None:
        await current_engine().abroadcast_app_event(app_event)

    @classmethod
    def get_session_id(cls) -> str | None:
        return current_engine().get_session_id()

    @classmethod
    def get_engine_id(cls) -> str | None:
        return current_engine().get_engine_id()

    @classmethod
    def clear_current_workflow_data(cls) -> None:
        current_engine().clear_current_workflow_data()

    @classmethod
    def EventManager(cls) -> EventManager:
        _forbid_manager_during_worker_execution(
            "EventManager",
            "your node's own APIs (append_value_to_parameter, publish_update_to_parameter), whose events forward on their own",
        )
        return current_engine().event_manager

    @classmethod
    def LibraryManager(cls) -> LibraryManager:
        _forbid_manager_during_worker_execution(
            "LibraryManager", "ListRegisteredLibrariesRequest(...) / GetNodeMetadataFromLibraryRequest(...)"
        )
        return current_engine().library_manager

    @classmethod
    def ModelManager(cls) -> ModelManager:
        _forbid_manager_during_worker_execution("ModelManager", "ListModelDownloadsRequest(...)")
        return current_engine().model_manager

    @classmethod
    def AccessManager(cls) -> AccessManager:
        _forbid_manager_during_worker_execution("AccessManager", "QueryModelAccessForNodeRequest(...)")
        return current_engine().access_manager

    @classmethod
    def ObjectManager(cls) -> ObjectManager:
        _forbid_manager_during_worker_execution(
            "ObjectManager", "a request against the orchestrator's graph (e.g., RenameObjectRequest(...))"
        )
        return current_engine().object_manager

    @classmethod
    def FlowManager(cls) -> FlowManager:
        _forbid_manager_during_worker_execution(
            "FlowManager", "flow requests (e.g., ListNodesInFlowRequest(...), ListConnectionsForNodeRequest(...))"
        )
        return current_engine().flow_manager

    @classmethod
    def NodeManager(cls) -> NodeManager:
        _forbid_manager_during_worker_execution(
            "NodeManager", "node requests (e.g., GetNodeResolutionStateRequest(...), AddParameterToNodeRequest(...))"
        )
        return current_engine().node_manager

    @classmethod
    def ContextManager(cls) -> ContextManager:
        _forbid_manager_during_worker_execution(
            "ContextManager", "GetCurrentProjectRequest(...) for the project; flow context belongs to the orchestrator"
        )
        return current_engine().context_manager

    @classmethod
    def WorkflowManager(cls) -> WorkflowManager:
        _forbid_manager_during_worker_execution(
            "WorkflowManager", "workflow requests (e.g., ListWorkflowsRequest(...))"
        )
        return current_engine().workflow_manager

    @classmethod
    def ArbitraryCodeExecManager(cls) -> ArbitraryCodeExecManager:
        _forbid_manager_during_worker_execution("ArbitraryCodeExecManager", "RunArbitraryPythonStringRequest(...)")
        return current_engine().arbitrary_code_exec_manager

    @classmethod
    def ConfigManager(cls) -> ConfigManager:
        _forbid_manager_during_worker_execution("ConfigManager", "GetConfigValueRequest(...)")
        return current_engine().config_manager

    @classmethod
    def OSManager(cls) -> OSManager:
        # Files are the exception to the reason this guard exists: a worker shares the workspace
        # on disk, so its own answer is right, and ReadFileRequest / WriteFileRequest are
        # answered locally rather than forwarded. Going through the request still matters --
        # paths get canonicalized and the OS boundary's policies apply -- so the guard stays,
        # but it must not claim the orchestrator is what makes the value correct.
        _forbid_manager_during_worker_execution(
            "OSManager",
            "ReadFileRequest(...) / WriteFileRequest(...), which canonicalize the path and apply the write policy",
        )
        return current_engine().os_manager

    @classmethod
    def SecretsManager(cls) -> SecretsManager:
        _forbid_manager_during_worker_execution("SecretsManager", "GetSecretValueRequest(...)")
        return current_engine().secrets_manager

    @classmethod
    def OperationDepthManager(cls) -> OperationDepthManager:
        _forbid_manager_during_worker_execution(
            "OperationDepthManager", "nothing: operation depth is orchestrator bookkeeping with no node-facing request"
        )
        return current_engine().operation_depth_manager

    @classmethod
    def StaticFilesManager(cls) -> StaticFilesManager:
        # Unguarded: a worker shares the workspace on disk and serves static files through the
        # orchestrator's server URL, so its StaticFilesManager answers correctly.
        return current_engine().static_files_manager

    @classmethod
    def AgentManager(cls) -> AgentManager:
        _forbid_manager_during_worker_execution("AgentManager", "agent requests (e.g., RunAgentRequest(...))")
        return current_engine().agent_manager

    @classmethod
    def VersionCompatibilityManager(cls) -> VersionCompatibilityManager:
        _forbid_manager_during_worker_execution("VersionCompatibilityManager", "GetEngineVersionRequest(...)")
        return current_engine().version_compatibility_manager

    @classmethod
    def SessionManager(cls) -> SessionManager:
        _forbid_manager_during_worker_execution(
            "SessionManager", "session state belongs to the orchestrator; there is no node-facing request"
        )
        return current_engine().session_manager

    @classmethod
    def MCPManager(cls) -> MCPManager:
        _forbid_manager_during_worker_execution("MCPManager", "MCP requests (e.g., ListMCPServersRequest(...))")
        return current_engine().mcp_manager

    @classmethod
    def EngineIdentityManager(cls) -> EngineIdentityManager:
        _forbid_manager_during_worker_execution("EngineIdentityManager", "GetEngineNameRequest(...)")
        return current_engine().engine_identity_manager

    @classmethod
    def ResourceManager(cls) -> ResourceManager:
        _forbid_manager_during_worker_execution(
            "ResourceManager", "resource requests (e.g., ListResourcesRequest(...))"
        )
        return current_engine().resource_manager

    @classmethod
    def SyncManager(cls) -> SyncManager:
        _forbid_manager_during_worker_execution(
            "SyncManager", "sync state belongs to the orchestrator; there is no node-facing request"
        )
        return current_engine().sync_manager

    @classmethod
    def VariablesManager(cls) -> VariablesManager:
        _forbid_manager_during_worker_execution(
            "VariablesManager",
            "variable requests (e.g., GetVariableRequest(...)); {VAR} macros resolve before process() runs",
        )
        return current_engine().variables_manager

    @classmethod
    def UserManager(cls) -> UserManager:
        _forbid_manager_during_worker_execution("UserManager", "user requests (e.g., GetUserRequest(...))")
        return current_engine().user_manager

    @classmethod
    def ProjectManager(cls) -> ProjectManager:
        _forbid_manager_during_worker_execution(
            "ProjectManager",
            "project requests (e.g., GetCurrentProjectRequest(...)), which answer locally and correctly",
        )
        return current_engine().project_manager

    @classmethod
    def ArtifactManager(cls) -> ArtifactManager:
        _forbid_manager_during_worker_execution(
            "ArtifactManager", "artifact requests (e.g., ExtractArtifactMetadataRequest(...)), which answer locally"
        )
        return current_engine().artifact_manager

    @classmethod
    def ManifestManager(cls) -> ManifestManager:
        _forbid_manager_during_worker_execution("ManifestManager", "manifest requests, answered by the orchestrator")
        return current_engine().manifest_manager

    @classmethod
    def WorkerManager(cls) -> WorkerManager:
        _forbid_manager_during_worker_execution(
            "WorkerManager", "nothing: worker lifecycle belongs to the orchestrator"
        )
        return current_engine().worker_manager
