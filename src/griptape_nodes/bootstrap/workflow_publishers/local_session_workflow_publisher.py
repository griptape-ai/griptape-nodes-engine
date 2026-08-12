"""Local session workflow publisher with WebSocket event emission support.

This module provides a workflow publisher that emits events over WebSocket
for real-time progress updates during the publishing process.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Self

from griptape_nodes.bootstrap.utils.subprocess_websocket_sender import SubprocessWebSocketSenderMixin
from griptape_nodes.bootstrap.workflow_publishers.local_workflow_publisher import (
    LocalPublisherError,
    LocalWorkflowPublisher,
)
from griptape_nodes.retained_mode.events.app_events import AppInitializationComplete
from griptape_nodes.retained_mode.events.base_events import (
    EventResultFailure,
    EventResultSuccess,
    ExecutionEvent,
    ExecutionGriptapeNodeEvent,
    ResultPayload,
)
from griptape_nodes.retained_mode.events.workflow_events import (
    PublishWorkflowProgressEvent,
    PublishWorkflowRequest,
    PublishWorkflowResultFailure,
)
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

if TYPE_CHECKING:
    from types import TracebackType

logger = logging.getLogger(__name__)


class LocalSessionWorkflowPublisher(LocalWorkflowPublisher, SubprocessWebSocketSenderMixin):
    """Publisher with WebSocket support for sending events to parent process.

    This publisher is used inside subprocesses to emit publishing progress events
    over WebSocket back to the parent process.
    """

    def __init__(self, session_id: str) -> None:
        super().__init__()
        self._init_websocket_sender(session_id)

    async def __aenter__(self) -> Self:
        """Async context manager entry: initialize queue and start WebSocket connection."""
        GriptapeNodes.EventManager().initialize_queue()
        await GriptapeNodes.EventManager().abroadcast_app_event(AppInitializationComplete())

        logger.info("Setting up publishing session %s", self._session_id)
        GriptapeNodes.SessionManager().save_session(self._session_id)
        GriptapeNodes.SessionManager().active_session_id = self._session_id
        await self._start_websocket_connection()

        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Async context manager exit."""
        await self._stop_websocket_connection()
        GriptapeNodes.SessionManager().remove_session(self._session_id)

    async def arun(
        self,
        workflow_name: str,
        workflow_path: str,
        publisher_name: str,
        published_workflow_file_name: str,
        **kwargs: Any,
    ) -> None:
        """Run the publish operation with WebSocket event emission enabled.

        Progress events are emitted by the Library handling the publishing process,
        which uses the GriptapeNodes event infrastructure to emit events that will
        be sent over WebSocket to the parent process.
        """
        try:
            await self._arun(
                workflow_name=workflow_name,
                workflow_path=workflow_path,
                publisher_name=publisher_name,
                published_workflow_file_name=published_workflow_file_name,
                **kwargs,
            )
        except Exception as e:
            msg = f"Unexpected error during publish: {e}"
            logger.exception(msg)
            raise LocalPublisherError(msg) from e
        finally:
            await self._stop_websocket_connection()

    async def _arun(  # noqa: C901, PLR0915
        self,
        workflow_name: str,
        workflow_path: str,
        publisher_name: str,
        published_workflow_file_name: str,
        **kwargs: Any,
    ) -> None:
        """Internal async run method with event queue monitoring and websocket integration."""
        # Load the workflow into memory
        await self.aprepare_workflow_for_run(flow_input={}, workflow_path=workflow_path)

        pickle_control_flow_result = kwargs.get("pickle_control_flow_result", False)
        publish_workflow_request = PublishWorkflowRequest(
            workflow_name=workflow_name,
            publisher_name=publisher_name,
            published_workflow_file_name=published_workflow_file_name,
            pickle_control_flow_result=pickle_control_flow_result,
        )

        # Send the publish request async (fire and forget pattern)
        publish_task = asyncio.create_task(GriptapeNodes.ahandle_request(publish_workflow_request))

        is_publish_finished = False
        error: Exception | None = None
        background_tasks: set[asyncio.Task] = set()

        def _handle_publish_result(task: asyncio.Task[ResultPayload]) -> None:
            nonlocal is_publish_finished, error
            try:
                publish_result = task.result()

                if isinstance(publish_result, PublishWorkflowResultFailure):
                    msg = f"Failed to publish workflow: {publish_result.result_details}"
                    logger.error(msg)
                    event_result_failure = EventResultFailure(request=publish_workflow_request, result=publish_result)
                    self.send_event("failure_result", event_result_failure.json())
                    is_publish_finished = True
                    error = LocalPublisherError(msg)
                else:
                    logger.info("Published workflow successfully")
                    event_result_success = EventResultSuccess(request=publish_workflow_request, result=publish_result)
                    self.send_event("success_result", event_result_success.json())
                    is_publish_finished = True
                    # Add a dummy event to wake up the loop now that publishing is done
                    event_queue = GriptapeNodes.EventManager().event_queue
                    queue_event_task = asyncio.create_task(event_queue.put(None))
                    background_tasks.add(queue_event_task)
                    queue_event_task.add_done_callback(background_tasks.discard)

            except Exception as e:
                msg = "Error during publish workflow"
                logger.exception(msg)
                is_publish_finished = True
                error = e
                # Add a dummy event to wake up the loop in failure cases
                event_queue = GriptapeNodes.EventManager().event_queue
                queue_event_task = asyncio.create_task(event_queue.put(None))
                background_tasks.add(queue_event_task)
                queue_event_task.add_done_callback(background_tasks.discard)

        publish_task.add_done_callback(_handle_publish_result)

        logger.info("Publish workflow request sent! Processing events...")

        def _handle_task_done(task: asyncio.Task) -> None:
            background_tasks.discard(task)
            if task.exception() and not task.cancelled():
                logger.error("Background task failed", exc_info=task.exception())

        event_queue = GriptapeNodes.EventManager().event_queue
        while not is_publish_finished:
            try:
                event = await event_queue.get()

                # Handle the dummy wake up event (None)
                if event is None:
                    event_queue.task_done()
                    continue

                logger.debug("Processing publish event: %s", type(event).__name__)

                if isinstance(event, ExecutionGriptapeNodeEvent):
                    # Unwrap the event and check if it contains a PublishWorkflowProgressEvent
                    wrapped_event = event.wrapped_event
                    if isinstance(wrapped_event, ExecutionEvent) and isinstance(
                        wrapped_event.payload, PublishWorkflowProgressEvent
                    ):
                        self.send_event("execution_event", wrapped_event.json())
                        logger.debug(
                            "Emitted progress event: %.1f%% - %s",
                            wrapped_event.payload.progress,
                            wrapped_event.payload.message,
                        )

                event_queue.task_done()

            except Exception as e:
                msg = f"Error handling queue event: {e}"
                logger.exception(msg)
                error = LocalPublisherError(msg)
                break

        if background_tasks:
            logger.info("Waiting for %d background tasks to complete", len(background_tasks))
            await asyncio.gather(*background_tasks, return_exceptions=True)

        await self._wait_for_websocket_queue_flush()

        if error is not None:
            raise error
