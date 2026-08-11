from __future__ import annotations

from typing import Any

from griptape_nodes.exe_types.base_iterative_nodes import BaseIterativeStartNode
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

        Args:
            nodes: A list of nodes to add to this group

        Returns:
            The nodes actually added, including any tethered companions pulled in.
        """
        nodes = self._expand_with_tethered_nodes(nodes)
        self._add_nodes_to_group_dict(nodes)

        node_names_in_group = set(self.nodes.keys())
        self.metadata["node_names_in_group"] = list(node_names_in_group)

        return nodes

    def remove_nodes_from_group(self, nodes: list[BaseNode]) -> None:
        """Remove nodes from this group.

        Args:
            nodes: A list of nodes to remove from this group
        """
        for node in nodes:
            if node.name in self.nodes:
                del self.nodes[node.name]

        node_names_in_group = set(self.nodes.keys())
        self.metadata["node_names_in_group"] = list(node_names_in_group)

    def _expand_with_tethered_nodes(self, nodes: list[BaseNode]) -> list[BaseNode]:
        """Add any tethered companions (e.g. an iterative Start node's paired End node).

        Companions already in the requested list or already in this group are skipped.
        """
        expanded = list(nodes)
        for node in nodes:
            if not isinstance(node, BaseIterativeStartNode):
                continue
            for companion in node.get_nodes_to_group_with():
                if companion in expanded:
                    continue
                if companion.name in self.nodes:
                    continue
                expanded.append(companion)

        return expanded

    def _add_nodes_to_group_dict(self, nodes: list[BaseNode]) -> None:
        """Add nodes to the group's node dictionary."""
        for node in nodes:
            node.parent_group = self
            self.nodes[node.name] = node

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
