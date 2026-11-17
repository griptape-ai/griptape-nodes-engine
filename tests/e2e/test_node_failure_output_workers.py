"""Golden tests that pin what a failing node reports when its library runs in a worker.

The worker is the simulated worker from ``tests.simulated_worker``: an engine of its own, sent the
orchestrator's node over the channel's wire form. A node that fails there loses its exception to the wire,
which keeps its type and its text, so the orchestrator words the failure from a rebuilt
``ForwardedException`` and puts the worker's type and traceback in the text.

A worker traceback's frames carry absolute paths and line numbers, so a golden pins that the block is there
and what it ends with, and not the frames.
"""

from __future__ import annotations

import copy
import json
from typing import TYPE_CHECKING, Any, ClassVar
from unittest.mock import ANY, AsyncMock

import pytest

from griptape_nodes.retained_mode.events.base_events import EventRequest, ForwardedException
from griptape_nodes.retained_mode.events.execution_events import ExecuteNodeRequest
from griptape_nodes.retained_mode.events.worker_events import WorkerGoneError
from griptape_nodes.retained_mode.managers.worker_manager import EVENT_RESULT, WorkerManager
from griptape_nodes.retained_mode.topics import default_response_topic
from tests.e2e.node_failure_support import (
    TRACEBACK_HEADER,
    client_exception,
    client_result,
    client_run_failure,
    details_dict,
    execute_node,
    expected_details,
    mask_worker_traceback,
    masked_client_form,
    new_flow,
    register_fixture_library,
    start_flow,
    traceback_ending_with,
    worker_traceback_block,
)
from tests.simulated_worker import SimulatedWorker, sent_from_open_workflow, simulated_worker

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from griptape_nodes.retained_mode.engine import Engine
    from tests.e2e.node_failure_support import FlowRun

pytestmark = pytest.mark.timeout(300, method="thread")

LEAF_LIBRARY_NAME = "Worker Leaf Library"
NODE_NAME = "Node1"
FLOW_FAILURE_PREFIX = "Attempted to run flow 'Flow'. Failed due to: "


class _WorkerFailureBase:
    """Registers the leaf library, runs a simulated worker for it, and creates the node under test."""

    @pytest.fixture
    def worker(
        self, engine: Engine, tmp_path: Path, materialize_library: Callable[..., Path]
    ) -> Iterator[SimulatedWorker]:
        """A worker that every node of the leaf library is sent to."""
        register_fixture_library(
            engine,
            tmp_path / "worker_leaf_library",
            materialize_library,
            library_dir_name="worker_leaf_library",
            node_file_name="worker_leaf_nodes.py",
        )
        with simulated_worker(engine, LEAF_LIBRARY_NAME) as simulated:
            yield simulated

    @pytest.fixture
    def node_flow(
        self, engine: Engine, create_node: Callable[..., str], worker: SimulatedWorker
    ) -> Callable[[str], str]:
        """A factory that makes a flow holding one leaf-library node of the given type, named ``Node1``."""

        def _make(node_type: str) -> str:
            assert worker.engine is not engine
            flow_name = new_flow(engine, "worker_failure_output")
            create_node(node_type, NODE_NAME, flow_name, library_name=LEAF_LIBRARY_NAME)
            return flow_name

        return _make

    def assert_failure_reported_once(self, run: FlowRun, run_message_head: str, details: dict, last_line: str) -> None:
        """Assert every audience got the failure with ``run_message_head`` and a worker traceback ending ``last_line``."""
        expected_message = run_message_head + worker_traceback_block(last_line)
        run_message = mask_worker_traceback(run.run_message or "")
        assert run_message.text == expected_message
        assert run_message.traceback is not None

        assert mask_worker_traceback(run.result_details).text == FLOW_FAILURE_PREFIX + expected_message
        assert [mask_worker_traceback(message).text for message in run.validation_exception_messages] == [
            expected_message
        ]
        assert [event.node_name for event in run.recorded.node_errors] == [NODE_NAME]
        [node_error] = run.recorded.node_errors
        assert mask_worker_traceback(node_error.error_message).text == expected_message
        assert mask_worker_traceback(node_error.error_message).traceback == run_message.traceback
        assert details_dict(node_error.error) == details
        [finish] = run.recorded.node_finishes
        assert finish.node_name == NODE_NAME
        assert mask_worker_traceback(finish.error_message or "").text == expected_message
        assert details_dict(finish.error) == details
        assert run.client == client_run_failure(
            FLOW_FAILURE_PREFIX + expected_message, [client_exception(expected_message)]
        )


class TestWorkerNodeRaisingABuiltin(_WorkerFailureBase):
    """Golden case 3: a worker node raises a builtin exception.

    The worker's exception type rides in square brackets ahead of the message, and its traceback follows the
    message. The details keep the node's own words without either.
    """

    HEAD = (
        "Node 'Node1' execution failed: [builtins.ValueError] Attempted to execute node 'Node1'. "
        "Failed with error: The worker refused the input value"
    )
    DETAILS = expected_details("The worker refused the input value", "builtins.ValueError")

    @pytest.mark.asyncio
    async def test_start_flow(
        self, engine: Engine, monkeypatch: pytest.MonkeyPatch, node_flow: Callable[[str], str]
    ) -> None:
        run = await start_flow(engine, monkeypatch, node_flow("WorkerValueErrorNode"))

        self.assert_failure_reported_once(
            run, self.HEAD, self.DETAILS, "ValueError: The worker refused the input value"
        )

    @pytest.mark.asyncio
    async def test_execute_node_request(self, engine: Engine, node_flow: Callable[[str], str]) -> None:
        node_flow("WorkerValueErrorNode")

        request, result = await execute_node(engine, NODE_NAME, "WorkerValueErrorNode", LEAF_LIBRARY_NAME)

        message = "Attempted to execute node 'Node1'. Failed with error: The worker refused the input value"
        assert str(result.result_details) == message
        assert isinstance(result.exception, ForwardedException)
        assert result.exception.original_type == "builtins.ValueError"
        assert str(result.exception) == "The worker refused the input value"
        assert result.exception.original_traceback is not None
        assert result.exception.original_traceback.startswith(TRACEBACK_HEADER)
        assert result.validation_exceptions is None
        assert details_dict(result.error) == self.DETAILS
        assert masked_client_form(client_result(request, result)) == {
            "altered_workflow_state": False,
            "result_details": {"result_details": [{"level": 40, "message": message}]},
            "exception": client_exception(
                "The worker refused the input value",
                "griptape_nodes.retained_mode.events.base_events.ForwardedException",
            ),
            "validation_exceptions": None,
            "error": self.DETAILS,
        }


class TestWorkerNodeRaisingANodeError(_WorkerFailureBase):
    """Golden case 3, ``WorkerFailingNode``: the prefix names the ``NodeError`` type by its full path."""

    HEAD = (
        "Node 'Node1' execution failed: [griptape_nodes.exe_types.node_error.NodeError] Attempted to execute "
        "node 'Node1'. Failed with error: The fixture node refused to run"
    )
    DETAILS = expected_details("The fixture node refused to run", "griptape_nodes.exe_types.node_error.NodeError")

    @pytest.mark.asyncio
    async def test_start_flow(
        self, engine: Engine, monkeypatch: pytest.MonkeyPatch, node_flow: Callable[[str], str]
    ) -> None:
        run = await start_flow(engine, monkeypatch, node_flow("WorkerFailingNode"))

        self.assert_failure_reported_once(
            run,
            self.HEAD,
            self.DETAILS,
            "griptape_nodes.exe_types.node_error.NodeError: The fixture node refused to run",
        )


class TestWorkerNodeDeclining(_WorkerFailureBase):
    """Golden case 4, worker half: ``validate_in_execution_environment`` fails where the node runs.

    The worker returns the problems as validation exceptions, so the failure reads as one that never ran,
    with no worker type and no traceback.
    """

    RUN_MESSAGE = "Node 'Node1' did not run because it failed validation: The worker's model is not installed"
    DETAILS = expected_details(
        "The worker's model is not installed",
        "builtins.ValueError",
        messages=["The worker's model is not installed"],
    )

    @pytest.mark.asyncio
    async def test_start_flow(
        self, engine: Engine, monkeypatch: pytest.MonkeyPatch, node_flow: Callable[[str], str]
    ) -> None:
        run = await start_flow(engine, monkeypatch, node_flow("WorkerDeclinesNode"))

        assert run.run_message == self.RUN_MESSAGE
        assert run.result_details == FLOW_FAILURE_PREFIX + self.RUN_MESSAGE
        assert run.validation_exception_messages == [self.RUN_MESSAGE]
        [node_error] = run.recorded.node_errors
        assert (node_error.node_name, node_error.error_message) == (NODE_NAME, self.RUN_MESSAGE)
        assert details_dict(node_error.error) == self.DETAILS
        [finish] = run.recorded.node_finishes
        assert finish.error_message == self.RUN_MESSAGE
        assert details_dict(finish.error) == self.DETAILS
        assert run.client == client_run_failure(
            FLOW_FAILURE_PREFIX + self.RUN_MESSAGE, [client_exception(self.RUN_MESSAGE)]
        )

    @pytest.mark.asyncio
    async def test_execute_node_request(self, engine: Engine, node_flow: Callable[[str], str]) -> None:
        node_flow("WorkerDeclinesNode")

        request, result = await execute_node(engine, NODE_NAME, "WorkerDeclinesNode", LEAF_LIBRARY_NAME)

        message = "Attempted to execute node 'Node1'. It declined to run: The worker's model is not installed"
        assert str(result.result_details) == message
        assert result.exception is None
        assert [
            (type(exception), exception.original_type, str(exception))
            for exception in result.validation_exceptions or []
            if isinstance(exception, ForwardedException)
        ] == [(ForwardedException, "builtins.ValueError", "The worker's model is not installed")]
        assert details_dict(result.error) == self.DETAILS
        assert masked_client_form(client_result(request, result)) == {
            "altered_workflow_state": False,
            "result_details": {"result_details": [{"level": 40, "message": message}]},
            "exception": None,
            "validation_exceptions": [
                client_exception(
                    "The worker's model is not installed",
                    "griptape_nodes.retained_mode.events.base_events.ForwardedException",
                )
            ],
            "error": self.DETAILS,
        }


class TestEngineFailuresAroundAWorker(_WorkerFailureBase):
    """Golden case 7: failures the engine words itself, because no node raised anything."""

    @pytest.fixture
    def worker_gone(self, engine: Engine, monkeypatch: pytest.MonkeyPatch, worker: SimulatedWorker) -> None:
        """Make the worker go away, at the moment a node is sent to it."""
        assert worker.engine is not engine
        monkeypatch.setattr(
            engine.worker_manager,
            "route_to_worker",
            AsyncMock(
                spec=WorkerManager.route_to_worker,
                side_effect=WorkerGoneError("The worker for 'Worker Leaf Library' was stopped."),
            ),
        )

    @pytest.mark.usefixtures("worker_gone")
    @pytest.mark.asyncio
    async def test_worker_gone(
        self,
        engine: Engine,
        monkeypatch: pytest.MonkeyPatch,
        node_flow: Callable[[str], str],
    ) -> None:
        flow_name = node_flow("WorkerFailingNode")

        run = await start_flow(engine, monkeypatch, flow_name)

        words = (
            "Attempted to run node 'Node1' in a separate process. Failed because The worker for "
            "'Worker Leaf Library' was stopped. Editing the node still works and your workflow keeps it."
        )
        run_message = f"Node 'Node1' execution failed: {words}"
        details = expected_details(words, "griptape_nodes.retained_mode.events.worker_events.WorkerGoneError")
        assert run.run_message == run_message
        assert run.result_details == FLOW_FAILURE_PREFIX + run_message
        assert run.validation_exception_messages == [run_message]
        [node_error] = run.recorded.node_errors
        assert (node_error.node_name, node_error.error_message) == (NODE_NAME, run_message)
        assert details_dict(node_error.error) == details
        [finish] = run.recorded.node_finishes
        assert finish.error_message == run_message
        assert details_dict(finish.error) == details
        assert run.client == client_run_failure(FLOW_FAILURE_PREFIX + run_message, [client_exception(run_message)])

    @pytest.mark.usefixtures("worker_gone")
    @pytest.mark.asyncio
    async def test_worker_gone_execute_node_request(self, engine: Engine, node_flow: Callable[[str], str]) -> None:
        node_flow("WorkerFailingNode")

        request, result = await execute_node(engine, NODE_NAME, "WorkerFailingNode", LEAF_LIBRARY_NAME)

        words = (
            "Attempted to run node 'Node1' in a separate process. Failed because The worker for "
            "'Worker Leaf Library' was stopped. Editing the node still works and your workflow keeps it."
        )
        assert str(result.result_details) == words
        assert isinstance(result.exception, WorkerGoneError)
        assert details_dict(result.error) is None
        assert masked_client_form(client_result(request, result)) == {
            "altered_workflow_state": False,
            "result_details": {"result_details": [{"level": 40, "message": words}]},
            "exception": client_exception(
                "The worker for 'Worker Leaf Library' was stopped.",
                "griptape_nodes.retained_mode.events.worker_events.WorkerGoneError",
                traceback_ending_with(
                    "griptape_nodes.retained_mode.events.worker_events.WorkerGoneError: "
                    "The worker for 'Worker Leaf Library' was stopped."
                ),
            ),
            "validation_exceptions": None,
            "error": None,
        }

    @pytest.mark.asyncio
    async def test_outputs_that_cannot_be_sent(
        self, engine: Engine, monkeypatch: pytest.MonkeyPatch, node_flow: Callable[[str], str]
    ) -> None:
        run = await start_flow(engine, monkeypatch, node_flow("WorkerUnsendableOutputNode"))

        words = (
            "Attempted to send node 'Node1' output 'blob' (A 'object' value has no plain-data form.) out of "
            "'Worker Leaf Library's isolated process. Failed because it has no plain-data form. Give the value a "
            "plain-data form, or declare its parameter serializable=False so the value stays in that process and "
            "the next node receives a reference to it."
        )
        run_message = f"Node 'Node1' execution failed: {words}"
        details = expected_details(words, None)
        assert run.run_message == run_message
        assert run.result_details == FLOW_FAILURE_PREFIX + run_message
        assert run.validation_exception_messages == [run_message]
        [node_error] = run.recorded.node_errors
        assert (node_error.node_name, node_error.error_message) == (NODE_NAME, run_message)
        assert details_dict(node_error.error) == details
        [finish] = run.recorded.node_finishes
        assert finish.error_message == run_message
        assert details_dict(finish.error) == details
        assert run.client == client_run_failure(FLOW_FAILURE_PREFIX + run_message, [client_exception(run_message)])


async def _send_message(topic: str, body: str, response_topic: str | None) -> None:
    """The shape of the function the orchestrator sends its clients messages with."""


class TestRelayOfAWorkerReply(_WorkerFailureBase):
    """The reply a worker sends for a failed node, relayed on to the editor.

    The reply is the one a simulated worker really produces for ``WorkerValueErrorNode``, in the form its
    channel carries. Relayed as it is, the editor receives the whole of it, so what the result carries is
    pinned key for key.
    """

    RESULT: ClassVar[dict[str, Any]] = {
        "altered_workflow_state": False,
        "error": expected_details("The worker refused the input value", "builtins.ValueError"),
        "exception": client_exception(
            "The worker refused the input value",
            "builtins.ValueError",
            traceback_ending_with("ValueError: The worker refused the input value"),
        ),
        "result_details": {
            "result_details": [
                {
                    "level": 40,
                    "message": "Attempted to execute node 'Node1'. Failed with error: The worker refused the input value",
                }
            ]
        },
        "validation_exceptions": None,
    }

    @pytest.fixture
    def sent_bodies(self) -> list[dict[str, Any]]:
        """The JSON bodies the orchestrator sent its clients."""
        return []

    @pytest.fixture
    def send_message(self, engine: Engine, sent_bodies: list[dict[str, Any]]) -> AsyncMock:
        """A transport attached to the orchestrator that records what it is asked to send."""

        async def record(topic: str, body: str, response_topic: str | None) -> None:  # noqa: ARG001
            sent_bodies.append(json.loads(body))

        send = AsyncMock(spec=_send_message, side_effect=record)
        engine.worker_manager.attach_transport(send_message=send)
        return send

    async def worker_reply(self, engine: Engine, worker: SimulatedWorker) -> dict[str, Any]:
        """The reply the worker sends when ``Node1``, a ``WorkerValueErrorNode``, fails."""
        request = sent_from_open_workflow(
            ExecuteNodeRequest(
                node_name=NODE_NAME,
                node_metadata={"node_type": "WorkerValueErrorNode", "library": LEAF_LIBRARY_NAME},
            ),
            engine,
        )
        return await worker.route_to_worker(EventRequest(request=request))

    def assert_relayed_failure(
        self, engine: Engine, send_message: AsyncMock, sent_bodies: list[dict[str, Any]]
    ) -> None:
        """Assert the one message sent was the failure, on the session topic, with its whole result."""
        relay_topic = default_response_topic(engine.get_engine_id(), engine.get_session_id())
        send_message.assert_awaited_once_with("failure_result", ANY, relay_topic)
        [body] = sent_bodies
        assert body["event_type"] == "EventResultFailure"
        assert body["request_type"] == "ExecuteNodeRequest"
        assert body["result_type"] == "ExecuteNodeResultFailure"
        assert body["response_topic"] == relay_topic
        assert set(body["result"]) == set(self.RESULT)
        assert masked_client_form(body["result"]) == self.RESULT

    @pytest.mark.asyncio
    async def test_unclaimed_reply(
        self,
        engine: Engine,
        node_flow: Callable[[str], str],
        worker: SimulatedWorker,
        send_message: AsyncMock,
        sent_bodies: list[dict[str, Any]],
    ) -> None:
        node_flow("WorkerValueErrorNode")
        reply = await self.worker_reply(engine, worker)

        await engine.worker_manager.relay_worker_result(reply)

        self.assert_relayed_failure(engine, send_message, sent_bodies)

    @pytest.mark.asyncio
    async def test_result_a_child_worker_sends_as_an_event(
        self,
        engine: Engine,
        node_flow: Callable[[str], str],
        worker: SimulatedWorker,
        send_message: AsyncMock,
        sent_bodies: list[dict[str, Any]],
    ) -> None:
        """A child worker's ``EventResult`` notification is relayed through ``relay_worker_result`` too."""
        node_flow("WorkerValueErrorNode")
        reply = await self.worker_reply(engine, worker)

        await engine.worker_manager._relay_worker_event(EVENT_RESULT, copy.deepcopy(reply))

        self.assert_relayed_failure(engine, send_message, sent_bodies)
