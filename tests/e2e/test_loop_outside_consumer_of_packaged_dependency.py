"""A loose data sink outside a legacy loop must see the current run's value, not the previous run's.

Shape: an unresolved node ("Sleep") feeds a data node ("Prompt") whose output goes to both a node
inside the loop body ("Body") and a loose data sink outside the loop with no control wires
("Outside Sink"). The loop packager pulls "Sleep" and "Prompt" into the packaged body (they are
data dependencies of "Body", which sits on the control path) and removes them from every DAG
network the outer flow runs in -- including the edge from "Prompt" to "Outside Sink". Under
PARALLEL execution, the outer flow can then run "Outside Sink" before packaging copies "Prompt"'s
fresh value back, so it reads the value "Prompt" held at the *end of the previous run*.

See [[parallel-resolution]] -- "Packaging drops a pulled-in node's edges to consumers outside the
loop" -- for the mechanism and PR griptape-ai/griptape-nodes-engine#5861.
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

pytestmark = pytest.mark.timeout(300, method="thread")

FIXTURE_LIBRARY_DIR = Path(__file__).parent / "fixtures" / "loop_library"
FIXTURE_LIBRARY_JSON_TEMPLATE = FIXTURE_LIBRARY_DIR / "griptape_nodes_library.json"
FIXTURE_NODE_FILE = FIXTURE_LIBRARY_DIR / "loop_nodes.py"
LIBRARY_NAME = "Griptape Nodes Library"


def _create_node(engine: Engine, node_type: str, node_name: str) -> str:
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
async def test_outside_loose_sink_sees_current_run_value(
    engine: Engine, materialize_library: Callable[..., Path], tmp_path: Path
) -> None:
    """Edit the upstream node's input between runs; the outside sink must track it every time."""
    library_json = materialize_library(
        tmp_path / "loop_library",
        template=FIXTURE_LIBRARY_JSON_TEMPLATE,
        node_file=FIXTURE_NODE_FILE,
        name=LIBRARY_NAME,
    )
    register_result = engine.handle_request(RegisterLibraryFromFileRequest(file_path=str(library_json)))
    assert isinstance(register_result, RegisterLibraryFromFileResultSuccess), register_result

    # The race needs independent nodes able to run while the loop's End node is mid-execution,
    # which SEQUENTIAL (max_nodes_in_parallel=1) never allows: the dispatch loop cannot start a
    # second task until the currently-running one finishes.
    config_manager = engine.config_manager
    prior_mode = config_manager.get_config_value("workflow_execution_mode")
    prior_max = config_manager.get_config_value("max_nodes_in_parallel")
    config_manager.set_config_value("workflow_execution_mode", "parallel")
    config_manager.set_config_value("max_nodes_in_parallel", 4)

    try:
        engine.context_manager.push_workflow(workflow_name="loop_outside_sink_e2e_workflow")
        flow_result = engine.handle_request(
            CreateFlowRequest(parent_flow_name=None, flow_name="LoopFlow", set_as_new_context=False)
        )
        assert isinstance(flow_result, CreateFlowResultSuccess), flow_result
        flow_name = flow_result.flow_name

        with engine.context_manager.flow(flow_name):
            sleep_name = _create_node(engine, "SlowSourceControlNode", "Sleep")
            prompt_name = _create_node(engine, "LoopBodyNode", "Prompt")
            start_name = _create_node(engine, "LoopStartNode", "Loop Start")
            body_name = _create_node(engine, "LoopBodyNode", "Body")
            outside_sink_name = _create_node(engine, "LoopBodyNode", "Outside Sink")

            start_node = engine.node_manager.get_node_by_name(start_name)
            assert isinstance(start_node, BaseIterativeStartNode), start_node
            end_node = start_node.end_node
            assert end_node is not None, "The start node did not tether an end node."

            # Sleep -> Prompt (both unresolved, pure data, no control wires).
            _connect(engine, sleep_name, "result_details", prompt_name, "text")
            # Prompt feeds both the loop body (control path) and the outside loose sink (no
            # control wires on either Prompt or Outside Sink).
            _connect(engine, prompt_name, "result", body_name, "text")
            _connect(engine, prompt_name, "result", outside_sink_name, "text")
            _connect(engine, start_name, "exec_out", body_name, "exec_in")
            _connect(engine, body_name, "exec_out", end_node.name, "add_item")

            observed = []
            for seconds in (0.1, 0.2, 0.3):
                set_result = engine.handle_request(
                    SetParameterValueRequest(node_name=sleep_name, parameter_name="seconds", value=seconds)
                )
                assert isinstance(set_result, SetParameterValueResultSuccess), set_result

                run_result = await engine.ahandle_request(StartFlowRequest(flow_name=flow_name, flow_node_name=None))
                assert isinstance(run_result, StartFlowResultSuccess), run_result

                expected_text = f"Slept for {seconds} seconds"
                prompt_node = engine.node_manager.get_node_by_name(prompt_name)
                body_node = engine.node_manager.get_node_by_name(body_name)
                outside_sink_node = engine.node_manager.get_node_by_name(outside_sink_name)

                prompt_result = str(prompt_node.parameter_output_values.get("result") or "")
                body_result = str(body_node.parameter_output_values.get("result") or "")
                outside_result = str(outside_sink_node.parameter_output_values.get("result") or "")

                observed.append((seconds, expected_text, prompt_result, body_result, outside_result))

                assert prompt_result == expected_text, f"seconds={seconds}: Prompt saw {prompt_result!r}"
                assert body_result == expected_text, f"seconds={seconds}: Body (inside loop) saw {body_result!r}"
                assert outside_result == expected_text, (
                    f"seconds={seconds}: Outside Sink saw {outside_result!r}, expected the "
                    f"current run's value {expected_text!r} (not a previous run's). All runs so "
                    f"far: {observed!r}"
                )
    finally:
        config_manager.set_config_value("workflow_execution_mode", prior_mode)
        config_manager.set_config_value("max_nodes_in_parallel", prior_max)
