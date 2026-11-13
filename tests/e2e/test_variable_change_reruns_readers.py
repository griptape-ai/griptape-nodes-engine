"""A node that reads a variable runs again when what its `{...}` text renders to has changed.

Before a play or Run, each RESOLVED node compares what its templates render to now with what they
rendered to when it last ran, and on a difference unresolves itself and everything downstream.
That covers every way the rendered text can change, including ones no variable request
announces: switching projects, a project variable set from another open workflow, and a variable
written while the node was already running. Each test plays a node below the reader, because
pressing play on a node always runs that node itself.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from griptape_nodes.common.project_templates import DEFAULT_PROJECT_TEMPLATE
from griptape_nodes.exe_types.node_types import NodeResolutionState
from griptape_nodes.retained_mode.events.connection_events import (
    CreateConnectionRequest,
    CreateConnectionResultSuccess,
)
from griptape_nodes.retained_mode.events.execution_events import ResolveNodeRequest, ResolveNodeResultSuccess
from griptape_nodes.retained_mode.events.flow_events import CreateFlowRequest, CreateFlowResultSuccess
from griptape_nodes.retained_mode.events.library_events import (
    RegisterLibraryFromFileRequest,
    RegisterLibraryFromFileResultSuccess,
)
from griptape_nodes.retained_mode.events.node_events import (
    CreateNodeRequest,
    CreateNodeResultSuccess,
    SetLockNodeStateRequest,
)
from griptape_nodes.retained_mode.events.parameter_events import SetParameterValueRequest
from griptape_nodes.retained_mode.events.project_events import (
    LoadProjectTemplateRequest,
    LoadProjectTemplateResultSuccess,
    SetCurrentProjectRequest,
    SetCurrentProjectResultSuccess,
)
from griptape_nodes.retained_mode.events.variable_events import (
    CreateVariableRequest,
    CreateVariableResultSuccess,
    SetVariableValueRequest,
    SetVariableValueResultSuccess,
)
from griptape_nodes.retained_mode.managers.event_manager import ResultContext

if TYPE_CHECKING:
    from collections.abc import Callable

    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.events.base_events import RequestPayload, ResultPayload
    from griptape_nodes.retained_mode.runtime.workflow_runtime import WorkflowRuntime

pytestmark = pytest.mark.timeout(300, method="thread")

FIXTURE_LIBRARY_DIR = Path(__file__).parent / "fixtures" / "variable_reader_library"
LIBRARY_NAME = "Variable Reader Library"
FLOW = "canvas"
_POLL_ATTEMPTS = 500
_POLL_SECONDS = 0.01


# --- helpers -------------------------------------------------------------------------------------


@pytest.fixture
def reader_library(engine: Engine, materialize_library: Callable[..., Path], tmp_path: Path) -> None:
    """Register the reader fixture library in this test's engine."""
    library_json = materialize_library(
        tmp_path / "reader_lib",
        template=FIXTURE_LIBRARY_DIR / "griptape_nodes_library.json",
        node_file=FIXTURE_LIBRARY_DIR / "variable_reader_nodes.py",
    )
    result = engine.handle_request(RegisterLibraryFromFileRequest(file_path=str(library_json)))
    assert isinstance(result, RegisterLibraryFromFileResultSuccess), result


def _open_tab(engine: Engine, display_name: str) -> WorkflowRuntime:
    """Open a new unsaved workflow beside whatever is open, as an editor tab does, with one flow."""
    workflow = engine.open_workflow(engine.workflow_registry.register_unsaved(display_name, listed=False).workflow_id)
    result = _send(engine, workflow, CreateFlowRequest(parent_flow_name=None, flow_name=FLOW))
    assert isinstance(result, CreateFlowResultSuccess), result
    return workflow


def _send(engine: Engine, workflow: WorkflowRuntime, request: RequestPayload) -> ResultPayload:
    return engine.event_manager.handle_request(
        request, result_context=ResultContext(open_workflow_id=workflow.workflow_id)
    ).result


async def _send_async(engine: Engine, workflow: WorkflowRuntime, request: RequestPayload) -> ResultPayload:
    result = await engine.event_manager.ahandle_request(
        request, result_context=ResultContext(open_workflow_id=workflow.workflow_id)
    )
    return result.result


def _node(engine: Engine, workflow: WorkflowRuntime, node_type: str, name: str, *, text: str = "") -> None:
    result = _send(
        engine,
        workflow,
        CreateNodeRequest(
            node_type=node_type, specific_library_name=LIBRARY_NAME, node_name=name, override_parent_flow_name=FLOW
        ),
    )
    assert isinstance(result, CreateNodeResultSuccess), result
    if text:
        _set_parameter(engine, workflow, name, "text", text)


def _set_parameter(engine: Engine, workflow: WorkflowRuntime, node: str, parameter: str, value: str) -> None:
    result = _send(engine, workflow, SetParameterValueRequest(node_name=node, parameter_name=parameter, value=value))
    assert result.succeeded(), result


def _connect(engine: Engine, workflow: WorkflowRuntime, source: str, target: str, target_parameter: str) -> None:
    result = _send(
        engine,
        workflow,
        CreateConnectionRequest(
            source_node_name=source,
            source_parameter_name="result",
            target_node_name=target,
            target_parameter_name=target_parameter,
        ),
    )
    assert isinstance(result, CreateConnectionResultSuccess), result


async def _play(engine: Engine, workflow: WorkflowRuntime, node: str) -> None:
    result = await _send_async(engine, workflow, ResolveNodeRequest(node_name=node))
    assert isinstance(result, ResolveNodeResultSuccess), result


def _get(workflow: WorkflowRuntime, node: str) -> object:
    return workflow.objects.get(node)


def _output(workflow: WorkflowRuntime, node: str, parameter: str) -> object:
    return _get(workflow, node).parameter_output_values.get(parameter)  # type: ignore[attr-defined]


def _state(workflow: WorkflowRuntime, node: str) -> NodeResolutionState:
    return _get(workflow, node).state  # type: ignore[attr-defined]


def _load_project(engine: Engine, directory: Path, shot_code: str) -> str:
    """Write and load a project whose writable `shot_code` variable holds `shot_code`, and return its id."""
    directory.mkdir(parents=True, exist_ok=True)
    project_yml = directory / "project_template.yml"
    project_yml.write_text(
        DEFAULT_PROJECT_TEMPLATE.to_overlay_yaml(DEFAULT_PROJECT_TEMPLATE)
        + f"\nvariables:\n  shot_code:\n    value: {shot_code}\n"
    )
    result = engine.handle_request(LoadProjectTemplateRequest(project_path=project_yml))
    assert isinstance(result, LoadProjectTemplateResultSuccess), result
    return result.project_id


async def _switch_project(engine: Engine, project_id: str | None) -> None:
    result = await engine.ahandle_request(SetCurrentProjectRequest(project_id=project_id))
    assert isinstance(result, SetCurrentProjectResultSuccess), result


async def _wait_until_resolving(workflow: WorkflowRuntime, node: str) -> None:
    for _ in range(_POLL_ATTEMPTS):
        if _state(workflow, node) is NodeResolutionState.RESOLVING:
            return
        await asyncio.sleep(_POLL_SECONDS)
    pytest.fail(f"Node '{node}' never started running.")


# --- switching projects -------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("reader_library")
async def test_switching_projects_reruns_a_node_that_reads_a_project_variable(engine: Engine, tmp_path: Path) -> None:
    """Reader -> Consumer, Reader reading `{shot_code}`. After switching projects, the next play should use the new value.

    Project values are variables, but switching projects sends no variable request; only the
    comparison before the play notices that `{shot_code}` renders differently.
    """
    project_a = _load_project(engine, tmp_path / "project_a", "sc042")
    project_b = _load_project(engine, tmp_path / "project_b", "sc777")
    await _switch_project(engine, project_a)
    try:
        tab = _open_tab(engine, "switch")
        _node(engine, tab, "VariableReader", "Reader", text="shot {shot_code}")
        _node(engine, tab, "Consumer", "Consumer")
        _connect(engine, tab, "Reader", "Consumer", "value")
        await _play(engine, tab, "Consumer")
        assert _output(tab, "Consumer", "result") == "shot sc042"

        await _switch_project(engine, project_b)
        await _play(engine, tab, "Consumer")

        assert _output(tab, "Consumer", "result") == "shot sc777", (
            f"Consumer still shows {_output(tab, 'Consumer', 'result')!r} after the switch; "
            f"Reader ran {_output(tab, 'Reader', 'run_count')} time(s) and reads {_state(tab, 'Reader')}"
        )
    finally:
        await _switch_project(engine, None)


# --- a project variable set in another tab ------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("reader_library")
async def test_setting_a_project_variable_in_one_tab_reruns_its_reader_in_another(
    engine: Engine, tmp_path: Path
) -> None:
    """Two open workflows share the project's `shot_code`; setting it in one should reach readers in both.

    Each tab has Reader -> Consumer. The set request names one workflow, and a project variable is
    shared by every workflow open in that project, so only the comparison reaches the other tab.
    """
    project = _load_project(engine, tmp_path / "project", "sc042")
    await _switch_project(engine, project)
    try:
        tabs = [_open_tab(engine, "tab_a"), _open_tab(engine, "tab_b")]
        for tab in tabs:
            _node(engine, tab, "VariableReader", "Reader", text="shot {shot_code}")
            _node(engine, tab, "Consumer", "Consumer")
            _connect(engine, tab, "Reader", "Consumer", "value")
            await _play(engine, tab, "Consumer")
        tab_a, tab_b = tabs

        result = _send(engine, tab_a, SetVariableValueRequest(name="shot_code", value="sc777", starting_flow=FLOW))
        assert isinstance(result, SetVariableValueResultSuccess), result
        for tab in tabs:
            await _play(engine, tab, "Consumer")

        assert _output(tab_a, "Consumer", "result") == "shot sc777"
        assert _output(tab_b, "Consumer", "result") == "shot sc777", (
            f"The other tab's Consumer still shows {_output(tab_b, 'Consumer', 'result')!r}; "
            f"its Reader ran {_output(tab_b, 'Reader', 'run_count')} time(s)"
        )
    finally:
        await _switch_project(engine, None)


# --- the reader is running when the variable changes --------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("reader_library")
async def test_a_reader_running_when_its_variable_changes_runs_again_next_time(engine: Engine, tmp_path: Path) -> None:
    """Reader -> Consumer. SHOT changes while Reader is running; the next play should use the new value.

    Reader read the old value when it started and finishes RESOLVED. What it rendered to was
    recorded at that start, so the next play sees the difference and runs it again.
    """
    gate = tmp_path / "gates" / "reader.gate"
    tab = _open_tab(engine, "midrun")
    created = _send(engine, tab, CreateVariableRequest(name="SHOT", type="str", value="sc001", owning_flow=FLOW))
    assert isinstance(created, CreateVariableResultSuccess), created
    _node(engine, tab, "VariableReader", "Reader", text="shot {SHOT}")
    _node(engine, tab, "Consumer", "Consumer")
    _connect(engine, tab, "Reader", "Consumer", "value")
    _set_parameter(engine, tab, "Reader", "gate_file", str(gate))

    run = asyncio.create_task(_play(engine, tab, "Consumer"))
    await _wait_until_resolving(tab, "Reader")
    result = _send(engine, tab, SetVariableValueRequest(name="SHOT", value="sc002", starting_flow=FLOW))
    assert isinstance(result, SetVariableValueResultSuccess), result
    gate.parent.mkdir(parents=True, exist_ok=True)
    gate.write_text("open")
    await run
    assert _output(tab, "Consumer", "result") == "shot sc001", "this play started before the change"

    # The gate file is still there, so the next run of Reader passes straight through it. Clearing
    # `gate_file` instead would be an edit to Reader, and an edit re-runs it on its own.
    await _play(engine, tab, "Consumer")

    assert _output(tab, "Consumer", "result") == "shot sc002", (
        f"Consumer still shows {_output(tab, 'Consumer', 'result')!r}; "
        f"Reader ran {_output(tab, 'Reader', 'run_count')} time(s) and reads {_state(tab, 'Reader')}"
    )


# --- known gap: a stale node sits between a re-running upstream node and the played node -------------


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Pre-existing since #5861, fails on main too: the single-node build trusts RESOLVED Middle, "
        "and Middle is only marked stale when Reader starts, so Played reads its old output and "
        "Middle runs one play late."
    ),
)
@pytest.mark.asyncio
@pytest.mark.usefixtures("reader_library")
async def test_a_consumer_between_a_stale_upstream_and_the_played_node_runs_in_the_same_play(
    engine: Engine,
) -> None:
    """Reader -> Middle -> Played, and Reader -> Played. Reader alone is stale; Middle should run this play.

    A single-node play trusts every RESOLVED node it meets while building, and Middle is only marked
    stale once Reader starts. Played reads Middle's old output and Middle runs on the next play.
    No request reaches this state: editing Reader, unlocking it, and a change in what its text
    renders to all unresolve Middle too. It is set directly here to show the mechanism.
    """
    tab = _open_tab(engine, "between")
    _node(engine, tab, "VariableReader", "Reader", text="v1")
    _node(engine, tab, "Consumer", "Middle")
    _node(engine, tab, "VariableReader", "Played")
    _connect(engine, tab, "Reader", "Middle", "value")
    _connect(engine, tab, "Middle", "Played", "text")
    await _play(engine, tab, "Played")
    assert _output(tab, "Middle", "run_count") == 1

    _get(tab, "Reader").make_node_unresolved(current_states_to_trigger_change_event=None)  # type: ignore[attr-defined]
    await _play(engine, tab, "Played")

    assert _output(tab, "Middle", "run_count") == 2, (  # noqa: PLR2004
        f"Middle ran {_output(tab, 'Middle', 'run_count')} time(s) and reads {_state(tab, 'Middle')}"
    )


# --- the requests that could reach the known gap all cascade ----------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("reader_library")
async def test_unlocking_an_upstream_node_also_unresolves_its_consumers(engine: Engine) -> None:
    """Unlocking unresolves the node and everything downstream, so it cannot leave a stale consumer behind."""
    tab = _open_tab(engine, "unlock")
    _node(engine, tab, "VariableReader", "Reader", text="v1")
    _node(engine, tab, "Consumer", "Middle")
    _connect(engine, tab, "Reader", "Middle", "value")
    _send(engine, tab, SetLockNodeStateRequest(node_name="Reader", lock=True))
    await _play(engine, tab, "Middle")
    assert _state(tab, "Middle") is NodeResolutionState.RESOLVED

    _send(engine, tab, SetLockNodeStateRequest(node_name="Reader", lock=False))

    assert _state(tab, "Reader") is NodeResolutionState.UNRESOLVED
    assert _state(tab, "Middle") is NodeResolutionState.UNRESOLVED


@pytest.mark.asyncio
@pytest.mark.usefixtures("reader_library")
async def test_switching_projects_reaches_a_reader_two_nodes_above_the_played_node(
    engine: Engine, tmp_path: Path
) -> None:
    """Reader -> Middle -> Played. After switching projects the change must reach Played through Middle.

    Middle is RESOLVED and holds no template, so the build only reaches Reader if whatever decides
    Reader is stale also marks the nodes downstream of it.
    """
    project_a = _load_project(engine, tmp_path / "project_a", "sc042")
    project_b = _load_project(engine, tmp_path / "project_b", "sc777")
    await _switch_project(engine, project_a)
    try:
        tab = _open_tab(engine, "switch_chain")
        _node(engine, tab, "VariableReader", "Reader", text="shot {shot_code}")
        _node(engine, tab, "Consumer", "Middle")
        _node(engine, tab, "Consumer", "Played")
        _connect(engine, tab, "Reader", "Middle", "value")
        _connect(engine, tab, "Middle", "Played", "value")
        await _play(engine, tab, "Played")
        assert _output(tab, "Played", "result") == "shot sc042"

        await _switch_project(engine, project_b)
        await _play(engine, tab, "Played")

        assert _output(tab, "Played", "result") == "shot sc777", (
            f"Played still shows {_output(tab, 'Played', 'result')!r}; Reader ran "
            f"{_output(tab, 'Reader', 'run_count')} time(s), Middle ran {_output(tab, 'Middle', 'run_count')}"
        )
    finally:
        await _switch_project(engine, None)
