"""Parameters that carry execution flow instead of data."""

from __future__ import annotations

import logging
from abc import ABC
from typing import TYPE_CHECKING

from griptape_nodes.exe_types.elements.parameter import Parameter
from griptape_nodes.exe_types.elements.parameter_types import (
    VALID_PARAMETER_RENDER_LOCATIONS,
    ParameterMode,
    ParameterTypeBuiltin,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Any

    from griptape_nodes.exe_types.elements.parameter_types import ParameterRenderLocation
    from griptape_nodes.exe_types.elements.trait import Trait

logger = logging.getLogger("griptape_nodes")


# Convenience classes to reduce boilerplate in node definitions
class ControlParameter(Parameter, ABC):
    def __init__(  # noqa: PLR0913, PLR0917
        self,
        name: str,
        tooltip: str | list[dict],
        input_types: list[str] | None = None,
        output_type: str | None = None,
        tooltip_as_input: str | list[dict] | None = None,
        tooltip_as_property: str | list[dict] | None = None,
        tooltip_as_output: str | list[dict] | None = None,
        allowed_modes: set[ParameterMode] | None = None,
        traits: set[type[Trait] | Trait] | None = None,
        converters: list[Callable[[Any], Any]] | None = None,
        validators: list[Callable[[Parameter, Any], None]] | None = None,
        ui_options: dict | None = None,
        *,
        parameter_render_location: ParameterRenderLocation | None = None,
        display_name: str | None = None,
        user_defined: bool = False,
        private: bool = False,
        exclude_from_metadata: bool = False,
    ):
        # Process ui_options before passing to parent
        if ui_options is None:
            processed_ui_options = {}
        else:
            processed_ui_options = ui_options.copy()

        # Validate for conflicts if explicitly provided
        if parameter_render_location is not None:
            self._validate_ui_option_conflict(
                ui_options_dict=processed_ui_options,
                param_name="parameter_render_location",
                param_value=parameter_render_location,
            )
            # Validate it's a valid value
            if parameter_render_location not in VALID_PARAMETER_RENDER_LOCATIONS:
                msg = f"Invalid parameter_render_location '{parameter_render_location}' for parameter '{name}'. Valid values: {sorted(VALID_PARAMETER_RENDER_LOCATIONS)}. Using 'top'."
                logger.warning(msg)
                parameter_render_location = "top"

        # By default, the editor renders all control parameters at the top of the node.
        # Set parameter_render_location to control where they render:
        # - "top": Render at the top of the node (default for ControlParameter)
        # - "bottom": Render at the bottom of the node
        # - "in-order": Render in-order in the order they are defined
        # This is useful for nodes with multiple control outputs that need to appear in specific positions.
        if "parameter_render_location" not in processed_ui_options:
            if parameter_render_location is not None:
                processed_ui_options["parameter_render_location"] = parameter_render_location
            else:
                processed_ui_options["parameter_render_location"] = "top"

        # Call parent with a few explicit tweaks.
        super().__init__(
            type=ParameterTypeBuiltin.CONTROL_TYPE.value,
            default_value=None,
            settable=True,
            name=name,
            tooltip=tooltip,
            input_types=input_types,
            output_type=output_type,
            tooltip_as_input=tooltip_as_input,
            tooltip_as_property=tooltip_as_property,
            tooltip_as_output=tooltip_as_output,
            allowed_modes=allowed_modes,
            traits=traits,
            converters=converters,
            validators=validators,
            ui_options=processed_ui_options,
            display_name=display_name,
            user_defined=user_defined,
            private=private,
            exclude_from_metadata=exclude_from_metadata,
            element_type=self.__class__.__name__,
        )


class ControlParameterInput(ControlParameter):
    def __init__(  # noqa: PLR0913, PLR0917
        self,
        tooltip: str | list[dict] = "Connection from previous node in the execution chain",
        name: str = "exec_in",
        display_name: str | None = "Flow In",
        tooltip_as_input: str | list[dict] | None = None,
        tooltip_as_property: str | list[dict] | None = None,
        tooltip_as_output: str | list[dict] | None = None,
        traits: set[type[Trait] | Trait] | None = None,
        converters: list[Callable[[Any], Any]] | None = None,
        validators: list[Callable[[Parameter, Any], None]] | None = None,
        *,
        parameter_render_location: ParameterRenderLocation | None = None,
        user_defined: bool = False,
        private: bool = False,
        exclude_from_metadata: bool = False,
    ):
        allowed_modes = {ParameterMode.INPUT}
        input_types = [ParameterTypeBuiltin.CONTROL_TYPE.value]

        # Call parent with a few explicit tweaks.
        super().__init__(
            name=name,
            tooltip=tooltip,
            input_types=input_types,
            output_type=None,
            tooltip_as_input=tooltip_as_input,
            tooltip_as_property=tooltip_as_property,
            tooltip_as_output=tooltip_as_output,
            allowed_modes=allowed_modes,
            traits=traits,
            converters=converters,
            validators=validators,
            parameter_render_location=parameter_render_location,
            display_name=display_name,
            user_defined=user_defined,
            private=private,
            exclude_from_metadata=exclude_from_metadata,
        )


class ControlParameterOutput(ControlParameter):
    def __init__(  # noqa: PLR0913, PLR0917
        self,
        tooltip: str | list[dict] = "Connection to the next node in the execution chain",
        name: str = "exec_out",
        display_name: str | None = "Flow Out",
        tooltip_as_input: str | list[dict] | None = None,
        tooltip_as_property: str | list[dict] | None = None,
        tooltip_as_output: str | list[dict] | None = None,
        traits: set[type[Trait] | Trait] | None = None,
        converters: list[Callable[[Any], Any]] | None = None,
        validators: list[Callable[[Parameter, Any], None]] | None = None,
        *,
        parameter_render_location: ParameterRenderLocation | None = None,
        user_defined: bool = False,
        private: bool = False,
        exclude_from_metadata: bool = False,
    ):
        allowed_modes = {ParameterMode.OUTPUT}
        output_type = ParameterTypeBuiltin.CONTROL_TYPE.value

        # Call parent with a few explicit tweaks.
        super().__init__(
            name=name,
            tooltip=tooltip,
            input_types=None,
            output_type=output_type,
            tooltip_as_input=tooltip_as_input,
            tooltip_as_property=tooltip_as_property,
            tooltip_as_output=tooltip_as_output,
            allowed_modes=allowed_modes,
            traits=traits,
            converters=converters,
            validators=validators,
            parameter_render_location=parameter_render_location,
            display_name=display_name,
            user_defined=user_defined,
            private=private,
            exclude_from_metadata=exclude_from_metadata,
        )
