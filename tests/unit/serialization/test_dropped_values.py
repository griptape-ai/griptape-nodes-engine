"""Values left out of something read back later, and the warning that names them."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

from griptape_nodes.retained_mode.events.node_events import SerializedNodeCommands, SerializedParameterValueTracker
from griptape_nodes.serialization.dropped_values import (
    DroppedValue,
    dropped_values_detail,
    keep_encodable_default,
    success_details,
    unique_dropped_values,
)

if TYPE_CHECKING:
    import pytest

    from griptape_nodes.exe_types.node_groups.subflow_node_group import SubflowNodeGroup
    from griptape_nodes.retained_mode.engine import Engine


class _Opaque:
    """Has no plain-data form."""


class TestDroppedValuesDetail:
    def test_names_each_node_and_parameter_at_warning_level(self) -> None:
        detail = dropped_values_detail(
            [
                DroppedValue("Agent", "prompt", "A 'Widget' value has no plain-data form."),
                DroppedValue("Agent", "seed", "A 'Gadget' value has no plain-data form.", is_default=True),
                DroppedValue(None, "loose", "A 'Thing' value has no plain-data form."),
            ],
            action="save the workflow",
        )

        assert detail.level == logging.WARNING
        assert detail.message.splitlines() == [
            "Attempted to save the workflow. Failed to keep these values because they have no plain-data form, so they were left out:",
            "- parameter 'prompt' on node 'Agent' (A 'Widget' value has no plain-data form.)",
            "- default of parameter 'seed' on node 'Agent' (A 'Gadget' value has no plain-data form.)",
            "- parameter 'loose' (A 'Thing' value has no plain-data form.)",
        ]


class TestSuccessDetails:
    def test_has_only_the_message_when_nothing_was_left_out(self) -> None:
        details = success_details("Saved.", [], action="save", level=logging.INFO)

        assert [(d.level, d.message) for d in details.result_details] == [(logging.INFO, "Saved.")]

    def test_adds_a_warning_after_the_message_when_values_were_left_out(self) -> None:
        details = success_details("Saved.", [DroppedValue("n", "p", "why")], action="save", level=logging.INFO)

        assert [d.level for d in details.result_details] == [logging.INFO, logging.WARNING]


class TestUniqueDroppedValues:
    def test_keeps_first_seen_order_and_drops_repeats(self) -> None:
        first = DroppedValue("n", "a", "why")
        second = DroppedValue("n", "b", "why")

        assert unique_dropped_values([first, second, first]) == [first, second]


class TestKeepEncodableDefault:
    def test_returns_an_encodable_default_unchanged(self) -> None:
        dropped: list[DroppedValue] = []
        default = {"a": (1, 2)}

        assert keep_encodable_default(default, "n", "p", dropped) is default
        assert dropped == []

    def test_returns_none_and_reports_a_default_with_no_plain_data_form(self, caplog: pytest.LogCaptureFixture) -> None:
        dropped: list[DroppedValue] = []
        caplog.set_level(logging.WARNING, logger="griptape_nodes")

        assert keep_encodable_default(_Opaque(), "n", "p", dropped) is None

        assert [(d.node_name, d.parameter_name, d.is_default) for d in dropped] == [("n", "p", True)]
        assert "parameter 'p' on node 'n'" in caplog.text


class TestPackagedGroupInputs:
    """A group parameter that flows into a packaged loop or group body is left out and reported."""

    def test_an_input_with_no_plain_data_form_is_dropped_and_reported(
        self, engine: Engine, caplog: pytest.LogCaptureFixture
    ) -> None:
        group = SimpleNamespace(
            name="Group",
            metadata={
                "execution_environment": {
                    "Lib": {"start_flow_node": "StartFlow", "parameter_names": ["startflow_a", "startflow_b"]}
                }
            },
            _get_raw_parameter_value=lambda param_name: {"startflow_a": _Opaque(), "startflow_b": 7}[param_name],
        )
        commands: list[SerializedNodeCommands.IndirectSetParameterValueCommand] = []
        dropped: list[DroppedValue] = []
        caplog.set_level(logging.WARNING, logger="griptape_nodes")

        engine.flow_manager._apply_node_group_parameters_to_start_node(
            node_group_node=cast("SubflowNodeGroup", group),
            start_node_library_name="Lib",
            start_node_type="StartFlow",
            start_node_parameter_value_commands=commands,
            unique_parameter_uuid_to_values={},
            serialized_parameter_value_tracker=SerializedParameterValueTracker(),
            dropped_values=dropped,
        )

        assert [command.set_parameter_value_command.parameter_name for command in commands] == ["b"]
        assert [(d.node_name, d.parameter_name) for d in dropped] == [("Group", "startflow_a")]
        assert "parameter 'startflow_a' of node 'Group'" in caplog.text
