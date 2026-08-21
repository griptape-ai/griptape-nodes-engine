"""ProjectDirectoryParameter - parameter component for project-aware directory creation.

Provisions a versioned output directory via situation-based macro routing and exposes it as a
DirectoryDestination. Falls back to a sensible default when no situation is configured.
"""

from griptape_nodes.common import macro_parser
from griptape_nodes.common.project_templates import situation_resolver
from griptape_nodes.exe_types import core_types, node_types
from griptape_nodes.exe_types.param_components import project_output_parameter
from griptape_nodes.files import directory as directory_mod
from griptape_nodes.retained_mode.events import project_events
from griptape_nodes.traits import file_system_picker

_FALLBACK_DIRECTORY_MACRO = "{outputs}/{node_name?:_}{dir_name}_v{_index:03}"


class ProjectDirectoryParameter(project_output_parameter.ProjectOutputParameter):
    """Parameter component for project-aware directory creation.

    Adds a directory name parameter to a node that, when processed, returns a
    ``DirectoryDestination`` with a versioned macro path for deferred resolution.

    Usage:
        # In node __init__:
        self._dir_param = ProjectDirectoryParameter(
            node=self,
            name="output_dir",
            default_dirname="renders",
        )
        self._dir_param.add_parameter()

        # In node process():
        dest = self._dir_param.build_directory()
        directory = dest.create()
        self.set_parameter_value("output_dir", directory.location)
    """

    DEFAULT_SITUATION = "save_output_directory"

    def __init__(  # noqa: PLR0913
        self,
        node: node_types.BaseNode,
        name: str,
        *,
        default_dirname: str,
        situation: str = DEFAULT_SITUATION,
        allowed_modes: set[core_types.ParameterMode] | None = None,
        ui_options: dict | None = None,
    ) -> None:
        super().__init__(
            node,
            name,
            default_value=default_dirname,
            situation=situation,
            allowed_modes=allowed_modes,
            ui_options=ui_options,
        )

    @property
    def _settings_node_type(self) -> str:
        return "DirectoryOutputSettings"

    @property
    def _settings_value_param_name(self) -> str:
        return "dirname"

    @property
    def _settings_source_param_name(self) -> str:
        return "directory_destination"

    @property
    def _parameter_output_type(self) -> str:
        return "Directory"

    def _make_parameter_traits(self) -> set:
        return {
            file_system_picker.FileSystemPicker(
                allow_files=False,
                allow_directories=True,
                allow_create=True,
            )
        }

    def build_directory(self, **extra_vars: str | int) -> directory_mod.DirectoryDestination:
        """Build a DirectoryDestination from the parameter's current value.

        If an upstream node exposes a ``directory_destination`` attribute, its
        ``DirectoryDestination`` is retrieved directly. Otherwise the parameter's
        string value is used as the directory name, combined with the situation macro.

        Args:
            **extra_vars: Additional variables for the macro (e.g., sub_dirs="renders")

        Returns:
            DirectoryDestination with a versioned MacroPath and baked-in policy.

        Raises:
            ValueError: If an upstream node exposes ``directory_destination`` but returns None.
        """
        upstream = self._get_upstream_destination("directory_destination", "DirectoryDestination")
        if upstream is not None:
            return upstream  # type: ignore[return-value]

        value = self._node.get_parameter_value(self._name)
        dirname = value if isinstance(value, str) and value else self._default_value

        if "node_name" not in extra_vars:
            extra_vars["node_name"] = self._node.name

        return _build_directory_destination_from_situation(dirname, self._situation_name, **extra_vars)


def _build_directory_destination_from_situation(
    dirname: str,
    situation: str,
    **extra_vars: str | int,
) -> directory_mod.DirectoryDestination:
    """Build a DirectoryDestination from a project situation template.

    Args:
        dirname: Directory name to use as the ``dir_name`` macro variable.
        situation: Situation name to look up in the current project.
        **extra_vars: Additional macro variables.

    Returns:
        DirectoryDestination with a MacroPath and baked-in creation policy.
    """
    resolved = situation_resolver.resolve_situation(situation, _FALLBACK_DIRECTORY_MACRO)
    variables: dict[str, str | int] = {
        "dir_name": dirname,
        **extra_vars,
    }
    macro_path = project_events.MacroPath(macro_parser.ParsedMacro(resolved.macro_template), variables)
    return directory_mod.DirectoryDestination(
        macro_path,
        existing_dir_policy=resolved.existing_file_policy,
        create_parents=resolved.create_parents,
    )
