"""Tests that the parent passes its event server address and token to the subprocess."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import pytest

from griptape_nodes.bootstrap.utils.subprocess_websocket_base import SUBPROCESS_EVENTS_TOKEN_ENV_VAR
from griptape_nodes.bootstrap.workflow_executors.subprocess_workflow_executor import SubprocessWorkflowExecutor
from griptape_nodes.bootstrap.workflow_publishers.subprocess_workflow_publisher import SubprocessWorkflowPublisher

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path


def _assert_channel_passed(executor: Any, call_kwargs: Mapping[str, Any]) -> None:
    args = call_kwargs["args"]
    env = call_kwargs["env"]
    assert args[args.index("--events-url") + 1] == executor._get_events_url()
    assert env[SUBPROCESS_EVENTS_TOKEN_ENV_VAR] == executor._events_token
    # The token must not be visible on the command line.
    assert executor._events_token not in " ".join(args)


@pytest.mark.asyncio
async def test_executor_passes_events_url_in_args_and_token_in_env(tmp_path: Path) -> None:
    """Private Execution runs get the server address as an argument and the token in the environment."""
    workflow_path = tmp_path / "workflow.py"
    workflow_path.write_text("")
    executor = SubprocessWorkflowExecutor(workflow_path=str(workflow_path))
    executor.execute_python_script = AsyncMock()  # type: ignore[method-assign]

    async with executor:
        await executor.arun(flow_input={})

    _assert_channel_passed(executor, executor.execute_python_script.call_args.kwargs)


@pytest.mark.asyncio
async def test_publisher_passes_events_url_in_args_and_token_in_env(tmp_path: Path) -> None:
    """Library-environment publishes get the server address as an argument and the token in the environment."""
    workflow_path = tmp_path / "workflow.py"
    workflow_path.write_text("")
    publisher = SubprocessWorkflowPublisher()
    publisher.execute_python_script = AsyncMock()  # type: ignore[method-assign]

    async with publisher:
        await publisher.arun(
            workflow_name="wf",
            workflow_path=str(workflow_path),
            publisher_name="pub",
            published_workflow_file_name="out",
        )

    _assert_channel_passed(publisher, publisher.execute_python_script.call_args.kwargs)
