"""Tests for the pure parts of generating a node type from a workflow file."""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, Mock

import pytest

from griptape_nodes.common.isolated_execution_scope import (
    IsolatedExecutionScope,
    current_isolated_execution_scope,
    isolated_execution_scope,
)
from griptape_nodes.common.loop_body_scope import LoopBodyScope, current_loop_body_scope, entered_loop_body_scope
from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
from griptape_nodes.exe_types.node_types import EndNode, StartNode
from griptape_nodes.exe_types.workflow_node import (
    WorkflowNode,
    WorkflowNodeDefinitionError,
    WorkflowNodeRoutingError,
    WorkflowParameterRoute,
    _workspace_relative_workflow_path,
    build_workflow_node_class,
    build_workflow_node_surface,
    flatten_shape_section,
    pair_shape_nodes,
)
from griptape_nodes.node_library.workflow_registry import WorkflowMetadata, WorkflowShape
from griptape_nodes.retained_mode.engine import current_engine
from griptape_nodes.retained_mode.events.execution_events import (
    StartLocalSubflowRequest,
    StartLocalSubflowResultFailure,
    StartLocalSubflowResultSuccess,
)
from griptape_nodes.retained_mode.events.failure_tree import (
    FailureKind,
    FailureLeaf,
    FailureTreeError,
    WorkflowNodeFailure,
    WorkflowRecursionLimitError,
)
from griptape_nodes.retained_mode.events.flow_events import (
    CreateFlowRequest,
    CreateFlowResultSuccess,
    ImportWorkflowAsReferencedSubFlowRequest,
)
from griptape_nodes.retained_mode.events.node_error_details import NodeErrorDetails
from griptape_nodes.retained_mode.events.node_events import GetFlowForNodeResultSuccess
from griptape_nodes.retained_mode.events.workflow_events import ImportWorkflowAsReferencedSubFlowResultFailure

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.events.base_events import ResultPayload

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
        "parent_element_name": None,
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
    current_engine().context_manager.push_workflow(workflow_name="workflow_node_live_routes")
    flow_result = current_engine().handle_request(
        CreateFlowRequest(parent_flow_name=None, flow_name="Subflow", set_as_new_context=False)
    )
    assert isinstance(flow_result, CreateFlowResultSuccess), flow_result

    flow = current_engine().flow_manager.get_flow_by_name(flow_result.flow_name)
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

    def test_end_node_status_parameters_dropped(self) -> None:
        """Every End Flow node carries these, and they describe that node's run rather than an output."""
        section = {
            "End Flow": {
                "was_successful": _param("was_successful", "bool", parent_element_name="Status"),
                "result_details": _param("result_details", parent_element_name="Status"),
                "result": _param("result"),
            }
        }

        assert list(flatten_shape_section(section)) == ["result"]

    def test_status_names_outside_the_status_group_are_kept(self) -> None:
        """Only the Status group's copies are dropped, not an author's own parameter of that name."""
        section = {"End Flow": {"was_successful": _param("was_successful", "bool")}}

        assert list(flatten_shape_section(section)) == ["was_successful"]

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
        # The node is in the workflow that holds its imported subflow.
        node._set_workflow(current_engine().current_workflow())

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
        assert current_engine().workflow_registry.has_workflow_with_name(registry_key)

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
        assert current_engine().workflow_registry.get_workflow_by_name(registry_key).metadata.is_internal is False

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


class TestWorkspaceRelativeWorkflowPath:
    @pytest.fixture
    def workspace(self, tmp_path: Path) -> Path:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        return workspace.resolve()

    def test_file_inside_the_workspace_is_workspace_relative(self, workspace: Path) -> None:
        """A file inside the workspace is named relative to it."""
        result = _workspace_relative_workflow_path(workspace, workspace / "nested" / "flow.py")

        assert result == str(Path("nested") / "flow.py")

    def test_file_outside_the_workspace_keeps_its_full_path(self, workspace: Path, tmp_path: Path) -> None:
        """A file outside the workspace keeps its full path."""
        outside = (tmp_path / "library" / "flow.py").resolve()

        result = _workspace_relative_workflow_path(workspace, outside)

        assert result == str(outside)

    @pytest.mark.skipif(sys.platform.startswith("win"), reason="symlink creation needs privileges on Windows")
    def test_link_inside_the_workspace_keeps_the_link_name(self, workspace: Path, tmp_path: Path) -> None:
        """A file reached through a link inside the workspace is named by the link."""
        real_folder = tmp_path / "elsewhere"
        real_folder.mkdir()
        (real_folder / "flow.py").write_text("")
        (workspace / "linked").symlink_to(real_folder, target_is_directory=True)

        result = _workspace_relative_workflow_path(workspace, workspace / "linked" / "flow.py")

        assert result == str(Path("linked") / "flow.py")

    @pytest.mark.skipif(sys.platform.startswith("win"), reason="symlink creation needs privileges on Windows")
    def test_link_outside_the_workspace_to_a_parent_folder_falls_back_to_the_real_path(
        self, workspace: Path, tmp_path: Path
    ) -> None:
        """A file inside the workspace reached through an outside link is named by its real place."""
        (workspace / "nested").mkdir()
        (workspace / "nested" / "flow.py").write_text("")
        outside_link = tmp_path / "outside_link"
        outside_link.symlink_to(workspace, target_is_directory=True)

        result = _workspace_relative_workflow_path(workspace, outside_link / "nested" / "flow.py")

        assert result == str(Path("nested") / "flow.py")

    @pytest.mark.skipif(sys.platform.startswith("win"), reason="symlink creation needs privileges on Windows")
    def test_linked_folder_above_the_workspace_does_not_hide_a_file_really_inside(self, tmp_path: Path) -> None:
        """A link above the workspace and another to the file still give the workspace-relative path."""
        real = tmp_path / "real"
        (real / "ws" / "libs" / "mylib").mkdir(parents=True)
        (real / "ws" / "libs" / "mylib" / "wf.py").write_text("")
        linked_parent = tmp_path / "linked"
        linked_parent.symlink_to(real, target_is_directory=True)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "lib").symlink_to(real / "ws" / "libs" / "mylib", target_is_directory=True)

        result = _workspace_relative_workflow_path(linked_parent / "ws", elsewhere / "lib" / "wf.py")

        assert result == str(Path("libs/mylib/wf.py"))


class TestAprocess:
    @pytest.fixture
    def workflow_node(self, monkeypatch: pytest.MonkeyPatch) -> WorkflowNode:
        shape = WorkflowShape(
            inputs={"Start Flow": {"text": _param("text")}},
            outputs={"End Flow": {"result": _param("result")}},
        )
        node_class = build_workflow_node_class(
            node_type="ShoutWorkflow",
            workflow_file_path=Path("/library/shout_workflow.py"),
            workflow_metadata=_metadata(shape),
        )
        node = node_class(name="Shout It")
        monkeypatch.setattr(node, "_load_subflow", AsyncMock(spec=WorkflowNode._load_subflow, return_value="Subflow"))
        monkeypatch.setattr(node, "_resolve_live_routes", Mock(spec=WorkflowNode._resolve_live_routes))
        monkeypatch.setattr(node, "_apply_inputs", Mock(spec=WorkflowNode._apply_inputs))
        monkeypatch.setattr(node, "_collect_outputs", Mock(spec=WorkflowNode._collect_outputs))
        return node

    @pytest.fixture
    def enclosing_scope(self) -> LoopBodyScope:
        return LoopBodyScope(canvas_node_names={"Shout It_1": "Shout It"}, engine_node_names=frozenset())

    @pytest.fixture
    def scope_seen_by_subflow(self) -> list[LoopBodyScope | None]:
        return []

    @pytest.fixture
    def run_subflow(
        self,
        workflow_node: WorkflowNode,
        monkeypatch: pytest.MonkeyPatch,
        scope_seen_by_subflow: list[LoopBodyScope | None],
    ) -> AsyncMock:
        async def record_scope(request: StartLocalSubflowRequest) -> ResultPayload:
            assert request.flow_name == "Subflow"
            scope_seen_by_subflow.append(current_loop_body_scope.get())
            return StartLocalSubflowResultSuccess(result_details="done")

        run = AsyncMock(spec=WorkflowNode._ahandle_request_in_workflow, side_effect=record_scope)
        monkeypatch.setattr(workflow_node, "_ahandle_request_in_workflow", run)
        return run

    @pytest.mark.asyncio
    async def test_subflow_runs_without_the_enclosing_loop_scope(
        self,
        workflow_node: WorkflowNode,
        run_subflow: AsyncMock,
        enclosing_scope: LoopBodyScope,
        scope_seen_by_subflow: list[LoopBodyScope | None],
    ) -> None:
        with entered_loop_body_scope(enclosing_scope):
            await workflow_node.aprocess()

        run_subflow.assert_awaited_once()
        assert scope_seen_by_subflow == [None]

    @pytest.mark.asyncio
    async def test_enclosing_loop_scope_is_restored_afterwards(
        self, workflow_node: WorkflowNode, run_subflow: AsyncMock, enclosing_scope: LoopBodyScope
    ) -> None:
        with entered_loop_body_scope(enclosing_scope):
            await workflow_node.aprocess()

            assert current_loop_body_scope.get() is enclosing_scope

        run_subflow.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_subflow_keeps_the_enclosing_isolated_execution_scope(
        self, workflow_node: WorkflowNode, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scopes_seen_by_subflow: list[IsolatedExecutionScope | None] = []

        async def record_scope(request: StartLocalSubflowRequest) -> ResultPayload:
            assert request.flow_name == "Subflow"
            scopes_seen_by_subflow.append(current_isolated_execution_scope.get())
            return StartLocalSubflowResultSuccess(result_details="done")

        run = AsyncMock(spec=WorkflowNode._ahandle_request_in_workflow, side_effect=record_scope)
        monkeypatch.setattr(workflow_node, "_ahandle_request_in_workflow", run)

        with isolated_execution_scope("My Group"):
            outer_scope = current_isolated_execution_scope.get()

            await workflow_node.aprocess()

            assert current_isolated_execution_scope.get() is outer_scope

        run.assert_awaited_once()
        assert scopes_seen_by_subflow == [IsolatedExecutionScope(owner_name="My Group")]


def _leaf(node_name: str, message: str = "boom") -> FailureLeaf:
    details = NodeErrorDetails(message=message, exception_type="builtins.ValueError")
    return FailureLeaf(node_name=node_name, kind=FailureKind.RAISED, details=details)


class TestAprocessSubflowFailure:
    """`WorkflowNode.aprocess` reports the failure behind a subflow that did not finish."""

    @pytest.fixture
    def workflow_node(self) -> Mock:
        node = Mock(spec=WorkflowNode)
        node.name = "Outer"
        node._load_subflow = AsyncMock(spec=WorkflowNode._load_subflow, return_value="Subflow")
        node._raise_subflow_failure = lambda result: WorkflowNode._raise_subflow_failure(node, result)
        return node

    def run_with(self, workflow_node: Mock, result: Any) -> AsyncMock:
        send = AsyncMock(spec=WorkflowNode._ahandle_request_in_workflow, return_value=result)
        workflow_node._ahandle_request_in_workflow = send
        return send

    @pytest.mark.asyncio
    async def test_raises_the_failure_inside_as_a_workflow_node_failure(self, workflow_node: Mock) -> None:
        inner = _leaf("Inner")
        self.run_with(
            workflow_node, StartLocalSubflowResultFailure(result_details="did not finish").attach_failure_tree(inner)
        )

        with pytest.raises(FailureTreeError) as raised:
            await WorkflowNode.aprocess(workflow_node)

        assert raised.value.failure_tree == WorkflowNodeFailure(node_name="Outer", inner=inner)
        assert str(raised.value) == (
            "Attempted to run the workflow behind node 'Outer'. Failed because node 'Inner' inside it failed: boom"
        )
        workflow_node._collect_outputs.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_refused_workflow_node_inside_is_the_node_that_failed_and_adds_no_depth(
        self, workflow_node: Mock
    ) -> None:
        early = FailureLeaf(
            node_name="Inner", kind=FailureKind.RECURSION_LIMIT, details=NodeErrorDetails(message="too deep")
        )
        self.run_with(
            workflow_node, StartLocalSubflowResultFailure(result_details="did not finish").attach_failure_tree(early)
        )

        with pytest.raises(FailureTreeError) as raised:
            await WorkflowNode.aprocess(workflow_node)

        assert raised.value.failure_tree == WorkflowNodeFailure(node_name="Outer", inner=early)
        assert str(raised.value) == (
            "Attempted to run the workflow behind node 'Outer'. Failed because node 'Inner' inside it failed: too deep"
        )

    @pytest.mark.asyncio
    async def test_a_failure_that_names_no_node_is_reported_by_its_text(self, workflow_node: Mock) -> None:
        self.run_with(workflow_node, StartLocalSubflowResultFailure(result_details="could not start"))

        with pytest.raises(RuntimeError) as raised:
            await WorkflowNode.aprocess(workflow_node)

        assert not isinstance(raised.value, FailureTreeError)
        assert str(raised.value) == (
            "Attempted to run the workflow behind node 'Outer'. "
            "Failed because the workflow did not finish: could not start"
        )

    @pytest.mark.asyncio
    async def test_the_subflow_request_does_not_log_the_failure_the_node_reports(self, workflow_node: Mock) -> None:
        send = self.run_with(
            workflow_node, StartLocalSubflowResultFailure(result_details="no").attach_failure_tree(_leaf("X"))
        )

        with pytest.raises(FailureTreeError):
            await WorkflowNode.aprocess(workflow_node)

        send.assert_awaited_once()
        request = send.await_args_list[0].args[0]
        assert isinstance(request, StartLocalSubflowRequest)
        assert (request.flow_name, request.failure_log_level) == ("Subflow", logging.DEBUG)

    @pytest.mark.asyncio
    async def test_collects_outputs_when_the_subflow_succeeds(self, workflow_node: Mock) -> None:
        self.run_with(workflow_node, StartLocalSubflowResultSuccess(result_details="ok"))

        await WorkflowNode.aprocess(workflow_node)

        workflow_node._collect_outputs.assert_called_once()


class TestLoadSubflowImportFailure:
    """`WorkflowNode._load_subflow` reports a failed import as a plain error, without logging it a second time."""

    @pytest.fixture
    def workflow_node(self) -> Mock:
        node = Mock(spec=WorkflowNode)
        node.name = "Outer"
        node.metadata = {}
        node.workflow_file_path = "/library/wf.py"
        node._handle_request_in_workflow.return_value = GetFlowForNodeResultSuccess(
            flow_name="Parent", result_details="ok"
        )
        node._register_workflow.return_value = "registry-key"
        node._ahandle_request_in_workflow = AsyncMock(
            spec=WorkflowNode._ahandle_request_in_workflow,
            return_value=ImportWorkflowAsReferencedSubFlowResultFailure(result_details="no such file"),
        )
        return node

    @pytest.mark.asyncio
    async def test_an_import_failure_is_a_plain_runtime_error(self, workflow_node: Mock) -> None:
        with pytest.raises(RuntimeError, match="the workflow could not be loaded: no such file") as raised:
            await WorkflowNode._load_subflow(workflow_node)

        assert not isinstance(raised.value, FailureTreeError)

    @pytest.mark.asyncio
    async def test_a_recursion_refusal_is_an_early_termination_named_after_the_node(self, workflow_node: Mock) -> None:
        refusal = WorkflowRecursionLimitError("too deep")
        workflow_node._ahandle_request_in_workflow.return_value = ImportWorkflowAsReferencedSubFlowResultFailure(
            result_details="too deep", exception=refusal
        )

        with pytest.raises(FailureTreeError) as raised:
            await WorkflowNode._load_subflow(workflow_node)

        assert raised.value.failure_tree == FailureLeaf(
            node_name="Outer",
            kind=FailureKind.RECURSION_LIMIT,
            details=NodeErrorDetails(
                message="too deep",
                exception_type="griptape_nodes.retained_mode.events.failure_tree.WorkflowRecursionLimitError",
            ),
        )
        assert str(raised.value) == (
            "Attempted to load the workflow behind node 'Outer'. "
            "Failed because the workflow could not be loaded: too deep"
        )

    @pytest.mark.asyncio
    async def test_the_import_request_does_not_log_the_failure_the_node_reports(self, workflow_node: Mock) -> None:
        with pytest.raises(RuntimeError):
            await WorkflowNode._load_subflow(workflow_node)

        [call] = workflow_node._ahandle_request_in_workflow.await_args_list
        request = call.args[0]
        assert isinstance(request, ImportWorkflowAsReferencedSubFlowRequest)
        assert request.failure_log_level == logging.DEBUG
