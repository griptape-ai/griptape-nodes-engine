from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import semver

from griptape_nodes.exe_types.flow import ControlFlow
from griptape_nodes.node_library.workflow_registry import WorkflowAddressError
from griptape_nodes.retained_mode.engine import EngineScoped
from griptape_nodes.retained_mode.events.base_events import ResultDetails
from griptape_nodes.retained_mode.events.flow_events import (
    ImportWorkflowAsReferencedSubFlowRequest,
    SetFlowMetadataRequest,
    SetFlowMetadataResultSuccess,
)
from griptape_nodes.retained_mode.events.workflow_events import (
    ImportWorkflowAsReferencedSubFlowResultFailure,
    ImportWorkflowAsReferencedSubFlowResultSuccess,
)
from griptape_nodes.retained_mode.managers.workflow.loading import is_loading_workflow
from griptape_nodes.retained_mode.managers.workflow.running import WorkflowExecutionResult, execution_result_details
from griptape_nodes.retained_mode.request_handlers import RequestScope, handles

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

    @handles(ImportWorkflowAsReferencedSubFlowRequest, scope=RequestScope.WORKFLOW)
    async def on_import_workflow_as_referenced_sub_flow_request(
        self, request: ImportWorkflowAsReferencedSubFlowRequest, workflow: WorkflowRuntime
    ) -> ResultPayload:
        """Import the saved workflow the request names as a new referenced sub flow of the open `workflow`."""
        # Saved workflow files reference their subflows by name, permanently, so a name here is
        # not only an old client's: it is resolved like any other address.
        try:
            imported = self.engine.workflow_registry.resolve_address(
                workflow_id=request.workflow_id,
                workflow_name=request.workflow_name,
                operation="import",
            )
        except WorkflowAddressError as err:
            return ImportWorkflowAsReferencedSubFlowResultFailure(result_details=str(err))

        validation_error = self._validate_import_prerequisites(workflow, request, imported)
        if validation_error:
            return validation_error

        # Determine target flow name
        if request.flow_name is not None:
            flow_name = request.flow_name
        else:
            flow_name = workflow.context.get_current_flow().name

        # Execute the import
        return await self._execute_workflow_import(workflow, request, imported, flow_name)

    def _validate_import_prerequisites(
        self, workflow: WorkflowRuntime, request: ImportWorkflowAsReferencedSubFlowRequest, imported: Workflow
    ) -> ResultPayload | None:
        """Validate all prerequisites for importing `imported` into `workflow`. Returns error result or None if valid."""
        workflow_name = imported.name

        # Import-as-referenced runs the workflow file as a subflow. Unsaved workflows
        # have no file to execute; callers must save first.
        if imported.file_path is None:
            details = (
                f"Attempted to import unsaved workflow '{workflow_name}' as a referenced sub flow. "
                "Save the workflow before importing it as a sub-flow."
            )
            return ImportWorkflowAsReferencedSubFlowResultFailure(result_details=details)

        # Check workflow version - Schema version 0.6.0+ required for referenced workflow imports
        # (workflow schema was fixed in 0.6.0 to support importing workflows)
        required_version = semver.VersionInfo(major=0, minor=6, patch=0)
        try:
            workflow_version = semver.VersionInfo.parse(imported.metadata.schema_version)
        except Exception as e:
            details = f"Attempted to import workflow '{workflow_name}' as referenced sub flow. Failed because workflow version '{imported.metadata.schema_version}' caused an error: {e}"
            return ImportWorkflowAsReferencedSubFlowResultFailure(result_details=details)
        if workflow_version < required_version:
            details = f"Attempted to import workflow '{workflow_name}' as referenced sub flow. Failed because workflow version '{imported.metadata.schema_version}' is less than required version '0.6.0'. To remedy, open the workflow you are attempting to import and save it again to upgrade it to the latest version."
            return ImportWorkflowAsReferencedSubFlowResultFailure(result_details=details)

        # Check target flow
        flow_name = request.flow_name
        if flow_name is None:
            if not workflow.context.has_current_flow():
                details = f"Attempted to import workflow '{workflow_name}' into Current Context. Failed because Current Context was empty"
                return ImportWorkflowAsReferencedSubFlowResultFailure(result_details=details)
        else:
            # Validate that the specified flow exists
            flow_manager = self.engine.flow_manager
            try:
                flow_manager.get_flow_by_name_in(workflow, flow_name)
            except KeyError:
                details = f"Attempted to import workflow '{workflow_name}' into flow '{flow_name}'. Failed because target flow does not exist"
                return ImportWorkflowAsReferencedSubFlowResultFailure(result_details=details)

        return None

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
        workflow_name = imported.name
        workflow_file_path = imported.file_path
        if workflow_file_path is None:
            details = f"Attempted to import unsaved workflow '{workflow_name}' as a referenced sub flow."
            return ImportWorkflowAsReferencedSubFlowResultFailure(result_details=details)

        # Get current flows before importing
        flows_before = set(workflow.objects.get_filtered_subset(type=ControlFlow).keys())

        # Execute the workflow within the target flow context.
        # When track_as_referenced is True, wrap in ReferencedWorkflowContext so the flow
        # serializes as an import command. When False, the flow serializes as inline content.
        workflow_manager = self.engine.workflow_manager
        with workflow.context.flow(self.engine.flow_manager.get_flow_by_name_in(workflow, flow_name)):
            if request.track_as_referenced:
                with workflow_manager.referenced_workflow(workflow_name):
                    workflow_result = await workflow_manager.runner.run_workflow(workflow_file_path)
            else:
                workflow_result = await workflow_manager.runner.run_workflow(workflow_file_path)

        if not workflow_result.execution_successful:
            details = f"Attempted to import workflow '{workflow_name}' as referenced sub flow. Failed because workflow execution failed: {workflow_result.execution_details}"
            return ImportWorkflowAsReferencedSubFlowResultFailure(
                result_details=self._import_result_details(workflow_result, details, level=logging.ERROR)
            )

        # Get flows after importing to find the new referenced sub flow
        flows_after = set(workflow.objects.get_filtered_subset(type=ControlFlow).keys())
        new_flows = flows_after - flows_before

        if not new_flows:
            details = f"Attempted to import workflow '{workflow_name}' as referenced sub flow. Failed because no new flow was created"
            return ImportWorkflowAsReferencedSubFlowResultFailure(result_details=details)

        created_flow_name = self._select_top_level_imported_flow(workflow, new_flows, flow_name, workflow_name)

        # Apply imported flow metadata if provided
        if request.imported_flow_metadata:
            set_metadata_request = SetFlowMetadataRequest(
                flow_name=created_flow_name, metadata=request.imported_flow_metadata
            )
            set_metadata_result = workflow.handle_request(set_metadata_request)

            if not isinstance(set_metadata_result, SetFlowMetadataResultSuccess):
                details = f"Attempted to import workflow '{workflow_name}' as referenced sub flow. Failed because metadata could not be applied to created flow '{created_flow_name}'"
                return ImportWorkflowAsReferencedSubFlowResultFailure(result_details=details)

            logger.debug(
                "Applied imported flow metadata to '%s': %s", created_flow_name, request.imported_flow_metadata
            )

        details = f"Successfully imported workflow '{workflow_name}' as referenced sub flow '{created_flow_name}'"
        return ImportWorkflowAsReferencedSubFlowResultSuccess(
            created_flow_name=created_flow_name,
            status=workflow_result.status,
            result_details=self._import_result_details(workflow_result, details, level=logging.DEBUG),
        )

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
