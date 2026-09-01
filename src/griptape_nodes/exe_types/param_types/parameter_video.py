"""ParameterVideo component for video inputs with enhanced UI options."""

from collections.abc import Callable
from typing import Any

from griptape_nodes.exe_types.core_types import BadgeData, Parameter, ParameterMode, Trait
from griptape_nodes.utils.artifact_normalization import normalize_artifact_input


class ParameterVideo(Parameter):
    """A specialized Parameter class for video inputs with enhanced UI options.

    This class provides a convenient way to create video parameters with common
    UI customizations like file browser, webcam capture, and video editing.
    It exposes these UI options as direct properties for easy runtime modification.

    Example:
        param = ParameterVideo(
            name="input_video",
            tooltip="Select a video",
            clickable_file_browser=True,
            webcam_capture_video=True,
            edit_video=True
        )
        param.pulse_on_run = True  # Change UI options at runtime
    """

    def __init__(  # noqa: C901, PLR0913
        self,
        name: str,
        tooltip: str | None = None,
        *,
        type: str = "VideoUrlArtifact",  # noqa: A002, ARG002
        input_types: list[str] | None = None,  # noqa: ARG002
        output_type: str = "VideoUrlArtifact",  # noqa: ARG002
        default_value: Any = None,
        tooltip_as_input: str | None = None,
        tooltip_as_property: str | None = None,
        tooltip_as_output: str | None = None,
        allowed_modes: set[ParameterMode] | None = None,
        traits: set[type[Trait] | Trait] | None = None,
        converters: list[Callable[[Any], Any]] | None = None,
        validators: list[Callable[[Parameter, Any], None]] | None = None,
        ui_options: dict | None = None,
        pulse_on_run: bool = False,
        clickable_file_browser: bool = True,
        webcam_capture_video: bool = False,
        edit_video: bool = False,
        accept_any: bool = True,
        hide: bool | None = None,
        hide_label: bool = False,
        hide_property: bool = False,
        display_name: str | None = None,
        allow_input: bool = True,
        allow_property: bool = True,
        allow_output: bool = True,
        settable: bool = True,
        serializable: bool = True,
        user_defined: bool = False,
        private: bool = False,
        exclude_from_metadata: bool = False,
        element_id: str | None = None,
        element_type: str | None = None,
        parent_container_name: str | None = None,
        badge: BadgeData | None = None,
    ) -> None:
        """Initialize a video parameter with enhanced UI options.

        Args:
            name: Parameter name
            tooltip: Parameter tooltip
            type: Parameter type (ignored, always "VideoUrlArtifact" for ParameterVideo)
            input_types: Allowed input types (ignored, set based on accept_any)
            output_type: Output type (ignored, always "VideoUrlArtifact" for ParameterVideo)
            default_value: Default parameter value
            tooltip_as_input: Tooltip for input mode
            tooltip_as_property: Tooltip for property mode
            tooltip_as_output: Tooltip for output mode
            allowed_modes: Allowed parameter modes
            traits: Parameter traits
            converters: Parameter converters
            validators: Parameter validators
            ui_options: Dictionary of UI options
            pulse_on_run: Whether to pulse the parameter on run
            clickable_file_browser: Whether to show clickable file browser
            webcam_capture_video: Whether to enable webcam capture
            edit_video: Whether to enable video editing functionality
            accept_any: Whether to accept any input type and convert to video (default: True)
            hide: Whether to hide the entire parameter
            hide_label: Whether to hide the parameter label
            hide_property: Whether to hide the parameter in property mode
            display_name: Human-readable label for the parameter (overrides default parameter name)
            allow_input: Whether to allow input mode
            allow_property: Whether to allow property mode
            allow_output: Whether to allow output mode
            settable: Whether the parameter is settable
            serializable: Whether the parameter is serializable
            user_defined: Whether the parameter is user-defined
            private: Whether this parameter is private
            exclude_from_metadata: Whether this parameter is excluded from plaintext sidecar and image metadata
            element_id: Element ID
            element_type: Element type
            parent_container_name: Name of parent container
            badge: Optional BadgeData for initial badge (title, message, variant, and whether to show a clear button).
        """
        # Build ui_options dictionary from the provided UI-specific parameters
        if ui_options is None:
            ui_options = {}
        else:
            ui_options = ui_options.copy()

        # Add video-specific UI options if they have values
        if pulse_on_run:
            ui_options["pulse_on_run"] = pulse_on_run
        if clickable_file_browser:
            ui_options["clickable_file_browser"] = clickable_file_browser
        if webcam_capture_video:
            ui_options["webcam_capture_video"] = webcam_capture_video
        if edit_video:
            ui_options["edit_video"] = edit_video

        # Auto-disable clickable_file_browser if neither input nor property modes are allowed
        if not allow_input and not allow_property and clickable_file_browser:
            ui_options.pop("clickable_file_browser", None)

        # Set up input types based on accept_any setting
        if accept_any:
            final_input_types = ["any"]
        else:
            final_input_types = ["VideoUrlArtifact"]

        # Add automatic converter to normalize string inputs to VideoUrlArtifact
        # This allows ParameterVideo to automatically handle file paths and localhost URLs
        video_converters = list(converters) if converters else []
        if accept_any:
            # Create a converter function that uses normalize_artifact_input with VideoUrlArtifact
            def _normalize_video(value: Any) -> Any:
                try:
                    from griptape.artifacts import VideoUrlArtifact
                except ImportError:
                    return value
                return normalize_artifact_input(value, VideoUrlArtifact)

            video_converters.insert(0, _normalize_video)

        # Call parent with explicit parameters, following ControlParameter pattern
        super().__init__(
            name=name,
            tooltip=tooltip,
            type="VideoUrlArtifact",  # Always a VideoUrlArtifact type for ParameterVideo
            input_types=final_input_types,
            output_type="VideoUrlArtifact",  # Always output as VideoUrlArtifact
            default_value=default_value,
            tooltip_as_input=tooltip_as_input,
            tooltip_as_property=tooltip_as_property,
            tooltip_as_output=tooltip_as_output,
            allowed_modes=allowed_modes,
            traits=traits,
            converters=video_converters,
            validators=validators,
            ui_options=ui_options,
            hide=hide,
            hide_label=hide_label,
            hide_property=hide_property,
            display_name=display_name,
            allow_input=allow_input,
            allow_property=allow_property,
            allow_output=allow_output,
            settable=settable,
            serializable=serializable,
            user_defined=user_defined,
            private=private,
            exclude_from_metadata=exclude_from_metadata,
            element_id=element_id,
            element_type=element_type,
            parent_container_name=parent_container_name,
            badge=badge,
        )

    @property
    def pulse_on_run(self) -> bool:
        """Get whether pulse on run is enabled.

        Returns:
            True if pulse on run is enabled, False otherwise
        """
        return self.ui_options.get("pulse_on_run", False)

    @pulse_on_run.setter
    def pulse_on_run(self, value: bool) -> None:
        """Set whether pulse on run is enabled.

        Args:
            value: Whether to enable pulse on run
        """
        if value:
            self.update_ui_options_key("pulse_on_run", value)
        else:
            ui_options = self.ui_options.copy()
            ui_options.pop("pulse_on_run", None)
            self.ui_options = ui_options

    @property
    def clickable_file_browser(self) -> bool:
        """Get whether clickable file browser is enabled.

        Returns:
            True if clickable file browser is enabled, False otherwise
        """
        return self.ui_options.get("clickable_file_browser", False)

    @clickable_file_browser.setter
    def clickable_file_browser(self, value: bool) -> None:
        """Set whether clickable file browser is enabled.

        Args:
            value: Whether to enable clickable file browser
        """
        if value:
            self.update_ui_options_key("clickable_file_browser", value)
        else:
            ui_options = self.ui_options.copy()
            ui_options.pop("clickable_file_browser", None)
            self.ui_options = ui_options

    @property
    def webcam_capture_video(self) -> bool:
        """Get whether webcam capture video is enabled.

        Returns:
            True if webcam capture video is enabled, False otherwise
        """
        return self.ui_options.get("webcam_capture_video", False)

    @webcam_capture_video.setter
    def webcam_capture_video(self, value: bool) -> None:
        """Set whether webcam capture video is enabled.

        Args:
            value: Whether to enable webcam capture video
        """
        if value:
            self.update_ui_options_key("webcam_capture_video", value)
        else:
            ui_options = self.ui_options.copy()
            ui_options.pop("webcam_capture_video", None)
            self.ui_options = ui_options

    @property
    def edit_video(self) -> bool:
        """Get whether edit video is enabled.

        Returns:
            True if edit video is enabled, False otherwise
        """
        return self.ui_options.get("edit_video", False)

    @edit_video.setter
    def edit_video(self, value: bool) -> None:
        """Set whether edit video is enabled.

        Args:
            value: Whether to enable edit video
        """
        if value:
            self.update_ui_options_key("edit_video", value)
        else:
            ui_options = self.ui_options.copy()
            ui_options.pop("edit_video", None)
            self.ui_options = ui_options
