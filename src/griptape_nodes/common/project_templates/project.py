"""Project template main class."""

from __future__ import annotations

import io
from typing import TYPE_CHECKING, ClassVar

import semver
from pydantic import BaseModel, Field, ValidationError
from ruamel.yaml import YAML

from griptape_nodes.common.project_templates.directory import DirectoryDefinition
from griptape_nodes.common.project_templates.project_path import PerPlatformProjectPath
from griptape_nodes.common.project_templates.situation import SituationTemplate
from griptape_nodes.common.project_templates.validation import (
    ProjectOverrideAction,
    ProjectOverrideCategory,
    ProjectValidationInfo,
)

if TYPE_CHECKING:
    from griptape_nodes.common.project_templates.loader import ProjectOverlayData


def build_project_yaml() -> YAML:
    """Build a YAML serializer with the shared project-template dump conventions.

    Single-sources the quoting/width rules every project YAML is written with, so
    standalone dumps (ProjectTemplate._dump_yaml) and in-place edits (the export
    package's template rename) stay byte-compatible. Callers add their own
    pre-processing (e.g. nested-key filtering) on top.
    """
    yaml = YAML()
    yaml.default_flow_style = False
    yaml.width = 4096
    # Double-quote all strings; bools and ints are left untagged: https://yaml.org/spec/1.2.2/
    yaml.representer.add_representer(str, lambda r, d: r.represent_scalar("tag:yaml.org,2002:str", d, style='"'))
    # Emit explicit "null" for None (deletion tombstones) so bare keys like `save_file:`
    # don't look truncated in a hand-read of the file.
    yaml.representer.add_representer(type(None), lambda r, _d: r.represent_scalar("tag:yaml.org,2002:null", "null"))
    return yaml


class ProjectTemplate(BaseModel):
    """Complete project template loaded from project.yml."""

    LATEST_SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    project_template_schema_version: str = Field(description="Schema version for the project template")
    name: str = Field(description="Name of the project")
    id: str | None = Field(
        default=None,
        description=(
            "Opaque identifier for the template, unique per engine. The UI sets a GUID by default, but a "
            "user may set any unique string. It is the identifier used by project events and referenced by "
            "external consumers such as policies; consumers must not parse or construct it. Absent on legacy "
            "projects that predate this field, in which case the engine derives the id from the canonicalized "
            "project file path. Set once at creation and immutable thereafter."
        ),
    )
    description: str | None = Field(default=None, description="Description of the project")
    parent_project_path: str | PerPlatformProjectPath | None = Field(
        default=None,
        description=(
            "Optional path to a parent project YAML. When set, the parent's merged template is the "
            "base for this template (instead of system defaults alone). The value may be: "
            "(1) a string — absolute, or relative to the directory of this project's YAML "
            "(e.g. `../base/griptape-nodes-project.yml`); or (2) a per-platform mapping with optional "
            "`linux`, `darwin`, `windows`, and `default` string fields, used when the parent lives at "
            "different filesystem paths on different OSes. Relative paths are preferred for cross-machine "
            "portability when both projects live under the same workspace; the per-platform form is "
            "preferred when the parent lives on shared storage mounted at different paths per OS. "
            "Macro tokens are not allowed: they would resolve against runtime state (e.g. the active "
            "workspace) that can change while the project is loaded, which would corrupt parent/child links."
        ),
    )
    parent_project_id: str | None = Field(
        default=None,
        description=(
            "Optional id of a parent project. When set, the parent's merged template is the base for this "
            "template, located via the engine registry rather than a filesystem path, so the link survives "
            "moving the file between machines. Mutually exclusive with parent_project_path: when this is set "
            "the engine ignores parent_project_path. parent_project_path is retained only as a backwards-compat "
            "fallback for legacy projects that have no parent_project_id."
        ),
    )
    workspace_dir: str | PerPlatformProjectPath | None = Field(
        default=None,
        description=(
            "Optional workspace directory this project uses. When set, it is the highest-priority workspace "
            "source: it overrides the per-user project_workspaces mapping, the GTN_CONFIG_WORKSPACE_DIRECTORY "
            "env var, the project-adjacent config, parent inheritance, and the global default. The directory "
            "need not contain a config file; an empty directory is valid. The value may be: (1) a string — "
            "absolute, or relative to the directory of this project's YAML (e.g. `./workspace`); or (2) a "
            "per-platform mapping with optional `linux`, `darwin`, `windows`, and `default` string fields. "
            "The raw string is stored verbatim and only resolved to an absolute path at workspace-resolution "
            "time, so a relative value keeps the project portable across machines."
        ),
    )
    libraries_dir: str | PerPlatformProjectPath | None = Field(
        default=None,
        description=(
            "Optional directory where this project's downloaded/registered libraries install and resolve, "
            "decoupled from workspace_dir. When set, libraries_to_download are provisioned here instead of "
            "under the workspace-relative libraries_directory, so a child project can share its parent's "
            "library install location and avoid re-downloading. It inherits down the parent-project chain: a "
            "child with no libraries_dir of its own adopts the nearest ancestor that declares one, resolved "
            "against that ancestor's project directory. The value may be: (1) a string — absolute, or relative "
            "to the directory of this project's YAML (e.g. `./libraries`); or (2) a per-platform mapping with "
            "optional `linux`, `darwin`, `windows`, and `default` string fields. The raw string is stored "
            "verbatim and only resolved to an absolute path at resolution time, so a relative value keeps the "
            "project portable across machines. When unset everywhere in the chain, libraries resolve the legacy "
            "way: the workspace-relative libraries_directory config value."
        ),
    )
    situations: dict[str, SituationTemplate] = Field(description="Situation templates (situation_name -> template)")
    directories: dict[str, DirectoryDefinition] = Field(
        description="Directory definitions (logical_name -> definition)",
    )
    environment: dict[str, str] = Field(default_factory=dict, description="Custom environment variables")
    file_extension_directories: dict[str, str] = Field(
        default_factory=dict,
        description="Mapping of file extension (without leading dot) to a macro (plain name or `{...}` template) used to populate the {file_extension_directory} macro variable. This variable is only available inside situation macros (the filename layer, resolved per-file at write time), not in directory or environment path_macros.",
    )

    def get_situation(self, situation_name: str) -> SituationTemplate | None:
        """Get a situation by name, returns None if not found."""
        return self.situations.get(situation_name)

    def get_directory(self, directory_name: str) -> DirectoryDefinition | None:
        """Get a directory definition by logical name."""
        return self.directories.get(directory_name)

    def to_overlay_yaml(self, base: ProjectTemplate) -> str:  # noqa: C901
        """Export only user customizations relative to a base template as YAML.

        Per-item atomicity: if a situation or directory differs from the base
        at all, the full item is emitted (not a sub-field diff). This mirrors
        the loader's atomic treatment of SituationPolicy and removes a class
        of partial-overlay round-trip bugs.

        Deletion semantics: items that exist in the base but are missing from
        self are emitted as null tombstones so the loader's merge can drop
        them. Same for environment variables. An explicit null `description`
        signals "clear the inherited description."

        Sections with no user content are omitted entirely.
        """
        self_dump = self.model_dump(mode="json")
        base_dump = base.model_dump(mode="json")

        output: dict = {
            "project_template_schema_version": self._version_to_write(self_dump["project_template_schema_version"]),
            "name": self_dump["name"],
        }

        # id is identity, never diffed against base: emit it whenever present so
        # the saved file always declares its own id. Absent only for templates
        # with no file-backed id (e.g. the in-memory default base).
        if self_dump.get("id") is not None:
            output["id"] = self_dump.get("id")

        # description: emit only when it diverges from base. Explicit null
        # tombstones a previously-set base description.
        if self_dump.get("description") != base_dump.get("description"):
            output["description"] = self_dump.get("description")

        # Parent link: parent_project_id and parent_project_path are mutually
        # exclusive, and parent_project_id is preferred (it is what new saves
        # emit). parent_project_path is emitted only for a legacy template that
        # has no parent_project_id, preserving backwards-compat round-trips.
        # Each is emitted only when it diverges from base; an explicit null
        # tombstones an inherited link.
        if self_dump.get("parent_project_id") is not None:
            if self_dump.get("parent_project_id") != base_dump.get("parent_project_id"):
                output["parent_project_id"] = self_dump.get("parent_project_id")
        elif self_dump.get("parent_project_path") != base_dump.get("parent_project_path"):
            output["parent_project_path"] = self_dump.get("parent_project_path")

        # workspace_dir: emit only when it diverges from base. The stored value is the
        # raw string or per-platform mapping (never absolutized), so a relative path
        # round-trips verbatim. An explicit null tombstones an inherited value.
        if self_dump.get("workspace_dir") != base_dump.get("workspace_dir"):
            output["workspace_dir"] = self_dump.get("workspace_dir")

        # libraries_dir: same semantics as workspace_dir. Raw string/mapping stored
        # verbatim; emitted only when it diverges from base; explicit null tombstones.
        if self_dump.get("libraries_dir") != base_dump.get("libraries_dir"):
            output["libraries_dir"] = self_dump.get("libraries_dir")

        situations_overlay = self._diff_named_items(self_dump["situations"], base_dump["situations"])
        if situations_overlay:
            output["situations"] = situations_overlay

        directories_overlay = self._diff_named_items(self_dump["directories"], base_dump["directories"])
        if directories_overlay:
            output["directories"] = directories_overlay

        environment_overlay = self._diff_environment(self_dump["environment"], base_dump["environment"])
        if environment_overlay:
            output["environment"] = environment_overlay

        file_extension_directories_overlay = self._diff_environment(
            self_dump.get("file_extension_directories", {}),
            base_dump.get("file_extension_directories", {}),
        )
        if file_extension_directories_overlay:
            output["file_extension_directories"] = file_extension_directories_overlay

        return self._dump_yaml(output)

    @staticmethod
    def _diff_named_items(self_items: dict, base_items: dict) -> dict:
        """Build a per-item atomic overlay: full item on change, null on removal."""
        changed = {name: value for name, value in self_items.items() if value != base_items.get(name)}
        removed = {name: None for name in base_items if name not in self_items}
        return {**changed, **removed}

    @staticmethod
    def _diff_environment(self_env: dict, base_env: dict) -> dict:
        """Build an environment overlay: changed values, plus null for removals."""
        changed = {key: value for key, value in self_env.items() if base_env.get(key) != value}
        removed = {key: None for key in base_env if key not in self_env}
        return {**changed, **removed}

    @classmethod
    def _version_to_write(cls, loaded_version: str) -> str:
        """Decide the schema version to stamp on save, per the version-fork policy.

        A save advances the version to the latest within the SAME major (minor/patch bumps
        are additive, so the label can roll forward freely), but never crosses a major: a v0
        project rolls up to the latest 0.x, never to 1.x, because the next major carries a
        different defaults baseline that could relocate the project. Crossing a major is an
        explicit, opt-in upgrade handled elsewhere. The bump is one-directional: a version
        already at or beyond the latest-for-its-major is left untouched (never downgraded).
        """
        # Lazy import: default_project_template imports ProjectTemplate, so importing it at
        # module scope here is a circular dependency. The per-major latest lives there because
        # it is the version each per-major default template declares.
        from griptape_nodes.common.project_templates.default_project_template import latest_version_for_major

        latest_in_major = latest_version_for_major(loaded_version)
        if latest_in_major is None:
            return loaded_version
        # latest_in_major came from a registered template (always valid semver); guard the
        # comparison so a loaded version that is not full semver is left untouched rather than
        # raising on the save path (the version is user-controlled).
        try:
            loaded_is_behind = semver.VersionInfo.parse(latest_in_major) > semver.VersionInfo.parse(loaded_version)
        except ValueError:
            return loaded_version
        if loaded_is_behind:
            return latest_in_major
        return loaded_version

    def to_yaml(self) -> str:
        """Export the complete, fully-resolved project template as YAML.

        Unlike to_overlay_yaml(), this emits every field without requiring a
        base template to merge against. Intended for standalone consumers
        (e.g., Griptape Cloud bundles) that receive the project on its own
        and have no DEFAULT_PROJECT_TEMPLATE to layer an overlay on top of.
        """
        data = self.model_dump(mode="json", exclude_none=True)
        data["project_template_schema_version"] = self._version_to_write(data["project_template_schema_version"])
        return self._dump_yaml(data)

    @staticmethod
    def _dump_yaml(data: dict) -> str:
        """Serialize project-template data to YAML using shared conventions.

        Loader injects `name` into nested objects from their dict keys, so
        nested `name` keys are filtered out to avoid duplication on round-trip.
        """
        yaml = build_project_yaml()

        nested_skip = frozenset({"name"})

        def filter_keys(d: dict, skip_keys: frozenset) -> dict:
            return {
                k: (filter_keys(v, skip_keys) if isinstance(v, dict) else v) for k, v in d.items() if k not in skip_keys
            }

        filtered = {k: (filter_keys(v, nested_skip) if isinstance(v, dict) else v) for k, v in data.items()}

        stream = io.StringIO()
        yaml.dump(filtered, stream)
        return stream.getvalue()

    @staticmethod
    def merge(  # noqa: C901, PLR0912, PLR0915
        base: ProjectTemplate,
        overlay: ProjectOverlayData,
        validation_info: ProjectValidationInfo,
    ) -> ProjectTemplate:
        """Merge overlay data on top of base template.

        Merge behavior:
        - name: From overlay (required)
        - description: From overlay if present, else base
        - project_template_schema_version: From overlay (required)
        - situations: Dict merge with field-level merging for conflicts
        - directories: Dict merge with field-level merging for conflicts
        - environment: Dict merge (overlay values override base)

        Override tracking (non-status-affecting):
        - Metadata: name (always MODIFIED), description (if different)
        - Situations: MODIFIED if exists in base, ADDED if new
        - Directories: MODIFIED if exists in base, ADDED if new
        - Environment: MODIFIED if exists in base, ADDED if new

        Note: Schema version compatibility should be checked by caller (ProjectManager)
        before calling merge. This method does not validate version compatibility.

        Args:
            base: Fully constructed base template (e.g., system defaults)
            overlay: Partially validated overlay data with raw dicts
            validation_info: Fresh ProjectValidationInfo for tracking overrides and errors

        Returns:
            New fully constructed merged ProjectTemplate with validation_info
        """
        # Track metadata overrides
        validation_info.add_override(
            category=ProjectOverrideCategory.METADATA,
            name="name",
            action=ProjectOverrideAction.MODIFIED,
        )

        if overlay.description is not None and overlay.description != base.description:
            validation_info.add_override(
                category=ProjectOverrideCategory.METADATA,
                name="description",
                action=ProjectOverrideAction.MODIFIED,
            )

        # Merge situations
        merged_situations: dict[str, SituationTemplate] = {}

        # Start with all base situations
        for sit_name, base_sit in base.situations.items():
            if sit_name in overlay.removed_situations:
                # Tombstone in overlay - drop from merged result
                validation_info.add_override(
                    category=ProjectOverrideCategory.SITUATION,
                    name=sit_name,
                    action=ProjectOverrideAction.REMOVED,
                )
                continue
            if sit_name in overlay.situations:
                # Field-level merge
                merged_sit = SituationTemplate.merge(
                    base=base_sit,
                    overlay_data=overlay.situations[sit_name],
                    field_path=f"situations.{sit_name}",
                    validation_info=validation_info,
                    line_info=overlay.line_info,
                )
                merged_situations[sit_name] = merged_sit

                validation_info.add_override(
                    category=ProjectOverrideCategory.SITUATION,
                    name=sit_name,
                    action=ProjectOverrideAction.MODIFIED,
                )
            else:
                # Inherit from base
                merged_situations[sit_name] = base_sit

        # Add new situations from overlay
        for sit_name, sit_data in overlay.situations.items():
            if sit_name not in base.situations:
                # New situation - construct from scratch
                # Add name to dict for model_validate
                sit_data_with_name = {"name": sit_name, **sit_data}

                try:
                    new_sit = SituationTemplate.model_validate(sit_data_with_name)
                    merged_situations[sit_name] = new_sit

                    validation_info.add_override(
                        category=ProjectOverrideCategory.SITUATION,
                        name=sit_name,
                        action=ProjectOverrideAction.ADDED,
                    )
                except ValidationError as e:
                    # Convert Pydantic validation errors
                    for error in e.errors():
                        error_field_path = ".".join(str(loc) for loc in error["loc"])
                        full_field_path = f"situations.{sit_name}.{error_field_path}"
                        message = error["msg"]
                        line_number = overlay.line_info.get_line(full_field_path)

                        validation_info.add_error(
                            field_path=full_field_path,
                            message=message,
                            line_number=line_number,
                        )

        # Merge directories
        merged_directories: dict[str, DirectoryDefinition] = {}

        for dir_name, base_dir in base.directories.items():
            if dir_name in overlay.removed_directories:
                # Tombstone in overlay - drop from merged result
                validation_info.add_override(
                    category=ProjectOverrideCategory.DIRECTORY,
                    name=dir_name,
                    action=ProjectOverrideAction.REMOVED,
                )
                continue
            if dir_name in overlay.directories:
                # Field-level merge
                merged_dir = DirectoryDefinition.merge(
                    base=base_dir,
                    overlay_data=overlay.directories[dir_name],
                    field_path=f"directories.{dir_name}",
                    validation_info=validation_info,
                    line_info=overlay.line_info,
                )
                merged_directories[dir_name] = merged_dir

                validation_info.add_override(
                    category=ProjectOverrideCategory.DIRECTORY,
                    name=dir_name,
                    action=ProjectOverrideAction.MODIFIED,
                )
            else:
                # Inherit from base
                merged_directories[dir_name] = base_dir

        # Add new directories from overlay
        for dir_name, dir_data in overlay.directories.items():
            if dir_name not in base.directories:
                # New directory - construct from scratch
                # Add name to dict for model_validate
                dir_data_with_name = {"name": dir_name, **dir_data}

                try:
                    new_dir = DirectoryDefinition.model_validate(dir_data_with_name)
                    merged_directories[dir_name] = new_dir

                    validation_info.add_override(
                        category=ProjectOverrideCategory.DIRECTORY,
                        name=dir_name,
                        action=ProjectOverrideAction.ADDED,
                    )
                except ValidationError as e:
                    # Convert Pydantic validation errors
                    for error in e.errors():
                        error_field_path = ".".join(str(loc) for loc in error["loc"])
                        full_field_path = f"directories.{dir_name}.{error_field_path}"
                        message = error["msg"]
                        line_number = overlay.line_info.get_line(full_field_path)

                        validation_info.add_error(
                            field_path=full_field_path,
                            message=message,
                            line_number=line_number,
                        )

        # Merge environment
        merged_environment = {**base.environment}
        for key in overlay.removed_environment:
            if key in merged_environment:
                del merged_environment[key]
                validation_info.add_override(
                    category=ProjectOverrideCategory.ENVIRONMENT,
                    name=key,
                    action=ProjectOverrideAction.REMOVED,
                )
        for key, value in overlay.environment.items():
            action = ProjectOverrideAction.MODIFIED if key in base.environment else ProjectOverrideAction.ADDED
            merged_environment[key] = value

            validation_info.add_override(
                category=ProjectOverrideCategory.ENVIRONMENT,
                name=key,
                action=action,
            )

        # Merge file_extension_directories (same semantics as environment: per-key
        # overwrite, null tombstones drop inherited entries).
        merged_file_extension_directories = {**base.file_extension_directories}
        for key in overlay.removed_file_extension_directories:
            if key in merged_file_extension_directories:
                del merged_file_extension_directories[key]
                validation_info.add_override(
                    category=ProjectOverrideCategory.FILE_EXTENSION_DIRECTORY,
                    name=key,
                    action=ProjectOverrideAction.REMOVED,
                )
        for key, value in overlay.file_extension_directories.items():
            action = (
                ProjectOverrideAction.MODIFIED
                if key in base.file_extension_directories
                else ProjectOverrideAction.ADDED
            )
            merged_file_extension_directories[key] = value

            validation_info.add_override(
                category=ProjectOverrideCategory.FILE_EXTENSION_DIRECTORY,
                name=key,
                action=action,
            )

        # Description: overlay value wins; explicit null clears; absent inherits base.
        if overlay.clears_description:
            merged_description = None
        elif overlay.description is not None:
            merged_description = overlay.description
        else:
            merged_description = base.description

        # id is identity, not inherited: the child's own id (which may be None for
        # a legacy overlay) always wins. The engine derives the effective registry
        # id from this value or the file path; merge never copies base.id down.
        merged_id = overlay.id

        # Parent links describe the child's OWN tree edge and are never inherited
        # from the (already-merged) base: a project does not adopt its parent's
        # parent. Each is taken from the overlay alone, so the merged result
        # round-trips exactly the child's declared link. An explicit null or an
        # omitted field both yield None (the clears_* tombstone is preserved for
        # parity with the loader but collapses to None here). This also keeps
        # parent_project_path and parent_project_id mutually exclusive on the
        # result: a legacy path-linked child never inherits an id-linked parent's
        # parent_project_id, and vice versa.
        merged_parent_project_path = None if overlay.clears_parent_project_path else overlay.parent_project_path
        merged_parent_project_id = None if overlay.clears_parent_project_id else overlay.parent_project_id

        # workspace_dir describes the child's OWN workspace and is never inherited from
        # the base: a child does not adopt its parent's workspace_dir (cross-project
        # workspace inheritance is handled by the resolution ladder's parent-chain branch,
        # not by merge). Taken from the overlay alone; an explicit null or omitted field
        # both yield None.
        merged_workspace_dir = None if overlay.clears_workspace_dir else overlay.workspace_dir

        # libraries_dir is never merge-inherited, for the same reason as workspace_dir:
        # cross-project library-root inheritance is handled by the resolution ladder's
        # parent-chain branch (decide_libraries_root), not by merge. Taken from the
        # overlay alone; an explicit null or omitted field both yield None.
        merged_libraries_dir = None if overlay.clears_libraries_dir else overlay.libraries_dir

        return ProjectTemplate(
            project_template_schema_version=overlay.project_template_schema_version,
            name=overlay.name,
            id=merged_id,
            parent_project_path=merged_parent_project_path,
            parent_project_id=merged_parent_project_id,
            workspace_dir=merged_workspace_dir,
            libraries_dir=merged_libraries_dir,
            situations=merged_situations,
            directories=merged_directories,
            environment=merged_environment,
            file_extension_directories=merged_file_extension_directories,
            description=merged_description,
        )
