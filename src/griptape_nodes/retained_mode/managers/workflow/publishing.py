from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

from griptape_nodes.exe_types.core_types import ParameterMode
from griptape_nodes.exe_types.node_types import ConfigurationNode, StartNode
from griptape_nodes.files.path_utils import derive_registry_key
from griptape_nodes.node_library.library_registry import LibraryRegistry, LibraryRegistryError
from griptape_nodes.node_library.workflow_registry import WorkflowAddressError
from griptape_nodes.retained_mode.engine import EngineScoped
from griptape_nodes.retained_mode.events.base_events import ResultDetail, ResultDetails
from griptape_nodes.retained_mode.events.node_events import CreateNodeRequest, CreateNodeResultSuccess
from griptape_nodes.retained_mode.events.parameter_events import (
    SetParameterValueRequest,
    SetParameterValueResultSuccess,
)
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
    SaveWorkflowResultSuccess,
)
from griptape_nodes.retained_mode.request_handlers import handles

if TYPE_CHECKING:
    from griptape_nodes.exe_types.node_types import BaseNode
    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.events.base_events import ResultPayload
    from griptape_nodes.retained_mode.managers.event_manager import EventManager
    from griptape_nodes.retained_mode.runtime.workflow_runtime import WorkflowRuntime


logger = logging.getLogger("griptape_nodes")

# Where a newly created Configuration node goes, relative to the workflow's Start node.
CONFIGURATION_NODE_OFFSET_Y = 300

_ConfigurableRequest = TypeVar("_ConfigurableRequest", GetPublishOptionsRequest, PublishWorkflowRequest)


class WorkflowPublishing(EngineScoped):
    def __init__(self, event_manager: EventManager, *, engine: Engine | None = None) -> None:
        super().__init__(engine)
        event_manager.register_request_handlers(self)

    @handles(GetPublishOptionsRequest)
    def on_get_publish_options_request(
        self,
        request: GetPublishOptionsRequest,
        workflow: WorkflowRuntime | None = None,
    ) -> ResultPayload:
        """Ask the publisher for its options, bound to `workflow`, whose graph its callback reads.

        `workflow` is None when the request names no workflow and more than one is open; the
        publisher still gets options, just with no Configuration node to prefill from.
        """
        options_request = request
        if request.workflow_id is not None:
            try:
                named = self.engine.workflow_registry.resolve_address(
                    workflow_id=request.workflow_id,
                    workflow_name=request.workflow_name,
                    action="get publish options for",
                )
            except WorkflowAddressError as err:
                return GetPublishOptionsResultFailure(result_details=str(err))
            # Publishers read the name.
            options_request = replace(request, workflow_name=named.name)
        if options_request.workflow_name is None:
            details = "Attempted to get publish options for a workflow. Failed because the request names no workflow. Pass workflow_id."
            return GetPublishOptionsResultFailure(result_details=details)

        event_handler_mappings = self.engine.library_manager.get_registered_event_handlers(
            request_type=PublishWorkflowRequest
        )
        publishing_handler = event_handler_mappings.get(request.publisher_name)
        if publishing_handler is None:
            details = f"No publishing handler found for '{request.publisher_name}'."
            return GetPublishOptionsResultFailure(exception=ValueError(details), result_details=details)

        event_data = publishing_handler.event_data
        if isinstance(event_data, PublishWorkflowRegisteredEventData) and event_data.get_publish_options is not None:
            return event_data.get_publish_options(self._with_configuration(options_request, workflow, event_data))

        return GetPublishOptionsResultSuccess(
            fields=[],
            result_details="No custom publish options for this publisher.",
        )

    @handles(PublishWorkflowRequest)
    async def on_publish_workflow_request(
        self, request: PublishWorkflowRequest, workflow: WorkflowRuntime
    ) -> ResultPayload:
        """Save and publish the open `workflow`, which the request names by its id or registry name.

        The publisher reads the graph through the bound workflow, which dispatch binds to `workflow`
        and `asyncio.to_thread` carries into the publisher's thread.
        """
        publish_request = self._publish_request_by_name(request, workflow)
        if isinstance(publish_request, PublishWorkflowResultFailure):
            return publish_request
        workflow_name = publish_request.workflow_name
        if workflow_name is None:
            details = "Attempted to publish a workflow. Failed because the request names no workflow. Pass workflow_id."
            return PublishWorkflowResultFailure(result_details=details)

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
            # Unsaved (registry-only) workflows cannot be published because publish emits a file
            # reference to the engine-registered workflow; the user must choose a save name first.
            workflow_file_name = workflow_name
            saved = self.engine.workflow_registry.find_by_name(workflow_name)
            if saved is None:
                details = (
                    f"While publishing, workflow '{workflow_name}' had not been saved or could not be found in the Workflow Registry. "
                    "Saving as a new and registered workflow before proceeding on publish attempt."
                )
                logger.info(details)
            else:
                # The publisher packages `workflow`'s graph, so another open workflow's name would
                # publish one workflow's graph under the other's file.
                if saved.workflow_id != workflow.workflow_id and self.engine.open_workflows.is_open(saved.workflow_id):
                    msg = (
                        f"Cannot publish workflow '{workflow_name}' from another workflow's tab. "
                        "Publish it from its own tab."
                    )
                    raise ValueError(msg)  # noqa: TRY301
                if saved.file_path is None:
                    msg = f"Cannot publish unsaved workflow '{workflow_name}'. Save the workflow before publishing."
                    raise ValueError(msg)  # noqa: TRY301
                workflow_file_name = derive_registry_key(saved.file_path)
            await workflow.ahandle_request(SaveWorkflowRequest(file_name=workflow_file_name))

            event_data = publishing_handler.event_data
            publish_request = self._with_configuration(publish_request, workflow, event_data)

            result = await asyncio.to_thread(publishing_handler.handler, publish_request)
            if isinstance(result, PublishWorkflowResultSuccess):
                await self._store_configuration(workflow, event_data, result, workflow_file_name)
            if isinstance(result, PublishWorkflowResultSuccess) and not result.skip_published_workflow_registration:
                workflow_file = Path(result.published_workflow_file_path)
                result = await self._register_published_workflow_file(workflow_file, result)

            return result  # noqa: TRY300
        except Exception as e:
            details = f"Failed to publish workflow '{workflow_name}': {e!s}"
            logger.exception(details)
            return PublishWorkflowResultFailure(exception=e, result_details=details)

    def _with_configuration(
        self, request: _ConfigurableRequest, workflow: WorkflowRuntime | None, event_data: object
    ) -> _ConfigurableRequest:
        """`request` carrying the values of the target's Configuration node in `workflow`, if it has one.

        `workflow` is None when the request is unbound to any open workflow (see
        `on_get_publish_options_request`); there is then no graph to look a node up in.
        """
        if workflow is None or not isinstance(event_data, PublishWorkflowRegisteredEventData):
            return request
        configuration_node = self._find_configuration_node(workflow, event_data)
        if configuration_node is None:
            return request
        return replace(request, configuration=_read_configuration(configuration_node))

    async def _store_configuration(
        self,
        workflow: WorkflowRuntime,
        event_data: object,
        result: PublishWorkflowResultSuccess,
        workflow_file_name: str,
    ) -> None:
        """Find or create the target's Configuration node, write the values the publisher returned, and save.

        The publish has already happened, so a failure here doesn't fail it: it is added to the
        result's details as a warning, and the next publish starts without the remembered values.
        """
        if not isinstance(event_data, PublishWorkflowRegisteredEventData):
            return
        node_type = event_data.configuration_node_type
        if node_type is None:
            return

        configuration_node = self._find_configuration_node(workflow, event_data)
        if configuration_node is None:
            configuration_node_name = await self._create_configuration_node(workflow, event_data)
            if configuration_node_name is None:
                _add_warning(
                    result,
                    f"Published, but couldn't add a '{node_type}' node to remember this target's settings. "
                    "The next publish won't start from them.",
                )
                return
        else:
            configuration_node_name = configuration_node.name

        for parameter_name, value in (result.configuration or {}).items():
            set_result = await workflow.ahandle_request(
                SetParameterValueRequest(node_name=configuration_node_name, parameter_name=parameter_name, value=value)
            )
            if not isinstance(set_result, SetParameterValueResultSuccess):
                _add_warning(
                    result,
                    f"Published, but couldn't remember '{parameter_name}' on '{configuration_node_name}': "
                    f"{set_result.result_details}",
                )

        save_result = await workflow.ahandle_request(SaveWorkflowRequest(file_name=workflow_file_name))
        if not isinstance(save_result, SaveWorkflowResultSuccess):
            _add_warning(
                result,
                f"Published, but couldn't save the workflow after updating '{configuration_node_name}'. "
                "Save the workflow to keep this target's settings.",
            )

    async def _create_configuration_node(
        self, workflow: WorkflowRuntime, event_data: PublishWorkflowRegisteredEventData
    ) -> str | None:
        """Add the target's Configuration node to the workflow's top-level flow and return its name, or None."""
        node_type = event_data.configuration_node_type
        if node_type is None:
            return None
        flow_name = self._top_level_flow_name(workflow)
        if flow_name is None:
            return None

        create_result = await workflow.ahandle_request(
            CreateNodeRequest(
                node_type=node_type,
                specific_library_name=event_data.configuration_node_library_name,
                node_name=_configuration_node_display_name(event_data),
                override_parent_flow_name=flow_name,
                metadata=self._configuration_node_position(workflow, flow_name),
                create_error_proxy_on_failure=False,
            )
        )
        if not isinstance(create_result, CreateNodeResultSuccess):
            logger.warning(
                "Attempted to add a '%s' node after publishing. Failed because: %s",
                node_type,
                create_result.result_details,
            )
            return None
        return create_result.node_name

    def _find_configuration_node(
        self, workflow: WorkflowRuntime, event_data: PublishWorkflowRegisteredEventData
    ) -> ConfigurationNode | None:
        """The target's Configuration node in `workflow`'s top-level flow, or None if it has none yet."""
        node_type = event_data.configuration_node_type
        if node_type is None:
            return None
        flow_name = self._top_level_flow_name(workflow)
        if flow_name is None:
            return None

        nodes = self.engine.flow_manager.get_flow_by_name_in(workflow, flow_name).nodes
        matches = [
            node
            for node in nodes.values()
            if isinstance(node, ConfigurationNode)
            and node.metadata.get("node_type") == node_type
            and node.metadata.get("library") == event_data.configuration_node_library_name
        ]
        if not matches:
            return None
        if len(matches) > 1:
            logger.warning(
                "Workflow has %d '%s' nodes; using '%s'. A workflow should hold one per publish target.",
                len(matches),
                node_type,
                matches[0].name,
            )
        return matches[0]

    def _configuration_node_position(self, workflow: WorkflowRuntime, flow_name: str) -> dict[str, Any] | None:
        """Metadata placing a new Configuration node below the workflow's Start node, or None to leave it to the editor."""
        nodes = self.engine.flow_manager.get_flow_by_name_in(workflow, flow_name).nodes
        for node in nodes.values():
            if not isinstance(node, StartNode):
                continue
            position = node.metadata.get("position")
            if not isinstance(position, dict) or "x" not in position or "y" not in position:
                return None
            return {"position": {"x": position["x"], "y": position["y"] + CONFIGURATION_NODE_OFFSET_Y}}
        return None

    def _publish_request_by_name(
        self, request: PublishWorkflowRequest, workflow: WorkflowRuntime
    ) -> PublishWorkflowRequest | PublishWorkflowResultFailure:
        """`request` with its `workflow_id`, which must be `workflow`'s, resolved to `workflow_name`, or why it can't publish.

        Publishers read the name.
        """
        if request.workflow_id is None:
            return request
        try:
            named = self.engine.workflow_registry.resolve_address(
                workflow_id=request.workflow_id, workflow_name=request.workflow_name, action="publish"
            )
        except WorkflowAddressError as err:
            return PublishWorkflowResultFailure(result_details=str(err))
        # The publisher packages `workflow`'s graph, so it publishes only that workflow.
        if named.workflow_id != workflow.workflow_id:
            details = (
                f"Attempted to publish workflow '{named.name}'. Failed because it isn't the workflow open in "
                "this tab. Publish it from its own tab."
            )
            return PublishWorkflowResultFailure(result_details=details)
        return replace(request, workflow_name=named.name)

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
                        workflow_file.name, load_metadata_result.metadata
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
    def _top_level_flow_name(workflow: WorkflowRuntime) -> str | None:
        for flow_name, parent in workflow.flows.name_to_parent_name.items():
            if parent is None:
                return flow_name
        return None


def _read_configuration(node: BaseNode) -> dict[str, Any]:
    """The values a Configuration node holds for its target's settings, keyed by parameter name."""
    return {
        parameter.name: node.get_parameter_value(parameter.name)
        for parameter in node.parameters
        if ParameterMode.PROPERTY in parameter.allowed_modes
    }


def _configuration_node_display_name(event_data: PublishWorkflowRegisteredEventData) -> str | None:
    """The library's display name for the Configuration node type, e.g. "Nuke Configuration"."""
    node_type = event_data.configuration_node_type
    library_name = event_data.configuration_node_library_name
    if node_type is None or library_name is None:
        return None
    try:
        return LibraryRegistry.get_library(library_name).get_node_metadata(node_type).display_name
    except LibraryRegistryError:
        return None


def _add_warning(result: PublishWorkflowResultSuccess, message: str) -> None:
    logger.warning(message)
    if isinstance(result.result_details, ResultDetails):
        details = list(result.result_details.result_details)
    else:
        details = [ResultDetail(message=result.result_details, level=logging.INFO)]
    details.append(ResultDetail(message=message, level=logging.WARNING))
    result.result_details = ResultDetails(*details)
