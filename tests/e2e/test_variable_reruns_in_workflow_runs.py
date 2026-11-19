"""Whole-workflow Runs reuse a node whose `{...}` text reads the same, and re-run it once it reads differently.

The other tests in this area press play on one node. These press **Run**, which seeds the DAG from
the workflow's control chains and validates every node in the flow first. Each graph feeds a data
node holding a template into a control chain, because control nodes run again on every Run anyway
(#6248) and would not show whether the template check reused anything.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from griptape_nodes.exe_types.base_iterative_nodes import BaseIterativeStartNode
from griptape_nodes.retained_mode.events.library_events import (
    RegisterLibraryFromFileRequest,
    RegisterLibraryFromFileResultSuccess,
)
from griptape_nodes.retained_mode.events.workflow_events import (
    SetVariableSubstitutionEnabledRequest,
    SetVariableSubstitutionEnabledResultSuccess,
)
from tests.e2e.variable_reader_scaffolding import (
    ReaderTab,
    load_project,
    register_reader_library,
    switch_project,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from griptape_nodes.retained_mode.engine import Engine

pytestmark = pytest.mark.timeout(300, method="thread")

LOOP_LIBRARY_DIR = Path(__file__).parent / "fixtures" / "loop_library"
# The loop packager asks for its flow endpoints from this library by name.
LOOP_LIBRARY_NAME = "Griptape Nodes Library"
SECOND_RUN = 2
THIRD_RUN = 3


@pytest.fixture
def reader_library(engine: Engine, materialize_library: Callable[..., Path], tmp_path: Path) -> None:
    """Register the reader fixture library in this test's engine."""
    register_reader_library(engine, materialize_library, tmp_path)


def _reader_into_chain(tab: ReaderTab, text: str) -> None:
    """Reader (data, holding `text`) -> Sink (control), so Run reaches Reader as Sink's data dependency."""
    tab.add_node("VariableReader", "Reader", text=text)
    tab.add_node("ChainSink", "Sink")
    tab.connect("Reader", "Sink", "value")


# --- nothing changed ----------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("reader_library")
@pytest.mark.parametrize(
    ("text", "expected_prefix"),
    [
        pytest.param("shot {SHOT}", "shot sc001", id="workflow-variable"),
        pytest.param("{workspace_dir}/cat.png", None, id="project-macro-path"),
    ],
)
async def test_running_again_reuses_a_reader_whose_text_reads_the_same(
    engine: Engine, text: str, expected_prefix: str | None
) -> None:
    """Three Runs with nothing changed: the control node runs each time, the templated reader once."""
    tab = ReaderTab.open_new(engine, "unchanged")
    tab.create_variable("SHOT", "sc001")
    _reader_into_chain(tab, text)

    for _ in range(THIRD_RUN):
        await tab.run()

    assert tab.runs("Sink") == THIRD_RUN
    assert tab.runs("Reader") == 1, f"Reader ran {tab.runs('Reader')} times with nothing changed"
    if expected_prefix is not None:
        assert tab.output("Sink", "result") == expected_prefix


# --- a variable changes between Runs ------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("reader_library")
async def test_running_after_a_variable_changes_reruns_the_reader_and_everything_below_it(engine: Engine) -> None:
    """Reader -> Middle (data) -> Sink. Setting SHOT between Runs re-runs Reader and Middle once each."""
    tab = ReaderTab.open_new(engine, "changed")
    tab.create_variable("SHOT", "sc001")
    tab.add_node("VariableReader", "Reader", text="shot {SHOT}")
    tab.add_node("Consumer", "Middle")
    tab.add_node("ChainSink", "Sink")
    tab.connect("Reader", "Middle", "value")
    tab.connect("Middle", "Sink", "value")
    await tab.run()

    tab.set_variable("SHOT", "sc002")
    await tab.run()
    await tab.run()

    assert tab.output("Sink", "result") == "shot sc002"
    assert (tab.runs("Reader"), tab.runs("Middle")) == (SECOND_RUN, SECOND_RUN)


# --- a Set Variable node writes during the Run --------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("reader_library")
async def test_a_value_a_set_variable_node_writes_after_its_reader_ran_is_used_by_the_next_run(engine: Engine) -> None:
    """Reader -> Sink by data, then Sink -> Set SHOT on the control chain. SHOT starts at sc001 and is set to sc002.

    Reader runs before Set SHOT writes, so the first Run shows sc001. The write lands while the run is
    still going, and the variable request does not unresolve readers mid-run, so only the comparison
    before the second Run can tell Reader that its text now reads differently.
    """
    tab = ReaderTab.open_new(engine, "set_during_run")
    tab.create_variable("SHOT", "sc001")
    _reader_into_chain(tab, "shot {SHOT}")
    tab.add_node("SetVariableNode", "Set SHOT")
    tab.set_parameter("Set SHOT", "variable_name", "SHOT")
    tab.set_parameter("Set SHOT", "value", "sc002")
    tab.connect("Sink", "Set SHOT", "exec_in", source_parameter="exec_out")

    await tab.run()
    assert tab.output("Sink", "result") == "shot sc001", "Reader is upstream of the write in the first Run"
    await tab.run()

    assert tab.output("Sink", "result") == "shot sc002", (
        f"Sink shows {tab.output('Sink', 'result')!r} on the second Run; Reader ran {tab.runs('Reader')} time(s)"
    )


# --- the project changes between Runs -----------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("reader_library")
async def test_switching_projects_between_runs_reruns_the_reader(engine: Engine, tmp_path: Path) -> None:
    """Reader reads the project variable `{shot_code}`; switching projects re-runs it on the next Run."""
    project_a = load_project(engine, tmp_path / "project_a", "sc042")
    project_b = load_project(engine, tmp_path / "project_b", "sc777")
    await switch_project(engine, project_a)
    try:
        tab = ReaderTab.open_new(engine, "switch_between_runs")
        _reader_into_chain(tab, "shot {shot_code}")
        await tab.run()
        assert tab.output("Sink", "result") == "shot sc042"

        await switch_project(engine, project_b)
        await tab.run()

        assert tab.output("Sink", "result") == "shot sc777"
        assert tab.runs("Reader") == SECOND_RUN
    finally:
        await switch_project(engine, None)


# --- substitution is switched off and on again --------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("reader_library")
async def test_switching_substitution_off_and_on_reruns_the_reader_each_time(engine: Engine) -> None:
    """With substitution off the text renders as written; switching it back renders the variable again."""
    tab = ReaderTab.open_new(engine, "toggle")
    tab.create_variable("SHOT", "sc001")
    _reader_into_chain(tab, "shot {SHOT}")
    await tab.run()
    assert tab.output("Sink", "result") == "shot sc001"

    off = tab.send(SetVariableSubstitutionEnabledRequest(enabled=False))
    assert isinstance(off, SetVariableSubstitutionEnabledResultSuccess), off
    await tab.run()
    assert tab.output("Sink", "result") == "shot {SHOT}"

    on = tab.send(SetVariableSubstitutionEnabledRequest(enabled=True))
    assert isinstance(on, SetVariableSubstitutionEnabledResultSuccess), on
    await tab.run()

    assert tab.output("Sink", "result") == "shot sc001"
    assert tab.runs("Reader") == THIRD_RUN


# --- the workflow is saved and opened again -----------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("reader_library")
async def test_a_reopened_workflow_runs_its_reader_once_and_then_reuses_it(engine: Engine) -> None:
    """A reopened workflow's nodes have not run this session, so the first Run runs Reader; the next reuses it.

    Reader comes back RESOLVED from the file with nothing recorded about what its text rendered to,
    so the first Run runs it. `Declares SHOT` stands in for the Create Variable or Get Variable node
    a real workflow has: a save keeps only the variables some node declares.
    """
    tab = ReaderTab.open_new(engine, "reopen")
    tab.create_variable("SHOT", "sc001")
    tab.add_node("DeclaresVariable", "Declares SHOT")
    tab.set_parameter("Declares SHOT", "variable_name", "SHOT")
    _reader_into_chain(tab, "shot {SHOT}")
    await tab.run()
    workflow_name = tab.save("variable_reader_reopen")
    tab.close()

    reopened = await ReaderTab.open_saved(engine, workflow_name)
    await reopened.run()
    await reopened.run()

    assert reopened.output("Sink", "result") == "shot sc001"
    assert reopened.runs("Reader") == 1, f"Reader ran {reopened.runs('Reader')} times after reopening"


# --- a loop reads a templated node from outside its body ----------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("reader_library")
async def test_a_loop_reading_a_templated_node_from_outside_its_body_follows_a_project_switch(
    engine: Engine, materialize_library: Callable[..., Path], tmp_path: Path
) -> None:
    """Loop Start -> Body A -> Body B -> Loop End -> After Loop, with Prefix outside the loop reading `{shot_code}`.

    Body A joins Prefix's text with each item. Switching projects sends no variable request, so the
    loop's results follow the new project only if the comparison before the Run re-runs Prefix.
    """
    loop_library = materialize_library(
        tmp_path / "loop_library",
        template=LOOP_LIBRARY_DIR / "griptape_nodes_library.json",
        node_file=LOOP_LIBRARY_DIR / "loop_nodes.py",
        name=LOOP_LIBRARY_NAME,
    )
    registered = engine.handle_request(RegisterLibraryFromFileRequest(file_path=str(loop_library)))
    assert isinstance(registered, RegisterLibraryFromFileResultSuccess), registered
    project_a = load_project(engine, tmp_path / "project_a", "sc042")
    project_b = load_project(engine, tmp_path / "project_b", "sc777")
    await switch_project(engine, project_a)
    try:
        tab = ReaderTab.open_new(engine, "loop")
        tab.add_node("LoopStartNode", "Loop Start", library=LOOP_LIBRARY_NAME)
        tab.add_node("VariableReader", "Prefix", text="{shot_code}-")
        tab.add_node("LoopJoinNode", "Body A", library=LOOP_LIBRARY_NAME)
        tab.add_node("LoopBodyNode", "Body B", library=LOOP_LIBRARY_NAME)
        tab.add_node("LoopResultsNode", "After Loop", library=LOOP_LIBRARY_NAME)
        start = tab.node("Loop Start")
        assert isinstance(start, BaseIterativeStartNode), start
        assert start.end_node is not None
        end = start.end_node.name

        tab.connect("Loop Start", "Body A", "exec_in", source_parameter="exec_out")
        tab.connect("Body A", "Body B", "exec_in", source_parameter="exec_out")
        tab.connect("Body B", end, "add_item", source_parameter="exec_out")
        tab.connect("Prefix", "Body A", "prefix")
        tab.connect("Loop Start", "Body A", "item", source_parameter="index")
        tab.connect("Body A", "Body B", "text")
        tab.connect("Body B", end, "new_item_to_add")
        tab.connect(end, "After Loop", "exec_in", source_parameter="exec_out")
        tab.connect(end, "After Loop", "items", source_parameter="results")

        await tab.run()
        await tab.run()
        assert tab.output("After Loop", "result") == ["sc042-0", "sc042-1", "sc042-2"]

        await switch_project(engine, project_b)
        await tab.run()

        assert tab.output("After Loop", "result") == ["sc777-0", "sc777-1", "sc777-2"]
    finally:
        await switch_project(engine, None)
