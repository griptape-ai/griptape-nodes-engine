"""Find the node types that start and end a packaged workflow.

Packaging nodes into a workflow (subflow groups, parallel loops, publishing) wraps them between a
start node and an end node supplied by some registered library. Which library and which node types
is resolved from the registry rather than assumed, so a replacement standard library works as long
as it provides them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from griptape_nodes.exe_types.node_types import EndNode, StartNode
from griptape_nodes.node_library.library_registry import Library, LibraryRegistry
from griptape_nodes.retained_mode.events.workflow_events import (
    PublishWorkflowRegisteredEventData,
    PublishWorkflowRequest,
)

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.managers.library_manager import LibraryManager


@dataclass
class WorkflowStartEndNodes:
    """The start and end node types used to wrap a packaged workflow, and the libraries providing them."""

    start_flow_node_type: str
    start_flow_node_library_name: str
    end_flow_node_type: str
    end_flow_node_library_name: str


class WorkflowStartEndNodesNotFoundError(LookupError):
    """No registered library provides the start and end nodes a packaged workflow needs."""


def resolve_workflow_start_end_nodes(
    library_manager: LibraryManager, library: Library | None = None
) -> WorkflowStartEndNodes:
    """Resolve the start and end node types for a packaged workflow.

    The given library is asked first. When there is none, or it provides neither, every registered
    library is asked in ``LibraryRegistry.list_libraries`` order: default libraries first, then
    registration order. A library answers with the start and end node types named by its
    ``PublishWorkflowRequest`` handler, or else with its first ``StartNode`` and ``EndNode``
    subclasses.

    Args:
        library_manager: Holds the registered ``PublishWorkflowRequest`` handlers.
        library: The library the workflow is packaged for, or None for local and isolated execution.

    Returns:
        The start and end node types and their libraries.

    Raises:
        WorkflowStartEndNodesNotFoundError: If no registered library provides start and end nodes.
    """
    if library is not None:
        found = find_workflow_start_end_nodes_in_library(library_manager, library)
        if found is not None:
            return found

    for library_name in LibraryRegistry.list_libraries():
        candidate = LibraryRegistry.get_library(library_name)
        if candidate is library:
            continue
        found = find_workflow_start_end_nodes_in_library(library_manager, candidate)
        if found is not None:
            return found

    if library is not None:
        target = f"the library '{library.get_library_data().name}'"
    else:
        target = "this machine"
    msg = (
        f"Attempted to find the Start Flow and End Flow nodes needed to package a workflow for {target}. "
        "Failed because no installed library provides them. Install a library that provides start and "
        "end flow nodes, such as the Griptape Nodes standard library."
    )
    raise WorkflowStartEndNodesNotFoundError(msg)


def find_workflow_start_end_nodes_in_library(
    library_manager: LibraryManager, library: Library
) -> WorkflowStartEndNodes | None:
    """Return the start and end nodes one library provides, or None if it provides neither.

    The library's ``PublishWorkflowRequest`` handler is checked first because it is cheap and names
    the types outright. The subclass scan can import every node module of a lazily loaded library.
    """
    library_name = library.get_library_data().name

    registered_handlers = library_manager.get_registered_event_handlers(PublishWorkflowRequest)
    handler = registered_handlers.get(library_name)
    if handler is not None and isinstance(handler.event_data, PublishWorkflowRegisteredEventData):
        event_data = handler.event_data
        return WorkflowStartEndNodes(
            start_flow_node_type=event_data.start_flow_node_type,
            start_flow_node_library_name=event_data.start_flow_node_library_name,
            end_flow_node_type=event_data.end_flow_node_type,
            end_flow_node_library_name=event_data.end_flow_node_library_name,
        )

    start_nodes = library.get_nodes_by_base_type(StartNode)
    if not start_nodes:
        return None
    end_nodes = library.get_nodes_by_base_type(EndNode)
    if not end_nodes:
        return None
    return WorkflowStartEndNodes(
        start_flow_node_type=start_nodes[0],
        start_flow_node_library_name=library_name,
        end_flow_node_type=end_nodes[0],
        end_flow_node_library_name=library_name,
    )
