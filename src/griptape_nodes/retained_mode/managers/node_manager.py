from __future__ import annotations

import asyncio
import contextlib
import copy
import logging
import pickle
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, NamedTuple, cast
from uuid import uuid4

from griptape_nodes.common.parameter_hydration import hydrate_parameter_values
from griptape_nodes.common.strict_mode import (
    STRICT_MODE,
    StrictModeScopeKind,
    StrictModeSeverity,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from griptape_nodes.node_library.library_declarations import LibraryDeclaration, NodeDeclaration
    from griptape_nodes.node_library.library_registry import LibrarySchema
    from griptape_nodes.retained_mode.managers.event_manager import EventManager
    from griptape_nodes.retained_mode.managers.worker_manager import WorkerManager
from griptape_nodes.exe_types.base_iterative_nodes import (
    BaseIterativeEndNode,
    BaseIterativeStartNode,
)
from griptape_nodes.exe_types.core_types import (
    BaseNodeElement,
    Parameter,
    ParameterContainer,
    ParameterGroup,
    ParameterList,
    ParameterMessage,
    ParameterMode,
    ParameterType,
    ParameterTypeBuiltin,
)
from griptape_nodes.exe_types.flow import ControlFlow
from griptape_nodes.exe_types.node_groups import SubflowNodeGroup
from griptape_nodes.exe_types.node_groups.base_node_group import BaseNodeGroup
from griptape_nodes.exe_types.node_types import (
    LOCAL_EXECUTION,
    PRIVATE_EXECUTION,
    BaseNode,
    ErrorProxyNode,
    NodeDependencies,
    NodeResolutionState,
    TransformedParameterValue,
    aprocess_scope,
    sanctioned_parameter_mutation,
)
from griptape_nodes.node_library.library_declarations import (
    ArbitraryPythonExecutionNodeProperty,
    LifecycleStageLibraryProperty,
    LifecycleStageNodeProperty,
    ModelProviderUsageNodeProperty,
    ModelUsageNodeProperty,
    find_model_catalog,
    resolve_node_models,
)
from griptape_nodes.node_library.library_registry import LibraryNameAndVersion, LibraryRegistry
from griptape_nodes.retained_mode.events.base_events import (
    EventRequest,
    ResultDetails,
    ResultPayload,
    ResultPayloadFailure,
)
from griptape_nodes.retained_mode.events.connection_events import (
    CreateConnectionRequest,
    CreateConnectionResultSuccess,
    DeleteConnectionRequest,
    DeleteConnectionResultFailure,
    DeleteConnectionResultSuccess,
    IncomingConnection,
    ListConnectionsForNodeRequest,
    ListConnectionsForNodeResultFailure,
    ListConnectionsForNodeResultSuccess,
    OutgoingConnection,
)
from griptape_nodes.retained_mode.events.event_converter import converter, safe_unstructure
from griptape_nodes.retained_mode.events.execution_events import (
    CancelExecuteNodeRequest,
    CancelExecuteNodeResultSuccess,
    CancelFlowRequest,
    ExecuteNodeRequest,
    ExecuteNodeResultFailure,
    ExecuteNodeResultSuccess,
    ResolveNodeRequest,
    ResolveNodeResultFailure,
    ResolveNodeResultSuccess,
    StartFlowResultFailure,
)
from griptape_nodes.retained_mode.events.flow_events import (
    ListNodesInFlowRequest,
    ListNodesInFlowResultSuccess,
)
from griptape_nodes.retained_mode.events.library_events import (
    GetLibraryMetadataRequest,
    GetLibraryMetadataResultFailure,
    GetLibraryMetadataResultSuccess,
)
from griptape_nodes.retained_mode.events.node_events import (
    AddNodesToNodeGroupRequest,
    AddNodesToNodeGroupResultFailure,
    AddNodesToNodeGroupResultSuccess,
    BatchSetNodeLockStateRequest,
    BatchSetNodeLockStateResultFailure,
    BatchSetNodeLockStateResultSuccess,
    BatchSetNodeMetadataRequest,
    BatchSetNodeMetadataResultFailure,
    BatchSetNodeMetadataResultSuccess,
    CanResetNodeToDefaultsRequest,
    CanResetNodeToDefaultsResultFailure,
    CanResetNodeToDefaultsResultSuccess,
    CreateNodeRequest,
    CreateNodeResultFailure,
    CreateNodeResultSuccess,
    DeleteNodeRequest,
    DeleteNodeResultFailure,
    DeleteNodeResultSuccess,
    DeserializeNodeFromCommandsRequest,
    DeserializeNodeFromCommandsResultFailure,
    DeserializeNodeFromCommandsResultSuccess,
    DeserializeSelectedNodesFromCommandsRequest,
    DeserializeSelectedNodesFromCommandsResultFailure,
    DeserializeSelectedNodesFromCommandsResultSuccess,
    DuplicateSelectedNodesRequest,
    DuplicateSelectedNodesResultFailure,
    DuplicateSelectedNodesResultSuccess,
    GetAllNodeInfoRequest,
    GetAllNodeInfoResultFailure,
    GetAllNodeInfoResultSuccess,
    GetFlowForNodeRequest,
    GetFlowForNodeResultFailure,
    GetFlowForNodeResultSuccess,
    GetNodeMetadataRequest,
    GetNodeMetadataResultFailure,
    GetNodeMetadataResultSuccess,
    GetNodeResolutionStateRequest,
    GetNodeResolutionStateResultFailure,
    GetNodeResolutionStateResultSuccess,
    ListParametersOnNodeRequest,
    ListParametersOnNodeResultFailure,
    ListParametersOnNodeResultSuccess,
    MoveNodeToNewFlowRequest,
    RemoveNodeFromNodeGroupRequest,
    RemoveNodeFromNodeGroupResultFailure,
    RemoveNodeFromNodeGroupResultSuccess,
    ResetNodeToDefaultsRequest,
    ResetNodeToDefaultsResultFailure,
    ResetNodeToDefaultsResultSuccess,
    SendNodeMessageRequest,
    SendNodeMessageResultFailure,
    SendNodeMessageResultSuccess,
    SerializedNodeCommands,
    SerializedParameterValueTracker,
    SerializedSelectedNodesCommands,
    SerializeNodeToCommandsRequest,
    SerializeNodeToCommandsResultFailure,
    SerializeNodeToCommandsResultSuccess,
    SerializeSelectedNodesToCommandsRequest,
    SerializeSelectedNodesToCommandsResultSuccess,
    SetLockNodeStateRequest,
    SetLockNodeStateResultFailure,
    SetLockNodeStateResultSuccess,
    SetNodeMetadataRequest,
    SetNodeMetadataResultFailure,
    SetNodeMetadataResultSuccess,
)
from griptape_nodes.retained_mode.events.object_events import (
    RenameObjectRequest,
    RenameObjectResultSuccess,
)
from griptape_nodes.retained_mode.events.parameter_events import (
    AddParameterGroupToNodeRequest,
    AddParameterGroupToNodeResultFailure,
    AddParameterGroupToNodeResultSuccess,
    AddParameterToNodeRequest,
    AddParameterToNodeResultFailure,
    AddParameterToNodeResultSuccess,
    AlterParameterDetailsRequest,
    AlterParameterDetailsResultFailure,
    AlterParameterDetailsResultSuccess,
    AlterParameterGroupDetailsRequest,
    AlterParameterGroupDetailsResultFailure,
    AlterParameterGroupDetailsResultSuccess,
    GetCompatibleParametersRequest,
    GetCompatibleParametersResultFailure,
    GetCompatibleParametersResultSuccess,
    GetConnectionsForParameterRequest,
    GetConnectionsForParameterResultFailure,
    GetConnectionsForParameterResultSuccess,
    GetNodeElementDetailsRequest,
    GetNodeElementDetailsResultFailure,
    GetNodeElementDetailsResultSuccess,
    GetParameterDetailsRequest,
    GetParameterDetailsResultFailure,
    GetParameterDetailsResultSuccess,
    GetParameterValueRequest,
    GetParameterValueResultFailure,
    GetParameterValueResultSuccess,
    MigrateParameterRequest,
    MigrateParameterResultFailure,
    MigrateParameterResultSuccess,
    ParameterAndMode,
    RemoveParameterFromNodeRequest,
    RemoveParameterFromNodeResultFailure,
    RemoveParameterFromNodeResultSuccess,
    RenameParameterRequest,
    RenameParameterResultFailure,
    RenameParameterResultSuccess,
    ReorderParameterListItemRequest,
    ReorderParameterListItemResultFailure,
    ReorderParameterListItemResultSuccess,
    SetParameterValueRequest,
    SetParameterValueResultFailure,
    SetParameterValueResultSuccess,
)
from griptape_nodes.retained_mode.events.validation_events import (
    ValidateNodeDependenciesRequest,
    ValidateNodeDependenciesResultFailure,
    ValidateNodeDependenciesResultSuccess,
)
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
from griptape_nodes.retained_mode.managers.authorization_checkpoint import (
    AuthorizationCheckpoint,
    CheckpointAction,
    CheckpointAttribute,
    CheckpointDenial,
    CheckpointSubjectType,
)
from griptape_nodes.retained_mode.retained_mode import RetainedMode

logger = logging.getLogger("griptape_nodes")

# Sentinel for "key not present in node.parameter_values". Distinct from None
# so a legitimately-stored None does not collide with "missing".
_PARAM_MISSING = object()


class SerializedParameterValues(NamedTuple):
    """Result of serializing parameter output values.

    Attributes:
        parameter_output_values: Either raw values or UUID references if pickling was used
        unique_parameter_uuid_to_values: Dictionary of pickled values (None if no pickling needed)
    """

    parameter_output_values: dict[str, Any]
    unique_parameter_uuid_to_values: dict[Any, Any] | None


class CanResetResult(NamedTuple):
    """Result of checking if a node can be reset to defaults.

    Attributes:
        can_reset: True if the node can be reset to defaults, False otherwise
        editor_tooltip_reason: Optional explanation if node cannot be reset
    """

    can_reset: bool
    editor_tooltip_reason: str | None


@dataclass
class SerializedGroupResult:
    """Result of serializing a group node with its children.

    This dataclass is used when serializing group nodes for copy/paste operations.
    It contains the serialized group node itself, along with all implicitly selected
    child nodes and their UUIDs for deserialization remapping.

    Attributes:
        group_command: The serialized group node command (None if serialization failed)
        group_parameter_commands: Parameter value commands for the group
        child_commands: List of serialized child node commands (excluding already-selected ones)
        child_parameter_commands: Dict mapping child UUIDs to their parameter value commands
        child_uuids: List of child node UUIDs for UUID-to-name remapping during deserialization
    """

    group_command: SerializedNodeCommands | None
    group_parameter_commands: list[SerializedNodeCommands.IndirectSetParameterValueCommand]
    child_commands: list[SerializedNodeCommands]
    child_parameter_commands: dict[
        SerializedNodeCommands.NodeUUID, list[SerializedNodeCommands.IndirectSetParameterValueCommand]
    ]
    child_uuids: list[SerializedNodeCommands.NodeUUID]


class _NodeInstantiationDeniedError(Exception):
    """Raised inside node creation when the license policy denies the node type.

    Caught by the same handler path that substitutes an Error Proxy for a node
    whose library failed to load, so a denied node surfaces identically: a proxy
    carrying the missing-permission detail, or a failure result when the caller
    opted out of proxy substitution.
    """


class NodeManager:
    _name_to_parent_flow_name: dict[str, str]

    def __init__(self, event_manager: EventManager) -> None:
        self._name_to_parent_flow_name = {}

        # Stack of active node-name remap recorders. While a recorder is on the stack
        # (see record_node_name_remaps), every node created records its requested ->
        # assigned name into the top recorder. Referenced-subflow imports use this to
        # learn which nodes were auto-renamed on a name collision (e.g. a second imported
        # subflow whose 'Start Flow' became 'Start Flow_1') so they can still route
        # inputs/outputs by the original name from the workflow shape.
        self._node_name_remap_recorders: list[dict[str, str]] = []

        # Orchestrator-side: node_name → (target_request_id, worker_engine_id, worker_request_topic)
        # for ExecuteNodeRequests currently routed to a worker. Populated in
        # _execute_node_via_worker, cleared in its finally. Used by
        # cancel_worker_execution to dispatch a CancelExecuteNodeRequest to the
        # correct worker.
        self._orch_worker_requests: dict[str, tuple[str, str, str]] = {}

        # Worker-side: request_id → (asyncio.Task, BaseNode) for the aprocess
        # task currently handling an ExecuteNodeRequest. Populated in
        # _hydrate_and_run_node (which captures asyncio.current_task), cleared
        # in its finally. Used by on_cancel_execute_node_request to locate the
        # task to cancel.
        self._worker_inflight_aprocesses: dict[str, tuple[asyncio.Task, BaseNode]] = {}

        event_manager.assign_manager_to_request_type(CreateNodeRequest, self.on_create_node_request)
        event_manager.assign_manager_to_request_type(
            AddNodesToNodeGroupRequest, self.on_add_nodes_to_node_group_request
        )
        event_manager.assign_manager_to_request_type(
            RemoveNodeFromNodeGroupRequest, self.on_remove_node_from_node_group_request
        )
        event_manager.assign_manager_to_request_type(DeleteNodeRequest, self.on_delete_node_request)
        event_manager.assign_manager_to_request_type(MoveNodeToNewFlowRequest, self.on_move_node_to_new_flow_request)
        event_manager.assign_manager_to_request_type(
            GetNodeResolutionStateRequest, self.on_get_node_resolution_state_request
        )
        event_manager.assign_manager_to_request_type(GetNodeMetadataRequest, self.on_get_node_metadata_request)
        event_manager.assign_manager_to_request_type(SetNodeMetadataRequest, self.on_set_node_metadata_request)
        event_manager.assign_manager_to_request_type(
            BatchSetNodeMetadataRequest, self.on_batch_set_node_metadata_request
        )
        event_manager.assign_manager_to_request_type(
            ListConnectionsForNodeRequest, self.on_list_connections_for_node_request
        )
        event_manager.assign_manager_to_request_type(
            GetConnectionsForParameterRequest, self.on_get_connections_for_parameter_request
        )
        event_manager.assign_manager_to_request_type(
            ListParametersOnNodeRequest, self.on_list_parameters_on_node_request
        )
        event_manager.assign_manager_to_request_type(AddParameterToNodeRequest, self.on_add_parameter_to_node_request)
        event_manager.assign_manager_to_request_type(
            AddParameterGroupToNodeRequest, self.on_add_parameter_group_to_node_request
        )
        event_manager.assign_manager_to_request_type(
            AlterParameterGroupDetailsRequest, self.on_alter_parameter_group_details_request
        )
        event_manager.assign_manager_to_request_type(
            RemoveParameterFromNodeRequest, self.on_remove_parameter_from_node_request
        )
        event_manager.assign_manager_to_request_type(GetParameterDetailsRequest, self.on_get_parameter_details_request)
        event_manager.assign_manager_to_request_type(
            AlterParameterDetailsRequest, self.on_alter_parameter_details_request
        )
        event_manager.assign_manager_to_request_type(GetParameterValueRequest, self.on_get_parameter_value_request)
        event_manager.assign_manager_to_request_type(SetParameterValueRequest, self.on_set_parameter_value_request)
        event_manager.assign_manager_to_request_type(RenameParameterRequest, self.on_rename_parameter_request)
        event_manager.assign_manager_to_request_type(
            ReorderParameterListItemRequest, self.on_reorder_parameter_list_item_request
        )
        event_manager.assign_manager_to_request_type(MigrateParameterRequest, self.on_migrate_parameter_request)
        event_manager.assign_manager_to_request_type(ResolveNodeRequest, self.on_resolve_from_node_request)
        event_manager.assign_manager_to_request_type(GetAllNodeInfoRequest, self.on_get_all_node_info_request)
        event_manager.assign_manager_to_request_type(
            GetCompatibleParametersRequest, self.on_get_compatible_parameters_request
        )
        event_manager.assign_manager_to_request_type(
            ValidateNodeDependenciesRequest, self.on_validate_node_dependencies_request
        )
        event_manager.assign_manager_to_request_type(
            GetNodeElementDetailsRequest, self.on_get_node_element_details_request
        )
        event_manager.assign_manager_to_request_type(SerializeNodeToCommandsRequest, self.on_serialize_node_to_commands)
        event_manager.assign_manager_to_request_type(
            DeserializeNodeFromCommandsRequest, self.on_deserialize_node_from_commands
        )
        event_manager.assign_manager_to_request_type(
            SerializeSelectedNodesToCommandsRequest, self.on_serialize_selected_nodes_to_commands
        )
        event_manager.assign_manager_to_request_type(
            DeserializeSelectedNodesFromCommandsRequest, self.on_deserialize_selected_nodes_from_commands
        )
        event_manager.assign_manager_to_request_type(DuplicateSelectedNodesRequest, self.on_duplicate_selected_nodes)
        event_manager.assign_manager_to_request_type(SetLockNodeStateRequest, self.on_toggle_lock_node_request)
        event_manager.assign_manager_to_request_type(GetFlowForNodeRequest, self.on_get_flow_for_node_request)
        event_manager.assign_manager_to_request_type(SendNodeMessageRequest, self.on_send_node_message_request)
        event_manager.assign_manager_to_request_type(
            CanResetNodeToDefaultsRequest, self.on_can_reset_node_to_defaults_request
        )
        event_manager.assign_manager_to_request_type(ResetNodeToDefaultsRequest, self.on_reset_node_to_defaults_request)
        event_manager.assign_manager_to_request_type(
            BatchSetNodeLockStateRequest, self.on_batch_set_lock_node_state_request
        )
        event_manager.assign_manager_to_request_type(ExecuteNodeRequest, self.on_execute_node_request)
        event_manager.assign_manager_to_request_type(CancelExecuteNodeRequest, self.on_cancel_execute_node_request)

    def handle_node_rename(self, old_name: str, new_name: str) -> None:
        # Get the node itself
        node = self.get_node_by_name(old_name)
        # Get all connections for this node and update them.
        flow_name = self.get_node_parent_flow_by_name(old_name)
        flow = GriptapeNodes.FlowManager().get_flow_by_name(flow_name)
        connections = GriptapeNodes.FlowManager().get_connections()
        # Get all incoming and outgoing connections and update them.
        if old_name in connections.incoming_index:
            incoming_connections = connections.incoming_index[old_name]
            for connection_ids in incoming_connections.values():
                for connection_id in connection_ids:
                    connection = connections.connections[connection_id]
                    connection.target_node.name = new_name
            temp = connections.incoming_index.pop(old_name)
            connections.incoming_index[new_name] = temp
        if old_name in connections.outgoing_index:
            outgoing_connections = connections.outgoing_index[old_name]
            for connection_ids in outgoing_connections.values():
                for connection_id in connection_ids:
                    connection = connections.connections[connection_id]
                    connection.source_node.name = new_name
            temp = connections.outgoing_index.pop(old_name)
            connections.outgoing_index[new_name] = temp

        # Update parent group membership if node belongs to a group
        parent_group = node.parent_group
        if parent_group is not None and isinstance(parent_group, BaseNodeGroup):
            parent_group.handle_child_node_rename(old_name, new_name)

        # update the node in the flow!
        flow.remove_node(old_name)
        node.name = new_name
        flow.add_node(node)
        # Replace the old node name and its parent.
        parent = self._name_to_parent_flow_name[old_name]
        self._name_to_parent_flow_name[new_name] = parent
        del self._name_to_parent_flow_name[old_name]

    def handle_flow_rename(self, old_name: str, new_name: str) -> None:
        # Find all instances where a node had the old parent and update it to the new one.
        for node_name, parent_flow_name in self._name_to_parent_flow_name.items():
            if parent_flow_name == old_name:
                self._name_to_parent_flow_name[node_name] = new_name

    def _cleanup_node_on_failed_deserialization(self, node_name: str) -> None:
        """Clean up a node that failed during deserialization.

        This method deletes the node (which cascades to delete all connections).

        Args:
            node_name: The name of the node to delete
        """
        delete_node_request = DeleteNodeRequest(node_name=node_name)
        delete_result = GriptapeNodes.handle_request(delete_node_request)
        if delete_result.failed():
            logger.warning(
                "Failed to clean up node '%s' after deserialization failure: %s",
                node_name,
                delete_result.result_details,
            )

    def _cleanup_created_nodes(self, node_names: list[str]) -> None:
        """Clean up multiple nodes that were created during a failed deserialization.

        This method deletes all nodes (which cascades to delete all connections).

        Args:
            node_names: The list of node names to delete
        """
        for node_name in node_names:
            self._cleanup_node_on_failed_deserialization(node_name)

    @staticmethod
    def _node_checkpoint_attributes(
        *,
        node_type: str,
        node_declarations: Sequence[NodeDeclaration],
        library_declarations: Sequence[LibraryDeclaration],
    ) -> dict[str, Any]:
        """Resolve the facts a hook may gate node instantiation on.

        `id` is the node type (so a policy can match a specific node type).
        `lifecycle_stage` is the node's effective stage: its own override when
        declared, else the library stage it inherits, omitted when neither states
        one. `executes_arbitrary_code` is the node's declared flag (absent means
        False). `model_ids` / `provider_ids` / `model_families` are the catalog
        handles a node binds to (see `_node_model_checkpoint_facts`), present only
        when the node declares model usage. The engine supplies what it resolved; a
        policy reads what it wants.

        Takes declaration lists rather than a registered `Library` so the identical
        gate runs from a loaded library (node instantiation) and from a library
        schema (library-load fitness preview), before any module is imported.
        """
        attributes: dict[str, Any] = {CheckpointAttribute.ID: node_type}
        node_stage = next(
            (
                declaration.stage
                for declaration in node_declarations
                if isinstance(declaration, LifecycleStageNodeProperty)
            ),
            None,
        )
        if node_stage is not None:
            attributes[CheckpointAttribute.LIFECYCLE_STAGE] = node_stage.value
        else:
            library_stage = next(
                (
                    declaration.stage
                    for declaration in library_declarations
                    if isinstance(declaration, LifecycleStageLibraryProperty)
                ),
                None,
            )
            if library_stage is not None:
                attributes[CheckpointAttribute.LIFECYCLE_STAGE] = library_stage.value
        arbitrary = next(
            (
                declaration
                for declaration in node_declarations
                if isinstance(declaration, ArbitraryPythonExecutionNodeProperty)
            ),
            None,
        )
        attributes[CheckpointAttribute.EXECUTES_ARBITRARY_CODE] = (
            bool(arbitrary.executes_arbitrary_python) if arbitrary else False
        )
        attributes.update(
            NodeManager._node_model_checkpoint_facts(
                node_declarations=node_declarations, library_declarations=library_declarations
            )
        )
        return attributes

    @staticmethod
    def _node_model_checkpoint_facts(
        *,
        node_declarations: Sequence[NodeDeclaration],
        library_declarations: Sequence[LibraryDeclaration],
    ) -> dict[str, Any]:
        """The model facts a node binds to, resolved against the library's model catalog.

        A node declares its models via `model_usage` (specific catalog ids) or
        `model_provider_usage` (whole providers). Resolving those against the
        library's `model_catalog` yields the concrete provider/model/family handles
        a policy gates on, so a provider/family/model-specific node is denied by the
        same `InstantiateNode` checkpoint that gates lifecycle and arbitrary code
        (and its reasons join the same denial). Returns `model_ids`, `provider_ids`,
        and `model_families`, each omitted when empty. A node that declares no model
        usage contributes nothing, so non-model nodes skip catalog resolution.
        """
        declared_provider_ids = [
            provider_id
            for declaration in node_declarations
            if isinstance(declaration, ModelProviderUsageNodeProperty)
            for provider_id in declaration.provider_ids
        ]
        declared_model_ids = [
            model_id
            for declaration in node_declarations
            if isinstance(declaration, ModelUsageNodeProperty)
            for model_id in declaration.model_ids
        ]
        if not declared_provider_ids and not declared_model_ids:
            return {}

        # Directly declared handles match a policy even when the catalog is absent
        # or a provider declares no concrete models. The catalog enriches them with
        # the provider a `model_usage` id belongs to and each model's family.
        model_ids = list(declared_model_ids)
        provider_ids = list(declared_provider_ids)
        families: list[str] = []
        catalog = find_model_catalog(library_declarations)
        if catalog is not None:
            for resolved in resolve_node_models(catalog, node_declarations):
                model_ids.append(resolved.model_id)
                provider_ids.append(resolved.provider_id)
                if resolved.model.family:
                    families.append(resolved.model.family)

        facts: dict[str, Any] = {}
        if model_ids:
            facts[CheckpointAttribute.MODEL_IDS] = list(dict.fromkeys(model_ids))
        if provider_ids:
            facts[CheckpointAttribute.PROVIDER_IDS] = list(dict.fromkeys(provider_ids))
        if families:
            facts[CheckpointAttribute.MODEL_FAMILIES] = list(dict.fromkeys(families))
        return facts

    @staticmethod
    def _evaluate_node_instantiation_checkpoint(
        *,
        node_type: str,
        node_declarations: Sequence[NodeDeclaration],
        library_declarations: Sequence[LibraryDeclaration],
    ) -> CheckpointDenial | None:
        """Ask any registered authorization hook whether this node type may be instantiated.

        Single source for the `InstantiateNode` checkpoint, shared by the
        instantiation path (CreateNode) and the library-load fitness preview so both
        resolve the same action and facts from whatever declarations they hold.
        """
        return GriptapeNodes.EventManager().evaluate_authorization_checkpoint(
            AuthorizationCheckpoint(
                action=CheckpointAction.INSTANTIATE_NODE,
                subject_type=CheckpointSubjectType.NODE_TYPE,
                subject_id=node_type,
                attributes=NodeManager._node_checkpoint_attributes(
                    node_type=node_type,
                    node_declarations=node_declarations,
                    library_declarations=library_declarations,
                ),
            )
        )

    @staticmethod
    def _evaluate_instantiation_checkpoint(
        *, node_type: str, specific_library_name: str | None
    ) -> CheckpointDenial | None:
        """Ask any registered authorization hook whether this node type may be instantiated."""
        library = LibraryRegistry.get_library_for_node_type(node_type, specific_library_name)
        return NodeManager._evaluate_node_instantiation_checkpoint(
            node_type=node_type,
            node_declarations=library.get_node_metadata(node_type).declarations,
            library_declarations=library.get_metadata().declarations,
        )

    @staticmethod
    def _enforce_instantiation_checkpoint(*, node_type: str, specific_library_name: str | None) -> None:
        """Raise `_NodeInstantiationDeniedError` when the policy denies this node type.

        Raised rather than returned so a denial joins the Error Proxy substitution
        path in `on_create_node_request`, identical to a node whose library failed
        to load. The message lists every missing permission.
        """
        denial = NodeManager._evaluate_instantiation_checkpoint(
            node_type=node_type, specific_library_name=specific_library_name
        )
        if denial is not None:
            message = denial.reason(separator="\n")
            raise _NodeInstantiationDeniedError(message)

    @staticmethod
    def evaluate_schema_node_instantiation_denials(schema: LibrarySchema) -> dict[str, CheckpointDenial]:
        """Denials that would block instantiating each node type a library schema declares.

        Lets library-load fitness preview the node-instantiation gate without
        importing the library's modules: a denied node type becomes a library
        problem now and, when later instantiated, an Error Proxy. Returns one entry
        per denied node type (class name -> denial); permitted node types are
        omitted. With no authorization hook installed every node is permitted, so
        the result is empty.
        """
        denials: dict[str, CheckpointDenial] = {}
        for node in schema.nodes:
            denial = NodeManager._evaluate_node_instantiation_checkpoint(
                node_type=node.class_name,
                node_declarations=node.metadata.declarations,
                library_declarations=schema.metadata.declarations,
            )
            if denial is not None:
                denials[node.class_name] = denial
        return denials

    @contextlib.contextmanager
    def record_node_name_remaps(self) -> Iterator[dict[str, str]]:
        """Record requested -> assigned node names for nodes created within this scope.

        Node names are globally unique, so a requested name that collides with an existing
        object is auto-renamed (``generate_name_for_object`` appends ``_1``, ``_2``, ...).
        Callers that need to correlate the names they asked for with the names that were
        actually assigned (notably referenced-subflow imports, where the same workflow can
        be imported alongside another that shares ``Start Flow`` / ``End Flow`` node names)
        wrap the work in this context manager and read the yielded mapping afterwards.

        Only the innermost recorder receives each node, so nested imports keep their
        mappings separate.
        """
        recorder: dict[str, str] = {}
        self._node_name_remap_recorders.append(recorder)
        try:
            yield recorder
        finally:
            self._node_name_remap_recorders.pop()

    def on_create_node_request(self, request: CreateNodeRequest) -> ResultPayload:  # noqa: C901, PLR0911, PLR0912, PLR0915
        # Validate as much as possible before we actually create one.
        parent_flow_name = request.override_parent_flow_name
        parent_flow = None
        if parent_flow_name is None:
            # Try to get the current context flow
            if not GriptapeNodes.ContextManager().has_current_flow():
                details = (
                    "Attempted to create Node in the Current Context. Failed because the Current Context was empty."
                )
                return CreateNodeResultFailure(result_details=details)
            parent_flow = GriptapeNodes.ContextManager().get_current_flow()
            parent_flow_name = parent_flow.name

        # Does this flow actually exist?
        if parent_flow is None:
            flow_mgr = GriptapeNodes.FlowManager()
            try:
                parent_flow = flow_mgr.get_flow_by_name(parent_flow_name)
            except KeyError as err:
                details = f"Attempted to create Node of type '{request.node_type}'. Failed when attempting to find the parent Flow. Error: {err}"
                return CreateNodeResultFailure(result_details=details)

        # Now ensure that we're giving a valid name.
        requested_node_name = request.node_name
        if requested_node_name is None:
            # The ask is to use the node's DISPLAY name if no name was specified. If that's blank, we'll use the node type.
            try:
                dest_library = LibraryRegistry.get_library_for_node_type(
                    node_type=request.node_type, specific_library_name=request.specific_library_name
                )
            except KeyError as err:
                details = f"Attempted to create Node of type '{request.node_type}'. Failed when attempting to find the library this node type was in. Error: {err}"
                return CreateNodeResultFailure(result_details=details)

            node_metadata = dest_library.get_node_metadata(request.node_type)
            requested_node_name = node_metadata.display_name
            if not requested_node_name:
                # Fall back to the class name
                requested_node_name = request.node_type

        obj_mgr = GriptapeNodes.ObjectManager()
        final_node_name = obj_mgr.generate_name_for_object(
            type_name=request.node_type, requested_name=requested_node_name
        )
        remapped_requested_node_name = (request.node_name is not None) and (request.node_name != final_node_name)

        # OK, let's try and create the Node.
        node = None
        try:
            # License-policy checkpoint: gate instantiating this node type on its
            # effective lifecycle stage and arbitrary-code flag. A denial raises
            # (from inside the helper) so it flows into the Error Proxy
            # substitution below -- the same surface a node whose library failed
            # to load uses -- and the proxy carries every missing permission.
            self._enforce_instantiation_checkpoint(
                node_type=request.node_type,
                specific_library_name=request.specific_library_name,
            )
            node = LibraryRegistry.create_node(
                name=final_node_name,
                node_type=request.node_type,
                specific_library_name=request.specific_library_name,
                metadata=request.metadata,
            )
        # modifying to exception to try to catch all possible issues with node creation.
        except Exception as err:
            logger.error(err)
            details = f"Could not create Node '{final_node_name}' of type '{request.node_type}': {err}"

            # Check if we should create an Error Proxy node instead of failing
            if request.create_error_proxy_on_failure:
                try:
                    # A policy denial is a recoverable restriction rather than a broken
                    # node, so the proxy carries the denial reason and renders as a warning
                    # instead of surfacing library-load diagnostics as a hard error.
                    denied_by_policy = isinstance(err, _NodeInstantiationDeniedError)
                    if denied_by_policy:
                        failure_reason = str(err)
                    else:
                        # Use fitness problem details if available for a more actionable error message
                        library_metadata_result = GriptapeNodes.handle_request(
                            GetLibraryMetadataRequest(library=request.specific_library_name or "")
                        )
                        if (
                            isinstance(library_metadata_result, GetLibraryMetadataResultFailure)
                            and library_metadata_result.problems is not None
                        ):
                            failure_reason = library_metadata_result.problems
                        else:
                            failure_reason = str(err)

                    # Create ErrorProxyNode directly since it needs special initialization
                    node = ErrorProxyNode(
                        name=final_node_name,
                        original_node_type=request.node_type,
                        original_library_name=request.specific_library_name or "Unknown",
                        failure_reason=failure_reason,
                        metadata=request.metadata,
                        denied_by_policy=denied_by_policy,
                    )

                    logger.warning(
                        "Created Error Proxy (placeholder) node '%s' to substitute for failed '%s'",
                        final_node_name,
                        request.node_type,
                    )
                except Exception as proxy_err:
                    details = f"Failed to create Error Proxy (placeholder) node: {proxy_err}"
                    return CreateNodeResultFailure(result_details=details)
            else:
                return CreateNodeResultFailure(result_details=details)
        # Add it to the Flow.
        parent_flow.add_node(node)

        # Record keeping.
        obj_mgr.add_object_by_name(node.name, node)
        self._name_to_parent_flow_name[node.name] = parent_flow_name

        # If a scoped operation is recording node-name remaps (e.g. a referenced-subflow
        # import), capture the requested -> assigned mapping so it can translate names that
        # collided and were auto-renamed.
        if self._node_name_remap_recorders and request.node_name is not None:
            self._node_name_remap_recorders[-1][request.node_name] = node.name

        # We don't want to start in a resolving state, bump it back to unresolved.
        state = request.resolution
        if state == NodeResolutionState.RESOLVING:
            state = NodeResolutionState.UNRESOLVED
            logger.warning(
                "Node '%s' was created in a RESOLVING state. This is not allowed. Setting to UNRESOLVED.", node.name
            )
        node.state = NodeResolutionState(state)

        # See if we want to push this into the context of the current flow.
        if request.set_as_new_context:
            GriptapeNodes.ContextManager().push_node(node=node)

        # Success message based on whether we used Current Context or explicit flow
        if request.override_parent_flow_name is None:
            details = (
                f"Successfully created Node '{final_node_name}' in the Current Context (Flow '{parent_flow_name}')"
            )
        else:
            details = f"Successfully created Node '{final_node_name}' in Flow '{parent_flow_name}'"

        log_level = logging.DEBUG
        if remapped_requested_node_name:
            details = f"{details}. Had to rename from original node name requested '{request.node_name}' as an object with this name already existed."

        # Special handling for paired classes (e.g., create a Start node and it automatically creates a corresponding End node already connected).
        if isinstance(node, BaseIterativeStartNode) and not request.initial_setup:
            # If it's StartLoop, create an EndLoop and connect it to the StartLoop.
            # Get the class name of the node
            node_class_name = node.__class__.__name__

            # Get the opposing EndNode
            # TODO: (griptape) Get paired classes implemented so we dont need to do name stuff. https://github.com/griptape-ai/griptape-nodes/issues/1549
            end_class_name = node_class_name.replace("Start", "End")

            # Check and see if the class exists
            libraries_with_node_type = LibraryRegistry.get_libraries_with_node_type(end_class_name)
            if not libraries_with_node_type:
                msg = f"Attempted to create a paired set of nodes for Node '{final_node_name}'. Failed because paired class '{end_class_name}' does not exist for start class '{node_class_name}'. The corresponding node will have to be created by hand and attached manually."
                logger.error(msg)  # while this is bad, it's not unsalvageable, so we'll consider this a success.
            else:
                # Create the EndNode
                end_loop = GriptapeNodes.handle_request(
                    CreateNodeRequest(
                        node_type=end_class_name,
                        metadata={
                            "position": {"x": node.metadata["position"]["x"] + 650, "y": node.metadata["position"]["y"]}
                        },
                        override_parent_flow_name=parent_flow_name,
                    )
                )
                if not isinstance(end_loop, CreateNodeResultSuccess):
                    msg = f"Attempted to create a paried set of nodes for Node '{final_node_name}'. Failed because paired class '{end_class_name}' failed to get created. The corresponding node will have to be created by hand and attached manually."
                    logger.error(msg)  # while this is bad, it's not unsalvageable, so we'll consider this a success.
                else:
                    # Create Loop between output and input to the start node.
                    GriptapeNodes.handle_request(
                        CreateConnectionRequest(
                            source_node_name=node.name,
                            source_parameter_name="loop",
                            target_node_name=end_loop.node_name,
                            target_parameter_name="from_start",
                        )
                    )
                    end_node = self.get_node_by_name(end_loop.node_name)
                    if not isinstance(end_node, BaseIterativeEndNode):
                        msg = f"Attempted to create a paried set of nodes for Node '{final_node_name}'. Failed because paired node '{end_loop.node_name}' was not a proper EndLoop instance. The corresponding node will have to be created by hand and attached manually."
                        logger.error(
                            msg
                        )  # while this is bad, it's not unsalvageable, so we'll consider this a success.
                    else:
                        # create the connection - only when we've confirmed correct types
                        node.end_node = end_node
                        end_node.start_node = node

        # Handle subflow_name for BaseNodeGroup nodes
        if request.subflow_name:
            if isinstance(node, BaseNodeGroup):
                # Set the subflow_name in metadata - the group will use this when creating/referencing its subflow
                if node.metadata is None:
                    node.metadata = {}
                node.metadata["subflow_name"] = request.subflow_name
            else:
                warning_details = (
                    f"Attempted to set subflow_name '{request.subflow_name}' on Node '{node.name}'. "
                    f"Failed because node is not a BaseNodeGroup."
                )
                return CreateNodeResultFailure(result_details=warning_details)

        # Handle node_names_to_add for BaseNodeGroup nodes
        if request.node_names_to_add:
            if isinstance(node, BaseNodeGroup):
                nodes_to_add = []
                for node_name in request.node_names_to_add:
                    try:
                        existing_node = self.get_node_by_name(node_name)
                        nodes_to_add.append(existing_node)
                    except KeyError:
                        warning_details = (
                            f"Attempted to add node '{node_name}' to NodeGroup '{node.name}'. "
                            f"Failed because node was not found."
                        )
                        logger.warning(warning_details)
                if nodes_to_add:
                    try:
                        node.add_nodes_to_group(nodes_to_add)
                    except Exception as err:
                        warning_msg = f"Failed to add nodes to NodeGroup '{node.name}': {err}"
                        logger.warning(warning_msg)
            else:
                warning_details = (
                    f"Attempted to add nodes '{request.node_names_to_add}' to Node '{node.name}'. "
                    f"Failed because node is not a BaseNodeGroup."
                )
                logger.warning(warning_details)

        # Handle parent_group_name: add this node to an existing group
        if request.parent_group_name:
            try:
                parent_group = self.get_node_by_name(request.parent_group_name)
                if isinstance(parent_group, BaseNodeGroup):
                    parent_group.add_nodes_to_group([node])
                else:
                    logger.warning(
                        "Attempted to add node '%s' to '%s'. Failed because it is not a BaseNodeGroup.",
                        node.name,
                        request.parent_group_name,
                    )
            except KeyError:
                logger.warning(
                    "Attempted to add node '%s' to parent group '%s'. Failed because group was not found.",
                    node.name,
                    request.parent_group_name,
                )

        return CreateNodeResultSuccess(
            node_name=node.name,
            node_type=node.__class__.__name__,
            specific_library_name=request.specific_library_name,
            parent_flow_name=parent_flow_name,
            parent_group_name=node.parent_group.name if node.parent_group else None,
            result_details=ResultDetails(message=details, level=log_level),
        )

    def _get_flow_for_node_group_operation(self, flow_name: str | None) -> AddNodesToNodeGroupResultFailure | None:
        """Get the flow for a node group operation."""
        if flow_name is None:
            if not GriptapeNodes.ContextManager().has_current_flow():
                details = "Attempted to add node to NodeGroup in the Current Context. Failed because the Current Context was empty."
                return AddNodesToNodeGroupResultFailure(result_details=details)
        else:
            try:
                GriptapeNodes.FlowManager().get_flow_by_name(flow_name)
            except KeyError as err:
                details = (
                    f"Attempted to add node to NodeGroup. Failed when attempting to find the parent Flow. Error: {err}"
                )
                return AddNodesToNodeGroupResultFailure(result_details=details)
        return None

    def _get_nodes_for_group_operation(
        self, node_names: list[str], node_group_name: str
    ) -> list[BaseNode] | AddNodesToNodeGroupResultFailure:
        """Get the list of nodes to add to a group.

        Collects all errors and returns them together if multiple nodes fail.
        """
        obj_mgr = GriptapeNodes.ObjectManager()
        nodes = []
        errors = []

        for node_name in node_names:
            try:
                node = obj_mgr.get_object_by_name(node_name)
            except KeyError:
                errors.append(f"Node '{node_name}' was not found")
                continue

            if not isinstance(node, BaseNode):
                errors.append(f"'{node_name}' is not a node")
                continue

            nodes.append(node)

        if errors:
            details = f"Attempted to add nodes to NodeGroup '{node_group_name}'. Failed for the following nodes: {'; '.join(errors)}"
            return AddNodesToNodeGroupResultFailure(result_details=details)

        return nodes

    def _get_node_group(
        self, node_group_name: str, node_names: list[str]
    ) -> BaseNodeGroup | AddNodesToNodeGroupResultFailure:
        """Get the NodeGroup node."""
        try:
            node_group = GriptapeNodes.ObjectManager().get_object_by_name(node_group_name)
        except KeyError:
            details = f"Attempted to add nodes '{node_names}' to NodeGroup '{node_group_name}'. Failed because NodeGroup was not found."
            return AddNodesToNodeGroupResultFailure(result_details=details)

        if not isinstance(node_group, BaseNodeGroup):
            details = f"Attempted to add nodes '{node_names}' to '{node_group_name}'. Failed because '{node_group_name}' is not a NodeGroup."
            return AddNodesToNodeGroupResultFailure(result_details=details)

        return node_group

    def on_add_nodes_to_node_group_request(self, request: AddNodesToNodeGroupRequest) -> ResultPayload:
        """Handle AddNodeToNodeGroupRequest to add a node to an existing NodeGroup."""
        flow_result = self._get_flow_for_node_group_operation(request.flow_name)
        if isinstance(flow_result, AddNodesToNodeGroupResultFailure):
            return flow_result

        nodes_result = self._get_nodes_for_group_operation(request.node_names, request.node_group_name)
        if isinstance(nodes_result, AddNodesToNodeGroupResultFailure):
            return nodes_result
        nodes = nodes_result

        node_group_result = self._get_node_group(request.node_group_name, request.node_names)
        if isinstance(node_group_result, AddNodesToNodeGroupResultFailure):
            return node_group_result
        node_group = node_group_result

        try:
            node_group.add_nodes_to_group(nodes)
        except Exception as err:
            details = f"Attempted to add node '{request.node_names}' to NodeGroup '{request.node_group_name}'. Failed with error: {err}"
            return AddNodesToNodeGroupResultFailure(result_details=details)

        details = f"Successfully added node '{request.node_names}' to NodeGroup '{request.node_group_name}'"
        return AddNodesToNodeGroupResultSuccess(
            result_details=ResultDetails(message=details, level=logging.DEBUG),
        )

    def _get_flow_for_remove_operation(self, flow_name: str | None) -> RemoveNodeFromNodeGroupResultFailure | None:
        """Get the flow for a remove node from group operation."""
        if flow_name is None:
            if not GriptapeNodes.ContextManager().has_current_flow():
                details = "Attempted to remove nodes from NodeGroup in the Current Context. Failed because the Current Context was empty."
                return RemoveNodeFromNodeGroupResultFailure(result_details=details)
        else:
            try:
                GriptapeNodes.FlowManager().get_flow_by_name(flow_name)
            except KeyError as err:
                details = f"Attempted to remove nodes from NodeGroup. Failed when attempting to find the parent Flow. Error: {err}"
                return RemoveNodeFromNodeGroupResultFailure(result_details=details)
        return None

    def _get_nodes_for_remove_operation(
        self, node_names: list[str], node_group_name: str
    ) -> list[BaseNode] | RemoveNodeFromNodeGroupResultFailure:
        """Get the list of nodes to remove from a group.

        Collects all errors and returns them together if multiple nodes fail.
        """
        obj_mgr = GriptapeNodes.ObjectManager()
        nodes = []
        errors = []

        for node_name in node_names:
            try:
                node = obj_mgr.get_object_by_name(node_name)
            except KeyError:
                errors.append(f"Node '{node_name}' was not found")
                continue

            if not isinstance(node, BaseNode):
                errors.append(f"'{node_name}' is not a node")
                continue

            nodes.append(node)

        if errors:
            details = f"Attempted to remove nodes from NodeGroup '{node_group_name}'. Failed for the following nodes: {'; '.join(errors)}"
            return RemoveNodeFromNodeGroupResultFailure(result_details=details)

        return nodes

    def _get_node_group_for_remove(
        self, node_group_name: str, node_names: list[str]
    ) -> BaseNodeGroup | RemoveNodeFromNodeGroupResultFailure:
        """Get the NodeGroup node for remove operation."""
        try:
            node_group = GriptapeNodes.ObjectManager().get_object_by_name(node_group_name)
        except KeyError:
            details = f"Attempted to remove nodes '{node_names}' from NodeGroup '{node_group_name}'. Failed because NodeGroup was not found."
            return RemoveNodeFromNodeGroupResultFailure(result_details=details)

        if not isinstance(node_group, BaseNodeGroup):
            details = f"Attempted to remove nodes '{node_names}' from '{node_group_name}'. Failed because '{node_group_name}' is not a NodeGroup."
            return RemoveNodeFromNodeGroupResultFailure(result_details=details)

        return node_group

    def on_remove_node_from_node_group_request(self, request: RemoveNodeFromNodeGroupRequest) -> ResultPayload:
        """Handle RemoveNodeFromNodeGroupRequest to remove nodes from an existing NodeGroup."""
        flow_result = self._get_flow_for_remove_operation(request.flow_name)
        if isinstance(flow_result, RemoveNodeFromNodeGroupResultFailure):
            return flow_result

        nodes_result = self._get_nodes_for_remove_operation(request.node_names, request.node_group_name)
        if isinstance(nodes_result, RemoveNodeFromNodeGroupResultFailure):
            return nodes_result
        nodes = nodes_result

        node_group_result = self._get_node_group_for_remove(request.node_group_name, request.node_names)
        if isinstance(node_group_result, RemoveNodeFromNodeGroupResultFailure):
            return node_group_result
        node_group = node_group_result

        try:
            node_group.remove_nodes_from_group(nodes)
        except ValueError as err:
            details = f"Attempted to remove nodes '{request.node_names}' from NodeGroup '{request.node_group_name}'. Failed with error: {err}"
            return RemoveNodeFromNodeGroupResultFailure(result_details=details)

        details = f"Successfully removed nodes '{request.node_names}' from NodeGroup '{request.node_group_name}'"
        return RemoveNodeFromNodeGroupResultSuccess(
            result_details=ResultDetails(message=details, level=logging.DEBUG),
        )

    def cancel_conditionally(
        self, parent_flow: ControlFlow, parent_flow_name: str, node: BaseNode
    ) -> ResultPayload | None:
        """Conditionally cancels a parent flow if it's currently executing nodes are connected to the specified node.

        This method checks if the parent flow is running, and if so, determines whether the currently
        executing or resolving node is connected to the specified node. If a connection exists, the parent
        flow is cancelled to prevent operations on the deleted node.

        Args:
            parent_flow: The control flow object that may need to be cancelled.
            parent_flow_name: The name of the parent flow for use in cancellation requests.
            node: The base node that is trying to be deleted.

        Returns:
            ResultPayload: A DeleteNodeResultFailure if cancellation was attempted but failed.
            None: If no cancellation was needed or cancellation succeeded.

        Note:
            This method also clears the flow queue regardless of whether cancellation occurred,
            to ensure the specified node is not processed in the future.
        """
        if GriptapeNodes.FlowManager().check_for_existing_running_flow():
            # get the current node executing / resolving
            # if it's in connected nodes, cancel flow.
            # otherwise, leave it.
            control_node_names, resolving_node_names, _ = GriptapeNodes.FlowManager().flow_state(parent_flow)
            connected_nodes = parent_flow.get_all_connected_nodes(node)
            cancelled = False
            if control_node_names is not None:
                for control_node_name in control_node_names:
                    control_node = GriptapeNodes.ObjectManager().get_object_by_name(control_node_name)
                    if control_node in connected_nodes:
                        result = GriptapeNodes.handle_request(CancelFlowRequest(flow_name=parent_flow_name))
                        cancelled = True
                        if result.failed():
                            details = f"Attempted to delete a Node '{node.name}'. Failed because running flow could not cancel."
                            return DeleteNodeResultFailure(result_details=details)
            if resolving_node_names is not None and not cancelled:
                for resolving_node_name in resolving_node_names:
                    resolving_node = GriptapeNodes.ObjectManager().get_object_by_name(resolving_node_name)
                    if resolving_node in connected_nodes:
                        result = GriptapeNodes.handle_request(CancelFlowRequest(flow_name=parent_flow_name))
                        if result.failed():
                            details = f"Attempted to delete a Node '{node.name}'. Failed because running flow could not cancel."
                            return DeleteNodeResultFailure(result_details=details)
                        break  # Only need to cancel once
            # Clear the execution queue, because we don't want to hit this node eventually.
            parent_flow.clear_execution_queue()
        return None

    def on_delete_node_request(self, request: DeleteNodeRequest) -> ResultPayload:  # noqa: C901, PLR0911, PLR0912, PLR0915 (Complex logic, lots of edge cases)
        node_name = request.node_name
        node = None
        if node_name is None:
            # Get from the current context.
            if not GriptapeNodes.ContextManager().has_current_node():
                details = (
                    "Attempted to delete a Node from the Current Context. Failed because the Current Context is empty."
                )
                return DeleteNodeResultFailure(result_details=details)

            node = GriptapeNodes.ContextManager().get_current_node()
            node_name = node.name
        if node is None:
            node = GriptapeNodes.ObjectManager().attempt_get_object_by_name_as_type(node_name, BaseNode)
        if node is None:
            details = f"Attempted to delete a Node '{node_name}', but no such Node was found."
            return DeleteNodeResultFailure(result_details=details)

        with GriptapeNodes.ContextManager().node(node=node):
            parent_flow_name = self._name_to_parent_flow_name[node_name]
            try:
                parent_flow = GriptapeNodes.FlowManager().get_flow_by_name(parent_flow_name)
            except KeyError as err:
                details = f"Attempted to delete a Node '{node_name}'. Error: {err}"
                return DeleteNodeResultFailure(result_details=details)

            cancel_result = self.cancel_conditionally(parent_flow, parent_flow_name, node)
            if cancel_result is not None:
                return cancel_result

            # Call after_node_deleted hook for cleanup of a node, implemented by node author.
            try:
                node.after_node_deleted()
            except ValueError as err:
                msg = f"Failed to delete node {node_name}, after_node_deleted method failed to run with error: {err}"
                return DeleteNodeResultFailure(result_details=msg)
            # Remove all connections from this Node using a loop to handle cascading deletions
            any_connections_remain = True
            while any_connections_remain:
                # Assume we're done
                any_connections_remain = False

                list_node_connections_request = ListConnectionsForNodeRequest(node_name=node_name)
                list_connections_result = GriptapeNodes.handle_request(request=list_node_connections_request)
                if not isinstance(list_connections_result, ListConnectionsForNodeResultSuccess):
                    details = f"Attempted to delete a Node '{node_name}'. Failed because it could not gather Connections to the Node."
                    return DeleteNodeResultFailure(result_details=details)

                # Check incoming connections
                if list_connections_result.incoming_connections:
                    any_connections_remain = True
                    connection = list_connections_result.incoming_connections[0]
                    delete_request = DeleteConnectionRequest(
                        source_node_name=connection.source_node_name,
                        source_parameter_name=connection.source_parameter_name,
                        target_node_name=node_name,
                        target_parameter_name=connection.target_parameter_name,
                    )
                    delete_result = GriptapeNodes.handle_request(delete_request)
                    if isinstance(delete_result, ResultPayloadFailure):
                        details = (
                            f"Attempted to delete a Node '{node_name}'. Failed when attempting to delete Connection."
                        )
                        return DeleteNodeResultFailure(result_details=details)
                    continue  # Refresh connection list after cascading deletions

                # Check outgoing connections
                if list_connections_result.outgoing_connections:
                    any_connections_remain = True
                    connection = list_connections_result.outgoing_connections[0]
                    delete_request = DeleteConnectionRequest(
                        source_node_name=node_name,
                        source_parameter_name=connection.source_parameter_name,
                        target_node_name=connection.target_node_name,
                        target_parameter_name=connection.target_parameter_name,
                    )
                    delete_result = GriptapeNodes.handle_request(delete_request)
                    if isinstance(delete_result, ResultPayloadFailure):
                        details = (
                            f"Attempted to delete a Node '{node_name}'. Failed when attempting to delete Connection."
                        )
                        return DeleteNodeResultFailure(result_details=details)

        # Check if it's in a node group
        if isinstance(node.parent_group, SubflowNodeGroup):
            try:
                node.parent_group.delete_nodes_from_group([node])
            except ValueError as e:
                details = f"Attempted to delete a Node '{node_name}'. Failed to remove it from the node group: {e}"
                return DeleteNodeResultFailure(result_details=details)

        parent_flow.remove_node(node.name)

        # Now remove the record keeping
        GriptapeNodes.ObjectManager().del_obj_by_name(node_name)
        del self._name_to_parent_flow_name[node_name]

        # If we were part of the Current Context, pop it.
        if request.node_name is None:
            GriptapeNodes.ContextManager().pop_node()

        details = f"Successfully deleted Node '{node_name}'."
        return DeleteNodeResultSuccess(result_details=details)

    def on_move_node_to_new_flow_request(self, request: MoveNodeToNewFlowRequest) -> ResultPayload:  # noqa: PLR0911
        """Move a node from one flow to another flow.

        Args:
            request: MoveNodeToNewFlowRequest containing node_name, target_flow_name, source_flow_name

        Returns:
            MoveNodeToNewFlowResultSuccess or MoveNodeToNewFlowResultFailure
        """
        from griptape_nodes.retained_mode.events.node_events import (
            MoveNodeToNewFlowResultFailure,
            MoveNodeToNewFlowResultSuccess,
        )

        node_name = request.node_name
        if node_name is None:
            if not GriptapeNodes.ContextManager().has_current_node():
                details = (
                    "Attempted to move a Node from the Current Context. Failed because the Current Context is empty."
                )
                return MoveNodeToNewFlowResultFailure(result_details=details)
            node = GriptapeNodes.ContextManager().get_current_node()
            node_name = node.name

        node = GriptapeNodes.ObjectManager().attempt_get_object_by_name_as_type(node_name, BaseNode)
        if node is None:
            details = f"Attempted to move Node '{node_name}', but no such Node was found."
            return MoveNodeToNewFlowResultFailure(result_details=details)

        if not request.target_flow_name:
            details = f"Attempted to move Node '{node_name}'. Failed because target_flow_name is required."
            return MoveNodeToNewFlowResultFailure(result_details=details)

        source_flow_name = request.source_flow_name
        if source_flow_name is None:
            if node_name not in self._name_to_parent_flow_name:
                details = f"Attempted to move Node '{node_name}'. Failed because Node has no parent flow."
                return MoveNodeToNewFlowResultFailure(result_details=details)
            source_flow_name = self._name_to_parent_flow_name[node_name]

        try:
            source_flow = GriptapeNodes.FlowManager().get_flow_by_name(source_flow_name)
        except KeyError:
            details = f"Attempted to move Node '{node_name}' from Flow '{source_flow_name}'. Failed because source flow was not found."
            return MoveNodeToNewFlowResultFailure(result_details=details)

        try:
            target_flow = GriptapeNodes.FlowManager().get_flow_by_name(request.target_flow_name)
        except KeyError:
            details = f"Attempted to move Node '{node_name}' to Flow '{request.target_flow_name}'. Failed because target flow was not found."
            return MoveNodeToNewFlowResultFailure(result_details=details)

        if node_name not in source_flow.nodes:
            details = f"Attempted to move Node '{node_name}' from Flow '{source_flow_name}'. Failed because Node is not in source flow."
            return MoveNodeToNewFlowResultFailure(result_details=details)

        source_flow.remove_node(node_name)
        target_flow.add_node(node)
        self._name_to_parent_flow_name[node_name] = request.target_flow_name

        details = f"Successfully moved Node '{node_name}' from Flow '{source_flow_name}' to Flow '{request.target_flow_name}'."
        return MoveNodeToNewFlowResultSuccess(
            node_name=node_name,
            source_flow_name=source_flow_name,
            target_flow_name=request.target_flow_name,
            result_details=details,
        )

    def on_get_node_resolution_state_request(self, request: GetNodeResolutionStateRequest) -> ResultPayload:
        node_name = request.node_name
        node = None
        if node_name is None:
            # Get from the current context.
            if not GriptapeNodes.ContextManager().has_current_node():
                details = "Attempted to get resolution state for a Node from the Current Context. Failed because the Current Context is empty."
                return GetNodeResolutionStateResultFailure(result_details=details)

            node = GriptapeNodes.ContextManager().get_current_node()
            node_name = node.name

        if node is None:
            # Does this node exist?
            obj_mgr = GriptapeNodes.ObjectManager()
            node = obj_mgr.attempt_get_object_by_name_as_type(node_name, BaseNode)
            if node is None:
                details = f"Attempted to get resolution state for a Node '{node_name}', but no such Node was found."
                result = GetNodeResolutionStateResultFailure(result_details=details)
                return result

        node_state = node.state

        details = f"Successfully got resolution state for Node '{node_name}'."
        result = GetNodeResolutionStateResultSuccess(state=node_state.name, result_details=details)
        return result

    def on_get_node_metadata_request(self, request: GetNodeMetadataRequest) -> ResultPayload:
        node_name = request.node_name
        node = None
        if node_name is None:
            # Get from the current context.
            if not GriptapeNodes.ContextManager().has_current_node():
                details = "Attempted to get metadata for a Node from the Current Context. Failed because the Current Context is empty."
                return GetNodeMetadataResultFailure(result_details=details)

            node = GriptapeNodes.ContextManager().get_current_node()
            node_name = node.name

        # Does this node exist?
        if node is None:
            obj_mgr = GriptapeNodes.ObjectManager()

            node = obj_mgr.attempt_get_object_by_name_as_type(node_name, BaseNode)
            if node is None:
                details = f"Attempted to get metadata for a Node '{node_name}', but no such Node was found."

                result = GetNodeMetadataResultFailure(result_details=details)
                return result

        metadata = node.metadata
        details = f"Successfully retrieved metadata for a Node '{node_name}'."
        result = GetNodeMetadataResultSuccess(metadata=metadata, result_details=details)
        return result

    def on_set_node_metadata_request(self, request: SetNodeMetadataRequest) -> ResultPayload:
        node_name = request.node_name
        node = None
        if node_name is None:
            # Get from the current context.
            if not GriptapeNodes.ContextManager().has_current_node():
                details = "Attempted to set metadata for a Node from the Current Context. Failed because the Current Context is empty."
                return SetNodeMetadataResultFailure(result_details=details)

            node = GriptapeNodes.ContextManager().get_current_node()
            node_name = node.name

        # Does this node exist?
        if node is None:
            obj_mgr = GriptapeNodes.ObjectManager()

            node = obj_mgr.attempt_get_object_by_name_as_type(node_name, BaseNode)
            if node is None:
                details = f"Attempted to set metadata for a Node '{node_name}', but no such Node was found."

                result = SetNodeMetadataResultFailure(result_details=details)
                return result

        # We can't completely overwrite metadata.
        for key, value in request.metadata.items():
            node.metadata[key] = value
        details = f"Successfully set metadata for a Node '{node_name}'."
        result = SetNodeMetadataResultSuccess(result_details=details)
        return result

    def on_batch_set_node_metadata_request(self, request: BatchSetNodeMetadataRequest) -> ResultPayload:
        updated_nodes = []
        failed_nodes = {}

        for node_name, metadata_update in request.node_metadata_updates.items():
            # Resolve node name and get node object
            node = None
            if node_name is None:
                # Get from current context
                if not GriptapeNodes.ContextManager().has_current_node():
                    failed_nodes["current_context"] = "No current context node available"
                    continue
                node = GriptapeNodes.ContextManager().get_current_node()
                actual_node_name = node.name
            else:
                actual_node_name = node_name

            # Look up node if we don't have it yet
            if node is None:
                obj_mgr = GriptapeNodes.ObjectManager()
                node = obj_mgr.attempt_get_object_by_name_as_type(actual_node_name, BaseNode)
                if node is None:
                    failed_nodes[actual_node_name] = f"Node '{actual_node_name}' not found"
                    continue

            single_request = SetNodeMetadataRequest(node_name=actual_node_name, metadata=metadata_update)
            result = self.on_set_node_metadata_request(single_request)

            if isinstance(result, SetNodeMetadataResultSuccess):
                updated_nodes.append(actual_node_name)
            else:
                failed_nodes[actual_node_name] = result.result_details

        if failed_nodes:
            return BatchSetNodeMetadataResultFailure(
                result_details=f"Failed to update any nodes. Failed nodes: {failed_nodes}"
            )

        return BatchSetNodeMetadataResultSuccess(
            updated_nodes=updated_nodes,
            failed_nodes=failed_nodes,
            result_details=f"Successfully updated metadata for {len(updated_nodes)} nodes.",
        )

    def on_list_connections_for_node_request(self, request: ListConnectionsForNodeRequest) -> ResultPayload:  # noqa: C901, PLR0912 Removed list comprehension
        node_name = request.node_name
        node = None
        if node_name is None:
            # Get from the current context.
            if not GriptapeNodes.ContextManager().has_current_node():
                details = "Attempted to list Connections for a Node from the Current Context. Failed because the Current Context is empty."
                return ListConnectionsForNodeResultFailure(result_details=details)

            node = GriptapeNodes.ContextManager().get_current_node()
            node_name = node.name

        # Does this node exist?
        if node is None:
            obj_mgr = GriptapeNodes.ObjectManager()

            node = obj_mgr.attempt_get_object_by_name_as_type(node_name, BaseNode)
            if node is None:
                details = f"Attempted to list Connections for a Node '{node_name}', but no such Node was found."

                result = ListConnectionsForNodeResultFailure(result_details=details)
                return result

        parent_flow_name = self._name_to_parent_flow_name[node_name]
        try:
            GriptapeNodes.FlowManager().get_flow_by_name(parent_flow_name)
        except KeyError as err:
            details = f"Attempted to list Connections for a Node '{node_name}'. Error: {err}"

            result = ListConnectionsForNodeResultFailure(result_details=details)
            return result

        # Kinda gross, but let's do it
        connection_mgr = GriptapeNodes.FlowManager().get_connections()
        # get outgoing connections
        outgoing_connections_list = []
        if node_name in connection_mgr.outgoing_index:
            for connection_lists in connection_mgr.outgoing_index[node_name].values():
                for connection_id in connection_lists:
                    connection = connection_mgr.connections[connection_id]
                    if request.include_internal or not connection.is_node_group_internal:
                        outgoing_connections_list.append(
                            OutgoingConnection(
                                source_parameter_name=connection.source_parameter.name,
                                target_node_name=connection.target_node.name,
                                target_parameter_name=connection.target_parameter.name,
                            )
                        )

        # get incoming connections
        incoming_connections_list = []
        if node_name in connection_mgr.incoming_index:
            for connection_lists in connection_mgr.incoming_index[node_name].values():
                for connection_id in connection_lists:
                    connection = connection_mgr.connections[connection_id]
                    if request.include_internal or not connection.is_node_group_internal:
                        incoming_connections_list.append(
                            IncomingConnection(
                                source_node_name=connection.source_node.name,
                                source_parameter_name=connection.source_parameter.name,
                                target_parameter_name=connection.target_parameter.name,
                            )
                        )

        details = f"Successfully listed all Connections to and from Node '{node_name}'."
        result = ListConnectionsForNodeResultSuccess(
            incoming_connections=incoming_connections_list,
            outgoing_connections=outgoing_connections_list,
            result_details=details,
        )
        return result

    def on_get_connections_for_parameter_request(
        self, request: GetConnectionsForParameterRequest
    ) -> GetConnectionsForParameterResultFailure | GetConnectionsForParameterResultSuccess:
        parameter_name = request.parameter_name
        node_name = request.node_name
        node = None

        if node_name is None:
            # Get from the current context.
            if not GriptapeNodes.ContextManager().has_current_node():
                details = "Attempted to get connections for a parameter from the Current Context. Failed because the Current Context is empty."
                return GetConnectionsForParameterResultFailure(result_details=details)

            node = GriptapeNodes.ContextManager().get_current_node()
            node_name = node.name

        # Does this node exist?
        if node is None:
            obj_mgr = GriptapeNodes.ObjectManager()
            node = obj_mgr.attempt_get_object_by_name_as_type(node_name, BaseNode)
            if node is None:
                details = f"Attempted to get connections for parameter '{parameter_name}' on node '{node_name}', but no such node was found."
                return GetConnectionsForParameterResultFailure(result_details=details)

        # Does this parameter exist on the node?
        parameter = node.get_parameter_by_name(parameter_name)
        if parameter is None:
            details = f"Attempted to get connections for parameter '{parameter_name}' on node '{node_name}', but no such parameter was found."
            return GetConnectionsForParameterResultFailure(result_details=details)

        parent_flow_name = self._name_to_parent_flow_name[node_name]
        try:
            GriptapeNodes.FlowManager().get_flow_by_name(parent_flow_name)
        except KeyError as err:
            details = (
                f"Attempted to get connections for parameter '{parameter_name}' on node '{node_name}'. Error: {err}"
            )
            return GetConnectionsForParameterResultFailure(result_details=details)

        # Get connections for this specific parameter
        connection_mgr = GriptapeNodes.FlowManager().get_connections()

        # Get outgoing connections for this parameter
        outgoing_connections_list = []
        if node_name in connection_mgr.outgoing_index and parameter_name in connection_mgr.outgoing_index[node_name]:
            outgoing_connections_list = [
                OutgoingConnection(
                    source_parameter_name=connection.source_parameter.name,
                    target_node_name=connection.target_node.name,
                    target_parameter_name=connection.target_parameter.name,
                )
                for connection_id in connection_mgr.outgoing_index[node_name][parameter_name]
                for connection in [connection_mgr.connections[connection_id]]
            ]

        # Get incoming connections for this parameter
        incoming_connections_list = []
        if node_name in connection_mgr.incoming_index and parameter_name in connection_mgr.incoming_index[node_name]:
            incoming_connections_list = [
                IncomingConnection(
                    source_node_name=connection.source_node.name,
                    source_parameter_name=connection.source_parameter.name,
                    target_parameter_name=connection.target_parameter.name,
                )
                for connection_id in connection_mgr.incoming_index[node_name][parameter_name]
                for connection in [connection_mgr.connections[connection_id]]
            ]

        details = f"Successfully retrieved connections for parameter '{parameter_name}' on node '{node_name}'."
        result = GetConnectionsForParameterResultSuccess(
            parameter_name=parameter_name,
            node_name=node_name,
            incoming_connections=incoming_connections_list,
            outgoing_connections=outgoing_connections_list,
            result_details=details,
        )
        return result

    def on_list_parameters_on_node_request(self, request: ListParametersOnNodeRequest) -> ResultPayload:
        node_name = request.node_name
        node = None

        if node_name is None:
            # Get from the current context.
            if not GriptapeNodes.ContextManager().has_current_node():
                details = "Attempted to list Parameters for a Node from the Current Context. Failed because the Current Context is empty."
                return ListParametersOnNodeResultFailure(result_details=details)

            node = GriptapeNodes.ContextManager().get_current_node()
            node_name = node.name

        # Does this node exist?
        if node is None:
            obj_mgr = GriptapeNodes.ObjectManager()
            node = obj_mgr.attempt_get_object_by_name_as_type(node_name, BaseNode)
            if node is None:
                details = f"Attempted to list Parameters for a Node '{node_name}', but no such Node was found."

                result = ListParametersOnNodeResultFailure(result_details=details)
                return result

        ret_list = [param.name for param in node.parameters]

        details = f"Successfully listed Parameters for Node '{node_name}'."
        result = ListParametersOnNodeResultSuccess(parameter_names=ret_list, result_details=details)
        return result

    def generate_unique_parameter_name(self, node: BaseNode, base_name: str) -> str:
        """Generate a unique parameter name for a node by appending a number if needed.

        Args:
            node: The node to check for existing parameter names
            base_name: The desired base name for the parameter

        Returns:
            A unique parameter name that doesn't conflict with existing parameters
        """
        if node.get_parameter_by_name(base_name) is None:
            return base_name

        counter = 1
        while node.get_parameter_by_name(f"{base_name}_{counter}") is not None:
            counter += 1
        return f"{base_name}_{counter}"

    def on_add_parameter_to_node_request(self, request: AddParameterToNodeRequest) -> ResultPayload:  # noqa: C901, PLR0911, PLR0912, PLR0915
        node_name = request.node_name
        node = None
        parent_group: ParameterGroup | None = None

        if node_name is None:
            # Get from the current context.
            if not GriptapeNodes.ContextManager().has_current_node():
                details = "Attempted to add Parameter to a Node from the Current Context. Failed because the Current Context is empty."
                return AddParameterToNodeResultFailure(result_details=details)

            node = GriptapeNodes.ContextManager().get_current_node()
            node_name = node.name

        # Does this node exist?
        if node is None:
            obj_mgr = GriptapeNodes.ObjectManager()
            node = obj_mgr.attempt_get_object_by_name_as_type(node_name, BaseNode)
            if node is None:
                details = f"Attempted to add Parameter '{request.parameter_name}' to a Node '{node_name}', but no such Node was found."

                result = AddParameterToNodeResultFailure(result_details=details)
                return result

        # Check if node is locked
        if node.lock:
            details = f"Attempted to add Parameter '{request.parameter_name}' to Node '{node_name}'. Failed because the Node was locked."
            result = AddParameterToNodeResultFailure(result_details=details)
            return result

        if request.parent_container_name and not request.initial_setup:
            parameter = node.get_parameter_by_name(request.parent_container_name)
            if parameter is None:
                details = f"Attempted to add Parameter to Container Parameter '{request.parent_container_name}' in node '{node_name}'. Failed because parameter didn't exist."
                result = AddParameterToNodeResultFailure(result_details=details)
                return result
            if not isinstance(parameter, ParameterContainer):
                details = f"Attempted to add Parameter to Container Parameter '{request.parent_container_name}' in node '{node_name}'. Failed because parameter wasn't a container."
                result = AddParameterToNodeResultFailure(result_details=details)
                return result
            try:
                new_param = parameter.add_child_parameter()
            except Exception as e:
                details = f"Attempted to add Parameter to Container Parameter '{request.parent_container_name}' in node '{node_name}'. Failed: {e}."
                logger.exception(details)
                result = AddParameterToNodeResultFailure(result_details=details)
                return result

            return AddParameterToNodeResultSuccess(
                parameter_name=new_param.name,
                type=new_param.type,
                node_name=node_name,
                result_details=f"Successfully added parameter '{new_param.name}' to container parameter '{request.parent_container_name}' in node '{node_name}'.",
            )
        if request.parent_element_name is not None:
            parent_element = node.get_element_by_name_and_type(request.parent_element_name)
            if parent_element is None:
                details = f"Attempted to add Parameter to Parent Element '{request.parent_element_name}' in node '{node_name}'. Failed because element didn't exist."
                result = AddParameterToNodeResultFailure(result_details=details)
                return result
            # Handle ParameterGroup parentage with potential to expand in future to other element types.
            if isinstance(parent_element, ParameterGroup):
                parent_group = parent_element
        if request.parameter_name is None or request.tooltip is None:
            details = f"Attempted to add Parameter to node '{node_name}'. Failed because default_value, tooltip, or parameter_name was not defined."
            result = AddParameterToNodeResultFailure(result_details=details)
            return result

        # Generate a unique parameter name if needed
        requested_parameter_name = request.parameter_name
        if requested_parameter_name is None:
            # Not allowed to have a parameter with no name, so we'll give it a default name
            requested_parameter_name = "parameter"

        final_param_name = self.generate_unique_parameter_name(node, requested_parameter_name)

        # Let's see if the Parameter is properly formed.
        # If a Parameter is intended for Control, it needs to have that be the exclusive type.
        # The 'type', 'types', and 'output_type' are a little weird to handle (see Parameter definition for details)
        has_control_type = False
        has_non_control_types = False
        if request.type is not None:
            if request.type.lower() == ParameterTypeBuiltin.CONTROL_TYPE.value.lower():
                has_control_type = True
            else:
                has_non_control_types = True
        if request.input_types is not None:
            for test_type in request.input_types:
                if test_type.lower() == ParameterTypeBuiltin.CONTROL_TYPE.value.lower():
                    has_control_type = True
                else:
                    has_non_control_types = True
        if request.output_type is not None:
            if request.output_type.lower() == ParameterTypeBuiltin.CONTROL_TYPE.value.lower():
                has_control_type = True
            else:
                has_non_control_types = True

        if has_control_type and has_non_control_types:
            details = f"Attempted to add Parameter '{request.parameter_name}' to Node '{node_name}'. Failed because it had 'ParameterControlType' AND at least one other non-control type. If a Parameter is intended for control, it must only accept that type."

            result = AddParameterToNodeResultFailure(result_details=details)
            return result

        allowed_modes = set()
        if request.mode_allowed_input:
            allowed_modes.add(ParameterMode.INPUT)
        if request.mode_allowed_property:
            allowed_modes.add(ParameterMode.PROPERTY)
        if request.mode_allowed_output:
            allowed_modes.add(ParameterMode.OUTPUT)

        # Let's roll, I guess.
        new_param = Parameter(
            name=final_param_name,
            type=request.type,
            input_types=request.input_types,
            output_type=request.output_type,
            default_value=request.default_value,
            user_defined=request.is_user_defined,
            tooltip=request.tooltip,
            tooltip_as_input=request.tooltip_as_input,
            tooltip_as_property=request.tooltip_as_property,
            tooltip_as_output=request.tooltip_as_output,
            allowed_modes=allowed_modes,
            ui_options=request.ui_options,
            parent_container_name=request.parent_container_name,
            parent_element_name=parent_group.name if parent_group is not None else None,
            settable=request.settable,
        )
        try:
            with sanctioned_parameter_mutation():
                if request.parent_container_name and request.initial_setup:
                    parameter_parent = node.get_parameter_by_name(request.parent_container_name)
                    if parameter_parent is not None:
                        parameter_parent.add_child(new_param)
                elif parent_group is not None:
                    parent_group.add_child(new_param)
                else:
                    node.add_parameter(new_param)
        except Exception as e:
            details = f"Couldn't add parameter with name {request.parameter_name} to Node '{node_name}'. Error: {e}"
            return AddParameterToNodeResultFailure(result_details=details)

        details = f"Successfully added Parameter '{final_param_name}' to Node '{node_name}'."
        log_level = logging.DEBUG
        if final_param_name != requested_parameter_name:
            log_level = logging.WARNING
            details = f"{details} WARNING: Had to rename from original parameter name '{requested_parameter_name}' as a parameter with this name already existed in node '{node_name}'."

        logger.log(level=log_level, msg=details)

        result = AddParameterToNodeResultSuccess(
            parameter_name=new_param.name, type=new_param.type, node_name=node_name, result_details=details
        )
        return result

    def on_add_parameter_group_to_node_request(  # noqa: C901, PLR0911
        self, request: AddParameterGroupToNodeRequest
    ) -> ResultPayload:
        """Handle request to add a ParameterGroup to a node."""
        node_name = request.node_name
        node = None
        parent_group: ParameterGroup | None = None

        if node_name is None:
            if not GriptapeNodes.ContextManager().has_current_node():
                details = "Attempted to add ParameterGroup to a Node from the Current Context. Failed because the Current Context is empty."
                return AddParameterGroupToNodeResultFailure(result_details=details)

            node = GriptapeNodes.ContextManager().get_current_node()
            node_name = node.name

        if node is None:
            obj_mgr = GriptapeNodes.ObjectManager()
            node = obj_mgr.attempt_get_object_by_name_as_type(node_name, BaseNode)
            if node is None:
                details = f"Attempted to add ParameterGroup '{request.group_name}' to a Node '{node_name}', but no such Node was found."
                return AddParameterGroupToNodeResultFailure(result_details=details)

        if node.lock:
            details = f"Attempted to add ParameterGroup '{request.group_name}' to Node '{node_name}'. Failed because the Node was locked."
            return AddParameterGroupToNodeResultFailure(result_details=details)

        if not request.group_name:
            details = (
                f"Attempted to add ParameterGroup to node '{node_name}'. Failed because group_name was not defined."
            )
            return AddParameterGroupToNodeResultFailure(result_details=details)

        existing_element = node.get_element_by_name_and_type(request.group_name)
        if existing_element is not None:
            details = f"Attempted to add ParameterGroup '{request.group_name}' to node '{node_name}'. Failed because an element with that name already exists."
            return AddParameterGroupToNodeResultFailure(result_details=details)

        if request.parent_element_name is not None:
            parent_element = node.get_element_by_name_and_type(request.parent_element_name)
            if parent_element is None:
                details = f"Attempted to add ParameterGroup '{request.group_name}' to Parent Element '{request.parent_element_name}' in node '{node_name}'. Failed because parent element didn't exist."
                return AddParameterGroupToNodeResultFailure(result_details=details)

            if isinstance(parent_element, ParameterGroup):
                parent_group = parent_element

        new_group = ParameterGroup(
            name=request.group_name,
            ui_options=request.ui_options or {},
            parent_group_name=parent_group.name if parent_group is not None else None,
            user_defined=request.is_user_defined,
        )

        if parent_group is not None:
            parent_group.add_child(new_group)
        else:
            node.add_node_element(new_group)

        details = f"Successfully added ParameterGroup '{request.group_name}' to Node '{node_name}'."
        logger.debug(details)

        return AddParameterGroupToNodeResultSuccess(
            group_name=new_group.name, node_name=node_name, result_details=details
        )

    def on_remove_parameter_from_node_request(self, request: RemoveParameterFromNodeRequest) -> ResultPayload:  # noqa: C901, PLR0911, PLR0912, PLR0915
        node_name = request.node_name
        node = None

        if node_name is None:
            # Get the Current Context
            if not GriptapeNodes.ContextManager().has_current_node():
                details = f"Attempted to remove Parameter '{request.parameter_name}' from a Node, but no Current Context was found."

                result = RemoveParameterFromNodeResultFailure(result_details=details)
                return result

            node = GriptapeNodes.ContextManager().get_current_node()
            node_name = node.name

        # Does this node exist?
        if node is None:
            obj_mgr = GriptapeNodes.ObjectManager()
            node = obj_mgr.attempt_get_object_by_name_as_type(node_name, BaseNode)
            if node is None:
                details = f"Attempted to remove Parameter '{request.parameter_name}' from a Node '{node_name}', but no such Node was found."

                result = RemoveParameterFromNodeResultFailure(result_details=details)
                return result
        # Check if the node is locked
        if node.lock:
            details = f"Attempted to remove Element '{request.parameter_name}' from Node '{node_name}'. Failed because the Node was locked."

            result = RemoveParameterFromNodeResultFailure(result_details=details)
            return result
        # Does the Element actually exist on the Node?
        element = node.get_element_by_name_and_type(request.parameter_name)
        if element is None:
            details = f"Attempted to remove Element '{request.parameter_name}' from Node '{node_name}'. Failed because it didn't have an Element with that name on it."

            result = RemoveParameterFromNodeResultFailure(result_details=details)
            return result

        # If it's a ParameterGroup, we need to remove all the Parameters inside it.
        if isinstance(element, ParameterGroup):
            for child in element.find_elements_by_type(Parameter):
                GriptapeNodes.handle_request(RemoveParameterFromNodeRequest(child.name, node_name))
            node.remove_node_element(element)

            return RemoveParameterFromNodeResultSuccess(
                result_details=f"Successfully removed parameter group '{request.parameter_name}' and all its children from node '{node_name}'."
            )

        if isinstance(element, ParameterMessage):
            node.remove_node_element(element)

            return RemoveParameterFromNodeResultSuccess(
                result_details=f"Successfully removed parameter message '{request.parameter_name}' from node '{node_name}'."
            )

        # No tricky stuff, users!
        # if user_defined doesn't exist, or is false, then it's not user-defined
        if not getattr(element, "user_defined", False):
            details = f"Attempted to remove Element '{request.parameter_name}' from Node '{node_name}'. Failed because the Element was not user-defined (i.e., critical to the Node implementation). Only user-defined Elements can be removed from a Node."

            result = RemoveParameterFromNodeResultFailure(result_details=details)
            return result

        # Get all the connections to/from this Parameter.
        if isinstance(element, Parameter):
            list_node_connections_request = ListConnectionsForNodeRequest(node_name=node_name)
            list_connections_result = GriptapeNodes.handle_request(request=list_node_connections_request)
            if not isinstance(list_connections_result, ListConnectionsForNodeResultSuccess):
                details = f"Attempted to remove Parameter '{request.parameter_name}' from Node '{node_name}'. Failed because we were unable to get a list of Connections for the Parameter's Node."

                result = RemoveParameterFromNodeResultFailure(result_details=details)
                return result

            # We have a list of all connections to the NODE. Sift down to just those that are about this PARAMETER.

            # Destroy all the incoming Connections to this PARAMETER
            for incoming_connection in list_connections_result.incoming_connections:
                if incoming_connection.target_parameter_name == request.parameter_name:
                    delete_request = DeleteConnectionRequest(
                        source_node_name=incoming_connection.source_node_name,
                        source_parameter_name=incoming_connection.source_parameter_name,
                        target_node_name=node_name,
                        target_parameter_name=incoming_connection.target_parameter_name,
                    )
                    delete_result = GriptapeNodes.handle_request(delete_request)
                    if isinstance(delete_result, DeleteConnectionResultFailure):
                        details = f"Attempted to remove Parameter '{request.parameter_name}' from Node '{node_name}'. Failed because we were unable to delete a Connection for that Parameter."

                        result = RemoveParameterFromNodeResultFailure(result_details=details)

            # Destroy all the outgoing Connections from this PARAMETER
            for outgoing_connection in list_connections_result.outgoing_connections:
                if outgoing_connection.source_parameter_name == request.parameter_name:
                    delete_request = DeleteConnectionRequest(
                        source_node_name=node_name,
                        source_parameter_name=outgoing_connection.source_parameter_name,
                        target_node_name=outgoing_connection.target_node_name,
                        target_parameter_name=outgoing_connection.target_parameter_name,
                    )
                    delete_result = GriptapeNodes.handle_request(delete_request)
                    if isinstance(delete_result, DeleteConnectionResultFailure):
                        details = f"Attempted to remove Parameter '{request.parameter_name}' from Node '{node_name}'. Failed because we were unable to delete a Connection for that Parameter."

                        result = RemoveParameterFromNodeResultFailure(result_details=details)

        # Delete the Element itself.
        if element is not None:
            with sanctioned_parameter_mutation():
                node.remove_parameter_element(element)
        else:
            details = f"Attempted to remove Element '{request.parameter_name}' from Node '{node_name}'. Failed because element didn't exist."

            result = RemoveParameterFromNodeResultFailure(result_details=details)

        details = f"Successfully removed Element '{request.parameter_name}' from Node '{node_name}'."
        result = RemoveParameterFromNodeResultSuccess(result_details=details)
        return result

    def on_get_parameter_details_request(self, request: GetParameterDetailsRequest) -> ResultPayload:
        node_name = request.node_name
        node = None

        if node_name is None:
            if not GriptapeNodes.ContextManager().has_current_node():
                details = f"Attempted to get details for Parameter '{request.parameter_name}' from a Node, but no Current Context was found."

                result = GetParameterDetailsResultFailure(result_details=details)
                return result
            node = GriptapeNodes.ContextManager().get_current_node()
            node_name = node.name

        # Does this node exist?
        if node is None:
            obj_mgr = GriptapeNodes.ObjectManager()
            node = obj_mgr.attempt_get_object_by_name_as_type(node_name, BaseNode)
            if node is None:
                details = f"Attempted to get details for Parameter '{request.parameter_name}' from a Node '{node_name}', but no such Node was found."

                result = GetParameterDetailsResultFailure(result_details=details)
                return result

        # Does the Element actually exist on the Node?
        element = node.get_element_by_name_and_type(request.parameter_name)

        if element is None:
            details = f"Attempted to get details for Element '{request.parameter_name}' from Node '{node_name}'. Failed because it didn't have an Element with that name on it."
            return GetParameterDetailsResultFailure(result_details=details)

        # Let's bundle up the details.
        allows_input = False
        allows_property = False
        allows_output = False

        if isinstance(element, Parameter):
            modes_allowed = element.allowed_modes
            allows_input = ParameterMode.INPUT in modes_allowed
            allows_property = ParameterMode.PROPERTY in modes_allowed
            allows_output = ParameterMode.OUTPUT in modes_allowed

        details = f"Successfully got details for Element '{request.parameter_name}' from Node '{node_name}'."
        result = GetParameterDetailsResultSuccess(
            element_id=element.element_id,
            type=getattr(element, "type", ""),
            input_types=getattr(element, "input_types", []),
            output_type=getattr(element, "output_type", ""),
            default_value=getattr(element, "default_value", None),
            tooltip=getattr(element, "tooltip", ""),
            tooltip_as_input=getattr(element, "tooltip_as_input", None),
            tooltip_as_property=getattr(element, "tooltip_as_property", None),
            tooltip_as_output=getattr(element, "tooltip_as_output", None),
            mode_allowed_input=allows_input,
            mode_allowed_property=allows_property,
            mode_allowed_output=allows_output,
            is_user_defined=getattr(element, "user_defined", False),
            settable=getattr(element, "settable", None),
            private=getattr(element, "private", False),
            ui_options=getattr(element, "ui_options", None),
            result_details=details,
        )
        return result

    def on_get_node_element_details_request(self, request: GetNodeElementDetailsRequest) -> ResultPayload:
        node_name = request.node_name
        node = None

        if node_name is None:
            if not GriptapeNodes.ContextManager().has_current_node():
                details = f"Attempted to get element details for element '{request.specific_element_id}` from a Node, but no Current Context was found."

                return GetNodeElementDetailsResultFailure(result_details=details)
            node = GriptapeNodes.ContextManager().get_current_node()
            node_name = node.name

        # Does this node exist?
        if node is None:
            obj_mgr = GriptapeNodes.ObjectManager()
            node = obj_mgr.attempt_get_object_by_name_as_type(node_name, BaseNode)
            if node is None:
                details = f"Attempted to get element details for Node '{node_name}', but no such Node was found."

                return GetNodeElementDetailsResultFailure(result_details=details)

        # Did they ask for a specific element ID?
        if request.specific_element_id is None:
            # No? Use the node's root element to search from.
            element = node.root_ui_element
        else:
            element = node.root_ui_element.find_element_by_id(request.specific_element_id)
            if element is None:
                details = f"Attempted to get element details for element '{request.specific_element_id}' from Node '{node_name}'. Failed because it didn't have an element with that ID on it."

                return GetNodeElementDetailsResultFailure(result_details=details)

        element_details = element.to_dict()
        # We need to get element values from here
        param_to_value = {}
        self._set_param_to_value(node, element, param_to_value)
        if param_to_value:
            element_details["element_id_to_value"] = param_to_value
        details = f"Successfully got element details for Node '{node_name}'."
        result = GetNodeElementDetailsResultSuccess(element_details=element_details, result_details=details)
        return result

    def _set_param_to_value(self, node: BaseNode, element: BaseNodeElement, param_to_value: dict) -> None:
        """This method builds our element_id_to_value mapping to eventually return in the Element Details Request."""
        # Get all parameters
        for parameter in element.find_elements_by_type(Parameter):
            # Check if they have an output value, that takes priority
            if parameter.name in node.parameter_output_values:
                value = node.parameter_output_values[parameter.name]
            else:
                # Otherwise grab the set value or default value
                value = node.get_parameter_value(parameter.name)
            if value is not None:
                element_id = parameter.element_id
                # Check if the value is in builtins. If it isn't we need to handle it specially.
                if value.__class__.__module__ != "builtins":
                    # Check if it has a to_dict method. Use that, if it's been implemented.
                    if hasattr(value, "to_dict"):
                        # If the object has a __dict__, use that
                        param_to_value[element_id] = value.to_dict()
                        continue
                    # Otherwise use __dict__.
                    if hasattr(value, "__dict__"):
                        param_to_value[element_id] = value.__dict__
                        continue
                # Otherwise, just set it here. It'll be handled in .json() when we send it over.
                param_to_value[element_id] = value

    def modify_alterable_fields(self, request: AlterParameterDetailsRequest, parameter: BaseNodeElement) -> None:
        if isinstance(parameter, Parameter):
            if request.tooltip:
                parameter.tooltip = request.tooltip
            if request.tooltip_as_input is not None:
                parameter.tooltip_as_input = request.tooltip_as_input
            if request.tooltip_as_property is not None:
                parameter.tooltip_as_property = request.tooltip_as_property
            if request.tooltip_as_output is not None:
                parameter.tooltip_as_output = request.tooltip_as_output
        if request.ui_options is not None and hasattr(parameter, "ui_options"):
            parameter.ui_options = request.ui_options  # type: ignore[attr-defined]

    def modify_key_parameter_fields(self, request: AlterParameterDetailsRequest, parameter: Parameter) -> None:  # noqa: C901, PLR0912
        if request.type is not None:
            parameter.type = request.type
        if request.input_types is not None:
            parameter.input_types = request.input_types
        if request.output_type is not None:
            parameter.output_type = request.output_type
        if request.clear_default_value:
            if request.default_value is not None:
                node_label = request.node_name if request.node_name is not None else "current context"
                logger.warning(
                    "Conflicting options: clear_default_value and default_value were both provided for parameter '%s' on node '%s'. "
                    "clear_default_value takes precedence, so the default value will be cleared and default_value will be ignored.",
                    parameter.name,
                    node_label,
                )
            parameter.default_value = None
        elif request.default_value is not None:
            parameter.default_value = request.default_value
        if request.mode_allowed_input is not None:
            # TODO: https://github.com/griptape-ai/griptape-nodes/issues/828
            if request.mode_allowed_input is True:
                parameter.allowed_modes.add(ParameterMode.INPUT)
            else:
                parameter.allowed_modes.discard(ParameterMode.INPUT)
        if request.mode_allowed_property is not None:
            # TODO: https://github.com/griptape-ai/griptape-nodes/issues/828
            if request.mode_allowed_property is True:
                parameter.allowed_modes.add(ParameterMode.PROPERTY)
            else:
                parameter.allowed_modes.discard(ParameterMode.PROPERTY)
        if request.mode_allowed_output is not None:
            # TODO: https://github.com/griptape-ai/griptape-nodes/issues/828
            if request.mode_allowed_output is True:
                parameter.allowed_modes.add(ParameterMode.OUTPUT)
            else:
                parameter.allowed_modes.discard(ParameterMode.OUTPUT)
        if request.settable is not None:
            parameter.settable = request.settable

    def _validate_and_break_invalid_connections(
        self, node_name: str, parameter: Parameter, request: AlterParameterDetailsRequest
    ) -> ResultPayload | None:
        """Validate and break any connections that are no longer valid after a parameter type change.

        This method checks both incoming and outgoing connections for a parameter and removes
        any that are no longer type-compatible after the parameter's type has been changed.

        Returns:
            ResultPayload | None: Returns AlterParameterDetailsResultFailure if any connection deletion fails,
                                 None otherwise.
        """
        # Get all connections for this node
        list_connections_request = ListConnectionsForNodeRequest(node_name=node_name)
        list_connections_result = self.on_list_connections_for_node_request(list_connections_request)

        if not isinstance(list_connections_result, ListConnectionsForNodeResultSuccess):
            # No connections exist for this node, which is not a failure - just nothing to validate
            return None

        # Check and break invalid incoming connections
        for conn in list_connections_result.incoming_connections:
            if conn.target_parameter_name == request.parameter_name:
                source_node = self.get_node_by_name(conn.source_node_name)
                source_param = source_node.get_parameter_by_name(conn.source_parameter_name)
                if source_param and not parameter.is_incoming_type_allowed(source_param.output_type):
                    delete_result = GriptapeNodes.FlowManager().on_delete_connection_request(
                        DeleteConnectionRequest(
                            source_node_name=conn.source_node_name,
                            source_parameter_name=conn.source_parameter_name,
                            target_node_name=node_name,
                            target_parameter_name=request.parameter_name,
                        )
                    )
                    if isinstance(delete_result, ResultPayloadFailure):
                        details = f"Failed to delete incompatible incoming connection from {conn.source_node_name}.{conn.source_parameter_name} to {node_name}.{request.parameter_name}: {delete_result}"
                        return AlterParameterDetailsResultFailure(result_details=details)

        # Check and break invalid outgoing connections
        for conn in list_connections_result.outgoing_connections:
            if conn.source_parameter_name == request.parameter_name:
                target_node = self.get_node_by_name(conn.target_node_name)
                target_param = target_node.get_parameter_by_name(conn.target_parameter_name)
                if target_param and not target_param.is_incoming_type_allowed(parameter.output_type):
                    delete_result = GriptapeNodes.FlowManager().on_delete_connection_request(
                        DeleteConnectionRequest(
                            source_node_name=node_name,
                            source_parameter_name=request.parameter_name,
                            target_node_name=conn.target_node_name,
                            target_parameter_name=conn.target_parameter_name,
                        )
                    )
                    if isinstance(delete_result, ResultPayloadFailure):
                        details = f"Failed to delete incompatible outgoing connection from {node_name}.{request.parameter_name} to {conn.target_node_name}.{conn.target_parameter_name}: {delete_result}"
                        return AlterParameterDetailsResultFailure(result_details=details)

        return None

    def on_alter_parameter_details_request(self, request: AlterParameterDetailsRequest) -> ResultPayload:  # noqa: C901, PLR0911, PLR0912
        node_name = request.node_name
        node = None

        if node_name is None:
            if not GriptapeNodes.ContextManager().has_current_node():
                details = f"Attempted to alter details for Parameter '{request.parameter_name}' from node in the Current Context. Failed because there was no such Node."

                return AlterParameterDetailsResultFailure(result_details=details)
            node = GriptapeNodes.ContextManager().get_current_node()
            node_name = node.name

        # Does this node exist?
        if node is None:
            obj_mgr = GriptapeNodes.ObjectManager()
            node = obj_mgr.attempt_get_object_by_name_as_type(node_name, BaseNode)
            if node is None:
                details = f"Attempted to alter details for Parameter '{request.parameter_name}' from Node '{node_name}', but no such Node was found."

                return AlterParameterDetailsResultFailure(result_details=details)

        # Is the node locked?
        if node.lock:
            details = f"Attempted to alter details for Parameter '{request.parameter_name}' from Node '{node_name}'. Failed because the Node was locked."
            return AlterParameterDetailsResultFailure(result_details=details)

        # Handle ErrorProxyNode parameter alteration requests
        if isinstance(node, ErrorProxyNode):
            if request.initial_setup:
                # Record the alteration request for serialization replay
                node.record_initialization_request(request)

                # Early return with warning - we're just preserving the original changes
                details = f"Parameter '{request.parameter_name}' alteration recorded for ErrorProxyNode '{node_name}'. Original node '{node.original_node_type}' had loading errors - preserving changes for correct recreation when dependency '{node.original_library_name}' is resolved."

                result_details = ResultDetails(message=details, level=logging.WARNING)
                return AlterParameterDetailsResultSuccess(result_details=result_details)

            # Reject runtime parameter alterations on ErrorProxy
            details = f"Cannot modify parameter '{request.parameter_name}' on placeholder node '{node_name}'. This placeholder preserves your workflow structure but doesn't allow parameter modifications, as they could cause issues when the original node is restored."
            return AlterParameterDetailsResultFailure(result_details=details)

        # Does the Element actually exist on the Node?
        element = node.get_element_by_name_and_type(request.parameter_name)
        if element is None:
            details = f"Attempted to alter details for Element '{request.parameter_name}' from Node '{node_name}'. Failed because it didn't have an Element with that name on it."
            return AlterParameterDetailsResultFailure(result_details=details)
        if request.ui_options is not None:
            element.ui_options = request.ui_options  # type: ignore[attr-defined]

        # Check and handle connections if type was changed
        if isinstance(element, Parameter) and (
            request.type is not None or request.input_types is not None or request.output_type is not None
        ):
            result = self._validate_and_break_invalid_connections(node_name, element, request)
            if isinstance(result, AlterParameterDetailsResultFailure):
                return result

        # TODO: https://github.com/griptape-ai/griptape-nodes/issues/827
        # Now change all the values on the Element.
        self.modify_alterable_fields(request, element)

        # The rest of these are not alterable
        if isinstance(element, Parameter):
            if hasattr(element, "user_defined") and element.user_defined is False and request.request_id:  # type: ignore[attr-defined]
                # TODO: https://github.com/griptape-ai/griptape-nodes/issues/826
                details = f"Attempted to alter details for Element '{request.parameter_name}' from Node '{node_name}'. Could only alter some values because the Element was not user-defined (i.e., critical to the Node implementation). Only user-defined Elements can be totally modified from a Node."
                return AlterParameterDetailsResultSuccess(
                    result_details=ResultDetails(message=details, level=logging.WARNING)
                )
            self.modify_key_parameter_fields(request, element)

        # This field requires the node as well
        if request.default_value is not None:
            # TODO: https://github.com/griptape-ai/griptape-nodes/issues/825
            node.parameter_values[request.parameter_name] = request.default_value

        details = f"Successfully altered details for Element '{request.parameter_name}' from Node '{node_name}'."
        result = AlterParameterDetailsResultSuccess(result_details=details)
        return result

    def on_alter_parameter_group_details_request(  # noqa: PLR0911
        self, request: AlterParameterGroupDetailsRequest
    ) -> ResultPayload:
        """Handle requests to alter ParameterGroup details (primarily ui_options)."""
        node_name = request.node_name
        node = None

        if node_name is None:
            if not GriptapeNodes.ContextManager().has_current_node():
                details = f"Attempted to alter details for ParameterGroup '{request.group_name}' from node in the Current Context. Failed because there was no such Node."
                return AlterParameterGroupDetailsResultFailure(result_details=details)
            node = GriptapeNodes.ContextManager().get_current_node()
            node_name = node.name

        if node is None:
            obj_mgr = GriptapeNodes.ObjectManager()
            node = obj_mgr.attempt_get_object_by_name_as_type(node_name, BaseNode)
            if node is None:
                details = f"Attempted to alter details for ParameterGroup '{request.group_name}' from Node '{node_name}', but no such Node was found."
                return AlterParameterGroupDetailsResultFailure(result_details=details)

        if node.lock:
            details = f"Attempted to alter details for ParameterGroup '{request.group_name}' from Node '{node_name}'. Failed because the Node was locked."
            return AlterParameterGroupDetailsResultFailure(result_details=details)

        # Handle ErrorProxyNode parameter group alteration requests
        if isinstance(node, ErrorProxyNode):
            if request.initial_setup:
                node.record_initialization_request(request)
                details = f"ParameterGroup '{request.group_name}' alteration recorded for ErrorProxyNode '{node_name}'. Original node '{node.original_node_type}' had loading errors - preserving changes for correct recreation when dependency '{node.original_library_name}' is resolved."
                result_details = ResultDetails(message=details, level=logging.WARNING)
                return AlterParameterGroupDetailsResultSuccess(result_details=result_details)

            details = f"Cannot modify ParameterGroup '{request.group_name}' on placeholder node '{node_name}'. This placeholder preserves your workflow structure but doesn't allow modifications."
            return AlterParameterGroupDetailsResultFailure(result_details=details)

        # Find the ParameterGroup
        group = node.get_element_by_name_and_type(request.group_name, ParameterGroup)
        if group is None or not isinstance(group, ParameterGroup):
            details = f"Attempted to alter details for ParameterGroup '{request.group_name}' from Node '{node_name}'. Failed because no such ParameterGroup was found."
            return AlterParameterGroupDetailsResultFailure(result_details=details)

        # Update ui_options if provided
        if request.ui_options is not None:
            group.ui_options = request.ui_options

        details = f"Successfully altered details for ParameterGroup '{request.group_name}' from Node '{node_name}'."
        return AlterParameterGroupDetailsResultSuccess(result_details=details)

    # For C901 (too complex): Need to give customers explicit reasons for failure on each case.
    def on_get_parameter_value_request(self, request: GetParameterValueRequest) -> ResultPayload:
        node_name = request.node_name
        node = None

        if node_name is None:
            if not GriptapeNodes.ContextManager().has_current_node():
                details = f"Attempted to get value for Parameter '{request.parameter_name}' from node in the Current Context. Failed because there was no such Node."

                return GetParameterValueResultFailure(result_details=details)
            node = GriptapeNodes.ContextManager().get_current_node()
            node_name = node.name

        # Parse the parameter name to check for list indexing
        param_name = request.parameter_name

        # Does this node exist?
        if node is None:
            obj_mgr = GriptapeNodes.ObjectManager()
            node = obj_mgr.attempt_get_object_by_name_as_type(node_name, BaseNode)
            if node is None:
                details = f'"{node_name}" not found'
                return GetParameterValueResultFailure(result_details=details)

        # Does the Parameter actually exist on the Node?
        parameter = node.get_parameter_by_name(param_name)
        if parameter is None:
            details = f'"{node_name}.{param_name}" not found'
            return GetParameterValueResultFailure(result_details=details)

        # Output values take priority (they represent the result of execution).
        if param_name in node.parameter_output_values:
            data_value = node.parameter_output_values[param_name]
        elif param_name in node.parameter_values:
            data_value = node.parameter_values[param_name]
        else:
            data_value = parameter.default_value

        # Cool.
        details = f"{node_name}.{request.parameter_name} = {data_value}"
        result = GetParameterValueResultSuccess(
            input_types=parameter.input_types,
            type=parameter.type,
            output_type=parameter.output_type,
            value=safe_unstructure(data_value),
            result_details=details,
        )
        return result

    class ModifiedReturnValue(NamedTuple):
        """Wrapper for a value and a boolean indicating if it was modified."""

        value: Any
        modified: bool

    # added ignoring C901 since this method is overly long because of granular error checking, not actual complexity.
    def on_set_parameter_value_request(self, request: SetParameterValueRequest) -> ResultPayload:  # noqa: C901, PLR0911, PLR0912, PLR0915
        node_name = request.node_name
        node = None

        if node_name is None:
            if not GriptapeNodes.ContextManager().has_current_node():
                details = f"Attempted to set parameter '{request.parameter_name}' value. Failed because no Node was found in the Current Context."
                return SetParameterValueResultFailure(result_details=details)
            node = GriptapeNodes.ContextManager().get_current_node()
            node_name = node.name

        # Parse the parameter name to check for list indexing
        param_name = request.parameter_name

        # Does this node exist?
        if node is None:
            obj_mgr = GriptapeNodes.ObjectManager()
            node = obj_mgr.attempt_get_object_by_name_as_type(node_name, BaseNode)
            if node is None:
                details = f"Attempted to set parameter '{param_name}' value on node '{node_name}'. Failed because no such Node could be found."
                return SetParameterValueResultFailure(result_details=details)

        # Is the node locked?
        if node.lock:
            details = f"Attempted to set parameter '{param_name}' value on node '{node_name}'. Failed because the Node was locked."
            return SetParameterValueResultFailure(result_details=details)

        # Let versioning system potentially squelch removed parameters.
        # This check must run BEFORE we validate parameter existence, since removed parameters won't exist.
        version_compat_result = GriptapeNodes.VersionCompatibilityManager().check_set_parameter_version_compatibility(
            node, param_name, request.value
        )
        if version_compat_result is not None:
            return version_compat_result

        # Handle ErrorProxyNode parameter value requests
        if isinstance(node, ErrorProxyNode):
            if request.initial_setup:
                # For initial_setup, actually create the parameter and set the value
                # This allows normal serialization to handle it, rather than recording the command
                node.on_attempt_set_parameter_value(param_name)
                # Continue with normal parameter value setting logic below
                logger.debug(
                    "Created parameter '%s' on ErrorProxyNode '%s' during initial setup", param_name, node_name
                )
            else:
                # Reject runtime parameter value changes on ErrorProxy
                details = f"Cannot set parameter '{param_name}' on placeholder node '{node_name}'. This placeholder preserves your workflow structure but doesn't allow parameter changes, as they could cause issues when the original node is restored."
                return SetParameterValueResultFailure(result_details=details)

        # Does the Parameter actually exist on the Node?
        parameter = node.get_parameter_by_name(param_name)

        if parameter is None:
            details = f"Attempted to set parameter value for '{node_name}.{param_name}'. Failed because no parameter with that name could be found."

            result = SetParameterValueResultFailure(result_details=details)
            return result

        # Validate incoming connection source fields consistency
        incoming_node_set = request.incoming_connection_source_node_name is not None
        incoming_param_set = request.incoming_connection_source_parameter_name is not None
        if incoming_node_set != incoming_param_set:
            details = f"Attempted to set parameter value for '{node_name}.{request.parameter_name}'. Failed because incoming connection source fields must both be None or both be set. Got incoming_connection_source_node_name={request.incoming_connection_source_node_name}, incoming_connection_source_parameter_name={request.incoming_connection_source_parameter_name}."
            result = SetParameterValueResultFailure(result_details=details)
            return result

        # Prevent manual property setting on parameters that have both INPUT and PROPERTY modes when they have incoming connections
        # When a parameter can accept both input connections AND manual property values, having an active connection should
        # make the parameter non-settable as a property to avoid conflicts between connected values and manual values
        # Skip this check if: initial_setup (workflow loading), or incoming_connection_source fields are set (system passing upstream values)
        if (
            not request.initial_setup
            and not incoming_node_set  # If incoming connection source fields are set, this is a legitimate upstream value pass
            and ParameterMode.INPUT in parameter.allowed_modes
            and ParameterMode.PROPERTY in parameter.allowed_modes
        ):
            # Check if this parameter has any incoming connections
            connections = GriptapeNodes.FlowManager().get_connections()
            target_connections = connections.incoming_index.get(node_name)
            if target_connections is not None:
                param_connections = target_connections.get(request.parameter_name)
                if param_connections:  # Has incoming connections
                    # TODO: https://github.com/griptape-ai/griptape-nodes/issues/1965 Consider emitting UI events when parameters become settable/unsettable due to connection changes
                    details = f"Attempted to set parameter value for '{node_name}.{request.parameter_name}'. Failed because this parameter has incoming connections and cannot be set as a property while connected."
                    result = SetParameterValueResultFailure(result_details=details)
                    return result

        # Store original values in temp vars before calling before_value_set
        parameter_value = request.value
        parameter_value_type = request.data_type

        # Call before_value_set hook (allows nodes to modify values and temporarily control settable state)
        try:
            modified_value = node.before_value_set(parameter, parameter_value)
            if modified_value is not None:
                # Check if it's a TransformedParameterValue (value + type)
                if isinstance(modified_value, TransformedParameterValue):
                    parameter_value = modified_value.value
                    parameter_value_type = modified_value.parameter_type
                else:
                    # Just a value, no type change
                    parameter_value = modified_value
        except Exception as err:
            details = f"Attempted to set parameter value for '{node_name}.{request.parameter_name}'. Failed because before_value_set hook raised exception: {err}"
            result = SetParameterValueResultFailure(result_details=details)
            return result

        # Update request with potentially transformed values
        request.value = parameter_value
        if parameter_value_type is not None:
            request.data_type = parameter_value_type

        # Validate that parameters can be set at all (note: we want the value to be set during initial setup, but not after)
        # We skip this if it's a passthru from a connection or if we're on initial setup; those always trump settable.
        # This check comes *AFTER* before_value_set() to allow nodes to temporarily modify settable state
        if not parameter.settable and not incoming_node_set and not request.initial_setup:
            details = f"Attempted to set parameter value for '{node_name}.{request.parameter_name}'. Failed because that Parameter was flagged as not settable."
            result = SetParameterValueResultFailure(result_details=details)
            return result
        object_type = parameter_value_type or parameter.type
        # If the parameter is control type, we shouldn't check the value being set, since it's just a marker for which path to take, not a real value, and will likely be a string, which doesn't match ControlType.
        if parameter.type != ParameterTypeBuiltin.CONTROL_TYPE.value and not parameter.is_incoming_type_allowed(
            object_type
        ):
            details = f"Attempted to set parameter value for '{node_name}.{request.parameter_name}'. Failed because the value's type of '{object_type}' was not in the Parameter's list of allowed types: {parameter.input_types}."

            result = SetParameterValueResultFailure(result_details=details)
            return result

        try:
            parent_flow_name = self.get_node_parent_flow_by_name(node.name)
        except KeyError:
            details = f"Attempted to set parameter value for '{node_name}.{request.parameter_name}'. Failed because the node's parent flow does not exist. Could not unresolve future nodes."
            return SetParameterValueResultFailure(result_details=details)

        obj_mgr = GriptapeNodes.ObjectManager()
        parent_flow = obj_mgr.attempt_get_object_by_name_as_type(parent_flow_name, ControlFlow)
        if not parent_flow:
            details = f"Attempted to set parameter value for '{node_name}.{request.parameter_name}'. Failed because the node's parent flow does not exist. Could not unresolve future nodes."
            return SetParameterValueResultFailure(result_details=details)
        # Snapshot output values to detect side-effect changes from after_value_set.
        # Some nodes recompute their output in after_value_set when an input changes.
        # We need to detect those changes and propagate them downstream.
        should_check_output_side_effects = not request.initial_setup and not request.is_output
        output_snapshot = dict(node.parameter_output_values) if should_check_output_side_effects else None

        try:
            finalized_value, modified = self._set_and_pass_through_values(request, node)
        except Exception as err:
            details = f"Attempted to set parameter value for '{node_name}.{request.parameter_name}'. Failed because Exception: {err}"
            return SetParameterValueResultFailure(result_details=details)
        if not request.initial_setup and modified:
            try:
                GriptapeNodes.FlowManager().get_connections().unresolve_future_nodes(node)
            except Exception as err:
                details = f"Attempted to set parameter value for '{node_name}.{request.parameter_name}'. Failed because Exception: {err}"
                return SetParameterValueResultFailure(result_details=details)
        if request.initial_setup is False and not request.is_output and modified:
            # Mark node as unresolved, broadcast an event
            node.make_node_unresolved(current_states_to_trigger_change_event=set({NodeResolutionState.RESOLVED}))
            # Get the flow
            # Pass the value through to connected downstream parameters!
            # Set incoming_connection_source fields to identify this as legitimate upstream value propagation
            # (not manual property setting) so it bypasses the INPUT+PROPERTY connection blocking logic
            conn_output_nodes = parent_flow.get_connected_output_parameters(node, parameter)
            for target_node, target_parameter in conn_output_nodes:
                # Skip propagation for:
                # 1. Control Parameters as they should not receive values
                # 2. Locked nodes
                is_control_parameter = (
                    ParameterType.attempt_get_builtin(parameter.output_type) == ParameterTypeBuiltin.CONTROL_TYPE
                )
                is_dest_node_locked = target_node.lock
                if (not is_control_parameter) and (not is_dest_node_locked):
                    GriptapeNodes.handle_request(
                        SetParameterValueRequest(
                            parameter_name=target_parameter.name,
                            node_name=target_node.name,
                            value=finalized_value,
                            data_type=object_type,  # Do type instead of output type, because it hasn't been processed.
                            incoming_connection_source_node_name=node.name,
                            incoming_connection_source_parameter_name=parameter.name,
                        )
                    )

        # Propagate side-effect output changes to downstream nodes.
        # When after_value_set modifies output parameters, those
        # changes must reach downstream nodes.
        if output_snapshot is not None and modified:
            for output_param_name, new_value in node.parameter_output_values.items():
                old_value = output_snapshot.get(output_param_name)
                if old_value is new_value or old_value == new_value:
                    continue
                output_param = node.get_parameter_by_name(output_param_name)
                if output_param is None:
                    continue
                if ParameterMode.OUTPUT not in output_param.allowed_modes:
                    continue
                is_control = (
                    ParameterType.attempt_get_builtin(output_param.output_type) == ParameterTypeBuiltin.CONTROL_TYPE
                )
                if is_control:
                    continue
                conn_targets = parent_flow.get_connected_output_parameters(node, output_param)
                for target_node, target_parameter in conn_targets:
                    if target_node.lock:
                        continue
                    GriptapeNodes.handle_request(
                        SetParameterValueRequest(
                            parameter_name=target_parameter.name,
                            node_name=target_node.name,
                            value=new_value,
                            data_type=output_param.output_type,
                            incoming_connection_source_node_name=node.name,
                            incoming_connection_source_parameter_name=output_param.name,
                        )
                    )

        # Cool.
        details = f"Successfully set value on Node '{node_name}' Parameter '{request.parameter_name}'."
        result = SetParameterValueResultSuccess(
            finalized_value=finalized_value, data_type=parameter.type, result_details=details
        )
        return result

    def _set_and_pass_through_values(self, request: SetParameterValueRequest, node: BaseNode) -> ModifiedReturnValue:
        """Set the parameter value on the node according to the specifications."""
        modified = False
        object_created = request.value
        # If the value should be set on the output dictionary:
        if request.is_output:
            # set it to output values
            if (
                request.parameter_name in node.parameter_output_values
                and node.parameter_output_values[request.parameter_name] != object_created
            ):
                modified = True
            node.parameter_output_values[request.parameter_name] = object_created
            return NodeManager.ModifiedReturnValue(object_created, modified)
        # Otherwise use set_parameter_value. This calls our converters and validators.
        # Skip before_value_set since we already called it earlier in the flow
        old_value = node.get_parameter_value(request.parameter_name)
        node.set_parameter_value(
            request.parameter_name, object_created, initial_setup=request.initial_setup, skip_before_value_set=True
        )
        # Get the "converted" value here.
        finalized_value = node.get_parameter_value(request.parameter_name)
        if old_value != finalized_value:
            modified = True
        # If any parameters were dependent on that value, we're calling this details request to emit the result to the editor.
        return NodeManager.ModifiedReturnValue(finalized_value, modified)

    # For C901 (too complex): Need to give customers explicit reasons for failure on each case.
    # For PLR0911 (too many return statements): don't want to do a ton of nested chains of success,
    # want to give clear reasoning for each failure.
    # For PLR0915 (too many statements): very little reusable code here, want to be explicit and
    # make debugger use friendly.
    def on_get_all_node_info_request(self, request: GetAllNodeInfoRequest) -> ResultPayload:  # noqa: C901, PLR0911
        node_name = request.node_name
        node = None

        # Get from the current context.
        if node_name is None:
            if not GriptapeNodes.ContextManager().has_current_node():
                details = "Attempted to get all info for a Node from the Current Context. Failed because the Current Context is empty."
                return GetAllNodeInfoResultFailure(result_details=details)

            node = GriptapeNodes.ContextManager().get_current_node()
            node_name = node.name

        # Does this node exist?
        if node is None:
            obj_mgr = GriptapeNodes.ObjectManager()
            node = obj_mgr.attempt_get_object_by_name_as_type(node_name, BaseNode)
            if node is None:
                # Logged at DEBUG: this is usually a benign race where the GUI requests info
                # about a node that has just been deleted (e.g. during batch deletes). The
                # caller still receives a failure result and can react as needed.
                details = f"Attempted to get all info for Node named '{node_name}', but no such Node was found."
                return GetAllNodeInfoResultFailure(result_details=ResultDetails(message=details, level=logging.DEBUG))

        get_metadata_request = GetNodeMetadataRequest(node_name=node_name)
        get_metadata_result = self.on_get_node_metadata_request(get_metadata_request)
        if not get_metadata_result.succeeded():
            details = f"Attempted to get all info for Node named '{node_name}', but failed getting the metadata."
            return GetAllNodeInfoResultFailure(result_details=details)

        get_resolution_state_request = GetNodeResolutionStateRequest(node_name=node_name)
        get_resolution_state_result = self.on_get_node_resolution_state_request(get_resolution_state_request)
        if not get_resolution_state_result.succeeded():
            details = (
                f"Attempted to get all info for Node named '{node_name}', but failed getting the resolution state."
            )
            return GetAllNodeInfoResultFailure(result_details=details)

        list_connections_request = ListConnectionsForNodeRequest(node_name=node_name)
        list_connections_result = self.on_list_connections_for_node_request(list_connections_request)
        if not list_connections_result.succeeded():
            details = (
                f"Attempted to get all info for Node named '{node_name}', but failed listing all connections for it."
            )

            return GetAllNodeInfoResultFailure(result_details=details)
        # Cast everything to get the linter off our back.
        try:
            get_metadata_success = cast("GetNodeMetadataResultSuccess", get_metadata_result)
            get_resolution_state_success = cast("GetNodeResolutionStateResultSuccess", get_resolution_state_result)
            list_connections_success = cast("ListConnectionsForNodeResultSuccess", list_connections_result)
        except Exception as err:
            details = f"Attempted to get all info for Node named '{node_name}'. Failed due to error: {err}."

            return GetAllNodeInfoResultFailure(result_details=details)
        get_node_elements_request = GetNodeElementDetailsRequest(node_name=node_name)
        get_node_elements_result = self.on_get_node_element_details_request(get_node_elements_request)
        if not get_node_elements_result.succeeded():
            details = (
                f"Attempted to get all info for Node named '{node_name}', but failed getting details for elements."
            )
            return GetAllNodeInfoResultFailure(result_details=details)
        try:
            get_element_details_success = cast("GetNodeElementDetailsResultSuccess", get_node_elements_result)
        except Exception as err:
            details = f"Attempted to get all info for Node named '{node_name}'. Failed due to error: {err}."
            return GetAllNodeInfoResultFailure(result_details=details)

        # this will return the node element and the value
        element_details = get_element_details_success.element_details
        if "element_id_to_value" in element_details:
            element_id_to_value = element_details["element_id_to_value"].copy()
            del element_details["element_id_to_value"]
        else:
            element_id_to_value = {}
        details = f"Successfully got all node info for node '{node_name}'."
        result = GetAllNodeInfoResultSuccess(
            metadata=get_metadata_success.metadata,
            node_resolution_state=get_resolution_state_success.state,
            locked=node.lock,
            connections=list_connections_success,
            element_id_to_value=element_id_to_value,
            root_node_element=element_details,
            result_details=details,
        )
        return result

    def on_get_compatible_parameters_request(self, request: GetCompatibleParametersRequest) -> ResultPayload:  # noqa: C901, PLR0911, PLR0912, PLR0915
        node_name = request.node_name
        node = None

        if node_name is None:
            if not GriptapeNodes.ContextManager().has_current_node():
                details = "Attempted to get compatible parameters for node, but no current node was found."
                return GetCompatibleParametersResultFailure(result_details=details)
            node = GriptapeNodes.ContextManager().get_current_node()
            node_name = node.name

        # Vet the node
        if node is None:
            obj_mgr = GriptapeNodes.ObjectManager()
            node = obj_mgr.attempt_get_object_by_name_as_type(node_name, BaseNode)
            if node is None:
                details = (
                    f"Attempted to get compatible parameters for node '{node_name}', but that node does not exist."
                )
                return GetCompatibleParametersResultFailure(result_details=details)

        # Vet the parameter.
        request_param = node.get_parameter_by_name(request.parameter_name)
        if request_param is None:
            details = f"Attempted to get compatible parameters for '{node_name}.{request.parameter_name}', but that no Parameter with that name could not be found."
            return GetCompatibleParametersResultFailure(result_details=details)

        # Figure out the mode we're going for, and if this parameter supports the mode.
        request_mode = ParameterMode.OUTPUT if request.is_output else ParameterMode.INPUT
        # Does this parameter support that?
        if request_mode not in request_param.allowed_modes:
            details = f"Attempted to get compatible parameters for '{node_name}.{request.parameter_name}' as '{request_mode}', but the Parameter didn't support that type of input/output."
            return GetCompatibleParametersResultFailure(result_details=details)

        # Get the parent flows.
        try:
            flow_name = self.get_node_parent_flow_by_name(node_name)
        except KeyError as err:
            details = f"Attempted to get compatible parameters for '{node_name}.{request.parameter_name}', but the node's parent flow could not be found: {err}"
            return GetCompatibleParametersResultFailure(result_details=details)

        # Iterate through all nodes in this Flow (yes, this restriction still sucks)
        list_nodes_in_flow_request = ListNodesInFlowRequest(flow_name=flow_name)
        list_nodes_in_flow_result = GriptapeNodes.FlowManager().on_list_nodes_in_flow_request(
            list_nodes_in_flow_request
        )
        if not list_nodes_in_flow_result.succeeded():
            details = f"Attempted to get compatible parameters for '{node_name}.{request.parameter_name}'. Failed due to inability to list nodes in parent flow '{flow_name}'."
            return GetCompatibleParametersResultFailure(result_details=details)

        try:
            list_nodes_in_flow_success = cast("ListNodesInFlowResultSuccess", list_nodes_in_flow_result)
        except Exception as err:
            details = f"Attempted to get compatible parameters for '{node_name}.{request.parameter_name}'. Failed due to {err}"
            return GetCompatibleParametersResultFailure(result_details=details)

        # Walk through all nodes that are NOT us to find compatible Parameters.
        valid_parameters_by_node = {}
        for test_node_name in list_nodes_in_flow_success.node_names:
            if test_node_name != request.node_name:
                # Get node by name
                try:
                    test_node = self.get_node_by_name(test_node_name)
                except ValueError as err:
                    details = f"Attempted to get compatible parameters for node '{node_name}', and sought to test against {test_node_name}, but that node does not exist. Error: {err}."
                    return GetCompatibleParametersResultFailure(result_details=details)

                # Get Parameters from Node
                for test_param in test_node.parameters:
                    # Are we compatible from an input/output perspective?
                    fits_mode = False
                    if request_mode == ParameterMode.INPUT:
                        fits_mode = ParameterMode.OUTPUT in test_param.allowed_modes
                    else:
                        fits_mode = ParameterMode.INPUT in test_param.allowed_modes

                    if fits_mode:
                        # Compare types for compatibility
                        types_compatible = False
                        if request_mode == ParameterMode.INPUT:
                            # See if MY inputs would accept THEIR output
                            types_compatible = request_param.is_incoming_type_allowed(test_param.output_type)
                        else:
                            # See if THEIR inputs would accept MY output
                            types_compatible = test_param.is_incoming_type_allowed(request_param.output_type)

                        if types_compatible:
                            param_and_mode = ParameterAndMode(
                                parameter_name=test_param.name, is_output=not request.is_output
                            )
                            # Add the test param to our dictionary.
                            if test_node_name in valid_parameters_by_node:
                                # Append this parameter to the list
                                compatible_list = valid_parameters_by_node[test_node_name]
                                compatible_list.append(param_and_mode)
                            else:
                                # Create new
                                compatible_list = [param_and_mode]
                                valid_parameters_by_node[test_node_name] = compatible_list

        details = f"Successfully got compatible parameters for '{node_name}.{request.parameter_name}'."
        return GetCompatibleParametersResultSuccess(
            valid_parameters_by_node=valid_parameters_by_node, result_details=details
        )

    def get_node_by_name(self, name: str) -> BaseNode:
        obj_mgr = GriptapeNodes.ObjectManager()

        node = obj_mgr.attempt_get_object_by_name_as_type(name, BaseNode)
        if node is None:
            msg = f"Node '{name}' not found."
            raise ValueError(msg)

        return node

    def get_node_parent_flow_by_name(self, node_name: str) -> str:
        if node_name not in self._name_to_parent_flow_name:
            msg = f"Node '{node_name}' could not be found."
            raise KeyError(msg)
        return self._name_to_parent_flow_name[node_name]

    async def on_resolve_from_node_request(self, request: ResolveNodeRequest) -> ResultPayload:  # noqa: C901, PLR0911, PLR0912
        node_name = request.node_name
        debug_mode = request.debug_mode

        if node_name is None:
            details = "No Node name was provided. Failed to resolve node."

            return ResolveNodeResultFailure(validation_exceptions=[], result_details=details)
        try:
            node = self.get_node_by_name(node_name)
        except ValueError as e:
            details = f'Resolve failure. "{node_name}" does not exist. {e}'

            return ResolveNodeResultFailure(validation_exceptions=[e], result_details=details)
        # try to get the flow parent of this node
        try:
            flow_name = self._name_to_parent_flow_name[node_name]
        except KeyError as e:
            details = f'Failed to fetch parent flow for "{node_name}": {e}'

            return ResolveNodeResultFailure(validation_exceptions=[e], result_details=details)
        try:
            obj_mgr = GriptapeNodes.ObjectManager()
            flow = obj_mgr.attempt_get_object_by_name_as_type(flow_name, ControlFlow)
        except KeyError as e:
            details = f'Failed to fetch parent flow for "{node_name}": {e}'

            return ResolveNodeResultFailure(validation_exceptions=[e], result_details=details)

        if flow is None:
            details = f'Failed to fetch parent flow for "{node_name}"'
            return ResolveNodeResultFailure(validation_exceptions=[], result_details=details)

        # Check for existing running flow
        flow_mgr = GriptapeNodes.FlowManager()
        if flow_mgr.check_for_existing_running_flow() and not flow_mgr._global_single_node_resolution:
            # Behavior should also match if the flow running is a Control Flow, and not a singular node resolution.
            errormsg = f"This workflow is already in progress. Please wait for the current control process to finish before starting {node.name} again."
            return ResolveNodeResultFailure(validation_exceptions=[RuntimeError(errormsg)], result_details=errormsg)

        # Check if the node is already in the DAG - if so, skip this resolution. It's already queued or has been resolved.
        if node.name in flow_mgr._global_dag_builder.node_to_reference:
            logger.error("Node %s is already executing. Cannot start execution.", node.name)
            return ResolveNodeResultFailure(
                validation_exceptions=[],
                result_details=f"Node {node.name} is already executing. Cannot start execution.",
            )
        try:
            GriptapeNodes.FlowManager().get_connections().unresolve_future_nodes(node)
        except Exception as e:
            details = f'Failed to mark future nodes dirty. Unable to kick off flow from "{node_name}": {e}'
            return ResolveNodeResultFailure(validation_exceptions=[e], result_details=details)
        # Validate here.
        result = self.on_validate_node_dependencies_request(ValidateNodeDependenciesRequest(node_name=node_name))
        try:
            if result.failed():
                details = f"Failed to resolve node '{node_name}'. Flow Validation Failed"
                return StartFlowResultFailure(validation_exceptions=[], result_details=details)
            result = cast("ValidateNodeDependenciesResultSuccess", result)

            if not result.validation_succeeded:
                details = f"Failed to resolve node '{node_name}'. Flow Validation Failed."
                if len(result.exceptions) > 0:
                    for exception in result.exceptions:
                        details = f"{details}\n\t{exception}"
                return StartFlowResultFailure(validation_exceptions=result.exceptions, result_details=details)
        except Exception as e:
            details = f"Failed to resolve node '{node_name}'. Flow Validation Failed. Error: {e}"
            return StartFlowResultFailure(validation_exceptions=[e], result_details=details)
        try:
            await GriptapeNodes.FlowManager().resolve_singular_node(flow, node, debug_mode=debug_mode)
        except Exception as e:
            details = f'Failed to resolve "{node_name}".  Error: {e}'
            return ResolveNodeResultFailure(validation_exceptions=[e], result_details=details)
        # TODO: https://github.com/griptape-ai/griptape-nodes/issues/4532 - support
        # wait_for_completion / completion_timeout_ms here, mirroring StartFlowRequest so
        # callers do not have to poll check_for_existing_running_flow() themselves.
        details = f'Starting to resolve "{node_name}" in "{flow_name}"'
        return ResolveNodeResultSuccess(result_details=details)

    async def on_execute_node_request(self, request: ExecuteNodeRequest) -> ResultPayload:
        """Execute a node. Orchestrator path is lookup-only; worker path is a pure RPC.

        On the orchestrator the node must already live in ObjectManager (created by
        prior CreateNodeRequest). A miss is a hard failure -- we never fabricate
        a fresh node from metadata on the orchestrator, because that would mask a
        real "node dropped from the live map" bug with a stub that has no
        connections and no flow parentage.

        On the worker (is_worker=True) the node is constructed from
        request.node_metadata on every call, hydrated, executed, and discarded.
        Nothing persists between ExecuteNodeRequests on the worker side -- the
        orchestrator is the single source of truth for node identity and
        parameter values.

        If the orchestrator's lookup succeeds and the node's library is owned by
        a worker, the request is forwarded over the wire to that worker.
        """
        library_manager = GriptapeNodes.LibraryManager()
        is_worker = library_manager.is_worker

        if is_worker:
            worker_node = self._materialize_transient_node_from_metadata(request)
            if isinstance(worker_node, ExecuteNodeResultFailure):
                return worker_node
            node = worker_node
        else:
            obj_mgr = GriptapeNodes.ObjectManager()
            orchestrator_node = obj_mgr.attempt_get_object_by_name_as_type(request.node_name, BaseNode)
            if orchestrator_node is None:
                return ExecuteNodeResultFailure(
                    result_details=(
                        f"Node '{request.node_name}' not found in ObjectManager on orchestrator. "
                        "Refusing to fabricate a fresh node from metadata; the node was dropped "
                        "from the live map and must be re-created via CreateNodeRequest."
                    ),
                )
            node = orchestrator_node

        library_name = node.metadata.get("library")

        # Forwarding to a worker exits the orchestrator's local code path before
        # the node runs, so strict-mode attribution belongs to the worker's
        # scope, not ours. Decide forwarding first; only open a local scope when
        # this process is actually going to execute the node.
        if not is_worker:
            worker = library_manager.get_worker_for_library(library_name) if library_name else None
            wm = GriptapeNodes.WorkerManager()
            if wm is not None and worker is not None:
                return await self._execute_node_via_worker(request, wm, worker)

        async with STRICT_MODE.scoped_execution(
            kind=StrictModeScopeKind.RUNTIME_EXECUTE,
            subject=request.node_name,
            library_name=library_name,
            is_worker=is_worker,
        ) as (ctx, scope):
            ctx.result = await self._hydrate_and_run_node(node, request)
            # Worker-side correctness policy: an ExecuteNodeResultSuccess that
            # carries an ERROR-severity violation must be promoted to a
            # failure. Worker output gets shipped back to the orchestrator,
            # so a "success" that violated correctness would silently corrupt
            # downstream state. The orchestrator already has the violation
            # log; the elevated failure forces the editor to surface it.
            errors = [v for v in scope.violations if v.severity is StrictModeSeverity.ERROR]
            if is_worker and errors and isinstance(ctx.result, ExecuteNodeResultSuccess):
                rules = ", ".join(sorted({v.rule_id for v in errors}))
                ctx.result = ExecuteNodeResultFailure(
                    result_details=f"Node '{request.node_name}' violated strict-mode rule(s) [{rules}].",
                )
        return ctx.result

    def _materialize_transient_node_from_metadata(
        self, request: ExecuteNodeRequest
    ) -> BaseNode | ExecuteNodeResultFailure:
        """Construct a fresh node from request.node_metadata for worker-side execution.

        The returned node is transient: it is NOT added to ObjectManager. It
        exists only for the duration of _hydrate_and_run_node and is released to
        GC when that call returns. Called only on the worker path.
        """
        node_name = request.node_name
        if not request.node_metadata:
            return ExecuteNodeResultFailure(
                result_details=f"Node '{node_name}' requires node_metadata on worker path.",
            )
        node_type = request.node_metadata.get("node_type")
        library_name = request.node_metadata.get("library")
        if not node_type:
            return ExecuteNodeResultFailure(
                result_details=f"Node '{node_name}' node_metadata is missing 'node_type'.",
            )
        # Gate worker-side construction on the same InstantiateNode checkpoint
        # CreateNode enforces. node_metadata is caller-supplied, so without this
        # a node the policy denies could be instantiated and executed by sending
        # ExecuteNodeRequest straight to a worker, never passing through the
        # gated CreateNode path.
        try:
            denial = self._evaluate_instantiation_checkpoint(node_type=node_type, specific_library_name=library_name)
        except KeyError:
            # Node type/library not registered here; let create_node below
            # surface the clearer "node type not found" failure rather than
            # masking it with a checkpoint-resolution error.
            denial = None
        if denial is not None:
            return ExecuteNodeResultFailure(
                result_details=(
                    f"Node '{node_name}' of type '{node_type}' denied by license policy: {denial.reason()}"
                ),
            )
        try:
            return LibraryRegistry.create_node(
                node_type=node_type,
                name=node_name,
                metadata=dict(request.node_metadata),
                specific_library_name=library_name,
            )
        except Exception as e:
            return ExecuteNodeResultFailure(
                result_details=f"Failed to create node '{node_name}' of type '{node_type}': {e}",
            )

    async def _execute_node_via_worker(
        self,
        request: ExecuteNodeRequest,
        wm: WorkerManager,
        worker: tuple[str, str],
    ) -> ResultPayload:
        """Dispatch ExecuteNodeRequest to a worker and return its result.

        The worker constructs a fresh transient node from request.node_metadata
        on every call and discards it after aprocess returns (see #4476);
        ExecuteNodeRequest is a pure RPC from the orchestrator's perspective.
        Output copy-back onto the orchestrator's live node happens in the caller
        (NodeExecutor.execute) so the write path is identical for local and
        worker routes.
        """
        worker_engine_id, worker_request_topic = worker
        # Assign the request_id on the payload itself so the worker handler can
        # read it from request.request_id. WorkerManager.route_to_worker will
        # re-use this id on the outer EventRequest and on its pending-future
        # registration, keeping both sides in sync. The id is what
        # cancel_worker_execution dispatches as CancelExecuteNodeRequest.target_request_id.
        if not request.request_id:
            request.request_id = str(uuid4())
        target_request_id = request.request_id
        self._orch_worker_requests[request.node_name] = (
            target_request_id,
            worker_engine_id,
            worker_request_topic,
        )
        try:
            event_request = EventRequest(request=request)
            event_request.request_id = target_request_id
            execute_raw = await wm.route_to_worker(
                event_request,
                worker_engine_id,
                worker_request_topic,
            )
        finally:
            # Drop the tracking entry regardless of success, failure, or cancellation
            # so a subsequent execute on the same node doesn't see a stale record.
            self._orch_worker_requests.pop(request.node_name, None)
        result_type_name = execute_raw.get("result_type", "")
        result_data = execute_raw.get("result", {})
        # Route through cattrs structure (not ``**result_data`` spread)
        # so the registered exception hook rebuilds ``self.exception``
        # into a ``ForwardedException`` carrying the worker-side type
        # name and traceback. A bare spread would leave ``exception``
        # as the raw {type, message, traceback} dict, breaking the
        # worker-frame surfacing in
        # ``NodeExecutor._format_node_failure_message``.
        if result_type_name == ExecuteNodeResultSuccess.__name__:
            return cast("ExecuteNodeResultSuccess", converter.structure(result_data, ExecuteNodeResultSuccess))
        return cast("ExecuteNodeResultFailure", converter.structure(result_data, ExecuteNodeResultFailure))

    async def cancel_worker_execution(self, node_name: str) -> None:
        """Dispatch CancelExecuteNodeRequest to the worker running node_name.

        No-op when the node is not currently routed to a worker (either because
        it runs locally or because no ExecuteNodeRequest is in flight). The
        cancel is fire-and-forget: the worker's handler runs under SkipTheLine
        and returns quickly, but the orchestrator does not await the ack --
        cooperative cancellation is signalled via task.cancel() on the
        orchestrator's own route_to_worker await, which unblocks here as soon
        as the cancel event has been put on the wire.
        """
        entry = self._orch_worker_requests.get(node_name)
        if entry is None:
            return
        target_request_id, worker_engine_id, worker_request_topic = entry
        wm = GriptapeNodes.WorkerManager()
        cancel_request = CancelExecuteNodeRequest(target_request_id=target_request_id)
        await wm.forward_event_to_worker(
            EventRequest(request=cancel_request),
            worker_engine_id=worker_engine_id,
            worker_request_topic=worker_request_topic,
        )

    async def on_cancel_execute_node_request(self, request: CancelExecuteNodeRequest) -> ResultPayload:
        """Worker-side handler: cancel an in-flight aprocess task by request_id.

        Sets the node's cooperative cancellation flag (so aprocess that checks
        is_cancellation_requested can exit cleanly) and cancels the asyncio task
        running aprocess (matches parallel_resolution.cancel_all_nodes semantics
        for local execution). Returns success even when the target request is
        not in flight so the cancel path is idempotent.
        """
        entry = self._worker_inflight_aprocesses.get(request.target_request_id)
        if entry is None:
            return CancelExecuteNodeResultSuccess(
                result_details=f"No in-flight ExecuteNodeRequest for request_id '{request.target_request_id}'.",
            )
        task, node = entry
        node.request_cancellation()
        if not task.done():
            task.cancel()
        return CancelExecuteNodeResultSuccess(
            result_details=f"Cancellation delivered for request_id '{request.target_request_id}'.",
        )

    async def _hydrate_and_run_node(self, node: BaseNode, request: ExecuteNodeRequest) -> ResultPayload:
        """Hydrate a node's input parameters and execute it.

        On a worker, hydration and node.aprocess() both run inside
        worker_node_execution_scope so that any nested handle_request calls
        originated from node code forward to the orchestrator. Hydration calls
        set_parameter_value, which cascades into ListConnectionsForNodeRequest
        and similar cross-node lookups; those must forward because the worker
        only owns its single node copy and cannot resolve parent-flow or peer-
        node state locally. On the orchestrator we skip the scope entirely --
        there is no RemoteHandler to read the flag, so opening it there would
        only bump a refcount nothing observes.
        """
        # Register this aprocess task under its request_id so
        # CancelExecuteNodeRequest can locate it. Only populated when the caller
        # supplied a request_id (set by _execute_node_via_worker on the
        # orchestrator; absent on the orchestrator-local path where the
        # resolution machine already owns the task).
        current_task = asyncio.current_task()
        tracked_request_id = request.request_id if current_task is not None else None
        if tracked_request_id and current_task is not None:
            self._worker_inflight_aprocesses[tracked_request_id] = (current_task, node)
        try:
            return await self._hydrate_and_run_node_inner(node, request)
        finally:
            if tracked_request_id:
                self._worker_inflight_aprocesses.pop(tracked_request_id, None)

    async def _hydrate_and_run_node_inner(self, node: BaseNode, request: ExecuteNodeRequest) -> ResultPayload:
        node_name = request.node_name
        # The node-execution scope only has meaning on a worker: it is what
        # RemoteHandler consults to decide whether to forward a request to the
        # orchestrator. On the orchestrator itself there is no RemoteHandler
        # installed (register_remote_handlers is worker-only), so opening the
        # scope there would just bump a refcount that nothing reads. Skip it
        # to keep the depth counter accurate to its name.
        is_worker = GriptapeNodes.LibraryManager()._is_worker
        scope_cm = GriptapeNodes.EventManager().worker_node_execution_scope() if is_worker else contextlib.nullcontext()
        with scope_cm:
            # Rehydrate serialized artifacts that crossed the orchestrator->worker JSON boundary.
            parameter_values = hydrate_parameter_values(request.parameter_values)
            for param_name, value in parameter_values.items():
                # Skip when the node already holds this value. The local path
                # calls ExecuteNodeRequest with dict(node.parameter_values) on
                # the same in-memory instance, so every iteration would be a
                # no-op mutation that still ran before/after_value_set hooks
                # and emitted a lifecycle event -- observably breaking nodes
                # like LoadImage. On the worker the node is fresh, so current
                # is _PARAM_MISSING and the normal set path runs.
                current = node.parameter_values.get(param_name, _PARAM_MISSING)
                if current is value or current == value:
                    continue
                try:
                    node.set_parameter_value(param_name, value)
                except Exception as e:
                    return ExecuteNodeResultFailure(
                        result_details=f"Attempted to set parameter '{param_name}' on node '{node_name}'. Failed with error: {e}",
                        exception=e,
                    )
            # Materialize parameter defaults into parameter_values so that user
            # process() code reading self.parameter_values[name] directly (rather
            # than via get_parameter_value) sees the default. Newly-dropped nodes
            # never receive a SetParameterValueRequest for still-default values,
            # so without this pass those params are absent from the dict on the
            # worker-side fresh node.
            for param in node.parameters:
                if param.name in node.parameter_values:
                    continue
                if param.default_value is None:
                    continue
                node.parameter_values[param.name] = param.default_value
            try:
                with aprocess_scope():
                    await node.aprocess()
            except Exception as e:
                # Pass the live exception through ``exception=`` so the
                # converter can capture worker-side frames into a
                # ForwardedException on the orchestrator. Without this
                # the orchestrator only sees the type and message --
                # PR06's whole reason for the dict wire-format -- and
                # NodeExecutor._format_node_failure_message would have
                # nothing to surface. ``__traceback__`` is populated
                # because ``e`` was actually raised, so the strict-mode
                # tripwire stays quiet for raise-in-process.
                return ExecuteNodeResultFailure(
                    result_details=f"Attempted to execute node '{node_name}'. Failed with error: {e}",
                    exception=e,
                )
        return ExecuteNodeResultSuccess(
            parameter_output_values=dict(node.parameter_output_values),
            result_details=f"Node '{node_name}' executed successfully.",
        )

    def on_validate_node_dependencies_request(self, request: ValidateNodeDependenciesRequest) -> ResultPayload:
        node_name = request.node_name
        obj_manager = GriptapeNodes.ObjectManager()
        node = obj_manager.attempt_get_object_by_name_as_type(node_name, BaseNode)
        if node is None:
            details = f'Failed to validate node dependencies. Node with "{node_name}" does not exist.'
            return ValidateNodeDependenciesResultFailure(result_details=details)
        try:
            flow_name = self.get_node_parent_flow_by_name(node_name)
        except Exception as e:
            details = f'Failed to validate node dependencies. Node with "{node_name}" has no parent flow. Error: {e}'
            return ValidateNodeDependenciesResultFailure(result_details=details)
        flow = GriptapeNodes.ObjectManager().attempt_get_object_by_name_as_type(flow_name, ControlFlow)
        if not flow:
            details = f'Failed to validate node dependencies. Flow with "{flow_name}" does not exist.'
            return ValidateNodeDependenciesResultFailure(result_details=details)
        # Gets all dependent nodes
        nodes = flow.get_node_dependencies(node)
        all_exceptions = []
        for dependent_node in nodes:
            exceptions = dependent_node.validate_before_workflow_run()
            if exceptions:
                all_exceptions = all_exceptions + exceptions
        return ValidateNodeDependenciesResultSuccess(
            validation_succeeded=(len(all_exceptions) == 0),
            exceptions=all_exceptions,
            result_details=f"Successfully validated dependencies for node '{node_name}'. Found {len(all_exceptions)} validation issues.",
        )

    def _serialize_group_with_children(
        self,
        group_node: BaseNodeGroup,
        unique_uuid_to_values: dict,
        serialized_parameter_value_tracker: SerializedParameterValueTracker,
    ) -> SerializedGroupResult:
        """Serialize a group node and its children for copy/paste operations.

        This method handles the special case of group nodes by serializing the group first,
        then each child with the group's UUID embedded in its metadata. This ordering ensures
        that during deserialization the group exists before its children are created, allowing
        each child's CreateNodeResultSuccess to include parent_group_name.

        Args:
            group_node: The group node to serialize
            unique_uuid_to_values: Shared dictionary for tracking pickled parameter values
            serialized_parameter_value_tracker: Tracker for parameter value hashes

        Returns:
            SerializedGroupResult containing the group command, child commands, and child UUIDs
        """
        group_name = group_node.name
        child_commands = []
        child_parameter_commands = {}
        child_uuids = []

        # Serialize the group node first so its UUID is known before children are serialized
        group_result = self.on_serialize_node_to_commands(
            SerializeNodeToCommandsRequest(
                node_name=group_name,
                unique_parameter_uuid_to_values=unique_uuid_to_values,
                serialized_parameter_value_tracker=serialized_parameter_value_tracker,
                use_pickling=True,
            )
        )

        if not isinstance(group_result, SerializeNodeToCommandsResultSuccess):
            logger.error("Failed to serialize group node '%s'", group_name)
            msg = f"Failed to serialize children and group node '{group_name}'"
            raise RuntimeError(msg)  # noqa: TRY004 Type Error doesn't make sense here, this is a runtime error.

        group_command = group_result.serialized_node_commands
        group_uuid = group_command.node_uuid

        # Clear node_names_to_add from the group's create command — children are assigned
        # to the group via _parent_group_uuid/parent_group_name during deserialization.
        # Leaving node_names_to_add set would cause on_create_node_request to call
        # add_nodes_to_group on the *original* child names, moving original nodes into
        # the new group instead of (or alongside) the newly created children.
        group_command.create_node_command.node_names_to_add = None

        # Serialize each child, embedding the group's UUID so deserialization can assign parentage
        for child_name in group_node.nodes:
            child_result = self.on_serialize_node_to_commands(
                SerializeNodeToCommandsRequest(
                    node_name=child_name,
                    unique_parameter_uuid_to_values=unique_uuid_to_values,
                    serialized_parameter_value_tracker=serialized_parameter_value_tracker,
                    use_pickling=True,
                )
            )

            if not isinstance(child_result, SerializeNodeToCommandsResultSuccess):
                logger.error("%s failed to serialize child node '%s'", group_name, child_name)
                msg = f"Failed to serialize child node '{child_name}'"
                raise RuntimeError(msg)  # noqa: TRY004 Type Error doesn't make sense here, this is a runtime error.

            child_cmd = child_result.serialized_node_commands

            # Embed parent group UUID in child metadata for deserialization remapping
            if child_cmd.create_node_command.metadata is None:
                child_cmd.create_node_command.metadata = {}
            child_cmd.create_node_command.metadata["_parent_group_uuid"] = group_uuid

            child_commands.append(child_cmd)
            child_parameter_commands[child_cmd.node_uuid] = child_result.set_parameter_value_commands
            child_uuids.append(child_cmd.node_uuid)

        return SerializedGroupResult(
            group_command=group_command,
            group_parameter_commands=group_result.set_parameter_value_commands,
            child_commands=child_commands,
            child_parameter_commands=child_parameter_commands,
            child_uuids=child_uuids,
        )

    def on_serialize_node_to_commands(self, request: SerializeNodeToCommandsRequest) -> ResultPayload:  # noqa: C901, PLR0912, PLR0915
        node_name = request.node_name
        node = None

        if node_name is None:
            if not GriptapeNodes.ContextManager().has_current_node():
                details = "Attempted to serialize a Node to commands from the Current Context. Failed because the Current Context is empty."
                return SerializeNodeToCommandsResultFailure(result_details=details)
            node = GriptapeNodes.ContextManager().get_current_node()
            node_name = node.name

        # Does this node exist?
        if node is None:
            obj_mgr = GriptapeNodes.ObjectManager()
            node = obj_mgr.attempt_get_object_by_name_as_type(node_name, BaseNode)
            if node is None:
                details = f"Attempted to serialize Node '{node_name}' to commands. Failed because no Node with that name could be found."
                return SerializeNodeToCommandsResultFailure(result_details=details)

        # This is our current dude.
        with GriptapeNodes.ContextManager().node(node=node):
            # Get the library and version details for all nodes.
            # A node's owning library is normally injected into metadata by
            # LibraryRegistry.create_node, but proxy/placeholder nodes can be created
            # without it when that path fails. Fall back to the proxy's recorded
            # original library, and tolerate a missing key rather than crashing the
            # entire save.
            library_used = node.metadata.get("library")
            if library_used is None and isinstance(node, ErrorProxyNode):
                library_used = node.original_library_name
            if library_used is None:
                # No owning library could be determined. Use an empty name so the
                # metadata lookup below fails cleanly instead of crashing the save.
                library_used = ""
            # For SubflowNodeGroup, also check if execution environment uses a special library
            execution_env_library_details = None
            if isinstance(node, SubflowNodeGroup):
                execution_env = node.get_parameter_value(node.execution_environment.name)
                if execution_env not in (LOCAL_EXECUTION, PRIVATE_EXECUTION):
                    # Get library details for the execution environment library
                    exec_env_metadata_request = GetLibraryMetadataRequest(library=execution_env)
                    exec_env_metadata_result = GriptapeNodes.LibraryManager().get_library_metadata_request(
                        exec_env_metadata_request
                    )
                    if isinstance(exec_env_metadata_result, GetLibraryMetadataResultSuccess):
                        exec_env_library_version = exec_env_metadata_result.metadata.library_version
                        execution_env_library_details = LibraryNameAndVersion(
                            library_name=execution_env, library_version=exec_env_library_version
                        )
            # Get the library metadata so we can get the version.
            library_metadata_request = GetLibraryMetadataRequest(library=library_used)
            # Call LibraryManager directly to avoid error toasts when library is unavailable (expected for ErrorProxyNode)
            # Per https://github.com/griptape-ai/griptape-nodes/issues/1940
            library_metadata_result = GriptapeNodes.LibraryManager().get_library_metadata_request(
                library_metadata_request
            )

            if not isinstance(library_metadata_result, GetLibraryMetadataResultSuccess):
                if isinstance(node, ErrorProxyNode):
                    # For ErrorProxyNode, use descriptive message when original library unavailable
                    library_version = "<version unavailable; workflow was saved when library was unable to be loaded>"
                    library_details = LibraryNameAndVersion(library_name=library_used, library_version=library_version)
                    details = f"Serializing Node '{node_name}' (original type: {node.original_node_type}) with unavailable library '{library_used}'. Saving as ErrorProxy with placeholder version. Fix the missing library and reload the workflow to restore the original node."
                    logger.warning(details)
                else:
                    # For regular nodes, this is still an error
                    details = f"Attempted to serialize Node '{node_name}' to commands. Failed to get metadata for library '{library_used}'."
                    return SerializeNodeToCommandsResultFailure(result_details=details)
            else:
                library_version = library_metadata_result.metadata.library_version
                library_details = LibraryNameAndVersion(library_name=library_used, library_version=library_version)

            # Handle BaseNodeGroup specially - serialize like normal nodes but preserve node group behavior
            if isinstance(node, BaseNodeGroup):
                if library_details is None:
                    details = f"Attempted to serialize Node '{node_name}' to commands. Library details missing."
                    return SerializeNodeToCommandsResultFailure(result_details=details)

                # Remove node_names_in_group from metadata - it's redundant and will be regenerated
                metadata_copy = copy.deepcopy(node.metadata)
                metadata_copy.pop("node_names_in_group", None)

                # Remove subflow_name for copy/paste operations (so pasted groups create fresh subflows)
                # Keep it for workflow file generation (so it can be extracted and used as a variable reference)
                if not request.include_existing_subflow_in_group:
                    metadata_copy.pop("subflow_name", None)

                # Note: Child serialization is handled in _serialize_group_with_children()
                # which is called from on_serialize_selected_nodes_to_commands()
                # This method just serializes the group node itself
                # Serialize like a normal node but add node group specific fields
                create_node_request = CreateNodeRequest(
                    node_type=node.__class__.__name__,
                    specific_library_name=library_details.library_name,
                    node_name=node_name,
                    node_names_to_add=list(node.nodes),
                    metadata=metadata_copy,
                )
            else:
                if library_details is None:
                    details = f"Attempted to serialize Node '{node_name}' to commands. Library details missing."
                    return SerializeNodeToCommandsResultFailure(result_details=details)

                # Handle ErrorProxyNode serialization - serialize as original node type
                if isinstance(node, ErrorProxyNode):
                    serialized_node_type = node.original_node_type
                    serialized_library_name = node.original_library_name
                else:
                    serialized_node_type = node.__class__.__name__
                    serialized_library_name = library_details.library_name

                # Get the creation details for regular nodes
                create_node_request = CreateNodeRequest(
                    node_type=serialized_node_type,
                    node_name=node_name,
                    specific_library_name=serialized_library_name,
                    metadata=copy.deepcopy(node.metadata),
                    # If it is actively resolving, mark as unresolved.
                    resolution=node.state.value,
                    initial_setup=True,
                )

            # We're going to compare this node instance vs. a canonical one. Rez that one up.
            # For ErrorProxyNode, we can't create a reference node, so skip comparison.
            # Wrap in ``LibraryRegistry.constructing_node()`` so the parameter-mutation
            # detector skips this ephemeral instance's declarative ``add_parameter``
            # calls (this construction bypasses ``LibraryRegistry.create_node``).
            #
            # Carry the node's library and type so the reference resolves the same
            # library-backed ``__init__`` data the live node did (e.g. a model
            # dropdown sourced from the ``model_catalog``); otherwise it would look
            # mutated.
            if isinstance(node, ErrorProxyNode):
                reference_node = None
            else:
                with LibraryRegistry.constructing_node():
                    reference_node = type(node)(
                        name="REFERENCE NODE",
                        metadata={
                            "library": node.metadata.get("library"),
                            "node_type": node.metadata.get("node_type"),
                        },
                    )

            # Now creation or alteration of all of the elements.
            element_modification_commands = []

            # Serialize only user-defined ParameterGroups (like parameters)
            all_groups = node.root_ui_element.find_elements_by_type(ParameterGroup)
            for group in all_groups:
                if group.user_defined:
                    add_group_request = AddParameterGroupToNodeRequest(
                        node_name=node_name,
                        group_name=group.name,
                        parent_element_name=group.parent_group_name,
                        ui_options=group.ui_options or {},
                        is_user_defined=True,
                        initial_setup=True,
                    )
                    element_modification_commands.append(add_group_request)

            # Then serialize parameters
            for parameter in node.parameters:
                # Create the parameter, or alter it on the existing node
                if parameter.user_defined:
                    # Always serialize user-defined parameters regardless of node type
                    param_dict = parameter.to_dict()
                    param_dict["initial_setup"] = True
                    add_param_request = AddParameterToNodeRequest.create(**param_dict)
                    element_modification_commands.append(add_param_request)
                elif isinstance(node, ErrorProxyNode):
                    # For ErrorProxyNode, replay all recorded initialization requests for this parameter
                    recorded_requests = node.get_recorded_initialization_requests()
                    matching_requests = [
                        recorded_request
                        for recorded_request in recorded_requests
                        if (
                            hasattr(recorded_request, "parameter_name")
                            and getattr(recorded_request, "parameter_name", None) == parameter.name
                        )
                    ]
                    element_modification_commands.extend(matching_requests)
                elif reference_node is None:
                    # Normal node with no reference - treat all parameters as needing serialization
                    param_dict = parameter.to_dict()
                    param_dict["initial_setup"] = True
                    add_param_request = AddParameterToNodeRequest.create(**param_dict)
                    element_modification_commands.append(add_param_request)
                else:
                    # Normal node - compare against reference node
                    diff = NodeManager._manage_alter_details(parameter, reference_node)
                    relevant = False
                    for key in diff:
                        if key in AlterParameterDetailsRequest.relevant_parameters():
                            relevant = True
                            break
                    if relevant:
                        diff["parameter_name"] = parameter.name
                        diff["initial_setup"] = True
                        alter_param_request = AlterParameterDetailsRequest.create(**diff)
                        element_modification_commands.append(alter_param_request)

            # Check for ParameterGroup alterations (ui_options changes like collapsed state)
            if reference_node is not None and not isinstance(node, ErrorProxyNode):
                # Compare ALL groups against the reference node (not just user-defined)
                # This matches the pattern used for parameter alterations
                for group in all_groups:
                    diff = NodeManager._manage_alter_group_details(group, reference_node)
                    relevant = False
                    for key in diff:
                        if key in AlterParameterGroupDetailsRequest.relevant_parameters():
                            relevant = True
                            break
                    if relevant:
                        diff["group_name"] = group.name
                        diff["initial_setup"] = True
                        alter_group_request = AlterParameterGroupDetailsRequest(**diff)
                        element_modification_commands.append(alter_group_request)

            # Now assignment of values to all of the parameters.
            set_value_commands = []

            # ErrorProxyNode uses normal parameter serialization now since we create real parameters
            # Only AlterParameterDetailsRequest commands are recorded and replayed
            # Normal node - use current parameter values
            for parameter in node.parameters:
                # SetParameterValueRequest event
                set_param_value_requests = NodeManager.handle_parameter_value_saving(
                    parameter=parameter,
                    node=node,
                    unique_parameter_uuid_to_values=request.unique_parameter_uuid_to_values,
                    serialized_parameter_value_tracker=request.serialized_parameter_value_tracker,
                    create_node_request=create_node_request,
                    use_pickling=request.use_pickling,
                )
                if set_param_value_requests is not None:
                    set_value_commands.extend(set_param_value_requests)

        # now check if locked
        if node.lock:
            lock_command = SetLockNodeStateRequest(node_name=None, lock=True)
        else:
            lock_command = None

        # Collect node dependencies
        node_dependencies = node.get_node_dependencies()
        if node_dependencies is None:
            # Ensure we always have a NodeDependencies object, even if empty
            node_dependencies = NodeDependencies()

        # Add the library dependency to the node dependencies (if applicable)
        if library_details is not None:
            node_dependencies.libraries.add(library_details)

        # For SubflowNodeGroup, also add execution environment library dependency if present
        if execution_env_library_details is not None:
            node_dependencies.libraries.add(execution_env_library_details)

        # Hooray
        serialized_node_commands = SerializedNodeCommands(
            create_node_command=create_node_request,
            element_modification_commands=element_modification_commands,
            node_dependencies=node_dependencies,
            lock_node_command=lock_command,
            is_node_group=isinstance(node, SubflowNodeGroup),
        )
        details = f"Successfully serialized node '{node_name}' into commands."
        result = SerializeNodeToCommandsResultSuccess(
            serialized_node_commands=serialized_node_commands,  # How to serialize this node
            set_parameter_value_commands=set_value_commands,  # The commands to serialize it with
            result_details=details,
        )
        return result

    def check_response(self, response: object, class_to_check: type, attribute_to_retrieve: Any) -> Any:
        """Helper function for remake_duplicates to check whether response is of a particular type before getting an attribute.

        Args:
            response (object): The response object to retrieve the attribute from.
            class_to_check (type): The class the response needs to be part of in order to retrieve the attribute.
            attribute_to_retrieve (Any): The attribute the function will retrieve if it matches the type.

        Returns:
            attribute (Any): The attribute retrieved by the function, none if no attributes are retrieved.
        """
        attribute = None
        if isinstance(response, class_to_check):
            attribute = getattr(response, attribute_to_retrieve)
        return attribute

    def parameter_type(self, source_parameter_name: str, source_node_name: str) -> str:
        """Helper function to get type of a parameter in remake_duplicates.

        Args:
            source_parameter_name (str): The name of the parameter to get info from.
            source_node_name (str): The name of the node the parameter is part of.

        Returns:
            The type of the parameter is returned, or None if the request fails.

        """
        connection_info_request = GetParameterDetailsRequest(source_parameter_name, source_node_name)
        connection_info_response = GriptapeNodes.handle_request(connection_info_request)
        # only get value if it succeeds
        connection_type = NodeManager.check_response(
            self, connection_info_response, GetParameterDetailsResultSuccess, "type"
        )
        return connection_type

    def remake_connections(self, old_node_names: list[str], new_node_names: list[str]) -> None:
        """Remakes the incoming data connections and outgoing control connections.

        for a list of new_node_names, using the connections from the corresponding old_node_names.

        Args:
            old_node_names (list[str]): The old node names the connections are taken from.
            new_node_names (list[str]): The new node names the duplicate connections will be added to.

        Returns:
            None

        """
        # Since it is a duplicate, it makes sense to remake all the old incoming connections the original had
        for old_node_name, new_node_name in zip(old_node_names, new_node_names, strict=True):
            # List the old incoming connections (excluding internal node group connections)
            list_connections_for_node_request = ListConnectionsForNodeRequest(
                node_name=old_node_name, include_internal=False
            )
            list_connections_for_node_response = GriptapeNodes.handle_request(list_connections_for_node_request)

            # Only get incoming/outgoing connections if it returns the proper type
            incoming_connections = NodeManager.check_response(
                self, list_connections_for_node_response, ListConnectionsForNodeResultSuccess, "incoming_connections"
            )
            outgoing_connections = NodeManager.check_response(
                self, list_connections_for_node_response, ListConnectionsForNodeResultSuccess, "outgoing_connections"
            )

            # Check if none to prevent an error in the for loops
            if incoming_connections is None:
                incoming_connections = []
            if outgoing_connections is None:
                outgoing_connections = []

            # If there are any incoming connections, loop over them
            for incoming_connection in incoming_connections:
                # Define some variables to reduce verbosity
                source_parameter_name = incoming_connection.source_parameter_name
                source_node_name = incoming_connection.source_node_name
                target_parameter_name = incoming_connection.target_parameter_name

                # Don't remake connections that are between selected duplicated nodes
                if source_node_name in old_node_names:
                    continue

                # Get info about parameter
                connection_type = NodeManager.parameter_type(self, source_parameter_name, source_node_name)

                # Skip control connections when it's incoming
                if connection_type != ParameterTypeBuiltin.CONTROL_TYPE:
                    create_old_incoming_connections_request = CreateConnectionRequest(
                        source_node_name=source_node_name,
                        source_parameter_name=source_parameter_name,
                        target_node_name=new_node_name,
                        target_parameter_name=target_parameter_name,
                    )
                    GriptapeNodes.handle_request(create_old_incoming_connections_request)

            # If there are any outgoing connections, loop over them
            for outgoing_connection in outgoing_connections:
                # Define some variables to reduce verbosity
                source_parameter_name = outgoing_connection.source_parameter_name
                target_node_name = outgoing_connection.target_node_name
                target_parameter_name = outgoing_connection.target_parameter_name

                # Don't remake connections that are between selected duplicated nodes
                if target_node_name in old_node_names:
                    continue

                # Get info about parameter
                connection_type = NodeManager.parameter_type(self, source_parameter_name, new_node_name)

                # Only remake control connections when its outgoing
                if connection_type == ParameterTypeBuiltin.CONTROL_TYPE:
                    create_old_outgoing_connections_request = CreateConnectionRequest(
                        source_node_name=new_node_name,
                        source_parameter_name=outgoing_connection.source_parameter_name,
                        target_node_name=target_node_name,
                        target_parameter_name=outgoing_connection.target_parameter_name,
                    )
                    GriptapeNodes.handle_request(create_old_outgoing_connections_request)

    def on_deserialize_node_from_commands(self, request: DeserializeNodeFromCommandsRequest) -> ResultPayload:
        # Issue the creation command first.
        create_node_request = request.serialized_node_commands.create_node_command
        create_node_result = GriptapeNodes().handle_request(create_node_request)
        if not isinstance(create_node_result, CreateNodeResultSuccess):
            req_node_name = create_node_request.node_name
            details = f"Attempted to deserialize a serialized set of Node Creation commands. Failed to create node '{req_node_name}'."
            return DeserializeNodeFromCommandsResultFailure(result_details=details)

        # Adopt the newly-created node as our current context.
        node_name = create_node_result.node_name
        node = GriptapeNodes.ObjectManager().attempt_get_object_by_name_as_type(node_name, BaseNode)
        if node is None:
            details = f"Attempted to deserialize a serialized set of Node Creation commands. Failed to get node '{node_name}'."
            return DeserializeNodeFromCommandsResultFailure(result_details=details)
        with GriptapeNodes.ContextManager().node(node=node):
            for element_command in request.serialized_node_commands.element_modification_commands:
                # TODO: https://github.com/griptape-ai/griptape-nodes-engine/issues/4862
                # This isinstance allowlist must be updated by hand for every new
                # element-modification request type that carries a node_name. Any type
                # not listed here silently keeps pointing at the original node. Consider
                # retargeting any element command that exposes a node_name attribute instead.
                if isinstance(
                    element_command,
                    (
                        AlterParameterDetailsRequest,
                        AddParameterToNodeRequest,
                        AddParameterGroupToNodeRequest,
                        AlterParameterGroupDetailsRequest,
                    ),
                ):
                    element_command.node_name = node_name
                element_result = GriptapeNodes().handle_request(element_command)
                if element_result.failed():
                    details = f"Attempted to deserialize a serialized set of Node Creation commands. Failed to execute an element command for node '{node_name}'."
                    self._cleanup_node_on_failed_deserialization(node_name)
                    return DeserializeNodeFromCommandsResultFailure(result_details=details)
        details = f"Successfully deserialized a serialized set of Node Creation commands for node '{node_name}'."
        return DeserializeNodeFromCommandsResultSuccess(node_name=node_name, result_details=details)

    def on_serialize_selected_nodes_to_commands(  # noqa: C901, PLR0912, PLR0915
        self, request: SerializeSelectedNodesToCommandsRequest
    ) -> ResultPayload:
        """This will take the selected nodes in the Object manager and serialize them into commands."""
        # These have already been sorted by the time they were selected.
        nodes_to_serialize = request.nodes_to_serialize
        # This is node_uuid to the serialization command.
        node_commands = {}
        # Node Name to UUID
        node_name_to_uuid = {}
        connections_to_serialize = []
        # This is also node_uuid to the parameter serialization command.
        parameter_commands = {}
        # This is node_uuid to lock commands.
        lock_commands = {}
        # I need to store node names and parameter names to UUID
        unique_uuid_to_values = {}
        # And track how values map into that map.
        serialized_parameter_value_tracker = SerializedParameterValueTracker()
        selected_node_names = [values[0] for values in nodes_to_serialize]
        # Track explicitly selected names (for group children deduplication)
        explicitly_selected = set(selected_node_names)
        # Track all selected nodes (explicit + implicit children) for connection filtering
        all_selected_for_connections = set(selected_node_names)
        # Separate lists for ordering: children must come before parents
        child_node_commands_list = []

        for node_name, _ in nodes_to_serialize:
            # Check if this is a group node that needs special handling
            node = GriptapeNodes.ObjectManager().attempt_get_object_by_name_as_type(node_name, BaseNode)
            if node is None:
                details = f"Attempted to serialize a selection of Nodes. Failed to get node '{node_name}'."
                return SerializeNodeToCommandsResultFailure(result_details=details)

            if isinstance(node, BaseNodeGroup):
                # Use special method to handle group + children
                group_result = self._serialize_group_with_children(
                    group_node=node,
                    unique_uuid_to_values=unique_uuid_to_values,
                    serialized_parameter_value_tracker=serialized_parameter_value_tracker,
                )

                if group_result.group_command is None:
                    details = f"Attempted to serialize a selection of Nodes. Failed to serialize group '{node_name}'."
                    return SerializeNodeToCommandsResultFailure(result_details=details)

                # Process the group node command
                node_commands[node_name] = group_result.group_command
                node_name_to_uuid[node_name] = group_result.group_command.node_uuid
                parameter_commands[group_result.group_command.node_uuid] = group_result.group_parameter_commands
                lock_commands[group_result.group_command.node_uuid] = group_result.group_command.lock_node_command

                # Process each child node command and add to tracking structures
                for child_command in group_result.child_commands:
                    child_name = child_command.create_node_command.node_name
                    if not child_name:
                        details = f"Attempted to serialize group node '{node.name}'. Failed because child node command has no name."
                        return SerializeNodeToCommandsResultFailure(result_details=details)
                    if child_name in explicitly_selected and child_name in node_commands:
                        # We need to remove the explicitly selected name from the commands that already exist
                        node_commands.pop(child_name)
                        duplicated_node_uuid = node_name_to_uuid.pop(child_name)
                        parameter_commands.pop(duplicated_node_uuid)
                        lock_commands.pop(duplicated_node_uuid)
                    # Now we'll re-add everything else
                    child_node_commands_list.append(child_command)
                    node_name_to_uuid[child_name] = child_command.node_uuid
                    parameter_commands[child_command.node_uuid] = group_result.child_parameter_commands[
                        child_command.node_uuid
                    ]
                    lock_commands[child_command.node_uuid] = child_command.lock_node_command
                    # Add to connection filtering set
                    all_selected_for_connections.add(child_name)
                    # We need to somehow get connections here.

            else:
                # Not a group, regular node.
                # Check to make sure it hasn't been child serialized
                if node_name in node_name_to_uuid:
                    # We've already serialized this node as a child.
                    continue
                # Regular node - serialize normally
                result = self.on_serialize_node_to_commands(
                    SerializeNodeToCommandsRequest(
                        node_name=node_name,
                        unique_parameter_uuid_to_values=unique_uuid_to_values,
                        serialized_parameter_value_tracker=serialized_parameter_value_tracker,
                        use_pickling=True,
                    )
                )
                if not isinstance(result, SerializeNodeToCommandsResultSuccess):
                    details = f"Attempted to serialize a selection of Nodes. Failed to serialize {node_name}."
                    return SerializeNodeToCommandsResultFailure(result_details=details)
                node_commands[node_name] = result.serialized_node_commands
                node_name_to_uuid[node_name] = result.serialized_node_commands.node_uuid
                parameter_commands[result.serialized_node_commands.node_uuid] = result.set_parameter_value_commands
                lock_commands[result.serialized_node_commands.node_uuid] = (
                    result.serialized_node_commands.lock_node_command
                )
        for node_name in all_selected_for_connections:
            try:
                flow_name = self.get_node_parent_flow_by_name(node_name)
                GriptapeNodes.FlowManager().get_flow_by_name(flow_name)
            except Exception:
                details = f"Attempted to serialize a selection of Nodes. Failed to get the flow of node {node_name}. Cannot serialize connections for this node."
                logger.warning(details)
                continue
            connections = GriptapeNodes.FlowManager().get_connections()
            if node_name in connections.outgoing_index:
                node_connections = [
                    connections.connections[connection_id]
                    for category_dict in connections.outgoing_index[node_name].values()
                    for connection_id in category_dict
                ]
                for connection in node_connections:
                    # Include connections to both explicitly and implicitly selected nodes
                    if connection.target_node.name not in all_selected_for_connections:
                        continue
                    connections_to_serialize.append(connection)
        serialized_connections = []
        for connection in connections_to_serialize:
            source_node_uuid = node_name_to_uuid[connection.source_node.name]
            target_node_uuid = node_name_to_uuid[connection.target_node.name]
            serialized_connections.append(
                SerializedSelectedNodesCommands.IndirectConnectionSerialization(
                    source_node_uuid=source_node_uuid,
                    source_parameter_name=connection.source_parameter.name,
                    target_node_uuid=target_node_uuid,
                    target_parameter_name=connection.target_parameter.name,
                )
            )
        # Final result for serialized node commands
        # Groups must come before their children so deserialization can assign parent_group_name
        all_serialized_commands = list(node_commands.values()) + child_node_commands_list

        # Build node_names_in_order to match the actual command serialization order
        # This ensures remake_connections receives matching old/new node name lists
        # Exclude child nodes - only include explicitly selected nodes (groups and regular nodes)
        uuid_to_node_name = {uuid: name for name, uuid in node_name_to_uuid.items()}
        child_node_uuids = {cmd.node_uuid for cmd in child_node_commands_list}
        node_names_in_order = []
        for command in all_serialized_commands:
            # Skip child nodes - they're handled by their parent groups
            if command.node_uuid in child_node_uuids:
                continue
            node_name = uuid_to_node_name[command.node_uuid]
            node_names_in_order.append(node_name)
        final_result = SerializedSelectedNodesCommands(
            serialized_node_commands=all_serialized_commands,
            serialized_connection_commands=serialized_connections,
            set_parameter_value_commands=parameter_commands,
            set_lock_commands_per_node=lock_commands,
        )

        # Encode pickled bytes to latin-1 strings for JSON serialization
        encoded_values = {}
        for uuid, value in unique_uuid_to_values.items():
            if isinstance(value, bytes):
                # Pickled bytes - encode as latin-1 string for transport
                encoded_values[uuid] = value.decode("latin1")
            else:
                # Non-pickled value - keep as-is (for backward compatibility)
                encoded_values[uuid] = value

        # Pickle the commands object and encode as latin-1 string for transport
        pickled_commands_bytes = pickle.dumps(final_result)
        pickled_commands_string = pickled_commands_bytes.decode("latin1")
        return SerializeSelectedNodesToCommandsResultSuccess(
            pickled_commands_string,  # Send pickled string instead of object
            pickled_values=encoded_values,
            node_names_serialized=node_names_in_order,
            result_details=f"Successfully serialized {len(request.nodes_to_serialize)} selected nodes to commands.",
        )

    def on_deserialize_selected_nodes_from_commands(  # noqa: C901, PLR0912, PLR0915
        self,
        request: DeserializeSelectedNodesFromCommandsRequest,
    ) -> ResultPayload:
        # Decode latin-1 encoded pickled strings back to Python objects
        decoded_values = {}
        if request.pickled_values:
            for uuid, latin1_string in request.pickled_values.items():
                if isinstance(latin1_string, str):
                    try:
                        # Decode: latin-1 string → bytes → unpickled object
                        pickled_bytes = latin1_string.encode("latin1")
                        decoded_values[uuid] = pickle.loads(pickled_bytes)  # noqa: S301 Expecting this from the GUI.
                    except Exception:
                        details = f"Failed to unpickle parameter value for UUID {uuid}"
                        logger.warning(details)
                        # Keep original value if unpickling fails
                        decoded_values[uuid] = latin1_string
                else:
                    # Not a string, keep as-is
                    decoded_values[uuid] = latin1_string

        # Unpickle the commands string into SerializedSelectedNodesCommands
        try:
            pickled_commands_bytes = request.deserialize_commands.encode("latin1")
            commands = pickle.loads(pickled_commands_bytes)  # noqa: S301 Expecting this from the GUI.
        except Exception as e:
            details = f"Failed to unpickle commands: {e}"
            return DeserializeSelectedNodesFromCommandsResultFailure(result_details=details)
        connections = commands.serialized_connection_commands
        node_uuid_to_name = {}
        created_node_names: list[str] = []

        # Build a set of child node UUIDs to identify implicitly selected nodes
        child_node_uuids = set()
        for node_command in commands.serialized_node_commands:
            metadata = node_command.create_node_command.metadata
            if metadata and "_parent_group_uuid" in metadata:
                child_node_uuids.add(node_command.node_uuid)

        # Separate position index - only increments for explicitly selected nodes (not children)
        position_index = 0

        # Deserialize nodes
        for node_command in commands.serialized_node_commands:
            # Create a deepcopy of the metadata so the nodes don't all share the same position.
            node_command.create_node_command.metadata = copy.deepcopy(node_command.create_node_command.metadata)

            # Check if this node is an implicitly selected child
            is_child_node = node_command.node_uuid in child_node_uuids

            # Apply position only to explicitly selected nodes (not children)
            if not is_child_node and request.positions is not None and position_index < len(request.positions):
                if node_command.create_node_command.metadata is None:
                    node_command.create_node_command.metadata = {
                        "position": {
                            "x": request.positions[position_index][0],
                            "y": request.positions[position_index][1],
                        }
                    }
                else:
                    node_command.create_node_command.metadata["position"] = {
                        "x": request.positions[position_index][0],
                        "y": request.positions[position_index][1],
                    }
                position_index += 1

            # Assign parent_group_name for child nodes using their embedded parent group UUID
            metadata = node_command.create_node_command.metadata
            if metadata and "_parent_group_uuid" in metadata:
                parent_group_uuid = metadata["_parent_group_uuid"]
                if parent_group_uuid not in node_uuid_to_name:
                    logger.error("Parent group UUID %s not found in UUID mapping", parent_group_uuid)
                    return DeserializeSelectedNodesFromCommandsResultFailure(
                        result_details="Parent group UUID not found in UUID mapping"
                    )
                node_command.create_node_command.parent_group_name = node_uuid_to_name[parent_group_uuid]
                del metadata["_parent_group_uuid"]

            result = self.on_deserialize_node_from_commands(
                DeserializeNodeFromCommandsRequest(serialized_node_commands=node_command)
            )
            if not isinstance(result, DeserializeNodeFromCommandsResultSuccess):
                details = "Attempted to deserialize node but ran into an error on node serialization."
                self._cleanup_created_nodes(created_node_names)
                return DeserializeSelectedNodesFromCommandsResultFailure(result_details=details)

            created_node_names.append(result.node_name)
            node_uuid_to_name[node_command.node_uuid] = result.node_name
            node = GriptapeNodes.ObjectManager().attempt_get_object_by_name_as_type(result.node_name, BaseNode)
            if node is None:
                details = "Attempted to deserialize node but ran into an error on node serialization."
                self._cleanup_created_nodes(created_node_names)
                return DeserializeSelectedNodesFromCommandsResultFailure(result_details=details)
            with GriptapeNodes.ContextManager().node(node=node):
                parameter_commands = commands.set_parameter_value_commands[node_command.node_uuid]
                for parameter_command in parameter_commands:
                    param_request = parameter_command.set_parameter_value_command
                    # Set the Node name
                    param_request.node_name = result.node_name
                    # Set the new value from decoded_values
                    if decoded_values and parameter_command.unique_value_uuid in decoded_values:
                        value = decoded_values[parameter_command.unique_value_uuid]
                        # Using try-except-pass instead of contextlib.suppress because it's clearer.
                        try:  # noqa: SIM105
                            # If we're pasting multiple times - we need to create a new copy for each paste so they don't all have the same reference.
                            value = copy.deepcopy(value)
                        except Exception:  # noqa: S110
                            pass
                        param_request.value = value
                        set_parameter_result = GriptapeNodes.handle_request(
                            parameter_command.set_parameter_value_command
                        )
                        if not set_parameter_result.succeeded():
                            details = f"Failed to set parameter value for {param_request.parameter_name} on node {param_request.node_name}"
                            logger.warning(details)
                lock_command = commands.set_lock_commands_per_node[node_command.node_uuid]
                if lock_command is not None:
                    lock_node_result = GriptapeNodes.handle_request(lock_command)
                    if not lock_node_result.succeeded():
                        details = f"Failed to lock node {lock_command.node_name}"
                        logger.warning(details)

        # create Connections
        for connection_command in connections:
            connection_request = CreateConnectionRequest(
                source_node_name=node_uuid_to_name[connection_command.source_node_uuid],
                source_parameter_name=connection_command.source_parameter_name,
                target_node_name=node_uuid_to_name[connection_command.target_node_uuid],
                target_parameter_name=connection_command.target_parameter_name,
            )
            result = GriptapeNodes.handle_request(connection_request)
            if result.failed():
                details = f"Failed to create a connection between {connection_request.source_node_name} and {connection_request.target_node_name}"
                logger.warning(details)
        # Build both lists: all nodes and explicitly selected nodes (for remake_connections)
        all_node_names = list(node_uuid_to_name.values())
        explicit_node_names = [name for uuid, name in node_uuid_to_name.items() if uuid not in child_node_uuids]
        return DeserializeSelectedNodesFromCommandsResultSuccess(
            node_names=all_node_names,
            non_children_names=explicit_node_names,
            result_details=f"Successfully deserialized {len(node_uuid_to_name)} nodes from commands.",
        )

    def on_duplicate_selected_nodes(self, request: DuplicateSelectedNodesRequest) -> ResultPayload:
        serialize_result = GriptapeNodes.handle_request(
            SerializeSelectedNodesToCommandsRequest(nodes_to_serialize=request.nodes_to_duplicate)
        )
        if not isinstance(serialize_result, SerializeSelectedNodesToCommandsResultSuccess):
            details = "Failed to serialized selected nodes."
            return DuplicateSelectedNodesResultFailure(result_details=details)

        # Pass the pickled commands and values to deserialization
        deserialize_request = DeserializeSelectedNodesFromCommandsRequest(
            deserialize_commands=serialize_result.serialized_selected_node_commands,
            pickled_values=serialize_result.pickled_values,
            positions=request.positions,
        )
        result = GriptapeNodes.handle_request(deserialize_request)
        if not isinstance(result, DeserializeSelectedNodesFromCommandsResultSuccess):
            details = "Failed to deserialize selected nodes."
            return DuplicateSelectedNodesResultFailure(result_details=details)

        # Remake duplicate connections of node (only for explicitly selected nodes, not children)

        NodeManager.remake_connections(
            self, new_node_names=result.non_children_names, old_node_names=serialize_result.node_names_serialized
        )
        return DuplicateSelectedNodesResultSuccess(
            result.node_names,
            result_details=f"Successfully duplicated {len(serialize_result.node_names_serialized)} nodes.",
        )

    @staticmethod
    def _manage_alter_details(parameter: Parameter, base_node_obj: BaseNode) -> dict:
        base_param = base_node_obj.get_parameter_by_name(parameter.name)
        if base_param:
            diff = base_param.equals(parameter)
        else:
            return vars(parameter)
        return diff

    @staticmethod
    def _manage_alter_group_details(group: ParameterGroup, base_node_obj: BaseNode) -> dict:
        """Compare a ParameterGroup against its base version and return differences.

        Args:
            group: The current ParameterGroup to compare
            base_node_obj: The reference node containing the base version

        Returns:
            Dictionary of differences, or empty dict if no changes
        """
        base_group = base_node_obj.get_element_by_name_and_type(group.name, ParameterGroup)
        if base_group and isinstance(base_group, ParameterGroup):
            diff = base_group.equals(group)
        else:
            return {"ui_options": group.ui_options}
        return diff

    @staticmethod
    def _handle_value_hashing(  # noqa: PLR0913
        value: Any,
        serialized_parameter_value_tracker: SerializedParameterValueTracker,
        unique_parameter_uuid_to_values: dict,
        parameter: Parameter,
        parameter_name: str,
        node_name: str,
        *,
        is_output: bool,
        use_pickling: bool = False,
    ) -> SerializedNodeCommands.IndirectSetParameterValueCommand | None:
        try:
            hash(value)
            value_id = (type(value), value)
        except TypeError:
            # Couldn't get a hash. Use the object's ID
            value_id = id(value)

        tracker_status = serialized_parameter_value_tracker.get_tracker_state(value_id)
        match tracker_status:
            case SerializedParameterValueTracker.TrackerState.SERIALIZABLE:
                # We have a match on this value. We're all good.
                unique_uuid = serialized_parameter_value_tracker.get_uuid_for_value_hash(value_id)
            case SerializedParameterValueTracker.TrackerState.NOT_SERIALIZABLE:
                # This value is not serializable. Bail.
                return None
            case SerializedParameterValueTracker.TrackerState.NOT_IN_TRACKER:
                # This value is new for us.

                # Check if parameter is marked as non-serializable (e.g., ImageDrivers, PromptDrivers, file handles)
                if not parameter.serializable:
                    serialized_parameter_value_tracker.add_as_not_serializable(value_id)
                    return None

                # Check if we can serialize it.
                try:
                    pickle.dumps(value)
                except Exception:
                    # Not serializable; don't waste time on future attempts.
                    serialized_parameter_value_tracker.add_as_not_serializable(value_id)
                    # Bail.
                    return None
                # The value should be serialized. Add it to the map of uniques.
                unique_uuid = SerializedNodeCommands.UniqueParameterValueUUID(str(uuid4()))

                if use_pickling:
                    # Use pickle serialization via WorkflowManager
                    workflow_manager = GriptapeNodes.WorkflowManager()
                    pickled_bytes = workflow_manager._patch_and_pickle_object(value)
                    unique_parameter_uuid_to_values[unique_uuid] = pickled_bytes
                else:
                    # Use existing deep copy approach
                    try:
                        unique_parameter_uuid_to_values[unique_uuid] = copy.deepcopy(value)
                    except Exception:
                        details = f"Attempted to serialize parameter '{parameter_name}` on node '{node_name}'. The parameter value could not be copied. It will be serialized by value. If problems arise from this, ensure the type '{type(value)}' works with copy.deepcopy()."
                        logger.warning(details)
                        unique_parameter_uuid_to_values[unique_uuid] = value
                serialized_parameter_value_tracker.add_as_serializable(value_id, unique_uuid)

        # Serialize it
        set_value_command = SetParameterValueRequest(
            parameter_name=parameter_name,
            value=None,  # <- this will get overridden when instantiated
            is_output=is_output,
            initial_setup=True,
        )
        indirect_set_value_command = SerializedNodeCommands.IndirectSetParameterValueCommand(
            set_parameter_value_command=set_value_command,
            unique_value_uuid=unique_uuid,
        )
        return indirect_set_value_command

    @staticmethod
    def handle_parameter_value_saving(  # noqa: PLR0913
        parameter: Parameter,
        node: BaseNode,
        unique_parameter_uuid_to_values: dict[SerializedNodeCommands.UniqueParameterValueUUID, Any],
        serialized_parameter_value_tracker: SerializedParameterValueTracker,
        create_node_request: CreateNodeRequest,
        *,
        use_pickling: bool = False,
        serialize_all_parameter_values: bool = False,
    ) -> list[SerializedNodeCommands.IndirectSetParameterValueCommand] | None:
        """Generates code to save a parameter value for a node in a Griptape workflow.

        This function handles the process of creating commands that will reconstruct and set
        parameter values for nodes. It performs the following steps:
        1. Retrieves the parameter value from the node's parameter values or output values
        2. Checks if the value has already been created in our map of unique values
        3. If so, it records the unique value UUID for later correlation.
        4. If not, confirm that the value will serialize reliably. If so,it adds the value to the uniques map and records the new UUID.
        5. Creates a SetParameterValueRequest to reconstruct this for the node

        Args:
            parameter (Parameter): The parameter object containing metadata
            node (BaseNode): The node object that contains the parameter
            unique_parameter_uuid_to_values (dict[SerializedNodeCommands.UniqueParameterValueUUID, Any]): Dictionary mapping unique value UUIDs to values
            serialized_parameter_value_tracker (SerializedParameterValueTracker): Object mapping maintaining value hashes to unique value UUIDs, and non-serializable values
            create_node_request (CreateNodeRequest): The node creation request that will be modified if serialization fails
            use_pickling (bool): If True, use pickle-based serialization; if False, use deep copy
            serialize_all_parameter_values (bool): If True, save all parameter values regardless of whether they were explicitly set or match defaults

        Returns:
            None (if no value to be serialized) or an IndirectSetParameterValueCommand linking the value to the unique value map

        Notes:
            - Parameter output values take precedence over regular parameter values
            - For values that can be hashed, the value itself is used as the key in values_created
            - For unhashable values, the object's id is used as the key
            - The function will reuse already created values to avoid duplication
        """
        output_value = None
        internal_value = None
        if parameter.name in node.parameter_output_values:
            # Output values are more important.
            output_value = node.parameter_output_values[parameter.name]
        # Get the effective value to check if it matches the default
        effective_value = node.get_parameter_value(parameter.name)
        # Save the value if it was explicitly set OR if it equals the default value.
        # The latter ensures the default is preserved when loading workflows,
        # even if the code's default value changes later.
        # If serialize_all_parameter_values is True, save all parameter values regardless.
        if (
            serialize_all_parameter_values
            or parameter.name in node.parameter_values
            or (parameter.default_value is not None and effective_value == parameter.default_value)
        ):
            internal_value = effective_value
        # A parameter can have BOTH an internal (set) value and an output value; each is
        # serialized independently against the same tracker/uniques map.
        commands = []
        internal_command = NodeManager._serialize_one_parameter_value_for_save(
            value=internal_value,
            value_kind="set",
            is_output=False,
            parameter=parameter,
            node=node,
            unique_parameter_uuid_to_values=unique_parameter_uuid_to_values,
            serialized_parameter_value_tracker=serialized_parameter_value_tracker,
            create_node_request=create_node_request,
            use_pickling=use_pickling,
        )
        if internal_command is not None:
            commands.append(internal_command)
        output_command = NodeManager._serialize_one_parameter_value_for_save(
            value=output_value,
            value_kind="output",
            is_output=True,
            parameter=parameter,
            node=node,
            unique_parameter_uuid_to_values=unique_parameter_uuid_to_values,
            serialized_parameter_value_tracker=serialized_parameter_value_tracker,
            create_node_request=create_node_request,
            use_pickling=use_pickling,
        )
        if output_command is not None:
            commands.append(output_command)
        return commands or None

    @staticmethod
    def _serialize_one_parameter_value_for_save(  # noqa: PLR0913
        *,
        value: Any,
        value_kind: str,
        is_output: bool,
        parameter: Parameter,
        node: BaseNode,
        unique_parameter_uuid_to_values: dict[SerializedNodeCommands.UniqueParameterValueUUID, Any],
        serialized_parameter_value_tracker: SerializedParameterValueTracker,
        create_node_request: CreateNodeRequest,
        use_pickling: bool,
    ) -> SerializedNodeCommands.IndirectSetParameterValueCommand | None:
        """Serialize one of a parameter's values (internal-set or output) for workflow save.

        Returns the command to record on success. Returns None when there is nothing to record:
        either no value was present, the author opted out via serializable=False, or serialization
        genuinely failed. In the genuine-failure case, also emits a warning and marks the node
        UNRESOLVED — opt-outs are silent.

        value_kind is "set" or "output" and is used only in the warning text.
        """
        # No value of this kind was set on the node.
        if value is None:
            return None
        command = NodeManager._handle_value_hashing(
            value=value,
            serialized_parameter_value_tracker=serialized_parameter_value_tracker,
            unique_parameter_uuid_to_values=unique_parameter_uuid_to_values,
            parameter=parameter,
            is_output=is_output,
            parameter_name=parameter.name,
            node_name=node.name,
            use_pickling=use_pickling,
        )
        if command is not None:
            return command
        # Author opted out via serializable=False — silently skip; not a failure.
        if not parameter.serializable:
            return None
        # Genuine serialization failure — warn and mark unresolved.
        details = f"Attempted to serialize {value_kind} value for parameter '{parameter.name}' on node '{node.name}'. The {value_kind} value will not be restored in anything that attempts to deserialize or save this node. The value for this parameter was not serialized because it did not match Griptape Nodes' criteria for serializability. To remedy, either update the value's type to support serializability or mark the parameter as not serializable by setting serializable=False when creating the parameter."
        logger.warning(details)
        if isinstance(create_node_request, CreateNodeRequest):
            create_node_request.resolution = NodeResolutionState.UNRESOLVED.value
        return None

    @staticmethod
    def serialize_parameter_output_values(node: BaseNode, *, use_pickling: bool = False) -> SerializedParameterValues:
        """Serialize parameter output values with optional pickling for complex objects.

        Args:
            node: The node whose parameter output values should be serialized
            use_pickling: If True, use pickle-based serialization; if False, use safe_unstructure

        Returns:
            SerializedParameterValues containing:
            - parameter_output_values: Either raw values or UUID references if pickling was used
            - unique_parameter_uuid_to_values: Dictionary of pickled values (None if no pickling needed)
        """
        if not node.parameters:
            return SerializedParameterValues({}, None)

        if not use_pickling:
            return NodeManager._serialize_without_pickling(node)

        return NodeManager._serialize_with_pickling(node)

    @staticmethod
    def _serialize_without_pickling(node: BaseNode) -> SerializedParameterValues:
        """Serialize parameter values using safe_unstructure.

        Args:
            node: The node whose parameter values should be serialized

        Returns:
            SerializedParameterValues with no pickling
        """
        param_values = {}
        for param in node.parameters:
            if param.name in node.parameter_output_values:
                param_values[param.name] = node.parameter_output_values[param.name]
            else:
                param_values[param.name] = node.get_parameter_value(param.name)
        simple_values = safe_unstructure(param_values)
        return SerializedParameterValues(simple_values, None)

    @staticmethod
    def _serialize_with_pickling(
        node: BaseNode,
    ) -> SerializedParameterValues:
        """Serialize parameter values using pickle-based serialization with UUID references.

        Args:
            node: The node whose parameter values should be serialized

        Returns:
            SerializedParameterValues with pickled values
        """
        unique_parameter_uuid_to_values = {}
        serialized_parameter_value_tracker = SerializedParameterValueTracker()
        uuid_referenced_values = {}

        for parameter in node.parameters:
            param_name = parameter.name
            param_value = NodeManager._get_parameter_value_for_serialization(node, param_name)

            unique_uuid = NodeManager._process_parameter_for_pickling(
                param_value,
                param_name,
                serialized_parameter_value_tracker,
                unique_parameter_uuid_to_values,
                uuid_referenced_values,
            )

            uuid_referenced_values[param_name] = unique_uuid

        return SerializedParameterValues(uuid_referenced_values, unique_parameter_uuid_to_values or None)

    @staticmethod
    def _get_parameter_value_for_serialization(node: BaseNode, param_name: str) -> Any:
        """Get parameter value for serialization, checking output values first.

        Args:
            node: The node to get the parameter value from
            param_name: The parameter name

        Returns:
            The parameter value
        """
        if param_name in node.parameter_output_values:
            return node.parameter_output_values[param_name]
        return node.get_parameter_value(param_name)

    @staticmethod
    def _process_parameter_for_pickling(
        param_value: Any,
        param_name: str,
        tracker: SerializedParameterValueTracker,
        unique_parameter_uuid_to_values: dict,
        uuid_referenced_values: dict,
    ) -> SerializedNodeCommands.UniqueParameterValueUUID | None:
        """Process a parameter value for pickle-based serialization.

        Args:
            param_value: The value to serialize
            param_name: Parameter name for tracking
            tracker: Tracker for managing serialization state
            unique_parameter_uuid_to_values: Dictionary to store pickled values
            uuid_referenced_values: Dictionary to store UUID references

        Returns:
            UUID reference for the value, or None if not serializable
        """
        try:
            hash(param_value)
            value_id = param_value
        except TypeError:
            value_id = id(param_value)

        tracker_status = tracker.get_tracker_state(value_id)

        match tracker_status:
            case SerializedParameterValueTracker.TrackerState.SERIALIZABLE:
                return tracker.get_uuid_for_value_hash(value_id)
            case SerializedParameterValueTracker.TrackerState.NOT_SERIALIZABLE:
                uuid_referenced_values[param_name] = None
                return None
            case SerializedParameterValueTracker.TrackerState.NOT_IN_TRACKER:
                return NodeManager._handle_new_value_for_pickling(
                    param_value, param_name, tracker, unique_parameter_uuid_to_values, uuid_referenced_values
                )

    @staticmethod
    def _handle_new_value_for_pickling(
        param_value: Any,
        param_name: str,
        tracker: SerializedParameterValueTracker,
        unique_parameter_uuid_to_values: dict,
        uuid_referenced_values: dict,
    ) -> SerializedNodeCommands.UniqueParameterValueUUID | None:
        """Handle a new value that hasn't been seen before in pickling serialization.

        Args:
            param_value: The value to pickle
            param_name: Parameter name for tracking
            tracker: Tracker for managing serialization state
            unique_parameter_uuid_to_values: Dictionary to store pickled values
            uuid_referenced_values: Dictionary to store UUID references

        Returns:
            UUID reference for the value, or None if not serializable
        """
        try:
            hash(param_value)
            value_id = param_value
        except TypeError:
            value_id = id(param_value)

        try:
            workflow_manager = GriptapeNodes.WorkflowManager()
            pickled_bytes = workflow_manager._patch_and_pickle_object(param_value)
        except Exception:
            tracker.add_as_not_serializable(value_id)
            uuid_referenced_values[param_name] = None
            return None

        unique_uuid = SerializedNodeCommands.UniqueParameterValueUUID(str(uuid4()))
        unique_parameter_uuid_to_values[unique_uuid] = pickled_bytes
        tracker.add_as_serializable(value_id, unique_uuid)
        return unique_uuid

    def on_rename_parameter_request(self, request: RenameParameterRequest) -> ResultPayload:  # noqa: C901, PLR0911, PLR0912
        """Handle renaming a parameter on a node.

        Args:
            request: The rename parameter request containing the old and new parameter names

        Returns:
            ResultPayload: Success or failure result
        """
        # Get the node
        node_name = request.node_name
        if node_name is None:
            if not GriptapeNodes.ContextManager().has_current_node():
                details = "Attempted to rename Parameter in the Current Context. Failed because the Current Context was empty."
                return RenameParameterResultFailure(result_details=details)
            node = GriptapeNodes.ContextManager().get_current_node()
            node_name = node.name
        else:
            try:
                node = self.get_node_by_name(node_name)
            except KeyError as err:
                details = f"Attempted to rename Parameter '{request.parameter_name}' on Node '{node_name}'. Failed because the Node could not be found. Error: {err}"
                return RenameParameterResultFailure(result_details=details)

        # Is the node locked?
        if node.lock:
            details = f"Attempted to rename Parameter '{request.parameter_name}' on Node '{node_name}'. Failed because the Node is locked."
            return RenameParameterResultFailure(result_details=details)

        # Get the parameter
        parameter = node.get_parameter_by_name(request.parameter_name)
        if parameter is None:
            details = f"Attempted to rename Parameter '{request.parameter_name}' on Node '{node_name}'. Failed because the Parameter could not be found."
            return RenameParameterResultFailure(result_details=details)

        # Only allow parameter rename for user-defined params
        if not parameter.user_defined:
            details = f"Attempted to rename Parameter '{request.parameter_name}' on Node '{node_name}'. Failed because the Parameter is not user-defined."
            return RenameParameterResultFailure(result_details=details)

        # Validate the new parameter name
        if any(char.isspace() for char in request.new_parameter_name):
            details = f"Failed to rename Parameter '{request.parameter_name}' to '{request.new_parameter_name}'. Parameter names cannot contain any whitespace characters."
            return RenameParameterResultFailure(result_details=details)

        # Check for duplicate names
        if node.does_name_exist(request.new_parameter_name):
            details = f"Failed to rename Parameter '{request.parameter_name}' to '{request.new_parameter_name}'. A Parameter with that name already exists."
            return RenameParameterResultFailure(result_details=details)

        # Get all connections for this node
        flow_name = self.get_node_parent_flow_by_name(node_name)
        GriptapeNodes.FlowManager().get_flow_by_name(flow_name)
        connections = GriptapeNodes.FlowManager().get_connections()

        # Update connections that reference this parameter
        if node_name in connections.incoming_index:
            incoming_connections = connections.incoming_index[node_name]
            if request.parameter_name in incoming_connections:
                connection_ids = incoming_connections[request.parameter_name]
                for connection_id in connection_ids:
                    connection = connections.connections[connection_id]
                    if connection.target_parameter.name == request.parameter_name:
                        connection.target_parameter.name = request.new_parameter_name
                # Update the index key from old name to new name
                incoming_connections[request.new_parameter_name] = incoming_connections.pop(request.parameter_name)

        if node_name in connections.outgoing_index:
            outgoing_connections = connections.outgoing_index[node_name]
            if request.parameter_name in outgoing_connections:
                connection_ids = outgoing_connections[request.parameter_name]
                for connection_id in connection_ids:
                    connection = connections.connections[connection_id]
                    if connection.source_parameter.name == request.parameter_name:
                        connection.source_parameter.name = request.new_parameter_name
                # Update the index key from old name to new name
                outgoing_connections[request.new_parameter_name] = outgoing_connections.pop(request.parameter_name)

        # Update parameter name
        old_name = parameter.name
        parameter.name = request.new_parameter_name

        # Update parameter values if they exist
        if old_name in node.parameter_values:
            node.parameter_values[request.new_parameter_name] = node.parameter_values.pop(old_name)
        if old_name in node.parameter_output_values:
            node.parameter_output_values[request.new_parameter_name] = node.parameter_output_values.pop(old_name)

        return RenameParameterResultSuccess(
            old_parameter_name=old_name,
            new_parameter_name=request.new_parameter_name,
            node_name=node_name,
            result_details=f"Successfully renamed parameter '{old_name}' to '{request.new_parameter_name}' on node '{node_name}'.",
        )

    def on_toggle_lock_node_request(self, request: SetLockNodeStateRequest) -> ResultPayload:
        node_name = request.node_name
        if node_name is None:
            if not GriptapeNodes.ContextManager().has_current_node():
                details = "Attempted to lock node in the Current Context. Failed because the Current Context was empty."
                return SetLockNodeStateResultFailure(result_details=details)
            node = GriptapeNodes.ContextManager().get_current_node()
            node_name = node.name
        else:
            try:
                node = self.get_node_by_name(node_name)
            except ValueError as err:
                details = f"Attempted to lock node '{request.node_name}'. Failed because the Node could not be found. Error: {err}"
                return SetLockNodeStateResultFailure(result_details=details)
        node.lock = request.lock
        return SetLockNodeStateResultSuccess(
            node_name=node_name,
            locked=node.lock,
            result_details=f"Successfully set lock state to {node.lock} for node '{node_name}'.",
        )

    def on_batch_set_lock_node_state_request(self, request: BatchSetNodeLockStateRequest) -> ResultPayload:
        updated: list[str] = []
        failed: dict[str, str] = {}
        for name in request.node_names:
            try:
                node = self.get_node_by_name(name)
            except ValueError as err:
                failed[name] = f"Node not found. Error: {err}"
                continue
            node.lock = request.lock
            updated.append(name)

        if not updated:
            details = f"Failed to update any nodes. Failed: {failed}"
            return BatchSetNodeLockStateResultFailure(result_details=details)
        details = f"Successfully set lock state to {request.lock} for nodes: {', '.join(updated)}." + (
            f" Failed: {failed}" if failed else ""
        )
        return BatchSetNodeLockStateResultSuccess(
            updated_nodes=updated,
            failed_nodes=failed,
            result_details=details,
        )

    def on_send_node_message_request(self, request: SendNodeMessageRequest) -> ResultPayload:
        """Handle a SendNodeMessageRequest by calling the node's message callback.

        Args:
            request: The SendNodeMessageRequest containing message details

        Returns:
            ResultPayload: Success or failure result with callback response
        """
        node_name = request.node_name
        node = None

        if node_name is None:
            # Get from the current context
            if not GriptapeNodes.ContextManager().has_current_node():
                details = "Attempted to send message to Node from Current Context. Failed because the Current Context is empty."
                return SendNodeMessageResultFailure(result_details=details)

            node = GriptapeNodes.ContextManager().get_current_node()
            node_name = node.name

        if node is None:
            # Find the node by name
            obj_mgr = GriptapeNodes.ObjectManager()
            node = obj_mgr.attempt_get_object_by_name_as_type(node_name, BaseNode)
            if node is None:
                details = f"Attempted to send message to Node '{node_name}', but no such Node was found."
                return SendNodeMessageResultFailure(result_details=details)

        # Validate optional_element_name if specified
        if request.optional_element_name is not None:
            element = node.root_ui_element.find_element_by_name(request.optional_element_name)
            if element is None:
                details = f"Attempted to send message to Node '{node_name}' with element '{request.optional_element_name}', but no such element was found."
                return SendNodeMessageResultFailure(result_details=details, altered_workflow_state=False)

        # Call the node's message callback
        callback_result = node.on_node_message_received(
            optional_element_name=request.optional_element_name,
            message_type=request.message_type,
            message=request.message,
        )

        if not callback_result.success:
            details = f"Failed to handle message for Node '{node_name}': {callback_result.details}"
            return SendNodeMessageResultFailure(
                result_details=callback_result.details,
                response=callback_result.response,
                altered_workflow_state=callback_result.altered_workflow_state,
            )

        details = f"Successfully sent message to Node '{node_name}': {callback_result.details}"
        return SendNodeMessageResultSuccess(
            result_details=callback_result.details,
            response=callback_result.response,
            altered_workflow_state=callback_result.altered_workflow_state,
        )

    def on_get_flow_for_node_request(self, request: GetFlowForNodeRequest) -> ResultPayload:
        """Get the flow name that contains a specific node."""
        try:
            flow_name = self.get_node_parent_flow_by_name(request.node_name)
            return GetFlowForNodeResultSuccess(
                flow_name=flow_name,
                result_details=f"Successfully retrieved flow '{flow_name}' for node '{request.node_name}'.",
            )
        except KeyError:
            return GetFlowForNodeResultFailure(
                result_details=f"Node '{request.node_name}' not found or not assigned to any flow.",
            )

    def on_migrate_parameter_request(
        self, request: MigrateParameterRequest
    ) -> MigrateParameterResultFailure | MigrateParameterResultSuccess:
        """Handle parameter migration requests."""
        # Validate nodes exist - get_node_by_name can raise ValueError
        try:
            source_node = self.get_node_by_name(request.source_node_name)
            target_node = self.get_node_by_name(request.target_node_name)
            logger.debug(
                "Successfully validated nodes exist for parameter migration: %s -> %s",
                source_node.name,
                target_node.name,
            )
        except ValueError as e:
            return MigrateParameterResultFailure(result_details=f"Node validation failed: {e}")

        # Get connections for the source parameter
        connections_result = self.on_get_connections_for_parameter_request(
            GetConnectionsForParameterRequest(
                parameter_name=request.source_parameter_name, node_name=request.source_node_name
            )
        )

        if not isinstance(connections_result, GetConnectionsForParameterResultSuccess):
            return MigrateParameterResultFailure(
                result_details=f"Failed to get connections for parameter '{request.source_parameter_name}' on node '{request.source_node_name}'."
            )

        # Break original connections if requested (do this FIRST before creating new connections)
        if request.break_connections:
            self._break_parameter_connections(
                connections_result, request.source_node_name, request.source_parameter_name
            )

        # Handle incoming connections
        if connections_result.has_incoming_connections():
            result = self._migrate_incoming_connections(request, connections_result)
            if isinstance(result, MigrateParameterResultFailure):
                return result

        # Handle outgoing connections
        if connections_result.has_outgoing_connections():
            result = self._migrate_outgoing_connections(request, connections_result)
            if isinstance(result, MigrateParameterResultFailure):
                return result

        # Handle value migration (no incoming connections)
        if not connections_result.has_incoming_connections():
            result = self._migrate_parameter_value(request)
            if isinstance(result, MigrateParameterResultFailure):
                return result

        return MigrateParameterResultSuccess(
            result_details=f"Successfully migrated parameter '{request.source_parameter_name}' from '{request.source_node_name}' to '{request.target_parameter_name}' on '{request.target_node_name}'."
        )

    def _break_parameter_connections(
        self,
        connections_result: GetConnectionsForParameterResultSuccess,
        source_node_name: str,
        source_parameter_name: str,
    ) -> None:
        """Break all incoming and outgoing connections for a parameter."""
        # Break incoming connections
        for incoming_connection in connections_result.incoming_connections:
            delete_result = GriptapeNodes.handle_request(
                DeleteConnectionRequest(
                    source_node_name=incoming_connection.source_node_name,
                    source_parameter_name=incoming_connection.source_parameter_name,
                    target_node_name=source_node_name,
                    target_parameter_name=source_parameter_name,
                )
            )
            if not isinstance(delete_result, DeleteConnectionResultSuccess):
                logger.warning(
                    "Failed to break incoming connection from %s.%s: %s",
                    incoming_connection.source_node_name,
                    incoming_connection.source_parameter_name,
                    delete_result,
                )

        # Break outgoing connections
        for outgoing_connection in connections_result.outgoing_connections:
            delete_result = GriptapeNodes.handle_request(
                DeleteConnectionRequest(
                    source_node_name=source_node_name,
                    source_parameter_name=source_parameter_name,
                    target_node_name=outgoing_connection.target_node_name,
                    target_parameter_name=outgoing_connection.target_parameter_name,
                )
            )
            if not isinstance(delete_result, DeleteConnectionResultSuccess):
                logger.warning(
                    "Failed to break outgoing connection to %s.%s: %s",
                    outgoing_connection.target_node_name,
                    outgoing_connection.target_parameter_name,
                    delete_result,
                )

    def _migrate_incoming_connections(
        self, request: MigrateParameterRequest, connections_result: GetConnectionsForParameterResultSuccess
    ) -> MigrateParameterResultFailure | None:
        """Handle migrating incoming connections with or without conversion."""
        if request.input_conversion:
            return self._create_input_conversion_node(request, connections_result)
        return self._create_direct_incoming_connections(request, connections_result)

    def _migrate_outgoing_connections(
        self, request: MigrateParameterRequest, connections_result: GetConnectionsForParameterResultSuccess
    ) -> MigrateParameterResultFailure | None:
        """Handle migrating outgoing connections with or without conversion."""
        if request.output_conversion:
            return self._create_output_conversion_node(request, connections_result)
        return self._create_direct_outgoing_connections(request, connections_result)

    def _migrate_parameter_value(self, request: MigrateParameterRequest) -> MigrateParameterResultFailure | None:
        """Handle migrating parameter value when no incoming connections exist."""
        # Get the current value from source
        get_value_result = GriptapeNodes.handle_request(
            GetParameterValueRequest(node_name=request.source_node_name, parameter_name=request.source_parameter_name)
        )

        if not isinstance(get_value_result, GetParameterValueResultSuccess):
            return MigrateParameterResultFailure(
                result_details=f"Failed to get value for parameter '{request.source_parameter_name}' on node '{request.source_node_name}'."
            )

        # Apply transformation if provided - this is user code that can raise exceptions
        value = get_value_result.value
        if request.value_transform:
            try:
                value = request.value_transform(value)
            except Exception as e:
                return MigrateParameterResultFailure(result_details=f"Failed to apply value transformation: {e!s}")

        # Set the value on target
        set_value_result = GriptapeNodes.handle_request(
            SetParameterValueRequest(
                node_name=request.target_node_name, parameter_name=request.target_parameter_name, value=value
            )
        )

        if not isinstance(set_value_result, SetParameterValueResultSuccess):
            return MigrateParameterResultFailure(
                result_details=f"Failed to set value for parameter '{request.target_parameter_name}' on node '{request.target_node_name}'."
            )

        return None

    def _create_input_conversion_node(
        self, request: MigrateParameterRequest, connections_result: GetConnectionsForParameterResultSuccess
    ) -> MigrateParameterResultFailure | None:
        """Create intermediate node for input conversion."""
        intermediate_node_name = f"{request.target_node_name}_{request.source_parameter_name}_input_converter"
        input_conversion = request.input_conversion
        if input_conversion is None:
            return MigrateParameterResultFailure(result_details="Input conversion configuration is required")

        # Create the intermediate node
        offset_side = input_conversion.offset_side or "left"
        create_node_result = RetainedMode.create_node_relative_to(
            reference_node_name=request.target_node_name,
            new_node_type=input_conversion.node_type,
            new_node_name=intermediate_node_name,
            specific_library_name=input_conversion.library,
            offset_side=offset_side,  # type: ignore[arg-type]
            offset_x=input_conversion.offset_x,
            offset_y=input_conversion.offset_y,
        )

        if not isinstance(create_node_result, str):
            return MigrateParameterResultFailure(
                result_details=f"Failed to create intermediate node '{intermediate_node_name}': {create_node_result}"
            )

        # Set additional parameters
        if input_conversion.additional_parameters:
            for param_name, param_value in input_conversion.additional_parameters.items():
                set_value_result = GriptapeNodes.handle_request(
                    SetParameterValueRequest(
                        node_name=intermediate_node_name, parameter_name=param_name, value=param_value
                    )
                )
                if not isinstance(set_value_result, SetParameterValueResultSuccess):
                    return MigrateParameterResultFailure(
                        result_details=f"Failed to set parameter '{param_name}' on intermediate node '{intermediate_node_name}': {set_value_result}"
                    )

        # Connect all sources to intermediate node
        for incoming_connection in connections_result.incoming_connections:
            connection_result = GriptapeNodes.handle_request(
                CreateConnectionRequest(
                    source_node_name=incoming_connection.source_node_name,
                    source_parameter_name=incoming_connection.source_parameter_name,
                    target_node_name=intermediate_node_name,
                    target_parameter_name=input_conversion.input_parameter,
                )
            )

            if not isinstance(connection_result, CreateConnectionResultSuccess):
                return MigrateParameterResultFailure(
                    result_details=f"Failed to connect source '{incoming_connection.source_node_name}.{incoming_connection.source_parameter_name}' to intermediate node: {connection_result}"
                )

        # Connect intermediate node to target
        connection_result = GriptapeNodes.handle_request(
            CreateConnectionRequest(
                source_node_name=intermediate_node_name,
                source_parameter_name=input_conversion.output_parameter,
                target_node_name=request.target_node_name,
                target_parameter_name=request.target_parameter_name,
            )
        )

        if not isinstance(connection_result, CreateConnectionResultSuccess):
            return MigrateParameterResultFailure(
                result_details=f"Failed to connect intermediate node to target: {connection_result}"
            )

        return None

    def _create_direct_incoming_connections(
        self, request: MigrateParameterRequest, connections_result: GetConnectionsForParameterResultSuccess
    ) -> MigrateParameterResultFailure | None:
        """Create direct incoming connections without conversion."""
        for incoming_connection in connections_result.incoming_connections:
            connection_result = GriptapeNodes.handle_request(
                CreateConnectionRequest(
                    source_node_name=incoming_connection.source_node_name,
                    source_parameter_name=incoming_connection.source_parameter_name,
                    target_node_name=request.target_node_name,
                    target_parameter_name=request.target_parameter_name,
                )
            )

            if not isinstance(connection_result, CreateConnectionResultSuccess):
                return MigrateParameterResultFailure(
                    result_details=f"Failed to create direct connection from '{incoming_connection.source_node_name}.{incoming_connection.source_parameter_name}': {connection_result}"
                )

        return None

    def _create_output_conversion_node(
        self, request: MigrateParameterRequest, connections_result: GetConnectionsForParameterResultSuccess
    ) -> MigrateParameterResultFailure | None:
        """Create intermediate node for output conversion."""
        intermediate_node_name = f"{request.target_node_name}_{request.source_parameter_name}_output_converter"
        output_conversion = request.output_conversion
        if output_conversion is None:
            return MigrateParameterResultFailure(result_details="Output conversion configuration is required")

        # Create the intermediate node
        offset_side = output_conversion.offset_side or "right"
        create_node_result = RetainedMode.create_node_relative_to(
            reference_node_name=request.target_node_name,
            new_node_type=output_conversion.node_type,
            new_node_name=intermediate_node_name,
            specific_library_name=output_conversion.library,
            offset_side=offset_side,  # type: ignore[arg-type]
            offset_x=output_conversion.offset_x,
            offset_y=output_conversion.offset_y,
        )

        if not isinstance(create_node_result, str):
            return MigrateParameterResultFailure(
                result_details=f"Failed to create intermediate node '{intermediate_node_name}': {create_node_result}"
            )

        # Set additional parameters
        if output_conversion.additional_parameters:
            for param_name, param_value in output_conversion.additional_parameters.items():
                set_value_result = GriptapeNodes.handle_request(
                    SetParameterValueRequest(
                        node_name=intermediate_node_name, parameter_name=param_name, value=param_value
                    )
                )
                if not isinstance(set_value_result, SetParameterValueResultSuccess):
                    return MigrateParameterResultFailure(
                        result_details=f"Failed to set parameter '{param_name}' on intermediate node '{intermediate_node_name}': {set_value_result}"
                    )

        # Connect target to intermediate node
        connection_result = GriptapeNodes.handle_request(
            CreateConnectionRequest(
                source_node_name=request.target_node_name,
                source_parameter_name=request.target_parameter_name,
                target_node_name=intermediate_node_name,
                target_parameter_name=output_conversion.input_parameter,
            )
        )

        if not isinstance(connection_result, CreateConnectionResultSuccess):
            return MigrateParameterResultFailure(
                result_details=f"Failed to connect target to intermediate node: {connection_result}"
            )

        # Connect intermediate node to all destinations
        for outgoing_connection in connections_result.outgoing_connections:
            connection_result = GriptapeNodes.handle_request(
                CreateConnectionRequest(
                    source_node_name=intermediate_node_name,
                    source_parameter_name=output_conversion.output_parameter,
                    target_node_name=outgoing_connection.target_node_name,
                    target_parameter_name=outgoing_connection.target_parameter_name,
                )
            )

            if not isinstance(connection_result, CreateConnectionResultSuccess):
                return MigrateParameterResultFailure(
                    result_details=f"Failed to connect intermediate node to destination '{outgoing_connection.target_node_name}.{outgoing_connection.target_parameter_name}': {connection_result}"
                )

        return None

    def _create_direct_outgoing_connections(
        self, request: MigrateParameterRequest, connections_result: GetConnectionsForParameterResultSuccess
    ) -> MigrateParameterResultFailure | None:
        """Create direct outgoing connections without conversion."""
        for outgoing_connection in connections_result.outgoing_connections:
            connection_result = GriptapeNodes.handle_request(
                CreateConnectionRequest(
                    source_node_name=request.target_node_name,
                    source_parameter_name=request.target_parameter_name,
                    target_node_name=outgoing_connection.target_node_name,
                    target_parameter_name=outgoing_connection.target_parameter_name,
                )
            )

            if not isinstance(connection_result, CreateConnectionResultSuccess):
                return MigrateParameterResultFailure(
                    result_details=f"Failed to create direct connection to '{outgoing_connection.target_node_name}.{outgoing_connection.target_parameter_name}': {connection_result}"
                )

        return None

    def _check_can_reset_node(self, node: BaseNode) -> CanResetResult:
        """Check if a node can be reset to defaults.

        Args:
            node: The node to check

        Returns:
            CanResetResult with can_reset flag and optional tooltip reason
        """
        if node.lock:
            return CanResetResult(
                can_reset=False,
                editor_tooltip_reason="Node is locked. Unlock the node in order to reset it.",
            )

        return CanResetResult(can_reset=True, editor_tooltip_reason=None)

    def on_can_reset_node_to_defaults_request(self, request: CanResetNodeToDefaultsRequest) -> ResultPayload:
        """Check if a node can be reset to its default state."""
        node_name = request.node_name
        node = None

        # FAILURE CHECK: Validate node_name
        if node_name is None:
            if not GriptapeNodes.ContextManager().has_current_node():
                details = (
                    "Attempted to check reset eligibility for a Node from the Current Context. "
                    "Failed because the Current Context is empty."
                )
                return CanResetNodeToDefaultsResultFailure(result_details=details)
            node = GriptapeNodes.ContextManager().get_current_node()
            node_name = node.name

        # FAILURE CHECK: Get source node
        if node is None:
            node = GriptapeNodes.ObjectManager().attempt_get_object_by_name_as_type(node_name, BaseNode)
        if node is None:
            details = f"Attempted to check reset eligibility for Node '{node_name}', but no such Node was found."
            return CanResetNodeToDefaultsResultFailure(result_details=details)

        # FAILURE CHECK: Get node type and library
        if "library" not in node.metadata:
            details = (
                f"Attempted to check reset eligibility for Node '{node_name}'. "
                f"Failed because node has no library information in metadata."
            )
            return CanResetNodeToDefaultsResultFailure(result_details=details)

        # Check if node can be reset
        can_reset_result = self._check_can_reset_node(node)
        if not can_reset_result.can_reset:
            details = f"Node '{node_name}' cannot be reset: {can_reset_result.editor_tooltip_reason}"
            return CanResetNodeToDefaultsResultSuccess(
                can_reset=False,
                editor_tooltip_reason=can_reset_result.editor_tooltip_reason,
                result_details=details,
            )

        # SUCCESS PATH: Node can be reset
        details = f"Node '{node_name}' can be reset to defaults."
        return CanResetNodeToDefaultsResultSuccess(
            can_reset=True,
            editor_tooltip_reason=None,
            result_details=details,
        )

    def on_reset_node_to_defaults_request(self, request: ResetNodeToDefaultsRequest) -> ResultPayload:  # noqa: C901, PLR0911, PLR0912, PLR0915
        """Reset a node to its default state while preserving connections where possible."""
        node_name = request.node_name
        node = None

        # FAILURE CHECK: Validate node_name
        if node_name is None:
            if not GriptapeNodes.ContextManager().has_current_node():
                details = (
                    "Attempted to reset a Node from the Current Context. Failed because the Current Context is empty."
                )
                return ResetNodeToDefaultsResultFailure(result_details=details)
            node = GriptapeNodes.ContextManager().get_current_node()
            node_name = node.name

        # FAILURE CHECK: Get source node
        if node is None:
            node = GriptapeNodes.ObjectManager().attempt_get_object_by_name_as_type(node_name, BaseNode)
        if node is None:
            details = f"Attempted to reset Node '{node_name}', but no such Node was found."
            return ResetNodeToDefaultsResultFailure(result_details=details)

        # FAILURE CHECK: Get node type and library
        node_type = node.__class__.__name__
        if "library" not in node.metadata:
            details = (
                f"Attempted to reset Node '{node_name}'. Failed because node has no library information in metadata."
            )
            return ResetNodeToDefaultsResultFailure(result_details=details)
        library_name = node.metadata["library"]

        # FAILURE CHECK: Check if node can be reset
        can_reset_result = self._check_can_reset_node(node)
        if not can_reset_result.can_reset:
            details = f"Attempted to reset Node '{node_name}'. Failed because: {can_reset_result.editor_tooltip_reason}"
            return ResetNodeToDefaultsResultFailure(result_details=details)

        # FAILURE CHECK: Gather node information
        all_info_request = GetAllNodeInfoRequest(node_name=node_name)
        all_info_result = self.on_get_all_node_info_request(all_info_request)
        if not isinstance(all_info_result, GetAllNodeInfoResultSuccess):
            details = f"Attempted to reset Node '{node_name}'. Failed to get node information."
            return ResetNodeToDefaultsResultFailure(result_details=details)

        connections = all_info_result.connections

        # FAILURE CHECK: Get parent flow name
        if node_name not in self._name_to_parent_flow_name:
            details = f"Attempted to reset Node '{node_name}'. Failed to find parent flow name."
            return ResetNodeToDefaultsResultFailure(result_details=details)
        parent_flow_name = self._name_to_parent_flow_name[node_name]

        # Preserve parent group membership
        parent_group_name = node.parent_group.name if node.parent_group else None

        # FAILURE CHECK: Create new node with temporary name
        temp_node_name = f"{node_name}_temp"
        create_node_request = CreateNodeRequest(
            node_type=node_type,
            specific_library_name=library_name,
            node_name=temp_node_name,
            override_parent_flow_name=parent_flow_name,
            parent_group_name=parent_group_name,
            create_error_proxy_on_failure=False,
        )
        create_result = self.on_create_node_request(create_node_request)
        if not isinstance(create_result, CreateNodeResultSuccess):
            details = f"Attempted to reset Node '{node_name}'. Failed to create new node of type '{node_type}'."
            return ResetNodeToDefaultsResultFailure(result_details=details)
        new_node_name = create_result.node_name

        # TODO: (griptape-nodes) Don't rely on manually copying metadata fields. https://github.com/griptape-ai/griptape-nodes/issues/2862
        # Copy only position and size from original node's metadata to preserve layout.
        # We don't copy the full metadata because it contains instance-specific data that shouldn't be transferred.
        artifact_metadata = all_info_result.metadata
        new_node = self.get_node_by_name(new_node_name)
        if "position" in artifact_metadata:
            new_node.metadata["position"] = copy.deepcopy(artifact_metadata["position"])
        if "size" in artifact_metadata:
            new_node.metadata["size"] = copy.deepcopy(artifact_metadata["size"])

        # NON-FATAL: Attempt to reconnect connections
        failed_incoming: list[IncomingConnection] = []
        failed_outgoing: list[OutgoingConnection] = []

        for incoming_connection in connections.incoming_connections:
            connection_request = CreateConnectionRequest(
                source_node_name=incoming_connection.source_node_name,
                source_parameter_name=incoming_connection.source_parameter_name,
                target_node_name=new_node_name,
                target_parameter_name=incoming_connection.target_parameter_name,
            )
            connection_result = GriptapeNodes.FlowManager().on_create_connection_request(connection_request)
            if not isinstance(connection_result, CreateConnectionResultSuccess):
                failed_incoming.append(incoming_connection)

        for outgoing_connection in connections.outgoing_connections:
            connection_request = CreateConnectionRequest(
                source_node_name=new_node_name,
                source_parameter_name=outgoing_connection.source_parameter_name,
                target_node_name=outgoing_connection.target_node_name,
                target_parameter_name=outgoing_connection.target_parameter_name,
            )
            connection_result = GriptapeNodes.FlowManager().on_create_connection_request(connection_request)
            if not isinstance(connection_result, CreateConnectionResultSuccess):
                failed_outgoing.append(outgoing_connection)

        # FAILURE CHECK: Delete source node
        delete_request = DeleteNodeRequest(node_name=node_name)
        delete_result = self.on_delete_node_request(delete_request)
        if not isinstance(delete_result, DeleteNodeResultSuccess):
            details = f"Attempted to reset Node '{node_name}'. Failed to delete original node."
            return ResetNodeToDefaultsResultFailure(result_details=details)

        # FAILURE CHECK: Rename new node to original name
        rename_request = RenameObjectRequest(
            object_name=new_node_name, requested_name=node_name, allow_next_closest_name_available=False
        )
        rename_result = GriptapeNodes.ObjectManager().on_rename_object_request(rename_request)
        if not isinstance(rename_result, RenameObjectResultSuccess):
            details = f"Attempted to reset Node '{node_name}'. Failed to rename new node to original name."
            return ResetNodeToDefaultsResultFailure(result_details=details)

        # SUCCESS PATH
        if not failed_incoming and not failed_outgoing:
            details = f"Successfully reset node '{node_name}' to defaults."
            log_level = logging.DEBUG
        else:
            details = f"Successfully reset node '{node_name}' but one or more connections could not be restored."
            if failed_incoming:
                source_node_names = {conn.source_node_name for conn in failed_incoming}
                details += f" Connections FROM the following nodes were not restored: {source_node_names}."
            if failed_outgoing:
                target_node_names = {conn.target_node_name for conn in failed_outgoing}
                details += f" Connections TO the following nodes were not restored: {target_node_names}."
            log_level = logging.WARNING

        return ResetNodeToDefaultsResultSuccess(
            node_name=node_name,
            failed_incoming_connections=failed_incoming,
            failed_outgoing_connections=failed_outgoing,
            result_details=ResultDetails(message=details, level=log_level),
        )

    def on_reorder_parameter_list_item_request(self, request: ReorderParameterListItemRequest) -> ResultPayload:  # noqa: PLR0911
        """Handle reordering an item within a ParameterList.

        Args:
            request: The reorder request containing the parameter list name and indices

        Returns:
            ResultPayload: Success or failure result
        """
        # Get the node
        node_name = request.node_name
        if node_name is None:
            if not GriptapeNodes.ContextManager().has_current_node():
                details = "Attempted to reorder ParameterList item in the Current Context. Failed because the Current Context was empty."
                return ReorderParameterListItemResultFailure(result_details=details)
            node = GriptapeNodes.ContextManager().get_current_node()
            node_name = node.name
        else:
            try:
                node = self.get_node_by_name(node_name)
            except KeyError as err:
                details = f"Attempted to reorder item in ParameterList '{request.parameter_list_name}' on Node '{node_name}'. Failed because the Node could not be found. Error: {err}"
                return ReorderParameterListItemResultFailure(result_details=details)

        # Is the node locked?
        if node.lock:
            details = f"Attempted to reorder item in ParameterList '{request.parameter_list_name}' on Node '{node_name}'. Failed because the Node is locked."
            return ReorderParameterListItemResultFailure(result_details=details)

        # Get the ParameterList
        parameter_list = node.get_parameter_by_name(request.parameter_list_name)
        if parameter_list is None:
            details = f"Attempted to reorder item in ParameterList '{request.parameter_list_name}' on Node '{node_name}'. Failed because the ParameterList could not be found."
            return ReorderParameterListItemResultFailure(result_details=details)

        # Validate it's actually a ParameterList
        if not isinstance(parameter_list, ParameterList):
            details = f"Attempted to reorder item in ParameterList '{request.parameter_list_name}' on Node '{node_name}'. Failed because '{request.parameter_list_name}' is not a ParameterList."
            return ReorderParameterListItemResultFailure(result_details=details)

        # Get child parameters
        children = parameter_list.get_child_parameters()
        list_length = len(children)

        # Validate indices
        if request.from_index < 0 or request.from_index >= list_length:
            details = f"Attempted to reorder item in ParameterList '{request.parameter_list_name}' on Node '{node_name}'. Failed because from_index {request.from_index} is out of range (list has {list_length} items)."
            return ReorderParameterListItemResultFailure(result_details=details)

        if request.to_index < 0 or request.to_index >= list_length:
            details = f"Attempted to reorder item in ParameterList '{request.parameter_list_name}' on Node '{node_name}'. Failed because to_index {request.to_index} is out of range (list has {list_length} items)."
            return ReorderParameterListItemResultFailure(result_details=details)

        # Early-out success: if indices are the same, item is already in correct position
        if request.from_index == request.to_index:
            details = f"Item in ParameterList '{request.parameter_list_name}' on Node '{node_name}' is already at index {request.from_index}. No reordering needed."
            return ReorderParameterListItemResultSuccess(
                result_details=ResultDetails(message=details, level=logging.WARNING)
            )

        # Perform the reorder by moving the item in the _children list
        item_to_move = children[request.from_index]
        parameter_list._children.remove(item_to_move)
        parameter_list._children.insert(request.to_index, item_to_move)

        # Mark the node as unresolved since parameter structure changed
        node.state = NodeResolutionState.UNRESOLVED

        # Manually add the reordered children structure to _changes so the event includes it
        parameter_list._changes["children"] = [child.to_dict() for child in parameter_list._children]

        # Emit update event for the parameter list with the reordered children
        parameter_list._emit_alter_element_event_if_possible()

        return ReorderParameterListItemResultSuccess(
            result_details=f"Successfully reordered item in ParameterList '{request.parameter_list_name}' on Node '{node_name}' from index {request.from_index} to {request.to_index}."
        )
