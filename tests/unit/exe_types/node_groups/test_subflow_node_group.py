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
    """Recording a rail parameter must add what is missing without disturbing what is already there.

    The rails are membership sets, so these assert on which names are present and on how many there
    are — the count is what catches a port being recorded twice, which the editor reads as height.
    """

    def test_fresh_group_records_each_control_port_once(
        self,
        engine: Engine,  # noqa: ARG002 - initialises the engine singleton for construction
    ) -> None:
        """A group built from nothing names its own control ports, once each."""
        group = _MiniSubflowGroup(name="fresh")

        expected_left = {"group_exec_in"}
        expected_right = {"group_exec_out"}
        assert set(group.metadata[LEFT_PARAMETERS_KEY]) == expected_left
        assert len(group.metadata[LEFT_PARAMETERS_KEY]) == len(expected_left)
        assert set(group.metadata[RIGHT_PARAMETERS_KEY]) == expected_right
        assert len(group.metadata[RIGHT_PARAMETERS_KEY]) == len(expected_right)

    def test_restored_group_keeps_saved_proxy_ports(
        self,
        engine: Engine,  # noqa: ARG002 - initialises the engine singleton for construction
    ) -> None:
        """Data proxies saved with the workflow survive the rebuild."""
        saved_metadata = {
            LEFT_PARAMETERS_KEY: ["group_exec_in", "prompt", "seed"],
            RIGHT_PARAMETERS_KEY: ["group_exec_out", "image"],
        }

        group = _MiniSubflowGroup(name="restored", metadata=saved_metadata)

        expected_left = {"group_exec_in", "prompt", "seed"}
        expected_right = {"group_exec_out", "image"}
        assert set(group.metadata[LEFT_PARAMETERS_KEY]) == expected_left
        assert len(group.metadata[LEFT_PARAMETERS_KEY]) == len(expected_left)
        assert set(group.metadata[RIGHT_PARAMETERS_KEY]) == expected_right
        assert len(group.metadata[RIGHT_PARAMETERS_KEY]) == len(expected_right)

    def test_repeated_restores_do_not_grow_the_rail_lists(
        self,
        engine: Engine,  # noqa: ARG002 - initialises the engine singleton for construction
    ) -> None:
        """Save/load cycles are stable: the third generation names the same ports as the first."""
        first = _MiniSubflowGroup(name="first")
        second = _MiniSubflowGroup(name="second", metadata=copy.deepcopy(first.metadata))
        third = _MiniSubflowGroup(name="third", metadata=copy.deepcopy(second.metadata))

        assert set(third.metadata[LEFT_PARAMETERS_KEY]) == set(first.metadata[LEFT_PARAMETERS_KEY])
        assert len(third.metadata[LEFT_PARAMETERS_KEY]) == len(first.metadata[LEFT_PARAMETERS_KEY])
        assert set(third.metadata[RIGHT_PARAMETERS_KEY]) == set(first.metadata[RIGHT_PARAMETERS_KEY])
        assert len(third.metadata[RIGHT_PARAMETERS_KEY]) == len(first.metadata[RIGHT_PARAMETERS_KEY])


class TestDedupeSideParameters:
    """A rail list carrying repeated names has to be cleaned up as the group is rebuilt.

    The rails are membership sets, so a repeated name says nothing extra, but it does inflate the
    height the editor gives a collapsed group and it survives proxy cleanup, which removes only the
    first copy of a name.
    """

    def test_duplicates_saved_by_an_older_engine_are_dropped(
        self,
        engine: Engine,  # noqa: ARG002 - initialises the engine singleton for construction
    ) -> None:
        """Repeated rail names in saved metadata are collapsed, and real proxy ports are kept."""
        saved_metadata = {
            LEFT_PARAMETERS_KEY: ["group_exec_in", "prompt", "group_exec_in", "prompt", "seed"],
            RIGHT_PARAMETERS_KEY: ["group_exec_out", "image", "group_exec_out", "image", "image"],
        }

        group = _MiniSubflowGroup(name="restored", metadata=saved_metadata)

        expected_left = {"group_exec_in", "prompt", "seed"}
        expected_right = {"group_exec_out", "image"}
        assert set(group.metadata[LEFT_PARAMETERS_KEY]) == expected_left
        assert len(group.metadata[LEFT_PARAMETERS_KEY]) == len(expected_left)
        assert set(group.metadata[RIGHT_PARAMETERS_KEY]) == expected_right
        assert len(group.metadata[RIGHT_PARAMETERS_KEY]) == len(expected_right)

    def test_an_out_of_date_library_adds_at_most_one_redundant_copy(
        self,
        engine: Engine,  # noqa: ARG002 - initialises the engine singleton for construction
    ) -> None:
        """Save/load cycles reach a fixed point even when the library still appends unguarded.

        The first generation is clean, the second picks up the one extra copy the subclass appends
        after normalisation has already run, and every generation after that matches the second:
        the growth is bounded at one redundant copy instead of compounding on every load.
        """
        generations = [_OldLibraryStyleGroup(name="gen1")]
        for index in range(2, 6):
            previous_metadata = copy.deepcopy(generations[-1].metadata)
            generations.append(_OldLibraryStyleGroup(name=f"gen{index}", metadata=previous_metadata))

        right_rails = [generation.metadata[RIGHT_PARAMETERS_KEY] for generation in generations]

        # Generation 1 is built from nothing, so the subclass's names appear exactly once.
        assert right_rails[0] == ["group_exec_out", "old_a", "old_b"]
        # Generation 2 normalises what it was given, then the subclass appends its names again.
        assert right_rails[1] == ["group_exec_out", "old_a", "old_b", "old_a", "old_b"]
        # From there it is stable: no further copies accumulate.
        assert right_rails[2] == right_rails[1]
        assert right_rails[3] == right_rails[1]
        assert right_rails[4] == right_rails[1]


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
        expected_left = {"group_exec_in", self.PROXY_NAME}
        expected_right = {"group_exec_out"}
        assert set(group.metadata[LEFT_PARAMETERS_KEY]) == expected_left
        assert len(group.metadata[LEFT_PARAMETERS_KEY]) == len(expected_left)
        assert set(group.metadata[RIGHT_PARAMETERS_KEY]) == expected_right
        assert len(group.metadata[RIGHT_PARAMETERS_KEY]) == len(expected_right)

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
        expected_left = {"group_exec_in"}
        expected_right = {"group_exec_out", self.PROXY_NAME}
        assert set(group.metadata[LEFT_PARAMETERS_KEY]) == expected_left
        assert len(group.metadata[LEFT_PARAMETERS_KEY]) == len(expected_left)
        assert set(group.metadata[RIGHT_PARAMETERS_KEY]) == expected_right
        assert len(group.metadata[RIGHT_PARAMETERS_KEY]) == len(expected_right)


class _MiniSubflowGroup(SubflowNodeGroup):
    """Minimal concrete SubflowNodeGroup exercising only _create_subflow."""

    async def aprocess(self) -> None:  # pragma: no cover - execution not exercised here
        await self.execute_subflow()

    def process(self) -> Any:  # pragma: no cover - execution not exercised here
        return None


class _OldLibraryStyleGroup(_MiniSubflowGroup):
    """Mimics a node library built before the rail helpers existed.

    Appends its own built-in rail names unguarded on every construction, the way the standard
    library's group nodes did.
    """

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None) -> None:
        super().__init__(name, metadata)
        self.metadata[RIGHT_PARAMETERS_KEY].extend(["old_a", "old_b"])
