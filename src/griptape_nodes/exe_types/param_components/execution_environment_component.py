from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from griptape_nodes.exe_types.core_types import (
    ControlParameter,
    Parameter,
    ParameterMode,
    ParameterTypeBuiltin,
    Trait,
)
from griptape_nodes.exe_types.node_types import (
    LOCAL_EXECUTION,
    get_library_names_with_publish_handlers,
)
from griptape_nodes.exe_types.param_components.subflow_execution_component import SubflowExecutionComponent
from griptape_nodes.node_library.library_registry import LibraryRegistry
from griptape_nodes.retained_mode.events.workflow_events import (
    PublishWorkflowRegisteredEventData,
    PublishWorkflowRequest,
)
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
from griptape_nodes.traits.options import Options

if TYPE_CHECKING:
    from griptape_nodes.exe_types.node_types import BaseNode

logger = logging.getLogger("griptape_nodes")

EXECUTION_ENVIRONMENT_PARAMETER_NAME = "execution_environment"
EXECUTION_ENVIRONMENT_METADATA_KEY = "execution_environment"
DEFAULT_START_FLOW_LIBRARY_NAME = "Griptape Nodes Library"
DEFAULT_START_FLOW_NODE_TYPE = "StartFlow"


class ExecutionEnvironmentComponent:
    """Lets a node that runs an inner flow choose where that flow executes.

    Adds the execution_environment dropdown, one prefixed copy of every registered library StartFlow
    parameter (e.g. deadlinecloudstartflow_job_name), the metadata["execution_environment"] map the
    editor's settings panel and the packaging code read, and the live execution status parameters.
    """

    def __init__(self, node: BaseNode, tooltip: str) -> None:
        self._node = node
        self._parameter = Parameter(
            name=EXECUTION_ENVIRONMENT_PARAMETER_NAME,
            tooltip=tooltip,
            type=ParameterTypeBuiltin.STR,
            allowed_modes={ParameterMode.PROPERTY},
            default_value=LOCAL_EXECUTION,
            traits={Options(choices=get_library_names_with_publish_handlers())},
        )
        self._subflow_execution_component = SubflowExecutionComponent(node)

    @property
    def parameter(self) -> Parameter:
        return self._parameter

    @property
    def subflow_execution_component(self) -> SubflowExecutionComponent:
        return self._subflow_execution_component

    def add_parameters(self) -> None:
        self._node.add_parameter(self._parameter)
        environment_metadata = self._node.metadata.setdefault(EXECUTION_ENVIRONMENT_METADATA_KEY, {})
        environment_metadata[DEFAULT_START_FLOW_LIBRARY_NAME] = {
            "start_flow_node": DEFAULT_START_FLOW_NODE_TYPE,
            "parameter_names": {},
        }
        self._node.metadata["executable"] = True

        library_manager = GriptapeNodes.LibraryManager()
        event_handlers = library_manager.get_registered_event_handlers(PublishWorkflowRequest)
        for library_name, handler in event_handlers.items():
            self._add_library_start_flow_parameters(library_name, handler)

        self._subflow_execution_component.add_output_parameters()

    def get_execution_environment(self) -> str:
        return self._node.get_parameter_value(self._parameter.name)

    def after_value_set(self, parameter: Parameter, value: Any) -> None:
        self._subflow_execution_component.after_value_set(parameter, value)

    def _add_library_start_flow_parameters(self, library_name: str, handler: Any) -> None:
        registered_event_data = handler.event_data
        if not isinstance(registered_event_data, PublishWorkflowRegisteredEventData):
            return

        start_flow_node_type = registered_event_data.start_flow_node_type
        start_flow_library_name = registered_event_data.start_flow_node_library_name

        try:
            library = LibraryRegistry.get_library(name=start_flow_library_name)
        except KeyError:
            logger.debug(
                "Library '%s' not found when adding StartFlow parameters for '%s'",
                start_flow_library_name,
                library_name,
            )
            return

        try:
            temp_start_flow_node = library.create_node(
                node_type=start_flow_node_type,
                name=f"temp_{start_flow_node_type}",
            )
        except Exception as e:
            # Library node constructors are third-party code and can fail in any way.
            logger.debug(
                "Failed to create temporary StartFlow node '%s' from library '%s': %s",
                start_flow_node_type,
                start_flow_library_name,
                e,
            )
            return

        class_name_prefix = start_flow_node_type.lower()
        parameter_names = []
        for param in temp_start_flow_node.parameters:
            if isinstance(param, ControlParameter):
                continue
            prefixed_param_name = f"{class_name_prefix}_{param.name}"
            parameter_names.append(prefixed_param_name)
            self._node.add_parameter(_clone_parameter(param, prefixed_param_name))

        self._node.metadata[EXECUTION_ENVIRONMENT_METADATA_KEY][library_name] = {
            "start_flow_node": start_flow_node_type,
            "parameter_names": parameter_names,
        }


def _clone_parameter(param: Parameter, new_name: str) -> Parameter:
    traits_set: set[type[Trait] | Trait] | None = {child for child in param.children if isinstance(child, Trait)}
    if not traits_set:
        traits_set = None

    return Parameter(
        name=new_name,
        tooltip=param.tooltip,
        type=param.type,
        allowed_modes=param.allowed_modes,
        default_value=param.default_value,
        traits=traits_set,
        parent_container_name=param.parent_container_name,
        parent_element_name=param.parent_element_name,
    )
