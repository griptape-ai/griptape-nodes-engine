"""The runtimes a worker keeps for its orchestrator's workflows, which it runs that workflow's nodes in."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

import pytest

from griptape_nodes.retained_mode.runtime.binding import WorkflowNotNamedError

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.runtime.workflow_runtime import WorkflowRuntime

PEER_ID = "00000000000000000000000000000abc"
OTHER_PEER_ID = "00000000000000000000000000000def"


class TestWorkerWorkflows:
    def test_a_workers_runtime_is_current_but_never_open_or_beside_an_open_workflow(self, engine: Engine) -> None:
        workflow = engine.context_manager.adopt_orchestrator_workflow(PEER_ID, None, None, None)

        assert engine.open_workflows.all_open() == []
        assert engine.workflow_registry.is_open(PEER_ID)
        # Unbound, as in a thread that does not copy the context, the worker's only runtime is current.
        assert engine.context_manager.has_current_workflow()
        assert engine.current_workflow_or_none() is workflow
        # The heartbeat reports what is open.
        assert engine._get_current_workflow_info()["current_workflow_id"] is None
        with engine.run_in_worker_workflow(workflow):
            assert engine.context_manager.get_current_workflow_id() == PEER_ID
            with pytest.raises(engine.context_manager.WorkflowAlreadyOpenError):
                engine.context_manager.push_workflow(workflow_name="local")

        engine.close_worker_workflow(workflow)
        assert not engine.context_manager.has_current_workflow()
        engine.context_manager.push_workflow(workflow_name="local")
        with pytest.raises(engine.context_manager.OrchestratorWorkflowError, match="a workflow is open"):
            engine.context_manager.adopt_orchestrator_workflow(PEER_ID, None, None, None)

    def test_each_runtime_has_a_context_of_its_own(self, engine: Engine) -> None:
        first = engine.context_manager.adopt_orchestrator_workflow(PEER_ID, None, None, None)
        second = engine.context_manager.adopt_orchestrator_workflow(OTHER_PEER_ID, None, None, None)

        with engine.run_in_worker_workflow(first):
            assert engine.context_manager.get_current_workflow_id() == PEER_ID
        with engine.run_in_worker_workflow(second):
            assert engine.context_manager.get_current_workflow_id() == OTHER_PEER_ID

    def test_code_in_a_thread_that_does_not_copy_the_context_finds_the_workers_only_runtime(
        self, engine: Engine
    ) -> None:
        workflow = engine.context_manager.adopt_orchestrator_workflow(PEER_ID, None, None, None)
        found: list[WorkflowRuntime | None] = []

        with engine.run_in_worker_workflow(workflow):
            thread = threading.Thread(target=lambda: found.append(engine.current_workflow_or_none()))
            thread.start()
            thread.join()

        assert found == [workflow]

    def test_unbound_work_on_a_worker_keeping_several_runtimes_raises(self, engine: Engine) -> None:
        for peer_id in (PEER_ID, OTHER_PEER_ID):
            engine.context_manager.adopt_orchestrator_workflow(peer_id, None, None, None)

        with pytest.raises(WorkflowNotNamedError, match="2 workflows are open"):
            engine.current_workflow_or_none()
