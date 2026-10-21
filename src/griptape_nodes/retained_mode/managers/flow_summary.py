"""Build the compact, model-facing view of a flow that GetFlowSummaryRequest returns."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from griptape_nodes.common.parameter_hydration import hydrate_value
from griptape_nodes.exe_types.core_types import (
    Parameter,
    ParameterContainer,
    ParameterList,
    ParameterMode,
    ParameterTypeBuiltin,
)
from griptape_nodes.exe_types.local_objects import is_reference
from griptape_nodes.exe_types.node_groups import BaseNodeGroup
from griptape_nodes.retained_mode.events.flow_events import FlowSummaryConnection, FlowSummaryNode
from griptape_nodes.serialization.values import TYPE_KEY, VALUE_KEY, Unencodable, try_encode, untag, value_key

if TYPE_CHECKING:
    from collections.abc import Iterator

    from griptape_nodes.exe_types.connections import Connections
    from griptape_nodes.exe_types.flow import ControlFlow
    from griptape_nodes.exe_types.node_types import BaseNode
    from griptape_nodes.serialization.values import JsonValue

# Long enough for a typical prompt or path, short enough that a pasted document doesn't fill the summary.
MAX_VALUE_TEXT_LENGTH = 200
# Items shown per list or dict before the rest are counted instead.
MAX_VALUE_ITEMS = 20
# Containers nested deeper than this are shown by type and size only.
_MAX_VALUE_DEPTH = 2

_HELD_OBJECT_PLACEHOLDER = "<held object>"


@dataclass
class SummarizedFlow:
    """The nodes and connections of one summarized flow."""

    nodes: list[FlowSummaryNode]
    connections: list[FlowSummaryConnection]


def summarize_flow(flow: ControlFlow, connections: Connections) -> SummarizedFlow:
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
            FlowSummaryConnection(
                source=f"{connection.source_node.name}.{connection.source_parameter.name}",
                target=f"{connection.target_node.name}.{connection.target_parameter.name}",
            )
        )
    return SummarizedFlow(nodes=nodes, connections=flow_connections)


def summarize_value(value: Any) -> Any:
    """Shorten a parameter value to what a model needs to see.

    The value is encoded the way a workflow saves it. Numbers, booleans, and short text are
    returned as is. Long text is cut to ``MAX_VALUE_TEXT_LENGTH`` characters, and lists and dicts
    show their first ``MAX_VALUE_ITEMS`` entries. An object with fields, such as an artifact,
    becomes ``"<ClassName>"``, and so does anything with no plain-data form.
    """
    if is_reference(value):
        return _HELD_OBJECT_PLACEHOLDER
    # An artifact set over a JSON boundary arrives as an untagged dict; rebuild it so it reads as one.
    encoded = try_encode(hydrate_value(value))
    if isinstance(encoded, Unencodable):
        return f"<{type(value).__name__}>"
    return _summarize_encoded(encoded, 0)


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


def _summarize_node(node: BaseNode, connections: Connections) -> FlowSummaryNode:
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

    return FlowSummaryNode(
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
    encoded_value = try_encode(value)
    encoded_default = try_encode(default)
    if isinstance(encoded_value, Unencodable) or isinstance(encoded_default, Unencodable):
        return False
    return value_key(encoded_value) == value_key(encoded_default)


def _is_unset(value: Any) -> bool:
    if value is None:
        return True
    return isinstance(value, (str, list, tuple, dict)) and len(value) == 0


def _summarize_encoded(data: JsonValue, depth: int) -> Any:  # noqa: PLR0911 - one return per kind of value
    if isinstance(data, str):
        return _summarize_text(data)
    if isinstance(data, list):
        if depth >= _MAX_VALUE_DEPTH:
            return f"<list of {len(data)} items>"
        return _summarize_items(data, depth)
    if not isinstance(data, dict):
        return data
    if is_reference(data):
        return _HELD_OBJECT_PLACEHOLDER
    if TYPE_KEY in data:
        return _summarize_tagged(data, depth)
    if depth >= _MAX_VALUE_DEPTH:
        return f"<dict of {len(data)} keys>"
    return _summarize_mapping(data, depth)


def _summarize_tagged(data: dict[str, JsonValue], depth: int) -> Any:
    """An object with fields shows as its class name, a value with a simple form (tuple, enum, path, date) as that form."""
    class_name = str(data[TYPE_KEY]).rpartition(":")[2].rpartition(".")[2]
    if VALUE_KEY not in data:
        return f"<{class_name}>"
    untagged = untag(data)
    if isinstance(untagged, dict) and TYPE_KEY in untagged:
        # Still tagged: a dict whose keys only stay apart that way, such as one with its own "$type" key.
        return f"<{class_name}>"
    return _summarize_encoded(untagged, depth)


def _summarize_text(text: str) -> str:
    if len(text) <= MAX_VALUE_TEXT_LENGTH:
        return text
    return f"{text[: MAX_VALUE_TEXT_LENGTH - 3]}... ({len(text)} characters)"


def _summarize_items(items: list[JsonValue], depth: int) -> list[Any]:
    summarized = [_summarize_encoded(item, depth + 1) for item in items[:MAX_VALUE_ITEMS]]
    hidden_count = len(items) - MAX_VALUE_ITEMS
    if hidden_count > 0:
        summarized.append(f"... ({hidden_count} more items)")
    return summarized


def _summarize_mapping(mapping: dict[str, JsonValue], depth: int) -> dict[str, Any]:
    keys = list(mapping)
    summarized = {key: _summarize_encoded(mapping[key], depth + 1) for key in keys[:MAX_VALUE_ITEMS]}
    hidden_count = len(keys) - MAX_VALUE_ITEMS
    if hidden_count > 0:
        summarized["..."] = f"{hidden_count} more keys"
    return summarized
