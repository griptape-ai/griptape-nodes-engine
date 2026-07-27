"""Regression test for issue #4994: serializable=False params must not retain RESOLVED state after save/load.

When a node has a serializable=False output parameter and is saved in the RESOLVED state,
the parameter value is (correctly) not persisted. But the RESOLVED state IS persisted,
so on load the engine thinks the node has already run and skips recomputation. Any
downstream node that reads from that parameter sees None instead of the recomputed value.

The fix: nodes with serializable=False parameters that participate in connections must
not be saved as RESOLVED, so they are recomputed on the next run after load.

This test builds a Producer -> Consumer chain where the producer's output is a
non-serializable Session object (analogous to a driver or connection handle), runs
the flow so both nodes resolve, serializes the flow (the "save" step), then
deserializes into a fresh flow (the "load" step). The producer must NOT be restored
as RESOLVED, because its output value was not persisted.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from griptape_nodes.exe_types.node_types import NodeResolutionState
from griptape_nodes.retained_mode.events.execution_events import StartFlowRequest, StartFlowResultSuccess
from griptape_nodes.retained_mode.events.flow_events import (
    CreateFlowRequest,
    CreateFlowResultSuccess,
    DeserializeFlowFromCommandsRequest,
    DeserializeFlowFromCommandsResultSuccess,
    SerializeFlowToCommandsRequest,
    SerializeFlowToCommandsResultSuccess,
)
from griptape_nodes.retained_mode.events.library_events import (
    RegisterLibraryFromFileRequest,
    RegisterLibraryFromFileResultSuccess,
)
from griptape_nodes.retained_mode.events.object_events import ClearAllObjectStateRequest
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

if TYPE_CHECKING:
    from collections.abc import Callable

# Timeout with thread dump.
pytestmark = pytest.mark.timeout(300, method="thread")

FIXTURE_LIBRARY_DIR = Path(__file__).parent / "fixtures" / "nonserializable_library"
FIXTURE_LIBRARY_JSON_TEMPLATE = FIXTURE_LIBRARY_DIR / "griptape_nodes_library.json"
FIXTURE_NODE_FILE = FIXTURE_LIBRARY_DIR / "nonserializable_nodes.py"
LIBRARY_NAME = "NonSerializable Library"


@pytest.mark.skipif(
    not FIXTURE_LIBRARY_JSON_TEMPLATE.exists(),
    reason=f"NonSerializable Library fixture missing at {FIXTURE_LIBRARY_JSON_TEMPLATE}",
)
@pytest.mark.asyncio
async def test_serializable_false_output_reresolved_on_load(
    tmp_path: Path,
    materialize_library: Callable[..., Path],
    create_node: Callable[..., str],
    connect: Callable[..., None],
) -> None:
    """A node with serializable=False output must not be re-resolved after save/load.

    Steps:
        1. Register fixture library, build Producer -> Consumer flow.
        2. Run the flow so both nodes resolve and the consumer gets the producer's value.
        3. Serialize the flow (save).
        4. Clear state, create a fresh flow, deserialize (load).
        5. Run the loaded flow — the consumer must get a recomputed Session, not None.
    """
    library_json = materialize_library(
        tmp_path / "library", template=FIXTURE_LIBRARY_JSON_TEMPLATE, node_file=FIXTURE_NODE_FILE
    )
    register_result = GriptapeNodes.handle_request(RegisterLibraryFromFileRequest(file_path=str(library_json)))
    assert isinstance(register_result, RegisterLibraryFromFileResultSuccess), register_result

    GriptapeNodes.ContextManager().push_workflow(workflow_name="nonserializable_test_wf")

    flow_result = GriptapeNodes.handle_request(
        CreateFlowRequest(parent_flow_name=None, flow_name="TestFlow", set_as_new_context=False)
    )
    assert isinstance(flow_result, CreateFlowResultSuccess), flow_result
    flow_name = flow_result.flow_name

    create_node("ProducerNode", "Producer", flow_name, library_name=LIBRARY_NAME)
    create_node("ConsumerNode", "Consumer", flow_name, library_name=LIBRARY_NAME)
    connect("Producer", "session", "Consumer", "session")

    # --- Step 2: Run the flow so both nodes become RESOLVED ---
    run_result = await GriptapeNodes.ahandle_request(
        StartFlowRequest(
            flow_name=flow_name,
            flow_node_name="Consumer",
            wait_for_completion=True,
            completion_timeout_ms=30_000,
        )
    )
    assert isinstance(run_result, StartFlowResultSuccess), run_result

    node_manager = GriptapeNodes.NodeManager()
    producer = node_manager.get_node_by_name("Producer")
    consumer = node_manager.get_node_by_name("Consumer")

    assert producer.state == NodeResolutionState.RESOLVED, "Producer should be RESOLVED after running"
    assert consumer.state == NodeResolutionState.RESOLVED, "Consumer should be RESOLVED after running"

    produced_session = producer.parameter_output_values.get("session")
    assert produced_session is not None, "Producer should have produced a Session"
    assert produced_session.marker == "live-session-marker"
    assert consumer.parameter_output_values.get("marker") == "live-session-marker"

    # --- Step 3: Serialize (save) ---
    serialize_result = GriptapeNodes.handle_request(SerializeFlowToCommandsRequest(flow_name=flow_name))
    assert isinstance(serialize_result, SerializeFlowToCommandsResultSuccess), serialize_result
    saved_commands = serialize_result.serialized_flow_commands

    # --- Step 4: Clear state and deserialize (load) ---
    GriptapeNodes.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
    GriptapeNodes.ContextManager().push_workflow(workflow_name="nonserializable_test_wf_reloaded")

    deserialize_result = GriptapeNodes.handle_request(
        DeserializeFlowFromCommandsRequest(serialized_flow_commands=saved_commands)
    )
    assert isinstance(deserialize_result, DeserializeFlowFromCommandsResultSuccess), deserialize_result
    loaded_flow = deserialize_result.flow_name

    # --- Step 5: Run the loaded flow — consumer must get a recomputed Session, not None ---
    restored_consumer_name = deserialize_result.node_name_mappings.get("Consumer", "Consumer")
    run_result_2 = await GriptapeNodes.ahandle_request(
        StartFlowRequest(
            flow_name=loaded_flow,
            flow_node_name=restored_consumer_name,
            wait_for_completion=True,
            completion_timeout_ms=30_000,
        )
    )
    assert isinstance(run_result_2, StartFlowResultSuccess), run_result_2

    restored_consumer = node_manager.get_node_by_name(restored_consumer_name)
    assert restored_consumer.parameter_output_values.get("marker") == "live-session-marker"
