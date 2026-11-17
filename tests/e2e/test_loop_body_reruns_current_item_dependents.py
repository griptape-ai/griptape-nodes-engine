"""A data node fed by a loop's ``current_item`` must re-evaluate per pass on every run, not just the first.

The loop body is packaged from the control path plus its upstream data dependencies, and the
dependency walk skips nodes that are already RESOLVED. After one run, a data node between
``current_item`` and the body is RESOLVED, so a second run left it out of the package and fed its
last-pass output into every iteration as a constant.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from griptape_nodes.exe_types.base_iterative_nodes import BaseIterativeStartNode
from griptape_nodes.retained_mode.events.connection_events import (
    CreateConnectionRequest,
    CreateConnectionResultSuccess,
)
from griptape_nodes.retained_mode.events.execution_events import (
    ResolveNodeRequest,
    ResolveNodeResultSuccess,
    StartFlowRequest,
    StartFlowResultSuccess,
)
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
from tests.e2e.fixtures.loop_library.loop_nodes import LoopStartNode

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


@pytest.mark.asyncio
@pytest.mark.parametrize("run_in_order", [True, False])
@pytest.mark.parametrize("run_whole_flow", [True, False])
async def test_current_item_dependent_re_evaluates_on_second_run(
    engine: Engine,
    materialize_library: Callable[..., Path],
    tmp_path: Path,
    *,
    run_in_order: bool,
    run_whole_flow: bool,
) -> None:
    """Run start -> body -> end twice, with a data node between ``current_item`` and the body."""
    library_json = materialize_library(
        tmp_path / "loop_library",
        template=FIXTURE_LIBRARY_JSON_TEMPLATE,
        node_file=FIXTURE_NODE_FILE,
        name=LIBRARY_NAME,
    )
    register_result = engine.handle_request(RegisterLibraryFromFileRequest(file_path=str(library_json)))
    assert isinstance(register_result, RegisterLibraryFromFileResultSuccess), register_result

    engine.context_manager.push_workflow(workflow_name="loop_rerun_e2e_workflow")
    flow_result = engine.handle_request(
        CreateFlowRequest(parent_flow_name=None, flow_name="LoopFlow", set_as_new_context=False)
    )
    assert isinstance(flow_result, CreateFlowResultSuccess), flow_result
    flow_name = flow_result.flow_name

    with engine.context_manager.flow(flow_name):
        start_name = _create_node(engine, "LoopStartNode", "Loop Start")
        # Data-only: sits between current_item and the body, off the control path.
        prompt_name = _create_node(engine, "LoopBodyNode", "Prompt")
        body_name = _create_node(engine, "LoopBodyNode", "Body Node")

        start_node = engine.node_manager.get_node_by_name(start_name)
        assert isinstance(start_node, BaseIterativeStartNode), start_node
        end_node = start_node.end_node
        assert end_node is not None, "The start node did not tether an end node."

        set_result = engine.handle_request(
            SetParameterValueRequest(node_name=start_name, parameter_name="run_in_order", value=run_in_order)
        )
        assert isinstance(set_result, SetParameterValueResultSuccess), set_result

        _connect(engine, start_name, "current_item", prompt_name, "text")
        _connect(engine, prompt_name, "result", body_name, "text")
        _connect(engine, start_name, "exec_out", body_name, "exec_in")
        _connect(engine, body_name, "exec_out", end_node.name, "add_item")
        _connect(engine, body_name, "result", end_node.name, "new_item_to_add")

        expected = list(LoopStartNode.ITEMS)
        # Packaging the loop body serializes nodes, which needs an active flow context.
        for run in ("first", "second"):
            if run_whole_flow:
                run_result = await engine.ahandle_request(StartFlowRequest(flow_name=flow_name))
                assert isinstance(run_result, StartFlowResultSuccess), run_result
            else:
                run_result = await engine.ahandle_request(ResolveNodeRequest(node_name=end_node.name))
                assert isinstance(run_result, ResolveNodeResultSuccess), run_result
            results = end_node.parameter_output_values.get("results")
            assert results == expected, f"The {run} run collected {results!r}, expected one result per item."
