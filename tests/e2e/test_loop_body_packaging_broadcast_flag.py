"""Where a loop body's node creations get silenced is load-bearing, so pin it.

``NodeExecutor._silence_packaged_node_creation_broadcasts`` clears ``broadcast_result`` on a
packaged body's create commands. Packaging is the obvious place to call it and the wrong one:
packaging runs *before* the execution-environment branch, and the private and cloud-publisher
branches hand the very same ``serialized_flow_commands`` to
``SaveWorkflowFileFromSerializedFlowRequest``. Workflow codegen writes out every create-command
field whose value differs from its default, so silencing at packaging time puts a transport flag
into a generated ``.py`` file -- on the publish path, into a library.

Nothing about the three local call sites is visible from a unit test of the helper, and reinstating
the packaging-time call keeps every other test in this change green. This test is the one that goes
red: it packages a real loop body and asserts the commands come out of the packager unsilenced.

See ``tests/unit/retained_mode/managers/test_workflow_manager_broadcast_flag_codegen.py`` for the
other half -- that codegen really is that literal about non-default fields.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from griptape_nodes.common.node_executor import NodeExecutor
from griptape_nodes.exe_types.base_iterative_nodes import BaseIterativeStartNode
from griptape_nodes.retained_mode.events.connection_events import (
    CreateConnectionRequest,
    CreateConnectionResultSuccess,
)
from griptape_nodes.retained_mode.events.flow_events import (
    CreateFlowRequest,
    CreateFlowResultSuccess,
    PackageNodesAsSerializedFlowResultSuccess,
)
from griptape_nodes.retained_mode.events.library_events import (
    RegisterLibraryFromFileRequest,
    RegisterLibraryFromFileResultSuccess,
)
from griptape_nodes.retained_mode.events.node_events import CreateNodeRequest, CreateNodeResultSuccess

if TYPE_CHECKING:
    from collections.abc import Callable

    from griptape_nodes.retained_mode.engine import Engine

# Timeout with thread dump.
pytestmark = pytest.mark.timeout(300, method="thread")

FIXTURE_LIBRARY_DIR = Path(__file__).parent / "fixtures" / "loop_library"
FIXTURE_LIBRARY_JSON_TEMPLATE = FIXTURE_LIBRARY_DIR / "griptape_nodes_library.json"
FIXTURE_NODE_FILE = FIXTURE_LIBRARY_DIR / "loop_nodes.py"
# The packager asks for its StartFlow/EndFlow endpoints from this library by name.
LIBRARY_NAME = "Griptape Nodes Library"


def _create_node(engine: Engine, node_type: str, node_name: str) -> str:
    # An iterative start node auto-creates its paired end node beside it, which needs a position
    # to offset from, so every node here carries one.
    result = engine.handle_request(
        CreateNodeRequest(
            node_type=node_type,
            specific_library_name=LIBRARY_NAME,
            node_name=node_name,
            metadata={"position": {"x": 0, "y": 0}},
        )
    )
    assert isinstance(result, CreateNodeResultSuccess), result
    return result.node_name


def _connect(engine: Engine, source: str, source_param: str, target: str, target_param: str) -> None:
    result = engine.handle_request(
        CreateConnectionRequest(
            source_node_name=source,
            source_parameter_name=source_param,
            target_node_name=target,
            target_parameter_name=target_param,
        )
    )
    assert isinstance(result, CreateConnectionResultSuccess), result


async def _package_a_real_loop_body(
    engine: Engine, materialize_library: Callable[..., Path], tmp_path: Path
) -> PackageNodesAsSerializedFlowResultSuccess:
    """Build start -> body -> end in a real flow and run the packager over it."""
    library_json = materialize_library(
        tmp_path / "loop_library",
        template=FIXTURE_LIBRARY_JSON_TEMPLATE,
        node_file=FIXTURE_NODE_FILE,
        name=LIBRARY_NAME,
    )
    register_result = engine.handle_request(RegisterLibraryFromFileRequest(file_path=str(library_json)))
    assert isinstance(register_result, RegisterLibraryFromFileResultSuccess), register_result

    engine.context_manager.push_workflow(workflow_name="loop_packaging_e2e_workflow")
    flow_result = engine.handle_request(
        CreateFlowRequest(parent_flow_name=None, flow_name="LoopFlow", set_as_new_context=False)
    )
    assert isinstance(flow_result, CreateFlowResultSuccess), flow_result

    # Serializing a node pushes a node context, which requires an active flow, so the whole build
    # and the packaging call happen inside the flow context.
    with engine.context_manager.flow(flow_result.flow_name):
        # Creating the start node auto-creates and tethers its paired end node, exactly as the
        # editor path does, so the pair does not need wiring up by hand here.
        start_name = _create_node(engine, "LoopStartNode", "Loop Start")
        body_name = _create_node(engine, "LoopBodyNode", "Body Node")

        start_node = engine.node_manager.get_node_by_name(start_name)
        assert isinstance(start_node, BaseIterativeStartNode), start_node
        end_node = start_node.end_node
        assert end_node is not None, "The start node did not tether an end node."

        _connect(engine, start_name, "exec_out", body_name, "exec_in")
        _connect(engine, body_name, "exec_out", end_node.name, "add_item")

        packaged = await NodeExecutor(engine=engine)._package_loop_body(start_node, end_node)

    assert packaged is not None, "The loop body came back empty, so this test asserted nothing."
    package_result, _execution_type = packaged
    assert package_result.serialized_flow_commands.serialized_node_commands, (
        "No node commands were packaged, so this test asserted nothing."
    )
    return package_result


def _broadcast_flags(package_result: PackageNodesAsSerializedFlowResultSuccess) -> dict[str, bool]:
    flags: dict[str, bool] = {}
    for serialized_node in package_result.serialized_flow_commands.serialized_node_commands:
        create_command = serialized_node.create_node_command
        flags[create_command.node_name or "<unnamed>"] = create_command.broadcast_result
    return flags


@pytest.mark.asyncio
async def test_packaging_a_loop_body_leaves_its_create_commands_unsilenced(
    engine: Engine, materialize_library: Callable[..., Path], tmp_path: Path
) -> None:
    """The packager's output is save-safe: whatever silences it must do so further downstream."""
    package_result = await _package_a_real_loop_body(engine, materialize_library, tmp_path)

    silenced = sorted(name for name, broadcasts in _broadcast_flags(package_result).items() if broadcasts is False)

    assert silenced == [], (
        f"Packaging silenced the create commands for {silenced}. The private and cloud-publisher "
        "branches save these very commands to a workflow file, and codegen writes non-default "
        "fields out -- so broadcast_result=False would land in the artist's .py file. Silence at "
        "the local deserialization boundary instead."
    )


@pytest.mark.asyncio
async def test_silencing_the_packaged_body_is_what_clears_the_flag(
    engine: Engine, materialize_library: Callable[..., Path], tmp_path: Path
) -> None:
    """The paired positive case: the flag is still reachable on a real package result.

    Without this, the assertion above would keep passing if the helper stopped working entirely.
    """
    package_result = await _package_a_real_loop_body(engine, materialize_library, tmp_path)

    NodeExecutor._silence_packaged_node_creation_broadcasts(package_result)

    assert all(broadcasts is False for broadcasts in _broadcast_flags(package_result).values())
