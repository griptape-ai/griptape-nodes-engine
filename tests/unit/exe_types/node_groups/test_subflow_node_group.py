"""Unit tests for SubflowNodeGroup."""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any
from unittest.mock import Mock, create_autospec

import pytest

from griptape_nodes.exe_types.core_types import Parameter, ParameterTypeBuiltin
from griptape_nodes.exe_types.node_groups.subflow_node_group import (
    LEFT_PARAMETERS_KEY,
    RIGHT_PARAMETERS_KEY,
    SubflowNodeGroup,
)
from griptape_nodes.retained_mode.events.flow_events import CreateFlowRequest, CreateFlowResultSuccess
from griptape_nodes.retained_mode.events.parameter_events import AddParameterToNodeResultSuccess

if TYPE_CHECKING:
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


class TestRegisterSideParameter:
    """Recording a rail parameter must add what is missing without disturbing what is already there."""

    def test_fresh_group_records_each_control_port_once(
        self,
        engine: Engine,  # noqa: ARG002 - initialises the engine singleton for construction
    ) -> None:
        """A group built from nothing names its own control ports, once each."""
        group = _MiniSubflowGroup(name="fresh")

        assert group.metadata[LEFT_PARAMETERS_KEY] == ["group_exec_in"]
        assert group.metadata[RIGHT_PARAMETERS_KEY] == ["group_exec_out"]

    def test_restored_group_keeps_saved_proxy_ports_in_saved_order(
        self,
        engine: Engine,  # noqa: ARG002 - initialises the engine singleton for construction
    ) -> None:
        """Data proxies saved with the workflow survive the rebuild, in the order they were saved."""
        saved_metadata = {
            LEFT_PARAMETERS_KEY: ["group_exec_in", "prompt", "seed"],
            RIGHT_PARAMETERS_KEY: ["group_exec_out", "image"],
        }

        group = _MiniSubflowGroup(name="restored", metadata=saved_metadata)

        assert group.metadata[LEFT_PARAMETERS_KEY] == ["group_exec_in", "prompt", "seed"]
        assert group.metadata[RIGHT_PARAMETERS_KEY] == ["group_exec_out", "image"]

    def test_repeated_restores_do_not_grow_the_rail_lists(
        self,
        engine: Engine,  # noqa: ARG002 - initialises the engine singleton for construction
    ) -> None:
        """Save/load cycles are stable: the third generation names the same ports as the first."""
        first = _MiniSubflowGroup(name="first")
        second = _MiniSubflowGroup(name="second", metadata=copy.deepcopy(first.metadata))
        third = _MiniSubflowGroup(name="third", metadata=copy.deepcopy(second.metadata))

        assert third.metadata[LEFT_PARAMETERS_KEY] == first.metadata[LEFT_PARAMETERS_KEY]
        assert third.metadata[RIGHT_PARAMETERS_KEY] == first.metadata[RIGHT_PARAMETERS_KEY]

    def test_at_front_pins_a_new_parameter_to_the_top_of_the_rail(
        self,
        engine: Engine,  # noqa: ARG002 - initialises the engine singleton for construction
    ) -> None:
        """Control ports sit above data ports, so a front-inserted name lands at index 0."""
        group = _MiniSubflowGroup(name="pinned", metadata={LEFT_PARAMETERS_KEY: ["prompt"]})

        group._register_side_parameter(LEFT_PARAMETERS_KEY, "exec_in", at_front=True)

        assert group.metadata[LEFT_PARAMETERS_KEY] == ["exec_in", "prompt", "group_exec_in"]

    def test_at_front_leaves_an_already_recorded_parameter_where_it_is(
        self,
        engine: Engine,  # noqa: ARG002 - initialises the engine singleton for construction
    ) -> None:
        """A restored rail already names the control port, so re-recording must not move or repeat it."""
        group = _MiniSubflowGroup(name="pinned", metadata={LEFT_PARAMETERS_KEY: ["prompt", "exec_in"]})

        group._register_side_parameter(LEFT_PARAMETERS_KEY, "exec_in", at_front=True)

        assert group.metadata[LEFT_PARAMETERS_KEY] == ["prompt", "exec_in", "group_exec_in"]


class TestCreateProxyParameterForConnection:
    """A proxy parameter has to land on the rail matching the direction of the connection it stands in for."""

    PROXY_NAME = "prompt"

    @pytest.fixture
    def group(
        self,
        engine: Engine,  # noqa: ARG002 - initialises the engine singleton for construction
    ) -> _MiniSubflowGroup:
        """A group already carrying the parameter the engine is stubbed to report it added."""
        group = _MiniSubflowGroup(name="G")
        group.add_parameter(Parameter(name=self.PROXY_NAME, tooltip=""))
        return group

    @pytest.fixture
    def mock_handle_request(
        self,
        engine: Engine,
        group: _MiniSubflowGroup,
        monkeypatch: pytest.MonkeyPatch,
    ) -> Mock:
        """Stand in for the engine handling AddParameterToNodeRequest, which is not under test here."""
        added_result = AddParameterToNodeResultSuccess(
            parameter_name=self.PROXY_NAME,
            type=ParameterTypeBuiltin.ANY.value,
            node_name=group.name,
            result_details="added",
        )
        mock_handle = create_autospec(engine.handle_request, return_value=added_result)
        monkeypatch.setattr(engine, "handle_request", mock_handle)
        return mock_handle

    def test_incoming_connection_records_the_proxy_on_the_left_rail(
        self,
        group: _MiniSubflowGroup,
        mock_handle_request: Mock,  # noqa: ARG002 - stubs the engine the call under test goes through
    ) -> None:
        """A connection coming into the group enters through a left-rail port."""
        proxy = group._create_proxy_parameter_for_connection(
            Parameter(name=self.PROXY_NAME, tooltip=""), is_incoming=True
        )

        assert proxy.name == self.PROXY_NAME
        assert group.metadata[LEFT_PARAMETERS_KEY] == ["group_exec_in", self.PROXY_NAME]
        assert group.metadata[RIGHT_PARAMETERS_KEY] == ["group_exec_out"]

    def test_outgoing_connection_records_the_proxy_on_the_right_rail(
        self,
        group: _MiniSubflowGroup,
        mock_handle_request: Mock,  # noqa: ARG002 - stubs the engine the call under test goes through
    ) -> None:
        """A connection leaving the group exits through a right-rail port."""
        proxy = group._create_proxy_parameter_for_connection(
            Parameter(name=self.PROXY_NAME, tooltip=""), is_incoming=False
        )

        assert proxy.name == self.PROXY_NAME
        assert group.metadata[RIGHT_PARAMETERS_KEY] == ["group_exec_out", self.PROXY_NAME]
        assert group.metadata[LEFT_PARAMETERS_KEY] == ["group_exec_in"]


class _MiniSubflowGroup(SubflowNodeGroup):
    """Minimal concrete SubflowNodeGroup exercising only _create_subflow."""

    async def aprocess(self) -> None:  # pragma: no cover - execution not exercised here
        await self.execute_subflow()

    def process(self) -> Any:  # pragma: no cover - execution not exercised here
        return None
