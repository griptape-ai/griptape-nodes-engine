"""Workflow metadata collection for files saved through the retained mode API."""

from __future__ import annotations

import base64
import logging
import pickle
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

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

# Parameter names containing any of these substrings (case-insensitive) are excluded from
# the plaintext sidecar JSON to avoid accidentally persisting API keys, passwords, or other
# credentials that users may have stored directly as node parameter values.
# Note: bare "token" is intentionally absent — it matches common non-secret AI parameters
# like max_tokens, num_tokens, and token_count. Use specific forms (auth_token, api_token,
# etc.) that unambiguously identify credential tokens.
_SENSITIVE_PARAM_SUBSTRINGS = frozenset(
    {
        "secret",
        "password",
        "passwd",
        "api_key",
        "apikey",
        "credential",
        "private",
        "auth_token",
        "access_token",
        "api_token",
        "bearer_token",
        "refresh_token",
    }
)


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


def _collect_parameter_values(node_name: str, engine: Engine) -> dict[str, Any] | None:
    """Collect current parameter values from a node's INPUT and PROPERTY parameters.

    Args:
        node_name: Name of the node to collect parameters from
        engine: The engine whose object manager resolves the node

    Returns:
        Dictionary of parameter names to serialized values, or None if collection fails
    """
    # Failure case: Attempt to get node object
    obj_mgr = engine.object_manager
    try:
        node = obj_mgr.attempt_get_object_by_name_as_type(node_name, BaseNode)
    except Exception as e:
        logger.warning("Failed to get node '%s' for parameter collection: %s", node_name, e)
        return None

    if node is None:
        logger.warning("Node '%s' not found for parameter collection", node_name)
        return None

    # Get all parameters from node
    all_parameters = node.parameters

    # Filter to INPUT and PROPERTY mode parameters only
    eligible_parameters = [
        param
        for param in all_parameters
        if ParameterMode.INPUT in param.allowed_modes or ParameterMode.PROPERTY in param.allowed_modes
    ]

    # Collect and serialize parameter values
    parameter_values = {}

    for param in eligible_parameters:
        # Get current value
        value = node.get_parameter_value(param.name)

        # Skip None values (not set)
        if value is None:
            continue

        # Serialize value with error handling
        try:
            serialized_value = safe_unstructure(value)
            parameter_values[param.name] = serialized_value
        except Exception as e:
            logger.warning("Failed to serialize parameter '%s' on node '%s': %s", param.name, node_name, e)
            continue

    # Success path: return collected values (may be empty dict)
    return parameter_values


def _collect_workflow_details(workflow_name: str, metadata: dict[str, str]) -> None:
    """Collect workflow details from registry and add to metadata dict.

    Args:
        workflow_name: Name of the workflow
        metadata: Dictionary to populate with workflow metadata (modified in-place)
    """
    try:
        workflow = WorkflowRegistry.get_workflow_by_name(workflow_name)

        if workflow.metadata.creation_date:
            metadata[f"{METADATA_NAMESPACE}workflow_created"] = workflow.metadata.creation_date.isoformat()

        if workflow.metadata.last_modified_date:
            metadata[f"{METADATA_NAMESPACE}workflow_modified"] = workflow.metadata.last_modified_date.isoformat()

        if workflow.metadata.engine_version_created_with:
            metadata[f"{METADATA_NAMESPACE}engine_version"] = workflow.metadata.engine_version_created_with

        if workflow.metadata.description:
            metadata[f"{METADATA_NAMESPACE}workflow_description"] = workflow.metadata.description
    except Exception:  # noqa: S110
        pass


def _is_sensitive_parameter(param_name: str) -> bool:
    """Return True if the parameter name suggests it holds a secret value."""
    lower = param_name.lower()
    return any(sensitive in lower for sensitive in _SENSITIVE_PARAM_SUBSTRINGS)


def _collect_workflow_info(workflow_name: str) -> dict[str, Any]:
    """Build the 'workflow' provenance block for a given workflow name."""
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


def _collect_flow_and_params(
    engine: Engine,
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    """Build the 'flow' and 'parameters' provenance blocks from current flow context.

    Returns:
        A (flow_info, safe_parameters, omitted_names) tuple.
        safe_parameters excludes sensitive names; omitted_names lists what was excluded.
    """
    flow = engine.context_manager.get_current_flow()
    flow_info: dict[str, Any] = {"name": flow.name}

    _, resolving_nodes, _ = engine.flow_manager.flow_state(flow)
    if not resolving_nodes:
        return flow_info, {}, []

    node_name = resolving_nodes[0]
    flow_info["node_name"] = node_name

    parameter_values = _collect_parameter_values(node_name, engine) or {}
    safe_params: dict[str, Any] = {}
    omitted_names: list[str] = []
    for name, value in parameter_values.items():
        if _is_sensitive_parameter(name):
            omitted_names.append(name)
        else:
            safe_params[name] = value
    return flow_info, safe_params, omitted_names


def collect_sidecar_provenance(engine: Engine) -> dict[str, Any]:
    """Collect structured provenance data for the sidecar JSON file.

    Returns typed, nested data suitable for JSON serialization, unlike
    collect_workflow_metadata() which flattens everything into strings for
    PNG text-chunk injection. Sensitive parameter names are excluded.

    Args:
        engine: The engine whose context manager and flow manager supply the metadata.

    Returns:
        Dict with optional 'workflow', 'flow', 'parameters', and 'parameters_omitted' keys.
    """
    result: dict[str, Any] = {}
    context_manager = engine.context_manager

    if not context_manager.has_current_workflow():
        return result

    try:
        workflow_name = context_manager.get_current_workflow_name()
        result["workflow"] = _collect_workflow_info(workflow_name)
    except Exception:
        logger.warning("Failed to collect workflow name for sidecar")

    if not context_manager.has_current_flow():
        return result

    try:
        flow_info, safe_params, omitted_names = _collect_flow_and_params(engine)
        result["flow"] = flow_info
        if safe_params:
            result["parameters"] = safe_params
        if omitted_names:
            result["parameters_omitted"] = omitted_names
    except Exception:
        logger.exception("Failed to collect flow/node metadata for sidecar")

    return result


def collect_workflow_metadata(engine: Engine) -> dict[str, str]:
    """Collect available workflow metadata from current execution context.

    Gathers metadata from the engine's ContextManager and WorkflowRegistry.
    All keys are prefixed with METADATA_NAMESPACE to avoid conflicts.

    Args:
        engine: The engine whose context manager and flow manager supply the metadata.

    Returns:
        Dictionary of metadata key-value pairs, may be empty if no context available
    """
    metadata = {}

    # Add save timestamp (always available)
    metadata[f"{METADATA_NAMESPACE}saved_at"] = datetime.now(UTC).isoformat()

    # Get context manager
    context_manager = engine.context_manager

    # Check workflow context
    if not context_manager.has_current_workflow():
        return metadata

    # Get workflow name
    try:
        workflow_name = context_manager.get_current_workflow_name()
        metadata[f"{METADATA_NAMESPACE}workflow_name"] = workflow_name
        _collect_workflow_details(workflow_name, metadata)
    except Exception:  # noqa: S110
        pass

    # Get flow context and resolving nodes
    if context_manager.has_current_flow():
        try:
            flow = context_manager.get_current_flow()
            metadata[f"{METADATA_NAMESPACE}flow_name"] = flow.name

            # Get resolving nodes (currently running nodes) from flow_state
            flow_manager = engine.flow_manager
            _, resolving_nodes, _ = flow_manager.flow_state(flow)

            if resolving_nodes:
                # Store node name(s) - if multiple, join with comma
                metadata[f"{METADATA_NAMESPACE}node_name"] = ", ".join(resolving_nodes)

            # Serialize the entire current flow to commands
            # This captures all nodes, connections, and parameter values in the flow
            flow_commands = _serialize_flow(engine)
            if flow_commands:
                metadata[FLOW_COMMANDS_KEY] = flow_commands

            if resolving_nodes:
                # Collect parameter values from the first resolving node
                parameter_values = _collect_parameter_values(resolving_nodes[0], engine)
                if parameter_values:
                    # Store each parameter as its own metadata key
                    for param_name, param_value in parameter_values.items():
                        metadata[f"{METADATA_NAMESPACE}param_{param_name}"] = str(param_value)
        except Exception:
            logger.exception("Failed to collect flow/node metadata")

    return metadata
