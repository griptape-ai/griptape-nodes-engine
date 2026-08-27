"""Regression tests for the workflow-context invariants behind issue #406.

https://github.com/griptape-ai/griptape-nodes-desktop/issues/406

The engine answers "what workflow is open" out of `ContextManager._workflow_stack[-1]` --
that is what `GetWorkflowContextRequest` returns and what the heartbeat reports as
`current_workflow`. Every editor adoption path trusts that answer without checking it, so
any stale or registry-orphaned entry left on the stack surfaces to the user as the editor
opening a workflow they never chose. These tests pin the invariants that keep the stack
honest: it replaces rather than accumulates, it unwinds on failure, and it never reports a
name the registry cannot back.
"""

import ast
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from griptape_nodes.node_library.workflow_registry import WorkflowMetadata, WorkflowRegistry
from griptape_nodes.retained_mode.engine import Engine
from griptape_nodes.retained_mode.events.context_events import (
    GetWorkflowContextRequest,
    GetWorkflowContextSuccess,
    SetWorkflowContextRequest,
    SetWorkflowContextSuccess,
)
from griptape_nodes.retained_mode.events.workflow_events import RunWorkflowFromRegistryRequest
from griptape_nodes.retained_mode.managers.workflow_manager import ImportRecorder, WorkflowManager


def _register_workflow(workspace: Path, key: str) -> None:
    """Register `key` against a stub file in `workspace`, the way a saved workflow appears."""
    (workspace / f"{key}.py").write_text("# stub")
    metadata = WorkflowMetadata(
        name=key,
        schema_version=WorkflowMetadata.LATEST_SCHEMA_VERSION,
        engine_version_created_with="test",
        node_libraries_referenced=[],
        creation_date=datetime.now(UTC),
    )
    WorkflowRegistry.generate_new_workflow(registry_key=key, metadata=metadata, file_path=f"{key}.py")


def _drain(context_manager) -> None:  # noqa: ANN001
    while context_manager.has_current_workflow():
        context_manager.pop_workflow()


class TestRunFromRegistryReplacesContext:
    """`RunWorkflowFromRegistry` must replace the current workflow, not stack onto it.

    Every one of these passes `run_with_clean_slate=False` explicitly. The request defaults
    the flag to True, and a clean slate drains the whole context stack up front, which hides
    the push/pop imbalance below. The editor happens to take the defaulted path today, so
    this is a latent API-reachable defect rather than the one reported in #406 -- but the
    handler is the single place the "current workflow" is established, and it should not
    depend on a caller-supplied flag to avoid corrupting its own stack.
    """

    @pytest.mark.asyncio
    async def test_repeated_runs_do_not_grow_the_workflow_stack(self, griptape_nodes: Engine, tmp_path: Path) -> None:
        """Opening workflows back-to-back replaces the context rather than burying it.

        The handler already warns "Replacing the old with the new", but only ever pushes.
        Each open therefore buries the previous entry, and any later single pop resurfaces
        a workflow the user closed long ago.
        """
        workflow_manager = griptape_nodes.WorkflowManager()
        context_manager = griptape_nodes.ContextManager()
        workflow_manager._workflows_loading_complete.set()
        config_manager = griptape_nodes.ConfigManager()

        original_workspace = config_manager.workspace_path
        config_manager.workspace_path = tmp_path
        try:
            with patch.dict(WorkflowRegistry._workflows, {}, clear=True):
                _register_workflow(tmp_path, "first")
                _register_workflow(tmp_path, "second")

                async def fake_run_workflow(relative_file_path: str) -> WorkflowManager.WorkflowExecutionResult:
                    # A real workflow file finds a context already pushed by the caller, so
                    # its own guarded push is a no-op. Model that: leave the stack alone.
                    return WorkflowManager.WorkflowExecutionResult(
                        execution_successful=True,
                        execution_details=f"ran {relative_file_path}",
                    )

                with patch.object(workflow_manager, "run_workflow", fake_run_workflow):
                    for key in ("first", "second"):
                        result = await workflow_manager.on_run_workflow_from_registry_request(
                            RunWorkflowFromRegistryRequest(workflow_name=key, run_with_clean_slate=False)
                        )
                        assert result.succeeded(), f"opening '{key}' failed: {result.result_details}"

                assert context_manager.get_current_workflow_name() == "second"
                assert len(context_manager._workflow_stack) == 1, (
                    "opening a second workflow must retire the first, not bury it"
                )
        finally:
            config_manager.workspace_path = original_workspace
            _drain(context_manager)

    @pytest.mark.asyncio
    async def test_failed_run_does_not_leave_its_workflow_in_context(
        self, griptape_nodes: Engine, tmp_path: Path
    ) -> None:
        """A workflow that fails to run must not stay in the Current Context.

        The handler pushes optimistically and relies on a follow-up ClearAllObjectState to
        undo it, but ignores whether that clear actually succeeded.
        """
        workflow_manager = griptape_nodes.WorkflowManager()
        context_manager = griptape_nodes.ContextManager()
        workflow_manager._workflows_loading_complete.set()
        config_manager = griptape_nodes.ConfigManager()

        original_workspace = config_manager.workspace_path
        config_manager.workspace_path = tmp_path
        try:
            with patch.dict(WorkflowRegistry._workflows, {}, clear=True):
                _register_workflow(tmp_path, "broken")

                async def failing_run_workflow(relative_file_path: str) -> WorkflowManager.WorkflowExecutionResult:
                    return WorkflowManager.WorkflowExecutionResult(
                        execution_successful=False,
                        execution_details=f"boom on {relative_file_path}",
                    )

                with patch.object(workflow_manager, "run_workflow", failing_run_workflow):
                    result = await workflow_manager.on_run_workflow_from_registry_request(
                        RunWorkflowFromRegistryRequest(workflow_name="broken", run_with_clean_slate=False)
                    )

                assert not result.succeeded()
                assert not context_manager.has_current_workflow(), (
                    "a workflow that failed to open must not remain the Current Context"
                )
        finally:
            config_manager.workspace_path = original_workspace
            _drain(context_manager)


class TestGetWorkflowContextRegistryBacking:
    """`GetWorkflowContext` must never report a name the registry cannot back."""

    def test_reports_none_when_the_context_key_is_not_registered(self, griptape_nodes: Engine) -> None:
        """A registry-orphaned stack entry is a phantom and must not be reported as open.

        The editor treats any non-empty `workflow_name` as "the engine has this open" and
        adopts it. Reporting a key with no registry entry hands the editor a workflow that
        cannot be loaded, displayed, or saved.
        """
        context_manager = griptape_nodes.ContextManager()
        _drain(context_manager)

        with patch.dict(WorkflowRegistry._workflows, {}, clear=True):
            context_manager.push_workflow(workflow_name="ghost_workflow")
            try:
                result = context_manager.on_get_workflow_context_request(GetWorkflowContextRequest())

                assert isinstance(result, GetWorkflowContextSuccess)
                assert result.workflow_name is None, (
                    "an unregistered context key must be reported as 'no workflow open'"
                )
            finally:
                _drain(context_manager)

    def test_reports_a_brand_new_unsaved_workflow(self, griptape_nodes: Engine) -> None:
        """Guard for the check above: a never-saved workflow is still registry-backed.

        `SetWorkflowContext` auto-registers `unsaved:<uuid>` keys, so requiring registry
        backing must not regress the blank-canvas flow.
        """
        context_manager = griptape_nodes.ContextManager()
        _drain(context_manager)

        try:
            set_result = context_manager.on_set_workflow_context_request(SetWorkflowContextRequest())
            assert isinstance(set_result, SetWorkflowContextSuccess)
            assert set_result.workflow_name.startswith(WorkflowRegistry.UNSAVED_KEY_PREFIX)

            get_result = context_manager.on_get_workflow_context_request(GetWorkflowContextRequest())

            assert isinstance(get_result, GetWorkflowContextSuccess)
            assert get_result.workflow_name == set_result.workflow_name
        finally:
            _drain(context_manager)


class TestRunWorkflowUnwindsOnFailure:
    """A workflow file that raises must not leave its own context behind."""

    @pytest.mark.asyncio
    async def test_failed_run_unwinds_contexts_the_workflow_file_pushed(
        self, griptape_nodes: Engine, tmp_path: Path
    ) -> None:
        """Generated workflows push their own context; an exception skips the matching pop.

        The push is emitted at the top of every serialized workflow, so anything that raises
        while building the graph strands it. `run_workflow` swallows the exception into a
        failure result, and the stranded entry then answers as the open workflow.
        """
        workflow_manager = griptape_nodes.WorkflowManager()
        context_manager = griptape_nodes.ContextManager()
        config_manager = griptape_nodes.ConfigManager()
        _drain(context_manager)

        workflow_file = tmp_path / "explodes.py"
        workflow_file.write_text(
            "from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes\n"
            "context_manager = GriptapeNodes.ContextManager()\n"
            "if not context_manager.has_current_workflow():\n"
            "    context_manager.push_workflow(file_path=__file__)\n"
            "raise RuntimeError('workflow blew up mid-build')\n"
        )

        original_workspace = config_manager.workspace_path
        config_manager.workspace_path = tmp_path
        try:
            # Library resolution is not what is under test; the file declares no header.
            async def no_library_work(**_kwargs: object) -> None:
                return None

            with patch.object(workflow_manager, "_ensure_libraries_for_workflow", no_library_work):
                result = await workflow_manager.run_workflow(relative_file_path="explodes.py")

            assert not result.execution_successful
            assert not context_manager.has_current_workflow(), (
                "a workflow that raised must not leave its context entered"
            )
        finally:
            config_manager.workspace_path = original_workspace
            _drain(context_manager)


class TestReconcileWithRegistry:
    """The context stack must not outlive the registry it was keyed against."""

    def test_reconcile_drops_orphaned_contexts_and_keeps_backed_ones(
        self, griptape_nodes: Engine, tmp_path: Path
    ) -> None:
        """Rebuilding the registry under a live context strands keys derived against the old one."""
        context_manager = griptape_nodes.ContextManager()
        config_manager = griptape_nodes.ConfigManager()
        _drain(context_manager)

        original_workspace = config_manager.workspace_path
        config_manager.workspace_path = tmp_path
        try:
            with patch.dict(WorkflowRegistry._workflows, {}, clear=True):
                _register_workflow(tmp_path, "still_here")
                context_manager.push_workflow(workflow_name="went_away")
                context_manager.push_workflow(workflow_name="still_here")

                discarded = context_manager.reconcile_with_registry()

                assert discarded == ["went_away"]
                assert len(context_manager._workflow_stack) == 1
                assert context_manager.get_current_workflow_name() == "still_here"
        finally:
            config_manager.workspace_path = original_workspace
            _drain(context_manager)


class TestForeignContextIsNotSilent:
    """A workflow file must not quietly run under a context that is not its own."""

    def test_generated_code_reports_a_foreign_context_instead_of_adopting_it(self, griptape_nodes: Engine) -> None:
        """The emitted push is guarded; the guarded-out branch must say something."""
        workflow_manager = griptape_nodes.WorkflowManager()
        code_blocks = workflow_manager._generate_workflow_run_prerequisite_code(
            import_recorder=ImportRecorder(),
            library_names=[],
        )
        module = ast.Module(body=[n for n in code_blocks if isinstance(n, ast.stmt)], type_ignores=[])
        ast.fix_missing_locations(module)
        source = ast.unparse(module)
        assert "push_workflow(file_path=__file__)" in source
        assert "warn_if_foreign_workflow_context(file_path=__file__)" in source

    def test_warn_returns_the_foreign_name_and_stays_quiet_when_it_matches(
        self, griptape_nodes: Engine, tmp_path: Path
    ) -> None:
        context_manager = griptape_nodes.ContextManager()
        config_manager = griptape_nodes.ConfigManager()
        _drain(context_manager)

        original_workspace = config_manager.workspace_path
        config_manager.workspace_path = tmp_path
        try:
            own_file = tmp_path / "mine.py"
            own_file.write_text("# stub")

            # Nothing entered: the file will push for itself, so there is nothing to report.
            assert context_manager.warn_if_foreign_workflow_context(str(own_file)) is None

            # Its own context entered: still nothing to report.
            context_manager.push_workflow(file_path=str(own_file))
            assert context_manager.warn_if_foreign_workflow_context(str(own_file)) is None
            _drain(context_manager)

            # Somebody else's context entered: this is the #406 shape.
            context_manager.push_workflow(workflow_name="Flix.2")
            assert context_manager.warn_if_foreign_workflow_context(str(own_file)) == "Flix.2"
        finally:
            config_manager.workspace_path = original_workspace
            _drain(context_manager)
