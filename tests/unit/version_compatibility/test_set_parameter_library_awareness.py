"""Tests for library-aware SetParameterVersionCompatibilityCheck implementations.

Verifies that compatibility checks only apply to nodes from the standard library
and do not interfere with identically-named nodes from extension libraries.
"""

from unittest.mock import MagicMock

import pytest

from griptape_nodes.retained_mode.events.parameter_events import SetParameterValueResultSuccess
from griptape_nodes.version_compatibility.versions.v0_65_4.run_in_parallel_to_run_in_order import (
    RunInParallelToRunInOrderCheck,
)
from griptape_nodes.version_compatibility.versions.v0_65_5.flux_2_removed_parameters import (
    Flux2RemovedParametersCheck,
)

STANDARD_LIBRARY = "griptape_nodes_library"
EXTENSION_LIBRARY = "blackforestlabs_library"


def _make_node(class_name: str, library_name: str) -> MagicMock:
    """Create a mock node with the given class name and library metadata."""
    node = MagicMock()
    node.metadata = {"library": library_name}
    type(node).__name__ = class_name
    return node


class TestFlux2RemovedParametersCheckLibraryAwareness:
    """Tests that Flux2RemovedParametersCheck only applies to standard library nodes."""

    @pytest.fixture
    def check(self) -> Flux2RemovedParametersCheck:
        # The check resolves the current workflow name through its injected engine.
        engine = MagicMock()
        engine.context_manager.get_current_workflow_name.return_value = "test_workflow"
        return Flux2RemovedParametersCheck(engine=engine)

    def test_applies_to_standard_library_node(self, check: Flux2RemovedParametersCheck) -> None:
        node = _make_node("Flux2ImageGeneration", STANDARD_LIBRARY)

        assert check.applies_to_set_parameter(node, "aspect_ratio", "16:9") is True

    def test_does_not_apply_to_extension_library_node(self, check: Flux2RemovedParametersCheck) -> None:
        node = _make_node("Flux2ImageGeneration", EXTENSION_LIBRARY)

        assert check.applies_to_set_parameter(node, "aspect_ratio", "16:9") is False

    def test_does_not_apply_to_node_without_library_metadata(self, check: Flux2RemovedParametersCheck) -> None:
        node = MagicMock()
        node.metadata = {}
        type(node).__name__ = "Flux2ImageGeneration"

        assert check.applies_to_set_parameter(node, "aspect_ratio", "16:9") is False

    def test_does_not_apply_to_unrelated_parameter(self, check: Flux2RemovedParametersCheck) -> None:
        node = _make_node("Flux2ImageGeneration", STANDARD_LIBRARY)

        assert check.applies_to_set_parameter(node, "width", 1024) is False

    def test_does_not_apply_to_different_node_type(self, check: Flux2RemovedParametersCheck) -> None:
        node = _make_node("SomeOtherNode", STANDARD_LIBRARY)

        assert check.applies_to_set_parameter(node, "aspect_ratio", "16:9") is False

    def test_set_parameter_returns_success(self, check: Flux2RemovedParametersCheck) -> None:
        node = _make_node("Flux2ImageGeneration", STANDARD_LIBRARY)

        result = check.set_parameter_value(node, "aspect_ratio", "16:9")

        assert isinstance(result, SetParameterValueResultSuccess)
        assert result.finalized_value is None


class TestRunInParallelToRunInOrderCheckLibraryAwareness:
    """Tests that RunInParallelToRunInOrderCheck only applies to standard library nodes."""

    @pytest.fixture
    def check(self) -> RunInParallelToRunInOrderCheck:
        return RunInParallelToRunInOrderCheck()

    def test_applies_to_standard_library_for_loop_node(self, check: RunInParallelToRunInOrderCheck) -> None:
        node = _make_node("ForLoopStartNode", STANDARD_LIBRARY)

        assert check.applies_to_set_parameter(node, "run_in_parallel", True) is True

    def test_applies_to_standard_library_for_each_node(self, check: RunInParallelToRunInOrderCheck) -> None:
        node = _make_node("ForEachStartNode", STANDARD_LIBRARY)

        assert check.applies_to_set_parameter(node, "run_in_parallel", True) is True

    def test_does_not_apply_to_extension_library_node(self, check: RunInParallelToRunInOrderCheck) -> None:
        node = _make_node("ForLoopStartNode", EXTENSION_LIBRARY)

        assert check.applies_to_set_parameter(node, "run_in_parallel", True) is False

    def test_does_not_apply_to_node_without_library_metadata(self, check: RunInParallelToRunInOrderCheck) -> None:
        node = MagicMock()
        node.metadata = {}
        type(node).__name__ = "ForLoopStartNode"

        assert check.applies_to_set_parameter(node, "run_in_parallel", True) is False

    def test_does_not_apply_to_unrelated_parameter(self, check: RunInParallelToRunInOrderCheck) -> None:
        node = _make_node("ForLoopStartNode", STANDARD_LIBRARY)

        assert check.applies_to_set_parameter(node, "some_other_param", True) is False

    def test_does_not_apply_to_different_node_type(self, check: RunInParallelToRunInOrderCheck) -> None:
        node = _make_node("SomeOtherNode", STANDARD_LIBRARY)

        assert check.applies_to_set_parameter(node, "run_in_parallel", True) is False
