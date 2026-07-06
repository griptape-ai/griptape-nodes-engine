"""ProjectFileDestination - project-aware FileDestination built from a situation template."""

import logging
from pathlib import Path

from griptape_nodes.common.macro_parser import MacroVariables, ParsedMacro
from griptape_nodes.common.project_templates import (
    FILE_EXTENSION_VARIABLE_NAME,
    FILE_NAME_BASE_VARIABLE_NAME,
    SUB_DIRS_VARIABLE_NAME,
)
from griptape_nodes.common.project_templates.situation import SituationFilePolicy
from griptape_nodes.files.file import File, FileDestination
from griptape_nodes.files.path_utils import FilenameParts
from griptape_nodes.retained_mode.events.os_events import ExistingFilePolicy
from griptape_nodes.retained_mode.events.project_events import (
    AttemptMapAbsolutePathToProjectRequest,
    AttemptMapAbsolutePathToProjectResultSuccess,
    GetSituationRequest,
    GetSituationResultSuccess,
    MacroPath,
)
from griptape_nodes.retained_mode.file_metadata.sidecar_metadata import (
    SidecarContent,
    SituationMetadata,
    SituationPolicy,
)
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

logger = logging.getLogger("griptape_nodes")

FALLBACK_MACRO_TEMPLATE = "{outputs}/{node_name?:_}{file_name_base}{_index?:03}.{file_extension}"


SITUATION_TO_FILE_POLICY: dict[str, ExistingFilePolicy] = {
    SituationFilePolicy.CREATE_NEW: ExistingFilePolicy.CREATE_NEW,
    SituationFilePolicy.OVERWRITE: ExistingFilePolicy.OVERWRITE,
    SituationFilePolicy.FAIL: ExistingFilePolicy.FAIL,
    SituationFilePolicy.PROMPT: ExistingFilePolicy.CREATE_NEW,  # PROMPT has no direct mapping; fall back to CREATE_NEW
}


def _attempt_map_to_project(absolute_path: Path) -> str | None:
    """Fire AttemptMapAbsolutePathToProjectRequest; return the mapped macro path string or None."""
    map_result = GriptapeNodes.handle_request(AttemptMapAbsolutePathToProjectRequest(absolute_path=absolute_path))
    if isinstance(map_result, AttemptMapAbsolutePathToProjectResultSuccess) and map_result.mapped_path is not None:
        return map_result.mapped_path
    return None


class ProjectFileDestination(FileDestination):
    """A FileDestination that maps written absolute paths back to project macro form.

    After each write, attempts to convert the resulting absolute path to its
    portable macro representation (e.g. ``{outputs}/image.png``).  Falls back
    to the plain absolute path if mapping is not possible.

    Construct directly with a ``MacroPath`` and write policy, or use the
    ``from_situation`` classmethod to build from a situation name and filename.

    Derivation rules (e.g. ``file_extension_directory``) run centrally inside
    the ``GetPathForMacroRequest`` handler, so any MacroPath stored here gets
    its derived variables filled in at resolution time without callers having
    to pre-apply them.
    """

    def write_bytes(self, content: bytes) -> File:
        return self._map_to_macro_file(super().write_bytes(content))

    async def awrite_bytes(self, content: bytes) -> File:
        return self._map_to_macro_file(await super().awrite_bytes(content))

    def write_text(self, content: str, encoding: str = "utf-8") -> File:
        return self._map_to_macro_file(super().write_text(content, encoding))

    async def awrite_text(self, content: str, encoding: str = "utf-8") -> File:
        return self._map_to_macro_file(await super().awrite_text(content, encoding))

    def _map_to_macro_file(self, result_file: File) -> File:
        """Attempt to convert the written path to its portable macro form.

        Returns a File holding the macro template (e.g. ``{outputs}/image.png``)
        when the path is inside a project directory, so callers can store a
        portable reference via ``file.as_macro()``.  Falls back to the original
        File (absolute path) if mapping is not possible.
        """
        map_result = GriptapeNodes.handle_request(
            AttemptMapAbsolutePathToProjectRequest(absolute_path=Path(result_file.resolve()))
        )
        if isinstance(map_result, AttemptMapAbsolutePathToProjectResultSuccess) and map_result.mapped_path is not None:
            return File(map_result.mapped_path)
        return result_file

    @classmethod
    def from_situation(
        cls,
        filename: str,
        situation: str,
        **extra_vars: str | int,
    ) -> "ProjectFileDestination":
        """Build a ProjectFileDestination from a filename + situation template.

        Splits ``filename`` via ``FilenameParts`` into the canonical variables
        the write path binds (``file_name_base``, ``file_extension``, and
        ``sub_dirs`` when the filename carries a relative directory), merges
        ``extra_vars`` on top, and delegates to ``_build_from_situation``.

        Absolute filenames bypass the situation macro (caller is declaring an
        on-disk location); ``_build_from_situation`` handles that via its
        ``override_absolute`` argument.

        Args:
            filename: Filename to parse into base and extension components.
            situation: Situation name to look up in the current project.
            **extra_vars: Additional macro variables (e.g., node_name="MyNode", _index=1).
        """
        parts = FilenameParts.from_filename(filename)
        variables: dict[str, str | int] = {
            FILE_NAME_BASE_VARIABLE_NAME: parts.stem,
            FILE_EXTENSION_VARIABLE_NAME: parts.extension,
            **extra_vars,
        }
        # When the filename carries its own relative directory component (e.g.
        # "foo/bar/output.png"), populate sub_dirs so situations with {sub_dirs?:/}
        # route the file into that sub-directory. An explicit sub_dirs kwarg in
        # extra_vars takes precedence. Absolute filenames still flow through the
        # macro; we skip the sub_dirs override for them so we don't feed a
        # leading-slash value into the macro substitution.
        directory_str = str(parts.directory)
        if (
            directory_str
            and directory_str != "."
            and not parts.directory.is_absolute()
            and SUB_DIRS_VARIABLE_NAME not in variables
        ):
            variables[SUB_DIRS_VARIABLE_NAME] = directory_str

        override_absolute = filename if parts.directory.is_absolute() else None
        return cls._build_from_situation(situation, variables, override_absolute=override_absolute)

    @classmethod
    def from_situation_with_variables(
        cls,
        situation: str,
        variables: MacroVariables,
    ) -> "ProjectFileDestination":
        """Build a ProjectFileDestination from a pre-computed variables bag.

        Used by callers who already have the writer's bag (typically returned
        by ``ListRelatedProjectFilesRequest``) and want to reuse it verbatim
        for a follow-on write (backups, versioned re-saves). Skips the filename
        split — the bag *is* the input, opaque to variable-name conventions.

        The situation's macro is resolved verbatim against these variables at
        write time. Callers who need to bind the sequence slot include
        ``SEQUENCE_VARIABLE_NAME`` in the bag.

        Args:
            situation: Situation name to look up in the current project.
            variables: Variables bag to pass through to the macro resolver.
        """
        return cls._build_from_situation(situation, dict(variables), override_absolute=None)

    @classmethod
    def _build_from_situation(
        cls,
        situation: str,
        variables: dict[str, str | int],
        override_absolute: str | None,
    ) -> "ProjectFileDestination":
        """Shared plumbing for ``from_situation`` and ``from_situation_with_variables``.

        Fetches the situation template, maps its policy to an ExistingFilePolicy,
        builds a MacroPath from the caller's variables, and records the sidecar
        SituationMetadata. When ``override_absolute`` is set (an absolute filename
        from ``from_situation``'s bypass path), returns a destination that writes
        to that path verbatim with sidecar metadata suppressed — the situation's
        macro won't re-resolve to it, so recording variables would be dishonest.

        Derivation rules (e.g. ``file_extension_directory``) run centrally inside
        the ``GetPathForMacroRequest`` handler, so any MacroPath stored here gets
        its derived variables filled in at resolution time; callers don't pre-apply.
        """
        result = GriptapeNodes.handle_request(GetSituationRequest(situation_name=situation))

        if isinstance(result, GetSituationResultSuccess):
            situation_obj = result.situation
            macro_template = situation_obj.macro
            on_collision = situation_obj.policy.on_collision
            existing_file_policy = SITUATION_TO_FILE_POLICY.get(on_collision, ExistingFilePolicy.CREATE_NEW)
            create_dirs = situation_obj.policy.create_dirs
        else:
            logger.error("Failed to load situation '%s', using fallback macro template", situation)
            situation_obj = None
            macro_template = FALLBACK_MACRO_TEMPLATE
            existing_file_policy = ExistingFilePolicy.CREATE_NEW
            create_dirs = True

        # Store only caller-supplied variables here; derived values (e.g.
        # file_extension_directory) are injected at resolve time so they stay
        # tied to the project's *current* taxonomy, not what it was at write time.
        macro_path = MacroPath(ParsedMacro(macro_template), variables)

        file_metadata = (
            SidecarContent(
                situation=SituationMetadata(
                    name=situation,
                    macro=situation_obj.macro,
                    policy=SituationPolicy(
                        on_collision=situation_obj.policy.on_collision,
                        create_dirs=situation_obj.policy.create_dirs,
                    ),
                    variables={k: str(v) for k, v in macro_path.variables.items()},
                ),
            )
            if situation_obj is not None
            else None
        )

        # Absolute filenames (from `from_situation`'s bypass path) get written
        # verbatim with no sidecar — the situation's macro + variables we built
        # above wouldn't re-resolve to the caller's declared on-disk location,
        # so recording them would produce a dishonest provenance trail.
        if override_absolute is not None:
            return cls(
                override_absolute,
                existing_file_policy=existing_file_policy,
                create_parents=create_dirs,
                file_metadata=None,
            )

        return cls(
            macro_path,
            existing_file_policy=existing_file_policy,
            create_parents=create_dirs,
            file_metadata=file_metadata,
        )
