"""Tests for generating workflow files whose Flows nest more than one level deep.

A node group owns a subflow, so a group nested inside another group produces a subflow inside a
subflow, to whatever depth the artist nests them. The generator has to follow that all the way down:
when it walked only the first level, nodes below it were left out of the file entirely, the groups
that claimed them were rebuilt with no members, and any connection touching them could not be
written at all.
"""

from __future__ import annotations

import ast
from typing import TYPE_CHECKING

import pytest

from griptape_nodes.exe_types.node_types import NodeDependencies
from griptape_nodes.node_library.workflow_registry import WorkflowMetadata
from griptape_nodes.retained_mode.events.flow_events import CreateFlowRequest, SerializedFlowCommands
from griptape_nodes.retained_mode.events.node_events import CreateNodeRequest, SerializedNodeCommands

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.engine import Engine


def _node_commands(node_name: str, *, is_node_group: bool = False, **create_kwargs: object) -> SerializedNodeCommands:
    return SerializedNodeCommands(
        create_node_command=CreateNodeRequest(node_type="EchoNode", node_name=node_name, **create_kwargs),  # type: ignore[arg-type]
        element_modification_commands=[],
        node_dependencies=NodeDependencies(),
        is_node_group=is_node_group,
    )


def _flow_commands(
    flow_name: str | None,
    *,
    nodes: list[SerializedNodeCommands] | None = None,
    connections: list[SerializedFlowCommands.IndirectConnectionSerialization] | None = None,
    sub_flows: list[SerializedFlowCommands] | None = None,
) -> SerializedFlowCommands:
    initialization_command = None
    if flow_name is not None:
        initialization_command = CreateFlowRequest(flow_name=flow_name, parent_flow_name=None)
    return SerializedFlowCommands(
        flow_initialization_command=initialization_command,
        serialized_node_commands=nodes or [],
        serialized_connections=connections or [],
        unique_parameter_uuid_to_values={},
        set_parameter_value_commands={},
        set_lock_commands_per_node={},
        sub_flows_commands=sub_flows or [],
        node_dependencies=NodeDependencies(),
        node_types_used=set(),
        flow_name=flow_name,
    )


def _generate(griptape_nodes: Engine, serialized_flow_commands: SerializedFlowCommands) -> str:
    metadata = WorkflowMetadata(
        name="nested_codegen_workflow",
        schema_version=WorkflowMetadata.LATEST_SCHEMA_VERSION,
        engine_version_created_with="1.0.0",
        node_libraries_referenced=[],
        workflow_shape=None,
    )
    return griptape_nodes.WorkflowManager()._generate_workflow_file_content(
        serialized_flow_commands=serialized_flow_commands, workflow_metadata=metadata
    )


class TestNestedFlowCodegen:
    def test_generates_nodes_from_a_flow_three_levels_deep(self, griptape_nodes: Engine) -> None:
        """A node two subflows below the top must still be written into the file."""
        leaf = _node_commands("Leaf")
        commands = _flow_commands(
            "top",
            sub_flows=[_flow_commands("middle", sub_flows=[_flow_commands("bottom", nodes=[leaf])])],
        )

        content = _generate(griptape_nodes, commands)

        assert "'Leaf'" in content
        # Each Flow gets its own variable, so all three levels are rebuilt.
        assert "flow0_name" in content
        assert "flow1_name" in content
        assert "flow2_name" in content

    def test_parents_each_subflow_to_the_flow_that_encloses_it(self, griptape_nodes: Engine) -> None:
        """The generated hierarchy has to mirror the nesting, not flatten onto the top-level flow."""
        commands = _flow_commands(
            "top",
            sub_flows=[_flow_commands("middle", sub_flows=[_flow_commands("bottom", nodes=[_node_commands("Leaf")])])],
        )

        content = _generate(griptape_nodes, commands)

        assert "parent_flow_name=flow0_name" in content
        assert "parent_flow_name=flow1_name" in content

    def test_gives_every_node_in_the_file_a_distinct_variable(self, griptape_nodes: Engine) -> None:
        """Node variables are file-wide, so names must not restart per Flow and collide."""
        commands = _flow_commands(
            "top",
            nodes=[_node_commands("TopNode")],
            sub_flows=[
                _flow_commands(
                    "middle",
                    nodes=[_node_commands("MiddleNode")],
                    sub_flows=[_flow_commands("bottom", nodes=[_node_commands("BottomNode")])],
                )
            ],
        )

        content = _generate(griptape_nodes, commands)

        assigned_names = [
            target.id
            for node in ast.walk(ast.parse(content))
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Name) and target.id.startswith("node")
        ]
        assert sorted(assigned_names) == ["node0_name", "node1_name", "node2_name"]

    def test_resolves_group_membership_across_a_deeper_subflow(self, griptape_nodes: Engine) -> None:
        """A group's member can be created in a subflow below it, and must still be named."""
        child = _node_commands("Child")
        group = _node_commands("Group", is_node_group=True, node_names_to_add=[child.node_uuid])
        commands = _flow_commands("top", nodes=[group], sub_flows=[_flow_commands("group_subflow", nodes=[child])])

        content = _generate(griptape_nodes, commands)

        # The child is referenced by its generated variable, and it is the variable belonging to the
        # child rather than to the group, which is what an unresolved membership list used to lose.
        assert "node_names_to_add=[node0_name]" in content
        assert "'Child'" in content
        child_variable_line = next(line for line in content.splitlines() if "'Child'" in line)
        assert child_variable_line.lstrip().startswith("node0_name")

    def test_writes_a_connection_once_even_though_each_level_reports_it(self, griptape_nodes: Engine) -> None:
        """Every Flow's serialized connections include its subflows', so the same edge repeats.

        Re-creating a connection tears down and rebuilds whatever it was routed through, so the
        generated file must ask for it exactly once.
        """
        source = _node_commands("A")
        target = _node_commands("B")
        connection = SerializedFlowCommands.IndirectConnectionSerialization(
            source_node_uuid=source.node_uuid,
            source_parameter_name="text",
            target_node_uuid=target.node_uuid,
            target_parameter_name="text",
        )
        commands = _flow_commands(
            "top",
            connections=[connection],
            sub_flows=[_flow_commands("child", nodes=[source, target], connections=[connection])],
        )

        content = _generate(griptape_nodes, commands)

        assert content.count("CreateConnectionRequest(") == 1

    def test_writes_a_nested_group_wall_connection_once_and_after_the_group_exists(
        self, griptape_nodes: Engine
    ) -> None:
        """The duplicate that actually happens crosses a nested group's wall, so cover that shape.

        An edge into a nested group attaches to a proxy parameter on the group node, so it belongs to
        the Flow holding that group -- here the outer group's subflow -- and aggregation then copies
        it up into the top level as well. Since a Flow writes its own groups before its connections,
        the level that owns the edge is also the one that has already declared the group variable.
        This pins both the count and that ordering.
        """
        feeder = _node_commands("Feeder")
        member = _node_commands("Member")
        inner_group = _node_commands("InnerGroup", is_node_group=True, node_names_to_add=[member.node_uuid])
        wall_connection = SerializedFlowCommands.IndirectConnectionSerialization(
            source_node_uuid=feeder.node_uuid,
            source_parameter_name="text",
            target_node_uuid=inner_group.node_uuid,
            target_parameter_name="text",
        )
        outer_subflow = _flow_commands(
            "outer_subflow",
            nodes=[feeder, inner_group],
            connections=[wall_connection],
            sub_flows=[_flow_commands("inner_subflow", nodes=[member])],
        )
        # Aggregation copies a subflow's connections up into every ancestor, so the top level reports
        # this edge too even though neither endpoint lives there.
        commands = _flow_commands("top", connections=[wall_connection], sub_flows=[outer_subflow])

        content = _generate(griptape_nodes, commands)

        assert content.count("CreateConnectionRequest(") == 1
        # The group's variable has to be assigned before the connection referring to it is written,
        # otherwise the generated file raises NameError when it is run.
        lines = content.splitlines()
        group_variable_lines = [
            index for index, line in enumerate(lines) if "'InnerGroup'" in line and "node_names_to_add" in line
        ]
        connection_lines = [index for index, line in enumerate(lines) if "CreateConnectionRequest(" in line]
        assert len(group_variable_lines) == 1, f"expected one line creating InnerGroup, got {group_variable_lines}"
        assert group_variable_lines[0] < connection_lines[0]

    def test_refuses_to_write_a_group_whose_member_is_not_in_the_file(self, griptape_nodes: Engine) -> None:
        """Silently dropping the member would save a group the artist has to refill by hand."""
        missing_child = _node_commands("Child")
        group = _node_commands("Group", is_node_group=True, node_names_to_add=[missing_child.node_uuid])
        # The child is claimed as a member but never handed to the generator.
        commands = _flow_commands("top", nodes=[group])

        with pytest.raises(ValueError, match="nodes in the group 'Group'"):
            _generate(griptape_nodes, commands)

    def test_refuses_to_write_a_connection_whose_endpoint_is_not_in_the_file(self, griptape_nodes: Engine) -> None:
        """A connection naming an unwritten node would emit a file that raises NameError."""
        source = _node_commands("A")
        missing_target = _node_commands("B")
        connection = SerializedFlowCommands.IndirectConnectionSerialization(
            source_node_uuid=source.node_uuid,
            source_parameter_name="text",
            target_node_uuid=missing_target.node_uuid,
            target_parameter_name="text",
        )
        commands = _flow_commands("top", nodes=[source], connections=[connection])

        with pytest.raises(ValueError, match="had not been written to the file yet"):
            _generate(griptape_nodes, commands)

    def test_emits_valid_python(self, griptape_nodes: Engine) -> None:
        """Whatever the nesting depth, the file has to parse."""
        commands = _flow_commands(
            "top",
            nodes=[_node_commands("TopNode")],
            sub_flows=[
                _flow_commands(
                    "middle",
                    nodes=[_node_commands("MiddleNode")],
                    sub_flows=[_flow_commands("bottom", nodes=[_node_commands("BottomNode")])],
                )
            ],
        )

        content = _generate(griptape_nodes, commands)

        ast.parse(content)  # raises SyntaxError if the generated file is malformed


class TestNestedFlowCodegenStructure:
    @pytest.mark.parametrize("depth", [1, 2, 4])
    def test_each_level_is_written_inside_the_one_above_it(self, griptape_nodes: Engine, depth: int) -> None:
        """Nodes must be created inside their own Flow's context block, at any depth."""
        commands = _flow_commands("level0", nodes=[_node_commands("Node0")])
        deepest = commands
        for level in range(1, depth + 1):
            child = _flow_commands(f"level{level}", nodes=[_node_commands(f"Node{level}")])
            deepest.sub_flows_commands.append(child)
            deepest = child

        content = _generate(griptape_nodes, commands)

        # Deeper Flows are nested further in, so their statements are indented further.
        indents = []
        for level in range(depth + 1):
            line = next(line for line in content.splitlines() if f"'Node{level}'" in line)
            indents.append(len(line) - len(line.lstrip()))
        assert indents == sorted(indents), f"expected increasing nesting, got indents {indents}"
        assert len(set(indents)) == len(indents), f"expected a distinct level per Flow, got {indents}"
