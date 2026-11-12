"""The open workflow's runtime: how it opens and closes, and the state it holds."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from griptape_nodes.common.directed_graph import DirectedGraph
from griptape_nodes.exe_types.core_types import Parameter
from griptape_nodes.machines.parallel_resolution import ExecuteDagState, ParallelResolutionContext
from griptape_nodes.retained_mode.events.base_events import RequestPayload, RequestSender, ResultPayloadSuccess
from griptape_nodes.retained_mode.events.execution_events import (
    ExecuteNodeRequest,
    ExecuteNodeResultFailure,
    StartFlowResultFailure,
    StartFlowResultSuccess,
)
from griptape_nodes.retained_mode.events.flow_events import (
    CreateFlowRequest,
    CreateFlowResultSuccess,
    ListFlowsInFlowRequest,
)
from griptape_nodes.retained_mode.events.generic_events import WorkflowNotNamedResultFailure
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
from griptape_nodes.retained_mode.managers import flow_manager as flow_manager_module
from griptape_nodes.retained_mode.managers.event_manager import ResultContext
from griptape_nodes.retained_mode.managers.run_queue import RunSpec
from griptape_nodes.retained_mode.request_handlers import RequiredSender
from griptape_nodes.retained_mode.runtime.binding import NO_WORKFLOW, WorkflowNotOpenError, bound_workflow
from griptape_nodes.retained_mode.runtime.open_workflow_set import NoOpenWorkflowError
from griptape_nodes.retained_mode.variable_types import VariableScope
from tests.unit.exe_types.mocks import MockNode
from tests.workflow_context import open_and_bind

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.events.base_events import ResultPayload


def _live_machine() -> MagicMock:
    """A control flow machine that reports a run in progress."""
    machine = MagicMock()
    machine.current_state = MagicMock()  # Truthy and not CompleteState: a run in progress.
    machine.resolution_machine.is_complete.return_value = False
    machine.resolution_machine.is_started.return_value = True
    machine.cancel_flow = AsyncMock()
    return machine


@dataclass(kw_only=True)
class _StartRunRequest(RequestPayload):
    """Starts a task that reads the run state after a wait, as a flow run does."""


@dataclass
class _RunSpec(RunSpec):
    """A queued run that executes `body`, standing in for a flow run."""

    flow_name: str
    body: Callable[[], Awaitable[ResultPayload]]

    async def execute(self, engine: Engine) -> ResultPayload:  # noqa: ARG002
        return await self.body()

    def failure(self, details: str) -> ResultPayload:
        return StartFlowResultFailure(validation_exceptions=[], result_details=details)


@dataclass(kw_only=True)
class _ReplaceWorkflowRequest(RequestPayload):
    """Closes the open workflow and opens another, as a load does."""


class TestRuntimeLifecycle:
    """The runtime is open exactly while a workflow is in context."""

    def test_a_push_onto_an_empty_stack_opens_the_pushed_workflow(self, engine: Engine) -> None:
        engine.context_manager.push_workflow(workflow_name="outer")

        workflow = engine.current_workflow()
        assert workflow.workflow_id == engine.context_manager.get_current_workflow_id()
        assert len(workflow.objects) == 0

    def test_a_push_bound_to_an_open_workflow_is_refused(self, engine: Engine, tmp_path: Path) -> None:
        engine.context_manager.push_workflow(workflow_name="outer")
        runtime = engine.current_workflow()
        unregistered_file = tmp_path / "never_registered.py"
        unregistered_file.write_text("# stub")

        with engine.bind_workflow(runtime):
            with pytest.raises(engine.context_manager.WorkflowAlreadyOpenError, match="is already open"):
                engine.context_manager.push_workflow(workflow_name="inner")
            with pytest.raises(engine.context_manager.WorkflowAlreadyOpenError):
                engine.context_manager.push_workflow(file_path=str(unregistered_file))

        assert engine.open_workflows.all_open() == [runtime]
        # Refused before anything is registered.
        assert engine.workflow_registry.find_by_name("inner") is None
        assert engine.workflow_registry.find_by_path(str(unregistered_file)) is None

    def test_popping_the_workflow_closes_it_with_its_context(self, engine: Engine) -> None:
        engine.context_manager.push_workflow(workflow_name="outer")
        flow = MagicMock(name="flow")
        engine.context_manager.push_flow(flow)
        assert engine.current_workflow().context.get_current_flow() is flow

        name = engine.context_manager.pop_workflow()
        engine.context_manager.push_workflow(workflow_name=name)

        assert not engine.context_manager.has_current_flow()

    def test_the_workflow_context_is_the_engines_current_workflow(self, engine: Engine) -> None:
        engine.context_manager.push_workflow(workflow_name="outer")

        with engine.bind_workflow(NO_WORKFLOW):
            assert not engine.context_manager.has_current_workflow()
        assert engine.context_manager.has_current_workflow()

    def test_closing_the_bound_workflow_refuses_the_rest_of_the_work_rather_than_moving_it(
        self, engine: Engine
    ) -> None:
        engine.context_manager.push_workflow(workflow_name="other")
        other = engine.current_workflow()
        closed = engine.open_workflows.open(
            engine.workflow_registry.register_unsaved("closed", listed=False).workflow_id
        )

        with engine.bind_workflow(closed):
            engine.context_manager.pop_workflow()
            with pytest.raises(WorkflowNotOpenError):
                engine.current_workflow_or_none()

        assert engine.open_workflows.all_open() == [other]

    def test_a_request_that_opens_a_workflow_stays_bound_as_dispatch_bound_it(self, engine: Engine) -> None:
        engine.context_manager.open_workflow("other")
        bindings: list[object] = []

        def open_workflow(_request: _ReplaceWorkflowRequest) -> ResultPayloadSuccess:
            engine.context_manager.push_workflow(workflow_name="opened")
            bindings.append(bound_workflow.get())
            return ResultPayloadSuccess(result_details="ok")

        # A request from another engine starts bound to no workflow, so it can open one beside "other".
        engine.event_manager.assign_manager_to_request_type(
            _ReplaceWorkflowRequest, open_workflow, sender=RequiredSender.ENGINE, without_workflow=None
        )

        engine.event_manager.handle_request(
            _ReplaceWorkflowRequest(), result_context=ResultContext(sender=RequestSender(engine_id="another-engine"))
        )

        assert len(engine.open_workflows.all_open()) == 2  # noqa: PLR2004
        assert bindings == [NO_WORKFLOW]
        assert bound_workflow.get() is None

    def test_clear_all_object_state_closes_the_open_workflow(self, engine: Engine) -> None:
        engine.context_manager.open_workflow("outer")

        result = engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))

        assert result.succeeded()
        assert engine.open_workflows.all_open() == []

    def test_deleting_the_open_workflow_closes_it(self, engine: Engine) -> None:
        opened = engine.context_manager.open_workflow("outer")

        result = engine.handle_request(
            DeleteWorkflowRequest(name=engine.workflow_registry.get(opened.workflow_id).name)
        )

        assert result.succeeded()
        assert engine.open_workflows.all_open() == []


class TestObjectsOnTheRuntime:
    def test_a_created_flow_is_held_by_the_open_workflow(self, engine: Engine) -> None:
        open_and_bind(engine, "outer")

        result = engine.handle_request(CreateFlowRequest(parent_flow_name=None, flow_name="canvas"))

        assert isinstance(result, CreateFlowResultSuccess)
        assert engine.current_workflow().objects.has("canvas")
        assert engine.object_manager.has_object_with_name("canvas")

    def test_lookups_find_nothing_with_no_workflow_open(self, engine: Engine) -> None:
        object_manager = engine.object_manager

        assert object_manager.get_filtered_subset() == {}
        assert object_manager.attempt_get_object_by_name("canvas") is None
        assert object_manager.generate_name_for_object("ControlFlow") == "ControlFlow_1"
        with pytest.raises(KeyError):
            object_manager.get_object_by_name("canvas")
        assert engine.current_workflow_or_none() is None

    def test_work_bound_to_no_workflow_finds_no_objects(self, engine: Engine) -> None:
        open_and_bind(engine, "outer")
        engine.handle_request(CreateFlowRequest(parent_flow_name=None, flow_name="canvas"))
        object_manager = engine.object_manager

        with engine.bind_workflow(NO_WORKFLOW):
            assert not object_manager.has_object_with_name("canvas")

        assert object_manager.has_object_with_name("canvas")

    def test_rename_moves_the_object_within_the_open_workflow(self, engine: Engine) -> None:
        open_and_bind(engine, "outer")
        engine.handle_request(CreateFlowRequest(parent_flow_name=None, flow_name="canvas"))

        result = engine.handle_request(RenameObjectRequest(object_name="canvas", requested_name="renamed"))

        assert isinstance(result, RenameObjectResultSuccess)
        objects = engine.current_workflow().objects
        assert objects.has("renamed")
        assert not objects.has("canvas")

    def test_popping_the_workflow_deletes_its_flows_before_closing_it(self, engine: Engine) -> None:
        engine.context_manager.push_workflow(workflow_name="outer")
        engine.handle_request(CreateFlowRequest(parent_flow_name=None, flow_name="canvas"))
        closed = engine.current_workflow()

        engine.context_manager.pop_workflow()
        engine.context_manager.push_workflow(workflow_name="next")

        assert len(closed.objects) == 0
        assert closed.flows.name_to_parent_name == {}
        assert not engine.object_manager.has_object_with_name("canvas")

    def test_a_teardown_that_fails_leaves_the_workflow_open(self, engine: Engine) -> None:
        engine.context_manager.push_workflow(workflow_name="outer")
        workflow = engine.current_workflow()

        with (
            patch.object(engine, "_delete_workflow_flows", side_effect=RuntimeError("delete blew up")),
            pytest.raises(RuntimeError, match="delete blew up"),
        ):
            engine.context_manager.pop_workflow()

        assert engine.open_workflows.all_open() == [workflow]

    def test_a_closed_workflows_queued_ui_changes_go_with_it(self, engine: Engine) -> None:
        engine.context_manager.push_workflow(workflow_name="outer")
        node = MockNode(name="node")
        parameter = Parameter(name="text", tooltip="Text", type="str")
        node.add_parameter(parameter)
        engine.object_manager.add_object_by_name_in(engine.current_workflow(), "node", node)
        parameter.track_change("tooltip", "Changed")
        # Closing refuses a workflow that still holds objects once its flows are deleted.
        engine.current_workflow().objects.remove("node")

        engine.context_manager.pop_workflow()
        engine.context_manager.push_workflow(workflow_name="next")

        assert engine.object_manager.take_nodes_with_queued_changes() == []

    def test_queued_ui_changes_are_taken_from_the_bound_workflow_or_every_open_one(self, engine: Engine) -> None:
        engine.context_manager.push_workflow(workflow_name="first")
        first = engine.current_workflow()
        second = engine.open_workflows.open(
            engine.workflow_registry.register_unsaved("second", listed=False).workflow_id
        )
        nodes = {}
        for workflow in (first, second):
            node = MockNode(name="node")
            parameter = Parameter(name="text", tooltip="Text", type="str")
            node.add_parameter(parameter)
            with engine.bind_workflow(workflow):
                engine.object_manager.add_object_by_name_in(engine.current_workflow(), "node", node)
            parameter.track_change("tooltip", "Changed")
            nodes[workflow] = node

        with engine.bind_workflow(second):
            taken_bound = engine.object_manager.take_nodes_with_queued_changes()
        taken_unbound = engine.object_manager.take_nodes_with_queued_changes()

        assert taken_bound == [nodes[second]]
        assert taken_unbound == [nodes[first]]


class TestVariablesOnTheRuntime:
    def test_global_and_flow_variables_are_held_by_the_open_workflow(self, engine: Engine) -> None:
        open_and_bind(engine, "outer")
        engine.handle_request(CreateFlowRequest(parent_flow_name=None, flow_name="canvas"))

        global_result = engine.handle_request(
            CreateVariableRequest(name="greeting", type="str", is_global=True, value="hi")
        )
        flow_result = engine.handle_request(
            CreateVariableRequest(name="farewell", type="str", owning_flow="canvas", value="bye")
        )

        assert isinstance(global_result, CreateVariableResultSuccess)
        assert isinstance(flow_result, CreateVariableResultSuccess)
        variables = engine.current_workflow().variables
        assert variables.global_layer.has("greeting")
        assert variables.flow_layers["canvas"].has("farewell")

    def test_creating_a_global_variable_needs_an_open_workflow(self, engine: Engine) -> None:
        result = engine.handle_request(CreateVariableRequest(name="greeting", type="str", is_global=True, value="hi"))

        assert isinstance(result, WorkflowNotNamedResultFailure)
        assert "names no workflow" in str(result.result_details)
        lookup = engine.handle_request(GetVariableValueRequest(name="greeting", lookup_scope=VariableScope.GLOBAL_ONLY))
        assert isinstance(lookup, WorkflowNotNamedResultFailure)

    def test_work_bound_to_no_workflow_reads_no_variables_and_cannot_create_one(self, engine: Engine) -> None:
        engine.context_manager.push_workflow(workflow_name="outer")
        engine.handle_request(CreateVariableRequest(name="greeting", type="str", is_global=True, value="hi"))

        with engine.bind_workflow(NO_WORKFLOW):
            lookup = engine.handle_request(
                GetVariableValueRequest(name="greeting", lookup_scope=VariableScope.GLOBAL_ONLY)
            )
            created = engine.handle_request(
                CreateVariableRequest(name="farewell", type="str", is_global=True, value="bye")
            )

        assert isinstance(lookup, WorkflowNotNamedResultFailure)
        assert not created.succeeded()
        assert not engine.current_workflow().variables.global_layer.has("farewell")

    def test_variables_do_not_survive_clearing_and_reopening(self, engine: Engine) -> None:
        open_and_bind(engine, "outer")
        engine.handle_request(CreateVariableRequest(name="greeting", type="str", is_global=True, value="hi"))

        engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
        open_and_bind(engine, "next")

        result = engine.handle_request(GetVariableValueRequest(name="greeting", lookup_scope=VariableScope.GLOBAL_ONLY))
        assert isinstance(result, GetVariableValueResultFailure)


class TestSubstitutionFlagOnTheRuntime:
    def test_with_no_workflow_open_the_flag_reads_enabled_and_cannot_be_set(self, engine: Engine) -> None:
        result = engine.handle_request(SetVariableSubstitutionEnabledRequest(enabled=False))

        assert isinstance(result, WorkflowNotNamedResultFailure)
        assert engine.workflow_manager.variable_substitution.is_enabled() is True

    def test_the_flag_is_held_by_the_open_workflow(self, engine: Engine) -> None:
        open_and_bind(engine, "outer")

        engine.handle_request(SetVariableSubstitutionEnabledRequest(enabled=False))

        assert engine.current_workflow().variables.substitution_enabled is False
        assert engine.workflow_manager.variable_substitution.is_enabled() is False

    def test_the_flag_does_not_survive_clearing_and_reopening_the_same_workflow(self, engine: Engine) -> None:
        name = engine.context_manager.push_workflow(workflow_name="outer")
        engine.handle_request(SetVariableSubstitutionEnabledRequest(enabled=False))

        engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
        engine.context_manager.push_workflow(workflow_name=name)

        assert engine.workflow_manager.variable_substitution.is_enabled() is True


class TestNodesAndFlowsOnTheRuntime:
    def test_node_and_flow_state_is_held_by_the_open_workflow_and_dropped_when_it_closes(self, engine: Engine) -> None:
        open_and_bind(engine, "outer")
        engine.handle_request(CreateFlowRequest(parent_flow_name=None, flow_name="canvas"))
        runtime = engine.current_workflow()
        runtime.nodes.name_to_parent_flow_name["node"] = "canvas"
        engine.flow_manager._global_single_node_resolution = True

        assert engine.node_manager.get_node_parent_flow_by_name("node") == "canvas"
        assert runtime.flows.name_to_parent_name == {"canvas": None}
        assert runtime.flows.global_single_node_resolution is True
        assert engine.flow_manager.get_connections() is runtime.flows.connections
        assert engine.flow_manager.global_dag_builder.connections is runtime.flows.connections

        engine.context_manager.pop_workflow()
        open_and_bind(engine, "outer")

        reopened = engine.current_workflow()
        assert reopened.nodes.name_to_parent_flow_name == {}
        assert reopened.flows.name_to_parent_name == {}
        assert not engine.flow_manager.global_single_node_resolution
        assert not engine.flow_manager.does_canvas_exist(reopened)

    def test_lookups_find_no_node_or_run_with_no_workflow_open(self, engine: Engine) -> None:
        with pytest.raises(KeyError, match="could not be found"):
            engine.node_manager.get_node_parent_flow_by_name("node")
        assert not engine.flow_manager.check_for_existing_running_flow()
        assert not engine.flow_manager.execution_is_advancing()

    def test_listing_the_canvas_flows_with_no_workflow_open_fails(self, engine: Engine) -> None:
        result = engine.handle_request(ListFlowsInFlowRequest(parent_flow_name=None))

        assert isinstance(result, WorkflowNotNamedResultFailure), result.result_details

    @pytest.mark.asyncio
    async def test_resets_are_refused_with_no_workflow_open(self, engine: Engine) -> None:
        with pytest.raises(NoOpenWorkflowError):
            engine.flow_manager.reset_global_execution_state()

    @pytest.mark.asyncio
    async def test_a_cancelled_run_waking_after_another_workflow_opens_leaves_that_workflows_run_alone(
        self, engine: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        engine.context_manager.push_workflow(workflow_name="first")
        first = engine.current_workflow()
        wake = asyncio.Event()
        parked_run = _live_machine()

        async def park_then_fail(*_: object, **__: object) -> None:
            await wake.wait()
            msg = "The cancelled node woke up and failed."
            raise RuntimeError(msg)

        parked_run.start_flow = AsyncMock(side_effect=park_then_fail)
        monkeypatch.setattr(flow_manager_module, "ControlFlowMachine", MagicMock(return_value=parked_run))
        run = asyncio.create_task(engine.flow_manager.start_flow(first, MagicMock(), MagicMock()))
        await asyncio.sleep(0)
        assert first.flows.global_control_flow_machine is parked_run

        # ClearAll cancels best-effort, so the parked driver outlives the workflow it ran in.
        result = await engine.ahandle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
        assert result.succeeded()
        engine.context_manager.push_workflow(workflow_name="second")
        second = engine.current_workflow()
        second_run = _live_machine()
        second.flows.global_control_flow_machine = second_run
        second.flows.global_single_node_resolution = True
        second.flows.global_dag_builder.node_to_reference["second_node"] = MagicMock()

        wake.set()
        with pytest.raises(RuntimeError, match="woke up and failed"):
            await asyncio.wait_for(run, timeout=5)

        assert second.flows.global_control_flow_machine is second_run
        second_run.cancel_flow.assert_not_awaited()
        second_run.reset_machine.assert_not_called()
        assert second.flows.global_single_node_resolution is True
        assert list(second.flows.global_dag_builder.node_to_reference) == ["second_node"]

    @pytest.mark.asyncio
    async def test_runs_wait_in_their_own_workflows_line_and_end_there_after_another_opens(
        self, engine: Engine
    ) -> None:
        engine.context_manager.push_workflow(workflow_name="first")
        first = engine.current_workflow()
        first_may_end = asyncio.Event()

        async def first_body() -> ResultPayload:
            await first_may_end.wait()
            return StartFlowResultSuccess(result_details="first ran")

        async def second_body() -> ResultPayload:
            return StartFlowResultSuccess(result_details="second ran")

        running = asyncio.ensure_future(engine.flow_manager.start_run(_RunSpec("flow", first_body)))
        await asyncio.sleep(0)
        waiting = engine.flow_manager.submit_run(_RunSpec("flow", second_body))

        assert first.flows.run_in_progress is not None
        assert first.flows.run_queue.snapshot() == [waiting]
        assert waiting.flows is first.flows

        engine.context_manager.pop_workflow()
        engine.context_manager.push_workflow(workflow_name="second")
        second = engine.current_workflow()
        assert second.flows.run_in_progress is None
        assert second.flows.run_queue.snapshot() == []

        first_may_end.set()
        await asyncio.wait_for(asyncio.gather(running, waiting.outcome), timeout=5)

        # The waiting run started and ended in its own workflow's line, never in the next one's.
        assert first.flows.run_in_progress is None
        assert first.flows.run_queue.snapshot() == []
        assert second.flows.run_in_progress is None

    @pytest.mark.asyncio
    async def test_a_worker_run_is_forgotten_by_its_workflow_even_if_it_closes_meanwhile(self, engine: Engine) -> None:
        engine.context_manager.push_workflow(workflow_name="outer")
        runtime = engine.current_workflow()
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

    @pytest.mark.asyncio
    @pytest.mark.parametrize("addressed", [True, False], ids=["with_envelope_id", "with_the_callers_binding"])
    async def test_a_run_reading_run_state_after_its_workflow_closed_never_reaches_the_next(
        self, engine: Engine, *, addressed: bool
    ) -> None:
        engine.context_manager.push_workflow(workflow_name="first")
        first = engine.current_workflow()
        release = asyncio.Event()
        runs: list[asyncio.Task[dict[str, object]]] = []

        async def read_run_state() -> dict[str, object]:
            await release.wait()
            flow_manager = engine.flow_manager
            return {
                "dag_builder": flow_manager.global_dag_builder,
                "flow_queue": flow_manager.global_flow_queue,
                "single_node_resolution": flow_manager.global_single_node_resolution,
            }

        async def start_run(_request: _StartRunRequest) -> ResultPayloadSuccess:
            runs.append(asyncio.create_task(read_run_state()))
            return ResultPayloadSuccess(result_details="ok")

        engine.event_manager.assign_manager_to_request_type(_StartRunRequest, start_run, without_workflow=None)
        if addressed:
            await engine.event_manager.ahandle_request(
                _StartRunRequest(), result_context=ResultContext(open_workflow_id=first.workflow_id)
            )
        else:
            with engine.bind_workflow(first):
                await engine.event_manager.ahandle_request(_StartRunRequest())
        engine.context_manager.pop_workflow()
        engine.context_manager.push_workflow(workflow_name="second")
        engine.current_workflow().flows.global_single_node_resolution = True
        release.set()

        # A run keeps the flows it captured when it started; through the binding it finds its
        # workflow closed rather than reading the next one's.
        with pytest.raises(WorkflowNotOpenError, match="was closed"):
            await asyncio.wait_for(runs[0], timeout=5)

    @pytest.mark.parametrize("next_workflow_opens", [True, False], ids=["another_opened", "none_open"])
    def test_a_run_resuming_after_its_workflow_closed_stops_by_its_own_single_node_rule(
        self, engine: Engine, *, next_workflow_opens: bool
    ) -> None:
        engine.context_manager.push_workflow(workflow_name="first")
        first = engine.current_workflow()
        graph = DirectedGraph()
        graph.add_node("other")
        first.flows.global_dag_builder.graphs["network"] = graph
        context = ParallelResolutionContext(
            "canvas", dag_builder=first.flows.global_dag_builder, workflow=first, engine=engine
        )
        engine.context_manager.pop_workflow()
        if next_workflow_opens:
            engine.context_manager.push_workflow(workflow_name="second")
        # Closing reset the run's state; the run sets its own rule, not the current workflow's.
        first.flows.global_single_node_resolution = True

        # A single-node run stops after its node, whichever workflow is current when it resumes.
        assert ExecuteDagState._should_skip_control_flow(context, MockNode(name="node"), "network")

        first.flows.global_single_node_resolution = False
        assert not ExecuteDagState._should_skip_control_flow(context, MockNode(name="node"), "network")


class TestSquelchWorkflowAltered:
    @pytest.mark.asyncio
    async def test_a_squelch_covers_its_own_task_and_not_another(self, engine: Engine) -> None:
        """A load reports its own edits as unaltered; an edit made meanwhile from another task still counts."""
        open_and_bind(engine, "outer")
        load_holds_squelch = asyncio.Event()
        other_edit_done = asyncio.Event()

        async def load() -> ResultPayload:
            with engine.workflow_manager.squelch_workflow_altered():
                load_holds_squelch.set()
                await other_edit_done.wait()
                return await engine.ahandle_request(CreateFlowRequest(parent_flow_name=None, flow_name="loaded"))

        async def edit_from_another_task() -> ResultPayload:
            await load_holds_squelch.wait()
            result = await engine.ahandle_request(CreateFlowRequest(parent_flow_name=None, flow_name="edited"))
            other_edit_done.set()
            return result

        loaded, edited = await asyncio.gather(load(), edit_from_another_task())

        assert isinstance(loaded, CreateFlowResultSuccess)
        assert isinstance(edited, CreateFlowResultSuccess)
        assert loaded.altered_workflow_state is False
        assert edited.altered_workflow_state is True
