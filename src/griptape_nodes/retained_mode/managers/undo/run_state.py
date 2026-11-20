"""Carry the workflow's latest run results into the checkpoint an undo or redo rebuilds it from.

A checkpoint holds the workflow as a save made at the time would, results included. An undo should
change what the user edited and nothing else, so a node the step did not touch keeps the results it
has now, whatever ran since. A node the step changed, and every node downstream of it, is marked to
run again, as editing it by hand would.

Which nodes changed is read off the commands: a node's results still hold when what they depend on
matches, which is its type, parameters, values, lock and incoming connections. Where it sits on the
canvas and the rest of its metadata are left out, as moving a node by hand keeps its results.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from griptape_nodes.exe_types.node_types import NodeResolutionState

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.events.flow_events import SerializedFlowCommands
    from griptape_nodes.retained_mode.events.node_events import SerializedNodeCommands


def carry_over_run_state(target: SerializedFlowCommands, live: SerializedFlowCommands) -> SerializedFlowCommands:
    """Return ``target`` with the run results of every node it shares, unchanged, with ``live``.

    Changes ``target`` in place and shares objects with ``live``, so pass commands nothing else holds.
    """
    target_nodes = _index(target)
    live_nodes = _index(live)

    changed = {
        name
        for name, node in target_nodes.nodes.items()
        if name not in live_nodes.nodes or node.run_key(target_nodes) != live_nodes.nodes[name].run_key(live_nodes)
    }
    to_run_again = target_nodes.downstream_of(changed)

    for name, node in target_nodes.nodes.items():
        if name in to_run_again:
            node.commands.create_node_command.resolution = NodeResolutionState.UNRESOLVED.value
            continue
        live_node = live_nodes.nodes[name]
        node.commands.create_node_command.resolution = live_node.commands.create_node_command.resolution
        node.level.set_parameter_value_commands[node.commands.node_uuid] = list(live_node.set_value_commands)
        for command in live_node.set_value_commands:
            node.level.unique_parameter_uuid_to_values[command.unique_value_uuid] = (
                live_node.level.unique_parameter_uuid_to_values[command.unique_value_uuid]
            )
    return target


@dataclass
class _Node:
    """One node in a serialization, with the flow level that holds it."""

    commands: SerializedNodeCommands
    level: SerializedFlowCommands
    set_value_commands: list[SerializedNodeCommands.IndirectSetParameterValueCommand]

    @property
    def name(self) -> str:
        return self.commands.create_node_command.node_name or ""

    def run_key(self, index: _Index) -> tuple[Any, ...]:
        """What this node's results depend on. Results from one serialization hold in another whose key matches."""
        incoming = index.incoming.get(self.name, frozenset())
        connected = {target_parameter for _source, _source_parameter, target_parameter in incoming}
        # Values are pooled under a hash of their content, so the same value has the same key in any
        # serialization.
        values = frozenset(
            (command.set_parameter_value_command.parameter_name, command.unique_value_uuid)
            for command in self.set_value_commands
            if not command.set_parameter_value_command.is_output
            and command.set_parameter_value_command.parameter_name not in connected
        )
        create_command = dataclasses.replace(self.commands.create_node_command, resolution="", metadata={})
        return (
            _level_name(self.level),
            create_command,
            self.commands.element_modification_commands,
            self.commands.lock_node_command,
            self.commands.is_node_group,
            values,
            incoming,
        )


@dataclass
class _Index:
    nodes: dict[str, _Node] = field(default_factory=dict)
    # Each node's incoming connections, as (source node, source parameter, target parameter).
    incoming: dict[str, frozenset[tuple[str, str, str]]] = field(default_factory=dict)
    outgoing: dict[str, set[str]] = field(default_factory=dict)

    def downstream_of(self, names: set[str]) -> set[str]:
        """The named nodes and every node a connection leads to from them."""
        found = set(names)
        frontier = list(names)
        while frontier:
            for target in self.outgoing.get(frontier.pop(), set()):
                if target not in found:
                    found.add(target)
                    frontier.append(target)
        return found


def _index(commands: SerializedFlowCommands) -> _Index:
    index = _Index()
    uuid_to_name: dict[SerializedNodeCommands.NodeUUID, str] = {}
    levels = _levels(commands)
    for level in levels:
        for node_commands in level.serialized_node_commands:
            node = _Node(
                commands=node_commands,
                level=level,
                set_value_commands=level.set_parameter_value_commands.get(node_commands.node_uuid, []),
            )
            index.nodes[node.name] = node
            uuid_to_name[node_commands.node_uuid] = node.name

    incoming: dict[str, set[tuple[str, str, str]]] = {}
    for level in levels:
        for connection in level.serialized_connections:
            source = uuid_to_name.get(connection.source_node_uuid)
            target = uuid_to_name.get(connection.target_node_uuid)
            if source is None or target is None:
                continue
            incoming.setdefault(target, set()).add(
                (source, connection.source_parameter_name, connection.target_parameter_name)
            )
            index.outgoing.setdefault(source, set()).add(target)
    index.incoming = {name: frozenset(connections) for name, connections in incoming.items()}
    return index


def _levels(commands: SerializedFlowCommands) -> list[SerializedFlowCommands]:
    """The flow and every flow beneath it."""
    levels = [commands]
    for sub_flow_commands in commands.sub_flows_commands:
        levels.extend(_levels(sub_flow_commands))
    return levels


def _level_name(level: SerializedFlowCommands) -> str | None:
    if level.flow_name is not None:
        return level.flow_name
    if level.flow_initialization_command is None:
        return None
    return level.flow_initialization_command.flow_name
