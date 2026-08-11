"""Tests for the pure parts of generating a node type from a workflow file."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from griptape_nodes.exe_types.core_types import ParameterMode
from griptape_nodes.exe_types.workflow_node import (
    WorkflowNode,
    WorkflowNodeDefinitionError,
    WorkflowParameterRoute,
    build_workflow_node_class,
    build_workflow_node_surface,
    flatten_shape_section,
    match_shape_nodes,
)
from griptape_nodes.node_library.workflow_registry import WorkflowMetadata, WorkflowRegistry, WorkflowShape

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
            "Start A.text": WorkflowParameterRoute("Start A", "text"),
            "only_a": WorkflowParameterRoute("Start A", "only_a"),
            "Start B.text": WorkflowParameterRoute("Start B", "text"),
        }

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

        assert list(surface) == ["text", "result"]
        assert surface["text"].input_route == WorkflowParameterRoute("Start Flow", "text")
        assert surface["text"].output_route is None
        assert surface["result"].output_route == WorkflowParameterRoute("End Flow", "result")
        assert surface["result"].input_route is None

    def test_name_on_both_sides_carries_both_routes(self) -> None:
        shape = WorkflowShape(
            inputs={"Start Flow": {"value": _param("value")}},
            outputs={"End Flow": {"value": _param("value")}},
        )

        surface = build_workflow_node_surface(_metadata(shape))

        assert list(surface) == ["value"]
        assert surface["value"].input_route == WorkflowParameterRoute("Start Flow", "value")
        assert surface["value"].output_route == WorkflowParameterRoute("End Flow", "value")

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


class TestMatchShapeNodes:
    def test_exact_names(self) -> None:
        assert match_shape_nodes(["Start Flow"], ["Start Flow"]) == {"Start Flow": "Start Flow"}

    def test_deduplicated_rename(self) -> None:
        assert match_shape_nodes(["Start Flow"], ["Start Flow_1"]) == {"Start Flow": "Start Flow_1"}

    def test_exact_match_wins_over_rename(self) -> None:
        matches = match_shape_nodes(["Start Flow"], ["Start Flow_1", "Start Flow"])

        assert matches == {"Start Flow": "Start Flow"}

    def test_each_declared_name_claims_a_distinct_live_name(self) -> None:
        matches = match_shape_nodes(["Start A", "Start B"], ["Start A_1", "Start B_1"])

        assert matches == {"Start A": "Start A_1", "Start B": "Start B_1"}

    def test_unmatched_declared_name_is_absent(self) -> None:
        assert match_shape_nodes(["Start Flow", "Other"], ["Start Flow"]) == {"Start Flow": "Start Flow"}


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
        # Backing workflows are hidden from the editor's workflow picker.
        assert WorkflowRegistry.get_workflow_by_name(registry_key).metadata.is_internal is True

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
