"""ProjectFileSequenceParameter - parameter component for project-aware file sequence output.

Provisions a versioned output directory and exposes a FileSequenceDestination whose entry()
method returns a per-frame FileDestination. Situation-based macro routing determines the
directory layout; falls back to a sensible default when no situation is configured.
"""

from griptape_nodes.common import macro_parser
from griptape_nodes.common.project_templates import situation_resolver
from griptape_nodes.exe_types import core_types, node_types
from griptape_nodes.exe_types.param_components import project_output_parameter
from griptape_nodes.files import file_sequence, path_utils
from griptape_nodes.retained_mode.events import os_events, project_events

_FALLBACK_SEQUENCE_MACRO = (
    "{outputs}/{node_name?:_}{file_name_base}_v{_index:03}/{file_name_base}_v{_index:03}_{entry:04}.{file_extension}"
)


class ProjectFileSequenceParameter(project_output_parameter.ProjectOutputParameter):
    """Parameter component for project-aware file sequence output.

    Adds a filename-pattern parameter to a node that, when processed, returns a
    ``FileSequenceDestination`` with a versioned macro path for deferred resolution.

    The parameter accepts a filename like ``"render.exr"`` or a ``####`` pattern
    like ``"render_####.exr"``. The situation macro wraps it with versioning.

    Usage:
        # In node __init__:
        self._seq_param = ProjectFileSequenceParameter(
            node=self,
            name="output_sequence",
            default_filename="render.exr",
        )
        self._seq_param.add_parameter()

        # In node process():
        dest = self._seq_param.build_sequence()
        for i, entry_data in enumerate(entries):
            dest.entry(i + 1).write_bytes(entry_data)
        seq = dest.file_sequence
        if seq is not None:
            self.set_parameter_value("output_sequence", seq.location)
    """

    DEFAULT_SITUATION = "save_file_sequence"

    def __init__(  # noqa: PLR0913
        self,
        node: node_types.BaseNode,
        name: str,
        *,
        default_filename: str,
        situation: str = DEFAULT_SITUATION,
        allowed_modes: set[core_types.ParameterMode] | None = None,
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
        return "FileSequenceSettings"

    @property
    def _settings_value_param_name(self) -> str:
        return "filename"

    @property
    def _settings_source_param_name(self) -> str:
        return "sequence_destination"

    @property
    def _parameter_output_type(self) -> str:
        return "FileSequence"

    def build_sequence(self, **extra_vars: str | int) -> file_sequence.FileSequenceDestination:
        """Build a FileSequenceDestination from the parameter's current value.

        If an upstream node exposes a ``file_sequence_destination`` attribute, its
        ``FileSequenceDestination`` is retrieved directly. Otherwise the parameter's
        string value (filename or #### pattern) is parsed and combined with the
        situation macro.

        Args:
            **extra_vars: Additional variables for the macro (e.g., sub_dirs="renders")

        Returns:
            FileSequenceDestination with a versioned MacroPath and baked-in policy.

        Raises:
            ValueError: If an upstream node exposes ``file_sequence_destination`` but returns None.
            FileSequenceError: If no available version index can be found.
        """
        upstream = self._get_upstream_destination("file_sequence_destination", "FileSequenceDestination")
        if upstream is not None:
            return upstream  # type: ignore[return-value]

        value = self._node.get_parameter_value(self._name)
        filename = value if isinstance(value, str) and value else self._default_value

        if "node_name" not in extra_vars:
            extra_vars["node_name"] = self._node.name

        return _build_sequence_destination_from_situation(filename, self._situation_name, **extra_vars)


def _build_sequence_destination_from_situation(
    filename: str,
    situation: str,
    **extra_vars: str | int,
) -> file_sequence.FileSequenceDestination:
    """Build a FileSequenceDestination from a project situation template.

    Parses the filename (or #### pattern) into parts, looks up the situation,
    and builds a versioned destination by updating all available ``_index`` variables.

    Args:
        filename: Filename or #### pattern (e.g., ``"render.exr"`` or ``"render_####.exr"``).
        situation: Situation name to look up in the current project.
        **extra_vars: Additional macro variables.

    Returns:
        FileSequenceDestination with a locked version index but unresolved element token.
    """
    resolved = situation_resolver.resolve_situation(
        situation, _FALLBACK_SEQUENCE_MACRO, os_events.ExistingFilePolicy.OVERWRITE
    )
    parts = path_utils.FilenameParts.from_filename(filename)
    variables: dict[str, str | int] = {
        "file_name_base": parts.stem,
        "file_extension": parts.extension,
        **extra_vars,
    }
    macro_path = project_events.MacroPath(macro_parser.ParsedMacro(resolved.macro_template), variables)
    return file_sequence.build_versioned_sequence_destination(
        macro_path,
        existing_file_policy=resolved.existing_file_policy,
        create_parents=resolved.create_parents,
    )
