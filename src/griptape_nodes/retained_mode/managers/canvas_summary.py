"""Build the compact, model-facing view of a flow that GetCanvasSummaryRequest returns."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from griptape_nodes.exe_types.core_types import (
    Parameter,
    ParameterContainer,
    ParameterList,
    ParameterMode,
    ParameterTypeBuiltin,
)
from griptape_nodes.exe_types.local_objects import is_reference
from griptape_nodes.exe_types.node_groups import BaseNodeGroup
from griptape_nodes.retained_mode.events.flow_events import CanvasSummaryConnection, CanvasSummaryNode

if TYPE_CHECKING:
    from collections.abc import Iterator

    from griptape_nodes.exe_types.connections import Connections
    from griptape_nodes.exe_types.flow import ControlFlow
    from griptape_nodes.exe_types.node_types import BaseNode

# Long enough for a typical prompt or path, short enough that a pasted document doesn't fill the summary.
MAX_VALUE_TEXT_LENGTH = 200
# Items shown per list or dict before the rest are counted instead.
MAX_VALUE_ITEMS = 20
# Containers nested deeper than this are shown by type and size only.
_MAX_VALUE_DEPTH = 2

# A serialized object whose top-level "type" ends in one of these is runtime state, not something the user typed.
_RUNTIME_OBJECT_TYPE_SUFFIXES = ("Artifact", "Agent")

_HELD_OBJECT_PLACEHOLDER = "<held object>"


@dataclass
class FlowSummary:
    """The nodes and connections of one summarized flow."""

    nodes: list[CanvasSummaryNode]
    connections: list[CanvasSummaryConnection]


def summarize_flow(flow: ControlFlow, connections: Connections) -> FlowSummary:
    """Summarize ``flow``'s nodes, including the members of its node groups, and the connections between them."""
    nodes = []
    node_names = set()
    for node in _walk_nodes(flow.nodes):
        if node.name in node_names:
            continue
        node_names.add(node.name)
        nodes.append(_summarize_node(node, connections))
    flow_connections = []
    for connection in connections.connections.values():
        if connection.source_node.name not in node_names or connection.target_node.name not in node_names:
            continue
        flow_connections.append(
            CanvasSummaryConnection(
                source=f"{connection.source_node.name}.{connection.source_parameter.name}",
                target=f"{connection.target_node.name}.{connection.target_parameter.name}",
            )
        )
    return FlowSummary(nodes=nodes, connections=flow_connections)


def summarize_value(value: Any, depth: int = 0) -> Any:  # noqa: PLR0911 - one return per kind of value
    """Shorten a parameter value to what a model needs to see.

    Numbers, booleans, and short text are returned as is. Long text is cut to
    ``MAX_VALUE_TEXT_LENGTH`` characters, lists and dicts show their first ``MAX_VALUE_ITEMS``
    entries, and anything else (artifacts, agents, held objects) becomes ``"<TypeName>"``.
    """
    if value is None or isinstance(value, (int, float, bool)):
        return value
    if isinstance(value, str):
        return _summarize_text(value)
    if is_reference(value):
        return _HELD_OBJECT_PLACEHOLDER
    if isinstance(value, (list, tuple)):
        if depth >= _MAX_VALUE_DEPTH:
            return f"<list of {len(value)} items>"
        return _summarize_items(list(value), depth)
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        runtime_type = _runtime_object_type(value)
        if runtime_type is not None:
            return f"<{runtime_type}>"
        if depth >= _MAX_VALUE_DEPTH:
            return f"<dict of {len(value)} keys>"
        return _summarize_mapping(value, depth)
    return f"<{type(value).__name__}>"


def _walk_nodes(nodes: dict[str, BaseNode]) -> Iterator[BaseNode]:
    """Yield each node, a group's members right after the group.

    Some groups move their members into a subflow and others leave them in the flow as well,
    so a member whose group is at the same level is skipped there and yielded under its group.
    """
    for node in nodes.values():
        if node.parent_group is not None and node.parent_group.name in nodes:
            continue
        yield node
        if isinstance(node, BaseNodeGroup):
            yield from _walk_nodes(node.nodes)


def _summarize_node(node: BaseNode, connections: Connections) -> CanvasSummaryNode:
    parameters = node.parameters
    containers = {parameter.name: parameter for parameter in parameters if isinstance(parameter, ParameterContainer)}
    connected_names = set(connections.incoming_index.get(node.name, {}))

    values: dict[str, Any] = {}
    for parameter in parameters:
        if parameter.parent_container_name in containers:
            continue
        if parameter.name in connected_names:
            continue
        if not _holds_user_setting(parameter):
            continue
        if isinstance(parameter, ParameterList):
            value = _unconnected_item_values(node, parameter, connected_names)
        elif isinstance(parameter, ParameterContainer) and _has_connected_child(parameter, connected_names):
            continue
        else:
            value = node._get_raw_parameter_value(parameter.name)
        if _same_value(value, parameter.default_value):
            continue
        values[parameter.name] = summarize_value(value)

    position = node.metadata.get("position")
    position_summary = None
    if isinstance(position, dict) and "x" in position and "y" in position:
        position_summary = {"x": position["x"], "y": position["y"]}

    multi_inputs = [
        parameter.name
        for parameter in parameters
        if isinstance(parameter, ParameterList) and ParameterMode.INPUT in parameter.allowed_modes
    ]

    group_name = None
    if node.parent_group is not None:
        group_name = node.parent_group.name

    return CanvasSummaryNode(
        name=node.name,
        type=node.metadata.get("node_type", type(node).__name__),
        library=node.metadata.get("library", ""),
        group=group_name,
        values=values,
        multi_inputs=multi_inputs,
        position=position_summary,
    )


def _holds_user_setting(parameter: Parameter) -> bool:
    """Whether the parameter's value is something a user sets, rather than an output or run result."""
    if parameter.type == ParameterTypeBuiltin.CONTROL_TYPE.value:
        return False
    if parameter.private:
        return False
    if not parameter.settable:
        return False
    modes = parameter.allowed_modes
    return ParameterMode.INPUT in modes or ParameterMode.PROPERTY in modes


def _unconnected_item_values(
    node: BaseNode, parameter_list: ParameterList, connected_names: set[str]
) -> dict[str, Any]:
    """The values the user set on a list's items, by item name, leaving out items fed by a connection."""
    values = {}
    for item in parameter_list.get_child_parameters():
        if item.name in connected_names:
            continue
        value = node._get_raw_parameter_value(item.name)
        if value is not None:
            values[item.name] = value
    return values


def _has_connected_child(container: ParameterContainer, connected_names: set[str]) -> bool:
    return any(child.name in connected_names for child in container.find_elements_by_type(Parameter))


def _same_value(value: Any, default: Any) -> bool:
    """Whether ``value`` is the default, treating None, empty text, and empty containers as the same unset value."""
    if value is default:
        return True
    if _is_unset(value) and _is_unset(default):
        return True
    value_json = _to_json(value)
    if value_json is None:
        return False
    return value_json == _to_json(default)


def _is_unset(value: Any) -> bool:
    if value is None:
        return True
    return isinstance(value, (str, list, tuple, dict)) and len(value) == 0


def _summarize_text(text: str) -> str:
    runtime_type = _serialized_runtime_object_type(text)
    if runtime_type is not None:
        return f"<{runtime_type}>"
    if len(text) <= MAX_VALUE_TEXT_LENGTH:
        return text
    return f"{text[: MAX_VALUE_TEXT_LENGTH - 3]}... ({len(text)} characters)"


def _serialized_runtime_object_type(text: str) -> str | None:
    """The type name when ``text`` is a serialized artifact or agent (or a list of them), otherwise None.

    This applies to short text too: JSON whose top-level "type" is an artifact or agent is shown by type.
    """
    stripped = text.lstrip()
    if not stripped.startswith(("{", "[")):
        return None
    try:
        parsed = json.loads(stripped)
    except ValueError:
        return None
    if not isinstance(parsed, list):
        return _runtime_object_type(parsed)
    if not parsed:
        return None
    type_name = _runtime_object_type(parsed[0])
    if type_name is None:
        return None
    return f"{type_name} list"


def _runtime_object_type(value: Any) -> str | None:
    """The type name when ``value`` is a dict whose top-level "type" names an artifact or agent, otherwise None."""
    if not isinstance(value, dict):
        return None
    type_name = value.get("type")
    if not isinstance(type_name, str) or not type_name.endswith(_RUNTIME_OBJECT_TYPE_SUFFIXES):
        return None
    return type_name


def _summarize_items(items: list[Any], depth: int) -> list[Any]:
    summarized = [summarize_value(item, depth + 1) for item in items[:MAX_VALUE_ITEMS]]
    hidden_count = len(items) - MAX_VALUE_ITEMS
    if hidden_count > 0:
        summarized.append(f"... ({hidden_count} more items)")
    return summarized


def _summarize_mapping(mapping: dict[str, Any], depth: int) -> dict[str, Any]:
    keys = list(mapping)
    summarized = {key: summarize_value(mapping[key], depth + 1) for key in keys[:MAX_VALUE_ITEMS]}
    hidden_count = len(keys) - MAX_VALUE_ITEMS
    if hidden_count > 0:
        summarized["..."] = f"{hidden_count} more keys"
    return summarized


def _to_json(value: Any) -> str | None:
    """``value`` as sorted JSON, or None when it holds anything but JSON-compatible primitives."""
    if not _is_json_compatible(value):
        return None
    return json.dumps(value, sort_keys=True)


def _is_json_compatible(value: Any) -> bool:
    if value is None or isinstance(value, (str, int, float, bool)):
        return True
    if isinstance(value, (list, tuple)):
        return all(_is_json_compatible(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(key, str) and _is_json_compatible(item) for key, item in value.items())
    return False
