"""Turn flow and node commands into JSON-ready data, and back, for image metadata and copied nodes.

The data follows the commands' fields, so it names no engine classes: the reader knows which class
each field holds. A field that can hold any request names it by its registered request name, and
parameter values keep the tagged form of ``values.py``, because a value can be of any type:

    {"version": 1, "commands": {"serialized_node_commands": [{"create_node_command": {...}}], ...}}
"""

from __future__ import annotations

import dataclasses
import types
import typing
from typing import Any

from cattrs import BaseValidationError, transform_error

from griptape_nodes.retained_mode.events.base_events import RequestPayload
from griptape_nodes.retained_mode.events.flow_events import SerializedFlowCommands
from griptape_nodes.retained_mode.events.node_events import SerializedSelectedNodesCommands
from griptape_nodes.retained_mode.events.parameter_events import (
    AddParameterGroupToNodeRequest,
    AddParameterToNodeRequest,
    AlterParameterDetailsRequest,
    AlterParameterGroupDetailsRequest,
)
from griptape_nodes.serialization.converter import converter
from griptape_nodes.serialization.values import DisplayValue, JsonValue, Value, encode_value

VERSION = 1
"""The layout ``encode_commands`` writes. Raise it when a change to the commands' fields would
stop an earlier engine's data from reading back, and teach ``decode_commands`` the earlier one."""

# Taken once the event modules have registered their hooks on the event converter.
_converter = converter.copy()
# Saved commands must come back as they were, so a value with no plain-data form fails the save
# instead of being written as its text, the way it is shown in the editor. Tagged even inside
# `for_clients`, since a client holds commands only to send them back.
_converter.register_unstructure_hook(Value, encode_value)
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


def check_request_fields(commands: SerializedFlowCommands | SerializedSelectedNodesCommands) -> None:
    """Refuse decoded commands holding a request anywhere serialization would not have put it.

    Each field must hold exactly the request type it declares. Pickle ignores declared types, so
    without this a payload could put any request in, say, a node's lock command, which loading runs.
    ``element_modification_commands`` declares any request, so it is held to the types in
    ``ALLOWED_ELEMENT_MODIFICATION_COMMAND_TYPES``.

    Raises:
        CommandsFormatError: A field holds a request of another type.
    """
    _check_dataclass(commands)


def _check_dataclass(instance: Any) -> None:
    field_types = _field_types(type(instance))
    for field in dataclasses.fields(instance):
        declared = field_types[field.name]
        _check_value(getattr(instance, field.name), declared, f"{type(instance).__qualname__}.{field.name}")


def _check_value(value: Any, declared: Any, where: str) -> None:
    declared = _without_new_types(declared)
    origin = typing.get_origin(declared)
    if origin in (typing.Union, types.UnionType):
        _check_union_member(value, declared, where)
        return
    if origin in (list, set, frozenset, tuple, dict):
        _check_items(value, declared, where)
        return
    if declared is RequestPayload:
        if type(value) not in ALLOWED_ELEMENT_MODIFICATION_COMMAND_TYPES:
            _refuse(value, where)
        return
    if isinstance(declared, type) and dataclasses.is_dataclass(declared):
        if type(value) is not declared:
            _refuse(value, where)
        _check_dataclass(value)
        return
    if isinstance(value, RequestPayload):
        # A request where the declaration names no request type at all.
        _refuse(value, where)


def _check_union_member(value: Any, declared: Any, where: str) -> None:
    if value is None:
        return
    matching = [option for option in typing.get_args(declared) if _is_instance_of(value, option)]
    if not matching:
        _refuse(value, where)
    _check_value(value, matching[0], where)


def _check_items(value: Any, declared: Any, where: str) -> None:
    arguments = typing.get_args(declared)
    if isinstance(value, dict):
        item_type = arguments[1] if len(arguments) > 1 else Any
        items = value.values()
    elif isinstance(value, list | set | frozenset | tuple):
        item_type = arguments[0] if arguments else Any
        items = value
    else:
        return
    for item in items:
        _check_value(item, item_type, where)


def _is_instance_of(value: Any, declared: Any) -> bool:
    declared = _without_new_types(declared)
    target = typing.get_origin(declared) or declared
    if not isinstance(target, type):
        return True
    if dataclasses.is_dataclass(target):
        return type(value) is target
    return isinstance(value, target)


def _without_new_types(declared: Any) -> Any:
    while hasattr(declared, "__supertype__"):
        declared = declared.__supertype__
    return declared


def _field_types(cls: type) -> dict[str, Any]:
    if cls not in _FIELD_TYPES:
        _FIELD_TYPES[cls] = typing.get_type_hints(cls)
    return _FIELD_TYPES[cls]


def _refuse(value: Any, where: str) -> typing.NoReturn:
    msg = f"'{where}' holds a '{type(value).__qualname__}', which Griptape Nodes never saves there"
    raise CommandsFormatError(msg)


_FIELD_TYPES: dict[type, dict[str, Any]] = {}


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
    check_request_fields(commands)
    return commands
