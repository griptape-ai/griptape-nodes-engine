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
    """
    engine = current_engine()
    if not engine.library_manager.is_worker:
        return
    if not engine.event_manager.in_node_execution():
        return
    msg = (
        f"Attempted to use {manager_name} while running in an isolated library process, where "
        f"it holds no authoritative state. Use GriptapeNodes.handle_request({request_hint}) "
        f"instead, which asks the main engine and returns the real value."
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
        return current_engine().event_manager

    @classmethod
    def LibraryManager(cls) -> LibraryManager:
        return current_engine().library_manager

    @classmethod
    def ModelManager(cls) -> ModelManager:
        return current_engine().model_manager

    @classmethod
    def AccessManager(cls) -> AccessManager:
        return current_engine().access_manager

    @classmethod
    def ObjectManager(cls) -> ObjectManager:
        return current_engine().object_manager

    @classmethod
    def FlowManager(cls) -> FlowManager:
        return current_engine().flow_manager

    @classmethod
    def NodeManager(cls) -> NodeManager:
        return current_engine().node_manager

    @classmethod
    def ContextManager(cls) -> ContextManager:
        return current_engine().context_manager

    @classmethod
    def WorkflowManager(cls) -> WorkflowManager:
        return current_engine().workflow_manager

    @classmethod
    def ArbitraryCodeExecManager(cls) -> ArbitraryCodeExecManager:
        return current_engine().arbitrary_code_exec_manager

    @classmethod
    def ConfigManager(cls) -> ConfigManager:
        _forbid_manager_during_worker_execution("ConfigManager", "GetConfigValueRequest(...)")
        return current_engine().config_manager

    @classmethod
    def OSManager(cls) -> OSManager:
        _forbid_manager_during_worker_execution("OSManager", "ReadFileRequest(...) / WriteFileRequest(...)")
        return current_engine().os_manager

    @classmethod
    def SecretsManager(cls) -> SecretsManager:
        _forbid_manager_during_worker_execution("SecretsManager", "GetSecretValueRequest(...)")
        return current_engine().secrets_manager

    @classmethod
    def OperationDepthManager(cls) -> OperationDepthManager:
        return current_engine().operation_depth_manager

    @classmethod
    def StaticFilesManager(cls) -> StaticFilesManager:
        return current_engine().static_files_manager

    @classmethod
    def AgentManager(cls) -> AgentManager:
        return current_engine().agent_manager

    @classmethod
    def VersionCompatibilityManager(cls) -> VersionCompatibilityManager:
        return current_engine().version_compatibility_manager

    @classmethod
    def SessionManager(cls) -> SessionManager:
        return current_engine().session_manager

    @classmethod
    def MCPManager(cls) -> MCPManager:
        return current_engine().mcp_manager

    @classmethod
    def EngineIdentityManager(cls) -> EngineIdentityManager:
        return current_engine().engine_identity_manager

    @classmethod
    def ResourceManager(cls) -> ResourceManager:
        return current_engine().resource_manager

    @classmethod
    def SyncManager(cls) -> SyncManager:
        return current_engine().sync_manager

    @classmethod
    def VariablesManager(cls) -> VariablesManager:
        return current_engine().variables_manager

    @classmethod
    def UserManager(cls) -> UserManager:
        return current_engine().user_manager

    @classmethod
    def ProjectManager(cls) -> ProjectManager:
        return current_engine().project_manager

    @classmethod
    def ArtifactManager(cls) -> ArtifactManager:
        return current_engine().artifact_manager

    @classmethod
    def ManifestManager(cls) -> ManifestManager:
        return current_engine().manifest_manager

    @classmethod
    def WorkerManager(cls) -> WorkerManager:
        return current_engine().worker_manager
