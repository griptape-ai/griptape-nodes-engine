from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Self

from griptape_nodes.bootstrap.utils.subprocess_websocket_sender import SubprocessWebSocketSenderMixin
from griptape_nodes.bootstrap.workflow_executors.local_workflow_executor import (
    LocalExecutorError,
    LocalWorkflowExecutor,
)
from griptape_nodes.drivers.storage import StorageBackend
from griptape_nodes.retained_mode.events.app_events import AppInitializationComplete
from griptape_nodes.retained_mode.events.base_events import (
    EventRequest,
    EventResultFailure,
    EventResultSuccess,
    ExecutionEvent,
    ExecutionGriptapeNodeEvent,
    ProgressEvent,
    ResultPayload,
)
from griptape_nodes.retained_mode.events.execution_events import (
    ControlFlowCancelledEvent,
    GriptapeEvent,
    StartFlowRequest,
    StartFlowResultFailure,
)
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

if TYPE_CHECKING:
    from argparse import ArgumentParser, Namespace
    from collections.abc import Callable
    from pathlib import Path
    from types import TracebackType

logger = logging.getLogger(__name__)


class LocalSessionWorkflowExecutor(LocalWorkflowExecutor, SubprocessWebSocketSenderMixin):
    def __init__(  # noqa: PLR0913
        self,
        session_id: str,
        storage_backend: StorageBackend = StorageBackend.LOCAL,
        on_start_flow_result: Callable[[ResultPayload], None] | None = None,
        save_on_failure_path: str | None = None,
        *,
        project_file_path: Path | None = None,
        pickle_control_flow_result: bool = False,
    ):
        super().__init__(
            storage_backend=storage_backend,
            project_file_path=project_file_path,
            save_on_failure_path=save_on_failure_path,
            pickle_control_flow_result=pickle_control_flow_result,
        )
        self._init_websocket_sender(session_id)
        self._on_start_flow_result = on_start_flow_result

    async def __aenter__(self) -> Self:
        """Async context manager entry: initialize queue and broadcast app initialization."""
        GriptapeNodes.EventManager().initialize_queue()
        await GriptapeNodes.EventManager().abroadcast_app_event(AppInitializationComplete())

        logger.info("Setting up session %s", self._session_id)
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

        # TODO: Broadcast shutdown https://github.com/griptape-ai/griptape-nodes/issues/2149

    async def _process_execution_event_async(self, event: ExecutionGriptapeNodeEvent) -> None:
        """Process execution events asynchronously for real-time websocket emission."""
        logger.debug("REAL-TIME: Processing execution event for session %s", self._session_id)
        self.send_event("execution_event", event.wrapped_event.json())

    async def arun(
        self,
        flow_input: Any,
        storage_backend: StorageBackend | None = None,
        *,
        pickle_control_flow_result: bool | None = None,
        **kwargs: Any,
    ) -> None:
        """Executes a local workflow.

        Executes a workflow by setting up event listeners, registering libraries,
        loading the user-defined workflow, and running the specified workflow.

        Parameters:
            flow_input: Input data for the flow, typically a dictionary.
            storage_backend: Accepted for compatibility with the base-class run path,
                but ignored here: the storage backend is applied once at construction
                via `_set_storage_backend`. Passing it to the run path has no effect.
            pickle_control_flow_result: Per-call override for the executor's
                save-time default. None means "use the instance default".

        Returns:
            None
        """
        try:
            await self._arun(
                flow_input=flow_input,
                storage_backend=storage_backend,
                pickle_control_flow_result=pickle_control_flow_result,
                **kwargs,
            )
        except Exception as e:
            msg = f"Workflow execution failed: {e}"
            logger.exception(msg)
            control_flow_cancelled_event = ControlFlowCancelledEvent(
                result_details="Encountered an error during workflow execution",
                exception=e,
            )
            execution_event = ExecutionEvent(payload=control_flow_cancelled_event)
            self.send_event("execution_event", execution_event.json())
            await self._wait_for_websocket_queue_flush()
            await asyncio.sleep(1)
            raise LocalExecutorError(msg) from e
        finally:
            await self._stop_websocket_connection()

    async def _arun(  # noqa: C901, PLR0915
        self,
        flow_input: Any,
        storage_backend: StorageBackend | None = None,  # noqa: ARG002
        *,
        pickle_control_flow_result: bool | None = None,
        **kwargs: Any,
    ) -> None:
        """Internal async run method with detailed event handling and websocket integration.

        `storage_backend` is accepted for signature parity with `arun`/the base run path,
        but ignored: the backend is applied once at construction via `_set_storage_backend`.
        """
        flow_name = await self.aprepare_workflow_for_run(
            flow_input=flow_input,
            **kwargs,
        )

        # Send the run command to actually execute it (fire and forget)
        effective_pickle = (
            pickle_control_flow_result if pickle_control_flow_result is not None else self._pickle_control_flow_result
        )
        start_flow_request = StartFlowRequest(flow_name=flow_name, pickle_control_flow_result=effective_pickle)
        start_flow_task = asyncio.create_task(GriptapeNodes.ahandle_request(start_flow_request))

        is_flow_finished = False
        error: Exception | None = None

        def _handle_start_flow_result(task: asyncio.Task[ResultPayload]) -> None:
            nonlocal is_flow_finished, error, start_flow_request
            try:
                start_flow_result = task.result()
                self._on_start_flow_result(start_flow_result) if self._on_start_flow_result is not None else None

                if isinstance(start_flow_result, StartFlowResultFailure):
                    msg = f"Failed to start flow {flow_name}"
                    logger.error(msg)
                    event_result_failure = EventResultFailure(request=start_flow_request, result=start_flow_result)
                    self.send_event("failure_result", event_result_failure.json())
                    raise LocalExecutorError(msg) from start_flow_result.exception  # noqa: TRY301

                event_result_success = EventResultSuccess(request=start_flow_request, result=start_flow_result)
                self.send_event("success_result", event_result_success.json())

            except Exception as e:
                msg = "Error starting workflow"
                logger.exception(msg)
                is_flow_finished = True
                error = e
                # The StartFlowRequest is sent asynchronously to enable real-time event emission via WebSocket.
                # The main while loop below then waits for events from the queue. However, if StartFlowRequest fails
                # immediately, then no events are ever added to the queue, causing the loop to hang indefinitely
                # on event_queue.get(). This fix adds a dummy event to wake up the loop in failure cases.
                event_queue = GriptapeNodes.EventManager().event_queue
                queue_event_task = asyncio.create_task(event_queue.put(None))
                background_tasks.add(queue_event_task)
                queue_event_task.add_done_callback(background_tasks.discard)

        start_flow_task.add_done_callback(_handle_start_flow_result)

        logger.info("Workflow start request sent! Processing events...")

        background_tasks: set[asyncio.Task] = set()

        def _handle_task_done(task: asyncio.Task) -> None:
            background_tasks.discard(task)
            if task.exception() and not task.cancelled():
                logger.exception("Background task failed", exc_info=task.exception())

        event_queue = GriptapeNodes.EventManager().event_queue
        while not is_flow_finished:
            try:
                event = await event_queue.get()

                # Handle the dummy wake up event (None)
                if event is None:
                    event_queue.task_done()
                    continue

                logger.debug("Processing event: %s", type(event).__name__)

                if isinstance(event, EventRequest):
                    self.send_event("event_request", event.json())
                    task = asyncio.create_task(self._handle_event_request(event))
                    background_tasks.add(task)
                    task.add_done_callback(_handle_task_done)
                elif isinstance(event, ExecutionGriptapeNodeEvent):
                    # Emit execution event via WebSocket
                    self.send_event("execution_event", event.wrapped_event.json())
                    task = asyncio.create_task(self._process_execution_event_async(event))
                    background_tasks.add(task)
                    task.add_done_callback(_handle_task_done)
                    is_flow_finished, error = await self._handle_execution_event(event, flow_name)
                elif isinstance(event, ProgressEvent):
                    # Convert ProgressEvent to GriptapeEvent and emit via WebSocket
                    payload = GriptapeEvent(
                        node_name=event.node_name,
                        parameter_name=event.parameter_name,
                        type=type(event).__name__,
                        value=event.value,
                    )
                    execution_event = ExecutionEvent(payload=payload)
                    self.send_event("execution_event", execution_event.json())

                event_queue.task_done()

            except Exception as e:
                msg = f"Error handling queue event: {e}"
                logger.exception(msg)
                error = LocalExecutorError(msg)
                break

        if background_tasks:
            logger.info("Waiting for %d background tasks to complete", len(background_tasks))
            await asyncio.gather(*background_tasks, return_exceptions=True)

        await self._wait_for_websocket_queue_flush()

        if error is not None:
            raise error

    @classmethod
    def add_cli_arguments(
        cls,
        parser: ArgumentParser,
        *,
        pickle_control_flow_result_default: bool = False,
    ) -> None:
        super().add_cli_arguments(parser, pickle_control_flow_result_default=pickle_control_flow_result_default)
        parser.add_argument(
            "--session-id",
            default=None,
            help="ID of the session to use",
        )

    @classmethod
    def _cli_constructor_kwargs(cls, args: Namespace) -> dict[str, Any]:
        kwargs = super()._cli_constructor_kwargs(args)
        kwargs["session_id"] = args.session_id
        return kwargs
