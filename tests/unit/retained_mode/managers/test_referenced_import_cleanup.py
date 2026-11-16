"""Tests for the cleanup of flows left behind by a failed referenced workflow import."""

import asyncio
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from contextvars import ContextVar
from unittest.mock import Mock

import pytest

from griptape_nodes.exe_types.flow import ControlFlow
from griptape_nodes.node_library.workflow_registry import UnknownWorkflowAddressError, Workflow, _WorkflowRegistry
from griptape_nodes.retained_mode.engine import Engine
from griptape_nodes.retained_mode.events.flow_events import (
    DeleteFlowRequest,
    DeleteFlowResultFailure,
    DeleteFlowResultSuccess,
    SetFlowMetadataResultFailure,
    SetFlowMetadataResultSuccess,
)
from griptape_nodes.retained_mode.events.workflow_events import (
    ImportWorkflowAsReferencedSubFlowRequest,
    ImportWorkflowAsReferencedSubFlowResultFailure,
    ImportWorkflowAsReferencedSubFlowResultSuccess,
    WorkflowStatus,
)
from griptape_nodes.retained_mode.managers.config_manager import ConfigManager
from griptape_nodes.retained_mode.managers.event_manager import EventManager
from griptape_nodes.retained_mode.managers.flow_manager import FlowManager
from griptape_nodes.retained_mode.managers.workflow.referenced_import import ReferencedWorkflowImport
from griptape_nodes.retained_mode.managers.workflow.running import WorkflowExecutionResult, WorkflowRunner
from griptape_nodes.retained_mode.managers.workflow_manager import WorkflowManager
from griptape_nodes.retained_mode.runtime.context import WorkflowContextStack
from griptape_nodes.retained_mode.runtime.objects import WorkflowObjects
from griptape_nodes.retained_mode.runtime.workflow_runtime import WorkflowRuntime

PARENT = "Parent"
OUTER = "Outer"
INNER = "Inner"
GROUP_BODY = "Group_Body"
NESTED = "Nested"
WORKFLOW_NAME = "wf"


class BoomError(Exception):
    """Raised by the fake workflow file."""


class TestExecuteWorkflowImportCleanup:
    """`_execute_workflow_import` removes the flows a failed import created."""

    _active_record: list[str]
    _nested_by_run: list[str]

    @pytest.fixture
    def parents(self) -> dict[str, str | None]:
        """Flow name to parent flow name; the flows that currently exist."""
        return {PARENT: None}

    @pytest.fixture
    def created_by_run(self) -> list[str]:
        """Flows the fake workflow file creates."""
        return [OUTER, INNER]

    @pytest.fixture
    def flow_manager(self) -> Mock:
        """A FlowManager mock whose `task_local_flow_creation_record` is the real one."""
        flow_manager = Mock(spec=FlowManager)
        record_owner = Mock(spec=FlowManager)
        record_owner._task_local_flow_creation_record = ContextVar("flow_creation_record", default=None)

        def task_local_flow_creation_record() -> AbstractContextManager[list[str]]:
            return FlowManager.task_local_flow_creation_record(record_owner)

        flow_manager.task_local_flow_creation_record.side_effect = task_local_flow_creation_record
        flow_manager.get_parent_flow_in.side_effect = self._get_parent_flow
        flow_manager.get_flow_by_name_in.side_effect = lambda _workflow, _name: Mock(spec=ControlFlow)
        return flow_manager

    @pytest.fixture
    def runner(self) -> Mock:
        return Mock(spec=WorkflowRunner)

    @pytest.fixture
    def engine(self, flow_manager: Mock, runner: Mock) -> Mock:
        engine = Mock(spec=Engine)
        engine.flow_manager = flow_manager
        engine.config_manager = Mock(spec=ConfigManager)
        engine.workflow_manager = Mock(spec=WorkflowManager)
        engine.workflow_manager.runner = runner
        engine.workflow_manager.referenced_workflow.side_effect = lambda _name: nullcontext()
        return engine

    @pytest.fixture
    def deleted(self) -> list[str]:
        return []

    @pytest.fixture
    def real_importer(
        self,
        monkeypatch: pytest.MonkeyPatch,
        engine: Mock,
        parents: dict[str, str | None],
        created_by_run: list[str],
        deleted: list[str],
    ) -> ReferencedWorkflowImport:
        """An importer with its own result formatting and flow selection, run as a load would."""
        importer = ReferencedWorkflowImport(Mock(spec=EventManager), engine=engine)
        self._deleted = deleted
        self._parents = parents
        self._delete_fails: set[str] = set()
        self._metadata_ok = True
        self._nested_by_run = []
        monkeypatch.setattr(
            "griptape_nodes.retained_mode.managers.workflow.referenced_import.is_loading_workflow", lambda: True
        )
        self._created_by_run = created_by_run
        self._flow_manager = engine.flow_manager
        engine.workflow_manager.runner.run_workflow.side_effect = self._run_succeeds
        return importer

    @pytest.fixture
    def importer(
        self, monkeypatch: pytest.MonkeyPatch, real_importer: ReferencedWorkflowImport
    ) -> ReferencedWorkflowImport:
        """The importer with result formatting and flow selection stubbed out."""
        importer = real_importer
        monkeypatch.setattr(importer, "_import_result_details", lambda _result, message, *, level: message)  # noqa: ARG005
        monkeypatch.setattr(
            importer,
            "_select_top_level_imported_flow",
            Mock(spec=importer._select_top_level_imported_flow, return_value=OUTER),
        )
        return importer

    @pytest.fixture
    def request_payload(self) -> ImportWorkflowAsReferencedSubFlowRequest:
        return ImportWorkflowAsReferencedSubFlowRequest(
            workflow_name=WORKFLOW_NAME, flow_name=PARENT, imported_flow_metadata={"transient": True}
        )

    @pytest.fixture
    def workflow(self, parents: dict[str, str | None]) -> Mock:
        """The open workflow the import runs in: its objects hold the flows in `parents`.

        Also sets `self._imported`, the saved workflow the import runs.
        """
        self._imported = Mock(spec=Workflow)
        self._imported.file_path = "/tmp/wf.py"  # noqa: S108
        self._imported.name = WORKFLOW_NAME
        workflow = Mock(spec=WorkflowRuntime)
        workflow.objects = Mock(spec=WorkflowObjects)
        workflow.objects.get_as_type.side_effect = lambda name, _type: (
            Mock(spec=ControlFlow) if name in parents else None
        )
        workflow.context = Mock(spec=WorkflowContextStack)
        workflow.context.task_local_flow_stack.side_effect = nullcontext
        workflow.context.flow.side_effect = lambda *_args, **_kwargs: nullcontext()
        workflow.handle_request.side_effect = self._handle_request
        return workflow

    def _handle_request(self, request: object) -> object:
        if isinstance(request, DeleteFlowRequest):
            assert request.flow_name is not None
            if request.flow_name in self._delete_fails:
                return DeleteFlowResultFailure(result_details="no")
            self._deleted.append(request.flow_name)
            self._remove_with_children(request.flow_name)
            return DeleteFlowResultSuccess(result_details="ok")
        if self._metadata_ok:
            return SetFlowMetadataResultSuccess(result_details="ok")
        return SetFlowMetadataResultFailure(result_details="no")

    def _get_parent_flow(self, _workflow: object, name: str) -> str | None:
        if name not in self._parents:
            msg = f"Flow with name {name} doesn't exist"
            raise ValueError(msg)
        return self._parents[name]

    def _remove_with_children(self, name: str) -> None:
        for child in [c for c, p in self._parents.items() if p == name]:
            self._remove_with_children(child)
        self._parents.pop(name, None)

    def _create_flows(self) -> None:
        # `_active_record` is the record the fake FlowManager handed to the import in progress.
        record = self._active_record
        for name in self._created_by_run:
            self._parents[name] = self._parent_for(name)
            record.append(name)
        # A nested import records into its own list, which never reaches ours.
        # Real nested flows sit inside a flow the file created. These sit under PARENT instead, on purpose:
        # only the success path reads them, and a leaked name must compete with OUTER to be caught.
        for name in self._nested_by_run:
            with self._flow_manager.task_local_flow_creation_record() as nested_record:
                self._parents[name] = PARENT
                nested_record.append(name)
        self._active_record = record

    @staticmethod
    def _parent_for(name: str) -> str:
        if name == OUTER:
            return PARENT
        if name == INNER:
            return OUTER
        return INNER

    async def _run_succeeds(self, _path: str, _workflow: WorkflowRuntime | None) -> WorkflowExecutionResult:
        self._create_flows()
        return WorkflowExecutionResult(
            execution_successful=True, execution_details="ok", status=WorkflowStatus.GOOD, problems=()
        )

    async def _run_fails(self, _path: str, _workflow: WorkflowRuntime | None) -> WorkflowExecutionResult:
        self._create_flows()
        return WorkflowExecutionResult(
            execution_successful=False, execution_details="bad", status=WorkflowStatus.UNUSABLE, problems=()
        )

    async def _run_loses_a_flow(self, _path: str, _workflow: WorkflowRuntime | None) -> WorkflowExecutionResult:
        self._create_flows()
        # Something other than the import deletes a flow it recorded before the import returns.
        self._parents.pop(INNER)
        return WorkflowExecutionResult(
            execution_successful=True, execution_details="ok", status=WorkflowStatus.GOOD, problems=()
        )

    async def _run_raises(self, _path: str, _workflow: WorkflowRuntime | None) -> WorkflowExecutionResult:
        self._create_flows()
        raise BoomError

    async def _run_cancelled(self, _path: str, _workflow: WorkflowRuntime | None) -> WorkflowExecutionResult:
        self._create_flows()
        raise asyncio.CancelledError

    @pytest.fixture(autouse=True)
    def _track_active_record(self, flow_manager: Mock) -> None:
        original = flow_manager.task_local_flow_creation_record.side_effect

        @contextmanager
        def wrapper() -> Iterator[list[str]]:
            with original() as created:
                self._active_record = created
                yield created

        flow_manager.task_local_flow_creation_record.side_effect = wrapper

    @pytest.mark.asyncio
    async def test_failed_import_deletes_created_flows(
        self,
        importer: ReferencedWorkflowImport,
        runner: Mock,
        request_payload: ImportWorkflowAsReferencedSubFlowRequest,
        workflow: Mock,
        deleted: list[str],
    ) -> None:
        runner.run_workflow.side_effect = self._run_fails

        result = await importer._execute_workflow_import(workflow, request_payload, self._imported, PARENT)

        assert isinstance(result, ImportWorkflowAsReferencedSubFlowResultFailure)
        assert deleted == [OUTER]

    @pytest.mark.asyncio
    async def test_metadata_failure_deletes_created_flows(
        self,
        importer: ReferencedWorkflowImport,
        request_payload: ImportWorkflowAsReferencedSubFlowRequest,
        workflow: Mock,
        deleted: list[str],
    ) -> None:
        self._metadata_ok = False

        result = await importer._execute_workflow_import(workflow, request_payload, self._imported, PARENT)

        assert isinstance(result, ImportWorkflowAsReferencedSubFlowResultFailure)
        assert deleted == [OUTER]

    @pytest.mark.asyncio
    async def test_exception_from_run_workflow_deletes_created_flows_and_reraises(
        self,
        importer: ReferencedWorkflowImport,
        runner: Mock,
        request_payload: ImportWorkflowAsReferencedSubFlowRequest,
        workflow: Mock,
        deleted: list[str],
    ) -> None:
        runner.run_workflow.side_effect = self._run_raises

        with pytest.raises(BoomError):
            await importer._execute_workflow_import(workflow, request_payload, self._imported, PARENT)

        assert deleted == [OUTER]

    @pytest.mark.asyncio
    async def test_a_created_flow_removed_before_selection_fails_the_import_and_deletes_created_flows(
        self,
        real_importer: ReferencedWorkflowImport,
        runner: Mock,
        request_payload: ImportWorkflowAsReferencedSubFlowRequest,
        workflow: Mock,
        deleted: list[str],
    ) -> None:
        runner.run_workflow.side_effect = self._run_loses_a_flow

        result = await real_importer._execute_workflow_import(workflow, request_payload, self._imported, PARENT)

        assert isinstance(result, ImportWorkflowAsReferencedSubFlowResultFailure)
        assert str(result.result_details) == (
            "Attempted to import workflow 'wf' as referenced sub flow. Failed because a flow it created "
            "was removed before the import finished: Flow with name Inner doesn't exist"
        )
        assert deleted == [OUTER]

    @pytest.mark.asyncio
    async def test_cancellation_deletes_created_flows_and_reraises(
        self,
        importer: ReferencedWorkflowImport,
        runner: Mock,
        request_payload: ImportWorkflowAsReferencedSubFlowRequest,
        workflow: Mock,
        deleted: list[str],
    ) -> None:
        runner.run_workflow.side_effect = self._run_cancelled

        with pytest.raises(asyncio.CancelledError):
            await importer._execute_workflow_import(workflow, request_payload, self._imported, PARENT)

        assert deleted == [OUTER]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("created_by_run", [[OUTER, INNER, GROUP_BODY]])
    async def test_cleanup_deletes_roots_only(
        self,
        importer: ReferencedWorkflowImport,
        runner: Mock,
        request_payload: ImportWorkflowAsReferencedSubFlowRequest,
        workflow: Mock,
        deleted: list[str],
    ) -> None:
        runner.run_workflow.side_effect = self._run_fails

        await importer._execute_workflow_import(workflow, request_payload, self._imported, PARENT)

        assert deleted == [OUTER]
        assert self._parents == {PARENT: None}

    @pytest.mark.asyncio
    async def test_delete_failure_during_cleanup_still_returns_original_failure(
        self,
        importer: ReferencedWorkflowImport,
        runner: Mock,
        request_payload: ImportWorkflowAsReferencedSubFlowRequest,
        workflow: Mock,
    ) -> None:
        runner.run_workflow.side_effect = self._run_fails
        self._delete_fails = {OUTER}

        result = await importer._execute_workflow_import(workflow, request_payload, self._imported, PARENT)

        assert isinstance(result, ImportWorkflowAsReferencedSubFlowResultFailure)
        assert "workflow execution failed: bad" in str(result.result_details)
        assert "could not be removed" in str(result.result_details)

    @pytest.mark.asyncio
    async def test_leftover_note_names_the_flows_and_survives_result_formatting(
        self,
        real_importer: ReferencedWorkflowImport,
        runner: Mock,
        request_payload: ImportWorkflowAsReferencedSubFlowRequest,
        workflow: Mock,
    ) -> None:
        runner.run_workflow.side_effect = self._run_fails
        self._delete_fails = {OUTER}

        result = await real_importer._execute_workflow_import(workflow, request_payload, self._imported, PARENT)

        assert isinstance(result, ImportWorkflowAsReferencedSubFlowResultFailure)
        details = str(result.result_details)
        assert "workflow execution failed: bad" in details
        assert "A flow the import had already built could not be removed: 'Outer'. Delete it by hand." in details

    @pytest.mark.asyncio
    @pytest.mark.parametrize("created_by_run", [[OUTER, GROUP_BODY]])
    async def test_failed_import_names_every_flow_it_could_not_remove(
        self,
        real_importer: ReferencedWorkflowImport,
        runner: Mock,
        request_payload: ImportWorkflowAsReferencedSubFlowRequest,
        workflow: Mock,
    ) -> None:
        runner.run_workflow.side_effect = self._run_fails
        self._delete_fails = {OUTER, GROUP_BODY}

        result = await real_importer._execute_workflow_import(workflow, request_payload, self._imported, PARENT)

        assert isinstance(result, ImportWorkflowAsReferencedSubFlowResultFailure)
        details = str(result.result_details)
        assert "Flows the import had already built could not be removed: 'Outer', 'Group_Body'." in details
        assert "Delete them by hand." in details
        assert "A flow the import had already built" not in details
        assert "Delete it by hand." not in details

    @pytest.mark.asyncio
    async def test_success_picks_its_own_flow_not_a_nested_imports(
        self,
        real_importer: ReferencedWorkflowImport,
        request_payload: ImportWorkflowAsReferencedSubFlowRequest,
        workflow: Mock,
        deleted: list[str],
    ) -> None:
        # "Nested" sorts before "Outer", so a `min()` over both flows would pick the wrong one.
        self._nested_by_run = [NESTED]

        result = await real_importer._execute_workflow_import(workflow, request_payload, self._imported, PARENT)

        assert isinstance(result, ImportWorkflowAsReferencedSubFlowResultSuccess)
        assert result.created_flow_name == OUTER
        assert deleted == []

    @pytest.mark.asyncio
    async def test_validation_failure_creates_and_deletes_nothing(
        self,
        importer: ReferencedWorkflowImport,
        runner: Mock,
        workflow: Mock,
        request_payload: ImportWorkflowAsReferencedSubFlowRequest,
        deleted: list[str],
    ) -> None:
        workflow_registry = Mock(spec=_WorkflowRegistry)
        workflow_registry.resolve_address.side_effect = UnknownWorkflowAddressError(WORKFLOW_NAME)
        importer.engine.workflow_registry = workflow_registry  # type: ignore[attr-defined]

        result = await importer.on_import_workflow_as_referenced_sub_flow_request(request_payload, workflow)

        assert isinstance(result, ImportWorkflowAsReferencedSubFlowResultFailure)
        runner.run_workflow.assert_not_called()
        assert deleted == []

    @pytest.mark.asyncio
    async def test_successful_import_deletes_nothing(
        self,
        importer: ReferencedWorkflowImport,
        request_payload: ImportWorkflowAsReferencedSubFlowRequest,
        workflow: Mock,
        deleted: list[str],
    ) -> None:
        result = await importer._execute_workflow_import(workflow, request_payload, self._imported, PARENT)

        assert isinstance(result, ImportWorkflowAsReferencedSubFlowResultSuccess)
        assert deleted == []
