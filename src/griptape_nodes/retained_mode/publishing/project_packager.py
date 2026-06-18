"""Project-scoped packaging utility for export/import.

Mirrors WorkflowPackager (a utility managers call, NOT a manager), but operates
at project scope: it bundles a whole project base directory into a portable .zip
and reads such a .zip back. Unlike WorkflowPackager it NEVER reads or writes
secret values; only required secret KEY names travel in the manifest.

The library partition is driven entirely by the project-adjacent
griptape_nodes_config.json:
- libraries_to_download entries (git-sourced, version-pinned in config) are
  REFERENCED only: the config entry travels, source does not, and the importing
  engine re-downloads them via its normal provisioning on activation.
- libraries_to_register entries (manually registered local libraries with no
  remote source) are TRUE-COPIED into the zip's libraries/ tree, and their
  config path is rewritten to a package-relative path so it resolves at the new
  location after import.
"""

from __future__ import annotations

import io
import json
import logging
import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from ruamel.yaml import YAML

from griptape_nodes.files.path_utils import canonicalize_for_identity
from griptape_nodes.retained_mode.events.os_events import (
    CopyTreeRequest,
    CopyTreeResultSuccess,
)
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
from griptape_nodes.retained_mode.managers.settings import (
    LIBRARIES_TO_DOWNLOAD_KEY,
    LIBRARIES_TO_REGISTER_KEY,
)
from griptape_nodes.utils.dict_utils import get_dot_value, set_dot_value
from griptape_nodes.utils.library_utils import (
    normalize_library_downloads,
    normalize_library_registrations,
)
from griptape_nodes.utils.version_utils import get_current_version

if TYPE_CHECKING:
    from griptape_nodes.common.project_templates import ProjectTemplate
    from griptape_nodes.retained_mode.managers.project_manager import ProjectInfo

logger = logging.getLogger("project_packager")

# Schema version of the manifest written into the zip. Major bumps are
# incompatible; the importer rejects a major it does not understand rather than
# silently falling back.
MANIFEST_SCHEMA_VERSION = "1.0.0"

# The standalone project template + its adjacent config always live at the root
# of the package, mirroring an on-disk project base directory.
PROJECT_TEMPLATE_FILENAME = "griptape-nodes-project.yml"
ADJACENT_CONFIG_FILENAME = "griptape_nodes_config.json"
MANIFEST_FILENAME = "manifest.json"
COPIED_LIBRARIES_DIRNAME = "libraries"

# Top-level config key (and default) for the directory the engine clones
# libraries_to_download into. When this sink sits inside the project base dir,
# its downloaded (referenced) source must NOT travel in the package; the
# importing engine re-downloads referenced libs from their pins. The default is
# relative to the project workspace (the base dir for a self-contained project).
LIBRARIES_DIRECTORY_KEY = "libraries_directory"
DEFAULT_LIBRARIES_DIRECTORY = "libraries"

# Top-level config key for the project's workspace directory. The engine resolves
# a relative libraries_directory against this (not the base dir); they coincide
# for a self-contained project but diverge when the adjacent config re-points the
# workspace. Absent here, the workspace is the project base dir.
WORKSPACE_DIRECTORY_KEY = "workspace_directory"

# Directory tree patterns never copied into the package. Secret values (.env),
# VCS/venv/pycache cruft, and the regenerable hidden caches are all excluded so
# the package is portable and small.
_MIRROR_IGNORE_PATTERNS = [
    ".env",
    ".venv",
    ".git",
    "__pycache__",
    ".griptape-nodes-previews",
    ".griptape-nodes-metadata",
    ".griptape-nodes-thumbnails",
]

# Stripped from copied local-library trees (same set WorkflowPackager.copy_tree
# uses by default).
_LIBRARY_COPY_IGNORE_PATTERNS = [".venv", ".git", "__pycache__"]


class LibraryDisposition(StrEnum):
    """How a project's library is handled when packaging."""

    REFERENCE = "REFERENCE"  # libraries_to_download: ship the pin, re-download on import
    COPY_LOCAL = "COPY_LOCAL"  # libraries_to_register: true-copy source into the zip


@dataclass
class ReferencedLibrary:
    """A libraries_to_download entry that travels by reference (no source copy)."""

    git_url: str
    version: str | None
    name: str | None


@dataclass
class LocalLibrary:
    """A libraries_to_register entry that travels as a true copy of its source.

    `registered_path` is the verbatim string from the config (which may be
    relative-to-workspace or absolute). `source_path` is that string resolved to
    a concrete on-disk path. `containing_dir` is the directory copied into the
    package (the library JSON's parent, or the registered directory itself).
    `path_within_containing_dir` is the JSON's path relative to that dir, or None
    when the entry registered a directory rather than a single JSON file.
    """

    registered_path: str
    source_path: Path
    containing_dir: Path
    path_within_containing_dir: str | None


@dataclass
class LibraryClassification:
    """The reference-vs-copy partition of a project's libraries."""

    referenced: list[ReferencedLibrary] = field(default_factory=list)
    copied: list[LocalLibrary] = field(default_factory=list)


@dataclass
class ProjectPackageResult:
    """Outcome of writing a project package to disk."""

    archive_path: Path
    manifest: dict
    referenced_library_names: list[str]
    copied_library_names: list[str]
    required_secret_keys: list[str]
    warnings: list[str]


class ProjectPackager:
    """Bundle a project base directory into a portable .zip and read it back.

    Usage:
        packager = ProjectPackager()
        result = packager.package_project_to_zip(project_info, adjacent_config, destination)
    """

    # -- Classification (pure: dict reads only, no disk mutation) --

    @staticmethod
    def classify_libraries(adjacent_config: dict, project_base_dir: Path) -> LibraryClassification:
        """Partition a project's libraries into referenced vs copied.

        Reads libraries_to_download and libraries_to_register from the
        project-adjacent config dict. Download entries are referenced (pin only);
        register entries are copied (source travels). A register entry whose path
        does not exist on disk is skipped here and surfaced as a warning by the
        caller via `find_missing_local_libraries`.
        """
        classification = LibraryClassification()

        raw_downloads = get_dot_value(adjacent_config, LIBRARIES_TO_DOWNLOAD_KEY, default=[]) or []
        for download in normalize_library_downloads(raw_downloads):
            classification.referenced.append(
                ReferencedLibrary(git_url=download.git_url, version=download.version, name=download.name)
            )

        raw_registrations = get_dot_value(adjacent_config, LIBRARIES_TO_REGISTER_KEY, default=[]) or []
        for registration in normalize_library_registrations(raw_registrations):
            local = ProjectPackager._resolve_local_library(registration.path, project_base_dir)
            if local is None:
                continue
            classification.copied.append(local)

        return classification

    @staticmethod
    def find_missing_local_libraries(adjacent_config: dict, project_base_dir: Path) -> list[str]:
        """Return registered_path strings whose on-disk source is missing.

        A libraries_to_register entry that does not resolve to an existing file or
        directory cannot be copied; the export surfaces these as warnings so the
        user knows the package may not be runnable.
        """
        raw_registrations = get_dot_value(adjacent_config, LIBRARIES_TO_REGISTER_KEY, default=[]) or []
        return [
            registration.path
            for registration in normalize_library_registrations(raw_registrations)
            if ProjectPackager._resolve_local_library(registration.path, project_base_dir) is None
        ]

    # -- Secret KEY collection (names only, never values) --

    @staticmethod
    def collect_required_secret_keys() -> list[str]:
        """Return the names of secrets the project needs, with NO values.

        Sourced from SecretsManager.secrets_to_register, which is core secrets
        plus library-declared secrets (a config read returning a name->default
        dict). The template.environment field resolves builtins/dirs/shell-env,
        not secrets, so it is not a secret source. GetAllSecretValuesRequest is
        never used: no secret VALUE ever leaves the machine.

        Scoping caveat: secrets_to_register reflects the engine's MERGED GLOBAL
        config (the currently-active project plus its LOADED libraries' declared
        secrets), not the exported project's own adjacent config. Exporting a
        project that is not the active one can therefore both over-report (keys
        the active project's libraries need but the exported one does not) and
        under-report (the exported project's own libraries are not loaded, so
        their declared secrets never reach the global config). Exporting the
        active project yields the closest-to-correct list. Scoping the key list
        to the exported project specifically is deferred.
        """
        return sorted(GriptapeNodes.SecretsManager().secrets_to_register.keys())

    # -- Export orchestration --

    def package_project_to_zip(
        self, project_info: ProjectInfo, adjacent_config: dict, destination_zip: Path
    ) -> ProjectPackageResult:
        """Build a staging tree from the project base dir and zip it to destination.

        Steps: mirror the base-dir tree (excluding secrets/caches), rewrite the
        template as a self-contained YAML, true-copy the register-only local
        libraries and rewrite their config paths to package-relative, write the
        manifest, then archive. The staging dir is always cleaned up.
        """
        classification = self.classify_libraries(adjacent_config, project_info.project_base_dir)
        warnings = [
            f"Local library '{path}' was registered but its source is missing on disk; it was not packaged."
            for path in self.find_missing_local_libraries(adjacent_config, project_info.project_base_dir)
        ]
        required_secret_keys = self.collect_required_secret_keys()

        staging_dir = Path(tempfile.mkdtemp(prefix="gtn-project-export-"))
        try:
            self._mirror_base_dir(staging_dir, project_info.project_base_dir)
            self._prune_download_library_sink(staging_dir, project_info.project_base_dir, adjacent_config)
            self._write_self_contained_template(staging_dir, project_info.template)
            copied_rewrites = self._copy_local_libraries(staging_dir, classification)
            self._rewrite_adjacent_config(staging_dir, adjacent_config, copied_rewrites, project_info.project_base_dir)
            manifest = self._build_manifest(
                project_info, classification, copied_rewrites, required_secret_keys, warnings
            )
            self._write_manifest(staging_dir, manifest)
            self._archive_zip(staging_dir, destination_zip)
        finally:
            shutil.rmtree(staging_dir, ignore_errors=True)

        return ProjectPackageResult(
            archive_path=destination_zip,
            manifest=manifest,
            referenced_library_names=[lib.name or lib.git_url for lib in classification.referenced],
            copied_library_names=[lib.registered_path for lib in classification.copied],
            required_secret_keys=required_secret_keys,
            warnings=warnings,
        )

    # -- Import reads (read manifest without extraction) --

    @staticmethod
    def read_manifest(archive_path: Path) -> dict:
        """Read and parse manifest.json from a package zip without extracting it.

        Raises FileNotFoundError when the archive is missing, zipfile.BadZipFile
        when it is not a zip, and KeyError when manifest.json is absent.
        """
        with zipfile.ZipFile(archive_path) as archive:
            raw = archive.read(MANIFEST_FILENAME)
        return json.loads(raw)

    @staticmethod
    def extract_archive(archive_path: Path, target_directory: Path) -> None:
        """Extract every package member except the manifest into target_directory.

        The mirrored base-dir tree lands at the target root 1:1, so the macro
        layer re-resolves {inputs}/{outputs}/etc. against the new location with no
        path surgery. manifest.json is provenance only and is not extracted.
        """
        target_directory.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(archive_path) as archive:
            members = [name for name in archive.namelist() if name != MANIFEST_FILENAME]
            archive.extractall(target_directory, members=members)

    @staticmethod
    def is_manifest_schema_compatible(manifest: dict) -> bool:
        """Return whether this engine can import a package with the given manifest.

        Compatibility is by MAJOR version only: a package whose major schema
        version differs from this engine's is rejected rather than imported with a
        silent fallback. A missing or malformed version is treated as incompatible.
        """
        declared = manifest.get("manifest_schema_version")
        if not isinstance(declared, str):
            return False
        declared_major = declared.split(".", 1)[0]
        current_major = MANIFEST_SCHEMA_VERSION.split(".", 1)[0]
        return declared_major == current_major

    @staticmethod
    def rename_project_template(project_template_path: Path, new_name: str) -> None:
        """Set the `name` field in an extracted project template YAML in place.

        Patches only the top-level name scalar (preserving the rest of the file)
        so an imported project can be a renamed duplicate/branch. Quoting matches
        ProjectTemplate._dump_yaml (all strings double-quoted).
        """
        yaml = YAML()
        yaml.default_flow_style = False
        yaml.width = 4096
        yaml.representer.add_representer(str, lambda r, d: r.represent_scalar("tag:yaml.org,2002:str", d, style='"'))
        yaml.representer.add_representer(type(None), lambda r, _d: r.represent_scalar("tag:yaml.org,2002:null", "null"))

        data = yaml.load(project_template_path.read_text(encoding="utf-8"))
        data["name"] = new_name
        buffer = io.StringIO()
        yaml.dump(data, buffer)
        project_template_path.write_text(buffer.getvalue(), encoding="utf-8")

    # -- Private export helpers --

    @staticmethod
    def _copy_tree(source_path: Path, destination_path: Path, ignore_patterns: list[str]) -> None:
        """Copy a directory tree via the engine's OS event system."""
        result = GriptapeNodes.handle_request(
            CopyTreeRequest(
                source_path=str(source_path),
                destination_path=str(destination_path),
                ignore_patterns=ignore_patterns,
                dirs_exist_ok=True,
            )
        )
        if not isinstance(result, CopyTreeResultSuccess):
            # The isinstance guard is a result-type check, not a type validation:
            # the copy request genuinely failed at runtime, so RuntimeError (which
            # the export handler catches) is the right type here.
            msg = f"Attempted to copy tree from '{source_path}' to '{destination_path}'. Failed during package staging."
            logger.error(msg)
            raise RuntimeError(msg)  # noqa: TRY004

    def _mirror_base_dir(self, staging_dir: Path, project_base_dir: Path) -> None:
        """Copy the project base-dir tree into staging, excluding secrets and caches."""
        self._copy_tree(project_base_dir, staging_dir, _MIRROR_IGNORE_PATTERNS)

    @staticmethod
    def _prune_download_library_sink(staging_dir: Path, project_base_dir: Path, adjacent_config: dict) -> None:
        """Remove the downloaded-library sink from the mirrored tree.

        The engine clones libraries_to_download into `libraries_directory`
        (default 'libraries'), resolving a relative value against the project's
        workspace directory (the adjacent config's workspace_directory, or the
        base dir when unset) exactly as ConfigManager does at load time. When that
        sink sits inside the base dir, the plain mirror would bundle the referenced
        libraries' source, which violates the reference-only contract. Drop the
        sink subtree here; register-only local libs are independently
        re-materialized by _copy_local_libraries, and referenced libs
        re-download on import.
        """
        configured = get_dot_value(adjacent_config, LIBRARIES_DIRECTORY_KEY, default=DEFAULT_LIBRARIES_DIRECTORY)
        sink_setting = configured if isinstance(configured, str) and configured else DEFAULT_LIBRARIES_DIRECTORY

        # A relative libraries_directory resolves against the workspace dir, which
        # the engine derives from the adjacent config's workspace_directory and
        # falls back to the base dir. A relative workspace_directory is itself
        # base-dir-relative.
        configured_workspace = get_dot_value(adjacent_config, WORKSPACE_DIRECTORY_KEY, default=None)
        if isinstance(configured_workspace, str) and configured_workspace:
            workspace_path = Path(configured_workspace)
            if not workspace_path.is_absolute():
                workspace_path = project_base_dir / workspace_path
        else:
            workspace_path = project_base_dir

        sink_path = Path(sink_setting)
        if not sink_path.is_absolute():
            sink_path = workspace_path / sink_path

        base_resolved = canonicalize_for_identity(project_base_dir)
        sink_resolved = canonicalize_for_identity(sink_path)
        if base_resolved not in sink_resolved.parents:
            return

        relative_sink = sink_resolved.relative_to(base_resolved)
        staged_sink = staging_dir / relative_sink
        if staged_sink.is_dir():
            shutil.rmtree(staged_sink, ignore_errors=True)

    @staticmethod
    def _write_self_contained_template(staging_dir: Path, template: ProjectTemplate) -> None:
        """Write a parent-less standalone project YAML at the staging root.

        Nulls parent_project_path/parent_project_id (no link back to the source
        machine) and id (so the imported project takes a fresh, path-derived id at
        its new location instead of colliding with the still-loaded source on the
        same engine). Directory paths stay as macro strings, so they re-resolve at
        import.
        """
        standalone = template.model_copy(deep=True)
        standalone.parent_project_path = None
        standalone.parent_project_id = None
        standalone.id = None
        (staging_dir / PROJECT_TEMPLATE_FILENAME).write_text(standalone.to_yaml(), encoding="utf-8")

    def _copy_local_libraries(
        self, staging_dir: Path, classification: LibraryClassification
    ) -> list[_CopiedLibraryRewrite]:
        """True-copy each register-only local library into libraries/<dirname>/.

        Returns the path rewrites the adjacent config needs: the original
        registered_path mapped to its new package-relative path. Collisions
        between two libs whose containing dirs share a basename are suffixed.
        """
        libraries_root = staging_dir / COPIED_LIBRARIES_DIRNAME
        rewrites: list[_CopiedLibraryRewrite] = []
        used_dirnames: set[str] = set()

        for local in classification.copied:
            dest_dirname = self._unique_dirname(local.containing_dir.name, used_dirnames)
            used_dirnames.add(dest_dirname)
            self._copy_tree(local.containing_dir, libraries_root / dest_dirname, _LIBRARY_COPY_IGNORE_PATTERNS)

            if local.path_within_containing_dir is None:
                package_relative = f"{COPIED_LIBRARIES_DIRNAME}/{dest_dirname}"
            else:
                package_relative = f"{COPIED_LIBRARIES_DIRNAME}/{dest_dirname}/{local.path_within_containing_dir}"
            rewrites.append(
                _CopiedLibraryRewrite(
                    registered_path=local.registered_path,
                    package_relative_path=package_relative,
                    name=local.containing_dir.name,
                )
            )
        return rewrites

    @staticmethod
    def _rewrite_adjacent_config(
        staging_dir: Path,
        adjacent_config: dict,
        copied_rewrites: list[_CopiedLibraryRewrite],
        project_base_dir: Path,
    ) -> None:
        """Rewrite the staged config's libraries_to_register to package-relative paths.

        libraries_to_download is left untouched (re-downloaded on import). A
        register entry whose path was missing on disk (no rewrite) is dropped so
        the imported config does not carry an unresolvable absolute path.

        A self-referential workspace_directory (one canonicalizing to the project's
        own base dir) is dropped so import falls to decide_workspace's auto-default
        branch and re-points the workspace to the import target. Otherwise the
        absolute source-machine path would survive the round trip and the importing
        engine would re-download referenced libraries into the source workspace
        instead of the imported project's own libraries/ dir. A workspace_directory
        pointing somewhere else (a genuine external/shared workspace) is preserved
        verbatim, since it names a real dependency we cannot relocate. Every other
        config key is preserved verbatim.
        """
        rewrite_by_path = {rewrite.registered_path: rewrite.package_relative_path for rewrite in copied_rewrites}

        raw_registrations = get_dot_value(adjacent_config, LIBRARIES_TO_REGISTER_KEY, default=[]) or []
        rewritten: list[str | dict] = []
        for entry in raw_registrations:
            original_path = entry.get("path") if isinstance(entry, dict) else entry
            if not isinstance(original_path, str):
                continue
            new_path = rewrite_by_path.get(original_path)
            if new_path is None:
                continue
            if isinstance(entry, dict):
                rewritten.append({**entry, "path": new_path})
            else:
                rewritten.append(new_path)

        config_copy = json.loads(json.dumps(adjacent_config))
        set_dot_value(config_copy, LIBRARIES_TO_REGISTER_KEY, rewritten)

        configured_workspace = get_dot_value(config_copy, WORKSPACE_DIRECTORY_KEY, default=None)
        if isinstance(configured_workspace, str) and configured_workspace:
            workspace_path = Path(configured_workspace)
            if not workspace_path.is_absolute():
                workspace_path = project_base_dir / workspace_path
            if canonicalize_for_identity(workspace_path) == canonicalize_for_identity(project_base_dir):
                config_copy.pop(WORKSPACE_DIRECTORY_KEY, None)

        (staging_dir / ADJACENT_CONFIG_FILENAME).write_text(json.dumps(config_copy, indent=2), encoding="utf-8")

    @staticmethod
    def _build_manifest(
        project_info: ProjectInfo,
        classification: LibraryClassification,
        copied_rewrites: list[_CopiedLibraryRewrite],
        required_secret_keys: list[str],
        warnings: list[str],
    ) -> dict:
        """Assemble the manifest dict (provenance + flat summary, KEYS only)."""
        rewrite_by_name = {rewrite.name: rewrite.package_relative_path for rewrite in copied_rewrites}

        referenced_entries = [
            {
                "name": referenced.name,
                "disposition": LibraryDisposition.REFERENCE.value,
                "git_url": referenced.git_url,
                "version": referenced.version,
            }
            for referenced in classification.referenced
        ]
        copied_entries = [
            {
                "name": local.containing_dir.name,
                "disposition": LibraryDisposition.COPY_LOCAL.value,
                "source_relative_path": rewrite_by_name.get(local.containing_dir.name),
            }
            for local in classification.copied
        ]
        libraries = referenced_entries + copied_entries

        return {
            "manifest_schema_version": MANIFEST_SCHEMA_VERSION,
            "engine_version": get_current_version(),
            "exported_at": datetime.now(UTC).isoformat(),
            "source_project_id": project_info.project_id,
            "project": {"name": project_info.template.name, "template_file": PROJECT_TEMPLATE_FILENAME},
            "libraries": libraries,
            "required_secret_keys": required_secret_keys,
            "warnings": warnings,
        }

    @staticmethod
    def _write_manifest(staging_dir: Path, manifest: dict) -> None:
        (staging_dir / MANIFEST_FILENAME).write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    @staticmethod
    def _archive_zip(staging_dir: Path, destination_zip: Path) -> None:
        """Zip the staging tree (deflated) to destination, including empty dirs."""
        with zipfile.ZipFile(destination_zip, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(staging_dir.rglob("*")):
                arcname = path.relative_to(staging_dir).as_posix()
                if path.is_dir():
                    if not any(path.iterdir()):
                        archive.writestr(arcname + "/", "")
                else:
                    archive.write(path, arcname)

    @staticmethod
    def _resolve_local_library(registered_path: str, project_base_dir: Path) -> LocalLibrary | None:
        """Resolve a registered_path to a LocalLibrary, or None when its source is missing.

        Relative register paths resolve against the project base dir (the import
        target's workspace), matching how the engine resolves them at load time.
        A path to a JSON file copies the file's parent dir; a path to a directory
        copies that directory. canonicalize_for_identity expands ~/env vars so a
        register path using either is resolved rather than wrongly reported missing.
        """
        candidate = canonicalize_for_identity(registered_path, base=project_base_dir)

        if not candidate.exists():
            return None

        if candidate.is_dir():
            return LocalLibrary(
                registered_path=registered_path,
                source_path=candidate,
                containing_dir=candidate,
                path_within_containing_dir=None,
            )

        return LocalLibrary(
            registered_path=registered_path,
            source_path=candidate,
            containing_dir=candidate.parent,
            path_within_containing_dir=candidate.name,
        )

    @staticmethod
    def _unique_dirname(name: str, used: set[str]) -> str:
        """Return `name`, suffixed with _2, _3, ... if already taken."""
        if name not in used:
            return name
        index = 2
        while f"{name}_{index}" in used:
            index += 1
        return f"{name}_{index}"


@dataclass
class _CopiedLibraryRewrite:
    """A single libraries_to_register path rewrite produced by copying a local lib."""

    registered_path: str
    package_relative_path: str
    name: str
