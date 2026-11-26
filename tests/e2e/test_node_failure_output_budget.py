"""Golden tests that pin what a node reports when Griptape Cloud refuses its call over budget.

Four routes lead to a budget halt, and each words it differently on its way out:

- a node lets the HTTP 403 out, and the engine words the halt from the response it carries,
- a node raises a halt that already names it, and the engine keeps it as it is,
- a library wraps the driver's halt in an exception of its own, and the engine finds the halt behind it,
- any of these in a worker, where the halt travels back as a type name, a message and a traceback.

The stub Cloud is the one ``test_budget_halt`` runs, from ``stub_cloud_support``. A worker traceback's frames carry
absolute paths and line numbers, so a golden pins the rest of the traceback and not the frames.
"""

from __future__ import annotations

import json
import logging
from contextlib import AbstractContextManager, nullcontext
from typing import TYPE_CHECKING, Any

import pytest

from griptape_nodes.retained_mode.events.parameter_events import SetParameterValueRequest
from tests.e2e.node_failure_support import (
    MASKED_FRAMES,
    TRACEBACK_HEADER,
    FlowRun,
    client_exception,
    client_run_failure,
    details_dict,
    expected_details,
    mask_worker_traceback,
    new_flow,
    register_fixture_library,
    start_flow,
)
from tests.e2e.stub_cloud_support import REFUSED_PATH
from tests.simulated_worker import simulated_worker

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from griptape_nodes.retained_mode.engine import Engine

pytestmark = pytest.mark.timeout(300, method="thread")

LIBRARY_NAME = "Budget Halt Library"
NODE_NAME = "Node1"
FLOW_FAILURE_PREFIX = "Attempted to run flow 'Flow'. Failed due to: "
HALT_TYPE = "griptape_nodes.utils.budget_refusal.BudgetExceededError"
HALT = (
    'Budget stopped this run. \'Node1\' was blocked by the budgets "tight" and "frozen-one" (frozen). '
    "Contact your Griptape administrator."
)
UNNAMED_HALT = (
    'Budget stopped this run. The next call was blocked by the budgets "tight" and "frozen-one" (frozen). '
    "Contact your Griptape administrator."
)
CREDITS_LOG_LINE = (
    "Node1: Griptape Cloud refused an invocation over budget. spend_id=9a2d5e70-0000-4000-8000-00000000000f "
    "effective_remaining_credits=10 cloud_message='Budget limit reached (tight, frozen-one).' blocked_by: "
    "tight (id=3f1c6b4e-0000-4000-8000-000000000001, scope=ORG, period=MONTHLY, enforcement=HARD, limit=100, "
    "spent=90, remaining=10, requested=50, frozen=False); frozen-one "
    "(id=3f1c6b4e-0000-4000-8000-000000000002, scope=LICENSE, period=DAILY, enforcement=HARD, limit=10000, "
    "spent=0, remaining=10000, requested=50, frozen=True)"
)
HALT_DETAILS = expected_details(HALT, HALT_TYPE)
WORKER_HEAD = f"Node 'Node1' execution failed: [{HALT_TYPE}] {HALT}"
CLOUD_PLACEHOLDER = "<cloud>"
CHAINED = "\nThe above exception was the direct cause of the following exception:\n\n"
# The worker's traceback for a halt the driver raised from the HTTP error: the error, then the halt.
HTTP_ERROR_TRACEBACK = (
    f"{TRACEBACK_HEADER}{MASKED_FRAMES}\n"
    f"httpx.HTTPStatusError: Client error '403 Forbidden' for url '{CLOUD_PLACEHOLDER}{REFUSED_PATH}'\n"
    "For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/403\n"
    f"{CHAINED}{TRACEBACK_HEADER}{MASKED_FRAMES}\n{HALT_TYPE}: {HALT}\n"
)
# The traceback for a halt a library wrapped: the driver's halt, the library's error, then the halt the engine words.
WRAPPED_TRACEBACK = (
    f"{HALT_TYPE}: {UNNAMED_HALT}\n"
    f"{CHAINED}{TRACEBACK_HEADER}{MASKED_FRAMES}\n"
    "RuntimeError: The image service could not finish the generation.\n"
    f"{CHAINED}{TRACEBACK_HEADER}{MASKED_FRAMES}\n{HALT_TYPE}: {HALT}\n"
)


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


class _BudgetFailureBase:
    """Runs a flow with one node that Cloud refuses, in this process or in a simulated worker."""

    async def run_refused_node(  # noqa: PLR0913, PLR0917
        self,
        engine: Engine,
        monkeypatch: pytest.MonkeyPatch,
        create_node: Callable[..., str],
        caplog: pytest.LogCaptureFixture,
        stub_cloud: str,
        node_type: str,
        *,
        in_worker: bool,
    ) -> FlowRun:
        """Create ``Node1`` pointed at the refusing route, run the flow, and gather what was reported."""
        worker_context: AbstractContextManager[object] = nullcontext()
        if in_worker:
            worker_context = simulated_worker(engine, LIBRARY_NAME)
        with worker_context:
            flow_name = new_flow(engine, "budget_failure_output")
            create_node(node_type, NODE_NAME, flow_name, library_name=LIBRARY_NAME)
            engine.handle_request(
                SetParameterValueRequest(parameter_name="url", node_name=NODE_NAME, value=f"{stub_cloud}{REFUSED_PATH}")
            )
            with caplog.at_level(logging.ERROR):
                return await start_flow(engine, monkeypatch, flow_name)

    def assert_halt_reported_in_process(self, run: FlowRun) -> None:
        """Assert every audience got the halt, worded for this node, with no engine preamble."""
        run_message = f"Node 'Node1' execution failed: {HALT}"
        assert run.run_message == run_message
        assert run.result_details == FLOW_FAILURE_PREFIX + run_message
        assert run.validation_exception_messages == [run_message]
        [node_error] = run.recorded.node_errors
        assert (node_error.node_name, node_error.error_message) == (NODE_NAME, run_message)
        assert details_dict(node_error.error) == HALT_DETAILS
        [finish] = run.recorded.node_finishes
        assert finish.error_message == run_message
        assert details_dict(finish.error) == HALT_DETAILS
        run.recorded.assert_finish_errors_match_node_errors()
        assert run.client == client_run_failure(FLOW_FAILURE_PREFIX + run_message, [client_exception(run_message)])

    def assert_halt_reported_from_worker(self, run: FlowRun, stub_cloud: str, traceback: str) -> None:
        """Assert every audience got the halt with the worker's type ahead of it and ``traceback`` after it.

        ``traceback`` is the masked worker traceback, with the stub Cloud's address as ``<cloud>``.
        """
        expected_message = f"{WORKER_HEAD}\nWorker traceback:\n{traceback}"

        def unmasked(text: str) -> str:
            return mask_worker_traceback(text).text.replace(stub_cloud, CLOUD_PLACEHOLDER)

        assert unmasked(run.run_message or "") == expected_message
        assert unmasked(run.result_details) == FLOW_FAILURE_PREFIX + expected_message
        assert [unmasked(message) for message in run.validation_exception_messages] == [expected_message]
        [node_error] = run.recorded.node_errors
        assert node_error.node_name == NODE_NAME
        assert unmasked(node_error.error_message) == expected_message
        assert details_dict(node_error.error) == HALT_DETAILS
        [finish] = run.recorded.node_finishes
        assert unmasked(finish.error_message or "") == expected_message
        assert details_dict(finish.error) == HALT_DETAILS
        run.recorded.assert_finish_errors_match_node_errors()
        assert self.without_cloud_address(run.client, stub_cloud) == client_run_failure(
            FLOW_FAILURE_PREFIX + expected_message, [client_exception(expected_message)]
        )

    def without_cloud_address(self, value: Any, stub_cloud: str) -> Any:
        """``value`` with the stub Cloud's address, which differs every run, replaced by ``<cloud>``."""
        return json.loads(json.dumps(value).replace(stub_cloud, CLOUD_PLACEHOLDER))

    def credits_log_lines(self, caplog: pytest.LogCaptureFixture) -> list[str]:
        """The ERROR lines that carry the refusal's credits."""
        return [
            record.getMessage()
            for record in caplog.records
            if record.levelno == logging.ERROR and "refused an invocation over budget" in record.getMessage()
        ]


class TestHttpRefusalWordedByTheEngine(_BudgetFailureBase):
    """Golden case 8, in-process HTTP refusal: the node lets the 403 out and the engine words the halt.

    The credits go to a separate ERROR log line, which names the node and every budget's figures.
    """

    @pytest.mark.asyncio
    async def test_in_process(
        self,
        engine: Engine,
        monkeypatch: pytest.MonkeyPatch,
        create_node: Callable[..., str],
        caplog: pytest.LogCaptureFixture,
        stub_cloud: str,
    ) -> None:
        run = await self.run_refused_node(
            engine, monkeypatch, create_node, caplog, stub_cloud, "UnhandledCloudCallNode", in_worker=False
        )

        self.assert_halt_reported_in_process(run)
        assert self.credits_log_lines(caplog) == [CREDITS_LOG_LINE]

    @pytest.mark.asyncio
    async def test_in_worker(
        self,
        engine: Engine,
        monkeypatch: pytest.MonkeyPatch,
        create_node: Callable[..., str],
        caplog: pytest.LogCaptureFixture,
        stub_cloud: str,
    ) -> None:
        run = await self.run_refused_node(
            engine, monkeypatch, create_node, caplog, stub_cloud, "UnhandledCloudCallNode", in_worker=True
        )

        self.assert_halt_reported_from_worker(run, stub_cloud, HTTP_ERROR_TRACEBACK)
        assert self.credits_log_lines(caplog) == [CREDITS_LOG_LINE]


class TestHaltAlreadyWorded(_BudgetFailureBase):
    """Golden case 8, already-worded halt: a node raises a ``BudgetExceededError`` that names it.

    The engine keeps the node's wording and logs no credits line, since it never read a refusal.
    """

    @pytest.mark.asyncio
    async def test_in_process(
        self,
        engine: Engine,
        monkeypatch: pytest.MonkeyPatch,
        create_node: Callable[..., str],
        caplog: pytest.LogCaptureFixture,
        stub_cloud: str,
    ) -> None:
        run = await self.run_refused_node(
            engine, monkeypatch, create_node, caplog, stub_cloud, "NamedHaltNode", in_worker=False
        )

        self.assert_halt_reported_in_process(run)
        assert self.credits_log_lines(caplog) == []

    @pytest.mark.asyncio
    async def test_in_worker(
        self,
        engine: Engine,
        monkeypatch: pytest.MonkeyPatch,
        create_node: Callable[..., str],
        caplog: pytest.LogCaptureFixture,
        stub_cloud: str,
    ) -> None:
        run = await self.run_refused_node(
            engine, monkeypatch, create_node, caplog, stub_cloud, "NamedHaltNode", in_worker=True
        )

        self.assert_halt_reported_from_worker(run, stub_cloud, HTTP_ERROR_TRACEBACK)
        assert self.credits_log_lines(caplog) == []


class TestHaltWrappedByALibrary(_BudgetFailureBase):
    """Golden case 8, wrapped halt: a library catches the driver's halt and raises its own exception from it.

    The engine finds the halt behind the wrapper, words it for the node, and reports it as a halt.
    """

    @pytest.mark.asyncio
    async def test_in_process(
        self,
        engine: Engine,
        monkeypatch: pytest.MonkeyPatch,
        create_node: Callable[..., str],
        caplog: pytest.LogCaptureFixture,
        stub_cloud: str,
    ) -> None:
        run = await self.run_refused_node(
            engine, monkeypatch, create_node, caplog, stub_cloud, "WrappedHaltNode", in_worker=False
        )

        self.assert_halt_reported_in_process(run)
        assert self.credits_log_lines(caplog) == [CREDITS_LOG_LINE]

    @pytest.mark.asyncio
    async def test_in_worker(
        self,
        engine: Engine,
        monkeypatch: pytest.MonkeyPatch,
        create_node: Callable[..., str],
        caplog: pytest.LogCaptureFixture,
        stub_cloud: str,
    ) -> None:
        """The worker's traceback carries the whole chain, so it also holds the halt as the driver worded it."""
        run = await self.run_refused_node(
            engine, monkeypatch, create_node, caplog, stub_cloud, "WrappedHaltNode", in_worker=True
        )

        self.assert_halt_reported_from_worker(run, stub_cloud, WRAPPED_TRACEBACK)
        assert self.credits_log_lines(caplog) == [CREDITS_LOG_LINE]
