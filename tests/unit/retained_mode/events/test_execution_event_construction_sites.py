"""Execution and progress events are built only where the workflow they belong to is known.

A run's events name their workflow so the editor can route them to the right tab. Building one
directly skips that, so every construction site outside the shared builder is listed here with why it
names no workflow, or how it names one. Calls are found by the event's class name, so a class
built reflectively, as event translation does with `type(event)(...)`, isn't seen.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import NamedTuple

import griptape_nodes

_SRC = Path(griptape_nodes.__file__).parent
_EVENT_TYPES = {"ExecutionEvent", "ProgressEvent"}

# (path under src/griptape_nodes, enclosing function) for each site allowed to build one directly,
# mapped to whether every event built there must pass `open_workflow_id`.
_ALLOWED_SITES = {
    # The one builder `WorkflowRuntime`, `WorkflowFlows` and `BaseNode.execution_event` share.
    ("retained_mode/events/base_events.py", "build_execution_event"): True,
    # Progress chunks name the node's workflow (`BaseNode._event_workflow_id`).
    ("exe_types/node_types.py", "append_value_to_parameter"): True,
    # A subprocess forwards a progress event to the engine that started it, keeping its id.
    ("bootstrap/workflow_executors/local_session_workflow_executor.py", "_arun"): True,
    # A chat about no workflow names none.
    ("retained_mode/managers/agent_manager.py", "emit"): False,
    # Publishing progress is about the engine, not a workflow.
    ("retained_mode/publishing/workflow_packager.py", "emit_progress"): False,
    # A subprocess run that failed may have failed before it had a workflow, so its cancel names none.
    ("bootstrap/workflow_executors/local_session_workflow_executor.py", "arun"): False,
}


class _Site(NamedTuple):
    path: str
    function: str
    passes_workflow_id: bool


class _SiteFinder(ast.NodeVisitor):
    """Records each call to an event type by the innermost function it is in."""

    def __init__(self, relative: str) -> None:
        self.relative = relative
        self.functions: list[str] = []
        self.sites: list[_Site] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def visit_Call(self, node: ast.Call) -> None:
        if _called_name(node.func) in _EVENT_TYPES:
            function = "<module>"
            if self.functions:
                function = self.functions[-1]
            passes_workflow_id = any(_names_a_workflow(keyword) for keyword in node.keywords)
            self.sites.append(_Site(self.relative, function, passes_workflow_id))
        self.generic_visit(node)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.functions.append(node.name)
        self.generic_visit(node)
        self.functions.pop()


def _names_a_workflow(keyword: ast.keyword) -> bool:
    """Whether `keyword` passes an `open_workflow_id` that isn't a literal None."""
    if keyword.arg != "open_workflow_id":
        return False
    return not (isinstance(keyword.value, ast.Constant) and keyword.value.value is None)


def _called_name(func: ast.expr) -> str | None:
    """`ExecutionEvent` for `ExecutionEvent(...)`, `base_events.ExecutionEvent(...)` and `ExecutionEvent[X](...)`."""
    if isinstance(func, ast.Subscript):
        func = func.value
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _construction_sites() -> list[_Site]:
    sites: list[_Site] = []
    for path in _SRC.rglob("*.py"):
        finder = _SiteFinder(path.relative_to(_SRC).as_posix())
        finder.visit(ast.parse(path.read_text(encoding="utf-8")))
        sites.extend(finder.sites)
    return sites


class TestExecutionEventConstructionSites:
    def test_events_are_built_only_by_the_builder_or_at_listed_sites(self) -> None:
        assert {(site.path, site.function) for site in _construction_sites()} - _ALLOWED_SITES.keys() == set()

    def test_every_listed_site_still_builds_one(self) -> None:
        assert _ALLOWED_SITES.keys() - {(site.path, site.function) for site in _construction_sites()} == set()

    def test_sites_that_name_a_workflow_pass_it_on_every_event(self) -> None:
        unstamped = [
            site
            for site in _construction_sites()
            if _ALLOWED_SITES.get((site.path, site.function)) and not site.passes_workflow_id
        ]

        assert unstamped == []
