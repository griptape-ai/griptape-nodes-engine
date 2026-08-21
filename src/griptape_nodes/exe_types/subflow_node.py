"""A node whose behavior is defined by an inline child flow the user edits directly.

Unlike WorkflowNode (which loads from a saved .py file on disk), SubflowNode holds a
live child flow that the user builds interactively. The editor opens the child flow in a
dedicated tab via OpenNodeInnerCanvasRequest. Users add Start Flow / End Flow nodes and
connect parameters to those nodes to promote them onto the collapsed node's visible surface.
SyncInnerFlowSurfaceRequest re-derives the surface after the inner canvas changes.
ExportFlowAsLibraryNodeRequest serializes the child flow to a portable .py package.
"""

from __future__ import annotations

import logging
from typing import Any

from griptape_nodes.exe_types.core_types import Parameter, ParameterMode, ParameterTypeBuiltin
from griptape_nodes.exe_types.node_types import ControlNode
from griptape_nodes.exe_types.workflow_node import SUBFLOW_NAME_KEY, WORKFLOW_NODE_KEY, _get_flow_or_none
from griptape_nodes.node_library.workflow_registry import WorkflowShape
from griptape_nodes.retained_mode.events.execution_events import (
    StartLocalSubflowRequest,
    StartLocalSubflowResultSuccess,
)
from griptape_nodes.retained_mode.events.flow_events import DeleteFlowRequest
from griptape_nodes.retained_mode.events.parameter_events import (
    RemoveParameterFromNodeRequest,
    SetParameterValueRequest,
)
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

logger = logging.getLogger("griptape_nodes")

SUBFLOW_NODE_TYPE_KEY = "subflow_node"
SURFACE_PARAMS_KEY = "surface_params"
SURFACE_PARAMS_DATA_KEY = "surface_params_data"


class SubflowNode(ControlNode):
    """A node backed by a live child flow the user edits interactively.

    The inner canvas is opened via OpenNodeInnerCanvasRequest, which creates the child
    flow on first access and returns its name for the editor to navigate to. Users add
    nodes and connect Start Flow / End Flow parameter ports to promote those parameters
    onto the collapsed node's surface. SyncInnerFlowSurfaceRequest re-derives the
    surface. ExportFlowAsLibraryNodeRequest writes the child flow to a portable .py package.
    """

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None) -> None:
        super().__init__(name, metadata)
        self.metadata[WORKFLOW_NODE_KEY] = True
        self.metadata[SUBFLOW_NODE_TYPE_KEY] = True
        if SUBFLOW_NAME_KEY in self.metadata:
            self._recreate_surface_params_from_metadata()

    def after_node_deleted(self) -> None:
        self._discard_child_flow()

    async def aprocess(self) -> None:
        child_flow_name = self.metadata.get(SUBFLOW_NAME_KEY)
        if child_flow_name is None:
            msg = (
                f"Attempted to run SubflowNode '{self.name}'. "
                "Failed because no inner canvas exists yet. "
                "Open the node to build the inner flow first."
            )
            raise RuntimeError(msg)

        child_flow = _get_flow_or_none(child_flow_name)
        if child_flow is None:
            msg = (
                f"Attempted to run SubflowNode '{self.name}'. "
                f"Failed because child flow '{child_flow_name}' is not live. "
                "Re-open the workflow to restore the inner canvas."
            )
            raise RuntimeError(msg)

        try:
            shape_dict = GriptapeNodes.WorkflowManager().extract_workflow_shape(
                workflow_name=self.name, flow_name=child_flow_name
            )
        except ValueError as err:
            msg = (
                f"Attempted to run SubflowNode '{self.name}'. "
                f"Failed because the inner canvas has no Start Flow or End Flow nodes: {err}. "
                "Add Start Flow and End Flow nodes to the inner canvas."
            )
            raise RuntimeError(msg) from err

        workflow_shape = WorkflowShape(inputs=shape_dict["input"], outputs=shape_dict["output"])
        self._apply_inputs(child_flow_name, workflow_shape)

        result = await GriptapeNodes.ahandle_request(StartLocalSubflowRequest(flow_name=child_flow_name))
        if not isinstance(result, StartLocalSubflowResultSuccess):
            msg = (
                f"Attempted to run SubflowNode '{self.name}'. "
                f"Failed because the inner canvas did not finish: {result.result_details}"
            )
            raise RuntimeError(msg)  # noqa: TRY004

        self._collect_outputs(child_flow_name, workflow_shape)

    def sync_surface_params(self, workflow_shape: WorkflowShape) -> tuple[list[str], list[str]]:  # noqa: C901, PLR0912
        desired: dict[str, tuple[dict, set[ParameterMode]]] = {}
        for params in workflow_shape.inputs.values():
            for param_name, param_dict in params.items():
                if param_dict.get("type") == ParameterTypeBuiltin.CONTROL_TYPE.value:
                    continue
                input_modes: set[ParameterMode] = {ParameterMode.INPUT}
                if param_dict.get("mode_allowed_property", True):
                    input_modes.add(ParameterMode.PROPERTY)
                desired[param_name] = (param_dict, input_modes)

        for params in workflow_shape.outputs.values():
            for param_name, param_dict in params.items():
                if param_dict.get("type") == ParameterTypeBuiltin.CONTROL_TYPE.value:
                    continue
                if param_name in desired:
                    existing_dict, existing_modes = desired[param_name]
                    desired[param_name] = (existing_dict, existing_modes | {ParameterMode.OUTPUT})
                else:
                    desired[param_name] = (param_dict, {ParameterMode.OUTPUT})

        current_names: set[str] = set(self.metadata.get(SURFACE_PARAMS_KEY, []))
        desired_names: set[str] = set(desired)

        for name in list(desired_names):
            if name not in current_names and self.get_parameter_by_name(name) is not None:
                desired_names.discard(name)
                desired.pop(name, None)

        to_remove = current_names - desired_names
        to_add = desired_names - current_names

        removed: list[str] = []
        for param_name in to_remove:
            self.parameter_output_values.pop(param_name, None)
            param = self.get_parameter_by_name(param_name)
            if param is not None:
                param.user_defined = True
            GriptapeNodes.handle_request(RemoveParameterFromNodeRequest(node_name=self.name, parameter_name=param_name))
            removed.append(param_name)

        added: list[str] = []
        for param_name in to_add:
            param_dict, allowed_modes = desired[param_name]
            self.add_parameter(_build_surface_parameter(param_name, param_dict, allowed_modes))
            added.append(param_name)

        self.metadata[SURFACE_PARAMS_KEY] = list(desired_names)
        stored: dict[str, dict] = {}
        for param_name, (param_dict, allowed_modes) in desired.items():
            stored[param_name] = {
                **param_dict,
                "_modes": [m.name for m in allowed_modes],
            }
        self.metadata[SURFACE_PARAMS_DATA_KEY] = stored
        return added, removed

    def _recreate_surface_params_from_metadata(self) -> None:
        stored: dict[str, dict] = self.metadata.get(SURFACE_PARAMS_DATA_KEY, {})
        for param_name, data in stored.items():
            modes_raw = data.get("_modes", [])
            allowed_modes: set[ParameterMode] = {ParameterMode[m] for m in modes_raw}
            param_dict = {k: v for k, v in data.items() if k != "_modes"}
            self.add_parameter(_build_surface_parameter(param_name, param_dict, allowed_modes))

    def _discard_child_flow(self) -> None:
        tracked = self.metadata.pop(SUBFLOW_NAME_KEY, None)
        if tracked is None:
            return
        if _get_flow_or_none(tracked) is None:
            return
        delete_result = GriptapeNodes.handle_request(DeleteFlowRequest(flow_name=tracked))
        if delete_result.failed():
            logger.warning(
                "SubflowNode '%s' could not clean up child flow '%s': %s",
                self.name,
                tracked,
                delete_result.result_details,
            )

    def _apply_inputs(self, child_flow_name: str, workflow_shape: WorkflowShape) -> None:
        flow = GriptapeNodes.FlowManager().get_flow_by_name(child_flow_name)
        for start_node_name, params in workflow_shape.inputs.items():
            start_node = flow.nodes.get(start_node_name)
            if start_node is None:
                continue
            for param_name, param_dict in params.items():
                if param_dict.get("type") == ParameterTypeBuiltin.CONTROL_TYPE.value:
                    continue
                value = self.get_parameter_value(param_name)
                GriptapeNodes.handle_request(
                    SetParameterValueRequest(
                        parameter_name=param_name,
                        node_name=start_node_name,
                        value=value,
                    )
                )

    def _collect_outputs(self, child_flow_name: str, workflow_shape: WorkflowShape) -> None:
        flow = GriptapeNodes.FlowManager().get_flow_by_name(child_flow_name)
        for end_node_name, params in workflow_shape.outputs.items():
            end_node = flow.nodes.get(end_node_name)
            if end_node is None:
                continue
            for param_name, param_dict in params.items():
                if param_dict.get("type") == ParameterTypeBuiltin.CONTROL_TYPE.value:
                    continue
                if param_name in end_node.parameter_output_values:
                    value = end_node.parameter_output_values[param_name]
                else:
                    value = end_node.get_parameter_value(param_name)
                self.parameter_output_values[param_name] = value


def _build_surface_parameter(name: str, param_dict: dict, allowed_modes: set[ParameterMode]) -> Parameter:
    return Parameter(
        name=name,
        tooltip=param_dict.get("tooltip", ""),
        type=param_dict.get("type"),
        input_types=param_dict.get("input_types"),
        output_type=param_dict.get("output_type"),
        default_value=param_dict.get("default_value"),
        allowed_modes=allowed_modes,
        ui_options=param_dict.get("ui_options"),
    )


