"""Workflow metadata collection for files saved through the retained mode API."""

from __future__ import annotations

import base64
import logging
import pickle
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, NamedTuple

from griptape_nodes.exe_types.core_types import ParameterMode
from griptape_nodes.exe_types.node_types import BaseNode
from griptape_nodes.node_library.workflow_registry import WorkflowRegistry
from griptape_nodes.retained_mode.events.event_converter import safe_unstructure
from griptape_nodes.retained_mode.events.flow_events import (
    SerializeFlowToCommandsRequest,
    SerializeFlowToCommandsResultSuccess,
)
from griptape_nodes.retained_mode.events.node_events import (
    SerializeNodeToCommandsRequest,
    SerializeNodeToCommandsResultSuccess,
)

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.engine import Engine

logger = logging.getLogger("griptape_nodes")

# Metadata namespace prefix for all auto-injected fields
METADATA_NAMESPACE = "gtn_"

# Metadata key for storing flow commands
FLOW_COMMANDS_KEY = f"{METADATA_NAMESPACE}flow_commands"


class _ParameterCollection(NamedTuple):
    values: dict[str, Any]
    omitted: list[str]


def _serialize_node(node_name: str, engine: Engine) -> str | None:
    """Serialize a specific node to JSON commands.

    Args:
        node_name: Name of the node to serialize
        engine: The engine whose request bus performs the serialization

    Returns:
        JSON string of serialized node commands, or None if serialization fails
    """
    serialize_request = SerializeNodeToCommandsRequest(
        node_name=node_name,
    )
    serialize_result = engine.handle_request(serialize_request)

    if isinstance(serialize_result, SerializeNodeToCommandsResultSuccess):
        # Convert to dict and then to JSON string
        return serialize_result.to_json()

    return None


def _serialize_flow(engine: Engine, flow_name: str | None = None) -> str | None:
    """Serialize a flow to pickle + base64 encoded commands.

    Args:
        engine: The engine whose request bus and context manager perform the serialization
        flow_name: Name of the flow to serialize (None for current context flow)

    Returns:
        Base64-encoded pickle string of serialized flow commands, or None if serialization fails
    """
    # Validation: Check if we have a flow context
    if flow_name is None and not engine.context_manager.has_current_flow():
        logger.warning("Cannot serialize flow: no current flow context available")
        return None

    # Create serialize request
    serialize_request = SerializeFlowToCommandsRequest(
        flow_name=flow_name,
        include_create_flow_command=False,
    )
    serialize_result = engine.handle_request(serialize_request)

    # Validation: Check if serialization succeeded
    if not isinstance(serialize_result, SerializeFlowToCommandsResultSuccess):
        logger.warning("Failed to serialize flow '%s' to commands", flow_name or "current")
        return None

    # Success path: Serialize using pickle + base64
    try:
        serialized_flow_commands = serialize_result.serialized_flow_commands
        # Pickle is safe here: serializing workflow data for metadata injection into saved images
        # The data will only be deserialized by this same application
        pickled_data = pickle.dumps(serialized_flow_commands)
        encoded_data = base64.b64encode(pickled_data).decode("ascii")
    except Exception as e:
        logger.warning("Failed to pickle/encode flow '%s': %s", flow_name or "current", e)
        return None
    else:
        return encoded_data


def _collect_parameter_values(node_name: str, engine: Engine) -> _ParameterCollection | None:
    """Collect current parameter values from a node's INPUT and PROPERTY parameters.

    Parameters marked exclude_from_metadata=True are excluded from the returned values and listed
    in the omitted field instead, so callers can surface the omission without revealing
    the value.

    Args:
        node_name: Name of the node to collect parameters from
        engine: The engine whose object manager resolves the node

    Returns:
        _ParameterCollection with serialized values and omitted private parameter names,
        or None if the node cannot be resolved.
    """
    obj_mgr = engine.object_manager
    try:
        node = obj_mgr.attempt_get_object_by_name_as_type(node_name, BaseNode)
    except Exception as e:
        logger.warning("Failed to get node '%s' for parameter collection: %s", node_name, e)
        return None

    if node is None:
        logger.warning("Node '%s' not found for parameter collection", node_name)
        return None

    eligible_parameters = [
        param
        for param in node.parameters
        if ParameterMode.INPUT in param.allowed_modes or ParameterMode.PROPERTY in param.allowed_modes
    ]

    values: dict[str, Any] = {}
    omitted: list[str] = []

    for param in eligible_parameters:
        if param.exclude_from_metadata:
            omitted.append(param.name)
            continue

        value = node.get_parameter_value(param.name)
        if value is None:
            continue

        try:
            values[param.name] = safe_unstructure(value)
        except Exception as e:
            logger.warning("Failed to serialize parameter '%s' on node '%s': %s", param.name, node_name, e)

    return _ParameterCollection(values=values, omitted=omitted)


def _collect_workflow_info(workflow_name: str) -> dict[str, Any]:
    """Build the structured workflow provenance block for a given workflow name."""
    info: dict[str, Any] = {"name": workflow_name}
    try:
        workflow = WorkflowRegistry.get_workflow_by_name(workflow_name)
        if workflow.metadata.creation_date:
            info["created"] = workflow.metadata.creation_date.isoformat()
        if workflow.metadata.last_modified_date:
            info["modified"] = workflow.metadata.last_modified_date.isoformat()
        if workflow.metadata.engine_version_created_with:
            info["engine_version"] = workflow.metadata.engine_version_created_with
        if workflow.metadata.description:
            info["description"] = workflow.metadata.description
    except Exception:  # noqa: S110
        pass
    return info


def _collect_raw_provenance(engine: Engine) -> dict[str, Any]:
    """Collect structured provenance from current execution context without any output formatting.

    This is the single source of truth for provenance data. Both collect_sidecar_provenance()
    and collect_workflow_metadata() delegate here and then format the result for their
    respective targets (JSON sidecar vs. flat PNG text-chunk strings).

    Args:
        engine: The engine whose context manager and flow manager supply the metadata.

    Returns:
        Dict with optional 'workflow', 'flow', and 'parameters' keys. 'flow' always includes
        a 'resolving_nodes' list so callers can decide how to present multiple nodes.
    """
    result: dict[str, Any] = {}
    context_manager = engine.context_manager

    if not context_manager.has_current_workflow():
        return result

    try:
        workflow_name = context_manager.get_current_workflow_name()
        result["workflow"] = _collect_workflow_info(workflow_name)
    except Exception:
        logger.warning("Failed to collect workflow name for provenance")

    if not context_manager.has_current_flow():
        return result

    try:
        flow = context_manager.get_current_flow()
        _, resolving_nodes, _ = engine.flow_manager.flow_state(flow)
        result["flow"] = {"name": flow.name, "resolving_nodes": list(resolving_nodes)}

        if resolving_nodes:
            collection = _collect_parameter_values(resolving_nodes[0], engine)
            if collection is not None:
                if collection.values:
                    result["parameters"] = collection.values
                if collection.omitted:
                    result["parameters_omitted"] = collection.omitted
    except Exception:
        logger.exception("Failed to collect flow/node metadata for provenance")

    return result


def collect_sidecar_provenance(engine: Engine) -> dict[str, Any]:
    """Collect structured provenance data for the sidecar JSON file.

    Delegates to _collect_raw_provenance() and reshapes the flow block for the
    sidecar's JSON schema. Private parameters are already excluded by
    _collect_parameter_values(); this function passes 'parameters_omitted' through
    unchanged. Unlike collect_workflow_metadata(), values stay typed (not stringified).

    Args:
        engine: The engine whose context manager and flow manager supply the metadata.

    Returns:
        Dict with optional 'workflow', 'flow', 'parameters', and 'parameters_omitted' keys.
    """
    raw = _collect_raw_provenance(engine)
    result: dict[str, Any] = {}

    if "workflow" in raw:
        result["workflow"] = raw["workflow"]

    if "flow" in raw:
        resolving_nodes = raw["flow"]["resolving_nodes"]
        flow_info: dict[str, Any] = {"name": raw["flow"]["name"]}
        if resolving_nodes:
            flow_info["node_name"] = resolving_nodes[0]
        result["flow"] = flow_info

    if "parameters" in raw:
        result["parameters"] = raw["parameters"]

    if "parameters_omitted" in raw:
        result["parameters_omitted"] = raw["parameters_omitted"]

    return result


def _flatten_workflow_info(wf: dict[str, Any], metadata: dict[str, str]) -> None:
    """Write a workflow provenance block into a flat gtn_-prefixed string dict."""
    metadata[f"{METADATA_NAMESPACE}workflow_name"] = wf["name"]
    if wf.get("created"):
        metadata[f"{METADATA_NAMESPACE}workflow_created"] = wf["created"]
    if wf.get("modified"):
        metadata[f"{METADATA_NAMESPACE}workflow_modified"] = wf["modified"]
    if wf.get("engine_version"):
        metadata[f"{METADATA_NAMESPACE}engine_version"] = wf["engine_version"]
    if wf.get("description"):
        metadata[f"{METADATA_NAMESPACE}workflow_description"] = wf["description"]


def collect_workflow_metadata(engine: Engine) -> dict[str, str]:
    """Collect available workflow metadata from current execution context.

    Delegates to _collect_raw_provenance() then flattens the result to a gtn_-prefixed
    string dict suitable for PNG text-chunk injection. Also appends the serialized flow
    commands blob, which is only needed for the embedded-metadata path.

    Args:
        engine: The engine whose context manager and flow manager supply the metadata.

    Returns:
        Dictionary of metadata key-value pairs, may be empty if no context available.
    """
    metadata: dict[str, str] = {}
    metadata[f"{METADATA_NAMESPACE}saved_at"] = datetime.now(UTC).isoformat()

    raw = _collect_raw_provenance(engine)

    if "workflow" in raw:
        _flatten_workflow_info(raw["workflow"], metadata)

    if "flow" not in raw:
        return metadata

    fl = raw["flow"]
    metadata[f"{METADATA_NAMESPACE}flow_name"] = fl["name"]
    resolving_nodes = fl["resolving_nodes"]

    if resolving_nodes:
        metadata[f"{METADATA_NAMESPACE}node_name"] = ", ".join(resolving_nodes)

    # Serialize the entire flow to commands — only needed for embedded image metadata,
    # not the sidecar (the pickle+base64 blob is too large and not useful in JSON).
    flow_commands = _serialize_flow(engine)
    if flow_commands:
        metadata[FLOW_COMMANDS_KEY] = flow_commands

    if "parameters" in raw:
        for param_name, param_value in raw["parameters"].items():
            metadata[f"{METADATA_NAMESPACE}param_{param_name}"] = str(param_value)

    return metadata
