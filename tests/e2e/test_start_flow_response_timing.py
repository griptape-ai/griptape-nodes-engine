"""End-to-end coverage for when `StartFlowRequest` answers its caller.

The request has two contracts and they must stay distinct:

- Default: the result is a kickoff acknowledgement. Clients (the editor, the bridge) send it,
  get the ack, and follow the run through execution events. A result that only lands once the
  run is over holds a request slot open for the whole run, and any client-side response timeout
  turns a healthy run into a visible failure.
- `wait_for_completion=True`: the result is the run's verdict, for callers that want to read
  output values straight afterwards without inventing their own polling loop.

Both are timing claims, so both are tested against a node that parks until the test lets it go.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio

from griptape_nodes.exe_types.node_types import NodeResolutionState
from griptape_nodes.retained_mode.events.execution_events import (
    StartFlowRequest,
    StartFlowResultFailure,
    StartFlowResultSuccess,
)
from griptape_nodes.retained_mode.events.flow_events import CreateFlowRequest, CreateFlowResultSuccess
from griptape_nodes.retained_mode.events.library_events import (
    RegisterLibraryFromFileRequest,
    RegisterLibraryFromFileResultSuccess,
)
from griptape_nodes.retained_mode.events.parameter_events import SetParameterValueRequest

if TYPE_CHECKING:
    from collections.abc import Callable

    from griptape_nodes.retained_mode.engine import Engine

pytestmark = pytest.mark.timeout(120, method="thread")

FIXTURE_LIBRARY_DIR = Path(__file__).parent / "fixtures" / "node_deletion_library"
FIXTURE_LIBRARY_JSON_TEMPLATE = FIXTURE_LIBRARY_DIR / "griptape_nodes_library.json"
FIXTURE_NODE_FILE = FIXTURE_LIBRARY_DIR / "gated_stream_nodes.py"
LIBRARY_NAME = "Node Deletion Library"
NODE_TYPE = "GatedStreamNode"

_POLL_ATTEMPTS = 500
_POLL_SECONDS = 0.01

requires_fixture_library = pytest.mark.skipif(
    not FIXTURE_LIBRARY_JSON_TEMPLATE.exists(),
    reason=f"Node Deletion Library fixture missing at {FIXTURE_LIBRARY_JSON_TEMPLATE}",
)


@pytest.fixture
def registered_library(engine: Engine, tmp_path: Path, materialize_library: Callable[..., Path]) -> None:
    """Materialize and register the gated fixture library into the isolated engine."""
    library_json = materialize_library(
        tmp_path / "library", template=FIXTURE_LIBRARY_JSON_TEMPLATE, node_file=FIXTURE_NODE_FILE
    )
    result = engine.handle_request(RegisterLibraryFromFileRequest(file_path=str(library_json)))
    assert isinstance(result, RegisterLibraryFromFileResultSuccess), result


@pytest_asyncio.fixture
async def engine_loop(engine: Engine) -> None:
    """Give the engine the running loop as its own, the way the app does on startup.

    A backgrounded run needs a loop that outlives the request that started it, and the engine
    loop is the only one it can assume that of. Without this the engine has no loop registered
    and every run is driven inline, which is the behaviour the sync test below covers. Async so
    that it is the test's own running loop that gets registered.
    """
    engine.event_manager.initialize_queue(asyncio.Queue())


def _new_flow(engine: Engine, workflow_name: str) -> str:
    engine.context_manager.push_workflow(workflow_name=workflow_name)
    result = engine.handle_request(CreateFlowRequest(parent_flow_name=None, flow_name="Flow", set_as_new_context=False))
    assert isinstance(result, CreateFlowResultSuccess), result
    return result.flow_name


def _set_parameter(engine: Engine, node_name: str, parameter_name: str, value: Any) -> None:
    engine.handle_request(SetParameterValueRequest(parameter_name=parameter_name, node_name=node_name, value=value))


def _configure(engine: Engine, node_name: str, *, text: str = "", gate_file: Path | None = None) -> None:
    _set_parameter(engine, node_name, "text", text)
    _set_parameter(engine, node_name, "gate_file", str(gate_file) if gate_file is not None else "")
    _set_parameter(engine, node_name, "chunk", "")


def _open_gate(gate_file: Path) -> None:
    gate_file.parent.mkdir(parents=True, exist_ok=True)
    gate_file.write_text("open")


async def _wait_until_resolving(engine: Engine, node_name: str) -> None:
    for _ in range(_POLL_ATTEMPTS):
        if engine.node_manager.get_node_by_name(node_name).state is NodeResolutionState.RESOLVING:
            return
        await asyncio.sleep(_POLL_SECONDS)
    pytest.fail(f"Node '{node_name}' never started executing.")


async def _wait_until_idle(engine: Engine) -> None:
    for _ in range(_POLL_ATTEMPTS):
        if not engine.flow_manager.check_for_existing_running_flow():
            return
        await asyncio.sleep(_POLL_SECONDS)
    pytest.fail("The run never finished, so the flow still reports itself as running.")


@requires_fixture_library
@pytest.mark.usefixtures("registered_library", "engine_loop")
@pytest.mark.asyncio
async def test_start_flow_acks_before_the_run_finishes(
    tmp_path: Path,
    engine: Engine,
    create_node: Callable[..., str],
) -> None:
    """The default result is an ack: it lands while the first node is still parked in its gate."""
    flow_name = _new_flow(engine, "start_flow_ack_wf")
    gate_file = tmp_path / "gates" / "runner.gate"

    create_node(NODE_TYPE, "Runner", flow_name, library_name=LIBRARY_NAME)
    _configure(engine, "Runner", text="parked", gate_file=gate_file)

    start_result = await engine.ahandle_request(StartFlowRequest(flow_name=flow_name))

    assert isinstance(start_result, StartFlowResultSuccess), start_result
    assert engine.flow_manager.check_for_existing_running_flow() is True, (
        "The run was acknowledged but the engine does not consider it live, so a second start would "
        "be accepted on top of it."
    )

    # The gate is still shut, so anything the run does from here it does without the caller.
    await _wait_until_resolving(engine, "Runner")

    second_start = await engine.ahandle_request(StartFlowRequest(flow_name=flow_name))
    assert isinstance(second_start, StartFlowResultFailure), (
        "A second start was accepted while the first run was still going."
    )

    _open_gate(gate_file)
    await _wait_until_idle(engine)
    assert engine.node_manager.get_node_by_name("Runner").state is NodeResolutionState.RESOLVED


@requires_fixture_library
@pytest.mark.usefixtures("registered_library", "engine_loop")
@pytest.mark.asyncio
async def test_start_flow_waits_for_completion_when_asked(
    tmp_path: Path,
    engine: Engine,
    create_node: Callable[..., str],
) -> None:
    """`wait_for_completion=True` does not answer until the parked node has been let go."""
    flow_name = _new_flow(engine, "start_flow_wait_wf")
    gate_file = tmp_path / "gates" / "runner.gate"

    create_node(NODE_TYPE, "Runner", flow_name, library_name=LIBRARY_NAME)
    _configure(engine, "Runner", text="parked", gate_file=gate_file)

    run = asyncio.create_task(engine.ahandle_request(StartFlowRequest(flow_name=flow_name, wait_for_completion=True)))
    await _wait_until_resolving(engine, "Runner")

    assert not run.done(), "wait_for_completion returned while the node was still parked in its gate."

    _open_gate(gate_file)
    start_result = await asyncio.wait_for(run, timeout=30)

    assert isinstance(start_result, StartFlowResultSuccess), start_result
    assert engine.node_manager.get_node_by_name("Runner").state is NodeResolutionState.RESOLVED
    assert engine.flow_manager.check_for_existing_running_flow() is False


@requires_fixture_library
@pytest.mark.usefixtures("registered_library", "engine_loop")
@pytest.mark.asyncio
async def test_start_flow_cancels_the_run_it_gave_up_waiting_for(
    tmp_path: Path,
    engine: Engine,
    create_node: Callable[..., str],
) -> None:
    """A wait that times out must stop the run, not leave it going unattended."""
    flow_name = _new_flow(engine, "start_flow_timeout_wf")
    gate_file = tmp_path / "gates" / "runner.gate"

    create_node(NODE_TYPE, "Runner", flow_name, library_name=LIBRARY_NAME)
    _configure(engine, "Runner", text="parked", gate_file=gate_file)

    start_result = await engine.ahandle_request(
        StartFlowRequest(flow_name=flow_name, wait_for_completion=True, completion_timeout_ms=250)
    )

    assert isinstance(start_result, StartFlowResultFailure), start_result
    assert "Timed out" in str(start_result.result_details), start_result.result_details
    assert engine.flow_manager.check_for_existing_running_flow() is False, (
        "The run the caller gave up on is still going, so the next start would be refused."
    )

    # Release the node whatever the cancel did to it, so nothing is left parked on the gate.
    _open_gate(gate_file)


@requires_fixture_library
@pytest.mark.usefixtures("registered_library")
def test_a_synchronous_caller_gets_the_run_inline(
    engine: Engine,
    create_node: Callable[..., str],
) -> None:
    """A sync dispatch has no loop left to run a backgrounded flow, so it must run inline.

    This is the `cmd.run_flow` path a saved workflow file takes. Backgrounding the run there would
    hand the caller an acknowledgement and then drop the run when its loop closed.
    """
    flow_name = _new_flow(engine, "start_flow_sync_wf")

    create_node(NODE_TYPE, "Runner", flow_name, library_name=LIBRARY_NAME)
    _configure(engine, "Runner", text="runs inline")

    start_result = engine.handle_request(StartFlowRequest(flow_name=flow_name))

    assert isinstance(start_result, StartFlowResultSuccess), start_result
    assert engine.node_manager.get_node_by_name("Runner").state is NodeResolutionState.RESOLVED, (
        "The run was backgrounded onto a loop that does not outlive the call, so it never happened."
    )
