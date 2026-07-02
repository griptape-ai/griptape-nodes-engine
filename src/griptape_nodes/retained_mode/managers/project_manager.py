"""ProjectManager - Manages project templates and file save situations."""

from __future__ import annotations

import json
import logging
import os
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

from pydantic import ValidationError

from griptape_nodes.common.macro_parser import (
    MacroMatchFailure,
    MacroMatchFailureReason,
    MacroResolutionError,
    MacroResolutionFailureReason,
    MacroVariables,
    ParsedMacro,
)
from griptape_nodes.common.project_templates import (
    DEFAULT_PROJECT_TEMPLATE,
    DirectoryDefinition,
    PerPlatformProjectPath,
    ProjectOverlayData,
    ProjectTemplate,
    ProjectValidationInfo,
    ProjectValidationProblemSeverity,
    ProjectValidationStatus,
    SituationTemplate,
    default_template_for_version,
    load_partial_project_template,
    schema_major_or_none,
    select_project_path,
)
from griptape_nodes.files.derivation import DERIVATION_RULES, apply_derivation_rules
from griptape_nodes.files.file import File, FileLoadError, FileWriteError
from griptape_nodes.files.path_utils import (
    canonicalize_for_identity,
    resolve_file_path,
    resolve_path_safely,
)
from griptape_nodes.node_library.workflow_registry import WorkflowRegistry
from griptape_nodes.retained_mode.events.app_events import AppInitializationComplete, CurrentProjectChanged
from griptape_nodes.retained_mode.events.library_events import (
    ReloadAllLibrariesRequest,
    ReloadAllLibrariesResultFailure,
)
from griptape_nodes.retained_mode.events.os_events import ReadFileRequest, ReadFileResultSuccess
from griptape_nodes.retained_mode.events.project_events import (
    ActivateWorkspaceProjectRequest,
    ActivateWorkspaceProjectResultFailure,
    ActivateWorkspaceProjectResultSuccess,
    AttemptMapAbsolutePathToProjectRequest,
    AttemptMapAbsolutePathToProjectResultFailure,
    AttemptMapAbsolutePathToProjectResultSuccess,
    AttemptMatchPathAgainstMacroRequest,
    AttemptMatchPathAgainstMacroResultFailure,
    AttemptMatchPathAgainstMacroResultSuccess,
    ExportProjectRequest,
    ExportProjectResultFailure,
    ExportProjectResultSuccess,
    GetAllSituationsForProjectRequest,
    GetAllSituationsForProjectResultFailure,
    GetAllSituationsForProjectResultSuccess,
    GetCurrentProjectRequest,
    GetCurrentProjectResultFailure,
    GetCurrentProjectResultSuccess,
    GetPathForMacroRequest,
    GetPathForMacroResultFailure,
    GetPathForMacroResultSuccess,
    GetProjectTemplateRequest,
    GetProjectTemplateResultFailure,
    GetProjectTemplateResultSuccess,
    GetSituationRequest,
    GetSituationResultFailure,
    GetSituationResultSuccess,
    GetStateForMacroRequest,
    GetStateForMacroResultFailure,
    GetStateForMacroResultSuccess,
    ImportProjectRequest,
    ImportProjectResultFailure,
    ImportProjectResultSuccess,
    ListProjectTemplatesRequest,
    ListProjectTemplatesResultSuccess,
    LoadProjectTemplateRequest,
    LoadProjectTemplateResultFailure,
    LoadProjectTemplateResultSuccess,
    MacroPath,
    PathResolutionFailureReason,
    PreviewImportProjectRequest,
    PreviewImportProjectResultFailure,
    PreviewImportProjectResultSuccess,
    ProjectTemplateInfo,
    ResolveProjectWorkspaceRequest,
    ResolveProjectWorkspaceResultSuccess,
    SaveProjectTemplateRequest,
    SaveProjectTemplateResultFailure,
    SaveProjectTemplateResultSuccess,
    SetCurrentProjectRequest,
    SetCurrentProjectResultFailure,
    SetCurrentProjectResultSuccess,
    UnregisterProjectTemplateRequest,
    UnregisterProjectTemplateResultFailure,
    UnregisterProjectTemplateResultSuccess,
    UpgradeProjectSchemaRequest,
    UpgradeProjectSchemaResultFailure,
    UpgradeProjectSchemaResultSuccess,
    ValidateProjectTemplateRequest,
    ValidateProjectTemplateResultSuccess,
)
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
from griptape_nodes.retained_mode.managers.authorization_checkpoint import (
    AuthorizationCheckpoint,
    CheckpointAction,
    CheckpointAttribute,
    CheckpointSubjectType,
)
from griptape_nodes.retained_mode.managers.settings import (
    LIBRARIES_TO_DOWNLOAD_KEY,
    LIBRARIES_TO_REGISTER_KEY,
    PROJECTS_TO_REGISTER_KEY,
    REQUIRES_ENGINE_KEY,
)
from griptape_nodes.retained_mode.publishing.project_packager import (
    extract_archive,
    is_manifest_schema_compatible,
    package_project_to_zip,
    read_manifest,
    rename_project_template,
)
from griptape_nodes.utils.file_utils import find_files_recursive
from griptape_nodes.utils.version_utils import engine_version, engine_version_failure_detail

if TYPE_CHECKING:
    from collections.abc import Callable

    from griptape_nodes.common.project_templates.directory import PerPlatformPathMacro
    from griptape_nodes.retained_mode.managers.config_manager import ConfigManager
    from griptape_nodes.retained_mode.managers.event_manager import EventManager
    from griptape_nodes.retained_mode.managers.secrets_manager import SecretsManager

logger = logging.getLogger("griptape_nodes")

# Type alias for project identifiers.
#
# A ProjectID is an opaque, unique-per-engine identifier. The UI sets a GUID by
# default, but a user may set any unique string. Consumers must NOT parse or
# construct it (e.g. by canonicalizing it as a path): it is matched verbatim
# against the registry. Legacy projects that predate the explicit `id` field use
# the canonicalized project file path string as their id (the legacy bridge), so
# the id-space is mixed (GUID/custom ids, legacy path-string ids, and the
# synthetic SYSTEM_DEFAULTS_KEY). The on-disk file path is a separate locator.
ProjectID = str

# Synthetic identifier for the system default project template
SYSTEM_DEFAULTS_KEY: ProjectID = "<system-defaults>"

# Filename for workspace-level project template overrides
WORKSPACE_PROJECT_FILE = "griptape-nodes-project.yml"

# Builtin variable name constants
BUILTIN_PROJECT_DIR = "project_dir"
BUILTIN_PROJECT_NAME = "project_name"
BUILTIN_WORKSPACE_DIR = "workspace_dir"
BUILTIN_WORKFLOW_NAME = "workflow_name"
BUILTIN_WORKFLOW_DIR = "workflow_dir"
BUILTIN_STATIC_FILES_DIR = "static_files_dir"


@dataclass(frozen=True)
class BuiltinVariableInfo:
    """Metadata about a builtin variable.

    Attributes:
        name: The variable name (e.g., "project_dir")
        is_directory: Whether this variable represents a directory path
    """

    name: str
    is_directory: bool


# Builtin variable definitions with metadata
_BUILTIN_VARIABLE_DEFINITIONS = [
    BuiltinVariableInfo(name=BUILTIN_PROJECT_DIR, is_directory=True),
    BuiltinVariableInfo(name=BUILTIN_PROJECT_NAME, is_directory=False),
    BuiltinVariableInfo(name=BUILTIN_WORKSPACE_DIR, is_directory=True),
    BuiltinVariableInfo(name=BUILTIN_WORKFLOW_NAME, is_directory=False),
    BuiltinVariableInfo(name=BUILTIN_WORKFLOW_DIR, is_directory=True),
    BuiltinVariableInfo(name=BUILTIN_STATIC_FILES_DIR, is_directory=False),
]

# Map of variable name to metadata
_BUILTIN_VARIABLE_INFO: dict[str, BuiltinVariableInfo] = {var.name: var for var in _BUILTIN_VARIABLE_DEFINITIONS}

# Builtin variables available in all macros (read-only)
BUILTIN_VARIABLES = frozenset(var.name for var in _BUILTIN_VARIABLE_DEFINITIONS)

# Variable names produced by derivation rules. These are only computed in the
# situation-macro path (on_get_path_for_macro_request runs apply_derivation_rules
# before resolution); the directory/env resolver below never runs derivation, so a
# derived token there can only ever be unresolved. Used to raise an explanatory
# error instead of a bare MISSING_REQUIRED_VARIABLES.
DERIVED_VARIABLE_NAMES = frozenset(rule.name for rule in DERIVATION_RULES)


@dataclass
class _ProjectVariableResolver:
    """Recursive resolver for project directory path_macros and environment values.

    Both directories and env vars may contain macros that reference builtins, other
    directories, other env vars, or shell env vars. Resolution walks those references
    transitively, caches results per name, and detects cycles. References that hit
    none of the known sources and are absent from shell env are left unresolved so
    the underlying ParsedMacro.resolve raises MISSING_REQUIRED_VARIABLES.

    Construct via `ProjectManager._build_variable_resolver`. Cycle detection and caches
    are instance-scoped so resolvers are single-use per call site.
    """

    template: ProjectTemplate
    get_builtin: Callable[[str], str]
    secrets_manager: SecretsManager
    builtins_cache: dict[str, str] = field(default_factory=dict)
    env_resolved: dict[str, str] = field(default_factory=dict)
    directories_resolved: dict[str, str] = field(default_factory=dict)
    in_progress: set[str] = field(default_factory=set)

    def resolve_directory(self, name: str) -> str:
        if name in self.directories_resolved:
            return self.directories_resolved[name]
        path_macro = self.template.directories[name].path_macro
        selected = self._select_platform_macro(name, path_macro)
        resolved = self._resolve_macro_string("directory", name, selected)
        self.directories_resolved[name] = resolved
        return resolved

    @staticmethod
    def _select_platform_macro(name: str, path_macro: str | PerPlatformPathMacro) -> str:
        """Pick the platform-specific path macro string from a directory definition.

        For string-form `path_macro`, returns it unchanged. For the per-platform
        mapping form, picks the active platform's value, falling back to `default`.
        Raises MacroResolutionError if neither the active platform key nor `default`
        is set.
        """
        if isinstance(path_macro, str):
            return path_macro
        selected = path_macro.select()
        if selected is None:
            msg = (
                f"Directory '{name}' has no path_macro for the current platform and no 'default' fallback was provided"
            )
            raise MacroResolutionError(
                msg,
                failure_reason=MacroResolutionFailureReason.MISSING_REQUIRED_VARIABLES,
                variable_name=name,
            )
        return selected

    def resolve_env(self, name: str) -> str:
        if name in self.env_resolved:
            return self.env_resolved[name]
        resolved = self._resolve_macro_string("environment variable", name, self.template.environment[name])
        self.env_resolved[name] = resolved
        return resolved

    def _get_builtin(self, name: str) -> str:
        if name not in self.builtins_cache:
            self.builtins_cache[name] = self.get_builtin(name)
        return self.builtins_cache[name]

    def _resolve_macro_string(self, owner_kind: str, owner_name: str, raw_value: str) -> str:
        token = f"{owner_kind}:{owner_name}"
        if token in self.in_progress:
            cycle = " -> ".join([*sorted(self.in_progress), token])
            msg = f"Cycle detected while resolving {owner_kind} '{owner_name}': {cycle}"
            raise MacroResolutionError(
                msg,
                failure_reason=MacroResolutionFailureReason.MISSING_REQUIRED_VARIABLES,
                variable_name=owner_name,
            )
        self.in_progress.add(token)
        try:
            parsed = ParsedMacro(raw_value)
            bag: MacroVariables = {}
            for var_info in parsed.get_variables():
                ref = var_info.name
                if ref in BUILTIN_VARIABLES:
                    try:
                        bag[ref] = self._get_builtin(ref)
                    except (RuntimeError, NotImplementedError) as e:
                        # An optional reference (e.g. `{workflow_dir?:/}`) degrades cleanly:
                        # leave it out of the bag so parsed.resolve() drops it, mirroring the
                        # situation-macro path in on_get_path_for_macro_request. A required
                        # builtin that can't resolve is a genuine error.
                        if not var_info.is_required:
                            continue
                        msg = (
                            f"Cannot resolve {owner_kind} '{owner_name}': "
                            f"builtin '{ref}' unavailable in current context ({e})"
                        )
                        raise MacroResolutionError(
                            msg,
                            failure_reason=MacroResolutionFailureReason.MISSING_REQUIRED_VARIABLES,
                            variable_name=ref,
                        ) from e
                elif ref in self.template.directories:
                    bag[ref] = self.resolve_directory(ref)
                elif ref in self.template.environment:
                    bag[ref] = self.resolve_env(ref)
                else:
                    shell_value = os.environ.get(ref)
                    if shell_value is not None:
                        bag[ref] = shell_value
                    elif ref in DERIVED_VARIABLE_NAMES and var_info.is_required:
                        # Derived variables are only computed in the situation-macro path
                        # (apply_derivation_rules runs there, not here). A required derived
                        # token in a directory/env macro can never resolve, so raise an
                        # explanatory error instead of a bare MISSING_REQUIRED_VARIABLES.
                        # The optional form (e.g. `{file_extension_directory?:/}`) is left
                        # unresolved and degrades cleanly via parsed.resolve().
                        msg = (
                            f"Cannot resolve {owner_kind} '{owner_name}': '{ref}' is a derived macro "
                            f"variable that is only available in situation macros (resolved per-file at "
                            f"write time), not in directory or environment path_macros. Move it to a "
                            f"situation's filename macro, e.g. `{{{ref}?:/}}`."
                        )
                        raise MacroResolutionError(
                            msg,
                            failure_reason=MacroResolutionFailureReason.MISSING_REQUIRED_VARIABLES,
                            variable_name=ref,
                        )
                    # else: leave unresolved; parsed.resolve() will raise MISSING_REQUIRED_VARIABLES
            resolved = parsed.resolve(bag, self.secrets_manager)
        finally:
            self.in_progress.discard(token)
        return resolved


@dataclass
class ProjectInfo:
    """Consolidated information about a loaded project.

    Stores all project-related data including template, validation,
    file paths, and cached parsed macros.
    """

    project_id: ProjectID
    project_file_path: Path | None  # None for system defaults or non-file sources
    project_base_dir: Path  # Directory for resolving relative paths ({project_dir})
    template: ProjectTemplate
    validation: ProjectValidationInfo

    # Cached parsed macros (populated during load for performance)
    parsed_situation_schemas: dict[str, ParsedMacro]  # situation_name -> ParsedMacro
    parsed_directory_schemas: dict[str, ParsedMacro]  # directory_name -> ParsedMacro


class _ProjectActivationOutcome(NamedTuple):
    """Result of establishing a project's config/workspace/env layers and reloading.

    `failure` is the reload failure (None on success). `workspace_changed` reports
    whether the workspace directory changed, so the caller can flag
    `altered_workflow_state` on the success result.
    """

    failure: SetCurrentProjectResultFailure | None
    workspace_changed: bool


class WorkspaceDecision(NamedTuple):
    """The workspace dir a project resolves to, plus whether activation pins it.

    `workspace_dir` is the directory whose griptape_nodes_config.json supplies the
    workspace config layer. `apply_override` is True only when activation calls
    set_workspace_override(workspace_dir): the project_workspaces mapping, the
    parent-chain inheritance, and the global-default branches. It is False when env
    vars or the project-adjacent config supply workspace_directory, because activation
    then leaves the override unset so the workspace config layer can re-point it.
    """

    workspace_dir: Path
    apply_override: bool


class _ProvisioningConfigDirs(NamedTuple):
    """The two directories that determine a project's merged provisioning config.

    `project_dir` holds the project-adjacent griptape_nodes_config.json;
    `workspace_dir` is the directory whose config supplies the workspace layer
    (decided read-only by decide_workspace). `apply_override` carries that
    decision's pin bit so the preview applies the workspace_directory override
    exactly when (and only when) activation would. The provisioning preview feeds
    all three into ConfigManager.compute_project_provisioning_config so the plan it
    shows matches what activation would reconcile.
    """

    project_dir: Path
    workspace_dir: Path
    apply_override: bool


class _ManifestValidation(NamedTuple):
    """Outcome of reading and schema-checking a project package's manifest.

    On success `manifest` is the parsed dict and `failure_reason` is None. On
    failure `manifest` is None and `failure_reason` holds the user-facing reason
    fragment (no "Attempted to ..." prefix), so each handler can prepend its own
    preview/import wording.
    """

    manifest: dict | None
    failure_reason: str | None


class ProjectManager:
    """Manages project templates, validation, and file path resolution.

    Responsibilities:
    - Load and cache project templates (system defaults + user customizations)
    - Track validation status for all load attempts (including MISSING files)
    - Parse and cache macro schemas for performance
    - Resolve file paths using situation templates and variable substitution
    - Manage current project selection
    - Handle project.yml file I/O via OSManager events

    State tracking uses two dicts:
    - registered_template_status: ALL load attempts (Path -> ProjectValidationInfo)
    - successful_templates: Only usable templates (Path -> ProjectTemplate)

    This allows UI to query validation status even when template failed to load.
    """

    def __init__(
        self,
        event_manager: EventManager,
        config_manager: ConfigManager,
        secrets_manager: SecretsManager,
    ) -> None:
        """Initialize the ProjectManager.

        Args:
            event_manager: The EventManager instance to use for event handling
            config_manager: ConfigManager instance for accessing configuration
            secrets_manager: SecretsManager instance for macro resolution
        """
        self._event_manager = event_manager
        self._config_manager = config_manager
        self._secrets_manager = secrets_manager

        # Consolidated project information storage
        self._successfully_loaded_project_templates: dict[ProjectID, ProjectInfo] = {}
        # Always populated. SYSTEM_DEFAULTS_KEY is the rest state when no user project
        # is selected. Any code path that previously cleared this to None now routes
        # back to system defaults via SetCurrentProjectRequest's default value.
        self._current_project_id: ProjectID = SYSTEM_DEFAULTS_KEY
        # Set to True at end of on_app_initialization_complete. Guards workspace switch
        # logic so expensive reloads don't fire during startup.
        self._initialization_complete: bool = False

        # Track validation status for ALL load attempts (including MISSING/UNUSABLE)
        # This allows UI to query why a project failed to load
        self._registered_template_status: dict[Path, ProjectValidationInfo] = {}

        # Snapshot of os.environ entries mutated by the currently-active project.
        # Maps env var name -> original value (or None if the var was not set before).
        # Restored on project switch so each project's env is isolated.
        self._applied_env_snapshot: dict[str, str | None] = {}

        # Transient id -> file path index used during boot to resolve id-based
        # parents whose child may load before the parent. Populated by a pre-pass
        # in _load_registered_projects and consulted by _resolve_parent_chain;
        # empty (and ignored) outside boot, where the live registry suffices.
        self._boot_id_to_file_path: dict[str, Path] = {}

        # Register event handlers
        event_manager.assign_manager_to_request_type(LoadProjectTemplateRequest, self.on_load_project_template_request)
        event_manager.assign_manager_to_request_type(GetProjectTemplateRequest, self.on_get_project_template_request)
        event_manager.assign_manager_to_request_type(
            ResolveProjectWorkspaceRequest, self.on_resolve_project_workspace_request
        )
        event_manager.assign_manager_to_request_type(
            ListProjectTemplatesRequest, self.on_list_project_templates_request
        )
        event_manager.assign_manager_to_request_type(GetSituationRequest, self.on_get_situation_request)
        event_manager.assign_manager_to_request_type(GetPathForMacroRequest, self.on_get_path_for_macro_request)
        event_manager.assign_manager_to_request_type(SetCurrentProjectRequest, self.on_set_current_project_request)
        event_manager.assign_manager_to_request_type(GetCurrentProjectRequest, self.on_get_current_project_request)
        event_manager.assign_manager_to_request_type(SaveProjectTemplateRequest, self.on_save_project_template_request)
        event_manager.assign_manager_to_request_type(
            UpgradeProjectSchemaRequest, self.on_upgrade_project_schema_request
        )
        event_manager.assign_manager_to_request_type(
            AttemptMatchPathAgainstMacroRequest, self.on_match_path_against_macro_request
        )
        event_manager.assign_manager_to_request_type(GetStateForMacroRequest, self.on_get_state_for_macro_request)
        event_manager.assign_manager_to_request_type(
            GetAllSituationsForProjectRequest, self.on_get_all_situations_for_project_request
        )
        event_manager.assign_manager_to_request_type(
            AttemptMapAbsolutePathToProjectRequest, self.on_attempt_map_absolute_path_to_project_request
        )
        event_manager.assign_manager_to_request_type(
            UnregisterProjectTemplateRequest, self.on_unregister_project_template_request
        )
        event_manager.assign_manager_to_request_type(
            ValidateProjectTemplateRequest, self.on_validate_project_template_request
        )
        event_manager.assign_manager_to_request_type(
            ActivateWorkspaceProjectRequest, self.on_activate_workspace_project_request
        )
        event_manager.assign_manager_to_request_type(ExportProjectRequest, self.on_export_project_request)
        event_manager.assign_manager_to_request_type(
            PreviewImportProjectRequest, self.on_preview_import_project_request
        )
        event_manager.assign_manager_to_request_type(ImportProjectRequest, self.on_import_project_request)

        # Register app initialization listener
        event_manager.add_listener_to_app_event(
            AppInitializationComplete,
            self.on_app_initialization_complete,
        )

        # Load system defaults eagerly so project-aware requests work before
        # AppInitializationComplete fires. Workflow scripts run in CLI mode
        # construct nodes at module import time, before the event is broadcast.
        self._load_system_defaults()
        self._current_project_id = SYSTEM_DEFAULTS_KEY

    async def on_load_project_template_request(
        self, request: LoadProjectTemplateRequest
    ) -> LoadProjectTemplateResultSuccess | LoadProjectTemplateResultFailure:
        """Load user's project.yml and merge with system defaults.

        Thin wrapper over _load_and_cache_project_template. Explicit loads
        persist the path so the project survives engine restarts.
        """
        return await self._load_and_cache_project_template(request.project_path, persist_path=True)

    async def _load_and_cache_project_template(
        self, project_path: Path, *, persist_path: bool
    ) -> LoadProjectTemplateResultSuccess | LoadProjectTemplateResultFailure:
        """Load a project.yml, merge with system defaults, and cache the result.

        Flow:
        1. Issue ReadFileRequest to OSManager (for proper Windows long path handling)
        2. Parse YAML and load partial template (overlay) using load_partial_project_template()
        3. Resolve the parent chain (if any) into a base ProjectTemplate
        4. Merge the overlay onto that base using ProjectTemplate.merge()
        5. Cache validation in registered_template_status
        6. If usable, cache template in successful_templates
        7. If persist_path, append the path to projects_to_register config
        8. Return LoadProjectTemplateResultSuccess or LoadProjectTemplateResultFailure

        persist_path is False for directory-discovered project files: the
        directory entry stays in config and is re-scanned each startup, so the
        individual files must not be persisted alongside it.
        """
        # Expand ~/env vars and resolve to absolute so the same file is always
        # located the same way regardless of how the caller spelled the path
        # (relative vs absolute, ~/ prefix, symlinks, etc.). The canonical path
        # is the file locator; _registered_template_status is keyed by it.
        project_file_path = canonicalize_for_identity(project_path)

        read_load = await self._read_overlay(project_file_path)
        if isinstance(read_load, LoadProjectTemplateResultFailure):
            return read_load
        validation, overlay = read_load

        # Derive the project id (the registry key). An explicit overlay id wins;
        # a legacy project with no id falls back to the canonical file path
        # string so it keeps a stable identity without a file rewrite. From here
        # on the id identifies the project and the path is only a locator.
        project_id = overlay.id if overlay.id is not None else str(project_file_path)

        # Fail closed on an id collision: a *different* file already holds this
        # id. Reloading the same file (same id, same path) is a no-op refresh and
        # must not collide.
        existing = self._successfully_loaded_project_templates.get(project_id)
        if existing is not None and existing.project_file_path != project_file_path:
            validation.add_error(
                field_path="id",
                message=(
                    f"Project id '{project_id}' is already used by a different project at "
                    f"'{existing.project_file_path}'. Project ids must be unique per engine."
                ),
            )
            self._registered_template_status[project_file_path] = validation
            return LoadProjectTemplateResultFailure(
                validation=validation,
                result_details=(
                    f"Attempted to load project template from '{project_file_path}'. "
                    f"Failed because its id '{project_id}' is already used by a different project at "
                    f"'{existing.project_file_path}'."
                ),
            )

        # Resolve the parent chain (if declared) into a base ProjectTemplate.
        # Cycle detection seeds the visited set with the current project's path
        # so a self-reference also fails fast.
        base_template = await self._resolve_parent_chain(
            overlay=overlay,
            project_file_path=project_file_path,
            validation=validation,
            visited={project_file_path},
        )
        if base_template is None:
            # _resolve_parent_chain records the specific cause (e.g. an
            # unregistered parent_project_id, or a cycle) on validation before
            # returning None. Surface that detail in result_details so the boot
            # warning names it, matching the collision case above.
            parent_chain_errors = [
                problem.message
                for problem in validation.problems
                if problem.severity == ProjectValidationProblemSeverity.ERROR
            ]
            failure_detail = "Failed because parent chain could not be resolved"
            if parent_chain_errors:
                failure_detail = f"{failure_detail}: {parent_chain_errors[-1]}"
            self._registered_template_status[project_file_path] = validation
            return LoadProjectTemplateResultFailure(
                validation=validation,
                result_details=f"Attempted to load project template from '{project_file_path}'. {failure_detail}",
            )

        template = ProjectTemplate.merge(base_template, overlay, validation)

        project_base_dir = project_file_path.parent

        # Parse all macros BEFORE creating ProjectInfo - collect ALL errors
        situation_schemas = self._parse_situation_macros(template.situations, validation)
        directory_schemas = self._parse_directory_macros(template.directories, validation)

        # Now check if validation is usable after collecting all errors
        if not validation.is_usable():
            self._registered_template_status[project_file_path] = validation
            return LoadProjectTemplateResultFailure(
                validation=validation,
                result_details=f"Attempted to load project template from '{project_file_path}'. Failed because template is not usable (status: {validation.status})",
            )

        # License-policy checkpoint: gate loading this project on its resolved
        # identity. A denial blocks the load -- the project is not cached as usable
        # and the failure carries the missing permissions -- so a project the policy
        # forbids never enters the engine, whether reached by explicit load or
        # directory discovery. Mirrors the activation gate, which resolves the same
        # facts; the name is passed in because the project is not cached yet.
        load_denial = GriptapeNodes.EventManager().evaluate_authorization_checkpoint(
            AuthorizationCheckpoint(
                action=CheckpointAction.LOAD_PROJECT,
                subject_type=CheckpointSubjectType.PROJECT,
                subject_id=project_id,
                attributes=self._project_checkpoint_attributes(project_id, name=template.name),
            )
        )
        if load_denial is not None:
            reason = load_denial.reason()
            validation.add_error(field_path="permission", message=reason)
            self._registered_template_status[project_file_path] = validation
            return LoadProjectTemplateResultFailure(
                validation=validation,
                result_details=(
                    f"Attempted to load project template from '{project_file_path}'. Failed because: {reason}"
                ),
            )

        # Create consolidated ProjectInfo with fully populated macro caches
        project_info = ProjectInfo(
            project_id=project_id,
            project_file_path=project_file_path,
            project_base_dir=project_base_dir,
            template=template,
            validation=validation,
            parsed_situation_schemas=situation_schemas,
            parsed_directory_schemas=directory_schemas,
        )

        # Store in new consolidated dict
        self._successfully_loaded_project_templates[project_id] = project_info

        # Track validation status for all load attempts (for UI display)
        self._registered_template_status[project_file_path] = validation

        # Persist the file path (the locator) so the project survives engine
        # restarts. PROJECTS_TO_REGISTER_KEY stores paths, not ids: boot reloads
        # each file by path and re-derives its id. Skipped for directory-discovered
        # files, which are covered by their directory entry.
        if persist_path:
            self._register_project_path(str(project_file_path))

        return LoadProjectTemplateResultSuccess(
            project_id=project_id,
            template=template,
            validation=validation,
            result_details=f"Template loaded successfully with status: {validation.status}",
        )

    async def _read_overlay(
        self, project_file_path: Path, *, record_status: bool = True
    ) -> tuple[ProjectValidationInfo, ProjectOverlayData] | LoadProjectTemplateResultFailure:
        """Read a project YAML and parse it into an overlay.

        Returns either (validation, overlay) on success or a fully formed
        LoadProjectTemplateResultFailure for the caller to return as-is.

        When record_status is True (the default, used by the load/boot flows) a failed read
        records the failure in _registered_template_status, which ListProjectTemplatesRequest
        surfaces as failed_to_load. Read-only probes (e.g. resolve_workspace_dir_for_project_id)
        pass record_status=False so a transient lookup does not inject phantom failed-load entries.
        """
        read_request = ReadFileRequest(
            file_path=str(project_file_path),
            encoding="utf-8",
            workspace_only=False,
        )
        read_result = await GriptapeNodes.ahandle_request(read_request)

        if read_result.failed():
            validation = ProjectValidationInfo(status=ProjectValidationStatus.MISSING)
            if record_status:
                self._registered_template_status[project_file_path] = validation
            return LoadProjectTemplateResultFailure(
                validation=validation,
                result_details=f"Attempted to load project template from '{project_file_path}'. Failed because file not found",
            )

        if not isinstance(read_result, ReadFileResultSuccess):
            validation = ProjectValidationInfo(status=ProjectValidationStatus.UNUSABLE)
            if record_status:
                self._registered_template_status[project_file_path] = validation
            return LoadProjectTemplateResultFailure(
                validation=validation,
                result_details=f"Attempted to load project template from '{project_file_path}'. Failed because file read returned unexpected result type",
            )

        yaml_text = read_result.content
        if not isinstance(yaml_text, str):
            validation = ProjectValidationInfo(status=ProjectValidationStatus.UNUSABLE)
            if record_status:
                self._registered_template_status[project_file_path] = validation
            return LoadProjectTemplateResultFailure(
                validation=validation,
                result_details=f"Attempted to load project template from '{project_file_path}'. Failed because template must be text, got binary content",
            )

        validation = ProjectValidationInfo(status=ProjectValidationStatus.GOOD)
        overlay = load_partial_project_template(yaml_text, validation)
        if overlay is None:
            if record_status:
                self._registered_template_status[project_file_path] = validation
            return LoadProjectTemplateResultFailure(
                validation=validation,
                result_details=f"Attempted to load project template from '{project_file_path}'. Failed because YAML could not be parsed",
            )

        return validation, overlay

    async def _resolve_parent_chain(  # noqa: C901, PLR0911
        self,
        overlay: ProjectOverlayData,
        project_file_path: Path,
        validation: ProjectValidationInfo,
        visited: set[Path],
    ) -> ProjectTemplate | None:
        """Resolve the parent chain declared by an overlay into a base ProjectTemplate.

        Parent links have two forms, checked in this precedence order:

        1. `parent_project_id` (preferred, portable): the parent is located via the
           engine registry (id -> file path), so the link survives moving the file
           between machines. If the id is not registered on this engine, resolution
           fails closed (records an error and returns None). `parent_project_path` is
           ignored entirely when an id is present.
        2. `parent_project_path` (legacy, back-compat): the parent YAML is located by
           filesystem path. A relative path resolves against the directory of
           `project_file_path` so a child can name its parent with a relative path
           (e.g. `parent_project_path: ../base/griptape-nodes-project.yml`). A
           per-platform mapping is reduced to the active OS's path first; a mapping
           with no key for this OS and no `default` is treated as no parent on this
           platform.
        3. Neither set: the base is `DEFAULT_PROJECT_TEMPLATE`.

        Once the parent file path is located, the parent YAML is read, recursively
        resolved, merged onto its own ancestors, and returned as the base for the
        caller. Macro tokens are rejected by the loader; only absolute or relative
        paths reach the path-based branch.

        Cycle detection: `visited` carries the canonical Paths of every project file
        that is currently being resolved further down the chain. A cycle records an
        error on `validation` and returns None.

        Errors during parent resolution (missing file, unregistered id, unparsable
        YAML, cycle) are recorded on the child's `validation` and surfaced to the
        caller as a None return.
        """
        # Precedence: an explicit parent_project_id (portable, registry-located)
        # wins and the path is ignored. parent_project_path is the legacy
        # fallback only when no id is present.
        if overlay.parent_project_id is not None:
            parent_link_field = "parent_project_id"
            parent_label = overlay.parent_project_id
            parent_file_path = self._locate_parent_file_path_by_id(overlay.parent_project_id)
            if parent_file_path is None:
                validation.add_error(
                    field_path=parent_link_field,
                    message=(
                        f"Parent project id '{overlay.parent_project_id}' is not registered on this engine. "
                        "Register the parent project before loading this child."
                    ),
                    line_number=overlay.line_info.get_line(parent_link_field),
                )
                return None
        elif overlay.parent_project_path is not None:
            parent_link_field = "parent_project_path"
            # Reduce the (possibly per-platform) value to a single string for the
            # active platform. A per-platform mapping with no key matching the
            # active OS and no `default` returns None — treat that as "no parent
            # on this platform" and fall back to the system default base.
            selected_parent = select_project_path(overlay.parent_project_path)
            if selected_parent is None:
                logger.debug(
                    "parent_project_path %r has no entry for the active platform and no default; "
                    "treating as no parent on this OS",
                    overlay.parent_project_path,
                )
                return default_template_for_version(overlay.project_template_schema_version)
            parent_label = selected_parent
            parent_path_raw = Path(selected_parent)
            if not parent_path_raw.is_absolute():
                parent_path_raw = project_file_path.parent / parent_path_raw
            parent_file_path = canonicalize_for_identity(parent_path_raw)
        else:
            return default_template_for_version(overlay.project_template_schema_version)

        if parent_file_path in visited:
            cycle = " -> ".join(str(p) for p in [*sorted(visited, key=str), parent_file_path])
            validation.add_error(
                field_path=parent_link_field,
                message=f"Cycle detected in project parent chain: {cycle}",
                line_number=overlay.line_info.get_line(parent_link_field),
            )
            return None

        parent_load = await self._read_overlay(parent_file_path)
        if isinstance(parent_load, LoadProjectTemplateResultFailure):
            # Surface the parent's failure as a child-level error pointing at the link.
            parent_status = parent_load.validation.status
            validation.add_error(
                field_path=parent_link_field,
                message=(f"Parent project '{parent_label}' could not be loaded (status: {parent_status})"),
                line_number=overlay.line_info.get_line(parent_link_field),
            )
            return None
        parent_validation, parent_overlay = parent_load

        if not parent_validation.is_usable():
            validation.add_error(
                field_path=parent_link_field,
                message=(f"Parent project '{parent_label}' has validation errors (status: {parent_validation.status})"),
                line_number=overlay.line_info.get_line(parent_link_field),
            )
            return None

        ancestor_base = await self._resolve_parent_chain(
            overlay=parent_overlay,
            project_file_path=parent_file_path,
            validation=validation,
            visited={*visited, parent_file_path},
        )
        if ancestor_base is None:
            return None

        # Merge the parent overlay onto its own ancestor base using a fresh
        # validation info so the parent's overrides don't bleed into the child's
        # validation record. Errors during the parent merge still propagate
        # upward via add_error below.
        parent_merge_validation = ProjectValidationInfo(status=ProjectValidationStatus.GOOD)
        parent_template = ProjectTemplate.merge(ancestor_base, parent_overlay, parent_merge_validation)
        if not parent_merge_validation.is_usable():
            for problem in parent_merge_validation.problems:
                validation.add_error(
                    field_path=f"{parent_link_field}.{problem.field_path}",
                    message=f"Parent '{parent_label}': {problem.message}",
                    line_number=overlay.line_info.get_line(parent_link_field),
                )
            return None
        return parent_template

    def get_loaded_project_dir(self, project_id: str) -> Path | None:
        """Return the directory of a loaded, file-backed project, or None.

        The directory holds the project YAML and its adjacent
        griptape_nodes_config.json. Returns None when the project is not loaded
        or has no backing file (e.g. system defaults), so callers can treat both
        as "no project-adjacent config to read".
        """
        project_info = self._successfully_loaded_project_templates.get(project_id)
        if project_info is None:
            return None
        if project_info.project_file_path is None:
            return None
        return project_info.project_file_path.parent

    def _locate_parent_file_path_by_id(self, parent_project_id: str) -> Path | None:
        """Locate a parent project's file path from its opaque id.

        Checks the live registry first (the parent is normally already loaded at
        runtime), then the transient boot index built by _load_registered_projects
        for the child-before-parent case during startup. Returns None when the id
        is not registered on this engine, which the caller treats as fail-closed.
        """
        existing = self._successfully_loaded_project_templates.get(parent_project_id)
        if existing is not None and existing.project_file_path is not None:
            return existing.project_file_path
        return self._boot_id_to_file_path.get(parent_project_id)

    def on_get_project_template_request(
        self, request: GetProjectTemplateRequest
    ) -> GetProjectTemplateResultSuccess | GetProjectTemplateResultFailure:
        """Get cached template for a project ID."""
        project_info = self._successfully_loaded_project_templates.get(request.project_id)

        if project_info is None:
            return GetProjectTemplateResultFailure(
                result_details=f"Attempted to get project template for '{request.project_id}'. Failed because template not loaded yet",
            )

        return GetProjectTemplateResultSuccess(
            template=project_info.template,
            validation=project_info.validation,
            result_details=f"Successfully retrieved project template for '{request.project_id}'. Status: {project_info.validation.status}",
        )

    async def on_resolve_project_workspace_request(
        self, request: ResolveProjectWorkspaceRequest
    ) -> ResolveProjectWorkspaceResultSuccess:
        """Resolve the workspace dir a project would use, without loading or activating it.

        A None resolution (the id maps to no readable project file) is a success carrying
        workspace_dir=None, matching resolve_workspace_dir_for_project_id's "nothing to resolve"
        contract; the GUI treats null as "no hint to show".
        """
        resolved = await self.resolve_workspace_dir_for_project_id(request.project_id)
        return ResolveProjectWorkspaceResultSuccess(
            workspace_dir=str(resolved) if resolved is not None else None,
            result_details=f"Resolved workspace for '{request.project_id}': {resolved}",
        )

    def on_list_project_templates_request(
        self, request: ListProjectTemplatesRequest
    ) -> ListProjectTemplatesResultSuccess:
        """List all project templates that have been loaded or attempted to load.

        Returns separate lists for successfully loaded and failed templates.
        """
        successfully_loaded: list[ProjectTemplateInfo] = []
        failed_to_load: list[ProjectTemplateInfo] = []

        # Map each loaded project's canonical file path to its id so a legacy
        # child's parent_project_path can be resolved to the parent's actual id
        # (the registry key), and so the failed-templates pass can correlate
        # Path-keyed status entries against the id-keyed registry by path.
        file_path_to_id: dict[Path, ProjectID] = {
            info.project_file_path: pid
            for pid, info in self._successfully_loaded_project_templates.items()
            if info.project_file_path is not None
        }

        # Gather successfully loaded templates from _successfully_loaded_project_templates
        for project_id, project_info in self._successfully_loaded_project_templates.items():
            # Skip system builtins unless requested
            if not request.include_system_builtins and project_id == SYSTEM_DEFAULTS_KEY:
                continue

            successfully_loaded.append(self._build_loaded_template_info(project_id, project_info, file_path_to_id))

        # Gather failed templates from _registered_template_status.
        # These are tracked by Path, so correlate against the id-keyed registry
        # by file path rather than by string-casting the path to an id.
        for template_path, validation in self._registered_template_status.items():
            # Skip if already loaded successfully (status might be FLAWED but still loaded)
            if template_path in file_path_to_id:
                continue

            project_id = str(template_path)

            # Skip system builtins unless requested
            if not request.include_system_builtins and project_id == SYSTEM_DEFAULTS_KEY:
                continue

            # Only include if status indicates failure (UNUSABLE or MISSING)
            if not validation.is_usable():
                failed_to_load.append(ProjectTemplateInfo(project_id=project_id, validation=validation))

        return ListProjectTemplatesResultSuccess(
            successfully_loaded=successfully_loaded,
            failed_to_load=failed_to_load,
            result_details=f"Successfully listed project templates. Loaded: {len(successfully_loaded)}, Failed: {len(failed_to_load)}",
        )

    def _build_loaded_template_info(
        self,
        project_id: ProjectID,
        project_info: ProjectInfo,
        file_path_to_id: dict[Path, ProjectID],
    ) -> ProjectTemplateInfo:
        """Build the ProjectTemplateInfo for a successfully loaded template.

        Resolves the parent's id and the project-adjacent engine-version
        compatibility for the listing emitted to the GUI.
        """
        # Emit the parent's id so the GUI can reconstruct the hierarchy by
        # matching it against another entry's project_id. An explicit
        # parent_project_id is already an id and is emitted as-is. A legacy
        # parent_project_path is resolved to a canonical path, then mapped to
        # the parent's actual id via the registry; if the parent is not
        # registered, its id is its canonical path string (the legacy bridge),
        # so the canonical string is the correct fallback. Per-platform
        # mappings are reduced to the active platform's value first.
        resolved_parent_id: str | None = None
        if project_info.template.parent_project_id is not None:
            resolved_parent_id = project_info.template.parent_project_id
        else:
            selected_parent = select_project_path(project_info.template.parent_project_path)
            if selected_parent is not None:
                parent_path = Path(selected_parent)
                if not parent_path.is_absolute() and project_info.project_file_path is not None:
                    parent_path = project_info.project_file_path.parent / parent_path
                canonical_parent = canonicalize_for_identity(parent_path)
                resolved_parent_id = file_path_to_id.get(canonical_parent, str(canonical_parent))

        # Read the project-adjacent config's requires_engine specifier without
        # merging it into the live config, so the GUI can disable activation
        # for a project the running engine can't satisfy. A project with no
        # backing file (or no specifier) is compatible by default.
        required_engine_version: str | None = None
        if project_info.project_file_path is not None:
            config_path = project_info.project_file_path.parent / "griptape_nodes_config.json"
            required_engine_version = self._config_manager.read_config_file_value(
                config_path, REQUIRES_ENGINE_KEY, default=None
            )
        engine_version_reason = engine_version_failure_detail(required_engine_version)

        return ProjectTemplateInfo(
            project_id=project_id,
            validation=project_info.validation,
            name=project_info.template.name,
            project_file_path=(
                str(project_info.project_file_path) if project_info.project_file_path is not None else None
            ),
            parent_project_id=resolved_parent_id,
            engine_version_compatible=engine_version_reason is None,
            required_engine_version=required_engine_version,
            current_engine_version=engine_version,
            engine_version_reason=engine_version_reason,
        )

    def on_get_situation_request(
        self, request: GetSituationRequest
    ) -> GetSituationResultSuccess | GetSituationResultFailure:
        """Get the complete situation template for a specific situation.

        Returns the full SituationTemplate including macro and policy.

        Flow:
        1. Get current project
        2. Get template from successful_templates
        3. Get situation from template
        4. Return complete SituationTemplate
        """
        current_project_request = GetCurrentProjectRequest()
        current_project_result = self.on_get_current_project_request(current_project_request)

        if not isinstance(current_project_result, GetCurrentProjectResultSuccess):
            return GetSituationResultFailure(
                result_details=f"Attempted to get situation '{request.situation_name}'. Failed because no current project is set or template not loaded",
            )

        template = current_project_result.project_info.template

        situation = template.situations.get(request.situation_name)
        if situation is None:
            return GetSituationResultFailure(
                result_details=f"Attempted to get situation '{request.situation_name}'. Failed because situation not found",
            )

        return GetSituationResultSuccess(
            situation=situation,
            result_details=f"Successfully retrieved situation '{request.situation_name}'. Macro: {situation.macro}, Policy: create_dirs={situation.policy.create_dirs}, on_collision={situation.policy.on_collision}",
        )

    def on_get_path_for_macro_request(  # noqa: C901, PLR0911, PLR0912, PLR0915
        self, request: GetPathForMacroRequest
    ) -> GetPathForMacroResultSuccess | GetPathForMacroResultFailure:
        """Resolve ANY macro schema with variables to final Path.

        Flow:
        1. Get current project
        2. Apply derivation rules to inject derived variables (e.g. file_extension_directory)
        3. Get variables from ParsedMacro.get_variables()
        4. For each variable:
           - If in directories dict → resolve directory, add to resolution bag
           - Else if in user_supplied_vars → use user value
           - If in BOTH → ERROR: RESERVED_NAME_COLLISION
           - Else → collect as missing
        5. If any missing → ERROR: MISSING_REQUIRED_VARIABLES
        6. Resolve macro with complete variable bag
        7. Return resolved Path
        """
        current_project_request = GetCurrentProjectRequest()
        current_project_result = self.on_get_current_project_request(current_project_request)

        if not isinstance(current_project_result, GetCurrentProjectResultSuccess):
            return GetPathForMacroResultFailure(
                failure_reason=PathResolutionFailureReason.MACRO_RESOLUTION_ERROR,
                result_details="Attempted to resolve macro path. Failed because no current project is set or template not loaded",
            )

        project_info = current_project_result.project_info
        template = project_info.template

        # Apply derivation rules centrally so every caller of GetPathForMacroRequest
        # gets derived variables (e.g. file_extension_directory) without duplicating
        # the pre-pass at each call site. Rules that can't fire (missing inputs,
        # unreferenced output) abstain silently, so plain macros pass through unchanged.
        resolved_macro_path = apply_derivation_rules(
            MacroPath(request.parsed_macro, request.variables), DERIVATION_RULES
        )
        effective_variables: MacroVariables = resolved_macro_path.variables

        variable_infos = request.parsed_macro.get_variables()
        directory_names = set(template.directories.keys())
        user_provided_names = set(effective_variables.keys())

        # Check for directory/user variable name conflicts
        conflicting = directory_names & user_provided_names
        if conflicting:
            return GetPathForMacroResultFailure(
                failure_reason=PathResolutionFailureReason.RESERVED_NAME_COLLISION,
                conflicting_variables=conflicting,
                result_details=f"Attempted to resolve macro path. Failed because variables conflict with directory names: {', '.join(sorted(conflicting))}",
            )

        resolution_bag: MacroVariables = {}
        disallowed_overrides: set[str] = set()
        # Directories and project env vars may reference each other, builtins, or shell
        # env vars via inner macros (e.g. `watch_output: "{watch_folder}/outputs"`).
        # A shared resolver caches results across both sources so nested references
        # don't re-parse or re-evaluate the same path_macro twice per request.
        resolver = self._build_variable_resolver(template, project_info)

        for var_info in variable_infos:
            var_name = var_info.name

            if var_name in directory_names:
                try:
                    resolution_bag[var_name] = resolver.resolve_directory(var_name)
                except MacroResolutionError as e:
                    return GetPathForMacroResultFailure(
                        failure_reason=PathResolutionFailureReason.MACRO_RESOLUTION_ERROR,
                        missing_variables=e.missing_variables,
                        result_details=f"Attempted to resolve macro path. Failed to resolve directory '{var_name}': {e}",
                    )
            elif var_name in user_provided_names:
                resolution_bag[var_name] = effective_variables[var_name]

            if var_name in BUILTIN_VARIABLES:
                try:
                    builtin_value = self._get_builtin_variable_value(var_name, project_info)
                except (RuntimeError, NotImplementedError) as e:
                    if not var_info.is_required:
                        continue
                    return GetPathForMacroResultFailure(
                        failure_reason=PathResolutionFailureReason.MACRO_RESOLUTION_ERROR,
                        result_details=f"Attempted to resolve macro path. Failed because builtin variable '{var_name}' cannot be resolved: {e}",
                    )
                # Confirm no monkey business with trying to override builtin values
                existing = resolution_bag.get(var_name)
                if existing is not None:
                    # For directory builtin variables, compare as resolved paths
                    builtin_info = _BUILTIN_VARIABLE_INFO.get(var_name)
                    if builtin_info and builtin_info.is_directory:
                        resolved_existing = resolve_path_safely(Path(str(existing)))
                        resolved_builtin = resolve_path_safely(Path(builtin_value))
                        if resolved_existing != resolved_builtin:
                            disallowed_overrides.add(var_name)
                    elif str(existing) != builtin_value:
                        disallowed_overrides.add(var_name)
                else:
                    resolution_bag[var_name] = builtin_value

        # Check if user tried to override builtins with different values
        if disallowed_overrides:
            return GetPathForMacroResultFailure(
                failure_reason=PathResolutionFailureReason.RESERVED_NAME_COLLISION,
                conflicting_variables=disallowed_overrides,
                result_details=f"Attempted to resolve macro path. Failed because cannot override builtin variables: {', '.join(sorted(disallowed_overrides))}",
            )

        # Project env vars fill any remaining referenced variable names. Precedence (high to
        # low): builtins > directories > caller-supplied > project env > shell env. Project
        # env values are recursively resolved (may reference builtins, directories, other
        # project env vars, or shell env vars) before being placed into the resolution bag.
        # Env keys that collide with a directory name or builtin AND are referenced by this
        # macro are rejected as RESERVED_NAME_COLLISION so users don't silently shadow core
        # resolution state.
        referenced_var_names = {v.name for v in variable_infos}
        project_env = template.environment
        env_collisions = set(project_env) & (directory_names | BUILTIN_VARIABLES) & referenced_var_names
        if env_collisions:
            return GetPathForMacroResultFailure(
                failure_reason=PathResolutionFailureReason.RESERVED_NAME_COLLISION,
                conflicting_variables=env_collisions,
                result_details=f"Attempted to resolve macro path. Failed because project environment variables collide with directory or builtin names: {', '.join(sorted(env_collisions))}",
            )
        env_needed = {v.name for v in variable_infos if v.name not in resolution_bag and v.name in project_env}
        for var_name in env_needed:
            try:
                resolution_bag[var_name] = resolver.resolve_env(var_name)
            except MacroResolutionError as e:
                return GetPathForMacroResultFailure(
                    failure_reason=PathResolutionFailureReason.MACRO_RESOLUTION_ERROR,
                    missing_variables=e.missing_variables,
                    result_details=f"Attempted to resolve macro path. Failed to resolve project environment variable '{var_name}': {e}",
                )

        # Shell environment is the final fallback, below project env. Lets authors reference
        # any var set in their shell ({HOME}, {USER}, etc.) without declaring it in project.yml.
        # Reserved names (builtins/directories) silently win: shells have hundreds of vars and
        # we can't police accidental shadowing.
        for var_info in variable_infos:
            var_name = var_info.name
            if var_name in resolution_bag:
                continue
            shell_value = os.environ.get(var_name)
            if shell_value is not None:
                resolution_bag[var_name] = shell_value

        required_vars = {v.name for v in variable_infos if v.is_required}
        provided_vars = set(resolution_bag.keys())
        missing = required_vars - provided_vars

        if missing:
            return GetPathForMacroResultFailure(
                failure_reason=PathResolutionFailureReason.MISSING_REQUIRED_VARIABLES,
                missing_variables=missing,
                result_details=f"Attempted to resolve macro path. Failed because missing required variables: {', '.join(sorted(missing))}",
            )

        try:
            resolved_string = request.parsed_macro.resolve(resolution_bag, self._secrets_manager)
        except MacroResolutionError as e:
            if e.failure_reason == MacroResolutionFailureReason.MISSING_REQUIRED_VARIABLES:
                path_failure_reason = PathResolutionFailureReason.MISSING_REQUIRED_VARIABLES
            else:
                path_failure_reason = PathResolutionFailureReason.MACRO_RESOLUTION_ERROR

            return GetPathForMacroResultFailure(
                failure_reason=path_failure_reason,
                missing_variables=e.missing_variables,
                result_details=f"Attempted to resolve macro path. Failed because macro resolution error: {e}",
            )

        resolved_path = Path(resolved_string)

        # Make absolute path by resolving against the workspace directory.
        # resolve_file_path handles ~, env vars, and absolute paths in addition to relative paths.
        workspace_path = self._config_manager.workspace_path
        absolute_path = resolve_file_path(resolved_string, workspace_path)

        return GetPathForMacroResultSuccess(
            resolved_path=resolved_path,
            absolute_path=absolute_path,
            result_details=f"Successfully resolved macro path. Result: {resolved_path}",
        )

    # Keys we refuse to silently clobber. Users can still set them from their
    # project.yml, but we emit a warning so overrides are visible in logs.
    _DANGEROUS_ENV_KEYS: frozenset[str] = frozenset({"PATH", "HOME", "PYTHONPATH", "LD_LIBRARY_PATH"})

    def get_pre_project_environ(self) -> dict[str, str]:
        """Return a copy of os.environ with the active project's env mutations reverted.

        A worker is spawned by the orchestrator, which has already applied its current
        project's environment to os.environ. Inheriting that polluted environ would make
        the worker snapshot a baseline that already contains project A's values, so on a
        later switch to project B the worker could not unset the keys project A added.
        Spawning with this reconstructed pre-project environ gives the worker the same
        clean baseline a freshly launched engine would have. Does not mutate os.environ.
        """
        base = dict(os.environ)
        for key, original in self._applied_env_snapshot.items():
            if original is None:
                base.pop(key, None)
            else:
                base[key] = original
        return base

    def _restore_project_env(self) -> None:
        """Revert any os.environ entries mutated by the currently-active project."""
        for key, original in self._applied_env_snapshot.items():
            if original is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = original
        self._applied_env_snapshot = {}

    def _apply_project_env(self, project_info: ProjectInfo) -> None:
        """Apply template.environment to os.environ, snapshotting originals for later restore.

        Env values are recursively resolved before being written to os.environ. Values that
        fail to resolve (e.g. reference a workflow-context builtin when no workflow is active,
        or form a cycle) are skipped with a warning so one bad entry doesn't poison the rest.
        """
        template = project_info.template
        try:
            resolved_env = self._resolve_project_env_values(template, project_info)
        except MacroResolutionError as e:
            logger.warning("Failed to resolve project environment variables; skipping os.environ application: %s", e)
            return
        for key, value in resolved_env.items():
            if key in self._DANGEROUS_ENV_KEYS:
                logger.warning("Project template is overriding sensitive environment variable '%s'", key)
            self._applied_env_snapshot[key] = os.environ.get(key)
            os.environ[key] = value

    def _resolve_project_env_values(self, template: ProjectTemplate, project_info: ProjectInfo) -> dict[str, str]:
        """Recursively resolve every entry in template.environment to a final string.

        Returns the full env-resolution map. See `_ProjectVariableResolver` for the
        recursion and reference-lookup rules.
        """
        resolver = self._build_variable_resolver(template, project_info)
        for key in template.environment:
            resolver.resolve_env(key)
        return dict(resolver.env_resolved)

    def _build_variable_resolver(
        self, template: ProjectTemplate, project_info: ProjectInfo
    ) -> _ProjectVariableResolver:
        """Build a resolver that recursively resolves directories and env vars for this project."""
        return _ProjectVariableResolver(
            template=template,
            get_builtin=lambda name: self._get_builtin_variable_value(name, project_info),
            secrets_manager=self._secrets_manager,
        )

    def resolve_provisioning_config_dirs(self, project_id: str) -> _ProvisioningConfigDirs | None:
        """Resolve the project-adjacent and workspace dirs for a provisioning preview.

        Looks up `project_id` verbatim as the registry key, the same way
        on_set_current_project_request does: the id is opaque and must NOT be
        canonicalized, or a GUID (or custom string) would be treated as a relative
        path against the CWD and miss the registry. Legacy projects whose id is a
        canonical path string were already canonicalized at load time, so a verbatim
        lookup still hits. Finds the loaded file-backed project, then decides its
        workspace dir + override bit read-only via decide_workspace. Returns None when
        the project is not loaded or has no backing file, mirroring
        get_loaded_project_dir's "nothing to preview" contract. Mutates no config state.
        """
        project_info = self._successfully_loaded_project_templates.get(project_id)
        if project_info is None:
            return None
        if project_info.project_file_path is None:
            return None

        project_file_path = project_info.project_file_path
        project_dir = project_file_path.parent
        project_config = self._config_manager.read_config_file(project_dir / "griptape_nodes_config.json")
        env_config = self._config_manager.read_env_config()
        template_workspace_dir = self._resolve_template_workspace_dir(
            project_info.template.workspace_dir, project_file_path
        )
        decision = self.decide_workspace(
            project_file_path, project_config, env_config, template_workspace_dir=template_workspace_dir
        )
        return _ProvisioningConfigDirs(
            project_dir=project_dir,
            workspace_dir=decision.workspace_dir,
            apply_override=decision.apply_override,
        )

    async def resolve_workspace_dir_for_project_id(self, project_id: str) -> Path | None:
        """Resolve the workspace directory a project would use WITHOUT loading it.

        Mirrors what activation's decide_workspace produces, but works for a project absent from
        the live registry. The id is resolved to a file path by an index that prefers the live
        registry and falls back to a read-only disk scan of projects_to_register (see
        _build_unloaded_id_index); a legacy id that is itself a canonical project file path is
        accepted directly. Returns None when the id resolves to no readable project file, matching
        resolve_provisioning_config_dirs' "nothing to resolve" contract.

        Branches 0-3 and 4-result/5 are computed by the same _decide_workspace_pre/post_inheritance
        helpers decide_workspace uses, so they cannot drift. The template's own workspace_dir (branch
        0, highest priority) is read from this project's overlay on disk rather than a loaded
        template, so it resolves without loading the project. Only branch 4 also differs: the parent
        chain is walked offline from disk (_inherit_workspace_from_parents_offline), reading each
        ancestor's overlay rather than its loaded template, so resolving a parent's workspace never
        forces that parent to be loaded/enabled. Parents are guaranteed to be registered
        (projects_to_register), which is what _build_unloaded_id_index relies on to map their ids to
        paths; they need not be loaded for this to work.

        The returned path is decide_workspace's selected workspace directory (the workspace-config
        layer source), expanded and resolved the way ConfigManager.set_workspace_override resolves
        it. Consistent with resolve_provisioning_config_dirs, it is the decision dir; it does not
        replay the unpinned branch-3 config-merge re-point the live config merge would apply.
        """
        id_index = await self._build_unloaded_id_index()
        project_file_path = id_index.get(project_id)
        if project_file_path is None:
            legacy_path_candidate = canonicalize_for_identity(Path(project_id))
            if not legacy_path_candidate.is_file():
                return None
            project_file_path = legacy_path_candidate

        project_config = self._config_manager.read_config_file(project_file_path.parent / "griptape_nodes_config.json")
        env_config = self._config_manager.read_env_config()

        # Read this project's own overlay (read-only, no status recording) to source its
        # workspace_dir field for branch 0, so an unloaded project still honors a declared workspace.
        template_workspace_dir: str | None = None
        own_overlay_load = await self._read_overlay(project_file_path, record_status=False)
        if not isinstance(own_overlay_load, LoadProjectTemplateResultFailure):
            _, own_overlay = own_overlay_load
            template_workspace_dir = self._resolve_template_workspace_dir(own_overlay.workspace_dir, project_file_path)

        pre_inheritance = self._decide_workspace_pre_inheritance(
            project_file_path, project_config, env_config, template_workspace_dir
        )
        if pre_inheritance is not None:
            return self._resolve_workspace_dir(pre_inheritance.workspace_dir)

        inherited = await self._inherit_workspace_from_parents_offline(project_file_path, id_index)
        decision = self._decide_workspace_post_inheritance(project_file_path, inherited)
        return self._resolve_workspace_dir(decision.workspace_dir)

    def _resolve_workspace_dir(self, workspace_dir: Path) -> Path:
        """Expand and resolve a decided workspace dir the way ConfigManager.set_workspace_override does."""
        return Path(workspace_dir).expanduser().resolve()

    async def _build_unloaded_id_index(self) -> dict[str, Path]:
        """Build a read-only project-id -> file-path index spanning loaded and registered projects.

        Mirrors the boot pre-pass in _load_registered_projects (id -> canonical path), but persists
        nothing and is safe to call at runtime. The loaded-template registry is seeded first so an
        already-loaded project resolves without a disk read; projects_to_register entries are then
        scanned from disk (directories expanded with find_files_recursive, the same way
        _load_projects_from_directory discovers files) and indexed only when an overlay declares an
        id. This disk scan is what lets a registered-but-unloaded parent map its id to a path without
        loading it. Loaded-template entries win over disk-scanned ones for the same id.
        """
        id_index: dict[str, Path] = {
            pid: info.project_file_path
            for pid, info in self._successfully_loaded_project_templates.items()
            if info.project_file_path is not None
        }

        registered_entries: list[str | dict | PerPlatformProjectPath] = (
            self._config_manager.get_config_value(PROJECTS_TO_REGISTER_KEY, default=[]) or []
        )
        resolved_paths = self._resolve_registered_entry_paths(registered_entries)
        directory_paths = [path for path in resolved_paths if path.is_dir()]
        file_paths = [path for path in resolved_paths if not path.is_dir()]
        # Canonicalize directory-discovered files the same way _load_projects_from_directory does, so
        # their paths collide with registry paths under the path-identity comparisons used downstream.
        for directory in directory_paths:
            discovered = await find_files_recursive(directory, WORKSPACE_PROJECT_FILE)
            file_paths.extend(canonicalize_for_identity(path) for path in discovered)

        for canonical_path in file_paths:
            read_load = await self._read_overlay(canonical_path, record_status=False)
            if isinstance(read_load, LoadProjectTemplateResultFailure):
                continue
            _, overlay = read_load
            if overlay.id is not None and overlay.id not in id_index:
                id_index[overlay.id] = canonical_path

        return id_index

    async def _inherit_workspace_from_parents_offline(
        self, project_file_path: Path, id_index: dict[str, Path]
    ) -> str | None:
        """Offline analogue of _inherit_workspace_from_parents: walk the parent chain from disk.

        Resolves an ancestor's workspace without forcing that ancestor to be loaded/enabled. The
        chain traversal, cycle guard, and single per-node overlay read live in the shared
        _nearest_ancestor_value_offline; only the per-node probe (an ancestor's explicit workspace,
        read from config) is supplied here. The walk begins at the parent: the starting project's own
        explicit sources are handled by _decide_workspace_pre_inheritance.
        """
        project_workspaces = self._config_manager.get_config_value(
            "project_workspaces",
            config_source="user_config",
            default={},
        )

        def probe(node_path: Path, _overlay: ProjectOverlayData) -> str | None:
            # Workspace is read from config, not the overlay; the overlay was read by the walker to
            # follow the parent link and doubles as the ancestor's readability check.
            return self._resolve_node_explicit_workspace(node_path, project_workspaces)

        return await self._nearest_ancestor_value_offline(project_file_path, id_index, probe)

    def _resolve_parent_id_to_path(self, parent_id: ProjectID, id_index: dict[str, Path]) -> Path | None:
        """Map a reduced parent id to a project file path for the offline walk, or None if unresolvable.

        A parent_project_id (or a legacy path that hit the index) maps through id_index. An
        unregistered legacy parent_project_path reduces to its canonical path string, which is itself
        the parent file path -- follow it directly from disk rather than failing closed, since the
        whole point of the offline walk is to not require the parent to be loaded.
        """
        indexed_path = id_index.get(parent_id)
        if indexed_path is not None:
            return indexed_path

        parent_path_candidate = Path(parent_id)
        if not parent_path_candidate.is_file():
            return None
        return parent_path_candidate

    async def resolve_libraries_root_for_project_id(self, project_id: str) -> Path | None:
        """Resolve the libraries root a project would use WITHOUT loading it, or None for the default.

        Offline analogue of decide_libraries_root, used by the provisioning preview so the previewed
        SKIP/INSTALL/OVERWRITE plan matches what activation reconciles. The id is resolved to a file
        path the same way resolve_workspace_dir_for_project_id does. Branch 0 reads this project's own
        libraries_dir from its on-disk overlay; branch 1 walks the parent chain offline. Returns None
        when no libraries_dir is declared anywhere in the chain, so the caller falls back to the
        workspace-relative libraries directory.
        """
        id_index = await self._build_unloaded_id_index()
        project_file_path = id_index.get(project_id)
        if project_file_path is None:
            legacy_path_candidate = canonicalize_for_identity(Path(project_id))
            if not legacy_path_candidate.is_file():
                return None
            project_file_path = legacy_path_candidate

        own_overlay_load = await self._read_overlay(project_file_path, record_status=False)
        if not isinstance(own_overlay_load, LoadProjectTemplateResultFailure):
            _, own_overlay = own_overlay_load
            template_libraries_dir = self._resolve_template_libraries_dir(own_overlay.libraries_dir, project_file_path)
            if template_libraries_dir is not None:
                return Path(template_libraries_dir)

        inherited = await self._inherit_libraries_dir_from_parents_offline(project_file_path, id_index)
        if inherited is not None:
            return Path(inherited)
        return None

    async def _inherit_libraries_dir_from_parents_offline(
        self, project_file_path: Path, id_index: dict[str, Path]
    ) -> str | None:
        """Offline analogue of _inherit_libraries_dir_from_parents: walk the parent chain from disk.

        Resolves an ancestor's libraries_dir without forcing that ancestor to be loaded/enabled. The
        chain traversal, cycle guard, and single per-node overlay read live in the shared
        _nearest_ancestor_value_offline; only the per-node probe (an ancestor's template libraries_dir,
        read from the overlay the walker already loaded) is supplied here. The walk begins at the
        parent: the starting project's own libraries_dir is handled by branch 0 of the caller.
        """

        def probe(node_path: Path, overlay: ProjectOverlayData) -> str | None:
            return self._resolve_template_libraries_dir(overlay.libraries_dir, node_path)

        return await self._nearest_ancestor_value_offline(project_file_path, id_index, probe)

    async def _nearest_ancestor_value_offline(
        self,
        project_file_path: Path,
        id_index: dict[str, Path],
        probe: Callable[[Path, ProjectOverlayData], str | None],
    ) -> str | None:
        """Walk the explicit parent chain from disk and return the first probe hit, or None.

        Shared skeleton for the offline workspace and libraries inheritance walks, used when a target
        project may not be loaded (e.g. the provisioning preview). Reads each node's overlay from disk
        exactly ONCE, at the top of the loop, and uses it both to reduce the parent link (via the
        shared _reduce_parent_link_to_id) and to probe that node. Each reduced id maps back to a file
        path through `id_index` for a parent_project_id (or a legacy path already indexed), else the
        reduced canonical path is followed directly from disk for an unregistered legacy
        parent_project_path; an id with no index entry and no readable file fails closed (None).

        The start project is NOT probed: its own value is handled by the caller's branch 0 / the
        earlier decide_workspace branches, so the walk begins at the parent. A node whose overlay
        cannot be read returns None (fail-closed) -- an unreadable project YAML is not a valid chain
        link. A visited id-set guards against a cyclic parent chain.
        """
        file_path_to_id: dict[Path, ProjectID] = {path: pid for pid, path in id_index.items()}

        # Seed the cycle guard with the start project's id (when it has one in the index) so a chain
        # that loops back to the start is detected on the hop into it, mirroring the live walk. A
        # legacy start reachable only by path has no index id; it is left unseeded.
        start_id = file_path_to_id.get(project_file_path)
        visited: set[ProjectID] = {start_id} if start_id is not None else set()

        current_path: Path | None = project_file_path
        is_start = True
        while current_path is not None:
            read_load = await self._read_overlay(current_path, record_status=False)
            if isinstance(read_load, LoadProjectTemplateResultFailure):
                return None
            _, overlay = read_load

            if not is_start:
                node_value = probe(current_path, overlay)
                if node_value is not None:
                    return node_value
            is_start = False

            parent_id = self._reduce_parent_link_to_id(overlay, current_path, file_path_to_id)
            if parent_id is None:
                return None
            if parent_id in visited:
                return None
            visited.add(parent_id)

            current_path = self._resolve_parent_id_to_path(parent_id, id_index)
        return None

    def decide_workspace(
        self,
        project_file_path: Path,
        project_config: dict,
        env_config: dict,
        template_workspace_dir: str | None = None,
    ) -> WorkspaceDecision:
        """Decide a project's workspace dir and override bit read-only, mutating nothing.

        Returns the directory whose griptape_nodes_config.json activation loads as the
        workspace layer, plus whether activation pins it via set_workspace_override.
        Priority, highest first (matching _activate_project's block):

        0. the project template's own workspace_dir field (passed in already resolved to an
           absolute path via _resolve_template_workspace_dir) -> (template dir, apply_override=True)
        1. project_workspaces user-config override, keyed by project ID or path ->
           (override dir, apply_override=True)
        2. workspace_directory from env vars -> (env dir, apply_override=False)
        3. workspace_directory from the project-adjacent config -> (project dir, apply_override=False)
        4. the nearest ancestor's resolved workspace, walking the explicit parent-project chain ->
           (ancestor workspace, apply_override=True)
        5. the global configured workspace_directory, else the project's own directory (auto-default)
           -> (configured root or project dir, apply_override=True)

        `template_workspace_dir` is the highest-priority source: a project that declares its own
        workspace_dir beats the per-user project_workspaces mapping and the env var. The caller
        resolves the (possibly per-platform, possibly relative) raw field to an absolute path before
        passing it, so this method and the offline resolver share the branch verbatim.

        Branch 4 walks the project's explicit parent chain (parent_project_id / legacy
        parent_project_path, resolved through the registry) and inherits the first ancestor that
        resolves a workspace via its own override mapping or project-adjacent config. This makes a
        derived project with no workspace of its own inherit its parent's workspace instead of
        treating its own subdir as a fresh workspace (which would resolve libraries_directory to an
        empty libraries/ tree and wrongly prompt to reinstall already downloaded libraries). It only
        fires when no explicit workspace was named above, so a project-adjacent workspace_directory
        (branch 3) still wins and a sidecar config remains a full opt-out at every level of the
        chain. See _inherit_workspace_from_parents.

        Branch 5's global default is unconditional: when the chain is exhausted with no ancestor
        workspace, the configured workspace_directory is used regardless of where the project file
        sits on disk, so an imported standalone project with no ancestor workspace adopts the global
        workspace. The final own-directory fallback only fires when workspace_directory is unset in
        both config layers; in a real engine the Settings default always populates default_config, so
        this is a defensive path exercised only by tests that mock both layers to None.

        `apply_override` is True only for the override-mapping, parent-inheritance, and global-default
        branches, because those are the cases where activation calls set_workspace_override. For env
        or project-adjacent workspace_directory, activation leaves the override unset so the
        workspace config layer can re-point the final workspace_path; a forced override
        would mask that. Both _activate_project (live) and the provisioning preview drive
        off this one decision, so the previewed library/engine_version plan and what
        _reconcile_libraries_from_config actually does cannot drift.

        Branches 1-3 and 4-result/5 are factored into _decide_workspace_pre_inheritance and
        _decide_workspace_post_inheritance so resolve_workspace_dir_for_project_id (which resolves an
        unloaded project) shares them verbatim, differing only in how `inherited` is produced (the
        live registry walk here vs. an offline disk walk there).
        """
        pre_inheritance = self._decide_workspace_pre_inheritance(
            project_file_path, project_config, env_config, template_workspace_dir
        )
        if pre_inheritance is not None:
            return pre_inheritance

        inherited = self._inherit_workspace_from_parents(project_file_path)
        return self._decide_workspace_post_inheritance(project_file_path, inherited)

    def _resolve_template_workspace_dir(
        self, raw: str | PerPlatformProjectPath | None, project_file_path: Path
    ) -> str | None:
        """Resolve a template's raw workspace_dir field to an absolute path string, or None.

        Reduces a per-platform mapping to the active platform's value, resolves a relative path
        against the project YAML's directory, and canonicalizes the result. Mirrors how
        parent_project_path is resolved (_resolve_parent_chain), so the two fields treat relative
        paths the same way. Returns None when the field is unset or has no value for the active
        platform.
        """
        selected = select_project_path(raw)
        if selected is None:
            return None
        candidate = Path(selected)
        if not candidate.is_absolute():
            candidate = project_file_path.parent / candidate
        return str(canonicalize_for_identity(candidate))

    def _decide_workspace_pre_inheritance(
        self,
        project_file_path: Path,
        project_config: dict,
        env_config: dict,
        template_workspace_dir: str | None = None,
    ) -> WorkspaceDecision | None:
        """Branches 0-3 of decide_workspace: the explicit, non-inherited workspace sources.

        Returns a decision for the template's own workspace_dir (branch 0, pinned, highest
        priority), the project_workspaces override (branch 1, pinned), an env workspace_directory
        (branch 2, unpinned), or a project-adjacent workspace_directory (branch 3, unpinned), in
        that priority. Returns None when none is set, leaving the parent-inheritance and
        global-default tail to _decide_workspace_post_inheritance. Shared verbatim by
        decide_workspace (live) and resolve_workspace_dir_for_project_id (offline) so the two cannot
        drift on these branches; only the source of template_workspace_dir differs (loaded template
        vs. disk overlay).
        """
        if template_workspace_dir is not None:
            return WorkspaceDecision(Path(template_workspace_dir), apply_override=True)

        project_workspaces = self._config_manager.get_config_value(
            "project_workspaces",
            config_source="user_config",
            default={},
        )
        workspace_override = self._find_workspace_override(project_file_path, project_workspaces)
        if workspace_override is not None:
            return WorkspaceDecision(Path(workspace_override), apply_override=True)

        env_workspace = env_config.get("workspace_directory")
        if env_workspace is not None:
            return WorkspaceDecision(Path(env_workspace), apply_override=False)

        project_workspace = project_config.get("workspace_directory")
        if project_workspace is not None:
            return WorkspaceDecision(Path(project_workspace), apply_override=False)

        return None

    def _decide_workspace_post_inheritance(self, project_file_path: Path, inherited: str | None) -> WorkspaceDecision:
        """Branches 4-result and 5 of decide_workspace: parent-inheritance result, then global default.

        `inherited` is the workspace a parent resolved (branch 4), or None when the chain defined
        none. A non-None value is pinned. Otherwise falls to the global configured workspace_directory
        (user config, then default config), and finally the project's own directory (branch 5b, a
        defensive path reached only when workspace_directory is unset in both layers). All of these
        pin via apply_override=True. Shared verbatim by decide_workspace and
        resolve_workspace_dir_for_project_id; only the source of `inherited` (registry vs. disk walk)
        differs between the two callers.
        """
        if inherited is not None:
            return WorkspaceDecision(Path(inherited), apply_override=True)

        configured_root = self._config_manager.get_config_value(
            "workspace_directory",
            config_source="user_config",
            default=None,
        )
        if configured_root is None:
            configured_root = self._config_manager.get_config_value(
                "workspace_directory",
                config_source="default_config",
                default=None,
            )
        if configured_root is not None:
            return WorkspaceDecision(Path(configured_root), apply_override=True)

        return WorkspaceDecision(project_file_path.parent, apply_override=True)

    def _find_workspace_override(self, project_file_path: Path, project_workspaces: dict[str, str]) -> str | None:
        """Return the user-configured workspace override for a project, or None if not mapped.

        A project_workspaces key may be either an opaque project ID or a project
        file path. Each key is resolved to a canonical project path and compared
        against the target's canonical path. See _resolve_workspace_key for the
        ID-then-path resolution order.
        """
        resolved_project_path = str(canonicalize_for_identity(project_file_path))
        return next(
            (v for k, v in project_workspaces.items() if self._resolve_workspace_key(k) == resolved_project_path),
            None,
        )

    def _resolve_workspace_key(self, key: str) -> str:
        """Canonical project path for a project_workspaces key (a project ID or a path).

        Tries the key as a loaded project's ID first: when a loaded project carries
        that id (and has a backing file), its file path is the resolved path. When no
        loaded project matches the id (or it has no backing file), the key is treated
        as a file path instead. IDs are looked up verbatim, never canonicalized, the
        same way the registry is keyed.
        """
        info = self._successfully_loaded_project_templates.get(key)
        if info is not None and info.project_file_path is not None:
            return str(canonicalize_for_identity(info.project_file_path))
        return str(canonicalize_for_identity(key))

    def _inherit_workspace_from_parents(self, project_file_path: Path) -> str | None:
        """Walk the explicit parent-project chain for the nearest ancestor's workspace.

        Returns the workspace_directory the nearest ancestor would resolve to (its
        project_workspaces override, else its adjacent griptape_nodes_config.json),
        or None when no ancestor in the chain defines one. The starting project's OWN
        explicit sources are handled by the earlier branches of decide_workspace, so the
        walk begins at the parent. The chain traversal and cycle guard live in the shared
        _nearest_ancestor_value_live; only the per-node probe (an ancestor's explicit
        workspace) is supplied here.
        """
        project_workspaces = self._config_manager.get_config_value(
            "project_workspaces",
            config_source="user_config",
            default={},
        )

        def probe(info: ProjectInfo) -> str | None:
            if info.project_file_path is None:
                return None
            return self._resolve_node_explicit_workspace(info.project_file_path, project_workspaces)

        return self._nearest_ancestor_value_live(project_file_path, probe)

    def decide_libraries_root(self, project_file_path: Path, template_libraries_dir: str | None) -> Path | None:
        """Decide where a project's libraries install/resolve, or None for the legacy default.

        Priority, highest first:

        0. the project's OWN libraries_dir field (passed in already resolved to an absolute path via
           _resolve_template_libraries_dir) -> that dir
        1. the nearest ancestor with a libraries_dir, walking the explicit parent-project chain,
           resolved against THAT ancestor's project dir -> the ancestor's dir
        2. None -> no explicit libraries root; the caller (ConfigManager.resolved_libraries_root)
           falls back to the workspace-relative libraries_directory, preserving legacy behavior.

        Unlike decide_workspace, this consults ONLY the project-template libraries_dir field (no
        project_workspaces mapping, no adjacent config, no env): library sharing is a portable,
        version-controlled, template-side concept. Branch 1 resolving the inherited value against the
        ancestor's own dir is what makes every child point at the same parent libraries/ tree, so a
        library declared on the parent is downloaded once and reused via SKIP. See
        _inherit_libraries_dir_from_parents.
        """
        if template_libraries_dir is not None:
            return Path(template_libraries_dir)
        inherited = self._inherit_libraries_dir_from_parents(project_file_path)
        if inherited is not None:
            return Path(inherited)
        return None

    def _resolve_template_libraries_dir(
        self, raw: str | PerPlatformProjectPath | None, project_file_path: Path
    ) -> str | None:
        """Resolve a template's raw libraries_dir field to an absolute path string, or None.

        Mirrors _resolve_template_workspace_dir: reduces a per-platform mapping to the active
        platform's value, resolves a relative path against the project YAML's directory, and
        canonicalizes the result. Returns None when the field is unset or has no value for the active
        platform. This doubles as the per-node leaf primitive for the parent-chain walks, so an
        inherited value resolves against the DECLARING node's directory.
        """
        selected = select_project_path(raw)
        if selected is None:
            return None
        candidate = Path(selected)
        if not candidate.is_absolute():
            candidate = project_file_path.parent / candidate
        return str(canonicalize_for_identity(candidate))

    def _inherit_libraries_dir_from_parents(self, project_file_path: Path) -> str | None:
        """Walk the explicit parent-project chain for the nearest ancestor's libraries_dir.

        Returns the resolved absolute libraries_dir the nearest ancestor declares (against that
        ancestor's own project dir), or None when no ancestor in the chain defines one. The starting
        project's OWN libraries_dir is handled by branch 0 of decide_libraries_root, so the walk
        begins at the parent. The chain traversal and cycle guard live in the shared
        _nearest_ancestor_value_live; only the per-node probe (an ancestor's template libraries_dir)
        is supplied here.
        """

        def probe(info: ProjectInfo) -> str | None:
            if info.project_file_path is None:
                return None
            return self._resolve_template_libraries_dir(info.template.libraries_dir, info.project_file_path)

        return self._nearest_ancestor_value_live(project_file_path, probe)

    def _nearest_ancestor_value_live(
        self, project_file_path: Path, probe: Callable[[ProjectInfo], str | None]
    ) -> str | None:
        """Walk the explicit parent chain (loaded registry) and return the first probe hit, or None.

        Shared skeleton for the live workspace and libraries inheritance walks. Traverses in id-space
        through _successfully_loaded_project_templates (no disk loads): find the start project's id,
        then hop parent to parent via _reduce_parent_link_to_id, guarding against a cyclic chain with
        a visited id-set. The walk begins at the parent (the starting project's own value is handled
        by the caller's branch 0 / earlier decide_workspace branches). `probe` is applied to each
        ancestor ProjectInfo; the first non-None result wins.
        """
        file_path_to_id: dict[Path, ProjectID] = {
            info.project_file_path: pid
            for pid, info in self._successfully_loaded_project_templates.items()
            if info.project_file_path is not None
        }

        resolved_start_path = canonicalize_for_identity(project_file_path)
        start_id = next(
            (
                pid
                for pid, info in self._successfully_loaded_project_templates.items()
                if info.project_file_path is not None
                and canonicalize_for_identity(info.project_file_path) == resolved_start_path
            ),
            None,
        )
        if start_id is None:
            return None

        visited: set[ProjectID] = {start_id}
        current_info = self._successfully_loaded_project_templates.get(start_id)
        while current_info is not None:
            parent_id = self._reduce_parent_link_to_id(
                current_info.template,
                current_info.project_file_path,
                file_path_to_id,
            )
            if parent_id is None:
                return None
            if parent_id in visited:
                return None
            visited.add(parent_id)
            parent_info = self._successfully_loaded_project_templates.get(parent_id)
            if parent_info is None:
                return None
            node_value = probe(parent_info)
            if node_value is not None:
                return node_value
            current_info = parent_info
        return None

    def _resolve_node_explicit_workspace(
        self, project_file_path: Path, project_workspaces: dict[str, str]
    ) -> str | None:
        """Resolve a single chain node's explicitly-named workspace, or None if it names none.

        Checks the node's project_workspaces override first, then its adjacent
        griptape_nodes_config.json's workspace_directory. This is the per-node leaf primitive
        applied at each ancestor by both the live (_inherit_workspace_from_parents) and offline
        (_inherit_workspace_from_parents_offline) parent walks, so the two stay in lockstep.
        """
        override = self._find_workspace_override(project_file_path, project_workspaces)
        if override is not None:
            return override
        node_config = self._config_manager.read_config_file(project_file_path.parent / "griptape_nodes_config.json")
        return node_config.get("workspace_directory")

    def _snapshot_library_config(self) -> str:
        """Return a stable string of the merged library-affecting config for change detection.

        Captures the merged `libraries_to_register`, `libraries_to_download`, and
        `requires_engine` values plus the RESOLVED libraries directory as one
        sorted-key JSON string so two snapshots can be compared with `==`.
        Including `requires_engine` ensures a pure requires_engine change still trips
        `library_config_changed`, which is what re-runs the reload (and so the
        engine_version gate) on activation. Including the resolved libraries dir
        catches a workspace-only switch: `libraries_directory` is workspace-relative
        by default, so two projects with identical config strings but different
        workspaces resolve to different on-disk `libraries/` trees and must still
        reload, even though the three values above are unchanged. The resolved dir
        also reflects a project's own/inherited `libraries_dir` override, so a switch
        between a sharing child and a non-sharing project trips the reload too.
        """
        resolved_libraries_dir = str(self._config_manager.resolved_libraries_root())
        snapshot = {
            LIBRARIES_TO_REGISTER_KEY: self._config_manager.get_config_value(LIBRARIES_TO_REGISTER_KEY, default=[]),
            LIBRARIES_TO_DOWNLOAD_KEY: self._config_manager.get_config_value(LIBRARIES_TO_DOWNLOAD_KEY, default=[]),
            REQUIRES_ENGINE_KEY: self._config_manager.get_config_value(REQUIRES_ENGINE_KEY, default=None),
            "resolved_libraries_directory": resolved_libraries_dir,
        }
        return json.dumps(snapshot, sort_keys=True, default=str)

    async def _reload_after_project_switch(
        self, project_id: str, *, workspace_changed: bool, library_config_changed: bool
    ) -> SetCurrentProjectResultFailure | None:
        """Reload libraries and optionally re-register workflows after a project switch.

        Only reloads libraries when the project's library-affecting config
        actually changed: the reload triggers LibraryManager's reconcile, which
        provisions sourced libraries and enforces the engine_version gate. A
        switch that leaves library config untouched (e.g. default project to
        default workspace) skips the deep reset. Workflows are re-registered only
        when the workspace directory changed.

        Returns a failure result if the library reload (reconcile/engine_version
        gate included) fails, otherwise None.
        """
        if library_config_changed:
            reload_result = await GriptapeNodes.ahandle_request(ReloadAllLibrariesRequest())
            if isinstance(reload_result, ReloadAllLibrariesResultFailure):
                return SetCurrentProjectResultFailure(
                    result_details=f"Attempted to set project '{project_id}'. "
                    f"Config updated but library reload failed: {reload_result.result_details}",
                )
        if workspace_changed:
            await GriptapeNodes.WorkflowManager().refresh_workflow_registry()
        return None

    def _project_checkpoint_attributes(self, project_id: ProjectID, *, name: str | None = None) -> dict[str, Any]:
        """The facts a hook may gate project load/activation on: id and (best-effort) name.

        `name` is the resolved template name when the caller already holds it (load
        time, before the project is cached); at activation it falls back to the
        cached template so the load and activation gates resolve the same facts.
        """
        attributes: dict[str, Any] = {CheckpointAttribute.ID: project_id}
        resolved_name = name if name is not None else self._cached_project_name(project_id)
        if resolved_name:
            attributes[CheckpointAttribute.NAME] = str(resolved_name)
        return attributes

    def _cached_project_name(self, project_id: ProjectID) -> str | None:
        info = self._successfully_loaded_project_templates.get(project_id)
        return getattr(getattr(info, "template", None), "name", None)

    async def on_set_current_project_request(
        self, request: SetCurrentProjectRequest
    ) -> SetCurrentProjectResultSuccess | SetCurrentProjectResultFailure:
        """Set which project user has selected.

        Establishes the target project's config/workspace/env layers and reloads
        libraries. When the reload fails (e.g. an engine_version mismatch or a
        failed provisioning), the previously active project is re-established so
        the engine is never left adopting a broken project: the user can keep
        working in the project they had. Re-establishment runs only after startup
        (interactive switches); during boot the failure is returned as-is.
        """
        # Remember the project that was active before this switch so a failed
        # activation can roll back to it. SYSTEM_DEFAULTS_KEY is a valid target.
        previous_project_id = self._current_project_id

        # `None` is the wire-level "no project specified" signal -- normalize to
        # SYSTEM_DEFAULTS_KEY so the engine lands on system defaults instead of
        # a phantom "no project" state. Any other value is an opaque project id
        # and is the registry key verbatim: do NOT canonicalize it. Canonicalizing
        # would treat a GUID (or custom string) as a relative path against the CWD
        # and miss the registry. Legacy projects whose id is a canonical path
        # string were already canonicalized at load time, so a verbatim lookup
        # still hits. SYSTEM_DEFAULTS_KEY is a synthetic id and is preserved as-is.
        resolved_project_id: ProjectID = request.project_id if request.project_id is not None else SYSTEM_DEFAULTS_KEY

        # License-policy checkpoint: gate activating a user project on its id. The
        # system-defaults rest state is always allowed -- it is the fallback a
        # failed activation rolls back to. A denial rejects the switch with the
        # missing permissions and leaves the current project untouched (the
        # activation below never runs).
        if resolved_project_id != SYSTEM_DEFAULTS_KEY:
            denial = GriptapeNodes.EventManager().evaluate_authorization_checkpoint(
                AuthorizationCheckpoint(
                    action=CheckpointAction.ACTIVATE_PROJECT,
                    subject_type=CheckpointSubjectType.PROJECT,
                    subject_id=resolved_project_id,
                    attributes=self._project_checkpoint_attributes(resolved_project_id),
                )
            )
            if denial is not None:
                reason = denial.reason()
                return SetCurrentProjectResultFailure(
                    result_details=f"Attempted to set current project '{resolved_project_id}'. Failed because: {reason}"
                )

        outcome = await self._activate_project(resolved_project_id)
        if outcome.failure is not None:
            # During boot, leave the failure to the caller (soft handling); there is
            # no prior interactive project to fall back to. After startup, restore the
            # previously active project so the engine stays in a working state, then
            # surface the original failure to the GUI.
            if self._initialization_complete and previous_project_id != resolved_project_id:
                rollback = await self._activate_project(previous_project_id)
                if rollback.failure is not None:
                    logger.error(
                        "Attempted to roll back to previous project '%s' after activation of '%s' failed. "
                        "Rollback also failed: %s",
                        previous_project_id,
                        resolved_project_id,
                        rollback.failure.result_details,
                    )
            return outcome.failure

        result = SetCurrentProjectResultSuccess(
            result_details=f"Successfully set current project. ID: {resolved_project_id}",
        )
        if outcome.workspace_changed and self._initialization_complete:
            result.altered_workflow_state = True

        # Push the switch to running workers so they adopt the orchestrator's project
        # even on a shallow switch (same workspace + library config) that would not
        # restart them. Boot is handled separately (a worker boots like an engine and
        # re-derives the same project), so emit only post-init and only when the
        # project actually changed. A worker that boots like the orchestrator has the
        # same registry, so the id resolves there too.
        if self._initialization_complete and previous_project_id != resolved_project_id:
            self._event_manager.broadcast_app_event(CurrentProjectChanged(project_id=resolved_project_id))
        return result

    async def _activate_project(self, resolved_project_id: ProjectID) -> _ProjectActivationOutcome:
        """Establish a project's config/workspace/env layers and reload libraries.

        Captures workspace path before and after config layer changes. If the
        workspace actually changed and startup is complete, performs an expensive
        workspace switch: reloads all libraries and re-registers workflows.
        During startup, LibraryManager handles library loading concurrently, so
        the workspace switch is skipped.

        `resolved_project_id` is already canonicalized. Returns the reload failure
        (None on success) and whether the workspace changed. This is the shared
        body used both for the requested switch and for rolling back to the
        previously active project when a switch fails.
        """
        # Restore os.environ entries mutated by the outgoing project before any config
        # layer changes. Workspace resolution below may consult env vars, so the old
        # project's values must not leak into the new project's workspace decision.
        self._restore_project_env()

        # Capture workspace and library-affecting config BEFORE config changes for comparison after
        old_workspace = self._config_manager.workspace_path
        old_library_config = self._snapshot_library_config()

        self._current_project_id = resolved_project_id

        # Each activation re-decides its config layers from scratch. Drop the prior
        # project's per-activation state (workspace override + project-adjacent and
        # workspace config-file paths) so none of it leaks into the new project. Without
        # this, a rollback to a project whose config supplies workspace_directory keeps
        # the failed project's override, and switching to system defaults re-merges the
        # prior project's griptape_nodes_config.json (re-applying its pins). The branches
        # below remerge via load_project_config()/load_workspace_config()/load_configs().
        self._config_manager.clear_project_layers()

        project_info = self._successfully_loaded_project_templates.get(resolved_project_id)
        if project_info is not None and project_info.project_file_path is not None:
            project_file_path = project_info.project_file_path
            project_dir = project_file_path.parent
            self._config_manager.load_project_config(project_dir)

            # Decide the workspace dir + override bit once (shared with the provisioning
            # preview via decide_workspace, so the two cannot drift). apply_override is
            # True for the project_workspaces mapping, parent-chain inheritance, and
            # global-default branches; for an env/project-adjacent workspace_directory it is
            # False, so the override stays unset and the workspace config layer can re-point
            # workspace_path.
            template_workspace_dir = self._resolve_template_workspace_dir(
                project_info.template.workspace_dir, project_file_path
            )
            decision = self.decide_workspace(
                project_file_path,
                self._config_manager.project_config,
                self._config_manager.env_config,
                template_workspace_dir=template_workspace_dir,
            )
            if decision.apply_override:
                self._config_manager.set_workspace_override(decision.workspace_dir)

            # Decide the libraries root independently of the workspace (a project may point
            # workspace_dir away from its own dir yet still share a parent's libraries). None
            # means no explicit libraries_dir anywhere in the chain, so the override stays
            # cleared and resolved_libraries_root() falls back to the workspace-relative
            # default. Set before the post-change snapshot below so a sharing-vs-not switch
            # is reflected in library_config_changed.
            template_libraries_dir = self._resolve_template_libraries_dir(
                project_info.template.libraries_dir, project_file_path
            )
            libraries_root = self.decide_libraries_root(project_file_path, template_libraries_dir)
            self._config_manager.set_libraries_root_override(libraries_root)

            # Load workspace config layer from the resolved workspace directory.
            self._config_manager.load_workspace_config(self._config_manager.workspace_path)
        elif project_info is not None and project_info.project_file_path is None:
            # Switching to system defaults: clear_project_layers() above already dropped
            # the prior project's override and config-file paths, so reloading configs now
            # resolves workspace_path and all config layers from defaults only.
            self._config_manager.load_configs()
        else:
            # Unknown project id (no loaded template): clear_project_layers() above already
            # dropped the prior project's layers, so remerge from defaults rather than leave
            # config in the cleared, unmerged state.
            self._config_manager.load_configs()

        # Apply the new project's environment variables to os.environ. Happens after
        # workspace resolution (so it doesn't affect workspace lookup -- the outgoing
        # project's entries were already restored above) and before library reload
        # (so nodes imported during reload observe the new values).
        new_project_info = self._successfully_loaded_project_templates.get(resolved_project_id)
        if new_project_info is not None:
            self._apply_project_env(new_project_info)

        new_workspace = self._config_manager.workspace_path
        workspace_changed = old_workspace != new_workspace
        new_library_config = self._snapshot_library_config()
        library_config_changed = old_library_config != new_library_config

        if self._initialization_complete:
            # The orchestrator owns project_file in the shared config. A worker adopting
            # the orchestrator's project must not write it back: both processes share the
            # on-disk config, so a worker write races the orchestrator's. The worker still
            # re-establishes its in-memory layers above; it just skips the persist.
            if not GriptapeNodes.LibraryManager().is_worker:
                # Persist the active project so the next engine restart restores it via
                # _resolve_project_file_path(). A file-backed project persists its path.
                # System defaults persists the SYSTEM_DEFAULTS_KEY sentinel so that a
                # deliberate "stay on system defaults" choice is honored on the next
                # restart (and by a freshly spawned worker, which boots like an engine):
                # the sentinel suppresses workspace discovery instead of re-adopting a
                # workspace griptape-nodes-project.yml.
                persisted_info = self._successfully_loaded_project_templates.get(resolved_project_id)
                if persisted_info is not None and persisted_info.project_file_path is not None:
                    persisted_project_file = str(persisted_info.project_file_path)
                else:
                    persisted_project_file = SYSTEM_DEFAULTS_KEY
                try:
                    self._config_manager.set_config_value("project_file", persisted_project_file)
                except Exception:
                    logger.warning("Failed to persist project_file '%s' to config", persisted_project_file)

            failure = await self._reload_after_project_switch(
                resolved_project_id,
                workspace_changed=workspace_changed,
                library_config_changed=library_config_changed,
            )
            if failure is not None:
                return _ProjectActivationOutcome(failure=failure, workspace_changed=workspace_changed)

        return _ProjectActivationOutcome(failure=None, workspace_changed=workspace_changed)

    async def ensure_project_loaded(self, project_id: ProjectID) -> bool:
        """Ensure a project id is present in the in-memory registry, re-deriving if absent.

        A worker boots like an engine and freezes its project registry at boot
        (`_load_registered_projects` / `_load_workspace_project` run only from
        `on_app_initialization_complete`). When the orchestrator switches to a project it
        registered AFTER this worker spawned, the worker's registry lacks that id. This
        re-reads the shared on-disk config and re-runs registered-project discovery (the
        same derivation boot uses) so the worker learns projects registered after spawn.

        Returns True if the id is present (already, or after re-derivation), False
        otherwise. SYSTEM_DEFAULTS_KEY is loaded at boot and so is always present.
        """
        if project_id in self._successfully_loaded_project_templates:
            return True
        self._config_manager.load_configs()
        await self._load_registered_projects()
        return project_id in self._successfully_loaded_project_templates

    def on_get_current_project_request(
        self, _request: GetCurrentProjectRequest
    ) -> GetCurrentProjectResultSuccess | GetCurrentProjectResultFailure:
        """Get currently selected project with template info."""
        project_info = self._successfully_loaded_project_templates.get(self._current_project_id)
        if project_info is None:
            return GetCurrentProjectResultFailure(
                result_details=f"Attempted to get current project. Failed because project not found for ID: '{self._current_project_id}'"
            )

        return GetCurrentProjectResultSuccess(
            project_info=project_info,
            result_details=f"Successfully retrieved current project. ID: {self._current_project_id}",
        )

    def on_save_project_template_request(  # noqa: C901, PLR0911
        self, request: SaveProjectTemplateRequest
    ) -> SaveProjectTemplateResultSuccess | SaveProjectTemplateResultFailure:
        """Save user customizations to project.yml.

        Flow:
        1. Validate template_data as a ProjectTemplate model
        2. Serialize to YAML using ProjectTemplate.to_overlay_yaml()
        3. Write to disk via File.write_text
        4. Invalidate cache (force reload on next access)
        """
        # Canonical file path: the identity locator for cache keys below and the
        # legacy bridge id for an id-less save. The write itself uses
        # request.project_path directly (the OS boundary canonicalizes it).
        canonical_path = canonicalize_for_identity(request.project_path)

        # Step 1: Validate and parse template_data
        try:
            template = ProjectTemplate.model_validate(request.template_data)
        except ValidationError as e:
            return SaveProjectTemplateResultFailure(
                result_details=f"Attempted to save project template to '{request.project_path}'. Failed because template data is invalid: {e}",
            )

        # A legacy (id-less) file being saved gets its derived path-string id
        # written explicitly, so the file becomes id'd on disk. This is the only
        # place a derived id is persisted, and only on an explicit Save.
        if template.id is None:
            template.id = str(canonical_path)

        # Step 2: Choose the diff base. When the child declares a parent, the overlay
        # must diff against the parent's fully-merged template so values inherited
        # from the parent don't redundantly appear in the child's YAML. The parent
        # must already be in the registry; if not, fail loudly rather than silently
        # diffing against system defaults (which would emit inherited values into
        # the child's overlay).
        #
        # Precedence mirrors load: an explicit parent_project_id (portable) wins
        # and is looked up directly in the registry; otherwise the legacy
        # parent_project_path is resolved by filesystem path. Per-platform path
        # mappings are reduced to the active platform's value first; a mapping
        # with no matching key and no `default` falls back to system defaults
        # (no parent on this OS).
        base_template: ProjectTemplate = default_template_for_version(template.project_template_schema_version)
        if template.parent_project_id is not None:
            parent_info = self._successfully_loaded_project_templates.get(template.parent_project_id)
            if parent_info is None:
                return SaveProjectTemplateResultFailure(
                    result_details=(
                        f"Attempted to save project template to '{request.project_path}'. "
                        f"Failed because parent project id '{template.parent_project_id}' is not loaded. "
                        f"Load the parent before saving the child."
                    ),
                )
            base_template = parent_info.template
        else:
            selected_parent = select_project_path(template.parent_project_path)
            if selected_parent is not None:
                parent_id = self._resolve_parent_path_for_lookup(
                    selected_parent,
                    anchor=request.project_path,
                )
                if parent_id is None:
                    return SaveProjectTemplateResultFailure(
                        result_details=(
                            f"Attempted to save project template to '{request.project_path}'. "
                            f"Failed because parent_project_path '{selected_parent}' "
                            f"is relative and no anchor could be resolved."
                        ),
                    )
                parent_info = self._successfully_loaded_project_templates.get(parent_id)
                if parent_info is None:
                    return SaveProjectTemplateResultFailure(
                        result_details=(
                            f"Attempted to save project template to '{request.project_path}'. "
                            f"Failed because parent project '{selected_parent}' "
                            f"(resolved to '{parent_id}') is not loaded. Load the parent before saving the child."
                        ),
                    )
                base_template = parent_info.template

        # Step 3: Serialize to YAML
        try:
            yaml_content = template.to_overlay_yaml(base_template)
        except Exception as e:
            return SaveProjectTemplateResultFailure(
                result_details=f"Attempted to save project template to '{request.project_path}'. Failed because YAML serialization failed: {e}",
            )

        # Step 3: Write to disk
        try:
            File(str(request.project_path)).write_text(yaml_content)
        except FileWriteError as e:
            return SaveProjectTemplateResultFailure(
                result_details=f"Attempted to save project template to '{request.project_path}'. Failed because file write failed: {e}",
            )

        # Step 4: Invalidate the cache so the next LoadProjectTemplateRequest reads
        # from disk. The registry is id-keyed, so locate the loaded entry by its
        # file path (a path string is not its id) and pop that id; the status map
        # is path-keyed, so pop it by the canonical path.
        for loaded_id, loaded_info in list(self._successfully_loaded_project_templates.items()):
            if loaded_info.project_file_path == canonical_path:
                self._successfully_loaded_project_templates.pop(loaded_id, None)
        self._registered_template_status.pop(canonical_path, None)

        return SaveProjectTemplateResultSuccess(
            result_details=f"Successfully saved project template to '{request.project_path}'",
        )

    async def on_upgrade_project_schema_request(  # noqa: PLR0911
        self, request: UpgradeProjectSchemaRequest
    ) -> UpgradeProjectSchemaResultSuccess | UpgradeProjectSchemaResultFailure:
        """Electively upgrade a loaded project to the latest schema major and re-save.

        A within-major advance happens automatically on save; this performs the explicit,
        opt-in crossing of a major boundary so the project ADOPTS the new major's defaults.

        It re-reads the project's own on-disk OVERLAY (its explicit customizations only -- NOT
        the merged template, whose inherited fields were materialized to the old-major values at
        load), restamps that overlay to the latest version, and re-merges it onto the new-major
        base. Re-saving that merged template diffs it back against the same new-major base, so a
        field the user never overrode is omitted and falls through to the NEW default, while
        genuine user overrides survive. BREAKING: a project's effective workspace/library/file
        layout can change, which is exactly the point of crossing a major.

        Failure cases (evaluated first): not loaded, no backing file, already at/ahead of the
        latest major (or an unparsable version), the overlay can't be re-read, or the re-save
        fails. Only then is the upgrade performed.
        """
        project_info = self._successfully_loaded_project_templates.get(request.project_id)
        if project_info is None:
            return UpgradeProjectSchemaResultFailure(
                result_details=(
                    f"Attempted to upgrade project '{request.project_id}'. Failed because it is not loaded."
                ),
            )
        if project_info.project_file_path is None:
            return UpgradeProjectSchemaResultFailure(
                result_details=(
                    f"Attempted to upgrade project '{request.project_id}'. "
                    f"Failed because it has no backing file (e.g. system defaults)."
                ),
            )

        previous_version = project_info.template.project_template_schema_version
        latest_version = ProjectTemplate.LATEST_SCHEMA_VERSION
        # Only upgrade STRICTLY older majors. A project already at -- or somehow ahead of (a
        # future major opened on an older engine, which the load path accepts forward-compat)
        # -- the latest major must not be touched: restamping it down to latest would be a
        # silent schema DOWNGRADE re-saved against an older baseline, contradicting the
        # never-downgrade contract in _version_to_write. schema_major_or_none keeps this from
        # raising on a malformed version (the load path tolerates one, so this must too).
        previous_major = schema_major_or_none(previous_version)
        latest_major = schema_major_or_none(latest_version)
        if previous_major is None or latest_major is None or previous_major >= latest_major:
            return UpgradeProjectSchemaResultFailure(
                result_details=(
                    f"Attempted to upgrade project '{request.project_id}'. "
                    f"Failed because its schema version '{previous_version}' is not an older major "
                    f"than the latest '{latest_version}' (or is unparsable)."
                ),
            )

        # Re-read the project's OWN overlay (explicit fields only) so inherited values are NOT
        # carried over as old-major pins. Restamp it to the latest version, then re-merge onto
        # the base for that version + the project's parent chain (the same base the save path
        # re-diffs against), so un-overridden fields adopt the new-major defaults.
        project_file_path = project_info.project_file_path
        overlay_load = await self._read_overlay(project_file_path, record_status=False)
        if isinstance(overlay_load, LoadProjectTemplateResultFailure):
            return UpgradeProjectSchemaResultFailure(
                result_details=(
                    f"Attempted to upgrade project '{request.project_id}'. "
                    f"Failed because its project file could not be re-read: {overlay_load.result_details}"
                ),
            )
        _, overlay = overlay_load
        upgraded_overlay = overlay._replace(project_template_schema_version=latest_version)

        validation = ProjectValidationInfo(status=ProjectValidationStatus.GOOD)
        base_template = await self._resolve_parent_chain(
            upgraded_overlay, project_file_path, validation, visited={canonicalize_for_identity(project_file_path)}
        )
        if base_template is None or not validation.is_usable():
            return UpgradeProjectSchemaResultFailure(
                result_details=(
                    f"Attempted to upgrade project '{request.project_id}'. "
                    f"Failed because the upgraded template could not be resolved against the latest base."
                ),
            )
        upgraded_template = ProjectTemplate.merge(base_template, upgraded_overlay, validation)

        save_result = self.on_save_project_template_request(
            SaveProjectTemplateRequest(
                project_path=project_file_path,
                template_data=upgraded_template.model_dump(mode="json"),
            )
        )
        if isinstance(save_result, SaveProjectTemplateResultFailure):
            return UpgradeProjectSchemaResultFailure(
                result_details=(
                    f"Attempted to upgrade project '{request.project_id}'. "
                    f"Failed because the re-save failed: {save_result.result_details}"
                ),
            )

        return UpgradeProjectSchemaResultSuccess(
            project_id=request.project_id,
            previous_schema_version=previous_version,
            new_schema_version=latest_version,
            result_details=(
                f"Upgraded project '{request.project_id}' from schema '{previous_version}' "
                f"to '{latest_version}'. The project now adopts the new-major defaults; its "
                f"effective workspace/library/file layout may have changed."
            ),
        )

    def on_validate_project_template_request(
        self, request: ValidateProjectTemplateRequest
    ) -> ValidateProjectTemplateResultSuccess:
        """Dry-run validate a template dict.

        Runs the same validation the load path runs (pydantic model validation
        plus macro parsing for situations and directories), but does not touch
        disk or the template registry. Always returns Success; callers inspect
        `validation.status` to decide whether the template is usable.
        """
        validation = ProjectValidationInfo(status=ProjectValidationStatus.GOOD)

        try:
            template = ProjectTemplate.model_validate(request.template_data)
        except ValidationError as e:
            for error in e.errors():
                field_path = ".".join(str(loc) for loc in error["loc"])
                validation.add_error(field_path=field_path, message=error["msg"])
            return ValidateProjectTemplateResultSuccess(
                validation=validation,
                result_details=f"Template validation failed with {len(validation.problems)} problem(s)",
            )

        self._parse_situation_macros(template.situations, validation)
        self._parse_directory_macros(template.directories, validation)
        self._check_parent_chain_cycles(template, validation, request.project_id)

        if validation.status == ProjectValidationStatus.GOOD:
            details = "Template is valid"
        else:
            details = f"Template validation found {len(validation.problems)} problem(s) (status: {validation.status})"
        return ValidateProjectTemplateResultSuccess(validation=validation, result_details=details)

    def _check_parent_chain_cycles(
        self,
        template: ProjectTemplate,
        validation: ProjectValidationInfo,
        editing_project_id: str | None,
    ) -> None:
        """Walk the parent chain through the registry and report any cycle.

        Only consults `_successfully_loaded_project_templates` (no disk I/O), so
        it catches the common GUI scenario where the user picks a parent whose
        own ancestry transitively points back to itself. A parent that isn't
        registered yet is silently allowed; the load path catches truly missing
        parents and cycles.

        The walk is conducted in id-space: every parent link is reduced to the
        parent's project id before comparison, so an opaque GUID id and a legacy
        path-string id are compared consistently. `editing_project_id` (the id of
        the project being edited) seeds the visited set *verbatim* (not
        canonicalized -- an id is not a path) so a cycle that includes "myself"
        (the user picks a parent that points back at the project being edited)
        is detected.

        A `parent_project_id` link is already an id. A legacy `parent_project_path`
        is resolved to a canonical path -- relative paths against the *containing*
        project's file path, taken from the registry, not from the opaque id --
        and then mapped to the parent's registered id; an unregistered legacy
        parent uses its canonical path string as its id (the legacy bridge).
        """
        visited: set[str] = set()
        if editing_project_id is not None:
            visited.add(editing_project_id)

        # Reverse map so a legacy parent_project_path link resolves to the
        # parent's real (id-keyed) registry key rather than its path string.
        file_path_to_id: dict[Path, ProjectID] = {
            info.project_file_path: pid
            for pid, info in self._successfully_loaded_project_templates.items()
            if info.project_file_path is not None
        }

        # Anchor for resolving the first hop's relative legacy path is the
        # editing project's own file path from the registry. A brand-new project
        # being validated is not registered yet, so the anchor is None and a
        # relative parent_project_path simply can't be resolved here (load-time
        # detection still applies). The opaque id is never used as a path anchor.
        editing_info = (
            self._successfully_loaded_project_templates.get(editing_project_id)
            if editing_project_id is not None
            else None
        )
        current_template: ProjectTemplate | None = template
        current_anchor: Path | None = editing_info.project_file_path if editing_info is not None else None
        while current_template is not None:
            parent_id = self._reduce_parent_link_to_id(current_template, current_anchor, file_path_to_id)
            if parent_id is None:
                return
            if parent_id in visited:
                field_path = (
                    "parent_project_id" if current_template.parent_project_id is not None else "parent_project_path"
                )
                validation.add_error(
                    field_path=field_path,
                    message=f"Cycle detected in parent chain at '{parent_id}'",
                )
                return
            visited.add(parent_id)
            parent_info = self._successfully_loaded_project_templates.get(parent_id)
            if parent_info is None:
                return
            current_template = parent_info.template
            current_anchor = parent_info.project_file_path

    def _reduce_parent_link_to_id(
        self,
        template: ProjectTemplate | ProjectOverlayData,
        anchor: Path | None,
        file_path_to_id: dict[Path, ProjectID],
    ) -> str | None:
        """Reduce a template's parent link to the parent's project id.

        Accepts either a merged ProjectTemplate (live walk) or a raw ProjectOverlayData (offline
        walk); both expose the parent_project_id / parent_project_path fields this reads.

        `parent_project_id` wins and is returned verbatim. Otherwise the legacy
        `parent_project_path` is reduced to the active platform's value, resolved
        against `anchor` (canonicalized), and mapped to the parent's registered id;
        an unregistered legacy parent uses its canonical path string as its id
        (the legacy bridge). Returns None when there is no parent link, when a
        per-platform path has no entry for this OS, or when a relative path has no
        anchor to resolve against.
        """
        if template.parent_project_id is not None:
            return template.parent_project_id
        selected_parent = select_project_path(template.parent_project_path)
        if selected_parent is None:
            return None
        resolved_path = self._resolve_parent_path_for_lookup(selected_parent, anchor)
        if resolved_path is None:
            return None
        return file_path_to_id.get(Path(resolved_path), resolved_path)

    def _resolve_parent_path_for_lookup(self, raw_parent: str, anchor: Path | str | None) -> str | None:
        """Resolve a stored parent_project_path to a canonical registry key.

        Resolves relative paths against `anchor` (the containing template's
        file path). Returns None if the path is relative and no anchor was
        provided, since we can't form an absolute path without one. Macro
        tokens are rejected by the loader; this resolver only sees absolute
        or anchor-relative paths.

        `anchor` is coerced to `Path` defensively because request payloads
        deserialized over the wire arrive with `project_path` as a `str`.
        """
        parent_path = Path(raw_parent)
        if not parent_path.is_absolute():
            if anchor is None:
                return None
            parent_path = Path(anchor).parent / parent_path
        return str(canonicalize_for_identity(parent_path))

    def on_unregister_project_template_request(  # noqa: C901, PLR0912
        self, request: UnregisterProjectTemplateRequest
    ) -> UnregisterProjectTemplateResultSuccess | UnregisterProjectTemplateResultFailure:
        """Remove a registered project template from in-memory caches and persisted config.

        Flow:
        1. Verify the project_id is known
        2. Remove from _successfully_loaded_project_templates and _registered_template_status
        3. Remove from PROJECTS_TO_REGISTER_KEY in user config
        4. If this was the current project, clear the current project
        """
        project_id = request.project_id

        # Locate the project's file path (the locator) from its id. A loaded
        # project carries its path in ProjectInfo; a legacy / failed-load entry
        # is only tracked in the Path-keyed status map, where its id IS its path
        # string. Either way we resolve to the canonical file path so the
        # path-keyed status map and the path-list persistence can be cleaned up.
        loaded_info = self._successfully_loaded_project_templates.get(project_id)
        file_path: Path | None = None
        if loaded_info is not None:
            file_path = loaded_info.project_file_path
        elif Path(project_id) in self._registered_template_status:
            file_path = Path(project_id)

        if project_id not in self._successfully_loaded_project_templates and file_path is None:
            return UnregisterProjectTemplateResultFailure(
                result_details=f"Attempted to unregister project template '{project_id}'. Failed because it is not registered.",
            )

        # Remove from in-memory caches: the registry is id-keyed, the status map
        # is path-keyed.
        self._successfully_loaded_project_templates.pop(project_id, None)
        if file_path is not None:
            self._registered_template_status.pop(file_path, None)

        # Remove from persisted config so it is not reloaded on restart.
        # PROJECTS_TO_REGISTER_KEY stores file paths, so filter by canonical-path
        # equality rather than comparing against the (possibly non-path) id.
        if file_path is not None:
            try:
                registered: list[str | dict | PerPlatformProjectPath] = (
                    self._config_manager.get_config_value(PROJECTS_TO_REGISTER_KEY, default=[]) or []
                )
                updated: list[str | dict | PerPlatformProjectPath] = []
                for entry in registered:
                    if isinstance(entry, dict):
                        try:
                            selected = select_project_path(PerPlatformProjectPath.model_validate(entry))
                        except ValidationError:
                            updated.append(entry)
                            continue
                    else:
                        selected = select_project_path(entry)
                    if selected is not None and canonicalize_for_identity(selected) == file_path:
                        continue
                    updated.append(entry)
                self._config_manager.set_config_value(PROJECTS_TO_REGISTER_KEY, updated)
            except Exception:
                logger.warning("Failed to remove project path '%s' from persisted config", file_path)

        # If this was the active project, fall back to system defaults (in-memory
        # and persisted) so the next restart doesn't try to restore a project
        # that is no longer registered.
        if self._current_project_id == project_id:
            self._current_project_id = SYSTEM_DEFAULTS_KEY
            try:
                self._config_manager.set_config_value("project_file", None)
            except Exception:
                logger.warning("Failed to clear project_file from config after unregister")

        return UnregisterProjectTemplateResultSuccess(
            result_details=f"Successfully unregistered project template '{project_id}'",
        )

    def on_match_path_against_macro_request(
        self, request: AttemptMatchPathAgainstMacroRequest
    ) -> AttemptMatchPathAgainstMacroResultSuccess | AttemptMatchPathAgainstMacroResultFailure:
        """Attempt to match a path against a macro schema and extract variables.

        Flow:
        1. Check secrets manager is available (failure = true error)
        2. Call ParsedMacro.extract_variables() with path and known variables
        3. If match succeeds, return success with extracted_variables
        4. If match fails, return success with match_failure (not an error)
        """
        extracted = request.parsed_macro.extract_variables(
            request.file_path,
            request.known_variables,
            self._secrets_manager,
        )

        if extracted is None:
            # Pattern didn't match - this is a normal outcome, not an error
            return AttemptMatchPathAgainstMacroResultSuccess(
                extracted_variables=None,
                match_failure=MacroMatchFailure(
                    failure_reason=MacroMatchFailureReason.STATIC_TEXT_MISMATCH,
                    expected_pattern=request.parsed_macro.template,
                    known_variables_used=request.known_variables,
                    error_details=f"Path '{request.file_path}' does not match macro pattern",
                ),
                result_details=f"Attempted to match path '{request.file_path}' against macro '{request.parsed_macro.template}'. Pattern did not match",
            )

        # Pattern matched successfully
        return AttemptMatchPathAgainstMacroResultSuccess(
            extracted_variables=extracted,
            match_failure=None,
            result_details=f"Successfully matched path '{request.file_path}' against macro '{request.parsed_macro.template}'. Extracted {len(extracted)} variables",
        )

    def on_get_state_for_macro_request(  # noqa: C901
        self, request: GetStateForMacroRequest
    ) -> GetStateForMacroResultSuccess | GetStateForMacroResultFailure:
        """Analyze a macro and return comprehensive state information.

        Flow:
        1. Get current project via GetCurrentProjectRequest
        2. Get template from current project
        3. For each variable, determine if it's:
           - A directory (from template)
           - User-provided (from request)
           - A builtin
        4. Check for conflicts:
           - User providing directory name
           - User overriding builtin with different value
        5. Calculate what's satisfied vs missing
        6. Determine if resolution would succeed
        """
        current_project_request = GetCurrentProjectRequest()
        current_project_result = self.on_get_current_project_request(current_project_request)

        if not isinstance(current_project_result, GetCurrentProjectResultSuccess):
            return GetStateForMacroResultFailure(
                result_details="Attempted to analyze macro state. Failed because no current project is set or template not loaded",
            )

        project_info = current_project_result.project_info
        template = project_info.template

        all_variables = request.parsed_macro.get_variables()
        directory_names = set(template.directories.keys())
        user_provided_names = set(request.variables.keys())

        satisfied_variables: set[str] = set()
        missing_required_variables: set[str] = set()
        conflicting_variables: set[str] = set()

        for var_info in all_variables:
            var_name = var_info.name

            if var_name in directory_names:
                satisfied_variables.add(var_name)
                if var_name in user_provided_names:
                    conflicting_variables.add(var_name)

            if var_name in user_provided_names:
                satisfied_variables.add(var_name)

            if var_name in BUILTIN_VARIABLES:
                try:
                    builtin_value = self._get_builtin_variable_value(var_name, project_info)
                except (RuntimeError, NotImplementedError) as e:
                    if not var_info.is_required:
                        continue
                    return GetStateForMacroResultFailure(
                        result_details=f"Attempted to analyze macro state. Failed because builtin variable '{var_name}' cannot be resolved: {e}",
                    )

                satisfied_variables.add(var_name)
                if var_name in user_provided_names:
                    user_value = str(request.variables[var_name])
                    if user_value != builtin_value:
                        conflicting_variables.add(var_name)

            if var_info.is_required and var_name not in satisfied_variables:
                missing_required_variables.add(var_name)

        can_resolve = len(missing_required_variables) == 0 and len(conflicting_variables) == 0

        return GetStateForMacroResultSuccess(
            all_variables=all_variables,
            satisfied_variables=satisfied_variables,
            missing_required_variables=missing_required_variables,
            conflicting_variables=conflicting_variables,
            can_resolve=can_resolve,
            result_details=f"Analyzed macro with {len(all_variables)} variables: {len(satisfied_variables)} satisfied, {len(missing_required_variables)} missing, {len(conflicting_variables)} conflicting",
        )

    async def on_activate_workspace_project_request(
        self, _request: ActivateWorkspaceProjectRequest
    ) -> ActivateWorkspaceProjectResultSuccess | ActivateWorkspaceProjectResultFailure:
        """Resolve and activate the workspace project before initialization completes.

        Called by the app orchestrator after role setup but before the
        AppInitializationComplete broadcast, mirroring the CLI executor which loads
        its project file first. Establishing the project's config/workspace/env
        layers now means LibraryManager loads libraries against the correct
        workspace (enforcing the project's engine_version and library pins) when the
        init event fires, instead of against the default workspace.

        Runs before `_initialization_complete` is set, so the activation it triggers
        establishes config/workspace/env layers but skips the in-handler library
        reload (LibraryManager performs the correctly-scoped load on init). A boot
        with no workspace project is a no-op success; a project that resolves but
        fails to load or activate is a failure (the detail comes from
        `_load_workspace_project`). The engine_version gate is intentionally deferred to
        LibraryManager at boot (soft-log); this handler reports load/activation failure
        only, not gate failure.
        """
        workspace_project_path = self._resolve_project_file_path()
        if workspace_project_path is None:
            return ActivateWorkspaceProjectResultSuccess(
                result_details="No workspace project found; system defaults remain active",
            )

        failure_detail = await self._load_workspace_project()
        if failure_detail is not None:
            return ActivateWorkspaceProjectResultFailure(
                result_details=f"Attempted to activate workspace project at '{workspace_project_path}'. "
                f"Failed because {failure_detail}",
            )

        return ActivateWorkspaceProjectResultSuccess(
            result_details=f"Activated workspace project: {self._current_project_id}",
        )

    def on_export_project_request(
        self, request: ExportProjectRequest
    ) -> ExportProjectResultSuccess | ExportProjectResultFailure:
        """Package a loaded project and its dependencies into a portable .zip.

        Validates that the project is loaded and file-backed and that the
        destination's parent directory exists, then hands the project base dir and
        its adjacent griptape_nodes_config.json to package_project_to_zip. Secret
        VALUES never leave the machine: only required secret KEY names travel.

        Any loaded project may be exported, active or not. The library/asset
        content is read from the exported project's own files and is correct
        regardless. The required-secret-KEY list, however, is derived from the
        engine's merged global config and is most accurate when the exported
        project is the active one (see _collect_required_secret_keys).
        """
        project_info = self._successfully_loaded_project_templates.get(request.project_id)
        if project_info is None:
            return ExportProjectResultFailure(
                result_details=f"Attempted to export project '{request.project_id}'. Failed because it is not loaded.",
            )
        if project_info.project_file_path is None:
            return ExportProjectResultFailure(
                result_details=(
                    f"Attempted to export project '{request.project_id}'. "
                    f"Failed because it has no backing file (e.g. system defaults)."
                ),
            )

        # Coerce destination_path at the boundary. Unlike the preview/import
        # requests, cattrs does NOT coerce this field from its wire string: this
        # request also carries project_id: ProjectID, and ProjectID is a
        # TYPE_CHECKING-only forward reference (project_events cannot import
        # project_manager at runtime without a cycle). get_type_hints() therefore
        # raises NameError for the whole class and cattrs falls back to a
        # no-coercion structure, so destination_path arrives as str.
        destination_path = Path(request.destination_path)
        if not destination_path.parent.is_dir():
            return ExportProjectResultFailure(
                result_details=(
                    f"Attempted to export project '{request.project_id}' to '{destination_path}'. "
                    f"Failed because the destination directory '{destination_path.parent}' does not exist."
                ),
            )

        project_dir = project_info.project_file_path.parent
        adjacent_config = self._config_manager.read_config_file(project_dir / "griptape_nodes_config.json")
        required_secret_keys = self._collect_required_secret_keys()

        try:
            result = package_project_to_zip(project_info, adjacent_config, destination_path, required_secret_keys)
        except (RuntimeError, OSError) as err:
            return ExportProjectResultFailure(
                result_details=(
                    f"Attempted to export project '{request.project_id}' to '{destination_path}'. "
                    f"Failed during packaging because {err}"
                ),
            )

        logger.info("Exported project '%s' to '%s'", request.project_id, result.archive_path)
        return ExportProjectResultSuccess(
            archive_path=result.archive_path,
            referenced_libraries=result.referenced_library_names,
            copied_libraries=result.copied_library_names,
            required_secret_keys=result.required_secret_keys,
            warnings=result.warnings,
            result_details=f"Exported project '{request.project_id}' to '{result.archive_path}'.",
        )

    def on_preview_import_project_request(
        self, request: PreviewImportProjectRequest
    ) -> PreviewImportProjectResultSuccess | PreviewImportProjectResultFailure:
        """Read a project package's manifest without extracting it (read-only).

        Surfaces the manifest plus the required secret keys that are unset in the
        current environment, computed via get_secret(should_error_on_not_found=False)
        so nothing is written.
        """
        validation = self._read_and_validate_manifest(request.archive_path)
        if validation.manifest is None:
            return PreviewImportProjectResultFailure(
                result_details=(
                    f"Attempted to preview project package '{request.archive_path}'. "
                    f"Failed because {validation.failure_reason}"
                ),
            )
        manifest = validation.manifest

        required_secret_keys = manifest.get("required_secret_keys", [])
        unset_secret_keys = self._compute_unset_secret_keys(required_secret_keys)
        return PreviewImportProjectResultSuccess(
            manifest=manifest,
            unset_secret_keys=unset_secret_keys,
            result_details=f"Read manifest from project package '{request.archive_path}'.",
        )

    async def on_import_project_request(
        self, request: ImportProjectRequest
    ) -> ImportProjectResultSuccess | ImportProjectResultFailure:
        """Extract a project package to a target directory and register it.

        Mirrors on_load_project_template_request: the package's base-dir tree is
        extracted 1:1, the optional rename is applied to the extracted YAML, then
        the project is loaded (which persists its path and re-derives a fresh id).
        Macro-defined directories re-resolve against the new location automatically.
        Secrets are never auto-created; required/unset keys are returned for the GUI.
        """
        validation = self._read_and_validate_manifest(request.archive_path)
        if validation.manifest is None:
            return ImportProjectResultFailure(
                result_details=(
                    f"Attempted to import project package '{request.archive_path}'. "
                    f"Failed because {validation.failure_reason}"
                ),
            )
        manifest = validation.manifest

        target_yaml = request.target_directory / WORKSPACE_PROJECT_FILE
        if target_yaml.exists() and not request.overwrite_existing:
            return ImportProjectResultFailure(
                result_details=(
                    f"Attempted to import project package '{request.archive_path}' into "
                    f"'{request.target_directory}'. Failed because a project file already exists at "
                    f"'{target_yaml}' and overwrite_existing is False."
                ),
            )

        try:
            extract_archive(request.archive_path, request.target_directory)
            if request.new_project_name is not None:
                rename_project_template(target_yaml, request.new_project_name)
        except (zipfile.BadZipFile, OSError) as err:
            return ImportProjectResultFailure(
                result_details=(
                    f"Attempted to import project package '{request.archive_path}' into "
                    f"'{request.target_directory}'. Failed during extraction because {err}"
                ),
            )

        load_result = await self.on_load_project_template_request(LoadProjectTemplateRequest(project_path=target_yaml))
        if isinstance(load_result, LoadProjectTemplateResultFailure):
            return ImportProjectResultFailure(
                result_details=(
                    f"Attempted to import project package '{request.archive_path}' into "
                    f"'{request.target_directory}'. Extracted successfully but the project failed to load: "
                    f"{load_result.result_details}"
                ),
            )

        required_secret_keys = manifest.get("required_secret_keys", [])
        unset_secret_keys = self._compute_unset_secret_keys(required_secret_keys)
        warnings = list(manifest.get("warnings", []))

        # Activation can fail without raising (e.g. the imported config's
        # requires_engine is incompatible). The project is still loaded and
        # registered, so this is success-with-caveat: surface the activation
        # failure as a warning rather than masking it behind a clean success.
        if request.set_as_current:
            activation_result = await self.on_set_current_project_request(
                SetCurrentProjectRequest(project_id=load_result.project_id)
            )
            if isinstance(activation_result, SetCurrentProjectResultFailure):
                warnings.append(f"Imported project was not activated: {activation_result.result_details}")

        logger.info("Imported project package '%s' into '%s'", request.archive_path, request.target_directory)
        return ImportProjectResultSuccess(
            project_id=load_result.project_id,
            project_file_path=target_yaml,
            required_secret_keys=required_secret_keys,
            unset_secret_keys=unset_secret_keys,
            warnings=warnings,
            result_details=f"Imported project package '{request.archive_path}' into '{request.target_directory}'.",
        )

    def _read_and_validate_manifest(self, archive_path: Path) -> _ManifestValidation:
        """Read a project package's manifest and check its schema compatibility.

        Returns the parsed manifest on success. On failure returns only the reason
        fragment (no handler prefix) so the preview and import handlers can supply
        their own user-facing wording.
        """
        try:
            manifest = read_manifest(archive_path)
        except (OSError, zipfile.BadZipFile, KeyError, json.JSONDecodeError) as err:
            return _ManifestValidation(manifest=None, failure_reason=str(err))

        if not is_manifest_schema_compatible(manifest):
            return _ManifestValidation(
                manifest=None,
                failure_reason=(
                    f"its manifest schema version "
                    f"'{manifest.get('manifest_schema_version')}' is incompatible with this engine."
                ),
            )

        return _ManifestValidation(manifest=manifest, failure_reason=None)

    def _collect_required_secret_keys(self) -> list[str]:
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
        return sorted(self._secrets_manager.secrets_to_register.keys())

    def _compute_unset_secret_keys(self, required_secret_keys: list[str]) -> list[str]:
        """Return the subset of required keys with no value in the current environment.

        Uses get_secret(should_error_on_not_found=False) so detection never writes
        a value or raises. Secrets are never auto-created on import.
        """
        return [
            key
            for key in required_secret_keys
            if self._secrets_manager.get_secret(key, should_error_on_not_found=False) is None
        ]

    async def on_app_initialization_complete(self, _payload: AppInitializationComplete) -> None:
        """Load system default project template when app initializes.

        Called by EventManager after all libraries are loaded.
        Loads system defaults, then checks workspace for a griptape-nodes-project.yml
        overlay file and sets it as the current project if found. If a project has
        already been explicitly selected before this event (e.g., by a CLI executor
        via --project-file-path), preserves that choice and skips workspace discovery.

        A worker boots exactly like an orchestrator: it re-derives the current project
        from the same shared on-disk config (project_file / workspace default), so it
        lands on the orchestrator's project for free. The orchestrator persists the
        SYSTEM_DEFAULTS_KEY sentinel when it deliberately stays on system defaults, so a
        worker honoring project_file does not "discover" a workspace griptape-nodes-project.yml
        the orchestrator chose to ignore.
        """
        # If an explicit project was selected before init completed (e.g., by
        # LocalWorkflowExecutor loading --project-file-path), keep it. Still load
        # registered projects for visibility and mark init complete.
        explicit_project_selected = self._current_project_id != SYSTEM_DEFAULTS_KEY
        if explicit_project_selected:
            await self._load_registered_projects()
            self._initialization_complete = True
            return

        # Set system defaults as current project (using synthetic key for system defaults)
        set_request = SetCurrentProjectRequest(project_id=SYSTEM_DEFAULTS_KEY)
        result = await self.on_set_current_project_request(set_request)

        if result.failed():
            logger.error("Failed to set default project as current: %s", result.result_details)
            return

        logger.debug("Successfully loaded default project template")

        # Check workspace for an optional project overlay file
        await self._load_workspace_project()

        # Load any additional project templates previously registered by the user
        await self._load_registered_projects()

        # Mark initialization complete so subsequent project switches trigger
        # workspace detection and library reload when the workspace actually changes.
        self._initialization_complete = True

    def on_get_all_situations_for_project_request(
        self, _request: GetAllSituationsForProjectRequest
    ) -> GetAllSituationsForProjectResultSuccess | GetAllSituationsForProjectResultFailure:
        """Get all situation names and schemas from current project template."""
        current_project_request = GetCurrentProjectRequest()
        current_project_result = self.on_get_current_project_request(current_project_request)

        if not isinstance(current_project_result, GetCurrentProjectResultSuccess):
            return GetAllSituationsForProjectResultFailure(
                result_details="Attempted to get all situations. Failed because no current project is set or template not loaded"
            )

        template = current_project_result.project_info.template
        situations = {situation_name: situation.macro for situation_name, situation in template.situations.items()}

        return GetAllSituationsForProjectResultSuccess(
            situations=situations,
            result_details=f"Successfully retrieved all situations. Found {len(situations)} situations",
        )

    def on_attempt_map_absolute_path_to_project_request(
        self, request: AttemptMapAbsolutePathToProjectRequest
    ) -> AttemptMapAbsolutePathToProjectResultSuccess | AttemptMapAbsolutePathToProjectResultFailure:
        """Find out if an absolute path exists anywhere within a Project directory.

        Returns Success with mapped_path if inside project (macro form returned).
        Returns Success with None if outside project (valid answer: "not in project").
        Returns Failure if operation cannot be performed (no project, no secrets manager).

        Args:
            request: Request containing the absolute path to check

        Returns:
            Success with mapped_path if path is inside project
            Success with None if path is outside project
            Failure if operation cannot be performed
        """
        # Check prerequisites - return Failure if missing
        current_project_request = GetCurrentProjectRequest()
        current_project_result = self.on_get_current_project_request(current_project_request)

        if not isinstance(current_project_result, GetCurrentProjectResultSuccess):
            return AttemptMapAbsolutePathToProjectResultFailure(
                result_details="Attempted to map absolute path. Failed because no current project is set"
            )

        project_info = current_project_result.project_info

        # Try to map the path
        try:
            mapped_path = self._absolute_path_to_macro_path(request.absolute_path, project_info)
        except (RuntimeError, NotImplementedError) as e:
            # Variable resolution failed - this is a Failure (can't complete the operation)
            return AttemptMapAbsolutePathToProjectResultFailure(
                result_details=f"Attempted to map absolute path '{request.absolute_path}'. Failed because: {e}"
            )

        # Path successfully checked
        if mapped_path is None:
            # Success: we successfully determined the path is outside project
            return AttemptMapAbsolutePathToProjectResultSuccess(
                mapped_path=None,
                result_details=f"Attempted to map absolute path '{request.absolute_path}'. Path is outside all project directories",
            )

        # Success: path mapped to macro form
        return AttemptMapAbsolutePathToProjectResultSuccess(
            mapped_path=mapped_path,
            result_details=f"Successfully mapped absolute path to '{mapped_path}'",
        )

    # Helper methods (private)

    @staticmethod
    def _parse_situation_macros(
        situations: dict[str, SituationTemplate], validation: ProjectValidationInfo
    ) -> dict[str, ParsedMacro]:
        """Parse all situation macros.

        This is called BEFORE creating ProjectInfo to ensure all macros are valid.
        Collects all parsing errors into the validation object instead of raising.

        Args:
            situations: Dictionary of situation templates to parse
            validation: Validation object to collect errors

        Returns:
            Dictionary mapping situation_name to ParsedMacro (only for successfully parsed macros)
        """
        situation_schemas: dict[str, ParsedMacro] = {}

        for situation_name, situation in situations.items():
            try:
                situation_schemas[situation_name] = ParsedMacro(situation.macro)
            except Exception as e:
                validation.add_error(f"situations.{situation_name}.macro", f"Failed to parse macro: {e}")

        return situation_schemas

    @staticmethod
    def _parse_directory_macros(
        directories: dict[str, DirectoryDefinition], validation: ProjectValidationInfo
    ) -> dict[str, ParsedMacro]:
        """Parse all directory macros.

        This is called BEFORE creating ProjectInfo to ensure all macros are valid.
        Collects all parsing errors into the validation object instead of raising.

        Args:
            directories: Dictionary of directory definitions to parse
            validation: Validation object to collect errors

        Returns:
            Dictionary mapping directory_name to ParsedMacro (only for successfully parsed macros)
        """
        directory_schemas: dict[str, ParsedMacro] = {}

        for directory_name, directory_def in directories.items():
            path_macro = directory_def.path_macro
            try:
                if isinstance(path_macro, str):
                    directory_schemas[directory_name] = ParsedMacro(path_macro)
                else:
                    # Per-platform mapping: parse every populated key to validate macro syntax.
                    # Cache the active-platform parse under the directory name so call sites that
                    # consume parsed_directory_schemas keep working.
                    selected = path_macro.select()
                    for platform_key in ("linux", "darwin", "windows", "default"):
                        raw = getattr(path_macro, platform_key)
                        if raw is not None:
                            ParsedMacro(raw)
                    if selected is not None:
                        directory_schemas[directory_name] = ParsedMacro(selected)
            except Exception as e:
                validation.add_error(f"directories.{directory_name}.path_macro", f"Failed to parse macro: {e}")

        return directory_schemas

    def _get_builtin_variable_value(self, var_name: str, project_info: ProjectInfo) -> str:  # noqa: C901
        """Get the value of a single builtin variable.

        Args:
            var_name: Name of the builtin variable
            project_info: Information about the current project

        Returns:
            String value of the builtin variable

        Raises:
            ValueError: If var_name is not a recognized builtin variable
            NotImplementedError: If builtin variable is not yet implemented
        """
        match var_name:
            case "project_dir":
                return str(project_info.project_base_dir)

            case "project_name":
                msg = f"{BUILTIN_PROJECT_NAME} not yet implemented"
                raise NotImplementedError(msg)

            case "workspace_dir":
                return str(self._config_manager.workspace_path)

            case "workflow_name":
                context_manager = GriptapeNodes.ContextManager()
                if not context_manager.has_current_workflow():
                    msg = "No current workflow"
                    raise RuntimeError(msg)
                return context_manager.get_current_workflow_name()

            case "workflow_dir":
                context_manager = GriptapeNodes.ContextManager()
                if not context_manager.has_current_workflow():
                    msg = "No current workflow"
                    raise RuntimeError(msg)
                workflow_name = context_manager.get_current_workflow_name()
                try:
                    workflow = WorkflowRegistry.get_workflow_by_name(workflow_name)
                except KeyError as e:
                    msg = f"Workflow '{workflow_name}' has not been saved yet"
                    raise RuntimeError(msg) from e
                if workflow.file_path is None:
                    msg = f"Workflow '{workflow_name}' has not been saved yet"
                    raise RuntimeError(msg)
                workflow_file_path = Path(WorkflowRegistry.get_complete_file_path(workflow.file_path))
                return str(workflow_file_path.parent)

            case "static_files_dir":
                return self._config_manager.get_config_value("static_files_directory", default="staticfiles")

            case _:
                msg = f"Unknown builtin variable: {var_name}"
                raise ValueError(msg)

    def _absolute_path_to_macro_path(self, absolute_path: Path, project_info: ProjectInfo) -> str | None:
        """Convert an absolute path to macro form using longest prefix matching.

        Resolves all project directories at runtime (to support env vars and macros),
        then checks if the absolute path is within any of them.
        Uses longest prefix matching to find the best match.

        Args:
            absolute_path: Absolute path to convert (e.g., /Users/james/project/outputs/file.png)
            project_info: Information about the current project

        Returns:
            Macro-ified path (e.g., {outputs}/file.png) if inside a project directory,
            or None if outside all project directories

        Raises:
            RuntimeError: If directory resolution fails or builtin variable cannot be resolved
            NotImplementedError: If a required builtin variable is not yet implemented

        Examples:
            /Users/james/project/outputs/renders/file.png → "{outputs}/renders/file.png"
            /Users/james/project/outputs/inputs/file.png → "{outputs}/inputs/file.png"
            /Users/james/Downloads/file.png → None
        """
        # Normalize paths for consistent cross-platform comparison
        absolute_path = resolve_path_safely(absolute_path)

        template = project_info.template
        workspace_dir = resolve_path_safely(self._config_manager.workspace_path)
        project_base_dir = resolve_path_safely(project_info.project_base_dir)

        # Shared recursive resolver so directories referencing other directories
        # (e.g. watch_output -> watch_folder) flatten through the same machinery
        # as forward macro resolution. Caches results across the whole inversion
        # pass below.
        resolver = self._build_variable_resolver(template, project_info)

        # Find all matching directories (where absolute_path is inside the directory)
        class DirectoryMatch(NamedTuple):
            directory_name: str
            resolved_path: Path
            prefix_length: int

        matches: list[DirectoryMatch] = []

        for directory_name in template.directories:
            try:
                resolved_path_str = resolver.resolve_directory(directory_name)
            except MacroResolutionError as e:
                msg = f"Failed to resolve directory '{directory_name}' macro: {e}"
                raise RuntimeError(msg) from e

            # Make absolute (resolve relative paths against the workspace directory).
            # resolve_file_path handles ~, env vars, and absolute paths in addition to relative paths.
            resolved_dir_path = resolve_file_path(resolved_path_str, workspace_dir)
            # Normalize for consistent cross-platform comparison
            resolved_dir_path = resolve_path_safely(resolved_dir_path)

            # Check if absolute_path is inside this directory
            try:
                # relative_to will raise ValueError if not a subpath
                _ = absolute_path.relative_to(resolved_dir_path)
                # Track the match with its prefix length (for longest match)
                matches.append(
                    DirectoryMatch(
                        directory_name=directory_name,
                        resolved_path=resolved_dir_path,
                        prefix_length=len(resolved_dir_path.parts),
                    )
                )
            except ValueError:
                # Not a subpath, skip
                continue

        # If no defined directories matched, try {project_dir} as fallback
        if not matches:
            # Check if path is inside project_base_dir
            try:
                relative_path = absolute_path.relative_to(project_base_dir)

                # Convert to {project_dir} macro form
                if str(relative_path) == ".":
                    return "{project_dir}"
                return f"{{project_dir}}/{relative_path.as_posix()}"
            except ValueError:
                # Not inside project_base_dir either
                return None

        # Use longest prefix match (most specific directory)
        best_match = matches[0]
        for match in matches:
            if match.prefix_length > best_match.prefix_length:
                best_match = match

        # Calculate relative path from the matched directory
        relative_path = absolute_path.relative_to(best_match.resolved_path)

        # Convert to macro form
        if str(relative_path) == ".":
            # File is directly in the directory root
            # Example: /Users/james/project/outputs → {outputs}
            return f"{{{best_match.directory_name}}}"

        # File is in a subdirectory
        # Example: /Users/james/project/outputs/renders/final.png → {outputs}/renders/final.png
        return f"{{{best_match.directory_name}}}/{relative_path.as_posix()}"

    # Private helper methods

    def _load_system_defaults(self) -> None:
        """Load bundled system default template.

        System defaults are now defined in Python as DEFAULT_PROJECT_TEMPLATE.
        This is always valid by construction.
        """
        logger.debug("Loading system default template")

        # Create validation info to track that defaults were loaded
        validation = ProjectValidationInfo(status=ProjectValidationStatus.GOOD)

        # System defaults use workspace directory as the base directory.
        workspace_dir = self._config_manager.workspace_path

        # Parse all macros BEFORE creating ProjectInfo (system defaults should always be valid)
        situation_schemas = self._parse_situation_macros(DEFAULT_PROJECT_TEMPLATE.situations, validation)
        directory_schemas = self._parse_directory_macros(DEFAULT_PROJECT_TEMPLATE.directories, validation)

        # Create consolidated ProjectInfo with fully populated macro caches
        project_info = ProjectInfo(
            project_id=SYSTEM_DEFAULTS_KEY,
            project_file_path=None,  # No actual file for system defaults
            project_base_dir=workspace_dir,  # Use workspace as base
            template=DEFAULT_PROJECT_TEMPLATE,
            validation=validation,
            parsed_situation_schemas=situation_schemas,
            parsed_directory_schemas=directory_schemas,
        )

        # Store in new consolidated dict
        self._successfully_loaded_project_templates[SYSTEM_DEFAULTS_KEY] = project_info

        logger.debug("System defaults loaded successfully")

    def _resolve_project_file_path(self) -> Path | None:
        """Resolve the path to the project file to load, or None if no file should be loaded.

        Checks config in the following order:
        1. The `project_file` config setting (if set). The SYSTEM_DEFAULTS_KEY sentinel
           means "deliberately on system defaults": return None WITHOUT falling through
           to workspace discovery, so a restart (or a freshly spawned worker booting like
           an engine) stays on defaults instead of re-adopting a workspace file.
        2. griptape-nodes-project.yml in the workspace directory (default)

        Returns None if no project file should be loaded (explicit system defaults,
        missing config, file not found).
        """
        project_file_value = self._config_manager.get_config_value("project_file")
        if project_file_value == SYSTEM_DEFAULTS_KEY:
            return None
        if project_file_value is not None:
            project_path = Path(project_file_value)
            if project_path.exists():
                return project_path
            logger.warning(
                "project_file config points to '%s' which does not exist, falling back to workspace default",
                project_path,
            )

        workspace_dir = self._config_manager.workspace_path
        workspace_project_path = workspace_dir / WORKSPACE_PROJECT_FILE
        if not workspace_project_path.exists():
            logger.debug("No workspace project file found at '%s'", workspace_project_path)
            return None

        return workspace_project_path

    async def _load_workspace_project(self) -> str | None:  # noqa: PLR0911
        """Load workspace-level project template overlay if present.

        Checks for a project file using _resolve_project_file_path. If found, loads
        it as an overlay on top of system defaults and sets it as the current project.
        If no file is found, the system defaults remain current.

        Returns a failure-detail string when a resolved project file fails to load or
        activate (the same text that is logged), or None on success or when no project
        file is present. Callers that report activation outcome use this signal directly
        rather than inferring failure from current-project read-back.
        """
        workspace_project_path = self._resolve_project_file_path()
        if workspace_project_path is None:
            return None

        workspace_project_path = workspace_project_path.resolve()
        logger.debug("Found workspace project file at '%s', loading", workspace_project_path)

        try:
            yaml_text = await File(str(workspace_project_path)).aread_text()
        except FileLoadError as e:
            logger.error(
                "Attempted to read workspace project file at '%s'. Failed with: %s",
                workspace_project_path,
                e.result_details,
            )
            return f"the project file could not be read: {e.result_details}"

        validation = ProjectValidationInfo(status=ProjectValidationStatus.GOOD)
        overlay = load_partial_project_template(yaml_text, validation)

        if overlay is None:
            logger.error(
                "Attempted to load workspace project from '%s'. Failed because YAML could not be parsed",
                workspace_project_path,
            )
            return "the project YAML could not be parsed"

        # Derive the project id (the registry key). An explicit overlay id wins;
        # a legacy project with no id falls back to the file path string. From
        # here on the id identifies the project and the path is only a locator.
        project_id = overlay.id if overlay.id is not None else str(workspace_project_path)

        # Fail closed on an id collision: a *different* file already holds this
        # id. Reloading the same file (same id, same path) is a no-op refresh.
        existing = self._successfully_loaded_project_templates.get(project_id)
        if existing is not None and existing.project_file_path != workspace_project_path:
            logger.error(
                "Attempted to load workspace project from '%s'. Failed because its id '%s' is already used by a "
                "different project at '%s'.",
                workspace_project_path,
                project_id,
                existing.project_file_path,
            )
            return f"its id '{project_id}' is already used by a different project at '{existing.project_file_path}'"

        template = ProjectTemplate.merge(
            default_template_for_version(overlay.project_template_schema_version), overlay, validation
        )

        if not validation.is_usable():
            problem_details = "; ".join(
                f"{p.field_path} (line {p.line_number}): {p.message}"
                if p.line_number is not None
                else f"{p.field_path}: {p.message}"
                for p in validation.problems
            )
            logger.error(
                "Attempted to load workspace project from '%s'. Failed because template is not usable (status: %s). Problems: %s",
                workspace_project_path,
                validation.status,
                problem_details,
            )
            return f"the project template is not usable (status: {validation.status}). Problems: {problem_details}"

        situation_schemas = self._parse_situation_macros(template.situations, validation)
        directory_schemas = self._parse_directory_macros(template.directories, validation)

        project_info = ProjectInfo(
            project_id=project_id,
            project_file_path=workspace_project_path,
            project_base_dir=workspace_project_path.parent,
            template=template,
            validation=validation,
            parsed_situation_schemas=situation_schemas,
            parsed_directory_schemas=directory_schemas,
        )
        self._successfully_loaded_project_templates[project_id] = project_info
        self._registered_template_status[workspace_project_path] = validation

        set_request = SetCurrentProjectRequest(project_id=project_id)
        set_result = await self.on_set_current_project_request(set_request)

        if set_result.failed():
            logger.error(
                "Attempted to set workspace project '%s' as current. Failed with: %s",
                workspace_project_path,
                set_result.result_details,
            )
            return f"setting it as the current project failed: {set_result.result_details}"

        logger.debug("Successfully loaded workspace project from '%s'", workspace_project_path)
        return None

    async def _load_registered_projects(self) -> None:
        """Load project templates from paths persisted in user config.

        Called after workspace project loading so that user-registered paths
        are available in the template list. Paths already loaded (e.g., the
        workspace project) are skipped. Missing or invalid files are skipped
        with a warning rather than raising.

        A pre-pass reads each registered file's overlay id to build a transient
        id -> file path index, so an id-based parent can be located even when its
        child is registered (and loaded) before it. The index is cleared once the
        load loop finishes; at runtime the live registry serves single-file loads.
        """
        registered_entries: list[str | dict | PerPlatformProjectPath] = (
            self._config_manager.get_config_value(PROJECTS_TO_REGISTER_KEY, default=[]) or []
        )
        resolved_paths = self._resolve_registered_entry_paths(registered_entries)

        # A directory entry is recursively scanned for project files (each loaded
        # without persisting), mirroring how libraries_to_register expands a
        # folder. Split directories from individual file entries so the id pre-pass
        # and the per-file load loop only see files; directories are scanned after.
        directory_paths = [path for path in resolved_paths if path.is_dir()]
        file_paths = [path for path in resolved_paths if not path.is_dir()]

        # Pre-pass: index id -> canonical path so child-before-parent ordering
        # still resolves id-based parents (which carry no path) during the load
        # loop below.
        self._boot_id_to_file_path = {}
        for canonical_path in file_paths:
            read_load = await self._read_overlay(canonical_path)
            if isinstance(read_load, LoadProjectTemplateResultFailure):
                continue
            _, overlay = read_load
            if overlay.id is not None:
                self._boot_id_to_file_path[overlay.id] = canonical_path

        try:
            for canonical_path in file_paths:
                # Skip files already loaded (e.g. the workspace project). Correlate
                # by the file path locator, not by id: the registry is id-keyed, so
                # a path string would never match an explicitly-id'd project's key.
                already_loaded = any(
                    info.project_file_path == canonical_path
                    for info in self._successfully_loaded_project_templates.values()
                )
                if already_loaded:
                    continue
                load_request = LoadProjectTemplateRequest(project_path=canonical_path)
                result = await self.on_load_project_template_request(load_request)
                if result.failed():
                    logger.warning(
                        "Failed to load registered project '%s' on startup: %s",
                        canonical_path,
                        result.result_details,
                    )
                else:
                    logger.debug("Reloaded registered project from '%s'", canonical_path)

            for directory in directory_paths:
                await self._load_projects_from_directory(directory)
        finally:
            # The index is only meaningful during boot.
            self._boot_id_to_file_path = {}

    def _resolve_registered_entry_paths(
        self, registered_entries: list[str | dict | PerPlatformProjectPath]
    ) -> list[Path]:
        """Resolve persisted projects_to_register entries to canonical file paths.

        Coerces raw dicts (from JSON/YAML config) into the per-platform model so
        select_project_path can apply the active-platform key and `default`
        fallback uniformly, selects the active-platform path, then canonicalizes
        it (expand ~/env vars + absolutize + follow symlinks) so different
        spellings of the same file collide. Entries with no path for the active
        platform, or that fail validation, are skipped with a warning. Duplicate
        canonical paths are de-duplicated so each file is processed once.
        """
        resolved: list[Path] = []
        seen: set[Path] = set()
        for entry in registered_entries:
            if isinstance(entry, dict):
                try:
                    selectable: str | PerPlatformProjectPath | None = PerPlatformProjectPath.model_validate(entry)
                except ValidationError as err:
                    logger.warning(
                        "Skipping invalid per-platform projects_to_register entry %s: %s",
                        entry,
                        err,
                    )
                    continue
            else:
                selectable = entry
            path_str = select_project_path(selectable)
            if path_str is None:
                logger.warning(
                    "Skipping per-platform projects_to_register entry with no key for the active platform "
                    "and no `default`: %s",
                    entry,
                )
                continue
            canonical_path = canonicalize_for_identity(path_str)
            if canonical_path in seen:
                continue
            seen.add(canonical_path)
            resolved.append(canonical_path)
        return resolved

    async def _load_projects_from_directory(self, directory: Path) -> None:
        """Discover and load every project file under a registered directory.

        Recursively scans for WORKSPACE_PROJECT_FILE, loading each match into
        memory without persisting it. The directory entry is the unit of
        registration, so discovered files cannot be individually unregistered;
        they are re-discovered on each startup. The scan is depth-bounded by the
        `discovery_max_depth` setting and hidden directories (e.g. .venv, .git)
        are skipped by find_files_recursive.
        """
        discovered = await find_files_recursive(directory, WORKSPACE_PROJECT_FILE)
        if not discovered:
            logger.warning(
                "projects_to_register directory '%s' contains no '%s' files; skipping",
                directory,
                WORKSPACE_PROJECT_FILE,
            )
            return
        for project_file in discovered:
            # Correlate by the file path locator, not by id: the registry is
            # id-keyed, so a path string would never match an explicitly-id'd
            # project's key.
            canonical_path = canonicalize_for_identity(project_file)
            already_loaded = any(
                info.project_file_path == canonical_path
                for info in self._successfully_loaded_project_templates.values()
            )
            if already_loaded:
                continue
            result = await self._load_and_cache_project_template(project_file, persist_path=False)
            if result.failed():
                logger.warning(
                    "Failed to load discovered project '%s' from directory '%s': %s",
                    project_file,
                    directory,
                    result.result_details,
                )
            else:
                logger.debug("Loaded discovered project '%s' from directory '%s'", project_file, directory)

    def _register_project_path(self, project_file_path: str) -> None:
        """Persist a project file path so it is loaded on the next engine restart.

        PROJECTS_TO_REGISTER_KEY stores file paths (locators), not ids: boot
        reloads each file by path and re-derives its id. Appends the canonical
        path to the list if not already present. Errors are logged as warnings
        and do not affect the load result.

        No-op on a worker: the orchestrator owns projects_to_register in the
        shared on-disk config. A worker that loads a project to adopt the
        orchestrator's switch must not write the shared file back, since both
        processes share it and a worker write races the orchestrator's.
        """
        if GriptapeNodes.LibraryManager().is_worker:
            return
        try:
            registered: list[str | dict | PerPlatformProjectPath] = (
                self._config_manager.get_config_value(PROJECTS_TO_REGISTER_KEY, default=[]) or []
            )
            # Compare by canonicalized path (~/env expansion + resolution) so a
            # previously persisted relative or ~/ spelling of the same file
            # isn't re-persisted as a duplicate. Per-platform entries are
            # reduced to the active-platform string before comparison; entries
            # with no match for this platform are skipped from the dedupe set.
            resolved_existing: set[str] = set()
            for entry in registered:
                if isinstance(entry, dict):
                    try:
                        selected = select_project_path(PerPlatformProjectPath.model_validate(entry))
                    except ValidationError:
                        continue
                else:
                    selected = select_project_path(entry)
                if selected is None:
                    continue
                resolved_existing.add(str(canonicalize_for_identity(selected)))
            if project_file_path not in resolved_existing:
                self._config_manager.set_config_value(PROJECTS_TO_REGISTER_KEY, [*registered, project_file_path])
        except Exception:
            logger.warning("Failed to persist project path '%s' to config", project_file_path)
