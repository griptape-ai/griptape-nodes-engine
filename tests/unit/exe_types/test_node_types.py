from unittest.mock import Mock, patch

import pytest

from griptape_nodes.exe_types.core_types import Parameter, ParameterList, ParameterMode
from griptape_nodes.exe_types.node_types import (
    AsyncResult,
    SuccessFailureNode,
    TrackedParameterOutputValues,
    aprocess_scope,
)
from griptape_nodes.traits.slider import Slider

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


class TestSetParameterValueStore:
    """Where `set_parameter_value` stores a value, and why the window is what decides.

    While a node's own body runs it is computing rather than being authored, so a value it sets on a
    Parameter that has an OUTPUT is what the run produced. `parameter_output_values` is where that
    belongs, and it is the only one of the two stores that travels back from a library's isolated
    process. A Parameter with no OUTPUT has no port to publish on, so a run's write to it is scratch.

    Outside that window the same set stores an authored value, whatever the modes, because
    `parameter_output_values` is cleared before every run and by `clear_node`, and a value set at edit
    time or replayed from a save has to outlive both.
    """

    def _node_with(self, param_name: str, modes: set[ParameterMode]) -> MockNode:
        node = MockNode(name="node")
        node.add_parameter(Parameter(name=param_name, type="str", tooltip="", allowed_modes=modes))
        return node

    def test_output_only_is_produced_while_the_node_runs(self) -> None:
        node = self._node_with("out", {ParameterMode.OUTPUT})

        with aprocess_scope(node=node):
            node.set_parameter_value("out", "done")

        assert node.parameter_output_values["out"] == "done"
        assert "out" not in node.parameter_values

    def test_output_only_reads_back_after_the_run(self) -> None:
        """The read is not confined to the window: the value is still what the Parameter holds."""
        node = self._node_with("out", {ParameterMode.OUTPUT})

        with aprocess_scope(node=node):
            node.set_parameter_value("out", "done")

        assert node.get_parameter_value("out") == "done"

    def test_output_only_set_at_edit_time_is_authored(self) -> None:
        """The pre-run clear would wipe a produced value, so an edit-time set must not go there."""
        node = self._node_with("out", {ParameterMode.OUTPUT})

        node.set_parameter_value("out", "set before any run")

        assert node.parameter_values["out"] == "set before any run"
        assert "out" not in node.parameter_output_values
        node.parameter_output_values.silent_clear()
        assert node.get_parameter_value("out") == "set before any run"

    def test_a_produced_value_wins_over_an_authored_one(self) -> None:
        node = self._node_with("out", {ParameterMode.OUTPUT})
        node.set_parameter_value("out", "set before any run")

        with aprocess_scope(node=node):
            node.set_parameter_value("out", "produced by the run")

        assert node.get_parameter_value("out") == "produced by the run"

    def test_a_container_child_keeps_the_container_whole(self) -> None:
        """The container is rebuilt from its children read raw, so a child must stay authored.

        Split Video in the standard library grows an output-only `ParameterList` this way, adding a
        child per clip and setting it while the node runs.
        """
        node = MockNode(name="node")
        images = ParameterList(name="images", type="str", tooltip="", allowed_modes={ParameterMode.OUTPUT})
        node.add_parameter(images)
        child = images.add_child_parameter()

        with aprocess_scope(node=node):
            node.set_parameter_value(child.name, "img0")

        assert node.get_parameter_value("images") == ["img0"]
        # The rebuilt container is what a worker ships back, so it is the produced value.
        assert node.parameter_output_values["images"] == ["img0"]

    def test_property_and_output_is_produced_while_the_node_runs(self) -> None:
        """A Parameter kept on display still publishes, so what a run puts there is a result."""
        node = self._node_with("both", {ParameterMode.PROPERTY, ParameterMode.OUTPUT})

        with aprocess_scope(node=node):
            node.set_parameter_value("both", "computed")

        assert node.parameter_output_values["both"] == "computed"
        assert "both" not in node.parameter_values

    def test_the_default_modes_are_produced_while_the_node_runs(self) -> None:
        """Declaring no modes at all allows OUTPUT, and that is most of the parameters in a library."""
        node = MockNode(name="node")
        node.add_parameter(Parameter(name="out", type="str", tooltip=""))

        with aprocess_scope(node=node):
            node.set_parameter_value("out", "computed")

        assert node.parameter_output_values["out"] == "computed"

    def test_property_and_output_set_at_edit_time_is_still_authored(self) -> None:
        """The window is what decides, so the editor's own write is unaffected by the above."""
        node = self._node_with("both", {ParameterMode.PROPERTY, ParameterMode.OUTPUT})

        node.set_parameter_value("both", "typed")

        assert node.parameter_values["both"] == "typed"
        assert "both" not in node.parameter_output_values

    def test_a_set_on_another_node_is_authored_there(self) -> None:
        """A running node sets values on other nodes, and on those it is an ordinary authored set.

        This is how a value reaches a connected input, and how a node driving a subflow feeds it. The
        receiving node is not running, so the value has to survive the clear before it does run.
        """
        node = self._node_with("out", {ParameterMode.OUTPUT})
        downstream = self._node_with("out", {ParameterMode.OUTPUT})

        with aprocess_scope(node=node):
            downstream.set_parameter_value("out", "handed over")

        assert downstream.parameter_values["out"] == "handed over"
        assert "out" not in downstream.parameter_output_values

    def test_a_parameter_with_no_output_stays_authored(self) -> None:
        """With no OUTPUT there is no port to publish on, so a run's write is scratch."""
        node = self._node_with("incoming", {ParameterMode.INPUT})
        node.add_parameter(Parameter(name="knob", type="str", tooltip="", allowed_modes={ParameterMode.PROPERTY}))

        with aprocess_scope(node=node):
            node.set_parameter_value("incoming", "delivered")
            node.set_parameter_value("knob", "scratch")

        assert node.parameter_values["incoming"] == "delivered"
        assert node.parameter_values["knob"] == "scratch"
        assert node.parameter_output_values == {}


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


class TestOutputValueChangeDetection:
    """`__setitem__` decides whether to emit by comparing old and new values.

    A node can hold an array-like whose `__ne__` returns an array rather than a bool, so the
    comparison itself raises and the assignment never completes.
    """

    class _ArrayLike:
        """Mimics numpy's refusal to reduce an element-wise comparison to one bool."""

        __hash__ = None  # type: ignore[assignment]

        def __ne__(self, other: object) -> bool:
            message = "The truth value of an array with more than one element is ambiguous."
            raise ValueError(message)

    def test_an_uncomparable_value_is_treated_as_changed(self) -> None:
        from griptape_nodes.exe_types.node_types import _values_differ

        assert _values_differ(self._ArrayLike(), self._ArrayLike()) is True

    def test_the_same_object_is_not_a_change(self) -> None:
        """Identity is checked first, so re-assigning the same array-like never touches `__ne__`."""
        from griptape_nodes.exe_types.node_types import _values_differ

        value = self._ArrayLike()

        assert _values_differ(value, value) is False

    def test_ordinary_values_compare_normally(self) -> None:
        from griptape_nodes.exe_types.node_types import _values_differ

        assert _values_differ(1, 2) is True
        assert _values_differ("a", "a") is False


class TestParameterVisibilityKeepsTraitStateLive:
    def test_hiding_a_parameter_with_a_trait_stores_no_trait_copy(self) -> None:
        node = MockNode()
        parameter = Parameter(name="top", tooltip="t", traits={Slider(min_val=0, max_val=100)})
        node.add_parameter(parameter)

        node.hide_parameter_by_name("top")

        assert parameter.ui_options["hide"] is True
        assert "slider" not in parameter.authored_ui_options()

    def test_a_later_trait_change_still_reaches_a_hidden_parameter(self) -> None:
        node = MockNode()
        trait = Slider(min_val=0, max_val=100)
        parameter = Parameter(name="top", tooltip="t", traits={trait})
        node.add_parameter(parameter)
        node.hide_parameter_by_name("top")

        trait.max = 512

        assert parameter.ui_options["slider"] == {"min_val": 0, "max_val": 512}
