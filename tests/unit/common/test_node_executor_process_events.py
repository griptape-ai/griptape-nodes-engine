"""Contract tests for the NodeStartProcessEvent and NodeFinishProcessEvent that NodeExecutor.execute() sends.

Pins down their order, fields, outcome, and how runs nest.
"""

import asyncio
from typing import Any, NamedTuple, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from griptape_nodes.common.loop_body_scope import LoopBodyScope, entered_loop_body_scope
from griptape_nodes.common.node_executor import (
    ExecuteNodeFailedError,
    NodeExecutor,
    current_executing_node_name,
    current_node_execution_id,
)
from griptape_nodes.exe_types.base_iterative_nodes import BaseIterativeEndNode
from griptape_nodes.exe_types.node_groups import (
    BaseIterativeNodeGroup,
    BaseWhileNodeGroup,
    SubflowNodeGroup,
)
from griptape_nodes.exe_types.node_types import ISOLATED_EXECUTION, LOCAL_EXECUTION
from griptape_nodes.retained_mode.events.base_events import ExecutionPayload
from griptape_nodes.retained_mode.events.execution_events import (
    ExecuteNodeResultFailure,
    ExecuteNodeResultSuccess,
    NodeFinishProcessEvent,
    NodeProcessOutcome,
    NodeStartProcessEvent,
)
from griptape_nodes.retained_mode.events.node_error_details import NodeErrorDetails


def _make_executor() -> NodeExecutor:
    executor = NodeExecutor(engine=MagicMock())
    mock_engine = cast("MagicMock", executor.engine)
    mock_engine.node_manager.get_node_parent_flow_by_name.return_value = "Canvas Flow"
    mock_engine.ahandle_request = AsyncMock(return_value=_success_result())
    return executor


def _make_node(name: str = "TestNode", library: str | None = "Test Library") -> MagicMock:
    node = MagicMock()
    node.name = name
    node.parameter_values = {}
    node.parameter_output_values = {}
    node.metadata = {}
    if library is not None:
        node.metadata["library"] = library
    node.is_cancellation_requested = False
    node.get_parameter_by_name.return_value = None
    node._list_rows.values_for_dispatch.side_effect = dict
    return node


def _make_spec_node(spec: type, name: str) -> MagicMock:
    node = MagicMock(spec=spec)
    node.name = name
    node.metadata = {}
    node.is_cancellation_requested = False
    return node


def _success_result() -> ExecuteNodeResultSuccess:
    return ExecuteNodeResultSuccess(result_details="ok", parameter_output_values={})


def _sent_payloads(executor: NodeExecutor) -> list[ExecutionPayload]:
    """The payloads execute() put on the event manager, in the order it put them."""
    put_event = cast("MagicMock", executor.engine).event_manager.put_event
    return [sent.args[0].wrapped_event.payload for sent in put_event.call_args_list]


def _node_events(executor: NodeExecutor) -> list[NodeStartProcessEvent | NodeFinishProcessEvent]:
    return [
        payload
        for payload in _sent_payloads(executor)
        if isinstance(payload, (NodeStartProcessEvent, NodeFinishProcessEvent))
    ]


class StartAndFinish(NamedTuple):
    start: NodeStartProcessEvent
    finish: NodeFinishProcessEvent


def _start_and_finish(executor: NodeExecutor) -> StartAndFinish:
    """The only two node events execute() sent: one run's start and finish."""
    events = _node_events(executor)
    assert len(events) == 2  # noqa: PLR2004
    start, finish = events
    assert isinstance(start, NodeStartProcessEvent)
    assert isinstance(finish, NodeFinishProcessEvent)
    return StartAndFinish(start=start, finish=finish)


class TestOrder:
    """Start goes out before the node does any work, and Finish after it."""

    @pytest.mark.asyncio
    async def test_start_is_sent_before_the_node_is_dispatched(self) -> None:
        executor = _make_executor()
        mock_engine = cast("MagicMock", executor.engine)
        seen_at_dispatch: list[list[ExecutionPayload]] = []

        async def dispatch(_request: Any) -> ExecuteNodeResultSuccess:
            seen_at_dispatch.append(list(_node_events(executor)))
            return _success_result()

        mock_engine.ahandle_request = AsyncMock(side_effect=dispatch)
        await executor.execute(_make_node())

        assert len(seen_at_dispatch) == 1
        assert [type(payload) for payload in seen_at_dispatch[0]] == [NodeStartProcessEvent]
        assert [type(payload) for payload in _node_events(executor)] == [
            NodeStartProcessEvent,
            NodeFinishProcessEvent,
        ]

    @pytest.mark.asyncio
    async def test_finish_is_sent_after_the_run_is_no_longer_current(self) -> None:
        executor = _make_executor()
        mock_engine = cast("MagicMock", executor.engine)
        seen_at_finish: list[tuple[str | None, str | None]] = []

        def put_event(event: Any) -> None:
            if isinstance(event.wrapped_event.payload, NodeFinishProcessEvent):
                seen_at_finish.append((current_executing_node_name.get(), current_node_execution_id.get()))

        mock_engine.event_manager.put_event.side_effect = put_event
        await executor.execute(_make_node())

        assert seen_at_finish == [(None, None)]


class TestFields:
    """The events describe the node as the artist placed it."""

    @pytest.mark.asyncio
    async def test_success_describes_the_node(self) -> None:
        executor = _make_executor()
        node = _make_node(name="Greeter", library="Greeting Library")

        await executor.execute(node)

        start, finish = _start_and_finish(executor)
        for event in (start, finish):
            assert event.node_name == "Greeter"
            assert event.node_type == "MagicMock"
            assert event.specific_library_name == "Greeting Library"
            assert event.flow_name == "Canvas Flow"
            assert event.parent_execution_id is None
        assert finish.outcome == NodeProcessOutcome.SUCCEEDED
        assert finish.error_message is None
        assert finish.error is None

    @pytest.mark.asyncio
    async def test_start_and_finish_of_one_run_share_an_execution_id(self) -> None:
        executor = _make_executor()

        await executor.execute(_make_node())

        start, finish = _start_and_finish(executor)
        assert start.execution_id
        assert finish.execution_id == start.execution_id

    @pytest.mark.asyncio
    async def test_each_run_gets_its_own_execution_id(self) -> None:
        executor = _make_executor()
        node = _make_node()

        await executor.execute(node)
        await executor.execute(node)

        execution_ids = {event.execution_id for event in _node_events(executor)}
        assert len(execution_ids) == 2  # noqa: PLR2004

    @pytest.mark.asyncio
    async def test_node_in_no_flow_reports_no_flow(self) -> None:
        executor = _make_executor()
        mock_engine = cast("MagicMock", executor.engine)
        mock_engine.node_manager.get_node_parent_flow_by_name.side_effect = KeyError("Node could not be found.")

        await executor.execute(_make_node())

        start, finish = _start_and_finish(executor)
        assert start.flow_name is None
        assert finish.flow_name is None

    @pytest.mark.asyncio
    async def test_node_with_no_library_reports_no_library(self) -> None:
        executor = _make_executor()

        await executor.execute(_make_node(library=None))

        start = _start_and_finish(executor).start
        assert start.specific_library_name is None


class TestOutcome:
    """The finish event says whether the run succeeded, failed, or was cancelled."""

    @pytest.mark.asyncio
    async def test_failure_result_reports_failed_with_the_nodes_own_details(self) -> None:
        executor = _make_executor()
        mock_engine = cast("MagicMock", executor.engine)
        built = NodeErrorDetails(message="Key 'b' not found", exception_type="builtins.KeyError")
        mock_engine.ahandle_request = AsyncMock(
            return_value=ExecuteNodeResultFailure(result_details="boom", exception=KeyError("b"), error=built)
        )

        with pytest.raises(ExecuteNodeFailedError) as caught:
            await executor.execute(_make_node(name="Broken"))

        finish = _start_and_finish(executor).finish
        assert finish.outcome == NodeProcessOutcome.FAILED
        assert finish.error is built
        assert finish.error_message == str(caught.value)

    @pytest.mark.asyncio
    async def test_engine_failure_reports_failed_with_the_engines_words(self) -> None:
        executor = _make_executor()
        mock_engine = cast("MagicMock", executor.engine)
        mock_engine.ahandle_request = AsyncMock(
            return_value=ExecuteNodeResultFailure(result_details="no worker is available")
        )

        with pytest.raises(ExecuteNodeFailedError):
            await executor.execute(_make_node(name="Broken"))

        finish = _start_and_finish(executor).finish
        assert finish.outcome == NodeProcessOutcome.FAILED
        assert finish.error is not None
        assert "no worker is available" in finish.error.message

    @pytest.mark.asyncio
    async def test_group_route_that_raises_reports_failed(self) -> None:
        executor = _make_executor()
        node = _make_spec_node(BaseIterativeEndNode, "EndLoop")
        raised = RuntimeError("Attempted to run all 3 iterations of loop 'EndLoop'. Failed because 1 did not finish.")

        with (
            patch.object(NodeExecutor, "handle_loop_execution", new_callable=AsyncMock, side_effect=raised),
            pytest.raises(RuntimeError),
        ):
            await executor.execute(node)

        finish = _start_and_finish(executor).finish
        assert finish.outcome == NodeProcessOutcome.FAILED
        assert finish.error_message == str(raised)
        assert finish.error is not None

    @pytest.mark.asyncio
    async def test_cancelled_run_reports_cancelled_and_stays_cancelled(self) -> None:
        executor = _make_executor()
        mock_engine = cast("MagicMock", executor.engine)
        mock_engine.ahandle_request = AsyncMock(side_effect=asyncio.CancelledError())

        with pytest.raises(asyncio.CancelledError):
            await executor.execute(_make_node())

        finish = _start_and_finish(executor).finish
        assert finish.outcome == NodeProcessOutcome.CANCELLED
        assert finish.error is None

    @pytest.mark.asyncio
    async def test_node_that_stops_early_after_a_cancel_request_reports_cancelled(self) -> None:
        executor = _make_executor()
        node = _make_node()
        node.is_cancellation_requested = True

        await executor.execute(node)

        finish = _start_and_finish(executor).finish
        assert finish.outcome == NodeProcessOutcome.CANCELLED


class TestEveryRoute:
    """Group and loop nodes, which never dispatch a request, are bracketed like any other node."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("spec", "route"),
        [
            (BaseWhileNodeGroup, "handle_while_group_execution"),
            (BaseIterativeNodeGroup, "handle_iterative_group_execution"),
            (BaseIterativeEndNode, "handle_loop_execution"),
        ],
    )
    async def test_loop_and_group_routes_send_one_start_and_one_finish(self, spec: type, route: str) -> None:
        executor = _make_executor()
        node = _make_spec_node(spec, "LoopOrGroup")

        with patch.object(NodeExecutor, route, new_callable=AsyncMock) as mock_route:
            await executor.execute(node)

        mock_route.assert_awaited_once_with(node)
        start, finish = _start_and_finish(executor)
        assert start.node_name == "LoopOrGroup"
        assert finish.outcome == NodeProcessOutcome.SUCCEEDED

    @pytest.mark.asyncio
    @pytest.mark.parametrize("execution_type", [LOCAL_EXECUTION, ISOLATED_EXECUTION, "Some Library"])
    async def test_subflow_group_routes_send_one_start_and_one_finish(self, execution_type: str) -> None:
        executor = _make_executor()
        node = _make_spec_node(SubflowNodeGroup, "Group")
        node.execution_environment = MagicMock()
        node.execution_environment.name = "execution_environment"
        node.get_parameter_value = MagicMock(return_value=execution_type)
        node.aprocess = AsyncMock()
        node.subflow_execution_component = MagicMock()

        with (
            patch.object(NodeExecutor, "_execute_private_workflow", new_callable=AsyncMock),
            patch.object(NodeExecutor, "_execute_library_workflow", new_callable=AsyncMock),
        ):
            await executor.execute(node)

        start, finish = _start_and_finish(executor)
        assert start.node_name == "Group"
        assert finish.outcome == NodeProcessOutcome.SUCCEEDED


class TestNesting:
    """A run inside another run names the outer run as its parent."""

    @pytest.mark.asyncio
    async def test_node_run_inside_a_loop_names_the_loop_as_its_parent(self) -> None:
        executor = _make_executor()
        loop_node = _make_spec_node(BaseIterativeEndNode, "EndLoop")
        body_node = _make_node(name="Body")

        async def run_body(_loop_node: Any) -> None:
            await executor.execute(body_node)

        with patch.object(NodeExecutor, "handle_loop_execution", new_callable=AsyncMock, side_effect=run_body):
            await executor.execute(loop_node)

        loop_start, body_start, body_finish, loop_finish = _node_events(executor)
        assert loop_start.node_name == "EndLoop"
        assert loop_start.parent_execution_id is None
        assert body_start.node_name == "Body"
        assert body_start.parent_execution_id == loop_start.execution_id
        assert body_finish.parent_execution_id == loop_start.execution_id
        assert loop_finish.execution_id == loop_start.execution_id

    @pytest.mark.asyncio
    async def test_runs_in_tasks_started_inside_a_run_name_it_as_their_parent(self) -> None:
        executor = _make_executor()
        loop_node = _make_spec_node(BaseIterativeEndNode, "EndLoop")

        async def run_bodies_concurrently(_loop_node: Any) -> None:
            await asyncio.gather(
                asyncio.ensure_future(executor.execute(_make_node(name="Body"))),
                asyncio.ensure_future(executor.execute(_make_node(name="Body"))),
            )

        with patch.object(
            NodeExecutor, "handle_loop_execution", new_callable=AsyncMock, side_effect=run_bodies_concurrently
        ):
            await executor.execute(loop_node)

        events = _node_events(executor)
        loop_start = events[0]
        body_starts = [
            event for event in events if isinstance(event, NodeStartProcessEvent) and event.node_name == "Body"
        ]
        assert len(body_starts) == 2  # noqa: PLR2004
        assert {event.parent_execution_id for event in body_starts} == {loop_start.execution_id}
        assert len({event.execution_id for event in body_starts}) == 2  # noqa: PLR2004


class TestLoopBodyScope:
    """Inside a loop body, copies are reported as the canvas nodes they stand for."""

    @pytest.mark.asyncio
    async def test_copy_reports_the_canvas_node_and_its_flow(self) -> None:
        executor = _make_executor()
        mock_engine = cast("MagicMock", executor.engine)
        mock_engine.node_manager.get_node_parent_flow_by_name.side_effect = {"Body": "Canvas Flow"}.__getitem__
        scope = LoopBodyScope(canvas_node_names={"Body_1": "Body"}, engine_node_names=frozenset())

        with entered_loop_body_scope(scope):
            await executor.execute(_make_node(name="Body_1"))

        start, finish = _start_and_finish(executor)
        assert start.node_name == "Body"
        assert finish.node_name == "Body"
        assert start.flow_name == "Canvas Flow"

    @pytest.mark.asyncio
    async def test_engine_made_node_runs_without_events(self) -> None:
        executor = _make_executor()
        mock_engine = cast("MagicMock", executor.engine)
        scope = LoopBodyScope(canvas_node_names={}, engine_node_names=frozenset({"Start_Package_MultiNode_1"}))

        with entered_loop_body_scope(scope):
            await executor.execute(_make_node(name="Start_Package_MultiNode_1"))

        mock_engine.ahandle_request.assert_awaited_once()
        assert _node_events(executor) == []
