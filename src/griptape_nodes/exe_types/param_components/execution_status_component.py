from typing import Any

from griptape_nodes.exe_types.core_types import (
    Parameter,
    ParameterGroup,
    ParameterMode,
)


class ExecutionStatusComponent:
    """A reusable component for managing execution status parameters.

    This component creates and manages a "Status" ParameterGroup containing:
    - was_successful: Boolean parameter indicating success/failure
    - result_details: String parameter with operation details

    The component can be customized for different parameter modes to support
    various node types (EndNode uses INPUT/PROPERTY, SuccessFailureNode uses OUTPUT).
    """

    def __init__(  # noqa: PLR0913
        self,
        node: Any,  # BaseNode type, but avoiding circular import
        *,
        was_successful_modes: set[ParameterMode],
        result_details_modes: set[ParameterMode],
        parameter_group_initially_collapsed: bool = True,
        result_details_tooltip: str = "Details about the operation result",
        result_details_placeholder: str = "Details on the operation will be presented here.",
    ) -> None:
        """Initialize the ExecutionStatusComponent and create the parameters immediately.

        Args:
            node: The node instance that will own these parameters
            was_successful_modes: Set of ParameterModes for was_successful parameter
            result_details_modes: Set of ParameterModes for result_details parameter
            parameter_group_initially_collapsed: Whether the Status group should start collapsed
            result_details_tooltip: Custom tooltip for result_details parameter
            result_details_placeholder: Custom placeholder text for result_details parameter
        """
        self._node = node

        # Create the Status ParameterGroup
        self._status_group = ParameterGroup(name="Status")
        self._status_group.ui_options = {"collapsed": parameter_group_initially_collapsed}

        # Boolean parameter to indicate success/failure
        self._was_successful = Parameter(
            name="was_successful",
            tooltip="Indicates whether it completed without errors.",
            type="bool",
            default_value=False,
            settable=False,
            allowed_modes=was_successful_modes,
        )

        # Result details parameter with multiline option
        self._result_details = Parameter(
            name="result_details",
            tooltip=result_details_tooltip,
            type="str",
            default_value=None,
            allowed_modes=result_details_modes,
            settable=False,
            ui_options={
                "multiline": True,
                "placeholder_text": result_details_placeholder,
            },
        )

        # Add parameters to the group
        self._status_group.add_child(self._was_successful)
        self._status_group.add_child(self._result_details)

        # Add the group to the node
        self._node.add_node_element(self._status_group)

    def get_parameter_group(self) -> ParameterGroup:
        """Get the Status ParameterGroup.

        Returns:
            ParameterGroup: The Status group containing was_successful and result_details
        """
        return self._status_group

    def _update_status_group_display_label(self, *, show_warning: bool) -> None:
        """Update the Status ParameterGroup display label with optional warning emoji."""
        if show_warning:
            self._status_group.update_ui_options_key("display_name", "⚠️ Status")
            return

        # Remove display_name to use default "Status" from name
        self._status_group.remove_ui_options_key("display_name")

    def set_execution_result(self, *, was_successful: bool, result_details: str) -> None:
        """Set the execution result values.

        Args:
            was_successful: Whether the operation succeeded
            result_details: Details about the operation result
        """
        self._update_parameter_value(self._was_successful, was_successful)
        self._update_parameter_value(self._result_details, result_details)
        # Update display label: show warning emoji only on actual failure (not placeholder/initialization messages)
        # While string matching is not the best practice, it's currently the only way to check for placeholder messages.
        # TODO: use a better approach to check for placeholder messages. https://github.com/griptape-ai/griptape-nodes/issues/3686
        is_placeholder = result_details in (
            "<Results will appear when the node executes>",
            "Beginning execution...",
            "",
        )
        show_warning = not was_successful and not is_placeholder
        self._update_status_group_display_label(show_warning=show_warning)

    def clear_execution_status(self, initial_message: str | None = None) -> None:
        """Clear execution status and reset parameters.

        Args:
            initial_message: Initial message to set in result_details. If None, clears result_details entirely.
        """
        if initial_message is None:
            initial_message = ""
        self.set_execution_result(was_successful=False, result_details=initial_message)
        # Reset to "Status" during execution (not a failure yet)
        self._update_status_group_display_label(show_warning=False)

    def append_to_result_details(self, additional_text: str, separator: str = "\n") -> None:
        """Append text to the existing result_details.

        Args:
            additional_text: Text to append to the current result_details
            separator: Separator to use between existing and new text (default: newline)
        """
        # Get current result_details value
        current_details = self._node.get_parameter_value(self._result_details.name)

        # Append the new text
        if current_details:
            updated_details = f"{current_details}{separator}{additional_text}"
        else:
            updated_details = additional_text

        # Use consolidated update method
        self._update_parameter_value(self._result_details, updated_details)

    def _update_parameter_value(self, parameter: Parameter, value: Any) -> None:
        """Update a parameter value with all necessary operations.

        Args:
            parameter: The parameter to update
            value: The new value to set
        """
        # ALWAYS set parameter value and publish update
        self._node.set_parameter_value(parameter.name, value)
        self._node.publish_update_to_parameter(parameter.name, value)

        # ONLY set output values if the parameter mode is OUTPUT
        if ParameterMode.OUTPUT in parameter.get_mode():
            self._node.parameter_output_values[parameter.name] = value
