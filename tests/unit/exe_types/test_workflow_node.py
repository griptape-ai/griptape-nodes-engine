"""Tests for the pure parts of generating a node type from a workflow file."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
from griptape_nodes.exe_types.node_types import EndNode, StartNode
from griptape_nodes.exe_types.workflow_node import (
    WorkflowNode,
    WorkflowNodeDefinitionError,
    WorkflowNodeRoutingError,
    WorkflowParameterRoute,
    build_workflow_node_class,
    build_workflow_node_surface,
    flatten_shape_section,
    pair_shape_nodes,
)
from griptape_nodes.node_library.workflow_registry import WorkflowMetadata, WorkflowRegistry, WorkflowShape
from griptape_nodes.retained_mode.events.flow_events import CreateFlowRequest, CreateFlowResultSuccess
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

CONTROL_TYPE = "parametercontroltype"


def _param(name: str, param_type: str = "str", **overrides: Any) -> dict[str, Any]:
    definition = {
        "name": name,
        "type": param_type,
        "input_types": [param_type],
        "output_type": param_type,
        "default_value": None,
        "tooltip": f"{name} tooltip",
        "mode_allowed_input": True,
        "mode_allowed_property": True,
        "mode_allowed_output": True,
    }
    definition.update(overrides)
    return definition


def _metadata(shape: WorkflowShape | None) -> WorkflowMetadata:
    return WorkflowMetadata(
        name="demo",
        schema_version=WorkflowMetadata.LATEST_SCHEMA_VERSION,
        engine_version_created_with="0.0.0",
        node_libraries_referenced=[],
        workflow_shape=shape,
    )


def _build_live_subflow() -> str:
    """Create a flow standing in for an imported workflow, and return its name.

    Mirrors what an import produces for the shape used by `TestResolveLiveRoutes`: a parameterless
    Start Flow node, a Start Flow node exposing `text`, and an End Flow node exposing `result`, all
    renamed because their original names were already taken on the canvas.
    """
    GriptapeNodes.ContextManager().push_workflow(workflow_name="workflow_node_live_routes")
    flow_result = GriptapeNodes.handle_request(
        CreateFlowRequest(parent_flow_name=None, flow_name="Subflow", set_as_new_context=False)
    )
    assert isinstance(flow_result, CreateFlowResultSuccess), flow_result

    flow = GriptapeNodes.FlowManager().get_flow_by_name(flow_result.flow_name)
    flow.add_node(StartNode(name="Start Flow_1"))

    start_with_text = StartNode(name="Start Flow_2")
    start_with_text.add_parameter(
        Parameter(name="text", tooltip="text tooltip", type="str", allowed_modes={ParameterMode.OUTPUT})
    )
    flow.add_node(start_with_text)

    end_node = EndNode(name="End Flow_1")
    end_node.add_parameter(
        Parameter(name="result", tooltip="result tooltip", type="str", allowed_modes={ParameterMode.INPUT})
    )
    flow.add_node(end_node)

    return flow_result.flow_name


class TestFlattenShapeSection:
    def test_bare_names_when_unique(self) -> None:
        section = {"Start Flow": {"text": _param("text"), "count": _param("count", "int")}}

        routes = flatten_shape_section(section)

        assert routes == {
            "text": WorkflowParameterRoute("Start Flow", "text"),
            "count": WorkflowParameterRoute("Start Flow", "count"),
        }

    def test_control_parameters_dropped(self) -> None:
        section = {"Start Flow": {"exec_out": _param("exec_out", CONTROL_TYPE), "text": _param("text")}}

        assert list(flatten_shape_section(section)) == ["text"]

    def test_collision_qualifies_every_side(self) -> None:
        section = {
            "Start A": {"text": _param("text"), "only_a": _param("only_a")},
            "Start B": {"text": _param("text")},
        }

        routes = flatten_shape_section(section)

        assert routes == {
            "Start_A.text": WorkflowParameterRoute("Start A", "text"),
            "only_a": WorkflowParameterRoute("Start A", "only_a"),
            "Start_B.text": WorkflowParameterRoute("Start B", "text"),
        }

    def test_qualifier_replaces_whitespace(self) -> None:
        """Parameter names cannot contain whitespace, and Start/End node names routinely do."""
        section = {"Start Flow": {"text": _param("text")}, "Start Flow_1": {"text": _param("text")}}

        routes = flatten_shape_section(section)

        assert set(routes) == {"Start_Flow.text", "Start_Flow_1.text"}
        assert not any(char.isspace() for name in routes for char in name)

    def test_qualifier_collision_is_rejected(self) -> None:
        """Two node names that differ only by whitespace would produce one parameter name."""
        section = {"Start Flow": {"text": _param("text")}, "Start_Flow": {"text": _param("text")}}

        with pytest.raises(WorkflowNodeDefinitionError, match="collides with"):
            flatten_shape_section(section)

    def test_result_is_order_independent(self) -> None:
        forward = {"Start A": {"text": _param("text")}, "Start B": {"text": _param("text")}}
        reversed_order = {"Start B": {"text": _param("text")}, "Start A": {"text": _param("text")}}

        assert flatten_shape_section(forward) == flatten_shape_section(reversed_order)


class TestBuildWorkflowNodeSurface:
    def test_inputs_precede_outputs(self) -> None:
        shape = WorkflowShape(
            inputs={"Start Flow": {"text": _param("text")}},
            outputs={"End Flow": {"result": _param("result")}},
        )

        surface = build_workflow_node_surface(_metadata(shape))

        assert list(surface.parameters) == ["text", "result"]
        assert surface.parameters["text"].input_route == WorkflowParameterRoute("Start Flow", "text")
        assert surface.parameters["text"].output_route is None
        assert surface.parameters["result"].output_route == WorkflowParameterRoute("End Flow", "result")
        assert surface.parameters["result"].input_route is None
        assert surface.start_node_names == ["Start Flow"]
        assert surface.end_node_names == ["End Flow"]

    def test_name_on_both_sides_carries_both_routes(self) -> None:
        shape = WorkflowShape(
            inputs={"Start Flow": {"value": _param("value")}},
            outputs={"End Flow": {"value": _param("value")}},
        )

        surface = build_workflow_node_surface(_metadata(shape))

        assert list(surface.parameters) == ["value"]
        assert surface.parameters["value"].input_route == WorkflowParameterRoute("Start Flow", "value")
        assert surface.parameters["value"].output_route == WorkflowParameterRoute("End Flow", "value")

    def test_parameterless_nodes_still_count_toward_the_shape(self) -> None:
        """A Start Flow node that only carries control flow is invisible in the parameter surface.

        It is still one of the nodes the live subflow gets paired against by position, so dropping it
        from `start_node_names` would make every run of the node fail the pairing count check.
        """
        shape = WorkflowShape(
            inputs={
                "Start Flow": {"exec_out": _param("exec_out", CONTROL_TYPE)},
                "Start Flow_1": {"text": _param("text")},
            },
            outputs={"End Flow": {"result": _param("result")}},
        )

        surface = build_workflow_node_surface(_metadata(shape))

        assert list(surface.parameters) == ["text", "result"]
        assert surface.start_node_names == ["Start Flow", "Start Flow_1"]
        assert surface.end_node_names == ["End Flow"]

    def test_missing_shape_rejected(self) -> None:
        with pytest.raises(WorkflowNodeDefinitionError, match="no saved input and output shape"):
            build_workflow_node_surface(_metadata(None))

    def test_control_only_shape_rejected(self) -> None:
        shape = WorkflowShape(
            inputs={"Start Flow": {"exec_out": _param("exec_out", CONTROL_TYPE)}},
            outputs={"End Flow": {"exec_in": _param("exec_in", CONTROL_TYPE)}},
        )

        with pytest.raises(WorkflowNodeDefinitionError, match="expose no parameters"):
            build_workflow_node_surface(_metadata(shape))


class TestPairShapeNodes:
    def test_exact_names(self) -> None:
        assert pair_shape_nodes(["Start Flow"], ["Start Flow"], role="Start Flow") == {"Start Flow": "Start Flow"}

    def test_deduplicated_rename(self) -> None:
        assert pair_shape_nodes(["Start Flow"], ["Start Flow_1"], role="Start Flow") == {"Start Flow": "Start Flow_1"}

    def test_prefix_overlapping_names_are_not_cross_wired(self) -> None:
        """A renamed node can collide textually with a different declared name.

        A workflow declaring both "Start Flow" and "Start Flow_1", imported into a canvas that
        already has a "Start Flow", gets renamed to "Start Flow_1" and "Start Flow_2". Matching by
        name would hand "Start Flow_1" to the wrong declared node and swap the two routes.
        """
        paired = pair_shape_nodes(["Start Flow", "Start Flow_1"], ["Start Flow_1", "Start Flow_2"], role="Start Flow")

        assert paired == {"Start Flow": "Start Flow_1", "Start Flow_1": "Start Flow_2"}

    def test_each_declared_name_claims_a_distinct_live_name(self) -> None:
        paired = pair_shape_nodes(["Start A", "Start B"], ["Start A_1", "Start B_1"], role="Start Flow")

        assert paired == {"Start A": "Start A_1", "Start B": "Start B_1"}

    def test_count_mismatch_is_rejected(self) -> None:
        with pytest.raises(WorkflowNodeRoutingError, match="lists 2 of them but the loaded copy has 1"):
            pair_shape_nodes(["Start Flow", "Other"], ["Start Flow"], role="Start Flow")


class TestResolveLiveRoutes:
    """Routes are re-pointed at the imported copy of the workflow before every run."""

    def test_parameterless_start_node_is_counted_and_skipped(self) -> None:
        """A Start Flow node carrying only control flow contributes no route but still holds a slot.

        Pairing is positional, so the parameterless node has to be counted on the declared side or
        every run of the node fails the count check even though the saved shape is correct.
        """
        shape = WorkflowShape(
            inputs={
                "Start Flow": {"exec_out": _param("exec_out", CONTROL_TYPE)},
                "Start Flow_1": {"text": _param("text")},
            },
            outputs={"End Flow": {"result": _param("result")}},
        )
        node_class = build_workflow_node_class(
            node_type="ShoutWorkflow",
            workflow_file_path=Path("/library/shout_workflow.py"),
            workflow_metadata=_metadata(shape),
        )
        node = node_class(name="Shout It")
        subflow_name = _build_live_subflow()

        live_routes = node._resolve_live_routes(subflow_name)

        # The import renamed every node, so each declared name resolves one position along.
        assert live_routes.inputs == {"text": WorkflowParameterRoute("Start Flow_2", "text")}
        assert live_routes.outputs == {"result": WorkflowParameterRoute("End Flow_1", "result")}


class TestBuildWorkflowNodeClass:
    def test_class_name_and_attributes(self) -> None:
        shape = WorkflowShape(
            inputs={"Start Flow": {"text": _param("text")}},
            outputs={"End Flow": {"result": _param("result")}},
        )
        metadata = _metadata(shape)

        node_class = build_workflow_node_class(
            node_type="ShoutWorkflow",
            workflow_file_path=Path("/library/shout_workflow.py"),
            workflow_metadata=metadata,
        )

        assert node_class.__name__ == "ShoutWorkflow"
        assert issubclass(node_class, WorkflowNode)
        assert node_class.workflow_file_path == Path("/library/shout_workflow.py")
        assert node_class.workflow_metadata is metadata

    def test_instance_parameters_mirror_the_shape(self) -> None:
        shape = WorkflowShape(
            inputs={"Start Flow": {"exec_out": _param("exec_out", CONTROL_TYPE), "text": _param("text")}},
            outputs={"End Flow": {"result": _param("result")}},
        )

        node_class = build_workflow_node_class(
            node_type="ShoutWorkflow",
            workflow_file_path=Path("/library/shout_workflow.py"),
            workflow_metadata=_metadata(shape),
        )
        node = node_class(name="Shout It")

        text_param = node.get_parameter_by_name("text")
        result_param = node.get_parameter_by_name("result")
        assert text_param is not None
        assert result_param is not None
        assert text_param.allowed_modes == {ParameterMode.INPUT, ParameterMode.PROPERTY}
        assert result_param.allowed_modes == {ParameterMode.OUTPUT}
        # The node brings its own control flow rather than surfacing the workflow's.
        assert node.get_parameter_by_name("exec_in") is node.control_parameter_in
        assert node.get_parameter_by_name("exec_out") is node.control_parameter_out

    def test_property_mode_withheld_when_shape_forbids_it(self) -> None:
        shape = WorkflowShape(
            inputs={"Start Flow": {"text": _param("text", mode_allowed_property=False)}},
            outputs={"End Flow": {"result": _param("result")}},
        )

        node_class = build_workflow_node_class(
            node_type="ShoutWorkflow",
            workflow_file_path=Path("/library/shout_workflow.py"),
            workflow_metadata=_metadata(shape),
        )
        node = node_class(name="Shout It")

        text_param = node.get_parameter_by_name("text")
        assert text_param is not None
        assert text_param.allowed_modes == {ParameterMode.INPUT}

    def test_mutable_defaults_are_not_shared_between_instances(self) -> None:
        """The shape lives on the generated type, so its defaults have to be copied per instance.

        A Parameter keeps its default value by reference. Handing every instance the same list would
        let one node's edit show up on every other node of the same type.
        """
        shape = WorkflowShape(
            inputs={"Start Flow": {"items": _param("items", "list", default_value=["a"])}},
            outputs={"End Flow": {"result": _param("result")}},
        )
        node_class = build_workflow_node_class(
            node_type="ListWorkflow",
            workflow_file_path=Path("/library/list_workflow.py"),
            workflow_metadata=_metadata(shape),
        )

        first = node_class(name="First")
        second = node_class(name="Second")

        first_items = first.get_parameter_by_name("items")
        second_items = second.get_parameter_by_name("items")
        assert first_items is not None
        assert second_items is not None
        first_items.default_value.append("b")
        assert second_items.default_value == ["a"]
        assert shape.inputs["Start Flow"]["items"]["default_value"] == ["a"]

    def test_stale_subflow_name_is_dropped_on_construction(self) -> None:
        shape = WorkflowShape(
            inputs={"Start Flow": {"text": _param("text")}},
            outputs={"End Flow": {"result": _param("result")}},
        )
        node_class = build_workflow_node_class(
            node_type="ShoutWorkflow",
            workflow_file_path=Path("/library/shout_workflow.py"),
            workflow_metadata=_metadata(shape),
        )

        node = node_class(name="Shout It", metadata={"subflow_name": "some_stale_flow"})

        assert "subflow_name" not in node.metadata
        assert node.metadata["workflow_node"] is True

    def test_colliding_shape_produces_a_constructible_node(self) -> None:
        """Qualified names must survive add_parameter, which rejects whitespace.

        Testing the flattening in isolation is not enough: node names routinely contain spaces, so a
        qualified name built from a raw node name makes the generated node type uncreatable.
        """
        shape = WorkflowShape(
            inputs={
                "Start Flow": {"text": _param("text")},
                "Start Flow_1": {"text": _param("text")},
            },
            outputs={"End Flow": {"result": _param("result")}},
        )
        node_class = build_workflow_node_class(
            node_type="ShoutTwiceWorkflow",
            workflow_file_path=Path("/library/shout_twice_workflow.py"),
            workflow_metadata=_metadata(shape),
        )

        node = node_class(name="Shout Twice")

        assert node.get_parameter_by_name("Start_Flow.text") is not None
        assert node.get_parameter_by_name("Start_Flow_1.text") is not None
        assert node.get_parameter_by_name("result") is not None


class TestEditorPreviewMetadata:
    """The editor renders these nodes with its subflow-preview wrapper keyed off node metadata.

    `workflow_node` selects the wrapper (which shows a "View Subflow" button) and
    `_workflow_file_value` is the registry key its preview imports when the node has not run yet.
    Without the second key the button opens nothing.
    """

    def test_registry_key_recorded_for_preview(self, tmp_path: Path) -> None:
        workflow_path = tmp_path / "shout_workflow.py"
        workflow_path.write_text("# placeholder\n", encoding="utf-8")
        shape = WorkflowShape(
            inputs={"Start Flow": {"text": _param("text")}},
            outputs={"End Flow": {"result": _param("result")}},
        )
        node_class = build_workflow_node_class(
            node_type="ShoutWorkflow",
            workflow_file_path=workflow_path,
            workflow_metadata=_metadata(shape),
        )

        node = node_class(name="Shout It")

        registry_key = node.metadata["_workflow_file_value"]
        assert WorkflowRegistry.has_workflow_with_name(registry_key)

    def test_workflow_is_internal_flag_is_respected_not_overridden(self, tmp_path: Path) -> None:
        """The workflow's own is_internal flag wins.

        Overriding it would race the library's own `workflows` registration and silently hide a
        workflow the author had listed deliberately.
        """
        workflow_path = tmp_path / "shout_workflow.py"
        workflow_path.write_text("# placeholder\n", encoding="utf-8")
        shape = WorkflowShape(
            inputs={"Start Flow": {"text": _param("text")}},
            outputs={"End Flow": {"result": _param("result")}},
        )
        metadata = _metadata(shape)
        assert metadata.is_internal is False, "sanity: the fixture does not opt into hiding"
        node_class = build_workflow_node_class(
            node_type="ShoutWorkflow",
            workflow_file_path=workflow_path,
            workflow_metadata=metadata,
        )

        node = node_class(name="Shout It")

        registry_key = node.metadata["_workflow_file_value"]
        assert WorkflowRegistry.get_workflow_by_name(registry_key).metadata.is_internal is False

    def test_missing_workflow_file_leaves_preview_unavailable(self) -> None:
        shape = WorkflowShape(
            inputs={"Start Flow": {"text": _param("text")}},
            outputs={"End Flow": {"result": _param("result")}},
        )
        node_class = build_workflow_node_class(
            node_type="ShoutWorkflow",
            workflow_file_path=Path("/definitely/absent/shout_workflow.py"),
            workflow_metadata=_metadata(shape),
        )

        node = node_class(name="Shout It")

        # The node is still usable; only the preview is unavailable.
        assert "_workflow_file_value" not in node.metadata
        assert node.get_parameter_by_name("text") is not None
