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
from typing import TYPE_CHECKING

import pytest

from griptape_nodes.exe_types.node_types import NodeResolutionState
from griptape_nodes.retained_mode.events.node_events import SetLockNodeStateRequest
from tests.e2e.variable_reader_scaffolding import (
    ReaderTab,
    load_project,
    open_gate,
    register_reader_library,
    switch_project,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from griptape_nodes.retained_mode.engine import Engine

pytestmark = pytest.mark.timeout(300, method="thread")

SECOND_RUN = 2


@pytest.fixture
def reader_library(engine: Engine, materialize_library: Callable[..., Path], tmp_path: Path) -> None:
    """Register the reader fixture library in this test's engine."""
    register_reader_library(engine, materialize_library, tmp_path)


# --- switching projects -------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("reader_library")
async def test_switching_projects_reruns_a_node_that_reads_a_project_variable(engine: Engine, tmp_path: Path) -> None:
    """Reader -> Consumer, Reader reading `{shot_code}`. After switching projects, the next play uses the new value.

    Project values are variables, but switching projects sends no variable request; only the
    comparison before the play notices that `{shot_code}` renders differently.
    """
    project_a = load_project(engine, tmp_path / "project_a", "sc042")
    project_b = load_project(engine, tmp_path / "project_b", "sc777")
    await switch_project(engine, project_a)
    try:
        tab = ReaderTab.open_new(engine, "switch")
        tab.add_node("VariableReader", "Reader", text="shot {shot_code}")
        tab.add_node("Consumer", "Consumer")
        tab.connect("Reader", "Consumer", "value")
        await tab.play("Consumer")
        assert tab.output("Consumer", "result") == "shot sc042"

        await switch_project(engine, project_b)
        await tab.play("Consumer")

        assert tab.output("Consumer", "result") == "shot sc777", (
            f"Consumer still shows {tab.output('Consumer', 'result')!r} after the switch; "
            f"Reader ran {tab.runs('Reader')} time(s) and reads {tab.state('Reader')}"
        )
    finally:
        await switch_project(engine, None)


@pytest.mark.asyncio
@pytest.mark.usefixtures("reader_library")
async def test_switching_projects_reaches_a_reader_two_nodes_above_the_played_node(
    engine: Engine, tmp_path: Path
) -> None:
    """Reader -> Middle -> Played. After switching projects the change reaches Played through Middle.

    Middle is RESOLVED and holds no template, so the build only reaches Reader if whatever decides
    Reader is stale also marks the nodes downstream of it.
    """
    project_a = load_project(engine, tmp_path / "project_a", "sc042")
    project_b = load_project(engine, tmp_path / "project_b", "sc777")
    await switch_project(engine, project_a)
    try:
        tab = ReaderTab.open_new(engine, "switch_chain")
        tab.add_node("VariableReader", "Reader", text="shot {shot_code}")
        tab.add_node("Consumer", "Middle")
        tab.add_node("Consumer", "Played")
        tab.connect("Reader", "Middle", "value")
        tab.connect("Middle", "Played", "value")
        await tab.play("Played")
        assert tab.output("Played", "result") == "shot sc042"

        await switch_project(engine, project_b)
        await tab.play("Played")

        assert tab.output("Played", "result") == "shot sc777", (
            f"Played still shows {tab.output('Played', 'result')!r}; "
            f"Reader ran {tab.runs('Reader')} time(s), Middle ran {tab.runs('Middle')}"
        )
    finally:
        await switch_project(engine, None)


# --- a project variable set in another tab ------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("reader_library")
async def test_setting_a_project_variable_in_one_tab_reruns_its_reader_in_another(
    engine: Engine, tmp_path: Path
) -> None:
    """Two open workflows share the project's `shot_code`; setting it in one reaches readers in both.

    Each tab has Reader -> Consumer. The set request names one workflow, and a project variable is
    shared by every workflow open in that project, so only the comparison reaches the other tab.
    """
    project = load_project(engine, tmp_path / "project", "sc042")
    await switch_project(engine, project)
    try:
        tabs = [ReaderTab.open_new(engine, "tab_a"), ReaderTab.open_new(engine, "tab_b")]
        for tab in tabs:
            tab.add_node("VariableReader", "Reader", text="shot {shot_code}")
            tab.add_node("Consumer", "Consumer")
            tab.connect("Reader", "Consumer", "value")
            await tab.play("Consumer")
        tab_a, tab_b = tabs

        tab_a.set_variable("shot_code", "sc777")
        for tab in tabs:
            await tab.play("Consumer")

        assert tab_a.output("Consumer", "result") == "shot sc777"
        assert tab_b.output("Consumer", "result") == "shot sc777", (
            f"The other tab's Consumer still shows {tab_b.output('Consumer', 'result')!r}; "
            f"its Reader ran {tab_b.runs('Reader')} time(s)"
        )
    finally:
        await switch_project(engine, None)


# --- the reader is running when the variable changes --------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("reader_library")
async def test_a_reader_running_when_its_variable_changes_runs_again_next_time(engine: Engine, tmp_path: Path) -> None:
    """Reader -> Consumer. SHOT changes while Reader is running; the next play uses the new value.

    Reader read the old value when it started and finishes RESOLVED. What it rendered to was
    recorded at that start, so the next play sees the difference and runs it again.
    """
    gate = tmp_path / "gates" / "reader.gate"
    tab = ReaderTab.open_new(engine, "midrun")
    tab.create_variable("SHOT", "sc001")
    tab.add_node("VariableReader", "Reader", text="shot {SHOT}")
    tab.add_node("Consumer", "Consumer")
    tab.connect("Reader", "Consumer", "value")
    tab.set_parameter("Reader", "gate_file", str(gate))

    run = asyncio.create_task(tab.play("Consumer"))
    await tab.wait_until_resolving("Reader")
    tab.set_variable("SHOT", "sc002")
    open_gate(gate)
    await run
    assert tab.output("Consumer", "result") == "shot sc001", "this play started before the change"

    # The gate file is still there, so the next run of Reader passes straight through it. Clearing
    # `gate_file` instead would be an edit to Reader, and an edit re-runs it on its own.
    await tab.play("Consumer")

    assert tab.output("Consumer", "result") == "shot sc002", (
        f"Consumer still shows {tab.output('Consumer', 'result')!r}; "
        f"Reader ran {tab.runs('Reader')} time(s) and reads {tab.state('Reader')}"
    )


# --- known gap: a stale node sits between a re-running upstream node and the played node --------


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
    tab = ReaderTab.open_new(engine, "between")
    tab.add_node("VariableReader", "Reader", text="v1")
    tab.add_node("Consumer", "Middle")
    tab.add_node("VariableReader", "Played")
    tab.connect("Reader", "Middle", "value")
    tab.connect("Middle", "Played", "text")
    await tab.play("Played")
    assert tab.runs("Middle") == 1

    tab.node("Reader").make_node_unresolved(current_states_to_trigger_change_event=None)
    await tab.play("Played")

    assert tab.runs("Middle") == SECOND_RUN, f"Middle ran {tab.runs('Middle')} time(s) and reads {tab.state('Middle')}"


# --- the requests that could reach the known gap all cascade ------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("reader_library")
async def test_unlocking_an_upstream_node_also_unresolves_its_consumers(engine: Engine) -> None:
    """Unlocking unresolves the node and everything downstream, so it cannot leave a stale consumer behind."""
    tab = ReaderTab.open_new(engine, "unlock")
    tab.add_node("VariableReader", "Reader", text="v1")
    tab.add_node("Consumer", "Middle")
    tab.connect("Reader", "Middle", "value")
    tab.send(SetLockNodeStateRequest(node_name="Reader", lock=True))
    await tab.play("Middle")
    assert tab.state("Middle") is NodeResolutionState.RESOLVED

    tab.send(SetLockNodeStateRequest(node_name="Reader", lock=False))

    assert tab.state("Reader") is NodeResolutionState.UNRESOLVED
    assert tab.state("Middle") is NodeResolutionState.UNRESOLVED
