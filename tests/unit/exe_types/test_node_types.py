from unittest.mock import Mock, patch

import pytest
from griptape.artifacts import ImageUrlArtifact, TextArtifact

from griptape_nodes.exe_types.core_types import Parameter, ParameterList, ParameterMode
from griptape_nodes.exe_types.node_types import (
    AsyncResult,
    SuccessFailureNode,
    TrackedParameterOutputValues,
    aprocess_scope,
)
from griptape_nodes.traits.slider import Slider
from griptape_nodes.utils.budget_refusal import BUDGET_HALT_PREFIX, BudgetExceededError, BudgetRefusal

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


class TestASetDuringARunAlsoRecordsAResult:
    """`set_parameter_value` always writes `parameter_values`, and sometimes also records a result.

    A value a node sets on itself while its own body is running is what that run computed, and of the
    node's two stores only `parameter_output_values` travels back from a library's isolated process. So
    a set in that window writes both: the result reaches the rest of the graph, and the authored copy is
    still there for the value to be read on the next run, after the produced store has been cleared.

    Nothing outside that window records a result, and neither does a Parameter with no OUTPUT to
    publish on, nor a container's child, which never travels on its own account.
    """

    def _node_with(self, param_name: str, modes: set[ParameterMode]) -> MockNode:
        node = MockNode(name="node")
        node.add_parameter(Parameter(name=param_name, type="str", tooltip="", allowed_modes=modes))
        return node

    def test_an_output_records_a_result_while_the_node_runs(self) -> None:
        node = self._node_with("out", {ParameterMode.OUTPUT})

        with aprocess_scope(node=node):
            node.set_parameter_value("out", "done")

        assert node.parameter_output_values["out"] == "done"
        assert node.parameter_values["out"] == "done"

    def test_the_value_survives_the_clear_before_the_next_run(self) -> None:
        """`SeedParameter` rolls a seed mid-run, and turning randomizing off has to keep that seed."""
        node = self._node_with("seed", {ParameterMode.PROPERTY, ParameterMode.INPUT, ParameterMode.OUTPUT})

        with aprocess_scope(node=node):
            node.set_parameter_value("seed", "1234")

        node.parameter_output_values.silent_clear()
        assert node.get_parameter_value("seed") == "1234"

    def test_a_property_and_output_records_a_result(self) -> None:
        """A Parameter kept on display still publishes, so what a run puts there is a result too."""
        node = self._node_with("both", {ParameterMode.PROPERTY, ParameterMode.OUTPUT})

        with aprocess_scope(node=node):
            node.set_parameter_value("both", "computed")

        assert node.parameter_output_values["both"] == "computed"

    def test_the_default_modes_record_a_result(self) -> None:
        """Declaring no modes at all allows OUTPUT, and that is most of the parameters in a library."""
        node = MockNode(name="node")
        node.add_parameter(Parameter(name="out", type="str", tooltip=""))

        with aprocess_scope(node=node):
            node.set_parameter_value("out", "computed")

        assert node.parameter_output_values["out"] == "computed"

    def test_a_set_at_edit_time_records_nothing(self) -> None:
        """No run produced this, and a stale result would shadow it for every reader that prefers one."""
        node = self._node_with("out", {ParameterMode.OUTPUT})

        node.set_parameter_value("out", "set before any run")

        assert node.parameter_values["out"] == "set before any run"
        assert "out" not in node.parameter_output_values

    def test_a_set_on_another_node_records_nothing_there(self) -> None:
        """A running node sets values on other nodes, and that is not the other node's result.

        This is how a value reaches a connected input, and how a node driving a subflow feeds it. The
        recipient is not running, and a result filed against it would be wiped by its own pre-run clear
        before it ever ran.
        """
        node = self._node_with("out", {ParameterMode.OUTPUT})
        downstream = self._node_with("out", {ParameterMode.OUTPUT})

        with aprocess_scope(node=node):
            downstream.set_parameter_value("out", "handed over")

        assert downstream.parameter_values["out"] == "handed over"
        assert "out" not in downstream.parameter_output_values

    def test_a_parameter_with_no_output_records_nothing(self) -> None:
        """With no OUTPUT there is no port to publish on, so a run's write to it is scratch."""
        node = self._node_with("incoming", {ParameterMode.INPUT})
        node.add_parameter(Parameter(name="knob", type="str", tooltip="", allowed_modes={ParameterMode.PROPERTY}))

        with aprocess_scope(node=node):
            node.set_parameter_value("incoming", "delivered")
            node.set_parameter_value("knob", "scratch")

        assert node.parameter_values["incoming"] == "delivered"
        assert node.parameter_values["knob"] == "scratch"
        assert node.parameter_output_values == {}

    def test_a_container_child_records_nothing_but_its_container_does(self) -> None:
        """Setting a child rebuilds the container, and it is the container that publishes.

        Split Video in the standard library grows an output-only `ParameterList` this way, adding a
        child per clip and setting it while the node runs.
        """
        node = MockNode(name="node")
        images = ParameterList(name="images", type="str", tooltip="", allowed_modes={ParameterMode.OUTPUT})
        node.add_parameter(images)
        child = images.add_child_parameter()

        with aprocess_scope(node=node):
            node.set_parameter_value(child.name, "img0")

        assert child.name not in node.parameter_output_values
        assert node.parameter_output_values["images"] == ["img0"]
        assert node.get_parameter_value("images") == ["img0"]


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

        def __eq__(self, other: object) -> bool:
            message = "The truth value of an array with more than one element is ambiguous."
            raise ValueError(message)

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


class TestArtifactChangeDetection:
    """Artifacts compare by content: a random `id` (and the `name` it defaults) is not a change."""

    def test_equal_content_with_different_ids_is_not_a_change(self) -> None:
        from griptape_nodes.exe_types.node_types import _values_differ

        assert _values_differ(ImageUrlArtifact("https://a.png"), ImageUrlArtifact("https://a.png")) is False

    def test_different_content_is_a_change(self) -> None:
        from griptape_nodes.exe_types.node_types import _values_differ

        assert _values_differ(ImageUrlArtifact("https://a.png"), ImageUrlArtifact("https://b.png")) is True

    def test_an_explicit_name_is_content(self) -> None:
        from griptape_nodes.exe_types.node_types import _values_differ

        old = ImageUrlArtifact("https://a.png", name="cat")
        new = ImageUrlArtifact("https://a.png", name="dog")

        assert _values_differ(old, new) is True

    def test_a_different_artifact_type_is_a_change(self) -> None:
        from griptape_nodes.exe_types.node_types import _values_differ

        assert _values_differ(TextArtifact("https://a.png"), ImageUrlArtifact("https://a.png")) is True

    def test_artifacts_inside_containers_compare_by_content(self) -> None:
        from griptape_nodes.exe_types.node_types import _values_differ

        old = {"images": [ImageUrlArtifact("https://a.png")]}
        new = {"images": [ImageUrlArtifact("https://a.png")]}

        assert _values_differ(old, new) is False
        assert _values_differ(old, {"images": [ImageUrlArtifact("https://b.png")]}) is True

    def test_a_shared_element_with_an_irreflexive_eq_is_not_a_change(self) -> None:
        """Containers rebuilt around the same elements, as `ParameterList` reads are, must read as unchanged."""
        from griptape_nodes.exe_types.node_types import _values_differ

        nan = float("nan")
        element = TestOutputValueChangeDetection._ArrayLike()

        assert _values_differ([nan], [nan]) is False
        assert _values_differ({"k": element}, {"k": element}) is False


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


class TestBudgetHaltsTakeTheFailureBranch:
    """A budget refusal is one node's failure, routed like any other.

    A Failed branch may lead somewhere the budget does not reach, such as a local model, so
    the node's wiring decides what happens next.
    """

    @staticmethod
    def _a_budget_error() -> BudgetExceededError:
        return BudgetExceededError(
            f"{BUDGET_HALT_PREFIX} Griptape Cloud refused the next call.",
            BudgetRefusal(),
        )

    def test_a_budget_error_takes_a_connected_failure_branch(self) -> None:
        node = SuccessFailureNode(name="refused_call")
        node._has_outgoing_connections = Mock(return_value=True)  # type: ignore[method-assign]

        node._handle_failure_exception(self._a_budget_error())

    def test_a_budget_error_raises_with_nothing_connected(self) -> None:
        node = SuccessFailureNode(name="refused_call")
        node._has_outgoing_connections = Mock(return_value=False)  # type: ignore[method-assign]

        with pytest.raises(BudgetExceededError):
            node._handle_failure_exception(self._a_budget_error())
