"""Tests for NodeExecutor._delete_workflow lookup and registration logic."""

from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from griptape_nodes.common.node_executor import NodeExecutor
from griptape_nodes.retained_mode.events.workflow_events import (
    DeleteWorkflowRequest,
    DeleteWorkflowResultSuccess,
    LoadWorkflowMetadataResultFailure,
    LoadWorkflowMetadataResultSuccess,
)


def _make_executor() -> NodeExecutor:
    return NodeExecutor(engine=MagicMock())


def _make_delete_success() -> DeleteWorkflowResultSuccess:
    return DeleteWorkflowResultSuccess(result_details="ok")


class TestDeleteWorkflowUsesTheRegisteredName:
    """_delete_workflow deletes the entry the registry holds for the file, by that entry's name."""

    @pytest.mark.asyncio
    async def test_deletes_the_registered_entry_by_its_name(self, tmp_path: Path) -> None:
        workflow_path = tmp_path / "subdir" / "my_flow.py"

        executor = _make_executor()
        mock_engine = cast("MagicMock", executor.engine)
        mock_engine.ahandle_request = AsyncMock(return_value=_make_delete_success())
        registered = MagicMock()
        registered.name = "subdir/my_flow"
        mock_engine.workflow_registry.find_by_path.return_value = registered

        await executor._delete_workflow(workflow_path=workflow_path)

        mock_engine.workflow_registry.find_by_path.assert_called_once_with(workflow_path)
        delete_request = mock_engine.ahandle_request.call_args.args[0]
        assert isinstance(delete_request, DeleteWorkflowRequest)
        assert delete_request.name == "subdir/my_flow"
        mock_engine.workflow_registry.register_file.assert_not_called()


class TestDeleteWorkflowRegistrationFallback:
    """_delete_workflow registers the workflow when it is absent from the registry."""

    @pytest.mark.asyncio
    async def test_registers_workflow_unlisted_when_not_in_registry(self, tmp_path: Path) -> None:
        """A file another process registered is registered here, unlisted, so it can be deleted."""
        workflow_path = tmp_path / "my_flow.py"

        mock_metadata = MagicMock()
        mock_metadata_result = MagicMock(spec=LoadWorkflowMetadataResultSuccess)
        mock_metadata_result.metadata = mock_metadata

        executor = _make_executor()
        mock_engine = cast("MagicMock", executor.engine)
        # First ahandle_request call loads metadata; second issues DeleteWorkflowRequest.
        mock_engine.ahandle_request = AsyncMock(side_effect=[mock_metadata_result, _make_delete_success()])
        mock_registry = mock_engine.workflow_registry
        mock_registry.find_by_path.return_value = None
        mock_registry.register_file.return_value.name = "my_flow"

        await executor._delete_workflow(workflow_path=workflow_path)

        mock_registry.register_file.assert_called_once_with(workflow_path, mock_metadata, listed=False)
        delete_request = mock_engine.ahandle_request.call_args.args[0]
        assert isinstance(delete_request, DeleteWorkflowRequest)
        assert delete_request.name == "my_flow"

    @pytest.mark.asyncio
    async def test_skips_the_delete_when_the_file_cannot_be_read(self, tmp_path: Path) -> None:
        """A file whose metadata cannot be read names no workflow, so nothing is deleted."""
        executor = _make_executor()
        mock_engine = cast("MagicMock", executor.engine)
        mock_engine.ahandle_request = AsyncMock(return_value=LoadWorkflowMetadataResultFailure(result_details="bad"))
        mock_engine.workflow_registry.find_by_path.return_value = None

        await executor._delete_workflow(workflow_path=tmp_path / "my_flow.py")

        mock_engine.workflow_registry.register_file.assert_not_called()
        assert mock_engine.ahandle_request.call_count == 1
