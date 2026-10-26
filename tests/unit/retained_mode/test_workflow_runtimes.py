"""The open workflow's runtime: how it opens and closes, and the state it holds."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from griptape_nodes.exe_types.flow import ControlFlow
from griptape_nodes.retained_mode.events.app_events import EngineHeartbeatRequest, EngineHeartbeatResultSuccess
from griptape_nodes.retained_mode.events.context_events import GetWorkflowContextRequest, GetWorkflowContextSuccess
from griptape_nodes.retained_mode.events.execution_events import (
    CancelFlowRequest,
    ExecuteNodeRequest,
    ExecuteNodeResultFailure,
)
from griptape_nodes.retained_mode.events.flow_events import (
    CreateFlowRequest,
    CreateFlowResultSuccess,
    DeleteFlowRequest,
    GetTopLevelFlowRequest,
    GetTopLevelFlowResultSuccess,
)
from griptape_nodes.retained_mode.events.object_events import (
    ClearAllObjectStateRequest,
    RenameObjectRequest,
    RenameObjectResultSuccess,
)
from griptape_nodes.retained_mode.events.variable_events import (
    CreateVariableRequest,
    CreateVariableResultSuccess,
    GetVariableValueRequest,
    GetVariableValueResultFailure,
)
from griptape_nodes.retained_mode.events.worker_events import WorkerGoneError
from griptape_nodes.retained_mode.events.workflow_events import (
    DeleteWorkflowRequest,
    SetVariableSubstitutionEnabledRequest,
)
from griptape_nodes.retained_mode.managers.context_manager import MirroredWorkflowContext
from griptape_nodes.retained_mode.runtime.context import (
    ElementContext,
    EmptyStackError,
    FlowContext,
    NoActiveFlowError,
    NodeContext,
    WorkflowContextStack,
)
from griptape_nodes.retained_mode.runtime.open_workflow_set import (
    NoOpenWorkflowError,
    OpenWorkflowError,
    OpenWorkflowSet,
)
from griptape_nodes.retained_mode.runtime.workflow_runtime import WorkflowRuntime
from griptape_nodes.retained_mode.variable_types import VariableScope
from tests.workflow_context import push_unsaved_workflow

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path

    from griptape_nodes.retained_mode.engine import Engine


class TestOpenWorkflowSet:
    def test_open_returns_an_empty_runtime_under_the_id(self) -> None:
        workflows = OpenWorkflowSet(MagicMock())

        workflow = workflows.open("abc")

        assert workflow.workflow_id == "abc"
        assert len(workflow.objects) == 0
        assert workflows.get("abc") is workflow

    def test_opening_the_same_id_twice_is_refused(self) -> None:
        workflows = OpenWorkflowSet(MagicMock())
        workflows.open("abc")

        with pytest.raises(OpenWorkflowError, match="already open"):
            workflows.open("abc")

    def test_opening_a_second_workflow_is_refused(self) -> None:
        workflows = OpenWorkflowSet(MagicMock())
        first = workflows.open("abc")

        with pytest.raises(OpenWorkflowError, match="'abc' is already open"):
            workflows.open("def")

        assert workflows.sole_workflow() is first

    def test_get_of_a_closed_workflow_raises(self) -> None:
        workflows = OpenWorkflowSet(MagicMock())

        with pytest.raises(OpenWorkflowError, match="not open"):
            workflows.get("abc")

    def test_close_forgets_the_workflow(self) -> None:
        workflows = OpenWorkflowSet(MagicMock())
        workflow = workflows.open("abc")

        workflows.close(workflow)

        assert workflows.sole_workflow_or_none() is None
        with pytest.raises(OpenWorkflowError, match="not open"):
            workflows.get("abc")

    def test_close_refuses_a_runtime_that_is_not_open(self) -> None:
        workflows = OpenWorkflowSet(MagicMock())
        workflow = workflows.open("abc")
        workflows.close(workflow)
        reopened = workflows.open("abc")

        with pytest.raises(OpenWorkflowError, match="not open"):
            workflows.close(workflow)
        assert workflows.sole_workflow() is reopened

    def test_sole_workflow_with_none_open(self) -> None:
        workflows = OpenWorkflowSet(MagicMock())

        assert workflows.sole_workflow_or_none() is None
        with pytest.raises(NoOpenWorkflowError, match="no workflow is open"):
            workflows.sole_workflow()

    def test_sole_workflow_with_one_open(self) -> None:
        workflows = OpenWorkflowSet(MagicMock())
        workflow = workflows.open("abc")

        assert workflows.sole_workflow() is workflow
        assert workflows.sole_workflow_or_none() is workflow

    def test_sole_workflow_never_picks_one_of_several(self) -> None:
        workflows = OpenWorkflowSet(MagicMock())
        workflows.open("abc")
        # Several open cannot happen through `open`; force it to check the read refuses to guess.
        workflows._by_id["def"] = WorkflowRuntime("def", MagicMock())

        with pytest.raises(OpenWorkflowError, match="2 workflows are open"):
            workflows.sole_workflow_or_none()
        with pytest.raises(OpenWorkflowError, match="2 workflows are open"):
            workflows.sole_workflow()

    def test_iterating_lists_every_open_workflow_in_the_order_opened(self) -> None:
        workflows = OpenWorkflowSet(MagicMock())
        assert list(workflows) == []
        first = workflows.open("abc")
        # Several open cannot happen through `open`; force it to check every one is listed.
        second = WorkflowRuntime("def", MagicMock())
        workflows._by_id["def"] = second

        assert list(workflows) == [first, second]

    def test_reads_never_open_a_workflow(self) -> None:
        workflows = OpenWorkflowSet(MagicMock())

        workflows.sole_workflow_or_none()
        with pytest.raises(OpenWorkflowError):
            workflows.get("abc")
        with pytest.raises(NoOpenWorkflowError):
            workflows.sole_workflow()

        assert workflows.sole_workflow_or_none() is None


class TestWorkflowContextStack:
    def test_a_new_runtimes_context_is_empty(self) -> None:
        context = WorkflowRuntime("abc", MagicMock()).context

        assert not context.has_current_flow()
        assert not context.has_current_node()
        assert not context.has_current_element()

    def test_flows_nodes_and_elements_nest(self) -> None:
        context = WorkflowContextStack()
        flow = MagicMock(name="flow")
        node = MagicMock()
        element = MagicMock()

        with FlowContext(context, flow), NodeContext(context, node), ElementContext(context, element):
            assert context.get_current_flow() is flow
            assert context.get_current_node() is node
            assert context.get_current_element() is element

        assert not context.has_current_flow()

    def test_each_level_needs_the_one_above(self) -> None:
        context = WorkflowContextStack()

        with pytest.raises(NoActiveFlowError):
            context.push_node(MagicMock())
        with pytest.raises(EmptyStackError):
            context.pop_flow()

        context.push_flow(MagicMock(name="flow"))
        with pytest.raises(EmptyStackError):
            context.push_element(MagicMock())
        with pytest.raises(EmptyStackError):
            context.get_current_node()

    def test_each_runtime_has_its_own_context(self) -> None:
        first = WorkflowRuntime("first", MagicMock())
        second = WorkflowRuntime("second", MagicMock())

        first.context.push_flow(MagicMock(name="flow"))

        assert not second.context.has_current_flow()


class TestRuntimeLifecycle:
    """The runtime is open exactly while a workflow is in context."""

    def test_a_push_onto_an_empty_stack_opens_the_pushed_workflow(self, engine: Engine) -> None:
        workflow = push_unsaved_workflow(engine)

        assert engine.open_workflows.sole_workflow().workflow_id == workflow.workflow_id

    def test_a_push_while_a_workflow_is_open_is_refused(self, engine: Engine, tmp_path: Path) -> None:
        outer = push_unsaved_workflow(engine, "outer")
        runtime = engine.open_workflows.sole_workflow()
        inner = engine.workflow_registry.register_unsaved("inner")
        unregistered_file = tmp_path / "never_registered.py"
        unregistered_file.write_text("# stub")

        with pytest.raises(engine.context_manager.WorkflowAlreadyOpenError, match="is already open"):
            engine.context_manager.push_workflow(workflow_id=inner.workflow_id)
        with pytest.raises(engine.context_manager.WorkflowAlreadyOpenError):
            engine.context_manager.push_workflow(file_path=str(unregistered_file))

        assert engine.open_workflows.sole_workflow() is runtime
        assert engine.context_manager.get_current_workflow_id() == outer.workflow_id
        # Refused before anything is registered.
        assert engine.workflow_registry.find_by_path(str(unregistered_file)) is None

    def test_the_workflow_block_opens_and_closes_the_workflow(self, engine: Engine) -> None:
        workflow = engine.workflow_registry.register_unsaved("block")

        with engine.context_manager.workflow(workflow.name):
            assert engine.open_workflows.sole_workflow().workflow_id == workflow.workflow_id

        assert engine.open_workflows.sole_workflow_or_none() is None

    def test_the_workflow_block_refuses_while_a_workflow_is_open(self, engine: Engine) -> None:
        push_unsaved_workflow(engine, "open")
        runtime = engine.open_workflows.sole_workflow()
        other = engine.workflow_registry.register_unsaved("other")

        with (
            pytest.raises(engine.context_manager.WorkflowAlreadyOpenError),
            engine.context_manager.workflow(other.name),
        ):
            pass

        assert engine.open_workflows.sole_workflow() is runtime

    def test_popping_the_workflow_closes_it(self, engine: Engine) -> None:
        push_unsaved_workflow(engine)

        engine.context_manager.pop_workflow()

        assert engine.open_workflows.sole_workflow_or_none() is None
        assert not engine.context_manager.has_current_workflow()

    def test_mirroring_a_peers_workflow_opens_nothing(self, engine: Engine) -> None:
        engine.context_manager.mirror_workflow_context("00000000000000000000000000000abc", None, None, None)

        assert engine.context_manager.has_current_workflow()
        assert engine.open_workflows.sole_workflow_or_none() is None

        engine.context_manager.mirror_workflow_context(None, None, None, None)
        assert engine.open_workflows.sole_workflow_or_none() is None


class TestIdentityFromTheRuntime:
    """The open workflow's id is its runtime's; its name and file are read from the registry entry by that id."""

    def test_the_getters_read_the_open_workflows_entry(self, engine: Engine, tmp_path: Path) -> None:
        workflow = push_unsaved_workflow(engine, "Shot", working_directory=str(tmp_path))
        context_manager = engine.context_manager

        assert context_manager.get_current_workflow_id() == engine.open_workflows.sole_workflow().workflow_id
        assert context_manager.get_current_workflow() is workflow
        assert context_manager.get_current_workflow_name() == workflow.name
        assert context_manager.get_current_workflow_file_path() is None
        assert context_manager.get_current_workflow_working_directory() == str(tmp_path)
        assert context_manager.is_workflow_in_context(workflow.workflow_id)
        assert engine.workflow_registry.is_open(workflow.workflow_id)

    def test_saving_and_moving_the_open_workflow_shows_its_new_name_and_file(
        self, engine: Engine, tmp_path: Path
    ) -> None:
        workflow = push_unsaved_workflow(engine)
        runtime = engine.open_workflows.sole_workflow()
        saved = tmp_path / "shot.py"
        saved.write_text("# stub")
        moved = tmp_path / "moved" / "final.py"
        moved.parent.mkdir()
        moved.write_text("# stub")

        engine.workflow_registry.set_file_path(workflow, saved)
        assert engine.context_manager.get_current_workflow_file_path() == str(workflow.absolute_path)
        engine.workflow_registry.set_file_path(workflow, moved)

        context = engine.handle_request(GetWorkflowContextRequest())
        heartbeat = engine.handle_request(EngineHeartbeatRequest(heartbeat_id="hb"))
        assert engine.open_workflows.sole_workflow() is runtime
        assert runtime.workflow_id == workflow.workflow_id
        assert engine.context_manager.get_current_workflow_name() == workflow.name
        assert engine.context_manager.get_current_workflow_file_path() == str(workflow.absolute_path)
        assert workflow.absolute_path is not None
        assert workflow.absolute_path.name == "final.py"
        assert isinstance(context, GetWorkflowContextSuccess)
        assert context.workflow_id == workflow.workflow_id
        assert context.workflow_name == workflow.name
        assert context.is_saved is True
        assert isinstance(heartbeat, EngineHeartbeatResultSuccess)
        assert heartbeat.current_workflow == workflow.name
        assert heartbeat.current_workflow_id == workflow.workflow_id
        assert heartbeat.workflow_file_path == str(workflow.absolute_path)

    def test_with_nothing_open_the_context_and_heartbeat_report_none(self, engine: Engine) -> None:
        context = engine.handle_request(GetWorkflowContextRequest())
        heartbeat = engine.handle_request(EngineHeartbeatRequest(heartbeat_id="hb"))

        assert isinstance(context, GetWorkflowContextSuccess)
        assert context.workflow_id is None
        assert context.workflow_name is None
        assert isinstance(heartbeat, EngineHeartbeatResultSuccess)
        assert heartbeat.current_workflow is None
        assert heartbeat.current_workflow_id is None
        with pytest.raises(engine.context_manager.NoActiveWorkflowError):
            engine.context_manager.get_current_workflow_id()


class TestWorkerMirror:
    """A worker's mirror of its orchestrator's workflow is not an open workflow."""

    PEER_ID = "00000000000000000000000000000abc"

    @pytest.fixture(autouse=True)
    def _clear_mirror(self, engine: Engine) -> Generator[None, None, None]:
        yield
        if engine.open_workflows.sole_workflow_or_none() is not None:
            engine.context_manager._mirror = None
            return
        engine.context_manager.mirror_workflow_context(None, None, None, None)

    def test_the_mirror_is_the_context_but_not_an_open_workflow(self, engine: Engine) -> None:
        engine.context_manager.mirror_workflow_context(self.PEER_ID, "peer", None, None)

        assert engine.context_manager.get_current_workflow_id() == self.PEER_ID
        assert engine.context_manager.get_current_workflow_name() == f"unsaved:{self.PEER_ID}"
        assert engine.open_workflows.sole_workflow_or_none() is None
        assert not engine.workflow_registry.is_open(self.PEER_ID)

    def test_node_code_on_a_worker_can_enter_a_node_context(self, engine: Engine) -> None:
        engine.context_manager.mirror_workflow_context(self.PEER_ID, "peer", None, None)
        flow = MagicMock(name="flow")
        node = MagicMock(name="node")

        with engine.context_manager.flow(flow), engine.context_manager.node(node):
            assert engine.context_manager.get_current_node() is node

        assert not engine.context_manager.has_current_flow()

    def test_a_worker_mirroring_a_workflow_opens_none(self, engine: Engine) -> None:
        engine.context_manager.mirror_workflow_context(self.PEER_ID, "peer", None, None)
        local = engine.workflow_registry.register_unsaved("local")

        with pytest.raises(engine.context_manager.WorkflowAlreadyOpenError):
            engine.context_manager.push_workflow(workflow_id=local.workflow_id)
        with pytest.raises(engine.context_manager.EmptyStackError):
            engine.context_manager.pop_workflow()

        assert engine.open_workflows.sole_workflow_or_none() is None
        assert engine.context_manager.get_current_workflow_id() == self.PEER_ID

    def test_an_engine_with_a_workflow_open_mirrors_none(self, engine: Engine) -> None:
        workflow = push_unsaved_workflow(engine)

        with pytest.raises(engine.context_manager.WorkflowContextError, match="a workflow is open"):
            engine.context_manager.mirror_workflow_context(self.PEER_ID, "peer", None, None)

        assert engine.context_manager.get_current_workflow_id() == workflow.workflow_id

    def test_an_open_workflow_and_a_mirror_at_once_is_never_read_as_either(self, engine: Engine) -> None:
        push_unsaved_workflow(engine)
        engine.context_manager._mirror = MirroredWorkflowContext(self.PEER_ID)

        with pytest.raises(engine.context_manager.WorkflowContextError, match="mirrored"):
            engine.context_manager.has_current_workflow()
        with pytest.raises(engine.context_manager.WorkflowContextError, match="mirrored"):
            engine.context_manager.has_current_flow()


class TestContextOnTheRuntime:
    def test_a_flow_entered_through_the_front_is_on_the_open_workflows_context(self, engine: Engine) -> None:
        push_unsaved_workflow(engine)
        flow = MagicMock(name="flow")

        with engine.context_manager.flow(flow):
            assert engine.open_workflows.sole_workflow().context.get_current_flow() is flow
            assert engine.context_manager.get_current_flow() is flow

        assert not engine.open_workflows.sole_workflow().context.has_current_flow()

    def test_the_context_does_not_survive_closing_the_workflow(self, engine: Engine) -> None:
        workflow = push_unsaved_workflow(engine)
        engine.context_manager.push_flow(MagicMock(name="flow"))

        engine.context_manager.pop_workflow()
        engine.context_manager.push_workflow(workflow_id=workflow.workflow_id)

        assert not engine.context_manager.has_current_flow()

    def test_reads_find_no_flow_with_no_workflow_open(self, engine: Engine) -> None:
        assert not engine.context_manager.has_current_flow()
        assert not engine.context_manager.has_current_node()
        with pytest.raises(engine.context_manager.NoActiveFlowError):
            engine.context_manager.get_current_flow()
        with pytest.raises(engine.context_manager.NoActiveWorkflowError):
            engine.context_manager.flow(MagicMock(name="flow"))


class TestCloseWorkflowState:
    @pytest.mark.asyncio
    async def test_cancels_the_running_flow_before_tearing_down(self, engine: Engine) -> None:
        push_unsaved_workflow(engine)
        calls: list[str] = []
        tear_down_open_workflow = engine._tear_down_open_workflow

        def record_teardown(workflow: WorkflowRuntime) -> None:
            calls.append("teardown")
            tear_down_open_workflow(workflow)

        with (
            patch.object(
                engine.flow_manager,
                "cancel_running_flow_before_teardown",
                AsyncMock(side_effect=lambda _workflow: calls.append("cancel")),
            ),
            patch.object(engine, "_tear_down_open_workflow", side_effect=record_teardown),
            patch.object(
                engine.worker_manager,
                "broadcast_local_object_teardown",
                AsyncMock(side_effect=lambda: calls.append("broadcast")),
            ),
        ):
            await engine.close_workflow_state(engine.open_workflows.sole_workflow())

        assert calls == ["cancel", "teardown", "broadcast"]

    @pytest.mark.asyncio
    async def test_leaves_no_workflow_open(self, engine: Engine) -> None:
        push_unsaved_workflow(engine)

        await engine.close_workflow_state(engine.open_workflows.sole_workflow())

        assert not engine.context_manager.has_current_workflow()
        assert engine.open_workflows.sole_workflow_or_none() is None

    @pytest.mark.asyncio
    async def test_tells_the_workers_even_when_the_teardown_raises(self, engine: Engine) -> None:
        push_unsaved_workflow(engine)

        with (
            patch.object(engine, "_tear_down_open_workflow", side_effect=RuntimeError("teardown blew up")),
            patch.object(engine.worker_manager, "broadcast_local_object_teardown", AsyncMock()) as broadcast,
            pytest.raises(RuntimeError, match="teardown blew up"),
        ):
            await engine.close_workflow_state(engine.open_workflows.sole_workflow())

        broadcast.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_workflow_opened_while_the_cancel_is_awaited_is_left_alone(self, engine: Engine) -> None:
        """Delete awaits the cancel, so the editor can clear and open another workflow meanwhile."""
        first = push_unsaved_workflow(engine, "first")
        cancel_calls: list[None] = []
        opened: dict[str, WorkflowRuntime] = {}

        async def clear_and_open_another(_workflow: WorkflowRuntime) -> None:
            cancel_calls.append(None)
            # ClearAll below awaits this same cancel; only the first call interleaves.
            if len(cancel_calls) > 1:
                return
            await asyncio.sleep(0)
            await engine.ahandle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
            push_unsaved_workflow(engine, "second")
            engine.handle_request(CreateFlowRequest(parent_flow_name=None, flow_name="canvas"))
            opened["second"] = engine.open_workflows.sole_workflow()

        with patch.object(
            engine.flow_manager, "cancel_running_flow_before_teardown", side_effect=clear_and_open_another
        ):
            result = await engine.ahandle_request(DeleteWorkflowRequest(workflow_id=first.workflow_id))

        assert result.succeeded()
        assert engine.open_workflows.sole_workflow() is opened["second"]
        assert engine.object_manager.has_object_with_name("canvas")

    @pytest.mark.asyncio
    async def test_a_workflow_closed_while_the_cancel_is_awaited_does_not_tell_the_workers(
        self, engine: Engine
    ) -> None:
        push_unsaved_workflow(engine)
        runtime = engine.open_workflows.sole_workflow()

        async def close_meanwhile(_workflow: WorkflowRuntime) -> None:
            engine.context_manager.pop_workflow()

        with (
            patch.object(engine.flow_manager, "cancel_running_flow_before_teardown", side_effect=close_meanwhile),
            patch.object(engine.worker_manager, "broadcast_local_object_teardown", AsyncMock()) as broadcast,
        ):
            await engine.close_workflow_state(runtime)

        broadcast.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_refuses_a_workflow_that_is_not_open(self, engine: Engine) -> None:
        push_unsaved_workflow(engine)

        with pytest.raises(OpenWorkflowError, match="not the open workflow"):
            await engine.close_workflow_state(WorkflowRuntime("stale", MagicMock()))

        assert engine.context_manager.has_current_workflow()

    def test_clear_all_object_state_closes_the_open_workflow_through_it(self, engine: Engine) -> None:
        push_unsaved_workflow(engine)
        runtime = engine.open_workflows.sole_workflow()

        with patch.object(engine, "close_workflow_state", wraps=engine.close_workflow_state) as close:
            result = engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))

        assert result.succeeded()
        close.assert_awaited_once_with(runtime)
        assert engine.open_workflows.sole_workflow_or_none() is None

    def test_clear_all_object_state_with_nothing_open_still_tells_the_workers(self, engine: Engine) -> None:
        with (
            patch.object(engine, "close_workflow_state", AsyncMock()) as close,
            patch.object(engine.worker_manager, "broadcast_local_object_teardown", AsyncMock()) as broadcast,
        ):
            result = engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))

        assert result.succeeded()
        close.assert_not_awaited()
        broadcast.assert_awaited_once()

    def test_clear_all_object_state_on_a_worker_clears_its_mirror_and_releases_what_it_holds(
        self, engine: Engine
    ) -> None:
        """A worker reloading its libraries clears all state with only a mirror on its stack."""
        engine.context_manager.mirror_workflow_context("00000000000000000000000000000abc", "peer", None, None)
        key = engine.resource_manager.put_local_object(object(), owner="Lib A", source="Builder", key="held")

        with patch.object(engine.worker_manager, "broadcast_local_object_teardown", AsyncMock()) as broadcast:
            result = engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))

        assert result.succeeded()
        assert not engine.context_manager.has_current_workflow()
        assert engine.workflow_registry.find_by_id("00000000000000000000000000000abc") is None
        assert engine.resource_manager.get_local_object(key, owner="Lib A") is None
        assert engine.open_workflows.sole_workflow_or_none() is None
        broadcast.assert_awaited_once()

    def test_deleting_the_open_workflow_closes_it_through_it(self, engine: Engine) -> None:
        workflow = push_unsaved_workflow(engine)
        runtime = engine.open_workflows.sole_workflow()

        with patch.object(engine, "close_workflow_state", wraps=engine.close_workflow_state) as close:
            result = engine.handle_request(DeleteWorkflowRequest(workflow_id=workflow.workflow_id))

        assert result.succeeded()
        close.assert_awaited_once_with(runtime)
        assert engine.open_workflows.sole_workflow_or_none() is None

    def test_deleting_the_open_workflow_while_it_runs_awaits_the_cancel_before_tearing_down(
        self, engine: Engine
    ) -> None:
        workflow = push_unsaved_workflow(engine)
        engine.handle_request(CreateFlowRequest(parent_flow_name=None, flow_name="canvas"))
        flow_manager = engine.flow_manager
        calls: list[str] = []
        running = [True]
        tear_down_open_workflow = engine._tear_down_open_workflow

        async def cancel_flow_run(_workflow: object) -> None:
            await asyncio.sleep(0)
            calls.append("cancel")
            running[0] = False

        def record_teardown(workflow: WorkflowRuntime) -> None:
            calls.append("teardown")
            tear_down_open_workflow(workflow)

        with (
            patch.object(flow_manager, "check_for_existing_running_flow_in", side_effect=lambda _workflow: running[0]),
            patch.object(flow_manager, "cancel_flow_run", side_effect=cancel_flow_run),
            patch.object(engine, "_tear_down_open_workflow", side_effect=record_teardown),
        ):
            result = engine.handle_request(DeleteWorkflowRequest(workflow_id=workflow.workflow_id))

        assert result.succeeded()
        assert calls == ["cancel", "teardown"]
        assert engine.open_workflows.sole_workflow_or_none() is None

    def test_clearing_the_open_workflow_addresses_its_cancel_and_delete_requests_to_it(self, engine: Engine) -> None:
        workflow = push_unsaved_workflow(engine)
        engine.handle_request(CreateFlowRequest(parent_flow_name=None, flow_name="canvas"))
        flow_manager = engine.flow_manager

        with (
            patch.object(flow_manager, "check_for_existing_running_flow_in", return_value=True),
            patch.object(flow_manager, "cancel_flow_run"),
            patch.object(engine, "handle_request", wraps=engine.handle_request) as handle_request,
        ):
            engine.clear_current_workflow_data()

        sent = {type(call.args[0]): call.kwargs.get("workflow_id") for call in handle_request.call_args_list}
        assert sent[CancelFlowRequest] == workflow.workflow_id
        assert sent[DeleteFlowRequest] == workflow.workflow_id
        assert engine.open_workflows.sole_workflow_or_none() is None

    def test_deleting_another_workflow_leaves_the_open_one_alone(self, engine: Engine) -> None:
        other = engine.workflow_registry.register_unsaved("other", None)
        push_unsaved_workflow(engine)
        runtime = engine.open_workflows.sole_workflow()

        with patch.object(engine, "close_workflow_state", AsyncMock()) as close:
            result = engine.handle_request(DeleteWorkflowRequest(workflow_id=other.workflow_id))

        assert result.succeeded()
        close.assert_not_awaited()
        assert engine.open_workflows.sole_workflow() is runtime


class TestObjectsOnTheRuntime:
    def test_a_created_flow_is_held_by_the_open_workflow(self, engine: Engine) -> None:
        push_unsaved_workflow(engine)

        result = engine.handle_request(CreateFlowRequest(parent_flow_name=None, flow_name="canvas"))

        assert isinstance(result, CreateFlowResultSuccess)
        assert engine.open_workflows.sole_workflow().objects.has("canvas")
        assert engine.object_manager.has_object_with_name("canvas")

    def test_lookups_find_nothing_with_no_workflow_open(self, engine: Engine) -> None:
        object_manager = engine.object_manager

        assert object_manager.get_filtered_subset() == {}
        assert object_manager.attempt_get_object_by_name("canvas") is None
        assert object_manager.attempt_get_object_by_name_as_type("canvas", ControlFlow) is None
        assert not object_manager.has_object_with_name("canvas")
        assert object_manager.generate_name_for_object("ControlFlow") == "ControlFlow_1"
        with pytest.raises(KeyError):
            object_manager.get_object_by_name("canvas")
        assert engine.open_workflows.sole_workflow_or_none() is None

    def test_changes_need_an_open_workflow(self, engine: Engine) -> None:
        object_manager = engine.object_manager

        with pytest.raises(NoOpenWorkflowError):
            object_manager.add_object_by_name("thing", object())
        with pytest.raises(NoOpenWorkflowError):
            object_manager.del_obj_by_name("thing")

    def test_create_flow_with_no_workflow_open_is_refused_before_it_needs_one(self, engine: Engine) -> None:
        result = engine.handle_request(CreateFlowRequest(parent_flow_name=None, flow_name="canvas"))

        assert result.failed()
        assert "names no workflow and none is open" in str(result.result_details)

    def test_rename_moves_the_object_within_the_open_workflow(self, engine: Engine) -> None:
        push_unsaved_workflow(engine)
        engine.handle_request(CreateFlowRequest(parent_flow_name=None, flow_name="canvas"))

        result = engine.handle_request(RenameObjectRequest(object_name="canvas", requested_name="renamed"))

        assert isinstance(result, RenameObjectResultSuccess)
        objects = engine.open_workflows.sole_workflow().objects
        assert objects.has("renamed")
        assert not objects.has("canvas")

    @pytest.mark.asyncio
    async def test_closing_drops_the_objects(self, engine: Engine) -> None:
        push_unsaved_workflow(engine)
        engine.handle_request(CreateFlowRequest(parent_flow_name=None, flow_name="canvas"))
        runtime = engine.open_workflows.sole_workflow()

        await engine.close_workflow_state(runtime)

        assert len(runtime.objects) == 0
        push_unsaved_workflow(engine)
        assert not engine.object_manager.has_object_with_name("canvas")

    def test_popping_the_last_entry_drops_objects_left_in_it(self, engine: Engine) -> None:
        push_unsaved_workflow(engine)
        engine.handle_request(CreateFlowRequest(parent_flow_name=None, flow_name="canvas"))

        engine.context_manager.pop_workflow()
        push_unsaved_workflow(engine)

        assert not engine.object_manager.has_object_with_name("canvas")


class TestVariablesOnTheRuntime:
    def test_a_global_variable_is_held_by_the_open_workflow(self, engine: Engine) -> None:
        push_unsaved_workflow(engine)

        result = engine.handle_request(CreateVariableRequest(name="greeting", type="str", is_global=True, value="hi"))

        assert isinstance(result, CreateVariableResultSuccess)
        assert engine.open_workflows.sole_workflow().variables.global_layer.has("greeting")

    def test_a_flow_variable_is_held_by_the_open_workflow(self, engine: Engine) -> None:
        push_unsaved_workflow(engine)
        engine.handle_request(CreateFlowRequest(parent_flow_name=None, flow_name="canvas"))

        result = engine.handle_request(
            CreateVariableRequest(name="greeting", type="str", owning_flow="canvas", value="hi")
        )

        assert isinstance(result, CreateVariableResultSuccess)
        layer = engine.open_workflows.sole_workflow().variables.flow_layer("canvas")
        assert layer is not None
        assert layer.has("greeting")

    def test_lookups_with_no_workflow_open_fail(self, engine: Engine) -> None:
        result = engine.handle_request(GetVariableValueRequest(name="greeting", lookup_scope=VariableScope.GLOBAL_ONLY))

        assert result.failed()
        assert "names no workflow and none is open" in str(result.result_details)
        assert engine.open_workflows.sole_workflow_or_none() is None

    def test_creating_a_global_variable_needs_an_open_workflow(self, engine: Engine) -> None:
        result = engine.handle_request(CreateVariableRequest(name="greeting", type="str", is_global=True, value="hi"))

        assert result.failed()
        assert "names no workflow and none is open" in str(result.result_details)
        assert engine.open_workflows.sole_workflow_or_none() is None

    def test_variables_do_not_survive_clearing_and_reopening(self, engine: Engine) -> None:
        push_unsaved_workflow(engine)
        engine.handle_request(CreateVariableRequest(name="greeting", type="str", is_global=True, value="hi"))

        engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
        push_unsaved_workflow(engine)

        result = engine.handle_request(GetVariableValueRequest(name="greeting", lookup_scope=VariableScope.GLOBAL_ONLY))
        assert isinstance(result, GetVariableValueResultFailure)

    def test_popping_the_last_entry_drops_the_variables(self, engine: Engine) -> None:
        push_unsaved_workflow(engine)
        engine.handle_request(CreateVariableRequest(name="greeting", type="str", is_global=True, value="hi"))

        engine.context_manager.pop_workflow()
        push_unsaved_workflow(engine)

        assert not engine.open_workflows.sole_workflow().variables.global_layer.has("greeting")


class TestSubstitutionFlagOnTheRuntime:
    def test_substitution_is_enabled_with_no_workflow_open(self, engine: Engine) -> None:
        assert engine.workflow_manager.variable_substitution.is_enabled() is True

    def test_setting_the_flag_with_no_workflow_open_fails(self, engine: Engine) -> None:
        result = engine.handle_request(SetVariableSubstitutionEnabledRequest(enabled=False))

        assert result.failed()
        assert "names no workflow and none is open" in str(result.result_details)

    def test_the_flag_is_held_by_the_open_workflow(self, engine: Engine) -> None:
        push_unsaved_workflow(engine)

        engine.handle_request(SetVariableSubstitutionEnabledRequest(enabled=False))

        assert engine.open_workflows.sole_workflow().variables.substitution_enabled is False
        assert engine.workflow_manager.variable_substitution.is_enabled() is False

    def test_the_flag_does_not_survive_clearing_and_reopening_the_same_workflow(self, engine: Engine) -> None:
        workflow = push_unsaved_workflow(engine)
        engine.handle_request(SetVariableSubstitutionEnabledRequest(enabled=False))

        engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
        engine.context_manager.push_workflow(workflow_id=workflow.workflow_id)

        assert engine.workflow_manager.variable_substitution.is_enabled() is True


def _create_canvas(engine: Engine) -> str:
    result = engine.handle_request(CreateFlowRequest(parent_flow_name=None, flow_name="canvas"))
    assert isinstance(result, CreateFlowResultSuccess)
    return result.flow_name


class TestNodesOnTheRuntime:
    def test_a_nodes_parent_flow_is_held_by_the_open_workflow(self, engine: Engine) -> None:
        push_unsaved_workflow(engine)
        engine.open_workflows.sole_workflow().nodes.name_to_parent_flow_name["node"] = "canvas"

        assert engine.node_manager.get_node_parent_flow_by_name("node") == "canvas"

    def test_lookups_find_no_node_with_no_workflow_open(self, engine: Engine) -> None:
        with pytest.raises(KeyError, match="could not be found"):
            engine.node_manager.get_node_parent_flow_by_name("node")

    @pytest.mark.asyncio
    async def test_cancelling_a_worker_run_with_no_workflow_open_does_nothing(self, engine: Engine) -> None:
        with patch.object(engine.worker_manager, "forward_event_to_worker", AsyncMock()) as forward:
            await engine.node_manager.cancel_worker_execution("node")

        forward.assert_not_awaited()

    def test_renaming_a_flow_repoints_its_nodes(self, engine: Engine) -> None:
        push_unsaved_workflow(engine)
        nodes = engine.open_workflows.sole_workflow().nodes
        nodes.name_to_parent_flow_name["node"] = "canvas"

        engine.node_manager.handle_flow_rename(engine.open_workflows.sole_workflow(), "canvas", "renamed")

        assert nodes.name_to_parent_flow_name == {"node": "renamed"}

    @pytest.mark.asyncio
    async def test_a_worker_run_is_forgotten_by_its_workflow_even_if_it_closes_meanwhile(self, engine: Engine) -> None:
        push_unsaved_workflow(engine)
        runtime = engine.open_workflows.sole_workflow()
        tracked_while_running: list[str] = []

        async def close_while_running(*_: object) -> dict:
            tracked_while_running.extend(runtime.nodes.orch_worker_requests)
            engine.context_manager.pop_workflow()
            msg = "The worker went away."
            raise WorkerGoneError(msg)

        worker_manager = MagicMock()
        worker_manager.route_to_worker = AsyncMock(side_effect=close_while_running)
        result = await engine.node_manager._execute_node_via_worker(
            runtime, ExecuteNodeRequest(node_name="node", parameter_values={}), worker_manager, ("worker", "topic")
        )

        assert isinstance(result, ExecuteNodeResultFailure)
        assert tracked_while_running == ["node"]
        assert runtime.nodes.orch_worker_requests == {}

    def test_popping_the_last_entry_drops_the_node_bookkeeping(self, engine: Engine) -> None:
        push_unsaved_workflow(engine)
        engine.open_workflows.sole_workflow().nodes.name_to_parent_flow_name["node"] = "canvas"

        engine.context_manager.pop_workflow()
        push_unsaved_workflow(engine)

        assert engine.open_workflows.sole_workflow().nodes.name_to_parent_flow_name == {}


class TestFlowsOnTheRuntime:
    def test_a_created_flows_parentage_is_held_by_the_open_workflow(self, engine: Engine) -> None:
        push_unsaved_workflow(engine)

        flow_name = _create_canvas(engine)

        assert engine.open_workflows.sole_workflow().flows.name_to_parent_name == {flow_name: None}
        assert engine.flow_manager.does_canvas_exist(engine.open_workflows.sole_workflow())

    def test_the_fronts_read_the_open_workflows_flows(self, engine: Engine) -> None:
        push_unsaved_workflow(engine)
        flows = engine.open_workflows.sole_workflow().flows
        flows.global_single_node_resolution = True

        flow_manager = engine.flow_manager
        assert flow_manager.get_connections() is flows.connections
        assert flow_manager.global_flow_queue is flows.global_flow_queue
        assert flow_manager.global_dag_builder is flows.global_dag_builder
        assert flow_manager.global_single_node_resolution is True

    def test_the_dag_builder_reads_its_workflows_connections(self, engine: Engine) -> None:
        push_unsaved_workflow(engine)
        flows = engine.open_workflows.sole_workflow().flows

        assert flows.global_dag_builder.connections is flows.connections

    def test_lookups_find_no_flows_and_no_run_with_no_workflow_open(self, engine: Engine) -> None:
        flow_manager = engine.flow_manager

        assert flow_manager.get_connections().connections == {}
        assert not flow_manager.check_for_existing_running_flow()
        assert not flow_manager.global_single_node_resolution
        assert flow_manager.global_flow_queue.empty()
        assert flow_manager.global_dag_builder.node_to_reference == {}
        assert flow_manager.get_next_node_from_execution_queue() is None
        with pytest.raises(ValueError, match="doesn't exist"):
            flow_manager.get_parent_flow("canvas")
        result = engine.handle_request(GetTopLevelFlowRequest())
        assert isinstance(result, GetTopLevelFlowResultSuccess)
        assert result.flow_name is None
        assert engine.open_workflows.sole_workflow_or_none() is None

    @pytest.mark.asyncio
    async def test_closing_drops_flows_connections_and_run_state(self, engine: Engine) -> None:
        workflow = push_unsaved_workflow(engine)
        _create_canvas(engine)
        runtime = engine.open_workflows.sole_workflow()
        runtime.flows.connections.connections[1] = MagicMock()
        runtime.flows.global_dag_builder.node_to_reference["node"] = MagicMock()
        runtime.flows.global_single_node_resolution = True

        await engine.close_workflow_state(runtime)
        engine.context_manager.push_workflow(workflow_id=workflow.workflow_id)

        reopened = engine.open_workflows.sole_workflow().flows
        assert reopened is not runtime.flows
        assert reopened.name_to_parent_name == {}
        assert reopened.connections.connections == {}
        assert reopened.global_control_flow_machine is None
        assert reopened.global_dag_builder.node_to_reference == {}
        assert not reopened.global_single_node_resolution
        assert not engine.flow_manager.does_canvas_exist(engine.open_workflows.sole_workflow())

    def test_a_bare_pop_of_the_last_entry_leaves_no_canvas_behind(self, engine: Engine) -> None:
        push_unsaved_workflow(engine)
        _create_canvas(engine)

        engine.context_manager.pop_workflow()
        push_unsaved_workflow(engine)

        assert not engine.flow_manager.does_canvas_exist(engine.open_workflows.sole_workflow())
        assert _create_canvas(engine) == "canvas"

    def test_a_run_in_one_workflow_is_not_seen_after_it_closes(self, engine: Engine) -> None:
        push_unsaved_workflow(engine)
        machine = MagicMock()
        machine.resolution_machine.is_complete.return_value = False
        machine.resolution_machine.is_started.return_value = True
        engine.open_workflows.sole_workflow().flows.global_control_flow_machine = machine
        assert engine.flow_manager.check_for_existing_running_flow()

        engine.context_manager.pop_workflow()

        assert not engine.flow_manager.check_for_existing_running_flow()
        push_unsaved_workflow(engine)
        assert not engine.flow_manager.check_for_existing_running_flow()
