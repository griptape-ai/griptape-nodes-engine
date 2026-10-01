"""A loop group reads its exit from the branch the body took, not from nodes that merely ran.

Each pass packages the body behind an End node that collects every body node's outputs, so a node
on a branch that was not taken can still run, pulled in to supply that data. The group used to
ask such a node which control output it would follow next, and a plain node always answers with
its only one, so its exit counted as taken. A body that branched one way exited as though it had
branched the other way.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

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
from griptape_nodes.retained_mode.events.parameter_events import (
    SetParameterValueRequest,
    SetParameterValueResultSuccess,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from griptape_nodes.retained_mode.engine import Engine

# Timeout with thread dump.
pytestmark = pytest.mark.timeout(300, method="thread")

FIXTURE_LIBRARY_DIR = Path(__file__).parent / "fixtures" / "loop_branch_library"
FIXTURE_LIBRARY_JSON_TEMPLATE = FIXTURE_LIBRARY_DIR / "griptape_nodes_library.json"
FIXTURE_NODE_FILE = FIXTURE_LIBRARY_DIR / "loop_branch_nodes.py"
# `flow_manager._validate_and_get_multi_node_library_info` resolves the packaged flow's
# StartFlow/EndFlow endpoints from a library with exactly this name.
LIBRARY_NAME = "Griptape Nodes Library"

FLOW_NAME = "ControlFlow_1"
GROUP_NAME = "Loop"
BRANCH_NAME = "Branch"
STEP_NAME = "Step"

MAX_ITERATIONS = 3
FOR_EACH_ITEM_COUNT = 3


@pytest.fixture
def flow(tmp_path: Path, engine: Engine, materialize_library: Callable[..., Path]) -> str:
    """An empty flow on an engine with the loop branching library registered."""
    library_json = materialize_library(
        tmp_path / "library", template=FIXTURE_LIBRARY_JSON_TEMPLATE, node_file=FIXTURE_NODE_FILE, name=LIBRARY_NAME
    )
    register_result = engine.handle_request(RegisterLibraryFromFileRequest(file_path=str(library_json)))
    assert isinstance(register_result, RegisterLibraryFromFileResultSuccess), register_result

    engine.context_manager.push_workflow(workflow_name="loop_branch_wf")
    flow_result = engine.handle_request(
        CreateFlowRequest(parent_flow_name=None, flow_name=FLOW_NAME, set_as_new_context=False)
    )
    assert isinstance(flow_result, CreateFlowResultSuccess), flow_result
    return flow_result.flow_name


class TestWhileGroup:
    @pytest.mark.asyncio
    async def test_done_ends_the_loop_when_the_other_branch_ends_in_continue(self, engine: Engine, flow: str) -> None:
        # Branch.yes -> Step -> continue_loop, Branch.no -> done. Taking "no" ends the loop on the
        # first pass, however Step's exit is wired.
        _create_node(engine, flow, "ProbeWhileGroupNode", GROUP_NAME, max_iterations=MAX_ITERATIONS)
        _create_node(engine, flow, "BranchNode", BRANCH_NAME, group=GROUP_NAME, take="no")
        _create_node(engine, flow, "StepNode", STEP_NAME, group=GROUP_NAME)
        _connect(engine, BRANCH_NAME, "yes", STEP_NAME, "exec_in")
        _connect(engine, STEP_NAME, "exec_out", GROUP_NAME, "continue_loop")
        _connect(engine, BRANCH_NAME, "no", GROUP_NAME, "done")

        result = await _run(engine, flow)

        assert isinstance(result, StartFlowResultSuccess), result
        group = engine.node_manager.get_node_by_name(GROUP_NAME)
        assert group.parameter_output_values["total_iterations"] == 1
        assert len(_runs_of(engine, BRANCH_NAME)) == 1


class TestForEachGroup:
    @pytest.mark.asyncio
    async def test_loop_complete_continues_the_loop_when_the_other_branch_ends_in_break(
        self, engine: Engine, flow: str
    ) -> None:
        # on_each -> Branch, Branch.yes -> Step -> break_loop, Branch.no -> loop_complete. Taking
        # "no" every pass completes every item, however Step's exit is wired.
        _create_node(engine, flow, "ProbeForEachGroupNode", GROUP_NAME)
        _create_node(engine, flow, "BranchNode", BRANCH_NAME, group=GROUP_NAME, take="no")
        _create_node(engine, flow, "StepNode", STEP_NAME, group=GROUP_NAME)
        _connect(engine, GROUP_NAME, "on_each", BRANCH_NAME, "exec_in")
        _connect(engine, BRANCH_NAME, "yes", STEP_NAME, "exec_in")
        _connect(engine, STEP_NAME, "exec_out", GROUP_NAME, "break_loop")
        _connect(engine, BRANCH_NAME, "no", GROUP_NAME, "loop_complete")

        result = await _run(engine, flow)

        assert isinstance(result, StartFlowResultSuccess), result
        assert len(_runs_of(engine, BRANCH_NAME)) == FOR_EACH_ITEM_COUNT

    @pytest.mark.asyncio
    async def test_a_pass_that_never_reaches_an_exit_does_not_repeat_the_previous_passes_exit(
        self, engine: Engine, flow: str
    ) -> None:
        # Branch.no -> skip_iteration, Branch.yes -> Step, whose exit is unwired. The first pass
        # skips; the others run off the end of the body, which adds their item as usual.
        _create_node(engine, flow, "ProbeForEachGroupNode", GROUP_NAME)
        _create_node(engine, flow, "BranchNode", BRANCH_NAME, group=GROUP_NAME, take="yes", take_first="no")
        _create_node(engine, flow, "StepNode", STEP_NAME, group=GROUP_NAME)
        _connect(engine, GROUP_NAME, "on_each", BRANCH_NAME, "exec_in")
        _connect(engine, BRANCH_NAME, "yes", STEP_NAME, "exec_in")
        _connect(engine, BRANCH_NAME, "no", GROUP_NAME, "skip_iteration")
        _connect(engine, STEP_NAME, "result", GROUP_NAME, "new_item_to_add")

        result = await _run(engine, flow)

        assert isinstance(result, StartFlowResultSuccess), result
        group = engine.node_manager.get_node_by_name(GROUP_NAME)
        assert len(group.parameter_output_values["results"]) == FOR_EACH_ITEM_COUNT - 1


async def _run(engine: Engine, flow: str) -> object:
    # Packaged copies of the body are what run, and they share their class with the canvas nodes,
    # so the class-level run records start empty only if cleared once the canvas is built.
    _runs_of(engine, BRANCH_NAME).clear()
    _runs_of(engine, STEP_NAME).clear()
    with engine.context_manager.flow(flow):
        return await engine.ahandle_request(StartFlowRequest(flow_name=flow))


def _runs_of(engine: Engine, node_name: str) -> list[str]:
    return type(engine.node_manager.get_node_by_name(node_name)).runs  # type: ignore[attr-defined]


def _create_node(
    engine: Engine, flow: str, node_type: str, name: str, group: str | None = None, **values: str | int
) -> None:
    with engine.context_manager.flow(flow):
        result = engine.handle_request(
            CreateNodeRequest(
                node_type=node_type, specific_library_name=LIBRARY_NAME, node_name=name, parent_group_name=group
            )
        )
    assert isinstance(result, CreateNodeResultSuccess), result
    for parameter_name, value in values.items():
        set_result = engine.handle_request(
            SetParameterValueRequest(node_name=name, parameter_name=parameter_name, value=value)
        )
        assert isinstance(set_result, SetParameterValueResultSuccess), set_result


def _connect(engine: Engine, source: str, source_parameter: str, target: str, target_parameter: str) -> None:
    result = engine.handle_request(
        CreateConnectionRequest(
            source_node_name=source,
            source_parameter_name=source_parameter,
            target_node_name=target,
            target_parameter_name=target_parameter,
        )
    )
    assert isinstance(result, CreateConnectionResultSuccess), f"{source}.{source_parameter} -> {target}: {result}"
