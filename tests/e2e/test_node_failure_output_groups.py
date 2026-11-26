"""Golden tests that pin what a failure reports when it happens inside a group or a loop.

A group or loop fails because something inside it did, so one failure is reported at each level: the inner
node's own ``NodeErrorEvent``, then the container's, whose text is the inner failure's with the container's
framing around it. The container's ``NodeErrorEvent.error`` has no node-built parts, only the whole text as the
message of a ``builtins.RuntimeError``.

Loops run their body from a copy of the nodes the artist placed, so what they report names the copies, while
the events a body copy sends are reported under the canvas name. A parallel loop reports no reason in its
failure at all, only a count, and logs each iteration's reason at WARNING.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

import pytest

from griptape_nodes.machines.control_flow import ControlFlowMachine
from griptape_nodes.retained_mode.events.connection_events import CreateConnectionRequest
from griptape_nodes.retained_mode.events.execution_events import (
    NodeProcessOutcome,
    ResolveNodeRequest,
    ResolveNodeResultFailure,
    StartFlowResultFailure,
    StartFlowResultSuccess,
)
from griptape_nodes.retained_mode.events.node_events import AddNodesToNodeGroupRequest
from griptape_nodes.retained_mode.events.parameter_events import SetParameterValueRequest
from tests.e2e.node_failure_support import (
    FlowRun,
    assert_subflow_failures,
    client_exception,
    client_result,
    client_run_failure,
    create_node_in_flow,
    details_dict,
    expected_details,
    mask_worker_traceback,
    masked_client_form,
    new_flow,
    record_failures,
    register_fixture_library,
    start_flow,
    traceback_ending_with,
    worker_traceback_block,
)
from tests.simulated_worker import simulated_worker

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from griptape_nodes.retained_mode.engine import Engine

pytestmark = pytest.mark.timeout(300, method="thread")

FAILURE_LIBRARY = "Failure Output Library"
SUBFLOW_LIBRARY = "Subflow Library"
LEAF_LIBRARY = "Worker Leaf Library"
# The loop packager looks up its Start Flow and End Flow nodes in a library with this name.
LOOP_LIBRARY = "Griptape Nodes Library"
FLOW_FAILURE_PREFIX = "Attempted to run flow 'Flow'. Failed due to: "
ONE_AT_A_TIME = "Run Group Items One at a Time"
ALL_AT_ONCE = "Run Group Items All at Once"

ECHO_FAILURE = (
    "Node 'X' execution failed: Attempted to execute node 'X'. Failed with error: Echo node must have text input"
)
LEAF_FAILURE_HEAD = (
    "Node 'X' execution failed: [griptape_nodes.exe_types.node_error.NodeError] Attempted to execute node 'X'. "
    "Failed with error: The fixture node refused to run"
)
WORKER_TRACEBACK_BLOCK = worker_traceback_block(
    "griptape_nodes.exe_types.node_error.NodeError: The fixture node refused to run"
)


def _container_details(message: str) -> dict:
    """What a group or loop reports as its ``NodeErrorEvent.error``: its whole text, and nothing attached."""
    return expected_details(message, "builtins.RuntimeError")


@pytest.mark.usefixtures("failure_output_library")
class _GroupFailureBase:
    """Registers the libraries a group test builds its graph from."""

    @pytest.fixture(autouse=True)
    def _libraries(self, engine: Engine, tmp_path: Path, materialize_library: Callable[..., Path]) -> None:
        for directory, node_file in (
            ("subflow_library", "subflow_echo_node.py"),
            ("worker_leaf_library", "worker_leaf_nodes.py"),
        ):
            register_fixture_library(
                engine, tmp_path / directory, materialize_library, library_dir_name=directory, node_file_name=node_file
            )


class TestLocalGroupWithFailingChild(_GroupFailureBase):
    """Golden case 9: a local group whose child fails.

    The group's own ``NodeErrorEvent`` carries the child's text as a ``builtins.RuntimeError`` with no fields,
    response or links, even though the child's carried its own type. The run's recorded message adds the
    group's framing, which the events do not have.
    """

    GROUP_MESSAGE = f"Node 'G' encountered a problem: {ECHO_FAILURE}"
    CHILD_DETAILS = expected_details("Echo node must have text input", "builtins.ValueError")

    @pytest.mark.asyncio
    async def test_start_flow(self, engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
        flow_name = new_flow(engine, "group_failure_output")
        create_node_in_flow(engine, "SubflowGroupNode", "G", flow_name, library_name=SUBFLOW_LIBRARY)
        create_node_in_flow(engine, "EchoNode", "X", flow_name, library_name=SUBFLOW_LIBRARY, parent_group_name="G")

        run = await start_flow(engine, monkeypatch, flow_name)

        assert isinstance(run.result, StartFlowResultFailure)
        assert run.run_message == self.GROUP_MESSAGE
        assert run.result_details == FLOW_FAILURE_PREFIX + self.GROUP_MESSAGE
        assert run.validation_exception_messages == [self.GROUP_MESSAGE]
        assert [(event.node_name, event.error_message) for event in run.recorded.node_errors] == [
            ("X", ECHO_FAILURE),
            ("G", ECHO_FAILURE),
        ]
        assert [details_dict(event.error) for event in run.recorded.node_errors] == [
            self.CHILD_DETAILS,
            _container_details(ECHO_FAILURE),
        ]
        assert [
            (finish.node_name, finish.flow_name, finish.outcome, finish.error_message)
            for finish in run.recorded.node_finishes
        ] == [
            ("X", "G_subflow", NodeProcessOutcome.FAILED, ECHO_FAILURE),
            ("G", "Flow", NodeProcessOutcome.FAILED, ECHO_FAILURE),
        ]
        assert [details_dict(finish.error) for finish in run.recorded.node_finishes] == [
            self.CHILD_DETAILS,
            _container_details(ECHO_FAILURE),
        ]
        run.recorded.assert_finish_errors_match_node_errors()
        assert run.client == client_run_failure(
            FLOW_FAILURE_PREFIX + self.GROUP_MESSAGE, [client_exception(self.GROUP_MESSAGE)]
        )
        assert_subflow_failures(run.subflow_runs, [("G_subflow", ECHO_FAILURE)])

    @pytest.mark.asyncio
    async def test_resolve_node_request(self, engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
        """``ResolveNodeRequest`` on the group words its failure the way the run does.

        The executor never sends a group an ``ExecuteNodeRequest``, so this is the request whose result carries
        a group's failure to a client. The result's one exception is always a ``RuntimeError`` that the flow
        manager makes up from the error text, so its type says nothing about the group.
        """
        flow_name = new_flow(engine, "group_resolve_failure_output")
        create_node_in_flow(engine, "SubflowGroupNode", "G", flow_name, library_name=SUBFLOW_LIBRARY)
        create_node_in_flow(engine, "EchoNode", "X", flow_name, library_name=SUBFLOW_LIBRARY, parent_group_name="G")
        recorded = record_failures(engine, monkeypatch)
        request = ResolveNodeRequest(node_name="G")

        result = await engine.ahandle_request(request)

        assert isinstance(result, ResolveNodeResultFailure)
        resolve_words = f'Failed to resolve "G".  Error: {self.GROUP_MESSAGE}'
        assert str(result.result_details) == resolve_words
        assert [(type(exception), str(exception)) for exception in result.validation_exceptions or []] == [
            (RuntimeError, self.GROUP_MESSAGE)
        ]
        assert [(event.node_name, event.error_message) for event in recorded.node_errors] == [
            ("X", ECHO_FAILURE),
            ("G", ECHO_FAILURE),
        ]
        assert [details_dict(event.error) for event in recorded.node_errors] == [
            self.CHILD_DETAILS,
            _container_details(ECHO_FAILURE),
        ]
        assert [(finish.node_name, finish.flow_name) for finish in recorded.node_finishes] == [
            ("X", "G_subflow"),
            ("G", "Flow"),
        ]
        recorded.assert_finish_errors_match_node_errors()
        assert masked_client_form(client_result(request, result)) == client_run_failure(
            resolve_words,
            [
                client_exception(
                    self.GROUP_MESSAGE,
                    "builtins.RuntimeError",
                    traceback_ending_with(f"RuntimeError: {self.GROUP_MESSAGE}"),
                )
            ],
        )


class TestGroupInsideAGroup(_GroupFailureBase):
    """Golden case 10, nested groups: each level frames the text of the one inside it, once."""

    INNER_MESSAGE = f"Node 'Inner' encountered a problem: {ECHO_FAILURE}"
    OUTER_MESSAGE = f"Node 'Outer' encountered a problem: {INNER_MESSAGE}"

    @pytest.mark.asyncio
    async def test_start_flow(self, engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
        flow_name = new_flow(engine, "nested_group_failure_output")
        create_node_in_flow(engine, "SubflowGroupNode", "Outer", flow_name, library_name=SUBFLOW_LIBRARY)
        create_node_in_flow(engine, "SubflowGroupNode", "Inner", flow_name, library_name=SUBFLOW_LIBRARY)
        create_node_in_flow(engine, "EchoNode", "X", flow_name, library_name=SUBFLOW_LIBRARY, parent_group_name="Inner")
        with engine.context_manager.flow(flow_name):
            nested = engine.handle_request(AddNodesToNodeGroupRequest(node_names=["Inner"], node_group_name="Outer"))
        assert nested.succeeded(), nested

        run = await start_flow(engine, monkeypatch, flow_name)

        assert run.run_message == self.OUTER_MESSAGE
        assert run.result_details == FLOW_FAILURE_PREFIX + self.OUTER_MESSAGE
        assert run.validation_exception_messages == [self.OUTER_MESSAGE]
        assert [(event.node_name, event.error_message) for event in run.recorded.node_errors] == [
            ("X", ECHO_FAILURE),
            ("Inner", ECHO_FAILURE),
            ("Outer", self.INNER_MESSAGE),
        ]
        assert [details_dict(event.error) for event in run.recorded.node_errors] == [
            expected_details("Echo node must have text input", "builtins.ValueError"),
            _container_details(ECHO_FAILURE),
            _container_details(self.INNER_MESSAGE),
        ]
        assert [
            (finish.node_name, finish.flow_name, finish.error_message) for finish in run.recorded.node_finishes
        ] == [
            ("X", "Inner_subflow", ECHO_FAILURE),
            ("Inner", "Outer_subflow", ECHO_FAILURE),
            ("Outer", "Flow", self.INNER_MESSAGE),
        ]
        assert [details_dict(finish.error) for finish in run.recorded.node_finishes] == [
            expected_details("Echo node must have text input", "builtins.ValueError"),
            _container_details(ECHO_FAILURE),
            _container_details(self.INNER_MESSAGE),
        ]
        run.recorded.assert_finish_errors_match_node_errors()
        assert run.client == client_run_failure(
            FLOW_FAILURE_PREFIX + self.OUTER_MESSAGE, [client_exception(self.OUTER_MESSAGE)]
        )
        assert_subflow_failures(
            run.subflow_runs, [("Inner_subflow", ECHO_FAILURE), ("Outer_subflow", self.INNER_MESSAGE)]
        )


class TestGroupWhoseChildRanInAWorker(_GroupFailureBase):
    """Golden case 10, worker child: the group's text embeds the worker's type and its whole traceback."""

    @pytest.mark.asyncio
    async def test_start_flow(self, engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
        flow_name = new_flow(engine, "worker_child_group_failure_output")
        create_node_in_flow(engine, "SubflowGroupNode", "G", flow_name, library_name=SUBFLOW_LIBRARY)
        create_node_in_flow(
            engine, "WorkerFailingNode", "X", flow_name, library_name=LEAF_LIBRARY, parent_group_name="G"
        )

        with simulated_worker(engine, LEAF_LIBRARY):
            run = await start_flow(engine, monkeypatch, flow_name)

        child_message = LEAF_FAILURE_HEAD + WORKER_TRACEBACK_BLOCK
        group_message = f"Node 'G' encountered a problem: {child_message}"
        run_message = mask_worker_traceback(run.run_message or "")
        assert run_message.text == group_message
        assert run_message.traceback is not None
        assert mask_worker_traceback(run.result_details).text == FLOW_FAILURE_PREFIX + group_message
        assert [event.node_name for event in run.recorded.node_errors] == ["X", "G"]
        child_error, group_error = run.recorded.node_errors
        assert mask_worker_traceback(child_error.error_message).text == child_message
        assert mask_worker_traceback(group_error.error_message).text == child_message
        assert mask_worker_traceback(group_error.error_message).traceback == run_message.traceback
        assert details_dict(child_error.error) == expected_details(
            "The fixture node refused to run", "griptape_nodes.exe_types.node_error.NodeError"
        )
        group_details = details_dict(group_error.error)
        assert group_details is not None
        assert mask_worker_traceback(group_details["message"]).text == child_message
        assert {key: value for key, value in group_details.items() if key != "message"} == {
            "exception_type": "builtins.RuntimeError",
            "messages": None,
            "fields": {},
            "response": None,
            "links": [],
        }
        assert [(finish.node_name, finish.flow_name) for finish in run.recorded.node_finishes] == [
            ("X", "G_subflow"),
            ("G", "Flow"),
        ]
        child_finish, group_finish = run.recorded.node_finishes
        assert mask_worker_traceback(child_finish.error_message or "").text == child_message
        assert mask_worker_traceback(group_finish.error_message or "").text == child_message
        assert details_dict(child_finish.error) == details_dict(child_error.error)
        assert details_dict(group_finish.error) == group_details
        run.recorded.assert_finish_errors_match_node_errors()
        assert masked_client_form(run.client) == client_run_failure(
            FLOW_FAILURE_PREFIX + group_message, [client_exception(group_message)]
        )
        assert_subflow_failures(run.subflow_runs, [("G_subflow", child_message)])


class TestSubflowFailingBeforeItsNodesRun(_GroupFailureBase):
    """Golden case 14: a group's subflow fails without any node in it failing."""

    @pytest.mark.asyncio
    async def test_the_subflow_fails_validation(self, engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
        """The subflow's problems are joined one per line, with no framing, as the subflow result's details."""
        flow_name = new_flow(engine, "subflow_validation_failure_output")
        create_node_in_flow(engine, "SubflowGroupNode", "G", flow_name, library_name=SUBFLOW_LIBRARY)
        create_node_in_flow(
            engine,
            "FailsWorkflowValidationNode",
            "V",
            flow_name,
            library_name=FAILURE_LIBRARY,
            parent_group_name="G",
        )

        run = await start_flow(engine, monkeypatch, flow_name)

        subflow_text = "The prompt is missing.\nThe seed is out of range."
        group_message = f"Node 'G' encountered a problem: {subflow_text}"
        assert run.run_message == group_message
        assert run.result_details == FLOW_FAILURE_PREFIX + group_message
        assert [(event.node_name, event.error_message) for event in run.recorded.node_errors] == [("G", subflow_text)]
        assert [details_dict(event.error) for event in run.recorded.node_errors] == [_container_details(subflow_text)]
        run.recorded.assert_finish_errors_match_node_errors()
        assert_subflow_failures(run.subflow_runs, [("G_subflow", subflow_text)])

    @pytest.mark.asyncio
    async def test_the_subflow_cannot_start(self, engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
        """The scheduler raising out of ``start_flow`` is worded ``Failed to run flow ... Error: ...``."""
        flow_name = new_flow(engine, "subflow_start_failure_output")
        create_node_in_flow(engine, "SubflowGroupNode", "G", flow_name, library_name=SUBFLOW_LIBRARY)
        create_node_in_flow(engine, "EchoNode", "X", flow_name, library_name=SUBFLOW_LIBRARY, parent_group_name="G")
        real_start_flow = ControlFlowMachine.start_flow

        async def start_flow_that_fails_in_subflows(
            machine: ControlFlowMachine, *args: object, **kwargs: object
        ) -> None:
            if machine._context.is_isolated:
                msg = "the scheduler fell over"
                raise RuntimeError(msg)
            await real_start_flow(machine, *args, **kwargs)  # pyright: ignore[reportArgumentType]

        monkeypatch.setattr(ControlFlowMachine, "start_flow", start_flow_that_fails_in_subflows)

        run = await start_flow(engine, monkeypatch, flow_name)

        subflow_text = "Failed to run flow G_subflow. Error: the scheduler fell over"
        group_message = f"Node 'G' encountered a problem: {subflow_text}"
        assert run.run_message == group_message
        assert run.result_details == FLOW_FAILURE_PREFIX + group_message
        assert [(event.node_name, event.error_message) for event in run.recorded.node_errors] == [("G", subflow_text)]
        assert [details_dict(event.error) for event in run.recorded.node_errors] == [_container_details(subflow_text)]
        run.recorded.assert_finish_errors_match_node_errors()
        assert_subflow_failures(run.subflow_runs, [("G_subflow", subflow_text)])


@pytest.mark.usefixtures("failure_output_library")
class _LoopFailureBase:
    """Builds a for-each group of fourteen items whose one body node fails."""

    @pytest.fixture(autouse=True)
    def _libraries(self, engine: Engine, tmp_path: Path, materialize_library: Callable[..., Path]) -> None:
        register_fixture_library(
            engine,
            tmp_path / "iterative_group_library",
            materialize_library,
            library_dir_name="iterative_group_library",
            node_file_name="iterative_group_nodes.py",
            name=LOOP_LIBRARY,
        )

    async def run_loop(
        self, engine: Engine, monkeypatch: pytest.MonkeyPatch, body_node_type: str, execution_mode: str
    ) -> FlowRun:
        """Run a fourteen-item for-each whose body node ``Body`` is of ``body_node_type``."""
        flow_name = new_flow(engine, "loop_failure_output")
        self.build_loop(engine, flow_name, body_node_type, execution_mode)
        return await start_flow(engine, monkeypatch, flow_name, within_flow_context=True)

    def build_loop(
        self,
        engine: Engine,
        flow_name: str,
        body_node_type: str,
        execution_mode: str,
        *,
        parent_group_name: str | None = None,
    ) -> None:
        """Create a fourteen-item for-each ``Loop`` in ``flow_name``, holding the body node ``Body``.

        The loop is created inside the group ``parent_group_name`` when one is given.
        """
        create_node_in_flow(
            engine,
            "FourteenItemForEachGroupNode",
            "Loop",
            flow_name,
            library_name=FAILURE_LIBRARY,
            parent_group_name=parent_group_name,
        )
        create_node_in_flow(
            engine, body_node_type, "Body", flow_name, library_name=FAILURE_LIBRARY, parent_group_name="Loop"
        )
        for source, source_parameter, target, target_parameter in (
            ("Loop", "on_each", "Body", "exec_in"),
            ("Loop", "index", "Body", "index"),
            ("Body", "exec_out", "Loop", "loop_complete"),
        ):
            connection = engine.handle_request(
                CreateConnectionRequest(
                    source_node_name=source,
                    source_parameter_name=source_parameter,
                    target_node_name=target,
                    target_parameter_name=target_parameter,
                )
            )
            assert connection.succeeded(), connection
        engine.handle_request(
            SetParameterValueRequest(parameter_name="execution_mode", node_name="Loop", value=execution_mode)
        )

    def assert_loop_failure_reported(self, run: FlowRun, loop_text: str, *, ordered: bool = True) -> None:
        """Assert what the loop reports as a whole, which every one of these loops reports alike.

        ``ordered=False`` is for iterations that finish in any order.
        """
        loop_message = f"Node 'Loop' encountered a problem: {loop_text}"
        assert isinstance(run.result, StartFlowResultFailure)
        assert run.run_message == loop_message
        assert run.result_details == FLOW_FAILURE_PREFIX + loop_message
        assert run.validation_exception_messages == [loop_message]
        loop_errors = [event for event in run.recorded.node_errors if event.node_name == "Loop"]
        assert [event.error_message for event in loop_errors] == [loop_text]
        assert run.recorded.node_errors[-1] is loop_errors[0], "the loop reports its failure after its body's"
        assert [details_dict(event.error) for event in loop_errors] == [_container_details(loop_text)]
        loop_finishes = [finish for finish in run.recorded.node_finishes if finish.node_name == "Loop"]
        assert [(finish.flow_name, finish.outcome, finish.error_message) for finish in loop_finishes] == [
            ("Flow", NodeProcessOutcome.FAILED, loop_text)
        ]
        assert [details_dict(finish.error) for finish in loop_finishes] == [_container_details(loop_text)]
        run.recorded.assert_finish_errors_match_node_errors(ordered=ordered)
        assert run.client == client_run_failure(FLOW_FAILURE_PREFIX + loop_message, [client_exception(loop_message)])


class TestSequentialLoop(_LoopFailureBase):
    """Golden case 11: a loop that runs its iterations one at a time reports every lost iteration.

    One line per distinct reason, ahead of which comes a summary. Every iteration runs in one copy of the body,
    so each line names the copy, ``Body_1``, and not the node the artist placed.
    """

    SUMMARY = "Attempted to run all 14 iterations of loop 'Loop'. Failed because 14 of them did not finish."

    @staticmethod
    def reason(copy_name: str, words: str) -> str:
        """The text a failing body copy reports, which each line of a loop's failure repeats."""
        return (
            f"Node '{copy_name}' execution failed: Attempted to execute node '{copy_name}'. Failed with error: {words}"
        )

    @pytest.mark.asyncio
    async def test_every_iteration_fails_for_the_same_reason(
        self, engine: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run = await self.run_loop(engine, monkeypatch, "ConstantFailingBodyNode", ONE_AT_A_TIME)

        body_text = self.reason("Body_1", "The body refused the item.")
        self.assert_loop_failure_reported(run, f"{self.SUMMARY}\n  Every iteration: {body_text}")
        self.assert_body_failures_reported_under_the_canvas_name(run, [body_text] * 14)

    @pytest.mark.asyncio
    async def test_more_reasons_than_a_loop_names(self, engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
        run = await self.run_loop(engine, monkeypatch, "DistinctFailingBodyNode", ONE_AT_A_TIME)

        lines = [
            f"  Iteration {number}: {self.reason('Body_1', f'The body refused item {number - 1}.')}"
            for number in range(1, 6)
        ]
        tail = "  ... and 9 more reason(s) affecting 9 iteration(s). See the engine log for every iteration."
        self.assert_loop_failure_reported(run, "\n".join([self.SUMMARY, *lines, tail]))
        self.assert_body_failures_reported_under_the_canvas_name(
            run, [self.reason("Body_1", f"The body refused item {number}.") for number in range(14)]
        )

    @pytest.mark.asyncio
    async def test_iterations_that_are_not_a_contiguous_run(
        self, engine: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Seven iterations share each reason, which is one more than a loop enumerates."""
        run = await self.run_loop(engine, monkeypatch, "ParityFailingBodyNode", ONE_AT_A_TIME)

        even = self.reason("Body_1", "The body refused an even item.")
        odd = self.reason("Body_1", "The body refused an odd item.")
        self.assert_loop_failure_reported(
            run,
            "\n".join(
                [
                    self.SUMMARY,
                    f"  Iterations 1, 3, 5, 7, 9, 11 (+1 more): {even}",
                    f"  Iterations 2, 4, 6, 8, 10, 12 (+1 more): {odd}",
                ]
            ),
        )
        self.assert_body_failures_reported_under_the_canvas_name(run, [even, odd] * 7)

    def assert_body_failures_reported_under_the_canvas_name(self, run: FlowRun, texts: list[str]) -> None:
        """Golden case 17: the body copy's events carry the canvas name, while its text names the copy."""
        body_errors = [event for event in run.recorded.node_errors if event.node_name != "Loop"]
        assert [(event.node_name, event.error_message) for event in body_errors] == [("Body", text) for text in texts]
        assert [event.error.exception_type for event in body_errors if event.error is not None] == [
            "builtins.RuntimeError"
        ] * len(texts)
        body_finishes = [finish for finish in run.recorded.node_finishes if finish.node_name != "Loop"]
        assert [
            (finish.node_name, finish.flow_name, finish.outcome, finish.error_message) for finish in body_finishes
        ] == [("Body", "Loop_subflow", NodeProcessOutcome.FAILED, text) for text in texts]
        assert [finish.error for finish in body_finishes] == [event.error for event in body_errors]
        assert [details_dict(event.error) for event in body_errors[:1]] == [
            expected_details(re.sub(r".*Failed with error: ", "", texts[0]), "builtins.RuntimeError")
        ]
        assert [event.run_seconds is not None for event in body_errors] == [True] * len(texts)
        assert_subflow_failures(run.subflow_runs, [("ControlFlow_1", text) for text in texts])


class TestParallelLoop(_LoopFailureBase):
    """Golden case 12: a loop that runs its iterations all at once.

    The group's failure is a count, and the reasons are only in a WARNING log line. Each iteration runs in a
    copy of its own, so the copies are ``Body_1`` to ``Body_14``. Their ``NodeErrorEvent`` names the copy, while
    their ``NodeFinishProcessEvent`` names the canvas node.
    """

    ITERATION_FAILURE_TAIL = (
        "  ... and 9 more reason(s) affecting 9 iteration(s). See the engine log for every iteration."
    )

    @staticmethod
    def reason(number: int, words: str) -> str:
        """The text the body copy of iteration ``number`` reports."""
        return (
            f"Node 'Body_{number}' execution failed: Attempted to execute node 'Body_{number}'. "
            f"Failed with error: {words}"
        )

    @pytest.mark.asyncio
    async def test_group(
        self, engine: Engine, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING):
            run = await self.run_loop(engine, monkeypatch, "ConstantFailingBodyNode", ALL_AT_ONCE)

        self.assert_loop_failure_reported(
            run, "Iterative group execution failed: 14 of 14 iterations failed", ordered=False
        )
        reasons = [
            f"  Iteration {number}: {self.reason(number, 'The body refused the item.')}" for number in range(1, 6)
        ]
        assert self.parallel_warnings(caplog) == [
            "Loop execution: 14 of 14 parallel iterations failed. Results will contain None for failed iterations.\n"
            + "\n".join([*reasons, self.ITERATION_FAILURE_TAIL])
        ]
        texts = {number: self.reason(number, "The body refused the item.") for number in range(1, 15)}
        self.assert_body_failures(run, texts)

    @pytest.mark.asyncio
    async def test_group_where_only_some_iterations_fail(
        self, engine: Engine, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Three of fourteen iterations fail, and that still fails the group, with only the count in its text."""
        with caplog.at_level(logging.WARNING):
            run = await self.run_loop(engine, monkeypatch, "SomeItemsFailingBodyNode", ALL_AT_ONCE)

        self.assert_loop_failure_reported(
            run, "Iterative group execution failed: 3 of 14 iterations failed", ordered=False
        )
        texts = {number: self.reason(number, f"The body refused item {number - 1}.") for number in (1, 6, 11)}
        assert self.parallel_warnings(caplog) == [
            "Loop execution: 3 of 14 parallel iterations failed. Results will contain None for failed iterations.\n"
            + "\n".join(f"  Iteration {number}: {text}" for number, text in texts.items())
        ]
        self.assert_body_failures(run, texts)
        succeeded = [finish for finish in run.recorded.node_finishes if finish.error_message is None]
        assert [(finish.node_name, finish.outcome) for finish in succeeded] == [
            ("Body", NodeProcessOutcome.SUCCEEDED)
        ] * 11

    def parallel_warnings(self, caplog: pytest.LogCaptureFixture) -> list[str]:
        """The WARNING lines the loop logs about its parallel iterations."""
        return [
            record.getMessage()
            for record in caplog.records
            if record.levelno == logging.WARNING and "parallel" in record.getMessage()
        ]

    def assert_body_failures(self, run: FlowRun, texts: dict[int, str]) -> None:
        """Assert the body copies' failures, by iteration number, as every audience heard them."""
        body_errors = sorted(
            (event for event in run.recorded.node_errors if event.node_name != "Loop"),
            key=lambda event: event.node_name.removeprefix("Body_").zfill(3),
        )
        assert [(event.node_name, event.error_message) for event in body_errors] == [
            (f"Body_{number}", text) for number, text in texts.items()
        ]
        assert [event.run_seconds is not None for event in body_errors] == [True] * len(texts)
        failed_finishes = [finish for finish in run.recorded.node_finishes if finish.error_message is not None]
        body_finishes = [finish for finish in failed_finishes if finish.node_name != "Loop"]
        assert {(finish.node_name, finish.flow_name, finish.outcome) for finish in body_finishes} == {
            ("Body", "Loop_subflow", NodeProcessOutcome.FAILED)
        }
        assert sorted(finish.error_message or "" for finish in body_finishes) == sorted(texts.values())
        assert sorted(repr(details_dict(finish.error)) for finish in body_finishes) == sorted(
            repr(details_dict(event.error)) for event in body_errors
        )
        assert {event.error.message for event in body_errors if event.error is not None} == {
            re.sub(r".*Failed with error: ", "", text) for text in texts.values()
        }
        by_flow = sorted(
            run.subflow_runs, key=lambda subflow: subflow.request.flow_name.removeprefix("ControlFlow_").zfill(3)
        )
        assert_subflow_failures(by_flow, [(f"ControlFlow_{number}", text) for number, text in texts.items()])


class TestStartAndEndNodeLoops:
    """Golden cases 11 and 12 for a loop built from Start and End nodes, where one of three iterations fails."""

    BODY_FAILURE = "FailOnSecondNode refuses 'second'"

    @pytest.fixture(autouse=True)
    def _loop_library(self, engine: Engine, tmp_path: Path, materialize_library: Callable[..., Path]) -> None:
        register_fixture_library(
            engine,
            tmp_path / "loop_library",
            materialize_library,
            library_dir_name="loop_library",
            node_file_name="loop_nodes.py",
            name=LOOP_LIBRARY,
        )

    async def run_loop(self, engine: Engine, monkeypatch: pytest.MonkeyPatch, *, run_in_order: bool) -> FlowRun:
        """Run Loop Start -> Body Node -> Loop End over three items, the second of which the body refuses."""
        flow_name = new_flow(engine, "start_end_loop_failure_output")
        # An iterative start node tethers an end node beside it, which needs a position to offset from.
        metadata = {"position": {"x": 0, "y": 0}}
        create_node_in_flow(
            engine, "LoopStartNode", "Loop Start", flow_name, library_name=LOOP_LIBRARY, metadata=metadata
        )
        create_node_in_flow(
            engine, "FailOnSecondNode", "Body Node", flow_name, library_name=LOOP_LIBRARY, metadata=metadata
        )
        end_node = engine.node_manager.get_node_by_name("Loop Start").end_node  # pyright: ignore[reportAttributeAccessIssue]
        assert end_node.name == "Loop End"
        engine.handle_request(
            SetParameterValueRequest(node_name="Loop Start", parameter_name="run_in_order", value=run_in_order)
        )
        for source, source_parameter, target, target_parameter in (
            ("Loop Start", "current_item", "Body Node", "text"),
            ("Loop Start", "exec_out", "Body Node", "exec_in"),
            ("Body Node", "exec_out", "Loop End", "add_item"),
        ):
            with engine.context_manager.flow(flow_name):
                connection = engine.handle_request(
                    CreateConnectionRequest(
                        source_node_name=source,
                        source_parameter_name=source_parameter,
                        target_node_name=target,
                        target_parameter_name=target_parameter,
                    )
                )
            assert connection.succeeded(), connection
        return await start_flow(engine, monkeypatch, flow_name, within_flow_context=True)

    @pytest.mark.asyncio
    async def test_sequential(self, engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
        """One reason on one iteration is named by its number, and the copy it names is ``Body Node_1``."""
        run = await self.run_loop(engine, monkeypatch, run_in_order=True)

        body_text = (
            f"Node 'Body Node_1' execution failed: Attempted to execute node 'Body Node_1'. "
            f"Failed with error: {self.BODY_FAILURE}"
        )
        loop_text = (
            "Attempted to run all 3 iterations of loop 'Loop End'. Failed because 1 of them did not finish.\n"
            f"  Iteration 2: {body_text}"
        )
        loop_message = f"Node 'Loop End' encountered a problem: {loop_text}"
        assert isinstance(run.result, StartFlowResultFailure)
        assert run.run_message == loop_message
        assert run.result_details == FLOW_FAILURE_PREFIX + loop_message
        assert [(event.node_name, event.error_message) for event in run.recorded.node_errors] == [
            ("Body Node", body_text),
            ("Loop End", loop_text),
        ]
        assert [details_dict(event.error) for event in run.recorded.node_errors] == [
            expected_details(self.BODY_FAILURE, "builtins.RuntimeError"),
            _container_details(loop_text),
        ]
        assert [
            (finish.node_name, finish.flow_name, finish.outcome, finish.error_message)
            for finish in run.recorded.node_finishes
            if finish.error_message is not None
        ] == [
            ("Body Node", "Flow", NodeProcessOutcome.FAILED, body_text),
            ("Loop End", "Flow", NodeProcessOutcome.FAILED, loop_text),
        ]
        assert [details_dict(finish.error) for finish in run.recorded.failed_finishes()] == [
            expected_details(self.BODY_FAILURE, "builtins.RuntimeError"),
            _container_details(loop_text),
        ]
        run.recorded.assert_finish_errors_match_node_errors()
        assert_subflow_failures(run.subflow_runs, [("ControlFlow_1", body_text)])
        assert run.client == client_run_failure(FLOW_FAILURE_PREFIX + loop_message, [client_exception(loop_message)])

    @pytest.mark.asyncio
    async def test_parallel(
        self, engine: Engine, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A failed parallel iteration is only logged: the run succeeds, and the copy's event names the copy."""
        with caplog.at_level(logging.WARNING):
            run = await self.run_loop(engine, monkeypatch, run_in_order=False)

        body_text = (
            f"Node 'Body Node_2' execution failed: Attempted to execute node 'Body Node_2'. "
            f"Failed with error: {self.BODY_FAILURE}"
        )
        # Known bug, pinned as current behaviour: the run reports success although an iteration failed, and
        # only the WARNING logs below say so.
        assert isinstance(run.result, StartFlowResultSuccess)
        assert run.run_message is None
        assert [(event.node_name, event.error_message) for event in run.recorded.node_errors] == [
            ("Body Node_2", body_text)
        ]
        assert [details_dict(event.error) for event in run.recorded.node_errors] == [
            expected_details(self.BODY_FAILURE, "builtins.RuntimeError")
        ]
        assert [
            (finish.node_name, finish.flow_name, finish.error_message)
            for finish in run.recorded.node_finishes
            if finish.error_message is not None
        ] == [("Body Node", "Flow", body_text)]
        assert [details_dict(finish.error) for finish in run.recorded.failed_finishes()] == [
            expected_details(self.BODY_FAILURE, "builtins.RuntimeError")
        ]
        run.recorded.assert_finish_errors_match_node_errors()
        assert_subflow_failures(run.subflow_runs, [("ControlFlow_2", body_text)])
        assert [
            record.getMessage()
            for record in caplog.records
            if record.levelno == logging.WARNING and record.getMessage().startswith("Loop execution:")
        ] == [
            (
                "Loop execution: 1 of 3 parallel iterations failed. Results will contain None for failed iterations.\n"
                f"  Iteration 2: {body_text}"
            ),
            "Loop execution: 1 of 3 iterations failed. Results will contain None for failed iterations.",
        ]


class TestLoopInsideAGroup(_LoopFailureBase):
    """Golden case 10, a loop in a group: the failing body is reported once at each of three levels.

    The body copy reports first, then the loop with the loop's own text, then the group framing the loop's
    run message. The group's subflow is the one that fails with that framed text.
    """

    BODY_WORDS = "The body refused the item."
    OUTER_SUBFLOW = "Outer_subflow"

    @pytest.fixture(autouse=True)
    def _subflow_library(self, engine: Engine, tmp_path: Path, materialize_library: Callable[..., Path]) -> None:
        register_fixture_library(
            engine,
            tmp_path / "subflow_library",
            materialize_library,
            library_dir_name="subflow_library",
            node_file_name="subflow_echo_node.py",
        )

    @staticmethod
    def body_text(copy_number: int) -> str:
        """The text the body copy of iteration ``copy_number`` reports."""
        return (
            f"Node 'Body_{copy_number}' execution failed: Attempted to execute node 'Body_{copy_number}'. "
            f"Failed with error: {TestLoopInsideAGroup.BODY_WORDS}"
        )

    @pytest.mark.asyncio
    async def test_sequential(self, engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
        run = await self.run_loop_in_group(engine, monkeypatch, ONE_AT_A_TIME)

        body_text = self.body_text(1)
        loop_text = (
            "Attempted to run all 14 iterations of loop 'Loop'. Failed because 14 of them did not finish.\n"
            f"  Every iteration: {body_text}"
        )
        self.assert_reported_through_the_group(run, loop_text, [body_text] * 14)
        assert_subflow_failures(
            run.subflow_runs,
            [("ControlFlow_1", body_text)] * 14
            + [(self.OUTER_SUBFLOW, f"Node 'Loop' encountered a problem: {loop_text}")],
        )

    @pytest.mark.asyncio
    async def test_parallel(self, engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
        run = await self.run_loop_in_group(engine, monkeypatch, ALL_AT_ONCE)

        loop_text = "Iterative group execution failed: 14 of 14 iterations failed"
        self.assert_reported_through_the_group(run, loop_text, [self.body_text(number) for number in range(1, 15)])
        by_flow = sorted(
            run.subflow_runs,
            key=lambda subflow: (
                subflow.request.flow_name == self.OUTER_SUBFLOW,
                subflow.request.flow_name.removeprefix("ControlFlow_").zfill(3),
            ),
        )
        assert_subflow_failures(
            by_flow,
            [(f"ControlFlow_{number}", self.body_text(number)) for number in range(1, 15)]
            + [(self.OUTER_SUBFLOW, f"Node 'Loop' encountered a problem: {loop_text}")],
        )

    async def run_loop_in_group(self, engine: Engine, monkeypatch: pytest.MonkeyPatch, execution_mode: str) -> FlowRun:
        """Run the loop of a failing body inside the group ``Outer``."""
        flow_name = new_flow(engine, "loop_in_group_failure_output")
        create_node_in_flow(engine, "SubflowGroupNode", "Outer", flow_name, library_name=SUBFLOW_LIBRARY)
        self.build_loop(engine, flow_name, "ConstantFailingBodyNode", execution_mode, parent_group_name="Outer")
        return await start_flow(engine, monkeypatch, flow_name, within_flow_context=True)

    def assert_reported_through_the_group(self, run: FlowRun, loop_text: str, body_texts: list[str]) -> None:
        """Assert every audience got the body's failures, then the loop's, then the group's."""
        outer_text = f"Node 'Loop' encountered a problem: {loop_text}"
        outer_message = f"Node 'Outer' encountered a problem: {outer_text}"
        assert isinstance(run.result, StartFlowResultFailure)
        assert run.run_message == outer_message
        assert run.result_details == FLOW_FAILURE_PREFIX + outer_message
        assert run.validation_exception_messages == [outer_message]
        errors = run.recorded.node_errors
        assert [(event.node_name, event.error_message) for event in errors[-2:]] == [
            ("Loop", loop_text),
            ("Outer", outer_text),
        ]
        body_errors = sorted(
            errors[:-2], key=lambda event: int(event.node_name.removeprefix("Body").removeprefix("_") or 1)
        )
        assert [event.error_message for event in body_errors] == body_texts
        assert [details_dict(event.error) for event in errors[-2:]] == [
            _container_details(loop_text),
            _container_details(outer_text),
        ]
        assert {event.error.message for event in body_errors if event.error is not None} == {self.BODY_WORDS}
        assert [event.run_seconds is not None for event in errors] == [True] * 16
        finishes = run.recorded.failed_finishes()
        assert [(finish.node_name, finish.flow_name, finish.outcome) for finish in finishes[-2:]] == [
            ("Loop", self.OUTER_SUBFLOW, NodeProcessOutcome.FAILED),
            ("Outer", "Flow", NodeProcessOutcome.FAILED),
        ]
        assert [(finish.error_message, details_dict(finish.error)) for finish in finishes[-2:]] == [
            (loop_text, _container_details(loop_text)),
            (outer_text, _container_details(outer_text)),
        ]
        assert sorted(finish.error_message or "" for finish in finishes[:-2]) == sorted(
            event.error_message for event in errors[:-2]
        )
        assert [finish.node_name for finish in finishes[:-2]] == ["Body"] * 14
        run.recorded.assert_finish_errors_match_node_errors(ordered=False)
        assert run.client == client_run_failure(FLOW_FAILURE_PREFIX + outer_message, [client_exception(outer_message)])
