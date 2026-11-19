"""The `GriptapeNodes` facade never guesses a workflow: callers act on the one they are bound to.

Saved workflow files and node libraries reach the engine through the facade. A saved file run
standalone opens itself with `push_workflow`, which binds the workflow it opens, so the facade
calls after it act on that workflow. Work bound to none acts on none, however many workflows are open.
"""

from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from griptape_nodes.retained_mode.events.flow_events import CreateFlowRequest, CreateFlowResultSuccess
from griptape_nodes.retained_mode.events.generic_events import WorkflowNotNamedResultFailure
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
from griptape_nodes.retained_mode.runtime.binding import NO_WORKFLOW, bound_workflow
from griptape_nodes.retained_mode.runtime.open_workflow_set import NoOpenWorkflowError

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.runtime.workflow_runtime import WorkflowRuntime


def _push_like_a_saved_file(engine: Engine) -> WorkflowRuntime:
    """What a saved workflow file does first when it runs standalone: open itself, then use the facade."""
    GriptapeNodes.ContextManager().push_workflow(workflow_name="standalone")
    return engine.open_workflows.all_open()[0]


def _open_unbound(engine: Engine, display_name: str) -> WorkflowRuntime:
    entry = engine.workflow_registry.register_unsaved(display_name, listed=False)
    return engine.open_workflows.open(entry.workflow_id)


def _create_canvas() -> CreateFlowRequest:
    return CreateFlowRequest(parent_flow_name=None, flow_name="canvas")


class TestFacadeDoesNotGuess:
    def test_a_request_acting_on_a_workflow_fails_with_one_open_and_the_caller_unbound(self, engine: Engine) -> None:
        only = _open_unbound(engine, "only")

        result = GriptapeNodes.handle_request(_create_canvas())

        assert isinstance(result, WorkflowNotNamedResultFailure)
        assert "Name the open workflow" in str(result.result_details)
        assert not only.flows.name_to_parent_name

    @pytest.mark.asyncio
    async def test_an_async_request_fails_the_same_way(self, engine: Engine) -> None:
        only = _open_unbound(engine, "only")

        result = await GriptapeNodes.ahandle_request(_create_canvas())

        assert isinstance(result, WorkflowNotNamedResultFailure)
        assert not only.flows.name_to_parent_name

    def test_a_request_with_none_open_says_none_is_open(self, engine: Engine) -> None:  # noqa: ARG002
        result = GriptapeNodes.handle_request(_create_canvas())

        assert isinstance(result, WorkflowNotNamedResultFailure)
        assert "none is open" in str(result.result_details)

    def test_a_request_with_several_open_says_how_many(self, engine: Engine) -> None:
        _open_unbound(engine, "A")
        _open_unbound(engine, "B")

        result = GriptapeNodes.handle_request(_create_canvas())

        assert isinstance(result, WorkflowNotNamedResultFailure)
        assert "2 workflows are open" in str(result.result_details)

    def test_the_context_manager_has_no_workflow(self, engine: Engine) -> None:
        _open_unbound(engine, "only")

        assert not GriptapeNodes.ContextManager().has_current_workflow()

    def test_an_object_lookup_finds_nothing(self, engine: Engine) -> None:
        only = _open_unbound(engine, "only")
        only.handle_request(_create_canvas())

        assert GriptapeNodes.ObjectManager().attempt_get_object_by_name("canvas") is None

    def test_get_connections_refuses(self, engine: Engine) -> None:
        _open_unbound(engine, "only")

        with pytest.raises(NoOpenWorkflowError):
            GriptapeNodes.FlowManager().get_connections()

    def test_clear_current_workflow_data_refuses(self, engine: Engine) -> None:
        only = _open_unbound(engine, "only")

        with pytest.raises(RuntimeError, match="bound workflow"):
            GriptapeNodes.clear_current_workflow_data()

        assert engine.open_workflows.all_open() == [only]

    def test_work_bound_to_no_workflow_acts_on_none(self, engine: Engine) -> None:
        only = _open_unbound(engine, "only")

        with engine.bind_workflow(NO_WORKFLOW):
            result = GriptapeNodes.handle_request(_create_canvas())

        assert isinstance(result, WorkflowNotNamedResultFailure)
        assert not only.flows.name_to_parent_name

    def test_work_bound_to_a_workflow_acts_on_it(self, engine: Engine) -> None:
        first = _open_unbound(engine, "A")
        second = _open_unbound(engine, "B")

        with engine.bind_workflow(second):
            result = GriptapeNodes.handle_request(_create_canvas())

        assert isinstance(result, CreateFlowResultSuccess), result.result_details
        assert "canvas" in second.flows.name_to_parent_name
        assert "canvas" not in first.flows.name_to_parent_name


class TestPushWorkflowBindsWhatItOpens:
    def test_a_request_after_a_push_acts_on_the_opened_workflow(self, engine: Engine) -> None:
        opened = _push_like_a_saved_file(engine)

        result = GriptapeNodes.handle_request(_create_canvas())

        assert isinstance(result, CreateFlowResultSuccess), result.result_details
        assert "canvas" in opened.flows.name_to_parent_name

    @pytest.mark.asyncio
    async def test_an_async_request_after_a_push_acts_on_the_opened_workflow(self, engine: Engine) -> None:
        opened = _push_like_a_saved_file(engine)

        result = await GriptapeNodes.ahandle_request(_create_canvas())

        assert isinstance(result, CreateFlowResultSuccess), result.result_details
        assert "canvas" in opened.flows.name_to_parent_name

    def test_the_manager_methods_act_on_the_opened_workflow(self, engine: Engine) -> None:
        opened = _push_like_a_saved_file(engine)
        created = GriptapeNodes.handle_request(_create_canvas())
        assert isinstance(created, CreateFlowResultSuccess)

        assert GriptapeNodes.ContextManager().get_current_workflow_id() == opened.workflow_id
        assert GriptapeNodes.FlowManager().get_flow_by_name(created.flow_name).name == created.flow_name
        assert GriptapeNodes.FlowManager().get_connections() is opened.flows.connections
        assert GriptapeNodes.ObjectManager().has_object_with_name(created.flow_name)

    def test_save_static_file_saves_for_the_opened_workflow(self, engine: Engine) -> None:
        opened = _push_like_a_saved_file(engine)

        with patch.object(
            type(engine.static_files_manager), "save_static_file_in", return_value="saved"
        ) as save_static_file_in:
            saved = GriptapeNodes.StaticFilesManager().save_static_file(b"data", "file.txt")

        assert saved == "saved"
        assert save_static_file_in.call_args.args[0] is opened

    def test_extract_workflow_shape_looks_in_the_opened_workflow(self, engine: Engine) -> None:
        opened = _push_like_a_saved_file(engine)

        with patch.object(
            type(engine.workflow_manager), "extract_workflow_shape_in", return_value={}
        ) as extract_workflow_shape_in:
            GriptapeNodes.WorkflowManager().extract_workflow_shape("standalone")

        assert extract_workflow_shape_in.call_args.args[0] is opened

    def test_a_task_started_after_the_push_acts_on_the_opened_workflow(self, engine: Engine) -> None:
        opened = _push_like_a_saved_file(engine)

        async def send_from_a_task() -> object:
            return await asyncio.to_thread(GriptapeNodes.handle_request, _create_canvas())

        result = asyncio.run(send_from_a_task())

        assert isinstance(result, CreateFlowResultSuccess), result.result_details  # type: ignore[attr-defined]
        assert "canvas" in opened.flows.name_to_parent_name

    def test_a_plain_thread_does_not_inherit_the_binding(self, engine: Engine) -> None:
        opened = _push_like_a_saved_file(engine)
        results: list[object] = []

        thread = threading.Thread(target=lambda: results.append(GriptapeNodes.handle_request(_create_canvas())))
        thread.start()
        thread.join()

        assert isinstance(results[0], WorkflowNotNamedResultFailure)
        assert not opened.flows.name_to_parent_name

    def test_the_with_block_binds_on_entry_and_unbinds_on_exit(self, engine: Engine) -> None:
        with GriptapeNodes.ContextManager().workflow("standalone"):
            opened = engine.open_workflows.all_open()[0]
            assert engine.current_workflow_or_none() is opened

        assert engine.current_workflow_or_none() is None
        assert engine.open_workflows.all_open() == []

    def test_a_binding_made_inside_a_bind_block_ends_with_the_block(self, engine: Engine) -> None:
        # What dispatch does around a handler that pushes a workflow (a library handler with no
        # scope): the caller's own binding is restored when the handler returns.
        with engine.bind_workflow(NO_WORKFLOW):
            opened = _push_like_a_saved_file(engine)
            assert bound_workflow.get() is opened

        assert bound_workflow.get() is None


class TestPopWorkflowUnbinds:
    def test_pop_ends_the_binding(self, engine: Engine) -> None:
        _push_like_a_saved_file(engine)

        GriptapeNodes.ContextManager().pop_workflow()

        assert bound_workflow.get() is None
        assert engine.open_workflows.all_open() == []

    def test_the_next_push_binds_the_next_workflow(self, engine: Engine) -> None:
        _push_like_a_saved_file(engine)
        GriptapeNodes.ContextManager().pop_workflow()

        second = _push_like_a_saved_file(engine)

        assert engine.current_workflow_or_none() is second
