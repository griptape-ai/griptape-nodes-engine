from unittest.mock import Mock, patch

import pytest

from griptape_nodes.exe_types.core_types import Parameter
from griptape_nodes.exe_types.node_types import AsyncResult, SuccessFailureNode, TrackedParameterOutputValues

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


class TestErrorProxyNode:
    """The placeholder substituted for a node that could not be created."""

    @staticmethod
    def _message(node):  # noqa: ANN001, ANN205
        from griptape_nodes.exe_types.core_types import ParameterMessage

        message = node.get_message_by_name_or_element_id("error_proxy_message")
        assert isinstance(message, ParameterMessage)
        return message

    def test_load_failure_reads_as_error(self) -> None:
        """A missing dependency / load failure keeps the hard-error treatment."""
        from griptape_nodes.exe_types.node_types import ErrorProxyNode

        node = ErrorProxyNode(
            name="proxy",
            original_node_type="FancyNode",
            original_library_name="fancy-lib",
            failure_reason="No module named 'fancy'",
        )

        message = self._message(node)
        assert node.denied_by_policy is False
        assert message.variant == "error"
        assert message.markdown is False
        assert "could not be loaded" in message.value

    def test_policy_denial_reads_as_warning(self) -> None:
        """A policy denial is recoverable, so it reads as a warning that surfaces the hook's reason."""
        from griptape_nodes.exe_types.node_types import ErrorProxyNode

        node = ErrorProxyNode(
            name="proxy",
            original_node_type="FancyNode",
            original_library_name="fancy-lib",
            failure_reason="Ask your admin to enable Labs nodes.",
            denied_by_policy=True,
        )

        message = self._message(node)
        assert node.denied_by_policy is True
        assert message.variant == "warning"
        assert message.markdown is True
        assert "**Permission denied**" in message.value
        assert "Ask your admin to enable Labs nodes." in message.value


class TestLockedSuccessFailureNodeRouting:
    """A locked SuccessFailureNode must route down Succeeded, not Failed or nowhere.

    A locked node never executes, so ``_execution_succeeded`` is never written for the current
    run. It is only assigned by ``_set_status_results`` and reset by ``_clear_execution_status``,
    both reached through ``process()``. So the attribute holds whatever the *previous* run left:
    ``None`` if the node never ran (which used to set ``stop_flow`` and dead-end the branch), or a
    stale ``False`` if the node failed before being locked (which used to route down Failed).
    """

    @staticmethod
    def _locked_node() -> SuccessFailureNode:
        node = SuccessFailureNode(name="locked_branch")
        node.lock = True
        return node

    def test_locked_node_that_never_ran_follows_success_path(self) -> None:
        """``_execution_succeeded is None`` must not set stop_flow when the node is locked."""
        node = self._locked_node()
        assert node._execution_succeeded is None

        assert node.get_next_control_output() is node.control_parameter_out
        assert node.stop_flow is False

    def test_locked_node_with_stale_failure_follows_success_path(self) -> None:
        """A stale ``False`` from a run before the lock must not route down Failed."""
        node = self._locked_node()
        node._execution_succeeded = False

        assert node.get_next_control_output() is node.control_parameter_out

    def test_unlocked_node_still_routes_on_its_result(self) -> None:
        """Unlocked nodes keep their normal success/failure/not-yet-run routing."""
        node = SuccessFailureNode(name="unlocked_branch")

        node._execution_succeeded = False
        assert node.get_next_control_output() is node.failure_output

        node._execution_succeeded = True
        assert node.get_next_control_output() is node.control_parameter_out

        node._execution_succeeded = None
        assert node.get_next_control_output() is None
        assert node.stop_flow is True
