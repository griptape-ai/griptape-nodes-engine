"""The open workflow's runtime: how it opens and closes, and the state it holds."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest

from griptape_nodes.common.directed_graph import DirectedGraph
from griptape_nodes.machines.parallel_resolution import ExecuteDagState, ParallelResolutionContext
from griptape_nodes.retained_mode.events.base_events import RequestPayload, ResultPayloadSuccess
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
    ListFlowsInFlowResultSuccess,
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
    SetVariableSubstitutionEnabledResultFailure,
)
from griptape_nodes.retained_mode.managers import flow_manager as flow_manager_module
from griptape_nodes.retained_mode.managers.event_manager import ResultContext
from griptape_nodes.retained_mode.runtime.binding import NO_WORKFLOW, WorkflowNotOpenError
from griptape_nodes.retained_mode.runtime.open_workflow_set import NoOpenWorkflowError
from griptape_nodes.retained_mode.variable_types import VariableScope
from tests.unit.exe_types.mocks import MockNode

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

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
class _RunSpec:
    """A queued run that executes `body`, standing in for a flow run."""

    flow_name: str
    body: Callable[[], Awaitable[ResultPayload]]

    async def execute(self, engine: Engine) -> ResultPayload:  # noqa: ARG002
        return await self.body()

    def failure(self, details: str) -> ResultPayload:
        return StartFlowResultFailure(validation_exceptions=[], result_details=details)


class TestRuntimeLifecycle:
    """The runtime is open exactly while the context stack holds a workflow."""

    def test_clear_all_object_state_closes_the_open_workflow(self, engine: Engine) -> None:
        engine.context_manager.push_workflow(workflow_name="outer")

        result = engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))

        assert result.succeeded()
        assert engine.current_workflow_or_none() is None

    def test_deleting_the_open_workflow_closes_it(self, engine: Engine) -> None:
        name = engine.context_manager.push_workflow(workflow_name="outer")

        result = engine.handle_request(DeleteWorkflowRequest(name=name))

        assert result.succeeded()
        assert engine.current_workflow_or_none() is None


class TestObjectsOnTheRuntime:
    def test_a_created_flow_is_held_by_the_open_workflow(self, engine: Engine) -> None:
        engine.context_manager.push_workflow(workflow_name="outer")

        result = engine.handle_request(CreateFlowRequest(parent_flow_name=None, flow_name="canvas"))

        assert isinstance(result, CreateFlowResultSuccess)
        assert engine.current_workflow().objects.has("canvas")
        assert engine.object_manager.has_object_with_name("canvas")

    def test_lookups_find_nothing_and_changes_are_refused_with_no_workflow_open(self, engine: Engine) -> None:
        object_manager = engine.object_manager

        assert object_manager.get_filtered_subset() == {}
        assert object_manager.attempt_get_object_by_name("canvas") is None
        assert object_manager.generate_name_for_object("ControlFlow") == "ControlFlow_1"
        with pytest.raises(KeyError):
            object_manager.get_object_by_name("canvas")
        with pytest.raises(NoOpenWorkflowError):
            object_manager.add_object_by_name("thing", object())
        with pytest.raises(NoOpenWorkflowError):
            object_manager.del_obj_by_name("thing")
        assert engine.current_workflow_or_none() is None

    def test_work_bound_to_no_workflow_finds_no_objects_and_cannot_add_one(self, engine: Engine) -> None:
        engine.context_manager.push_workflow(workflow_name="outer")
        engine.handle_request(CreateFlowRequest(parent_flow_name=None, flow_name="canvas"))
        object_manager = engine.object_manager

        with engine.bind_workflow(NO_WORKFLOW):
            assert not object_manager.has_object_with_name("canvas")
            with pytest.raises(NoOpenWorkflowError):
                object_manager.add_object_by_name("thing", object())

        assert object_manager.has_object_with_name("canvas")
        assert not object_manager.has_object_with_name("thing")

    def test_rename_moves_the_object_within_the_open_workflow(self, engine: Engine) -> None:
        engine.context_manager.push_workflow(workflow_name="outer")
        engine.handle_request(CreateFlowRequest(parent_flow_name=None, flow_name="canvas"))

        result = engine.handle_request(RenameObjectRequest(object_name="canvas", requested_name="renamed"))

        assert isinstance(result, RenameObjectResultSuccess)
        objects = engine.current_workflow().objects
        assert objects.has("renamed")
        assert not objects.has("canvas")

    def test_popping_the_last_entry_drops_objects_left_in_it(self, engine: Engine) -> None:
        engine.context_manager.push_workflow(workflow_name="outer")
        engine.handle_request(CreateFlowRequest(parent_flow_name=None, flow_name="canvas"))

        engine.context_manager.pop_workflow()
        engine.context_manager.push_workflow(workflow_name="next")

        assert not engine.object_manager.has_object_with_name("canvas")


class TestVariablesOnTheRuntime:
    def test_global_and_flow_variables_are_held_by_the_open_workflow(self, engine: Engine) -> None:
        engine.context_manager.push_workflow(workflow_name="outer")
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

        assert not result.succeeded()
        assert "no workflow is open" in str(result.result_details)
        lookup = engine.handle_request(GetVariableValueRequest(name="greeting", lookup_scope=VariableScope.GLOBAL_ONLY))
        assert isinstance(lookup, GetVariableValueResultFailure)

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

        assert isinstance(lookup, GetVariableValueResultFailure)
        assert not created.succeeded()
        assert not engine.current_workflow().variables.global_layer.has("farewell")

    def test_variables_do_not_survive_clearing_and_reopening(self, engine: Engine) -> None:
        engine.context_manager.push_workflow(workflow_name="outer")
        engine.handle_request(CreateVariableRequest(name="greeting", type="str", is_global=True, value="hi"))

        engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
        engine.context_manager.push_workflow(workflow_name="next")

        result = engine.handle_request(GetVariableValueRequest(name="greeting", lookup_scope=VariableScope.GLOBAL_ONLY))
        assert isinstance(result, GetVariableValueResultFailure)


class TestSubstitutionFlagOnTheRuntime:
    def test_with_no_workflow_open_the_flag_reads_enabled_and_cannot_be_set(self, engine: Engine) -> None:
        result = engine.handle_request(SetVariableSubstitutionEnabledRequest(enabled=False))

        assert isinstance(result, SetVariableSubstitutionEnabledResultFailure)
        assert engine.workflow_manager.variable_substitution.is_enabled() is True

    def test_the_flag_is_held_by_the_open_workflow(self, engine: Engine) -> None:
        engine.context_manager.push_workflow(workflow_name="outer")

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
        engine.context_manager.push_workflow(workflow_name="outer")
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
        engine.context_manager.push_workflow(workflow_name="outer")

        reopened = engine.current_workflow()
        assert reopened.nodes.name_to_parent_flow_name == {}
        assert reopened.flows.name_to_parent_name == {}
        assert not engine.flow_manager.global_single_node_resolution
        assert not engine.flow_manager.does_canvas_exist()

    def test_lookups_find_no_node_or_run_with_no_workflow_open(self, engine: Engine) -> None:
        with pytest.raises(KeyError, match="could not be found"):
            engine.node_manager.get_node_parent_flow_by_name("node")
        assert not engine.flow_manager.does_canvas_exist()
        assert not engine.flow_manager.check_for_existing_running_flow()
        assert not engine.flow_manager.execution_is_advancing()

    def test_listing_the_canvas_flows_with_no_workflow_open_finds_none(self, engine: Engine) -> None:
        result = engine.handle_request(ListFlowsInFlowRequest(parent_flow_name=None))

        assert isinstance(result, ListFlowsInFlowResultSuccess), result.result_details
        assert result.flow_names == []

    @pytest.mark.asyncio
    async def test_runs_and_resets_are_refused_with_no_workflow_open(self, engine: Engine) -> None:
        with pytest.raises(NoOpenWorkflowError):
            await engine.flow_manager.start_flow(MagicMock())
        with pytest.raises(NoOpenWorkflowError):
            await engine.flow_manager.cancel_flow_run()
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
        run = asyncio.create_task(engine.flow_manager.start_flow(MagicMock(), MagicMock()))
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
            ExecuteNodeRequest(node_name="node", parameter_values={}), worker_manager, ("worker", "topic")
        )

        assert isinstance(result, ExecuteNodeResultFailure)
        assert tracked_while_running == ["node"]
        assert runtime.nodes.orch_worker_requests == {}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("addressed", [True, False], ids=["with_envelope_id", "without_envelope_id"])
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

        engine.event_manager.assign_manager_to_request_type(_StartRunRequest, start_run)
        workflow_id = None
        if addressed:
            workflow_id = first.workflow_id

        await engine.event_manager.ahandle_request(
            _StartRunRequest(), result_context=ResultContext(open_workflow_id=workflow_id)
        )
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
        first.flows.global_single_node_resolution = True
        graph = DirectedGraph()
        graph.add_node("other")
        first.flows.global_dag_builder.graphs["network"] = graph
        context = ParallelResolutionContext(
            "canvas", dag_builder=first.flows.global_dag_builder, flows=first.flows, engine=engine
        )
        engine.context_manager.pop_workflow()
        if next_workflow_opens:
            engine.context_manager.push_workflow(workflow_name="second")

        # A single-node run stops after its node, whichever workflow is current when it resumes.
        assert ExecuteDagState._should_skip_control_flow(context, MockNode(name="node"), "network")

        first.flows.global_single_node_resolution = False
        assert not ExecuteDagState._should_skip_control_flow(context, MockNode(name="node"), "network")
