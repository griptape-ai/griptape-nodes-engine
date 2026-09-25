"""Tests for the parameter-value pooling primitives used by node serialization.

These functions decide, for a single parameter value, whether it needs to be recorded at all,
whether it has already been recorded (so it can be referenced by UUID instead of duplicated), and
what happens when recording it fails. The workflow-save path (``handle_parameter_value_saving`` /
``_handle_value_hashing``) builds on them. ``result_parameter_values`` gathers a finished flow's
values for its result event.
"""

# ruff: noqa: PLR2004

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from griptape_nodes.exe_types.core_types import Parameter
from griptape_nodes.retained_mode.events.node_events import SerializedNodeCommands
from griptape_nodes.retained_mode.events.parameter_events import SetParameterValueRequest
from griptape_nodes.retained_mode.managers.node_manager import (
    NodeManager,
    SerializedParameterValueTracker,
)
from tests.unit.exe_types.mocks import MockNode

if TYPE_CHECKING:
    import pytest

    from griptape_nodes.retained_mode.engine import Engine


def _make_param(name: str, *, serializable: bool = True) -> Parameter:
    return Parameter(
        name=name,
        input_types=["str"],
        type="str",
        output_type="str",
        tooltip="",
        serializable=serializable,
    )


class _DeepcopyHostile:
    """A value that pickles fine but whose deepcopy always raises.

    Exercises the ``copy.deepcopy`` failure branch inside ``_handle_value_hashing``, which falls
    back to storing the value by reference and warns rather than losing it.
    """

    def __init__(self, payload: str) -> None:
        self.payload = payload

    def __deepcopy__(self, memo: dict) -> _DeepcopyHostile:
        msg = "this type refuses to be deep-copied"
        raise RuntimeError(msg)


class _AlwaysFailsPickle:
    """A hashable value that always fails to pickle, counting how many times it tried.

    Used to assert that a value marked not-serializable is remembered and never retried.
    """

    def __init__(self) -> None:
        self.attempt_count = 0

    def __reduce__(self) -> tuple:
        self.attempt_count += 1
        msg = "refuses to be pickled"
        raise TypeError(msg)


class TestSerializedParameterValueTracker:
    """The tracker records, per value hash, whether a value is serializable and its pool UUID."""

    def test_unseen_hash_reports_not_in_tracker(self) -> None:
        tracker = SerializedParameterValueTracker()
        assert tracker.get_tracker_state("anything") == SerializedParameterValueTracker.TrackerState.NOT_IN_TRACKER

    def test_serializable_hash_reports_serializable_and_returns_its_uuid(self) -> None:
        tracker = SerializedParameterValueTracker()
        unique_uuid = SerializedNodeCommands.UniqueParameterValueUUID(str(uuid4()))
        tracker.add_as_serializable("value_hash", unique_uuid)

        assert tracker.get_tracker_state("value_hash") == SerializedParameterValueTracker.TrackerState.SERIALIZABLE
        assert tracker.get_uuid_for_value_hash("value_hash") == unique_uuid

    def test_not_serializable_hash_reports_not_serializable(self) -> None:
        tracker = SerializedParameterValueTracker()
        tracker.add_as_not_serializable("bad_value")

        assert tracker.get_tracker_state("bad_value") == SerializedParameterValueTracker.TrackerState.NOT_SERIALIZABLE

    def test_serializable_count_counts_distinct_values_not_not_serializable_ones(self) -> None:
        tracker = SerializedParameterValueTracker()
        tracker.add_as_serializable("a", SerializedNodeCommands.UniqueParameterValueUUID(str(uuid4())))
        tracker.add_as_serializable("b", SerializedNodeCommands.UniqueParameterValueUUID(str(uuid4())))
        tracker.add_as_not_serializable("c")

        assert tracker.get_serializable_count() == 2


class TestHandleValueHashing:
    """``_handle_value_hashing`` pools a value once and lets every later reference reuse its UUID."""

    def test_identical_hashable_value_reused_across_two_calls(self, engine: Engine) -> None:
        parameter = _make_param("p")
        tracker = SerializedParameterValueTracker()
        pool: dict[Any, Any] = {}

        first = NodeManager._handle_value_hashing(
            value="shared value",
            serialized_parameter_value_tracker=tracker,
            unique_parameter_uuid_to_values=pool,
            parameter=parameter,
            parameter_name="p",
            node_name="n",
            is_output=False,
            workflow_manager=engine.workflow_manager,
        )
        second = NodeManager._handle_value_hashing(
            value="shared value",
            serialized_parameter_value_tracker=tracker,
            unique_parameter_uuid_to_values=pool,
            parameter=parameter,
            parameter_name="p",
            node_name="n",
            is_output=False,
            workflow_manager=engine.workflow_manager,
        )

        assert first is not None
        assert second is not None
        assert first.unique_value_uuid == second.unique_value_uuid
        assert len(pool) == 1

    def test_type_disambiguates_int_and_bool_with_equal_hash(self, engine: Engine) -> None:
        """``hash(True) == hash(1)``, so the pool key must include the type, not just the value."""
        parameter = _make_param("p")
        tracker = SerializedParameterValueTracker()
        pool: dict[Any, Any] = {}

        bool_command = NodeManager._handle_value_hashing(
            value=True,
            serialized_parameter_value_tracker=tracker,
            unique_parameter_uuid_to_values=pool,
            parameter=parameter,
            parameter_name="flag",
            node_name="n",
            is_output=False,
            workflow_manager=engine.workflow_manager,
        )
        int_command = NodeManager._handle_value_hashing(
            value=1,
            serialized_parameter_value_tracker=tracker,
            unique_parameter_uuid_to_values=pool,
            parameter=parameter,
            parameter_name="count",
            node_name="n",
            is_output=False,
            workflow_manager=engine.workflow_manager,
        )

        assert bool_command is not None
        assert int_command is not None
        assert bool_command.unique_value_uuid != int_command.unique_value_uuid
        assert len(pool) == 2

    def test_unhashable_same_object_reuses_its_pool_entry(self, engine: Engine) -> None:
        parameter = _make_param("p")
        tracker = SerializedParameterValueTracker()
        pool: dict[Any, Any] = {}
        shared_list = ["a", "b"]

        first = NodeManager._handle_value_hashing(
            value=shared_list,
            serialized_parameter_value_tracker=tracker,
            unique_parameter_uuid_to_values=pool,
            parameter=parameter,
            parameter_name="p",
            node_name="n",
            is_output=False,
            workflow_manager=engine.workflow_manager,
        )
        second = NodeManager._handle_value_hashing(
            value=shared_list,
            serialized_parameter_value_tracker=tracker,
            unique_parameter_uuid_to_values=pool,
            parameter=parameter,
            parameter_name="p",
            node_name="n",
            is_output=False,
            workflow_manager=engine.workflow_manager,
        )

        assert first is not None
        assert second is not None
        assert first.unique_value_uuid == second.unique_value_uuid
        assert len(pool) == 1

    def test_unhashable_equal_but_distinct_objects_get_separate_pool_entries(self, engine: Engine) -> None:
        """Two different list objects with equal contents are not the same value for pooling purposes.

        Both lists are kept alive as local variables for the whole test: an unhashable value is
        pooled by ``id()``, and a value that is garbage-collected between calls can have its id
        reused by an unrelated object, which is a real hazard but not what this test targets.
        """
        parameter = _make_param("p")
        tracker = SerializedParameterValueTracker()
        pool: dict[Any, Any] = {}
        first_list = ["a", "b"]
        second_list = ["a", "b"]

        first = NodeManager._handle_value_hashing(
            value=first_list,
            serialized_parameter_value_tracker=tracker,
            unique_parameter_uuid_to_values=pool,
            parameter=parameter,
            parameter_name="p",
            node_name="n",
            is_output=False,
            workflow_manager=engine.workflow_manager,
        )
        second = NodeManager._handle_value_hashing(
            value=second_list,
            serialized_parameter_value_tracker=tracker,
            unique_parameter_uuid_to_values=pool,
            parameter=parameter,
            parameter_name="p",
            node_name="n",
            is_output=False,
            workflow_manager=engine.workflow_manager,
        )

        assert first is not None
        assert second is not None
        assert first.unique_value_uuid != second.unique_value_uuid
        assert len(pool) == 2

    def test_non_serializable_parameter_skips_and_marks_tracker(self, engine: Engine) -> None:
        parameter = _make_param("p", serializable=False)
        tracker = SerializedParameterValueTracker()
        pool: dict[Any, Any] = {}

        result = NodeManager._handle_value_hashing(
            value="opted out",
            serialized_parameter_value_tracker=tracker,
            unique_parameter_uuid_to_values=pool,
            parameter=parameter,
            parameter_name="p",
            node_name="n",
            is_output=False,
            workflow_manager=engine.workflow_manager,
        )

        assert result is None
        assert pool == {}

    def test_pickle_failure_marks_value_not_serializable_and_returns_none(self, engine: Engine) -> None:
        parameter = _make_param("p")
        tracker = SerializedParameterValueTracker()
        pool: dict[Any, Any] = {}
        unpicklable = _AlwaysFailsPickle()

        result = NodeManager._handle_value_hashing(
            value=unpicklable,
            serialized_parameter_value_tracker=tracker,
            unique_parameter_uuid_to_values=pool,
            parameter=parameter,
            parameter_name="p",
            node_name="n",
            is_output=False,
            workflow_manager=engine.workflow_manager,
        )

        assert result is None
        assert pool == {}
        assert (
            tracker.get_tracker_state((type(unpicklable), unpicklable))
            == SerializedParameterValueTracker.TrackerState.NOT_SERIALIZABLE
        )

    def test_pickle_failure_is_not_retried_on_second_call(self, engine: Engine) -> None:
        parameter = _make_param("p")
        tracker = SerializedParameterValueTracker()
        pool: dict[Any, Any] = {}
        unpicklable = _AlwaysFailsPickle()

        NodeManager._handle_value_hashing(
            value=unpicklable,
            serialized_parameter_value_tracker=tracker,
            unique_parameter_uuid_to_values=pool,
            parameter=parameter,
            parameter_name="p",
            node_name="n",
            is_output=False,
            workflow_manager=engine.workflow_manager,
        )
        NodeManager._handle_value_hashing(
            value=unpicklable,
            serialized_parameter_value_tracker=tracker,
            unique_parameter_uuid_to_values=pool,
            parameter=parameter,
            parameter_name="p",
            node_name="n",
            is_output=False,
            workflow_manager=engine.workflow_manager,
        )

        assert unpicklable.attempt_count == 1

    def test_use_pickling_true_stores_pickled_bytes_in_pool(self, engine: Engine) -> None:
        parameter = _make_param("p")
        tracker = SerializedParameterValueTracker()
        pool: dict[Any, Any] = {}

        command = NodeManager._handle_value_hashing(
            value="pickle me",
            serialized_parameter_value_tracker=tracker,
            unique_parameter_uuid_to_values=pool,
            parameter=parameter,
            parameter_name="p",
            node_name="n",
            is_output=False,
            workflow_manager=engine.workflow_manager,
            use_pickling=True,
        )

        assert command is not None
        assert isinstance(pool[command.unique_value_uuid], bytes)

    def test_use_pickling_false_stores_a_deep_copy_not_the_original_reference(self, engine: Engine) -> None:
        parameter = _make_param("p")
        tracker = SerializedParameterValueTracker()
        pool: dict[Any, Any] = {}
        original = {"nested": ["value"]}

        command = NodeManager._handle_value_hashing(
            value=original,
            serialized_parameter_value_tracker=tracker,
            unique_parameter_uuid_to_values=pool,
            parameter=parameter,
            parameter_name="p",
            node_name="n",
            is_output=False,
            workflow_manager=engine.workflow_manager,
            use_pickling=False,
        )

        assert command is not None
        stored = pool[command.unique_value_uuid]
        assert stored == original
        assert stored is not original

    def test_deepcopy_failure_falls_back_to_storing_the_raw_value_with_a_warning(
        self, engine: Engine, caplog: pytest.LogCaptureFixture
    ) -> None:
        parameter = _make_param("hostile_param")
        tracker = SerializedParameterValueTracker()
        pool: dict[Any, Any] = {}
        hostile_value = _DeepcopyHostile("payload")

        caplog.clear()
        caplog.set_level(logging.WARNING, logger="griptape_nodes")

        command = NodeManager._handle_value_hashing(
            value=hostile_value,
            serialized_parameter_value_tracker=tracker,
            unique_parameter_uuid_to_values=pool,
            parameter=parameter,
            parameter_name="hostile_param",
            node_name="hostile_node",
            is_output=False,
            workflow_manager=engine.workflow_manager,
            use_pickling=False,
        )

        assert command is not None
        assert pool[command.unique_value_uuid] is hostile_value
        warning_messages = [record.message for record in caplog.records if record.levelno == logging.WARNING]
        assert any(
            "could not be copied" in message and "'hostile_param'" in message and "'hostile_node'" in message
            for message in warning_messages
        )

    def test_returns_an_indirect_set_parameter_value_command_referencing_the_pool(self, engine: Engine) -> None:
        parameter = _make_param("p")
        tracker = SerializedParameterValueTracker()
        pool: dict[Any, Any] = {}

        command = NodeManager._handle_value_hashing(
            value="v",
            serialized_parameter_value_tracker=tracker,
            unique_parameter_uuid_to_values=pool,
            parameter=parameter,
            parameter_name="p",
            node_name="n",
            is_output=True,
            workflow_manager=engine.workflow_manager,
        )

        assert command is not None
        assert isinstance(command.set_parameter_value_command, SetParameterValueRequest)
        assert command.set_parameter_value_command.parameter_name == "p"
        assert command.set_parameter_value_command.is_output is True
        assert command.set_parameter_value_command.initial_setup is True
        assert command.unique_value_uuid in pool


class TestSerializeOneParameterValueForSave:
    """``_serialize_one_parameter_value_for_save`` decides whether a single value gets recorded."""

    def test_none_value_returns_none_without_touching_the_tracker(self, engine: Engine) -> None:
        parameter = _make_param("p")
        node = MockNode(name="n")
        node.add_parameter(parameter)
        tracker = SerializedParameterValueTracker()
        pool: dict[Any, Any] = {}
        create_request = _make_create_node_request()

        result = NodeManager._serialize_one_parameter_value_for_save(
            value=None,
            value_kind="set",
            is_output=False,
            parameter=parameter,
            node=node,
            unique_parameter_uuid_to_values=pool,
            serialized_parameter_value_tracker=tracker,
            create_node_request=create_request,
            workflow_manager=engine.workflow_manager,
            use_pickling=False,
        )

        assert result is None
        assert pool == {}
        from griptape_nodes.exe_types.node_types import NodeResolutionState

        assert create_request.resolution != NodeResolutionState.UNRESOLVED.value

    def test_serializable_false_records_nothing_and_forces_unresolved_silently(
        self, engine: Engine, caplog: pytest.LogCaptureFixture
    ) -> None:
        from griptape_nodes.exe_types.node_types import NodeResolutionState

        parameter = _make_param("p", serializable=False)
        node = MockNode(name="n")
        node.add_parameter(parameter)
        tracker = SerializedParameterValueTracker()
        pool: dict[Any, Any] = {}
        create_request = _make_create_node_request(resolution=NodeResolutionState.RESOLVED.value)

        caplog.clear()
        caplog.set_level(logging.WARNING, logger="griptape_nodes")

        result = NodeManager._serialize_one_parameter_value_for_save(
            value="opted out value",
            value_kind="set",
            is_output=False,
            parameter=parameter,
            node=node,
            unique_parameter_uuid_to_values=pool,
            serialized_parameter_value_tracker=tracker,
            create_node_request=create_request,
            workflow_manager=engine.workflow_manager,
            use_pickling=False,
        )

        assert result is None
        assert create_request.resolution == NodeResolutionState.UNRESOLVED.value
        warning_messages = [record.message for record in caplog.records if record.levelno == logging.WARNING]
        assert not any("Attempted to serialize" in message for message in warning_messages)

    def test_genuine_failure_warns_naming_parameter_and_node_and_forces_unresolved(
        self, engine: Engine, caplog: pytest.LogCaptureFixture
    ) -> None:
        from griptape_nodes.exe_types.node_types import NodeResolutionState

        parameter = _make_param("troublesome")
        node = MockNode(name="the_node")
        node.add_parameter(parameter)
        tracker = SerializedParameterValueTracker()
        pool: dict[Any, Any] = {}
        create_request = _make_create_node_request(resolution=NodeResolutionState.RESOLVED.value)

        caplog.clear()
        caplog.set_level(logging.WARNING, logger="griptape_nodes")

        result = NodeManager._serialize_one_parameter_value_for_save(
            value=_AlwaysFailsPickle(),
            value_kind="output",
            is_output=True,
            parameter=parameter,
            node=node,
            unique_parameter_uuid_to_values=pool,
            serialized_parameter_value_tracker=tracker,
            create_node_request=create_request,
            workflow_manager=engine.workflow_manager,
            use_pickling=False,
        )

        assert result is None
        assert create_request.resolution == NodeResolutionState.UNRESOLVED.value
        warning_messages = [record.message for record in caplog.records if record.levelno == logging.WARNING]
        assert any(
            "'troublesome'" in message and "'the_node'" in message and "output value" in message
            for message in warning_messages
        )

    def test_successful_serialization_returns_a_command_and_leaves_resolution_untouched(self, engine: Engine) -> None:
        from griptape_nodes.exe_types.node_types import NodeResolutionState

        parameter = _make_param("p")
        node = MockNode(name="n")
        node.add_parameter(parameter)
        tracker = SerializedParameterValueTracker()
        pool: dict[Any, Any] = {}
        create_request = _make_create_node_request(resolution=NodeResolutionState.RESOLVED.value)

        result = NodeManager._serialize_one_parameter_value_for_save(
            value="a fine value",
            value_kind="set",
            is_output=False,
            parameter=parameter,
            node=node,
            unique_parameter_uuid_to_values=pool,
            serialized_parameter_value_tracker=tracker,
            create_node_request=create_request,
            workflow_manager=engine.workflow_manager,
            use_pickling=False,
        )

        assert result is not None
        assert result.unique_value_uuid in pool
        assert create_request.resolution == NodeResolutionState.RESOLVED.value


class _NoPlainDataForm:
    """A value the value codec cannot encode."""


class TestResultParameterValues:
    """``result_parameter_values`` gathers a finished flow's values for its result event."""

    def test_node_with_no_parameters_has_no_values(self) -> None:
        assert NodeManager.result_parameter_values(MockNode(name="n")) == {}

    def test_every_parameter_has_an_entry(self) -> None:
        node = MockNode(name="n")
        node.add_parameter(_make_param("has_value"))
        node.add_parameter(_make_param("no_value"))
        node.parameter_values["has_value"] = "set"

        assert NodeManager.result_parameter_values(node) == {"has_value": "set", "no_value": None}

    def test_output_value_wins_over_set_value(self) -> None:
        node = MockNode(name="n")
        node.add_parameter(_make_param("p"))
        node.parameter_values["p"] = "set value"
        node.parameter_output_values["p"] = "output value"

        assert NodeManager.result_parameter_values(node) == {"p": "output value"}

    def test_value_with_no_plain_data_form_becomes_none(self, caplog: pytest.LogCaptureFixture) -> None:
        node = MockNode(name="n")
        node.add_parameter(_make_param("bad"))
        node.add_parameter(_make_param("good"))
        node.parameter_output_values["bad"] = _NoPlainDataForm()
        node.parameter_output_values["good"] = "kept"

        with caplog.at_level(logging.WARNING):
            values = NodeManager.result_parameter_values(node)

        assert values == {"bad": None, "good": "kept"}
        assert "'bad'" in caplog.text


def _make_create_node_request(*, resolution: str | None = None) -> Any:
    from griptape_nodes.exe_types.node_types import NodeResolutionState
    from griptape_nodes.retained_mode.events.node_events import CreateNodeRequest

    return CreateNodeRequest(
        node_type="TestNode",
        node_name="n",
        resolution=resolution or NodeResolutionState.RESOLVED.value,
    )
