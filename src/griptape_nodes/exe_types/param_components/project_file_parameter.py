"""ProjectFileParameter - parameter component for project-aware file saving.

Wraps a MacroPath into a FileDestination with a baked-in write policy, deferring
path resolution to execution time via the situation-based macro system.
"""

from griptape_nodes.common.project_templates.situation import BuiltInSituation
from griptape_nodes.exe_types.core_types import ParameterMode
from griptape_nodes.exe_types.node_types import BaseNode
from griptape_nodes.exe_types.param_components.project_output_parameter import ProjectOutputParameter
from griptape_nodes.files.file import FileDestination
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
        situation to build a ``FileDestination``.

        Args:
            **extra_vars: Additional variables for the macro (e.g., sub_dirs="renders")

        Returns:
            FileDestination with a MacroPath and baked-in write policy for deferred path resolution

        Raises:
            ValueError: If an upstream node exposes ``file_destination`` but returns None.
        """
        upstream = self._get_upstream_destination("file_destination", "FileDestination")
        if upstream is not None:
            return upstream  # type: ignore[return-value]

        value = self._node.get_parameter_value(self._name)
        filename = value if isinstance(value, str) and value else self._default_value

        if "node_name" not in extra_vars:
            extra_vars["node_name"] = self._node.name

        return ProjectFileDestination.from_situation(
            filename,
            self._situation_name,
            **extra_vars,
        )
