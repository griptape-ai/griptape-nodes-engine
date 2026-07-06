"""Events for project template management."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

# Runtime import (not TYPE_CHECKING): the Path-typed request fields below rely on
# cattrs coercing wire-form strings to Path. cattrs resolves field types via
# get_type_hints, which needs Path importable at runtime; under TYPE_CHECKING it
# raises NameError and cattrs silently skips coercion, handing handlers raw strings.
from pathlib import Path  # noqa: TC003
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
    from griptape_nodes.common.sequences.models import Sequence

    # Circular import: os_events already imports MacroPath from this module, so the
    # sequence-scan failure enums come in under TYPE_CHECKING to keep runtime imports
    # one-way. `from __future__ import annotations` (top of file) preserves the
    # runtime type hints for dataclass fields without requiring the enums.
    from griptape_nodes.retained_mode.events.os_events import FileIOFailureReason, SequenceScanFailureReason

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


class UnresolvedSequenceSlotBehavior(StrEnum):
    """How ``GetPathForMacroRequest`` handles a REQUIRED, unresolved sequence slot.

    A "sequence slot" is a variable whose ``format_specs`` contains a
    ``SequenceFormat`` — emitted by ``{###}`` / ``{###?}`` shorthand in the
    macro template. Optional slots (``{###?}``) that aren't bound are always
    omitted by the resolver, so this enum only takes effect on a required
    ``{###}`` slot with no value in the ``variables`` dict.

    Values:
        FAIL — default. Return ``GetPathForMacroResultFailure`` with
            ``MISSING_REQUIRED_VARIABLES``. This is what the write path (via
            ``on_write_file_request``) wants: the failure is the signal the
            seed-and-retry logic uses to auto-allocate the first index.
        RENDER_SEQUENCE_PATTERN — render the slot as its bare hash glyphs
            (``###``, ``####``, ...) into the resolved path — the ffmpeg /
            Houdini / Nuke convention that reads universally as "digits go
            here." **Presentation only.** The output previews the eventual
            on-disk shape of the path (a saved file becomes e.g.
            ``render_v001.png``); it is NOT a valid filesystem path itself
            and must not be opened, written, or handed to any I/O primitive.
            Use when showing users a preview of the macro shape (e.g. UI
            destination fields, path-classification previews).
        START_AT_ZERO — seed the slot with ``0`` and render it (``000``
            at min_width=3). Useful for 0-indexed preview flows.
        START_AT_ONE — seed the slot with ``1`` and render it (``001`` at
            min_width=3). Matches the write-path seed's starting value,
            so this is the right choice when previewing "what would my
            first save land at" (assuming the destination is empty).
    """

    FAIL = "FAIL"
    RENDER_SEQUENCE_PATTERN = "RENDER_SEQUENCE_PATTERN"
    START_AT_ZERO = "START_AT_ZERO"
    START_AT_ONE = "START_AT_ONE"


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
        project_id: The opaque id for the loaded project (the registry key).
            Echoed back so callers can activate/preview by id rather than path.
            Consumers must not parse or construct it.
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
        project_id: Opaque id of the project (the registry key). Consumers must
            not parse or construct it.

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
@PayloadRegistry.register
class ResolveProjectWorkspaceRequest(RequestPayload):
    """Resolve the workspace directory a project would use, WITHOUT loading/activating it.

    Use when: Showing a project's effective workspace in the detail view when the project does not
    declare its own workspace_dir, so the user can see where its workspace would land.

    Args:
        project_id: Opaque id of the project (the registry key). Consumers must not parse it.

    Results: ResolveProjectWorkspaceResultSuccess
    """

    project_id: ProjectID


@dataclass
@PayloadRegistry.register
class ResolveProjectWorkspaceResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Resolved workspace directory for a project.

    Args:
        workspace_dir: Absolute path string the project would use, or None when the id resolves to
            no readable project file (matches the resolver's "nothing to resolve" contract).
    """

    workspace_dir: str | None = None


@dataclass
class ProjectTemplateInfo:
    """Information about a loaded or failed project template.

    Fields:
        project_id: The opaque id identifying this template in the registry.
            Consumers must not parse or construct it. Legacy projects with no id
            use their canonical file path string as the id (the legacy bridge).
        validation: Outcome of loading + parsing this template.
        name: Display name from the template body, when available.
        project_file_path: Canonical file path locating this template on disk, or
            None for templates that are not file-backed (e.g. the system
            defaults). Carried separately from project_id so consumers never have
            to assume the id is a path.
        parent_project_id: The parent's id, suitable for direct equality matching
            against another entry's project_id when reconstructing the
            parent/child hierarchy. For a legacy child linked by
            parent_project_path, this is resolved to the parent's id (its
            canonical path string when the parent itself is legacy). None means no
            parent (system defaults are the only base).
        engine_version_compatible: False when the project's project-adjacent
            config declares a `requires_engine` specifier the running engine
            fails (or that is malformed). The GUI disables activation for such a
            project. True when compatible or when no requires_engine is declared.
        required_engine_version: The declared `requires_engine` specifier, when any.
        current_engine_version: The running engine version, for display.
        engine_version_reason: Human-readable detail explaining an incompatibility,
            None when compatible.
    """

    project_id: ProjectID
    validation: ProjectValidationInfo
    name: str | None = None
    project_file_path: str | None = None
    parent_project_id: str | None = None
    engine_version_compatible: bool = True
    required_engine_version: str | None = None
    current_engine_version: str | None = None
    engine_version_reason: str | None = None


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
        unresolved_sequence_slot_behavior: How to handle a required sequence
            slot (``{###}``) with no value bound. Defaults to ``FAIL`` — the
            write-path contract. Preview / display callers should pass
            ``RENDER_SEQUENCE_PATTERN`` so the slot renders as its source
            pattern instead of failing. See ``UnresolvedSequenceSlotBehavior``.

    Results: GetPathForMacroResultSuccess | GetPathForMacroResultFailure
    """

    parsed_macro: ParsedMacro
    variables: MacroVariables
    unresolved_sequence_slot_behavior: UnresolvedSequenceSlotBehavior = UnresolvedSequenceSlotBehavior.FAIL


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


class ScanSituationSequenceFailureReason(StrEnum):
    """Why a ``ScanSituationSequenceRequest`` could not produce a sequence view.

    Separated by stage so callers can distinguish a wiring problem (situation not
    registered, macro didn't parse) from a data-layer problem (directory
    unreadable, invalid template) without parsing detail strings.
    """

    SITUATION_NOT_FOUND = "situation_not_found"  # `GetSituationRequest` failed (missing situation, missing project).
    MACRO_PARSE_ERROR = "macro_parse_error"  # Situation's stored macro string did not parse.
    MACRO_RESOLUTION_ERROR = "macro_resolution_error"  # `GetPathForMacroRequest` failed to render the pattern.
    SCAN_FAILED = "scan_failed"  # Underlying `ScanSequencesRequest` failed; see `scan_failure_reason`.


@dataclass
@PayloadRegistry.register
class ScanSituationSequenceRequest(RequestPayload):
    """Programmatic scan: enumerate on-disk files for a situation given a variables bag.

    Use when: You already have a variables bag (from your own state, from user
    input in a search UI, or from a prior ``ListRelatedProjectFilesRequest``) and
    want to list on-disk files the situation's macro produces for it. If instead
    you're starting from a *filename* and want files a related situation produced
    for it, use ``ListRelatedProjectFilesRequest`` — that handler derives the
    bag via reverse-match before delegating here.

    The handler composes ``GetSituationRequest`` → ``GetPathForMacroRequest``
    (with ``UnresolvedSequenceSlotBehavior.RENDER_SEQUENCE_PATTERN``) →
    ``ScanSequencesRequest`` (with ``policy=SKIP``, ``no_token_behavior=SINGLE_FILE``).
    Situations whose macro has no ``{###}`` slot resolve to a literal path and
    return 0 or 1 entries — same shape as any empty scan.

    The sequence slot (``_index``) must remain unbound in ``variables`` so
    ``RENDER_SEQUENCE_PATTERN`` can render it as ``####`` for the scanner.

    Args:
        situation_name: Situation to look up (e.g. ``BuiltInSituation.SAVE_WORKFLOW_BACKUP``).
        variables: Variables bag to bind against the situation's macro. All
            required non-sequence variables must be present; the sequence key
            (if the macro has a sequence slot) must be absent.

    Results: ScanSituationSequenceResultSuccess (sequence + rendered pattern) |
        ScanSituationSequenceResultFailure (see ``failure_reason`` for which stage failed).
    """

    situation_name: str
    variables: MacroVariables


@dataclass
@PayloadRegistry.register
class ScanSituationSequenceResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Successful scan of a situation's sequence.

    Attributes:
        sequence: On-disk entries for the resolved sequence pattern. ``None`` when
            nothing on disk matched — a legitimate result, not a failure.
        pattern: Rendered fileseq pattern (e.g. ``/ws/backups/wf_backup_v####.py``)
            that was scanned. Preserved so callers can log or re-scan the same shape.
    """

    sequence: Sequence | None
    pattern: str

    @property
    def present_numbers(self) -> set[int]:
        """Empty set when the sequence was empty — safe to use directly for retention math."""
        if self.sequence is None:
            return set()
        return self.sequence.present_numbers


@dataclass
@PayloadRegistry.register
class ScanSituationSequenceResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """A stage of ``ScanSituationSequenceRequest`` failed. See ``failure_reason``.

    Attributes:
        failure_reason: Which stage failed (situation lookup, macro parse,
            macro resolution, or the underlying scan).
        pattern: Rendered pattern when the failure happened after render (e.g.
            ``SCAN_FAILED``), else ``None``. Useful for log lines identifying
            which pattern the scanner rejected.
        scan_failure_reason: When ``failure_reason == SCAN_FAILED``, the
            underlying ``ScanSequencesRequest`` failure reason. ``None`` for
            every other stage.
    """

    failure_reason: ScanSituationSequenceFailureReason
    pattern: str | None = None
    scan_failure_reason: SequenceScanFailureReason | FileIOFailureReason | None = None


class ListRelatedProjectFilesFailureReason(StrEnum):
    """Why a ``ListRelatedProjectFilesRequest`` could not produce a related-files view.

    One reason per pipeline stage so callers can act on the specific problem
    without parsing detail strings.
    """

    # Absolute source_filename couldn't be mapped to a project directory
    # (`AttemptMapAbsolutePathToProjectRequest` failed or returned no mapping).
    PATH_MAP_FAILED = "path_map_failed"
    # Reverse-match against source_situation's macro failed — source_filename
    # doesn't fit the shape that source_situation would have produced.
    SOURCE_MACRO_MISMATCH = "source_macro_mismatch"
    # Underlying `ScanSituationSequenceRequest` failed; see `scan_failure_reason`
    # for the specific downstream stage.
    SCAN_FAILED = "scan_failed"


@dataclass
@PayloadRegistry.register
class ListRelatedProjectFilesRequest(RequestPayload):
    """List on-disk files that one situation produced *for* a file another situation produced.

    Use when: You have a filename that some source situation wrote (a saved
    workflow, a saved node output, etc.) and you want to enumerate all the
    on-disk files a *related* target situation produces for that same source.
    The handler reverse-matches the source filename against the source
    situation's macro to derive the variables bag, then scans the target
    situation's macro for on-disk files sharing that bag.

    Five example call shapes (backups, versions, node-output history,
    cross-situation preview, cross-situation sidecar metadata)::

        # 1. List the backups of a workflow.
        await handle_request(ListRelatedProjectFilesRequest(
            source_filename="{workspace_dir}/episodes/scene_one.py",
            source_situation=BuiltInSituation.SAVE_WORKFLOW,
            target_situation=BuiltInSituation.SAVE_WORKFLOW_BACKUP,
        ))
        # → sequence lists scene_one_backup_v001.py, scene_one_backup_v002.py, ...
        # → source_variables = {"file_name_base": "scene_one",
        #                       "file_extension": "py",
        #                       "sub_dirs": "episodes"}

        # 2. List all versioned saves of a workflow (absolute path also OK).
        await handle_request(ListRelatedProjectFilesRequest(
            source_filename="/abs/path/to/workspace/wf.py",
            source_situation=BuiltInSituation.SAVE_WORKFLOW,
            target_situation=BuiltInSituation.CREATE_VERSIONED_WORKFLOW,
        ))
        # → sequence lists wf_v001.py, wf_v002.py, ...

        # 3. List a node's past outputs sharing a name (self-related — the same
        #    situation for source and target: "give me all numbered siblings").
        await handle_request(ListRelatedProjectFilesRequest(
            source_filename="{outputs}/render.png",
            source_situation=BuiltInSituation.SAVE_NODE_OUTPUT,
            target_situation=BuiltInSituation.SAVE_NODE_OUTPUT,
        ))
        # → sequence lists render_v001.png, render_v002.png, ...

        # 4. Find the preview file for a saved output (cross-situation).
        await handle_request(ListRelatedProjectFilesRequest(
            source_filename="{outputs}/render.png",
            source_situation=BuiltInSituation.SAVE_NODE_OUTPUT,
            target_situation=BuiltInSituation.SAVE_GRIPTAPE_NODES_PREVIEW,
        ))
        # → sequence lists the preview file(s) for render.png (usually 0 or 1).

        # 5. Find the sidecar metadata for a project file (cross-situation).
        await handle_request(ListRelatedProjectFilesRequest(
            source_filename="{workspace_dir}/foo.py",
            source_situation=BuiltInSituation.SAVE_WORKFLOW,
            target_situation=BuiltInSituation.SAVE_GRIPTAPE_NODES_METADATA,
        ))
        # → sequence points at foo.py's sidecar JSON (0 or 1 entry).

    Args:
        source_filename: The file to look up. Macro-form ("{workspace_dir}/foo.py")
            or a plain absolute path. Absolute paths are converted to macro form
            via ``AttemptMapAbsolutePathToProjectRequest`` internally.
        source_situation: The situation whose macro should be reverse-matched
            against ``source_filename`` to derive the variables bag. Typically
            the situation that *wrote* ``source_filename``.
        target_situation: The situation whose macro to scan for on-disk files
            matching the derived bag.

    Results: ListRelatedProjectFilesResultSuccess (sequence + source_variables) |
        ListRelatedProjectFilesResultFailure (see ``failure_reason`` for which stage failed).
    """

    source_filename: str
    source_situation: str
    target_situation: str


@dataclass
@PayloadRegistry.register
class ListRelatedProjectFilesResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Related-files listing produced successfully.

    Attributes:
        sequence: On-disk entries for the target situation's macro, keyed by
            the reverse-matched source variables. ``None`` when nothing on disk
            matched — a legitimate result, not a failure.
        source_variables: The variables bag reverse-matched from
            ``source_filename`` against ``source_situation``'s macro. Callers
            doing a follow-on write (e.g. a backup write after listing existing
            backups) can hand this bag to
            ``ProjectFileDestination.from_situation_with_variables`` verbatim.
    """

    sequence: Sequence | None
    source_variables: MacroVariables


@dataclass
@PayloadRegistry.register
class ListRelatedProjectFilesResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """A stage of ``ListRelatedProjectFilesRequest`` failed. See ``failure_reason``.

    Attributes:
        failure_reason: Which pipeline stage failed. See ``ListRelatedProjectFilesFailureReason``.
        scan_failure_reason: When ``failure_reason == SCAN_FAILED``, the
            downstream ``ScanSituationSequenceRequest`` failure reason.
            ``None`` for every other stage.
    """

    failure_reason: ListRelatedProjectFilesFailureReason
    scan_failure_reason: ScanSituationSequenceFailureReason | None = None


@dataclass
@PayloadRegistry.register
class SetCurrentProjectRequest(RequestPayload):
    """Set which project user has currently selected.

    Use when: User switches between projects, opens a new workspace.

    If the workspace directory changes as a result of setting the project,
    and startup is complete, this handler automatically reloads all libraries
    and re-registers workflows from config and the new workspace.

    Args:
        project_id: Opaque id of the project to set as current (matched verbatim
            against the registry; not parsed as a path). None lands the engine on
            the system defaults rather than a "no project" state.

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
        project_id: Opaque id of the project template to unregister (the registry
            key). Consumers must not parse or construct it.

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


@dataclass
@PayloadRegistry.register
class ExportProjectRequest(RequestPayload):
    """Package a loaded project and its dependencies into a portable .zip.

    Use when: User wants to archive or branch an entire project. The package
    carries the project template, its adjacent config, workflow files, and on-disk
    assets, plus a true copy of any register-only local libraries. Git-sourced
    libraries (libraries_to_download) travel by reference and are re-downloaded on
    import. Required secret KEY NAMES travel in the manifest; secret VALUES never
    leave the machine and .env is never copied.

    Args:
        project_id: Opaque id of the loaded project to export (the registry key).
            Consumers must not parse or construct it.
        destination_path: Full path to the .zip file to create.

    Results: ExportProjectResultSuccess | ExportProjectResultFailure
    """

    project_id: ProjectID
    destination_path: Path


@dataclass
@PayloadRegistry.register
class ExportProjectResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Project exported successfully.

    Args:
        archive_path: Path to the written .zip.
        referenced_libraries: Names (or git urls) of libraries shipped by reference.
        copied_libraries: Registered paths of local libraries true-copied into the zip.
        required_secret_keys: Names of secrets the project needs (NO values).
        warnings: Non-fatal issues encountered, e.g. a registered local library
            whose source was missing on disk and could not be packaged.
    """

    archive_path: Path
    referenced_libraries: list[str]
    copied_libraries: list[str]
    required_secret_keys: list[str]
    warnings: list[str]


@dataclass
@PayloadRegistry.register
class ExportProjectResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Project export failed.

    Common causes:
    - project_id not loaded
    - project has no backing file (e.g. system defaults)
    - destination parent directory missing or unwritable
    """


@dataclass
@PayloadRegistry.register
class PreviewImportProjectRequest(RequestPayload):
    """Read a project package's manifest without extracting it.

    Use when: The GUI wants to show what a .zip contains (libraries, required
    secret keys) and which required secrets are unset in the target environment,
    before committing to an import. Read-only: no files are written.

    Args:
        archive_path: Path to the project package .zip to inspect.

    Results: PreviewImportProjectResultSuccess | PreviewImportProjectResultFailure
    """

    archive_path: Path


@dataclass
@PayloadRegistry.register
class PreviewImportProjectResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Package manifest read successfully.

    Args:
        manifest: The parsed manifest.json (provenance + flat library/secret summary).
        unset_secret_keys: Required secret keys with no value in the target
            environment (computed without writing anything).
    """

    manifest: dict[str, Any]
    unset_secret_keys: list[str]


@dataclass
@PayloadRegistry.register
class PreviewImportProjectResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Package preview failed.

    Common causes:
    - archive missing or not a zip
    - manifest.json absent
    - incompatible major manifest_schema_version
    """


@dataclass
@PayloadRegistry.register
class ImportProjectRequest(RequestPayload):
    """Extract a project package to a target directory and register it.

    Use when: User archives or branches a project. The mirrored base-dir tree is
    extracted 1:1 at the target, so {inputs}/{outputs}/etc. macro paths re-resolve
    against the new location automatically. Git-sourced libraries re-provision on
    activation; copied local libraries register from their package-relative path.
    Secrets are never auto-created: required/unset keys are returned for the GUI
    to prompt.

    Args:
        archive_path: Path to the project package .zip.
        target_directory: Directory to extract the project into.
        new_project_name: When set, renames the imported project (duplicate/branch).
        set_as_current: When True, activates the imported project after import.
        overwrite_existing: When True, allows extracting over an existing project
            file in target_directory; otherwise that collision fails.

    Results: ImportProjectResultSuccess | ImportProjectResultFailure
    """

    archive_path: Path
    target_directory: Path
    new_project_name: str | None = None
    set_as_current: bool = False
    overwrite_existing: bool = False


@dataclass
@PayloadRegistry.register
class ImportProjectResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Project imported and registered successfully.

    Args:
        project_id: Opaque id of the newly registered project (the registry key).
        project_file_path: Path to the extracted project template file.
        required_secret_keys: Names of secrets the project needs (NO values).
        unset_secret_keys: Required secret keys with no value in the current
            environment, for the GUI to prompt.
        warnings: Non-fatal issues recorded in the package manifest.
    """

    project_id: ProjectID
    project_file_path: Path
    required_secret_keys: list[str]
    unset_secret_keys: list[str]
    warnings: list[str]


@dataclass
@PayloadRegistry.register
class ImportProjectResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Project import failed.

    Common causes:
    - archive missing / not a zip / manifest absent / incompatible schema
    - target project file already exists and overwrite_existing is False
    - extraction or re-registration failed
    """


@dataclass
@PayloadRegistry.register
class UpgradeProjectSchemaRequest(RequestPayload):
    """Electively upgrade a loaded project to the latest schema MAJOR version.

    A within-major version advance happens automatically on save; crossing a MAJOR
    (e.g. 0.x -> 1.0.0) does not, because the new major carries a different defaults
    baseline that can change where the project resolves its workspace, libraries, and
    file destinations. This request performs that crossing explicitly: it re-reads the
    project's explicit overrides (its own overlay, not the materialized old-major
    defaults), restamps to the latest version, and re-merges onto the new-major base, so
    the project ADOPTS the new-major defaults for everything it did not explicitly override.

    BREAKING: this is opt-in and may change the project's effective layout. Only the
    project's explicit overrides are preserved; previously-default values are dropped so
    they pick up the new-major defaults (it does not pin old behavior).

    Use when: the user explicitly chooses to upgrade an outdated project.

    Args:
        project_id: Opaque id of the loaded project template to upgrade.

    Results: UpgradeProjectSchemaResultSuccess | UpgradeProjectSchemaResultFailure
    """

    project_id: str


@dataclass
@PayloadRegistry.register
class UpgradeProjectSchemaResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Project upgraded to the latest schema major.

    Args:
        project_id: The upgraded project's id.
        previous_schema_version: The schema version before the upgrade.
        new_schema_version: The schema version written (the latest).
    """

    project_id: str
    previous_schema_version: str
    new_schema_version: str


@dataclass
@PayloadRegistry.register
class UpgradeProjectSchemaResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Project schema upgrade failed.

    Common causes:
    - project_id not loaded
    - project has no backing file (e.g. system defaults)
    - already at the latest major
    - save/write failure
    """
