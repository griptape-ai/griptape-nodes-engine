"""A node's declared pre-run validators fail it at its turn, reported with its own pre-run checks."""

from __future__ import annotations

import asyncio
import subprocess
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from griptape_nodes.common.directed_graph import DirectedGraph
from griptape_nodes.exe_types.node_error import NodeError
from griptape_nodes.exe_types.param_types.parameter_image import ParameterImage
from griptape_nodes.exe_types.param_types.parameter_video import ParameterVideo
from griptape_nodes.exe_types.validators.media import Duration, SideRange
from griptape_nodes.machines.dag_builder import DagBuilder, DagNode, NodeState
from griptape_nodes.machines.fsm import WorkflowState
from griptape_nodes.machines.parallel_resolution import ExecuteDagState, ParallelResolutionMachine
from griptape_nodes.retained_mode.events.execution_events import NodeErrorEvent
from griptape_nodes.utils import media_probe
from tests.unit.exe_types.mocks import MockNode
from tests.unit.exe_types.validators.media_files import image_file

if TYPE_CHECKING:
    from pathlib import Path


class _Checked(MockNode):
    def __init__(self, own_errors: list[Exception] | None) -> None:
        super().__init__(name="checked")
        self._own_errors = own_errors
        self.add_parameter(ParameterImage(name="image", pre_run_validators=[SideRange(min=300)]))

    def validate_before_node_run(self) -> list[Exception] | None:
        return self._own_errors


def _machine_for(node: MockNode) -> tuple[ParallelResolutionMachine, AsyncMock]:
    engine = MagicMock()
    put_event = AsyncMock()
    engine.event_manager.aput_event = put_event
    dag_builder = DagBuilder(engine)
    graph = DirectedGraph()
    graph.add_node(node.name)
    dag_builder.graphs[node.name] = graph
    dag_builder.graph_to_nodes[node.name] = {node.name}
    dag_builder.node_to_reference[node.name] = DagNode(node_reference=node, node_state=NodeState.QUEUED)
    machine = ParallelResolutionMachine("flow", max_nodes_in_parallel=1, dag_builder=dag_builder, engine=engine)
    machine.context.node_priority_queue.add_node(dag_builder.node_to_reference[node.name])
    return machine, put_event


def _node_error_events(put_event: AsyncMock) -> list[NodeErrorEvent]:
    events = []
    for call in put_event.await_args_list:
        payload = call.args[0].wrapped_event.payload
        if isinstance(payload, NodeErrorEvent):
            events.append(payload)
    return events


@pytest.fixture(autouse=True)
def no_upstream_values(monkeypatch: pytest.MonkeyPatch) -> None:
    """The node has no connections; skip gathering values for it."""
    monkeypatch.setattr(ExecuteDagState, "collect_values_from_upstream_nodes", AsyncMock())


@pytest.mark.asyncio
async def test_reports_validator_and_own_errors_as_one_failure(tmp_path: Path) -> None:
    """Validator errors come first, then the node's own, in one NodeErrorEvent."""
    own = NodeError("The prompt is empty.", links=None)
    node = _Checked(own_errors=[own])
    node.set_parameter_value("image", str(image_file(tmp_path, 200, 200)))
    machine, put_event = _machine_for(node)

    await asyncio.wait_for(machine.resolve_node(), timeout=5)

    assert machine.context.workflow_state == WorkflowState.ERRORED
    (event,) = _node_error_events(put_event)
    assert event.node_name == "checked"
    assert event.error is not None
    messages = event.error_message
    assert "each side must be at least 300 pixels" in messages
    assert "The prompt is empty." in messages
    assert messages.index("300 pixels") < messages.index("The prompt is empty.")


@pytest.mark.asyncio
async def test_a_validator_error_alone_fails_the_node(tmp_path: Path) -> None:
    """A node whose own checks pass still fails on a declared limit, worded as before."""
    node = _Checked(own_errors=None)
    node.set_parameter_value("image", str(image_file(tmp_path, 200, 200)))
    machine, put_event = _machine_for(node)

    await asyncio.wait_for(machine.resolve_node(), timeout=5)

    assert machine.context.workflow_state == WorkflowState.ERRORED
    assert machine.get_error_message() == (
        "Node 'checked' encountered problems: "
        """[NodeError("The image in 'image' is 200x200 and each side must be at least 300 pixels. Use a larger one.")]"""
    )
    assert len(_node_error_events(put_event)) == 1


class _VideoNode(MockNode):
    def __init__(self) -> None:
        super().__init__(name="edit")
        self.add_parameter(ParameterVideo(name="video", display_name="Video", pre_run_validators=[Duration(max=5)]))


@pytest.fixture
def slow_ffprobe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every ffprobe run time out."""

    def run(cmd: list[str], **kwargs: Any) -> None:
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

    monkeypatch.setattr(media_probe, "_installed_ffprobe", lambda: "/usr/bin/ffprobe")
    monkeypatch.setattr(media_probe.subprocess, "run", run)


@pytest.mark.asyncio
@pytest.mark.usefixtures("slow_ffprobe")
async def test_runs_the_node_when_an_input_could_not_be_read_in_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A video that took too long to read is skipped, like any input that can't be read here."""
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"video")
    node = _VideoNode()
    node.set_parameter_value("video", str(clip))
    machine, _put_event = _machine_for(node)
    execute = AsyncMock()
    monkeypatch.setattr(ExecuteDagState, "execute_node", execute)

    await asyncio.wait_for(machine.resolve_node(), timeout=5)

    # It got past its checks and was started.
    execute.assert_awaited_once()
