"""Tests for NodeExecutor._delete_workflow key derivation and registration logic."""

import tempfile
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import anyio
import pytest

from griptape_nodes.common.node_executor import NodeExecutor
from griptape_nodes.retained_mode.events.workflow_events import (
    DeleteWorkflowRequest,
    DeleteWorkflowResultSuccess,
    LoadWorkflowMetadataResultSuccess,
)

MODULE_PATH = "griptape_nodes.common.node_executor"


def _make_executor() -> NodeExecutor:
    return NodeExecutor(engine=MagicMock())


def _make_delete_success() -> DeleteWorkflowResultSuccess:
    return DeleteWorkflowResultSuccess(result_details="ok")


class TestDeleteWorkflowKeyDerivation:
    """_delete_workflow derives the correct registry key from workflow_path."""

    @pytest.mark.asyncio
    async def test_workflow_in_workspace_root_uses_stem(self) -> None:
        """A workflow file directly in the workspace produces a stem-only key."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = await anyio.Path(tmp_dir).resolve()
            workflow_path = workspace / "my_flow.py"
            await workflow_path.touch()

            executor = _make_executor()
            mock_engine = cast("MagicMock", executor.engine)
            mock_engine.config_manager.workspace_path = workspace
            mock_engine.ahandle_request = AsyncMock(return_value=_make_delete_success())

            with patch(f"{MODULE_PATH}.WorkflowRegistry") as mock_registry:
                # Mark as already registered so key derivation is the only thing being tested.
                mock_registry.has_workflow_with_name.return_value = True

                await executor._delete_workflow(workflow_path=Path(workflow_path))

            delete_request = mock_engine.ahandle_request.call_args.args[0]
            assert isinstance(delete_request, DeleteWorkflowRequest)
            assert delete_request.name == "my_flow"

    @pytest.mark.asyncio
    async def test_workflow_in_workspace_subdir_uses_relative_path(self) -> None:
        """A workflow in a workspace subdirectory produces a subdir/stem key."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = await anyio.Path(tmp_dir).resolve()
            workflow_path = workspace / "subdir" / "my_flow.py"
            await workflow_path.parent.mkdir()
            await workflow_path.touch()

            executor = _make_executor()
            mock_engine = cast("MagicMock", executor.engine)
            mock_engine.config_manager.workspace_path = workspace
            mock_engine.ahandle_request = AsyncMock(return_value=_make_delete_success())

            with patch(f"{MODULE_PATH}.WorkflowRegistry") as mock_registry:
                mock_registry.has_workflow_with_name.return_value = True

                await executor._delete_workflow(workflow_path=Path(workflow_path))

            delete_request = mock_engine.ahandle_request.call_args.args[0]
            assert isinstance(delete_request, DeleteWorkflowRequest)
            assert delete_request.name == "subdir/my_flow"

    @pytest.mark.asyncio
    async def test_workflow_outside_workspace_uses_absolute_key(self) -> None:
        """A workflow file outside the workspace produces an absolute path key."""
        with (
            tempfile.TemporaryDirectory() as workspace_dir,
            tempfile.TemporaryDirectory() as other_dir,
        ):
            workspace = await anyio.Path(workspace_dir).resolve()
            workflow_path = await anyio.Path(other_dir).resolve() / "my_flow.py"
            await workflow_path.touch()

            executor = _make_executor()
            mock_engine = cast("MagicMock", executor.engine)
            mock_engine.config_manager.workspace_path = workspace
            mock_engine.ahandle_request = AsyncMock(return_value=_make_delete_success())

            with patch(f"{MODULE_PATH}.WorkflowRegistry") as mock_registry:
                mock_registry.has_workflow_with_name.return_value = True

                await executor._delete_workflow(workflow_path=Path(workflow_path))

            delete_request = mock_engine.ahandle_request.call_args.args[0]
            assert isinstance(delete_request, DeleteWorkflowRequest)
            expected_key = (await anyio.Path(other_dir).resolve() / "my_flow").as_posix()
            assert delete_request.name == expected_key


class TestDeleteWorkflowRegistrationFallback:
    """_delete_workflow registers the workflow when it is absent from the registry."""

    @pytest.mark.asyncio
    async def test_registers_workflow_when_not_in_registry(self) -> None:
        """generate_new_workflow is called when the workflow is not registered."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = await anyio.Path(tmp_dir).resolve()
            workflow_path = workspace / "my_flow.py"
            await workflow_path.touch()

            mock_metadata = MagicMock()
            mock_metadata_result = MagicMock(spec=LoadWorkflowMetadataResultSuccess)
            mock_metadata_result.metadata = mock_metadata

            executor = _make_executor()
            mock_engine = cast("MagicMock", executor.engine)
            mock_engine.config_manager.workspace_path = workspace
            # First ahandle_request call loads metadata; second issues DeleteWorkflowRequest.
            mock_engine.ahandle_request = AsyncMock(side_effect=[mock_metadata_result, _make_delete_success()])

            with patch(f"{MODULE_PATH}.WorkflowRegistry") as mock_registry:
                mock_registry.has_workflow_with_name.return_value = False

                await executor._delete_workflow(workflow_path=Path(workflow_path))

            mock_registry.generate_new_workflow.assert_called_once_with(
                registry_key="my_flow", metadata=mock_metadata, file_path="my_flow.py"
            )

    @pytest.mark.asyncio
    async def test_skips_registration_when_already_in_registry(self) -> None:
        """generate_new_workflow is not called when the workflow is already registered."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = await anyio.Path(tmp_dir).resolve()
            workflow_path = workspace / "my_flow.py"
            await workflow_path.touch()

            executor = _make_executor()
            mock_engine = cast("MagicMock", executor.engine)
            mock_engine.config_manager.workspace_path = workspace
            mock_engine.ahandle_request = AsyncMock(return_value=_make_delete_success())

            with patch(f"{MODULE_PATH}.WorkflowRegistry") as mock_registry:
                mock_registry.has_workflow_with_name.return_value = True

                await executor._delete_workflow(workflow_path=Path(workflow_path))

            mock_registry.generate_new_workflow.assert_not_called()
