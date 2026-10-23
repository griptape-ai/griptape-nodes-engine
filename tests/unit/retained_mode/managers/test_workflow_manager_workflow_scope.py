"""Workflow managers' handlers act on the workflow dispatch hands them, or on the saved workflow they name.

The envelope id names the tab's open workflow; a body `workflow_id` names the saved workflow a request
acts on. Several tests open a second workflow beside the first, so a handler that fell back to "the
only open workflow" would fail instead of passing by luck.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest

from griptape_nodes.common.project_templates.default_project_template import DEFAULT_PROJECT_TEMPLATE
from griptape_nodes.node_library.workflow_registry import WorkflowMetadata, WorkflowShape, new_workflow_id
from griptape_nodes.retained_mode.events.context_events import (
    EnsureWorkflowAndFlowRequest,
    EnsureWorkflowAndFlowResultFailure,
    EnsureWorkflowAndFlowResultSuccess,
)
from griptape_nodes.retained_mode.events.flow_events import (
    CreateFlowRequest,
    CreateFlowResultSuccess,
    GetTopLevelFlowRequest,
    GetTopLevelFlowResultSuccess,
    ImportWorkflowAsReferencedSubFlowRequest,
    SerializedFlowCommands,
    SerializeFlowToCommandsRequest,
    SerializeFlowToCommandsResultSuccess,
)
from griptape_nodes.retained_mode.events.object_events import ClearAllObjectStateRequest
from griptape_nodes.retained_mode.events.project_events import (
    LoadProjectTemplateRequest,
    LoadProjectTemplateResultSuccess,
    SetCurrentProjectRequest,
)
from griptape_nodes.retained_mode.events.workflow_events import (
    DeleteWorkflowRequest,
    DeleteWorkflowResultSuccess,
    GetVariableSubstitutionEnabledRequest,
    GetWorkflowRunCommandRequest,
    GetWorkflowRunCommandResultFailure,
    GetWorkflowRunCommandResultSuccess,
    ImportWorkflowAsReferencedSubFlowResultFailure,
    ImportWorkflowAsReferencedSubFlowResultSuccess,
    PublishWorkflowRegisteredEventData,
    PublishWorkflowRequest,
    PublishWorkflowResultSuccess,
    RenameWorkflowRequest,
    RenameWorkflowResultSuccess,
    SaveSubflowToWorkflowRequest,
    SaveSubflowToWorkflowResultSuccess,
    SaveWorkflowFileFromSerializedFlowResultSuccess,
    SaveWorkflowRequest,
    SaveWorkflowResultFailure,
    SaveWorkflowResultSuccess,
    SetVariableSubstitutionEnabledRequest,
)
from griptape_nodes.retained_mode.managers.context_manager import ContextManager
from griptape_nodes.retained_mode.managers.library.common import RegisteredEventHandler
from griptape_nodes.retained_mode.managers.workflow.branching import WorkflowBranching
from griptape_nodes.retained_mode.managers.workflow.catalog import WorkflowCatalog
from griptape_nodes.retained_mode.managers.workflow.file_operations import WorkflowFileOperations
from griptape_nodes.retained_mode.managers.workflow.publishing import WorkflowPublishing
from griptape_nodes.retained_mode.managers.workflow.referenced_import import ReferencedWorkflowImport
from griptape_nodes.retained_mode.managers.workflow.running import WorkflowExecutionResult, WorkflowRunner
from griptape_nodes.retained_mode.managers.workflow.saving import WorkflowSaver
from griptape_nodes.retained_mode.managers.workflow.variable_substitution import VariableSubstitution
from griptape_nodes.retained_mode.managers.workflow_manager import WorkflowManager
from griptape_nodes.retained_mode.request_handlers import RequestScope, handled_request_scope, handled_request_types
from griptape_nodes.retained_mode.runtime.workflow_runtime import WorkflowRuntime
from tests.workflow_context import push_unsaved_workflow

if TYPE_CHECKING:
    from collections.abc import Iterator

    from griptape_nodes.node_library.workflow_registry import Workflow
    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.events.base_events import RequestPayload, ResultPayload

_HANDLER_CLASSES = (
    ContextManager,
    WorkflowManager,
    WorkflowBranching,
    WorkflowCatalog,
    WorkflowFileOperations,
    WorkflowPublishing,
    ReferencedWorkflowImport,
    WorkflowRunner,
    WorkflowSaver,
    VariableSubstitution,
)
# They act on the tab's open workflow: they read or change its graph.
_WORKFLOW_REQUESTS = {
    SaveWorkflowRequest,
    SaveSubflowToWorkflowRequest,
    ImportWorkflowAsReferencedSubFlowRequest,
    GetVariableSubstitutionEnabledRequest,
    SetVariableSubstitutionEnabledRequest,
}
# They use the tab's open workflow when there is one and work without it.
_OPTIONAL_WORKFLOW_REQUESTS = {EnsureWorkflowAndFlowRequest, GetWorkflowRunCommandRequest}


@contextmanager
def _also_open(engine: Engine, display_name: str = "beside") -> Iterator[WorkflowRuntime]:
    """Open a registered unsaved workflow beside the open one, so nothing can fall back to a sole open workflow."""
    entry = engine.workflow_registry.register_unsaved(display_name)
    workflow = WorkflowRuntime(entry.workflow_id, engine)
    with patch.dict(engine.open_workflows._by_id, {workflow.workflow_id: workflow}):
        yield workflow


def _create_flow(engine: Engine, workflow: WorkflowRuntime, flow_name: str) -> str:
    result = engine.flow_manager.on_create_flow_request(
        CreateFlowRequest(parent_flow_name=None, flow_name=flow_name, set_as_new_context=False), workflow
    )
    assert isinstance(result, CreateFlowResultSuccess), result.result_details
    return result.flow_name


def _write_saved_workflow(engine: Engine, path: Path, *, workflow_shape: WorkflowShape | None = None) -> Workflow:
    """Register a saved workflow whose file holds only a placeholder body."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# a saved workflow\n", encoding="utf-8")
    metadata = WorkflowMetadata(
        workflow_id=new_workflow_id(),
        name=path.stem,
        schema_version=WorkflowMetadata.LATEST_SCHEMA_VERSION,
        engine_version_created_with="0.0.0",
        node_libraries_referenced=[],
        workflow_shape=workflow_shape,
    )
    return engine.workflow_registry.register_file(path, metadata)


class _RecordedRequests:
    """Stands in for `engine.ahandle_request`, answering flow requests and recording the workflow each names."""

    def __init__(self, flow_name: str) -> None:
        self.flow_name = flow_name
        self.workflow_ids: list[tuple[type, str | None]] = []

    async def __call__(self, request: RequestPayload, *, workflow_id: str | None = None) -> ResultPayload:
        self.workflow_ids.append((type(request), workflow_id))
        if isinstance(request, GetTopLevelFlowRequest):
            return GetTopLevelFlowResultSuccess(flow_name=self.flow_name, result_details="ok")
        if isinstance(request, SerializeFlowToCommandsRequest):
            commands = SerializedFlowCommands(
                flow_initialization_command=None,
                serialized_node_commands=[],
                serialized_connections=[],
                unique_parameter_uuid_to_values={},
                set_parameter_value_commands={},
                set_lock_commands_per_node={},
                sub_flows_commands=[],
                node_dependencies=MagicMock(),
                node_types_used=set(),
            )
            return SerializeFlowToCommandsResultSuccess(serialized_flow_commands=commands, result_details="ok")
        msg = f"Unexpected request in test: {type(request).__name__}"
        raise AssertionError(msg)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """An empty workspace folder, links resolved so paths compare as the registry stores them."""
    workspace = tmp_path.resolve() / "workspace"
    workspace.mkdir()
    return workspace


@pytest.fixture
def project_engine(engine: Engine, workspace: Path) -> Iterator[Engine]:
    """The engine on the default project, with `workspace` as its workspace, so saves write real files."""
    project_yml = workspace / "project_template.yml"
    project_yml.write_text(DEFAULT_PROJECT_TEMPLATE.to_overlay_yaml(DEFAULT_PROJECT_TEMPLATE))
    load_result = engine.handle_request(LoadProjectTemplateRequest(project_path=project_yml))
    assert isinstance(load_result, LoadProjectTemplateResultSuccess)
    engine.handle_request(SetCurrentProjectRequest(project_id=load_result.project_id))
    engine.config_manager.workspace_path = workspace
    yield engine
    engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
    engine.handle_request(SetCurrentProjectRequest(project_id=None))


def _open_and_save(engine: Engine, display_name: str) -> SaveWorkflowResultSuccess:
    ensured = engine.handle_request(EnsureWorkflowAndFlowRequest(display_name=display_name))
    assert isinstance(ensured, EnsureWorkflowAndFlowResultSuccess), ensured.result_details
    saved = asyncio.run(engine.ahandle_request(SaveWorkflowRequest(), workflow_id=ensured.workflow_id))
    assert isinstance(saved, SaveWorkflowResultSuccess), saved.result_details
    return saved


class TestWorkflowManagerScopes:
    def test_every_handler_declares_its_scope(self) -> None:
        scopes = {
            request_type: handled_request_scope(attr)
            for handler_class in _HANDLER_CLASSES
            for attr in vars(handler_class).values()
            for request_type in handled_request_types(attr)
        }

        assert len(scopes) > len(_WORKFLOW_REQUESTS) + len(_OPTIONAL_WORKFLOW_REQUESTS)
        for request_type, scope in scopes.items():
            expected = RequestScope.HOST
            if request_type in _WORKFLOW_REQUESTS:
                expected = RequestScope.WORKFLOW
            if request_type in _OPTIONAL_WORKFLOW_REQUESTS:
                expected = RequestScope.OPTIONAL_WORKFLOW
            assert scope is expected, f"{request_type.__name__} is {scope}, expected {expected}"


class TestSavedWorkflowRequestsActOnTheWorkflowTheyName:
    """Sent from a tab whose open workflow differs from its target, a host request acts on the target."""

    def test_renaming_a_workflow_that_is_not_open_renames_its_file_and_leaves_the_tab_alone(
        self, project_engine: Engine
    ) -> None:
        other = _open_and_save(project_engine, "other")
        project_engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
        tab = _open_and_save(project_engine, "tab")
        tab_content = Path(tab.file_path).read_text(encoding="utf-8")

        renamed = asyncio.run(
            project_engine.ahandle_request(
                RenameWorkflowRequest(workflow_id=other.workflow_id, requested_name="renamed"),
                workflow_id=tab.workflow_id,
            )
        )

        assert isinstance(renamed, RenameWorkflowResultSuccess), renamed.result_details
        assert renamed.workflow_id == other.workflow_id
        assert not Path(other.file_path).exists()
        assert Path(other.file_path).with_name("renamed.py").exists()
        assert project_engine.open_workflows.is_open(tab.workflow_id or "")
        assert Path(tab.file_path).read_text(encoding="utf-8") == tab_content

    def test_deleting_the_open_workflow_by_id_closes_it(self, project_engine: Engine) -> None:
        tab = _open_and_save(project_engine, "tab")

        deleted = asyncio.run(
            project_engine.ahandle_request(
                DeleteWorkflowRequest(workflow_id=tab.workflow_id), workflow_id=tab.workflow_id
            )
        )

        assert isinstance(deleted, DeleteWorkflowResultSuccess), deleted.result_details
        assert not project_engine.open_workflows.is_open(tab.workflow_id or "")
        assert project_engine.workflow_registry.find_by_id(tab.workflow_id or "") is None
        assert not Path(tab.file_path).exists()

    def test_deleting_another_workflow_leaves_the_open_one_open(self, project_engine: Engine) -> None:
        other = _open_and_save(project_engine, "other")
        project_engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
        tab = _open_and_save(project_engine, "tab")

        deleted = asyncio.run(
            project_engine.ahandle_request(
                DeleteWorkflowRequest(workflow_id=other.workflow_id), workflow_id=tab.workflow_id
            )
        )

        assert isinstance(deleted, DeleteWorkflowResultSuccess), deleted.result_details
        assert project_engine.open_workflows.is_open(tab.workflow_id or "")
        assert project_engine.workflow_registry.find_by_id(other.workflow_id or "") is None

    def test_renaming_an_open_workflow_saves_that_workflow(self, engine: Engine) -> None:
        tab = push_unsaved_workflow(engine, "tab")
        saved_runtimes: list[WorkflowRuntime] = []

        async def save_open_workflow(workflow: WorkflowRuntime, request: SaveWorkflowRequest, **_: Any) -> Any:
            saved_runtimes.append(workflow)
            return SaveWorkflowResultSuccess(
                file_path=f"{request.file_name}.py", workflow_name=str(request.file_name), result_details="ok"
            )

        with (
            _also_open(engine, "target") as target,
            patch.object(engine.workflow_manager.saver, "save_open_workflow", side_effect=save_open_workflow),
            patch.object(engine.workflow_manager, "persist_external_workflow_registration"),
        ):
            renamed = asyncio.run(
                engine.ahandle_request(
                    RenameWorkflowRequest(workflow_id=target.workflow_id, requested_name="renamed"),
                    workflow_id=tab.workflow_id,
                )
            )

        assert isinstance(renamed, RenameWorkflowResultSuccess), renamed.result_details
        assert saved_runtimes == [target]

    def test_publishing_saves_the_workflow_it_publishes(self, project_engine: Engine) -> None:
        target = _open_and_save(project_engine, "target")
        project_engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
        tab = _open_and_save(project_engine, "tab")
        target_runtime = WorkflowRuntime(target.workflow_id or "", project_engine)
        saved_runtimes: list[WorkflowRuntime] = []

        async def save_open_workflow(workflow: WorkflowRuntime, _request: SaveWorkflowRequest, **_: Any) -> Any:
            saved_runtimes.append(workflow)
            return SaveWorkflowResultSuccess(file_path="", workflow_name="", result_details="ok")

        publisher = RegisteredEventHandler(
            handler=lambda _request: PublishWorkflowResultSuccess(
                published_workflow_file_path="", skip_published_workflow_registration=True, result_details="ok"
            ),
            library_data=MagicMock(),
            event_data=PublishWorkflowRegisteredEventData(
                start_flow_node_type="Start",
                start_flow_node_library_name="lib",
                end_flow_node_type="End",
                end_flow_node_library_name="lib",
            ),
        )
        with (
            patch.dict(project_engine.open_workflows._by_id, {target_runtime.workflow_id: target_runtime}),
            patch.object(
                project_engine.library_manager, "get_registered_event_handlers", return_value={"lib": publisher}
            ),
            patch.object(project_engine.workflow_manager.saver, "save_open_workflow", side_effect=save_open_workflow),
        ):
            published = asyncio.run(
                project_engine.ahandle_request(
                    PublishWorkflowRequest(workflow_id=target.workflow_id, publisher_name="lib"),
                    workflow_id=tab.workflow_id,
                )
            )

        assert isinstance(published, PublishWorkflowResultSuccess), published.result_details
        assert saved_runtimes == [target_runtime]


class TestWorkflowRequestsActOnTheWorkflowTheyAreHanded:
    def test_save_serializes_and_records_the_workflow_it_is_handed(self, engine: Engine) -> None:
        push_unsaved_workflow(engine, "tab")
        recorded = _RecordedRequests(flow_name="target_canvas")
        record_saved_file = MagicMock(return_value=MagicMock(name="saved", workflow_id="saved"))

        with (
            _also_open(engine, "target") as target,
            patch.object(engine, "ahandle_request", new=recorded),
            patch.object(engine.workflow_manager, "extract_workflow_shape_in", side_effect=ValueError("no shape")),
            patch.object(
                engine.workflow_manager.saver,
                "_save_workflow_file_inline",
                return_value=SaveWorkflowFileFromSerializedFlowResultSuccess(
                    file_path="target.py", workflow_metadata=MagicMock(), result_details="ok"
                ),
            ),
            patch.object(engine.workflow_manager.saver, "_record_saved_file", record_saved_file),
        ):
            result = asyncio.run(
                engine.workflow_manager.saver.on_save_workflow_request(SaveWorkflowRequest(file_name="target"), target)
            )
            extract_calls = engine.workflow_manager.extract_workflow_shape_in.call_args_list  # type: ignore[attr-defined]

        assert isinstance(result, SaveWorkflowResultSuccess), result.result_details
        assert recorded.workflow_ids == [
            (GetTopLevelFlowRequest, target.workflow_id),
            (SerializeFlowToCommandsRequest, target.workflow_id),
        ]
        assert [call.args[0] for call in extract_calls] == [target]
        assert record_saved_file.call_args.kwargs["current_workflow"] is engine.workflow_registry.get(
            target.workflow_id
        )

    def test_save_addressed_to_a_workflow_that_is_not_open_fails_with_its_own_failure(self, engine: Engine) -> None:
        push_unsaved_workflow(engine, "tab")

        result = engine.handle_request(SaveWorkflowRequest(file_name="anything"), workflow_id="not-open")

        assert isinstance(result, SaveWorkflowResultFailure)
        assert "isn't open" in str(result.result_details)

    def test_save_subflow_serializes_the_handed_workflow_into_the_named_workflows_file(
        self, engine: Engine, tmp_path: Path
    ) -> None:
        push_unsaved_workflow(engine, "tab")
        saved_target = _write_saved_workflow(engine, tmp_path.resolve() / "target.py")
        recorded = _RecordedRequests(flow_name="unused")
        written_paths: list[str | None] = []

        async def save_file(request: Any) -> SaveWorkflowFileFromSerializedFlowResultSuccess:
            written_paths.append(request.file_path)
            return SaveWorkflowFileFromSerializedFlowResultSuccess(
                file_path=str(request.file_path), workflow_metadata=saved_target.metadata, result_details="ok"
            )

        with (
            _also_open(engine, "subflow source") as source,
            patch.object(engine, "ahandle_request", new=recorded),
            patch.object(engine.workflow_manager, "extract_workflow_shape_in", side_effect=ValueError("no shape")),
            patch.object(
                engine.workflow_manager.saver,
                "on_save_workflow_file_from_serialized_flow_request",
                side_effect=save_file,
            ),
        ):
            result = asyncio.run(
                engine.workflow_manager.saver.on_save_subflow_to_workflow(
                    SaveSubflowToWorkflowRequest(flow_name="Sub", workflow_id=saved_target.workflow_id), source
                )
            )

        assert isinstance(result, SaveSubflowToWorkflowResultSuccess), result.result_details
        assert recorded.workflow_ids == [(SerializeFlowToCommandsRequest, source.workflow_id)]
        assert written_paths == [str(saved_target.absolute_path)]

    def test_referenced_import_creates_its_flow_in_the_workflow_it_is_handed(
        self, engine: Engine, tmp_path: Path
    ) -> None:
        push_unsaved_workflow(engine, "tab")
        tab = engine.open_workflows.sole_workflow()
        _create_flow(engine, tab, "tab_canvas")
        imported = _write_saved_workflow(engine, tmp_path.resolve() / "imported.py")

        with _also_open(engine, "importing") as importing:
            canvas = _create_flow(engine, importing, "importing_canvas")

            async def run_workflow(_relative_file_path: str) -> WorkflowExecutionResult:
                # A workflow file creates its flow under the flow it is run in.
                created = engine.flow_manager.on_create_flow_request(
                    CreateFlowRequest(parent_flow_name=canvas, flow_name="imported_flow"), importing
                )
                assert isinstance(created, CreateFlowResultSuccess), created.result_details
                return WorkflowExecutionResult(execution_successful=True, execution_details="ok")

            with patch.object(engine.workflow_manager.runner, "run_workflow", side_effect=run_workflow):
                result = asyncio.run(
                    engine.ahandle_request(
                        ImportWorkflowAsReferencedSubFlowRequest(workflow_id=imported.workflow_id, flow_name=canvas),
                        workflow_id=importing.workflow_id,
                    )
                )
                into_tab_flow = asyncio.run(
                    engine.ahandle_request(
                        ImportWorkflowAsReferencedSubFlowRequest(
                            workflow_id=imported.workflow_id, flow_name="tab_canvas"
                        ),
                        workflow_id=importing.workflow_id,
                    )
                )

            assert isinstance(result, ImportWorkflowAsReferencedSubFlowResultSuccess), result.result_details
            assert result.created_flow_name == "imported_flow"
            assert importing.flows.name_to_parent_name["imported_flow"] == canvas
            assert "imported_flow" not in tab.flows.name_to_parent_name
            # The tab's flow is not in the workflow the import was handed.
            assert isinstance(into_tab_flow, ImportWorkflowAsReferencedSubFlowResultFailure)
            assert "target flow does not exist" in str(into_tab_flow.result_details)

    def test_ensure_creates_its_flow_in_the_workflow_it_is_handed(self, engine: Engine) -> None:
        tab = push_unsaved_workflow(engine, "tab")

        with _also_open(engine, "target") as target:
            result = engine.handle_request(
                EnsureWorkflowAndFlowRequest(flow_name="canvas"), workflow_id=target.workflow_id
            )

            assert isinstance(result, EnsureWorkflowAndFlowResultSuccess), result.result_details
            assert result.workflow_id == target.workflow_id
            assert result.created_workflow is False
            assert result.created_flow is True
            assert target.context.get_current_flow().name == result.flow_name
            assert engine.open_workflows.get(tab.workflow_id).flows.name_to_parent_name == {}

    def test_ensure_naming_another_workflow_than_the_one_it_is_handed_fails(self, engine: Engine) -> None:
        tab = push_unsaved_workflow(engine, "tab")
        other = engine.workflow_registry.register_unsaved("other")

        result = engine.handle_request(
            EnsureWorkflowAndFlowRequest(workflow_id=other.workflow_id), workflow_id=tab.workflow_id
        )

        assert isinstance(result, EnsureWorkflowAndFlowResultFailure)
        assert "'unsaved:" in str(result.result_details)
        assert engine.open_workflows.get(tab.workflow_id).flows.name_to_parent_name == {}

    def test_run_command_naming_nothing_answers_for_the_workflow_it_is_handed(
        self, engine: Engine, tmp_path: Path
    ) -> None:
        workspace = tmp_path.resolve()
        engine.config_manager.workspace_path = workspace
        tab = _write_saved_workflow(engine, workspace / "tab.py", workflow_shape=WorkflowShape(inputs={}, outputs={}))
        engine.context_manager.push_workflow(workflow_id=tab.workflow_id)

        with _also_open(engine, "beside"):
            result = asyncio.run(engine.ahandle_request(GetWorkflowRunCommandRequest(), workflow_id=tab.workflow_id))

        assert isinstance(result, GetWorkflowRunCommandResultSuccess), result.result_details
        assert "tab.py" in result.run_command

    def test_run_command_naming_nothing_with_no_workflow_fails(self, engine: Engine) -> None:
        result = asyncio.run(
            engine.workflow_manager.runner.on_get_workflow_run_command_request(GetWorkflowRunCommandRequest())
        )

        assert isinstance(result, GetWorkflowRunCommandResultFailure)
        assert "no workflow is loaded" in str(result.result_details)
