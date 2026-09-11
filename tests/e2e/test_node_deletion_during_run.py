"""End-to-end coverage for deleting a node while a run is in flight.

Deleting a node mid-run can fail in two very different shapes, and only one of them is loud:

1. **The run wedges.** The scheduler still believes the deleted node owes it something, so
   in-degrees never reach zero, no leaves are left to pick up, and the driver idles forever. The
   editor's Run button never clears.
2. **The run survives, but stops being observable — or worse, silently changes answer.** Deleting a
   node tears down its connections through the ordinary connection-deletion path, which resets a
   target's input to the parameter default. If the target is *currently executing*, it computes off
   the default and the run reports success with the wrong output.

The policy these tests encode: cancelling the whole run is only acceptable when the deleted node
still owed the run **unresolved** work. Deleting something the run has already finished with — a
resolved upstream node, a node nothing live depends on, an unconnected bystander — must leave the
run alone.

Streaming is how shape 2 becomes visible. Text appearing on a node in the editor is a series of
``ProgressEvent``s, so a node that keeps computing but stops announcing chunks looks exactly like a
dead run. The fixture streams while it is parked so a test can watch that.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from griptape_nodes.exe_types.node_types import NodeResolutionState
from griptape_nodes.retained_mode.events.base_events import ProgressEvent
from griptape_nodes.retained_mode.events.execution_events import (
    ControlFlowCancelledEvent,
    InvolvedNodesEvent,
    NodeUnresolvedEvent,
    ResolveNodeRequest,
    StartFlowRequest,
)
from griptape_nodes.retained_mode.events.flow_events import CreateFlowRequest, CreateFlowResultSuccess
from griptape_nodes.retained_mode.events.library_events import (
    RegisterLibraryFromFileRequest,
    RegisterLibraryFromFileResultSuccess,
)
from griptape_nodes.retained_mode.events.node_events import DeleteNodeRequest, DeleteNodeResultSuccess
from griptape_nodes.retained_mode.events.parameter_events import SetParameterValueRequest

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from griptape_nodes.retained_mode.engine import Engine

# Timeout with thread dump. A wedged run hangs rather than failing, so the dump is the diagnosis.
pytestmark = pytest.mark.timeout(300, method="thread")

FIXTURE_LIBRARY_DIR = Path(__file__).parent / "fixtures" / "node_deletion_library"
FIXTURE_LIBRARY_JSON_TEMPLATE = FIXTURE_LIBRARY_DIR / "griptape_nodes_library.json"
FIXTURE_NODE_FILE = FIXTURE_LIBRARY_DIR / "gated_stream_nodes.py"
LIBRARY_NAME = "Node Deletion Library"
NODE_TYPE = "GatedStreamNode"

_RUN_TIMEOUT_SECONDS = 30
_POLL_ATTEMPTS = 500
_POLL_SECONDS = 0.01

requires_fixture_library = pytest.mark.skipif(
    not FIXTURE_LIBRARY_JSON_TEMPLATE.exists(),
    reason=f"Node Deletion Library fixture missing at {FIXTURE_LIBRARY_JSON_TEMPLATE}",
)


@pytest.fixture
def parallel_mode(engine: Engine) -> Iterator[None]:
    """Run these tests under the parallel scheduler, which is what the editor defaults to.

    Sequential mode is parallel with ``max_nodes_in_parallel=1``, so the deletion paths are shared,
    but only parallel mode lets a second node be live while the first is parked in its gate.
    """
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


def _new_flow(engine: Engine, workflow_name: str) -> str:
    """Create a workflow context and a single top-level flow to hold the test's nodes."""
    engine.context_manager.push_workflow(workflow_name=workflow_name)
    result = engine.handle_request(CreateFlowRequest(parent_flow_name=None, flow_name="Flow", set_as_new_context=False))
    assert isinstance(result, CreateFlowResultSuccess), result
    return result.flow_name


def _set_parameter(engine: Engine, node_name: str, parameter_name: str, value: Any) -> None:
    engine.handle_request(SetParameterValueRequest(parameter_name=parameter_name, node_name=node_name, value=value))


def _configure(
    engine: Engine, node_name: str, *, text: str = "", gate_file: Path | None = None, chunk: str = ""
) -> None:
    """Set the fixture node's knobs. A gate file makes the node park until the test opens it."""
    _set_parameter(engine, node_name, "text", text)
    _set_parameter(engine, node_name, "gate_file", str(gate_file) if gate_file is not None else "")
    _set_parameter(engine, node_name, "chunk", chunk)


def _record_published(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, payload_type: type, extract: Callable[[Any], Any]
) -> list[Any]:
    """Tap the event bus and record every published payload of the given type.

    Assertions about what the editor would have *seen* have to read the published stream, not the
    engine's internal state: the whole failure class here is a run that is internally fine and
    externally silent.
    """
    recorded: list[Any] = []
    event_manager = engine.event_manager
    real_put_event = event_manager.put_event

    def _collect(event: object) -> None:
        payload = getattr(getattr(event, "wrapped_event", None), "payload", event)
        if isinstance(payload, payload_type):
            recorded.append(extract(payload))
        real_put_event(event)

    monkeypatch.setattr(event_manager, "put_event", _collect)
    return recorded


async def _wait_until_resolving(engine: Engine, node_name: str) -> None:
    """Block until the node has actually entered execution, so a deletion lands mid-run."""
    for _ in range(_POLL_ATTEMPTS):
        node = engine.node_manager.get_node_by_name(node_name)
        if node.state is NodeResolutionState.RESOLVING:
            return
        await asyncio.sleep(_POLL_SECONDS)
    pytest.fail(f"Node '{node_name}' never started executing, so the deletion could not land mid-run.")


async def _wait_until_one_is_resolving(engine: Engine, *node_names: str) -> str:
    """Block until exactly one of these nodes is executing, and name it.

    Which one the scheduler picks is its own business, so a test that needs "one of these started
    and the rest did not" has to ask rather than assume.
    """
    for _ in range(_POLL_ATTEMPTS):
        resolving = [
            name
            for name in node_names
            if engine.node_manager.get_node_by_name(name).state is NodeResolutionState.RESOLVING
        ]
        if len(resolving) == 1:
            return resolving[0]
        await asyncio.sleep(_POLL_SECONDS)
    pytest.fail(f"None of {node_names} started executing on its own, so the deletion could not land mid-run.")


async def _wait_until_resolved(engine: Engine, node_name: str) -> None:
    """Block until the node has finished and handed its outputs downstream."""
    for _ in range(_POLL_ATTEMPTS):
        node = engine.node_manager.get_node_by_name(node_name)
        if node.state is NodeResolutionState.RESOLVED:
            return
        await asyncio.sleep(_POLL_SECONDS)
    pytest.fail(f"Node '{node_name}' never resolved.")


async def _wait_for_progress(recorded: list[Any], node_name: str, *, at_least: int) -> None:
    """Block until the node has published at least this many streaming chunks."""
    for _ in range(_POLL_ATTEMPTS):
        if sum(1 for name in recorded if name == node_name) >= at_least:
            return
        await asyncio.sleep(_POLL_SECONDS)
    pytest.fail(f"Node '{node_name}' never streamed {at_least} chunks; streaming is the observable this test needs.")


async def _delete_node(engine: Engine, flow_name: str, node_name: str) -> DeleteNodeResultSuccess:
    """Delete a node the way the editor does: inside its flow's context, on the running loop.

    Awaited rather than dispatched synchronously because the deletion path itself reaches into the
    running flow, and a synchronous dispatch from inside a live loop cannot.
    """
    with engine.context_manager.flow(flow_name):
        result = await engine.ahandle_request(DeleteNodeRequest(node_name=node_name))
    assert isinstance(result, DeleteNodeResultSuccess), result
    return result


def _open_gate(gate_file: Path) -> None:
    gate_file.parent.mkdir(parents=True, exist_ok=True)
    gate_file.write_text("open")


async def _drain_cancelled_run(run: asyncio.Task) -> None:
    """Let a cancelled run settle, without caring what it settles into.

    A cancelled run may return a result, raise, or never resolve the node it was working on -- all
    three are acceptable, so none of them is asserted on. The wedge these tests exist to catch is
    caught by the caller's `check_for_existing_running_flow()` assertion instead; the bounded wait
    is only here so a wedged run fails the suite instead of hanging it.
    """
    with contextlib.suppress(TimeoutError, asyncio.CancelledError):
        await asyncio.wait_for(asyncio.shield(run), timeout=_RUN_TIMEOUT_SECONDS)
    run.cancel()


# ---------------------------------------------------------------------------------------------
# Unresolved work is lost -> cancelling the run is the correct outcome.
#
# Each of these asserts the same three things: the delete succeeds, the editor is told the run was
# cancelled, and the flow reads as not running afterwards. That last assertion is the stuck Run
# button: a run that wedges leaves it true forever.
# ---------------------------------------------------------------------------------------------


@requires_fixture_library
@pytest.mark.usefixtures("registered_library", "parallel_mode")
@pytest.mark.asyncio
async def test_deleting_the_running_node_cancels_the_run(
    tmp_path: Path,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    create_node: Callable[..., str],
) -> None:
    """Deleting the node that is currently executing must cancel the run, not wedge it."""
    flow_name = _new_flow(engine, "delete_running_node_wf")
    gate_file = tmp_path / "gates" / "runner.gate"

    create_node(NODE_TYPE, "Runner", flow_name, library_name=LIBRARY_NAME)
    _configure(engine, "Runner", text="never finishes", gate_file=gate_file)

    cancellations = _record_published(engine, monkeypatch, ControlFlowCancelledEvent, lambda _payload: True)

    run = asyncio.create_task(engine.ahandle_request(ResolveNodeRequest(node_name="Runner")))
    await _wait_until_resolving(engine, "Runner")

    delete_result = await _delete_node(engine, flow_name, "Runner")

    # Open the gate so a run that ignored the cancel still terminates instead of hanging the suite.
    _open_gate(gate_file)
    await _drain_cancelled_run(run)

    # Stopping someone's run is not something to do silently, and "it was still running" is the
    # whole reason -- so the result has to say both.
    assert "cancel" in str(delete_result.result_details).lower(), (
        f"The delete stopped a running workflow without saying so: {delete_result.result_details}"
    )
    assert "still running" in str(delete_result.result_details), delete_result.result_details

    assert cancellations, "Deleting the running node cancelled the run but never told the editor."
    assert engine.flow_manager.check_for_existing_running_flow() is False, (
        "The flow still reports itself as running, so the editor's Run button never clears."
    )


@requires_fixture_library
@pytest.mark.usefixtures("registered_library", "parallel_mode")
@pytest.mark.asyncio
async def test_deleting_an_unresolved_upstream_cancels_the_run(
    tmp_path: Path,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    create_node: Callable[..., str],
    connect: Callable[..., None],
) -> None:
    """An upstream node still parked in its gate owes the run work; deleting it must cancel."""
    flow_name = _new_flow(engine, "delete_unresolved_upstream_wf")
    upstream_gate = tmp_path / "gates" / "upstream.gate"

    create_node(NODE_TYPE, "Upstream", flow_name, library_name=LIBRARY_NAME)
    create_node(NODE_TYPE, "Downstream", flow_name, library_name=LIBRARY_NAME)
    connect("Upstream", "result", "Downstream", "linked_text")
    _configure(engine, "Upstream", text="from upstream", gate_file=upstream_gate)
    _configure(engine, "Downstream")

    cancellations = _record_published(engine, monkeypatch, ControlFlowCancelledEvent, lambda _payload: True)

    run = asyncio.create_task(engine.ahandle_request(ResolveNodeRequest(node_name="Downstream")))
    await _wait_until_resolving(engine, "Upstream")

    delete_result = await _delete_node(engine, flow_name, "Upstream")

    _open_gate(upstream_gate)
    await _drain_cancelled_run(run)

    # Saying why is what makes the cancellation make sense to whoever pressed delete -- otherwise
    # the run just stops for no stated reason. Here the deleted node was itself still parked in its
    # gate, so it is its own reason.
    assert "cancel" in str(delete_result.result_details).lower(), (
        f"The delete stopped a running workflow without saying so: {delete_result.result_details}"
    )
    assert "still running" in str(delete_result.result_details), delete_result.result_details

    assert cancellations, "Deleting an unresolved upstream cancelled the run but never told the editor."
    assert engine.flow_manager.check_for_existing_running_flow() is False, (
        "Downstream is stranded on a dependency that can never arrive and the flow never ends."
    )


@requires_fixture_library
@pytest.mark.usefixtures("registered_library", "parallel_mode")
@pytest.mark.asyncio
async def test_deleting_a_finished_node_a_queued_consumer_still_needs_cancels_the_run(
    tmp_path: Path,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    create_node: Callable[..., str],
    connect: Callable[..., None],
) -> None:
    """A node can be finished and *still* be needed, if a consumer has not been dispatched yet.

    Nodes collect their inputs from upstream at the moment they are dispatched, not when the upstream
    produces them. So a consumer still sitting in the queue has not received anything, and deleting
    its supplier -- however finished that supplier looks -- leaves it to run on its parameter
    defaults and the run to finish with the wrong answer and no complaint. That has to cancel.

    A diamond with one node running at a time is what makes the state reachable: once the supplier is
    done both consumers are ready, but only one can start, so the other is provably undispatched.
    """
    flow_name = _new_flow(engine, "delete_finished_node_queued_consumer_wf")
    engine.config_manager.set_config_value("max_nodes_in_parallel", 1)

    supplier_gate = tmp_path / "gates" / "supplier.gate"
    consumer_gate = tmp_path / "gates" / "consumer.gate"

    create_node(NODE_TYPE, "Supplier", flow_name, library_name=LIBRARY_NAME)
    create_node(NODE_TYPE, "ConsumerA", flow_name, library_name=LIBRARY_NAME)
    create_node(NODE_TYPE, "ConsumerB", flow_name, library_name=LIBRARY_NAME)
    create_node(NODE_TYPE, "Sink", flow_name, library_name=LIBRARY_NAME)

    connect("Supplier", "result", "ConsumerA", "linked_text")
    connect("Supplier", "result", "ConsumerB", "linked_text")
    # Two different inputs, so resolving Sink pulls both consumers into the same run.
    connect("ConsumerA", "result", "Sink", "linked_text")
    connect("ConsumerB", "result", "Sink", "text")

    _configure(engine, "Supplier", text="from supplier", gate_file=supplier_gate)
    _configure(engine, "ConsumerA", gate_file=consumer_gate)
    _configure(engine, "ConsumerB", gate_file=consumer_gate)
    _configure(engine, "Sink")

    cancellations = _record_published(engine, monkeypatch, ControlFlowCancelledEvent, lambda _payload: True)

    run = asyncio.create_task(engine.ahandle_request(ResolveNodeRequest(node_name="Sink")))

    await _wait_until_resolving(engine, "Supplier")
    _open_gate(supplier_gate)
    await _wait_until_resolved(engine, "Supplier")

    # One consumer is now executing; with a single slot, the other cannot have been dispatched.
    running_consumer = await _wait_until_one_is_resolving(engine, "ConsumerA", "ConsumerB")
    queued_consumer = "ConsumerB" if running_consumer == "ConsumerA" else "ConsumerA"

    delete_result = await _delete_node(engine, flow_name, "Supplier")

    _open_gate(consumer_gate)
    await _drain_cancelled_run(run)

    assert cancellations, (
        f"'{queued_consumer}' had not collected Supplier's value yet, so deleting Supplier had to "
        f"cancel rather than let it run on its defaults."
    )
    assert queued_consumer in str(delete_result.result_details), (
        f"The cancellation never named the node that still needed the deleted one: {delete_result.result_details}"
    )
    assert "waiting on it" in str(delete_result.result_details), delete_result.result_details
    assert engine.flow_manager.check_for_existing_running_flow() is False


@requires_fixture_library
@pytest.mark.usefixtures("registered_library", "parallel_mode")
@pytest.mark.asyncio
async def test_deleting_an_unresolved_node_from_a_control_chain_ends_the_run(
    tmp_path: Path,
    engine: Engine,
    create_node: Callable[..., str],
    connect: Callable[..., None],
) -> None:
    """A real ``StartFlowRequest`` run must still end when a chain member is deleted mid-execution.

    Every other test here drives a single node via ``ResolveNodeRequest``. This one exercises the
    control-chain path, which the scheduler advances by nodes reaching a done state — so a node
    removed mid-chain is exactly the case that can leave the walk with nowhere to go.
    """
    flow_name = _new_flow(engine, "delete_from_control_chain_wf")
    middle_gate = tmp_path / "gates" / "middle.gate"

    create_node("GatedStreamStartNode", "Start", flow_name, library_name=LIBRARY_NAME)
    create_node(NODE_TYPE, "Middle", flow_name, library_name=LIBRARY_NAME)
    create_node(NODE_TYPE, "Last", flow_name, library_name=LIBRARY_NAME)
    create_node("GatedStreamEndNode", "End", flow_name, library_name=LIBRARY_NAME)

    connect("Start", "exec_out", "Middle", "exec_in")
    connect("Middle", "exec_out", "Last", "exec_in")
    connect("Last", "exec_out", "End", "exec_in")
    connect("Last", "result", "End", "result")

    _set_parameter(engine, "Start", "text", "chain start")
    _configure(engine, "Middle", text="middle", gate_file=middle_gate)
    _configure(engine, "Last", text="last")

    run = asyncio.create_task(engine.ahandle_request(StartFlowRequest(flow_name=flow_name, wait_for_completion=True)))
    await _wait_until_resolving(engine, "Middle")

    await _delete_node(engine, flow_name, "Middle")

    _open_gate(middle_gate)
    await _drain_cancelled_run(run)

    assert engine.flow_manager.check_for_existing_running_flow() is False, (
        "The control chain was truncated by the deletion and the run never terminated."
    )


# ---------------------------------------------------------------------------------------------
# Nothing unresolved is lost -> the delete must succeed and the run must carry on untouched.
# ---------------------------------------------------------------------------------------------


@requires_fixture_library
@pytest.mark.usefixtures("registered_library", "parallel_mode")
@pytest.mark.asyncio
async def test_deleting_an_unrelated_node_does_not_interrupt_streaming(
    tmp_path: Path,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    create_node: Callable[..., str],
) -> None:
    """The reported bug: deleting an unconnected bystander must not disturb a streaming run.

    The bystander shares nothing with the run — no connections, not in the graph being resolved.
    Deleting it must leave the streamed chunks flowing and the final value correct.
    """
    flow_name = _new_flow(engine, "delete_bystander_wf")
    gate_file = tmp_path / "gates" / "survivor.gate"

    create_node(NODE_TYPE, "Survivor", flow_name, library_name=LIBRARY_NAME)
    create_node(NODE_TYPE, "Bystander", flow_name, library_name=LIBRARY_NAME)
    _configure(engine, "Survivor", text="survived", gate_file=gate_file, chunk="tick")
    _configure(engine, "Bystander", text="unrelated")

    streamed = _record_published(engine, monkeypatch, ProgressEvent, lambda payload: payload.node_name)

    run = asyncio.create_task(engine.ahandle_request(ResolveNodeRequest(node_name="Survivor")))
    await _wait_until_resolving(engine, "Survivor")
    await _wait_for_progress(streamed, "Survivor", at_least=3)

    delete_result = await _delete_node(engine, flow_name, "Bystander")
    chunks_at_delete = sum(1 for name in streamed if name == "Survivor")

    # Nothing was cancelled, so the result must not say anything was. A delete that cries wolf is
    # its own bug: the artist goes looking for a run that is still perfectly fine.
    assert "cancel" not in str(delete_result.result_details).lower(), (
        f"The delete claimed to cancel a run it left alone: {delete_result.result_details}"
    )

    # Streaming has to keep going *across* the deletion, not merely resume by the time the run ends.
    await _wait_for_progress(streamed, "Survivor", at_least=chunks_at_delete + 3)

    _open_gate(gate_file)
    await asyncio.wait_for(run, timeout=_RUN_TIMEOUT_SECONDS)

    survivor = engine.node_manager.get_node_by_name("Survivor")
    assert survivor.state is NodeResolutionState.RESOLVED
    assert survivor.parameter_output_values.get("result") == "survived"
    assert engine.flow_manager.check_for_existing_running_flow() is False


@requires_fixture_library
@pytest.mark.usefixtures("registered_library", "parallel_mode")
@pytest.mark.asyncio
async def test_deleting_a_resolved_upstream_leaves_its_running_successor_alone(
    tmp_path: Path,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    create_node: Callable[..., str],
    connect: Callable[..., None],
) -> None:
    """The sharpest case: a resolved upstream is deleted while its successor is still executing.

    The upstream has already handed its value downstream, so it owes the run nothing and the run
    must not be cancelled. But deleting it tears down the connection into ``linked_text``, which has
    no PROPERTY mode — so the ordinary connection-teardown path wants to reset that value to the
    parameter default while the successor is parked inside ``aprocess``.

    If it does, the run reports success with the wrong answer. That is worse than any hang, because
    nothing anywhere says something went wrong.
    """
    flow_name = _new_flow(engine, "delete_resolved_upstream_wf")
    upstream_gate = tmp_path / "gates" / "upstream.gate"
    downstream_gate = tmp_path / "gates" / "downstream.gate"

    create_node(NODE_TYPE, "Upstream", flow_name, library_name=LIBRARY_NAME)
    create_node(NODE_TYPE, "Downstream", flow_name, library_name=LIBRARY_NAME)
    connect("Upstream", "result", "Downstream", "linked_text")
    _configure(engine, "Upstream", text="from upstream", gate_file=upstream_gate)
    _configure(engine, "Downstream", text="not from the connection", gate_file=downstream_gate)

    unresolved = _record_published(engine, monkeypatch, NodeUnresolvedEvent, lambda payload: payload.node_name)

    run = asyncio.create_task(engine.ahandle_request(ResolveNodeRequest(node_name="Downstream")))

    # Let Upstream finish and propagate, then wait for Downstream to be parked in its own gate.
    await _wait_until_resolving(engine, "Upstream")
    _open_gate(upstream_gate)
    await _wait_until_resolved(engine, "Upstream")
    await _wait_until_resolving(engine, "Downstream")

    # Only events from the delete onward are interesting. Starting a run legitimately announces the
    # node it is about to run as unresolved, and that happened long before the delete.
    unresolved_before_delete = len(unresolved)
    await _delete_node(engine, flow_name, "Upstream")
    unresolved_after_delete = unresolved[unresolved_before_delete:]

    # Still parked in its gate, so it must still be holding the value it is running on.
    downstream = engine.node_manager.get_node_by_name("Downstream")
    assert downstream.parameter_values.get("linked_text") == "from upstream", (
        "The connection teardown reset a running node's input out from under it."
    )

    _open_gate(downstream_gate)
    await asyncio.wait_for(run, timeout=_RUN_TIMEOUT_SECONDS)

    assert downstream.state is NodeResolutionState.RESOLVED
    assert downstream.parameter_output_values.get("result") == "from upstream", (
        "Downstream was executing on the upstream value and finished on the parameter default "
        "instead. The run reported success with the wrong answer."
    )
    assert "Downstream" not in unresolved_after_delete, (
        "Downstream was announced unresolved while it was still executing, which the finishing run then contradicts."
    )
    assert engine.flow_manager.check_for_existing_running_flow() is False

    # The reset was deferred, not cancelled. Now that the node is done, the input has to be back at
    # its parameter default -- an input with no PROPERTY mode is invisible and unclearable in the
    # editor, so a value left behind would keep being produced on every later run.
    assert downstream.parameter_values.get("linked_text") == "", (
        "The deferred reset never applied, so the node keeps a value from a connection that no longer exists."
    )


@requires_fixture_library
@pytest.mark.usefixtures("registered_library", "parallel_mode")
@pytest.mark.asyncio
async def test_deleting_a_downstream_node_that_has_not_started_leaves_the_run_alone(
    tmp_path: Path,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    create_node: Callable[..., str],
    connect: Callable[..., None],
) -> None:
    """A node only ever *downstream* of live work is not entangled with it.

    ``Sink`` is wired to consume ``Producer``'s output but nothing is resolving it, so it has not
    started and the run does not depend on it. Deleting it mid-run must not take the run down.
    """
    flow_name = _new_flow(engine, "delete_downstream_wf")
    gate_file = tmp_path / "gates" / "producer.gate"

    create_node(NODE_TYPE, "Producer", flow_name, library_name=LIBRARY_NAME)
    create_node(NODE_TYPE, "Sink", flow_name, library_name=LIBRARY_NAME)
    connect("Producer", "result", "Sink", "linked_text")
    _configure(engine, "Producer", text="produced", gate_file=gate_file, chunk="tick")
    _configure(engine, "Sink")

    cancellations = _record_published(engine, monkeypatch, ControlFlowCancelledEvent, lambda _payload: True)

    run = asyncio.create_task(engine.ahandle_request(ResolveNodeRequest(node_name="Producer")))
    await _wait_until_resolving(engine, "Producer")

    await _delete_node(engine, flow_name, "Sink")

    _open_gate(gate_file)
    await asyncio.wait_for(run, timeout=_RUN_TIMEOUT_SECONDS)

    producer = engine.node_manager.get_node_by_name("Producer")
    assert not cancellations, "Deleting a not-yet-started downstream node cancelled a healthy run."
    assert producer.state is NodeResolutionState.RESOLVED
    assert producer.parameter_output_values.get("result") == "produced"
    assert engine.flow_manager.check_for_existing_running_flow() is False


@requires_fixture_library
@pytest.mark.usefixtures("registered_library", "parallel_mode")
@pytest.mark.asyncio
async def test_deleting_a_settled_node_removes_it_from_the_live_dag(
    tmp_path: Path,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    create_node: Callable[..., str],
    connect: Callable[..., None],
) -> None:
    """A node deleted mid-run must not be left behind in the scheduler's bookkeeping.

    Deleting a node removes it from the flow, but for a long time nothing removed it from the DAG
    the run is executing -- and that DAG is only emptied when the run tears down. A leftover entry
    is not inert: it is published to the editor as a node involved in the run, it is iterated when
    the run is cancelled, and it blocks a node of the same name from being started again.
    """
    flow_name = _new_flow(engine, "delete_settled_dag_hygiene_wf")
    upstream_gate = tmp_path / "gates" / "upstream.gate"
    downstream_gate = tmp_path / "gates" / "downstream.gate"

    create_node(NODE_TYPE, "Upstream", flow_name, library_name=LIBRARY_NAME)
    create_node(NODE_TYPE, "Downstream", flow_name, library_name=LIBRARY_NAME)
    connect("Upstream", "result", "Downstream", "linked_text")
    _configure(engine, "Upstream", text="from upstream", gate_file=upstream_gate)
    _configure(engine, "Downstream", text="downstream default", gate_file=downstream_gate)

    involved = _record_published(engine, monkeypatch, InvolvedNodesEvent, lambda payload: payload.involved_nodes)

    run = asyncio.create_task(engine.ahandle_request(ResolveNodeRequest(node_name="Downstream")))

    await _wait_until_resolving(engine, "Upstream")
    _open_gate(upstream_gate)
    await _wait_until_resolved(engine, "Upstream")
    await _wait_until_resolving(engine, "Downstream")

    dag_nodes = engine.flow_manager.global_dag_builder.node_to_reference
    assert "Upstream" in dag_nodes, "Test setup is wrong: Upstream was never in the live DAG to begin with."

    await _delete_node(engine, flow_name, "Upstream")

    assert "Upstream" not in dag_nodes, (
        "A deleted node was left in the live DAG, where it is still announced to the editor as part "
        "of the run and still iterated when the run is cancelled."
    )
    for graph in engine.flow_manager.global_dag_builder.graphs.values():
        assert "Upstream" not in graph.nodes()
    for graph_node_names in engine.flow_manager.global_dag_builder.graph_to_nodes.values():
        assert "Upstream" not in graph_node_names

    involved_before_finish = len(involved)
    _open_gate(downstream_gate)
    await asyncio.wait_for(run, timeout=_RUN_TIMEOUT_SECONDS)

    # Nothing published after the delete may still claim the deleted node is part of the run.
    for involved_nodes in involved[involved_before_finish:]:
        assert "Upstream" not in involved_nodes
    assert engine.flow_manager.check_for_existing_running_flow() is False
