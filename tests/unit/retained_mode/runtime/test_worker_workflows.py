"""The runtimes a worker keeps for its orchestrator's workflows, which it runs that workflow's nodes in."""

from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING, Any

import pytest

from griptape_nodes.retained_mode.events.parameter_events import GetParameterValueRequest
from griptape_nodes.retained_mode.runtime.binding import WorkflowNotOpenError

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.events.base_events import EventRequest
    from griptape_nodes.retained_mode.runtime.workflow_runtime import WorkflowRuntime

PEER_ID = "00000000000000000000000000000abc"
OTHER_PEER_ID = "00000000000000000000000000000def"


class _OrchestratorThatRecordsRequests:
    """Stands in for the worker's request client and keeps each request it is asked to forward."""

    def __init__(self) -> None:
        self.forwarded: list[EventRequest] = []

    async def request_to_orchestrator(self, *, event_request: EventRequest, **_: Any) -> dict:
        self.forwarded.append(event_request)
        msg = "recorded"
        raise RuntimeError(msg)


async def _forward_a_request(engine: Engine) -> EventRequest:
    orchestrator = _OrchestratorThatRecordsRequests()
    engine.event_manager.configure_worker_forwarding(
        request_client=orchestrator,  # type: ignore[arg-type]
        orchestrator_request_topic="orchestrator",
        worker_response_topic="worker",
        websocket_event_loop=asyncio.get_running_loop(),
    )
    with pytest.raises(RuntimeError, match="recorded"):
        await engine.event_manager.forward_to_orchestrator(
            GetParameterValueRequest(node_name="node", parameter_name="value"), {}
        )
    return orchestrator.forwarded[0]


class TestWorkerWorkflows:
    def test_a_workers_runtime_is_current_only_where_bound_and_never_open(self, engine: Engine) -> None:
        workflow = engine.context_manager.adopt_orchestrator_workflow(PEER_ID, None, None, None)

        assert engine.open_workflows.all_open() == []
        assert engine.workflow_registry.is_open(PEER_ID)
        # Unbound work on a worker finds no workflow; its node work is bound to the runtime.
        assert not engine.context_manager.has_current_workflow()
        assert engine.current_workflow_or_none() is None
        # The heartbeat reports what is open.
        assert engine._get_current_workflow_info()["current_workflow_id"] is None
        with engine.run_in_worker_workflow(workflow):
            assert engine.context_manager.get_current_workflow_id() == PEER_ID
            # The workflow manager reads the same current workflow as the context manager.
            with engine.workflow_manager.referenced_workflow("library_workflow"):
                assert workflow.loading.referenced_workflow_stack == ["library_workflow"]
            with pytest.raises(engine.context_manager.WorkflowAlreadyOpenError):
                engine.context_manager.push_workflow(workflow_name="local")

        engine.close_worker_workflow(workflow)
        assert not engine.context_manager.has_current_workflow()
        engine.context_manager.push_workflow(workflow_name="local")
        with pytest.raises(engine.context_manager.OrchestratorWorkflowError, match="a workflow is open"):
            engine.context_manager.adopt_orchestrator_workflow(PEER_ID, None, None, None)

    def test_work_still_bound_to_a_closed_runtime_is_refused(self, engine: Engine) -> None:
        workflow = engine.context_manager.adopt_orchestrator_workflow(PEER_ID, None, None, None)

        with engine.bind_workflow(workflow):
            engine.close_worker_workflow(workflow)
            assert workflow.closed
            with pytest.raises(WorkflowNotOpenError, match="was closed"):
                engine.current_workflow_or_none()

    def test_popping_a_workers_runtime_is_refused_and_keeps_it(self, engine: Engine) -> None:
        workflow = engine.context_manager.adopt_orchestrator_workflow(PEER_ID, None, None, None)

        with (
            engine.run_in_worker_workflow(workflow),
            pytest.raises(engine.context_manager.WorkerWorkflowNotClosableError, match="that engine closes it"),
        ):
            engine.context_manager.pop_workflow()

        assert engine.worker_workflows.find(PEER_ID) is workflow
        assert engine.workflow_registry.is_open(PEER_ID)

    def test_each_runtime_has_a_context_of_its_own(self, engine: Engine) -> None:
        first = engine.context_manager.adopt_orchestrator_workflow(PEER_ID, None, None, None)
        second = engine.context_manager.adopt_orchestrator_workflow(OTHER_PEER_ID, None, None, None)

        with engine.run_in_worker_workflow(first):
            assert engine.context_manager.get_current_workflow_id() == PEER_ID
        with engine.run_in_worker_workflow(second):
            assert engine.context_manager.get_current_workflow_id() == OTHER_PEER_ID

    def test_code_in_a_thread_that_does_not_copy_the_context_finds_no_workflow(self, engine: Engine) -> None:
        """A plain thread loses the node's binding. `asyncio.to_thread` and `contextvars.copy_context` keep it."""
        workflow = engine.context_manager.adopt_orchestrator_workflow(PEER_ID, None, None, None)
        found: list[WorkflowRuntime | None] = []

        with engine.run_in_worker_workflow(workflow):
            thread = threading.Thread(target=lambda: found.append(engine.current_workflow_or_none()))
            thread.start()
            thread.join()

        assert found == [None]

    def test_unbound_work_on_a_worker_keeping_several_runtimes_finds_no_workflow(self, engine: Engine) -> None:
        """How many runtimes a worker keeps never changes what unbound work finds."""
        for peer_id in (PEER_ID, OTHER_PEER_ID):
            engine.context_manager.adopt_orchestrator_workflow(peer_id, None, None, None)

        assert engine.current_workflow_or_none() is None
        assert not engine.context_manager.has_current_workflow()

    @pytest.mark.asyncio
    async def test_a_request_forwarded_from_a_node_names_its_orchestrator_workflow(self, engine: Engine) -> None:
        engine.context_manager.adopt_orchestrator_workflow(PEER_ID, None, None, None)
        workflow = engine.context_manager.adopt_orchestrator_workflow(OTHER_PEER_ID, None, None, None)

        with engine.run_in_worker_workflow(workflow):
            forwarded = await _forward_a_request(engine)

        assert forwarded.open_workflow_id == OTHER_PEER_ID

    @pytest.mark.asyncio
    async def test_a_request_forwarded_outside_a_node_names_no_workflow(self, engine: Engine) -> None:
        engine.context_manager.adopt_orchestrator_workflow(PEER_ID, None, None, None)

        forwarded = await _forward_a_request(engine)

        assert forwarded.open_workflow_id is None
