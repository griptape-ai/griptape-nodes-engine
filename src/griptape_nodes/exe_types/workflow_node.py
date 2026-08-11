"""Node types whose behavior is defined by a saved workflow file.

A library can declare a node in its ``workflow_nodes`` list instead of its ``nodes`` list,
pointing at a saved workflow ``.py`` that contains Start Flow and End Flow nodes. The engine
reads the workflow's saved shape and generates a node type whose inputs are the workflow's
Start Flow parameters and whose outputs are its End Flow parameters. Running the node imports
the workflow as a transient subflow, feeds the inputs in, runs it, and copies the End Flow
values back out.

The generated node types are ordinary ``BaseNode`` subclasses built by
``build_workflow_node_class``, so they behave like any other library node on the canvas.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, ClassVar, NamedTuple

from griptape_nodes.exe_types.core_types import Parameter, ParameterMode, ParameterTypeBuiltin
from griptape_nodes.exe_types.flow import ControlFlow
from griptape_nodes.exe_types.node_types import ControlNode, EndNode, StartNode
from griptape_nodes.files.path_utils import derive_registry_key
from griptape_nodes.node_library.workflow_registry import WorkflowMetadata, WorkflowRegistry
from griptape_nodes.retained_mode.events.execution_events import (
    StartLocalSubflowRequest,
    StartLocalSubflowResultSuccess,
)
from griptape_nodes.retained_mode.events.flow_events import TRANSIENT_KEY, DeleteFlowRequest
from griptape_nodes.retained_mode.events.node_events import GetFlowForNodeRequest, GetFlowForNodeResultSuccess
from griptape_nodes.retained_mode.events.parameter_events import SetParameterValueRequest
from griptape_nodes.retained_mode.events.workflow_events import (
    ImportWorkflowAsReferencedSubFlowRequest,
    ImportWorkflowAsReferencedSubFlowResultSuccess,
)
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from pathlib import Path

    from griptape_nodes.node_library.workflow_registry import NodeParametersMapping, ParameterMinimalDict

logger = logging.getLogger("griptape_nodes")

# Separator used to qualify a surface parameter with its Start/End node name when two nodes in the
# workflow expose the same parameter name.
QUALIFIED_NAME_SEPARATOR = "."

# Node-metadata key holding the name of the live imported subflow. Runtime-only: the subflow is
# tagged transient so it never round-trips through a save, which makes any value present at load
# time stale. The editor reads it to offer a "view the subflow" affordance during a session.
SUBFLOW_NAME_KEY = "subflow_name"

# Node-metadata flag the editor uses to recognize a node that runs a workflow. Presence of this key
# is what makes the editor render the node with its subflow-preview wrapper (see
# `determineNodeType` in the editor's ConstructNode.ts), which offers a "View Subflow" button.
WORKFLOW_NODE_KEY = "workflow_node"

# Node-metadata key holding the registry key of the backing workflow. The editor's preview reads
# this to import and display the workflow when the node has not run yet and therefore has no live
# subflow. Shared contract with the standard library's SubflowWorkflowNode, which stores its
# dropdown selection here.
WORKFLOW_FILE_VALUE_KEY = "_workflow_file_value"


class WorkflowNodeDefinitionError(Exception):
    """Raised when a workflow file cannot back a node type."""


class WorkflowParameterRoute(NamedTuple):
    """Where a surface parameter reads from or writes to inside the workflow."""

    workflow_node_name: str
    parameter_name: str


class WorkflowNodeLiveRoutes(NamedTuple):
    """Surface parameter names mapped onto the live subflow parameters they address."""

    inputs: dict[str, WorkflowParameterRoute]
    outputs: dict[str, WorkflowParameterRoute]


class WorkflowNodeSurfaceParameter(NamedTuple):
    """One parameter on the generated node, plus the workflow parameters it is wired to.

    A name that appears on both a Start Flow node and an End Flow node carries both routes, so a
    single parameter serves as input and output (matching how the workflow itself passes it
    through).
    """

    definition: ParameterMinimalDict
    input_route: WorkflowParameterRoute | None
    output_route: WorkflowParameterRoute | None


def flatten_shape_section(section: NodeParametersMapping) -> dict[str, WorkflowParameterRoute]:
    """Flatten one half of a workflow shape into surface parameter names.

    A parameter name used by exactly one Start/End node keeps its bare name. A name used by more
    than one node is qualified as ``<node name>.<parameter name>`` for every node that uses it, so
    the result never depends on iteration order. Control parameters are dropped: the generated node
    supplies its own control flow.
    """
    nodes_using_name: dict[str, list[str]] = {}
    for workflow_node_name, parameters in section.items():
        for parameter_name, definition in parameters.items():
            if _is_control_parameter(definition):
                continue
            nodes_using_name.setdefault(parameter_name, []).append(workflow_node_name)

    routes: dict[str, WorkflowParameterRoute] = {}
    for workflow_node_name, parameters in section.items():
        for parameter_name, definition in parameters.items():
            if _is_control_parameter(definition):
                continue
            if len(nodes_using_name[parameter_name]) == 1:
                surface_name = parameter_name
            else:
                surface_name = f"{workflow_node_name}{QUALIFIED_NAME_SEPARATOR}{parameter_name}"
            routes[surface_name] = WorkflowParameterRoute(
                workflow_node_name=workflow_node_name, parameter_name=parameter_name
            )
    return routes


def build_workflow_node_surface(workflow_metadata: WorkflowMetadata) -> dict[str, WorkflowNodeSurfaceParameter]:
    """Derive the generated node's parameter surface from a workflow's saved shape.

    Inputs come first so they render above the outputs on the canvas.

    Raises:
        WorkflowNodeDefinitionError: The workflow carries no saved shape, meaning it has no Start
            Flow and End Flow nodes to derive a surface from.
    """
    workflow_shape = workflow_metadata.workflow_shape
    if workflow_shape is None:
        msg = (
            f"Workflow '{workflow_metadata.name}' cannot back a node because it has no saved input and output "
            "shape. Add a Start Flow node and an End Flow node to the workflow, then save it."
        )
        raise WorkflowNodeDefinitionError(msg)

    input_routes = flatten_shape_section(workflow_shape.inputs)
    output_routes = flatten_shape_section(workflow_shape.outputs)
    if not input_routes and not output_routes:
        msg = (
            f"Workflow '{workflow_metadata.name}' cannot back a node because its Start Flow and End Flow nodes "
            "expose no parameters. Add at least one parameter to a Start Flow or End Flow node, then save it."
        )
        raise WorkflowNodeDefinitionError(msg)

    surface: dict[str, WorkflowNodeSurfaceParameter] = {}
    for surface_name, route in input_routes.items():
        definition = workflow_shape.inputs[route.workflow_node_name][route.parameter_name]
        surface[surface_name] = WorkflowNodeSurfaceParameter(
            definition=definition, input_route=route, output_route=None
        )
    for surface_name, route in output_routes.items():
        definition = workflow_shape.outputs[route.workflow_node_name][route.parameter_name]
        existing = surface.get(surface_name)
        if existing is None:
            surface[surface_name] = WorkflowNodeSurfaceParameter(
                definition=definition, input_route=None, output_route=route
            )
        else:
            surface[surface_name] = existing._replace(output_route=route)
    return surface


def match_shape_nodes(declared_names: Sequence[str], live_names: Iterable[str]) -> dict[str, str]:
    """Pair the Start/End node names in a saved shape with their names in an imported subflow.

    Importing a workflow renames any node whose name is already taken elsewhere in the session,
    appending a ``_N`` suffix, so the saved shape's node names can be stale. An exact name match
    wins; a declared name otherwise claims the first unclaimed live name that looks like a
    de-duplicated spelling of it.
    """
    remaining = list(live_names)
    matches: dict[str, str] = {}

    for declared_name in declared_names:
        if declared_name in remaining:
            matches[declared_name] = declared_name
            remaining.remove(declared_name)

    for declared_name in declared_names:
        if declared_name in matches:
            continue
        renamed = next((live_name for live_name in remaining if live_name.startswith(f"{declared_name}_")), None)
        if renamed is not None:
            matches[declared_name] = renamed
            remaining.remove(renamed)

    return matches


def ensure_workflow_registered(workflow_file_path: Path, workflow_metadata: WorkflowMetadata) -> str:
    """Register `workflow_file_path` in the workflow registry if it is not there already.

    Returns the registry key the workflow is available under. Registration is idempotent and
    happens lazily, at execution time, because the registry is cleared and rebuilt when the
    workspace changes while a library's node types survive that reload.

    The entry is marked internal so a workflow that exists only to back a node does not show up
    in the editor's workflow picker. A library that also lists the file in its ``workflows`` array
    registers it through the normal path first, and that entry wins.
    """
    registry_key = derive_registry_key(str(workflow_file_path))
    if WorkflowRegistry.has_workflow_with_name(registry_key):
        return registry_key

    WorkflowRegistry.generate_new_workflow(
        registry_key=registry_key,
        metadata=workflow_metadata.model_copy(update={"is_internal": True}),
        file_path=str(workflow_file_path),
    )
    return registry_key


class WorkflowNode(ControlNode):
    """Base class for node types generated from a saved workflow file.

    ``build_workflow_node_class`` produces the concrete subclasses; the class attributes below are
    filled in per workflow. Instantiating this base class directly is not useful.
    """

    # Absolute path to the workflow that backs this node type.
    workflow_file_path: ClassVar[Path]
    # Metadata read from that workflow's header at library load time.
    workflow_metadata: ClassVar[WorkflowMetadata]
    # Surface parameter name -> the workflow parameters it is wired to.
    workflow_surface: ClassVar[dict[str, WorkflowNodeSurfaceParameter]]

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None) -> None:
        super().__init__(name, metadata)

        # The imported subflow is an execution-time artifact tracked through node metadata. It is
        # never serialized, so anything present at load time is stale (or, after a copy/paste,
        # belongs to a different node).
        self.metadata.pop(SUBFLOW_NAME_KEY, None)
        self.metadata[WORKFLOW_NODE_KEY] = True
        self._publish_workflow_registry_key()

        for surface_name, surface_parameter in self.workflow_surface.items():
            self.add_parameter(_build_surface_parameter(surface_name, surface_parameter))

    def _publish_workflow_registry_key(self) -> None:
        """Register the backing workflow and record its key so the editor can preview it.

        Registration happens here, rather than only at execution time, so the workflow can be
        previewed before the node has ever run. Failure is not fatal: the node stays usable and the
        preview is simply unavailable until the next run re-registers the workflow.
        """
        try:
            self.metadata[WORKFLOW_FILE_VALUE_KEY] = ensure_workflow_registered(
                self.workflow_file_path, self.workflow_metadata
            )
        except (KeyError, ValueError):
            logger.warning(
                "Node '%s' could not register its workflow '%s' for preview; it will be registered on the next run.",
                self.name,
                self.workflow_file_path,
            )

    def after_node_deleted(self) -> None:
        self._discard_subflow()

    async def aprocess(self) -> None:
        subflow_name = await self._load_subflow()
        live_routes = self._resolve_live_routes(subflow_name)

        self._apply_inputs(subflow_name, live_routes)

        result = await GriptapeNodes.ahandle_request(StartLocalSubflowRequest(flow_name=subflow_name))
        if not isinstance(result, StartLocalSubflowResultSuccess):
            msg = (
                f"Attempted to run the workflow behind node '{self.name}'. "
                f"Failed because the workflow did not finish: {result.result_details}"
            )
            raise RuntimeError(msg)  # noqa: TRY004 - the workflow failed at run time; this is not a type error

        self._collect_outputs(subflow_name, live_routes)

    async def _load_subflow(self) -> str:
        """Return the name of this node's live subflow, importing the workflow if needed.

        The subflow is reused across runs so repeat executions do not pay for another import. It
        is tagged transient so the engine never serializes it, which keeps the node-to-subflow
        link out of saved workflows entirely.
        """
        tracked = self.metadata.get(SUBFLOW_NAME_KEY)
        if tracked is not None:
            if _get_flow_or_none(tracked) is not None:
                return tracked
            # Torn down out from under us (for example when the parent flow was deleted).
            self.metadata.pop(SUBFLOW_NAME_KEY, None)

        flow_result = GriptapeNodes.handle_request(GetFlowForNodeRequest(node_name=self.name))
        if not isinstance(flow_result, GetFlowForNodeResultSuccess):
            msg = (
                f"Attempted to run the workflow behind node '{self.name}'. "
                f"Failed because the flow containing the node could not be found: {flow_result.result_details}"
            )
            raise RuntimeError(msg)  # noqa: TRY004 - the lookup failed at run time; this is not a type error

        registry_key = ensure_workflow_registered(self.workflow_file_path, self.workflow_metadata)
        import_result = await GriptapeNodes.ahandle_request(
            ImportWorkflowAsReferencedSubFlowRequest(
                workflow_name=registry_key,
                flow_name=flow_result.flow_name,
                imported_flow_metadata={TRANSIENT_KEY: True},
            )
        )
        if not isinstance(import_result, ImportWorkflowAsReferencedSubFlowResultSuccess):
            msg = (
                f"Attempted to load the workflow at '{self.workflow_file_path}' for node '{self.name}'. "
                f"Failed because the workflow could not be loaded: {import_result.result_details}"
            )
            raise RuntimeError(msg)  # noqa: TRY004 - the import failed at run time; this is not a type error

        self.metadata[SUBFLOW_NAME_KEY] = import_result.created_flow_name
        return import_result.created_flow_name

    def _discard_subflow(self) -> None:
        """Delete this node's subflow if it is still live, and stop tracking it."""
        tracked = self.metadata.pop(SUBFLOW_NAME_KEY, None)
        if tracked is None:
            return
        if _get_flow_or_none(tracked) is None:
            return
        delete_result = GriptapeNodes.handle_request(DeleteFlowRequest(flow_name=tracked))
        if delete_result.failed():
            logger.warning(
                "Node '%s' could not clean up its workflow subflow '%s': %s",
                self.name,
                tracked,
                delete_result.result_details,
            )

    def _resolve_live_routes(self, subflow_name: str) -> WorkflowNodeLiveRoutes:
        """Map each surface parameter onto the parameter it addresses in the imported subflow.

        The saved shape's node names can be stale (an import renames nodes whose names are already
        taken), so the Start/End nodes are re-read from the live subflow and paired back up with
        the declared names.
        """
        flow = GriptapeNodes.FlowManager().get_flow_by_name(subflow_name)
        live_start_nodes = [node.name for node in flow.nodes.values() if isinstance(node, StartNode)]
        live_end_nodes = [node.name for node in flow.nodes.values() if isinstance(node, EndNode)]

        declared_start_nodes = _declared_route_nodes(surface.input_route for surface in self.workflow_surface.values())
        declared_end_nodes = _declared_route_nodes(surface.output_route for surface in self.workflow_surface.values())

        start_node_names = match_shape_nodes(declared_start_nodes, live_start_nodes)
        end_node_names = match_shape_nodes(declared_end_nodes, live_end_nodes)

        live_routes = WorkflowNodeLiveRoutes(inputs={}, outputs={})
        for surface_name, surface_parameter in self.workflow_surface.items():
            input_route = _relocate_route(surface_parameter.input_route, start_node_names)
            if input_route is not None:
                live_routes.inputs[surface_name] = input_route
            output_route = _relocate_route(surface_parameter.output_route, end_node_names)
            if output_route is not None:
                live_routes.outputs[surface_name] = output_route
        return live_routes

    def _apply_inputs(self, subflow_name: str, live_routes: WorkflowNodeLiveRoutes) -> None:
        for surface_name, route in live_routes.inputs.items():
            set_result = GriptapeNodes.handle_request(
                SetParameterValueRequest(
                    parameter_name=route.parameter_name,
                    node_name=route.workflow_node_name,
                    value=self.get_parameter_value(surface_name),
                )
            )
            if set_result.failed():
                msg = (
                    f"Attempted to pass '{surface_name}' into the workflow behind node '{self.name}'. "
                    f"Failed because the value could not be set on '{route.workflow_node_name}': "
                    f"{set_result.result_details}"
                )
                raise RuntimeError(msg)
        logger.debug("Node '%s' applied inputs to workflow subflow '%s'.", self.name, subflow_name)

    def _collect_outputs(self, subflow_name: str, live_routes: WorkflowNodeLiveRoutes) -> None:
        flow = GriptapeNodes.FlowManager().get_flow_by_name(subflow_name)
        for surface_name, route in live_routes.outputs.items():
            end_node = flow.nodes.get(route.workflow_node_name)
            if end_node is None:
                continue
            # A node that ran publishes to parameter_output_values; fall back to the stored value
            # for parameters an End Flow node only receives as input.
            if route.parameter_name in end_node.parameter_output_values:
                value = end_node.parameter_output_values[route.parameter_name]
            else:
                value = end_node.get_parameter_value(route.parameter_name)
            self.parameter_output_values[surface_name] = value


def build_workflow_node_class(
    *,
    node_type: str,
    workflow_file_path: Path,
    workflow_metadata: WorkflowMetadata,
) -> type[WorkflowNode]:
    """Generate a node type named `node_type` that runs the workflow at `workflow_file_path`.

    Raises:
        WorkflowNodeDefinitionError: The workflow has no saved input and output shape to build a
            parameter surface from.
    """
    surface = build_workflow_node_surface(workflow_metadata)
    return type(
        node_type,
        (WorkflowNode,),
        {
            "workflow_file_path": workflow_file_path,
            "workflow_metadata": workflow_metadata,
            "workflow_surface": surface,
            "__doc__": workflow_metadata.description or f"Runs the '{workflow_metadata.name}' workflow.",
        },
    )


def _is_control_parameter(definition: ParameterMinimalDict) -> bool:
    return definition.get("type") == ParameterTypeBuiltin.CONTROL_TYPE.value


def _build_surface_parameter(surface_name: str, surface_parameter: WorkflowNodeSurfaceParameter) -> Parameter:
    definition = surface_parameter.definition
    allowed_modes: set[ParameterMode] = set()
    if surface_parameter.input_route is not None:
        allowed_modes.add(ParameterMode.INPUT)
        if definition.get("mode_allowed_property", True):
            allowed_modes.add(ParameterMode.PROPERTY)
    if surface_parameter.output_route is not None:
        allowed_modes.add(ParameterMode.OUTPUT)

    return Parameter(
        name=surface_name,
        tooltip=definition.get("tooltip", ""),
        type=definition.get("type"),
        input_types=definition.get("input_types"),
        output_type=definition.get("output_type"),
        default_value=definition.get("default_value"),
        allowed_modes=allowed_modes,
        ui_options=definition.get("ui_options"),
    )


def _declared_route_nodes(routes: Iterable[WorkflowParameterRoute | None]) -> list[str]:
    """Return the distinct workflow node names referenced by `routes`, in first-seen order."""
    node_names: list[str] = []
    for route in routes:
        if route is not None and route.workflow_node_name not in node_names:
            node_names.append(route.workflow_node_name)
    return node_names


def _relocate_route(route: WorkflowParameterRoute | None, node_names: dict[str, str]) -> WorkflowParameterRoute | None:
    if route is None:
        return None
    live_node_name = node_names.get(route.workflow_node_name)
    if live_node_name is None:
        return None
    return route._replace(workflow_node_name=live_node_name)


def _get_flow_or_none(flow_name: str) -> ControlFlow | None:
    return GriptapeNodes.ObjectManager().attempt_get_object_by_name_as_type(flow_name, ControlFlow)
