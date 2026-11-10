"""An iterative group collects its own ``index`` wired straight into its own ``new_item_to_add``.

The group is not packaged with its children, so this wire has no packaged endpoint and the
packager carries it from the StartFlow node to the EndFlow node. The wire also makes the group a
data dependency of itself, which the DAG must ignore or the group never runs at all.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from griptape_nodes.retained_mode.events.connection_events import (
    CreateConnectionRequest,
    CreateConnectionResultSuccess,
)
from griptape_nodes.retained_mode.events.execution_events import StartFlowRequest, StartFlowResultSuccess
from griptape_nodes.retained_mode.events.flow_events import CreateFlowRequest, CreateFlowResultSuccess
from griptape_nodes.retained_mode.events.library_events import (
    RegisterLibraryFromFileRequest,
    RegisterLibraryFromFileResultSuccess,
)
from griptape_nodes.retained_mode.events.node_events import CreateNodeRequest, CreateNodeResultSuccess
from griptape_nodes.retained_mode.events.parameter_events import SetParameterValueRequest
from tests.workflow_context import open_and_bind

if TYPE_CHECKING:
    from collections.abc import Callable

    from griptape_nodes.retained_mode.engine import Engine

# Timeout with thread dump.
pytestmark = pytest.mark.timeout(300, method="thread")

FIXTURE_LIBRARY_DIR = Path(__file__).parent / "fixtures" / "iterative_group_library"
FIXTURE_LIBRARY_JSON_TEMPLATE = FIXTURE_LIBRARY_DIR / "griptape_nodes_library.json"
FIXTURE_NODE_FILE = FIXTURE_LIBRARY_DIR / "iterative_group_nodes.py"
# The packager asks for its StartFlow/EndFlow endpoints from this library by name.
LIBRARY_NAME = "Griptape Nodes Library"

GROUP_NODE_NAME = "Group"
CHILD_NODE_NAME = "Child"


async def _run_group_with_own_index_as_item(
    engine: Engine, materialize_library: Callable[..., Path], tmp_path: Path, execution_mode: str
) -> list[Any]:
    """Group with one child on the control chain and ``Group.index -> Group.new_item_to_add``."""
    library_json = materialize_library(
        tmp_path / "library", template=FIXTURE_LIBRARY_JSON_TEMPLATE, node_file=FIXTURE_NODE_FILE, name=LIBRARY_NAME
    )
    register_result = engine.handle_request(RegisterLibraryFromFileRequest(file_path=str(library_json)))
    assert isinstance(register_result, RegisterLibraryFromFileResultSuccess), register_result

    open_and_bind(engine, "group_own_item_wf")
    parent_result = engine.handle_request(
        CreateFlowRequest(parent_flow_name=None, flow_name="ParentFlow", set_as_new_context=False)
    )
    assert isinstance(parent_result, CreateFlowResultSuccess), parent_result
    parent_flow = parent_result.flow_name

    with engine.context_manager.flow(parent_flow):
        group_result = engine.handle_request(
            CreateNodeRequest(
                node_type="ProbeForEachGroupNode", specific_library_name=LIBRARY_NAME, node_name=GROUP_NODE_NAME
            )
        )
        assert isinstance(group_result, CreateNodeResultSuccess), group_result
        child_result = engine.handle_request(
            CreateNodeRequest(
                node_type="LoopBodyControlNode",
                specific_library_name=LIBRARY_NAME,
                node_name=CHILD_NODE_NAME,
                parent_group_name=group_result.node_name,
            )
        )
        assert isinstance(child_result, CreateNodeResultSuccess), child_result

    for source_node, source_param, target_node, target_param in (
        (GROUP_NODE_NAME, "on_each", CHILD_NODE_NAME, "exec_in"),
        (CHILD_NODE_NAME, "exec_out", GROUP_NODE_NAME, "loop_complete"),
        (GROUP_NODE_NAME, "index", GROUP_NODE_NAME, "new_item_to_add"),
    ):
        connection_result = engine.handle_request(
            CreateConnectionRequest(
                source_node_name=source_node,
                source_parameter_name=source_param,
                target_node_name=target_node,
                target_parameter_name=target_param,
            )
        )
        assert isinstance(connection_result, CreateConnectionResultSuccess), (
            f"{source_node}.{source_param} -> {target_node}.{target_param}: {connection_result}"
        )

    engine.handle_request(
        SetParameterValueRequest(parameter_name="execution_mode", node_name=GROUP_NODE_NAME, value=execution_mode)
    )

    with engine.context_manager.flow(parent_flow):
        run_result = await engine.ahandle_request(
            StartFlowRequest(flow_name=parent_flow, flow_node_name=GROUP_NODE_NAME)
        )
    assert isinstance(run_result, StartFlowResultSuccess), run_result

    node = engine.node_manager.get_node_by_name(GROUP_NODE_NAME)
    assert node.state.value == "resolved", f"The group did not run (state {node.state.value})."
    return list(node.parameter_output_values.get("results") or [])


@pytest.mark.asyncio
@pytest.mark.parametrize("execution_mode", ["Run Group Items One at a Time", "Run Group Items All at Once"])
async def test_group_collects_its_own_index(
    engine: Engine, materialize_library: Callable[..., Path], tmp_path: Path, execution_mode: str
) -> None:
    """Every pass collects the group's own index, in either execution mode."""
    results = await _run_group_with_own_index_as_item(engine, materialize_library, tmp_path, execution_mode)
    assert results == [0, 1, 2], f"The group collected {results!r}."
