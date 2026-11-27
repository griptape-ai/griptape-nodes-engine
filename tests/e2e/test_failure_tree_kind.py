"""The kind of a node's failure survives the trip from the worker that ran the node to the engine that ran the flow."""

from __future__ import annotations

from contextlib import AbstractContextManager, nullcontext
from typing import TYPE_CHECKING

import pytest

from griptape_nodes.retained_mode.events.failure_tree import FailureKind, FailureLeaf, FailureTree, LoopFailure
from griptape_nodes.retained_mode.events.parameter_events import SetParameterValueRequest
from tests.e2e.node_failure_support import new_flow, register_fixture_library, start_flow
from tests.e2e.stub_cloud_support import REFUSED_PATH
from tests.e2e.test_node_failure_output_groups import ONE_AT_A_TIME, _LoopFailureBase
from tests.simulated_worker import simulated_worker

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from griptape_nodes.retained_mode.engine import Engine

pytestmark = pytest.mark.timeout(300, method="thread")

LIBRARY_NAME = "Budget Halt Library"
NODE_NAME = "Node1"
HALT_TYPE = "griptape_nodes.utils.budget_refusal.BudgetExceededError"
ITEM_COUNT = 14


@pytest.fixture(autouse=True)
def _budget_library(engine: Engine, tmp_path: Path, materialize_library: Callable[..., Path]) -> None:
    """Register the library of nodes that spend credits."""
    register_fixture_library(
        engine,
        tmp_path / "budget_halt_library",
        materialize_library,
        library_dir_name="budget_halt_library",
        node_file_name="budget_nodes.py",
    )


class TestBudgetHaltKind:
    async def failure_of_refused_node(
        self,
        engine: Engine,
        monkeypatch: pytest.MonkeyPatch,
        create_node: Callable[..., str],
        stub_cloud: str,
        *,
        in_worker: bool,
    ) -> FailureTree:
        """Run a flow whose one node Cloud refuses, and return the failure the run recorded."""
        worker_context: AbstractContextManager[object] = nullcontext()
        if in_worker:
            worker_context = simulated_worker(engine, LIBRARY_NAME)
        with worker_context:
            flow_name = new_flow(engine, "budget_failure_kind")
            create_node("UnhandledCloudCallNode", NODE_NAME, flow_name, library_name=LIBRARY_NAME)
            engine.handle_request(
                SetParameterValueRequest(parameter_name="url", node_name=NODE_NAME, value=f"{stub_cloud}{REFUSED_PATH}")
            )
            await start_flow(engine, monkeypatch, flow_name)
        machine = engine.current_workflow().flows.global_control_flow_machine
        assert machine is not None
        failure_tree = machine.resolution_machine.get_error_failure_tree()
        assert failure_tree is not None
        return failure_tree

    @pytest.mark.asyncio
    @pytest.mark.parametrize("in_worker", [False, True], ids=["in_process", "in_worker"])
    async def test_a_budget_halt_is_still_one_when_it_arrives(
        self,
        engine: Engine,
        monkeypatch: pytest.MonkeyPatch,
        create_node: Callable[..., str],
        stub_cloud: str,
        in_worker: bool,  # noqa: FBT001
    ) -> None:
        failure_tree = await self.failure_of_refused_node(
            engine, monkeypatch, create_node, stub_cloud, in_worker=in_worker
        )

        assert isinstance(failure_tree, FailureLeaf)
        assert failure_tree.kind is FailureKind.BUDGET_HALT
        assert failure_tree.node_name == NODE_NAME
        assert failure_tree.details.exception_type == HALT_TYPE


class TestLoopFailureKind(_LoopFailureBase):
    @pytest.mark.asyncio
    async def test_a_loops_reason_names_the_body_the_artist_placed(
        self, engine: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await self.run_loop(engine, monkeypatch, "ConstantFailingBodyNode", ONE_AT_A_TIME)

        machine = engine.current_workflow().flows.global_control_flow_machine
        assert machine is not None
        failure_tree = machine.resolution_machine.get_error_failure_tree()
        assert isinstance(failure_tree, LoopFailure)
        assert failure_tree.node_name == "Loop"
        assert failure_tree.total_iterations == ITEM_COUNT
        assert failure_tree.failed_iterations == ITEM_COUNT
        reason_tree = failure_tree.reasons[0].failure_tree
        assert isinstance(reason_tree, FailureLeaf)
        assert reason_tree.node_name == "Body"
        assert reason_tree.kind is FailureKind.RAISED
