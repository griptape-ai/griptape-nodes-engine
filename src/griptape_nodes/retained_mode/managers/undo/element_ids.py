"""Keep the element ids of a node's parameters, groups and containers across an undo or redo.

A rebuild makes every element again, and a new element gets a new id. Clients hold elements by id,
so an element that comes back where it was takes its old id: one whose node has the same name and
that is reached through the same element names. A trait's name is made up anew each time, so a trait
is known by its kind instead. An element that can't be told apart from a sibling keeps its new id.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from griptape_nodes.exe_types.elements.trait import Trait
from griptape_nodes.exe_types.node_types import BaseNode

if TYPE_CHECKING:
    from griptape_nodes.exe_types.elements.base import BaseNodeElement
    from griptape_nodes.retained_mode.runtime.workflow_runtime import WorkflowRuntime

# Each node's element ids, by node name, then by the keys leading to the element from the node.
ElementIds = dict[str, dict[tuple[str, ...], str]]


def element_ids(workflow: WorkflowRuntime) -> ElementIds:
    """The ids of the elements of every node in the workflow."""
    return {node.name: _ids_by_path(node.root_ui_element) for node in _nodes(workflow)}


def restore_element_ids(workflow: WorkflowRuntime, ids: ElementIds) -> None:
    """Give each element that ``ids`` holds an id for that id back."""
    for node in _nodes(workflow):
        old_ids = ids.get(node.name)
        if old_ids is None:
            continue
        for path, element in _elements_by_path(node.root_ui_element).items():
            old_id = old_ids.get(path)
            if old_id is not None:
                element.element_id = old_id


def _nodes(workflow: WorkflowRuntime) -> list[BaseNode]:
    return list(workflow.objects.get_filtered_subset(type=BaseNode).values())


def _ids_by_path(root: BaseNodeElement) -> dict[tuple[str, ...], str]:
    return {path: element.element_id for path, element in _elements_by_path(root).items()}


def _elements_by_path(root: BaseNodeElement) -> dict[tuple[str, ...], BaseNodeElement]:
    found: dict[tuple[str, ...], BaseNodeElement] = {(): root}
    frontier: list[tuple[tuple[str, ...], BaseNodeElement]] = [((), root)]
    while frontier:
        path, element = frontier.pop()
        keys = [_key(child) for child in element.children]
        for child, key in zip(element.children, keys, strict=True):
            if keys.count(key) > 1:
                continue
            child_path = (*path, key)
            found[child_path] = child
            frontier.append((child_path, child))
    return found


def _key(element: BaseNodeElement) -> str:
    if isinstance(element, Trait):
        return f"trait:{type(element).__name__}"
    return element.name
