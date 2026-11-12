"""A loop body rebuilt inside the engine must not announce its nodes to editors.

When a loop runs, ``NodeExecutor`` packages the body into a short-lived child flow and
deserializes it once per run (or once per iteration, in parallel). Each rebuild dispatches a
nested ``CreateNodeRequest``, and its success result reports the transient flow as the node's
parent. An editor that receives it asks the engine for that flow's details -- by which time
the flow has been torn down -- so the artist gets an error toast naming a flow they never
made. ``NodeExecutor._silence_packaged_node_creation_broadcasts`` clears
``broadcast_result`` on the packaged create commands to keep the rebuild inside the engine.

These tests pin the transport half of that contract end-to-end: that a deserialized flow's
``broadcast_result=False`` create command genuinely stays off the event queue while an
unflagged one reaches it. The paired positive case is what makes the negative case
meaningful -- without it the assertion would also pass if nothing were ever queued.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from griptape_nodes.common.node_executor import LOOP_EVENTS_TO_SUPPRESS
from griptape_nodes.retained_mode.events.base_events import GriptapeNodeEvent
from griptape_nodes.retained_mode.events.flow_events import (
    CreateFlowRequest,
    DeserializeFlowFromCommandsRequest,
    DeserializeFlowFromCommandsResultSuccess,
    SerializedFlowCommands,
)
from griptape_nodes.retained_mode.events.library_events import (
    RegisterLibraryFromFileRequest,
    RegisterLibraryFromFileResultSuccess,
)
from griptape_nodes.retained_mode.events.node_events import (
    CreateNodeRequest,
    CreateNodeResultSuccess,
    NodeDependencies,
    SerializedNodeCommands,
)
from griptape_nodes.retained_mode.managers.event_manager import EventSuppressionContext

if TYPE_CHECKING:
    from collections.abc import Callable

    from griptape_nodes.retained_mode.engine import Engine

# Timeout with thread dump.
pytestmark = pytest.mark.timeout(300, method="thread")

FIXTURE_LIBRARY_DIR = Path(__file__).parent / "fixtures" / "echo_library"
FIXTURE_LIBRARY_JSON_TEMPLATE = FIXTURE_LIBRARY_DIR / "griptape_nodes_library.json"
FIXTURE_NODE_FILE = FIXTURE_LIBRARY_DIR / "echo_node.py"
LIBRARY_NAME = "Echo Library"


def _register_echo_library(engine: Engine, materialize_library: Callable[..., Path], tmp_path: Path) -> None:
    library_json = materialize_library(
        tmp_path / "echo_library",
        template=FIXTURE_LIBRARY_JSON_TEMPLATE,
        node_file=FIXTURE_NODE_FILE,
    )
    register_result = engine.handle_request(RegisterLibraryFromFileRequest(file_path=str(library_json)))
    assert isinstance(register_result, RegisterLibraryFromFileResultSuccess), register_result
    # Creating a flow requires an active workflow in the Current Context.
    engine.context_manager.push_workflow(workflow_name="transient_broadcast_e2e_workflow")


def _build_body_flow_commands(*, broadcast_result: bool) -> SerializedFlowCommands:
    """One-node flow standing in for a packaged loop body, mirroring what the packager emits."""
    create_node_command = CreateNodeRequest(
        node_type="EchoNode",
        specific_library_name=LIBRARY_NAME,
        node_name="Body Node",
    )
    create_node_command.broadcast_result = broadcast_result
    return SerializedFlowCommands(
        flow_initialization_command=CreateFlowRequest(parent_flow_name=None, set_as_new_context=False),
        serialized_node_commands=[
            SerializedNodeCommands(
                create_node_command=create_node_command,
                element_modification_commands=[],
                node_dependencies=NodeDependencies(),
            )
        ],
        serialized_connections=[],
        unique_parameter_uuid_to_values={},
        set_parameter_value_commands={},
        set_lock_commands_per_node={},
        sub_flows_commands=[],
        node_dependencies=NodeDependencies(),
        node_types_used=set(),
    )


def _drain_created_node_results(queue: asyncio.Queue) -> list[CreateNodeResultSuccess]:
    """Every CreateNodeResultSuccess an editor would have received."""
    created_node_results = []
    while not queue.empty():
        event = queue.get_nowait()
        if not isinstance(event, GriptapeNodeEvent):
            continue
        result = getattr(event.wrapped_event, "result", None)
        if isinstance(result, CreateNodeResultSuccess):
            created_node_results.append(result)
    return created_node_results


def _deserialize_body_flow(
    engine: Engine, *, broadcast_result: bool, suppress_events: bool = False
) -> DeserializeFlowFromCommandsResultSuccess:
    """Deserialize the stand-in body flow with the event queue live, then return the result.

    ``put_event`` no-ops while ``_event_queue`` is None, so the queue must be initialized or
    the negative assertion would pass vacuously.
    """
    engine.event_manager.initialize_queue(asyncio.Queue())
    request = DeserializeFlowFromCommandsRequest(
        serialized_flow_commands=_build_body_flow_commands(broadcast_result=broadcast_result)
    )
    if suppress_events:
        with EventSuppressionContext(engine.event_manager, LOOP_EVENTS_TO_SUPPRESS):
            deserialize_result = engine.handle_request(request)
    else:
        deserialize_result = engine.handle_request(request)

    assert isinstance(deserialize_result, DeserializeFlowFromCommandsResultSuccess), deserialize_result
    return deserialize_result


def test_flagged_create_command_is_not_broadcast(
    engine: Engine, materialize_library: Callable[..., Path], tmp_path: Path
) -> None:
    """The node is really built, but no editor is told -- so none can ask about its flow."""
    _register_echo_library(engine, materialize_library, tmp_path)

    deserialize_result = _deserialize_body_flow(engine, broadcast_result=False)

    # The rebuild succeeded: the node exists under the transient flow.
    assert deserialize_result.node_name_mappings, deserialize_result
    created_node_name = next(iter(deserialize_result.node_name_mappings.values()))
    assert engine.object_manager.attempt_get_object_by_name(created_node_name) is not None

    queue = engine.event_manager._event_queue
    assert queue is not None
    created_node_results = _drain_created_node_results(queue)
    parent_flow_names = [result.parent_flow_name for result in created_node_results]
    assert created_node_results == [], (
        f"A packaged body node was announced to editors, naming parent flow(s) {parent_flow_names}. "
        "An editor will ask the engine for those flows and toast an error once they are torn down."
    )


def test_unflagged_create_command_is_broadcast(
    engine: Engine, materialize_library: Callable[..., Path], tmp_path: Path
) -> None:
    """Control case: without the flag the result does reach the queue.

    Proves the negative assertion above is detecting the flag rather than an inert queue.
    """
    _register_echo_library(engine, materialize_library, tmp_path)

    _deserialize_body_flow(engine, broadcast_result=True)

    queue = engine.event_manager._event_queue
    assert queue is not None
    created_node_results = _drain_created_node_results(queue)
    assert len(created_node_results) == 1, created_node_results


def test_suppression_window_catches_an_unflagged_create_command(
    engine: Engine, materialize_library: Callable[..., Path], tmp_path: Path
) -> None:
    """The backstop: a creation that escaped the flag is still hidden inside a loop's window.

    ``LOOP_EVENTS_TO_SUPPRESS`` is what covers any future path that rebuilds a loop body without
    going through ``_silence_packaged_node_creation_broadcasts``. Same input as the control case
    above -- ``broadcast_result=True`` -- so the only difference is the suppression window.
    """
    _register_echo_library(engine, materialize_library, tmp_path)

    _deserialize_body_flow(engine, broadcast_result=True, suppress_events=True)

    queue = engine.event_manager._event_queue
    assert queue is not None
    assert _drain_created_node_results(queue) == []
