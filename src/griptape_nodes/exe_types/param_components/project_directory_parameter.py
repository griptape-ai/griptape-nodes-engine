"""ProjectDirectoryParameter - parameter component for project-aware directory creation.

Provisions a versioned output directory via situation-based macro routing and exposes it as a
DirectoryDestination. Falls back to a sensible default when no situation is configured.
"""

from __future__ import annotations

import pathlib
import typing

from griptape_nodes.common import macro_parser
from griptape_nodes.common.project_templates import situation as situation_mod
from griptape_nodes.exe_types.param_components import project_output_parameter
from griptape_nodes.files import directory as directory_mod
from griptape_nodes.files import path_utils, situation_resolver
from griptape_nodes.retained_mode.events import destination_events, os_events, project_events
from griptape_nodes.traits import file_system_picker

if typing.TYPE_CHECKING:
    from griptape_nodes.exe_types import core_types, node_types
    from griptape_nodes.retained_mode.runtime.workflow_runtime import WorkflowRuntime

_FALLBACK_DIRECTORY_MACRO = "{outputs}/{sub_dirs?:/}{dir_name}_v{###}"


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

    DEFAULT_SITUATION = situation_mod.BuiltInSituation.SAVE_OUTPUT_DIRECTORY

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

        If a node implementing ``DirectoryDestinationProvider`` is connected to this
        parameter, its ``DirectoryDestination`` is used. Otherwise the parameter's
        string value is used as the directory name, combined with the situation macro.

        Args:
            **extra_vars: Additional variables for the macro (e.g., sub_dirs="renders")

        Returns:
            DirectoryDestination with a versioned MacroPath and baked-in policy.

        Either way the destination creates the directory in the node's workflow, so it can be
        built and used from any thread.

        Raises:
            ValueError: If the connected settings node can't be read or has no destination set.
        """
        upstream = self._get_upstream_destination(
            destination_events.GetConnectedDirectoryDestinationRequest(
                node_name=self._node.name, parameter_name=self._name
            ),
            destination_events.GetConnectedDirectoryDestinationResultSuccess,
            directory_mod.DirectoryDestination.from_spec,
        )
        if upstream is not None:
            return upstream

        value = self._node.get_parameter_value(self._name)
        dirname = value if isinstance(value, str) and value else self._default_value

        return _build_directory_destination_from_situation(
            dirname, self._situation_name, self._node.workflow, **extra_vars
        )


def _build_directory_destination_from_situation(
    dirname: str,
    situation: str,
    workflow: WorkflowRuntime | None = None,
    /,
    **extra_vars: str | int,
) -> directory_mod.DirectoryDestination:
    """Build a DirectoryDestination from a project situation template.

    Args:
        dirname: Directory name to use as the ``dir_name`` macro variable. An absolute path,
            ``~`` path, or ``file://`` address is used as the destination directly, bypassing
            the situation.
        situation: Situation name to look up in the current project.
        workflow: The workflow to look the situation up and create the directory in, or None
            for the caller's. Positional-only so a macro variable named `workflow` stays in
            `extra_vars`.
        **extra_vars: Additional macro variables.

    Returns:
        DirectoryDestination with a MacroPath and baked-in creation policy.

    Raises:
        ValueError: If ``dirname`` is a web address, another address that cannot be saved to,
            or a ``~`` path whose home folder cannot be found. ``~name`` is a plain name.
    """
    explicit_path = _explicit_directory_path(dirname)
    if explicit_path is not None:
        # The user named the destination, so use it as given rather than nesting it inside the
        # situation's directory. Only a `{###}` slot versions it; without one the user chose this
        # exact location. OVERWRITE on a slotted path would resolve with the slot unbound and fail.
        existing_dir_policy = os_events.ExistingFilePolicy.OVERWRITE
        if _has_version_slot(explicit_path):
            existing_dir_policy = os_events.ExistingFilePolicy.CREATE_NEW
        return directory_mod.DirectoryDestination(
            explicit_path,
            existing_dir_policy=existing_dir_policy,
            create_parents=True,
            workflow=workflow,
        )

    resolved = situation_resolver.resolve_situation(situation, _FALLBACK_DIRECTORY_MACRO, workflow=workflow)
    variables: dict[str, str | int] = {
        "dir_name": dirname,
        **extra_vars,
    }
    macro_path = project_events.MacroPath(macro_parser.ParsedMacro(resolved.macro_template), variables)
    return directory_mod.DirectoryDestination(
        macro_path,
        existing_dir_policy=resolved.existing_file_policy,
        create_parents=resolved.create_parents,
        workflow=workflow,
    )


def _has_version_slot(path: str) -> bool:
    """Return True if `path` contains a `{###}` version slot."""
    try:
        variables = macro_parser.ParsedMacro(path).get_variables()
    except macro_parser.MacroSyntaxError:
        return False
    return any(variable.name == macro_parser.SEQUENCE_VARIABLE_NAME for variable in variables)


def _explicit_directory_path(dirname: str) -> str | None:
    """Return the local path `dirname` names, or None when it is a plain directory name.

    Absolute paths, `~` paths, and `file://` addresses name a location. Any other address, and
    a drive path from another operating system, cannot be saved to and raise.

    Raises:
        ValueError: If `dirname` is an address, drive path, or `~` path that names no location on
            this computer.
    """
    local_path_from_uri = path_utils.parse_file_uri(dirname)
    if local_path_from_uri is not None:
        if (
            pathlib.PureWindowsPath(local_path_from_uri).is_absolute()
            and not pathlib.Path(local_path_from_uri).is_absolute()
        ):
            msg = (
                f"Attempted to save to directory '{dirname}'. Failed because that address does not name a "
                f"location on this computer. A path from another operating system, such as a 'C:' drive "
                f"on macOS or Linux, has no equivalent here."
            )
            raise ValueError(msg)
        return local_path_from_uri

    if path_utils.is_url(dirname):
        msg = (
            f"Attempted to save to directory '{dirname}'. Failed because that address points somewhere this "
            f"computer cannot save to. Enter a directory name, a folder path, or a 'file://' address "
            f"naming a folder on this machine."
        )
        raise ValueError(msg)

    # Only `~` and `~/` expand. Expanding `~name` is inconsistent across operating systems: POSIX
    # fails for an unknown user while Windows swaps the name into the profile's parent folder.
    if dirname != "~" and not dirname.startswith(("~/", "~\\")):
        expanded = pathlib.Path(dirname)
    else:
        try:
            expanded = pathlib.Path(dirname).expanduser()
        except RuntimeError as e:
            msg = (
                f"Attempted to save to directory '{dirname}'. Failed because the home folder it starts with "
                f"could not be found on this computer. Use a full folder path instead."
            )
            raise ValueError(msg) from e
    if expanded.is_absolute():
        return str(expanded)
    return None
