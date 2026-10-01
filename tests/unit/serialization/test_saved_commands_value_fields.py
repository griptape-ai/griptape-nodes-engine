"""No field of the commands that are saved, copied, or embedded in an image is a DisplayValue, except placeholders.

A DisplayValue sends a value with no plain-data form as its text. In saved commands that text would
be read back as the value, so every such field must be a strict Value.
"""

from __future__ import annotations

import dataclasses
import typing
from typing import TYPE_CHECKING, Any

import attrs
import pytest

# Loads the event modules in the order the engine does, so their deferred models finish building.
import griptape_nodes.retained_mode.engine  # noqa: F401
from griptape_nodes.retained_mode.events.flow_events import (
    CreateFlowRequest,
    CreateFlowResultSuccess,
    SerializedFlowCommands,
    SerializeFlowToCommandsRequest,
    SerializeFlowToCommandsResultSuccess,
)
from griptape_nodes.retained_mode.events.node_events import (
    CreateNodeRequest,
    CreateNodeResultSuccess,
    SerializedNodeCommands,
    SerializedSelectedNodesCommands,
)
from griptape_nodes.retained_mode.events.object_events import ClearAllObjectStateRequest
from griptape_nodes.retained_mode.events.parameter_events import (
    AddParameterToNodeRequest,
    SetParameterValueRequest,
    SetParameterValueResultSuccess,
)
from griptape_nodes.retained_mode.variable_types import FlowVariable
from griptape_nodes.serialization.commands import ALLOWED_ELEMENT_MODIFICATION_COMMAND_TYPES
from griptape_nodes.serialization.values import DisplayValue

if TYPE_CHECKING:
    from collections.abc import Generator

    from griptape_nodes.retained_mode.engine import Engine

# A node's element_modification_commands is typed to hold any request, so its real contents are
# named here instead of found in the field's type.
_ROOTS: list[type] = [
    SerializedNodeCommands,
    SerializedFlowCommands,
    SerializedSelectedNodesCommands,
    *ALLOWED_ELEMENT_MODIFICATION_COMMAND_TYPES,
]


# Placeholders: in saved commands these are always None, and the value pool holds the real value.
# They stay DisplayValue because live requests send them to the editor. Exact fields, so any other
# DisplayValue field still fails.
_PLACEHOLDER_FIELDS = frozenset({"SetParameterValueRequest.value", "CreateVariableRequest.value"})


def _has_fields(cls: object) -> bool:
    return isinstance(cls, type) and (dataclasses.is_dataclass(cls) or attrs.has(cls))


def _annotations_in(annotation: Any) -> list[Any]:
    """The annotation and every annotation inside it, such as a list's item type."""
    found = [annotation]
    for argument in typing.get_args(annotation):
        found.extend(_annotations_in(argument))
    return found


def _reachable_fields() -> dict[str, Any]:
    """Map 'Class.field' to its annotation, for every field reachable from the roots."""
    fields: dict[str, Any] = {}
    pending = list(_ROOTS)
    seen: set[type] = set()
    while pending:
        cls = pending.pop()
        if cls in seen:
            continue
        seen.add(cls)
        for name, annotation in typing.get_type_hints(cls).items():
            fields[f"{cls.__qualname__}.{name}"] = annotation
            pending.extend(inner for inner in _annotations_in(annotation) if _has_fields(inner))
    return fields


def test_reachable_fields_are_found() -> None:
    """Guards the walk, so a broken walk cannot pass by finding nothing."""
    fields = _reachable_fields()

    assert fields.keys() >= _PLACEHOLDER_FIELDS
    assert "AddParameterToNodeRequest.default_value" in fields
    assert "CreateNodeRequest.node_name" in fields
    assert AddParameterToNodeRequest in _ROOTS


@pytest.mark.parametrize("field_name", sorted(set(_reachable_fields()) - _PLACEHOLDER_FIELDS))
def test_field_is_not_a_display_value(field_name: str) -> None:
    """A field of saved commands sends a value with no plain-data form as its text."""
    annotation = _reachable_fields()[field_name]

    assert DisplayValue not in _annotations_in(annotation)


@pytest.mark.parametrize("field_name", sorted(_PLACEHOLDER_FIELDS))
def test_placeholder_field_is_a_display_value(field_name: str) -> None:
    """Keeps the allowlist honest: a field made strict must leave it."""
    assert DisplayValue in _annotations_in(_reachable_fields()[field_name])


@pytest.fixture
def clean_object_state(engine: Engine) -> Generator[None, None, None]:
    """Clear all object state around a test so leftover flows never bleed across tests."""
    engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
    try:
        yield
    finally:
        engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))


@pytest.mark.usefixtures("clean_object_state")
def test_saved_placeholders_are_none(engine: Engine) -> None:
    """The reason the placeholder fields may stay DisplayValue: a save never puts a value in them."""
    engine.context_manager.push_workflow(workflow_name="wf_placeholders")
    flow = engine.handle_request(CreateFlowRequest(parent_flow_name=None, flow_name="flow_a", set_as_new_context=True))
    assert isinstance(flow, CreateFlowResultSuccess)
    node = engine.handle_request(CreateNodeRequest(node_type="TestNodeA", node_name="node_a"))
    assert isinstance(node, CreateNodeResultSuccess)
    set_value = engine.handle_request(
        SetParameterValueRequest(node_name="node_a", parameter_name="text", value="hello", initial_setup=True)
    )
    assert isinstance(set_value, SetParameterValueResultSuccess)

    result = engine.handle_request(SerializeFlowToCommandsRequest(flow_name="flow_a"))
    assert isinstance(result, SerializeFlowToCommandsResultSuccess)
    commands = result.serialized_flow_commands
    variable_command = engine.flow_manager._build_serialized_variable_command(
        FlowVariable(name="v", owning_flow_name="flow_a", type="str", value="text"), {}
    )

    set_commands = [command for commands_ in commands.set_parameter_value_commands.values() for command in commands_]
    assert set_commands
    assert all(command.set_parameter_value_command.value is None for command in set_commands)
    assert variable_command.create_variable_command.value is None
