"""Values with no plain-data form are left out of what is read back later, and the user is told.

The node types named below are never resolvable through a real library in this test environment, so
each becomes an ``ErrorProxyNode`` placeholder that grows whatever parameters a request touches. It
serializes through the same commands a real node does.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, cast

import pytest

from griptape_nodes.retained_mode.events.flow_events import (
    CreateFlowRequest,
    CreateFlowResultSuccess,
    SerializeFlowToCommandsRequest,
    SerializeFlowToCommandsResultSuccess,
)
from griptape_nodes.retained_mode.events.node_events import (
    CreateNodeRequest,
    CreateNodeResultSuccess,
    DeserializeSelectedNodesFromCommandsRequest,
    DeserializeSelectedNodesFromCommandsResultSuccess,
    DuplicateSelectedNodesRequest,
    DuplicateSelectedNodesResultSuccess,
    SerializeNodeToCommandsRequest,
    SerializeNodeToCommandsResultSuccess,
    SerializeSelectedNodesToCommandsRequest,
    SerializeSelectedNodesToCommandsResultSuccess,
)
from griptape_nodes.retained_mode.events.object_events import ClearAllObjectStateRequest
from griptape_nodes.retained_mode.events.parameter_events import (
    AddParameterToNodeRequest,
    AddParameterToNodeResultSuccess,
    SetParameterValueRequest,
    SetParameterValueResultSuccess,
)
from griptape_nodes.serialization.dropped_values import DroppedValue

if TYPE_CHECKING:
    from collections.abc import Generator

    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.events.base_events import ResultDetails


class _Opaque:
    """Has no plain-data form."""


@pytest.fixture(autouse=True)
def clean_object_state(engine: Engine) -> Generator[None, None, None]:
    """Clear all object state around a test so leftover flows never bleed across tests."""
    engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
    engine.context_manager.push_workflow(workflow_name="wf_dropped_values")
    try:
        yield
    finally:
        engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))


def _create_flow(engine: Engine, flow_name: str, *, parent_flow_name: str | None = None) -> None:
    result = engine.handle_request(
        CreateFlowRequest(parent_flow_name=parent_flow_name, flow_name=flow_name, set_as_new_context=True)
    )
    assert isinstance(result, CreateFlowResultSuccess), result


def _create_node(engine: Engine, node_name: str) -> None:
    result = engine.handle_request(CreateNodeRequest(node_type="TestNodeA", node_name=node_name))
    assert isinstance(result, CreateNodeResultSuccess), result


def _set_value(engine: Engine, node_name: str, parameter_name: str, value: object) -> None:
    result = engine.handle_request(
        SetParameterValueRequest(node_name=node_name, parameter_name=parameter_name, value=value, initial_setup=True)
    )
    assert isinstance(result, SetParameterValueResultSuccess), result


def _details(result: object) -> list:
    return cast("ResultDetails", getattr(result, "result_details")).result_details  # noqa: B009


def _warnings(result: object) -> list[str]:
    return [detail.message for detail in _details(result) if detail.level == logging.WARNING]


class TestSerializeNode:
    def test_a_value_with_no_plain_data_form_is_dropped_and_reported(self, engine: Engine) -> None:
        _create_flow(engine, "flow_a")
        _create_node(engine, "node_a")
        _set_value(engine, "node_a", "text", _Opaque())
        _set_value(engine, "node_a", "count", 3)

        result = engine.handle_request(SerializeNodeToCommandsRequest(node_name="node_a"))

        assert isinstance(result, SerializeNodeToCommandsResultSuccess)
        assert [(d.node_name, d.parameter_name, d.is_default) for d in result.dropped_values] == [
            ("node_a", "text", False)
        ]
        assert "_Opaque" in result.dropped_values[0].reason
        assert [c.set_parameter_value_command.parameter_name for c in result.set_parameter_value_commands] == ["count"]

    def test_a_value_shared_by_two_parameters_is_reported_for_each(self, engine: Engine) -> None:
        _create_flow(engine, "flow_a")
        _create_node(engine, "node_a")
        shared = _Opaque()
        _set_value(engine, "node_a", "first", shared)
        _set_value(engine, "node_a", "second", shared)

        result = engine.handle_request(SerializeNodeToCommandsRequest(node_name="node_a"))

        assert isinstance(result, SerializeNodeToCommandsResultSuccess)
        assert {d.parameter_name for d in result.dropped_values} == {"first", "second"}

    def test_a_user_defined_default_with_no_plain_data_form_is_dropped_and_reported(self, engine: Engine) -> None:
        _create_flow(engine, "flow_a")
        _create_node(engine, "node_a")
        added = engine.handle_request(
            AddParameterToNodeRequest(
                node_name="node_a", parameter_name="extra", default_value=_Opaque(), type="any", tooltip="t"
            )
        )
        assert isinstance(added, AddParameterToNodeResultSuccess), added

        result = engine.handle_request(SerializeNodeToCommandsRequest(node_name="node_a"))

        assert isinstance(result, SerializeNodeToCommandsResultSuccess)
        # The parameter holds its default as its value, so the value is left out too.
        assert {(d.node_name, d.parameter_name, d.is_default) for d in result.dropped_values} == {
            ("node_a", "extra", True),
            ("node_a", "extra", False),
        }
        add_commands = [
            command
            for command in result.serialized_node_commands.element_modification_commands
            if isinstance(command, AddParameterToNodeRequest) and command.parameter_name == "extra"
        ]
        assert [command.default_value for command in add_commands] == [None]

    def test_a_plain_default_is_kept(self, engine: Engine) -> None:
        _create_flow(engine, "flow_a")
        _create_node(engine, "node_a")
        engine.handle_request(
            AddParameterToNodeRequest(
                node_name="node_a", parameter_name="extra", default_value={"a": 1}, type="any", tooltip="t"
            )
        )

        result = engine.handle_request(SerializeNodeToCommandsRequest(node_name="node_a"))

        assert isinstance(result, SerializeNodeToCommandsResultSuccess)
        assert result.dropped_values == []
        defaults = [
            command.default_value
            for command in result.serialized_node_commands.element_modification_commands
            if isinstance(command, AddParameterToNodeRequest) and command.parameter_name == "extra"
        ]
        assert defaults == [{"a": 1}]


class TestSerializeFlow:
    def test_values_dropped_in_a_child_flow_are_reported_by_the_parent(self, engine: Engine) -> None:
        _create_flow(engine, "flow_a")
        _create_node(engine, "top_node")
        _set_value(engine, "top_node", "text", _Opaque())
        _create_flow(engine, "flow_b", parent_flow_name="flow_a")
        _create_node(engine, "child_node")
        _set_value(engine, "child_node", "text", _Opaque())

        result = engine.handle_request(SerializeFlowToCommandsRequest(flow_name="flow_a"))

        assert isinstance(result, SerializeFlowToCommandsResultSuccess)
        assert {(d.node_name, d.parameter_name) for d in result.dropped_values} == {
            ("top_node", "text"),
            ("child_node", "text"),
        }


class TestCopyAndPaste:
    def test_copy_reports_the_values_it_left_out(self, engine: Engine) -> None:
        _create_flow(engine, "flow_a")
        _create_node(engine, "node_a")
        _set_value(engine, "node_a", "text", _Opaque())

        result = engine.handle_request(SerializeSelectedNodesToCommandsRequest(nodes_to_serialize=[["node_a", "0"]]))

        assert isinstance(result, SerializeSelectedNodesToCommandsResultSuccess)
        assert result.dropped_values == [DroppedValue("node_a", "text", result.dropped_values[0].reason)]
        [warning] = _warnings(result)
        assert "copy 1 nodes" in warning
        assert "parameter 'text' on node 'node_a'" in warning

    def test_copy_with_nothing_left_out_has_no_warning(self, engine: Engine) -> None:
        _create_flow(engine, "flow_a")
        _create_node(engine, "node_a")
        _set_value(engine, "node_a", "text", "hello")

        result = engine.handle_request(SerializeSelectedNodesToCommandsRequest(nodes_to_serialize=[["node_a", "0"]]))

        assert isinstance(result, SerializeSelectedNodesToCommandsResultSuccess)
        assert result.dropped_values == []
        assert _warnings(result) == []

    def test_paste_reports_a_copied_value_it_could_not_read(self, engine: Engine) -> None:
        _create_flow(engine, "flow_a")
        _create_node(engine, "node_a")
        _set_value(engine, "node_a", "text", "hello")
        copied = engine.handle_request(SerializeSelectedNodesToCommandsRequest(nodes_to_serialize=[["node_a", "0"]]))
        assert isinstance(copied, SerializeSelectedNodesToCommandsResultSuccess)
        unreadable = dict.fromkeys(copied.pickled_values, "not json and not a pickle")

        result = engine.handle_request(
            DeserializeSelectedNodesFromCommandsRequest(
                deserialize_commands=copied.serialized_selected_node_commands, pickled_values=unreadable
            )
        )

        assert isinstance(result, DeserializeSelectedNodesFromCommandsResultSuccess)
        assert [(d.node_name, d.parameter_name) for d in result.dropped_values] == [(result.node_names[0], "text")]
        [warning] = _warnings(result)
        assert "paste 1 nodes" in warning
        assert f"parameter 'text' on node '{result.node_names[0]}'" in warning

    def test_paste_of_a_readable_copy_has_no_warning(self, engine: Engine) -> None:
        _create_flow(engine, "flow_a")
        _create_node(engine, "node_a")
        _set_value(engine, "node_a", "text", "hello")
        copied = engine.handle_request(SerializeSelectedNodesToCommandsRequest(nodes_to_serialize=[["node_a", "0"]]))
        assert isinstance(copied, SerializeSelectedNodesToCommandsResultSuccess)
        assert json.loads(next(iter(copied.pickled_values.values()))) == "hello"

        result = engine.handle_request(
            DeserializeSelectedNodesFromCommandsRequest(
                deserialize_commands=copied.serialized_selected_node_commands, pickled_values=copied.pickled_values
            )
        )

        assert isinstance(result, DeserializeSelectedNodesFromCommandsResultSuccess)
        assert result.dropped_values == []
        assert _warnings(result) == []

    def test_duplicate_reports_the_values_it_left_out(self, engine: Engine) -> None:
        _create_flow(engine, "flow_a")
        _create_node(engine, "node_a")
        _set_value(engine, "node_a", "text", _Opaque())

        result = engine.handle_request(DuplicateSelectedNodesRequest(nodes_to_duplicate=[["node_a", "0"]]))

        assert isinstance(result, DuplicateSelectedNodesResultSuccess)
        [warning] = _warnings(result)
        assert "duplicate 1 nodes" in warning
        assert "parameter 'text' on node 'node_a'" in warning
