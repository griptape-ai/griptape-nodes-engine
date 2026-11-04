"""A data node outside a loop runs once across the passes of a sequential loop, and once for a group.

Legacy Start/End loops package unresolved upstream data dependencies into the loop body, outside
nodes included (``NodeExecutor._collect_loop_body_nodes``). With ``run_in_order`` set, that one flow
instance is reused for every pass, so the packaged copy runs once: an Agent or image generator
outside the loop makes one call, not one per pass. The legacy tests here set ``run_in_order``
explicitly, because the fixture defaults it to False.

Not pinned: the legacy loop in "all at once" mode, which deserializes a fresh copy of the body per
pass and so runs the packaged outside node once per pass. That is a limitation of the two-node
Start/End pair, which is being deprecated in favor of the group nodes (griptape-nodes-engine#6016).
The outside counter is an instance attribute that restarts at zero in every deserialized copy, so a
test of that mode would also need a counter that survives packaging.

The iterative group path never packages outside nodes; its outside values arrive already resolved
through ``get_resolved_upstream_values``. It is covered here so both loop shapes are pinned.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from griptape_nodes.exe_types.base_iterative_nodes import BaseIterativeStartNode
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

if TYPE_CHECKING:
    from collections.abc import Callable

    from griptape_nodes.retained_mode.engine import Engine

pytestmark = pytest.mark.timeout(300, method="thread")

LOOP_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "loop_library"
LOOP_LIBRARY_JSON = LOOP_FIXTURE_DIR / "griptape_nodes_library.json"
LOOP_NODE_FILE = LOOP_FIXTURE_DIR / "loop_nodes.py"

GROUP_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "iterative_group_library"
GROUP_LIBRARY_JSON = GROUP_FIXTURE_DIR / "griptape_nodes_library.json"
GROUP_NODE_FILE = GROUP_FIXTURE_DIR / "iterative_group_nodes.py"

LIBRARY_NAME = "Griptape Nodes Library"
TOTAL_ITERATIONS = 3


def _connect(engine: Engine, source: str, source_param: str, target: str, target_param: str) -> None:
    result = engine.handle_request(
        CreateConnectionRequest(
            source_node_name=source,
            source_parameter_name=source_param,
            target_node_name=target,
            target_parameter_name=target_param,
        )
    )
    assert isinstance(result, CreateConnectionResultSuccess), (
        f"{source}.{source_param} -> {target}.{target_param}: {result}"
    )


def _create(engine: Engine, node_type: str, node_name: str, **kwargs: object) -> str:
    result = engine.handle_request(
        CreateNodeRequest(
            node_type=node_type,
            specific_library_name=LIBRARY_NAME,
            node_name=node_name,
            metadata={"position": {"x": 0, "y": 0}},
            **kwargs,  # type: ignore[arg-type]
        )
    )
    assert isinstance(result, CreateNodeResultSuccess), result
    return result.node_name


async def _run_legacy_loop_with_outside_dependency(
    tmp_path: Path, engine: Engine, materialize_library: Callable[..., Path], *, with_downstream_sink: bool
) -> int:
    """Legacy Start/End loop; Outside -> Body (data).

    ``StartFlowRequest.flow_node_name`` is deliberately never set: a Start/End loop's control
    graph is cyclic (End's iteration signal loops back to Start), and
    ``FlowManager.get_start_node_from_node``/``get_prev_node`` walk incoming control connections
    with no visited set -- supplying an explicit target hangs forever (see
    [[transient-iteration-flow]] "Related bug: FlowManager cycle-guard hang"). Omitting it lets
    the normal queue discover the real control entry (``Loop Start``) and avoids the hang.

    ``with_downstream_sink`` adds a node after End that depends on End's data output, so End gets
    pulled into the DAG as a data dependency of something further downstream rather than only
    being reached by the loop's own control chain completing -- the #5753 shape.

    Returns the outside node's run_count after a full run.
    """
    library_json = materialize_library(
        tmp_path / "loop_library", template=LOOP_LIBRARY_JSON, node_file=LOOP_NODE_FILE, name=LIBRARY_NAME
    )
    register_result = engine.handle_request(RegisterLibraryFromFileRequest(file_path=str(library_json)))
    assert isinstance(register_result, RegisterLibraryFromFileResultSuccess), register_result

    engine.context_manager.push_workflow(workflow_name="outside_dep_legacy_wf")
    flow_result = engine.handle_request(
        CreateFlowRequest(parent_flow_name=None, flow_name="LoopFlow", set_as_new_context=False)
    )
    assert isinstance(flow_result, CreateFlowResultSuccess), flow_result

    with engine.context_manager.flow(flow_result.flow_name):
        _create(engine, "OutsideCounterDataNode", "Outside")
        start_name = _create(engine, "LoopStartNode", "Loop Start")
        body_name = _create(engine, "LoopBodyNode", "Body Node")

        start_node = engine.node_manager.get_node_by_name(start_name)
        assert isinstance(start_node, BaseIterativeStartNode), start_node
        end_node = start_node.end_node
        assert end_node is not None

        _connect(engine, start_name, "exec_out", body_name, "exec_in")
        _connect(engine, body_name, "exec_out", end_node.name, "add_item")
        _connect(engine, "Outside", "value", body_name, "text")
        # One reused body flow per run is what makes "runs once" hold; see the module docstring.
        engine.handle_request(SetParameterValueRequest(parameter_name="run_in_order", node_name=start_name, value=True))

        if with_downstream_sink:
            downstream_name = _create(engine, "AnySinkDataNode", "Downstream Sink")
            _connect(engine, end_node.name, "results", downstream_name, "value")

    with engine.context_manager.flow(flow_result.flow_name):
        run_result = await engine.ahandle_request(StartFlowRequest(flow_name=flow_result.flow_name))
    assert isinstance(run_result, StartFlowResultSuccess), run_result

    # The ORIGINAL "Outside" node is marked RESOLVED and excluded from outer-flow execution
    # once it is pulled into packaging (see _package_loop_body's "Mark all packaged nodes as
    # RESOLVED" step) -- its run_count never moves. The node that actually executes is a
    # freshly deserialized COPY living inside the packaged loop-body flow, under a generated
    # name this test does not know. Read the run count back out through the one thing that
    # does cross the packaging boundary: the copy's output value, relayed through Body's
    # connected "result".
    body_node = engine.node_manager.get_node_by_name("Body Node")
    body_result = str(body_node.parameter_output_values.get("result") or "")
    match = re.search(r"outside-run-(\d+)", body_result)
    return int(match.group(1)) if match else 0


async def _run_group_loop_with_outside_dependency(
    tmp_path: Path, engine: Engine, materialize_library: Callable[..., Path]
) -> int:
    """ForEach group; Outside is OUTSIDE the group, feeding one of its children through a proxy."""
    library_json = materialize_library(
        tmp_path / "iterative_group_library", template=GROUP_LIBRARY_JSON, node_file=GROUP_NODE_FILE, name=LIBRARY_NAME
    )
    register_result = engine.handle_request(RegisterLibraryFromFileRequest(file_path=str(library_json)))
    assert isinstance(register_result, RegisterLibraryFromFileResultSuccess), register_result

    engine.context_manager.push_workflow(workflow_name="outside_dep_group_wf")
    flow_result = engine.handle_request(
        CreateFlowRequest(parent_flow_name=None, flow_name="ParentFlow", set_as_new_context=False)
    )
    assert isinstance(flow_result, CreateFlowResultSuccess), flow_result

    with engine.context_manager.flow(flow_result.flow_name):
        outside_result = engine.handle_request(
            CreateNodeRequest(
                node_type="OutsideCounterDataNode", specific_library_name=LIBRARY_NAME, node_name="Outside"
            )
        )
        assert isinstance(outside_result, CreateNodeResultSuccess), outside_result
        group_result = engine.handle_request(
            CreateNodeRequest(node_type="ProbeForEachGroupNode", specific_library_name=LIBRARY_NAME, node_name="Group")
        )
        assert isinstance(group_result, CreateNodeResultSuccess), group_result
        sink_result = engine.handle_request(
            CreateNodeRequest(
                node_type="LoopBodyControlNode",
                specific_library_name=LIBRARY_NAME,
                node_name="Sink",
                parent_group_name="Group",
            )
        )
        assert isinstance(sink_result, CreateNodeResultSuccess), sink_result

    for source_node, source_param, target_node, target_param in (
        ("Group", "on_each", "Sink", "exec_in"),
        ("Sink", "exec_out", "Group", "loop_complete"),
        ("Outside", "value", "Sink", "text"),
    ):
        _connect(engine, source_node, source_param, target_node, target_param)

    engine.handle_request(
        SetParameterValueRequest(
            parameter_name="execution_mode", node_name="Group", value="Run Group Items One at a Time"
        )
    )

    with engine.context_manager.flow(flow_result.flow_name):
        run_result = await engine.ahandle_request(
            StartFlowRequest(flow_name=flow_result.flow_name, flow_node_name="Group")
        )
    assert isinstance(run_result, StartFlowResultSuccess), run_result

    outside_node = engine.node_manager.get_node_by_name("Outside")
    return int(getattr(outside_node, "run_count", 0))


@pytest.mark.asyncio
async def test_outside_dependency_resolved_from_end_directly(
    tmp_path: Path, engine: Engine, materialize_library: Callable[..., Path]
) -> None:
    """A sequential legacy loop whose End is reached by its own control chain runs the outside node once."""
    run_count = await _run_legacy_loop_with_outside_dependency(
        tmp_path, engine, materialize_library, with_downstream_sink=False
    )
    assert run_count == 1, f"outside dependency ran {run_count} times over {TOTAL_ITERATIONS} passes, expected 1"


@pytest.mark.asyncio
async def test_outside_dependency_resolved_from_downstream_node(
    tmp_path: Path, engine: Engine, materialize_library: Callable[..., Path]
) -> None:
    """Same, when End is reached as a downstream node's data dependency (issue #5753's shape)."""
    run_count = await _run_legacy_loop_with_outside_dependency(
        tmp_path, engine, materialize_library, with_downstream_sink=True
    )
    assert run_count == 1, f"outside dependency ran {run_count} times over {TOTAL_ITERATIONS} passes, expected 1"


@pytest.mark.asyncio
async def test_outside_dependency_for_group_node(
    tmp_path: Path, engine: Engine, materialize_library: Callable[..., Path]
) -> None:
    """An iterative group runs the outside node once."""
    run_count = await _run_group_loop_with_outside_dependency(tmp_path, engine, materialize_library)
    assert run_count == 1, f"outside dependency ran {run_count} times, expected exactly 1"
