"""ProjectFileParameter - parameter component for project-aware file saving."""

import logging

from griptape_nodes.common.project_templates.situation import BuiltInSituation
from griptape_nodes.exe_types.core_types import NodeMessageResult, Parameter, ParameterMode
from griptape_nodes.exe_types.node_types import BaseNode
from griptape_nodes.files.file import FileDestination, FileDestinationProvider
from griptape_nodes.files.project_file import ProjectFileDestination
from griptape_nodes.retained_mode.events.connection_events import (
    ListConnectionsForNodeRequest,
    ListConnectionsForNodeResultSuccess,
)
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
from griptape_nodes.retained_mode.retained_mode import RetainedMode
from griptape_nodes.traits.button import Button, ButtonDetailsMessagePayload
from griptape_nodes.traits.file_system_picker import FileSystemPicker

logger = logging.getLogger("griptape_nodes")


class ProjectFileParameter:
    """Parameter component for project-aware file saving.

    Adds a file path parameter to a node that, when processed, returns a
    FileDestination containing a MacroPath and baked-in write policy for
    deferred path resolution.

    Usage:
        # In node __init__:
        self._file_param = ProjectFileParameter(
            node=self,
            name="output_file",
            default_filename="image.png",
        )
        self._file_param.add_parameter()

        # In node process():
        output_file = self._file_param.build_file(sub_dirs="renders")
    """

    DEFAULT_SITUATION = BuiltInSituation.SAVE_NODE_OUTPUT

    def __init__(  # noqa: PLR0913
        self,
        node: BaseNode,
        name: str,
        *,
        default_filename: str,
        situation: str = DEFAULT_SITUATION,
        allowed_modes: set[ParameterMode] | None = None,
        ui_options: dict | None = None,
    ) -> None:
        """Initialize with situation context.

        Args:
            node: Parent node instance
            name: Parameter name
            default_filename: Default filename if parameter is empty
            situation: Situation name (default: "save_node_output")
            allowed_modes: Set of allowed parameter modes (default: INPUT, PROPERTY)
            ui_options: Optional UI options to pass to the generated parameter
        """
        self._node = node
        self._name = name
        self._situation_name = situation
        self._default_filename = default_filename
        self._allowed_modes = allowed_modes or {ParameterMode.INPUT, ParameterMode.PROPERTY}
        self._ui_options = ui_options

    def add_parameter(self) -> None:
        """Create and add the file path parameter to the node."""
        tooltip = f"Output filename (uses '{self._situation_name}' situation template)"

        traits: set = {
            FileSystemPicker(
                allow_files=True,
                allow_directories=False,
                allow_create=True,
            )
        }

        if ParameterMode.INPUT in self._allowed_modes:
            traits.add(
                Button(
                    icon="cog",
                    size="icon",
                    variant="secondary",
                    tooltip="Create and connect a FileOutputSettings node",
                    on_click=self._on_configure_button_clicked,
                )
            )

        parameter = Parameter(
            name=self._name,
            type="str",
            default_value=self._default_filename,
            allowed_modes=self._allowed_modes,
            tooltip=tooltip,
            input_types=["str"],
            output_type="str",
            traits=traits,
            ui_options=self._ui_options,
        )
        parameter.on_incoming_connection_removed.append(self._reset_to_default)

        self._node.add_parameter(parameter)

    def build_file(self, **extra_vars: str | int) -> FileDestination:
        """Build a FileDestination with a MacroPath from the parameter's current value.

        If an upstream node implements FileDestinationProvider (e.g., FileOutputSettings),
        its FileDestination is retrieved directly without deserializing from the wire.
        An upstream provider that returns None (misconfigured) raises instead of silently
        falling back to the default situation, since that fallback hides user intent.

        Otherwise the parameter's string value is parsed into
        file_name_base/file_extension, combined with this component's default
        situation, and wrapped in a FileDestination.

        Args:
            **extra_vars: Additional variables for the macro (e.g., sub_dirs="renders")

        Returns:
            FileDestination with a MacroPath and baked-in write policy for deferred path resolution

        Raises:
            ValueError: If an upstream FileDestinationProvider is connected but returns None.
        """
        result = GriptapeNodes.handle_request(ListConnectionsForNodeRequest(node_name=self._node.name))
        if isinstance(result, ListConnectionsForNodeResultSuccess):
            for conn in result.incoming_connections:
                if conn.target_parameter_name == self._name:
                    source_node = GriptapeNodes.ObjectManager().attempt_get_object_by_name(conn.source_node_name)
                    if isinstance(source_node, FileDestinationProvider):
                        file_dest = source_node.file_destination
                        if file_dest is None:
                            msg = (
                                f"Attempted to build file destination for {self._node.name}.{self._name}. "
                                f"Failed because upstream node '{conn.source_node_name}' provides a "
                                f"FileDestination but returned None (likely missing a filename)."
                            )
                            raise ValueError(msg)
                        return file_dest

        value = self._node.get_parameter_value(self._name)

        if isinstance(value, str) and value:
            filename = value
        else:
            filename = self._default_filename

        if "node_name" not in extra_vars:
            extra_vars["node_name"] = self._node.name

        return ProjectFileDestination.from_situation(
            filename,
            self._situation_name,
            **extra_vars,
        )

    def _reset_to_default(self, parameter: Parameter, source_node_name: str, source_parameter_name: str) -> None:  # noqa: ARG002
        self._node.set_parameter_value(self._name, self._default_filename)
        self._node.publish_update_to_parameter(self._name, self._default_filename)

    def _on_configure_button_clicked(
        self,
        button: Button,  # noqa: ARG002
        button_details: ButtonDetailsMessagePayload,
    ) -> NodeMessageResult:
        """Create and connect a FileOutputSettings node to this parameter."""
        node_name = self._node.name

        has_incoming = False
        result = GriptapeNodes.handle_request(ListConnectionsForNodeRequest(node_name=node_name))
        if isinstance(result, ListConnectionsForNodeResultSuccess):
            has_incoming = any(conn.target_parameter_name == self._name for conn in result.incoming_connections)

        if has_incoming:
            return NodeMessageResult(
                success=False,
                details=f"{node_name}: {self._name} parameter already has an incoming connection",
                response=button_details,
                altered_workflow_state=False,
            )

        # TODO: https://github.com/griptape-ai/griptape-nodes/issues/4097
        # Replace with a non-RM utility for creating sibling nodes relative to a given node.
        create_result = RetainedMode.create_node_relative_to(
            reference_node_name=node_name,
            new_node_type="FileOutputSettings",
            offset_side="left",
            offset_x=-750,
            offset_y=0,
            lock=False,
        )

        if not isinstance(create_result, str):
            return NodeMessageResult(
                success=False,
                details=f"{node_name}: Failed to create FileOutputSettings node",
                response=button_details,
                altered_workflow_state=False,
            )

        configure_node_name = create_result

        configure_node = GriptapeNodes.ObjectManager().attempt_get_object_by_name(configure_node_name)
        if configure_node is not None:
            configure_node.set_parameter_value("situation", self._situation_name)
            configure_node.publish_update_to_parameter("situation", self._situation_name)

            current_filename = self._node.get_parameter_value(self._name)
            if isinstance(current_filename, str) and current_filename:
                configure_node.set_parameter_value("filename", current_filename)
                configure_node.publish_update_to_parameter("filename", current_filename)

        connection_result = RetainedMode.connect(
            source=f"{configure_node_name}.file_destination",
            destination=f"{node_name}.{self._name}",
        )

        if not connection_result.succeeded():
            return NodeMessageResult(
                success=False,
                details=f"{node_name}: Failed to connect {configure_node_name}.file_destination to {self._name}",
                response=button_details,
                altered_workflow_state=True,
            )

        return NodeMessageResult(
            success=True,
            details=f"{node_name}: Created and connected {configure_node_name}",
            response=button_details,
            altered_workflow_state=True,
        )
