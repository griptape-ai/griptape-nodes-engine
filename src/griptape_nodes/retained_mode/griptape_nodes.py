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

from griptape_nodes.common.strict_mode import STRICT_MODE, StrictModeScopeKind
from griptape_nodes.common.strict_mode_checks import RULES
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
    from griptape_nodes.retained_mode.managers.budget_manager import BudgetManager
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


def _check_manager_access_during_execution(manager_name: str, request_hint: str) -> None:
    """Report node code reaching for a manager while a node runs, and refuse it in a worker process.

    A worker's copy of this state can diverge from the real one, so a read that looks local is
    answered by a process that is not the source of truth. Node code gets there with
    ``GriptapeNodes.handle_request(...)``, which routes to whichever process owns that state:
    the orchestrator for what it owns, this worker for the workspace it shares on disk. That is
    why the filesystem requests named below are answered locally and are still the right call --
    the routing decision is the point, not the round trip. In-process the manager's answer is
    right, so the access is reported (``state-read-bypassing-requests``) rather than refused, for
    the author to fix before the node runs in a worker.

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
    if not engine.event_manager.in_node_execution():
        return
    # A worker loading its copy of a node answers everything from its own graph, which its own managers
    # hold. Decided per chain, as the load is: the execution count is for the whole process, so it is
    # also set while another node runs.
    if engine.event_manager.rebuilding_node_name() is not None:
        return
    scope = STRICT_MODE.current_scope()
    if scope is not None and scope.kind is StrictModeScopeKind.RUNTIME_EXECUTE:
        rule = RULES["state-read-bypassing-requests"]
        STRICT_MODE.report(
            rule_id=rule.rule_id,
            message=rule.render(node_name=scope.subject, manager_name=manager_name, request_hint=request_hint),
        )
    if not engine.library_manager.is_worker:
        return
    msg = (
        f"Attempted to use {manager_name} while running in an isolated library process, whose "
        f"copy of that state is not the source of truth. Use "
        f"GriptapeNodes.handle_request({request_hint}) instead: a request is routed to whichever "
        f"process owns that state, so the answer is authoritative wherever it is served."
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
        _check_manager_access_during_execution(
            "EventManager",
            "your node's own APIs (append_value_to_parameter, publish_update_to_parameter), whose events forward on their own",
        )
        return current_engine().event_manager

    @classmethod
    def LibraryManager(cls) -> LibraryManager:
        _check_manager_access_during_execution(
            "LibraryManager", "ListRegisteredLibrariesRequest(...) / GetNodeMetadataFromLibraryRequest(...)"
        )
        return current_engine().library_manager

    @classmethod
    def ModelManager(cls) -> ModelManager:
        _check_manager_access_during_execution("ModelManager", "ListModelDownloadsRequest(...)")
        return current_engine().model_manager

    @classmethod
    def AccessManager(cls) -> AccessManager:
        _check_manager_access_during_execution("AccessManager", "QueryModelAccessForNodeRequest(...)")
        return current_engine().access_manager

    @classmethod
    def ObjectManager(cls) -> ObjectManager:
        _check_manager_access_during_execution(
            "ObjectManager", "a request against the orchestrator's graph (e.g., RenameObjectRequest(...))"
        )
        return current_engine().object_manager

    @classmethod
    def FlowManager(cls) -> FlowManager:
        _check_manager_access_during_execution(
            "FlowManager", "flow requests (e.g., ListNodesInFlowRequest(...), ListConnectionsForNodeRequest(...))"
        )
        return current_engine().flow_manager

    @classmethod
    def NodeManager(cls) -> NodeManager:
        _check_manager_access_during_execution(
            "NodeManager", "node requests (e.g., GetNodeResolutionStateRequest(...), AddParameterToNodeRequest(...))"
        )
        return current_engine().node_manager

    @classmethod
    def ContextManager(cls) -> ContextManager:
        _check_manager_access_during_execution(
            "ContextManager", "GetCurrentProjectRequest(...) for the project; flow context belongs to the orchestrator"
        )
        return current_engine().context_manager

    @classmethod
    def WorkflowManager(cls) -> WorkflowManager:
        _check_manager_access_during_execution("WorkflowManager", "workflow requests (e.g., ListWorkflowsRequest(...))")
        return current_engine().workflow_manager

    @classmethod
    def ArbitraryCodeExecManager(cls) -> ArbitraryCodeExecManager:
        _check_manager_access_during_execution("ArbitraryCodeExecManager", "RunArbitraryPythonStringRequest(...)")
        return current_engine().arbitrary_code_exec_manager

    @classmethod
    def ConfigManager(cls) -> ConfigManager:
        _check_manager_access_during_execution("ConfigManager", "GetConfigValueRequest(...)")
        return current_engine().config_manager

    @classmethod
    def OSManager(cls) -> OSManager:
        # Files are the exception to the reason this guard exists: a worker shares the workspace
        # on disk, so its own answer is right, and ReadFileRequest / WriteFileRequest are
        # answered locally rather than forwarded. Going through the request still matters --
        # paths get canonicalized and the OS boundary's policies apply -- so the guard stays,
        # but it must not claim the orchestrator is what makes the value correct.
        _check_manager_access_during_execution(
            "OSManager",
            "ReadFileRequest(...) / WriteFileRequest(...), which canonicalize the path and apply the write policy",
        )
        return current_engine().os_manager

    @classmethod
    def SecretsManager(cls) -> SecretsManager:
        _check_manager_access_during_execution("SecretsManager", "GetSecretValueRequest(...)")
        return current_engine().secrets_manager

    @classmethod
    def OperationDepthManager(cls) -> OperationDepthManager:
        _check_manager_access_during_execution(
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
        _check_manager_access_during_execution("AgentManager", "agent requests (e.g., RunAgentRequest(...))")
        return current_engine().agent_manager

    @classmethod
    def VersionCompatibilityManager(cls) -> VersionCompatibilityManager:
        _check_manager_access_during_execution("VersionCompatibilityManager", "GetEngineVersionRequest(...)")
        return current_engine().version_compatibility_manager

    @classmethod
    def SessionManager(cls) -> SessionManager:
        _check_manager_access_during_execution(
            "SessionManager", "session state belongs to the orchestrator; there is no node-facing request"
        )
        return current_engine().session_manager

    @classmethod
    def MCPManager(cls) -> MCPManager:
        _check_manager_access_during_execution("MCPManager", "MCP requests (e.g., ListMCPServersRequest(...))")
        return current_engine().mcp_manager

    @classmethod
    def EngineIdentityManager(cls) -> EngineIdentityManager:
        _check_manager_access_during_execution("EngineIdentityManager", "GetEngineNameRequest(...)")
        return current_engine().engine_identity_manager

    @classmethod
    def ResourceManager(cls) -> ResourceManager:
        _check_manager_access_during_execution("ResourceManager", "resource requests (e.g., ListResourcesRequest(...))")
        return current_engine().resource_manager

    @classmethod
    def SyncManager(cls) -> SyncManager:
        _check_manager_access_during_execution(
            "SyncManager", "sync state belongs to the orchestrator; there is no node-facing request"
        )
        return current_engine().sync_manager

    @classmethod
    def VariablesManager(cls) -> VariablesManager:
        _check_manager_access_during_execution(
            "VariablesManager",
            "variable requests (e.g., GetVariableRequest(...)); {VAR} macros resolve before process() runs",
        )
        return current_engine().variables_manager

    @classmethod
    def UserManager(cls) -> UserManager:
        _check_manager_access_during_execution("UserManager", "user requests (e.g., GetUserRequest(...))")
        return current_engine().user_manager

    @classmethod
    def ProjectManager(cls) -> ProjectManager:
        _check_manager_access_during_execution(
            "ProjectManager",
            "project requests (e.g., GetCurrentProjectRequest(...)), which answer locally and correctly",
        )
        return current_engine().project_manager

    @classmethod
    def ArtifactManager(cls) -> ArtifactManager:
        _check_manager_access_during_execution(
            "ArtifactManager", "artifact requests (e.g., ExtractArtifactMetadataRequest(...)), which answer locally"
        )
        return current_engine().artifact_manager

    @classmethod
    def ManifestManager(cls) -> ManifestManager:
        _check_manager_access_during_execution("ManifestManager", "manifest requests, answered by the orchestrator")
        return current_engine().manifest_manager

    @classmethod
    def BudgetManager(cls) -> BudgetManager:
        _check_manager_access_during_execution("BudgetManager", "GetAttributionContextRequest(...)")
        return current_engine().budget_manager

    @classmethod
    def WorkerManager(cls) -> WorkerManager:
        _check_manager_access_during_execution("WorkerManager", "nothing: worker lifecycle belongs to the orchestrator")
        return current_engine().worker_manager
