"""Golden tests that pin what a failing node reports, for a node that runs in this process.

Each test runs a real flow through a real engine and pins, as exact strings and dicts, every
audience's view of the failure:

- the ``NodeErrorEvent`` and ``NodeFinishProcessEvent`` the editor hears (``error_message``, and
  ``error`` as a whole ``NodeErrorDetails``),
- the failure message the run's resolution machine recorded,
- the result the request returns,
- the ``converters.client`` form of that result, which is what MCP clients receive.

They pin the output exactly, odd corners included, so a change to how failures are carried shows up
as a changed string rather than a silent difference. Traceback frames carry absolute paths and line
numbers of the engine's own source, so a golden pins where a traceback is and not what is in it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from griptape_nodes.retained_mode.events.connection_events import CreateConnectionRequest
from griptape_nodes.retained_mode.events.execution_events import (
    ExecuteNodeRequest,
    ExecuteNodeResultFailure,
    NodeProcessOutcome,
    ResolveNodeRequest,
    ResolveNodeResultFailure,
    StartFlowResultFailure,
)
from griptape_nodes.retained_mode.managers.node_manager import NodeManager
from tests.e2e.node_failure_support import (
    client_exception,
    client_result,
    client_run_failure,
    details_dict,
    execute_node,
    expected_details,
    masked_client_form,
    new_flow,
    record_failures,
    start_flow,
    traceback_ending_with,
)
from tests.simulated_worker import sent_from_open_workflow

if TYPE_CHECKING:
    from collections.abc import Callable

    from griptape_nodes.retained_mode.engine import Engine

pytestmark = [pytest.mark.timeout(120, method="thread"), pytest.mark.usefixtures("failure_output_library")]

LIBRARY_NAME = "Failure Output Library"
NODE_NAME = "Node1"
FLOW_FAILURE_PREFIX = "Attempted to run flow 'Flow'. Failed due to: "


@pytest.fixture
def node_flow(engine: Engine, create_node: Callable[..., str]) -> Callable[[str], str]:
    """Return a factory that makes a flow holding one node of the given type, named ``Node1``."""

    def _make(node_type: str) -> str:
        flow_name = new_flow(engine, "node_failure_output")
        create_node(node_type, NODE_NAME, flow_name, library_name=LIBRARY_NAME)
        return flow_name

    return _make


class TestKeyErrorNamingItsNode:
    """Golden case 1: a KeyError whose text starts with the node's name.

    ``str(KeyError)`` keeps its quotes and the name prefix, so the flattened message shows them, while
    the structured details strip both.
    """

    RAW = "'Node1: some sentence'"
    RUN_MESSAGE = f"Node 'Node1' execution failed: Attempted to execute node 'Node1'. Failed with error: {RAW}"
    DETAILS = expected_details("some sentence", "builtins.KeyError")

    @pytest.mark.asyncio
    async def test_start_flow(
        self, engine: Engine, monkeypatch: pytest.MonkeyPatch, node_flow: Callable[[str], str]
    ) -> None:
        run = await start_flow(engine, monkeypatch, node_flow("KeyErrorNode"))

        assert isinstance(run.result, StartFlowResultFailure)
        assert run.run_message == self.RUN_MESSAGE
        assert run.result_details == FLOW_FAILURE_PREFIX + self.RUN_MESSAGE
        assert run.validation_exception_messages == [self.RUN_MESSAGE]
        assert [(event.node_name, event.error_message) for event in run.recorded.node_errors] == [
            (NODE_NAME, self.RUN_MESSAGE)
        ]
        assert [details_dict(event.error) for event in run.recorded.node_errors] == [self.DETAILS]
        assert [(event.node_name, event.outcome) for event in run.recorded.node_finishes] == [
            (NODE_NAME, NodeProcessOutcome.FAILED)
        ]
        [finish] = run.recorded.node_finishes
        assert finish.error_message == self.RUN_MESSAGE
        assert details_dict(finish.error) == self.DETAILS
        run.recorded.assert_finish_errors_match_node_errors()
        assert [event.run_seconds is not None for event in run.recorded.node_errors] == [True], (
            "a node that was dispatched reports how long it ran"
        )
        assert run.client == client_run_failure(
            FLOW_FAILURE_PREFIX + self.RUN_MESSAGE, [client_exception(self.RUN_MESSAGE)]
        )

    @pytest.mark.asyncio
    async def test_execute_node_request(self, engine: Engine, node_flow: Callable[[str], str]) -> None:
        node_flow("KeyErrorNode")

        request, result = await execute_node(engine, NODE_NAME, "KeyErrorNode", LIBRARY_NAME)

        assert str(result.result_details) == f"Attempted to execute node 'Node1'. Failed with error: {self.RAW}"
        assert isinstance(result.exception, KeyError)
        assert str(result.exception) == self.RAW
        assert result.validation_exceptions is None
        assert details_dict(result.error) == self.DETAILS
        assert masked_client_form(client_result(request, result)) == {
            "altered_workflow_state": False,
            "result_details": {
                "result_details": [
                    {"level": 40, "message": f"Attempted to execute node 'Node1'. Failed with error: {self.RAW}"}
                ]
            },
            "exception": client_exception(
                self.RAW, "builtins.KeyError", traceback_ending_with(f"KeyError: {self.RAW}")
            ),
            "validation_exceptions": None,
            "error": self.DETAILS,
        }


class TestNodeErrorWithFieldsResponseAndLinks:
    """Golden case 2: a NodeError that carries fields, a response body and links."""

    MESSAGE = "The image service could not process the request: proxy client error."
    RUN_MESSAGE = f"Node 'Node1' execution failed: Attempted to execute node 'Node1'. Failed with error: {MESSAGE}"
    DETAILS = expected_details(
        MESSAGE,
        "griptape_nodes.exe_types.node_error.NodeError",
        fields={"generation_id": "90dbcfa0-ac4b-4feb-b234-0badff151ee2", "status": "ERRORED"},
        response={"status": "ERRORED", "status_detail": {"details": "proxy client error"}},
        links=[{"label": "Error handling guide", "url": "https://example.com/errors"}],
    )

    @pytest.mark.asyncio
    async def test_start_flow(
        self, engine: Engine, monkeypatch: pytest.MonkeyPatch, node_flow: Callable[[str], str]
    ) -> None:
        run = await start_flow(engine, monkeypatch, node_flow("ProviderErrorNode"))

        assert run.run_message == self.RUN_MESSAGE
        assert run.result_details == FLOW_FAILURE_PREFIX + self.RUN_MESSAGE
        assert run.validation_exception_messages == [self.RUN_MESSAGE]
        assert [(event.node_name, event.error_message) for event in run.recorded.node_errors] == [
            (NODE_NAME, self.RUN_MESSAGE)
        ]
        assert [details_dict(event.error) for event in run.recorded.node_errors] == [self.DETAILS]
        [finish] = run.recorded.node_finishes
        assert finish.error_message == self.RUN_MESSAGE
        assert details_dict(finish.error) == self.DETAILS
        run.recorded.assert_finish_errors_match_node_errors()
        assert run.client == client_run_failure(
            FLOW_FAILURE_PREFIX + self.RUN_MESSAGE, [client_exception(self.RUN_MESSAGE)]
        )

    @pytest.mark.asyncio
    async def test_execute_node_request(self, engine: Engine, node_flow: Callable[[str], str]) -> None:
        node_flow("ProviderErrorNode")

        request, result = await execute_node(engine, NODE_NAME, "ProviderErrorNode", LIBRARY_NAME)

        assert str(result.result_details) == f"Attempted to execute node 'Node1'. Failed with error: {self.MESSAGE}"
        assert details_dict(result.error) == self.DETAILS
        assert masked_client_form(client_result(request, result)) == {
            "altered_workflow_state": False,
            "result_details": {
                "result_details": [
                    {"level": 40, "message": f"Attempted to execute node 'Node1'. Failed with error: {self.MESSAGE}"}
                ]
            },
            "exception": client_exception(
                self.MESSAGE,
                "griptape_nodes.exe_types.node_error.NodeError",
                traceback_ending_with(f"griptape_nodes.exe_types.node_error.NodeError: {self.MESSAGE}"),
            ),
            "validation_exceptions": None,
            "error": self.DETAILS,
        }


class TestNodeDecliningToRunInProcess:
    """Golden case 4, in-process half: ``validate_in_execution_environment`` returns problems.

    The node never runs, and the executor words it as a validation failure rather than a crash.
    """

    REASONS = "The model is not installed.; The GPU is busy."
    RUN_MESSAGE = f"Node 'Node1' did not run because it failed validation: {REASONS}"
    DETAILS = expected_details(
        "The model is not installed.",
        "builtins.ValueError",
        messages=["The model is not installed.", "The GPU is busy."],
    )

    @pytest.mark.asyncio
    async def test_start_flow(
        self, engine: Engine, monkeypatch: pytest.MonkeyPatch, node_flow: Callable[[str], str]
    ) -> None:
        run = await start_flow(engine, monkeypatch, node_flow("DeclinesInExecutionEnvironmentNode"))

        assert run.run_message == self.RUN_MESSAGE
        assert run.result_details == FLOW_FAILURE_PREFIX + self.RUN_MESSAGE
        assert run.validation_exception_messages == [self.RUN_MESSAGE]
        assert [(event.node_name, event.error_message) for event in run.recorded.node_errors] == [
            (NODE_NAME, self.RUN_MESSAGE)
        ]
        assert [details_dict(event.error) for event in run.recorded.node_errors] == [self.DETAILS]
        [finish] = run.recorded.node_finishes
        assert finish.error_message == self.RUN_MESSAGE
        assert details_dict(finish.error) == self.DETAILS
        run.recorded.assert_finish_errors_match_node_errors()
        assert run.client == client_run_failure(
            FLOW_FAILURE_PREFIX + self.RUN_MESSAGE, [client_exception(self.RUN_MESSAGE)]
        )

    @pytest.mark.asyncio
    async def test_execute_node_request(self, engine: Engine, node_flow: Callable[[str], str]) -> None:
        node_flow("DeclinesInExecutionEnvironmentNode")

        request, result = await execute_node(engine, NODE_NAME, "DeclinesInExecutionEnvironmentNode", LIBRARY_NAME)

        assert str(result.result_details) == f"Attempted to execute node 'Node1'. It declined to run: {self.REASONS}"
        assert result.exception is None
        assert [(type(exception), str(exception)) for exception in result.validation_exceptions or []] == [
            (ValueError, "The model is not installed."),
            (RuntimeError, "The GPU is busy."),
        ]
        assert details_dict(result.error) == self.DETAILS
        assert masked_client_form(client_result(request, result)) == {
            "altered_workflow_state": False,
            "result_details": {
                "result_details": [
                    {"level": 40, "message": f"Attempted to execute node 'Node1'. It declined to run: {self.REASONS}"}
                ]
            },
            "exception": None,
            "validation_exceptions": [
                client_exception("The model is not installed.", "builtins.ValueError"),
                client_exception("The GPU is busy.", "builtins.RuntimeError"),
            ],
            "error": self.DETAILS,
        }


class TestValidateBeforeNodeRunFailing:
    """Golden case 5: ``validate_before_node_run`` fails in the DAG, so the node is never dispatched.

    The recorded message embeds the list's repr, and so does ``NodeErrorEvent.error_message``.
    """

    PROBLEMS = "[ValueError(\"Connect an image to 'Input Image'.\"), NodeError(\"'Prompt' is empty.\")]"
    RUN_MESSAGE = f"Node 'Node1' encountered problems: {PROBLEMS}"
    DETAILS = expected_details(
        "Connect an image to 'Input Image'.",
        "builtins.ValueError",
        messages=["Connect an image to 'Input Image'.", "'Prompt' is empty."],
    )

    @pytest.mark.asyncio
    async def test_start_flow(
        self, engine: Engine, monkeypatch: pytest.MonkeyPatch, node_flow: Callable[[str], str]
    ) -> None:
        run = await start_flow(engine, monkeypatch, node_flow("ValidateBeforeRunNode"))

        assert run.run_message == self.RUN_MESSAGE
        assert run.result_details == FLOW_FAILURE_PREFIX + self.RUN_MESSAGE
        assert run.validation_exception_messages == [self.RUN_MESSAGE]
        assert [(event.node_name, event.error_message) for event in run.recorded.node_errors] == [
            (NODE_NAME, self.PROBLEMS)
        ]
        assert [details_dict(event.error) for event in run.recorded.node_errors] == [self.DETAILS]
        assert run.recorded.node_finishes == [], "a node that was never dispatched never started"
        assert [event.run_seconds for event in run.recorded.node_errors] == [None], (
            "a node that was never dispatched did not run, so it reports no run time"
        )
        assert run.client == client_run_failure(
            FLOW_FAILURE_PREFIX + self.RUN_MESSAGE, [client_exception(self.RUN_MESSAGE)]
        )


class TestParameterPassthroughFailing:
    """Golden case 6: handing an upstream value to the node's input is refused, before the node runs."""

    DETAILS_TEXT = (
        "Failed to set parameter value for node 'Refuses' and parameter 'text'. Details: Attempted to set "
        "parameter value for 'Refuses.text'. Failed because before_value_set hook raised exception: "
        "The text cannot be 'poison'."
    )
    RUN_MESSAGE = f"Parameter passthrough failed for node 'Refuses': {DETAILS_TEXT}"

    @pytest.mark.asyncio
    async def test_start_flow(
        self, engine: Engine, monkeypatch: pytest.MonkeyPatch, create_node: Callable[..., str]
    ) -> None:
        flow_name = new_flow(engine, "node_failure_output_passthrough")
        create_node("PoisonSourceNode", "Source", flow_name, library_name=LIBRARY_NAME)
        create_node("RefusesPoisonInputNode", "Refuses", flow_name, library_name=LIBRARY_NAME)
        for source_parameter, target_parameter in (("exec_out", "exec_in"), ("text", "text")):
            result = engine.handle_request(
                CreateConnectionRequest(
                    source_node_name="Source",
                    source_parameter_name=source_parameter,
                    target_node_name="Refuses",
                    target_parameter_name=target_parameter,
                )
            )
            assert result.succeeded(), result

        run = await start_flow(engine, monkeypatch, flow_name)

        assert run.run_message == self.RUN_MESSAGE
        assert run.result_details == FLOW_FAILURE_PREFIX + self.RUN_MESSAGE
        assert run.validation_exception_messages == [self.RUN_MESSAGE]
        assert [(event.node_name, event.error_message) for event in run.recorded.node_errors] == [
            ("Refuses", self.DETAILS_TEXT)
        ]
        assert [details_dict(event.error) for event in run.recorded.node_errors] == [
            expected_details(self.DETAILS_TEXT, "builtins.RuntimeError")
        ]
        assert [event.node_name for event in run.recorded.node_finishes] == ["Source"]
        assert run.recorded.failed_finishes() == []
        assert run.client == client_run_failure(
            FLOW_FAILURE_PREFIX + self.RUN_MESSAGE, [client_exception(self.RUN_MESSAGE)]
        )


class TestFlowValidationFailing:
    """Golden case 14, flow-validation half: a node refuses the run from ``validate_before_workflow_run``."""

    RESULT_DETAILS = (
        "Couldn't start flow with name Flow. Flow Validation Failed.\n"
        "\tThe prompt is missing.\n"
        "\tThe seed is out of range."
    )

    @pytest.mark.asyncio
    async def test_start_flow(
        self, engine: Engine, monkeypatch: pytest.MonkeyPatch, node_flow: Callable[[str], str]
    ) -> None:
        run = await start_flow(engine, monkeypatch, node_flow("FailsWorkflowValidationNode"))

        assert isinstance(run.result, StartFlowResultFailure)
        assert run.result_details == self.RESULT_DETAILS
        assert [(type(exception), str(exception)) for exception in run.result.validation_exceptions or []] == [
            (ValueError, "The prompt is missing."),
            (ValueError, "The seed is out of range."),
        ]
        assert run.run_message is None
        assert run.recorded.node_errors == []
        assert run.recorded.node_finishes == []
        assert run.client == client_run_failure(
            self.RESULT_DETAILS,
            [
                client_exception("The prompt is missing.", "builtins.ValueError"),
                client_exception("The seed is out of range.", "builtins.ValueError"),
            ],
        )


class TestResolveNodeOnFailingNode:
    """Golden case 16: ``ResolveNodeRequest`` on a node that fails.

    The result words the failure ``Failed to resolve "X".  Error: ...``, with two spaces after the
    full stop, and its exception is the one the resolution raised, so its traceback is that of the engine
    code that raised it.
    """

    RUN_MESSAGE = (
        "Node 'Node1' execution failed: Attempted to execute node 'Node1'. Failed with error: 'Node1: some sentence'"
    )

    @pytest.mark.asyncio
    async def test_key_error_node(
        self, engine: Engine, monkeypatch: pytest.MonkeyPatch, node_flow: Callable[[str], str]
    ) -> None:
        node_flow("KeyErrorNode")
        recorded = record_failures(engine, monkeypatch)
        request = ResolveNodeRequest(node_name=NODE_NAME)

        result = await engine.ahandle_request(request)

        assert isinstance(result, ResolveNodeResultFailure)
        assert str(result.result_details) == f'Failed to resolve "Node1".  Error: {self.RUN_MESSAGE}'
        assert [(type(exception), str(exception)) for exception in result.validation_exceptions or []] == [
            (RuntimeError, self.RUN_MESSAGE)
        ]
        assert [(event.node_name, event.error_message) for event in recorded.node_errors] == [
            (NODE_NAME, self.RUN_MESSAGE)
        ]
        assert [details_dict(event.error) for event in recorded.node_errors] == [
            expected_details("some sentence", "builtins.KeyError")
        ]
        recorded.assert_finish_errors_match_node_errors()
        assert masked_client_form(client_result(request, result)) == client_run_failure(
            f'Failed to resolve "Node1".  Error: {self.RUN_MESSAGE}',
            [
                client_exception(
                    self.RUN_MESSAGE,
                    "builtins.RuntimeError",
                    traceback_ending_with(f"RuntimeError: {self.RUN_MESSAGE}"),
                )
            ],
        )

    @pytest.mark.asyncio
    async def test_node_failing_validation_before_it_runs(
        self, engine: Engine, monkeypatch: pytest.MonkeyPatch, node_flow: Callable[[str], str]
    ) -> None:
        node_flow("ValidateBeforeRunNode")
        recorded = record_failures(engine, monkeypatch)
        request = ResolveNodeRequest(node_name=NODE_NAME)

        result = await engine.ahandle_request(request)

        assert isinstance(result, ResolveNodeResultFailure)
        message = f"Node 'Node1' encountered problems: {TestValidateBeforeNodeRunFailing.PROBLEMS}"
        assert str(result.result_details) == f'Failed to resolve "Node1".  Error: {message}'
        assert [str(exception) for exception in result.validation_exceptions or []] == [message]
        assert [(event.node_name, event.error_message) for event in recorded.node_errors] == [
            (NODE_NAME, TestValidateBeforeNodeRunFailing.PROBLEMS)
        ]
        assert [details_dict(event.error) for event in recorded.node_errors] == [
            TestValidateBeforeNodeRunFailing.DETAILS
        ]
        assert recorded.node_finishes == [], "a node that was never dispatched never started"
        assert masked_client_form(client_result(request, result)) == client_run_failure(
            f'Failed to resolve "Node1".  Error: {message}',
            [client_exception(message, "builtins.RuntimeError", traceback_ending_with(f"RuntimeError: {message}"))],
        )


class TestNodeMissingOnTheOrchestrator:
    """Golden case 7, a node the orchestrator cannot find: the engine words the failure itself."""

    MISSING = (
        "Node 'Ghost' not found in ObjectManager on orchestrator. Refusing to fabricate a fresh node from "
        "metadata; the node was dropped from the live map and must be re-created via CreateNodeRequest."
    )

    @pytest.mark.asyncio
    async def test_execute_node_request(self, engine: Engine, node_flow: Callable[[str], str]) -> None:
        node_flow("KeyErrorNode")
        request = sent_from_open_workflow(
            ExecuteNodeRequest(node_name="Ghost", node_metadata={"node_type": "KeyErrorNode", "library": LIBRARY_NAME}),
            engine,
        )

        result = await engine.current_workflow().ahandle_request(request)

        assert isinstance(result, ExecuteNodeResultFailure)
        assert str(result.result_details) == self.MISSING
        assert result.exception is None
        assert result.validation_exceptions is None
        assert result.error is None
        assert client_result(request, result) == {
            "altered_workflow_state": False,
            "result_details": {"result_details": [{"level": 40, "message": self.MISSING}]},
            "exception": None,
            "validation_exceptions": None,
            "error": None,
        }

    def test_request_that_names_no_workflow(self) -> None:
        request = ExecuteNodeRequest(node_name="Ghost")

        words = NodeManager._orchestrator_node_missing(request, None)

        assert words == (
            "Attempted to execute node 'Ghost'. Failed because the request named no workflow to find it in. "
            "Send it from the workflow the node belongs to."
        )
