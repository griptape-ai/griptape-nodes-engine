"""A node group run in its own engine process hands that engine the run it belongs to."""

from __future__ import annotations

from argparse import ArgumentParser
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from griptape_nodes.bootstrap.workflow_executors.local_session_workflow_executor import LocalSessionWorkflowExecutor
from griptape_nodes.bootstrap.workflow_executors.subprocess_workflow_executor import SubprocessWorkflowExecutor

if TYPE_CHECKING:
    from pathlib import Path


async def _child_args(tmp_path: Path, parent_run_id: str | None) -> list[str]:
    workflow = tmp_path / "workflow.py"
    workflow.write_text("")
    executor = SubprocessWorkflowExecutor(workflow_path=str(workflow), parent_run_id=parent_run_id)
    execute = AsyncMock()
    executor.execute_python_script = execute  # type: ignore[method-assign]

    await executor.arun(flow_input={})

    return execute.call_args.kwargs["args"]


def _parsed_parent_run_id(args: list[str]) -> str | None:
    """What the child's executor reads from the arguments it was started with."""
    parser = ArgumentParser()
    LocalSessionWorkflowExecutor.add_cli_arguments(parser)
    parser.add_argument("--json-input")
    parser.add_argument("--workflow-path")
    return parser.parse_args(args).parent_run_id


@pytest.mark.asyncio
async def test_the_child_engine_is_told_the_parent_run(tmp_path: Path) -> None:
    """The group's spend then counts toward the run that started the group."""
    args = await _child_args(tmp_path, "parent-run")

    assert _parsed_parent_run_id(args) == "parent-run"


@pytest.mark.asyncio
async def test_no_parent_run_passes_no_parent_run_id(tmp_path: Path) -> None:
    """A workflow run outside any run starts the child with no parent run, so it counts on its own."""
    args = await _child_args(tmp_path, None)

    assert _parsed_parent_run_id(args) is None
