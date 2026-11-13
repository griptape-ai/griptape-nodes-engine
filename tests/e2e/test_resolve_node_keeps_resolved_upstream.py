"""Playing a node reuses the resolved nodes upstream of it until something they read changes.

``Load Image -> Generate -> Compare``, with ``Load Image`` also wired straight into ``Compare``.
``Generate`` stands in for a paid generation node: every run is a new generation.

A parameter holding a ``{...}`` token, such as a project path ``{inputs}/cat.png`` on Load Image,
used to send its node back to run on every play. Its re-run then marked ``Generate`` stale, so
``Generate`` ran again on the next play with nothing changed. A node that reads a variable runs
again when that variable changes, and only then.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from griptape.artifacts import ImageUrlArtifact

from griptape_nodes.retained_mode.events.execution_events import ResolveNodeRequest, ResolveNodeResultSuccess
from griptape_nodes.retained_mode.events.flow_events import CreateFlowRequest, CreateFlowResultSuccess
from griptape_nodes.retained_mode.events.library_events import (
    RegisterLibraryFromFileRequest,
    RegisterLibraryFromFileResultSuccess,
)
from griptape_nodes.retained_mode.events.parameter_events import SetParameterValueRequest
from griptape_nodes.retained_mode.events.variable_events import (
    CreateVariableRequest,
    CreateVariableResultSuccess,
    SetVariableValueRequest,
    SetVariableValueResultSuccess,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from griptape_nodes.retained_mode.engine import Engine

pytestmark = pytest.mark.timeout(300, method="thread")

FIXTURE_LIBRARY_DIR = Path(__file__).parent / "fixtures" / "rerun_upstream_library"
LIBRARY_NAME = "Rerun Upstream Library"
PLAYS = 4


@pytest.fixture
def compare_flow(
    engine: Engine,
    materialize_library: Callable[..., Path],
    create_node: Callable[..., str],
    connect: Callable[..., None],
    tmp_path: Path,
) -> str:
    """Build Load Image -> Generate -> Compare, Load Image also into Compare, and return the flow name."""
    library_json = materialize_library(
        tmp_path / "lib",
        template=FIXTURE_LIBRARY_DIR / "griptape_nodes_library.json",
        node_file=FIXTURE_LIBRARY_DIR / "rerun_upstream_nodes.py",
    )
    register_result = engine.handle_request(RegisterLibraryFromFileRequest(file_path=str(library_json)))
    assert isinstance(register_result, RegisterLibraryFromFileResultSuccess), register_result

    engine.context_manager.push_workflow(workflow_name="keeps_resolved_upstream_wf")
    flow_result = engine.handle_request(
        CreateFlowRequest(parent_flow_name=None, flow_name="Flow", set_as_new_context=False)
    )
    assert isinstance(flow_result, CreateFlowResultSuccess), flow_result
    flow_name = flow_result.flow_name

    create_node("FakeLoadImage", "Load Image", flow_name, library_name=LIBRARY_NAME)
    create_node("FakeGenerate", "Generate", flow_name, library_name=LIBRARY_NAME)
    create_node("FakeCompareImages", "Compare Images", flow_name, library_name=LIBRARY_NAME)
    engine.handle_request(
        SetParameterValueRequest(
            parameter_name="image",
            node_name="Load Image",
            value=ImageUrlArtifact(value="{inputs}/cat.png", name="cat.png"),
        )
    )
    engine.handle_request(
        SetParameterValueRequest(parameter_name="path", node_name="Load Image", value="{inputs}/cat.png")
    )
    connect("Load Image", "image", "Generate", "image")
    connect("Load Image", "image", "Compare Images", "Image_1")
    connect("Generate", "image_url", "Compare Images", "Image_2")
    return flow_name


@pytest.fixture
def play(engine: Engine, compare_flow: str) -> Callable[[], Awaitable[None]]:
    """Press play on Compare Images and wait for the run to end."""

    async def _play() -> None:
        with engine.context_manager.flow(compare_flow):
            result = await engine.ahandle_request(ResolveNodeRequest(node_name="Compare Images"))
        assert isinstance(result, ResolveNodeResultSuccess), result
        assert not engine.flow_manager.check_for_existing_running_flow(), "The flow is still marked as running."

    return _play


def _run_counts(engine: Engine) -> tuple[int, int]:
    load = engine.node_manager.get_node_by_name("Load Image")
    generate = engine.node_manager.get_node_by_name("Generate")
    return load.parameter_output_values["run_count"], generate.parameter_output_values["run_count"]


@pytest.mark.asyncio
async def test_a_project_path_does_not_rerun_the_nodes_upstream_of_the_played_node(
    engine: Engine, play: Callable[[], Awaitable[None]]
) -> None:
    """Load Image holds `{inputs}/cat.png`; neither it nor Generate runs again on later plays."""
    counts = []
    for _ in range(PLAYS):
        await play()
        counts.append(_run_counts(engine))

    assert counts == [(1, 1)] * PLAYS, f"(Load Image, Generate) runs after each play: {counts}"


@pytest.mark.asyncio
async def test_a_node_reading_a_variable_runs_again_only_when_the_variable_changes(
    engine: Engine, compare_flow: str, play: Callable[[], Awaitable[None]]
) -> None:
    """Generate's prompt reads `{STYLE}`: it is reused while STYLE holds, and runs again once it changes."""
    create_result = engine.handle_request(
        CreateVariableRequest(name="STYLE", type="str", value="painterly", owning_flow=compare_flow)
    )
    assert isinstance(create_result, CreateVariableResultSuccess), create_result
    engine.handle_request(
        SetParameterValueRequest(parameter_name="prompt", node_name="Generate", value="make it {STYLE}")
    )

    await play()
    await play()
    assert _run_counts(engine) == (1, 1), "Generate ran again with nothing changed"

    set_result = engine.handle_request(
        SetVariableValueRequest(name="STYLE", value="watercolor", starting_flow=compare_flow)
    )
    assert isinstance(set_result, SetVariableValueResultSuccess), set_result
    await play()

    assert _run_counts(engine) == (1, 2), "Generate did not run again after STYLE changed"
