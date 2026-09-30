"""ProjectFileDestination - project-aware FileDestination built from a situation template."""

import logging
from pathlib import Path, PureWindowsPath

from griptape_nodes.common.macro_parser import ParsedMacro
from griptape_nodes.files.file import File, FileDestination
from griptape_nodes.files.path_utils import FilenameParts, is_url, parse_file_uri
from griptape_nodes.files.situation_resolver import resolve_situation
from griptape_nodes.retained_mode.events.project_events import (
    AttemptMapAbsolutePathToProjectRequest,
    AttemptMapAbsolutePathToProjectResultSuccess,
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
        mapped = _attempt_map_to_project(Path(result_file.resolve()))
        if mapped is not None:
            return File(mapped)
        return result_file

    @classmethod
    def from_situation(
        cls,
        filename: str,
        situation: str,
        **extra_vars: str | int,
    ) -> "ProjectFileDestination":
        """Build a ProjectFileDestination from a project situation template.

        Looks up the named situation in the current project to obtain the macro
        template and write policy, then constructs the destination. The
        resulting destination uses the engine default for extension coercion;
        callers that need to override (e.g. the FileOutputSettings node) build
        the destination directly via the constructor.

        Args:
            filename: Filename to parse into base and extension components.
            situation: Situation name to look up in the current project.
            **extra_vars: Additional macro variables (e.g., node_name="MyNode", _index=1).

        Raises:
            ValueError: If the filename is a URL that names no local file.
        """
        # Classify the input before any path handling: FilenameParts.from_filename is a
        # plain Path() split with no URL awareness, so a URL that reaches it is mangled
        # rather than rejected. `file:///something.png` split that way yields
        # directory=Path("file:"), which is neither "." nor absolute, so the sub_dirs
        # branch below would fire and produce `{outputs}/file:/something.png` -- a path
        # pointing nowhere, with no error raised.
        #
        # The read side already classifies its input this way (`_resolve_plain_path`
        # calls parse_file_uri first), so doing it here is what makes build_file() and
        # File.resolve() agree about what a `file://` string means.
        # https://github.com/griptape-ai/griptape-nodes-engine/issues/5360
        local_path_from_uri = parse_file_uri(filename)
        if local_path_from_uri is not None and not Path(local_path_from_uri).name:
            # `file:///` and `file://localhost/` parse to "/", a real local path naming no file.
            # Refused here rather than below, where an empty filename would take the bypass and
            # build a destination pointing at nothing. The host-only `file://` forms parse to
            # None instead, so the URL branch below is what refuses those.
            msg = (
                f"Attempted to save to '{filename}'. Failed because that address does not name a file. "
                f"Add the file name you want, for example 'file:///renders/output.png'."
            )
            raise ValueError(msg)
        if (
            local_path_from_uri is not None
            and PureWindowsPath(local_path_from_uri).is_absolute()
            and not Path(local_path_from_uri).is_absolute()
        ):
            # A drive-anchored path on a host that has no drives. `file:///C:/renders/out.png`
            # yields `C:/renders/out.png`, which POSIX reads as relative, so the bypass below
            # would hand a relative string to File.resolve() and land at
            # `{workspace}/C:/renders/out.png` -- a directory named `C:` inside the workspace.
            # Tested against PureWindowsPath rather than the host's own is_absolute() so a
            # leading-slash path, which Windows resolves against the current drive, still
            # reaches the bypass on Windows.
            msg = (
                f"Attempted to save to '{filename}'. Failed because that address does not name a location "
                f"on this computer. A path from another operating system, such as a 'C:' drive on macOS or "
                f"Linux, has no equivalent here."
            )
            raise ValueError(msg)
        if local_path_from_uri is None and is_url(filename):
            # Covers a remote web address, any other scheme, and a `file://` URI naming a
            # host this OS cannot reach -- on Windows such a host is read as a UNC server
            # and yields a real path, so it never arrives here.
            # `parse_static_server_url` maps the engine's own
            # `http://localhost:8124/workspace/staticfiles/...` form back to a real file on
            # the read side, and is deliberately NOT adopted here: that URL names an asset
            # the engine already wrote, so treating it as a save destination would
            # overwrite one node's output from another node.
            msg = (
                f"Attempted to save to '{filename}'. Failed because that address points somewhere this "
                f"computer cannot save to. Enter a file name, a folder path, or a 'file://' address "
                f"naming a file on this machine."
            )
            raise ValueError(msg)
        if local_path_from_uri is not None:
            # A file:// URI names an explicit on-disk location, so swap in the local path it
            # names. It is absolute by the check above, so it reaches the same verbatim
            # bypass an absolute filename takes below.
            filename = local_path_from_uri

        resolved = resolve_situation(situation, FALLBACK_MACRO_TEMPLATE)
        situation_obj = resolved.situation_obj
        existing_file_policy = resolved.existing_file_policy
        create_dirs = resolved.create_parents

        parts = FilenameParts.from_filename(filename)

        # An explicit on-disk location bypasses the situation macro: the caller is
        # declaring where the file goes, so honor it verbatim rather than treating the
        # leading-slash directory as sub_dirs within {outputs}/etc. Two shapes qualify --
        # an absolute filename, and a file:// URI, whose local path we substituted above.
        # The URI keeps its own flag because `file:///renders/out.png` is not is_absolute()
        # on Windows, where a driveless path is current-drive-relative; without it that URI
        # would fall into sub_dirs on Windows only.
        # No sidecar metadata: the situation macro + variables won't re-resolve to the
        # actual on-disk location, so recording them would produce a dishonest
        # provenance trail.
        if local_path_from_uri is not None or parts.directory.is_absolute():
            return cls(
                filename,
                existing_file_policy=existing_file_policy,
                create_parents=create_dirs,
                file_metadata=None,
            )

        variables: dict[str, str | int] = {
            "file_name_base": parts.stem,
            "file_extension": parts.extension,
            **extra_vars,
        }
        # When the filename carries its own relative directory component (e.g.
        # "foo/bar/output.png"), populate sub_dirs so situations with {sub_dirs?:/}
        # route the file into that sub-directory. An explicit sub_dirs kwarg in
        # extra_vars takes precedence.
        directory_str = str(parts.directory)
        if directory_str and directory_str != "." and "sub_dirs" not in variables:
            variables["sub_dirs"] = directory_str

        # Derived variables (e.g. file_extension_directory) are injected by the
        # GetPathForMacroRequest handler at resolve time, so we store only the
        # caller-supplied variables here. The sidecar records the raw inputs;
        # anyone re-resolving the path against the current project gets the
        # same derived values the write used.
        macro_path = MacroPath(ParsedMacro(resolved.macro_template), variables)

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

        return cls(
            macro_path,
            existing_file_policy=existing_file_policy,
            create_parents=create_dirs,
            file_metadata=file_metadata,
        )
