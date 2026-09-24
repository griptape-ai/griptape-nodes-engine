"""Tests for rez wrapping of workflow subprocesses in PythonSubprocessExecutor."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from griptape_nodes.bootstrap.utils.python_subprocess_executor import PythonSubprocessExecutor

EXECUTOR_MODULE = "griptape_nodes.bootstrap.utils.python_subprocess_executor"
REZ_PREFIX = ["rez", "env", "griptape_nodes_library_demo", "--"]


def _finished_process() -> MagicMock:
    """A subprocess stand-in that exits 0 with no output streams."""
    proc = MagicMock()
    proc.stdout = None
    proc.stderr = None
    proc.wait = AsyncMock()
    proc.returncode = 0
    proc.pid = 1234
    return proc


async def _run(*, rez_enabled: bool, specs: list[str] | None) -> tuple[list[str], MagicMock, MagicMock]:
    """Run a script through the executor and return the argv passed to the subprocess."""
    script = Path("workflow.py")
    with (
        patch(f"{EXECUTOR_MODULE}.is_rez_enabled", return_value=rez_enabled),
        patch(f"{EXECUTOR_MODULE}.build_rez_env_prefix", return_value=REZ_PREFIX) as mock_prefix,
        patch(f"{EXECUTOR_MODULE}.resolve_and_log_rez_context", return_value=[]) as mock_resolve,
        patch("asyncio.create_subprocess_exec", AsyncMock(return_value=_finished_process())) as mock_exec,
    ):
        await PythonSubprocessExecutor().execute_python_script(script, args=["--flag"], rez_package_specs=specs)

    argv = list(mock_exec.call_args.args)
    return argv, mock_prefix, mock_resolve


class TestRezWrapping:
    @pytest.mark.asyncio
    async def test_wraps_command_when_rez_enabled_and_specs_given(self) -> None:
        specs = ["griptape_nodes_library_demo"]

        argv, mock_prefix, mock_resolve = await _run(rez_enabled=True, specs=specs)

        assert argv == [*REZ_PREFIX, sys.executable, "workflow.py", "--flag"]
        mock_prefix.assert_called_once_with(specs)
        mock_resolve.assert_called_once_with(specs)

    @pytest.mark.asyncio
    async def test_no_wrap_when_rez_disabled(self) -> None:
        argv, mock_prefix, mock_resolve = await _run(rez_enabled=False, specs=["griptape_nodes_library_demo"])

        assert argv == [sys.executable, "workflow.py", "--flag"]
        mock_prefix.assert_not_called()
        mock_resolve.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_wrap_when_no_specs(self) -> None:
        argv, mock_prefix, mock_resolve = await _run(rez_enabled=True, specs=None)

        assert argv == [sys.executable, "workflow.py", "--flag"]
        mock_prefix.assert_not_called()
        mock_resolve.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_wrap_when_specs_empty(self) -> None:
        argv, mock_prefix, _ = await _run(rez_enabled=True, specs=[])

        assert argv == [sys.executable, "workflow.py", "--flag"]
        mock_prefix.assert_not_called()
