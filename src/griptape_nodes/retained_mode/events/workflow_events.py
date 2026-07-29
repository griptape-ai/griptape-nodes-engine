from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel

from griptape_nodes.node_library.workflow_registry import WorkflowMetadata, WorkflowShape
from griptape_nodes.retained_mode.events.base_events import (
    RequestPayload,
    ResultPayloadFailure,
    ResultPayloadSuccess,
    WorkflowAlteredMixin,
    WorkflowNotAlteredMixin,
)
from griptape_nodes.retained_mode.events.execution_events import ExecutionPayload
from griptape_nodes.retained_mode.events.payload_registry import PayloadRegistry

if TYPE_CHECKING:
    # Circular import: flow_events <-> workflow_events
    from griptape_nodes.retained_mode.events.flow_events import SerializedFlowCommands


@dataclass
@PayloadRegistry.register
class RunWorkflowFromScratchRequest(RequestPayload):
    """Run a workflow from file, starting with a clean state.

    Use when: Loading and executing saved workflows, testing workflows from files,
    running workflows in clean environments, batch processing workflows.

    Args:
        file_path: Path to the workflow file to load and execute

    Results: RunWorkflowFromScratchResultSuccess | RunWorkflowFromScratchResultFailure (file not found, load error)
    """

    file_path: str


@dataclass
@PayloadRegistry.register
class RunWorkflowFromScratchResultSuccess(ResultPayloadSuccess):
    """Workflow loaded and started successfully from file."""


@dataclass
@PayloadRegistry.register
class RunWorkflowFromScratchResultFailure(ResultPayloadFailure):
    """Workflow execution from file failed. Common causes: file not found, invalid workflow format, load error."""


@dataclass
@PayloadRegistry.register
class RunWorkflowWithCurrentStateRequest(RequestPayload):
    """Run a workflow from file, preserving current state.

    Use when: Loading workflows while keeping existing node values, updating workflow structure
    without losing progress, iterative workflow development.

    Args:
        file_path: Path to the workflow file to load while preserving current state

    Results: RunWorkflowWithCurrentStateResultSuccess | RunWorkflowWithCurrentStateResultFailure (file not found, merge error)
    """

    file_path: str


@dataclass
@PayloadRegistry.register
class RunWorkflowWithCurrentStateResultSuccess(WorkflowAlteredMixin, ResultPayloadSuccess):
    """Workflow loaded successfully while preserving current state."""


@dataclass
@PayloadRegistry.register
class RunWorkflowWithCurrentStateResultFailure(ResultPayloadFailure):
    """Workflow execution with current state failed. Common causes: file not found, state merge conflict, load error."""


@dataclass
@PayloadRegistry.register
class RunWorkflowFromRegistryRequest(RequestPayload):
    """Run a workflow from the registry.

    Use when: Executing registered workflows, running workflows by name,
    using workflow templates, automated workflow execution.

    Args:
        workflow_name: Name of the workflow in the registry to execute
        run_with_clean_slate: Whether to start with a clean state (default: True)

    Results: RunWorkflowFromRegistryResultSuccess | RunWorkflowFromRegistryResultFailure (workflow not found, execution error)
    """

    workflow_name: str
    run_with_clean_slate: bool = True


@dataclass
@PayloadRegistry.register
class RunWorkflowFromRegistryResultSuccess(ResultPayloadSuccess):
    """Workflow from registry started successfully."""


@dataclass
@PayloadRegistry.register
class RunWorkflowFromRegistryResultFailure(ResultPayloadFailure):
    """Workflow execution from registry failed. Common causes: workflow not found, execution error, registry error."""


@dataclass
@PayloadRegistry.register
class RegisterWorkflowRequest(RequestPayload):
    """Register a workflow in the registry.

    Use when: Publishing workflows for reuse, creating workflow templates,
    managing workflow libraries, making workflows available by name.

    Args:
        metadata: Workflow metadata containing name, description, and other properties
        file_name: Name of the workflow file to register

    Results: RegisterWorkflowResultSuccess (with workflow name) | RegisterWorkflowResultFailure (registration error)
    """

    metadata: WorkflowMetadata
    file_name: str


@dataclass
@PayloadRegistry.register
class RegisterWorkflowResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Workflow registered successfully.

    Args:
        workflow_name: Name assigned to the registered workflow
    """

    workflow_name: str


@dataclass
@PayloadRegistry.register
class RegisterWorkflowResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Workflow registration failed. Common causes: invalid metadata, file not found, name conflict."""


@dataclass
@PayloadRegistry.register
class ImportWorkflowRequest(RequestPayload):
    """Import and register a workflow from a file.

    Use when: Importing workflows from external sources, batch workflow imports,
    command-line workflow registration, loading workflows from shared locations.

    Args:
        file_path: Path to the workflow file to import and register

    Results: ImportWorkflowResultSuccess (with workflow name) | ImportWorkflowResultFailure (import error)
    """

    file_path: str


@dataclass
@PayloadRegistry.register
class ImportWorkflowResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Workflow imported and registered successfully.

    Args:
        workflow_name: Name of the imported workflow
    """

    workflow_name: str


@dataclass
@PayloadRegistry.register
class ImportWorkflowResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Workflow import failed. Common causes: file not found, invalid workflow format, metadata extraction error, registration error."""


@dataclass
@PayloadRegistry.register
class ListAllWorkflowsRequest(RequestPayload):
    """List all workflows in the registry.

    Use when: Displaying workflow catalogs, browsing available workflows,
    implementing workflow selection UIs, workflow management.

    Results: ListAllWorkflowsResultSuccess (with workflows dict) | ListAllWorkflowsResultFailure (registry error)
    """


@dataclass
@PayloadRegistry.register
class ListAllWorkflowsResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Workflows listed successfully.

    Args:
        workflows: Dictionary of workflow names to metadata
    """

    workflows: dict


@dataclass
@PayloadRegistry.register
class ListAllWorkflowsResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Workflow listing failed. Common causes: registry not initialized, registry error."""


@dataclass
@PayloadRegistry.register
class ListCallableWorkflowsRequest(RequestPayload):
    """List workflows that have a workflow_shape (i.e. contain StartFlow and EndFlow nodes)."""


@dataclass
@PayloadRegistry.register
class ListCallableWorkflowsResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    workflow_names: list[str]


@dataclass
@PayloadRegistry.register
class ListCallableWorkflowsResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Callable workflow listing failed."""


@dataclass
@PayloadRegistry.register
class DeleteWorkflowRequest(RequestPayload):
    """Delete a workflow from the registry.

    Use when: Removing obsolete workflows, cleaning up workflow libraries,
    unregistering workflows, workflow management.

    Args:
        name: Name of the workflow to delete from the registry

    Results: DeleteWorkflowResultSuccess | DeleteWorkflowResultFailure (workflow not found, deletion error)
    """

    name: str


@dataclass
@PayloadRegistry.register
class DeleteWorkflowResultSuccess(WorkflowAlteredMixin, ResultPayloadSuccess):
    """Workflow deleted successfully from registry."""


@dataclass
@PayloadRegistry.register
class DeleteWorkflowResultFailure(ResultPayloadFailure):
    """Workflow deletion failed. Common causes: workflow not found, deletion not allowed, registry error."""


class RenameDisplayNameBehavior(StrEnum):
    """Controls what happens to ``WorkflowMetadata.name`` (the human-facing display name) on rename.

    Rename is always a file-name operation; the display name is independent metadata. This enum
    lets callers decide whether the rename should touch it.

    Values:
        MATCH_FILE_NAME: Overwrite the display name with the (unsanitized) requested new name.
            Historical behavior and the default so on-the-wire parity with existing callers is
            preserved.
        PRESERVE_EXISTING: Leave the current display name untouched. Opt-in; the file moves but
            ``metadata.name`` stays put. Use this to fix the "renaming a workflow silently rewrites
            its display name" corruption path (engine #4992).
        OVERRIDE: Set the display name to the caller-supplied ``display_name`` value, independent
            of the file name. Requires ``display_name`` to be non-empty.
    """

    PRESERVE_EXISTING = "preserve_existing"
    MATCH_FILE_NAME = "match_file_name"
    OVERRIDE = "override"


@dataclass
@PayloadRegistry.register
class RenameWorkflowRequest(RequestPayload):
    """Rename a workflow in the registry.

    Use when: Updating workflow names, organizing workflow libraries,
    fixing naming conflicts, workflow management.

    Args:
        workflow_name: Current name of the workflow
        requested_name: New name for the workflow (drives the on-disk filename)
        display_name_behavior: How to treat ``WorkflowMetadata.name`` on rename. Defaults to
            ``MATCH_FILE_NAME`` to preserve historical wire behavior — callers who want the
            display name preserved on rename must opt in with ``PRESERVE_EXISTING``. See
            :class:`RenameDisplayNameBehavior`.
        display_name: New display name. Required (non-empty) when ``display_name_behavior`` is
            ``OVERRIDE``. MUST be ``None`` for the other behaviors — supplying it there is
            rejected up front to catch callers who set it thinking it will take effect.

    Results: RenameWorkflowResultSuccess | RenameWorkflowResultFailure (workflow not found, name conflict)
    """

    workflow_name: str
    requested_name: str
    display_name_behavior: RenameDisplayNameBehavior = RenameDisplayNameBehavior.MATCH_FILE_NAME
    display_name: str | None = None


@dataclass
@PayloadRegistry.register
class RenameWorkflowResultSuccess(WorkflowAlteredMixin, ResultPayloadSuccess):
    """Workflow renamed successfully.

    Args:
        new_workflow_name: The sanitized registry key of the renamed workflow (file stem).
    """

    new_workflow_name: str


@dataclass
@PayloadRegistry.register
class RenameWorkflowResultFailure(ResultPayloadFailure):
    """Workflow rename failed. Common causes: workflow not found, name already exists, invalid name."""


@dataclass
@PayloadRegistry.register
class SaveWorkflowRequest(RequestPayload):
    """Save the current workflow to a file.

    Use when: Persisting workflow changes, creating workflow backups,
    exporting workflows, saving before major changes.

    Args:
        file_name: Name of the file to save the workflow to (None for auto-generated)
        image_path: Path to save workflow image/thumbnail (None for no image)
        pickle_control_flow_result: Whether to use pickle-based serialization for control flow results (None for default behavior)
        display_name: Optional display name (metadata.name). If provided, overrides the existing display name instead of preserving it.
        create_versioned: When True, route the save through the ``create_versioned_workflow`` situation so each save produces a new versioned file (e.g. ``my_workflow_v001.py``, ``my_workflow_v002.py``, ...). When False (default), route through ``save_workflow``, which overwrites the existing file in place.
        allow_overwrite: When False and file_name would overwrite a different registered workflow, the save fails with WORKFLOW_CONFLICT. When True (default), proceed with overwrite. This flag only protects against overwriting OTHER workflows; saving over the current workflow always succeeds.

    Results: SaveWorkflowResultSuccess (with file path) | SaveWorkflowResultFailure (save error or workflow conflict)
    """

    file_name: str | None = None
    image_path: str | None = None
    pickle_control_flow_result: bool | None = None
    display_name: str | None = None
    create_versioned: bool = False
    allow_overwrite: bool = True


@dataclass
@PayloadRegistry.register
class ImportWorkflowAsReferencedSubFlowRequest(RequestPayload):
    """Import a workflow as a referenced sub-flow.

    Use when: Reusing workflows as components, creating modular workflows,
    importing workflow templates, building composite workflows.

    Results: ImportWorkflowAsReferencedSubFlowResultSuccess (with flow name) | ImportWorkflowAsReferencedSubFlowResultFailure (import error)
    """

    workflow_name: str
    flow_name: str | None = None  # If None, import into current context flow
    imported_flow_metadata: dict | None = None  # Metadata to apply to the imported flow
    track_as_referenced: bool = True  # If False, the flow serializes as inline content instead of an import command


@dataclass
@PayloadRegistry.register
class ImportWorkflowAsReferencedSubFlowResultSuccess(WorkflowAlteredMixin, ResultPayloadSuccess):
    """Workflow imported successfully as referenced sub-flow.

    Args:
        created_flow_name: Name of the created sub-flow
    """

    created_flow_name: str


@dataclass
@PayloadRegistry.register
class ImportWorkflowAsReferencedSubFlowResultFailure(ResultPayloadFailure):
    """Workflow import as sub-flow failed. Common causes: workflow not found, import error, name conflict."""


@dataclass
@PayloadRegistry.register
class SaveWorkflowResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Workflow saved successfully.

    Args:
        file_path: Path where the workflow was saved
        workflow_name: Registry key of the saved workflow, for use with workflow lookup requests
    """

    file_path: str
    workflow_name: str


@dataclass
@PayloadRegistry.register
class SaveWorkflowResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Workflow save failed. Common causes: file system error, permission denied, invalid path, workflow conflict."""

    failure_reason: str | None = None


@dataclass
@PayloadRegistry.register
class LoadWorkflowMetadata(RequestPayload):
    """Load workflow metadata from a file.

    Use when: Inspecting workflow properties, validating workflow files,
    displaying workflow information, workflow management.

    Args:
        file_name: Name of the workflow file to load metadata from

    Results: LoadWorkflowMetadataResultSuccess (with metadata) | LoadWorkflowMetadataResultFailure (load error)
    """

    file_name: str


@dataclass
@PayloadRegistry.register
class LoadWorkflowMetadataResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Workflow metadata loaded successfully.

    Args:
        metadata: Workflow metadata object
    """

    metadata: WorkflowMetadata


@dataclass
@PayloadRegistry.register
class LoadWorkflowMetadataResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Workflow metadata load failed. Common causes: file not found, invalid format, parse error."""


@dataclass
@PayloadRegistry.register
class GetWorkflowRunCommandRequest(RequestPayload):
    """Get a command-line string to run a workflow using the engine's Python.

    Use when: Showing users how to run a workflow from a terminal, copying a run command,
    scripting workflow execution from the command line.

    Provide workflow_name (from registry), file_path (path to workflow file), or neither to use
    the workflow in the current context.

    Args:
        workflow_name: Name of the workflow in the registry (optional if file_path or current context)
        file_path: Path to the workflow file, relative to workspace or absolute (optional if workflow_name or current context)

    Results: GetWorkflowRunCommandResultSuccess (with run_command) | GetWorkflowRunCommandResultFailure
    """

    workflow_name: str | None = None
    file_path: str | None = None


@dataclass
@PayloadRegistry.register
class GetWorkflowRunCommandResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    r"""Workflow run command retrieved successfully.

    Only returned when the workflow has Start and End nodes. When absent, the request fails with GetWorkflowRunCommandResultFailure.

    Args:
        run_command: Full command string; paths are quoted only when required (e.g. spaces), e.g. python.exe workflow.py or "C:\...\python.exe" "C:\...\workflow.py"
        workflow_shape: Input and output shape from StartNodes/EndNodes (inputs/outputs per node)
        engine_os: Platform StrEnum value where the engine is running (windows, darwin, linux)
    """

    run_command: str
    workflow_shape: WorkflowShape
    engine_os: str


@dataclass
@PayloadRegistry.register
class GetWorkflowRunCommandResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Workflow run command retrieval failed. Common causes: neither workflow_name nor file_path provided, workflow not found, file not found."""


class PublishFieldType(StrEnum):
    """The UI widget type for a publish dialog field."""

    DROPDOWN = "dropdown"
    TEXT = "text"
    FILE_PICKER = "file_picker"


class PublishOptionField(BaseModel):
    """Describes a single field to render in the publish dialog."""

    name: str
    label: str
    field_type: PublishFieldType
    tooltip: str = ""
    choices: list[str] | None = None
    default_value: str | None = None
    depends_on: str | None = None  # name of another field whose change triggers a re-fetch
    hidden: bool = False


@dataclass
@PayloadRegistry.register
class GetPublishOptionsRequest(RequestPayload):
    """Get publisher-specific options to display in the publish dialog before publishing.

    Use when: Opening the publish dialog for a specific publisher, refreshing dependent
    options after a selection change.

    Results: GetPublishOptionsResultSuccess (with fields) | GetPublishOptionsResultFailure
    """

    workflow_name: str
    publisher_name: str
    current_selections: dict | None = None  # current user selections; used for dependent field resolution


@dataclass
@PayloadRegistry.register
class GetPublishOptionsResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Publish options retrieved successfully.

    Args:
        fields: Ordered list of fields to render in the publish dialog
        title: Optional dialog title (e.g. "Update Published Gizmo"). None uses the frontend default.
        button_label: Optional publish button label (e.g. "Update"). None uses the frontend default.
    """

    fields: list[PublishOptionField]
    title: str | None = None
    button_label: str | None = None


@dataclass
@PayloadRegistry.register
class GetPublishOptionsResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Publish options retrieval failed."""


@dataclass
class PublishWorkflowRegisteredEventData:
    """Data specific to registering a PublishWorkflowRequest event handler.

    Args:
        start_flow_node_type: Node type used as the start of the published flow.
        start_flow_node_library_name: Library that provides the start node type.
        end_flow_node_type: Node type used as the end of the published flow.
        end_flow_node_library_name: Library that provides the end node type.
        get_publish_options: Optional callable returning custom publish options for this target.
        display_name: Optional human-readable name for the publishing target. When set, the GUI
            should show this in the publish menu/dialog instead of the raw library name. When None,
            the frontend falls back to the library name, preserving today's behavior.
        description: Optional short description of the publishing target. When set, the GUI may show
            it alongside the target in the publish menu/dialog (e.g. as a subtitle or tooltip). When
            None, no description is shown, preserving today's behavior.
        icon: Optional icon identifier for the publishing target. When set, the GUI may render it
            next to the target in the publish menu/dropdown. The value is either a Lucide icon name
            (e.g. "rocket") or a path/URL to an image the frontend renders as-is (e.g.
            "logos/my-target.svg" or "https://example.com/logo.png") — not raw image data. When
            None, the GUI uses its default/no icon, preserving today's behavior.
    """

    start_flow_node_type: str
    start_flow_node_library_name: str
    end_flow_node_type: str
    end_flow_node_library_name: str
    get_publish_options: Callable[["GetPublishOptionsRequest"], GetPublishOptionsResultSuccess] | None = None
    display_name: str | None = None
    description: str | None = None
    icon: str | None = None


@dataclass
@PayloadRegistry.register
class PublishWorkflowRequest(RequestPayload):
    """Publish a workflow for distribution.

    Use when: Sharing workflows with others, creating workflow packages,
    distributing workflow templates, workflow publishing.

    Results: PublishWorkflowResultSuccess (with file path) | PublishWorkflowResultFailure (publish error)
    """

    workflow_name: str
    publisher_name: str
    # This can be removed after GUI release
    execute_on_publish: bool | None = None
    published_workflow_file_name: str | None = None
    pickle_control_flow_result: bool = False
    metadata: dict | None = None


@dataclass
@PayloadRegistry.register
class PublishWorkflowResultSuccess(ResultPayloadSuccess):
    """Workflow published successfully.

    Args:
        published_workflow_file_path: Path to the published workflow file
    """

    published_workflow_file_path: str
    metadata: dict | None = None
    skip_published_workflow_registration: bool = False


@dataclass
@PayloadRegistry.register
class PublishWorkflowResultFailure(ResultPayloadFailure):
    """Workflow publish failed. Common causes: workflow not found, publish error, file system error."""


@dataclass
@PayloadRegistry.register
class PublishWorkflowProgressEvent(ExecutionPayload):
    """Event emitted to indicate progress during workflow publishing.

    Args:
        progress: Progress percentage (0-100)
        message: Optional progress message
    """

    progress: float
    message: str | None = None


@dataclass
@PayloadRegistry.register
class BranchWorkflowRequest(RequestPayload):
    """Create a branch (copy) of an existing workflow with branch tracking.

    Use when: Creating workflow variants, branching workflows for experimentation,
    creating personal copies of shared workflows, preparing for workflow collaboration.

    Args:
        workflow_name: Name of the workflow to branch
        branched_workflow_name: Name for the branched workflow (None for auto-generated)

    Results: BranchWorkflowResultSuccess (with branch name) | BranchWorkflowResultFailure (branch error)
    """

    workflow_name: str
    branched_workflow_name: str | None = None


@dataclass
@PayloadRegistry.register
class BranchWorkflowResultSuccess(WorkflowAlteredMixin, ResultPayloadSuccess):
    """Workflow branched successfully.

    Args:
        branched_workflow_name: Name of the created branch
        original_workflow_name: Name of the original workflow
    """

    branched_workflow_name: str
    original_workflow_name: str


@dataclass
@PayloadRegistry.register
class BranchWorkflowResultFailure(ResultPayloadFailure):
    """Workflow branch failed. Common causes: workflow not found, name conflict, save error."""


@dataclass
@PayloadRegistry.register
class CreateWorkflowFromTemplateRequest(RequestPayload):
    """Create a new workflow file from a template (Griptape-provided or user-provided).

    Use when: User selects a template from a list and wants to create a new workflow
    without opening the template first. Creates a copy in the workspace root with
    a unique name.

    Args:
        template_name: Registry name of the template workflow
        file_name: Base name for new file (None = use template stem)

    Results: CreateWorkflowFromTemplateResultSuccess (with workflow_name, file_path) | CreateWorkflowFromTemplateResultFailure
    """

    template_name: str
    file_name: str | None = None


@dataclass
@PayloadRegistry.register
class CreateWorkflowFromTemplateResultSuccess(WorkflowAlteredMixin, ResultPayloadSuccess):
    """Workflow created from template successfully.

    Args:
        workflow_name: Registry name of the created workflow
        file_path: Path where the workflow file was written
    """

    workflow_name: str
    file_path: str


@dataclass
@PayloadRegistry.register
class CreateWorkflowFromTemplateResultFailure(ResultPayloadFailure):
    """Create workflow from template failed. Common causes: template not found, not a template, file not found."""


@dataclass
@PayloadRegistry.register
class MergeWorkflowBranchRequest(RequestPayload):
    """Merge a branch back into its source workflow, removing the branch when complete.

    Use when: Integrating branch changes back into the original workflow, consolidating
    successful branch experiments, applying approved branch modifications to source.

    Args:
        workflow_name: Name of the branch workflow to merge back into its source

    Results: MergeWorkflowBranchResultSuccess (with merge details) | MergeWorkflowBranchResultFailure (merge error)
    """

    workflow_name: str


@dataclass
@PayloadRegistry.register
class MergeWorkflowBranchResultSuccess(WorkflowAlteredMixin, ResultPayloadSuccess):
    """Branch merge back to source completed successfully.

    Args:
        merged_workflow_name: Name of the source workflow after merge
    """

    merged_workflow_name: str


@dataclass
@PayloadRegistry.register
class MergeWorkflowBranchResultFailure(ResultPayloadFailure):
    """Workflow branch merge failed."""


@dataclass
@PayloadRegistry.register
class ResetWorkflowBranchRequest(RequestPayload):
    """Reset a branch to match its source workflow, discarding branch changes.

    Use when: Discarding branch modifications, reverting branch to source state,
    abandoning branch experiments, syncing branch with latest source changes.

    Args:
        workflow_name: Name of the branch workflow to reset to its source

    Results: ResetWorkflowBranchResultSuccess (with reset details) | ResetWorkflowBranchResultFailure (reset error)
    """

    workflow_name: str


@dataclass
@PayloadRegistry.register
class ResetWorkflowBranchResultSuccess(WorkflowAlteredMixin, ResultPayloadSuccess):
    """Branch reset to source completed successfully.

    Args:
        reset_workflow_name: Name of the branch workflow after reset
    """

    reset_workflow_name: str


@dataclass
@PayloadRegistry.register
class ResetWorkflowBranchResultFailure(ResultPayloadFailure):
    """Workflow branch reset failed. Common causes: workflows not branch-related, reset conflict, save error."""


@dataclass
@PayloadRegistry.register
class CompareWorkflowsRequest(RequestPayload):
    """Compare two workflows to determine if one is ahead, behind, or up-to-date relative to the other.

    Use when: Checking if branched workflows need updates, determining if local changes exist,
    managing workflow synchronization, preparing for merge operations.

    Args:
        workflow_name: Name of the workflow to evaluate
        compare_workflow_name: Name of the workflow to compare against

    Results: CompareWorkflowsResultSuccess (with status details) | CompareWorkflowsResultFailure (evaluation error)
    """

    workflow_name: str
    compare_workflow_name: str


@dataclass
@PayloadRegistry.register
class CompareWorkflowsResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Workflow comparison completed successfully.

    Args:
        workflow_name: Name of the evaluated workflow
        compare_workflow_name: Name of the workflow being compared against (if any)
        status: Status relative to source - "up_to_date", "ahead", "behind", "diverged", or "no_source"
        workflow_last_modified: Last modified timestamp of the workflow
        source_last_modified: Last modified timestamp of the source (if exists)
        details: Additional details about the comparison
    """

    workflow_name: str
    compare_workflow_name: str | None
    status: Literal["up_to_date", "ahead", "behind", "diverged", "no_source"]
    workflow_last_modified: str | None
    source_last_modified: str | None
    details: str


@dataclass
@PayloadRegistry.register
class CompareWorkflowsResultFailure(ResultPayloadFailure):
    """Workflow comparison failed. Common causes: workflow not found, source not accessible, comparison error."""


@dataclass
@PayloadRegistry.register
class MoveWorkflowRequest(RequestPayload):
    """Move a workflow to a different directory in the workspace.

    Use when: Organizing workflows into directories, restructuring workflow hierarchies,
    moving workflows to categorized folders, cleaning up workspace organization.

    Args:
        workflow_name: Name of the workflow to move
        target_directory: Target directory path relative to workspace root

    Results: MoveWorkflowResultSuccess (with new path) | MoveWorkflowResultFailure (move error)
    """

    workflow_name: str
    target_directory: str


@dataclass
@PayloadRegistry.register
class MoveWorkflowResultSuccess(WorkflowAlteredMixin, ResultPayloadSuccess):
    """Workflow moved successfully.

    Args:
        moved_file_path: New file path after the move
        new_workflow_name: New registry key for the workflow after the move
    """

    moved_file_path: str
    new_workflow_name: str


@dataclass
@PayloadRegistry.register
class MoveWorkflowResultFailure(ResultPayloadFailure):
    """Workflow move failed. Common causes: workflow not found, invalid target directory, file system error."""


@dataclass
@PayloadRegistry.register
class GetWorkflowMetadataRequest(RequestPayload):
    """Get selected metadata for a workflow by name from the registry."""

    workflow_name: str


@dataclass
@PayloadRegistry.register
class GetWorkflowMetadataResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Workflow metadata retrieved successfully."""

    workflow_metadata: WorkflowMetadata


@dataclass
@PayloadRegistry.register
class GetWorkflowMetadataResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Workflow metadata retrieval failed. Common causes: workflow not found, registry error, file load error."""


class WorkflowStatus(StrEnum):
    """The status of a workflow that was attempted to be loaded."""

    GOOD = "GOOD"
    FLAWED = "FLAWED"
    UNUSABLE = "UNUSABLE"
    MISSING = "MISSING"


class WorkflowDependencyStatus(StrEnum):
    """The status of a single library dependency for a workflow."""

    PERFECT = "PERFECT"
    GOOD = "GOOD"
    CAUTION = "CAUTION"
    BAD = "BAD"
    MISSING = "MISSING"
    UNKNOWN = "UNKNOWN"


@dataclass
class WorkflowDependencyInfo:
    """Information about a single library dependency for a workflow.

    Args:
        library_name: Name of the library
        version_requested: Version of the library required by the workflow
        version_present: Version of the library currently installed, or None if not found
        status: Dependency status
    """

    library_name: str
    version_requested: str
    version_present: str | None
    status: WorkflowDependencyStatus


@dataclass
@PayloadRegistry.register
class GetWorkflowInfoRequest(RequestPayload):
    """Get fitness/health information for a workflow.

    Use when: Displaying workflow health warnings in the UI, checking whether a workflow
    has compatibility issues before loading it, showing dependency status.

    Args:
        workflow_name: Registry key for the workflow.

    Results: GetWorkflowInfoResultSuccess (with status and details) | GetWorkflowInfoResultFailure (not found)
    """

    workflow_name: str


@dataclass
@PayloadRegistry.register
class GetWorkflowInfoResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Workflow info retrieved successfully.

    Args:
        status: Overall fitness status (GOOD, FLAWED, UNUSABLE, or MISSING)
        workflow_name: Display name of the workflow, or None if unavailable
        workflow_path: Absolute file path to the workflow file
        problems: List of human-readable problem descriptions, one entry per problem group
        workflow_dependencies: List of library dependency details
    """

    status: WorkflowStatus
    workflow_name: str | None
    workflow_path: str
    problems: list[str]
    workflow_dependencies: list[WorkflowDependencyInfo]


@dataclass
@PayloadRegistry.register
class GetWorkflowInfoResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Workflow info retrieval failed. Common causes: workflow not found, neither or both identifiers provided."""


@dataclass
class WorkflowInfoSummary:
    """Serializable summary of a workflow's fitness/health information.

    Args:
        status: Overall fitness status
        workflow_name: Display name of the workflow, or None if unavailable
        workflow_path: Absolute file path to the workflow file
        problems: List of human-readable problem descriptions, one entry per problem group
        workflow_dependencies: List of library dependency details
    """

    status: WorkflowStatus
    workflow_name: str | None
    workflow_path: str
    problems: list[str]
    workflow_dependencies: list[WorkflowDependencyInfo]


@dataclass
@PayloadRegistry.register
class ListAllWorkflowInfoRequest(RequestPayload):
    """Get fitness/health information for all registered workflows in a single request.

    Use when: Populating the workflow list UI with health indicators for every workflow
    at once, avoiding many individual GetWorkflowInfoRequest calls.

    Results: ListAllWorkflowInfoResultSuccess (with workflow_infos dict) | ListAllWorkflowInfoResultFailure
    """


@dataclass
@PayloadRegistry.register
class ListAllWorkflowInfoResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Workflow info for all registered workflows retrieved successfully.

    Args:
        workflow_infos: Dict mapping registry key to workflow info summary.
            Workflows with no info entry are omitted.
    """

    workflow_infos: dict[str, WorkflowInfoSummary]


@dataclass
@PayloadRegistry.register
class ListAllWorkflowInfoResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Workflow info listing failed."""


@dataclass
@PayloadRegistry.register
class SetWorkflowMetadataRequest(RequestPayload):
    """Replace the workflow's metadata entirely and persist to file."""

    workflow_name: str
    workflow_metadata: WorkflowMetadata


@dataclass
@PayloadRegistry.register
class SetWorkflowMetadataResultSuccess(WorkflowAlteredMixin, ResultPayloadSuccess):
    """Workflow metadata updated successfully."""


@dataclass
@PayloadRegistry.register
class SetWorkflowMetadataResultFailure(ResultPayloadFailure):
    """Workflow metadata update failed. Common causes: workflow not found, invalid keys/types, file system error."""


@dataclass
@PayloadRegistry.register
class RefreshWorkflowRegistryRequest(RequestPayload):
    """Rescan the workspace and config for workflow files and refresh the in-memory registry.

    Use when: Workflows have been added or removed on disk outside of the engine,
    forcing a re-discovery without changing the workspace.

    Results: RefreshWorkflowRegistryResultSuccess | RefreshWorkflowRegistryResultFailure
    """


@dataclass
@PayloadRegistry.register
class RefreshWorkflowRegistryResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Workflow registry refreshed successfully."""


@dataclass
@PayloadRegistry.register
class RefreshWorkflowRegistryResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Workflow registry refresh failed."""


@dataclass
@PayloadRegistry.register
class RegisterWorkflowsFromConfigRequest(RequestPayload):
    """Register workflows from configuration section.

    Use when: Loading workflows from configuration after library initialization,
    registering workflows from synced directories, batch workflow registration.

    Args:
        config_section: Configuration section path containing workflow paths to register

    Results: RegisterWorkflowsFromConfigResultSuccess (with count) | RegisterWorkflowsFromConfigResultFailure (registration error)
    """

    config_section: str


@dataclass
@PayloadRegistry.register
class RegisterWorkflowsFromConfigResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Workflows registered from configuration successfully.

    Args:
        succeeded_workflows: List of workflow names that were successfully registered
        failed_workflows: List of workflow names that failed to register
    """

    succeeded_workflows: list[str]
    failed_workflows: list[str]


@dataclass
@PayloadRegistry.register
class RegisterWorkflowsFromConfigResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Workflow registration from configuration failed. Common causes: configuration not found, invalid paths, registration errors."""


@dataclass
@PayloadRegistry.register
class SaveWorkflowFileFromSerializedFlowRequest(RequestPayload):
    """Save a workflow file from serialized flow commands without registry overhead.

    Use when: Creating workflow files from user-supplied subsets of existing workflows,
    exporting partial workflows, creating standalone workflow files without registration.

    Args:
        serialized_flow_commands: The serialized commands representing the workflow structure
        file_name: Name for the workflow file (without .py extension); also used as registry key
        creation_date: Optional creation date for the workflow metadata (defaults to current time if not provided)
        display_name: Optional display name for the workflow (metadata.name). Defaults to file_name if not provided.
        image_path: Optional path to workflow image/thumbnail. If None, callers may preserve existing image.
        description: Optional workflow description text. If None, callers may preserve existing description.
        is_template: Optional template status flag. If None, callers may preserve existing template status.
        branched_from: Optional branched from information to preserve workflow lineage
        workflow_shape: Optional workflow shape defining inputs and outputs for external callers
        file_path: Optional specific file path to use (defaults to workspace path if not provided)
        pickle_control_flow_result: Whether to pickle control flow results in generated execution code (defaults to False)

    Results: SaveWorkflowFileFromSerializedFlowResultSuccess (with file path) | SaveWorkflowFileFromSerializedFlowResultFailure (save error)
    """

    serialized_flow_commands: "SerializedFlowCommands"
    file_name: str
    file_path: str | None = None
    creation_date: datetime | None = None
    display_name: str | None = None
    image_path: str | None = None
    description: str | None = None
    is_template: bool | None = None
    branched_from: str | None = None
    workflow_shape: WorkflowShape | None = None
    pickle_control_flow_result: bool = False


@dataclass
@PayloadRegistry.register
class SaveWorkflowFileFromSerializedFlowResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Workflow file saved successfully from serialized flow commands.

    Args:
        file_path: Path where the workflow file was written
        workflow_metadata: The metadata that was generated for the workflow
    """

    file_path: str
    workflow_metadata: WorkflowMetadata


@dataclass
@PayloadRegistry.register
class SaveWorkflowFileFromSerializedFlowResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Workflow file save failed. Common causes: file system error, permission denied, invalid serialized commands."""


@dataclass
@PayloadRegistry.register
class SaveSubflowToWorkflowRequest(RequestPayload):
    """Serialize a subflow and save it back to its original workflow file.

    Use when: Persisting changes made to a subflow in a modal editor back to
    the workflow file it was loaded from.

    Args:
        flow_name: The engine flow name of the subflow to serialize
        workflow_name: Registry key for the target workflow (used to derive file path and metadata)

    Results: SaveSubflowToWorkflowResultSuccess | SaveSubflowToWorkflowResultFailure
    """

    flow_name: str
    workflow_name: str


@dataclass
@PayloadRegistry.register
class SaveSubflowToWorkflowResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Subflow saved successfully to workflow file.

    Args:
        file_path: Path where the workflow file was written
        workflow_metadata: The metadata generated for the saved workflow
    """

    file_path: str
    workflow_metadata: WorkflowMetadata


@dataclass
@PayloadRegistry.register
class SaveSubflowToWorkflowResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Subflow save to workflow file failed. Common causes: serialization error, file system error, invalid flow name."""


@dataclass
@PayloadRegistry.register
class SetVariableSubstitutionEnabledRequest(RequestPayload):
    """Enable or disable inline variable substitution for the current workflow.

    The setting is stored in memory and, when the workflow is saved, is baked into
    the generated build_workflow() function as a SetVariableSubstitutionEnabledRequest
    call. This means the flag survives a full reload — including running the workflow
    .py file directly as a script — without any registry lookup.

    Args:
        enabled: True (default) to substitute {VAR} tokens at execution time;
                 False to leave parameter values unchanged.
        initial_setup: True when called from build_workflow() during file load.
                       Prevents marking the workflow as unsaved on reload.
    """

    enabled: bool = True
    initial_setup: bool = False


@dataclass
@PayloadRegistry.register
class SetVariableSubstitutionEnabledResultSuccess(WorkflowAlteredMixin, ResultPayloadSuccess):
    """Variable substitution flag updated successfully (interactive change)."""


@dataclass
@PayloadRegistry.register
class SetVariableSubstitutionEnabledResultNotAlteredSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Variable substitution flag restored successfully during workflow load."""


@dataclass
@PayloadRegistry.register
class SetVariableSubstitutionEnabledResultFailure(ResultPayloadFailure):
    """Variable substitution flag update failed. Common cause: no active workflow context."""
