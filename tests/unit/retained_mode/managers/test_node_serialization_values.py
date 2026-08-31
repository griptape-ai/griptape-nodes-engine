"""Tests for the parameter-value pooling primitives used by node serialization.

These functions decide, for a single parameter value, whether it needs to be recorded at all,
whether it has already been recorded (so it can be referenced by UUID instead of duplicated), and
what happens when recording it fails. They are the foundation both the workflow-save path
(``handle_parameter_value_saving`` / ``_handle_value_hashing``) and the output-value pickling path
(``serialize_parameter_output_values`` / ``_serialize_with_pickling``) build on.
"""

# ruff: noqa: PLR2004

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest

from griptape_nodes.exe_types.core_types import Parameter
from griptape_nodes.retained_mode.events.node_events import SerializedNodeCommands
from griptape_nodes.retained_mode.events.parameter_events import SetParameterValueRequest
from griptape_nodes.retained_mode.managers.node_manager import (
    NodeManager,
    SerializedParameterValues,
    SerializedParameterValueTracker,
)
from tests.unit.exe_types.mocks import MockNode

if TYPE_CHECKING:
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
        parameter = _make_param("p")
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
            parameter_name="p",
            node_name="n",
            is_output=False,
            workflow_manager=engine.workflow_manager,
            use_pickling=False,
        )

        assert command is not None
        assert pool[command.unique_value_uuid] is hostile_value
        warning_messages = [record.message for record in caplog.records if record.levelno == logging.WARNING]
        assert any(
            "could not be copied" in message and "p" in message and "n" in message for message in warning_messages
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


class TestGetParameterValueForSerialization:
    """Output values take precedence over set values when both exist for a parameter."""

    def test_prefers_output_value_over_set_value(self) -> None:
        parameter = _make_param("p")
        node = MockNode(name="n")
        node.add_parameter(parameter)
        node.parameter_values["p"] = "set value"
        node.parameter_output_values["p"] = "output value"

        value = NodeManager._get_parameter_value_for_serialization(node, "p")

        assert value == "output value"

    def test_falls_back_to_set_value_when_no_output_value(self) -> None:
        parameter = _make_param("p")
        node = MockNode(name="n")
        node.add_parameter(parameter)
        node.parameter_values["p"] = "set value"

        value = NodeManager._get_parameter_value_for_serialization(node, "p")

        assert value == "set value"


class TestSerializeParameterOutputValues:
    """``serialize_parameter_output_values`` is the entry point used by control-flow execution."""

    def test_node_with_no_parameters_returns_empty_values_and_no_pool(self, engine: Engine) -> None:
        node = MockNode(name="n")

        result = NodeManager.serialize_parameter_output_values(node, workflow_manager=engine.workflow_manager)

        assert result == SerializedParameterValues({}, None)

    def test_without_pickling_every_parameter_has_an_entry_and_pool_is_none(self, engine: Engine) -> None:
        node = MockNode(name="n")
        node.add_parameter(_make_param("has_value"))
        node.add_parameter(_make_param("no_value"))
        node.parameter_values["has_value"] = "set"

        result = NodeManager.serialize_parameter_output_values(
            node, workflow_manager=engine.workflow_manager, use_pickling=False
        )

        assert result.parameter_output_values == {"has_value": "set", "no_value": None}
        assert result.unique_parameter_uuid_to_values is None

    def test_without_pickling_prefers_output_value(self, engine: Engine) -> None:
        node = MockNode(name="n")
        node.add_parameter(_make_param("p"))
        node.parameter_values["p"] = "set value"
        node.parameter_output_values["p"] = "output value"

        result = NodeManager.serialize_parameter_output_values(
            node, workflow_manager=engine.workflow_manager, use_pickling=False
        )

        assert result.parameter_output_values == {"p": "output value"}

    def test_without_pickling_output_is_json_safe_for_nested_containers(self, engine: Engine) -> None:
        import json

        node = MockNode(name="n")
        node.add_parameter(_make_param("p"))
        node.parameter_output_values["p"] = {"nested": [1, 2, {"deep": True}]}

        result = NodeManager.serialize_parameter_output_values(
            node, workflow_manager=engine.workflow_manager, use_pickling=False
        )

        json.dumps(result.parameter_output_values)

    def test_with_pickling_every_parameter_maps_to_a_uuid(self, engine: Engine) -> None:
        node = MockNode(name="n")
        node.add_parameter(_make_param("p"))
        node.parameter_output_values["p"] = "value"

        result = NodeManager.serialize_parameter_output_values(
            node, workflow_manager=engine.workflow_manager, use_pickling=True
        )

        assert result.unique_parameter_uuid_to_values is not None
        assert result.parameter_output_values["p"] in result.unique_parameter_uuid_to_values

    def test_with_pickling_unserializable_value_maps_to_none_without_raising(self, engine: Engine) -> None:
        node = MockNode(name="n")
        node.add_parameter(_make_param("p"))
        node.parameter_output_values["p"] = _AlwaysFailsPickle()

        result = NodeManager.serialize_parameter_output_values(
            node, workflow_manager=engine.workflow_manager, use_pickling=True
        )

        assert result.parameter_output_values["p"] is None

    def test_with_pickling_dedups_the_same_value_shared_by_two_parameters(self, engine: Engine) -> None:
        node = MockNode(name="n")
        node.add_parameter(_make_param("first"))
        node.add_parameter(_make_param("second"))
        shared_value = "shared string"
        node.parameter_output_values["first"] = shared_value
        node.parameter_output_values["second"] = shared_value

        result = NodeManager.serialize_parameter_output_values(
            node, workflow_manager=engine.workflow_manager, use_pickling=True
        )

        assert result.parameter_output_values["first"] == result.parameter_output_values["second"]
        assert result.unique_parameter_uuid_to_values is not None
        assert len(result.unique_parameter_uuid_to_values) == 1


class TestBoolIntPoolCollisionBug:
    """Pinned bug: the pickling dedup pool keys by raw value, so ``True`` and ``1`` alias.

    ``_process_parameter_for_pickling`` / ``_handle_new_value_for_pickling`` key their dedup cache
    by ``param_value`` alone (``hash(True) == hash(1)`` and ``True == 1``), unlike the safer
    ``_handle_value_hashing`` used by the workflow-save path, which keys by ``(type(value),
    value)``. As a result, a bool-valued parameter and an int-valued parameter whose values are
    pickle-equal collapse into ONE pool entry: the second parameter silently reads back the
    first parameter's stored value instead of its own. Intended contract: each parameter's pooled
    value is keyed by its own type-and-value identity, so ``True`` and ``1`` never alias.
    """

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "DATA-LOSS: bug, scoped to the runtime output-value pickling path only "
            "(_process_parameter_for_pickling/_handle_new_value_for_pickling, reached via "
            "serialize_parameter_output_values(use_pickling=True), whose only caller is "
            "machines/control_flow.py): they dedup by raw `param_value` alone (hash(True) == "
            "hash(1) and True == 1), so a bool output value and an int output value collapse "
            "into one pool entry and the second parameter reads back the first's UUID. The "
            "disk-save path is NOT affected: _handle_value_hashing (node_manager.py, reached via "
            "on_serialize_node_to_commands) already keys its dedup cache by `(type(value), "
            "value)`, so the two functions have diverged and only this one needs the fix. "
            "- see #5435"
        ),
    )
    def test_bool_and_int_output_values_get_distinct_pool_entries(self, engine: Engine) -> None:
        node = MockNode(name="n")
        node.add_parameter(_make_param("flag"))
        node.add_parameter(_make_param("count"))
        node.parameter_output_values["flag"] = True
        node.parameter_output_values["count"] = 1

        result = NodeManager.serialize_parameter_output_values(
            node, workflow_manager=engine.workflow_manager, use_pickling=True
        )

        assert result.parameter_output_values["flag"] != result.parameter_output_values["count"]
        assert result.unique_parameter_uuid_to_values is not None
        assert len(result.unique_parameter_uuid_to_values) == 2


def _make_create_node_request(*, resolution: str | None = None) -> Any:
    from griptape_nodes.exe_types.node_types import NodeResolutionState
    from griptape_nodes.retained_mode.events.node_events import CreateNodeRequest

    return CreateNodeRequest(
        node_type="TestNode",
        node_name="n",
        resolution=resolution or NodeResolutionState.RESOLVED.value,
    )
