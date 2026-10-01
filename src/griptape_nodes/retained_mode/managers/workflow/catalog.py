from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

import anyio

from griptape_nodes.files.project_file import ProjectFileDestination
from griptape_nodes.node_library.workflow_registry import (
    Workflow,
    WorkflowMetadata,
)
from griptape_nodes.retained_mode.engine import EngineScoped
from griptape_nodes.retained_mode.events.base_events import ResultDetails
from griptape_nodes.retained_mode.events.os_events import (
    ExistingFilePolicy,
)
from griptape_nodes.retained_mode.events.workflow_events import (
    GetWorkflowInfoRequest,
    GetWorkflowInfoResultFailure,
    GetWorkflowInfoResultSuccess,
    GetWorkflowMetadataRequest,
    GetWorkflowMetadataResultFailure,
    GetWorkflowMetadataResultSuccess,
    ListAllWorkflowInfoRequest,
    ListAllWorkflowInfoResultFailure,
    ListAllWorkflowInfoResultSuccess,
    ListAllWorkflowsRequest,
    ListAllWorkflowsResultFailure,
    ListAllWorkflowsResultSuccess,
    ListCallableWorkflowsRequest,
    ListCallableWorkflowsResultFailure,
    ListCallableWorkflowsResultSuccess,
    SetWorkflowMetadataRequest,
    SetWorkflowMetadataResultFailure,
    SetWorkflowMetadataResultSuccess,
    WorkflowInfoSummary,
    WorkflowStatus,
)
from griptape_nodes.retained_mode.managers.workflow.running import collate_problems_by_type
from griptape_nodes.retained_mode.request_handlers import handles

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.events.base_events import ResultPayload
    from griptape_nodes.retained_mode.managers.event_manager import EventManager
    from griptape_nodes.retained_mode.managers.workflow_manager import WorkflowManager


logger = logging.getLogger("griptape_nodes")


class WorkflowPathResolution(NamedTuple):
    """Resolution result for workflow and its corresponding file path."""

    workflow: Workflow | None
    file_path: Path | None
    error: str | None


class WorkflowCatalog(EngineScoped):
    def __init__(self, event_manager: EventManager, *, engine: Engine | None = None) -> None:
        super().__init__(engine)
        event_manager.register_request_handlers(self)

    @handles(ListAllWorkflowsRequest)
    async def on_list_all_workflows_request(self, _request: ListAllWorkflowsRequest) -> ResultPayload:
        await self.engine.workflow_manager.wait_for_workflows_loaded()

        try:
            workflows = self.engine.workflow_registry.list_workflows()
        except Exception:
            details = "Failed to list all workflows."
            return ListAllWorkflowsResultFailure(result_details=details)
        return ListAllWorkflowsResultSuccess(
            workflows=workflows, result_details=f"Successfully retrieved {len(workflows)} workflows."
        )

    @handles(ListCallableWorkflowsRequest)
    async def on_list_callable_workflows_request(self, _request: ListCallableWorkflowsRequest) -> ResultPayload:
        await self.engine.workflow_manager.wait_for_workflows_loaded()

        try:
            workflow_names = [
                key
                for key, wf in self.engine.workflow_registry.list_workflows().items()
                if wf.get("workflow_shape") is not None
            ]
        except Exception:
            details = "Failed to list callable workflows."
            return ListCallableWorkflowsResultFailure(result_details=details)
        return ListCallableWorkflowsResultSuccess(
            workflow_names=workflow_names,
            result_details=f"Successfully retrieved {len(workflow_names)} callable workflows.",
        )

    @handles(GetWorkflowInfoRequest)
    def on_get_workflow_info_request(self, request: GetWorkflowInfoRequest) -> ResultPayload:
        try:
            workflow = self.engine.workflow_registry.get_workflow_by_name(request.workflow_name)
        except KeyError:
            details = f"Attempted to get workflow info. Failed because workflow '{request.workflow_name}' was not found in the registry."
            return GetWorkflowInfoResultFailure(result_details=details)

        # Unsaved workflows never went through the on-disk metadata load, so
        # the manager has no load record for them. Report a healthy stub.
        if workflow.file_path is None:
            return GetWorkflowInfoResultSuccess(
                status=WorkflowStatus.GOOD,
                workflow_name=workflow.metadata.name,
                workflow_path="",
                problems=[],
                workflow_dependencies=[],
                result_details=f"Workflow '{request.workflow_name}' is unsaved; returning empty info stub.",
            )

        workflow_file_path = self._build_workflow_info_key(workflow.file_path)

        wf_info = self.engine.workflow_manager.find_workflow_info_for_attempted_load(workflow_file_path)
        if wf_info is None:
            details = (
                f"Attempted to get workflow info. Failed because no info was found for path '{workflow_file_path}'."
            )
            return GetWorkflowInfoResultFailure(result_details=details)

        payload = self._build_workflow_info_payload(wf_info)
        return GetWorkflowInfoResultSuccess(
            status=payload.status,
            workflow_name=payload.workflow_name,
            workflow_path=payload.workflow_path,
            problems=payload.problems,
            workflow_dependencies=payload.workflow_dependencies,
            result_details=f"Successfully retrieved workflow info for '{workflow_file_path}'.",
        )

    @handles(ListAllWorkflowInfoRequest)
    def on_list_all_workflow_info_request(self, _request: ListAllWorkflowInfoRequest) -> ResultPayload:
        try:
            registry_keys = self.engine.workflow_registry.list_workflows()
        except Exception as e:
            details = f"Attempted to list all workflow info. Failed to list workflows: {e}"
            return ListAllWorkflowInfoResultFailure(result_details=details)

        workflow_infos: dict[str, WorkflowInfoSummary] = {}
        for registry_key in registry_keys:
            try:
                workflow = self.engine.workflow_registry.get_workflow_by_name(registry_key)
            except KeyError:
                continue
            # Unsaved workflows are registry-only (no on-disk metadata to summarize).
            if workflow.file_path is None:
                continue
            workflow_file_path = self._build_workflow_info_key(workflow.file_path)
            wf_info = self.engine.workflow_manager.find_workflow_info_for_attempted_load(workflow_file_path)
            if wf_info is None:
                continue
            workflow_infos[registry_key] = self._build_workflow_info_payload(wf_info)

        return ListAllWorkflowInfoResultSuccess(
            workflow_infos=workflow_infos,
            result_details=f"Successfully retrieved workflow info for {len(workflow_infos)} workflows.",
        )

    @handles(GetWorkflowMetadataRequest)
    def on_get_workflow_metadata_request(self, request: GetWorkflowMetadataRequest) -> ResultPayload:
        try:
            workflow = self.engine.workflow_registry.get_workflow_by_name(request.workflow_name)
        except KeyError:
            details = f"Failed to get metadata. Workflow '{request.workflow_name}' not found."
            return GetWorkflowMetadataResultFailure(result_details=details)

        return GetWorkflowMetadataResultSuccess(
            workflow_metadata=workflow.metadata,
            result_details="Successfully retrieved workflow metadata.",
        )

    @handles(SetWorkflowMetadataRequest)
    async def on_set_workflow_metadata_request(self, request: SetWorkflowMetadataRequest) -> ResultPayload:
        await self.engine.workflow_manager.wait_for_workflows_loaded()

        # Unsaved workflows have no file on disk; update the in-memory registry entry only.
        # This keeps display-name / description edits in sync with the registry so a refresh
        # re-hydrates the latest state without needing a save.
        if self.engine.workflow_registry.has_workflow_with_name(request.workflow_name):
            workflow = self.engine.workflow_registry.get_workflow_by_name(request.workflow_name)
            if workflow.file_path is None:
                try:
                    merged = self._merge_metadata(workflow.metadata, request.workflow_metadata)
                except ValueError as e:
                    return SetWorkflowMetadataResultFailure(result_details=str(e))
                merged.last_modified_date = datetime.now(tz=UTC)
                workflow.metadata = merged
                return SetWorkflowMetadataResultSuccess(
                    result_details=ResultDetails(
                        message=(
                            f"Successfully updated in-memory metadata for unsaved workflow '{request.workflow_name}'."
                        ),
                        level=logging.INFO,
                    )
                )

        # Resolve workflow and file path for saved workflows
        resolution = self._get_workflow_and_path(request.workflow_name)
        if resolution.error is not None or resolution.workflow is None or resolution.file_path is None:
            return SetWorkflowMetadataResultFailure(result_details=resolution.error or "Failed to resolve workflow.")

        try:
            new_metadata = self._merge_metadata(resolution.workflow.metadata, request.workflow_metadata)
        except ValueError as e:
            return SetWorkflowMetadataResultFailure(result_details=str(e))
        # Refresh last_modified_date to reflect this change
        new_metadata.last_modified_date = datetime.now(tz=UTC)

        # Persist header
        write_error = await self._write_metadata_header(file_path=resolution.file_path, workflow_metadata=new_metadata)
        if write_error is not None:
            return SetWorkflowMetadataResultFailure(result_details=write_error)

        # Update registry
        resolution.workflow.metadata = new_metadata

        return SetWorkflowMetadataResultSuccess(
            result_details=ResultDetails(
                message=f"Successfully updated metadata for workflow '{request.workflow_name}'.", level=logging.INFO
            )
        )

    def _build_workflow_info_key(self, file_path: str) -> str:
        """Build the key used to look up a workflow's load record.

        Matches the key construction in on_load_workflow_metadata_request, which uses
        workspace_path.joinpath() without resolving symlinks.
        """
        return str(self.engine.config_manager.workspace_path.joinpath(file_path))

    def _build_workflow_info_payload(self, wf_info: WorkflowManager.WorkflowInfo) -> WorkflowInfoSummary:
        """Build a WorkflowInfoSummary from a WorkflowInfo, collating problems for display."""
        collated_problems = collate_problems_by_type(wf_info.problems)
        return WorkflowInfoSummary(
            status=wf_info.status,
            workflow_name=wf_info.workflow_name,
            workflow_path=str(wf_info.workflow_path),
            problems=collated_problems,
            workflow_dependencies=wf_info.workflow_dependencies,
        )

    def _get_workflow_and_path(self, workflow_name: str) -> WorkflowPathResolution:
        """Resolve workflow from registry and return absolute file path.

        Returns an error resolution for unsaved workflows since there is no file on disk
        to read or update.
        """
        try:
            workflow = self.engine.workflow_registry.get_workflow_by_name(workflow_name)
        except KeyError:
            return WorkflowPathResolution(
                workflow=None, file_path=None, error=f"Failed to set metadata. Workflow '{workflow_name}' not found."
            )

        if workflow.file_path is None:
            return WorkflowPathResolution(
                workflow=workflow,
                file_path=None,
                error=f"Failed to set metadata. Workflow '{workflow_name}' is unsaved (no file on disk).",
            )

        complete_file_path = self.engine.workflow_registry.get_complete_file_path(workflow.file_path)
        file_path_obj = Path(complete_file_path)
        if not file_path_obj.is_file():
            return WorkflowPathResolution(
                workflow=workflow,
                file_path=None,
                error=f"Failed to set metadata. File path '{complete_file_path}' does not exist.",
            )

        return WorkflowPathResolution(workflow=workflow, file_path=file_path_obj, error=None)

    async def _write_metadata_header(self, file_path: Path, workflow_metadata: WorkflowMetadata) -> str | None:
        """Replace the workflow header and persist changes to disk."""
        try:
            existing_content = await anyio.Path(file_path).read_text(encoding="utf-8")
        except OSError as e:
            return f"Failed to read workflow file '{file_path}': {e!s}"

        updated_content = self.engine.workflow_manager.codegen.replace_workflow_metadata_header(
            existing_content, workflow_metadata
        )
        if updated_content is None:
            return "Failed to update metadata header."

        # Metadata-header rewrite: we already have the absolute on-disk path of an
        # existing workflow file. write_workflow_file's single-destination contract
        # (so macro-driven saves can thread their unresolved MacroPath through to
        # OSManager) means we wrap the literal path here. File's constructor stores
        # non-macro strings verbatim, so the write goes through OSManager's
        # sanitize-and-write branch with no macro resolution. OVERWRITE matches the
        # in-place semantics this caller needs.
        destination = ProjectFileDestination(
            str(file_path),
            existing_file_policy=ExistingFilePolicy.OVERWRITE,
        )
        write_result = self.engine.workflow_manager.saver.write_workflow_file(
            destination=destination, content=updated_content, file_name=workflow_metadata.name
        )
        if not write_result.success:
            return write_result.error_details
        return None

    def _merge_metadata(
        self, existing: WorkflowMetadata, incoming: WorkflowMetadata | dict[str, Any]
    ) -> WorkflowMetadata:
        """Coerce incoming metadata (dict or WorkflowMetadata) into a merged WorkflowMetadata.

        Dicts from the frontend may omit required fields; merge on top of existing
        metadata so required fields are preserved. Raises ValueError on invalid input.
        """
        if not isinstance(incoming, dict):
            return incoming
        existing_metadata_dict = existing.model_dump()
        # Only overlay non-None values from the incoming dict to preserve required fields.
        # Allow explicit None for these optional fields.
        optional_none_allowed = ("description", "image", "branched_from", "workflow_shape")
        for key, value in incoming.items():
            if value is not None or key in optional_none_allowed:
                existing_metadata_dict[key] = value
        try:
            return WorkflowMetadata.model_validate(existing_metadata_dict)
        except Exception as e:
            msg = f"Invalid workflow_metadata: {e!s}"
            raise ValueError(msg) from e
