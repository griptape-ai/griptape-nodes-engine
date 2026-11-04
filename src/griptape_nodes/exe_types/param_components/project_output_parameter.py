"""ProjectOutputParameter - shared base class for project-aware output parameter components."""

from __future__ import annotations

import abc
import typing

from griptape_nodes.exe_types import core_types
from griptape_nodes.retained_mode import retained_mode as retained_mode_mod
from griptape_nodes.retained_mode.events import connection_events
from griptape_nodes.traits import button as button_mod

if typing.TYPE_CHECKING:
    from collections.abc import Callable

    from griptape_nodes.exe_types import node_types


class ProjectOutputParameter(abc.ABC):
    """Shared base for project-aware output parameter components.

    Handles the cog-button pattern (create + connect a settings node) and
    upstream provider resolution, which are identical across all output types.
    Subclasses supply the output-type-specific behaviour via abstract properties
    and implement the ``build_xxx()`` method that returns the concrete destination.
    """

    def __init__(  # noqa: PLR0913
        self,
        node: node_types.BaseNode,
        name: str,
        *,
        default_value: str,
        situation: str,
        allowed_modes: set[core_types.ParameterMode] | None = None,
        ui_options: dict | None = None,
    ) -> None:
        """Initialise with situation context.

        Args:
            node: Parent node instance.
            name: Parameter name.
            default_value: Default value when the parameter is empty.
            situation: Situation name used to build the output path macro.
            allowed_modes: Allowed parameter modes (default: INPUT, PROPERTY).
            ui_options: Optional UI options forwarded to the generated parameter.
        """
        self._node = node
        self._name = name
        self._situation_name = situation
        self._default_value = default_value
        self._allowed_modes = allowed_modes or {core_types.ParameterMode.INPUT, core_types.ParameterMode.PROPERTY}
        self._ui_options = ui_options

    # ---- Abstract pieces each subclass must supply ----

    @property
    @abc.abstractmethod
    def _settings_node_type(self) -> str:
        """Node type to create when the cog button is clicked (e.g. 'FileOutputSettings')."""

    @property
    @abc.abstractmethod
    def _settings_value_param_name(self) -> str:
        """Parameter name on the settings node that holds the filename/dirname (e.g. 'filename')."""

    @property
    @abc.abstractmethod
    def _settings_source_param_name(self) -> str:
        """Output parameter on the settings node to wire to this parameter (e.g. 'file_destination')."""

    @property
    @abc.abstractmethod
    def _parameter_output_type(self) -> str:
        """The output_type string for the generated parameter (e.g. 'str', 'Directory')."""

    # ---- Overridable hooks ----

    def _make_parameter_traits(self) -> set:
        """Return additional traits for the parameter (e.g. a FileSystemPicker).

        Returns an empty set by default; subclasses override to add pickers.
        """
        return set()

    # ---- Shared concrete logic ----

    def add_parameter(self) -> None:
        """Create and add the output parameter to the node."""
        tooltip = f"Output path (uses '{self._situation_name}' situation template)"

        traits = self._make_parameter_traits()
        if core_types.ParameterMode.INPUT in self._allowed_modes:
            traits.add(
                button_mod.Button(
                    icon="cog",
                    size="icon",
                    variant="secondary",
                    tooltip=f"Create and connect a {self._settings_node_type} node",
                    on_click=self._on_configure_button_clicked,
                )
            )

        parameter = core_types.Parameter(
            name=self._name,
            type="str",
            default_value=self._default_value,
            allowed_modes=self._allowed_modes,
            tooltip=tooltip,
            input_types=["str"],
            output_type=self._parameter_output_type,
            traits=traits,
            ui_options=self._ui_options,
        )
        parameter.on_incoming_connection_removed.append(self._reset_to_default)

        self._node.add_parameter(parameter)

    def _reset_to_default(
        self,
        parameter: core_types.Parameter,  # noqa: ARG002
        source_node_name: str,  # noqa: ARG002
        source_parameter_name: str,  # noqa: ARG002
    ) -> None:
        self._node.set_parameter_value(self._name, self._default_value)
        self._node.publish_update_to_parameter(self._name, self._default_value)

    def _get_upstream_destination[ProviderT, DestinationT](
        self,
        provider_type: type[ProviderT],
        get_destination: Callable[[ProviderT], DestinationT | None],
        destination_type_name: str,
    ) -> DestinationT | None:
        """Return the destination from the first connected upstream node that implements ``provider_type``.

        Returns None if no connected node implements the provider protocol.

        Args:
            provider_type: ``runtime_checkable`` Protocol the upstream node must satisfy
                (e.g. ``FileDestinationProvider``).
            get_destination: Reads the destination off a matching provider.
            destination_type_name: Human-readable type name used in error messages
                (e.g. ``'FileDestination'``).

        Raises:
            ValueError: If a connected provider returns None.
        """
        result = self._node.engine.handle_request(
            connection_events.ListConnectionsForNodeRequest(node_name=self._node.name)
        )
        if not isinstance(result, connection_events.ListConnectionsForNodeResultSuccess):
            return None

        for conn in result.incoming_connections:
            if conn.target_parameter_name != self._name:
                continue
            source_node = self._node.engine.object_manager.attempt_get_object_by_name(conn.source_node_name)
            if not isinstance(source_node, provider_type):
                continue
            destination = get_destination(source_node)
            if destination is None:
                msg = (
                    f"Attempted to build {destination_type_name} for {self._node.name}.{self._name}. "
                    f"Failed because upstream node '{conn.source_node_name}' provides a "
                    f"{destination_type_name} but returned None (likely missing a filename or path)."
                )
                raise ValueError(msg)
            return destination

        return None

    def _on_configure_button_clicked(
        self,
        button: button_mod.Button,  # noqa: ARG002
        button_details: button_mod.ButtonDetailsMessagePayload,
    ) -> core_types.NodeMessageResult:
        """Create and connect the appropriate settings node to this parameter."""
        node_name = self._node.name

        has_incoming = False
        result = self._node.engine.handle_request(connection_events.ListConnectionsForNodeRequest(node_name=node_name))
        if isinstance(result, connection_events.ListConnectionsForNodeResultSuccess):
            has_incoming = any(conn.target_parameter_name == self._name for conn in result.incoming_connections)

        if has_incoming:
            return core_types.NodeMessageResult(
                success=False,
                details=f"{node_name}: {self._name} parameter already has an incoming connection",
                response=button_details,
                altered_workflow_state=False,
            )

        # TODO: https://github.com/griptape-ai/griptape-nodes/issues/4097
        # Replace with a non-RM utility for creating sibling nodes relative to a given node.
        # Until then this is the one remaining path from exe_types to the facade: RetainedMode is
        # a re-export, so the TID251 ban does not see it. It resolves the ambient engine, which is
        # correct here only because button handlers run where the node lives.
        create_result = retained_mode_mod.RetainedMode.create_node_relative_to(
            reference_node_name=node_name,
            new_node_type=self._settings_node_type,
            offset_side="left",
            offset_x=-750,
            offset_y=0,
            lock=False,
        )

        if not isinstance(create_result, str):
            return core_types.NodeMessageResult(
                success=False,
                details=f"{node_name}: Failed to create {self._settings_node_type} node",
                response=button_details,
                altered_workflow_state=False,
            )

        configure_node_name = create_result

        configure_node = self._node.engine.object_manager.attempt_get_object_by_name(configure_node_name)
        if configure_node is not None:
            configure_node.set_parameter_value("situation", self._situation_name)
            configure_node.publish_update_to_parameter("situation", self._situation_name)

            current_value = self._node.get_parameter_value(self._name)
            if isinstance(current_value, str) and current_value:
                configure_node.set_parameter_value(self._settings_value_param_name, current_value)
                configure_node.publish_update_to_parameter(self._settings_value_param_name, current_value)

        # Dispatched through this node's engine rather than RetainedMode. RetainedMode is a
        # facade re-export, so going through it resolves the ambient engine -- which the TID251
        # ban does not catch, and which would answer from a different engine than the request
        # above it.
        connection_result = self._node.engine.handle_request(
            connection_events.CreateConnectionRequest(
                source_node_name=configure_node_name,
                source_parameter_name=self._settings_source_param_name,
                target_node_name=node_name,
                target_parameter_name=self._name,
            )
        )

        if not connection_result.succeeded():
            return core_types.NodeMessageResult(
                success=False,
                details=f"{node_name}: Failed to connect {configure_node_name}.{self._settings_source_param_name} to {self._name}",
                response=button_details,
                altered_workflow_state=True,
            )

        return core_types.NodeMessageResult(
            success=True,
            details=f"{node_name}: Created and connected {configure_node_name}",
            response=button_details,
            altered_workflow_state=True,
        )
