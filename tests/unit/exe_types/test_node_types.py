from unittest.mock import Mock, patch

import pytest

from griptape_nodes.exe_types.core_types import Parameter
from griptape_nodes.exe_types.node_types import AsyncResult, TrackedParameterOutputValues

from .mocks import MockNode


class TestNodeTypes:
    """Test suite for node types functionality."""

    @pytest.mark.asyncio
    async def test_aprocess_with_multiple_yields(self) -> None:
        """Test that aprocess correctly handles nodes with multiple yields."""
        results = []

        def callable1() -> str:
            return "result1"

        def callable2() -> str:
            return "result2"

        def generator() -> AsyncResult:
            result1 = yield callable1
            results.append(result1)

            result2 = yield callable2
            results.append(result2)

        node = MockNode(process_result=generator())

        # Should complete without error
        await node.aprocess()

        # Verify all yields were processed
        assert results == ["result1", "result2"]


class TestConnectionRemovedHooks:
    def _make_param(self, name: str) -> Parameter:
        return Parameter(name=name, input_types=["str"], type="str", output_type="str", tooltip="test")

    def test_after_incoming_connection_removed_calls_callbacks(self) -> None:
        source_node = MockNode(name="source_node")
        target_node = MockNode(name="target_node")
        source_param = self._make_param("source_param")
        target_param = self._make_param("target_param")

        callback = Mock()
        target_param.on_incoming_connection_removed.append(callback)

        target_node.after_incoming_connection_removed(source_node, source_param, target_param)

        callback.assert_called_once_with(target_param, "source_node", "source_param")

    def test_after_incoming_connection_removed_calls_multiple_callbacks(self) -> None:
        source_node = MockNode(name="source_node")
        target_node = MockNode(name="target_node")
        source_param = self._make_param("source_param")
        target_param = self._make_param("target_param")

        callback1 = Mock()
        callback2 = Mock()
        target_param.on_incoming_connection_removed.append(callback1)
        target_param.on_incoming_connection_removed.append(callback2)

        target_node.after_incoming_connection_removed(source_node, source_param, target_param)

        callback1.assert_called_once_with(target_param, "source_node", "source_param")
        callback2.assert_called_once_with(target_param, "source_node", "source_param")

    def test_after_incoming_connection_removed_no_callbacks(self) -> None:
        source_node = MockNode(name="source_node")
        target_node = MockNode(name="target_node")
        source_param = self._make_param("source_param")
        target_param = self._make_param("target_param")

        # Should not raise when no callbacks are registered
        target_node.after_incoming_connection_removed(source_node, source_param, target_param)

    def test_after_outgoing_connection_removed_calls_callbacks(self) -> None:
        source_node = MockNode(name="source_node")
        target_node = MockNode(name="target_node")
        source_param = self._make_param("source_param")
        target_param = self._make_param("target_param")

        callback = Mock()
        source_param.on_outgoing_connection_removed.append(callback)

        source_node.after_outgoing_connection_removed(source_param, target_node, target_param)

        callback.assert_called_once_with(source_param, "target_node", "target_param")

    def test_after_outgoing_connection_removed_calls_multiple_callbacks(self) -> None:
        source_node = MockNode(name="source_node")
        target_node = MockNode(name="target_node")
        source_param = self._make_param("source_param")
        target_param = self._make_param("target_param")

        callback1 = Mock()
        callback2 = Mock()
        source_param.on_outgoing_connection_removed.append(callback1)
        source_param.on_outgoing_connection_removed.append(callback2)

        source_node.after_outgoing_connection_removed(source_param, target_node, target_param)

        callback1.assert_called_once_with(source_param, "target_node", "target_param")
        callback2.assert_called_once_with(source_param, "target_node", "target_param")

    def test_after_outgoing_connection_removed_no_callbacks(self) -> None:
        source_node = MockNode(name="source_node")
        target_node = MockNode(name="target_node")
        source_param = self._make_param("source_param")
        target_param = self._make_param("target_param")

        # Should not raise when no callbacks are registered
        source_node.after_outgoing_connection_removed(source_param, target_node, target_param)


class TestTrackedParameterOutputValuesSetItem:
    """__setitem__ emits a change event whenever the stored value changes.

    This includes the unset -> None transition that the old `old_value != value`
    guard silently dropped (self.get(key) returns None for both absent and
    present-as-None).
    """

    def _make_tracked(self) -> TrackedParameterOutputValues:
        return TrackedParameterOutputValues(MockNode(name="mock_node"))

    def test_emits_on_unset_to_none(self) -> None:
        """Setting an absent key to None must emit -- this is the regression."""
        tracked = self._make_tracked()

        with patch.object(TrackedParameterOutputValues, "_emit_parameter_change_event") as mock_emit:
            tracked["out"] = None

        mock_emit.assert_called_once_with("out", None)
        assert tracked["out"] is None

    def test_emits_on_value_to_none(self) -> None:
        """Setting an existing real value to None must still emit."""
        tracked = self._make_tracked()
        tracked["out"] = 42

        with patch.object(TrackedParameterOutputValues, "_emit_parameter_change_event") as mock_emit:
            tracked["out"] = None

        mock_emit.assert_called_once_with("out", None)

    def test_emits_on_fresh_non_none_value(self) -> None:
        """A first-time assignment of a non-None value emits."""
        tracked = self._make_tracked()

        with patch.object(TrackedParameterOutputValues, "_emit_parameter_change_event") as mock_emit:
            tracked["out"] = 42

        mock_emit.assert_called_once_with("out", 42)

    def test_no_emit_on_unchanged_value(self) -> None:
        """Re-setting a key to its current value is idempotent -- no emit."""
        tracked = self._make_tracked()
        tracked["out"] = 42

        with patch.object(TrackedParameterOutputValues, "_emit_parameter_change_event") as mock_emit:
            tracked["out"] = 42

        mock_emit.assert_not_called()

    def test_no_emit_on_none_to_none(self) -> None:
        """Once a key is present as None, re-setting it to None does not emit."""
        tracked = self._make_tracked()
        tracked["out"] = None

        with patch.object(TrackedParameterOutputValues, "_emit_parameter_change_event") as mock_emit:
            tracked["out"] = None

        mock_emit.assert_not_called()
