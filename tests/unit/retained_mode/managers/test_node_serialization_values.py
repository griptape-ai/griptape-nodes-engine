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
from griptape_nodes.retained_mode.managers.node_manager import (
    NodeManager,
    SerializedParameterValueTracker,
)
from griptape_nodes.serialization.values import encode_value, value_key
from tests.unit.exe_types.mocks import MockNode

if TYPE_CHECKING:
    import pytest


def _make_param(name: str, *, serializable: bool = True) -> Parameter:
    return Parameter(
        name=name,
        input_types=["str"],
        type="str",
        output_type="str",
        tooltip="",
        serializable=serializable,
    )


class _NoPlainDataForm:
    """A value the value codec cannot encode."""


class _CountsEncodes:
    """Supplies its own state, counting how often it is asked for it."""

    encode_count = 0

    def __init__(self, payload: str) -> None:
        self.payload = payload

    def to_state(self) -> dict[str, str]:
        type(self).encode_count += 1
        return {"payload": self.payload}

    @classmethod
    def from_state(cls, state: dict[str, str]) -> _CountsEncodes:
        return cls(state["payload"])


class _FailsToEncode:
    """Its state cannot be produced, counting how many times it was asked."""

    def __init__(self) -> None:
        self.attempt_count = 0

    def to_state(self) -> dict:
        self.attempt_count += 1
        msg = "refuses to be encoded"
        raise TypeError(msg)

    @classmethod
    def from_state(cls, _state: dict) -> _FailsToEncode:
        return cls()


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


def _pool_value(
    value: Any,
    tracker: SerializedParameterValueTracker,
    pool: dict[Any, Any],
    *,
    parameter: Parameter | None = None,
    is_output: bool = False,
) -> SerializedNodeCommands.IndirectSetParameterValueCommand | None:
    return NodeManager._handle_value_hashing(
        value=value,
        serialized_parameter_value_tracker=tracker,
        unique_parameter_uuid_to_values=pool,
        parameter=parameter or _make_param("p"),
        parameter_name="p",
        node_name="n",
        is_output=is_output,
    )


class TestHandleValueHashing:
    """``_handle_value_hashing`` pools a value's encoded form under a hash of its content."""

    def test_pool_holds_the_encoded_value_under_its_content_key(self) -> None:
        pool: dict[Any, Any] = {}

        command = _pool_value((1, "b"), SerializedParameterValueTracker(), pool)

        assert command is not None
        encoded = encode_value((1, "b"))
        assert pool == {value_key(encoded): encoded}
        assert command.unique_value_uuid == value_key(encoded)

    def test_equal_values_in_distinct_objects_share_one_entry(self) -> None:
        tracker = SerializedParameterValueTracker()
        pool: dict[Any, Any] = {}

        first = _pool_value({"k": [1, 2]}, tracker, pool)
        second = _pool_value({"k": [1, 2]}, tracker, pool)

        assert first is not None
        assert second is not None
        assert first.unique_value_uuid == second.unique_value_uuid
        assert len(pool) == 1

    def test_bool_and_int_with_equal_hash_get_distinct_entries(self) -> None:
        tracker = SerializedParameterValueTracker()
        pool: dict[Any, Any] = {}

        as_int = _pool_value(1, tracker, pool)
        as_bool = _pool_value(True, tracker, pool)

        assert as_int is not None
        assert as_bool is not None
        assert as_int.unique_value_uuid != as_bool.unique_value_uuid
        assert set(pool.values()) == {1}

    def test_the_same_object_is_encoded_once(self) -> None:
        tracker = SerializedParameterValueTracker()
        pool: dict[Any, Any] = {}
        shared = _CountsEncodes("payload")
        _CountsEncodes.encode_count = 0

        first = _pool_value(shared, tracker, pool)
        second = _pool_value(shared, tracker, pool)

        assert first is not None
        assert second is not None
        assert first.unique_value_uuid == second.unique_value_uuid
        assert _CountsEncodes.encode_count == 1

    def test_non_serializable_parameter_skips_and_marks_tracker(self) -> None:
        tracker = SerializedParameterValueTracker()
        pool: dict[Any, Any] = {}
        value = "opted out"

        command = _pool_value(value, tracker, pool, parameter=_make_param("p", serializable=False))

        assert command is None
        assert pool == {}
        assert tracker.get_tracker_state(id(value)) == SerializedParameterValueTracker.TrackerState.NOT_SERIALIZABLE

    def test_value_with_no_plain_data_form_is_skipped_and_not_retried(self) -> None:
        tracker = SerializedParameterValueTracker()
        pool: dict[Any, Any] = {}
        value = _FailsToEncode()

        assert _pool_value(value, tracker, pool) is None
        assert _pool_value(value, tracker, pool) is None

        assert pool == {}
        assert value.attempt_count == 1

    def test_returns_an_indirect_set_parameter_value_command_referencing_the_pool(self) -> None:
        pool: dict[Any, Any] = {}

        command = _pool_value("output value", SerializedParameterValueTracker(), pool, is_output=True)

        assert command is not None
        assert command.set_parameter_value_command.parameter_name == "p"
        assert command.set_parameter_value_command.is_output is True
        assert command.set_parameter_value_command.initial_setup is True
        assert command.unique_value_uuid in pool


class TestSerializeOneParameterValueForSave:
    """``_serialize_one_parameter_value_for_save`` decides whether a single value gets recorded."""

    def test_none_value_returns_none_without_touching_the_tracker(self) -> None:
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
        )

        assert result is None
        assert pool == {}
        from griptape_nodes.exe_types.node_types import NodeResolutionState

        assert create_request.resolution != NodeResolutionState.UNRESOLVED.value

    def test_serializable_false_records_nothing_and_forces_unresolved_silently(
        self, caplog: pytest.LogCaptureFixture
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
        )

        assert result is None
        assert create_request.resolution == NodeResolutionState.UNRESOLVED.value
        warning_messages = [record.message for record in caplog.records if record.levelno == logging.WARNING]
        assert not any("Attempted to save" in message for message in warning_messages)

    def test_genuine_failure_warns_naming_parameter_and_node_and_forces_unresolved(
        self, caplog: pytest.LogCaptureFixture
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
            value=_NoPlainDataForm(),
            value_kind="output",
            is_output=True,
            parameter=parameter,
            node=node,
            unique_parameter_uuid_to_values=pool,
            serialized_parameter_value_tracker=tracker,
            create_node_request=create_request,
        )

        assert result is None
        assert create_request.resolution == NodeResolutionState.UNRESOLVED.value
        warning_messages = [record.message for record in caplog.records if record.levelno == logging.WARNING]
        assert any(
            "'troublesome'" in message and "'the_node'" in message and "output value" in message
            for message in warning_messages
        )

    def test_successful_serialization_returns_a_command_and_leaves_resolution_untouched(self) -> None:
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
        )

        assert result is not None
        assert result.unique_value_uuid in pool
        assert create_request.resolution == NodeResolutionState.RESOLVED.value


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
