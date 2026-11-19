"""An iterative group leaves out of ``results`` every pass whose body exits through ``skip_iteration``.

The body is a filter: it sends control to ``skip_iteration`` on one item and to ``loop_complete``
on the rest, while the group's ``index`` is wired to its own ``new_item_to_add``.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from griptape_nodes.exe_types.node_groups.base_iterative_node_group import EXECUTION_MODE_CHOICES
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
from griptape_nodes.retained_mode.events.parameter_events import (
    SetParameterValueRequest,
    SetParameterValueResultSuccess,
)

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
FILTER_NODE_NAME = "Filter"


async def _run_filter_group(
    engine: Engine, materialize_library: Callable[..., Path], tmp_path: Path, execution_mode: str
) -> list[Any]:
    library_json = materialize_library(
        tmp_path / "library", template=FIXTURE_LIBRARY_JSON_TEMPLATE, node_file=FIXTURE_NODE_FILE, name=LIBRARY_NAME
    )
    register_result = engine.handle_request(RegisterLibraryFromFileRequest(file_path=str(library_json)))
    assert isinstance(register_result, RegisterLibraryFromFileResultSuccess), register_result

    engine.context_manager.push_workflow(workflow_name="group_skip_iteration_wf")
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
        filter_result = engine.handle_request(
            CreateNodeRequest(
                node_type="SkipIndexOneControlNode",
                specific_library_name=LIBRARY_NAME,
                node_name=FILTER_NODE_NAME,
                parent_group_name=group_result.node_name,
            )
        )
        assert isinstance(filter_result, CreateNodeResultSuccess), filter_result

    for source_node, source_param, target_node, target_param in (
        (GROUP_NODE_NAME, "on_each", FILTER_NODE_NAME, "exec_in"),
        (GROUP_NODE_NAME, "index", FILTER_NODE_NAME, "index"),
        (FILTER_NODE_NAME, "keep", GROUP_NODE_NAME, "loop_complete"),
        (FILTER_NODE_NAME, "drop", GROUP_NODE_NAME, "skip_iteration"),
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

    set_result = engine.handle_request(
        SetParameterValueRequest(parameter_name="execution_mode", node_name=GROUP_NODE_NAME, value=execution_mode)
    )
    assert isinstance(set_result, SetParameterValueResultSuccess), set_result

    with engine.context_manager.flow(parent_flow):
        run_result = await engine.ahandle_request(
            StartFlowRequest(flow_name=parent_flow, flow_node_name=GROUP_NODE_NAME)
        )
    assert isinstance(run_result, StartFlowResultSuccess), run_result

    node = engine.node_manager.get_node_by_name(GROUP_NODE_NAME)
    assert node.state.value == "resolved", f"The group did not run (state {node.state.value})."
    return list(node.parameter_output_values.get("results") or [])


@pytest.mark.asyncio
@pytest.mark.parametrize("execution_mode", EXECUTION_MODE_CHOICES)
async def test_group_leaves_skipped_items_out_of_results(
    engine: Engine, materialize_library: Callable[..., Path], tmp_path: Path, execution_mode: str
) -> None:
    """A skipped pass adds nothing to results, in either execution mode."""
    results = await _run_filter_group(engine, materialize_library, tmp_path, execution_mode)
    assert results == [0, 2], f"The group collected {results!r}."
