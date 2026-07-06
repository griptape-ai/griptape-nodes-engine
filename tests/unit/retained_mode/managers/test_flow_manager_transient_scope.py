"""Tests for FlowManager's transient-flow scope.

Parallel/iterative execution (e.g. a ForEach group running "all at once") deserializes a
fresh Flow per iteration, runs it, and deletes it, all within a single node execution. Those
Flows must not leak into the editor-facing flow enumerators, otherwise the canvas discovers
them, polls their metadata, and floods with "no such Flow was found" errors once they are
torn down. FlowManager.transient_flow_scope() marks such Flows so the enumerators hide them.
"""

import pytest

from griptape_nodes.retained_mode.events.flow_events import (
    CreateFlowRequest,
    CreateFlowResultSuccess,
    GetTopLevelFlowRequest,
    GetTopLevelFlowResultSuccess,
    ListFlowsInFlowRequest,
    ListFlowsInFlowResultSuccess,
)
from griptape_nodes.retained_mode.events.object_events import ClearAllObjectStateRequest
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
from griptape_nodes.retained_mode.managers.flow_manager import TRANSIENT_EXECUTION_FLOW_METADATA_KEY


def _create_flow(griptape_nodes: GriptapeNodes, *, parent: str | None, name: str) -> str:
    result = griptape_nodes.handle_request(
        CreateFlowRequest(parent_flow_name=parent, flow_name=name, set_as_new_context=False)
    )
    assert isinstance(result, CreateFlowResultSuccess)
    return result.flow_name


class TestTransientFlowScope:
    @pytest.fixture(autouse=True)
    def _reset(self, griptape_nodes: GriptapeNodes) -> None:
        griptape_nodes.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
        griptape_nodes.ContextManager().push_workflow("wf")

    def test_flow_created_in_scope_is_hidden_from_list_flows(self, griptape_nodes: GriptapeNodes) -> None:
        flow_manager = griptape_nodes.FlowManager()
        canvas = _create_flow(griptape_nodes, parent=None, name="canvas")
        visible = _create_flow(griptape_nodes, parent=canvas, name="normal_child")
        with flow_manager.transient_flow_scope():
            hidden = _create_flow(griptape_nodes, parent=canvas, name="iteration_flow")

        result = griptape_nodes.handle_request(ListFlowsInFlowRequest(parent_flow_name=canvas))
        assert isinstance(result, ListFlowsInFlowResultSuccess)
        assert visible in result.flow_names
        assert hidden not in result.flow_names

    def test_transient_flow_never_reported_as_top_level(self, griptape_nodes: GriptapeNodes) -> None:
        flow_manager = griptape_nodes.FlowManager()
        canvas = _create_flow(griptape_nodes, parent=None, name="canvas")
        # A transient flow parented at top-level must not be returned as the canvas root.
        with flow_manager.transient_flow_scope():
            griptape_nodes.handle_request(
                CreateFlowRequest(parent_flow_name=canvas, flow_name="iteration_flow", set_as_new_context=False)
            )

        result = griptape_nodes.handle_request(GetTopLevelFlowRequest())
        assert isinstance(result, GetTopLevelFlowResultSuccess)
        assert result.flow_name == canvas

    def test_flow_created_outside_scope_is_visible(self, griptape_nodes: GriptapeNodes) -> None:
        canvas = _create_flow(griptape_nodes, parent=None, name="canvas")
        child = _create_flow(griptape_nodes, parent=canvas, name="normal_child")

        result = griptape_nodes.handle_request(ListFlowsInFlowRequest(parent_flow_name=canvas))
        assert isinstance(result, ListFlowsInFlowResultSuccess)
        assert child in result.flow_names

    def test_scope_stamps_metadata_marker(self, griptape_nodes: GriptapeNodes) -> None:
        flow_manager = griptape_nodes.FlowManager()
        canvas = _create_flow(griptape_nodes, parent=None, name="canvas")
        with flow_manager.transient_flow_scope():
            hidden = _create_flow(griptape_nodes, parent=canvas, name="iteration_flow")

        flow = GriptapeNodes.ObjectManager().get_object_by_name(hidden)
        assert flow.metadata.get(TRANSIENT_EXECUTION_FLOW_METADATA_KEY) is True

    def test_scope_is_reference_counted(self, griptape_nodes: GriptapeNodes) -> None:
        flow_manager = griptape_nodes.FlowManager()
        assert flow_manager.is_in_transient_flow_scope() is False
        with flow_manager.transient_flow_scope():
            assert flow_manager.is_in_transient_flow_scope() is True
            with flow_manager.transient_flow_scope():
                assert flow_manager.is_in_transient_flow_scope() is True
            # Still active after the inner scope exits (nested loops compose).
            assert flow_manager.is_in_transient_flow_scope() is True
        assert flow_manager.is_in_transient_flow_scope() is False

    def test_flows_created_after_scope_exit_are_visible(self, griptape_nodes: GriptapeNodes) -> None:
        flow_manager = griptape_nodes.FlowManager()
        canvas = _create_flow(griptape_nodes, parent=None, name="canvas")
        with flow_manager.transient_flow_scope():
            _create_flow(griptape_nodes, parent=canvas, name="iteration_flow")
        after = _create_flow(griptape_nodes, parent=canvas, name="normal_child")

        result = griptape_nodes.handle_request(ListFlowsInFlowRequest(parent_flow_name=canvas))
        assert isinstance(result, ListFlowsInFlowResultSuccess)
        assert after in result.flow_names
