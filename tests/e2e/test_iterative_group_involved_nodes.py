"""Regression test for issue #5486: a running group must stay in the editor's involved nodes.

The editor keeps ONE involved-node set and replaces it wholesale on every `InvolvedNodesEvent`,
and it draws a group's "Running" pill only while the group is in that set. Each iteration of a
group body runs as its own isolated `ControlFlowMachine`, and `start_flow` used to announce that
transient flow's nodes unconditionally -- so iteration 1 replaced the set with packaged copies
(`Body_1`, `Start_Package_MultiNode`) that the canvas has never heard of, and the group fell out
of it for the rest of the run. The pill went dark in both execution modes; it was only *reported*
against "Run Group Items All at Once" because in sequential mode the real child node keeps
lighting up, so the group still reads as busy.

A second, closely related leak from the same isolated flows: they also announced their own
completion. `ControlFlowResolvedEvent` reads as "the run is over" to every listener -- the editor
clears its running-node sets on it -- so a per-iteration announcement extinguished the running
group from iteration 1 onward.

Asserted here, over both execution modes, by draining the same queue the websocket serializes
from: every involvement announcement names the group and the group's real child, none of them ever
names a packaged copy, and no flow announces itself as finished while the group is still running.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from griptape_nodes.retained_mode.events.base_events import ExecutionGriptapeNodeEvent
from griptape_nodes.retained_mode.events.connection_events import (
    CreateConnectionRequest,
    CreateConnectionResultSuccess,
)
from griptape_nodes.retained_mode.events.execution_events import (
    InvolvedNodesEvent,
    NodeResolvedEvent,
    StartFlowRequest,
    StartFlowResultSuccess,
)
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

# Timeout with thread dump.
pytestmark = pytest.mark.timeout(300, method="thread")

FIXTURE_LIBRARY_DIR = Path(__file__).parent / "fixtures" / "iterative_group_library"
FIXTURE_LIBRARY_JSON_TEMPLATE = FIXTURE_LIBRARY_DIR / "griptape_nodes_library.json"
FIXTURE_NODE_FILE = FIXTURE_LIBRARY_DIR / "iterative_group_nodes.py"
# `flow_manager._validate_and_get_multi_node_library_info` resolves the packaged flow's
# StartFlow/EndFlow endpoints from a library with exactly this name.
LIBRARY_NAME = "Griptape Nodes Library"

GROUP_NODE_NAME = "ForEachGroup"
CHILD_NODE_NAME = "Body"
# Names the packager generates. None of these exist on the canvas, so none may be announced.
PACKAGED_NAME_MARKERS = ("_Package", "Package_", f"{CHILD_NODE_NAME}_")


def _drain_execution_payloads(queue: asyncio.Queue) -> list[Any]:
    """Take every execution payload off the queue, in order.

    Drained once and shared by the checks below, because consuming the queue is destructive.
    """
    payloads = []
    while not queue.empty():
        event = queue.get_nowait()
        if not isinstance(event, ExecutionGriptapeNodeEvent):
            continue
        payloads.append(event.wrapped_event.payload)
    return payloads


def _involved_node_announcements(payloads: list[Any]) -> list[list[str]]:
    return [list(payload.involved_nodes) for payload in payloads if isinstance(payload, InvolvedNodesEvent)]


def _payload_names_before_group_resolved(payloads: list[Any], group_node_name: str) -> list[str]:
    """Payload type names emitted while the group was still running.

    The group's own `NodeResolvedEvent` is the moment it stops running, so anything before it
    happened mid-run from the editor's point of view.
    """
    names = []
    for payload in payloads:
        if isinstance(payload, NodeResolvedEvent) and payload.node_name == group_node_name:
            break
        names.append(type(payload).__name__)
    return names


@pytest.mark.parametrize("execution_mode", ["Run Group Items One at a Time", "Run Group Items All at Once"])
@pytest.mark.asyncio
async def test_running_group_stays_involved(
    tmp_path: Path, engine: Engine, materialize_library: Callable[..., Path], execution_mode: str
) -> None:
    """Every involved-node announcement during a group run names the group and its real child."""
    library_json = materialize_library(
        tmp_path / "library", template=FIXTURE_LIBRARY_JSON_TEMPLATE, node_file=FIXTURE_NODE_FILE, name=LIBRARY_NAME
    )
    register_result = engine.handle_request(RegisterLibraryFromFileRequest(file_path=str(library_json)))
    assert isinstance(register_result, RegisterLibraryFromFileResultSuccess), register_result

    engine.context_manager.push_workflow(workflow_name="involved_nodes_wf")

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

    # Wire the body the way a real graph is wired, so every iteration re-executes it. Without the
    # `on_each -> exec_in` / `exec_out -> loop_complete` pair the body resolves once and later
    # iterations never touch it, which is the case that hides this bug.
    for source_node, source_param, target_node, target_param in (
        (GROUP_NODE_NAME, "on_each", CHILD_NODE_NAME, "exec_in"),
        (CHILD_NODE_NAME, "exec_out", GROUP_NODE_NAME, "loop_complete"),
        (GROUP_NODE_NAME, "index", CHILD_NODE_NAME, "text"),
        (CHILD_NODE_NAME, "result", GROUP_NODE_NAME, "new_item_to_add"),
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
        SetParameterValueRequest(
            parameter_name="execution_mode", node_name=group_result.node_name, value=execution_mode
        )
    )

    # This is the queue the websocket drains, so whatever lands here is what the editor sees.
    queue: asyncio.Queue = asyncio.Queue()
    engine.event_manager.initialize_queue(queue)

    with engine.context_manager.flow(parent_flow):
        run_result = await engine.ahandle_request(
            StartFlowRequest(
                flow_name=parent_flow,
                flow_node_name=group_result.node_name,
                wait_for_completion=True,
                completion_timeout_ms=60000,
            )
        )
    assert isinstance(run_result, StartFlowResultSuccess), run_result

    payloads = _drain_execution_payloads(queue)

    # The editor treats ControlFlowResolvedEvent as "the run is over" and clears its running-node
    # sets on it, and the payload carries no flow identity to say which flow ended. Each iteration
    # of the group body is its own control flow, so an ungated announcement arrived once per
    # iteration and told the editor the run had finished while the group was still going.
    mid_run_payload_names = _payload_names_before_group_resolved(payloads, group_result.node_name)
    assert "ControlFlowResolvedEvent" not in mid_run_payload_names, (
        f"an iteration announced the whole run as finished while the group was still running "
        f"({execution_mode}): {mid_run_payload_names}"
    )

    announcements = _involved_node_announcements(payloads)
    assert announcements, "the run announced no involved nodes at all"

    # The final announcement is the engine telling the editor the run is over. Everything before it
    # describes a run in progress, and the group is running for all of it.
    assert announcements[-1] == [], f"run did not end by clearing involved nodes: {announcements[-1]}"
    for announcement in announcements[:-1]:
        assert group_result.node_name in announcement, (
            f"group dropped out of involved nodes mid-run ({execution_mode}): {announcement}"
        )
        assert child_result.node_name in announcement, (
            f"group child never announced as involved ({execution_mode}): {announcement}"
        )

    announced_names = {name for announcement in announcements for name in announcement}
    packaged_names = [name for name in announced_names if any(marker in name for marker in PACKAGED_NAME_MARKERS)]
    assert not packaged_names, (
        f"packaged copies announced to the editor, which has no such nodes ({execution_mode}): {packaged_names}"
    )
