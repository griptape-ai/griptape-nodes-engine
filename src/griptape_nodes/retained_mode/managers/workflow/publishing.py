from __future__ import annotations

import asyncio
import dataclasses
import logging
from pathlib import Path
from typing import TYPE_CHECKING, cast

from griptape_nodes.files.path_utils import derive_registry_key
from griptape_nodes.node_library.workflow_registry import WorkflowAddressError
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
    RegisterWorkflowRequest,
    RegisterWorkflowResultFailure,
    RegisterWorkflowResultSuccess,
    SaveWorkflowRequest,
)
from griptape_nodes.retained_mode.request_handlers import RequestScope, handles

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.events.base_events import ResultPayload
    from griptape_nodes.retained_mode.managers.event_manager import EventManager


logger = logging.getLogger("griptape_nodes")


class WorkflowPublishing(EngineScoped):
    def __init__(self, event_manager: EventManager, *, engine: Engine | None = None) -> None:
        super().__init__(engine)
        event_manager.register_request_handlers(self)

    @handles(GetPublishOptionsRequest, scope=RequestScope.HOST)
    def on_get_publish_options_request(self, request: GetPublishOptionsRequest) -> ResultPayload:
        try:
            workflow = self.engine.workflow_registry.resolve_address(
                workflow_id=request.workflow_id,
                workflow_name=request.workflow_name,
                operation="get publish options for",
            )
        except WorkflowAddressError as err:
            return GetPublishOptionsResultFailure(exception=err, result_details=str(err))
        # Publishers read the open graph, so they can only publish the open workflow.
        if not self.engine.workflow_registry.is_open(workflow.workflow_id):
            details = (
                f"Attempted to get publish options for workflow '{workflow.name}'. "
                "Failed because it is not open. Open it, then publish it."
            )
            return GetPublishOptionsResultFailure(exception=ValueError(details), result_details=details)

        event_handler_mappings = self.engine.library_manager.get_registered_event_handlers(
            request_type=PublishWorkflowRequest
        )
        publishing_handler = event_handler_mappings.get(request.publisher_name)
        if publishing_handler is None:
            details = f"No publishing handler found for '{request.publisher_name}'."
            return GetPublishOptionsResultFailure(exception=ValueError(details), result_details=details)

        event_data = publishing_handler.event_data
        if isinstance(event_data, PublishWorkflowRegisteredEventData) and event_data.get_publish_options is not None:
            # Publishers read the workflow's name, so they get a copy with it filled in.
            return event_data.get_publish_options(
                dataclasses.replace(request, workflow_name=workflow.name, workflow_id=workflow.workflow_id)
            )

        return GetPublishOptionsResultSuccess(
            fields=[],
            result_details="No custom publish options for this publisher.",
        )

    @handles(PublishWorkflowRequest, scope=RequestScope.HOST)
    async def on_publish_workflow_request(self, request: PublishWorkflowRequest) -> ResultPayload:
        try:
            workflow = self.engine.workflow_registry.resolve_address(
                workflow_id=request.workflow_id, workflow_name=request.workflow_name, operation="publish"
            )
        except WorkflowAddressError as err:
            return PublishWorkflowResultFailure(exception=err, result_details=str(err))
        # Publishers read the open graph (its nodes, shape and StartFlow settings), so publishing
        # any other workflow would package a mix of the two.
        open_workflow = self.engine.open_workflows.find_by_id(workflow.workflow_id)
        if open_workflow is None:
            details = f"Attempted to publish workflow '{workflow.name}'. Failed because it is not open. Open it, then publish it."
            return PublishWorkflowResultFailure(exception=ValueError(details), result_details=details)
        # Unsaved (registry-only) workflows cannot be published because publish emits a file
        # reference to the engine-registered workflow; the user must choose a save name first.
        if workflow.file_path is None:
            details = f"Cannot publish unsaved workflow '{workflow.name}'. Save the workflow before publishing."
            return PublishWorkflowResultFailure(exception=ValueError(details), result_details=details)

        try:
            publisher_name = request.publisher_name
            event_handler_mappings = self.engine.library_manager.get_registered_event_handlers(
                request_type=type(request)
            )
            publishing_handler = event_handler_mappings.get(publisher_name)

            if publishing_handler is None:
                msg = f"No publishing handler found for '{publisher_name}' in request type '{type(request).__name__}'."
                raise ValueError(msg)  # noqa: TRY301

            # Save before publishing, so the latest changes in memory are included. A failed save
            # would leave the publisher packaging the stale file.
            save_result = await open_workflow.ahandle_request(
                SaveWorkflowRequest(file_name=derive_registry_key(workflow.file_path))
            )
            if save_result.failed():
                details = (
                    f"Attempted to publish workflow '{workflow.name}'. Failed because it could not be saved "
                    f"first: {save_result.result_details}"
                )
                return PublishWorkflowResultFailure(result_details=details)

            # Publishers read the workflow's name, so they get a copy with it filled in.
            publisher_request = dataclasses.replace(
                request, workflow_name=workflow.name, workflow_id=workflow.workflow_id
            )
            result = await asyncio.to_thread(publishing_handler.handler, publisher_request)
            if isinstance(result, PublishWorkflowResultSuccess) and not result.skip_published_workflow_registration:
                workflow_file = Path(result.published_workflow_file_path)
                result = await self._register_published_workflow_file(workflow_file, result)

            return result  # noqa: TRY300
        except Exception as e:
            details = f"Failed to publish workflow '{workflow.name}': {e!s}"
            logger.exception(details)
            return PublishWorkflowResultFailure(exception=e, result_details=details)

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
                previously_registered = self.engine.workflow_registry.find_by_name(
                    derive_registry_key(workflow_file.name)
                )
                if previously_registered is not None:
                    # This workflow was registered previously, but now it's been updated (potentially including the metadata), so let's re-register
                    self.engine.workflow_registry.remove(previously_registered.workflow_id)

                register_workflow_result = self.engine.workflow_manager.on_register_workflow_request(
                    RegisterWorkflowRequest(
                        metadata=load_metadata_result.metadata,
                        file_name=workflow_file.name,
                    )
                )
                if isinstance(register_workflow_result, RegisterWorkflowResultSuccess):
                    success_message = f"Successfully registered new workflow with file '{workflow_file.name}'."
                    result_messages.append(ResultDetail(message=success_message, level=logging.INFO))
                    final_result.result_details = ResultDetails(*result_messages)
                else:
                    exception = cast("RegisterWorkflowResultFailure", register_workflow_result).exception
                    failure_message = f"Failed to register workflow with file '{workflow_file.name}': {exception}"
                    result_messages.append(ResultDetail(message=failure_message, level=logging.ERROR))
                    final_result = PublishWorkflowResultFailure(
                        result_details=ResultDetails(*result_messages), exception=exception
                    )
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
