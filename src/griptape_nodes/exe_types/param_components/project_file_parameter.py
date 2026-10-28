"""ProjectFileParameter - parameter component for project-aware file saving.

Wraps a MacroPath into a FileDestination with a baked-in write policy, deferring
path resolution to execution time via the situation-based macro system.
"""

from pathlib import Path

from griptape_nodes.common.project_templates.situation import BuiltInSituation
from griptape_nodes.exe_types.core_types import ParameterMode
from griptape_nodes.exe_types.node_types import BaseNode
from griptape_nodes.exe_types.param_components.project_output_parameter import ProjectOutputParameter
from griptape_nodes.files.file import FileDestination, FileDestinationProvider
from griptape_nodes.files.path_utils import is_url, with_default_extension
from griptape_nodes.files.project_file import ProjectFileDestination
from griptape_nodes.traits.file_system_picker import FileSystemPicker


class ProjectFileParameter(ProjectOutputParameter):
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
        super().__init__(
            node,
            name,
            default_value=default_filename,
            situation=situation,
            allowed_modes=allowed_modes,
            ui_options=ui_options,
        )

    @property
    def _settings_node_type(self) -> str:
        return "FileOutputSettings"

    @property
    def _settings_value_param_name(self) -> str:
        return "filename"

    @property
    def _settings_source_param_name(self) -> str:
        return "file_destination"

    @property
    def _parameter_output_type(self) -> str:
        return "str"

    def _make_parameter_traits(self) -> set:
        return {
            FileSystemPicker(
                allow_files=True,
                allow_directories=False,
                allow_create=True,
            )
        }

    def build_file(self, **extra_vars: str | int) -> FileDestination:
        """Build a FileDestination with a MacroPath from the parameter's current value.

        If an upstream node (e.g. FileOutputSettings) exposes a ``file_destination``
        attribute, its ``FileDestination`` is retrieved directly without deserialising
        from the wire. A node that exposes the attribute but returns None raises instead
        of silently falling back to the default situation, since that fallback hides
        user intent.

        Otherwise the parameter's string value is combined with this component's
        situation to build a ``FileDestination``. A filename with no extension takes the
        extension of the default filename. An absolute path or URL is passed through as
        typed: the destination honors an explicit location verbatim, and a URL it
        refuses is quoted back as entered.

        Args:
            **extra_vars: Additional variables for the macro (e.g., sub_dirs="renders")

        Returns:
            FileDestination with a MacroPath and baked-in write policy for deferred path resolution

        Raises:
            ValueError: If an upstream FileDestinationProvider is connected but returns None,
                if the parameter's value is a URL that names no local file, or if it
                names a folder rather than a file.
        """
        upstream = self._get_upstream_destination(
            FileDestinationProvider, lambda provider: provider.file_destination, "FileDestination"
        )
        if upstream is not None:
            return upstream

        value = self._node.get_parameter_value(self._name)
        filename = value if isinstance(value, str) and value else self._default_value
        if not is_url(filename) and not Path(filename).is_absolute():
            filename = with_default_extension(filename, self._default_value)

        if "node_name" not in extra_vars:
            extra_vars["node_name"] = self._node.name

        return ProjectFileDestination.from_situation(
            filename,
            self._situation_name,
            **extra_vars,
        )
