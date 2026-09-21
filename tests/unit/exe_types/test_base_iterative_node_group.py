"""Unit tests for control-flow ports on BaseIterativeNodeGroup."""

from __future__ import annotations

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

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.engine import Engine


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
