from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from griptape_nodes.files.path_utils import derive_registry_key
from griptape_nodes.node_library.workflow_registry import (
    WorkflowAddressError,
    WorkflowMetadataError,
    read_workflow_header,
)
from griptape_nodes.retained_mode.engine import EngineScoped
from griptape_nodes.retained_mode.events.base_events import ResultDetail, ResultDetails
from griptape_nodes.retained_mode.events.workflow_events import (
    GetPublishOptionsRequest,
    GetPublishOptionsResultFailure,
    GetPublishOptionsResultSuccess,
    LoadWorkflowMetadata,
    LoadWorkflowMetadataResultSuccess,
    PublishWorkflowRegisteredEventData,
    PublishWorkflowRequest,
    PublishWorkflowResultFailure,
    PublishWorkflowResultSuccess,
    SaveWorkflowRequest,
)
from griptape_nodes.retained_mode.request_handlers import handles

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.events.base_events import ResultPayload
    from griptape_nodes.retained_mode.managers.event_manager import EventManager
    from griptape_nodes.retained_mode.runtime.workflow_runtime import WorkflowRuntime


logger = logging.getLogger("griptape_nodes")


class WorkflowPublishing(EngineScoped):
    def __init__(self, event_manager: EventManager, *, engine: Engine | None = None) -> None:
        super().__init__(engine)
        event_manager.register_request_handlers(self)

    @handles(GetPublishOptionsRequest)
    def on_get_publish_options_request(
        self,
        request: GetPublishOptionsRequest,
        workflow: WorkflowRuntime | None = None,  # noqa: ARG002 - declares the scope dispatch binds for the callback
    ) -> ResultPayload:
        """Ask the publisher for its options, bound to `workflow`, whose graph its callback reads."""
        try:
            named = self.engine.workflow_registry.resolve_address(request.workflow_id, action="get publish options for")
        except WorkflowAddressError as err:
            return GetPublishOptionsResultFailure(result_details=str(err))
        # Publishers read the name.
        options_request = replace(request, workflow_name=named.name)

        event_handler_mappings = self.engine.library_manager.get_registered_event_handlers(
            request_type=PublishWorkflowRequest
        )
        publishing_handler = event_handler_mappings.get(request.publisher_name)
        if publishing_handler is None:
            details = f"No publishing handler found for '{request.publisher_name}'."
            return GetPublishOptionsResultFailure(exception=ValueError(details), result_details=details)

        event_data = publishing_handler.event_data
        if isinstance(event_data, PublishWorkflowRegisteredEventData) and event_data.get_publish_options is not None:
            return event_data.get_publish_options(options_request)

        return GetPublishOptionsResultSuccess(
            fields=[],
            result_details="No custom publish options for this publisher.",
        )

    @handles(PublishWorkflowRequest)
    async def on_publish_workflow_request(
        self, request: PublishWorkflowRequest, workflow: WorkflowRuntime
    ) -> ResultPayload:
        """Save and publish the open `workflow`, handing the publisher its registry name in `workflow_name`.

        The publisher reads the graph through the bound workflow, which dispatch binds to `workflow`
        and `asyncio.to_thread` carries into the publisher's thread.
        """
        refusal = self._refuse_other_workflow(request.workflow_id, workflow, action="publish")
        if refusal is not None:
            return PublishWorkflowResultFailure(result_details=refusal)
        saved = self.engine.workflow_registry.get(workflow.workflow_id)
        workflow_name = saved.name
        if saved.file_path is None:
            details = f"Attempted to publish workflow '{workflow_name}'. Failed because it is unsaved. Save the workflow before publishing."
            return PublishWorkflowResultFailure(result_details=details)
        publish_request = replace(request, workflow_name=workflow_name)

        try:
            publisher_name = request.publisher_name
            event_handler_mappings = self.engine.library_manager.get_registered_event_handlers(
                request_type=type(request)
            )
            publishing_handler = event_handler_mappings.get(publisher_name)

            if publishing_handler is None:
                msg = f"No publishing handler found for '{publisher_name}' in request type '{type(request).__name__}'."
                raise ValueError(msg)  # noqa: TRY301

            # Save the workflow before publishing to ensure the latest changes in memory are included.
            await workflow.ahandle_request(SaveWorkflowRequest(file_name=derive_registry_key(saved.file_path)))

            result = await asyncio.to_thread(publishing_handler.handler, publish_request)
            if isinstance(result, PublishWorkflowResultSuccess) and not result.skip_published_workflow_registration:
                workflow_file = Path(result.published_workflow_file_path)
                result = await self._register_published_workflow_file(workflow_file, result)

            return result  # noqa: TRY300
        except Exception as e:
            details = f"Failed to publish workflow '{workflow_name}': {e!s}"
            logger.exception(details)
            return PublishWorkflowResultFailure(exception=e, result_details=details)

    def _refuse_other_workflow(self, workflow_id: str | None, workflow: WorkflowRuntime, *, action: str) -> str | None:
        """Why a request that gives `workflow_id` can't act on `workflow`, or None.

        The publisher packages `workflow`'s graph, so a request can only name that workflow.
        """
        if workflow_id is None or workflow_id == workflow.workflow_id:
            return None
        try:
            named = self.engine.workflow_registry.resolve_address(workflow_id, action=action)
        except WorkflowAddressError as err:
            return str(err)
        return (
            f"Attempted to {action} workflow '{named.name}'. Failed because it isn't the workflow open in "
            "this tab. Do it from its own tab."
        )

    async def _register_published_workflow_file(
        self, workflow_file: Path, result: PublishWorkflowResultSuccess
    ) -> ResultPayload:
        """Register a published workflow file in the workflow registry."""
        result_messages: list[ResultDetail] = []

        final_result: ResultPayload = result
        if isinstance(result.result_details, ResultDetails):
            result_messages.extend(result.result_details.result_details)
        else:
            result_messages.append(ResultDetail(message=result.result_details, level=logging.INFO))

        if workflow_file.exists() and workflow_file.is_file():  # noqa: ASYNC240
            load_workflow_metadata_request = LoadWorkflowMetadata(
                file_name=workflow_file.name,
            )
            load_metadata_result = await self.engine.workflow_manager.on_load_workflow_metadata_request(
                load_workflow_metadata_request
            )
            if isinstance(load_metadata_result, LoadWorkflowMetadataResultSuccess):
                # The scan's path: a workflow published before keeps its entry, and with it its id,
                # and takes the metadata just read.
                try:
                    self.engine.workflow_manager.refresh_workflow_file(
                        workflow_file.name,
                        load_metadata_result.metadata,
                        header_id=self._header_id_of(workflow_file),
                    )
                except ValueError as err:
                    failure_message = f"Failed to register workflow with file '{workflow_file.name}': {err}"
                    result_messages.append(ResultDetail(message=failure_message, level=logging.ERROR))
                    final_result = PublishWorkflowResultFailure(
                        result_details=ResultDetails(*result_messages), exception=err
                    )
                else:
                    success_message = f"Successfully registered new workflow with file '{workflow_file.name}'."
                    result_messages.append(ResultDetail(message=success_message, level=logging.INFO))
                    final_result.result_details = ResultDetails(*result_messages)
            else:
                metadata_failure_message = (
                    f"Failed to load metadata for workflow file '{workflow_file.name}'. Not registering workflow."
                )
                result_messages = [ResultDetail(message=metadata_failure_message, level=logging.ERROR)]
                final_result = PublishWorkflowResultFailure(result_details=ResultDetails(*result_messages))

        else:
            result_messages.append(
                ResultDetail(message=f"Workflow file '{workflow_file.name}' does not exist.", level=logging.ERROR)
            )
            final_result = PublishWorkflowResultFailure(result_details=ResultDetails(*result_messages))

        return final_result

    @staticmethod
    def _header_id_of(workflow_file: Path) -> str | None:
        """The id a published file's header carries, which a publisher may have copied from the workflow it published."""
        try:
            return read_workflow_header(workflow_file).workflow_id
        except WorkflowMetadataError:
            return None
