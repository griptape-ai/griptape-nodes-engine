"""A parameter default with no plain-data form is left out of saved commands instead of saved as text."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pytest

from griptape_nodes.retained_mode.events.flow_events import CreateFlowRequest, CreateFlowResultSuccess
from griptape_nodes.retained_mode.events.node_events import (
    CreateNodeRequest,
    CreateNodeResultSuccess,
    SerializeNodeToCommandsRequest,
    SerializeNodeToCommandsResultSuccess,
)
from griptape_nodes.retained_mode.events.object_events import ClearAllObjectStateRequest
from griptape_nodes.retained_mode.events.parameter_events import (
    AddParameterToNodeRequest,
    AddParameterToNodeResultSuccess,
)
from griptape_nodes.serialization.values import encodable_default

if TYPE_CHECKING:
    from collections.abc import Generator

    from griptape_nodes.retained_mode.engine import Engine


class _Opaque:
    """Has no plain-data form."""


@pytest.fixture(autouse=True)
def clean_object_state(engine: Engine) -> Generator[None, None, None]:
    """Clear all object state around a test so leftover flows never bleed across tests."""
    engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
    engine.context_manager.push_workflow(workflow_name="wf_default_value_saving")
    try:
        yield
    finally:
        engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))


def _node_with_default(engine: Engine, default_value: object) -> None:
    flow = engine.handle_request(CreateFlowRequest(parent_flow_name=None, flow_name="flow_a", set_as_new_context=True))
    assert isinstance(flow, CreateFlowResultSuccess), flow
    node = engine.handle_request(CreateNodeRequest(node_type="TestNodeA", node_name="node_a"))
    assert isinstance(node, CreateNodeResultSuccess), node
    added = engine.handle_request(
        AddParameterToNodeRequest(
            node_name="node_a", parameter_name="extra", default_value=default_value, type="any", tooltip="t"
        )
    )
    assert isinstance(added, AddParameterToNodeResultSuccess), added


def _saved_defaults(engine: Engine) -> list[object]:
    result = engine.handle_request(SerializeNodeToCommandsRequest(node_name="node_a"))
    assert isinstance(result, SerializeNodeToCommandsResultSuccess)
    return [
        command.default_value
        for command in result.serialized_node_commands.element_modification_commands
        if isinstance(command, AddParameterToNodeRequest) and command.parameter_name == "extra"
    ]


class TestSerializeNodeDefaults:
    def test_a_default_with_no_plain_data_form_is_left_out(
        self, engine: Engine, caplog: pytest.LogCaptureFixture
    ) -> None:
        _node_with_default(engine, _Opaque())
        caplog.set_level(logging.WARNING, logger="griptape_nodes")

        assert _saved_defaults(engine) == [None]
        assert "default value of parameter 'extra' on node 'node_a'" in caplog.text

    def test_a_plain_default_is_kept(self, engine: Engine) -> None:
        _node_with_default(engine, {"a": 1})

        assert _saved_defaults(engine) == [{"a": 1}]


class TestEncodableDefault:
    def test_returns_an_encodable_default_unchanged(self) -> None:
        default = {"a": (1, 2)}

        assert encodable_default(default, "n", "p") is default

    def test_returns_none_and_warns_for_a_default_with_no_plain_data_form(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.WARNING, logger="griptape_nodes")

        assert encodable_default(_Opaque(), "n", "p") is None
        assert "parameter 'p' on node 'n'" in caplog.text
