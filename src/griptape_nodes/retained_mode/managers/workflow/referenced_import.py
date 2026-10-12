from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import semver

from griptape_nodes.exe_types.flow import ControlFlow
from griptape_nodes.exe_types.node_types import NODE_GROUP_FLOW
from griptape_nodes.exe_types.workflow_node_errors import WorkflowRecursionLimitError
from griptape_nodes.retained_mode.engine import EngineScoped
from griptape_nodes.retained_mode.events.base_events import ResultDetails
from griptape_nodes.retained_mode.events.flow_events import (
    DeleteFlowRequest,
    DeleteFlowResultSuccess,
    ImportWorkflowAsReferencedSubFlowRequest,
    SetFlowMetadataRequest,
    SetFlowMetadataResultSuccess,
)
from griptape_nodes.retained_mode.events.workflow_events import (
    ImportWorkflowAsReferencedSubFlowResultFailure,
    ImportWorkflowAsReferencedSubFlowResultSuccess,
)
from griptape_nodes.retained_mode.managers.settings import MAX_WORKFLOW_RECURSION_DEPTH_KEY
from griptape_nodes.retained_mode.managers.workflow.loading import is_loading_workflow
from griptape_nodes.retained_mode.managers.workflow.running import WorkflowExecutionResult, execution_result_details
from griptape_nodes.retained_mode.request_handlers import handles

if TYPE_CHECKING:
    from griptape_nodes.node_library.workflow_registry import Workflow
    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.events.base_events import ResultPayload
    from griptape_nodes.retained_mode.managers.event_manager import EventManager
    from griptape_nodes.retained_mode.runtime.workflow_runtime import WorkflowRuntime


logger = logging.getLogger("griptape_nodes")


class ReferencedWorkflowImport(EngineScoped):
    def __init__(self, event_manager: EventManager, *, engine: Engine | None = None) -> None:
        super().__init__(engine)
        event_manager.register_request_handlers(self)

    @handles(ImportWorkflowAsReferencedSubFlowRequest)
    async def on_import_workflow_as_referenced_sub_flow_request(
        self, request: ImportWorkflowAsReferencedSubFlowRequest, workflow: WorkflowRuntime
    ) -> ResultPayload:
        """Import the saved workflow the request names as a new referenced sub flow of the open `workflow`."""
        # Validate prerequisites
        validation_error = self._validate_import_prerequisites(workflow, request)
        if validation_error:
            return validation_error

        # Get the workflow (validation passed, so we know it exists)
        imported = self._get_workflow_by_name(request.workflow_name)

        # Determine target flow name
        if request.flow_name is not None:
            flow_name = request.flow_name
        else:
            flow_name = workflow.context.get_current_flow().name

        # Execute the import
        return await self._execute_workflow_import(workflow, request, imported, flow_name)

    def _validate_import_prerequisites(  # noqa: PLR0911
        self, workflow: WorkflowRuntime, request: ImportWorkflowAsReferencedSubFlowRequest
    ) -> ResultPayload | None:
        """Validate all prerequisites for importing the named workflow into `workflow`. Returns error result or None if valid."""
        # Check workflow exists and get it
        try:
            imported = self._get_workflow_by_name(request.workflow_name)
        except KeyError:
            details = f"Attempted to import workflow '{request.workflow_name}' as referenced sub flow. Failed because workflow is not registered"
            return ImportWorkflowAsReferencedSubFlowResultFailure(result_details=details)

        # Import-as-referenced runs the workflow file as a subflow. Unsaved workflows
        # have no file to execute; callers must save first.
        if imported.file_path is None:
            details = (
                f"Attempted to import unsaved workflow '{request.workflow_name}' as a referenced sub flow. "
                "Save the workflow before importing it as a sub-flow."
            )
            return ImportWorkflowAsReferencedSubFlowResultFailure(result_details=details)

        # Check workflow version - Schema version 0.6.0+ required for referenced workflow imports
        # (workflow schema was fixed in 0.6.0 to support importing workflows)
        required_version = semver.VersionInfo(major=0, minor=6, patch=0)
        try:
            workflow_version = semver.VersionInfo.parse(imported.metadata.schema_version)
        except Exception as e:
            details = f"Attempted to import workflow '{request.workflow_name}' as referenced sub flow. Failed because workflow version '{imported.metadata.schema_version}' caused an error: {e}"
            return ImportWorkflowAsReferencedSubFlowResultFailure(result_details=details)
        if workflow_version < required_version:
            details = f"Attempted to import workflow '{request.workflow_name}' as referenced sub flow. Failed because workflow version '{imported.metadata.schema_version}' is less than required version '0.6.0'. To remedy, open the workflow you are attempting to import and save it again to upgrade it to the latest version."
            return ImportWorkflowAsReferencedSubFlowResultFailure(result_details=details)

        # Check target flow
        flow_name = request.flow_name
        if flow_name is None:
            if not workflow.context.has_current_flow():
                details = f"Attempted to import workflow '{request.workflow_name}' into Current Context. Failed because Current Context was empty"
                return ImportWorkflowAsReferencedSubFlowResultFailure(result_details=details)
        else:
            # Validate that the specified flow exists
            flow_manager = self.engine.flow_manager
            try:
                flow_manager.get_flow_by_name_in(workflow, flow_name)
            except KeyError:
                details = f"Attempted to import workflow '{request.workflow_name}' into flow '{flow_name}'. Failed because target flow does not exist"
                return ImportWorkflowAsReferencedSubFlowResultFailure(result_details=details)

        # Refuse when this workflow already appears inside itself as deep as the
        # recursion limit allows. That is how a Workflow node pointing back at its own
        # workflow, directly or through other workflows, shows up at run time; without a
        # limit it would re-run the file forever.
        if flow_name is None:
            flow_name = workflow.context.get_current_flow().name
        return self._validate_recursion_depth(workflow, flow_name, request.workflow_name)

    def _validate_recursion_depth(
        self, workflow: WorkflowRuntime, flow_name: str, workflow_name: str
    ) -> ResultPayload | None:
        """Return a failure when importing `workflow_name` into `flow_name` would nest it too deeply."""
        depth = self._count_enclosing_imports_of(workflow, flow_name, workflow_name)
        limit = self.engine.config_manager.max_workflow_recursion_depth
        if depth > limit:
            if limit == 1:
                level_word = "level"
            else:
                level_word = "levels"
            details = (
                f"Attempted to import workflow '{workflow_name}' into flow '{flow_name}'. "
                f"Failed because '{workflow_name}' contains itself more than {limit} {level_word} deep, "
                f"the limit set by the `{MAX_WORKFLOW_RECURSION_DEPTH_KEY}` setting. A Workflow node inside it "
                "keeps pointing back to it, directly or through another workflow. Make sure the recursion "
                "stops, or raise the setting."
            )
            return ImportWorkflowAsReferencedSubFlowResultFailure(
                result_details=details, exception=WorkflowRecursionLimitError(details)
            )

        return None

    def _count_enclosing_imports_of(self, workflow: WorkflowRuntime, flow_name: str, workflow_name: str) -> int:
        """Count the copies of `workflow_name` that `flow_name` is already inside.

        That is the flows from `flow_name` up through the flows enclosing it that were
        created by importing the workflow, plus one when it is also the workflow that
        was opened at the top level. Group subflows are skipped: the Workflow node they
        hold is what nests, not the group.
        """
        flow_manager = self.engine.flow_manager
        count = 0
        seen: set[str] = set()
        current: str | None = flow_name
        while current is not None and current not in seen:
            seen.add(current)
            flow = workflow.objects.get_as_type(current, ControlFlow)
            if (
                flow is not None
                and flow.metadata.get("flow_type") != NODE_GROUP_FLOW
                and flow_manager.get_referenced_workflow_name(workflow, flow) == workflow_name
            ):
                count += 1
            current = flow_manager.get_enclosing_flow(workflow, current)
        if self._is_opened_workflow(workflow, workflow_name):
            count += 1
        return count

    def _is_opened_workflow(self, workflow: WorkflowRuntime, workflow_name: str) -> bool:
        """Whether `workflow_name` is the workflow opened at the top level, which is `workflow`.

        The opened workflow's own top flow is never tagged as an import, so it is
        counted separately.
        """
        return self.engine.workflow_registry.get(workflow.workflow_id).name == workflow_name

    def _get_workflow_by_name(self, workflow_name: str) -> Workflow:
        """Get workflow by name from the registry."""
        return self.engine.workflow_registry.get_workflow_by_name(workflow_name)

    async def _execute_workflow_import(
        self,
        workflow: WorkflowRuntime,
        request: ImportWorkflowAsReferencedSubFlowRequest,
        imported: Workflow,
        flow_name: str,
    ) -> ResultPayload:
        """Run the file of `imported` into `workflow`'s flow `flow_name`.

        Precondition: `imported.file_path is not None` (enforced by `_validate_import_prerequisites`).
        """
        workflow_file_path = imported.file_path
        if workflow_file_path is None:
            details = f"Attempted to import unsaved workflow '{request.workflow_name}' as a referenced sub flow."
            return ImportWorkflowAsReferencedSubFlowResultFailure(result_details=details)

        # Execute the workflow within the target flow context, on this task's own copy
        # of the flow stack so concurrent imports cannot push onto each other's. Only
        # the flows this import creates are recorded, so flows created by other tasks
        # are never mistaken for ours. A nested import records into its own list; its
        # flows sit inside ours, so deleting ours removes them.
        flow_manager = self.engine.flow_manager
        with flow_manager.task_local_flow_creation_record() as created_flows:
            try:
                workflow_result = await self._run_workflow_file_on_task_local_flow_stack(
                    workflow, request, workflow_file_path, flow_name
                )
            except BaseException:
                # BaseException on purpose: a cancelled import (CancelledError) must
                # also remove its half-built flows before the cancellation carries on.
                self._delete_created_flows(workflow, created_flows)
                raise

        if not workflow_result.execution_successful:
            details = f"Attempted to import workflow '{request.workflow_name}' as referenced sub flow. Failed because workflow execution failed: {workflow_result.execution_details}"
            return self._failed_import(workflow, created_flows, details, workflow_result=workflow_result)

        if not created_flows:
            details = f"Attempted to import workflow '{request.workflow_name}' as referenced sub flow. Failed because no new flow was created"
            return self._failed_import(workflow, created_flows, details)

        # A flow the import created and then deleted makes the selection fail.
        try:
            created_flow_name = self._select_top_level_imported_flow(
                workflow, set(created_flows), flow_name, request.workflow_name
            )
        except ValueError as err:
            details = f"Attempted to import workflow '{request.workflow_name}' as referenced sub flow. Failed because a flow it created was removed before the import finished: {err}"
            return self._failed_import(workflow, created_flows, details)

        # Apply imported flow metadata if provided
        if request.imported_flow_metadata:
            set_metadata_request = SetFlowMetadataRequest(
                flow_name=created_flow_name, metadata=request.imported_flow_metadata
            )
            set_metadata_result = workflow.handle_request(set_metadata_request)

            if not isinstance(set_metadata_result, SetFlowMetadataResultSuccess):
                details = f"Attempted to import workflow '{request.workflow_name}' as referenced sub flow. Failed because metadata could not be applied to created flow '{created_flow_name}'"
                return self._failed_import(workflow, created_flows, details)

            logger.debug(
                "Applied imported flow metadata to '%s': %s", created_flow_name, request.imported_flow_metadata
            )

        details = (
            f"Successfully imported workflow '{request.workflow_name}' as referenced sub flow '{created_flow_name}'"
        )
        return ImportWorkflowAsReferencedSubFlowResultSuccess(
            created_flow_name=created_flow_name,
            status=workflow_result.status,
            result_details=self._import_result_details(workflow_result, details, level=logging.DEBUG),
        )

    async def _run_workflow_file_on_task_local_flow_stack(
        self,
        workflow: WorkflowRuntime,
        request: ImportWorkflowAsReferencedSubFlowRequest,
        workflow_file_path: str,
        flow_name: str,
    ) -> WorkflowExecutionResult:
        """Run the workflow file on its own task-local flow stack, inside `flow_name`.

        When track_as_referenced is True, wrap in `referenced_workflow` so the flow
        serializes as an import command. When False, the flow serializes as inline
        content.
        """
        workflow_manager = self.engine.workflow_manager
        target_flow = self.engine.flow_manager.get_flow_by_name_in(workflow, flow_name)
        with (
            workflow.context.task_local_flow_stack(),
            # Entered after the task-local stack so its push lands on the task-local stack.
            workflow.context.flow(target_flow),
        ):
            if request.track_as_referenced:
                with workflow_manager.referenced_workflow(request.workflow_name):
                    return await workflow_manager.runner.run_workflow(workflow_file_path, opens_workflow=False)
            return await workflow_manager.runner.run_workflow(workflow_file_path, opens_workflow=False)

    def _failed_import(
        self,
        workflow: WorkflowRuntime,
        new_flows: list[str],
        message: str,
        *,
        workflow_result: WorkflowExecutionResult | None = None,
    ) -> ImportWorkflowAsReferencedSubFlowResultFailure:
        """Remove the flows the import created, then build its failure result."""
        leftover_flows = self._delete_created_flows(workflow, new_flows)
        if leftover_flows:
            names = ", ".join(f"'{name}'" for name in leftover_flows)
            if len(leftover_flows) == 1:
                message += f" A flow the import had already built could not be removed: {names}. Delete it by hand."
            else:
                message += f" Flows the import had already built could not be removed: {names}. Delete them by hand."
        if workflow_result is None:
            return ImportWorkflowAsReferencedSubFlowResultFailure(result_details=message)
        return ImportWorkflowAsReferencedSubFlowResultFailure(
            result_details=self._import_result_details(workflow_result, message, level=logging.ERROR)
        )

    def _delete_created_flows(self, workflow: WorkflowRuntime, new_flows: list[str]) -> list[str]:
        """Delete the top-most flows among `new_flows` that still exist; return the ones that would not go.

        Deleting a flow deletes the flows and nodes inside it, so flows whose parent is also in the
        record are left to their parent.
        """
        flow_manager = self.engine.flow_manager
        created = set(new_flows)
        roots = [
            name
            for name in dict.fromkeys(new_flows)
            if workflow.objects.get_as_type(name, ControlFlow) is not None
            and flow_manager.get_parent_flow_in(workflow, name) not in created
        ]

        leftover_flows: list[str] = []
        for root in roots:
            delete_result = workflow.handle_request(DeleteFlowRequest(flow_name=root))
            if not isinstance(delete_result, DeleteFlowResultSuccess):
                logger.warning(
                    "Could not remove flow '%s' left behind by a failed workflow import: %s",
                    root,
                    delete_result.result_details,
                )
                leftover_flows.append(root)
        return leftover_flows

    def _import_result_details(
        self, workflow_result: WorkflowExecutionResult, message: str, *, level: int
    ) -> ResultDetails:
        """Render an import result, naming the subflow's problems only if nobody else will.

        Nested inside a load, the subflow's problems have already bubbled into the enclosing
        frame and will be reported on that load's result; naming them here too would warn twice
        for one condition. Imported on its own -- the editor dropping a workflow into a flow --
        there is no such load, so this is the only result that can name them.
        """
        if is_loading_workflow():
            return ResultDetails(message=message, level=level)
        return ResultDetails(*execution_result_details(workflow_result, level=level, message=message))

    def _select_top_level_imported_flow(
        self, workflow: WorkflowRuntime, new_flows: set[str], parent_flow_name: str, workflow_name: str
    ) -> str:
        """Select the top-level flow among those created by importing a referenced workflow.

        A workflow that contains node groups (ForEach, etc.) imports as more than one flow: its
        top-level flow plus each group's body flow. The caller wants the top-level flow (the one
        holding the Start/End nodes). It is the new flow whose parent is the import target.

        Args:
            workflow: The open workflow the flows were created in.
            new_flows: Names of the flows created during the import.
            parent_flow_name: The flow the workflow was imported into (the import target).
            workflow_name: Name of the imported workflow, for diagnostics.

        Returns:
            The name of the top-level imported flow.
        """
        flow_manager = self.engine.flow_manager
        top_level_flows = [
            flow for flow in new_flows if flow_manager.get_parent_flow_in(workflow, flow) == parent_flow_name
        ]

        if len(top_level_flows) == 1:
            return top_level_flows[0]

        # Not exactly one flow parented to the target -- unexpected for a well-formed single-workflow
        # import. Fall back to a deterministic (sorted) choice rather than hash-ordered set iteration,
        # and log for diagnosis.
        candidates = top_level_flows or list(new_flows)
        selected = min(candidates)
        logger.warning(
            "Import of '%s' created %d flow(s) with %d parented to target '%s'; expected exactly one "
            "top-level flow. Using '%s'. All new flows: %s",
            workflow_name,
            len(new_flows),
            len(top_level_flows),
            parent_flow_name,
            selected,
            sorted(new_flows),
        )
        return selected
