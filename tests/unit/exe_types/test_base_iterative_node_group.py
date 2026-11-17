"""Unit tests for control-flow ports on BaseIterativeNodeGroup."""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any

import pytest

from griptape_nodes.exe_types.core_types import (
    ControlParameterInput,
    ControlParameterOutput,
)
from griptape_nodes.exe_types.node_groups.base_iterative_node_group import BaseIterativeNodeGroup
from griptape_nodes.exe_types.node_groups.subflow_node_group import (
    LEFT_PARAMETERS_KEY,
    RIGHT_PARAMETERS_KEY,
)
from griptape_nodes.exe_types.node_types import ControlNode
from griptape_nodes.retained_mode.events.connection_events import (
    CreateConnectionRequest,
    CreateConnectionResultSuccess,
)
from griptape_nodes.retained_mode.events.flow_events import CreateFlowRequest, CreateFlowResultSuccess
from griptape_nodes.retained_mode.events.object_events import ClearAllObjectStateRequest

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.engine import Engine


class _Step(ControlNode):
    """A control node with the plain exec_in / exec_out pair, to sit on either side of a loop."""

    def process(self) -> None:
        pass


class MockIterativeGroup(BaseIterativeNodeGroup):
    """Minimal concrete subclass for testing BaseIterativeNodeGroup."""

    def _get_iteration_items(self) -> list[Any]:
        return []

    def _get_current_item_value(self, iteration_index: int) -> Any:  # noqa: ARG002
        return None


@pytest.fixture
def iterative_group(engine: Engine) -> MockIterativeGroup:  # noqa: ARG001
    """Return a freshly constructed MockIterativeGroup."""
    return MockIterativeGroup(name="test_iterative_group")


class TestBaseIterativeNodeGroupControlPorts:
    """Tests for the three control-flow parameters added to BaseIterativeNodeGroup."""

    def test_exec_in_exists_with_correct_element_type(self, iterative_group: MockIterativeGroup) -> None:
        """exec_in must be registered and have element_type ControlParameterInput."""
        param = iterative_group.get_parameter_by_name("exec_in")
        assert param is not None
        assert param.element_type == ControlParameterInput.__name__

    def test_exec_in_display_name(self, iterative_group: MockIterativeGroup) -> None:
        """exec_in must carry the display name 'Start Loop'."""
        param = iterative_group.get_parameter_by_name("exec_in")
        assert param is not None
        display_name = param.ui_options.get("display_name")
        assert display_name == "Start Loop"

    def test_on_each_exists_with_correct_element_type(self, iterative_group: MockIterativeGroup) -> None:
        """on_each must be registered and have element_type ControlParameterOutput."""
        param = iterative_group.get_parameter_by_name("on_each")
        assert param is not None
        assert param.element_type == ControlParameterOutput.__name__

    def test_on_each_display_name(self, iterative_group: MockIterativeGroup) -> None:
        """on_each must carry the display name 'On Each'."""
        param = iterative_group.get_parameter_by_name("on_each")
        assert param is not None
        display_name = param.ui_options.get("display_name")
        assert display_name == "On Each"

    def test_exec_out_exists_with_correct_element_type(self, iterative_group: MockIterativeGroup) -> None:
        """exec_out must be registered and have element_type ControlParameterOutput."""
        param = iterative_group.get_parameter_by_name("exec_out")
        assert param is not None
        assert param.element_type == ControlParameterOutput.__name__

    def test_exec_out_display_name(self, iterative_group: MockIterativeGroup) -> None:
        """exec_out must carry the display name 'On Complete'."""
        param = iterative_group.get_parameter_by_name("exec_out")
        assert param is not None
        display_name = param.ui_options.get("display_name")
        assert display_name == "On Complete"

    def test_exec_in_in_left_parameters_metadata(self, iterative_group: MockIterativeGroup) -> None:
        """exec_in must appear in metadata[left_parameters]."""
        left_params = iterative_group.metadata.get(LEFT_PARAMETERS_KEY, [])
        assert "exec_in" in left_params

    def test_on_each_in_left_parameters_metadata(self, iterative_group: MockIterativeGroup) -> None:
        """on_each must appear in metadata[left_parameters]."""
        left_params = iterative_group.metadata.get(LEFT_PARAMETERS_KEY, [])
        assert "on_each" in left_params

    def test_exec_out_in_right_parameters_metadata(self, iterative_group: MockIterativeGroup) -> None:
        """exec_out must appear in metadata[right_parameters]."""
        right_params = iterative_group.metadata.get(RIGHT_PARAMETERS_KEY, [])
        assert "exec_out" in right_params

    def test_get_next_control_output_returns_exec_out(self, iterative_group: MockIterativeGroup) -> None:
        """get_next_control_output() must return the exec_out parameter instance."""
        result = iterative_group.get_next_control_output()
        assert result is iterative_group.exec_out


class TestBaseIterativeNodeGroupSingleControlPair:
    """A loop group shows one way in and one way out: Start Loop and On Complete (#6250).

    The base pair (group_exec_in / group_exec_out, drawn as Flow In / Flow Out) used to sit beside the
    loop's own pair, and nothing told the artist which to wire. Only the loop's pair ever advanced the
    chain, so a workflow wired through Flow Out stopped at the loop.
    """

    def test_the_loop_pair_is_the_group_pair(self, iterative_group: MockIterativeGroup) -> None:
        """Start Loop and On Complete are the base class's control_in / control_out, relabelled."""
        assert iterative_group.exec_in is iterative_group.control_in
        assert iterative_group.exec_out is iterative_group.control_out

    def test_exactly_one_entry_control_port(self, iterative_group: MockIterativeGroup) -> None:
        """The only way into the loop from upstream is Start Loop; the body re-enters through its own ports."""
        entry_ports = [
            param.name
            for param in iterative_group.parameters
            if param.element_type == ControlParameterInput.__name__
            and param.name not in {"loop_complete", "skip_iteration", "break_loop"}
        ]
        assert entry_ports == ["exec_in"]

    def test_exactly_one_exit_control_port(self, iterative_group: MockIterativeGroup) -> None:
        """The only way on from the loop is On Complete; On Each fires into the body, not downstream."""
        exit_ports = [
            param.name
            for param in iterative_group.parameters
            if param.element_type == ControlParameterOutput.__name__ and param.name != "on_each"
        ]
        assert exit_ports == ["exec_out"]

    def test_exec_out_display_name(self, iterative_group: MockIterativeGroup) -> None:
        assert iterative_group.exec_out.ui_options.get("display_name") == "On Complete"


class TestBaseIterativeNodeGroupLegacyControlPortNames:
    """Workflows saved while the loop still carried Flow In / Flow Out keep loading and running."""

    def test_legacy_names_resolve_to_the_loop_ports(self, iterative_group: MockIterativeGroup) -> None:
        assert iterative_group.get_parameter_by_name("group_exec_in") is iterative_group.exec_in
        assert iterative_group.get_parameter_by_name("group_exec_out") is iterative_group.exec_out
        assert iterative_group.get_parameter_by_name("not_a_port") is None

    def test_saved_rails_drop_the_legacy_names(self, iterative_group: MockIterativeGroup) -> None:
        """Rail metadata saved by an older engine lists the retired pair; the rebuilt group leaves it out."""
        saved = copy.deepcopy(iterative_group.metadata)
        saved[LEFT_PARAMETERS_KEY] = ["exec_in", "group_exec_in", "on_each", "index"]
        saved[RIGHT_PARAMETERS_KEY] = ["exec_out", "group_exec_out", *saved[RIGHT_PARAMETERS_KEY][1:]]

        restored = MockIterativeGroup(name="restored", metadata=saved)

        assert set(restored.metadata[LEFT_PARAMETERS_KEY]) == set(iterative_group.metadata[LEFT_PARAMETERS_KEY])
        assert set(restored.metadata[RIGHT_PARAMETERS_KEY]) == set(iterative_group.metadata[RIGHT_PARAMETERS_KEY])

    def test_connection_saved_against_legacy_names_lands_on_the_loop_ports(self, engine: Engine) -> None:
        """A saved workflow's CreateConnectionRequest naming group_exec_in / group_exec_out wires the loop's pair."""
        engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
        engine.context_manager.push_workflow("legacy_loop_ports_wf")
        result = engine.handle_request(CreateFlowRequest(parent_flow_name=None, set_as_new_context=True))
        assert isinstance(result, CreateFlowResultSuccess)
        workflow = engine.current_workflow()
        before, loop, after = _Step(name="before"), MockIterativeGroup(name="loop"), _Step(name="after")
        for node in (before, loop, after):
            engine.flow_manager.get_flow_by_name_in(workflow, result.flow_name).add_node(node)
            engine.object_manager.add_object_by_name_in(workflow, node.name, node)
            workflow.nodes.name_to_parent_flow_name[node.name] = result.flow_name
        try:
            into = engine.handle_request(
                CreateConnectionRequest(
                    source_node_name="before",
                    source_parameter_name="exec_out",
                    target_node_name="loop",
                    target_parameter_name="group_exec_in",
                )
            )
            out_of = engine.handle_request(
                CreateConnectionRequest(
                    source_node_name="loop",
                    source_parameter_name="group_exec_out",
                    target_node_name="after",
                    target_parameter_name="exec_in",
                )
            )
            assert isinstance(into, CreateConnectionResultSuccess), into.result_details
            assert isinstance(out_of, CreateConnectionResultSuccess), out_of.result_details

            connections = engine.flow_manager.get_connections()
            incoming = connections.get_incoming_connections_to_parameter(loop, loop.exec_in)
            assert [conn.source_node.name for conn in incoming] == ["before"]
            outgoing = connections.get_outgoing_connections_from_parameter(loop, loop.exec_out)
            assert [conn.target_node.name for conn in outgoing] == ["after"]
        finally:
            engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))


class TestBaseIterativeNodeGroupRails:
    """The rail metadata names which of the group's parameters the editor draws down each side."""

    def test_fresh_group_names_each_rail_parameter_once(self, iterative_group: MockIterativeGroup) -> None:
        """The rails are membership sets, so this pins which names are there and that none repeats."""
        expected_left = {"exec_in", "on_each", "index"}
        expected_right = {
            "exec_out",
            "loop_complete",
            "new_item_to_add",
            "skip_iteration",
            "break_loop",
            "results",
        }
        assert set(iterative_group.metadata[LEFT_PARAMETERS_KEY]) == expected_left
        assert len(iterative_group.metadata[LEFT_PARAMETERS_KEY]) == len(expected_left)
        assert set(iterative_group.metadata[RIGHT_PARAMETERS_KEY]) == expected_right
        assert len(iterative_group.metadata[RIGHT_PARAMETERS_KEY]) == len(expected_right)


class TestBaseIterativeNodeGroupRestoreRoundTrip:
    """Rebuilding a group from its saved metadata must reproduce its rails, not grow them.

    Load hands the saved metadata straight to the constructor, and the group holds on to that very
    list, so any port the constructor re-records is a port the artist sees twice.
    """

    def test_restored_group_has_the_same_rail_parameters(self, iterative_group: MockIterativeGroup) -> None:
        """A group rebuilt from what it was saved with names the same ports, once each."""
        restored = MockIterativeGroup(name="restored", metadata=copy.deepcopy(iterative_group.metadata))

        assert set(restored.metadata[LEFT_PARAMETERS_KEY]) == set(iterative_group.metadata[LEFT_PARAMETERS_KEY])
        assert len(restored.metadata[LEFT_PARAMETERS_KEY]) == len(iterative_group.metadata[LEFT_PARAMETERS_KEY])
        assert set(restored.metadata[RIGHT_PARAMETERS_KEY]) == set(iterative_group.metadata[RIGHT_PARAMETERS_KEY])
        assert len(restored.metadata[RIGHT_PARAMETERS_KEY]) == len(iterative_group.metadata[RIGHT_PARAMETERS_KEY])

    def test_a_third_generation_still_has_the_same_rail_parameters(self, iterative_group: MockIterativeGroup) -> None:
        """Duplicates compound across save/load cycles, so a second rebuild has to stay stable too."""
        restored = MockIterativeGroup(name="restored", metadata=copy.deepcopy(iterative_group.metadata))
        restored_again = MockIterativeGroup(name="restored_again", metadata=copy.deepcopy(restored.metadata))

        assert set(restored_again.metadata[LEFT_PARAMETERS_KEY]) == set(iterative_group.metadata[LEFT_PARAMETERS_KEY])
        assert len(restored_again.metadata[LEFT_PARAMETERS_KEY]) == len(iterative_group.metadata[LEFT_PARAMETERS_KEY])
        assert set(restored_again.metadata[RIGHT_PARAMETERS_KEY]) == set(iterative_group.metadata[RIGHT_PARAMETERS_KEY])
        assert len(restored_again.metadata[RIGHT_PARAMETERS_KEY]) == len(iterative_group.metadata[RIGHT_PARAMETERS_KEY])


class TestFlattenResults:
    """Tests for the Flatten Results setting on BaseIterativeNodeGroup."""

    def test_results_keep_their_shape_by_default(self, iterative_group: MockIterativeGroup) -> None:
        iterative_group._results_list = [["a", "b"], ["c"]]
        iterative_group._output_results_list()
        assert iterative_group.parameter_output_values["results"] == [["a", "b"], ["c"]]

    def test_flatten_results_flattens_every_level_at_depth_zero(self, iterative_group: MockIterativeGroup) -> None:
        iterative_group.set_parameter_value("flatten_results", True)
        iterative_group._results_list = [["a", ["b", ["c"]]], "d", None]
        iterative_group._output_results_list()
        assert iterative_group.parameter_output_values["results"] == ["a", "b", "c", "d", None]

    def test_flatten_depth_limits_levels(self, iterative_group: MockIterativeGroup) -> None:
        iterative_group.set_parameter_value("flatten_results", True)
        iterative_group.set_parameter_value("flatten_depth", 1)
        iterative_group._results_list = [["a", ["b"]], ["c"]]
        iterative_group._output_results_list()
        assert iterative_group.parameter_output_values["results"] == ["a", ["b"], "c"]

    def test_strings_and_dicts_stay_whole(self, iterative_group: MockIterativeGroup) -> None:
        iterative_group.set_parameter_value("flatten_results", True)
        iterative_group._results_list = ["ab", {"k": [1, 2]}, ["cd"]]
        iterative_group._output_results_list()
        assert iterative_group.parameter_output_values["results"] == ["ab", {"k": [1, 2]}, "cd"]

    def test_flatten_depth_shows_only_while_flatten_results_is_on(self, iterative_group: MockIterativeGroup) -> None:
        depth = iterative_group.get_parameter_by_name("flatten_depth")
        assert depth is not None
        assert depth.ui_options.get("hide") is True
        iterative_group.set_parameter_value("flatten_results", True)
        assert not depth.ui_options.get("hide")
        iterative_group.set_parameter_value("flatten_results", False)
        assert depth.ui_options.get("hide") is True

    def test_flatten_settings_are_in_the_group_settings_panel(self, iterative_group: MockIterativeGroup) -> None:
        settings = iterative_group.metadata["group_settings_params"]
        assert "flatten_results" in settings
        assert "flatten_depth" in settings
