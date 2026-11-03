"""A packaged loop body does not carry values its own connections will supply.

A loop runs its body as a rebuilt copy of the canvas nodes, built from a snapshot that is replayed
through the normal set-value path, validators included. A connected input in that snapshot is dead
on arrival: the copy's connections feed it before its node runs. Replaying it only adds a way to
fail, and it did (engine issue #5889): a node whose hard slider was narrowed after its value was set
holds a value its validator rejects, and rebuilding the body aborted on it before iteration 0.

The stand-in node narrows the slider on ``left`` whenever ``image_width`` is set. The canvas state
these tests build is: ``left`` restored to 80 by a workflow load while the slider allowed 100, then
a width of 50 arriving the way the next run's upstream value would.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from griptape_nodes.retained_mode.events.connection_events import (
    CreateConnectionRequest,
    CreateConnectionResultSuccess,
)
from griptape_nodes.retained_mode.events.execution_events import StartFlowRequest, StartFlowResultSuccess
from griptape_nodes.retained_mode.events.flow_events import (
    CreateFlowRequest,
    CreateFlowResultSuccess,
    DeleteFlowRequest,
    DeserializeFlowFromCommandsRequest,
    DeserializeFlowFromCommandsResultFailure,
    PackageNodesAsSerializedFlowRequest,
    PackageNodesAsSerializedFlowResultSuccess,
    SerializedFlowCommands,
    SerializeFlowToCommandsRequest,
    SerializeFlowToCommandsResultSuccess,
)
from griptape_nodes.retained_mode.events.library_events import (
    RegisterLibraryFromFileRequest,
    RegisterLibraryFromFileResultSuccess,
)
from griptape_nodes.retained_mode.events.node_events import (
    CreateNodeRequest,
    CreateNodeResultSuccess,
    SerializedNodeCommands,
    SetLockNodeStateRequest,
)
from griptape_nodes.retained_mode.events.parameter_events import (
    SetParameterValueRequest,
    SetParameterValueResultSuccess,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from griptape_nodes.retained_mode.engine import Engine

FIXTURE_LIBRARY_DIR = Path(__file__).parent / "fixtures" / "loop_body_values_library"
FIXTURE_LIBRARY_JSON_TEMPLATE = FIXTURE_LIBRARY_DIR / "griptape_nodes_library.json"
FIXTURE_NODE_FILE = FIXTURE_LIBRARY_DIR / "loop_body_values_nodes.py"
# The packager resolves its StartFlow/EndFlow endpoints from a library with exactly this name.
LIBRARY_NAME = "Griptape Nodes Library"

GROUP = "Loop"
SOURCE = "Source"
TARGET = "Target"
WIDE = 100
NARROW = 50
STALE_LEFT = 80

SEQUENTIAL = "Run Group Items One at a Time"
PARALLEL = "Run Group Items All at Once"


def _register_library(tmp_path: Path, engine: Engine, materialize_library: Callable[..., Path]) -> None:
    library_json = materialize_library(
        tmp_path / "library", template=FIXTURE_LIBRARY_JSON_TEMPLATE, node_file=FIXTURE_NODE_FILE, name=LIBRARY_NAME
    )
    result = engine.handle_request(RegisterLibraryFromFileRequest(file_path=str(library_json)))
    assert isinstance(result, RegisterLibraryFromFileResultSuccess), result


def _set(engine: Engine, node_name: str, parameter_name: str, value: object, *, initial_setup: bool = False) -> None:
    result = engine.handle_request(
        SetParameterValueRequest(
            node_name=node_name, parameter_name=parameter_name, value=value, initial_setup=initial_setup
        )
    )
    assert isinstance(result, SetParameterValueResultSuccess), result


def _connect(engine: Engine, source_node: str, source_param: str, target_node: str, target_param: str) -> None:
    result = engine.handle_request(
        CreateConnectionRequest(
            source_node_name=source_node,
            source_parameter_name=source_param,
            target_node_name=target_node,
            target_parameter_name=target_param,
        )
    )
    assert isinstance(result, CreateConnectionResultSuccess), (
        f"{source_node}.{source_param} -> {target_node}.{target_param}: {result}"
    )


def _build_loop(engine: Engine, flow_name: str) -> None:
    """A group whose body is Source -> Target, with Target.left fed by Source.result.

    Source.left is fed from the group boundary (current_item), so the body has one input fed from
    outside the group and one fed from inside it.
    """
    with engine.context_manager.flow(flow_name):
        group = engine.handle_request(
            CreateNodeRequest(node_type="ItemsForEachGroupNode", specific_library_name=LIBRARY_NAME, node_name=GROUP)
        )
        assert isinstance(group, CreateNodeResultSuccess), group
        for node_name in (SOURCE, TARGET):
            child = engine.handle_request(
                CreateNodeRequest(
                    node_type="NarrowingSliderNode",
                    specific_library_name=LIBRARY_NAME,
                    node_name=node_name,
                    parent_group_name=GROUP,
                )
            )
            assert isinstance(child, CreateNodeResultSuccess), child

    _connect(engine, GROUP, "on_each", SOURCE, "exec_in")
    _connect(engine, SOURCE, "exec_out", TARGET, "exec_in")
    _connect(engine, TARGET, "exec_out", GROUP, "loop_complete")
    _connect(engine, GROUP, "current_item", SOURCE, "left")
    _connect(engine, SOURCE, "result", TARGET, "left")
    _connect(engine, TARGET, "result", GROUP, "new_item_to_add")
    # The fixture node insists on `note` before a run. Source's is typed; Target's is set per test.
    _set(engine, SOURCE, "note", "typed")


def _leave_target_holding_a_stale_left(engine: Engine) -> None:
    """Put Target into the state a load followed by a narrower upstream value leaves it in.

    A load replays saved values after the connections exist, with initial_setup=True, which is the
    only path that writes a value onto a connected input. The next run's upstream value then
    narrows the slider under it.
    """
    _set(engine, TARGET, "image_width", WIDE)
    _set(engine, TARGET, "left", STALE_LEFT, initial_setup=True)
    _set(engine, TARGET, "image_width", NARROW)
    _set(engine, TARGET, "note", "kept")


class _NodeInFlow:
    """One serialized node and the flow commands that own it, since values are pooled per flow."""

    def __init__(self, flow_commands: SerializedFlowCommands, node_uuid: SerializedNodeCommands.NodeUUID) -> None:
        self.flow_commands = flow_commands
        self.node_uuid = node_uuid

    def set_values_by_parameter(self) -> dict[str, object]:
        """The non-output values that would be replayed onto this node, by parameter name."""
        values = {}
        for command in self.flow_commands.set_parameter_value_commands.get(self.node_uuid, []):
            set_command = command.set_parameter_value_command
            if set_command.is_output:
                continue
            values[set_command.parameter_name] = self.flow_commands.unique_parameter_uuid_to_values[
                command.unique_value_uuid
            ]
        return values


def _find_node(commands: SerializedFlowCommands, node_name: str) -> _NodeInFlow:
    """Find a node by name in these commands or any sub-flow's; group children live in a sub-flow."""
    for node_commands in commands.serialized_node_commands:
        if node_commands.create_node_command.node_name == node_name:
            return _NodeInFlow(commands, node_commands.node_uuid)
    for sub_flow_commands in commands.sub_flows_commands:
        try:
            return _find_node(sub_flow_commands, node_name)
        except AssertionError:
            continue
    msg = f"no serialized node named {node_name!r}"
    raise AssertionError(msg)


@pytest.fixture
def loop_flow(tmp_path: Path, engine: Engine, materialize_library: Callable[..., Path]) -> str:
    """A parent flow holding the loop, with Target already in the stale state."""
    _register_library(tmp_path, engine, materialize_library)
    engine.context_manager.push_workflow(workflow_name="loop_body_values")
    flow = engine.handle_request(CreateFlowRequest(parent_flow_name=None, flow_name="Parent", set_as_new_context=False))
    assert isinstance(flow, CreateFlowResultSuccess), flow
    _build_loop(engine, flow.flow_name)
    _leave_target_holding_a_stale_left(engine)
    return flow.flow_name


def test_package_leaves_out_values_its_connections_supply(engine: Engine, loop_flow: str) -> None:
    """A body input fed by a connection, from inside the group or across its boundary, is not replayed."""
    with engine.context_manager.flow(loop_flow):
        result = engine.handle_request(
            PackageNodesAsSerializedFlowRequest(node_names=[SOURCE, TARGET], node_group_name=GROUP)
        )
    assert isinstance(result, PackageNodesAsSerializedFlowResultSuccess), result
    commands = result.serialized_flow_commands

    target_values = _find_node(commands, TARGET).set_values_by_parameter()
    assert "left" not in target_values, "Target.left is fed by Source.result inside the body"
    assert target_values["note"] == "kept", "an unconnected value is still restored"
    assert target_values["image_width"] == NARROW

    source_values = _find_node(commands, SOURCE).set_values_by_parameter()
    assert "left" not in source_values, "Source.left is fed from the group boundary through the Start node"


def test_package_keeps_a_locked_nodes_connected_values(engine: Engine, loop_flow: str) -> None:
    """A locked body node never runs, so nothing would feed its inputs; its frozen values travel with it."""
    locked = engine.handle_request(SetLockNodeStateRequest(node_name=TARGET, lock=True))
    assert not locked.failed(), locked

    with engine.context_manager.flow(loop_flow):
        result = engine.handle_request(
            PackageNodesAsSerializedFlowRequest(node_names=[SOURCE, TARGET], node_group_name=GROUP)
        )
    assert isinstance(result, PackageNodesAsSerializedFlowResultSuccess), result

    target_values = _find_node(result.serialized_flow_commands, TARGET).set_values_by_parameter()
    assert target_values["left"] == STALE_LEFT


def test_saving_the_workflow_still_keeps_connected_values(engine: Engine, loop_flow: str) -> None:
    """Only the package changes. A saved workflow keeps the last value a connected input saw."""
    result = engine.handle_request(SerializeFlowToCommandsRequest(flow_name=loop_flow))
    assert isinstance(result, SerializeFlowToCommandsResultSuccess), result
    commands = result.serialized_flow_commands

    target_values = _find_node(commands, TARGET).set_values_by_parameter()
    assert target_values["left"] == STALE_LEFT


@pytest.mark.parametrize("execution_mode", [SEQUENTIAL, PARALLEL])
@pytest.mark.asyncio
async def test_loop_runs_when_a_connected_input_holds_a_stale_value(
    engine: Engine, loop_flow: str, execution_mode: str
) -> None:
    """The reporter's failure, in both execution modes: the body rebuilds and every iteration runs."""
    _set(engine, GROUP, "execution_mode", execution_mode)
    queue: asyncio.Queue = asyncio.Queue()
    engine.event_manager.initialize_queue(queue)

    with engine.context_manager.flow(loop_flow):
        run_result = await engine.ahandle_request(StartFlowRequest(flow_name=loop_flow, flow_node_name=GROUP))
    assert isinstance(run_result, StartFlowResultSuccess), run_result

    group = engine.node_manager.get_node_by_name(GROUP)
    assert group.parameter_output_values.get("results") == ["10", "20"]


def test_restore_failure_says_why(engine: Engine, loop_flow: str) -> None:
    """When a saved value is refused on restore, the error carries the validator's reason."""
    saved = engine.handle_request(SerializeFlowToCommandsRequest(flow_name=loop_flow))
    assert isinstance(saved, SerializeFlowToCommandsResultSuccess), saved
    deleted = engine.handle_request(DeleteFlowRequest(flow_name=loop_flow))
    assert not deleted.failed(), deleted

    restored = engine.handle_request(
        DeserializeFlowFromCommandsRequest(serialized_flow_commands=saved.serialized_flow_commands)
    )
    assert isinstance(restored, DeserializeFlowFromCommandsResultFailure), restored
    details = str(restored.result_details)
    assert f"'{TARGET}.left'" in details
    assert f"must be between 0 and {NARROW}" in details


@pytest.fixture
def loop_flow_with_connected_note(tmp_path: Path, engine: Engine, materialize_library: Callable[..., Path]) -> str:
    """The loop, with Target.note wired from Source.result instead of typed, and no value stored on it."""
    _register_library(tmp_path, engine, materialize_library)
    engine.context_manager.push_workflow(workflow_name="loop_body_connected_note")
    flow = engine.handle_request(CreateFlowRequest(parent_flow_name=None, flow_name="Parent", set_as_new_context=False))
    assert isinstance(flow, CreateFlowResultSuccess), flow
    _build_loop(engine, flow.flow_name)
    _connect(engine, SOURCE, "result", TARGET, "note")
    return flow.flow_name


@pytest.mark.parametrize("execution_mode", [SEQUENTIAL, PARALLEL])
@pytest.mark.asyncio
async def test_pre_run_validation_treats_a_connected_input_as_provided(
    engine: Engine, loop_flow_with_connected_note: str, execution_mode: str
) -> None:
    """The body copy is validated before its connections deliver. A connected input isn't blank then."""
    _set(engine, GROUP, "execution_mode", execution_mode)
    queue: asyncio.Queue = asyncio.Queue()
    engine.event_manager.initialize_queue(queue)

    with engine.context_manager.flow(loop_flow_with_connected_note):
        run_result = await engine.ahandle_request(
            StartFlowRequest(flow_name=loop_flow_with_connected_note, flow_node_name=GROUP)
        )
    assert isinstance(run_result, StartFlowResultSuccess), run_result
    group = engine.node_manager.get_node_by_name(GROUP)
    assert group.parameter_output_values.get("results") == ["10", "20"]


@pytest.mark.asyncio
async def test_pre_run_validation_still_reports_a_blank_unconnected_input(
    tmp_path: Path, engine: Engine, materialize_library: Callable[..., Path]
) -> None:
    """Nothing is wired into Target.note and nothing is typed, so the run is refused before it starts."""
    _register_library(tmp_path, engine, materialize_library)
    engine.context_manager.push_workflow(workflow_name="loop_body_blank_note")
    flow = engine.handle_request(CreateFlowRequest(parent_flow_name=None, flow_name="Parent", set_as_new_context=False))
    assert isinstance(flow, CreateFlowResultSuccess), flow
    _build_loop(engine, flow.flow_name)
    queue: asyncio.Queue = asyncio.Queue()
    engine.event_manager.initialize_queue(queue)

    with engine.context_manager.flow(flow.flow_name):
        run_result = await engine.ahandle_request(StartFlowRequest(flow_name=flow.flow_name, flow_node_name=GROUP))
    assert not isinstance(run_result, StartFlowResultSuccess), run_result
    assert 'Parameter "note" was left blank' in str(run_result.result_details)
