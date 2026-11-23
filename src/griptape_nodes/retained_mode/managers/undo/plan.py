"""Work out what an undo or redo has to change to bring the live workflow to a checkpoint.

Both sides are serializations of what the user edited, without run results (see `UndoManager`).
A node that matches its checkpoint is left alone, so it keeps everything it holds: its results,
the objects it cached, and the ids of its elements. A node whose position, size or other metadata
differs, or whose lock differs, is changed in place, since neither changes what it computes. Any
other difference rebuilds the node from the checkpoint, through the same code that opens a saved
workflow.

A group is rebuilt with its members, as one unit: a group holds its members by name and makes the
flow they live in, so a group and its members are only made together.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from griptape_nodes.retained_mode.events.node_events import SerializedNodeCommands

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.events.flow_events import SerializedFlowCommands


@dataclass
class NodeEdit:
    """A change to a node that keeps it."""

    metadata: dict[str, Any] | None = None
    removed_metadata_keys: list[str] = field(default_factory=list)
    lock: bool | None = None


@dataclass
class RestorePlan:
    """What brings the live workflow to the checkpoint.

    Args:
        units_to_delete: The live nodes to delete, each with its members first.
        units_to_build: The checkpoint's nodes to build, groups with their members.
        kept: Nodes left in place, by name.
        node_edits: Changes to nodes left in place, by name.
        flow_metadata: The flow's metadata to set, or None when it already matches.
        removed_flow_metadata_keys: Keys of the flow's metadata to remove.
    """

    units_to_delete: list[list[str]] = field(default_factory=list)
    units_to_build: set[str] = field(default_factory=set)
    kept: set[str] = field(default_factory=set)
    node_edits: dict[str, NodeEdit] = field(default_factory=dict)
    flow_metadata: dict[str, Any] | None = None
    removed_flow_metadata_keys: list[str] = field(default_factory=list)

    @property
    def changes_nothing(self) -> bool:
        return (
            not self.units_to_delete
            and not self.units_to_build
            and not self.node_edits
            and self.flow_metadata is None
            and not self.removed_flow_metadata_keys
        )


def plan_restore(target: SerializedFlowCommands, live: SerializedFlowCommands) -> RestorePlan:
    """What brings ``live`` to ``target``."""
    target_index = Index.of(target)
    live_index = Index.of(live)
    plan = RestorePlan()

    for root in sorted(target_index.units.keys() | live_index.units.keys()):
        if _unit_matches(root, target_index, live_index):
            for name in target_index.units[root]:
                plan.kept.add(name)
                edit = _edit_in_place(target_index.nodes[name], live_index.nodes[name])
                if edit is not None:
                    plan.node_edits[name] = edit
            continue
        if root in live_index.units:
            plan.units_to_delete.append(live_index.members_first(root))
        if root in target_index.units:
            plan.units_to_build.update(target_index.units[root])

    target_flow_metadata = _flow_metadata(target)
    live_flow_metadata = _flow_metadata(live)
    if target_flow_metadata != live_flow_metadata:
        plan.flow_metadata = target_flow_metadata
        plan.removed_flow_metadata_keys = sorted(live_flow_metadata.keys() - target_flow_metadata.keys())
    return plan


def plan_rebuild(target: SerializedFlowCommands, live: SerializedFlowCommands, names: set[str]) -> RestorePlan:
    """What rebuilds the units holding ``names`` from ``target``, keeping every other node.

    For kept nodes that did not come out matching the checkpoint, such as one a neighbor's
    rebuild changed through a connection.
    """
    target_index = Index.of(target)
    live_index = Index.of(live)
    plan = RestorePlan(kept=set(target_index.nodes))
    for root in sorted({target_index.unit_of(name) for name in names}):
        unit = target_index.units[root]
        plan.kept -= unit
        if root in live_index.units:
            plan.units_to_delete.append(live_index.members_first(root))
        plan.units_to_build.update(unit)
    return plan


def commands_to_build(target: SerializedFlowCommands, plan: RestorePlan) -> SerializedFlowCommands:
    """The part of ``target`` that builds the plan's nodes into the existing flow, and connects them to the kept ones."""
    return _level_to_build(target, plan.units_to_build, is_top=True)


def _level_to_build(level: SerializedFlowCommands, to_build: set[str], *, is_top: bool) -> SerializedFlowCommands:
    node_commands = [commands for commands in level.serialized_node_commands if _name(commands) in to_build]
    uuids = {commands.node_uuid for commands in node_commands}
    names_by_uuid = {commands.node_uuid: _name(commands) for commands in _all_node_commands(level)}
    connections = [
        connection
        for connection in level.serialized_connections
        if names_by_uuid.get(connection.source_node_uuid) in to_build
        or names_by_uuid.get(connection.target_node_uuid) in to_build
    ]
    sub_flows = [
        _level_to_build(sub_flow, to_build, is_top=False)
        for sub_flow in level.sub_flows_commands
        if any(_name(commands) in to_build for commands in sub_flow.serialized_node_commands)
    ]
    return dataclasses.replace(
        level,
        # The top level builds into the flow that is already there. A subflow is made anew with its group.
        flow_initialization_command=None if is_top else level.flow_initialization_command,
        serialized_node_commands=node_commands,
        serialized_connections=connections,
        set_parameter_value_commands={
            uuid: commands for uuid, commands in level.set_parameter_value_commands.items() if uuid in uuids
        },
        set_lock_commands_per_node={
            uuid: command for uuid, command in level.set_lock_commands_per_node.items() if uuid in uuids
        },
        sub_flows_commands=sub_flows,
        serialized_variable_commands=[],
    )


def _unit_matches(root: str, target_index: Index, live_index: Index) -> bool:
    target_unit = target_index.units.get(root)
    live_unit = live_index.units.get(root)
    if target_unit is None or live_unit is None or target_unit != live_unit:
        return False
    return all(
        target_index.nodes[name].build_key(target_index) == live_index.nodes[name].build_key(live_index)
        for name in target_unit
    )


def _edit_in_place(target: _Node, live: _Node) -> NodeEdit | None:
    edit = NodeEdit()
    target_metadata = target.commands.create_node_command.metadata or {}
    live_metadata = live.commands.create_node_command.metadata or {}
    if target_metadata != live_metadata:
        edit.metadata = target_metadata
        edit.removed_metadata_keys = sorted(live_metadata.keys() - target_metadata.keys())
    target_lock = target.commands.lock_node_command is not None
    if target_lock != (live.commands.lock_node_command is not None):
        edit.lock = target_lock
    if edit.metadata is None and edit.lock is None:
        return None
    return edit


def _flow_metadata(commands: SerializedFlowCommands) -> dict[str, Any]:
    command = commands.flow_initialization_command
    metadata = getattr(command, "metadata", None)
    if metadata is None:
        return {}
    return dict(metadata)


@dataclass
class _Node:
    """One node in a serialization, with the flow level that holds it."""

    commands: SerializedNodeCommands
    level: SerializedFlowCommands

    @property
    def name(self) -> str:
        return _name(self.commands)

    def build_key(self, index: Index) -> tuple[Any, ...]:
        """Everything about the node that only building it again can change."""
        set_value_commands = self.level.set_parameter_value_commands.get(self.commands.node_uuid, [])
        # Values are pooled under a hash of their content, so the same value has the same key in any
        # serialization.
        values = frozenset(
            (command.set_parameter_value_command.parameter_name, command.unique_value_uuid)
            for command in set_value_commands
        )
        create_command = dataclasses.replace(self.commands.create_node_command, metadata={})
        return (
            _level_name(self.level),
            create_command,
            self.commands.element_modification_commands,
            self.commands.is_node_group,
            values,
            index.incoming.get(self.name, frozenset()),
        )


@dataclass
class Index:
    """The nodes of a serialization, by name, with their groups and incoming connections."""

    nodes: dict[str, _Node] = field(default_factory=dict)
    # Each node's incoming connections, as (source node, source parameter, target parameter).
    incoming: dict[str, frozenset[tuple[str, str, str]]] = field(default_factory=dict)
    # The group each grouped node belongs to.
    group_of: dict[str, str] = field(default_factory=dict)
    # Each outermost node, with every node it holds, itself included.
    units: dict[str, frozenset[str]] = field(default_factory=dict)

    @classmethod
    def of(cls, commands: SerializedFlowCommands) -> Index:
        index = cls()
        names_by_uuid: dict[SerializedNodeCommands.NodeUUID, str] = {}
        levels = _levels(commands)
        for level in levels:
            for node_commands in level.serialized_node_commands:
                node = _Node(commands=node_commands, level=level)
                index.nodes[node.name] = node
                names_by_uuid[node_commands.node_uuid] = node.name
        for node in index.nodes.values():
            # A serialized group names its members by node UUID.
            for member_uuid in node.commands.create_node_command.node_names_to_add or []:
                member = names_by_uuid.get(SerializedNodeCommands.NodeUUID(member_uuid))
                if member is not None:
                    index.group_of[member] = node.name
        incoming: dict[str, set[tuple[str, str, str]]] = {}
        for level in levels:
            for connection in level.serialized_connections:
                source = names_by_uuid.get(connection.source_node_uuid)
                target = names_by_uuid.get(connection.target_node_uuid)
                if source is None or target is None:
                    continue
                incoming.setdefault(target, set()).add(
                    (source, connection.source_parameter_name, connection.target_parameter_name)
                )
        index.incoming = {name: frozenset(connections) for name, connections in incoming.items()}
        units: dict[str, set[str]] = {}
        for name in index.nodes:
            units.setdefault(index.unit_of(name), set()).add(name)
        index.units = {root: frozenset(names) for root, names in units.items()}
        return index

    def unit_of(self, name: str) -> str:
        """The outermost group holding ``name``, or ``name`` itself when no group holds it."""
        while name in self.group_of:
            name = self.group_of[name]
        return name

    def members_first(self, root: str) -> list[str]:
        """The unit of ``root``, each group after the nodes it holds."""
        return sorted(self.units[root], key=self._depth, reverse=True)

    def _depth(self, name: str) -> int:
        depth = 0
        while name in self.group_of:
            name = self.group_of[name]
            depth += 1
        return depth


def _name(commands: SerializedNodeCommands) -> str:
    return commands.create_node_command.node_name or ""


def _all_node_commands(level: SerializedFlowCommands) -> list[SerializedNodeCommands]:
    return [node_commands for sub_level in _levels(level) for node_commands in sub_level.serialized_node_commands]


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
    return getattr(level.flow_initialization_command, "flow_name", None)
