from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, ClassVar, NamedTuple, TypeVar

import anyio
import semver
from rich.box import HEAVY_EDGE
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from griptape_nodes.files.path_utils import (
    canonicalize_for_identity,
    derive_registry_key,
)
from griptape_nodes.node_library.workflow_registry import (
    WorkflowMetadata,
    WorkflowMetadataError,
    WorkflowMetadataFileError,
    WorkflowMetadataMissingTableError,
    WorkflowMetadataSchemaError,
    WorkflowMetadataSectionCountError,
    WorkflowMetadataTomlError,
    find_metadata_blocks,
    read_workflow_metadata,
)
from griptape_nodes.retained_mode.engine import EngineScoped
from griptape_nodes.retained_mode.events.app_events import (
    EngineInitializationProgress,
    InitializationPhase,
    InitializationStatus,
)

# Runtime imports for ResultDetails since it's used at runtime
from griptape_nodes.retained_mode.events.base_events import AppEvent, ResultDetails
from griptape_nodes.retained_mode.events.library_events import (
    GetLibraryMetadataRequest,
    GetLibraryMetadataResultSuccess,
    ListRegisteredLibrariesRequest,
    ListRegisteredLibrariesResultSuccess,
)
from griptape_nodes.retained_mode.events.workflow_events import (
    ImportWorkflowRequest,
    ImportWorkflowResultFailure,
    ImportWorkflowResultSuccess,
    LoadWorkflowMetadata,
    LoadWorkflowMetadataResultFailure,
    LoadWorkflowMetadataResultSuccess,
    RefreshWorkflowRegistryRequest,
    RefreshWorkflowRegistryResultFailure,
    RefreshWorkflowRegistryResultSuccess,
    RegisterWorkflowRequest,
    RegisterWorkflowResultFailure,
    RegisterWorkflowResultSuccess,
    RegisterWorkflowsFromConfigRequest,
    RegisterWorkflowsFromConfigResultFailure,
    RegisterWorkflowsFromConfigResultSuccess,
    WorkflowDependencyInfo,
    WorkflowDependencyStatus,
    WorkflowStatus,
)
from griptape_nodes.retained_mode.managers.fitness_problems.workflows import (
    InvalidDependencyVersionStringProblem,
    InvalidLibraryVersionStringProblem,
    InvalidMetadataSchemaProblem,
    InvalidMetadataSectionCountProblem,
    InvalidTomlFormatProblem,
    LibraryNotRegisteredProblem,
    LibraryVersionBelowRequiredProblem,
    LibraryVersionLargeDifferenceProblem,
    LibraryVersionMajorMismatchProblem,
    LibraryVersionMinorDifferenceProblem,
    MissingCreationDateProblem,
    MissingLastModifiedDateProblem,
    MissingTomlSectionProblem,
    WorkflowNotFoundProblem,
)
from griptape_nodes.retained_mode.managers.settings import WORKFLOWS_TO_REGISTER_KEY
from griptape_nodes.retained_mode.managers.workflow.branching import WorkflowBranching
from griptape_nodes.retained_mode.managers.workflow.catalog import WorkflowCatalog
from griptape_nodes.retained_mode.managers.workflow.codegen import WORKFLOW_METADATA_HEADER, WorkflowCodeGenerator
from griptape_nodes.retained_mode.managers.workflow.file_operations import WorkflowFileOperations
from griptape_nodes.retained_mode.managers.workflow.loading import EPOCH_START
from griptape_nodes.retained_mode.managers.workflow.publishing import WorkflowPublishing
from griptape_nodes.retained_mode.managers.workflow.referenced_import import ReferencedWorkflowImport
from griptape_nodes.retained_mode.managers.workflow.running import WorkflowRunner, collate_problems_by_type
from griptape_nodes.retained_mode.managers.workflow.saving import (
    WorkflowSaver,
)
from griptape_nodes.retained_mode.managers.workflow.shape import extract_workflow_shape
from griptape_nodes.retained_mode.managers.workflow.variable_substitution import VariableSubstitution
from griptape_nodes.retained_mode.request_handlers import handles
from griptape_nodes.utils.file_utils import find_files_recursive

if TYPE_CHECKING:
    import re
    from collections.abc import Callable
    from types import TracebackType

    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.events.base_events import ResultPayload
    from griptape_nodes.retained_mode.managers.event_manager import EventManager
    from griptape_nodes.retained_mode.managers.fitness_problems.workflows.workflow_problem import WorkflowProblem
    from griptape_nodes.retained_mode.managers.workflow.running import WorkflowExecutionResult


T = TypeVar("T")

logger = logging.getLogger("griptape_nodes")


class WorkflowRegistrationResult(NamedTuple):
    """Result of processing workflows for registration."""

    succeeded: list[str]
    failed: list[str]


class WorkflowManager(EngineScoped):
    WORKFLOW_METADATA_HEADER: ClassVar[str] = WORKFLOW_METADATA_HEADER
    MAX_MINOR_VERSION_DEVIATION: ClassVar[int] = (
        100  # TODO: https://github.com/griptape-ai/griptape-nodes/issues/1219 <- make the versioning enforcement softer after we get a release going
    )

    WorkflowStatus = WorkflowStatus
    WorkflowDependencyStatus = WorkflowDependencyStatus
    WorkflowDependencyInfo = WorkflowDependencyInfo

    @dataclass
    class WorkflowInfo:
        """Information about a workflow that was attempted to be loaded."""

        status: WorkflowStatus
        workflow_path: str
        workflow_name: str | None = None
        workflow_dependencies: list[WorkflowDependencyInfo] = field(default_factory=list)
        problems: list[WorkflowProblem] = field(default_factory=list)

    _workflow_file_path_to_info: dict[str, WorkflowInfo]

    # Track how many contexts we have that intend to squelch (set to False) altered_workflow_state event values.
    class WorkflowSquelchContext:
        """Context manager to squelch workflow altered events."""

        def __init__(self, manager: WorkflowManager):
            self.manager = manager

        def __enter__(self) -> None:
            self.manager._squelch_workflow_altered_count += 1

        def __exit__(
            self,
            exc_type: type[BaseException] | None,
            exc_value: BaseException | None,
            exc_traceback: TracebackType | None,
        ) -> None:
            self.manager._squelch_workflow_altered_count -= 1

    _squelch_workflow_altered_count: int = 0

    # Track referenced workflow import context stack
    class ReferencedWorkflowContext:
        """Context manager for tracking workflow import operations."""

        def __init__(self, manager: WorkflowManager, workflow_name: str):
            self.manager = manager
            self.workflow_name = workflow_name

        def __enter__(self) -> WorkflowManager.ReferencedWorkflowContext:
            self.manager._referenced_workflow_stack.append(self.workflow_name)
            return self

        def __exit__(
            self,
            exc_type: type[BaseException] | None,
            exc_value: BaseException | None,
            exc_traceback: TracebackType | None,
        ) -> None:
            self.manager._referenced_workflow_stack.pop()

    _referenced_workflow_stack: list[str] = field(default_factory=list)

    def __init__(self, event_manager: EventManager, *, engine: Engine | None = None) -> None:
        super().__init__(engine)
        self._workflow_file_path_to_info = {}
        self._squelch_workflow_altered_count = 0
        self._referenced_workflow_stack = []
        # Initialize as set: before refresh_workflow_registry has run, the registry
        # is simply empty. Handlers invoked during library load (e.g. from a node
        # __init__ that issues a workflow query) should return an empty result
        # rather than block on an event that's waiting on the same call stack to
        # unwind. refresh_workflow_registry clears this while it mutates the registry.
        self._workflows_loading_complete = asyncio.Event()
        self._workflows_loading_complete.set()

        self.codegen = WorkflowCodeGenerator(engine)
        self.variable_substitution = VariableSubstitution(event_manager, engine=engine)
        self.runner = WorkflowRunner(event_manager, engine=engine)
        self.saver = WorkflowSaver(event_manager, engine=engine)
        self.branching = WorkflowBranching(event_manager, engine=engine)
        self.file_operations = WorkflowFileOperations(event_manager, engine=engine)
        self.catalog = WorkflowCatalog(event_manager, engine=engine)
        self.publishing = WorkflowPublishing(event_manager, engine=engine)
        self.referenced_import = ReferencedWorkflowImport(event_manager, engine=engine)
        event_manager.register_request_handlers(self)

    def has_current_referenced_workflow(self) -> bool:
        """Check if there is currently a referenced workflow context active."""
        return len(self._referenced_workflow_stack) > 0

    def get_current_referenced_workflow(self) -> str:
        """Get the current workflow source path from the context stack.

        Raises:
            IndexError: If no referenced workflow context is active.
        """
        return self._referenced_workflow_stack[-1]

    async def refresh_workflow_registry(self, workflows_to_register: list[str] | None = None) -> None:
        # All of the libraries have loaded, and any workflows they came with have been registered.
        # Clear any previously registered user/workspace workflows before re-registering, so that
        # a workspace change (e.g. project switch) takes effect cleanly. Library-provided workflows
        # (is_griptape_provided=True) registered above this call are preserved.
        self.engine.workflow_registry.clear_user_workflows()

        # Discover workflows from both config and workspace.
        self._workflows_loading_complete.clear()

        try:
            default_workflow_section = "app_events.on_app_initialization_complete.workflows_to_register"
            config_mgr = self.engine.config_manager

            if workflows_to_register is None:
                workflows_to_register = []

                # Add from config
                config_workflows = config_mgr.get_config_value(default_workflow_section, default=[])
                workflows_to_register.extend(config_workflows)

                # Add from workspace (avoiding duplicates)
                workspace_path = config_mgr.workspace_path
                workflows_to_register.extend([str(workspace_path)])

            # Register all discovered workflows at once if any were found
            await self._process_workflows_for_registration(workflows_to_register)

            # Now remove any workflows that were missing files.
            paths_to_remove = set()
            for workflow_path, workflow_info in self._workflow_file_path_to_info.items():
                if workflow_info.status == WorkflowManager.WorkflowStatus.MISSING:
                    # Remove this file path from the config.
                    paths_to_remove.add(workflow_path.lower())

            if paths_to_remove:
                workflows_to_register = config_mgr.get_config_value(default_workflow_section)
                if workflows_to_register:
                    workflows_to_register = [
                        workflow for workflow in workflows_to_register if workflow.lower() not in paths_to_remove
                    ]
                    config_mgr.set_config_value(default_workflow_section, workflows_to_register)
        finally:
            self._workflows_loading_complete.set()

    def get_workflow_metadata(self, workflow_file_path: Path, block_name: str) -> list[re.Match[str]]:
        """Get the workflow metadata for a given workflow file path.

        Args:
            workflow_file_path (Path): The path to the workflow file.
            block_name (str): The name of the metadata block to search for.

        Returns:
            list[re.Match[str]]: A list of regex matches for the specified metadata block.

        """
        with workflow_file_path.open("r", encoding="utf-8") as file:
            workflow_content = file.read()

        return find_metadata_blocks(workflow_content, block_name)

    def print_workflow_load_status(self, min_status: WorkflowStatus = WorkflowStatus.FLAWED) -> None:
        workflow_file_paths = self.get_workflows_attempted_to_load()
        workflow_infos = []
        for workflow_file_path in workflow_file_paths:
            workflow_info = self.get_workflow_info_for_attempted_load(workflow_file_path)
            workflow_infos.append(workflow_info)

        # Filter workflows to only show those at or worse than min_status
        all_statuses = list(self.WorkflowStatus)
        min_status_index = all_statuses.index(min_status)
        filtered_workflow_infos = [
            wf_info for wf_info in workflow_infos if all_statuses.index(wf_info.status) >= min_status_index
        ]

        # Sort workflows by severity (worst to best)
        filtered_workflow_infos.sort(key=lambda wf: all_statuses.index(wf.status), reverse=True)

        console = Console()

        # Check if the list is empty
        if not filtered_workflow_infos:
            empty_message = Text("No workflow information available", style="italic")
            panel = Panel(empty_message, title="Workflow Information", border_style="blue")
            console.print(panel)
            return

        # Add filter message if not showing all workflows
        if min_status != self.WorkflowStatus.GOOD:
            statuses_shown = all_statuses[min_status_index:]
            status_names = ", ".join(s.value for s in statuses_shown)
            filter_message = Text(
                f"Only displaying workflows with a fitness of {status_names}",
                style="italic yellow",
            )
            console.print(filter_message)
            console.print()

        # Create a table with three columns and row dividers
        table = Table(show_header=True, box=HEAVY_EDGE, show_lines=True, expand=True)
        table.add_column("Workflow", style="green", ratio=2)
        table.add_column("Problems", style="yellow", ratio=3)
        table.add_column("Dependencies", style="magenta", ratio=2)

        # Status emojis mapping
        status_emoji = {
            self.WorkflowStatus.GOOD: "[green]OK[/green]",
            self.WorkflowStatus.FLAWED: "[yellow]![/yellow]",
            self.WorkflowStatus.UNUSABLE: "[red]X[/red]",
            self.WorkflowStatus.MISSING: "[red]?[/red]",
        }

        # Status text mapping (colored)
        status_text = {
            self.WorkflowStatus.GOOD: "[green](GOOD)[/green]",
            self.WorkflowStatus.FLAWED: "[yellow](FLAWED)[/yellow]",
            self.WorkflowStatus.UNUSABLE: "[red](UNUSABLE)[/red]",
            self.WorkflowStatus.MISSING: "[red](MISSING)[/red]",
        }

        dependency_status_emoji = {
            self.WorkflowDependencyStatus.PERFECT: "[green]OK[/green]",
            self.WorkflowDependencyStatus.GOOD: "[green]GOOD[/green]",
            self.WorkflowDependencyStatus.CAUTION: "[yellow]CAUTION[/yellow]",
            self.WorkflowDependencyStatus.BAD: "[red]BAD[/red]",
            self.WorkflowDependencyStatus.MISSING: "[red]MISSING[/red]",
            self.WorkflowDependencyStatus.UNKNOWN: "[red]UNKNOWN[/red]",
        }

        # Add rows for each workflow info
        for wf_info in filtered_workflow_infos:
            # Workflow name column with emoji, name, colored status, and file path underneath
            emoji = status_emoji.get(wf_info.status, "ERR: Unknown/Unexpected Workflow Status")
            colored_status = status_text.get(wf_info.status, "(UNKNOWN)")
            name = wf_info.workflow_name or "*UNKNOWN*"
            file_path = wf_info.workflow_path
            workflow_name_with_path = Text.from_markup(
                f"{emoji} - {name} {colored_status}\n[cyan dim]{file_path}[/cyan dim]"
            )
            workflow_name_with_path.overflow = "fold"

            # Problems column - collate by type
            if not wf_info.problems:
                problems = "No problems detected."
            else:
                collated_strings = collate_problems_by_type(wf_info.problems)

                # Format for display
                if len(collated_strings) == 1:
                    problems = collated_strings[0]
                else:
                    problems = "\n".join([f"{j + 1}. {problem}" for j, problem in enumerate(collated_strings)])

            # Dependencies column
            if wf_info.status == self.WorkflowStatus.MISSING or (
                wf_info.status == self.WorkflowStatus.UNUSABLE and not wf_info.workflow_dependencies
            ):
                dependencies = "[red]?[/red] UNKNOWN"
            else:
                dependencies = (
                    "\n".join(
                        f"{dependency_status_emoji.get(dep.status, '?')} - {dep.library_name} ({dep.version_requested}): {dep.status.value}"
                        for dep in wf_info.workflow_dependencies
                    )
                    if wf_info.workflow_dependencies
                    else "No dependencies"
                )

            table.add_row(
                workflow_name_with_path,
                problems,
                dependencies,
            )

        # Wrap the table in a panel
        panel = Panel(table, title="Workflow Information", border_style="blue")
        console.print(panel)

    def get_workflows_attempted_to_load(self) -> list[str]:
        return list(self._workflow_file_path_to_info.keys())

    def get_workflow_info_for_attempted_load(self, workflow_file_path: str) -> WorkflowInfo:
        return self._workflow_file_path_to_info[workflow_file_path]

    def find_workflow_info_for_attempted_load(self, workflow_file_path: str) -> WorkflowInfo | None:
        return self._workflow_file_path_to_info.get(workflow_file_path)

    async def wait_for_workflows_loaded(self) -> None:
        """Wait until any registry refresh in progress has finished."""
        await self._workflows_loading_complete.wait()

    def referenced_workflow(self, workflow_name: str) -> ReferencedWorkflowContext:
        """Track `workflow_name` as the referenced workflow being imported for the `with` block."""
        return self.ReferencedWorkflowContext(self, workflow_name)

    def squelch_workflow_altered(self) -> WorkflowSquelchContext:
        """Suppress workflow-altered events for the duration of the `with` block."""
        return self.WorkflowSquelchContext(self)

    def should_squelch_workflow_altered(self) -> bool:
        return self._squelch_workflow_altered_count > 0

    def clear_object_state(self) -> None:
        """Clear per-workflow state when the engine is reset."""
        self.variable_substitution.clear()

    def persist_external_workflow_registration(self, full_path: str) -> None:
        """Persist an out-of-workspace workflow path to global config so it survives restarts.

        Self-guarding: paths inside the workspace are discovered by directory scan and need
        no config entry, so this is a no-op for them.
        """
        config_manager = self.engine.config_manager
        try:
            canonicalize_for_identity(full_path).relative_to(canonicalize_for_identity(config_manager.workspace_path))
        except ValueError:
            existing_workflows = config_manager.get_config_value(WORKFLOWS_TO_REGISTER_KEY)
            if not existing_workflows:
                existing_workflows = []
            if full_path not in existing_workflows:
                existing_workflows.append(full_path)
            config_manager.set_config_value(WORKFLOWS_TO_REGISTER_KEY, existing_workflows)

    @handles(RegisterWorkflowRequest)
    def on_register_workflow_request(self, request: RegisterWorkflowRequest) -> ResultPayload:
        # The registry key is derived from the file path (minus extension), independent of the display name.
        registry_key = derive_registry_key(request.file_name)
        try:
            if isinstance(request.metadata, dict):
                request.metadata = WorkflowMetadata(**request.metadata)

            request.metadata.name = self._repair_path_shaped_display_name(
                display_name=request.metadata.name, registry_key=registry_key
            )

            self.engine.workflow_registry.generate_new_workflow(
                registry_key=registry_key, metadata=request.metadata, file_path=request.file_name
            )
        except Exception as e:
            details = f"Failed to register workflow with name '{request.metadata.name}'. Error: {e}"
            return RegisterWorkflowResultFailure(result_details=details)
        return RegisterWorkflowResultSuccess(
            workflow_name=registry_key,
            result_details=ResultDetails(
                message=f"Successfully registered workflow: {registry_key}",
                level=logging.DEBUG,
            ),
        )

    def _repair_path_shaped_display_name(self, *, display_name: str, registry_key: str) -> str:
        """Repair a display name that an older branch/merge/reset wrote as a registry key.

        Those three sites used to write the path-derived registry key straight into ``metadata.name``,
        so a branch of "Shot 010 Comp" living under ``shots/sh010/`` loaded as
        "shots/sh010/comp_branch_1" everywhere the editor shows a workflow title. Files written by
        those versions are still on disk, and they carry the *current* schema version -- the bug was
        never a schema change -- so there is no version to gate on. We key on the damage itself.

        Exact equality with the registry key is the fingerprint: a title someone actually typed does
        not coincide with its own file path. Everything else is left alone, including a name that
        merely happens to contain a separator, which may well be deliberate.

        In-memory only. Rewriting headers during load would touch a pile of user files, churning
        mtimes and git diffs for a cosmetic label; the repaired name persists on its own the next
        time the workflow is saved for any other reason.
        """
        if display_name != registry_key:
            return display_name
        if "/" not in registry_key:
            # A workspace-root workflow whose title matches its file stem. Nothing path-shaped here,
            # and nothing the three sites broke -- they only read badly with directories in the key.
            return display_name

        repaired_name = PurePosixPath(registry_key).name
        logger.debug(
            "Workflow '%s' carries its registry key as its display name (written by a pre-fix branch, "
            "merge, or reset). Showing it as '%s'; the file keeps the old name until its next save.",
            registry_key,
            repaired_name,
        )
        return repaired_name

    @handles(ImportWorkflowRequest)
    async def on_import_workflow_request(self, request: ImportWorkflowRequest) -> ResultPayload:
        # First, attempt to load metadata from the file
        load_metadata_request = LoadWorkflowMetadata(file_name=request.file_path)
        load_metadata_result = await self.on_load_workflow_metadata_request(load_metadata_request)

        if not isinstance(load_metadata_result, LoadWorkflowMetadataResultSuccess):
            return ImportWorkflowResultFailure(result_details=load_metadata_result.result_details)

        # Check if workflow is already registered by file path (registry key).
        # The registry key is derived from the file path, not metadata.name (the display name).
        workflow_name = derive_registry_key(request.file_path)
        if self.engine.workflow_registry.has_workflow_with_name(workflow_name):
            # Workflow already exists - no need to re-register
            return ImportWorkflowResultSuccess(
                workflow_name=workflow_name,
                result_details=f"Workflow '{workflow_name}' already exists - no need to re-import.",
            )

        # Now register the workflow with the extracted metadata
        register_request = RegisterWorkflowRequest(metadata=load_metadata_result.metadata, file_name=request.file_path)
        register_result = self.on_register_workflow_request(register_request)

        if not isinstance(register_result, RegisterWorkflowResultSuccess):
            return ImportWorkflowResultFailure(result_details=register_result.result_details)

        # Persist external workflows to global config so they survive restarts and appear in all projects.
        # Workspace workflows are discovered by directory scan and don't need an explicit entry.
        full_path = self.engine.workflow_registry.get_complete_file_path(request.file_path)
        self.persist_external_workflow_registration(full_path)

        return ImportWorkflowResultSuccess(
            workflow_name=register_result.workflow_name,
            result_details=ResultDetails(
                message=f"Successfully imported workflow: {register_result.workflow_name}", level=logging.INFO
            ),
        )

    @handles(LoadWorkflowMetadata)
    async def on_load_workflow_metadata_request(  # noqa: C901, PLR0912, PLR0915
        self, request: LoadWorkflowMetadata
    ) -> ResultPayload:
        # The editor can send LoadWorkflowMetadata before library registration finishes
        # (observed on Windows, engine cold start). Without this gate, the dependency
        # check below would race LibraryRegistry and return LibraryNotRegisteredProblem
        # for libraries that are milliseconds from being registered.
        await self.engine.library_manager._libraries_loading_complete.wait()
        # Let us go into the darkness.
        complete_file_path = self.engine.config_manager.workspace_path.joinpath(request.file_name)
        str_path = str(complete_file_path)
        if not await anyio.Path(complete_file_path).is_file():
            self._workflow_file_path_to_info[str(str_path)] = WorkflowManager.WorkflowInfo(
                status=WorkflowManager.WorkflowStatus.MISSING,
                workflow_path=str_path,
                workflow_name=None,
                workflow_dependencies=[],
                problems=[WorkflowNotFoundProblem()],
            )
            details = f"Attempted to load workflow metadata for a file at '{complete_file_path}. Failed because no file could be found at that path."
            return LoadWorkflowMetadataResultFailure(result_details=details)

        # Find the metadata block.
        metadata_or_failure = self._read_workflow_metadata_for_request(complete_file_path, str_path)
        if isinstance(metadata_or_failure, LoadWorkflowMetadataResultFailure):
            return metadata_or_failure
        workflow_metadata = metadata_or_failure

        # We have valid dependencies, etc.
        # TODO: validate schema versions, engine versions: https://github.com/griptape-ai/griptape-nodes/issues/617
        problems = []
        had_critical_error = False

        # Confirm dates are correct.
        if workflow_metadata.creation_date is None:
            # Assign it to the epoch start and flag it as a warning.
            workflow_metadata.creation_date = EPOCH_START
            problems.append(MissingCreationDateProblem(default_date=str(EPOCH_START)))
        if workflow_metadata.last_modified_date is None:
            # Assign it to the epoch start and flag it as a warning.
            workflow_metadata.last_modified_date = EPOCH_START
            problems.append(MissingLastModifiedDateProblem(default_date=str(EPOCH_START)))

        list_libraries_result = await self.engine.ahandle_request(
            ListRegisteredLibrariesRequest(broadcast_result=False)
        )

        if not isinstance(list_libraries_result, ListRegisteredLibrariesResultSuccess):
            registered_libraries = []
        else:
            registered_libraries = list_libraries_result.libraries

        dependency_infos = []
        for node_library_referenced in workflow_metadata.node_libraries_referenced:
            library_name = node_library_referenced.library_name
            desired_version_str = node_library_referenced.library_version
            try:
                desired_version = semver.VersionInfo.parse(desired_version_str)
            except Exception:
                had_critical_error = True
                problems.append(
                    InvalidDependencyVersionStringProblem(library_name=library_name, version_string=desired_version_str)
                )
                dependency_infos.append(
                    WorkflowManager.WorkflowDependencyInfo(
                        library_name=library_name,
                        version_requested=desired_version_str,
                        version_present=None,
                        status=WorkflowManager.WorkflowDependencyStatus.UNKNOWN,
                    )
                )
                # SKIP IT.
                continue
            # See how our desired version compares against the actual library we (may) have.
            # Check if library is registered (silent check - no error logging)
            if library_name not in registered_libraries:
                # Library not registered. Recoverable, not critical: the workflow opens with
                # ErrorProxyNode placeholders standing in for that library's nodes, so calling it
                # UNUSABLE would contradict what the editor is about to show (issue #5505). The
                # version-mismatch and malformed-metadata problems below stay critical.
                problems.append(LibraryNotRegisteredProblem(library_name=library_name))
                dependency_infos.append(
                    WorkflowManager.WorkflowDependencyInfo(
                        library_name=library_name,
                        version_requested=desired_version_str,
                        version_present=None,
                        status=WorkflowManager.WorkflowDependencyStatus.MISSING,
                    )
                )
                # SKIP IT.
                continue

            # Get library metadata (we know library is registered, so no error logging)
            library_metadata_request = GetLibraryMetadataRequest(library=library_name)
            library_metadata_result = self.engine.library_manager.catalog.get_library_metadata_request(
                library_metadata_request
            )

            if not isinstance(library_metadata_result, GetLibraryMetadataResultSuccess):
                # Should not happen since we verified library is registered, but handle gracefully.
                # Recoverable for the same reason as the unregistered case above: the library IS
                # in the registry, so its nodes still construct -- only its version is unknown.
                problems.append(LibraryNotRegisteredProblem(library_name=library_name))
                dependency_infos.append(
                    WorkflowManager.WorkflowDependencyInfo(
                        library_name=library_name,
                        version_requested=desired_version_str,
                        version_present=None,
                        status=WorkflowManager.WorkflowDependencyStatus.MISSING,
                    )
                )
                # SKIP IT.
                continue

            # Attempt to parse out the version string.
            library_metadata = library_metadata_result.metadata
            library_version_str = library_metadata.library_version
            try:
                library_version = semver.VersionInfo.parse(library_version_str)
            except Exception:
                had_critical_error = True
                problems.append(
                    InvalidLibraryVersionStringProblem(library_name=library_name, version_string=library_version_str)
                )
                dependency_infos.append(
                    WorkflowManager.WorkflowDependencyInfo(
                        library_name=library_name,
                        version_requested=desired_version_str,
                        version_present=None,
                        status=WorkflowManager.WorkflowDependencyStatus.UNKNOWN,
                    )
                )
                # SKIP IT.
                continue
            # How does it compare?
            major_matches = library_version.major == desired_version.major
            minor_matches = library_version.minor == desired_version.minor
            patch_matches = library_version.patch == desired_version.patch
            if major_matches and minor_matches and patch_matches:
                status = WorkflowManager.WorkflowDependencyStatus.PERFECT
            elif major_matches and minor_matches:
                status = WorkflowManager.WorkflowDependencyStatus.GOOD
            elif major_matches:
                # Let's see if the dependency is ahead and within our tolerance.
                delta = library_version.minor - desired_version.minor
                if delta < 0:
                    problems.append(
                        LibraryVersionBelowRequiredProblem(
                            library_name=library_name,
                            current_version=str(library_version),
                            required_version=str(desired_version),
                        )
                    )
                    status = WorkflowManager.WorkflowDependencyStatus.BAD
                    had_critical_error = True
                elif delta > WorkflowManager.MAX_MINOR_VERSION_DEVIATION:
                    problems.append(
                        LibraryVersionLargeDifferenceProblem(
                            library_name=library_name,
                            workflow_version=str(desired_version),
                            current_version=str(library_version),
                        )
                    )
                    status = WorkflowManager.WorkflowDependencyStatus.BAD
                    had_critical_error = True
                else:
                    problems.append(
                        LibraryVersionMinorDifferenceProblem(
                            library_name=library_name,
                            workflow_version=str(desired_version),
                            current_version=str(library_version),
                        )
                    )
                    status = WorkflowManager.WorkflowDependencyStatus.CAUTION
            else:
                problems.append(
                    LibraryVersionMajorMismatchProblem(
                        library_name=library_name,
                        workflow_version=str(desired_version),
                        current_version=str(library_version),
                    )
                )
                status = WorkflowManager.WorkflowDependencyStatus.BAD
                had_critical_error = True

            # Append the latest info for this dependency.
            dependency_infos.append(
                WorkflowManager.WorkflowDependencyInfo(
                    library_name=library_name,
                    version_requested=str(desired_version),
                    version_present=str(library_version),
                    status=status,
                )
            )

        # Check for workflow version-based compatibility issues and add to problems
        workflow_version_issues = await self.engine.version_compatibility_manager.check_workflow_version_compatibility(
            workflow_metadata
        )
        for issue in workflow_version_issues:
            problems.append(issue.problem)
            if issue.severity == WorkflowManager.WorkflowStatus.UNUSABLE:
                had_critical_error = True

        # OK, we have all of our dependencies together. Let's look at the overall scenario.
        if had_critical_error:
            overall_status = WorkflowManager.WorkflowStatus.UNUSABLE
        elif problems:
            overall_status = WorkflowManager.WorkflowStatus.FLAWED
        else:
            overall_status = WorkflowManager.WorkflowStatus.GOOD

        self._workflow_file_path_to_info[str(str_path)] = WorkflowManager.WorkflowInfo(
            status=overall_status,
            workflow_path=str_path,
            workflow_name=workflow_metadata.name,
            workflow_dependencies=dependency_infos,
            problems=problems,
        )
        return LoadWorkflowMetadataResultSuccess(
            metadata=workflow_metadata, result_details="Workflow metadata loaded successfully."
        )

    def _read_workflow_metadata_for_request(
        self, workflow_file_path: Path, workflow_path: str
    ) -> WorkflowMetadata | LoadWorkflowMetadataResultFailure:
        """Read a workflow's metadata header, turning each way it can fail into a reportable problem.

        The parsing itself lives in `read_workflow_metadata` so the library loader and this handler
        read headers the same way. What this adds is the editor's per-stage reporting: each failure
        becomes the specific problem the workflow-load report displays for it.
        """
        try:
            return read_workflow_metadata(workflow_file_path)
        except WorkflowMetadataFileError as err:
            return self._record_workflow_metadata_failure(
                workflow_path,
                status=WorkflowManager.WorkflowStatus.MISSING,
                problem=WorkflowNotFoundProblem(),
                error=err,
            )
        except WorkflowMetadataSectionCountError as err:
            return self._record_workflow_metadata_failure(
                workflow_path,
                status=WorkflowManager.WorkflowStatus.UNUSABLE,
                problem=InvalidMetadataSectionCountProblem(section_name=err.section_name, count=err.count),
                error=err,
            )
        except WorkflowMetadataTomlError as err:
            return self._record_workflow_metadata_failure(
                workflow_path,
                status=WorkflowManager.WorkflowStatus.UNUSABLE,
                problem=InvalidTomlFormatProblem(error_message=err.error_message),
                error=err,
            )
        except WorkflowMetadataMissingTableError as err:
            return self._record_workflow_metadata_failure(
                workflow_path,
                status=WorkflowManager.WorkflowStatus.UNUSABLE,
                problem=MissingTomlSectionProblem(section_path=err.section_path),
                error=err,
            )
        except WorkflowMetadataSchemaError as err:
            return self._record_workflow_metadata_failure(
                workflow_path,
                status=WorkflowManager.WorkflowStatus.UNUSABLE,
                problem=InvalidMetadataSchemaProblem(section_path=err.section_path, error_message=err.error_message),
                error=err,
            )

    def _record_workflow_metadata_failure(
        self,
        workflow_path: str,
        *,
        status: WorkflowManager.WorkflowStatus,
        problem: WorkflowProblem,
        error: WorkflowMetadataError,
    ) -> LoadWorkflowMetadataResultFailure:
        """Record why a workflow's metadata header could not be read, and fail the request."""
        self._workflow_file_path_to_info[workflow_path] = WorkflowManager.WorkflowInfo(
            status=status,
            workflow_path=workflow_path,
            workflow_name=None,
            workflow_dependencies=[],
            problems=[problem],
        )
        return LoadWorkflowMetadataResultFailure(result_details=str(error))

    async def register_workflows_from_config(self, config_section: str) -> None:
        workflows_to_register = self.engine.config_manager.get_config_value(config_section)
        if workflows_to_register:
            await self.register_list_of_workflows(workflows_to_register)

    async def register_list_of_workflows(self, workflows_to_register: list[str]) -> None:
        await self._process_workflows_for_registration(workflows_to_register)

    def _register_workflow(self, workflow_to_register: str, workflow_metadata: WorkflowMetadata) -> bool:
        """Registers a workflow from a file.

        Args:
            workflow_to_register: The path to the workflow file to register.
            workflow_metadata: Metadata already loaded from that file by the caller.
                Passed in rather than re-read here: loading it parses the file's TOML
                header, and the caller has to do that anyway to decide the file is
                registerable, so re-reading would parse every workflow twice.

        Returns:
            bool: True if the workflow was successfully registered, False otherwise.
        """
        # Presently, this will not fail if a workflow with that name is already registered. That failure happens with a later check.
        # However, the table of WorkflowInfo DOES get updated in this request, which may present a confusing state of affairs to the user.
        # On one hand, we want the user to know how a specific workflow fared, but also not let them think it was registered when it wasn't.
        # TODO: https://github.com/griptape-ai/griptape-nodes/issues/996

        # Register it as a success.
        workflow_register_request = RegisterWorkflowRequest(
            metadata=workflow_metadata, file_name=str(workflow_to_register)
        )
        workflow_register_result = self.engine.handle_request(workflow_register_request)
        if not isinstance(workflow_register_result, RegisterWorkflowResultSuccess):
            err_str = f"Error attempting to register workflow '{workflow_to_register}': {workflow_register_result}. SKIPPING IT."
            logger.error(err_str)
            return False

        return True

    async def run_workflow(self, relative_file_path: str) -> WorkflowExecutionResult:
        """Kept for node libraries that call it. See `WorkflowRunner.run_workflow`."""
        return await self.runner.run_workflow(relative_file_path)

    def extract_workflow_shape(self, workflow_name: str, flow_name: str | None = None) -> dict[str, Any]:
        """Extracts the input and output shape for a workflow. See `shape.extract_workflow_shape`."""
        return extract_workflow_shape(self.engine.flow_manager, workflow_name, flow_name)

    def _walk_object_tree(
        self, obj: Any, process_class_fn: Callable[[type, Any], None], visited: set[int] | None = None
    ) -> None:
        """Kept for node libraries that call it. See `WorkflowCodeGenerator.walk_object_tree`."""
        self.codegen.walk_object_tree(obj, process_class_fn, visited)

    @handles(RefreshWorkflowRegistryRequest)
    async def on_refresh_workflow_registry_request(self, _request: RefreshWorkflowRegistryRequest) -> ResultPayload:
        try:
            await self.refresh_workflow_registry()
        except Exception as e:
            return RefreshWorkflowRegistryResultFailure(result_details=f"Failed to refresh workflow registry: {e!s}")
        return RefreshWorkflowRegistryResultSuccess(result_details="Workflow registry refreshed successfully.")

    @handles(RegisterWorkflowsFromConfigRequest)
    async def on_register_workflows_from_config_request(
        self, request: RegisterWorkflowsFromConfigRequest
    ) -> ResultPayload:
        """Register workflows from a configuration section."""
        try:
            workflows_to_register = self.engine.config_manager.get_config_value(request.config_section)
            if not workflows_to_register:
                details = f"No workflows found in configuration section '{request.config_section}'"
                return RegisterWorkflowsFromConfigResultSuccess(
                    succeeded_workflows=[], failed_workflows=[], result_details=details
                )

            # Process all workflows and track results
            succeeded, failed = await self._process_workflows_for_registration(workflows_to_register)

        except Exception as e:
            details = f"Failed to register workflows from configuration section '{request.config_section}': {e!s}"
            return RegisterWorkflowsFromConfigResultFailure(result_details=details)
        else:
            return RegisterWorkflowsFromConfigResultSuccess(
                succeeded_workflows=succeeded,
                failed_workflows=failed,
                result_details=ResultDetails(
                    message=f"Successfully processed workflows: {len(succeeded)} succeeded, {len(failed)} failed.",
                    level=logging.INFO,
                ),
            )

    async def _process_workflows_for_registration(  # noqa: C901
        self, workflows_to_register: list[str]
    ) -> WorkflowRegistrationResult:
        """Process a list of workflow paths for registration.

        Returns:
            WorkflowRegistrationResult with succeeded and failed workflow names
        """
        succeeded = []
        failed = []

        # Build the set of registered-library roots (excluding sandbox) so their bundled
        # workflow files are skipped during the workspace scan. Library-declared workflows
        # (listed in griptape_nodes_library.json) are registered separately via
        # LibraryManager._collect_library_workflow_files before this scan runs. Sandbox
        # libraries are intentionally left scannable so in-development workflows appear.
        library_exclusion_roots: list[Path] = []
        for library_info in self.engine.library_manager._library_file_path_to_info.values():
            if library_info.is_sandbox:
                continue
            library_exclusion_roots.append(Path(library_info.library_path).parent.resolve())

        # First pass: collect all workflow files to determine total count
        all_workflow_files: set[Path] = set()

        async def collect_workflow_files(path: Path) -> None:  # noqa: C901
            """Collect workflow files from a path."""
            apath = anyio.Path(path)
            if not await apath.exists():
                return
            if await apath.is_dir():
                # find_files_recursive skips hidden directories (.venv, .git) and
                # bounds recursion depth, so a deep or symlink-looped tree can't stall
                # the boot scan.
                for workflow_file in await find_files_recursive(
                    path, "*.py", max_depth=self.engine.config_manager.discovery_max_depth
                ):
                    # Unsaved workflows are ephemeral; any file with this prefix is a
                    # leak from a pre-fix save and cannot be registered (the registry
                    # rejects unsaved keys paired with a file path).
                    if workflow_file.name.startswith(self.engine.workflow_registry.UNSAVED_KEY_PREFIX):
                        continue
                    if library_exclusion_roots:
                        resolved_workflow_file = workflow_file.resolve()
                        if any(resolved_workflow_file.is_relative_to(root) for root in library_exclusion_roots):
                            continue
                    # Check if file has workflow metadata
                    try:
                        metadata_blocks = self.get_workflow_metadata(
                            workflow_file, block_name=WorkflowManager.WORKFLOW_METADATA_HEADER
                        )
                        if len(metadata_blocks) == 1:
                            all_workflow_files.add(workflow_file)
                    except Exception as e:
                        # Skip files that can't be read or parsed
                        logger.debug("Skipping workflow file %s due to error: %s", workflow_file, e)
                        continue
            elif path.suffix == ".py":
                try:
                    metadata_blocks = self.get_workflow_metadata(
                        path, block_name=WorkflowManager.WORKFLOW_METADATA_HEADER
                    )
                    if len(metadata_blocks) == 1:
                        all_workflow_files.add(path)
                except Exception as e:
                    logger.debug("Skipping workflow file %s due to error: %s", path, e)

        # Collect all workflow files first
        for workflow_to_register in workflows_to_register:
            await collect_workflow_files(Path(workflow_to_register))

        # Track progress
        total_workflows = len(all_workflow_files)

        # Second pass: process each workflow file with progress events
        for current_index, workflow_file in enumerate(all_workflow_files, start=1):
            workflow_name = str(workflow_file.name)

            # Emit loading event
            self.engine.event_manager.put_event(
                AppEvent(
                    payload=EngineInitializationProgress(
                        phase=InitializationPhase.WORKFLOWS,
                        item_name=workflow_name,
                        status=InitializationStatus.LOADING,
                        current=current_index,
                        total=total_workflows,
                    )
                )
            )

            # Process the workflow
            result_name = await self._process_single_workflow_file(workflow_file)
            if result_name:
                succeeded.append(result_name)
                # Emit success event
                self.engine.event_manager.put_event(
                    AppEvent(
                        payload=EngineInitializationProgress(
                            phase=InitializationPhase.WORKFLOWS,
                            item_name=workflow_name,
                            status=InitializationStatus.COMPLETE,
                            current=current_index,
                            total=total_workflows,
                        )
                    )
                )
            else:
                failed.append(str(workflow_file))
                # Emit failure event
                self.engine.event_manager.put_event(
                    AppEvent(
                        payload=EngineInitializationProgress(
                            phase=InitializationPhase.WORKFLOWS,
                            item_name=workflow_name,
                            status=InitializationStatus.FAILED,
                            current=current_index,
                            total=total_workflows,
                            error="Failed to process workflow file",
                        )
                    )
                )

        return WorkflowRegistrationResult(succeeded=succeeded, failed=failed)

    async def _process_single_workflow_file(self, workflow_file: Path) -> str | None:
        """Process a single workflow file for registration.

        Returns:
            Workflow name if registered successfully, None if failed or skipped
        """
        # Parse metadata once and use it for both registration check and actual registration
        load_metadata_request = LoadWorkflowMetadata(file_name=str(workflow_file))
        load_metadata_result = await self.on_load_workflow_metadata_request(load_metadata_request)

        if not isinstance(load_metadata_result, LoadWorkflowMetadataResultSuccess):
            logger.debug("Skipping workflow with invalid metadata: %s", workflow_file)
            return None

        # Convert to relative path if the workflow is under workspace_path before checking registry
        config_mgr = self.engine.config_manager
        workspace_path = config_mgr.workspace_path

        if workflow_file.is_relative_to(workspace_path):
            relative_path = workflow_file.relative_to(workspace_path)
            file_path_to_register = str(relative_path)
        else:
            file_path_to_register = str(workflow_file)

        registry_key = derive_registry_key(file_path_to_register)

        # Check if workflow is already registered using the path-based registry key
        if self.engine.workflow_registry.has_workflow_with_name(registry_key):
            logger.debug("Skipping already registered workflow: %s", workflow_file)
            return None

        # Hand the already-parsed metadata to the registrar so the file's TOML header is
        # read once per workflow rather than twice.
        if self._register_workflow(file_path_to_register, load_metadata_result.metadata):
            return registry_key
        return None
