"""Entry points that send requests without an envelope id bind the workflow those requests act on.

Every test runs with dispatch's "the one open workflow" fallback off, so a request that neither its
envelope nor a binding addresses gets no workflow, as it will once addressing is required.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from griptape_nodes.bootstrap.workflow_executors.local_workflow_executor import LocalWorkflowExecutor
from griptape_nodes.node_library.workflow_registry import WorkflowMetadata, new_workflow_id
from griptape_nodes.retained_mode.events.flow_events import (
    CreateFlowRequest,
    CreateFlowResultSuccess,
    GetTopLevelFlowRequest,
    GetTopLevelFlowResultSuccess,
    ImportWorkflowAsReferencedSubFlowRequest,
)
from griptape_nodes.retained_mode.events.library_events import ReloadAllLibrariesRequest
from griptape_nodes.retained_mode.events.workflow_events import (
    ImportWorkflowAsReferencedSubFlowResultSuccess,
    PublishWorkflowRegisteredEventData,
    PublishWorkflowRequest,
    PublishWorkflowResultSuccess,
    RunWorkflowFromRegistryRequest,
    RunWorkflowFromRegistryResultSuccess,
    RunWorkflowFromScratchRequest,
    RunWorkflowFromScratchResultSuccess,
    RunWorkflowWithCurrentStateRequest,
    RunWorkflowWithCurrentStateResultSuccess,
    SaveWorkflowResultSuccess,
)
from griptape_nodes.retained_mode.file_metadata.workflow_metadata import FLOW_COMMANDS_KEY, collect_workflow_metadata
from griptape_nodes.retained_mode.managers.library.common import RegisteredEventHandler
from tests.workflow_context import push_unsaved_workflow, without_sole_workflow_fallback

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from griptape_nodes.node_library.workflow_registry import Workflow
    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.events.base_events import RequestPayload, ResultPayload
    from griptape_nodes.retained_mode.runtime.workflow_runtime import WorkflowRuntime

# A saved workflow file as codegen writes one: it opens its workflow unless one is open, then sends
# requests that name no workflow.
_SAVED_WORKFLOW_SOURCE = """
from griptape_nodes.retained_mode.events.flow_events import CreateFlowRequest
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

async def build_workflow() -> None:
    context_manager = GriptapeNodes.ContextManager()
    if not context_manager.has_current_workflow():
        context_manager.push_workflow(file_path=__file__)
    await GriptapeNodes.ahandle_request(
        CreateFlowRequest(parent_flow_name=None, flow_name="saved_canvas", set_as_new_context=False)
    )
"""


@pytest.fixture(autouse=True)
def no_sole_workflow_fallback() -> Iterator[None]:
    """Turn the fallback off for every test here."""
    with without_sole_workflow_fallback():
        yield


def _write_saved_workflow(path: Path) -> Path:
    path.write_text(_SAVED_WORKFLOW_SOURCE, encoding="utf-8")
    return path


def _register_saved_workflow(engine: Engine, path: Path) -> Workflow:
    _write_saved_workflow(path)
    metadata = WorkflowMetadata(
        workflow_id=new_workflow_id(),
        name=path.stem,
        schema_version=WorkflowMetadata.LATEST_SCHEMA_VERSION,
        engine_version_created_with="0.0.0",
        node_libraries_referenced=[],
    )
    return engine.workflow_registry.register_file(path, metadata)


def _create_flow(engine: Engine, workflow: WorkflowRuntime, flow_name: str) -> str:
    result = engine.flow_manager.on_create_flow_request(
        CreateFlowRequest(parent_flow_name=None, flow_name=flow_name, set_as_new_context=False), workflow
    )
    assert isinstance(result, CreateFlowResultSuccess), result.result_details
    return result.flow_name


class TestSavedWorkflowFilesTheEngineRuns:
    def test_a_file_run_from_scratch_builds_into_the_workflow_it_opens(self, engine: Engine, tmp_path: Path) -> None:
        saved = _write_saved_workflow(tmp_path / "saved.py")

        result = asyncio.run(engine.ahandle_request(RunWorkflowFromScratchRequest(file_path=str(saved))))

        assert isinstance(result, RunWorkflowFromScratchResultSuccess), result.result_details
        assert result.workflow_id is not None
        opened = engine.open_workflows.get(result.workflow_id)
        assert "saved_canvas" in opened.flows.name_to_parent_name
        assert engine.bound_workflow is None

    def test_a_file_run_from_the_registry_builds_into_the_workflow_the_run_opens(
        self, engine: Engine, tmp_path: Path
    ) -> None:
        saved = _register_saved_workflow(engine, tmp_path / "registered.py")

        result = asyncio.run(
            engine.ahandle_request(
                RunWorkflowFromRegistryRequest(workflow_id=saved.workflow_id, run_with_clean_slate=True)
            )
        )

        assert isinstance(result, RunWorkflowFromRegistryResultSuccess), result.result_details
        opened = engine.open_workflows.get(saved.workflow_id)
        assert "saved_canvas" in opened.flows.name_to_parent_name

    def test_a_file_run_with_the_current_state_builds_into_the_open_workflow(
        self, engine: Engine, tmp_path: Path
    ) -> None:
        open_workflow = engine.open_workflows.get(push_unsaved_workflow(engine).workflow_id)
        saved = _write_saved_workflow(tmp_path / "saved.py")

        result = asyncio.run(engine.ahandle_request(RunWorkflowWithCurrentStateRequest(file_path=str(saved))))

        assert isinstance(result, RunWorkflowWithCurrentStateResultSuccess), result.result_details
        assert result.workflow_id == open_workflow.workflow_id
        assert "saved_canvas" in open_workflow.flows.name_to_parent_name

    def test_a_referenced_import_builds_into_the_workflow_it_is_handed(self, engine: Engine, tmp_path: Path) -> None:
        importing = engine.open_workflows.get(push_unsaved_workflow(engine).workflow_id)
        canvas = _create_flow(engine, importing, "canvas")
        imported = _register_saved_workflow(engine, tmp_path / "imported.py")

        result = asyncio.run(
            importing.ahandle_request(
                ImportWorkflowAsReferencedSubFlowRequest(workflow_id=imported.workflow_id, flow_name=canvas)
            )
        )

        assert isinstance(result, ImportWorkflowAsReferencedSubFlowResultSuccess), result.result_details
        assert importing.flows.name_to_parent_name[result.created_flow_name] == canvas


class TestSavedWorkflowFilesRunOnTheirOwn:
    def test_the_workflow_a_file_opens_stays_bound_for_the_rest_of_its_task(
        self, engine: Engine, tmp_path: Path
    ) -> None:
        saved = _write_saved_workflow(tmp_path / "script.py")
        namespace: dict[str, Any] = {"__file__": str(saved)}
        exec(compile(saved.read_text(encoding="utf-8"), str(saved), "exec"), namespace)  # noqa: S102

        async def run_as_script() -> ResultPayload:
            await namespace["build_workflow"]()
            # As a script's executor does next, in the same task.
            return await engine.ahandle_request(GetTopLevelFlowRequest())

        result = asyncio.run(run_as_script())

        assert isinstance(result, GetTopLevelFlowResultSuccess), result.result_details
        assert result.flow_name == "saved_canvas"
        # The binding lived in the script's task only.
        assert engine.bound_workflow is None

    def test_opening_a_workflow_by_id_binds_nothing(self, engine: Engine) -> None:
        """Engine code opens workflows by id and addresses its requests itself."""
        push_unsaved_workflow(engine)

        assert engine.bound_workflow is None

    def test_a_workflow_with_block_ends_its_binding_with_the_block(self, engine: Engine) -> None:
        name = engine.workflow_registry.register_unsaved("block").name

        with engine.context_manager.workflow(name):
            bound = engine.bound_workflow
            assert bound is not None
            assert bound.workflow_id == engine.context_manager.get_current_workflow_id()

        assert engine.bound_workflow is None


class TestLocalWorkflowExecutor:
    def test_a_workflow_it_loads_from_a_file_stays_bound_for_the_rest_of_the_run(
        self, engine: Engine, tmp_path: Path
    ) -> None:
        saved = _write_saved_workflow(tmp_path / "saved.py")
        failed_path = tmp_path / "failed.py"
        executor = LocalWorkflowExecutor(save_on_failure_path=str(failed_path))

        async def run() -> tuple[str, ResultPayload]:
            flow_name = await executor.aprepare_workflow_for_run(flow_input={}, workflow_path=str(saved))
            top_level = await engine.ahandle_request(GetTopLevelFlowRequest())
            # Its failure save, run as the executor exits, reads the same workflow.
            await executor._save_failed_workflow(RuntimeError("The run failed."))
            return flow_name, top_level

        flow_name, top_level = asyncio.run(run())

        assert flow_name == "saved_canvas"
        assert isinstance(top_level, GetTopLevelFlowResultSuccess), top_level.result_details
        assert top_level.flow_name == "saved_canvas"
        assert failed_path.exists()

    def test_with_no_file_it_reuses_the_workflow_a_script_opened(self, tmp_path: Path) -> None:
        saved = _write_saved_workflow(tmp_path / "script.py")
        namespace: dict[str, Any] = {"__file__": str(saved)}
        exec(compile(saved.read_text(encoding="utf-8"), str(saved), "exec"), namespace)  # noqa: S102
        executor = LocalWorkflowExecutor()

        async def run_as_script() -> WorkflowRuntime | None:
            await namespace["build_workflow"]()
            return await executor.ensure_default_workflow()

        reused = asyncio.run(run_as_script())

        assert reused is not None
        assert "saved_canvas" in reused.flows.name_to_parent_name


class TestPublishing:
    def test_the_requests_a_publisher_sends_act_on_the_workflow_it_publishes(
        self, engine: Engine, tmp_path: Path
    ) -> None:
        saved = _register_saved_workflow(engine, tmp_path / "published.py")
        engine.context_manager.push_workflow(workflow_id=saved.workflow_id)
        published = engine.open_workflows.get(saved.workflow_id)

        def publish(_request: RequestPayload) -> ResultPayload:
            created = engine.handle_request(
                CreateFlowRequest(parent_flow_name=None, flow_name="packaged", set_as_new_context=False)
            )
            assert isinstance(created, CreateFlowResultSuccess), created.result_details
            return PublishWorkflowResultSuccess(
                published_workflow_file_path="", skip_published_workflow_registration=True, result_details="ok"
            )

        publisher = RegisteredEventHandler(
            handler=publish,
            library_data=MagicMock(),
            event_data=PublishWorkflowRegisteredEventData(
                start_flow_node_type="Start",
                start_flow_node_library_name="lib",
                end_flow_node_type="End",
                end_flow_node_library_name="lib",
            ),
        )
        with (
            patch.object(engine.library_manager, "get_registered_event_handlers", return_value={"lib": publisher}),
            patch.object(
                engine.workflow_manager.saver,
                "save_open_workflow",
                AsyncMock(return_value=SaveWorkflowResultSuccess(file_path="", workflow_name="", result_details="ok")),
            ),
        ):
            result = asyncio.run(
                engine.ahandle_request(PublishWorkflowRequest(workflow_id=saved.workflow_id, publisher_name="lib"))
            )

        assert isinstance(result, PublishWorkflowResultSuccess), result.result_details
        assert "packaged" in published.flows.name_to_parent_name


class TestImageMetadata:
    def test_metadata_for_a_file_written_for_a_workflow_embeds_that_workflow_and_its_flow(self, engine: Engine) -> None:
        entry = push_unsaved_workflow(engine, "pictured")
        workflow = engine.open_workflows.get(entry.workflow_id)
        canvas = _create_flow(engine, workflow, "canvas")
        workflow.context.push_flow(engine.flow_manager.get_flow_by_name_in(workflow, canvas))

        metadata = collect_workflow_metadata(engine, workflow)

        assert metadata["gtn_workflow_name"] == entry.name
        assert FLOW_COMMANDS_KEY in metadata


class TestCleanSlateClosesEveryOpenWorkflow:
    """Work that starts from a clean slate closes each open workflow by id, never "the only one"."""

    def test_the_open_workflow_is_closed_by_id(self, engine: Engine) -> None:
        push_unsaved_workflow(engine)

        result = asyncio.run(engine.object_manager.close_open_workflows())

        assert result.succeeded(), result.result_details
        assert list(engine.open_workflows) == []

    def test_with_none_open_it_clears_with_no_workflow_even_under_a_stale_binding(self, engine: Engine) -> None:
        closed = engine.open_workflows.get(push_unsaved_workflow(engine).workflow_id)
        asyncio.run(engine.close_workflow_state(closed))

        with engine.bind_workflow(closed):
            result = asyncio.run(engine.object_manager.close_open_workflows())

        assert result.succeeded(), result.result_details

    def test_a_run_from_scratch_replaces_the_open_workflow(self, engine: Engine, tmp_path: Path) -> None:
        replaced = push_unsaved_workflow(engine)
        saved = _write_saved_workflow(tmp_path / "saved.py")

        result = asyncio.run(engine.ahandle_request(RunWorkflowFromScratchRequest(file_path=str(saved))))

        assert isinstance(result, RunWorkflowFromScratchResultSuccess), result.result_details
        assert not engine.open_workflows.is_open(replaced.workflow_id)
        assert [workflow.workflow_id for workflow in engine.open_workflows] == [result.workflow_id]

    def test_a_run_from_the_registry_with_a_clean_slate_replaces_the_open_workflow(
        self, engine: Engine, tmp_path: Path
    ) -> None:
        replaced = push_unsaved_workflow(engine)
        saved = _register_saved_workflow(engine, tmp_path / "registered.py")

        result = asyncio.run(
            engine.ahandle_request(
                RunWorkflowFromRegistryRequest(workflow_id=saved.workflow_id, run_with_clean_slate=True)
            )
        )

        assert isinstance(result, RunWorkflowFromRegistryResultSuccess), result.result_details
        assert not engine.open_workflows.is_open(replaced.workflow_id)

    def test_a_library_reload_closes_the_open_workflow(self, engine: Engine) -> None:
        push_unsaved_workflow(engine)
        library_manager = engine.library_manager

        with (
            patch.object(library_manager, "load_all_libraries_from_config", AsyncMock(return_value=[])),
            patch.object(library_manager.workers, "maybe_start_workers_for_existing_session", AsyncMock()),
        ):
            result = asyncio.run(library_manager._run_reload_libraries(ReloadAllLibrariesRequest()))

        assert result.succeeded(), result.result_details
        assert list(engine.open_workflows) == []
