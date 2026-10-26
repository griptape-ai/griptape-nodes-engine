"""Every way a node joins a workflow leaves `node.workflow` naming it; nodes built to inspect have none."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

import pytest

from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
from griptape_nodes.exe_types.node_groups.base_node_group import BaseNodeGroup
from griptape_nodes.exe_types.node_groups.subflow_node_group import SubflowNodeGroup
from griptape_nodes.exe_types.node_types import BaseNode, DataNode, EndNode, ErrorProxyNode, StartNode
from griptape_nodes.node_library.library_registry import LibraryMetadata, LibraryRegistry, LibrarySchema, NodeMetadata
from griptape_nodes.node_library.workflow_registry import WorkflowMetadata, new_workflow_id
from griptape_nodes.retained_mode.events.connection_events import CreateConnectionRequest, CreateConnectionResultSuccess
from griptape_nodes.retained_mode.events.execution_events import CurrentControlNodeEvent, ExecuteNodeRequest
from griptape_nodes.retained_mode.events.flow_events import (
    CreateFlowRequest,
    CreateFlowResultSuccess,
    DeserializeFlowFromCommandsRequest,
    DeserializeFlowFromCommandsResultSuccess,
    ImportWorkflowAsReferencedSubFlowRequest,
    PackageNodesAsSerializedFlowRequest,
    PackageNodesAsSerializedFlowResultSuccess,
    SerializeFlowToCommandsRequest,
    SerializeFlowToCommandsResultSuccess,
)
from griptape_nodes.retained_mode.events.library_events import (
    DescribeNodeTypeRequest,
    DescribeNodeTypeResultSuccess,
)
from griptape_nodes.retained_mode.events.node_events import (
    CreateNodeRequest,
    CreateNodeResultSuccess,
    DeleteNodeRequest,
    DeleteNodeResultSuccess,
    DeserializeSelectedNodesFromCommandsRequest,
    DeserializeSelectedNodesFromCommandsResultSuccess,
    DuplicateSelectedNodesRequest,
    DuplicateSelectedNodesResultSuccess,
    SerializeSelectedNodesToCommandsRequest,
    SerializeSelectedNodesToCommandsResultSuccess,
)
from griptape_nodes.retained_mode.events.object_events import ClearAllObjectStateRequest
from griptape_nodes.retained_mode.events.parameter_events import (
    ConversionConfig,
    MigrateParameterRequest,
    MigrateParameterResultSuccess,
)
from griptape_nodes.retained_mode.events.workflow_events import (
    ImportWorkflowAsReferencedSubFlowResultSuccess,
    ImportWorkflowRequest,
    ImportWorkflowResultSuccess,
)
from tests.workflow_context import push_unsaved_workflow

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.runtime.workflow_runtime import WorkflowRuntime

_LIBRARY_NAME = "Node Workflow Paths Library"


class _ValueNode(DataNode):
    """A node with a connectable "value" parameter that records every instance built."""

    built: ClassVar[list[_ValueNode]] = []

    def __init__(self, name: str, metadata: dict | None = None) -> None:
        super().__init__(name, metadata=metadata)
        self.add_parameter(
            Parameter(
                name="value",
                tooltip="Value to echo",
                type="str",
                default_value="",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY, ParameterMode.OUTPUT},
            )
        )
        _ValueNode.built.append(self)

    def process(self) -> None:
        self.parameter_output_values["value"] = self.get_parameter_value("value")


class _Group(BaseNodeGroup):
    """A plain visual group with no subflow."""

    def process(self) -> None:
        return None


class _SubflowGroup(SubflowNodeGroup):
    """A group that moves its members into a subflow of its own."""

    async def aprocess(self) -> None:
        return None

    def process(self) -> None:
        return None


class _Start(StartNode):
    def process(self) -> None:
        return None


class _End(EndNode):
    def process(self) -> None:
        return None


@pytest.fixture(autouse=True)
def node_library(engine: Engine) -> Iterator[None]:
    """A library providing the node types below, so the nodes these tests create are real ones."""
    LibraryRegistry._clear()
    _ValueNode.built.clear()
    schema = LibrarySchema(
        name=_LIBRARY_NAME,
        library_schema_version=LibrarySchema.LATEST_SCHEMA_VERSION,
        metadata=LibraryMetadata(
            author="test", description="test", library_version="0.1.0", engine_version="0.0.0", tags=[]
        ),
        categories=[],
        nodes=[],
    )
    library = LibraryRegistry.generate_new_library(schema)
    for node_class in (_ValueNode, _Group, _SubflowGroup, _Start, _End):
        library.register_new_node_type(
            node_class, NodeMetadata(category="test", description="d", display_name=node_class.__name__)
        )
    try:
        yield
    finally:
        engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
        _ValueNode.built.clear()
        LibraryRegistry._clear()


@pytest.fixture
def workflow(engine: Engine) -> WorkflowRuntime:
    """An open workflow whose current flow is "canvas"."""
    push_unsaved_workflow(engine)
    created = engine.handle_request(CreateFlowRequest(parent_flow_name=None, flow_name="canvas"))
    assert isinstance(created, CreateFlowResultSuccess), created.result_details
    return engine.open_workflows.sole_workflow()


def _create_node(engine: Engine, node_name: str, node_type: str = "_ValueNode", **fields: Any) -> BaseNode:
    result = engine.handle_request(
        CreateNodeRequest(node_type=node_type, specific_library_name=_LIBRARY_NAME, node_name=node_name, **fields)
    )
    assert isinstance(result, CreateNodeResultSuccess), result.result_details
    return engine.node_manager.get_node_by_name(result.node_name)


def _nodes_in(workflow: WorkflowRuntime) -> dict[str, BaseNode]:
    return workflow.objects.get_filtered_subset(type=BaseNode)


def _assert_every_node_knows_its_workflow(workflow: WorkflowRuntime) -> None:
    for node_name, node in _nodes_in(workflow).items():
        assert node.workflow is workflow, node_name


def _serialize_selected(engine: Engine, node_names: list[str]) -> SerializeSelectedNodesToCommandsResultSuccess:
    result = engine.handle_request(
        SerializeSelectedNodesToCommandsRequest(nodes_to_serialize=[[name, "0"] for name in node_names])
    )
    assert isinstance(result, SerializeSelectedNodesToCommandsResultSuccess), result.result_details
    return result


def _write_workflow_with_two_nodes(engine: Engine, path: Path) -> None:
    """Write a saved workflow whose flow holds nodes "first" and "second", then close what built it."""
    push_unsaved_workflow(engine, path.stem)
    engine.handle_request(CreateFlowRequest(parent_flow_name=None, flow_name="inner", set_as_new_context=False))
    with engine.context_manager.flow("inner"):
        _create_node(engine, "first")
        _create_node(engine, "second")
    serialized = engine.handle_request(SerializeFlowToCommandsRequest(flow_name="inner"))
    assert isinstance(serialized, SerializeFlowToCommandsResultSuccess), serialized.result_details
    metadata = WorkflowMetadata(
        workflow_id=new_workflow_id(),
        name=path.stem,
        schema_version=WorkflowMetadata.LATEST_SCHEMA_VERSION,
        engine_version_created_with="0.0.0",
        node_libraries_referenced=list(serialized.serialized_flow_commands.node_dependencies.libraries),
        workflow_shape=None,
    )
    path.write_text(
        engine.workflow_manager.codegen.generate_workflow_file_content(
            serialized_flow_commands=serialized.serialized_flow_commands, workflow_metadata=metadata
        )
    )
    engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))


class TestNodesThatJoinAWorkflow:
    def test_a_created_node(self, engine: Engine, workflow: WorkflowRuntime) -> None:
        node = _create_node(engine, "node")

        assert node.workflow is workflow

    def test_an_error_proxy_for_a_node_that_could_not_be_built(self, engine: Engine, workflow: WorkflowRuntime) -> None:
        result = engine.handle_request(
            CreateNodeRequest(node_type="Missing", node_name="proxy", create_error_proxy_on_failure=True)
        )

        assert isinstance(result, CreateNodeResultSuccess), result.result_details
        proxy = engine.node_manager.get_node_by_name(result.node_name)
        assert isinstance(proxy, ErrorProxyNode)
        assert proxy.workflow is workflow

    def test_nodes_created_in_a_group(self, engine: Engine, workflow: WorkflowRuntime) -> None:
        group = _create_node(engine, "group", node_type="_Group")
        member = _create_node(engine, "member", parent_group_name=group.name)

        assert member.parent_group is group
        _assert_every_node_knows_its_workflow(workflow)

    def test_nodes_moved_into_a_subflow_group(self, engine: Engine, workflow: WorkflowRuntime) -> None:
        group = _create_node(engine, "group", node_type="_SubflowGroup")
        member = _create_node(engine, "member", parent_group_name=group.name)

        assert isinstance(group, SubflowNodeGroup)
        assert workflow.nodes.name_to_parent_flow_name[member.name] == group.metadata["subflow_name"]
        _assert_every_node_knows_its_workflow(workflow)

    def test_a_reloaded_flow(self, engine: Engine) -> None:
        push_unsaved_workflow(engine)
        engine.handle_request(CreateFlowRequest(parent_flow_name=None, flow_name="canvas"))
        _create_node(engine, "first")
        _create_node(engine, "second")
        serialized = engine.handle_request(SerializeFlowToCommandsRequest(flow_name="canvas"))
        assert isinstance(serialized, SerializeFlowToCommandsResultSuccess), serialized.result_details
        engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
        push_unsaved_workflow(engine)

        result = engine.handle_request(
            DeserializeFlowFromCommandsRequest(serialized_flow_commands=serialized.serialized_flow_commands)
        )

        assert isinstance(result, DeserializeFlowFromCommandsResultSuccess), result.result_details
        reloaded = engine.open_workflows.sole_workflow()
        assert set(_nodes_in(reloaded)) == {"first", "second"}
        _assert_every_node_knows_its_workflow(reloaded)

    def test_pasted_nodes(self, engine: Engine, workflow: WorkflowRuntime) -> None:
        copied = _serialize_selected(engine, [_create_node(engine, "node").name])

        result = engine.handle_request(
            DeserializeSelectedNodesFromCommandsRequest(
                deserialize_commands=copied.serialized_selected_node_commands, pickled_values=copied.pickled_values
            )
        )

        assert isinstance(result, DeserializeSelectedNodesFromCommandsResultSuccess), result.result_details
        assert len(_nodes_in(workflow)) == 2  # noqa: PLR2004
        _assert_every_node_knows_its_workflow(workflow)

    def test_duplicated_nodes(self, engine: Engine, workflow: WorkflowRuntime) -> None:
        node = _create_node(engine, "node")

        result = engine.handle_request(DuplicateSelectedNodesRequest(nodes_to_duplicate=[[node.name, "0"]]))

        assert isinstance(result, DuplicateSelectedNodesResultSuccess), result.result_details
        assert len(_nodes_in(workflow)) == 2  # noqa: PLR2004
        _assert_every_node_knows_its_workflow(workflow)

    def test_a_packaged_flow_unpacked_into_the_workflow(self, engine: Engine, workflow: WorkflowRuntime) -> None:
        node = _create_node(engine, "node")
        packaged = engine.handle_request(
            PackageNodesAsSerializedFlowRequest(
                node_names=[node.name],
                start_node_type="_Start",
                end_node_type="_End",
                start_node_library_name=_LIBRARY_NAME,
                end_node_library_name=_LIBRARY_NAME,
            )
        )
        assert isinstance(packaged, PackageNodesAsSerializedFlowResultSuccess), packaged.result_details

        result = engine.handle_request(
            DeserializeFlowFromCommandsRequest(serialized_flow_commands=packaged.serialized_flow_commands)
        )

        assert isinstance(result, DeserializeFlowFromCommandsResultSuccess), result.result_details
        unpacked = set(result.node_name_mappings.values())
        assert unpacked
        assert unpacked <= set(_nodes_in(workflow))
        _assert_every_node_knows_its_workflow(workflow)

    @pytest.mark.asyncio
    async def test_nodes_of_a_referenced_import(self, engine: Engine, tmp_path: Path) -> None:
        path = tmp_path / "imported.py"
        _write_workflow_with_two_nodes(engine, path)
        saved = await engine.ahandle_request(ImportWorkflowRequest(file_path=str(path)))
        assert isinstance(saved, ImportWorkflowResultSuccess), saved.result_details
        push_unsaved_workflow(engine, "importing")
        engine.handle_request(CreateFlowRequest(parent_flow_name=None, flow_name="canvas", set_as_new_context=False))

        result = await engine.ahandle_request(
            ImportWorkflowAsReferencedSubFlowRequest(workflow_id=saved.workflow_id, flow_name="canvas")
        )

        assert isinstance(result, ImportWorkflowAsReferencedSubFlowResultSuccess), result.result_details
        importing = engine.open_workflows.sole_workflow()
        assert set(_nodes_in(importing)) == {"first", "second"}
        _assert_every_node_knows_its_workflow(importing)

    def test_a_conversion_node_built_by_migrate_parameter(self, engine: Engine, workflow: WorkflowRuntime) -> None:
        feeder = _create_node(engine, "feeder")
        old_node = _create_node(engine, "old")
        replacement = _create_node(
            engine, "replacement", metadata={"position": {"x": 0, "y": 0}, "size": {"width": 200, "height": 100}}
        )
        connected = engine.handle_request(
            CreateConnectionRequest(
                source_node_name=feeder.name,
                source_parameter_name="value",
                target_node_name=old_node.name,
                target_parameter_name="value",
            )
        )
        assert isinstance(connected, CreateConnectionResultSuccess), connected.result_details

        result = engine.handle_request(
            MigrateParameterRequest(
                source_node_name=old_node.name,
                target_node_name=replacement.name,
                source_parameter_name="value",
                target_parameter_name="value",
                input_conversion=ConversionConfig(
                    library=_LIBRARY_NAME, node_type="_ValueNode", input_parameter="value", output_parameter="value"
                ),
            )
        )

        assert isinstance(result, MigrateParameterResultSuccess), result.result_details
        assert "replacement_value_input_converter" in _nodes_in(workflow)
        _assert_every_node_knows_its_workflow(workflow)


class TestNodesThatLeaveAWorkflow:
    def test_a_deleted_node_has_none(self, engine: Engine, workflow: WorkflowRuntime) -> None:  # noqa: ARG002
        node = _create_node(engine, "node")

        result = engine.handle_request(DeleteNodeRequest(node_name=node.name))

        assert isinstance(result, DeleteNodeResultSuccess), result.result_details
        assert node.workflow is None

    def test_closing_the_workflow_leaves_its_nodes_with_none(self, engine: Engine, workflow: WorkflowRuntime) -> None:  # noqa: ARG002
        nodes = [_create_node(engine, "first"), _create_node(engine, "second")]

        engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))

        assert [node.workflow for node in nodes] == [None, None]


class TestNodesBuiltToInspect:
    def test_a_describe_probe_has_none(self, engine: Engine) -> None:
        result = engine.handle_request(DescribeNodeTypeRequest(node_type="_ValueNode", library=_LIBRARY_NAME))

        assert isinstance(result, DescribeNodeTypeResultSuccess), result.result_details
        assert [node.workflow for node in _ValueNode.built] == [None]

    def test_the_reference_node_serialization_compares_against_has_none(
        self, engine: Engine, workflow: WorkflowRuntime
    ) -> None:
        node = _create_node(engine, "node")

        _serialize_selected(engine, [node.name])

        reference_nodes = [built for built in _ValueNode.built if built is not node]
        assert reference_nodes
        assert [built.workflow for built in reference_nodes] == [None] * len(reference_nodes)
        _assert_every_node_knows_its_workflow(workflow)

    def test_a_worker_transient_node_has_none(self, engine: Engine) -> None:
        transient = engine.node_manager._materialize_transient_node_from_metadata(
            ExecuteNodeRequest(node_name="node", node_metadata={"node_type": "_ValueNode", "library": _LIBRARY_NAME})
        )

        assert isinstance(transient, _ValueNode)
        assert transient.workflow is None

    def test_a_worker_transient_nodes_events_name_the_orchestrator_workflow_it_runs_for(self, engine: Engine) -> None:
        transient = engine.node_manager._materialize_transient_node_from_metadata(
            ExecuteNodeRequest(
                node_name="node",
                node_metadata={"node_type": "_ValueNode", "library": _LIBRARY_NAME},
                orchestrator_workflow_id="orchestrator-wf",
            )
        )

        assert isinstance(transient, _ValueNode)
        event = transient.execution_event(CurrentControlNodeEvent(node_name="node"))
        assert event.wrapped_event.workflow_id == "orchestrator-wf"
