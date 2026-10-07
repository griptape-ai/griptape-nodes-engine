from __future__ import annotations

import json
import logging
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self

import anyio

from griptape_nodes.bootstrap.utils.python_subprocess_executor import PythonSubprocessExecutor
from griptape_nodes.bootstrap.utils.subprocess_websocket_listener import SubprocessWebSocketListenerMixin
from griptape_nodes.bootstrap.workflow_executors.workflow_executor import WorkflowExecutor
from griptape_nodes.drivers.storage import StorageBackend
from griptape_nodes.retained_mode.events.base_events import (
    EventResultFailure,
    EventResultSuccess,
    ExecutionEvent,
)
from griptape_nodes.retained_mode.events.execution_events import (
    ControlFlowCancelledEvent,
    ControlFlowResolvedEvent,
    StartFlowRequest,
)
from griptape_nodes.serialization.values import encode_value

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import TracebackType

    from griptape_nodes.retained_mode.events.base_events import ResultPayload

logger = logging.getLogger(__name__)


class SubprocessWorkflowExecutorError(Exception):
    """Exception raised during subprocess workflow execution."""


class SubprocessWorkflowExecutor(WorkflowExecutor, PythonSubprocessExecutor, SubprocessWebSocketListenerMixin):
    def __init__(
        self,
        workflow_path: str,
        on_start_flow_result: Callable[[ResultPayload], None] | None = None,
        on_event: Callable[[dict], None] | None = None,
        session_id: str | None = None,
    ) -> None:
        WorkflowExecutor.__init__(self)
        PythonSubprocessExecutor.__init__(self)
        self._init_websocket_listener(session_id=session_id, on_event=on_event)
        self._workflow_path = workflow_path
        self._on_start_flow_result = on_start_flow_result
        self._stored_exception: SubprocessWorkflowExecutorError | None = None

    async def __aenter__(self) -> Self:
        """Async context manager entry: start WebSocket listener."""
        await self._start_websocket_listener()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Async context manager exit: stop WebSocket listener."""
        await self._stop_websocket_listener()

    async def arun(
        self,
        flow_input: Any,
        storage_backend: StorageBackend = StorageBackend.LOCAL,
        **kwargs: Any,  # noqa: ARG002
    ) -> None:
        """Execute a workflow in a subprocess and wait for completion."""
        script_path = Path(__file__).parent / "utils" / "subprocess_script.py"

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_workflow_path = Path(tmpdir) / "workflow.py"
            tmp_script_path = Path(tmpdir) / "subprocess_script.py"

            try:
                async with (
                    await anyio.open_file(self._workflow_path, "rb") as src,
                    await anyio.open_file(tmp_workflow_path, "wb") as dst,
                ):
                    await dst.write(await src.read())

                async with (
                    await anyio.open_file(script_path, "rb") as src,
                    await anyio.open_file(tmp_script_path, "wb") as dst,
                ):
                    await dst.write(await src.read())
            except Exception as e:
                msg = f"Failed to copy workflow or script to temp directory: {e}"
                logger.exception(msg)
                raise SubprocessWorkflowExecutorError(msg) from e

            args = [
                "--json-input",
                json.dumps(encode_value(flow_input)),
                "--session-id",
                self._session_id,
                "--storage-backend",
                storage_backend.value,
                "--workflow-path",
                str(tmp_workflow_path),
            ]

            try:
                await self.execute_python_script(
                    script_path=tmp_script_path,
                    args=args,
                    cwd=Path(tmpdir),
                    env={
                        "GTN_CONFIG_ENABLE_WORKSPACE_FILE_WATCHING": "false",
                    },
                )
            except Exception as e:
                msg = f"Failed to execute subprocess script: {e}"
                logger.exception(msg)
                raise SubprocessWorkflowExecutorError(msg) from e
            finally:
                # Check if an exception was stored coming from the WebSocket
                if self._stored_exception:
                    raise self._stored_exception

    async def _handle_subprocess_event(self, event: dict) -> None:
        """Handle executor-specific events from the subprocess.

        Processes execution events and result events.
        """
        event_type = event.get("type", "unknown")
        if event_type == "execution_event":
            await self._process_execution_event(event)
        elif event_type in ["success_result", "failure_result"]:
            await self._process_result_event(event)

    async def _process_execution_event(self, event: dict) -> None:
        payload = event.get("payload", {})
        event_type = payload.get("event_type", "")

        # Focusing on ExecutionEvent types for the workflow executor
        if event_type not in ["ExecutionEvent", "EventResultSuccess", "EventResultFailure"]:
            logger.debug("Ignoring event type: %s", event_type)
            return

        ex_event = ExecutionEvent.from_dict(data=payload)

        if isinstance(ex_event.payload, ControlFlowResolvedEvent):
            logger.info("Workflow execution completed successfully")
            self.output = {ex_event.payload.end_node_name: ex_event.payload.parameter_output_values}

        if isinstance(ex_event.payload, ControlFlowCancelledEvent):
            logger.error("Workflow execution cancelled")

            details = ex_event.payload.result_details or "No details provided"
            msg = f"Workflow execution cancelled: {details}"

            if ex_event.payload.exception:
                msg = f"Exception running workflow: {ex_event.payload.exception}"
                self._stored_exception = SubprocessWorkflowExecutorError(ex_event.payload.exception)
            else:
                self._stored_exception = SubprocessWorkflowExecutorError(msg)

    async def _process_result_event(self, event: dict) -> None:
        payload = event.get("payload", {})
        if payload.get("type", "unknown") == "success_result":
            result_event = EventResultSuccess.from_dict(data=payload)
        else:
            result_event = EventResultFailure.from_dict(data=payload)

        if isinstance(result_event.request, StartFlowRequest):
            logger.info("Received StartFlowRequest result event")
            if self._on_start_flow_result:
                self._on_start_flow_result(result_event.result)
        else:
            logger.warning("Ignoring result event for request type: %s", type(result_event.request).__name__)
