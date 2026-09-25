from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self

import anyio

from griptape_nodes.bootstrap.utils.python_subprocess_executor import PythonSubprocessExecutor
from griptape_nodes.bootstrap.utils.subprocess_websocket_listener import SubprocessWebSocketListenerMixin
from griptape_nodes.bootstrap.workflow_publishers.local_workflow_publisher import LocalWorkflowPublisher

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import TracebackType

logger = logging.getLogger(__name__)


class SubprocessWorkflowPublisherError(Exception):
    """Exception raised during subprocess workflow publishing."""


class SubprocessWorkflowPublisher(LocalWorkflowPublisher, PythonSubprocessExecutor, SubprocessWebSocketListenerMixin):
    def __init__(
        self,
        on_event: Callable[[dict], None] | None = None,
        session_id: str | None = None,
    ) -> None:
        PythonSubprocessExecutor.__init__(self)
        self._init_websocket_listener(session_id=session_id, on_event=on_event)

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
        workflow_name: str,
        workflow_path: str,
        publisher_name: str,
        published_workflow_file_name: str,
        **kwargs: Any,  # noqa: ARG002 callers may still pass the deprecated pickle_control_flow_result
    ) -> None:
        """Publish a workflow in a subprocess and wait for completion."""
        script_path = Path(__file__).parent / "utils" / "subprocess_script.py"

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_workflow_path = Path(tmpdir) / "workflow.py"
            tmp_script_path = Path(tmpdir) / "subprocess_script.py"

            try:
                async with (
                    await anyio.open_file(workflow_path, "rb") as src,
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
                raise SubprocessWorkflowPublisherError(msg) from e

            args = [
                "--workflow-name",
                workflow_name,
                "--workflow-path",
                str(tmp_workflow_path),
                "--publisher-name",
                publisher_name,
                "--published-workflow-file-name",
                published_workflow_file_name,
                "--session-id",
                self._session_id,
            ]
            await self.execute_python_script(
                script_path=tmp_script_path,
                args=args,
                cwd=Path(tmpdir),
                env={
                    "GTN_CONFIG_ENABLE_WORKSPACE_FILE_WATCHING": "false",
                },
            )

    async def _handle_subprocess_event(self, event: dict) -> None:
        """Handle publisher-specific events from the subprocess.

        Currently, this is a no-op as we just forward all events via the on_event callback.
        Subclasses can override to add specific event handling logic.
        """
