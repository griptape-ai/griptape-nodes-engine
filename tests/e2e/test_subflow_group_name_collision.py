"""Regression test for the SubflowNodeGroup subflow-name collision bug (runtime symptom).

See https://github.com/griptape-ai/griptape-nodes-engine/issues/5126.

Root cause / trigger:

* ``SubflowNodeGroup._create_subflow`` derives ``f"{self.name}_subflow"`` and
  stores it in ``metadata['subflow_name']`` *before* ``CreateFlowRequest``,
  never syncing to the deduplicated ``result.flow_name``.
* Renaming a group frees its default node name; the next group reuses it,
  recomputes the SAME subflow name, collides on flow creation, and silently
  keeps the stale (first group's) name -- routing its member into the first
  group's subflow.

This test reproduces the *runtime* symptom: two groups collide, then running the
first group also executes the second group's member (on a different fork), which
raises because it has no valid input.
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from griptape_nodes.exe_types.node_types import NodeResolutionState
from griptape_nodes.node_library.library_registry import LibraryRegistry
from griptape_nodes.retained_mode.events.execution_events import StartFlowRequest, StartFlowResultSuccess
from griptape_nodes.retained_mode.events.flow_events import CreateFlowRequest, CreateFlowResultSuccess
from griptape_nodes.retained_mode.events.library_events import (
    RegisterLibraryFromFileRequest,
    RegisterLibraryFromFileResultSuccess,
)
from griptape_nodes.retained_mode.events.node_events import CreateNodeRequest, CreateNodeResultSuccess
from griptape_nodes.retained_mode.events.object_events import (
    ClearAllObjectStateRequest,
    ClearAllObjectStateResultSuccess,
    RenameObjectRequest,
    RenameObjectResultSuccess,
)
from griptape_nodes.retained_mode.events.parameter_events import SetParameterValueRequest
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

if TYPE_CHECKING:
    from collections.abc import Iterator

FIXTURE_LIBRARY_DIR = Path(__file__).parent / "fixtures" / "subflow_library"
FIXTURE_LIBRARY_JSON_TEMPLATE = FIXTURE_LIBRARY_DIR / "griptape_nodes_library.json"
FIXTURE_NODE_FILE = FIXTURE_LIBRARY_DIR / "subflow_echo_node.py"
# Distinct name so the LibraryManager's LOADED bookkeeping doesn't collide with the
# other in-process subflow tests sharing a worker.
LIBRARY_NAME = "Subflow Group Metadata Collision Library"


def _materialize_library(target_dir: Path) -> Path:
    from griptape_nodes.utils.version_utils import engine_version

    target_dir.mkdir(parents=True, exist_ok=True)
    schema = json.loads(FIXTURE_LIBRARY_JSON_TEMPLATE.read_text())
    schema["name"] = LIBRARY_NAME
    schema["metadata"]["engine_version"] = engine_version
    library_json = target_dir / "griptape_nodes_library.json"
    library_json.write_text(json.dumps(schema, indent=2))
    (target_dir / FIXTURE_NODE_FILE.name).write_text(FIXTURE_NODE_FILE.read_text())
    return library_json


@pytest.fixture
def _clean_engine_state() -> Iterator[None]:
    """Keep the shared engine singleton clean despite running in-process."""
    GriptapeNodes.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
    yield
    clear_result = GriptapeNodes.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
    assert isinstance(clear_result, ClearAllObjectStateResultSuccess), clear_result
    with contextlib.suppress(KeyError):
        LibraryRegistry.unregister_library(LIBRARY_NAME)


@pytest.mark.skipif(
    not FIXTURE_LIBRARY_JSON_TEMPLATE.exists(),
    reason=f"Subflow Library fixture missing at {FIXTURE_LIBRARY_JSON_TEMPLATE}",
)
@pytest.mark.asyncio
@pytest.mark.usefixtures("_clean_engine_state")
async def test_running_first_group_does_not_execute_second_groups_member(tmp_path: Path) -> None:
    """Running the first group must execute ONLY its own member, not the second group's.

    Builds the collision trigger (group, rename, group-reusing-the-freed-name), where the
    first group's member has a valid input and the second group's member has none. Under
    the bug both members share one subflow, so running the first group also runs the second
    group's member, which raises ``Echo node must have text input``. After the fix each
    group owns its own subflow and running the first group succeeds.
    """
    library_json = _materialize_library(tmp_path / "library")
    register_result = GriptapeNodes.handle_request(RegisterLibraryFromFileRequest(file_path=str(library_json)))
    assert isinstance(register_result, RegisterLibraryFromFileResultSuccess), register_result

    GriptapeNodes.ContextManager().push_workflow(workflow_name="subflow_collision_wf")

    parent_result = GriptapeNodes.handle_request(
        CreateFlowRequest(parent_flow_name=None, flow_name="ParentFlow", set_as_new_context=False)
    )
    assert isinstance(parent_result, CreateFlowResultSuccess), parent_result
    parent_flow = parent_result.flow_name

    with GriptapeNodes.ContextManager().flow(parent_flow):
        # ChildA has a valid input; ChildB has none, so ChildB raises if it is ever executed.
        child_a = GriptapeNodes.handle_request(
            CreateNodeRequest(node_type="EchoNode", specific_library_name=LIBRARY_NAME, node_name="ChildA")
        )
        assert isinstance(child_a, CreateNodeResultSuccess), child_a
        child_b = GriptapeNodes.handle_request(
            CreateNodeRequest(node_type="EchoNode", specific_library_name=LIBRARY_NAME, node_name="ChildB")
        )
        assert isinstance(child_b, CreateNodeResultSuccess), child_b

        set_result = GriptapeNodes.handle_request(
            SetParameterValueRequest(node_name="ChildA", parameter_name="text", value="hello")
        )
        assert set_result.succeeded(), set_result

        # --- Group A around ChildA, then rename it (frees the default group name) ---
        group_a = GriptapeNodes.handle_request(
            CreateNodeRequest(
                node_type="SubflowGroupNode",
                specific_library_name=LIBRARY_NAME,
                node_name="G",
                node_names_to_add=["ChildA"],
            )
        )
        assert isinstance(group_a, CreateNodeResultSuccess), group_a
        rename_a = GriptapeNodes.handle_request(RenameObjectRequest(object_name="G", requested_name="GroupA"))
        assert isinstance(rename_a, RenameObjectResultSuccess), rename_a

        # --- Group B reuses the freed name "G", triggering the subflow-name collision ---
        group_b = GriptapeNodes.handle_request(
            CreateNodeRequest(
                node_type="SubflowGroupNode",
                specific_library_name=LIBRARY_NAME,
                node_name="G",
                node_names_to_add=["ChildB"],
            )
        )
        assert isinstance(group_b, CreateNodeResultSuccess), group_b
        rename_b = GriptapeNodes.handle_request(RenameObjectRequest(object_name="G", requested_name="GroupB"))
        assert isinstance(rename_b, RenameObjectResultSuccess), rename_b

    # Run ONLY the first group. It must not drag the second group's member into execution.
    run_result = await GriptapeNodes.ahandle_request(
        StartFlowRequest(
            flow_name=parent_flow,
            flow_node_name="GroupA",
            wait_for_completion=True,
            completion_timeout_ms=30000,
        )
    )
    assert isinstance(run_result, StartFlowResultSuccess), run_result

    node_manager = GriptapeNodes.NodeManager()
    child_a_node = node_manager.get_node_by_name("ChildA")
    child_b_node = node_manager.get_node_by_name("ChildB")

    assert child_a_node.state == NodeResolutionState.RESOLVED, child_a_node.state
    assert child_a_node.parameter_output_values.get("text") == "hello"
    # ChildB belongs to the OTHER group/fork and must never have executed.
    assert child_b_node.state == NodeResolutionState.UNRESOLVED, child_b_node.state
