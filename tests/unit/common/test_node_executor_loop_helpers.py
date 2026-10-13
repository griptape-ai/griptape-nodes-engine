"""Contract tests for NodeExecutor loop-control helpers.

These tests cover small, near-pure helpers that decide loop control flow:

* ``get_node_parameter_mappings`` - select the start or end mapping out of a
  PackageNodesAsSerializedFlowResultSuccess.
* ``_get_iteration_control_action`` - determine BREAK/SKIP/ADD for both the
  legacy BaseIterativeEndNode path and the BaseIterativeNodeGroup path.
* ``_check_control_source_fired`` - decide whether a (source_node, source_param)
  pair has fired its control output.
* ``_find_source_for_control_param`` - return the first source for a given
  control parameter name, or None.
* ``_format_loop_failure_message`` / ``_format_iteration_failure_lines`` - compose
  the artist-facing error naming which iterations failed and why.
* ``_silence_packaged_node_creation_broadcasts`` - keep a packaged loop body's node
  creations from reaching editors.
"""

import asyncio
import logging
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from griptape_nodes.common.node_executor import (
    IterationControlAction,
    IterationFailure,
    IterationOutcome,
    NodeExecutor,
)
from griptape_nodes.exe_types.base_iterative_nodes import BaseIterativeEndNode, BaseIterativeStartNode
from griptape_nodes.exe_types.node_groups.base_iterative_node_group import BaseIterativeNodeGroup
from griptape_nodes.exe_types.node_groups.base_while_node_group import BaseWhileNodeGroup
from griptape_nodes.exe_types.node_types import LOCAL_EXECUTION
from griptape_nodes.exe_types.param_components.progress_bar_component import ProgressBarComponent
from griptape_nodes.exe_types.workflow_node_errors import (
    NestedWorkflowNodeError,
    SubflowNodeFailedError,
    WorkflowRecursionLimitError,
)
from griptape_nodes.retained_mode.engine import Engine
from griptape_nodes.retained_mode.events.base_events import ResultDetail, ResultDetails
from griptape_nodes.retained_mode.events.execution_events import (
    StartLocalSubflowResultFailure,
    StartLocalSubflowResultSuccess,
)
from griptape_nodes.retained_mode.events.flow_events import (
    DeserializeFlowFromCommandsResultSuccess,
    PackagedNodeParameterMapping,
    PackageNodesAsSerializedFlowResultSuccess,
    SerializedFlowCommands,
)
from griptape_nodes.retained_mode.events.node_error_details import NodeErrorDetails
from griptape_nodes.retained_mode.events.node_events import (
    CreateNodeRequest,
    ListConnectionsForNodeResultSuccess,
    NodeDependencies,
    SerializedNodeCommands,
)
from griptape_nodes.retained_mode.managers.context_manager import ContextManager
from griptape_nodes.retained_mode.managers.event_manager import EventManager
from griptape_nodes.retained_mode.managers.flow_manager import FlowManager
from griptape_nodes.retained_mode.managers.node_manager import NodeManager

PARALLEL_ITERATIONS = 2
WHILE_TOTAL_ITERATIONS = 5


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

        def fake_check_fired(source: tuple[str, str] | None, _mappings: dict) -> bool:
            if source is None:
                return False
            return check_fired_returns.get(source[0], False)

        executor = _make_executor()
        mock_engine = cast("MagicMock", executor.engine)
        mock_engine.handle_request.return_value = connections_result
        with (
            patch.object(NodeExecutor, "_find_sources_for_control_param", side_effect=fake_find_sources),
            patch.object(NodeExecutor, "_check_control_source_fired", side_effect=fake_check_fired),
        ):
            return executor._get_iteration_control_action(end_node, {})

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
        result = executor._get_iteration_control_action(end_node, {})
        assert result == IterationControlAction.ADD


class TestCheckControlSourceFired:
    """_check_control_source_fired matches a node's next control output to a parameter."""

    @staticmethod
    def _make_source_node(*, next_control_output: Any, params: dict[str, Any] | None = None) -> Any:
        node = MagicMock()
        node.get_next_control_output.return_value = next_control_output
        params = params or {}
        node.get_parameter_by_name.side_effect = params.get
        return node

    def test_returns_false_when_source_is_none(self) -> None:
        assert _make_executor()._check_control_source_fired(None, {}) is False

    def test_returns_false_when_source_node_not_in_mappings(self) -> None:
        result = _make_executor()._check_control_source_fired(("SrcOrig", "out"), {})
        assert result is False

    def test_returns_false_when_node_manager_raises_value_error(self) -> None:
        executor = _make_executor()
        mock_engine = cast("MagicMock", executor.engine)
        mock_engine.node_manager.get_node_by_name.side_effect = ValueError("not found")
        result = executor._check_control_source_fired(
            ("SrcOrig", "out"),
            {"SrcOrig": "Src_inst1"},
        )
        assert result is False

    def test_returns_false_when_node_manager_returns_none(self) -> None:
        executor = _make_executor()
        mock_engine = cast("MagicMock", executor.engine)
        mock_engine.node_manager.get_node_by_name.return_value = None
        result = executor._check_control_source_fired(
            ("SrcOrig", "out"),
            {"SrcOrig": "Src_inst1"},
        )
        assert result is False

    def test_returns_false_when_no_next_control_output(self) -> None:
        node = self._make_source_node(next_control_output=None)
        executor = _make_executor()
        mock_engine = cast("MagicMock", executor.engine)
        mock_engine.node_manager.get_node_by_name.return_value = node
        result = executor._check_control_source_fired(
            ("SrcOrig", "out"),
            {"SrcOrig": "Src_inst1"},
        )
        assert result is False

    def test_returns_true_when_next_control_output_matches_parameter(self) -> None:
        target_param = MagicMock()
        target_param.name = "out"
        node = self._make_source_node(
            next_control_output=target_param,
            params={"out": target_param},
        )
        executor = _make_executor()
        mock_engine = cast("MagicMock", executor.engine)
        mock_engine.node_manager.get_node_by_name.return_value = node
        result = executor._check_control_source_fired(
            ("SrcOrig", "out"),
            {"SrcOrig": "Src_inst1"},
        )
        assert result is True

    def test_returns_false_when_next_control_output_is_a_different_parameter(self) -> None:
        wrong_param = MagicMock(name="wrong")
        target_param = MagicMock(name="target")
        node = self._make_source_node(
            next_control_output=wrong_param,
            params={"out": target_param},
        )
        executor = _make_executor()
        mock_engine = cast("MagicMock", executor.engine)
        mock_engine.node_manager.get_node_by_name.return_value = node
        result = executor._check_control_source_fired(
            ("SrcOrig", "out"),
            {"SrcOrig": "Src_inst1"},
        )
        assert result is False


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

    _find_source_for_control_param and _check_control_source_fired are NOT mocked,
    so a regression in parameter-name matching will fail these tests.
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
        deserialized_nodes: dict[str, Any],
    ) -> IterationControlAction:
        """Run _get_iteration_control_action with only the engine mocked.

        _find_source_for_control_param and _check_control_source_fired execute
        against real logic so that parameter-name matching is exercised.
        """
        connections_result = self._make_connections_result(connections)
        executor = _make_executor()
        mock_engine = cast("MagicMock", executor.engine)

        def fake_get_node(name: str) -> Any:
            return deserialized_nodes.get(name)

        mock_engine.handle_request.return_value = connections_result
        mock_engine.node_manager.get_node_by_name.side_effect = fake_get_node
        return executor._get_iteration_control_action(end_node, node_name_mappings)

    def _make_deserialized_node_firing(self, param_name: str) -> MagicMock:
        """Return a mock deserialized node whose get_next_control_output fires the named param."""
        fired_param = MagicMock()
        fired_param.name = param_name
        node = MagicMock()
        node.get_next_control_output.return_value = fired_param
        node.get_parameter_by_name.side_effect = lambda n: fired_param if n == param_name else None
        return node

    def _make_deserialized_node_not_firing(self) -> MagicMock:
        """Return a mock deserialized node whose get_next_control_output returns None."""
        node = MagicMock()
        node.get_next_control_output.return_value = None
        return node

    def test_legacy_break_detected_via_real_name_matching(self) -> None:
        """BREAK is detected through real connection/name resolution for a legacy end node."""
        end_node = MagicMock(spec=BaseIterativeEndNode)
        end_node.name = "EndLoop"
        connections = [
            self._make_connection(target_param="break_loop", source_node="CondNode_orig", source_param="exec_out"),
        ]
        node_name_mappings = {"CondNode_orig": "CondNode_inst1"}
        deserialized_nodes = {
            "CondNode_inst1": self._make_deserialized_node_firing("exec_out"),
        }
        result = self._run_real(end_node, connections, node_name_mappings, deserialized_nodes)
        assert result == IterationControlAction.BREAK

    def test_legacy_skip_detected_via_real_name_matching(self) -> None:
        """SKIP is detected through real connection/name resolution for a legacy end node."""
        end_node = MagicMock(spec=BaseIterativeEndNode)
        end_node.name = "EndLoop"
        connections = [
            self._make_connection(target_param="skip_iteration", source_node="CondNode_orig", source_param="exec_out"),
        ]
        node_name_mappings = {"CondNode_orig": "CondNode_inst1"}
        deserialized_nodes = {
            "CondNode_inst1": self._make_deserialized_node_firing("exec_out"),
        }
        result = self._run_real(end_node, connections, node_name_mappings, deserialized_nodes)
        assert result == IterationControlAction.SKIP

    def test_wrong_parameter_name_does_not_trigger_break(self) -> None:
        """A connection targeting the wrong param name does NOT trigger BREAK."""
        end_node = MagicMock(spec=BaseIterativeEndNode)
        end_node.name = "EndLoop"
        connections = [
            # Misspelled control param — should NOT match break_loop
            self._make_connection(target_param="break_loooop", source_node="CondNode_orig", source_param="exec_out"),
        ]
        node_name_mappings = {"CondNode_orig": "CondNode_inst1"}
        deserialized_nodes = {
            "CondNode_inst1": self._make_deserialized_node_firing("exec_out"),
        }
        result = self._run_real(end_node, connections, node_name_mappings, deserialized_nodes)
        assert result == IterationControlAction.ADD

    def test_node_not_in_mappings_does_not_trigger_break(self) -> None:
        """If the source node is missing from node_name_mappings, no BREAK fires."""
        end_node = MagicMock(spec=BaseIterativeEndNode)
        end_node.name = "EndLoop"
        connections = [
            self._make_connection(target_param="break_loop", source_node="CondNode_orig", source_param="exec_out"),
        ]
        # CondNode_orig is NOT in node_name_mappings
        result = self._run_real(end_node, connections, {}, {})
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


class TestIterationFailureDetail:
    """A failed iteration is described by the nested failure's own words when it holds a Workflow node."""

    @staticmethod
    def _result(exception: Exception | None) -> StartLocalSubflowResultFailure:
        return StartLocalSubflowResultFailure(
            result_details="Node 'Inner Workflow' execution failed: a long wrapped text", exception=exception
        )

    def test_a_nested_workflow_failure_is_described_by_its_own_message(self) -> None:
        nested = NestedWorkflowNodeError(
            "Inner Workflow", root_details=NodeErrorDetails(message="boom"), node_path=["Failing"]
        )
        failure = SubflowNodeFailedError("Inner Workflow", NodeErrorDetails(message="wrapped"), nested)

        detail = NodeExecutor._iteration_failure_detail(self._result(failure))

        assert detail == str(nested)
        assert "long wrapped text" not in detail

    def test_any_other_failure_keeps_the_result_details(self) -> None:
        failure = SubflowNodeFailedError("Body", NodeErrorDetails(message="boom"), ValueError("boom"))

        detail = NodeExecutor._iteration_failure_detail(self._result(failure))

        assert detail == "Node 'Inner Workflow' execution failed: a long wrapped text"

    def test_a_failure_without_an_exception_keeps_the_result_details(self) -> None:
        detail = NodeExecutor._iteration_failure_detail(self._result(None))

        assert detail == "Node 'Inner Workflow' execution failed: a long wrapped text"

    def test_a_result_that_did_not_fail_keeps_its_details(self) -> None:
        result = StartLocalSubflowResultSuccess(result_details="ok")

        assert NodeExecutor._iteration_failure_detail(result) == "ok"


class TestIterationFailureException:
    def test_a_failed_result_gives_its_exception(self) -> None:
        raised = ValueError("boom")

        result = StartLocalSubflowResultFailure(result_details="failed", exception=raised)

        assert NodeExecutor._iteration_failure_exception(result, {}) is raised

    def test_a_result_that_did_not_fail_gives_none(self) -> None:
        result = StartLocalSubflowResultSuccess(result_details="ok")

        assert NodeExecutor._iteration_failure_exception(result, {}) is None

    def test_a_failing_node_is_named_as_the_artist_sees_it(self) -> None:
        """The iteration flow runs copies of the body nodes; the failure names the original."""
        details = NodeErrorDetails(message="boom")
        cause = ValueError("boom")
        raised = SubflowNodeFailedError("Inner Workflow_1", details, cause)
        result = StartLocalSubflowResultFailure(result_details="failed", exception=raised)

        failure = NodeExecutor._iteration_failure_exception(result, {"Inner Workflow_1": "Inner Workflow"})

        assert isinstance(failure, SubflowNodeFailedError)
        assert failure.node_name == "Inner Workflow"
        assert failure.details is details
        assert failure.cause is cause

    def test_a_failing_node_with_no_original_keeps_its_name(self) -> None:
        raised = SubflowNodeFailedError("Body", NodeErrorDetails(message="boom"), None)
        result = StartLocalSubflowResultFailure(result_details="failed", exception=raised)

        assert NodeExecutor._iteration_failure_exception(result, {"Other_1": "Other"}) is raised


class TestLoopFailureError:
    """The error a loop raises keeps its message and chains from the first iteration's failure."""

    def test_the_message_is_the_loop_failure_message(self) -> None:
        failures = _make_iteration_failures(["first", "second"])

        error = NodeExecutor._loop_failure_error(loop_name="Loop", total_iterations=2, iteration_failures=failures)

        assert str(error) == NodeExecutor._format_loop_failure_message("Loop", 2, failures)

    def test_it_chains_from_the_first_failure_that_has_an_exception(self) -> None:
        first = ValueError("first")
        second = ValueError("second")
        failures = [
            IterationFailure(iteration_index=0, detail="no exception"),
            IterationFailure(iteration_index=1, detail="first", exception=first),
            IterationFailure(iteration_index=2, detail="second", exception=second),
        ]

        error = NodeExecutor._loop_failure_error(loop_name="Loop", total_iterations=3, iteration_failures=failures)

        assert error.__cause__ is first

    def test_it_chains_from_a_refusal_over_an_earlier_ordinary_failure(self) -> None:
        refusal = _refusal()
        failures = [
            IterationFailure(iteration_index=0, detail="ordinary", exception=ValueError("ordinary")),
            IterationFailure(iteration_index=1, detail="refused", exception=refusal),
        ]

        error = NodeExecutor._loop_failure_error(loop_name="Loop", total_iterations=2, iteration_failures=failures)

        assert error.__cause__ is refusal

    def test_it_has_no_cause_when_no_iteration_kept_an_exception(self) -> None:
        error = NodeExecutor._loop_failure_error(
            loop_name="Loop", total_iterations=2, iteration_failures=_make_iteration_failures(["a", "b"])
        )

        assert error.__cause__ is None


class TestIterationsRunBeforeRefusal:
    """A sequential loop that stops on a refusal knows how many iterations it got through."""

    def test_counts_up_to_and_including_the_refused_iteration(self) -> None:
        failures = [IterationFailure(iteration_index=1, detail="refused", exception=_refusal())]

        assert NodeExecutor._iterations_run_before_refusal(failures) == 2  # noqa: PLR2004

    def test_is_none_when_the_last_failure_is_ordinary(self) -> None:
        failures = [IterationFailure(iteration_index=1, detail="ordinary", exception=ValueError("x"))]

        assert NodeExecutor._iterations_run_before_refusal(failures) is None

    def test_is_none_when_there_are_no_failures(self) -> None:
        assert NodeExecutor._iterations_run_before_refusal([]) is None


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

    def test_says_how_many_iterations_were_never_run(self) -> None:
        msg = NodeExecutor._format_loop_failure_message(
            loop_name="Loop",
            total_iterations=3,
            iteration_failures=_make_iteration_failures(["refused"]),
            iterations_run=1,
        )
        assert "Failed because 1 of them did not finish. The remaining 2 iterations were not run." in msg

    def test_says_it_in_the_singular_for_one_unrun_iteration(self) -> None:
        msg = NodeExecutor._format_loop_failure_message(
            loop_name="Loop",
            total_iterations=2,
            iteration_failures=_make_iteration_failures(["refused"]),
            iterations_run=1,
        )
        assert "The remaining iteration was not run." in msg

    def test_says_nothing_about_unrun_iterations_when_all_were_run(self) -> None:
        msg = NodeExecutor._format_loop_failure_message(
            loop_name="Loop",
            total_iterations=2,
            iteration_failures=_make_iteration_failures(["a", "b"]),
            iterations_run=2,
        )
        assert "not run" not in msg

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


def _refusal() -> SubflowNodeFailedError:
    nested = NestedWorkflowNodeError(
        "Inner Workflow",
        root_details=NodeErrorDetails(message="too deep"),
        node_path=[],
        root_cause=WorkflowRecursionLimitError("too deep"),
    )
    return SubflowNodeFailedError("Inner Workflow", NodeErrorDetails(message="wrapped"), nested)


class TestIterationRecursionRefusal:
    """An iteration that failed on a recursion refusal says so, however its failure was wrapped."""

    def test_a_failed_iteration_chained_to_a_refusal(self) -> None:
        failure = IterationFailure(iteration_index=0, detail="x", exception=_refusal())

        assert failure.is_recursion_refusal

    def test_an_ordinary_failed_iteration(self) -> None:
        failure = IterationFailure(iteration_index=0, detail="x", exception=ValueError("boom"))

        assert not failure.is_recursion_refusal

    def test_a_failed_iteration_with_no_exception(self) -> None:
        assert not IterationFailure(iteration_index=0, detail="x").is_recursion_refusal

    def test_a_parallel_outcome_carrying_a_refusal(self) -> None:
        outcome = IterationOutcome(iteration_index=1, succeeded=False, detail="x", exception=_refusal())

        assert outcome.is_recursion_refusal

    def test_a_successful_parallel_outcome(self) -> None:
        assert not IterationOutcome(iteration_index=1, succeeded=True, detail="").is_recursion_refusal


class TestRunIterationTasksUntilRefused:
    """Parallel iterations run to the end unless one is refused, which cancels the rest."""

    @pytest.mark.asyncio
    async def test_every_iteration_finishes_when_none_is_refused(self) -> None:
        async def finish(index: int) -> IterationOutcome:
            await asyncio.sleep(0)
            return IterationOutcome(iteration_index=index, succeeded=True, detail="")

        tasks = [asyncio.ensure_future(finish(index)) for index in range(3)]

        results = await NodeExecutor(engine=Mock(spec=Engine))._run_iteration_tasks_until_refused(tasks)

        assert [result.iteration_index for result in results if isinstance(result, IterationOutcome)] == [0, 1, 2]

    @pytest.mark.asyncio
    async def test_an_ordinary_failure_does_not_stop_the_others(self) -> None:
        async def fail() -> IterationOutcome:
            return IterationOutcome(iteration_index=0, succeeded=False, detail="x", exception=ValueError("boom"))

        async def finish_later() -> IterationOutcome:
            await asyncio.sleep(0.01)
            return IterationOutcome(iteration_index=1, succeeded=True, detail="")

        tasks = [asyncio.ensure_future(fail()), asyncio.ensure_future(finish_later())]

        results = await NodeExecutor(engine=Mock(spec=Engine))._run_iteration_tasks_until_refused(tasks)

        assert isinstance(results[1], IterationOutcome)
        assert results[1].succeeded

    @pytest.mark.asyncio
    async def test_a_refusal_cancels_the_iterations_still_running(self) -> None:
        async def refused() -> IterationOutcome:
            return IterationOutcome(iteration_index=0, succeeded=False, detail="x", exception=_refusal())

        async def run_forever() -> IterationOutcome:
            await asyncio.sleep(3600)
            return IterationOutcome(iteration_index=1, succeeded=True, detail="")

        tasks = [asyncio.ensure_future(refused()), asyncio.ensure_future(run_forever())]

        results = await NodeExecutor(engine=Mock(spec=Engine))._run_iteration_tasks_until_refused(tasks)

        assert isinstance(results[0], IterationOutcome)
        assert results[0].is_recursion_refusal
        assert isinstance(results[1], asyncio.CancelledError)
        assert tasks[1].cancelled()


class _StopAfterDeserializingError(RuntimeError):
    """Raised by a test to end a run once the iteration flows have been created."""


class TestIterationFlowOwner:
    """Each iteration flow is recorded as owned by the flow that holds the loop, at every site that makes one."""

    @pytest.fixture
    def executor(self) -> NodeExecutor:
        engine = Mock(spec=Engine)
        engine.node_manager = Mock(spec=NodeManager)
        engine.node_manager.get_node_parent_flow_by_name.return_value = "Holding_flow"
        engine.flow_manager = Mock(spec=FlowManager)
        engine.context_manager = Mock(spec=ContextManager)
        engine.context_manager.has_current_flow.return_value = False
        engine.event_manager = Mock(spec=EventManager)
        return NodeExecutor(engine=engine)

    @pytest.fixture
    def package_result(self) -> Mock:
        """A package result whose dataclass fields are set explicitly, since a class spec does not list them."""
        package_result = Mock(spec=PackageNodesAsSerializedFlowResultSuccess)
        package_result.serialized_flow_commands = Mock(spec=SerializedFlowCommands)
        return package_result

    @pytest.fixture(autouse=True)
    def quiet_package(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            NodeExecutor,
            "_silence_packaged_node_creation_broadcasts",
            Mock(spec=NodeExecutor._silence_packaged_node_creation_broadcasts),
        )

    def test_while_group_flow_is_owned_by_the_flow_holding_the_group(
        self, executor: NodeExecutor, package_result: Mock
    ) -> None:
        engine = cast("Mock", executor.engine)
        engine.handle_request.return_value = DeserializeFlowFromCommandsResultSuccess(
            result_details="ok", flow_name="While_subflow_1", node_name_mappings={"Start": "Start_1"}
        )

        executor._deserialize_while_flow(package_result, "Start", "While")

        engine.node_manager.get_node_parent_flow_by_name.assert_called_once_with("While")
        engine.flow_manager.record_iteration_flow_owner.assert_called_once_with("While_subflow_1", "Holding_flow")

    @pytest.mark.asyncio
    async def test_parallel_iteration_flows_are_owned_by_the_flow_holding_the_loop(
        self, executor: NodeExecutor, package_result: Mock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        engine = cast("Mock", executor.engine)
        engine.handle_request.return_value = DeserializeFlowFromCommandsResultSuccess(
            result_details="ok", flow_name="Iteration_flow", node_name_mappings={}
        )
        engine.ahandle_request = AsyncMock(spec=Engine.ahandle_request)
        monkeypatch.setattr(
            executor,
            "get_node_parameter_mappings",
            Mock(spec=executor.get_node_parameter_mappings, side_effect=_StopAfterDeserializingError),
        )
        monkeypatch.setattr(executor, "_delete_iteration_flows", AsyncMock(spec=executor._delete_iteration_flows))
        end_node = Mock(spec=BaseIterativeNodeGroup)
        end_node.name = "Loop"

        with pytest.raises(_StopAfterDeserializingError):
            await executor._execute_loop_iterations_locally(
                package_result=package_result,
                total_iterations=PARALLEL_ITERATIONS,
                parameter_values_per_iteration={},
                end_loop_node=end_node,
            )

        engine.node_manager.get_node_parent_flow_by_name.assert_called_with("Loop")
        engine.flow_manager.record_iteration_flow_owner.assert_called_with("Iteration_flow", "Holding_flow")
        assert engine.flow_manager.record_iteration_flow_owner.call_count == PARALLEL_ITERATIONS


class TestExecuteLoopIterationsLocallyFailures:
    """An all-at-once loop raises when an iteration was refused for nesting too deeply, and carries on otherwise."""

    @pytest.fixture
    def engine(self) -> Mock:
        engine = Mock(spec=Engine)
        engine.node_manager = Mock(spec=NodeManager)
        engine.node_manager.get_node_parent_flow_by_name.return_value = "Holding_flow"
        engine.flow_manager = Mock(spec=FlowManager)
        engine.context_manager = Mock(spec=ContextManager)
        engine.context_manager.has_current_flow.return_value = False
        engine.event_manager = Mock(spec=EventManager)
        engine.handle_request.return_value = DeserializeFlowFromCommandsResultSuccess(
            result_details="ok", flow_name="Iteration_flow", node_name_mappings={"Start": "Start_1"}
        )
        engine.ahandle_request = AsyncMock(spec=Engine.ahandle_request)
        return engine

    @pytest.fixture
    def executor(self, engine: Mock, monkeypatch: pytest.MonkeyPatch) -> NodeExecutor:
        executor = NodeExecutor(engine=engine)
        start_mapping = PackagedNodeParameterMapping(node_name="Start", parameter_mappings={})
        monkeypatch.setattr(
            executor,
            "get_node_parameter_mappings",
            Mock(spec=executor.get_node_parameter_mappings, return_value=start_mapping),
        )
        monkeypatch.setattr(executor, "_delete_iteration_flows", AsyncMock(spec=executor._delete_iteration_flows))
        monkeypatch.setattr(
            executor,
            "get_parameter_values_from_iterations",
            Mock(spec=executor.get_parameter_values_from_iterations, return_value={}),
        )
        monkeypatch.setattr(
            executor,
            "get_last_iteration_values_for_packaged_nodes",
            Mock(spec=executor.get_last_iteration_values_for_packaged_nodes, return_value={}),
        )
        monkeypatch.setattr(
            NodeExecutor,
            "_silence_packaged_node_creation_broadcasts",
            Mock(spec=NodeExecutor._silence_packaged_node_creation_broadcasts),
        )
        return executor

    @pytest.fixture
    def package_result(self) -> Mock:
        package_result = Mock(spec=PackageNodesAsSerializedFlowResultSuccess)
        package_result.serialized_flow_commands = Mock(spec=SerializedFlowCommands)
        return package_result

    async def _run(self, executor: NodeExecutor, package_result: Mock) -> Any:
        end_node = Mock(spec=BaseIterativeNodeGroup)
        end_node.name = "Loop"
        return await executor._execute_loop_iterations_locally(
            package_result=package_result,
            total_iterations=PARALLEL_ITERATIONS,
            parameter_values_per_iteration={0: {}, 1: {}},
            end_loop_node=end_node,
        )

    @pytest.mark.asyncio
    async def test_a_refused_iteration_raises_an_error_chained_to_the_refusal(
        self, executor: NodeExecutor, engine: Mock, package_result: Mock
    ) -> None:
        refusal = _refusal()
        engine.ahandle_request.return_value = StartLocalSubflowResultFailure(result_details="failed", exception=refusal)

        with pytest.raises(RuntimeError, match="Attempted to run all 2 iterations of loop 'Loop'") as raised:
            await self._run(executor, package_result)

        assert raised.value.__cause__ is refusal
        cast("AsyncMock", executor._delete_iteration_flows).assert_awaited_once()

    @pytest.mark.parametrize("refused_iteration", [0, 1])
    @pytest.mark.asyncio
    async def test_a_refused_iteration_names_the_original_node_not_its_own_iteration_copy(
        self, executor: NodeExecutor, engine: Mock, package_result: Mock, refused_iteration: int
    ) -> None:
        engine.handle_request.side_effect = [
            DeserializeFlowFromCommandsResultSuccess(
                result_details="ok", flow_name="Iteration_flow", node_name_mappings={"Start": "Start_1"}
            ),
            DeserializeFlowFromCommandsResultSuccess(
                result_details="ok", flow_name="Iteration_flow", node_name_mappings={"Start": "Start_2"}
            ),
        ]
        refusal = _refusal()
        copy_failure = SubflowNodeFailedError(
            f"Start_{refused_iteration + 1}", NodeErrorDetails(message="wrapped"), refusal.cause
        )
        outcomes: list[Any] = [
            StartLocalSubflowResultSuccess(result_details="ok"),
            StartLocalSubflowResultSuccess(result_details="ok"),
        ]
        outcomes[refused_iteration] = StartLocalSubflowResultFailure(result_details="failed", exception=copy_failure)
        engine.ahandle_request.side_effect = outcomes

        with pytest.raises(RuntimeError, match="Attempted to run all 2 iterations of loop 'Loop'") as raised:
            await self._run(executor, package_result)

        cause = raised.value.__cause__
        assert isinstance(cause, SubflowNodeFailedError)
        assert cause.node_name == "Start"

    @pytest.mark.asyncio
    async def test_an_ordinary_failure_still_returns_partial_results(
        self, executor: NodeExecutor, engine: Mock, package_result: Mock
    ) -> None:
        engine.ahandle_request.return_value = StartLocalSubflowResultFailure(
            result_details="failed", exception=ValueError("boom")
        )

        _results, successful_iterations, _last_values = await self._run(executor, package_result)

        assert successful_iterations == []
        cast("AsyncMock", executor._delete_iteration_flows).assert_awaited_once()


class TestHandleSequentialIterativeGroupExecutionRefusal:
    """A sequential group stopped by a refusal says how many iterations were never run."""

    @pytest.mark.asyncio
    async def test_the_error_reports_the_iterations_that_were_not_run(self, monkeypatch: pytest.MonkeyPatch) -> None:
        executor = NodeExecutor(engine=Mock(spec=Engine))
        monkeypatch.setattr(
            executor,
            "_package_subflow_group_body",
            AsyncMock(
                spec=executor._package_subflow_group_body,
                return_value=Mock(spec=PackageNodesAsSerializedFlowResultSuccess),
            ),
        )
        monkeypatch.setattr(
            executor,
            "_get_merged_parameter_values_for_iterative_group",
            Mock(spec=executor._get_merged_parameter_values_for_iterative_group, return_value={}),
        )
        refused = IterationFailure(iteration_index=0, detail="too deep", exception=_refusal())
        monkeypatch.setattr(
            executor,
            "_execute_loop_iterations_sequentially",
            AsyncMock(
                spec=executor._execute_loop_iterations_sequentially,
                return_value=({}, [], {}, 0, False, [refused]),
            ),
        )
        group = Mock(spec=BaseIterativeNodeGroup)
        group.name = "Group"
        group._get_total_iterations.return_value = 3

        with pytest.raises(RuntimeError, match="The remaining 2 iterations were not run"):
            await executor._handle_sequential_iterative_group_execution(group, LOCAL_EXECUTION)


class TestHandleSequentialLoopExecutionRefusal:
    """A sequential loop stopped by a refusal says how many iterations were never run."""

    @pytest.mark.asyncio
    async def test_the_error_reports_the_iterations_that_were_not_run(self, monkeypatch: pytest.MonkeyPatch) -> None:
        executor = NodeExecutor(engine=Mock(spec=Engine))
        package_result = Mock(spec=PackageNodesAsSerializedFlowResultSuccess)
        package_result.packaged_node_names = []
        monkeypatch.setattr(
            executor,
            "_package_loop_body",
            AsyncMock(spec=executor._package_loop_body, return_value=(package_result, LOCAL_EXECUTION)),
        )
        monkeypatch.setattr(
            executor,
            "get_parameter_values_per_iteration",
            Mock(spec=executor.get_parameter_values_per_iteration, return_value={}),
        )
        monkeypatch.setattr(
            executor,
            "get_resolved_upstream_values",
            Mock(spec=executor.get_resolved_upstream_values, return_value={}),
        )
        refused = IterationFailure(iteration_index=0, detail="too deep", exception=_refusal())
        monkeypatch.setattr(
            executor,
            "_execute_loop_iterations_sequentially",
            AsyncMock(
                spec=executor._execute_loop_iterations_sequentially,
                return_value=({}, [], {}, 0, False, [refused]),
            ),
        )
        start_node = Mock(spec=BaseIterativeStartNode)
        start_node.name = "Start"
        start_node._get_total_iterations.return_value = 3
        start_node._progress_bar = Mock(spec=ProgressBarComponent)
        end_node = Mock(spec=BaseIterativeEndNode)
        end_node.name = "End"

        with pytest.raises(RuntimeError, match="The remaining 2 iterations were not run"):
            await executor._handle_sequential_loop_execution(start_node, end_node)


class TestRunWhileLoopIterations:
    """A while group keeps retrying a failed iteration, except one refused for nesting too deeply."""

    @pytest.fixture
    def executor(self, monkeypatch: pytest.MonkeyPatch) -> NodeExecutor:
        engine = Mock(spec=Engine)
        engine.ahandle_request = AsyncMock(spec=Engine.ahandle_request)
        executor = NodeExecutor(engine=engine)
        monkeypatch.setattr(
            executor, "_set_while_iteration_parameters", AsyncMock(spec=executor._set_while_iteration_parameters)
        )
        return executor

    async def _run(self, executor: NodeExecutor) -> bool:
        node = Mock(spec=BaseWhileNodeGroup)
        node.name = "While"
        return await executor._run_while_loop_iterations(
            node=node,
            flow_name="flow",
            node_name_mappings={},
            packaged_start_node_name="Start_1",
            resolved_upstream_values={},
            iteration_startflow_params=[],
            reverse_node_mapping={},
            event_manager=Mock(spec=EventManager),
            max_iterations=WHILE_TOTAL_ITERATIONS - 1,
            total_iterations=WHILE_TOTAL_ITERATIONS,
        )

    @pytest.mark.asyncio
    async def test_a_refused_iteration_ends_the_loop_with_an_error(self, executor: NodeExecutor) -> None:
        engine = cast("Mock", executor.engine)
        engine.ahandle_request.return_value = StartLocalSubflowResultFailure(
            result_details="failed", exception=_refusal()
        )

        with pytest.raises(RuntimeError, match="iteration 1 of while group 'While'"):
            await self._run(executor)

        assert engine.ahandle_request.await_count == 1

    @pytest.mark.asyncio
    async def test_an_ordinary_failure_is_retried_until_iterations_run_out(
        self, executor: NodeExecutor, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        engine = cast("Mock", executor.engine)
        engine.ahandle_request.return_value = StartLocalSubflowResultFailure(
            result_details="failed", exception=ValueError("boom")
        )
        monkeypatch.setattr(
            executor,
            "_evaluate_while_iteration_result",
            Mock(spec=executor._evaluate_while_iteration_result, side_effect=[None, None, None, None, False]),
        )

        assert await self._run(executor) is False

        assert engine.ahandle_request.await_count == WHILE_TOTAL_ITERATIONS
