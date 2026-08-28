"""Execute a dispatched cluster of nodes in this process.

The cluster is the dispatch unit for isolated execution: nodes joined by unserializable
values must run in one process (see ``common/execution_clusters.py`` for how clusters are
computed), so they arrive as one ``ExecuteClusterRequest``. Members are constructed fresh,
boundary inputs are hydrated from the wire, and execution proceeds in dependency order with
intra-cluster values handed off as **live references** -- the entire reason the cluster
exists. Only serializable outputs of the requested output nodes go back over the wire.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from griptape_nodes.common.parameter_hydration import hydrate_parameter_values
from griptape_nodes.exe_types.node_types import aprocess_scope
from griptape_nodes.node_library.library_registry import LibraryRegistry
from griptape_nodes.retained_mode.events.execution_events import (
    ExecuteClusterRequest,
    ExecuteClusterResultFailure,
    ExecuteClusterResultSuccess,
)

if TYPE_CHECKING:
    from griptape_nodes.exe_types.node_types import BaseNode
    from griptape_nodes.retained_mode.events.base_events import ResultPayload
    from griptape_nodes.retained_mode.managers.event_manager import EventManager

logger = logging.getLogger("griptape_nodes")


def _topological_order(request: ExecuteClusterRequest) -> list[str] | None:
    """Return member names in dependency order, or None when the edges form a cycle."""
    names = [spec.node_name for spec in request.nodes]
    incoming_count = dict.fromkeys(names, 0)
    targets_by_source: dict[str, list[str]] = {name: [] for name in names}
    for edge in request.edges:
        incoming_count[edge.target_node] += 1
        targets_by_source[edge.source_node].append(edge.target_node)

    ready = [name for name in names if incoming_count[name] == 0]
    order = []
    while ready:
        name = ready.pop(0)
        order.append(name)
        for target in targets_by_source[name]:
            incoming_count[target] -= 1
            if incoming_count[target] == 0:
                ready.append(target)

    if len(order) != len(names):
        return None
    return order


def _validate(request: ExecuteClusterRequest) -> str | None:
    """Return a user-facing failure detail for a malformed cluster, or None when well-formed."""
    names = [spec.node_name for spec in request.nodes]
    if not names:
        return "Attempted to execute a cluster with no nodes."
    if len(set(names)) != len(names):
        return "Attempted to execute a cluster with duplicate node names."
    known = set(names)
    for edge in request.edges:
        if edge.source_node not in known or edge.target_node not in known:
            return (
                f"Attempted to execute a cluster, but edge {edge.source_node!r} -> "
                f"{edge.target_node!r} references a node that is not in the cluster."
            )
    for name in request.output_nodes:
        if name not in known:
            return f"Attempted to execute a cluster, but output node {name!r} is not in the cluster."
    return None


def _serializable_outputs(node: BaseNode) -> dict[str, Any]:
    """The node's outputs that may cross a process boundary.

    A serializable=False output is by definition consumed inside the cluster (or not at
    all); returning it would hand the orchestrator a value it cannot hold.
    """
    outputs = {}
    for parameter_name, value in node.parameter_output_values.items():
        parameter = node.get_parameter_by_name(parameter_name)
        if parameter is not None and parameter.serializable:
            outputs[parameter_name] = value
    return outputs


async def execute_cluster(request: ExecuteClusterRequest, event_manager: EventManager) -> ResultPayload:
    """Construct, hydrate, and run a cluster's members in dependency order."""
    validation_failure = _validate(request)
    if validation_failure is not None:
        return ExecuteClusterResultFailure(result_details=validation_failure)

    order = _topological_order(request)
    if order is None:
        return ExecuteClusterResultFailure(result_details="Attempted to execute a cluster, but its edges form a cycle.")

    nodes: dict[str, BaseNode] = {}
    for spec in request.nodes:
        try:
            node = LibraryRegistry.create_node(
                node_type=spec.node_type,
                name=spec.node_name,
                metadata=spec.node_metadata or None,
                specific_library_name=spec.library_name,
            )
        except KeyError as e:
            details = (
                f"Attempted to execute a cluster containing node {spec.node_name!r} of type "
                f"{spec.node_type!r} from library {spec.library_name!r}. Failed due to: {e}"
            )
            return ExecuteClusterResultFailure(failed_node=spec.node_name, result_details=details)
        for parameter_name, value in hydrate_parameter_values(spec.parameter_values).items():
            node.set_parameter_value(parameter_name, value)
        nodes[spec.node_name] = node

    edges_by_source: dict[str, list[Any]] = {}
    for edge in request.edges:
        edges_by_source.setdefault(edge.source_node, []).append(edge)

    for name in order:
        node = nodes[name]
        try:
            with event_manager.worker_node_execution_scope(), aprocess_scope(request.variables):
                await node.aprocess()
        except Exception as e:
            details = f"Attempted to execute node {name!r} in a cluster of {len(nodes)}. Failed due to: {e}"
            logger.exception(details)
            return ExecuteClusterResultFailure(failed_node=name, result_details=details)
        for edge in edges_by_source.get(name, []):
            # The whole point of the cluster: values on internal edges are handed to the
            # consumer as live references, never serialized.
            value = node.parameter_output_values.get(edge.source_parameter)
            nodes[edge.target_node].set_parameter_value(edge.target_parameter, value)

    outputs = {name: _serializable_outputs(nodes[name]) for name in request.output_nodes}
    details = f"Executed cluster of {len(nodes)} nodes"
    return ExecuteClusterResultSuccess(parameter_output_values=outputs, result_details=details)
