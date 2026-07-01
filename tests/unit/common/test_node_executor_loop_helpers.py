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
"""

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from griptape_nodes.common.node_executor import IterationControlAction, NodeExecutor
from griptape_nodes.exe_types.base_iterative_nodes import BaseIterativeEndNode
from griptape_nodes.exe_types.node_groups.base_iterative_node_group import BaseIterativeNodeGroup
from griptape_nodes.retained_mode.events.node_events import ListConnectionsForNodeResultSuccess

_GRIPTAPE_NODES_PATH = "griptape_nodes.common.node_executor.GriptapeNodes"


def _make_executor() -> NodeExecutor:
    return NodeExecutor.__new__(NodeExecutor)


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
        with patch(_GRIPTAPE_NODES_PATH) as mock_gn:
            mock_gn.handle_request.return_value = connections_result
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
        # Return a non-success result from handle_request
        with patch(_GRIPTAPE_NODES_PATH) as mock_gn:
            mock_gn.handle_request.return_value = MagicMock(spec=object)  # not ListConnectionsForNodeResultSuccess
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
        with patch(_GRIPTAPE_NODES_PATH):
            assert _make_executor()._check_control_source_fired(None, {}) is False

    def test_returns_false_when_source_node_not_in_mappings(self) -> None:
        with patch(_GRIPTAPE_NODES_PATH):
            result = _make_executor()._check_control_source_fired(("SrcOrig", "out"), {})
        assert result is False

    def test_returns_false_when_node_manager_raises_value_error(self) -> None:
        with patch(_GRIPTAPE_NODES_PATH) as mock_gn:
            mock_gn.NodeManager.return_value.get_node_by_name.side_effect = ValueError("not found")
            result = _make_executor()._check_control_source_fired(
                ("SrcOrig", "out"),
                {"SrcOrig": "Src_inst1"},
            )
        assert result is False

    def test_returns_false_when_node_manager_returns_none(self) -> None:
        with patch(_GRIPTAPE_NODES_PATH) as mock_gn:
            mock_gn.NodeManager.return_value.get_node_by_name.return_value = None
            result = _make_executor()._check_control_source_fired(
                ("SrcOrig", "out"),
                {"SrcOrig": "Src_inst1"},
            )
        assert result is False

    def test_returns_false_when_no_next_control_output(self) -> None:
        node = self._make_source_node(next_control_output=None)
        with patch(_GRIPTAPE_NODES_PATH) as mock_gn:
            mock_gn.NodeManager.return_value.get_node_by_name.return_value = node
            result = _make_executor()._check_control_source_fired(
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
        with patch(_GRIPTAPE_NODES_PATH) as mock_gn:
            mock_gn.NodeManager.return_value.get_node_by_name.return_value = node
            result = _make_executor()._check_control_source_fired(
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
        with patch(_GRIPTAPE_NODES_PATH) as mock_gn:
            mock_gn.NodeManager.return_value.get_node_by_name.return_value = node
            result = _make_executor()._check_control_source_fired(
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
