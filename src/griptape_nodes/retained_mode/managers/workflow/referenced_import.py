"""Handles importing a registered workflow into a flow as a referenced subflow."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import semver

from griptape_nodes.exe_types.flow import ControlFlow
from griptape_nodes.retained_mode.engine import EngineScoped
from griptape_nodes.retained_mode.events.base_events import ResultDetails
from griptape_nodes.retained_mode.events.flow_events import (
    SetFlowMetadataRequest,
    SetFlowMetadataResultSuccess,
)
from griptape_nodes.retained_mode.events.workflow_events import (
    ImportWorkflowAsReferencedSubFlowRequest,
    ImportWorkflowAsReferencedSubFlowResultFailure,
    ImportWorkflowAsReferencedSubFlowResultSuccess,
)
from griptape_nodes.retained_mode.managers.workflow.loading import is_loading_workflow
from griptape_nodes.retained_mode.request_handlers import handles

if TYPE_CHECKING:
    from griptape_nodes.node_library.workflow_registry import Workflow
    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.events.base_events import ResultPayload
    from griptape_nodes.retained_mode.managers.event_manager import EventManager
    from griptape_nodes.retained_mode.managers.workflow.running import WorkflowExecutionResult


logger = logging.getLogger("griptape_nodes")


class ReferencedWorkflowImport(EngineScoped):
    def __init__(self, event_manager: EventManager, *, engine: Engine | None = None) -> None:
        super().__init__(engine)
        event_manager.register_request_handlers(self)

    @handles(ImportWorkflowAsReferencedSubFlowRequest)
    async def on_import_workflow_as_referenced_sub_flow_request(
        self, request: ImportWorkflowAsReferencedSubFlowRequest
    ) -> ResultPayload:
        """Import a registered workflow as a new referenced sub flow in the current context."""
        # Validate prerequisites
        validation_error = self._validate_import_prerequisites(request)
        if validation_error:
            return validation_error

        # Get the workflow (validation passed, so we know it exists)
        workflow = self._get_workflow_by_name(request.workflow_name)

        # Determine target flow name
        if request.flow_name is not None:
            flow_name = request.flow_name
        else:
            flow_name = self.engine.context_manager.get_current_flow().name

        # Execute the import
        return await self._execute_workflow_import(request, workflow, flow_name)

    def _validate_import_prerequisites(self, request: ImportWorkflowAsReferencedSubFlowRequest) -> ResultPayload | None:  # noqa: PLR0911
        """Validate all prerequisites for import. Returns error result or None if valid."""
        # Check workflow exists and get it
        try:
            workflow = self._get_workflow_by_name(request.workflow_name)
        except KeyError:
            details = f"Attempted to import workflow '{request.workflow_name}' as referenced sub flow. Failed because workflow is not registered"
            return ImportWorkflowAsReferencedSubFlowResultFailure(result_details=details)

        # Import-as-referenced runs the workflow file as a subflow. Unsaved workflows
        # have no file to execute; callers must save first.
        if workflow.file_path is None:
            details = (
                f"Attempted to import unsaved workflow '{request.workflow_name}' as a referenced sub flow. "
                "Save the workflow before importing it as a sub-flow."
            )
            return ImportWorkflowAsReferencedSubFlowResultFailure(result_details=details)

        # Check workflow version - Schema version 0.6.0+ required for referenced workflow imports
        # (workflow schema was fixed in 0.6.0 to support importing workflows)
        required_version = semver.VersionInfo(major=0, minor=6, patch=0)
        try:
            workflow_version = semver.VersionInfo.parse(workflow.metadata.schema_version)
        except Exception as e:
            details = f"Attempted to import workflow '{request.workflow_name}' as referenced sub flow. Failed because workflow version '{workflow.metadata.schema_version}' caused an error: {e}"
            return ImportWorkflowAsReferencedSubFlowResultFailure(result_details=details)
        if workflow_version < required_version:
            details = f"Attempted to import workflow '{request.workflow_name}' as referenced sub flow. Failed because workflow version '{workflow.metadata.schema_version}' is less than required version '0.6.0'. To remedy, open the workflow you are attempting to import and save it again to upgrade it to the latest version."
            return ImportWorkflowAsReferencedSubFlowResultFailure(result_details=details)

        # Check target flow
        flow_name = request.flow_name
        if flow_name is None:
            if not self.engine.context_manager.has_current_flow():
                details = f"Attempted to import workflow '{request.workflow_name}' into Current Context. Failed because Current Context was empty"
                return ImportWorkflowAsReferencedSubFlowResultFailure(result_details=details)
        else:
            # Validate that the specified flow exists
            flow_manager = self.engine.flow_manager
            try:
                flow_manager.get_flow_by_name(flow_name)
            except KeyError:
                details = f"Attempted to import workflow '{request.workflow_name}' into flow '{flow_name}'. Failed because target flow does not exist"
                return ImportWorkflowAsReferencedSubFlowResultFailure(result_details=details)

        return None

    def _get_workflow_by_name(self, workflow_name: str) -> Workflow:
        """Get workflow by name from the registry."""
        return self.engine.workflow_registry.get_workflow_by_name(workflow_name)

    async def _execute_workflow_import(
        self, request: ImportWorkflowAsReferencedSubFlowRequest, workflow: Workflow, flow_name: str
    ) -> ResultPayload:
        """Execute the actual workflow import.

        Precondition: `workflow.file_path is not None` (enforced by `_validate_import_prerequisites`).
        """
        workflow_file_path = workflow.file_path
        if workflow_file_path is None:
            details = f"Attempted to import unsaved workflow '{request.workflow_name}' as a referenced sub flow."
            return ImportWorkflowAsReferencedSubFlowResultFailure(result_details=details)

        # Get current flows before importing
        obj_manager = self.engine.object_manager
        flows_before = set(obj_manager.get_filtered_subset(type=ControlFlow).keys())

        # Execute the workflow within the target flow context.
        # When track_as_referenced is True, wrap in ReferencedWorkflowContext so the flow
        # serializes as an import command. When False, the flow serializes as inline content.
        workflow_manager = self.engine.workflow_manager
        with self.engine.context_manager.flow(flow_name):
            if request.track_as_referenced:
                with workflow_manager.ReferencedWorkflowContext(workflow_manager, request.workflow_name):
                    workflow_result = await workflow_manager.runner.run_workflow(workflow_file_path)
            else:
                workflow_result = await workflow_manager.runner.run_workflow(workflow_file_path)

        if not workflow_result.execution_successful:
            details = f"Attempted to import workflow '{request.workflow_name}' as referenced sub flow. Failed because workflow execution failed: {workflow_result.execution_details}"
            return ImportWorkflowAsReferencedSubFlowResultFailure(
                result_details=self._import_result_details(workflow_result, details, level=logging.ERROR)
            )

        # Get flows after importing to find the new referenced sub flow
        flows_after = set(obj_manager.get_filtered_subset(type=ControlFlow).keys())
        new_flows = flows_after - flows_before

        if not new_flows:
            details = f"Attempted to import workflow '{request.workflow_name}' as referenced sub flow. Failed because no new flow was created"
            return ImportWorkflowAsReferencedSubFlowResultFailure(result_details=details)

        created_flow_name = self._select_top_level_imported_flow(
            new_flows, flow_name, request.workflow_name, self.engine
        )

        # Apply imported flow metadata if provided
        if request.imported_flow_metadata:
            set_metadata_request = SetFlowMetadataRequest(
                flow_name=created_flow_name, metadata=request.imported_flow_metadata
            )
            set_metadata_result = self.engine.handle_request(set_metadata_request)

            if not isinstance(set_metadata_result, SetFlowMetadataResultSuccess):
                details = f"Attempted to import workflow '{request.workflow_name}' as referenced sub flow. Failed because metadata could not be applied to created flow '{created_flow_name}'"
                return ImportWorkflowAsReferencedSubFlowResultFailure(result_details=details)

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
        return ResultDetails(
            *self.engine.workflow_manager.runner.execution_result_details(workflow_result, level=level, message=message)
        )

    @staticmethod
    def _select_top_level_imported_flow(
        new_flows: set[str], parent_flow_name: str, workflow_name: str, engine: Engine
    ) -> str:
        """Select the top-level flow among those created by importing a referenced workflow.

        A workflow that contains node groups (ForEach, etc.) imports as more than one flow: its
        top-level flow plus each group's body flow. The caller wants the top-level flow (the one
        holding the Start/End nodes). It is the new flow whose parent is the import target.

        Args:
            new_flows: Names of the flows created during the import.
            parent_flow_name: The flow the workflow was imported into (the import target).
            workflow_name: Name of the imported workflow, for diagnostics.
            engine: The engine whose FlowManager resolves flow parentage.

        Returns:
            The name of the top-level imported flow.
        """
        flow_manager = engine.flow_manager
        top_level_flows = [flow for flow in new_flows if flow_manager.get_parent_flow(flow) == parent_flow_name]

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
