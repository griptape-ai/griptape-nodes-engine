"""Type-adaptive wildcard pins: parameters that take the type of the first wire connected to them.

A node opts in per parameter with ``Parameter(..., type="any", adaptive_type_group="T")``. Every
parameter on one node that names the same group shares one state:

* untyped: no wire has given the group a type yet. Its parameters accept a wire of any type.
* typed(T): the first wire whose other end has a concrete type gave the group the type ``T``. Every
  parameter in the group, inputs and outputs, now has ``T`` as its type, and a wire that cannot carry
  ``T`` is rejected.

The group goes back to untyped, and its parameters to the types they were declared with, when the
last wire connected to any parameter in the group is removed.

Typing a group never silently breaks a wire it already has. A group can hold wires while untyped,
when the other end of each is untyped too (``any``). If giving the group a type would leave one of
those wires carrying a type its target cannot accept, the new connection is rejected and the
failure names the wire in the way.

A type only flows across the one node. An adaptive output that is wired into another node's
untyped adaptive input does not retype that node when its own group is typed later.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from griptape_nodes.exe_types.elements.parameter_types import ParameterTypeBuiltin, accepts_incoming_type

if TYPE_CHECKING:
    from griptape_nodes.exe_types.connections import Connections
    from griptape_nodes.exe_types.core_types import Parameter
    from griptape_nodes.exe_types.node_types import BaseNode, Connection

# Types that say nothing about what a wire carries, so they never give a group a type.
_NON_CONCRETE_TYPES = frozenset(
    {
        ParameterTypeBuiltin.ANY.value,
        ParameterTypeBuiltin.ALL.value,
        ParameterTypeBuiltin.NONE.value,
        ParameterTypeBuiltin.CONTROL_TYPE.value,
    }
)


@dataclass(frozen=True)
class AdaptiveTyping:
    """A group that a connection gives a type to, applied once the connection exists."""

    node: BaseNode
    group: str
    new_type: str


@dataclass
class AdaptiveConnectionPlan:
    """What adaptive parameters do to a connection that is about to be made.

    ``source_type`` is the type the wire carries once the source's group has its type. When
    ``target_accepts_any`` is set, the target is an untyped adaptive input and takes whatever arrives;
    otherwise the target's own input types decide. ``typings`` are the groups the connection types.
    """

    source_type: str
    target_accepts_any: bool = False
    typings: list[AdaptiveTyping] = field(default_factory=list)


@dataclass(frozen=True)
class WireCheck:
    """One existing wire to vet against a type its group is about to take."""

    connection: Connection
    # Whether the adaptive parameter is the wire's target (True) or its source (False).
    adaptive_end_is_target: bool


def is_concrete_type(type_name: str | None) -> bool:
    """Whether a type says what a wire carries, so it can give an adaptive group its type."""
    if type_name is None:
        return False
    return type_name.lower() not in _NON_CONCRETE_TYPES


def adaptive_group_parameters(node: BaseNode, group: str) -> list[Parameter]:
    """Every parameter on ``node`` that belongs to the adaptive group ``group``."""
    return [parameter for parameter in node.parameters if parameter.adaptive_type_group == group]


def group_connections(
    node: BaseNode, group: str, connections: Connections, excluding: Connection | None = None
) -> list[WireCheck]:
    """Every wire on a parameter of the group, except ``excluding``."""
    found: list[WireCheck] = []
    for parameter in adaptive_group_parameters(node, group):
        incoming_ids = connections.incoming_index.get(node.name, {}).get(parameter.name, [])
        found.extend(
            WireCheck(connection=connections.connections[connection_id], adaptive_end_is_target=True)
            for connection_id in incoming_ids
            if connections.connections[connection_id] is not excluding
        )
        outgoing_ids = connections.outgoing_index.get(node.name, {}).get(parameter.name, [])
        found.extend(
            WireCheck(connection=connections.connections[connection_id], adaptive_end_is_target=False)
            for connection_id in outgoing_ids
            if connections.connections[connection_id] is not excluding
        )
    return found


def effective_group_type(
    node: BaseNode, group: str, connections: Connections, excluding: Connection | None = None
) -> str | None:
    """The group's type, or None if it is untyped once ``excluding`` is gone.

    ``excluding`` is the connection a new one is about to replace: if it is the group's only wire,
    removing it sends the group back to untyped before the new wire arrives.
    """
    members = adaptive_group_parameters(node, group)
    if not members:
        return None
    resolved = members[0].adaptive_resolved_type
    if resolved is None:
        return None
    if not group_connections(node, group, connections, excluding=excluding):
        return None
    return resolved


def plan_adaptive_connection(  # noqa: PLR0913
    *,
    source_node: BaseNode,
    source_parameter: Parameter,
    target_node: BaseNode,
    target_parameter: Parameter,
    connections: Connections,
    replaced: Connection | None,
) -> AdaptiveConnectionPlan:
    """Work out the types a connection carries and the groups it types, without changing anything."""
    source_group = source_parameter.adaptive_type_group
    target_group = target_parameter.adaptive_type_group

    source_type = source_parameter.output_type
    source_untyped = False
    if source_group is not None:
        source_group_type = effective_group_type(source_node, source_group, connections, excluding=replaced)
        if source_group_type is None:
            source_untyped = True
            source_type = ParameterTypeBuiltin.ANY.value
        else:
            source_type = source_group_type

    target_untyped = False
    target_types = target_parameter.input_types
    if target_group is not None:
        target_group_type = effective_group_type(target_node, target_group, connections, excluding=replaced)
        if target_group_type is None:
            target_untyped = True
            target_types = [ParameterTypeBuiltin.ANY.value]
        else:
            target_types = [target_group_type]

    plan = AdaptiveConnectionPlan(source_type=source_type, target_accepts_any=target_untyped)

    if target_untyped and target_group is not None and is_concrete_type(source_type):
        plan.typings.append(AdaptiveTyping(node=target_node, group=target_group, new_type=source_type))

    if source_untyped and source_group is not None:
        concrete_target_types = [type_name for type_name in target_types if is_concrete_type(type_name)]
        if concrete_target_types:
            new_type = concrete_target_types[0]
            plan.typings.append(AdaptiveTyping(node=source_node, group=source_group, new_type=new_type))
            plan.source_type = new_type

    return plan


def apply_adaptive_type(node: BaseNode, group: str, new_type: str) -> list[Parameter]:
    """Give every parameter in the group the type ``new_type``. Returns the parameters it retyped.

    The type setters queue an ``AlterElementEvent`` for each parameter, so the editor sees the new
    ``type``, ``input_types``, ``output_type``, and ``adaptive_resolved_type``.
    """
    retyped: list[Parameter] = []
    for parameter in adaptive_group_parameters(node, group):
        parameter.type = new_type
        parameter.input_types = [new_type]
        parameter.output_type = new_type
        _report_all_types(parameter)
        if parameter.adaptive_resolved_type != new_type:
            parameter.adaptive_resolved_type = new_type
            parameter.track_change("adaptive_resolved_type", new_type)
            retyped.append(parameter)
    return retyped


def revert_adaptive_type_if_unwired(node: BaseNode, group: str, connections: Connections) -> list[Parameter]:
    """Send the group back to untyped if no wire is left on it. Returns the parameters it reverted."""
    if group_connections(node, group, connections):
        return []
    reverted: list[Parameter] = []
    for parameter in adaptive_group_parameters(node, group):
        if parameter.adaptive_resolved_type is None:
            continue
        parameter.type = parameter._adaptive_declared_type
        parameter.input_types = parameter._adaptive_declared_input_types
        parameter.output_type = parameter._adaptive_declared_output_type
        _report_all_types(parameter)
        parameter.adaptive_resolved_type = None
        parameter.track_change("adaptive_resolved_type", None)
        reverted.append(parameter)
    return reverted


def find_wires_blocked_by_typing(
    typing: AdaptiveTyping, connections: Connections, excluding: Connection | None
) -> list[Connection]:
    """The group's existing wires that could not carry ``typing.new_type``.

    A wire whose other end is itself an untyped adaptive pin is never blocked: that end takes
    whatever arrives.
    """
    blocked: list[Connection] = []
    for wire in group_connections(typing.node, typing.group, connections, excluding=excluding):
        connection = wire.connection
        if wire.adaptive_end_is_target:
            other_node = connection.source_node
            other_parameter = connection.source_parameter
        else:
            other_node = connection.target_node
            other_parameter = connection.target_parameter
        if _is_untyped_adaptive(other_node, other_parameter, connections, excluding):
            continue
        if wire.adaptive_end_is_target:
            allowed = accepts_incoming_type([typing.new_type], other_parameter.output_type)
        else:
            allowed = other_parameter.is_incoming_type_allowed(typing.new_type)
        if not allowed:
            blocked.append(connection)
    return blocked


def _is_untyped_adaptive(
    node: BaseNode, parameter: Parameter, connections: Connections, excluding: Connection | None
) -> bool:
    group = parameter.adaptive_type_group
    if group is None:
        return False
    return effective_group_type(node, group, connections, excluding=excluding) is None


def _report_all_types(parameter: Parameter) -> None:
    """Queue all three types for the editor.

    The setters only report a field whose getter changed, and ``input_types`` and ``output_type``
    fall back to ``type`` when unset, so setting ``type`` first can hide their change.
    """
    parameter.track_change("type", parameter.type)
    parameter.track_change("input_types", parameter.input_types)
    parameter.track_change("output_type", parameter.output_type)
