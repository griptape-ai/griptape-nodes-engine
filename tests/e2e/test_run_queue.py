"""End-to-end coverage for the run queue: a run started while another is in progress waits its turn.

Each test parks a node on a gate file so a run stays in progress for as long as the test needs, then
starts another run and checks what it does while the first goes on and once it ends.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from griptape_nodes.exe_types.node_types import NodeResolutionState
from griptape_nodes.retained_mode.events.execution_events import (
    CancelFlowRequest,
    CancelFlowResultSuccess,
    ContinueExecutionStepRequest,
    ResolveNodeRequest,
    ResolveNodeResultSuccess,
    RunQueuedEvent,
    StartFlowFromNodeRequest,
    StartFlowFromNodeResultSuccess,
)
from griptape_nodes.retained_mode.events.flow_events import CreateFlowRequest, CreateFlowResultSuccess
from griptape_nodes.retained_mode.events.library_events import (
    RegisterLibraryFromFileRequest,
    RegisterLibraryFromFileResultSuccess,
)
from griptape_nodes.retained_mode.events.parameter_events import SetParameterValueRequest

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from griptape_nodes.retained_mode.engine import Engine

# Timeout with thread dump. A run that never gets the machine hangs rather than failing.
pytestmark = pytest.mark.timeout(300, method="thread")

FIXTURE_LIBRARY_DIR = Path(__file__).parent / "fixtures" / "node_deletion_library"
FIXTURE_LIBRARY_JSON_TEMPLATE = FIXTURE_LIBRARY_DIR / "griptape_nodes_library.json"
FIXTURE_NODE_FILE = FIXTURE_LIBRARY_DIR / "gated_stream_nodes.py"
LIBRARY_NAME = "Node Deletion Library"
NODE_TYPE = "GatedStreamNode"

_RUN_TIMEOUT_SECONDS = 30
_POLL_ATTEMPTS = 500
_POLL_SECONDS = 0.01
# Long enough for a run that wrongly skipped the line to get its node resolved.
_WAITING_SECONDS = 0.5


@pytest.fixture
def parallel_mode(engine: Engine) -> Iterator[None]:
    """Run under the parallel scheduler, the editor's default, so two nodes can be live at once."""
    config_manager = engine.config_manager
    previous_mode = config_manager.get_config_value("workflow_execution_mode")
    previous_max = config_manager.get_config_value("max_nodes_in_parallel")
    config_manager.set_config_value("workflow_execution_mode", "parallel")
    config_manager.set_config_value("max_nodes_in_parallel", 4)
    try:
        yield
    finally:
        config_manager.set_config_value("workflow_execution_mode", previous_mode)
        config_manager.set_config_value("max_nodes_in_parallel", previous_max)


@pytest.fixture
def registered_library(engine: Engine, tmp_path: Path, materialize_library: Callable[..., Path]) -> None:
    """Materialize and register the gated fixture library into the isolated engine."""
    library_json = materialize_library(
        tmp_path / "library", template=FIXTURE_LIBRARY_JSON_TEMPLATE, node_file=FIXTURE_NODE_FILE
    )
    result = engine.handle_request(RegisterLibraryFromFileRequest(file_path=str(library_json)))
    assert isinstance(result, RegisterLibraryFromFileResultSuccess), result


@pytest.fixture
def flow_name(engine: Engine, registered_library: None, create_node: Callable[..., str], tmp_path: Path) -> str:  # noqa: ARG001
    """A flow with two unconnected nodes: Holder, which parks on its gate, and Next, which does not."""
    engine.context_manager.push_workflow(workflow_name="run_queue_wf")
    result = engine.handle_request(CreateFlowRequest(parent_flow_name=None, flow_name="Flow", set_as_new_context=False))
    assert isinstance(result, CreateFlowResultSuccess), result
    create_node(NODE_TYPE, "Holder", result.flow_name, library_name=LIBRARY_NAME)
    create_node(NODE_TYPE, "Next", result.flow_name, library_name=LIBRARY_NAME)
    _set_parameter(engine, "Holder", "gate_file", str(_gate(tmp_path)))
    return result.flow_name


def _gate(tmp_path: Path) -> Path:
    return tmp_path / "gates" / "holder.gate"


def _open_gate(tmp_path: Path) -> None:
    gate_file = _gate(tmp_path)
    gate_file.parent.mkdir(parents=True, exist_ok=True)
    gate_file.write_text("open")


def _set_parameter(engine: Engine, node_name: str, parameter_name: str, value: Any) -> None:
    engine.handle_request(SetParameterValueRequest(parameter_name=parameter_name, node_name=node_name, value=value))


def _state(engine: Engine, node_name: str) -> NodeResolutionState:
    return engine.node_manager.get_node_by_name(node_name).state


async def _wait_for_state(engine: Engine, node_name: str, state: NodeResolutionState) -> None:
    for _ in range(_POLL_ATTEMPTS):
        if _state(engine, node_name) is state:
            return
        await asyncio.sleep(_POLL_SECONDS)
    pytest.fail(f"Node '{node_name}' never reached {state}; it is {_state(engine, node_name)}.")


async def _finish(run: asyncio.Task) -> Any:
    return await asyncio.wait_for(run, timeout=_RUN_TIMEOUT_SECONDS)


@pytest.mark.usefixtures("parallel_mode")
@pytest.mark.asyncio
async def test_a_run_started_while_another_is_in_progress_waits_for_it_to_end(
    engine: Engine, flow_name: str, tmp_path: Path
) -> None:
    """The second run neither fails nor runs alongside the first: it starts once the first ends."""
    holding = asyncio.create_task(engine.ahandle_request(ResolveNodeRequest(node_name="Holder")))
    await _wait_for_state(engine, "Holder", NodeResolutionState.RESOLVING)

    waiting = asyncio.create_task(engine.ahandle_request(StartFlowFromNodeRequest(node_name="Next")))
    await asyncio.sleep(_WAITING_SECONDS)
    assert not waiting.done(), f"The second run did not wait for the first: {waiting.result()}"
    assert _state(engine, "Next") is not NodeResolutionState.RESOLVED

    _open_gate(tmp_path)

    assert isinstance(await _finish(holding), ResolveNodeResultSuccess)
    assert isinstance(await _finish(waiting), StartFlowFromNodeResultSuccess)
    assert _state(engine, "Next") is NodeResolutionState.RESOLVED
    assert engine.flow_manager.check_for_existing_running_flow() is False, flow_name


@pytest.mark.usefixtures("parallel_mode")
@pytest.mark.asyncio
async def test_the_editor_hears_a_waiting_runs_place_in_line_until_it_starts(
    engine: Engine, flow_name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The editor learns the run is next in line while it waits, and that it stopped waiting once it starts."""
    heard = _record_run_queued(engine, monkeypatch)
    holding = asyncio.create_task(engine.ahandle_request(ResolveNodeRequest(node_name="Holder")))
    await _wait_for_state(engine, "Holder", NodeResolutionState.RESOLVING)
    waiting = asyncio.create_task(engine.ahandle_request(StartFlowFromNodeRequest(node_name="Next")))
    await asyncio.sleep(_WAITING_SECONDS)

    assert heard == [(flow_name, 1)], "the editor was not told the run is next in line"

    _open_gate(tmp_path)
    await _finish(holding)
    await _finish(waiting)
    assert heard == [(flow_name, 1), (flow_name, 0)]


def _record_run_queued(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, int]]:
    heard: list[tuple[str, int]] = []
    real_put_event = engine.event_manager.put_event

    def _collect(event: object) -> None:
        payload = getattr(getattr(event, "wrapped_event", None), "payload", None)
        if isinstance(payload, RunQueuedEvent):
            heard.append((payload.flow_name, payload.position))
        real_put_event(event)

    monkeypatch.setattr(engine.event_manager, "put_event", _collect)
    return heard


@pytest.mark.usefixtures("parallel_mode")
@pytest.mark.asyncio
async def test_a_node_run_while_another_node_runs_joins_that_run(
    engine: Engine, flow_name: str, tmp_path: Path
) -> None:
    """Running one node and then another runs them side by side, as it always has."""
    holding = asyncio.create_task(engine.ahandle_request(ResolveNodeRequest(node_name="Holder")))
    await _wait_for_state(engine, "Holder", NodeResolutionState.RESOLVING)

    joined = asyncio.create_task(engine.ahandle_request(ResolveNodeRequest(node_name="Next")))
    await _wait_for_state(engine, "Next", NodeResolutionState.RESOLVED)

    assert _state(engine, "Holder") is NodeResolutionState.RESOLVING, "Next waited instead of joining the run"
    _open_gate(tmp_path)
    assert isinstance(await _finish(joined), ResolveNodeResultSuccess)
    assert isinstance(await _finish(holding), ResolveNodeResultSuccess)
    assert engine.flow_manager.check_for_existing_running_flow() is False, flow_name


@pytest.mark.usefixtures("parallel_mode")
@pytest.mark.asyncio
async def test_cancelling_stops_the_run_in_progress_and_the_waiting_run_then_starts(
    engine: Engine, flow_name: str
) -> None:
    """Cancel is for the run in progress: a run waiting behind it is not cancelled with it."""
    holding = asyncio.create_task(engine.ahandle_request(ResolveNodeRequest(node_name="Holder")))
    await _wait_for_state(engine, "Holder", NodeResolutionState.RESOLVING)
    waiting = asyncio.create_task(engine.ahandle_request(StartFlowFromNodeRequest(node_name="Next")))
    await asyncio.sleep(_WAITING_SECONDS)

    cancelled = await engine.ahandle_request(CancelFlowRequest(flow_name=flow_name))

    assert isinstance(cancelled, CancelFlowResultSuccess), cancelled
    await _finish(holding)
    assert isinstance(await _finish(waiting), StartFlowFromNodeResultSuccess)
    assert _state(engine, "Next") is NodeResolutionState.RESOLVED
    assert _state(engine, "Holder") is not NodeResolutionState.RESOLVED


@pytest.mark.usefixtures("parallel_mode")
@pytest.mark.asyncio
async def test_a_paused_debug_run_keeps_the_machine_until_it_ends(engine: Engine, flow_name: str) -> None:
    """A debug run's request returns at its first pause, but the run goes on until it is continued."""
    _set_parameter(engine, "Holder", "gate_file", "")
    paused = await engine.ahandle_request(StartFlowFromNodeRequest(node_name="Holder", debug_mode=True))
    assert isinstance(paused, StartFlowFromNodeResultSuccess), paused
    assert engine.flow_manager.check_for_existing_running_flow() is True, "the debug run did not pause"

    waiting = asyncio.create_task(engine.ahandle_request(StartFlowFromNodeRequest(node_name="Next")))
    await asyncio.sleep(_WAITING_SECONDS)
    assert not waiting.done(), f"A run started while a debug run was paused: {waiting.result()}"

    await engine.ahandle_request(ContinueExecutionStepRequest(flow_name=flow_name))

    assert isinstance(await _finish(waiting), StartFlowFromNodeResultSuccess)
    assert _state(engine, "Next") is NodeResolutionState.RESOLVED
