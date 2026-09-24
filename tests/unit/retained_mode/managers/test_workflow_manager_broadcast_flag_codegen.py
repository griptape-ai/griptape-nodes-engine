"""A transport flag set for the sake of a loop must not end up in a saved workflow file.

``NodeExecutor._silence_packaged_node_creation_broadcasts`` clears ``broadcast_result`` on a
packaged loop body's create commands so rebuilding the body stays invisible to editors. Workflow
codegen, though, writes out every create-command field whose value differs from its default
(``WorkflowManager._generate_node_creation_code``) -- so the same mutation applied anywhere upstream
of a save writes ``broadcast_result=False`` into the artist's ``.py`` file, and on the publish path
into a library. That is why the silencing lives at the local deserialization boundaries, which never
save, rather than at packaging time, which several branches do save from.

These tests pin both halves: that codegen really is that literal about non-default fields (so the
constraint is real and not folklore), and that a silenced package result would carry the flag
through if it were ever handed to the writer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from griptape_nodes.common.node_executor import NodeExecutor
from griptape_nodes.exe_types.node_types import NodeDependencies
from griptape_nodes.node_library.workflow_registry import (
    LibraryNameAndNodeType,
    WorkflowMetadata,
    WorkflowShape,
)
from griptape_nodes.retained_mode.events.flow_events import (
    CreateFlowRequest,
    PackageNodesAsSerializedFlowResultSuccess,
    SerializedFlowCommands,
)
from griptape_nodes.retained_mode.events.node_events import CreateNodeRequest, SerializedNodeCommands

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.engine import Engine


def _package_result(*node_names: str) -> PackageNodesAsSerializedFlowResultSuccess:
    """A packaging result shaped the way the loop executor receives one."""
    serialized_nodes = [
        SerializedNodeCommands(
            create_node_command=CreateNodeRequest(node_type="EchoNode", node_name=node_name),
            element_modification_commands=[],
            node_dependencies=NodeDependencies(),
        )
        for node_name in node_names
    ]
    serialized_flow_commands = SerializedFlowCommands(
        flow_initialization_command=CreateFlowRequest(flow_name="packaged_body", parent_flow_name=None),
        serialized_node_commands=serialized_nodes,
        serialized_connections=[],
        unique_parameter_uuid_to_values={},
        set_parameter_value_commands={},
        set_lock_commands_per_node={},
        sub_flows_commands=[],
        node_dependencies=NodeDependencies(),
        node_types_used={LibraryNameAndNodeType(library_name="Echo Library", node_type="EchoNode")},
        flow_name="packaged_body",
    )
    return PackageNodesAsSerializedFlowResultSuccess(
        serialized_flow_commands=serialized_flow_commands,
        workflow_shape=WorkflowShape(),
        packaged_node_names=list(node_names),
        parameter_name_mappings=[],
        result_details="Packaged the loop body.",
    )


def _generate(engine: Engine, serialized_flow_commands: SerializedFlowCommands) -> str:
    metadata = WorkflowMetadata(
        name="broadcast_flag_workflow",
        schema_version=WorkflowMetadata.LATEST_SCHEMA_VERSION,
        engine_version_created_with="1.0.0",
        node_libraries_referenced=[],
        workflow_shape=None,
    )
    return engine.workflow_manager._generate_workflow_file_content(
        serialized_flow_commands=serialized_flow_commands, workflow_metadata=metadata
    )


class TestBroadcastFlagAndCodegen:
    def test_an_untouched_package_result_writes_no_transport_flag(self) -> None:
        """The baseline: nothing about packaging mentions broadcasting."""
        package_result = _package_result("Body Node")
        assert package_result.serialized_flow_commands.serialized_node_commands[0].create_node_command.broadcast_result

    def test_silencing_makes_broadcast_result_a_non_default_field(self) -> None:
        """The coupling in one line: codegen emits non-default fields, and this makes it non-default."""
        package_result = _package_result("Body Node", "Other Body Node")
        NodeExecutor._silence_packaged_node_creation_broadcasts(package_result)

        create_commands = [
            serialized_node.create_node_command
            for serialized_node in package_result.serialized_flow_commands.serialized_node_commands
        ]
        assert [command.broadcast_result for command in create_commands] == [False, False]

    def test_codegen_writes_the_flag_out_when_it_is_silenced(self, engine: Engine) -> None:
        """The consequence, on the real writer: a silenced command reaches the file as source.

        This is the test that says *why* the silencing call may not move upstream of a save. If
        codegen ever stops writing the flag, this failure is the place to record that the
        constraint has been lifted -- do not simply delete the assertion.
        """
        package_result = _package_result("Body Node")
        NodeExecutor._silence_packaged_node_creation_broadcasts(package_result)

        generated = _generate(engine, package_result.serialized_flow_commands)

        assert "broadcast_result=False" in generated

    def test_codegen_is_clean_for_a_package_result_that_was_not_silenced(self, engine: Engine) -> None:
        """The paired positive case: the flag appears only because of the mutation, not always."""
        package_result = _package_result("Body Node")

        generated = _generate(engine, package_result.serialized_flow_commands)

        assert "broadcast_result" not in generated
