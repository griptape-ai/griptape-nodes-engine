import contextlib
import logging

import pytest

from griptape_nodes.exe_types.core_types import Parameter
from griptape_nodes.exe_types.node_types import BaseNode
from griptape_nodes.retained_mode.events.node_events import (
    BatchSetNodeMetadataRequest,
    BatchSetNodeMetadataResultFailure,
    BatchSetNodeMetadataResultSuccess,
)
from griptape_nodes.retained_mode.events.parameter_events import AlterParameterDetailsRequest
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes


class TestNodeManagerBatchSetNodeMetadata:
    """Test the batch_set_node_metadata functionality in NodeManager."""

    def test_batch_set_node_metadata_empty_request_succeeds(self) -> None:
        """Test that an empty batch request succeeds without errors."""
        # Create an empty batch request
        request = BatchSetNodeMetadataRequest(node_metadata_updates={})

        # Execute the batch update through GriptapeNodes
        result = GriptapeNodes.handle_request(request)

        # Should succeed even with no updates
        assert isinstance(result, BatchSetNodeMetadataResultSuccess)
        assert result.updated_nodes == []
        assert result.failed_nodes == {}

    def test_batch_set_node_metadata_all_nodes_not_found_fails(self) -> None:
        """Test that batch update fails when all nodes are not found."""
        # Create request with non-existent nodes
        request = BatchSetNodeMetadataRequest(
            node_metadata_updates={
                "nonexistent_node1": {"position": {"x": 100, "y": 200}},
                "nonexistent_node2": {"position": {"x": 300, "y": 400}},
            }
        )

        # Execute the batch update through GriptapeNodes
        result = GriptapeNodes.handle_request(request)

        # Should fail because all nodes failed to be found
        assert isinstance(result, BatchSetNodeMetadataResultFailure)
        # Check that the error message contains expected information
        result_str = str(result.result_details)
        assert "Failed to update any nodes" in result_str
        assert "nonexistent_node1" in result_str
        assert "nonexistent_node2" in result_str


class TestNodeManagerResolutionStateSerialization:
    """Test that node resolution states are preserved correctly during serialization."""

    def test_resolved_node_with_no_parameter_value_preserves_resolution(self) -> None:
        """Test that a resolved node with no parameter value set maintains its resolution state."""
        from unittest.mock import MagicMock

        from griptape_nodes.exe_types.core_types import Parameter
        from griptape_nodes.exe_types.node_types import BaseNode, NodeResolutionState
        from griptape_nodes.retained_mode.events.node_events import CreateNodeRequest
        from griptape_nodes.retained_mode.managers.node_manager import NodeManager

        # Create a simple parameter and node
        mock_parameter = MagicMock(spec=Parameter)
        mock_parameter.name = "test_param"

        mock_node = MagicMock(spec=BaseNode)
        mock_node.name = "test_node"
        mock_node.parameter_values = {}  # No value set
        mock_node.parameter_output_values = {}  # No output value

        # Start with resolved state
        create_node_request = CreateNodeRequest(
            node_type="TestNode", node_name="test_node", resolution=NodeResolutionState.RESOLVED.value
        )

        # Call the function
        result = NodeManager.handle_parameter_value_saving(
            parameter=mock_parameter,
            node=mock_node,
            unique_parameter_uuid_to_values={},
            serialized_parameter_value_tracker=MagicMock(),
            create_node_request=create_node_request,
        )

        # Should return None (no values to serialize) but preserve resolution
        assert result is None
        assert create_node_request.resolution == NodeResolutionState.RESOLVED.value

    def test_resolved_node_with_unserializable_parameter_becomes_unresolved(self) -> None:
        """Test that a resolved node becomes unresolved when parameter serialization fails."""
        from unittest.mock import MagicMock

        from griptape_nodes.exe_types.core_types import Parameter
        from griptape_nodes.exe_types.node_types import BaseNode, NodeResolutionState
        from griptape_nodes.retained_mode.events.node_events import CreateNodeRequest
        from griptape_nodes.retained_mode.managers.node_manager import NodeManager, SerializedParameterValueTracker

        # Create parameter with unserializable value
        mock_parameter = MagicMock(spec=Parameter)
        mock_parameter.name = "test_param"
        mock_parameter.serializable = True

        mock_node = MagicMock(spec=BaseNode)
        mock_node.name = "test_node"
        mock_node.parameter_values = {"test_param": "has_value"}
        mock_node.parameter_output_values = {}
        mock_node.get_parameter_value.return_value = "some_value"

        create_node_request = CreateNodeRequest(
            node_type="TestNode", node_name="test_node", resolution=NodeResolutionState.RESOLVED.value
        )

        # Mock tracker to return NOT_SERIALIZABLE to simulate serialization failure
        mock_tracker = MagicMock()
        mock_tracker.get_tracker_state.return_value = SerializedParameterValueTracker.TrackerState.NOT_SERIALIZABLE

        # Call the function - this should trigger the serialization failure path
        NodeManager.handle_parameter_value_saving(
            parameter=mock_parameter,
            node=mock_node,
            unique_parameter_uuid_to_values={},
            serialized_parameter_value_tracker=mock_tracker,
            create_node_request=create_node_request,
        )

        # Resolution should be reset to UNRESOLVED due to serialization failure
        assert create_node_request.resolution == NodeResolutionState.UNRESOLVED.value


class TestNodeManagerAlterParameterDetailsClearDefaultValue:
    """Test AlterParameterDetailsRequest behavior when clear_default_value and default_value are both set."""

    def test_clear_default_value_with_default_value_logs_warning_and_clears(
        self, griptape_nodes: GriptapeNodes, caplog: pytest.LogCaptureFixture
    ) -> None:
        """When both clear_default_value and default_value are provided, default is cleared and a warning is logged."""
        parameter = Parameter(name="test_param", default_value="original_value")
        request = AlterParameterDetailsRequest(
            parameter_name="test_param",
            node_name="test_node",
            clear_default_value=True,
            default_value="ignored_value",
        )

        caplog.clear()
        caplog.set_level(logging.WARNING)

        griptape_nodes.NodeManager().modify_key_parameter_fields(request, parameter)

        assert parameter.default_value is None
        assert len(caplog.records) == 1
        assert caplog.records[0].levelno == logging.WARNING
        assert "Conflicting options" in caplog.records[0].message
        assert "clear_default_value takes precedence" in caplog.records[0].message
        assert "test_param" in caplog.records[0].message
        assert "test_node" in caplog.records[0].message


class TestGetParameterValueOutputPriority:
    """Output values must take priority over input/property values in GetParameterValueRequest."""

    def test_output_value_takes_priority_over_parameter_value(self, griptape_nodes: GriptapeNodes) -> None:
        """When both parameter_values and parameter_output_values contain a key, the output value wins."""
        from unittest.mock import MagicMock

        from griptape_nodes.exe_types.core_types import Parameter
        from griptape_nodes.exe_types.node_types import BaseNode
        from griptape_nodes.retained_mode.events.parameter_events import (
            GetParameterValueRequest,
            GetParameterValueResultSuccess,
        )

        input_value: float = 0.0
        output_value: float = 42.0

        param = Parameter(name="result", type="float", default_value=input_value)
        mock_node = MagicMock(spec=BaseNode)
        mock_node.name = "test_node"
        mock_node.parameter_values = {"result": input_value}
        mock_node.parameter_output_values = {"result": output_value}
        mock_node.get_parameter_by_name.return_value = param

        obj_mgr = griptape_nodes.ObjectManager()
        obj_mgr.add_object_by_name("test_node", mock_node)

        node_manager = griptape_nodes.NodeManager()
        request = GetParameterValueRequest(parameter_name="result", node_name="test_node")
        result = node_manager.on_get_parameter_value_request(request)

        assert isinstance(result, GetParameterValueResultSuccess)
        assert result.value == output_value

    def test_falls_back_to_parameter_value_when_no_output(self, griptape_nodes: GriptapeNodes) -> None:
        """When parameter_output_values is empty, parameter_values is used."""
        from unittest.mock import MagicMock

        from griptape_nodes.exe_types.core_types import Parameter
        from griptape_nodes.exe_types.node_types import BaseNode
        from griptape_nodes.retained_mode.events.parameter_events import (
            GetParameterValueRequest,
            GetParameterValueResultSuccess,
        )

        input_value: float = 3.0

        param = Parameter(name="a", type="float", default_value=0.0)
        mock_node = MagicMock(spec=BaseNode)
        mock_node.name = "test_node"
        mock_node.parameter_values = {"a": input_value}
        mock_node.parameter_output_values = {}
        mock_node.get_parameter_by_name.return_value = param

        obj_mgr = griptape_nodes.ObjectManager()
        obj_mgr.add_object_by_name("test_node", mock_node)

        node_manager = griptape_nodes.NodeManager()
        request = GetParameterValueRequest(parameter_name="a", node_name="test_node")
        result = node_manager.on_get_parameter_value_request(request)

        assert isinstance(result, GetParameterValueResultSuccess)
        assert result.value == input_value

    def test_falls_back_to_default_when_no_values(self, griptape_nodes: GriptapeNodes) -> None:
        """When neither dict contains the key, the parameter default is returned."""
        from unittest.mock import MagicMock

        from griptape_nodes.exe_types.core_types import Parameter
        from griptape_nodes.exe_types.node_types import BaseNode
        from griptape_nodes.retained_mode.events.parameter_events import (
            GetParameterValueRequest,
            GetParameterValueResultSuccess,
        )

        default_value: float = 99.0

        param = Parameter(name="x", type="float", default_value=default_value)
        mock_node = MagicMock(spec=BaseNode)
        mock_node.name = "test_node"
        mock_node.parameter_values = {}
        mock_node.parameter_output_values = {}
        mock_node.get_parameter_by_name.return_value = param

        obj_mgr = griptape_nodes.ObjectManager()
        obj_mgr.add_object_by_name("test_node", mock_node)

        node_manager = griptape_nodes.NodeManager()
        request = GetParameterValueRequest(parameter_name="x", node_name="test_node")
        result = node_manager.on_get_parameter_value_request(request)

        assert isinstance(result, GetParameterValueResultSuccess)
        assert result.value == default_value


class TestNodeManagerCancelExecuteNode:
    """Tests for the CancelExecuteNodeRequest handler and cancel_worker_execution dispatch."""

    @pytest.mark.asyncio
    async def test_handler_no_inflight_returns_success(self, griptape_nodes: GriptapeNodes) -> None:
        """Cancelling a request_id that isn't tracked is idempotent success."""
        from griptape_nodes.retained_mode.events.execution_events import (
            CancelExecuteNodeRequest,
            CancelExecuteNodeResultSuccess,
        )

        node_manager = griptape_nodes.NodeManager()
        request = CancelExecuteNodeRequest(target_request_id="not-tracked")

        result = await node_manager.on_cancel_execute_node_request(request)

        assert isinstance(result, CancelExecuteNodeResultSuccess)

    @pytest.mark.asyncio
    async def test_handler_cancels_tracked_task_and_sets_flag(self, griptape_nodes: GriptapeNodes) -> None:
        """With an in-flight task registered, the handler sets the node's cancel flag and cancels the task."""
        import asyncio

        from griptape_nodes.exe_types.node_types import BaseNode
        from griptape_nodes.retained_mode.events.execution_events import (
            CancelExecuteNodeRequest,
            CancelExecuteNodeResultSuccess,
        )

        node_manager = griptape_nodes.NodeManager()

        cancellation_seen = {"value": False}

        async def long_running() -> None:
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                cancellation_seen["value"] = True
                raise

        task = asyncio.create_task(long_running())
        # Give the task a chance to start
        await asyncio.sleep(0)

        node = BaseNode(name="n1")
        try:
            node_manager._worker_inflight_aprocesses["req-1"] = (task, node)

            result = await node_manager.on_cancel_execute_node_request(
                CancelExecuteNodeRequest(target_request_id="req-1")
            )

            assert isinstance(result, CancelExecuteNodeResultSuccess)
            assert node.is_cancellation_requested is True

            # Let the cancellation propagate
            with contextlib.suppress(asyncio.CancelledError):
                await task
            assert cancellation_seen["value"] is True
        finally:
            node_manager._worker_inflight_aprocesses.pop("req-1", None)
            if not task.done():
                task.cancel()

    @pytest.mark.asyncio
    async def test_cancel_worker_execution_noop_when_not_tracked(self, griptape_nodes: GriptapeNodes) -> None:
        """cancel_worker_execution is a no-op when the node isn't routed to a worker."""
        node_manager = griptape_nodes.NodeManager()

        # Should not raise; there is no entry for "ghost_node" and
        # WorkerManager.forward_event_to_worker should not be invoked.
        await node_manager.cancel_worker_execution("ghost_node")


class TestDeserializeNodeFromCommandsRetargetsElementCommands:
    """Deserializing a node (copy/paste) must retarget every element command at the new copy.

    This includes ParameterGroup commands, not just parameter commands.
    Previously the isinstance check only covered AddParameterToNodeRequest and
    AlterParameterDetailsRequest, so AddParameterGroupToNodeRequest / AlterParameterGroupDetailsRequest
    kept pointing at the original node. Copy-pasting a node with user-defined ParameterGroups then
    failed with "an element with that name already exists" because the group was re-added to the
    original node.
    """

    def test_group_commands_node_name_retargeted_to_copy(self) -> None:
        from unittest.mock import MagicMock, patch

        from griptape_nodes.exe_types.node_types import BaseNode
        from griptape_nodes.retained_mode.events.base_events import ResultPayload
        from griptape_nodes.retained_mode.events.node_events import (
            CreateNodeRequest,
            CreateNodeResultSuccess,
            DeserializeNodeFromCommandsRequest,
            DeserializeNodeFromCommandsResultSuccess,
            SerializedNodeCommands,
        )
        from griptape_nodes.retained_mode.events.parameter_events import (
            AddParameterGroupToNodeRequest,
            AddParameterToNodeRequest,
            AlterParameterGroupDetailsRequest,
        )
        from griptape_nodes.retained_mode.managers.node_manager import NodeManager

        original_name = "OriginalNode"
        copy_name = "OriginalNode_1"

        element_commands = [
            AddParameterGroupToNodeRequest(node_name=original_name, group_name="exif_data"),
            AddParameterToNodeRequest(node_name=original_name, parameter_name="width", type="int"),
            AlterParameterGroupDetailsRequest(node_name=original_name, group_name="exif_data"),
        ]
        serialized = SerializedNodeCommands(
            create_node_command=CreateNodeRequest(node_type="ReadImageMetadata", node_name=original_name),
            element_modification_commands=element_commands,
            node_dependencies=MagicMock(),
            node_uuid=SerializedNodeCommands.NodeUUID("uuid-1"),
        )
        request = DeserializeNodeFromCommandsRequest(serialized_node_commands=serialized)

        manager = NodeManager(MagicMock())

        create_result = CreateNodeResultSuccess(
            node_name=copy_name,
            node_type="ReadImageMetadata",
            specific_library_name=None,
            parent_flow_name=None,
            result_details=MagicMock(),
        )

        def fake_handle_request(req: object) -> ResultPayload:
            success = MagicMock()
            success.failed.return_value = False
            return create_result if req is request.serialized_node_commands.create_node_command else success

        mock_node = MagicMock(spec=BaseNode)
        with (
            patch("griptape_nodes.retained_mode.managers.node_manager.GriptapeNodes") as mock_griptape_nodes,
            patch.object(NodeManager, "_cleanup_node_on_failed_deserialization"),
        ):
            mock_griptape_nodes.return_value.handle_request.side_effect = fake_handle_request
            mock_griptape_nodes.ObjectManager.return_value.attempt_get_object_by_name_as_type.return_value = mock_node

            result = manager.on_deserialize_node_from_commands(request)

        assert isinstance(result, DeserializeNodeFromCommandsResultSuccess)
        assert result.node_name == copy_name
        # Every element command must have been retargeted at the new copy, not the original node.
        for command in element_commands:
            assert command.node_name == copy_name


class _GateProbe(BaseNode):
    """Concrete BaseNode used to exercise node-instantiation checkpoint resolution."""

    def __init__(self, name: str, metadata=None) -> None:  # noqa: ANN001
        super().__init__(name=name, metadata=metadata)


class TestNodeInstantiationAuthorizationCheckpoint:
    """The license-policy checkpoint wired into node instantiation."""

    _LIBRARY_NAME = "node-checkpoint-test-library"

    @pytest.fixture(autouse=True)
    def _clean_registry(self):  # noqa: ANN202
        from griptape_nodes.node_library.library_registry import LibraryRegistry

        stores = ("_libraries", "_node_aliases", "_collision_node_names_to_library_names", "_registered_widgets")
        for store in stores:
            getattr(LibraryRegistry, store).clear()
        yield
        for store in stores:
            getattr(LibraryRegistry, store).clear()

    def _register(self, node_declarations=(), library_declarations=()):  # noqa: ANN001, ANN202
        from griptape_nodes.node_library.library_registry import (
            LibraryMetadata,
            LibraryRegistry,
            LibrarySchema,
            NodeMetadata,
        )

        schema = LibrarySchema(
            name=self._LIBRARY_NAME,
            library_schema_version=LibrarySchema.LATEST_SCHEMA_VERSION,
            metadata=LibraryMetadata(
                author="t",
                description="d",
                library_version="1.0.0",
                engine_version="1.0.0",
                tags=[],
                declarations=list(library_declarations),
            ),
            categories=[],
            nodes=[],
        )
        library = LibraryRegistry.generate_new_library(library_data=schema)
        library.register_new_node_type(
            _GateProbe,
            NodeMetadata(category="t", description="d", display_name="Probe", declarations=list(node_declarations)),
        )
        return LibraryRegistry.get_library_for_node_type(_GateProbe.__name__, self._LIBRARY_NAME)

    @staticmethod
    def _attrs(library):  # noqa: ANN001, ANN205
        from griptape_nodes.retained_mode.managers.node_manager import NodeManager

        return NodeManager._node_checkpoint_attributes(
            node_type=_GateProbe.__name__,
            node_declarations=library.get_node_metadata(_GateProbe.__name__).declarations,
            library_declarations=library.get_metadata().declarations,
        )

    def test_node_override_stage_wins(self) -> None:
        from griptape_nodes.node_library.library_declarations import (
            LifecycleStage,
            LifecycleStageLibraryProperty,
            LifecycleStageNodeProperty,
        )

        library = self._register(
            node_declarations=[LifecycleStageNodeProperty(stage=LifecycleStage.LABS)],
            library_declarations=[LifecycleStageLibraryProperty(stage=LifecycleStage.STABLE)],
        )
        attrs = self._attrs(library)
        assert attrs["id"] == _GateProbe.__name__
        assert attrs["lifecycle_stage"] == "LABS"
        assert attrs["executes_arbitrary_code"] is False

    def test_inherits_library_stage_then_unstated(self) -> None:
        from griptape_nodes.node_library.library_declarations import (
            LifecycleStage,
            LifecycleStageLibraryProperty,
        )

        inherit = self._register(library_declarations=[LifecycleStageLibraryProperty(stage=LifecycleStage.BETA)])
        assert self._attrs(inherit)["lifecycle_stage"] == "BETA"

        # Re-register with neither stated -> lifecycle_stage omitted entirely.
        for store in ("_libraries", "_node_aliases", "_collision_node_names_to_library_names", "_registered_widgets"):
            from griptape_nodes.node_library.library_registry import LibraryRegistry

            getattr(LibraryRegistry, store).clear()
        unstated = self._register()
        assert "lifecycle_stage" not in self._attrs(unstated)

    def test_arbitrary_code_flag(self) -> None:
        from griptape_nodes.node_library.library_declarations import ArbitraryPythonExecutionNodeProperty

        library = self._register(
            node_declarations=[ArbitraryPythonExecutionNodeProperty(executes_arbitrary_python=True)]
        )
        assert self._attrs(library)["executes_arbitrary_code"] is True

    def test_enforce_raises_on_denial(self, griptape_nodes: GriptapeNodes) -> None:
        from griptape_nodes.node_library.library_declarations import LifecycleStage, LifecycleStageNodeProperty
        from griptape_nodes.retained_mode.managers.authorization_checkpoint import CheckpointDenial, CheckpointFailure
        from griptape_nodes.retained_mode.managers.node_manager import NodeManager, _NodeInstantiationDeniedError

        self._register(node_declarations=[LifecycleStageNodeProperty(stage=LifecycleStage.LABS)])

        seen: dict[str, object] = {}

        def deny(checkpoint: object) -> CheckpointDenial:
            seen["action"] = checkpoint.action  # type: ignore[attr-defined]
            seen["stage"] = checkpoint.attributes.get("lifecycle_stage")  # type: ignore[attr-defined]
            return CheckpointDenial(failures=(CheckpointFailure(detail="Ask your admin to enable Labs nodes."),))

        griptape_nodes.EventManager().add_authorization_hook(deny)
        with pytest.raises(_NodeInstantiationDeniedError, match="Ask your admin to enable Labs nodes"):
            NodeManager._enforce_instantiation_checkpoint(
                node_type=_GateProbe.__name__, specific_library_name=self._LIBRARY_NAME
            )
        assert seen == {"action": "InstantiateNode", "stage": "LABS"}

    def test_enforce_allows_without_hook(self, griptape_nodes: GriptapeNodes) -> None:  # noqa: ARG002
        from griptape_nodes.retained_mode.managers.node_manager import NodeManager

        self._register()
        # No hook registered -> no denial, no raise.
        NodeManager._enforce_instantiation_checkpoint(
            node_type=_GateProbe.__name__, specific_library_name=self._LIBRARY_NAME
        )

    def test_schema_preview_returns_denied_node_types(self, griptape_nodes: GriptapeNodes) -> None:
        from griptape_nodes.node_library.library_declarations import LifecycleStage, LifecycleStageNodeProperty
        from griptape_nodes.retained_mode.managers.authorization_checkpoint import CheckpointDenial, CheckpointFailure
        from griptape_nodes.retained_mode.managers.node_manager import NodeManager

        schema = self._schema_with_node(node_declarations=[LifecycleStageNodeProperty(stage=LifecycleStage.LABS)])

        def deny(checkpoint: object) -> CheckpointDenial | None:
            if checkpoint.attributes.get("lifecycle_stage") == "LABS":  # type: ignore[attr-defined]
                return CheckpointDenial(failures=(CheckpointFailure(detail="Labs nodes are disabled."),))
            return None

        griptape_nodes.EventManager().add_authorization_hook(deny)
        denials = NodeManager.evaluate_schema_node_instantiation_denials(schema)
        assert set(denials) == {_GateProbe.__name__}
        assert denials[_GateProbe.__name__].messages() == ["Labs nodes are disabled."]

    def test_schema_preview_empty_without_hook(self, griptape_nodes: GriptapeNodes) -> None:  # noqa: ARG002
        from griptape_nodes.retained_mode.managers.node_manager import NodeManager

        # No hook -> nothing denied.
        assert NodeManager.evaluate_schema_node_instantiation_denials(self._schema_with_node()) == {}

    def test_model_usage_resolves_catalog_facts(self) -> None:
        from griptape_nodes.node_library.library_declarations import ModelUsageNodeProperty
        from griptape_nodes.retained_mode.managers.node_manager import NodeManager

        attrs = NodeManager._node_checkpoint_attributes(
            node_type="ModelNode",
            node_declarations=[ModelUsageNodeProperty(model_ids=["claude-opus-4"])],
            library_declarations=[self._catalog()],
        )
        assert attrs["model_ids"] == ["claude-opus-4"]
        assert attrs["provider_ids"] == ["anthropic"]
        assert attrs["model_families"] == ["Claude 4"]

    def test_provider_usage_resolves_provider_and_its_models(self) -> None:
        from griptape_nodes.node_library.library_declarations import ModelProviderUsageNodeProperty
        from griptape_nodes.retained_mode.managers.node_manager import NodeManager

        attrs = NodeManager._node_checkpoint_attributes(
            node_type="ProviderNode",
            node_declarations=[ModelProviderUsageNodeProperty(provider_ids=["anthropic"])],
            library_declarations=[self._catalog()],
        )
        assert attrs["provider_ids"] == ["anthropic"]
        # The whole provider expands to its catalog models.
        assert attrs["model_ids"] == ["claude-opus-4", "claude-sonnet-4"]
        assert attrs["model_families"] == ["Claude 4"]

    def test_provider_usage_without_catalog_models_still_gates_provider(self) -> None:
        from griptape_nodes.node_library.library_declarations import ModelProviderUsageNodeProperty
        from griptape_nodes.retained_mode.managers.node_manager import NodeManager

        # No catalog declared: the directly declared provider id is still surfaced
        # so a provider-level policy can match, but no model ids or families exist.
        attrs = NodeManager._node_checkpoint_attributes(
            node_type="ProviderNode",
            node_declarations=[ModelProviderUsageNodeProperty(provider_ids=["ollama"])],
            library_declarations=[],
        )
        assert attrs["provider_ids"] == ["ollama"]
        assert "model_ids" not in attrs
        assert "model_families" not in attrs

    def test_non_model_node_has_no_model_facts(self) -> None:
        from griptape_nodes.retained_mode.managers.node_manager import NodeManager

        attrs = NodeManager._node_checkpoint_attributes(
            node_type="Plain", node_declarations=[], library_declarations=[self._catalog()]
        )
        assert "model_ids" not in attrs
        assert "provider_ids" not in attrs
        assert "model_families" not in attrs

    def test_model_facts_reach_the_checkpoint_and_can_be_denied(self, griptape_nodes: GriptapeNodes) -> None:
        from griptape_nodes.node_library.library_declarations import ModelUsageNodeProperty
        from griptape_nodes.retained_mode.managers.authorization_checkpoint import CheckpointDenial, CheckpointFailure
        from griptape_nodes.retained_mode.managers.node_manager import NodeManager

        schema = self._schema_with_node(
            node_declarations=[ModelUsageNodeProperty(model_ids=["claude-opus-4"])],
            library_declarations=[self._catalog()],
        )

        seen: dict[str, object] = {}

        def deny(checkpoint: object) -> CheckpointDenial | None:
            seen["provider_ids"] = checkpoint.attributes.get("provider_ids")  # type: ignore[attr-defined]
            seen["model_families"] = checkpoint.attributes.get("model_families")  # type: ignore[attr-defined]
            if "anthropic" in (checkpoint.attributes.get("provider_ids") or []):  # type: ignore[attr-defined]
                return CheckpointDenial(failures=(CheckpointFailure(detail="Anthropic is not enabled."),))
            return None

        griptape_nodes.EventManager().add_authorization_hook(deny)
        denials = NodeManager.evaluate_schema_node_instantiation_denials(schema)
        assert seen == {"provider_ids": ["anthropic"], "model_families": ["Claude 4"]}
        assert set(denials) == {_GateProbe.__name__}
        assert denials[_GateProbe.__name__].messages() == ["Anthropic is not enabled."]

    @staticmethod
    def _catalog():  # noqa: ANN205
        from griptape_nodes.node_library.library_declarations import (
            KeySupport,
            Model,
            ModelCatalogLibraryProperty,
            ModelProvider,
        )

        return ModelCatalogLibraryProperty(
            providers={
                "anthropic": ModelProvider(
                    display_name="Anthropic",
                    models={
                        "claude-opus-4": Model(
                            display_name="Opus", family="Claude 4", key_support=KeySupport.REQUIRES_CUSTOMER_KEY
                        ),
                        "claude-sonnet-4": Model(
                            display_name="Sonnet", family="Claude 4", key_support=KeySupport.REQUIRES_CUSTOMER_KEY
                        ),
                    },
                )
            }
        )

    @staticmethod
    def _schema_with_node(node_declarations=(), library_declarations=()):  # noqa: ANN001, ANN205
        from griptape_nodes.node_library.library_registry import (
            LibraryMetadata,
            LibrarySchema,
            NodeDefinition,
            NodeMetadata,
        )

        return LibrarySchema(
            name="preview-lib",
            library_schema_version=LibrarySchema.LATEST_SCHEMA_VERSION,
            metadata=LibraryMetadata(
                author="t",
                description="d",
                library_version="1.0.0",
                engine_version="1.0.0",
                tags=[],
                declarations=list(library_declarations),
            ),
            categories=[],
            nodes=[
                NodeDefinition(
                    class_name=_GateProbe.__name__,
                    file_path="probe.py",
                    metadata=NodeMetadata(
                        category="t", description="d", display_name="Probe", declarations=list(node_declarations)
                    ),
                )
            ],
        )
