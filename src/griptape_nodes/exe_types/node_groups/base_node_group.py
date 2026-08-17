from __future__ import annotations

from typing import Any

from griptape_nodes.exe_types.core_types import (
    Parameter,
    ParameterMode,
)
from griptape_nodes.exe_types.node_types import (
    BaseNode,
)

GROUP_SETTINGS_PARAMS_METADATA_KEY = "group_settings_params"


class BaseNodeGroup(BaseNode):
    """Base class for node group implementations.

    Node groups are collections of nodes that are treated as a single unit.
    This base class provides the core functionality for managing a group of
    nodes, which may itself include other node groups.
    """

    nodes: dict[str, BaseNode]

    def __init__(
        self,
        name: str,
        metadata: dict[Any, Any] | None = None,
    ) -> None:
        """Initialize the node group base.

        Args:
            name: The name of this node group
            metadata: Optional metadata dictionary
        """
        super().__init__(name, metadata)
        self.nodes = {}
        self.metadata["is_node_group"] = True
        self.metadata["executable"] = False

    def add_parameter_to_group_settings(self, parameter: Parameter) -> None:
        """Add a parameter to the Group settings panel.

        Group settings parameters are determined by metadata in the frontend.

        Args:
            parameter: The parameter to add to settings
        """
        if ParameterMode.PROPERTY not in parameter.allowed_modes:
            msg = f"Parameter '{parameter.name}' must allow PROPERTY mode to be added to settings."
            raise ValueError(msg)

        if GROUP_SETTINGS_PARAMS_METADATA_KEY not in self.metadata:
            self.metadata[GROUP_SETTINGS_PARAMS_METADATA_KEY] = []

        group_settings_params: list[str] = self.metadata.get(GROUP_SETTINGS_PARAMS_METADATA_KEY, [])
        if parameter.name not in group_settings_params:
            group_settings_params.append(parameter.name)
            self.metadata[GROUP_SETTINGS_PARAMS_METADATA_KEY] = group_settings_params

    def add_nodes_to_group(self, nodes: list[BaseNode]) -> list[BaseNode]:
        """Add nodes to this group.

        A plain group takes exactly the nodes it was asked for; it does not pull in tethered
        companions the way a SubflowNodeGroup does. It does absorb nodes a previous owner
        released alongside them — see `_remove_nodes_from_existing_parents`.

        Args:
            nodes: A list of nodes to add to this group

        Returns:
            The nodes actually added, including any extra nodes a previous owner released.
        """
        # Reject impossible nesting before detaching anything: _remove_nodes_from_existing_parents
        # mutates the previous owner, so validating after it would leave a rejected add having
        # already ejected the nodes from the group that held them.
        self._validate_nodes_can_be_nested(nodes)
        nodes = nodes + self._remove_nodes_from_existing_parents(nodes)
        self._add_nodes_to_group_dict(nodes)

        self.metadata["node_names_in_group"] = list(self.nodes.keys())

        return nodes

    def contains_node(self, node: BaseNode) -> bool:
        """Whether this group holds the node directly, or inside a group nested within it.

        Args:
            node: The node to look for

        Returns:
            True if the node is anywhere inside this group's nesting tree
        """
        return self in self.get_enclosing_groups(node)

    def remove_nodes_from_group(self, nodes: list[BaseNode]) -> list[BaseNode]:
        """Remove nodes from this group.

        Nodes that are not members are skipped rather than raising, so callers can hand over a
        best-effort list.

        Args:
            nodes: A list of nodes to remove from this group

        Returns:
            The nodes actually removed — non-members are excluded so callers report only real changes.
        """
        nodes_removed = []
        for node in nodes:
            if node.name not in self.nodes:
                continue
            node.parent_group = None
            del self.nodes[node.name]
            nodes_removed.append(node)

        self.metadata["node_names_in_group"] = list(self.nodes.keys())

        return nodes_removed

    def _remove_nodes_from_existing_parents(self, nodes: list[BaseNode]) -> list[BaseNode]:
        """Detach nodes from whichever group currently owns them before reparenting them here.

        Returns:
            Any nodes detached beyond those requested. A previous owner may release more than it
            was asked to — a SubflowNodeGroup takes tethered companions with it — and those extras
            belong here now. Leaving them behind would eject them from every group.
        """
        requested_names = {node.name for node in nodes}
        nodes_by_parent: dict[BaseNodeGroup, list[BaseNode]] = {}
        for node in nodes:
            parent_group = node.parent_group
            if parent_group is self:
                continue
            if isinstance(parent_group, BaseNodeGroup):
                nodes_by_parent.setdefault(parent_group, []).append(node)

        extra_nodes: list[BaseNode] = []
        extra_names: set[str] = set()
        for parent_group, nodes_to_detach in nodes_by_parent.items():
            for detached_node in parent_group.remove_nodes_from_group(nodes_to_detach):
                if detached_node.name in requested_names or detached_node.name in extra_names:
                    continue
                extra_names.add(detached_node.name)
                extra_nodes.append(detached_node)

        return extra_nodes

    def _add_nodes_to_group_dict(self, nodes: list[BaseNode]) -> None:
        """Add nodes to the group's node dictionary."""
        for node in nodes:
            node.parent_group = self
            self.nodes[node.name] = node

    def _validate_nodes_can_be_nested(self, nodes: list[BaseNode]) -> None:
        """Reject additions that would make a group contain itself.

        Nesting a group inside one of its own descendants (or inside itself) would create a
        cycle in the parent_group chain, which every ancestor walk would then traverse forever.

        Args:
            nodes: The nodes about to be added

        Raises:
            ValueError: If any node is this group itself or one of its ancestors
        """
        for node in nodes:
            if node is self:
                msg = f"Cannot add group '{self.name}' to itself."
                raise ValueError(msg)
            if isinstance(node, BaseNodeGroup) and node.contains_node(self):
                msg = (
                    f"Attempted to add group '{node.name}' to group '{self.name}'. "
                    f"Failed because '{self.name}' is already inside '{node.name}', "
                    f"and a group cannot contain itself."
                )
                raise ValueError(msg)

    def _validate_nodes_in_group(self, nodes: list[BaseNode]) -> None:
        """Validate that all nodes are in the group."""
        for node in nodes:
            if node.name not in self.nodes:
                msg = f"Node {node.name} is not in node group {self.name}"
                raise ValueError(msg)

    def handle_child_node_rename(self, old_name: str, new_name: str) -> None:
        """Update group membership when a child node is renamed.

        Args:
            old_name: The old name of the child node
            new_name: The new name of the child node
        """
        if old_name not in self.nodes:
            return

        # Update the nodes dictionary
        node = self.nodes.pop(old_name)
        self.nodes[new_name] = node

        # Update the metadata
        node_names_in_group = self.metadata.get("node_names_in_group", [])
        if old_name in node_names_in_group:
            node_names_in_group.remove(old_name)
            node_names_in_group.append(new_name)
            self.metadata["node_names_in_group"] = node_names_in_group

    @staticmethod
    def get_enclosing_groups(node: BaseNode) -> list[BaseNodeGroup]:
        """Walk the parent_group chain and return every group enclosing the node, innermost first.

        Membership in a nested group is transitive: a node inside an inner group is also inside
        every group wrapping it. Callers that only inspect ``node.parent_group`` see the innermost
        group alone and treat the outer ones as external, so they must use this instead.

        Args:
            node: The node whose enclosing groups are wanted

        Returns:
            The enclosing groups, innermost first (empty if the node is not in a group)
        """
        enclosing_groups: list[BaseNodeGroup] = []
        # Guard against a malformed parent_group cycle rather than looping forever.
        visited: set[int] = {id(node)}
        current = node.parent_group
        while isinstance(current, BaseNodeGroup) and id(current) not in visited:
            enclosing_groups.append(current)
            visited.add(id(current))
            current = current.parent_group
        return enclosing_groups
