"""Nested Workflow-node failures when the nodes inside the workflow run in a worker.

A Workflow node always runs on the orchestrator, because it reads the flows it imports. The nodes inside
its subflow are routed to their own libraries' workers. A library whose nodes run in a worker process
hands every result back to the orchestrator through ``converters.engine``, which turns each exception into a
``ForwardedException`` that keeps only the type name, message and traceback.

The worker is the in-process stand-in from ``tests.e2e.worker_stand_in``, which documents what it mirrors and
where its fidelity stops.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from griptape_nodes.exe_types.flow import ControlFlow
from griptape_nodes.exe_types.node_types import ISOLATED_EXECUTION
from griptape_nodes.retained_mode.beta_features import ISOLATED_EXECUTION_IN_WORKER
from griptape_nodes.retained_mode.events.base_events import (
    ExecutionGriptapeNodeEvent,
    ForwardedException,
)
from griptape_nodes.retained_mode.events.execution_events import (
    ExecuteNodeResultFailure,
    NodeErrorEvent,
    StartFlowRequest,
    StartFlowResultFailure,
    StartFlowResultSuccess,
)
from griptape_nodes.retained_mode.events.flow_events import CreateFlowRequest, CreateFlowResultSuccess
from griptape_nodes.retained_mode.events.library_events import (
    RegisterLibraryFromFileRequest,
    RegisterLibraryFromFileResultSuccess,
)
from griptape_nodes.retained_mode.events.node_events import CreateNodeRequest, CreateNodeResultSuccess
from griptape_nodes.retained_mode.events.parameter_events import SetParameterValueRequest
from griptape_nodes.retained_mode.events.workflow_events import ImportWorkflowAsReferencedSubFlowResultFailure
from griptape_nodes.retained_mode.managers.worker_manager import WorkerRegistration
from tests.e2e.test_subflow_import_cycle import (
    COUNTDOWN,
    COUNTDOWN_FAIL,
    COUNTDOWN_GROUP,
    COUNTDOWN_LOOP,
    CYCLE_A,
    CYCLE_B,
    DEFAULT_RECURSION_DEPTH,
    FAILED_NODE_PATH_FIELD,
    FIXTURE_FAILURE_MESSAGE,
    FIXTURE_LIBRARY_DIR,
    FIXTURE_LIBRARY_JSON_TEMPLATE,
    FIXTURE_NODE_FILE,
    LIBRARY_NAME,
    LOOP_LIBRARY_JSON_TEMPLATE,
    LOOP_LIBRARY_NAME,
    LOOP_NODE_FILE,
    NESTED,
    PATH_SEPARATOR,
    RECURSION_DEPTH_KEY,
    RECURSIVE,
    RECURSIVE_LOOP,
    RECURSIVE_LOOP_PARALLEL,
    RUN_SENTENCE,
    WORKFLOW_FILES,
    FixtureWorkflow,
)
from tests.e2e.worker_stand_in import Crossings, StandInWorker, without_worker_traceback

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from griptape_nodes.retained_mode.engine import Engine

pytestmark = pytest.mark.timeout(300, method="thread")

COUNTDOWN_FAIL_WORKER = FixtureWorkflow(node_type="CountdownFailWorkerWorkflow", stem="countdown_fail_worker_workflow")
LEAF_LIBRARY_DIR = Path(__file__).parent / "fixtures" / "worker_leaf_library"
LEAF_LIBRARY_JSON_TEMPLATE = LEAF_LIBRARY_DIR / "griptape_nodes_library.json"
LEAF_NODE_FILE = LEAF_LIBRARY_DIR / "worker_leaf_nodes.py"
LEAF_LIBRARY_NAME = "Worker Leaf Library"
REGISTERED_WORKFLOWS = [
    RECURSIVE,
    CYCLE_A,
    CYCLE_B,
    NESTED,
    COUNTDOWN,
    COUNTDOWN_FAIL,
    COUNTDOWN_FAIL_WORKER,
    COUNTDOWN_GROUP,
    COUNTDOWN_LOOP,
    RECURSIVE_LOOP,
    RECURSIVE_LOOP_PARALLEL,
]
ALL_WORKFLOWS = [
    *WORKFLOW_FILES,
    FIXTURE_LIBRARY_DIR / f"{COUNTDOWN_FAIL_WORKER.stem}.py",
]
NODE_ERROR_TYPE = "griptape_nodes.exe_types.node_error.NodeError"
RECURSION_ERROR_TYPE = "griptape_nodes.exe_types.workflow_node_errors.WorkflowRecursionLimitError"


class _Scenario:
    """A registered fixture library set, a stand-in worker, and the spies a worker-boundary test reads."""

    def __init__(self, engine: Engine, worker: StandInWorker, aput_event: AsyncMock, library_dir: Path) -> None:
        self.engine = engine
        self.worker = worker
        self.aput_event = aput_event
        self.library_dir = library_dir

    @property
    def crossings(self) -> Crossings:
        """What crossed the stand-in worker boundary."""
        return self.worker.crossings

    def new_parent_flow(self) -> str:
        """Create the flow the test's Outer node lives in."""
        self.engine.context_manager.push_workflow(workflow_name="import_cycle_worker_parent")
        result = self.engine.handle_request(
            CreateFlowRequest(parent_flow_name=None, flow_name="ParentFlow", set_as_new_context=False)
        )
        assert isinstance(result, CreateFlowResultSuccess), result
        return result.flow_name

    def key(self, workflow: FixtureWorkflow) -> str:
        """Return the registry key the engine uses for a library-shipped workflow file."""
        return (self.library_dir / workflow.stem).as_posix()

    def flow_count(self) -> int:
        """Count the flows in the engine."""
        return len(self.engine.object_manager.get_filtered_subset(type=ControlFlow))

    def node_errors(self) -> list[NodeErrorEvent]:
        """Return the NodeErrorEvents published through the spied event bus, in order."""
        node_errors: list[NodeErrorEvent] = []
        for call in self.aput_event.call_args_list:
            event = call.args[0]
            if not isinstance(event, ExecutionGriptapeNodeEvent):
                continue
            payload = event.wrapped_event.payload
            if isinstance(payload, NodeErrorEvent):
                node_errors.append(payload)
        return node_errors

    async def run_outer(
        self, create_node: Callable[..., str], workflow: FixtureWorkflow, *, text: str | None = None
    ) -> StartFlowResultFailure:
        """Run a Workflow node named Outer for `workflow` and return its failure."""
        parent_flow = self.new_parent_flow()
        create_node(workflow.node_type, "Outer", parent_flow, library_name=LIBRARY_NAME)
        if text is not None:
            set_result = self.engine.handle_request(
                SetParameterValueRequest(parameter_name="text", node_name="Outer", value=text)
            )
            assert set_result.succeeded(), set_result
        result = await self.engine.ahandle_request(StartFlowRequest(flow_name=parent_flow, flow_node_name="Outer"))
        assert isinstance(result, StartFlowResultFailure), result
        return result

    def assert_countdown_fail_reported_once(self, result: StartFlowResultFailure) -> None:
        """Assert the failure names the failing node once, with the depth and path."""
        details = without_worker_traceback(str(result.result_details))
        # Two characters of text recurse twice, so the failing node sits inside two nested Workflow nodes. Node
        # names are unique across flows, so the third copy of the fixture's failing node is `Failing_2`.
        assert details.count(RUN_SENTENCE) == 1, details
        assert details.count(FIXTURE_FAILURE_MESSAGE) == 1, details
        assert "node 'Failing_2', inside 2 nested Workflow nodes, failed" in details, details
        outer = self.node_errors()[-1]
        assert outer.node_name == "Outer", outer
        assert outer.error is not None, outer
        assert outer.error.message.count(FIXTURE_FAILURE_MESSAGE) == 1, outer.error
        assert "did not finish" not in outer.error.message, outer.error
        assert outer.error.fields[FAILED_NODE_PATH_FIELD].split(PATH_SEPARATOR) == [
            "Inner Workflow",
            "Inner Workflow_1",
            "Failing_2",
        ], outer.error
        assert outer.error.exception_type == NODE_ERROR_TYPE, outer.error

    def forwarded_refusals(self) -> list[ImportWorkflowAsReferencedSubFlowResultFailure]:
        """Return the forwarded import failures the worker's requests got back."""
        return [
            forwarded.result
            for forwarded in self.crossings.forwarded
            if isinstance(forwarded.result, ImportWorkflowAsReferencedSubFlowResultFailure)
        ]

    def failures_that_crossed_as(self, original_type: str) -> list[ExecuteNodeResultFailure]:
        """Return the failures the worker sent back whose exception arrived as a ForwardedException of this type."""
        return [
            failure
            for failure in self.crossings.node_failures()
            if isinstance(failure.exception, ForwardedException) and failure.exception.original_type == original_type
        ]


class _WorkerBoundaryBase:
    """Fixtures that register the libraries and build the scenario."""

    @pytest.fixture
    def scenario(
        self,
        tmp_path: Path,
        engine: Engine,
        materialize_library: Callable[..., Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> Iterator[_Scenario]:
        """Register the leaf and Workflow Node libraries and set up the stand-in worker, hosting nothing yet."""
        leaf_json = materialize_library(
            tmp_path / "leaf_library", template=LEAF_LIBRARY_JSON_TEMPLATE, node_file=LEAF_NODE_FILE
        )
        leaf_result = engine.handle_request(RegisterLibraryFromFileRequest(file_path=str(leaf_json)))
        assert isinstance(leaf_result, RegisterLibraryFromFileResultSuccess), leaf_result

        library_json = materialize_library(
            tmp_path / "library",
            template=FIXTURE_LIBRARY_JSON_TEMPLATE,
            node_file=FIXTURE_NODE_FILE,
            extra_files=ALL_WORKFLOWS,
        )
        schema = json.loads(library_json.read_text())
        for workflow in REGISTERED_WORKFLOWS:
            schema["workflow_nodes"].append(
                {
                    "node_type": workflow.node_type,
                    "workflow_path": f"{workflow.stem}.py",
                    "metadata": {
                        "category": "test",
                        "description": workflow.node_type,
                        "display_name": workflow.node_type,
                    },
                }
            )
        library_json.write_text(json.dumps(schema, indent=2))
        result = engine.handle_request(RegisterLibraryFromFileRequest(file_path=str(library_json)))
        assert isinstance(result, RegisterLibraryFromFileResultSuccess), result

        event_manager = engine.event_manager
        real_aput_event = event_manager.aput_event
        spy = AsyncMock(spec=real_aput_event, wraps=real_aput_event)
        monkeypatch.setattr(event_manager, "aput_event", spy)

        config_manager = engine.config_manager
        previous_depth = config_manager.get_config_value(RECURSION_DEPTH_KEY)
        worker = StandInWorker(engine, monkeypatch)
        yield _Scenario(engine, worker, spy, library_json.parent)
        config_manager.set_config_value(RECURSION_DEPTH_KEY, previous_depth)

    @pytest.fixture
    def loop_library(self, tmp_path: Path, engine: Engine, materialize_library: Callable[..., Path]) -> None:
        """Register the library that supplies the loop group and the Start and End Flow nodes it packages with."""
        loop_json = materialize_library(
            tmp_path / "loop_library",
            template=LOOP_LIBRARY_JSON_TEMPLATE,
            node_file=LOOP_NODE_FILE,
            name=LOOP_LIBRARY_NAME,
        )
        result = engine.handle_request(RegisterLibraryFromFileRequest(file_path=str(loop_json)))
        assert isinstance(result, RegisterLibraryFromFileResultSuccess), result


class TestLeafNodeInWorker(_WorkerBoundaryBase):
    """Only the failing node of a nested workflow runs in a worker; the Workflow nodes stay on the orchestrator."""

    @pytest.mark.asyncio
    async def test_node_failing_several_levels_down_is_reported_once_with_the_depth(
        self, engine: Engine, scenario: _Scenario, create_node: Callable[..., str]
    ) -> None:
        """A worker-hosted node failing at the bottom of nested Workflow nodes is named once, with the depth."""
        scenario.worker.host(LEAF_LIBRARY_NAME)
        engine.config_manager.set_config_value(RECURSION_DEPTH_KEY, 5)

        result = await scenario.run_outer(create_node, COUNTDOWN_FAIL_WORKER, text="aa")

        scenario.assert_countdown_fail_reported_once(result)
        # The leaf's failure crossed the boundary, so its exception arrived as a placeholder.
        crossed = scenario.failures_that_crossed_as(NODE_ERROR_TYPE)
        assert len(crossed) == 1, scenario.crossings
        assert crossed[0].error is not None, crossed[0]
        assert crossed[0].error.exception_type == NODE_ERROR_TYPE, crossed[0].error


class TestWholeLibraryInWorker(_WorkerBoundaryBase):
    """The whole library is hosted in a worker: Workflow nodes stay here and the nodes in their subflows cross."""

    @pytest.mark.asyncio
    async def test_node_failing_several_levels_down_is_reported_once_with_the_depth(
        self, engine: Engine, scenario: _Scenario, create_node: Callable[..., str]
    ) -> None:
        """A node failing at the bottom of nested Workflow nodes, in a hosted library, is named once."""
        scenario.worker.host(LIBRARY_NAME)
        engine.config_manager.set_config_value(RECURSION_DEPTH_KEY, 5)

        result = await scenario.run_outer(create_node, COUNTDOWN_FAIL, text="aa")

        scenario.assert_countdown_fail_reported_once(result)
        # The Workflow nodes ran here, reading the orchestrator's flows. The nodes inside their subflows crossed.
        scenario.crossings.assert_only_inner_nodes_crossed()
        assert "Failing_2" in scenario.crossings.node_names(), scenario.crossings
        assert scenario.failures_that_crossed_as(NODE_ERROR_TYPE), scenario.crossings

    @pytest.mark.asyncio
    async def test_runaway_recursion_reports_its_cause_once_with_the_depth(
        self, scenario: _Scenario, create_node: Callable[..., str]
    ) -> None:
        """The error for runaway recursion, in a hosted library, states the cause once and the depth."""
        scenario.worker.host(LIBRARY_NAME)

        result = await scenario.run_outer(create_node, RECURSIVE)

        details = without_worker_traceback(str(result.result_details))
        key = scenario.key(RECURSIVE)
        root_cause = f"'{key}' contains itself more than {DEFAULT_RECURSION_DEPTH} levels deep"
        # Every flow but the parent is an import, and each holds one Workflow node that nests the next.
        depth = scenario.flow_count() - 1
        assert details.count(RUN_SENTENCE) == 1, details
        assert details.count(root_cause) == 1, details
        # The path holds one Workflow node per import; the last is the one refused, so it is not counted.
        assert f"inside {depth - 1} nested Workflow nodes" in details, details
        outer = scenario.node_errors()[-1]
        assert outer.node_name == "Outer", outer
        assert outer.error is not None, outer
        assert root_cause in outer.error.message, outer.error
        assert "did not finish" not in outer.error.message, outer.error
        assert "could not be loaded" not in outer.error.message, outer.error
        # Eleven nodes are too many to list, so the field keeps both ends and counts the rest.
        path = outer.error.fields[FAILED_NODE_PATH_FIELD].split(PATH_SEPARATOR)
        assert path == [
            "Inner Workflow",
            "Inner Workflow_1",
            f"\u2026 ({depth - 4} more)",
            f"Inner Workflow_{depth - 2}",
            f"Inner Workflow_{depth - 1}",
        ], path
        assert outer.error.exception_type == RECURSION_ERROR_TYPE, outer.error
        # The Workflow nodes ran here, so the refusal never crossed the boundary.
        scenario.crossings.assert_only_inner_nodes_crossed()
        assert scenario.forwarded_refusals() == [], scenario.crossings

    @pytest.mark.asyncio
    async def test_recursion_through_a_group_reports_its_cause_once_with_the_depth_and_the_group(
        self, scenario: _Scenario, create_node: Callable[..., str]
    ) -> None:
        """A group between Workflow nodes, in a hosted library, shows up in the path but is not a level."""
        scenario.worker.host(LIBRARY_NAME)
        scenario.engine.config_manager.set_config_value(RECURSION_DEPTH_KEY, 3)

        result = await scenario.run_outer(create_node, COUNTDOWN_GROUP, text="aaaaaaaa")

        scenario.crossings.assert_only_inner_nodes_crossed()

        details = without_worker_traceback(str(result.result_details))
        root_cause = f"'{scenario.key(COUNTDOWN_GROUP)}' contains itself more than 3 levels deep"
        assert details.count(RUN_SENTENCE) == 1, details
        assert details.count(root_cause) == 1, details
        outer = scenario.node_errors()[-1]
        assert outer.error is not None, outer
        assert "node 'Inner Workflow_3', inside 3 nested Workflow nodes, failed" in outer.error.message, outer.error
        assert outer.error.fields[FAILED_NODE_PATH_FIELD].split(PATH_SEPARATOR) == [
            "Group",
            "Inner Workflow",
            "\u2026 (4 more)",
            "Group_3",
            "Inner Workflow_3",
        ], outer.error
        assert outer.error.exception_type == RECURSION_ERROR_TYPE, outer.error

    @pytest.mark.usefixtures("loop_library")
    @pytest.mark.asyncio
    async def test_node_failing_below_a_loop_is_reported_once_with_the_depth_and_the_loop(
        self, scenario: _Scenario, create_node: Callable[..., str]
    ) -> None:
        """A loop between Workflow nodes, in a hosted library, shows up in the path but is not a level."""
        scenario.worker.host(LIBRARY_NAME)

        parent_flow = scenario.new_parent_flow()
        create_node(COUNTDOWN_LOOP.node_type, "Outer", parent_flow, library_name=LIBRARY_NAME)
        set_result = scenario.engine.handle_request(
            SetParameterValueRequest(parameter_name="text", node_name="Outer", value="aa")
        )
        assert set_result.succeeded(), set_result
        # Packaging a loop body needs a current flow, which the editor always has.
        with scenario.engine.context_manager.flow(parent_flow):
            result = await scenario.engine.ahandle_request(
                StartFlowRequest(flow_name=parent_flow, flow_node_name="Outer")
            )

        assert isinstance(result, StartFlowResultFailure), result
        scenario.crossings.assert_only_inner_nodes_crossed()
        details = without_worker_traceback(str(result.result_details))
        assert details.count(RUN_SENTENCE) == 1, details
        assert details.count(FIXTURE_FAILURE_MESSAGE) == 1, details
        assert "did not finish" not in details, details
        outer = scenario.node_errors()[-1]
        assert outer.error is not None, outer
        assert "node 'Failing_2', inside 2 nested Workflow nodes, failed" in outer.error.message, outer.error
        assert outer.error.fields[FAILED_NODE_PATH_FIELD].split(PATH_SEPARATOR) == [
            "Loop",
            "Inner Workflow",
            "Loop_1",
            "Inner Workflow_2",
            "Failing_2",
        ], outer.error

    @pytest.mark.timeout(60, method="thread")
    @pytest.mark.usefixtures("loop_library")
    @pytest.mark.asyncio
    async def test_recursion_through_a_loop_fails_promptly(
        self, scenario: _Scenario, create_node: Callable[..., str]
    ) -> None:
        """A refusal inside a loop between Workflow nodes, in a hosted library, ends the loop at once."""
        scenario.worker.host(LIBRARY_NAME)

        parent_flow = scenario.new_parent_flow()
        create_node(RECURSIVE_LOOP.node_type, "Outer", parent_flow, library_name=LIBRARY_NAME)
        # Packaging a loop body needs a current flow, which the editor always has.
        with scenario.engine.context_manager.flow(parent_flow):
            result = await scenario.engine.ahandle_request(
                StartFlowRequest(flow_name=parent_flow, flow_node_name="Outer")
            )

        assert isinstance(result, StartFlowResultFailure), result
        scenario.crossings.assert_only_inner_nodes_crossed()
        details = without_worker_traceback(str(result.result_details))
        root_cause = f"'{scenario.key(RECURSIVE_LOOP)}' contains itself more than {DEFAULT_RECURSION_DEPTH} levels deep"
        assert details.count(root_cause) == 1, details
        outer = scenario.node_errors()[-1]
        assert outer.error is not None, outer
        assert root_cause in outer.error.message, outer.error


class TestWorkflowNodeInIsolatedExecutionGroup(_WorkerBoundaryBase):
    """A Workflow node inside an Isolated Execution group runs here while the nodes of its workflow go to the worker."""

    @pytest.mark.asyncio
    async def test_workflow_node_runs_locally_and_its_inner_nodes_go_to_the_worker(
        self, engine: Engine, scenario: _Scenario
    ) -> None:
        """The Workflow node is not sent to the worker, but the nodes of its subflow still are."""
        engine.worker_manager._workers["general-purpose-worker"] = WorkerRegistration(
            request_topic="sessions/test/workers/general-purpose-worker/request", worker_key=None
        )
        workers = engine.library_manager.workers
        workers._general_purpose_worker_generation = workers._library_set_generation
        config_manager = engine.config_manager
        config_manager.set_config_value(ISOLATED_EXECUTION_IN_WORKER.config_key, True)
        try:
            parent_flow = scenario.new_parent_flow()
            with engine.context_manager.flow(parent_flow):
                group = engine.handle_request(
                    CreateNodeRequest(
                        node_type="FixtureGroupNode", specific_library_name=LIBRARY_NAME, node_name="Group"
                    )
                )
                assert isinstance(group, CreateNodeResultSuccess), group
                member = engine.handle_request(
                    CreateNodeRequest(
                        node_type="ShoutWorkflow",
                        specific_library_name=LIBRARY_NAME,
                        node_name="Shouter",
                        parent_group_name=group.node_name,
                    )
                )
                assert isinstance(member, CreateNodeResultSuccess), member
            set_result = engine.handle_request(
                SetParameterValueRequest(parameter_name="text", node_name="Shouter", value="hey")
            )
            assert set_result.succeeded(), set_result
            set_result = engine.handle_request(
                SetParameterValueRequest(
                    parameter_name="execution_environment", node_name=group.node_name, value=ISOLATED_EXECUTION
                )
            )
            assert set_result.succeeded(), set_result
            result = await engine.ahandle_request(
                StartFlowRequest(flow_name=parent_flow, flow_node_name=group.node_name)
            )
        finally:
            config_manager.set_config_value(ISOLATED_EXECUTION_IN_WORKER.config_key, False)
            engine.worker_manager._workers.pop("general-purpose-worker", None)

        assert isinstance(result, StartFlowResultSuccess), result
        crossings = scenario.crossings
        assert crossings.workflow_node_names() == [], crossings
        assert "Shouter" not in crossings.node_names(), crossings
        assert any(name.startswith("Shout") for name in crossings.node_names()), crossings
        shouter = engine.node_manager.get_node_by_name("Shouter")
        assert shouter.parameter_output_values.get("result") == "HEY!", shouter.parameter_output_values
