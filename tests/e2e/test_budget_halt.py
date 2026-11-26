"""End-to-end coverage for a run that Griptape Cloud refuses over budget.

The unit tests prove each piece: the body parses, the sentence reads well, the halt is found under
whatever wrapped it. None of them proves the pieces are wired to each other. This suite runs a real
flow against a real HTTP server answering a real 403, and asks the three questions an artist would:

1. **Did the run stop?** A refusal the engine notices but does not act on is the worst outcome --
   the run carries on spending against a wall it has already hit.
2. **Does the message say which budget?** The epic's words are "never a generic error". The failure
   the editor receives has to name every budget that refused, not the node that noticed.
3. **Does a wired Failed branch still run?** A refusal is one node's failure. The Failed branch may
   lead somewhere the budget does not reach, such as a local model, so it is taken like any other.

Both execution modes run every test, since each frames a node failure on its own way out and the
halt's wording has to survive both.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from griptape_nodes.retained_mode.events.execution_events import (
    NodeErrorEvent,
    StartFlowRequest,
    StartFlowResultFailure,
)
from griptape_nodes.retained_mode.events.flow_events import CreateFlowRequest, CreateFlowResultSuccess
from griptape_nodes.retained_mode.events.library_events import (
    RegisterLibraryFromFileRequest,
    RegisterLibraryFromFileResultSuccess,
)
from griptape_nodes.retained_mode.events.parameter_events import SetParameterValueRequest
from griptape_nodes.utils.budget_refusal import BUDGET_HALT_PREFIX
from tests.e2e.stub_cloud_support import OK_BODY, OK_PATH, REFUSED_PATH

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from griptape_nodes.retained_mode.engine import Engine

# Timeout with thread dump. A run that fails to halt hangs rather than failing, so the dump is the
# diagnosis.
pytestmark = pytest.mark.timeout(300, method="thread")

FIXTURE_LIBRARY_DIR = Path(__file__).parent / "fixtures" / "budget_halt_library"
FIXTURE_LIBRARY_JSON_TEMPLATE = FIXTURE_LIBRARY_DIR / "griptape_nodes_library.json"
FIXTURE_NODE_FILE = FIXTURE_LIBRARY_DIR / "budget_nodes.py"
LIBRARY_NAME = "Budget Halt Library"
NODE_TYPE = "CloudCallNode"
UNHANDLED_NODE_TYPE = "UnhandledCloudCallNode"
DRIVER_HALT_NODE_TYPE = "DriverHaltNode"

requires_fixture_library = pytest.mark.skipif(
    not FIXTURE_LIBRARY_JSON_TEMPLATE.exists(),
    reason=f"Budget Halt Library fixture missing at {FIXTURE_LIBRARY_JSON_TEMPLATE}",
)


@pytest.fixture(params=["sequential", "parallel"])
def execution_mode(request: pytest.FixtureRequest, engine: Engine) -> Iterator[str]:
    """Run each test under both schedulers, restoring the engine's configuration afterwards."""
    config_manager = engine.config_manager
    previous_mode = config_manager.get_config_value("workflow_execution_mode")
    previous_max = config_manager.get_config_value("max_nodes_in_parallel")
    config_manager.set_config_value("workflow_execution_mode", request.param)
    config_manager.set_config_value("max_nodes_in_parallel", 4)
    try:
        yield request.param
    finally:
        config_manager.set_config_value("workflow_execution_mode", previous_mode)
        config_manager.set_config_value("max_nodes_in_parallel", previous_max)


@pytest.fixture
def registered_library(engine: Engine, tmp_path: Path, materialize_library: Callable[..., Path]) -> None:
    """Materialize and register the budget fixture library into the isolated engine."""
    library_json = materialize_library(
        tmp_path / "library", template=FIXTURE_LIBRARY_JSON_TEMPLATE, node_file=FIXTURE_NODE_FILE
    )
    result = engine.handle_request(RegisterLibraryFromFileRequest(file_path=str(library_json)))
    assert isinstance(result, RegisterLibraryFromFileResultSuccess), result


def _new_flow(engine: Engine, workflow_name: str) -> str:
    """Create a workflow context and a single top-level flow to hold the test's nodes."""
    engine.context_manager.push_workflow(workflow_name=workflow_name)
    result = engine.handle_request(CreateFlowRequest(parent_flow_name=None, flow_name="Flow"))
    assert isinstance(result, CreateFlowResultSuccess), result
    return result.flow_name


def _set_parameter(engine: Engine, node_name: str, parameter_name: str, value: Any) -> None:
    engine.handle_request(SetParameterValueRequest(parameter_name=parameter_name, node_name=node_name, value=value))


def _record_published(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, payload_type: type, extract: Callable[[Any], Any]
) -> list[Any]:
    """Tap the event bus and record every published payload of the given type.

    What the editor shows comes off this stream, so "the artist was told which budget" is a claim
    about published events, not about the engine's internal state.
    """
    recorded: list[Any] = []
    event_manager = engine.event_manager
    real_put_event = event_manager.put_event
    real_aput_event = event_manager.aput_event

    def _note(event: object) -> None:
        payload = getattr(getattr(event, "wrapped_event", None), "payload", event)
        if isinstance(payload, payload_type):
            recorded.append(extract(payload))

    def _collect(event: object) -> None:
        _note(event)
        real_put_event(event)

    async def _acollect(event: object) -> None:
        _note(event)
        await real_aput_event(event)

    # Both spellings, because which one an event takes is an implementation detail of the code
    # that emits it: the scheduler's error path is async and uses `aput_event`, the flow manager's
    # cleanup is not and uses `put_event`. A tap on one of them silently records half the stream.
    monkeypatch.setattr(event_manager, "put_event", _collect)
    monkeypatch.setattr(event_manager, "aput_event", _acollect)
    return recorded


async def _run(engine: Engine, flow_name: str) -> Any:
    """Run the flow to completion and hand back whatever the engine concluded."""
    return await engine.ahandle_request(StartFlowRequest(flow_name=flow_name))


@requires_fixture_library
@pytest.mark.usefixtures("registered_library", "execution_mode")
@pytest.mark.asyncio
async def test_a_refusal_halts_the_run_and_names_every_budget(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    create_node: Callable[..., str],
    stub_cloud: str,
) -> None:
    """The whole requirement in one run: it stops, and it says which budgets stopped it."""
    flow_name = _new_flow(engine, "budget_halt_wf")
    create_node(NODE_TYPE, "Refused", flow_name, library_name=LIBRARY_NAME)
    _set_parameter(engine, "Refused", "url", f"{stub_cloud}{REFUSED_PATH}")

    node_errors = _record_published(engine, monkeypatch, NodeErrorEvent, lambda payload: payload.error_message)

    result = await _run(engine, flow_name)

    assert isinstance(result, StartFlowResultFailure), (
        f"Cloud refused the call and the run reported success anyway: {result}"
    )
    details = str(result.result_details)
    assert BUDGET_HALT_PREFIX in details, f"The run failed generically instead of as a budget halt: {details}"
    for budget_name in ("tight", "frozen-one"):
        assert budget_name in details, (
            f"The halt named some of the budgets that refused but not '{budget_name}': {details}"
        )
    assert "$" not in details, f"Credits are the only figure Cloud and the engine agree on: {details}"

    assert len(node_errors) == 1, f"Expected exactly one node error, got {node_errors}"
    assert BUDGET_HALT_PREFIX in node_errors[0], (
        f"The node-level error the editor pins to the node is generic: {node_errors[0]}"
    )
    assert engine.flow_manager.check_for_existing_running_flow_in(engine.current_workflow()) is False, (
        "The run never ended, so the editor's Run button never clears."
    )


@requires_fixture_library
@pytest.mark.usefixtures("registered_library", "execution_mode")
@pytest.mark.asyncio
async def test_a_refusal_takes_a_wired_failure_branch(
    tmp_path: Path,
    engine: Engine,
    create_node: Callable[..., str],
    connect: Callable[..., None],
    stub_cloud: str,
) -> None:
    """A workflow can fall back when Cloud refuses a call, the same as when Cloud errors.

    The fallback here answers from a route the budget does not cover, standing in for a local model.
    """
    flow_name = _new_flow(engine, "budget_halt_branch_wf")
    receipt = tmp_path / "receipts" / "fallback.txt"

    create_node(NODE_TYPE, "Refused", flow_name, library_name=LIBRARY_NAME)
    create_node(NODE_TYPE, "Fallback", flow_name, library_name=LIBRARY_NAME)
    connect("Refused", "failure", "Fallback", "exec_in")
    _set_parameter(engine, "Refused", "url", f"{stub_cloud}{REFUSED_PATH}")
    _set_parameter(engine, "Fallback", "url", f"{stub_cloud}{OK_PATH}")
    _set_parameter(engine, "Fallback", "receipt_file", str(receipt))

    await _run(engine, flow_name)

    assert receipt.exists(), "A budget refusal skipped the Failed branch the workflow wired up for it."
    fallback = engine.node_manager.get_node_by_name("Fallback")
    assert fallback.parameter_output_values.get("result") == json.dumps(OK_BODY), "The fallback ran but did not finish."


@requires_fixture_library
@pytest.mark.usefixtures("registered_library", "execution_mode")
@pytest.mark.asyncio
async def test_a_node_that_catches_nothing_still_halts_with_the_budgets_named(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    create_node: Callable[..., str],
    stub_cloud: str,
) -> None:
    """Most node types catch nothing, and the refusal has to be recognized for them.

    Asking each of the ~50 to handle it would mean the one that was missed fails as a bare 403.
    Every node failure crosses ``NodeManager``, so that is where this is answered.
    """
    flow_name = _new_flow(engine, "budget_halt_unhandled_wf")
    create_node(UNHANDLED_NODE_TYPE, "Refused", flow_name, library_name=LIBRARY_NAME)
    _set_parameter(engine, "Refused", "url", f"{stub_cloud}{REFUSED_PATH}")

    node_errors = _record_published(engine, monkeypatch, NodeErrorEvent, lambda payload: payload.error_message)

    result = await _run(engine, flow_name)

    assert isinstance(result, StartFlowResultFailure), (
        f"Cloud refused the call and the run reported success anyway: {result}"
    )
    details = str(result.result_details)
    assert BUDGET_HALT_PREFIX in details, f"The run failed generically instead of as a budget halt: {details}"
    for budget_name in ("tight", "frozen-one"):
        assert budget_name in details, (
            f"The halt named some of the budgets that refused but not '{budget_name}': {details}"
        )
    assert "Refused" in details, f"The halt does not say which node was refused: {details}"

    assert node_errors, "The node failed and the editor was told nothing about it."
    assert BUDGET_HALT_PREFIX in node_errors[0], (
        f"The node-level error the editor pins to the node is generic: {node_errors[0]}"
    )


@requires_fixture_library
@pytest.mark.usefixtures("registered_library", "execution_mode")
@pytest.mark.asyncio
async def test_a_halt_raised_without_a_node_name_is_worded_with_one(
    engine: Engine,
    create_node: Callable[..., str],
    stub_cloud: str,
) -> None:
    """A driver knows the refusal but not whose call it was; the engine supplies the name.

    Otherwise the artist reads that a budget stopped the run and is left to find the node
    themselves -- which on a large canvas is the whole of the problem.
    """
    flow_name = _new_flow(engine, "budget_halt_driver_wf")
    create_node(DRIVER_HALT_NODE_TYPE, "Spender", flow_name, library_name=LIBRARY_NAME)
    _set_parameter(engine, "Spender", "url", f"{stub_cloud}{REFUSED_PATH}")

    result = await _run(engine, flow_name)

    assert isinstance(result, StartFlowResultFailure), result
    details = str(result.result_details)
    assert BUDGET_HALT_PREFIX in details, f"The driver's halt was not recognized as one: {details}"
    assert "Spender" in details, f"The halt reached the artist without naming the node: {details}"


@requires_fixture_library
@pytest.mark.usefixtures("registered_library", "execution_mode")
@pytest.mark.asyncio
async def test_a_node_that_worded_its_own_halt_keeps_it(
    engine: Engine,
    create_node: Callable[..., str],
    stub_cloud: str,
) -> None:
    """A node that already named itself is left alone, so the wording is never applied twice."""
    flow_name = _new_flow(engine, "budget_halt_selfworded_wf")
    create_node(NODE_TYPE, "Refused", flow_name, library_name=LIBRARY_NAME)
    _set_parameter(engine, "Refused", "url", f"{stub_cloud}{REFUSED_PATH}")

    result = await _run(engine, flow_name)

    details = str(result.result_details)
    assert details.count(BUDGET_HALT_PREFIX) == 1, f"The halt was worded more than once: {details}"
