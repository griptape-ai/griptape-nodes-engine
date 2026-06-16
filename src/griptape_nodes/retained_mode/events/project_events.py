"""Events for project template management."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, NamedTuple

from griptape_nodes.common.macro_parser import MacroMatchFailure, MacroVariables, ParsedMacro, VariableInfo
from griptape_nodes.common.project_templates import ProjectTemplate, ProjectValidationInfo, SituationTemplate
from griptape_nodes.retained_mode.events.base_events import (
    RequestPayload,
    ResultPayloadFailure,
    ResultPayloadSuccess,
    WorkflowNotAlteredMixin,
)
from griptape_nodes.retained_mode.events.payload_registry import PayloadRegistry

if TYPE_CHECKING:
    from pathlib import Path

    # Circular import: project_events -> project_manager -> file.py -> os_events -> project_events
    from griptape_nodes.retained_mode.managers.project_manager import ProjectID, ProjectInfo


class MacroPath(NamedTuple):
    """A macro path with its parsed template and variable values.

    Used when file paths need macro resolution before filesystem operations.

    Attributes:
        parsed_macro: The parsed macro template
        variables: Variable values for macro substitution
    """

    parsed_macro: ParsedMacro
    variables: MacroVariables


class PathResolutionFailureReason(StrEnum):
    """Reason why path resolution from macro failed."""

    MISSING_REQUIRED_VARIABLES = "MISSING_REQUIRED_VARIABLES"
    MACRO_RESOLUTION_ERROR = "MACRO_RESOLUTION_ERROR"
    RESERVED_NAME_COLLISION = "RESERVED_NAME_COLLISION"


@dataclass
@PayloadRegistry.register
class LoadProjectTemplateRequest(RequestPayload):
    """Load user's project.yml and merge with system defaults.

    Use when: User opens a workspace, user creates new project, user modifies project.yml.

    Args:
        project_path: Path to the project.yml file to load

    Results: LoadProjectTemplateResultSuccess | LoadProjectTemplateResultFailure
    """

    project_path: Path


@dataclass
@PayloadRegistry.register
class LoadProjectTemplateResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Project template loaded successfully.

    Args:
        project_id: The identifier for the loaded project
        template: The merged ProjectTemplate (system defaults + user customizations)
        validation: Validation info with status and any problems encountered
    """

    project_id: ProjectID
    template: ProjectTemplate
    validation: ProjectValidationInfo


@dataclass
@PayloadRegistry.register
class LoadProjectTemplateResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Project template loading failed.

    Args:
        validation: Validation info with error details
    """

    validation: ProjectValidationInfo


@dataclass
@PayloadRegistry.register
class GetProjectTemplateRequest(RequestPayload):
    """Get cached project template for a project ID.

    Use when: Querying current project configuration, checking validation status.

    Args:
        project_id: Identifier of the project

    Results: GetProjectTemplateResultSuccess | GetProjectTemplateResultFailure
    """

    project_id: ProjectID


@dataclass
@PayloadRegistry.register
class GetProjectTemplateResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Project template retrieved from cache.

    Args:
        template: The successfully loaded ProjectTemplate
        validation: Validation info for the template
    """

    template: ProjectTemplate
    validation: ProjectValidationInfo


@dataclass
@PayloadRegistry.register
class GetProjectTemplateResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Project template retrieval failed (not loaded yet)."""


@dataclass
class ProjectTemplateInfo:
    """Information about a loaded or failed project template.

    Fields:
        project_id: Canonical absolute path identifying this template in the registry.
        validation: Outcome of loading + parsing this template.
        name: Display name from the template body, when available.
        parent_project_path: The parent's canonical absolute path, suitable for
            direct equality matching against another entry's project_id when
            reconstructing the parent/child hierarchy. None means no parent
            (system defaults are the only base).
    """

    project_id: ProjectID
    validation: ProjectValidationInfo
    name: str | None = None
    parent_project_path: str | None = None


@dataclass
@PayloadRegistry.register
class ListProjectTemplatesRequest(RequestPayload):
    """List all project templates that have been loaded or attempted to load.

    Use when: Displaying available projects, checking which projects are loaded.

    Args:
        include_system_builtins: Whether to include system builtin templates like SYSTEM_DEFAULTS_KEY

    Results: ListProjectTemplatesResultSuccess
    """

    include_system_builtins: bool = False


@dataclass
@PayloadRegistry.register
class ListProjectTemplatesResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """List of all project templates retrieved.

    Args:
        successfully_loaded: List of templates that loaded successfully
        failed_to_load: List of templates that failed to load with validation errors
    """

    successfully_loaded: list[ProjectTemplateInfo]
    failed_to_load: list[ProjectTemplateInfo]


@dataclass
@PayloadRegistry.register
class GetSituationRequest(RequestPayload):
    """Get the full situation template for a specific situation.

    Returns the complete SituationTemplate including macro and policy.

    Use when: Need situation macro and/or policy for file operations.
    Uses the current project for context.

    Args:
        situation_name: Name of the situation template (e.g., "save_node_output")

    Results: GetSituationResultSuccess | GetSituationResultFailure
    """

    situation_name: str


@dataclass
@PayloadRegistry.register
class GetSituationResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Situation template retrieved successfully.

    Args:
        situation: The complete situation template including macro and policy.
                  Access via situation.macro, situation.policy.create_dirs, etc.
    """

    situation: SituationTemplate


@dataclass
@PayloadRegistry.register
class GetSituationResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Situation template retrieval failed (situation not found or template not loaded)."""


@dataclass
@PayloadRegistry.register
class GetPathForMacroRequest(RequestPayload):
    """Resolve ANY macro schema with variables to produce final file path.

    Use when: Resolving paths, saving files. Works with any macro string, not tied to situations.

    Uses the current project for context. Caller must parse the macro string
    into a ParsedMacro before creating this request.

    Args:
        parsed_macro: The parsed macro to resolve
        variables: Variable values for macro substitution (e.g., {"file_name": "output", "file_ext": "png"})

    Results: GetPathForMacroResultSuccess | GetPathForMacroResultFailure
    """

    parsed_macro: ParsedMacro
    variables: MacroVariables


@dataclass
@PayloadRegistry.register
class GetPathForMacroResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Path resolved successfully from macro.

    Args:
        resolved_path: The relative project path after macro substitution (e.g., "outputs/file.png")
        absolute_path: The absolute filesystem path (e.g., "/workspace/outputs/file.png")
    """

    resolved_path: Path
    absolute_path: Path


@dataclass
@PayloadRegistry.register
class GetPathForMacroResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Path resolution failed.

    Args:
        failure_reason: Specific reason for failure
        missing_variables: List of required variable names that were not provided (for MISSING_REQUIRED_VARIABLES)
        conflicting_variables: List of variables that collide with a reserved name (for RESERVED_NAME_COLLISION)
    """

    failure_reason: PathResolutionFailureReason
    missing_variables: set[str] | None = None
    conflicting_variables: set[str] | None = None


@dataclass
@PayloadRegistry.register
class SetCurrentProjectRequest(RequestPayload):
    """Set which project user has currently selected.

    Use when: User switches between projects, opens a new workspace.

    If the workspace directory changes as a result of setting the project,
    and startup is complete, this handler automatically reloads all libraries
    and re-registers workflows from config and the new workspace.

    Args:
        project_id: Identifier of the project to set as current. None lands the
            engine on the system defaults rather than a "no project" state.

    Results: SetCurrentProjectResultSuccess | SetCurrentProjectResultFailure
    """

    project_id: ProjectID | None


@dataclass
@PayloadRegistry.register
class SetCurrentProjectResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Current project set successfully."""


@dataclass
@PayloadRegistry.register
class SetCurrentProjectResultFailure(ResultPayloadFailure):
    """Current project set failed."""


@dataclass
@PayloadRegistry.register
class GetCurrentProjectRequest(RequestPayload):
    """Get the currently selected project path.

    Use when: Need to know which project user is working with.

    Results: GetCurrentProjectResultSuccess | GetCurrentProjectResultFailure
    """


@dataclass
@PayloadRegistry.register
class GetCurrentProjectResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Current project retrieved.

    Args:
        project_info: Complete information about the current project
    """

    project_info: ProjectInfo


@dataclass
@PayloadRegistry.register
class GetCurrentProjectResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """No current project is set."""


@dataclass
@PayloadRegistry.register
class SaveProjectTemplateRequest(RequestPayload):
    """Save user customizations to project.yml file.

    Use when: User modifies project configuration, exports template.

    Args:
        project_path: Path where project.yml should be saved
        template_data: Dict representation of the template to save

    Results: SaveProjectTemplateResultSuccess | SaveProjectTemplateResultFailure
    """

    project_path: Path
    template_data: dict[str, Any]


@dataclass
@PayloadRegistry.register
class SaveProjectTemplateResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Project template saved successfully."""


@dataclass
@PayloadRegistry.register
class SaveProjectTemplateResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Project template save failed.

    Common causes:
    - Permission denied
    - Invalid path
    - Disk full
    """


@dataclass
@PayloadRegistry.register
class ValidateProjectTemplateRequest(RequestPayload):
    """Validate a project template dict without saving or loading it.

    Use when: UI needs to check whether a template would be accepted by the
    engine before committing changes to disk. Runs the same validation pipeline
    that LoadProjectTemplateRequest uses, without any side effects.

    Args:
        template_data: Dict representation of the template to validate
        project_id: Optional project_id of the template being edited. When provided,
            it is seeded into the parent-chain visited set so a cycle that includes
            "myself" (e.g. setting parent to a project that already points back at
            this one) is detected.

    Results: ValidateProjectTemplateResultSuccess | ValidateProjectTemplateResultFailure
    """

    template_data: dict[str, Any]
    project_id: str | None = None


@dataclass
@PayloadRegistry.register
class ValidateProjectTemplateResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Validation completed.

    The validation itself always produces a structured result; check
    `validation.status` to determine if the template is usable.

    Args:
        validation: Validation info with status and any problems encountered
    """

    validation: ProjectValidationInfo


@dataclass
@PayloadRegistry.register
class ValidateProjectTemplateResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Validation could not be performed (e.g. malformed request)."""


@dataclass
@PayloadRegistry.register
class AttemptMatchPathAgainstMacroRequest(RequestPayload):
    """Attempt to match a path against a macro schema and extract variables.

    Use when: Validating paths, extracting info from file paths,
    identifying which schema produced a file.

    Uses the current project for context. Caller must parse the macro string
    into a ParsedMacro before creating this request.

    Pattern non-matches are returned as success with match_failure populated.
    Only true system errors (missing SecretsManager, etc.) return failure.

    Args:
        parsed_macro: Parsed macro template to match against
        file_path: Path string to test
        known_variables: Variables we already know

    Results: AttemptMatchPathAgainstMacroResultSuccess | AttemptMatchPathAgainstMacroResultFailure
    """

    parsed_macro: ParsedMacro
    file_path: str
    known_variables: MacroVariables


@dataclass
@PayloadRegistry.register
class AttemptMatchPathAgainstMacroResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Attempt completed (match succeeded or pattern didn't match).

    Check match_failure to determine outcome:
    - match_failure is None: Pattern matched, extracted_variables contains results
    - match_failure is not None: Pattern didn't match (normal case, not an error)
    """

    extracted_variables: MacroVariables | None
    match_failure: MacroMatchFailure | None


@dataclass
@PayloadRegistry.register
class AttemptMatchPathAgainstMacroResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """System error occurred (missing SecretsManager, invalid configuration, etc.)."""


@dataclass
@PayloadRegistry.register
class GetStateForMacroRequest(RequestPayload):
    """Analyze a macro and return comprehensive state information.

    Use when: Building UI forms, real-time validation, checking if resolution
    would succeed before actually resolving.

    Uses the current project for context. Caller must parse the macro string
    into a ParsedMacro before creating this request.

    Args:
        parsed_macro: The parsed macro to analyze
        variables: Currently provided variable values

    Results: GetStateForMacroResultSuccess | GetStateForMacroResultFailure
    """

    parsed_macro: ParsedMacro
    variables: MacroVariables


@dataclass
@PayloadRegistry.register
class GetStateForMacroResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Macro state analysis completed successfully.

    Args:
        all_variables: All variables found in the macro
        satisfied_variables: Variables that have values (from user, directories, or builtins)
        missing_required_variables: Required variables that are missing values
        conflicting_variables: Variables that conflict (e.g., user overriding builtin with different value)
        can_resolve: Whether the macro can be fully resolved (no missing required vars, no conflicts)
    """

    all_variables: set[VariableInfo]
    satisfied_variables: set[str]
    missing_required_variables: set[str]
    conflicting_variables: set[str]
    can_resolve: bool


@dataclass
@PayloadRegistry.register
class GetStateForMacroResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Macro state analysis failed.

    Failure occurs when:
    - No current project is set
    - Current project template is not loaded
    - A builtin variable cannot be resolved (RuntimeError or NotImplementedError)
    """


@dataclass
@PayloadRegistry.register
class AttemptMapAbsolutePathToProjectRequest(RequestPayload):
    """Find out if an absolute path exists anywhere within a Project directory.

    Use when: User selects or types an absolute path via FilePicker and you need to know:
      1. Is this path inside any project directory?
      2. If yes, what's the macro form (e.g., {outputs}/file.png)?

    This enables automatic conversion of absolute paths to portable macro form for workflow portability.

    Uses longest prefix matching to find the most specific directory match.
    Returns Success with mapped_path if inside project, or Success with None if outside.
    Returns Failure if operation cannot be performed (no project loaded, secrets unavailable).

    Args:
        absolute_path: The absolute filesystem path to check

    Results: AttemptMapAbsolutePathToProjectResultSuccess | AttemptMapAbsolutePathToProjectResultFailure

    Examples:
        Path inside project directory:
            Request: absolute_path = /Users/james/project/outputs/renders/image.png
            Result: mapped_path = "{outputs}/renders/image.png"

        Path outside project:
            Request: absolute_path = /Users/james/Downloads/image.png
            Result: mapped_path = None

        Path at directory root:
            Request: absolute_path = /Users/james/project/outputs
            Result: mapped_path = "{outputs}"
    """

    absolute_path: Path


@dataclass
@PayloadRegistry.register
class AttemptMapAbsolutePathToProjectResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Path check completed successfully.

    Success means the check was performed (not necessarily that a match was found).
    - mapped_path is NOT None: Path is inside a project directory (macro form returned)
    - mapped_path is None: Path is outside all project directories (valid answer)

    Args:
        mapped_path: The macro form if path is inside a project directory (e.g., "{outputs}/file.png"),
                    or None if path is outside all project directories

    Examples:
        Path inside project:
            mapped_path = "{outputs}/renders/image.png"
            result_details = "Successfully mapped absolute path to '{outputs}/renders/image.png'"

        Path outside project:
            mapped_path = None
            result_details = "Attempted to map absolute path '/Users/james/Downloads/image.png'. Path is outside all project directories"
    """

    mapped_path: str | None


@dataclass
@PayloadRegistry.register
class AttemptMapAbsolutePathToProjectResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Path mapping attempt failed.

    Returned when the operation cannot be performed (no current project, secrets manager unavailable).
    This is distinct from "path is outside project" which returns Success with None values.

    Examples:
        No current project:
            result_details = "Attempted to map absolute path. Failed because no current project is set"

        Secrets manager unavailable:
            result_details = "Attempted to map absolute path. Failed because SecretsManager not available"
    """


@dataclass
@PayloadRegistry.register
class UnregisterProjectTemplateRequest(RequestPayload):
    """Remove a registered project template from the engine.

    Removes the template from in-memory caches and from the persisted
    projects_to_register config list so it is not reloaded on restart.

    If the template is currently active, the current project is cleared.

    Use when: User wants to remove a stale or unwanted project template reference.

    Args:
        project_id: Identifier of the project template to unregister

    Results: UnregisterProjectTemplateResultSuccess | UnregisterProjectTemplateResultFailure
    """

    project_id: str


@dataclass
@PayloadRegistry.register
class UnregisterProjectTemplateResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Project template unregistered successfully."""


@dataclass
@PayloadRegistry.register
class UnregisterProjectTemplateResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Project template unregistration failed.

    Common causes:
    - project_id not found in registered templates
    """


@dataclass
@PayloadRegistry.register
class GetAllSituationsForProjectRequest(RequestPayload):
    """Get all situation names and schemas from current project template."""


@dataclass
@PayloadRegistry.register
class GetAllSituationsForProjectResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Success result containing all situations."""

    situations: dict[str, str]


@dataclass
@PayloadRegistry.register
class GetAllSituationsForProjectResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Failure result when cannot get situations."""


@dataclass
@PayloadRegistry.register
class ActivateWorkspaceProjectRequest(RequestPayload):
    """Resolve and activate the workspace project before app initialization completes.

    Emitted by the app orchestrator after role setup but before the
    AppInitializationComplete broadcast, mirroring the CLI executor which loads
    its --project-file-path before broadcasting. Establishing the project's
    config/workspace/env layers first ensures LibraryManager loads libraries
    against the correct workspace (and enforces the project's engine_version and
    library pins) instead of the default workspace.
    """


@dataclass
@PayloadRegistry.register
class ActivateWorkspaceProjectResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Workspace project activated, or no workspace project found (a no-op is success)."""


@dataclass
@PayloadRegistry.register
class ActivateWorkspaceProjectResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Workspace project activation failed.

    Boot is soft: the app logs this and continues so the engine still starts and
    the user can switch to a working project.
    """
