"""Tests for ContextManager.push_workflow."""

import ast
import tempfile
from pathlib import Path

import pytest

from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes


class TestPushWorkflow:
    """Tests for ContextManager.push_workflow."""

    def test_push_workflow_with_name(self, griptape_nodes: GriptapeNodes) -> None:
        """workflow_name is used directly as the registry key."""
        context_manager = griptape_nodes.ContextManager()
        result = context_manager.push_workflow(workflow_name="my_workflow")

        assert result == "my_workflow"
        assert context_manager.get_current_workflow_name() == "my_workflow"

        context_manager.pop_workflow()

    def test_push_workflow_with_file_path_inside_workspace(self, griptape_nodes: GriptapeNodes) -> None:
        """file_path inside workspace produces a workspace-relative registry key."""
        context_manager = griptape_nodes.ContextManager()
        config_manager = griptape_nodes.ConfigManager()

        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            original = config_manager.workspace_path
            config_manager.workspace_path = workspace
            try:
                workflow_file = workspace / "subdir" / "my_flow.py"
                result = context_manager.push_workflow(file_path=str(workflow_file))
            finally:
                config_manager.workspace_path = original

        assert result == "subdir/my_flow"
        assert context_manager.get_current_workflow_name() == "subdir/my_flow"

        context_manager.pop_workflow()

    def test_push_workflow_with_file_path_outside_workspace(self, griptape_nodes: GriptapeNodes) -> None:
        """file_path outside workspace uses the absolute path as the registry key."""
        context_manager = griptape_nodes.ContextManager()
        config_manager = griptape_nodes.ConfigManager()

        with tempfile.TemporaryDirectory() as workspace_dir, tempfile.TemporaryDirectory() as other_dir:
            workspace = Path(workspace_dir)
            original = config_manager.workspace_path
            config_manager.workspace_path = workspace
            try:
                workflow_file = Path(other_dir) / "my_flow.py"
                result = context_manager.push_workflow(file_path=str(workflow_file))
            finally:
                config_manager.workspace_path = original

        expected = (Path(other_dir).resolve() / "my_flow").as_posix()
        assert result == expected
        assert context_manager.get_current_workflow_name() == expected

        context_manager.pop_workflow()

    def test_push_workflow_raises_when_both_provided(self, griptape_nodes: GriptapeNodes) -> None:
        """Raises ValueError when both workflow_name and file_path are given."""
        context_manager = griptape_nodes.ContextManager()

        with pytest.raises(ValueError, match="not both"):
            context_manager.push_workflow(workflow_name="my_workflow", file_path="/some/path.py")

    def test_push_workflow_raises_when_neither_provided(self, griptape_nodes: GriptapeNodes) -> None:
        """Raises ValueError when neither workflow_name nor file_path is given."""
        context_manager = griptape_nodes.ContextManager()

        with pytest.raises(ValueError, match="must be provided"):
            context_manager.push_workflow()

    def test_push_workflow_strips_extension(self, griptape_nodes: GriptapeNodes) -> None:
        """Registry key derived from file_path has no file extension."""
        context_manager = griptape_nodes.ContextManager()
        config_manager = griptape_nodes.ConfigManager()

        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            original = config_manager.workspace_path
            config_manager.workspace_path = workspace
            try:
                workflow_file = workspace / "my_workflow.py"
                result = context_manager.push_workflow(file_path=str(workflow_file))
            finally:
                config_manager.workspace_path = original

        assert result == "my_workflow"
        assert "." not in result

        context_manager.pop_workflow()


class TestGeneratedWorkflowCode:
    """Tests that generated workflow code uses push_workflow(file_path=__file__)."""

    def test_generated_code_uses_file_path_not_workflow_name(self, griptape_nodes: GriptapeNodes) -> None:
        """_generate_workflow_run_prerequisite_code emits push_workflow(file_path=__file__)."""
        from griptape_nodes.retained_mode.managers.workflow_manager import ImportRecorder

        workflow_manager = griptape_nodes.WorkflowManager()
        import_recorder = ImportRecorder()
        code_blocks = workflow_manager._generate_workflow_run_prerequisite_code(
            import_recorder=import_recorder,
            library_names=[],
        )

        module = ast.Module(body=[n for n in code_blocks if isinstance(n, ast.stmt)], type_ignores=[])
        ast.fix_missing_locations(module)
        source = ast.unparse(module)

        assert "push_workflow(file_path=__file__)" in source
        assert "workflow_name=" not in source
        assert "workflow_name=" not in source


class TestEnsureWorkflowAndFlowRequest:
    """Tests for ContextManager.on_ensure_workflow_and_flow_request."""

    def _cleanup(self, griptape_nodes: GriptapeNodes) -> None:
        """Tear down any workflow/flow state left over between tests."""
        from griptape_nodes.retained_mode.events.object_events import ClearAllObjectStateRequest

        griptape_nodes.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))

    def test_creates_workflow_and_flow_from_cold_start(self, griptape_nodes: GriptapeNodes) -> None:
        from griptape_nodes.retained_mode.events.context_events import (
            EnsureWorkflowAndFlowRequest,
            EnsureWorkflowAndFlowResultSuccess,
        )

        self._cleanup(griptape_nodes)
        context_manager = griptape_nodes.ContextManager()
        assert not context_manager.has_current_workflow()
        assert not context_manager.has_current_flow()

        result = context_manager.on_ensure_workflow_and_flow_request(
            EnsureWorkflowAndFlowRequest(workflow_name="my_workflow", flow_name="my_flow")
        )

        assert isinstance(result, EnsureWorkflowAndFlowResultSuccess)
        assert result.workflow_name == "my_workflow"
        assert result.flow_name == "my_flow"
        assert result.created_workflow is True
        assert result.created_flow is True
        assert context_manager.has_current_workflow()
        assert context_manager.has_current_flow()

        self._cleanup(griptape_nodes)

    def test_reuses_existing_workflow_and_flow(self, griptape_nodes: GriptapeNodes) -> None:
        from griptape_nodes.retained_mode.events.context_events import (
            EnsureWorkflowAndFlowRequest,
            EnsureWorkflowAndFlowResultSuccess,
        )

        self._cleanup(griptape_nodes)
        context_manager = griptape_nodes.ContextManager()

        # Bootstrap once.
        first = context_manager.on_ensure_workflow_and_flow_request(
            EnsureWorkflowAndFlowRequest(workflow_name="scratch", flow_name="canvas")
        )
        assert isinstance(first, EnsureWorkflowAndFlowResultSuccess)

        # Calling again with different names should be a no-op: existing context wins.
        second = context_manager.on_ensure_workflow_and_flow_request(
            EnsureWorkflowAndFlowRequest(workflow_name="ignored", flow_name="also_ignored")
        )

        assert isinstance(second, EnsureWorkflowAndFlowResultSuccess)
        assert second.workflow_name == "scratch"
        assert second.flow_name == "canvas"
        assert second.created_workflow is False
        assert second.created_flow is False

        self._cleanup(griptape_nodes)

    def test_auto_generates_workflow_name_when_none_given(self, griptape_nodes: GriptapeNodes) -> None:
        from griptape_nodes.node_library.workflow_registry import WorkflowRegistry
        from griptape_nodes.retained_mode.events.context_events import (
            EnsureWorkflowAndFlowRequest,
            EnsureWorkflowAndFlowResultSuccess,
        )

        self._cleanup(griptape_nodes)
        context_manager = griptape_nodes.ContextManager()

        result = context_manager.on_ensure_workflow_and_flow_request(EnsureWorkflowAndFlowRequest())

        assert isinstance(result, EnsureWorkflowAndFlowResultSuccess)
        assert result.workflow_name.startswith(WorkflowRegistry.UNSAVED_KEY_PREFIX)
        assert WorkflowRegistry.has_workflow_with_name(result.workflow_name)
        assert result.created_workflow is True
        assert result.created_flow is True

        self._cleanup(griptape_nodes)
