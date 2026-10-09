from __future__ import annotations

import logging
import sys
from collections import defaultdict
from contextlib import nullcontext
from inspect import iscoroutinefunction
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

import anyio

from griptape_nodes.files.path_utils import (
    resolve_workspace_path,
)
from griptape_nodes.retained_mode.engine import EngineScoped
from griptape_nodes.retained_mode.events.base_events import ResultDetail, ResultDetails
from griptape_nodes.retained_mode.events.library_events import (
    RegisterLibraryFromFileRequest,
    RegisterLibraryFromFileResultFailure,
)
from griptape_nodes.retained_mode.events.object_events import ClearAllObjectStateRequest
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
from griptape_nodes.retained_mode.managers.event_manager import EventSuppressionContext
from griptape_nodes.retained_mode.managers.fitness_problems.workflows import (
    LibraryNotRegisteredProblem,
)
from griptape_nodes.retained_mode.managers.os_manager import OSManager
from griptape_nodes.retained_mode.managers.workflow.loading import LoadProblemFrame
from griptape_nodes.retained_mode.request_handlers import handles

if TYPE_CHECKING:
    from collections.abc import Iterable

    from griptape_nodes.node_library.workflow_registry import (
        WorkflowShape,
    )
    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.events.base_events import ResultPayload
    from griptape_nodes.retained_mode.managers.event_manager import EventManager
    from griptape_nodes.retained_mode.managers.fitness_problems.workflows.workflow_problem import WorkflowProblem


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

    async def run_workflow(self, relative_file_path: str) -> WorkflowExecutionResult:
        # Resolve path using utility function
        workspace_path = self.engine.config_manager.workspace_path
        complete_file_path = resolve_workspace_path(Path(relative_file_path), workspace_path)
        # Problems found anywhere under this load land in the frame, including those of a
        # referenced subflow imported from inside exec() -- see LoadProblemFrame.
        with LoadProblemFrame() as frame:
            return await self._run_workflow_in_frame(
                relative_file_path=relative_file_path, complete_file_path=complete_file_path, frame=frame
            )

    @handles(RunWorkflowFromScratchRequest)
    async def on_run_workflow_from_scratch_request(self, request: RunWorkflowFromScratchRequest) -> ResultPayload:
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

            # Start with a clean slate.
            clear_all_request = ClearAllObjectStateRequest(i_know_what_im_doing=True)
            clear_all_result = await self.engine.ahandle_request(clear_all_request)
            if not clear_all_result.succeeded():
                details = f"Failed to clear the existing object state when trying to run '{complete_file_path}'."
                return RunWorkflowFromScratchResultFailure(result_details=details)

            # Run the file, goddamn it
            execution_result = await self.run_workflow(relative_file_path=relative_file_path)
            if execution_result.execution_successful:
                return RunWorkflowFromScratchResultSuccess(
                    status=execution_result.status,
                    result_details=ResultDetails(*execution_result_details(execution_result, level=logging.DEBUG)),
                )

            return RunWorkflowFromScratchResultFailure(
                result_details=ResultDetails(*execution_result_details(execution_result, level=logging.ERROR))
            )

    @handles(RunWorkflowWithCurrentStateRequest)
    async def on_run_workflow_with_current_state_request(
        self, request: RunWorkflowWithCurrentStateRequest
    ) -> ResultPayload:
        relative_file_path = request.file_path
        if self.engine.context_manager.has_current_flow():
            # Disallow opening a workflow inside another workflow this way. It would
            # become an invisible child flow (no way to reach it in the UI), persisted
            # with the parent workflow on save, and executed invisibly whenever the
            # parent workflow was executed.
            open_flow_name = self.engine.context_manager.get_current_flow().name
            details = (
                f"Attempted to open workflow '{relative_file_path}' while the flow '{open_flow_name}' is still open. "
                "Close the current workflow first, before opening this one."
            )
            return RunWorkflowWithCurrentStateResultFailure(result_details=details)

        complete_file_path = self.engine.workflow_registry.get_complete_file_path(relative_file_path=relative_file_path)
        if not await anyio.Path(complete_file_path).is_file():
            details = f"Failed to find file. Path '{complete_file_path}' doesn't exist."
            return RunWorkflowWithCurrentStateResultFailure(result_details=details)
        execution_result = await self.run_workflow(relative_file_path=relative_file_path)

        if execution_result.execution_successful:
            return RunWorkflowWithCurrentStateResultSuccess(
                status=execution_result.status,
                result_details=ResultDetails(*execution_result_details(execution_result, level=logging.DEBUG)),
            )
        return RunWorkflowWithCurrentStateResultFailure(
            result_details=ResultDetails(*execution_result_details(execution_result, level=logging.ERROR))
        )

    @handles(RunWorkflowFromRegistryRequest)
    async def on_run_workflow_from_registry_request(self, request: RunWorkflowFromRegistryRequest) -> ResultPayload:
        await self.engine.workflow_manager.wait_for_workflows_loaded()

        # get workflow from registry
        try:
            workflow = self.engine.workflow_registry.get_workflow_by_name(request.workflow_name)
        except KeyError:
            details = f"Failed to get workflow '{request.workflow_name}' from registry."
            return RunWorkflowFromRegistryResultFailure(result_details=details)

        # RunWorkflowFromRegistry is file-based (it re-executes the serialized .py on disk).
        # Unsaved workflows have no file; callers should use StartFlowRequest against the
        # live flow for in-memory execution. Keep this reject explicit so the UI can
        # distinguish "run this unsaved edit" (StartFlow) from "run the last saved version".
        relative_file_path = workflow.file_path
        if relative_file_path is None:
            details = (
                f"Cannot run unsaved workflow '{request.workflow_name}' from the registry because it has no file on disk. "
                "Save the workflow first, or use StartFlowRequest to execute the current in-memory flow."
            )
            return RunWorkflowFromRegistryResultFailure(result_details=details)

        # Update current context for workflow. The editor always keeps a workflow in the
        # Current Context (a blank canvas is itself an "unsaved:" workflow), so replacing an
        # existing context is the steady state, not an anomaly. When the caller passes
        # run_with_clean_slate=True, replacement is the explicitly requested behavior, so
        # only warn when clobbering the context was NOT asked for.
        context_warning = None
        if not request.run_with_clean_slate and self.engine.context_manager.has_current_workflow():
            context_warning = f"Started a new workflow '{request.workflow_name}' but a workflow '{self.engine.context_manager.get_current_workflow_name()}' was already in the Current Context. Replacing the old with the new."

        # Squelch any ResultPayloads that indicate the workflow was changed, because we are loading it.
        with self.engine.workflow_manager.squelch_workflow_altered():
            if request.run_with_clean_slate:
                # Start with a clean slate.
                clear_all_request = ClearAllObjectStateRequest(i_know_what_im_doing=True)
                clear_all_result = await self.engine.ahandle_request(clear_all_request)
                if not clear_all_result.succeeded():
                    details = f"Failed to clear the existing object state when preparing to run workflow '{request.workflow_name}'."
                    return RunWorkflowFromRegistryResultFailure(result_details=details)

            # Let's run under the assumption that this Workflow will become our Current Context; if we fail, it will revert.
            self.engine.context_manager.push_workflow(request.workflow_name)
            # run file
            execution_result = await self.run_workflow(relative_file_path=relative_file_path)

            if not execution_result.execution_successful:
                result_messages = []
                if context_warning:
                    result_messages.append(ResultDetail(message=context_warning, level=logging.WARNING))
                result_messages.extend(execution_result_details(execution_result, level=logging.ERROR))

                # Attempt to clear everything out, as we modified the engine state getting here.
                clear_all_request = ClearAllObjectStateRequest(i_know_what_im_doing=True)
                clear_all_result = await self.engine.ahandle_request(clear_all_request)

                # The clear-all above here wipes the ContextManager, so no need to do a pop_workflow().
                return RunWorkflowFromRegistryResultFailure(result_details=ResultDetails(*result_messages))

        # Success!
        result_messages = []
        if context_warning:
            result_messages.append(ResultDetail(message=context_warning, level=logging.WARNING))
        result_messages.extend(execution_result_details(execution_result, level=logging.DEBUG))
        return RunWorkflowFromRegistryResultSuccess(
            status=execution_result.status, result_details=ResultDetails(*result_messages)
        )

    @handles(GetWorkflowRunCommandRequest)
    async def on_get_workflow_run_command_request(self, request: GetWorkflowRunCommandRequest) -> ResultPayload:  # noqa: C901, PLR0911, PLR0912
        workflow_name = request.workflow_name
        file_path = request.file_path

        # Failure: no identifier and no current context
        if workflow_name is None and file_path is None:
            context_manager = self.engine.context_manager
            if not context_manager.has_current_workflow():
                return GetWorkflowRunCommandResultFailure(
                    result_details=(
                        "Attempted to get workflow run command. Failed with workflow_name=None, file_path=None "
                        "because no workflow is loaded in the current context. Provide workflow_name or file_path, or load a workflow."
                    )
                )
            # When neither workflow_name nor file_path is provided, use the workflow in the current context as a fallback.
            workflow_name = context_manager.get_current_workflow_name()

        # Failure: both workflow_name and file_path provided
        if workflow_name is not None and file_path is not None:
            return GetWorkflowRunCommandResultFailure(
                result_details=(
                    "Attempted to get workflow run command. Failed with both workflow_name and file_path provided "
                    "because only one may be provided. Provide workflow_name or file_path, not both."
                )
            )

        # Resolve relative_file_path and workflow_shape (or fail)
        workflow_shape: WorkflowShape | None = None
        if workflow_name is not None:
            try:
                workflow = self.engine.workflow_registry.get_workflow_by_name(workflow_name)
            except KeyError:
                return GetWorkflowRunCommandResultFailure(
                    result_details=(
                        f"Attempted to get workflow run command. Failed with workflow_name='{workflow_name}' "
                        "because the workflow was not found in the registry. Save the workflow first, or provide file_path."
                    )
                )
            relative_file_path = workflow.file_path
            workflow_shape = workflow.metadata.workflow_shape
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

    async def _ensure_workflow_context_established(self) -> None:
        """Ensure there's a current workflow and flow context after workflow execution."""
        context_manager = self.engine.context_manager

        # First check: Do we have a current workflow? If not, that's a critical failure.
        if not context_manager.has_current_workflow():
            error_message = "Workflow execution completed but no current workflow is established in context"
            raise RuntimeError(error_message)

        # Second check: Do we have a current flow? If not, try to establish one.
        if not context_manager.has_current_flow():
            # Use the proper request to get the top-level flow
            from griptape_nodes.retained_mode.events.flow_events import (
                GetTopLevelFlowRequest,
                GetTopLevelFlowResultSuccess,
            )

            top_level_flow_request = GetTopLevelFlowRequest()
            top_level_flow_result = await self.engine.ahandle_request(top_level_flow_request)

            if (
                isinstance(top_level_flow_result, GetTopLevelFlowResultSuccess)
                and top_level_flow_result.flow_name is not None
            ):
                # Push the flow to the context stack permanently using FlowManager
                flow_manager = self.engine.flow_manager
                flow_obj = flow_manager.get_flow_by_name(top_level_flow_result.flow_name)
                context_manager.push_flow(flow_obj)
                details = f"Workflow execution completed. Set '{top_level_flow_result.flow_name}' as current context."
                logger.debug(details)

            # If we still don't have a flow, that's a critical error
            if not context_manager.has_current_flow():
                error_message = "Workflow execution completed but no current flow context could be established"
                raise RuntimeError(error_message)

    async def _run_workflow_in_frame(
        self, *, relative_file_path: str, complete_file_path: Path, frame: LoadProblemFrame
    ) -> WorkflowExecutionResult:
        """Read, resolve libraries for, and exec one workflow file, recording problems in `frame`."""
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
            with duplicate_library_failures:
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
                await self._ensure_workflow_context_established()

        except Exception as e:
            return WorkflowExecutionResult(
                execution_successful=False,
                execution_details=f"Failed to run workflow on path '{complete_file_path}'. Exception: {e}",
                status=WorkflowStatus.UNUSABLE,
                problems=tuple(frame.problems),
            )
        return WorkflowExecutionResult(
            execution_successful=True,
            execution_details=f"Succeeded in running workflow on path '{complete_file_path}'.",
            # A problem that did not stop the load leaves it recoverable: the graph is on the
            # canvas, with placeholders where the missing library's nodes belong.
            status=WorkflowStatus.FLAWED if frame.problems else WorkflowStatus.GOOD,
            problems=tuple(frame.problems),
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
