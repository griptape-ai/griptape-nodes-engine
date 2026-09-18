"""Unit tests for SubflowNodeGroup."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import create_autospec

from griptape_nodes.exe_types.core_types import ControlParameterInput, ControlParameterOutput, ParameterMode
from griptape_nodes.exe_types.node_groups.subflow_node_group import SubflowNodeGroup
from griptape_nodes.retained_mode.events.flow_events import CreateFlowRequest, CreateFlowResultSuccess

if TYPE_CHECKING:
    import pytest

    from griptape_nodes.retained_mode.engine import Engine


class TestSubflowNodeGroupCreateSubflow:
    """_create_subflow must persist the deduplicated flow name it actually created."""

    def test_records_deduplicated_flow_name_on_collision(
        self,
        engine: Engine,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The group records the flow name it got back, not the (colliding) name it requested."""
        group = _MiniSubflowGroup(name="G")

        # Simulate the engine deduplicating the requested "G_subflow" (already taken) to "G_subflow_1".
        deduped_result = CreateFlowResultSuccess(flow_name="G_subflow_1", result_details="created")
        mock_handle = create_autospec(engine.handle_request, return_value=deduped_result)
        monkeypatch.setattr(engine, "handle_request", mock_handle)

        # _create_subflow reads the current flow only to parent the request; keep it off engine state.
        context_manager = engine.context_manager
        monkeypatch.setattr(
            context_manager,
            "get_current_flow",
            create_autospec(context_manager.get_current_flow, return_value=None),
        )

        group._create_subflow()

        # The request is derived from the group's own name...
        mock_handle.assert_called_once_with(
            CreateFlowRequest(
                flow_name="G_subflow",
                parent_flow_name=None,
                set_as_new_context=False,
                metadata={"flow_type": "NodeGroupFlow"},
            )
        )
        # ...but the group must record the flow it ACTUALLY got back, not the requested name.
        assert group.metadata["subflow_name"] == "G_subflow_1"

    def test_preserves_saved_proxy_side_metadata(self, engine: Engine) -> None:  # noqa: ARG002
        group = _MiniSubflowGroup(
            name="G",
            metadata={"left_parameters": ["exec_in"], "right_parameters": ["exec_out"]},
        )

        assert group.metadata["left_parameters"] == ["group_exec_in", "exec_in"]
        assert group.metadata["right_parameters"] == ["group_exec_out", "exec_out"]


class TestGetAllNodes:
    """get_all_nodes has to reach the whole body, not just the first level down.

    Callers use it to package a group for execution (remote, private, iterative), so a node it
    misses is a node that silently does not run.
    """

    def test_collects_members_nested_more_than_one_level_deep(
        self,
        engine: Engine,  # noqa: ARG002 - initialises the engine singleton for construction
    ) -> None:
        outer = _MiniSubflowGroup(name="outer")
        middle = _MiniSubflowGroup(name="middle")
        inner = _MiniSubflowGroup(name="inner")
        leaf = _MiniSubflowGroup(name="leaf")

        # Wire membership directly: this covers the traversal, not the add-to-group machinery.
        outer.nodes = {"middle": middle}
        middle.nodes = {"inner": inner}
        inner.nodes = {"leaf": leaf}

        # "leaf" is three levels down; walking a single level would stop at "middle".
        assert set(outer.get_all_nodes()) == {"middle", "inner", "leaf"}

    def test_returns_direct_members_when_nothing_is_nested(
        self,
        engine: Engine,  # noqa: ARG002 - initialises the engine singleton for construction
    ) -> None:
        group = _MiniSubflowGroup(name="group")
        group.nodes = {"only": _MiniSubflowGroup(name="only")}

        assert set(group.get_all_nodes()) == {"only"}


class TestSubflowNodeGroupProxyParameters:
    """Boundary proxies must remain control ports after request-handler reconstruction."""

    def test_control_proxy_preserves_port_shape_and_bridge_modes(self, engine: Engine) -> None:
        group = _MiniSubflowGroup(name="group")
        engine.object_manager.add_object_by_name(group.name, group)

        incoming_proxy = group._create_proxy_parameter_for_connection(
            ControlParameterInput(name="upstream_exec"), is_incoming=True
        )
        outgoing_proxy = group._create_proxy_parameter_for_connection(
            ControlParameterOutput(name="downstream_exec"), is_incoming=False
        )

        assert isinstance(incoming_proxy, ControlParameterInput)
        assert isinstance(outgoing_proxy, ControlParameterOutput)
        assert incoming_proxy.allowed_modes == {ParameterMode.INPUT, ParameterMode.OUTPUT}
        assert outgoing_proxy.allowed_modes == {ParameterMode.INPUT, ParameterMode.OUTPUT}
        assert ParameterMode.PROPERTY not in incoming_proxy.allowed_modes
        assert ParameterMode.PROPERTY not in outgoing_proxy.allowed_modes

    def test_control_port_serialization_preserves_directional_shape(self) -> None:
        incoming = ControlParameterInput(name="exec_in")
        outgoing = ControlParameterOutput(name="exec_out")

        incoming_dict = incoming.to_dict()
        outgoing_dict = outgoing.to_dict()

        assert incoming_dict["input_types"] == ["parametercontroltype"]
        assert incoming_dict["output_type"] is None
        assert outgoing_dict["input_types"] is None
        assert outgoing_dict["output_type"] == "parametercontroltype"


class _MiniSubflowGroup(SubflowNodeGroup):
    """Minimal concrete SubflowNodeGroup exercising only _create_subflow."""

    async def aprocess(self) -> None:  # pragma: no cover - execution not exercised here
        await self.execute_subflow()

    def process(self) -> Any:  # pragma: no cover - execution not exercised here
        return None
