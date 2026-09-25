from __future__ import annotations

import logging
from typing import Any

from griptape_nodes.bootstrap.workflow_executors.local_workflow_executor import LocalWorkflowExecutor
from griptape_nodes.retained_mode.events.workflow_events import PublishWorkflowRequest, PublishWorkflowResultSuccess
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

logger = logging.getLogger(__name__)


class LocalPublisherError(Exception):
    """Exception raised during local workflow publish."""


class LocalWorkflowPublisher(LocalWorkflowExecutor):
    async def arun(
        self,
        workflow_name: str,
        workflow_path: str,
        publisher_name: str,
        published_workflow_file_name: str,
        **kwargs: Any,  # noqa: ARG002 callers may still pass the deprecated pickle_control_flow_result
    ) -> None:
        # Load the workflow into memory
        await self.aprepare_workflow_for_run(flow_input={}, workflow_path=workflow_path)
        publish_workflow_request = PublishWorkflowRequest(
            workflow_name=workflow_name,
            publisher_name=publisher_name,
            published_workflow_file_name=published_workflow_file_name,
        )
        publish_workflow_result = await GriptapeNodes.ahandle_request(publish_workflow_request)

        if isinstance(publish_workflow_result, PublishWorkflowResultSuccess):
            logger.info("Published workflow to %s", publish_workflow_result.published_workflow_file_path)
        else:
            msg = f"Failed to publish workflow: {publish_workflow_result}"
            raise LocalPublisherError(msg)
