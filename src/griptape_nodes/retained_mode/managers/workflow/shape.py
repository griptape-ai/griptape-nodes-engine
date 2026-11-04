from __future__ import annotations

import json
from enum import StrEnum
from typing import TYPE_CHECKING, Any, cast

from griptape_nodes.exe_types.core_types import ParameterTypeBuiltin
from griptape_nodes.exe_types.node_types import EndNode, StartNode
from griptape_nodes.retained_mode.events.flow_events import GetTopLevelFlowRequest, GetTopLevelFlowResultSuccess
from griptape_nodes.retained_mode.events.workflow_events import WorkflowShape

if TYPE_CHECKING:
    from collections.abc import Sequence

    from griptape_nodes.exe_types.core_types import Parameter
    from griptape_nodes.exe_types.node_types import BaseNode
    from griptape_nodes.retained_mode.managers.flow_manager import FlowManager

ParameterShapeInfo = dict[str, Any]  # Parameter metadata dict from convert_parameter_to_minimal_dict
NodeParameterMap = dict[str, ParameterShapeInfo]  # {param_name: param_info}
WorkflowShapeNodes = dict[str, NodeParameterMap]  # {node_name: {param_name: param_info}}

SHAPE_DEFAULT_VALUE_KEY = "default_value"  # Key in ParameterShapeInfo holding the parameter's default


class WorkflowShapeType(StrEnum):
    """Top-level keys of a workflow shape: the Start Flow inputs and the End Flow outputs."""

    INPUT = "input"
    OUTPUT = "output"


def extract_workflow_shape(
    flow_manager: FlowManager, workflow_name: str, flow_name: str | None = None
) -> dict[str, Any]:
    """Extracts the input and output shape for a workflow.

    Here we gather information about the Workflow's exposed input and output Parameters
    such that a client invoking the Workflow can understand what values to provide
    as well as what values to expect back as output.

    Args:
        flow_manager: Resolves the flow to inspect.
        workflow_name: Registry key used in error messages.
        flow_name: Specific flow to inspect. If None, the top-level flow is used.
    """
    workflow_shape: dict[str, Any] = {WorkflowShapeType.INPUT: {}, WorkflowShapeType.OUTPUT: {}}

    if flow_name is None:
        result = flow_manager.on_get_top_level_flow_request(GetTopLevelFlowRequest())
        if result.failed():
            details = f"Workflow '{workflow_name}' does not have a top-level flow."
            raise ValueError(details)
        flow_name = cast("GetTopLevelFlowResultSuccess", result).flow_name
        if flow_name is None:
            details = f"Workflow '{workflow_name}' does not have a top-level flow."
            raise ValueError(details)

    control_flow = flow_manager.get_flow_by_name(flow_name)
    nodes = control_flow.nodes

    start_nodes: list[StartNode] = []
    end_nodes: list[EndNode] = []

    # First, validate that there are at least one StartNode and one EndNode
    for node in nodes.values():
        if isinstance(node, StartNode):
            start_nodes.append(node)
        elif isinstance(node, EndNode):
            end_nodes.append(node)
    if len(start_nodes) < 1:
        details = f"Workflow '{workflow_name}' does not have a StartNode."
        raise ValueError(details)
    if len(end_nodes) < 1:
        details = f"Workflow '{workflow_name}' does not have an EndNode."
        raise ValueError(details)

    # Now, we need to gather the input and output parameters for each node type.
    workflow_shape = create_workflow_shape_from_nodes(
        nodes=start_nodes,
        workflow_shape=workflow_shape,
        workflow_shape_type=WorkflowShapeType.INPUT,
    )
    return create_workflow_shape_from_nodes(
        nodes=end_nodes,
        workflow_shape=workflow_shape,
        workflow_shape_type=WorkflowShapeType.OUTPUT,
    )


def create_workflow_shape_from_nodes(
    nodes: Sequence[BaseNode],
    workflow_shape: dict[str, Any],
    workflow_shape_type: WorkflowShapeType,
) -> dict[str, Any]:
    """Creates a workflow shape from the nodes.

    This method iterates over a sequence of a certain Node type (input or output)
    and creates a dictionary representation of the workflow shape. This informs which
    Parameters can be set for input, and which Parameters are expected as output.
    """
    for node in nodes:
        for param in node.parameters:
            # Expose only the parameters that are relevant for workflow input and output.
            param_info = extract_parameter_shape_info(param, include_control_params=True)
            if param_info is not None:
                if workflow_shape_type == WorkflowShapeType.INPUT:
                    apply_set_value_as_default(node, param, param_info)
                if node.name in workflow_shape[workflow_shape_type]:
                    cast("dict", workflow_shape[workflow_shape_type][node.name])[param.name] = param_info
                else:
                    workflow_shape[workflow_shape_type][node.name] = {param.name: param_info}
    return workflow_shape


def apply_set_value_as_default(node: BaseNode, param: Parameter, param_info: ParameterShapeInfo) -> None:
    """Record the value set on a Start Flow parameter as its default in the shape.

    A Start Flow parameter's declared default is usually empty; the value the workflow's author
    typed in is stored on the node. Nodes that run the workflow read their defaults from the
    shape, so without this they start out empty. A value the shape's JSON header cannot hold
    (an artifact, say) keeps the declared default.
    """
    if param.name not in node.parameter_values:
        return
    value = node._get_raw_parameter_value(param.name)
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return
    param_info[SHAPE_DEFAULT_VALUE_KEY] = value


def extract_parameter_shape_info(parameter: Parameter, *, include_control_params: bool) -> ParameterShapeInfo | None:
    """Extract shape information from a parameter for workflow shape building.

    Expose only the parameters that are relevant for workflow input and output.

    Args:
        parameter: The parameter to extract shape info from
        include_control_params: Whether to include control type parameters (default: False)

    Returns:
        Parameter info dict if relevant for workflow shape, None if should be excluded
    """
    # Conditionally exclude control types
    if not include_control_params and parameter.type == ParameterTypeBuiltin.CONTROL_TYPE.value:
        return None

    return convert_parameter_to_minimal_dict(parameter)


def build_workflow_shape_from_parameter_info(
    input_node_params: WorkflowShapeNodes, output_node_params: WorkflowShapeNodes
) -> WorkflowShape:
    """Build a WorkflowShape from collected parameter information.

    Args:
        input_node_params: Mapping of input node names to their parameter info
        output_node_params: Mapping of output node names to their parameter info

    Returns:
        WorkflowShape object with inputs and outputs
    """
    return WorkflowShape(inputs=input_node_params, outputs=output_node_params)


def convert_parameter_to_minimal_dict(parameter: Parameter) -> dict[str, Any]:
    """Converts a parameter to a minimal dictionary for loading up a dynamic, black-box Node."""
    param_dict = parameter.to_dict()
    fields_to_include = [
        "name",
        "tooltip",
        "type",
        "input_types",
        "output_type",
        SHAPE_DEFAULT_VALUE_KEY,
        "tooltip_as_input",
        "tooltip_as_property",
        "tooltip_as_output",
        "mode_allowed_input",
        "mode_allowed_property",
        "mode_allowed_output",
        "converters",
        "validators",
        "traits",
        "ui_options",
        "settable",
        "is_user_defined",
        "private",
        "parent_container_name",
        "parent_element_name",
    ]
    minimal_dict = {key: param_dict[key] for key in fields_to_include if key in param_dict}
    minimal_dict["settable"] = bool(getattr(parameter, "settable", True))
    minimal_dict["is_user_defined"] = bool(getattr(param_dict, "is_user_defined", True))

    return minimal_dict
