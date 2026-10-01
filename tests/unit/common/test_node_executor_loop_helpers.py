"""Contract tests for NodeExecutor loop-control helpers.

These tests cover small, near-pure helpers that decide loop control flow:

* ``get_node_parameter_mappings`` - select the start or end mapping out of a
  PackageNodesAsSerializedFlowResultSuccess.
* ``_get_iteration_control_action`` - determine BREAK/SKIP/ADD for both the
  legacy BaseIterativeEndNode path and the BaseIterativeNodeGroup path.
* ``_get_control_source_that_reached_end`` / ``_forget_control_source_that_reached_end`` -
  read and clear the body exit that handed control to the packaged End node.
* ``_check_control_source_fired`` - decide whether a (source_node, source_param)
  pair is the exit that handed control to the packaged End node.
* ``_find_source_for_control_param`` - return the first source for a given
  control parameter name, or None.
* ``_format_loop_failure_message`` / ``_format_iteration_failure_lines`` - compose
  the artist-facing error naming which iterations failed and why.
* ``_silence_packaged_node_creation_broadcasts`` - keep a packaged loop body's node
  creations from reaching editors.
"""

import logging
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest

from griptape_nodes.common.node_executor import IterationControlAction, IterationFailure, NodeExecutor
from griptape_nodes.exe_types.base_iterative_nodes import BaseIterativeEndNode
from griptape_nodes.exe_types.node_groups.base_iterative_node_group import BaseIterativeNodeGroup
from griptape_nodes.retained_mode.events.base_events import ResultDetail, ResultDetails
from griptape_nodes.retained_mode.events.flow_events import OriginalNodeParameter, PackagedNodeParameterMapping
from griptape_nodes.retained_mode.events.node_events import (
    CreateNodeRequest,
    ListConnectionsForNodeResultSuccess,
    NodeDependencies,
    SerializedNodeCommands,
)


def _make_executor() -> NodeExecutor:
    return NodeExecutor(engine=MagicMock())


def _make_package_result(
    *,
    start_node_name: str = "StartPkg",
    end_node_name: str = "EndPkg",
    start_param_mappings: dict[str, Any] | None = None,
    end_param_mappings: dict[str, Any] | None = None,
) -> MagicMock:
    """Mock PackageNodesAsSerializedFlowResultSuccess with start/end mappings at indices 0/1."""
    package_result = MagicMock()
    start_mapping = MagicMock()
    start_mapping.node_name = start_node_name
    start_mapping.parameter_mappings = start_param_mappings or {}
    end_mapping = MagicMock()
    end_mapping.node_name = end_node_name
    end_mapping.parameter_mappings = end_param_mappings or {}
    package_result.parameter_name_mappings = [start_mapping, end_mapping]
    return package_result


def _make_end_node_mapping(
    parameter_mappings: dict[str, OriginalNodeParameter] | None = None,
) -> PackagedNodeParameterMapping:
    return PackagedNodeParameterMapping(node_name="EndPkg", parameter_mappings=parameter_mappings or {})


def _make_end_node(*, entered_on: str | None) -> MagicMock:
    """Mock packaged End node whose recorded entry is the named control input, or None."""
    end_node = MagicMock()
    if entered_on is None:
        end_node._entry_control_parameter = None
        return end_node
    entry_parameter = MagicMock()
    entry_parameter.name = entered_on
    end_node._entry_control_parameter = entry_parameter
    return end_node


class TestGetNodeParameterMappings:
    """Returns index 0 for 'start', index 1 for 'end'; raises for anything else."""

    def test_returns_start_mapping_for_start(self) -> None:
        package = _make_package_result(start_node_name="MyStart")
        mapping = _make_executor().get_node_parameter_mappings(package, "start")
        assert mapping.node_name == "MyStart"

    def test_returns_end_mapping_for_end(self) -> None:
        package = _make_package_result(end_node_name="MyEnd")
        mapping = _make_executor().get_node_parameter_mappings(package, "end")
        assert mapping.node_name == "MyEnd"

    def test_is_case_insensitive(self) -> None:
        package = _make_package_result(start_node_name="MyStart", end_node_name="MyEnd")
        executor = _make_executor()
        assert executor.get_node_parameter_mappings(package, "START").node_name == "MyStart"
        assert executor.get_node_parameter_mappings(package, "End").node_name == "MyEnd"

    def test_raises_value_error_for_other_strings(self) -> None:
        package = _make_package_result()
        with pytest.raises(ValueError, match="middle"):
            _make_executor().get_node_parameter_mappings(package, "middle")


class TestGetIterationControlAction:
    """_get_iteration_control_action returns BREAK/SKIP/ADD for both legacy and group end nodes."""

    @staticmethod
    def _make_connections_result(connections: list[Any]) -> MagicMock:
        result = MagicMock(spec=ListConnectionsForNodeResultSuccess)
        result.incoming_connections = connections
        return result

    @staticmethod
    def _make_connection(*, target_param: str, source_node: str, source_param: str) -> MagicMock:
        conn = MagicMock()
        conn.target_parameter_name = target_param
        conn.source_node_name = source_node
        conn.source_parameter_name = source_param
        return conn

    def _run(
        self,
        end_node: Any,
        connections: list[Any],
        check_fired_returns: dict[str, bool],
    ) -> IterationControlAction:
        """Run _get_iteration_control_action with mocked connections and fired results."""
        connections_result = self._make_connections_result(connections)

        # _find_sources_for_control_param returns the direct source for each connection
        # _check_control_source_fired is keyed on the source_node_name returned
        def fake_find_sources(incoming: list, param_name: str) -> list[tuple[str, str]]:
            return [
                (c.source_node_name, c.source_parameter_name) for c in incoming if c.target_parameter_name == param_name
            ]

        def fake_check_fired(source: tuple[str, str] | None, _source_that_reached_end: Any) -> bool:
            if source is None:
                return False
            return check_fired_returns.get(source[0], False)

        executor = _make_executor()
        mock_engine = cast("MagicMock", executor.engine)
        mock_engine.handle_request.return_value = connections_result
        with (
            patch.object(NodeExecutor, "_find_sources_for_control_param", side_effect=fake_find_sources),
            patch.object(NodeExecutor, "_get_control_source_that_reached_end", return_value=None),
            patch.object(NodeExecutor, "_check_control_source_fired", side_effect=fake_check_fired),
        ):
            return executor._get_iteration_control_action(end_node, _make_end_node_mapping(), {})

    def test_returns_add_when_no_connections(self) -> None:
        end_node = MagicMock(spec=BaseIterativeEndNode)
        end_node.name = "EndLoop"
        result = self._run(end_node, [], {})
        assert result == IterationControlAction.ADD

    def test_returns_add_when_no_source_fired(self) -> None:
        end_node = MagicMock(spec=BaseIterativeEndNode)
        end_node.name = "EndLoop"
        connections = [
            self._make_connection(target_param="break_loop", source_node="BodyNode", source_param="exec_out"),
        ]
        result = self._run(end_node, connections, {"BodyNode": False})
        assert result == IterationControlAction.ADD

    def test_legacy_end_node_returns_break_when_break_source_fired(self) -> None:
        end_node = MagicMock(spec=BaseIterativeEndNode)
        end_node.name = "EndLoop"
        connections = [
            self._make_connection(target_param="break_loop", source_node="CondNode", source_param="exec_out"),
        ]
        result = self._run(end_node, connections, {"CondNode": True})
        assert result == IterationControlAction.BREAK

    def test_legacy_end_node_returns_skip_when_skip_source_fired(self) -> None:
        end_node = MagicMock(spec=BaseIterativeEndNode)
        end_node.name = "EndLoop"
        connections = [
            self._make_connection(target_param="skip_iteration", source_node="CondNode", source_param="exec_out"),
        ]
        result = self._run(end_node, connections, {"CondNode": True})
        assert result == IterationControlAction.SKIP

    def test_break_takes_priority_over_skip(self) -> None:
        end_node = MagicMock(spec=BaseIterativeEndNode)
        end_node.name = "EndLoop"
        connections = [
            self._make_connection(target_param="break_loop", source_node="BreakNode", source_param="exec_out"),
            self._make_connection(target_param="skip_iteration", source_node="SkipNode", source_param="exec_out"),
        ]
        result = self._run(end_node, connections, {"BreakNode": True, "SkipNode": True})
        assert result == IterationControlAction.BREAK

    def test_group_end_node_returns_break_when_break_source_fired(self) -> None:
        end_node = MagicMock(spec=BaseIterativeNodeGroup)
        end_node.name = "ForEachGroup"
        connections = [
            self._make_connection(target_param="break_loop", source_node="BodyNode", source_param="exec_out"),
        ]
        result = self._run(end_node, connections, {"BodyNode": True})
        assert result == IterationControlAction.BREAK

    def test_returns_add_when_list_connections_fails(self) -> None:
        end_node = MagicMock(spec=BaseIterativeEndNode)
        end_node.name = "EndLoop"
        executor = _make_executor()
        mock_engine = cast("MagicMock", executor.engine)
        # Return a non-success result from handle_request
        mock_engine.handle_request.return_value = MagicMock(spec=object)  # not ListConnectionsForNodeResultSuccess
        result = executor._get_iteration_control_action(end_node, _make_end_node_mapping(), {})
        assert result == IterationControlAction.ADD


class TestCheckControlSourceFired:
    """_check_control_source_fired matches a connection source to the exit that reached End."""

    def test_returns_false_when_source_is_none(self) -> None:
        reached = OriginalNodeParameter(node_name="Src", parameter_name="out")
        assert _make_executor()._check_control_source_fired(None, reached) is False

    def test_returns_false_when_control_never_reached_end(self) -> None:
        assert _make_executor()._check_control_source_fired(("Src", "out"), None) is False

    def test_returns_true_when_source_is_the_exit_that_reached_end(self) -> None:
        reached = OriginalNodeParameter(node_name="Src", parameter_name="out")
        assert _make_executor()._check_control_source_fired(("Src", "out"), reached) is True

    def test_returns_false_for_another_output_of_the_same_node(self) -> None:
        reached = OriginalNodeParameter(node_name="Src", parameter_name="other")
        assert _make_executor()._check_control_source_fired(("Src", "out"), reached) is False

    def test_returns_false_for_the_same_output_name_on_another_node(self) -> None:
        reached = OriginalNodeParameter(node_name="Other", parameter_name="out")
        assert _make_executor()._check_control_source_fired(("Src", "out"), reached) is False


class TestGetControlSourceThatReachedEnd:
    """_get_control_source_that_reached_end reads the packaged End node's recorded entry."""

    def test_returns_none_when_end_node_not_in_mappings(self) -> None:
        mapping = _make_end_node_mapping()
        assert _make_executor()._get_control_source_that_reached_end(mapping, {}) is None

    def test_returns_none_when_node_manager_raises_value_error(self) -> None:
        executor = _make_executor()
        mock_engine = cast("MagicMock", executor.engine)
        mock_engine.node_manager.get_node_by_name.side_effect = ValueError("not found")
        result = executor._get_control_source_that_reached_end(_make_end_node_mapping(), {"EndPkg": "EndPkg_inst1"})
        assert result is None

    def test_returns_none_when_control_never_entered_end(self) -> None:
        executor = _make_executor()
        mock_engine = cast("MagicMock", executor.engine)
        mock_engine.node_manager.get_node_by_name.return_value = _make_end_node(entered_on=None)
        result = executor._get_control_source_that_reached_end(_make_end_node_mapping(), {"EndPkg": "EndPkg_inst1"})
        assert result is None

    def test_returns_none_when_end_entered_on_an_input_with_no_body_source(self) -> None:
        # The while path wires the Start node straight to End's exec_in, which maps to no body exit.
        executor = _make_executor()
        mock_engine = cast("MagicMock", executor.engine)
        mock_engine.node_manager.get_node_by_name.return_value = _make_end_node(entered_on="exec_in")
        result = executor._get_control_source_that_reached_end(_make_end_node_mapping(), {"EndPkg": "EndPkg_inst1"})
        assert result is None

    def test_returns_the_body_exit_the_entry_input_maps_to(self) -> None:
        executor = _make_executor()
        mock_engine = cast("MagicMock", executor.engine)
        mock_engine.node_manager.get_node_by_name.return_value = _make_end_node(entered_on="Cond_True")
        mapping = _make_end_node_mapping({"Cond_True": OriginalNodeParameter(node_name="Cond", parameter_name="True")})
        result = executor._get_control_source_that_reached_end(mapping, {"EndPkg": "EndPkg_inst1"})
        assert result == OriginalNodeParameter(node_name="Cond", parameter_name="True")
        mock_engine.node_manager.get_node_by_name.assert_called_once_with("EndPkg_inst1")


class TestForgetControlSourceThatReachedEnd:
    """_forget_control_source_that_reached_end clears End's recorded entry before a pass."""

    def test_clears_the_end_nodes_entry(self) -> None:
        executor = _make_executor()
        mock_engine = cast("MagicMock", executor.engine)
        end_node = _make_end_node(entered_on="Cond_True")
        mock_engine.node_manager.get_node_by_name.return_value = end_node
        executor._forget_control_source_that_reached_end(_make_end_node_mapping(), {"EndPkg": "EndPkg_inst1"})
        end_node.set_entry_control_parameter.assert_called_once_with(None)

    def test_does_nothing_when_end_node_not_in_mappings(self) -> None:
        executor = _make_executor()
        mock_engine = cast("MagicMock", executor.engine)
        executor._forget_control_source_that_reached_end(_make_end_node_mapping(), {})
        mock_engine.node_manager.get_node_by_name.assert_not_called()


class TestFindSourceForControlParam:
    """_find_source_for_control_param returns the first source from the multi-source helper."""

    def test_returns_first_source_when_multiple_present(self) -> None:
        executor = _make_executor()
        with patch.object(
            NodeExecutor,
            "_find_sources_for_control_param",
            return_value=[("A", "out"), ("B", "out")],
        ) as mock_multi:
            result = executor._find_source_for_control_param([], "break_loop")

        assert result == ("A", "out")
        mock_multi.assert_called_once_with([], "break_loop")

    def test_returns_none_when_no_sources(self) -> None:
        executor = _make_executor()
        with patch.object(NodeExecutor, "_find_sources_for_control_param", return_value=[]):
            result = executor._find_source_for_control_param([], "break_loop")

        assert result is None


class TestGetIterationControlActionEndToEnd:
    """Real name-matching tests for _get_iteration_control_action.

    _find_source_for_control_param, _get_control_source_that_reached_end and
    _check_control_source_fired are NOT mocked, so a regression in parameter-name
    matching will fail these tests.
    """

    @staticmethod
    def _make_connection(*, target_param: str, source_node: str, source_param: str) -> MagicMock:
        conn = MagicMock()
        conn.target_parameter_name = target_param
        conn.source_node_name = source_node
        conn.source_parameter_name = source_param
        return conn

    @staticmethod
    def _make_connections_result(connections: list[Any]) -> MagicMock:
        result = MagicMock(spec=ListConnectionsForNodeResultSuccess)
        result.incoming_connections = connections
        return result

    def _run_real(
        self,
        end_node: Any,
        connections: list[Any],
        node_name_mappings: dict[str, str],
        entered_on: str | None,
    ) -> IterationControlAction:
        """Run _get_iteration_control_action with only the engine mocked.

        The packaged End node maps its CondNode_orig_exec_out input to CondNode_orig.exec_out
        and records ``entered_on`` as the input control reached it on.
        """
        connections_result = self._make_connections_result(connections)
        executor = _make_executor()
        mock_engine = cast("MagicMock", executor.engine)
        deserialized_nodes = {"EndPkg_inst1": _make_end_node(entered_on=entered_on)}

        # Canvas lookups of the body's source nodes get a plain node; only End's entry matters.
        def fake_get_node(name: str) -> Any:
            return deserialized_nodes.get(name, MagicMock())

        mock_engine.handle_request.return_value = connections_result
        mock_engine.node_manager.get_node_by_name.side_effect = fake_get_node
        end_node_mapping = _make_end_node_mapping(
            {"CondNode_orig_exec_out": OriginalNodeParameter(node_name="CondNode_orig", parameter_name="exec_out")}
        )
        return executor._get_iteration_control_action(end_node, end_node_mapping, node_name_mappings)

    def test_legacy_break_detected_via_real_name_matching(self) -> None:
        """BREAK is detected through real connection/name resolution for a legacy end node."""
        end_node = MagicMock(spec=BaseIterativeEndNode)
        end_node.name = "EndLoop"
        connections = [
            self._make_connection(target_param="break_loop", source_node="CondNode_orig", source_param="exec_out"),
        ]
        result = self._run_real(end_node, connections, {"EndPkg": "EndPkg_inst1"}, "CondNode_orig_exec_out")
        assert result == IterationControlAction.BREAK

    def test_legacy_skip_detected_via_real_name_matching(self) -> None:
        """SKIP is detected through real connection/name resolution for a legacy end node."""
        end_node = MagicMock(spec=BaseIterativeEndNode)
        end_node.name = "EndLoop"
        connections = [
            self._make_connection(target_param="skip_iteration", source_node="CondNode_orig", source_param="exec_out"),
        ]
        result = self._run_real(end_node, connections, {"EndPkg": "EndPkg_inst1"}, "CondNode_orig_exec_out")
        assert result == IterationControlAction.SKIP

    def test_wrong_parameter_name_does_not_trigger_break(self) -> None:
        """A connection targeting the wrong param name does NOT trigger BREAK."""
        end_node = MagicMock(spec=BaseIterativeEndNode)
        end_node.name = "EndLoop"
        connections = [
            # Misspelled control param — should NOT match break_loop
            self._make_connection(target_param="break_loooop", source_node="CondNode_orig", source_param="exec_out"),
        ]
        result = self._run_real(end_node, connections, {"EndPkg": "EndPkg_inst1"}, "CondNode_orig_exec_out")
        assert result == IterationControlAction.ADD

    def test_break_not_triggered_when_control_never_reached_end(self) -> None:
        """A break source that is wired but was not the exit taken does NOT trigger BREAK."""
        end_node = MagicMock(spec=BaseIterativeEndNode)
        end_node.name = "EndLoop"
        connections = [
            self._make_connection(target_param="break_loop", source_node="CondNode_orig", source_param="exec_out"),
        ]
        result = self._run_real(end_node, connections, {"EndPkg": "EndPkg_inst1"}, None)
        assert result == IterationControlAction.ADD

    def test_end_node_not_in_mappings_does_not_trigger_break(self) -> None:
        """If the packaged End node is missing from node_name_mappings, no BREAK fires."""
        end_node = MagicMock(spec=BaseIterativeEndNode)
        end_node.name = "EndLoop"
        connections = [
            self._make_connection(target_param="break_loop", source_node="CondNode_orig", source_param="exec_out"),
        ]
        result = self._run_real(end_node, connections, {}, "CondNode_orig_exec_out")
        assert result == IterationControlAction.ADD


def _make_iteration_failures(details: list[str]) -> list[IterationFailure]:
    """One IterationFailure per detail, indexed 0..n-1."""
    return [IterationFailure(iteration_index=index, detail=detail) for index, detail in enumerate(details)]


class TestFormatIterationFailureLines:
    """Renders one indented line per distinct reason, collapsing iterations that agree."""

    def test_returns_no_lines_for_no_failures(self) -> None:
        assert NodeExecutor._format_iteration_failure_lines([]) == []

    def test_renders_one_based_iteration_number(self) -> None:
        """Iteration 0 internally is iteration 1 to the artist who built the loop."""
        lines = NodeExecutor._format_iteration_failure_lines([IterationFailure(iteration_index=0, detail="boom")])
        assert lines == ["  Iteration 1: boom"]

    def test_collapses_iterations_sharing_a_detail(self) -> None:
        """The common case is every iteration failing identically; say the reason once."""
        failures = [IterationFailure(iteration_index=index, detail="same reason") for index in range(3)]
        lines = NodeExecutor._format_iteration_failure_lines(failures)
        assert lines == ["  Iterations 1-3: same reason"]

    def test_names_a_wholly_failed_loop_as_such(self) -> None:
        """When nothing survived, saying so beats naming every iteration."""
        failures = [IterationFailure(iteration_index=index, detail="same reason") for index in range(50)]
        lines = NodeExecutor._format_iteration_failure_lines(failures, total_iterations=50)
        assert lines == ["  Every iteration: same reason"]

    def test_names_the_iteration_when_the_loop_had_only_one(self) -> None:
        """A one-item loop satisfies both "every iteration" and "one iteration"; the number wins."""
        lines = NodeExecutor._format_iteration_failure_lines(
            [IterationFailure(iteration_index=0, detail="boom")], total_iterations=1
        )
        assert lines == ["  Iteration 1: boom"]

    def test_keeps_the_line_short_for_a_long_collapsed_run(self) -> None:
        """Capping reasons is not enough: the iteration list inside one reason must be bounded too.

        Without this, 500 iterations failing identically push the reason -- the part worth reading --
        kilobytes to the right of where the artist starts reading.
        """
        iteration_count = 500
        failures = [IterationFailure(iteration_index=index, detail="boom") for index in range(iteration_count)]
        # No total_iterations, so the "every iteration" shortcut cannot apply and the run must
        # still be summarised rather than enumerated.
        lines = NodeExecutor._format_iteration_failure_lines(failures)
        assert lines == ["  Iterations 1-500: boom"]

    def test_truncates_a_scattered_iteration_list(self) -> None:
        """A non-contiguous set has no range to collapse to, so the enumeration itself is capped."""
        scattered = [1, 3, 5, 7, 9, 11, 13, 15, 17]
        failures = [IterationFailure(iteration_index=index, detail="boom") for index in scattered]
        lines = NodeExecutor._format_iteration_failure_lines(failures)
        assert lines == ["  Iterations 2, 4, 6, 8, 10, 12 (+3 more): boom"]

    def test_names_a_two_iteration_gap_without_a_range(self) -> None:
        failures = [IterationFailure(iteration_index=index, detail="boom") for index in (0, 4)]
        lines = NodeExecutor._format_iteration_failure_lines(failures)
        assert lines == ["  Iterations 1, 5: boom"]

    def test_keeps_distinct_details_on_separate_lines(self) -> None:
        lines = NodeExecutor._format_iteration_failure_lines(_make_iteration_failures(["first", "second"]))
        assert lines == ["  Iteration 1: first", "  Iteration 2: second"]

    def test_caps_lines_and_reports_the_remainder(self) -> None:
        reason_count = 8
        max_lines = 3
        failures = _make_iteration_failures([f"reason {index}" for index in range(reason_count)])
        lines = NodeExecutor._format_iteration_failure_lines(failures, max_lines=max_lines)
        assert len(lines) == max_lines + 1  # the capped reasons, plus the tail line
        assert f"... and {reason_count - max_lines} more reason(s)" in lines[-1]
        assert "engine log" in lines[-1]

    def test_tail_line_counts_the_iterations_behind_the_omitted_reasons(self) -> None:
        """The lines above the tail are phrased in iterations, so the tail has to be too.

        A reason count alone reads as an iteration count: here one omitted reason covers 95 of the
        100 iterations, and "and 1 more reason(s)" undersells the loop's failure by two orders of
        magnitude.
        """
        max_lines = 5
        failures = [IterationFailure(iteration_index=index, detail=f"reason {index}") for index in range(max_lines)]
        failures += [
            IterationFailure(iteration_index=max_lines + index, detail="the reason that got cut") for index in range(95)
        ]

        lines = NodeExecutor._format_iteration_failure_lines(failures, total_iterations=100, max_lines=max_lines)

        assert len(lines) == max_lines + 1
        assert "1 more reason(s) affecting 95 iteration(s)" in lines[-1]

    def test_does_not_add_a_tail_line_when_everything_fits(self) -> None:
        failures = _make_iteration_failures(["a", "b", "c"])
        lines = NodeExecutor._format_iteration_failure_lines(failures, max_lines=len(failures) + 2)
        assert len(lines) == len(failures)
        assert not any("more reason" in line for line in lines)

    def test_preserves_multiline_detail_text(self) -> None:
        """ResultDetails.__str__ joins several messages with newlines; keep them verbatim."""
        detail = "Attempted to run node 'Blur'.\nFailed because the input image was empty."
        lines = NodeExecutor._format_iteration_failure_lines([IterationFailure(iteration_index=4, detail=detail)])
        assert lines == [f"  Iteration 5: {detail}"]


class TestFormatLoopFailureMessage:
    """Leads with what was attempted and how much was lost, then names the reasons."""

    def test_includes_loop_name_and_counts(self) -> None:
        msg = NodeExecutor._format_loop_failure_message(
            loop_name="Trim Frames End",
            total_iterations=4,
            iteration_failures=_make_iteration_failures(["first", "second"]),
        )
        assert "'Trim Frames End'" in msg
        assert "all 4 iterations" in msg
        assert "2 of them did not finish" in msg

    def test_follows_the_attempted_failed_because_form(self) -> None:
        msg = NodeExecutor._format_loop_failure_message(
            loop_name="Trim Frames End",
            total_iterations=1,
            iteration_failures=_make_iteration_failures(["boom"]),
        )
        assert msg.startswith("Attempted to run all")
        assert ". Failed because " in msg

    def test_appends_one_detail_line_per_reason(self) -> None:
        failures = _make_iteration_failures(["first", "second"])
        msg = NodeExecutor._format_loop_failure_message(
            loop_name="Trim Frames End",
            total_iterations=len(failures),
            iteration_failures=failures,
        )
        # One newline joining the summary to the detail block, then one per extra reason.
        assert msg.count("\n") == len(failures)

    def test_returns_summary_only_when_failures_empty(self) -> None:
        """Guards the branch even though production only calls this with failures."""
        msg = NodeExecutor._format_loop_failure_message(
            loop_name="Trim Frames End", total_iterations=3, iteration_failures=[]
        )
        assert "\n" not in msg

    def test_message_survives_str_of_result_details(self) -> None:
        """The collector stores str(result_details); both messages must reach the artist."""
        details = ResultDetails(
            ResultDetail(message="Attempted to run node 'Blur'.", level=logging.ERROR),
            ResultDetail(message="Failed because the input image was empty.", level=logging.ERROR),
        )
        msg = NodeExecutor._format_loop_failure_message(
            loop_name="Trim Frames End",
            total_iterations=1,
            iteration_failures=[IterationFailure(iteration_index=0, detail=str(details))],
        )
        assert "Attempted to run node 'Blur'." in msg
        assert "Failed because the input image was empty." in msg


def _make_package_result_with_nodes(node_count: int) -> MagicMock:
    """Package result whose serialized_node_commands are real, so flag flips are observable."""
    serialized_nodes = [
        SerializedNodeCommands(
            create_node_command=CreateNodeRequest(node_type="Note", node_name=f"Body Node {index}"),
            element_modification_commands=[],
            node_dependencies=NodeDependencies(),
        )
        for index in range(node_count)
    ]
    package_result = MagicMock()
    package_result.serialized_flow_commands.serialized_node_commands = serialized_nodes
    return package_result


class TestSilencePackagedNodeCreationBroadcasts:
    """A packaged loop body is engine-internal; its node creations must not reach editors."""

    def test_clears_broadcast_on_every_create_command(self) -> None:
        package_result = _make_package_result_with_nodes(3)
        serialized_nodes = package_result.serialized_flow_commands.serialized_node_commands
        assert all(node.create_node_command.broadcast_result for node in serialized_nodes)

        NodeExecutor._silence_packaged_node_creation_broadcasts(package_result)

        assert all(node.create_node_command.broadcast_result is False for node in serialized_nodes)

    def test_is_idempotent(self) -> None:
        """The parallel path deserializes the same commands once per iteration."""
        node_count = 3
        package_result = _make_package_result_with_nodes(node_count)
        NodeExecutor._silence_packaged_node_creation_broadcasts(package_result)
        NodeExecutor._silence_packaged_node_creation_broadcasts(package_result)

        serialized_nodes = package_result.serialized_flow_commands.serialized_node_commands
        assert len(serialized_nodes) == node_count
        assert all(node.create_node_command.broadcast_result is False for node in serialized_nodes)

    def test_does_not_touch_other_create_command_fields(self) -> None:
        """Guards against a future rewrite reaching for replace() and dropping fields."""
        package_result = _make_package_result_with_nodes(2)
        serialized_nodes = package_result.serialized_flow_commands.serialized_node_commands
        before = [(node.create_node_command.node_type, node.create_node_command.node_name) for node in serialized_nodes]

        NodeExecutor._silence_packaged_node_creation_broadcasts(package_result)

        after = [(node.create_node_command.node_type, node.create_node_command.node_name) for node in serialized_nodes]
        assert after == before

    def test_handles_a_flow_with_no_nodes(self) -> None:
        package_result = _make_package_result_with_nodes(0)
        NodeExecutor._silence_packaged_node_creation_broadcasts(package_result)
        assert package_result.serialized_flow_commands.serialized_node_commands == []
