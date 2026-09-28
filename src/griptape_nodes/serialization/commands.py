"""Turn flow and node commands into JSON-ready data, and back, for image metadata and copied nodes.

The data follows the commands' fields, so it names no engine classes: the reader knows which class
each field holds. A field that can hold any request names it by its registered request name, and
parameter values keep the tagged form of ``values.py``, because a value can be of any type:

    {"version": 1, "commands": {"serialized_node_commands": [{"create_node_command": {...}}], ...}}
"""

from __future__ import annotations

from typing import Any

from cattrs import BaseValidationError, transform_error

from griptape_nodes.retained_mode.events.flow_events import SerializedFlowCommands
from griptape_nodes.retained_mode.events.node_events import SerializedNodeCommands, SerializedSelectedNodesCommands
from griptape_nodes.retained_mode.events.parameter_events import (
    AddParameterGroupToNodeRequest,
    AddParameterToNodeRequest,
    AlterParameterDetailsRequest,
    AlterParameterGroupDetailsRequest,
)
from griptape_nodes.serialization.converter import converter
from griptape_nodes.serialization.values import DisplayValue, JsonValue, encode_value

VERSION = 1
"""The layout ``encode_commands`` writes. Raise it when a change to the commands' fields would
stop an earlier engine's data from reading back, and teach ``decode_commands`` the earlier one."""

# Taken once the event modules have registered their hooks on the event converter.
_converter = converter.copy()
# Saved commands must come back as they were, so a value with no plain-data form fails the save
# instead of being written as its text, the way it is shown in the editor.
_converter.register_unstructure_hook(DisplayValue, encode_value)

# The only request types on_serialize_node_to_commands puts into element_modification_commands
# (see node_manager.py) or that ErrorProxyNode.record_initialization_request replays. That field
# is typed to hold any registered request, so a decoded payload naming another one, such as a
# request that runs code or reads/writes files, must be refused rather than run.
ALLOWED_ELEMENT_MODIFICATION_COMMAND_TYPES: frozenset[type] = frozenset(
    {
        AddParameterToNodeRequest,
        AlterParameterDetailsRequest,
        AddParameterGroupToNodeRequest,
        AlterParameterGroupDetailsRequest,
    }
)


class CommandsFormatError(Exception):
    """Data is not commands this version can read. The message completes 'Failed because ...'."""


def check_element_modification_commands(commands: SerializedFlowCommands | SerializedSelectedNodesCommands) -> None:
    """Refuse decoded commands naming an element command type serialization never writes.

    Raises:
        CommandsFormatError: A node's element_modification_commands holds such a type.
    """
    if isinstance(commands, SerializedFlowCommands):
        _check_flow_commands(commands)
    else:
        for node_commands in commands.serialized_node_commands:
            _check_node_commands(node_commands)


def _check_flow_commands(flow_commands: SerializedFlowCommands) -> None:
    for node_commands in flow_commands.serialized_node_commands:
        _check_node_commands(node_commands)
    for sub_flow_commands in flow_commands.sub_flows_commands:
        _check_flow_commands(sub_flow_commands)


def _check_node_commands(node_commands: SerializedNodeCommands) -> None:
    for element_command in node_commands.element_modification_commands:
        if type(element_command) not in ALLOWED_ELEMENT_MODIFICATION_COMMAND_TYPES:
            msg = (
                f"a node's element command is a '{type(element_command).__qualname__}', which "
                "Griptape Nodes never saves there"
            )
            raise CommandsFormatError(msg)


def encode_commands(commands: SerializedFlowCommands | SerializedSelectedNodesCommands) -> dict[str, JsonValue]:
    """Return ``commands`` as data that ``dump_json`` writes and ``decode_commands`` reads back.

    Raises:
        ValueEncodeError: A value in ``commands`` has no plain-data form. An object in a field the
            converter has no hook for passes through, and fails in ``dump_json`` instead.
    """
    return {"version": VERSION, "commands": _converter.unstructure(commands)}


def decode_commands[T: SerializedFlowCommands | SerializedSelectedNodesCommands](
    data: Any, commands_type: type[T]
) -> T:
    """Rebuild the ``commands_type`` commands ``encode_commands`` produced ``data`` from.

    Parameter values in a flow's value pool stay encoded, so each use can decode its own copy.

    Raises:
        CommandsFormatError: ``data`` is not such commands.
    """
    if not isinstance(data, dict) or "version" not in data or "commands" not in data:
        msg = "the data is not in a layout Griptape Nodes writes"
        raise CommandsFormatError(msg)
    version = data["version"]
    if type(version) is not int or version < 1:
        msg = f"the data has an unknown layout version, {version!r}"
        raise CommandsFormatError(msg)
    if version > VERSION:
        msg = "the data was saved by a later version of Griptape Nodes"
        raise CommandsFormatError(msg)
    try:
        commands = _converter.structure(data["commands"], commands_type)
    except BaseValidationError as error:
        problems = "; ".join(transform_error(error))
        msg = f"the data is incomplete or damaged ({problems})"
        raise CommandsFormatError(msg) from error
    check_element_modification_commands(commands)
    return commands
