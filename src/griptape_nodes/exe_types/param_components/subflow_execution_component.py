from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from griptape_nodes.exe_types.core_types import NodeMessageResult, Parameter, ParameterMode
from griptape_nodes.exe_types.node_types import LOCAL_EXECUTION, PRIVATE_EXECUTION
from griptape_nodes.retained_mode.events import converters
from griptape_nodes.retained_mode.events.base_events import (
    EventResultSuccess,
    ExecutionEvent,
)
from griptape_nodes.retained_mode.events.execution_events import (
    ControlFlowCancelledEvent,
    ControlFlowResolvedEvent,
    GriptapeEvent,
    NodeFinishProcessEvent,
    NodeResolvedEvent,
    NodeStartProcessEvent,
)
from griptape_nodes.retained_mode.events.workflow_events import (
    PublishWorkflowProgressEvent,
    PublishWorkflowResultFailure,
    PublishWorkflowResultSuccess,
)
from griptape_nodes.traits.button import Button, ButtonDetailsMessagePayload, OnClickMessageResultPayload

if TYPE_CHECKING:
    from griptape_nodes.exe_types.node_types import BaseNode

logger = logging.getLogger(__name__)


class SubflowExecutionComponent:
    """A reusable component for managing subprocess execution event parameters.

    This component creates and manages parameters that display
    real-time events from subprocess execution in the GUI.
    """

    def __init__(self, node: BaseNode) -> None:
        """Initialize the SubflowExecutionComponent.

        Args:
            node: The node instance that will own the parameter
        """
        self._node = node

    def add_output_parameters(self) -> None:
        """Add the parameters to the node."""
        self._node.add_parameter(
            Parameter(
                name="publishing_progress",
                output_type="float",
                allowed_modes={ParameterMode.PROPERTY},
                tooltip="Progress bar showing workflow publishing completion (0.0 to 1.0)",
                ui_options={"progress_bar": True},
                settable=False,
                hide=True,
            )
        )
        self._node.add_parameter(
            Parameter(
                name="publishing_target_link",
                output_type="str",
                tooltip="Click the button to open the published workflow location",
                hide=True,
                allowed_modes={ParameterMode.PROPERTY},
                traits={
                    Button(
                        icon="link",
                        on_click=self._handle_get_publishing_target_link,
                        tooltip="Open publishing target link",
                        state="normal",
                    ),
                },
                ui_options={"placeholder_text": "Link will appear after publishing"},
            )
        )
        self._node.add_parameter(
            Parameter(
                name="execution_events",
                output_type="str",
                allowed_modes={ParameterMode.PROPERTY},
                tooltip="Real-time events from subprocess execution",
                ui_options={"multiline": True},
            )
        )
        if "execution_panel" not in self._node.metadata:
            self._node.metadata["execution_panel"] = {"params": []}
        self._node.metadata["execution_panel"]["params"].append("execution_events")
        self._node.metadata["execution_panel"]["params"].append("publishing_progress")
        self._node.metadata["execution_panel"]["params"].append("publishing_target_link")

    def _handle_get_publishing_target_link(
        self,
        button: Button,  # noqa: ARG002
        button_details: ButtonDetailsMessagePayload,
    ) -> NodeMessageResult | None:
        publishing_target_link = self._node.get_parameter_value("publishing_target_link")
        if publishing_target_link:
            return NodeMessageResult(
                success=True,
                details="Publishing target link retrieved successfully.",
                response=OnClickMessageResultPayload(
                    button_details=button_details,
                    href=publishing_target_link,
                ),
                altered_workflow_state=False,
            )
        return None

    def clear_execution_state(self) -> None:
        """Clear the component state."""
        self.reset_publishing_progress()
        self.clear_events()
        self.clear_publishing_target_link()

    def clear_events(self) -> None:
        """Clear events at start of execution."""
        self._node.publish_update_to_parameter("execution_events", "")

    def clear_publishing_target_link(self) -> None:
        """Clear the publishing target link parameter."""
        self._node.set_parameter_value("publishing_target_link", None)

    def append_event(self, event_str: str) -> None:
        """Append a stringified event to the parameter.

        Args:
            event_str: The event string to append
        """
        self._node.append_value_to_parameter("execution_events", event_str + "\n")

    def after_value_set(self, parameter: Parameter, value: Any) -> None:
        """Handle actions after a parameter value is set.

        Args:
            parameter: The parameter that was set
            value: The new value of the parameter
        """
        if parameter.name == "execution_environment":
            if value in {LOCAL_EXECUTION, PRIVATE_EXECUTION}:
                self._node.hide_parameter_by_name("publishing_progress")
                self._node.hide_parameter_by_name("publishing_target_link")
            else:
                self._node.show_parameter_by_name("publishing_progress")

        if parameter.name == "publishing_target_link":
            if value and self._node.get_parameter_value("execution_environment") not in {
                LOCAL_EXECUTION,
                PRIVATE_EXECUTION,
            }:
                self._node.show_parameter_by_name("publishing_target_link")
            else:
                self._node.hide_parameter_by_name("publishing_target_link")

    def reset_publishing_progress(self) -> None:
        """Reset the publishing progress bar to 0."""
        self._node.publish_update_to_parameter("publishing_progress", 0.0)

    def _parse_execution_event(self, event: dict) -> ExecutionEvent | None:
        """Parse an execution event dictionary into an ExecutionEvent object.

        Args:
            event: The event dictionary containing the execution event data.
                   Expected to have type="execution_event" and a payload with payload_type.

        Returns:
            The parsed ExecutionEvent if successful, None if the event cannot be parsed
            (wrong type, unknown payload type, etc.)
        """
        event_type = event.get("type", "unknown")
        if event_type != "execution_event":
            return None

        payload = event.get("payload", {})
        return converters.engine.structure(payload, ExecutionEvent)

    def handle_publishing_event(self, event: dict) -> None:
        """Handle events from SubprocessWorkflowPublisher.

        Processes publishing events and updates the GUI with relevant information.
        Handles PublishWorkflowProgressEvent for progress bar updates, and
        PublishWorkflowResultSuccess/Failure for completion status.

        Args:
            event: The event dictionary from the subprocess publisher
        """
        event_type = event.get("type", "unknown")

        # Handle result events (success/failure)
        if event_type in ("success_result", "failure_result"):
            self._handle_publishing_result_event(event)
            return

        # Handle execution events (progress updates)
        ex_event = self._parse_execution_event(event)
        if ex_event is None:
            return

        if isinstance(ex_event.payload, PublishWorkflowProgressEvent):
            # Update progress bar (convert from 0-100 to 0.0-1.0)
            progress_value = min(1.0, max(0.0, ex_event.payload.progress / 100.0))
            self._node.publish_update_to_parameter("publishing_progress", progress_value)

            # Also append a user-friendly message if provided
            if ex_event.payload.message:
                self.append_event(f"Publishing: {ex_event.payload.message} ({ex_event.payload.progress:.0f}%)")

    def _handle_publishing_result_event(self, event: dict) -> None:
        """Handle publishing result events (success/failure).

        Args:
            event: The event dictionary containing the result
        """
        payload = event.get("payload", {})
        result_data = payload.get("result", {})

        event_result = converters.engine.structure(payload, EventResultSuccess)
        if isinstance(event_result.result, PublishWorkflowResultSuccess):
            publish_workflow_result_success = event_result.result
            target_link = (
                publish_workflow_result_success.metadata.get("publish_target_link")
                if publish_workflow_result_success.metadata
                else None
            )
            if target_link:
                self._node.set_parameter_value("publishing_target_link", target_link)

        elif isinstance(event_result.result, PublishWorkflowResultFailure):
            result_details = result_data.get("result_details", "Unknown error")
            self.append_event(f"Publishing failed: {result_details}")

    def handle_execution_event(self, event: dict) -> None:
        """Handle events from SubprocessWorkflowExecutor.

        Processes execution events and updates the GUI with relevant information.
        Filters to only display relevant events with formatted messages.

        Args:
            event: The event dictionary from the subprocess executor
        """
        ex_event = self._parse_execution_event(event)

        if ex_event is None:
            return

        formatted_message = self._format_execution_event(ex_event)
        if formatted_message is not None:
            self.append_event(formatted_message)

    def _format_execution_event(self, ex_event: ExecutionEvent) -> str | None:
        """Format an execution event into a user-friendly message.

        Args:
            ex_event: The parsed ExecutionEvent

        Returns:
            A formatted string message, or None if the event should be filtered out
        """
        payload = ex_event.payload
        payload_type = type(payload)

        # Map payload types to their formatting functions
        formatters = {
            NodeStartProcessEvent: self._format_node_start_process,
            NodeFinishProcessEvent: self._format_node_finish_process,
            NodeResolvedEvent: self._format_node_resolved,
            ControlFlowResolvedEvent: self._format_control_flow_resolved,
            ControlFlowCancelledEvent: self._format_control_flow_cancelled,
            GriptapeEvent: self._format_griptape_event,
        }

        formatter = formatters.get(payload_type)
        if formatter is None:
            # Filter out other event types
            return None

        return formatter(payload)

    def _format_node_start_process(self, payload: NodeStartProcessEvent) -> str:
        """Format a NodeStartProcessEvent."""
        return f"Starting: {payload.node_name}"

    def _format_node_finish_process(self, payload: NodeFinishProcessEvent) -> str:
        """Format a NodeFinishProcessEvent."""
        return f"Finished: {payload.node_name}"

    def _format_node_resolved(self, payload: NodeResolvedEvent) -> str:
        """Format a NodeResolvedEvent."""
        return f"Resolved: {payload.node_name}"

    def _format_control_flow_resolved(self, payload: ControlFlowResolvedEvent) -> str:
        """Format a ControlFlowResolvedEvent."""
        return f"Flow completed: {payload.end_node_name}"

    def _format_control_flow_cancelled(self, payload: ControlFlowCancelledEvent) -> str:
        """Format a ControlFlowCancelledEvent."""
        details = payload.result_details or "Unknown error"
        return f"Flow cancelled: {details}"

    def _format_griptape_event(self, payload: GriptapeEvent) -> str | None:
        """Format a GriptapeEvent (progress event).

        Only formats events for the 'result_details' parameter, filtering out others.
        """
        if payload.parameter_name != "result_details":
            return None

        return f"{payload.node_name}: {payload.value}"
