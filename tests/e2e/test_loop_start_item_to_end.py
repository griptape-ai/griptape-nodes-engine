"""A loop collects ``current_item`` wired straight from its start node into the end node's ``new_item_to_add``.

The start node is not part of the packaged loop body, so the end node's item comes from no
body node. With a non-empty body the loop must still collect the start node's per-pass value,
the same way an empty body does, and collect nothing for a pass whose body failed or skipped.
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
from griptape_nodes.retained_mode.events.execution_events import StartFlowRequest
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
from tests.workflow_context import open_and_bind

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


async def _run_loop(  # noqa: PLR0913 (fixtures plus the knobs that shape the loop)
    engine: Engine,
    materialize_library: Callable[..., Path],
    tmp_path: Path,
    *,
    body_node_type: str,
    source_param: str,
    run_in_order: bool,
    body_exits: tuple[tuple[str, str], ...] = (("exec_out", "add_item"),),
) -> list | None:
    """Run start -> body -> end with ``source_param`` wired from the start node to ``new_item_to_add``.

    ``body_exits`` pairs each body control output with the end node control input it leads to.
    """
    library_json = materialize_library(
        tmp_path / "loop_library",
        template=FIXTURE_LIBRARY_JSON_TEMPLATE,
        node_file=FIXTURE_NODE_FILE,
        name=LIBRARY_NAME,
    )
    register_result = engine.handle_request(RegisterLibraryFromFileRequest(file_path=str(library_json)))
    assert isinstance(register_result, RegisterLibraryFromFileResultSuccess), register_result

    open_and_bind(engine, "loop_start_item_e2e_workflow")
    flow_result = engine.handle_request(
        CreateFlowRequest(parent_flow_name=None, flow_name="LoopFlow", set_as_new_context=False)
    )
    assert isinstance(flow_result, CreateFlowResultSuccess), flow_result
    flow_name = flow_result.flow_name

    with engine.context_manager.flow(flow_name):
        start_name = _create_node(engine, "LoopStartNode", "Loop Start")
        body_name = _create_node(engine, body_node_type, "Body Node")

        start_node = engine.node_manager.get_node_by_name(start_name)
        assert isinstance(start_node, BaseIterativeStartNode), start_node
        end_node = start_node.end_node
        assert end_node is not None, "The start node did not tether an end node."

        set_result = engine.handle_request(
            SetParameterValueRequest(node_name=start_name, parameter_name="run_in_order", value=run_in_order)
        )
        assert isinstance(set_result, SetParameterValueResultSuccess), set_result

        _connect(engine, start_name, "current_item", body_name, "text")
        _connect(engine, start_name, "exec_out", body_name, "exec_in")
        for body_output, end_input in body_exits:
            _connect(engine, body_name, body_output, end_node.name, end_input)
        _connect(engine, start_name, source_param, end_node.name, "new_item_to_add")

        await engine.ahandle_request(StartFlowRequest(flow_name=flow_name))
        return end_node.parameter_output_values.get("results")


@pytest.mark.asyncio
@pytest.mark.parametrize("run_in_order", [True, False])
@pytest.mark.parametrize(
    ("source_param", "expected"),
    [("current_item", list(LoopStartNode.ITEMS)), ("index", list(range(len(LoopStartNode.ITEMS))))],
)
async def test_start_node_item_collected_with_non_empty_body(  # noqa: PLR0913 (fixtures plus parametrized knobs)
    engine: Engine,
    materialize_library: Callable[..., Path],
    tmp_path: Path,
    *,
    run_in_order: bool,
    source_param: str,
    expected: list,
) -> None:
    """Every pass collects the start node's value."""
    results = await _run_loop(
        engine,
        materialize_library,
        tmp_path,
        body_node_type="LoopBodyNode",
        source_param=source_param,
        run_in_order=run_in_order,
    )
    assert results == expected, f"The loop collected {results!r}, expected {expected!r}."


@pytest.mark.asyncio
async def test_start_node_item_not_collected_for_failed_pass(
    engine: Engine,
    materialize_library: Callable[..., Path],
    tmp_path: Path,
) -> None:
    """A parallel pass whose body raised contributes None, not the start node's item."""
    results = await _run_loop(
        engine,
        materialize_library,
        tmp_path,
        body_node_type="FailOnSecondNode",
        source_param="current_item",
        run_in_order=False,
    )
    assert results == ["first", None, "third"], f"The loop collected {results!r}."


@pytest.mark.asyncio
@pytest.mark.parametrize("run_in_order", [True, False])
async def test_start_node_item_not_collected_for_skipped_pass(
    engine: Engine,
    materialize_library: Callable[..., Path],
    tmp_path: Path,
    *,
    run_in_order: bool,
) -> None:
    """A filter body that exits through skip_iteration drops that pass's item, in either run order."""
    results = await _run_loop(
        engine,
        materialize_library,
        tmp_path,
        body_node_type="SkipSecondNode",
        source_param="current_item",
        run_in_order=run_in_order,
        body_exits=(("keep", "add_item"), ("drop", "skip_iteration")),
    )
    assert results == ["first", "third"], f"The loop collected {results!r}."
