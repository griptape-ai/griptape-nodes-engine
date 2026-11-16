from __future__ import annotations

import logging
import sys
from collections import defaultdict
from contextlib import nullcontext
from dataclasses import dataclass
from inspect import iscoroutinefunction
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

import anyio

from griptape_nodes.files.path_utils import (
    resolve_workspace_path,
)
from griptape_nodes.node_library.workflow_registry import WorkflowAddressError
from griptape_nodes.retained_mode.engine import EngineScoped
from griptape_nodes.retained_mode.events.base_events import ResultDetail, ResultDetails
from griptape_nodes.retained_mode.events.context_events import (
    InvalidWorkingDirectoryResultFailure,
    OpenWorkflowRequest,
    OpenWorkflowResultFailure,
    OpenWorkflowResultSuccess,
    WorkflowNotFoundResultFailure,
)
from griptape_nodes.retained_mode.events.library_events import (
    RegisterLibraryFromFileRequest,
    RegisterLibraryFromFileResultFailure,
)
from griptape_nodes.retained_mode.events.os_events import (
    GetFileInfoRequest,
    GetFileInfoResultFailure,
    GetFileInfoResultSuccess,
)
from griptape_nodes.retained_mode.events.workflow_events import (
    GetWorkflowRunCommandRequest,
    GetWorkflowRunCommandResultFailure,
    GetWorkflowRunCommandResultSuccess,
    LoadWorkflowMetadata,
    LoadWorkflowMetadataResultSuccess,
    RunWorkflowFromRegistryRequest,
    RunWorkflowFromRegistryResultFailure,
    RunWorkflowFromRegistryResultSuccess,
    RunWorkflowFromScratchRequest,
    RunWorkflowFromScratchResultFailure,
    RunWorkflowFromScratchResultSuccess,
    RunWorkflowWithCurrentStateRequest,
    RunWorkflowWithCurrentStateResultFailure,
    RunWorkflowWithCurrentStateResultSuccess,
    WorkflowStatus,
)
from griptape_nodes.retained_mode.managers.context_manager import ContextManager
from griptape_nodes.retained_mode.managers.event_manager import EventSuppressionContext
from griptape_nodes.retained_mode.managers.fitness_problems.workflows import (
    LibraryNotRegisteredProblem,
)
from griptape_nodes.retained_mode.managers.os_manager import OSManager
from griptape_nodes.retained_mode.managers.workflow.loading import LoadProblemFrame
from griptape_nodes.retained_mode.request_handlers import handles
from griptape_nodes.retained_mode.runtime.binding import NO_WORKFLOW
from griptape_nodes.retained_mode.runtime.open_workflow_set import OpenWorkflowError

if TYPE_CHECKING:
    from collections.abc import Iterable

    from griptape_nodes.node_library.workflow_registry import (
        Workflow,
        WorkflowShape,
    )
    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.events.app_events import OpenWorkflowInfo
    from griptape_nodes.retained_mode.events.base_events import ResultPayload
    from griptape_nodes.retained_mode.managers.event_manager import EventManager
    from griptape_nodes.retained_mode.managers.fitness_problems.workflows.workflow_problem import WorkflowProblem
    from griptape_nodes.retained_mode.runtime.workflow_runtime import WorkflowRuntime


logger = logging.getLogger("griptape_nodes")


class WorkflowExecutionResult(NamedTuple):
    """Result of a workflow execution.

    `status` and `problems` mirror WorkflowInfo's fields, so a load's fitness is described
    the same way whether it was assessed from the metadata header or observed while
    replaying the file. Both are populated whether or not the run succeeded: a library that
    never loads leaves the load FLAWED (its nodes come back as placeholders), and is the
    likeliest explanation when the load fails outright.

    Keeping the problems typed rather than pre-rendered lets a caller decide per problem
    class -- an executor can refuse a FLAWED load that the editor is happy to open -- and
    leaves the wording to each problem's own collate_problems_for_display.
    """

    execution_successful: bool
    execution_details: str
    status: WorkflowStatus = WorkflowStatus.GOOD
    problems: tuple[WorkflowProblem, ...] = ()
    # The workflow the file built into, which may have closed since, or None when it opened none.
    workflow_id: str | None = None


@dataclass(frozen=True)
class OpenedWorkflowBuild:
    """The workflow `WorkflowRunner.open_and_build_workflow` returned.

    Args:
        workflow_id: Its id. Closed again when its build failed.
        already_open: Whether it was open before, and so was returned unchanged.
        execution_result: The run of its file, or None when it was not built from one.
    """

    workflow_id: str
    already_open: bool
    execution_result: WorkflowExecutionResult | None

    @property
    def build_failed(self) -> bool:
        return self.execution_result is not None and not self.execution_result.execution_successful


def execution_result_details(
    execution_result: WorkflowExecutionResult, *, level: int, message: str | None = None
) -> list[ResultDetail]:
    """The run's problems as warnings, ahead of its detail (or `message` in its place).

    The problems come first: they explain both the placeholders on a successful load and,
    on a failed one, the most likely reason the file could not be replayed. Every handler
    that consumes a WorkflowExecutionResult reports them, or the load looks clean to the
    caller while its graph is quietly full of placeholders.
    """
    details = [
        ResultDetail(message=problem, level=logging.WARNING)
        for problem in collate_problems_by_type(execution_result.problems)
    ]
    # Only a load that survived has placeholders to point at; a failed one cleared the
    # canvas. Said once for the whole load rather than per problem, which is what keeps
    # the problems' own wording intact.
    if execution_result.problems and execution_result.execution_successful:
        details.append(
            ResultDetail(
                message="Nodes from the libraries above opened as placeholders. "
                "They preserve the graph but cannot run until their library is available.",
                level=logging.WARNING,
            )
        )
    details.append(ResultDetail(message=message or execution_result.execution_details, level=level))
    return details


def collate_problems_by_type(problems: Iterable[WorkflowProblem]) -> list[str]:
    """Group problems by type and let each type render its own instances, one string per group.

    Every problem class owns its wording and its singular/plural form, so grouping is what
    lets a workflow with five unregistered libraries say so once instead of five times.
    """
    problems_by_type: dict[type, list[WorkflowProblem]] = defaultdict(list)
    for problem in problems:
        problems_by_type[type(problem)].append(problem)
    return [
        problem_class.collate_problems_for_display(instances) for problem_class, instances in problems_by_type.items()
    ]


class WorkflowRunner(EngineScoped):
    def __init__(self, event_manager: EventManager, *, engine: Engine | None = None) -> None:
        super().__init__(engine)
        event_manager.register_request_handlers(self)

    async def run_workflow(self, relative_file_path: str, workflow: WorkflowRuntime | None) -> WorkflowExecutionResult:
        """Run a saved workflow file's `build_workflow()` into the open `workflow`, or with None, into the file opened as one.

        A saved file never decides whether to open a workflow; its caller does. The file's requests
        name no workflow, so the run binds the workflow it builds into, and the binding ends with
        the run. The result names that workflow. With None, the file opens through
        `open_and_build_workflow`, so a concurrent open of it waits for the build.
        """
        # Resolve path using utility function
        workspace_path = self.engine.config_manager.workspace_path
        complete_file_path = resolve_workspace_path(Path(relative_file_path), workspace_path)
        if workflow is None:
            return await self._open_file_and_build(complete_file_path)
        # Problems found anywhere under this load land in the frame, including those of a
        # referenced subflow imported from inside exec() -- see LoadProblemFrame.
        # Bound to no workflow; the run binds `workflow` around the file's own requests.
        with LoadProblemFrame() as frame, self.engine.bind_workflow(NO_WORKFLOW):
            return await self._run_workflow_in_frame(
                relative_file_path=relative_file_path,
                complete_file_path=complete_file_path,
                frame=frame,
                workflow=workflow,
            )

    async def _open_file_and_build(self, complete_file_path: Path) -> WorkflowExecutionResult:
        """Open the file at `complete_file_path` as a workflow and build it, reporting a refused open as a failed run."""
        try:
            # Bound to no workflow, so opening the file never sees the caller's.
            with self.engine.bind_workflow(NO_WORKFLOW):
                built = await self.open_and_build_workflow(file_path=str(complete_file_path))
        except ContextManager.OpenRefusedError as err:
            return WorkflowExecutionResult(
                execution_successful=False, execution_details=str(err), status=WorkflowStatus.UNUSABLE
            )
        if built.already_open or built.execution_result is None:
            details = f"Attempted to run workflow file '{complete_file_path}'. Failed because it is already open."
            return WorkflowExecutionResult(
                execution_successful=False, execution_details=details, status=WorkflowStatus.UNUSABLE
            )
        return built.execution_result

    @handles(RunWorkflowFromScratchRequest)
    async def on_run_workflow_from_scratch_request(self, request: RunWorkflowFromScratchRequest) -> ResultPayload:
        """Close what a clean slate closes (`_clear_slate_for`), then open the file as a workflow."""
        # Squelch any ResultPayloads that indicate the workflow was changed, because we are loading it into a blank slate.
        with self.engine.workflow_manager.squelch_workflow_altered():
            # Check if file path exists
            relative_file_path = request.file_path
            complete_file_path = self.engine.workflow_registry.get_complete_file_path(
                relative_file_path=relative_file_path
            )
            if not await anyio.Path(complete_file_path).is_file():
                details = f"Failed to find file. Path '{complete_file_path}' doesn't exist."
                return RunWorkflowFromScratchResultFailure(result_details=details)

            target = self.engine.workflow_registry.find_by_path(str(complete_file_path))
            close_failure = await self._clear_slate_for(target)
            if close_failure is not None:
                details = f"Attempted to run '{complete_file_path}'. Failed because {close_failure}"
                return RunWorkflowFromScratchResultFailure(result_details=details)

            # Opens by the absolute path, as a standalone run does (`push_workflow(file_path=__file__)`),
            # so both find the same registry entry.
            execution_result = await self.run_workflow(relative_file_path=relative_file_path, workflow=None)
            if not execution_result.execution_successful:
                return RunWorkflowFromScratchResultFailure(
                    result_details=ResultDetails(*execution_result_details(execution_result, level=logging.ERROR))
                )
            self._mark_clean_slate_copy(target, execution_result.workflow_id)
            return RunWorkflowFromScratchResultSuccess(
                status=execution_result.status,
                workflow_id=execution_result.workflow_id,
                open_workflow=self._open_workflow_info(execution_result.workflow_id),
                result_details=ResultDetails(*execution_result_details(execution_result, level=logging.DEBUG)),
            )

    @handles(RunWorkflowWithCurrentStateRequest)
    async def on_run_workflow_with_current_state_request(
        self, request: RunWorkflowWithCurrentStateRequest, workflow: WorkflowRuntime | None = None
    ) -> ResultPayload:
        """Build the file into `workflow`, or with None, open the file as a workflow."""
        relative_file_path = request.file_path
        if workflow is not None and workflow.context.has_current_flow():
            # Disallow opening a workflow inside another workflow this way. It would
            # become an invisible child flow (no way to reach it in the UI), persisted
            # with the parent workflow on save, and executed invisibly whenever the
            # parent workflow was executed.
            open_flow_name = workflow.context.get_current_flow().name
            details = (
                f"Attempted to open workflow '{relative_file_path}' while the flow '{open_flow_name}' is still open. "
                "Close the current workflow first, before opening this one."
            )
            return RunWorkflowWithCurrentStateResultFailure(result_details=details)

        complete_file_path = self.engine.workflow_registry.get_complete_file_path(relative_file_path=relative_file_path)
        if not await anyio.Path(complete_file_path).is_file():
            details = f"Failed to find file. Path '{complete_file_path}' doesn't exist."
            return RunWorkflowWithCurrentStateResultFailure(result_details=details)
        execution_result = await self.run_workflow(relative_file_path=relative_file_path, workflow=workflow)

        if execution_result.execution_successful:
            return RunWorkflowWithCurrentStateResultSuccess(
                status=execution_result.status,
                workflow_id=execution_result.workflow_id,
                open_workflow=self._open_workflow_info(execution_result.workflow_id),
                result_details=ResultDetails(*execution_result_details(execution_result, level=logging.DEBUG)),
            )
        return RunWorkflowWithCurrentStateResultFailure(
            result_details=ResultDetails(*execution_result_details(execution_result, level=logging.ERROR))
        )

    @handles(RunWorkflowFromRegistryRequest)
    async def on_run_workflow_from_registry_request(  # noqa: PLR0911
        self, request: RunWorkflowFromRegistryRequest
    ) -> ResultPayload:
        """Open the registered workflow and run its file into it.

        With a clean slate, first closes what `_clear_slate_for` closes.
        """
        await self.engine.workflow_manager.wait_for_workflows_loaded()

        # get workflow from registry
        try:
            registered = self.engine.workflow_registry.get_workflow_by_name(request.workflow_name)
        except KeyError:
            details = f"Failed to get workflow '{request.workflow_name}' from registry."
            return RunWorkflowFromRegistryResultFailure(result_details=details)

        # RunWorkflowFromRegistry is file-based (it re-executes the serialized .py on disk).
        # Unsaved workflows have no file; callers should use StartFlowRequest against the
        # live flow for in-memory execution. Keep this reject explicit so the UI can
        # distinguish "run this unsaved edit" (StartFlow) from "run the last saved version".
        relative_file_path = registered.file_path
        if relative_file_path is None:
            details = (
                f"Cannot run unsaved workflow '{request.workflow_name}' from the registry because it has no file on disk. "
                "Save the workflow first, or use StartFlowRequest to execute the current in-memory flow."
            )
            return RunWorkflowFromRegistryResultFailure(result_details=details)

        # Squelch any ResultPayloads that indicate the workflow was changed, because we are loading it.
        with self.engine.workflow_manager.squelch_workflow_altered():
            if request.run_with_clean_slate:
                # Refused before anything is registered or run.
                close_failure = await self._clear_slate_for(registered)
                if close_failure is not None:
                    details = f"Attempted to run workflow '{request.workflow_name}'. Failed because {close_failure}"
                    return RunWorkflowFromRegistryResultFailure(result_details=details)

            try:
                built = await self.open_and_build_workflow(workflow_name=request.workflow_name)
            except ContextManager.OpenRefusedError as err:
                return RunWorkflowFromRegistryResultFailure(result_details=str(err))
            if built.already_open:
                details = (
                    f"Attempted to run workflow '{request.workflow_name}'. Failed because it is already open. "
                    "Close it first."
                )
                return RunWorkflowFromRegistryResultFailure(result_details=details)
            execution_result = built.execution_result
            if execution_result is None:
                details = (
                    f"Attempted to run workflow '{request.workflow_name}'. Failed because it opened without its file."
                )
                return RunWorkflowFromRegistryResultFailure(result_details=details)
            if not execution_result.execution_successful:
                return RunWorkflowFromRegistryResultFailure(
                    result_details=ResultDetails(*execution_result_details(execution_result, level=logging.ERROR))
                )
            if request.run_with_clean_slate:
                self._mark_clean_slate_copy(registered, built.workflow_id)

        return RunWorkflowFromRegistryResultSuccess(
            status=execution_result.status,
            workflow_id=built.workflow_id,
            open_workflow=self._open_workflow_info(built.workflow_id),
            result_details=ResultDetails(*execution_result_details(execution_result, level=logging.DEBUG)),
        )

    @handles(OpenWorkflowRequest)
    async def on_open_workflow_request(self, request: OpenWorkflowRequest) -> ResultPayload:
        """Return the workflow the request names if it is open, else open it and build its file into it."""
        await self.engine.workflow_manager.wait_for_workflows_loaded()
        with self.engine.workflow_manager.squelch_workflow_altered():
            try:
                built = await self.open_and_build_workflow(
                    workflow_id=request.workflow_id,
                    workflow_name=request.workflow_name,
                    display_name=request.display_name,
                    working_directory=request.working_directory,
                    create_flow=request.create_flow,
                )
            except ContextManager.RequestedWorkflowNotFoundError as err:
                return WorkflowNotFoundResultFailure(result_details=str(err))
            except ContextManager.InvalidWorkingDirectoryError as err:
                return InvalidWorkingDirectoryResultFailure(result_details=str(err))
            except ContextManager.OpenRefusedError as err:
                return OpenWorkflowResultFailure(result_details=str(err))

        execution_result = built.execution_result
        if execution_result is not None and not execution_result.execution_successful:
            return OpenWorkflowResultFailure(
                result_details=ResultDetails(*execution_result_details(execution_result, level=logging.ERROR))
            )
        workflow = self.engine.open_workflows.get(built.workflow_id)
        workflow_name = self.engine.workflow_registry.get(built.workflow_id).name
        status = WorkflowStatus.GOOD
        details = ResultDetails(message=f"Opened workflow '{workflow_name}'.", level=logging.DEBUG)
        if built.already_open:
            details = ResultDetails(message=f"Workflow '{workflow_name}' is already open.", level=logging.DEBUG)
        elif execution_result is not None:
            status = execution_result.status
            details = ResultDetails(*execution_result_details(execution_result, level=logging.DEBUG))
        return OpenWorkflowResultSuccess(
            open_workflow=self.engine.open_workflow_info(workflow),
            already_open=built.already_open,
            status=status,
            result_details=details,
        )

    async def open_and_build_workflow(  # noqa: PLR0913
        self,
        *,
        workflow_id: str | None = None,
        workflow_name: str | None = None,
        file_path: str | None = None,
        display_name: str | None = None,
        working_directory: str | None = None,
        create_flow: bool = False,
    ) -> OpenedWorkflowBuild:
        """Open the workflow a request names (`ContextManager.opening_requested_workflow`) and build its file into it.

        A workflow already open comes back unchanged, once any build another request started has
        ended. A workflow with a file is built from it. `create_flow` gives the workflow its
        top-level flow when it has none once built. A build that fails or is cancelled closes the
        workflow again, before a concurrent open of it stops waiting (see `Engine.opening_workflow`).

        Raises:
            ContextManager.OpenRefusedError: The open was refused, as `opening_requested_workflow`
                raises, the flow `create_flow` asked for could not be created, or the workflow was
                already open and the build another request started failed.
        """
        with self.engine.context_manager.opening_requested_workflow(
            workflow_id=workflow_id,
            workflow_name=workflow_name,
            file_path=file_path,
            display_name=display_name,
            working_directory=working_directory,
        ) as opened:
            if not opened.already_open:
                built = await self._build_opened_workflow(opened.workflow, file_path=file_path, create_flow=create_flow)
                if built.build_failed:
                    opened.abandon()
                return built
        await self._wait_for_build(opened.workflow)
        return OpenedWorkflowBuild(workflow_id=opened.workflow.workflow_id, already_open=True, execution_result=None)

    async def _build_opened_workflow(
        self, workflow: WorkflowRuntime, *, file_path: str | None, create_flow: bool
    ) -> OpenedWorkflowBuild:
        """Build the file of `workflow`, which is being opened, into it.

        Raises:
            ContextManager.OpenRefusedError: `create_flow` asked for a flow that could not be created.
        """
        relative_file_path = file_path
        if relative_file_path is None:
            relative_file_path = self._file_to_build(workflow)
        execution_result = None
        if relative_file_path is not None:
            execution_result = await self.run_workflow(relative_file_path=relative_file_path, workflow=workflow)
            if not execution_result.execution_successful:
                return OpenedWorkflowBuild(
                    workflow_id=workflow.workflow_id, already_open=False, execution_result=execution_result
                )
        if create_flow and not workflow.context.has_current_flow():
            self.engine.context_manager.create_top_level_flow_or_refuse(workflow, flow_name=None)
        return OpenedWorkflowBuild(
            workflow_id=workflow.workflow_id, already_open=False, execution_result=execution_result
        )

    def _file_to_build(self, workflow: WorkflowRuntime) -> str | None:
        """The file an open of `workflow` builds into it: its own, or for a library workflow's unsaved copy, the library's."""
        registry = self.engine.workflow_registry
        entry = registry.get(workflow.workflow_id)
        if entry.file_path is not None:
            return entry.file_path
        seed_source = registry.seed_source(entry)
        if seed_source is None:
            return None
        return seed_source.file_path

    @handles(GetWorkflowRunCommandRequest)
    async def on_get_workflow_run_command_request(  # noqa: C901, PLR0911, PLR0912
        self, request: GetWorkflowRunCommandRequest, workflow: WorkflowRuntime | None = None
    ) -> ResultPayload:
        """The command line that runs the saved workflow or file the request names, else the open `workflow`."""
        workflow_name = request.workflow_name
        workflow_id = request.workflow_id
        file_path = request.file_path

        # Failure: no identifier and no current context
        if workflow_name is None and workflow_id is None and file_path is None:
            if workflow is None:
                return GetWorkflowRunCommandResultFailure(
                    result_details=(
                        "Attempted to get workflow run command. Failed with workflow_name=None, file_path=None "
                        "because no workflow is loaded in the current context. Provide workflow_name or file_path, or load a workflow."
                    )
                )
            # When neither workflow_name nor file_path is provided, use the open workflow as a fallback.
            workflow_name = self.engine.workflow_registry.get(workflow.workflow_id).name

        names_saved_workflow = workflow_name is not None or workflow_id is not None
        # Failure: both a saved workflow and file_path provided
        if names_saved_workflow and file_path is not None:
            return GetWorkflowRunCommandResultFailure(
                result_details=(
                    "Attempted to get workflow run command. Failed with both workflow_name and file_path provided "
                    "because only one may be provided. Provide workflow_name or file_path, not both."
                )
            )

        # Resolve relative_file_path and workflow_shape (or fail)
        workflow_shape: WorkflowShape | None = None
        if names_saved_workflow:
            try:
                saved = self.engine.workflow_registry.resolve_address(
                    workflow_id=workflow_id, workflow_name=workflow_name, action="get the run command for"
                )
            except WorkflowAddressError as err:
                return GetWorkflowRunCommandResultFailure(
                    result_details=f"{err} Save the workflow first, or provide file_path."
                )
            if saved.file_path is None:
                # Such as the open workflow when it is a Griptape-provided workflow's unsaved copy.
                return GetWorkflowRunCommandResultFailure(
                    result_details=(
                        f"Attempted to get the run command for workflow '{saved.name}'. Failed because the "
                        "workflow isn't saved, so it has no file to run. Save the workflow first."
                    )
                )
            relative_file_path = saved.file_path
            workflow_shape = saved.metadata.workflow_shape
        else:
            relative_file_path = file_path

        # Failure: path still missing after resolution
        if relative_file_path is None:
            return GetWorkflowRunCommandResultFailure(
                result_details=(
                    "Attempted to get workflow run command. Failed with no resolvable file path "
                    "because neither workflow_name nor file_path was provided. Provide workflow_name or file_path."
                )
            )

        complete_file_path = self.engine.workflow_registry.get_complete_file_path(relative_file_path)

        # Failure: workflow file does not exist or is not a file (use GetFileInfoRequest for consistency)
        get_file_info_result = self.engine.handle_request(
            GetFileInfoRequest(path=relative_file_path, workspace_only=True)
        )
        if isinstance(get_file_info_result, GetFileInfoResultFailure):
            return GetWorkflowRunCommandResultFailure(
                result_details=(
                    f"Attempted to get workflow run command. Failed with file_path='{complete_file_path}' "
                    f"because file info could not be retrieved: {get_file_info_result.result_details}"
                )
            )
        if not isinstance(get_file_info_result, GetFileInfoResultSuccess):
            return GetWorkflowRunCommandResultFailure(
                result_details=(
                    f"Attempted to get workflow run command. Failed with file_path='{complete_file_path}' "
                    "because file info could not be retrieved."
                )
            )
        file_entry = get_file_info_result.file_entry
        if file_entry is None:
            return GetWorkflowRunCommandResultFailure(
                result_details=(
                    f"Attempted to get workflow run command. Failed with file_path='{complete_file_path}' "
                    "because the workflow file does not exist."
                )
            )
        if file_entry.is_dir:
            return GetWorkflowRunCommandResultFailure(
                result_details=(
                    f"Attempted to get workflow run command. Failed with file_path='{complete_file_path}' "
                    "because the path is a directory, not a workflow file."
                )
            )

        # Optional: load workflow_shape from file when resolved by file_path only
        if workflow_shape is None:
            load_metadata_request = LoadWorkflowMetadata(file_name=relative_file_path)
            load_metadata_result = await self.engine.workflow_manager.on_load_workflow_metadata_request(
                load_metadata_request
            )
            if isinstance(load_metadata_result, LoadWorkflowMetadataResultSuccess):
                workflow_shape = load_metadata_result.metadata.workflow_shape

        # Failure: workflow has no Start/End nodes (or metadata could not be loaded)
        if workflow_shape is None:
            return GetWorkflowRunCommandResultFailure(
                result_details=(
                    f"Attempted to get workflow run command. Failed with file_path='{complete_file_path}' "
                    "because the workflow has no Start or End nodes. Add Start and End nodes to run from the command line."
                )
            )

        # Success path at end: quote paths so run_command works on Windows (spaces in path) and when copy-pasted into a shell
        run_command = OSManager.format_command_line([sys.executable, str(complete_file_path)])
        return GetWorkflowRunCommandResultSuccess(
            run_command=run_command,
            workflow_shape=workflow_shape,
            engine_os=self.engine.os_manager.platform_name(),
            result_details=ResultDetails(message=f"Run command: {run_command}", level=logging.DEBUG),
        )

    async def _wait_for_build(self, workflow: WorkflowRuntime) -> None:
        """Wait for the build of `workflow` that another request started, if one is under way.

        Raises:
            ContextManager.OpenRefusedError: That build failed or was cancelled, which closed the workflow.
        """
        await self.engine.wait_for_opening(workflow)
        if self.engine.open_workflows.find(workflow.workflow_id) is workflow:
            return
        entry = self.engine.workflow_registry.find_by_id(workflow.workflow_id)
        label = workflow.workflow_id
        if entry is not None:
            label = entry.name
        msg = (
            f"Attempted to open workflow '{label}'. Failed because another request was opening it "
            "and did not finish building it."
        )
        raise ContextManager.OpenRefusedError(msg)

    def _open_workflow_info(self, workflow_id: str | None) -> OpenWorkflowInfo | None:
        """What clients are told of the workflow a load built into, or None when there is none or it closed since."""
        if workflow_id is None:
            return None
        opened = self.engine.open_workflows.find(workflow_id)
        if opened is None:
            return None
        return self.engine.open_workflow_info(opened)

    async def _clear_slate_for(self, target: Workflow | None) -> str | None:
        """Close what a clean slate closes before `target` opens, returning why it could not, or None.

        A clean slate starts `target` over, so it closes `target` when it is open. A library
        workflow is never open itself, so for one it closes the copy the last clean slate run of it
        opened, if that is still open. Copies a tab opened, and other open workflows, stay open.
        """
        if target is None:
            return None
        to_close = self._open_workflow_started_from(target)
        if to_close is None:
            return None
        to_close_name = self.engine.workflow_registry.get(to_close.workflow_id).name
        try:
            await self.engine.aclose_workflow(to_close)
        except OpenWorkflowError as err:
            return f"closing the open workflow '{to_close_name}' failed: {err}"
        return None

    def _open_workflow_started_from(self, target: Workflow) -> WorkflowRuntime | None:
        """`target` if it is open, else the unsaved copy of it a clean slate run opened, if still open.

        A copy saved since has no `seed_source`: it is the user's own workflow now, not one to start over.
        """
        open_workflows = self.engine.open_workflows
        target_open = open_workflows.find(target.workflow_id)
        if target_open is not None:
            return target_open
        registry = self.engine.workflow_registry
        for workflow in open_workflows.all_open():
            entry = registry.get(workflow.workflow_id)
            if entry.opened_by_clean_slate_run and registry.seed_source(entry) is target:
                return workflow
        return None

    def _mark_clean_slate_copy(self, target: Workflow | None, opened_workflow_id: str | None) -> None:
        """Mark the workflow a clean slate run of `target` opened, when it is an unsaved copy of `target`."""
        if target is None or opened_workflow_id is None:
            return
        entry = self.engine.workflow_registry.find_by_id(opened_workflow_id)
        if entry is None or self.engine.workflow_registry.seed_source(entry) is not target:
            return
        entry.opened_by_clean_slate_run = True

    async def _ensure_workflow_context_established(self, workflow: WorkflowRuntime) -> None:
        """Ensure `workflow`, which a file just built into, has a current flow."""
        if not workflow.context.has_current_flow():
            # Use the proper request to get the top-level flow
            from griptape_nodes.retained_mode.events.flow_events import (
                GetTopLevelFlowRequest,
                GetTopLevelFlowResultSuccess,
            )

            top_level_flow_request = GetTopLevelFlowRequest()
            top_level_flow_result = await workflow.ahandle_request(top_level_flow_request)

            if (
                isinstance(top_level_flow_result, GetTopLevelFlowResultSuccess)
                and top_level_flow_result.flow_name is not None
            ):
                # Push the flow to the context stack permanently using FlowManager
                flow_manager = self.engine.flow_manager
                flow_obj = flow_manager.get_flow_by_name_in(workflow, top_level_flow_result.flow_name)
                workflow.context.push_flow(flow_obj)
                details = f"Workflow execution completed. Set '{top_level_flow_result.flow_name}' as current context."
                logger.debug(details)

            # If we still don't have a flow, that's a critical error
            if not workflow.context.has_current_flow():
                error_message = "Workflow execution completed but no current flow context could be established"
                raise RuntimeError(error_message)

    async def _run_workflow_in_frame(
        self,
        *,
        relative_file_path: str,
        complete_file_path: Path,
        frame: LoadProblemFrame,
        workflow: WorkflowRuntime,
    ) -> WorkflowExecutionResult:
        """Read, resolve libraries for, and exec one workflow file into `workflow`, recording problems in `frame`."""
        try:
            async with await anyio.open_file(Path(complete_file_path), encoding="utf-8") as file:
                workflow_content = await file.read()

            # Resolve the workflow's declared library dependencies before exec.
            # The metadata header lists every library the workflow uses; each must
            # be registered (discovery is triggered if needed) so node construction
            # inside the script can succeed.
            frame.problems.extend(await self._ensure_libraries_for_workflow(relative_file_path=relative_file_path))

            # _generate_workflow_run_prerequisite_code emits one registration per header entry,
            # so each library we just failed to register is about to fail again on a request the
            # GUI would toast -- that equivalence is what makes suppressing the type here safe.
            # It is conditional because an unreadable header pre-registers nothing: there the
            # in-file failures are the only record of what the workflow needs.
            duplicate_library_failures = (
                EventSuppressionContext(self.engine.event_manager, {RegisterLibraryFromFileResultFailure})
                if any(isinstance(problem, LibraryNotRegisteredProblem) for problem in frame.problems)
                else nullcontext()
            )
            with duplicate_library_failures, self.engine.bind_workflow(workflow):
                # Execute the workflow module with a dedicated namespace so `__file__` resolves
                # to the workflow path and the `if __name__ == "__main__"` guard does not fire
                # (which would try to spin up a second event loop via asyncio.run).
                namespace: dict[str, Any] = {
                    "__file__": str(complete_file_path),
                    "__name__": "__gtn_workflow__",
                }
                exec(workflow_content, namespace)  # noqa: S102

                # New-style workflows wrap graph-building requests in `async def build_workflow()`
                # so the module is inert at import time. Await it here. Legacy workflows without
                # build_workflow() have already executed their requests top-to-bottom during exec().
                workflow_builder = namespace.get("build_workflow")
                if workflow_builder is not None and iscoroutinefunction(workflow_builder):
                    await workflow_builder()

                # After workflow execution, ensure there's always a current context by pushing
                # the top-level flow if the context is empty. This fixes regressions where
                # with Workflow Schema version 0.6.0+ workflows expect context to be established.
                await self._ensure_workflow_context_established(workflow)

        except Exception as e:
            return WorkflowExecutionResult(
                execution_successful=False,
                execution_details=f"Failed to run workflow on path '{complete_file_path}'. Exception: {e}",
                status=WorkflowStatus.UNUSABLE,
                problems=tuple(frame.problems),
                workflow_id=workflow.workflow_id,
            )
        return WorkflowExecutionResult(
            execution_successful=True,
            execution_details=f"Succeeded in running workflow on path '{complete_file_path}'.",
            # A problem that did not stop the load leaves it recoverable: the graph is on the
            # canvas, with placeholders where the missing library's nodes belong.
            status=WorkflowStatus.FLAWED if frame.problems else WorkflowStatus.GOOD,
            problems=tuple(frame.problems),
            workflow_id=workflow.workflow_id,
        )

    async def _ensure_libraries_for_workflow(self, *, relative_file_path: str) -> list[WorkflowProblem]:
        """Register every library the workflow declares before exec, tolerating the ones that won't.

        Reads node_libraries_referenced from the workflow's TOML metadata header
        and dispatches a RegisterLibraryFromFileRequest for each entry via
        ahandle_request. Returns a LibraryNotRegisteredProblem per library that
        would not register; an empty list when every library resolved. That is the
        same problem type on_load_workflow_metadata_request records for the same
        condition, so the two paths describe it identically.

        A library that cannot be registered does not by itself stop the load. The
        nodes it owns come back from CreateNodeRequest as ErrorProxyNode placeholders
        that carry the reason and preserve the graph's values and connections, so the
        rest of the workflow stays open and editable and only execution is lost.
        Refusing the load instead would deny the artist the one view that shows
        which nodes need the library (issue #5505).

        The load can still fail downstream for a reason the missing library causes:
        a parameter value whose CLASS the library declares is emitted as a hard
        import inside build_workflow() (see _build_deferred_import_statements), and
        that raises before any node is created. Only the node types are recoverable.

        The engine (not the workflow file itself) owns library registration
        because worker-backed libraries spin up a dedicated subprocess when they
        register. If the workflow file emitted RegisterLibraryFromFileRequest
        during exec(), a worker library would need to start its own subprocess
        while the workflow was mid-execution -- bootstrapping a worker from
        inside code running on that worker is a circular dependency. Declaring
        libraries in the metadata header and resolving them here, before exec(),
        breaks the cycle.
        """
        load_metadata_result = await self.engine.workflow_manager.on_load_workflow_metadata_request(
            LoadWorkflowMetadata(file_name=relative_file_path)
        )
        if not isinstance(load_metadata_result, LoadWorkflowMetadataResultSuccess):
            # No usable metadata block (missing, malformed, or schema-invalid).
            # Fall through to exec without pre-registering libraries; the engine
            # startup path may have already loaded them. This mirrors prior
            # behavior where a missing prereq block was survivable.
            return []
        problems: list[WorkflowProblem] = []
        for lib_ref in load_metadata_result.metadata.node_libraries_referenced:
            register_result = await self.engine.ahandle_request(
                RegisterLibraryFromFileRequest(
                    library_name=lib_ref.library_name,
                    perform_discovery_if_not_found=True,
                    # The run result carries this library in a user-readable form already;
                    # suppressing this inner result keeps the GUI from showing a
                    # `RegisterLibraryFromFile Failed` toast on top of it.
                    failure_log_level=logging.DEBUG,
                )
            )
            if not register_result.succeeded():
                # The declared version is deliberately not reported. A library that never
                # registered has no version to compare against, which is why the not-registered
                # problem carries none -- the version-mismatch problems cover the case where a
                # library IS present at the wrong version. It also keeps the non-semver
                # placeholder a workflow stores when saved without its library (see
                # node_manager._serialize_node_to_commands) from ever reaching the reader.
                problems.append(
                    LibraryNotRegisteredProblem(
                        library_name=lib_ref.library_name,
                        reason=str(getattr(register_result, "result_details", "")) or None,
                    )
                )
        return problems
