"""Execution and progress events are built only where the workflow they belong to is known.

A run's events name their workflow so the editor can route them to the right tab. Building one
directly skips that, so every construction site outside the factories is listed here with why it
names no workflow, or how it names one.
"""

from __future__ import annotations

import ast
from pathlib import Path

import griptape_nodes

_SRC = Path(griptape_nodes.__file__).parent
_EVENT_TYPES = {"ExecutionEvent", "ProgressEvent"}

# (path under src/griptape_nodes, enclosing function) for each site allowed to build one directly.
_ALLOWED_SITES = {
    # The factories.
    ("retained_mode/runtime/workflow_runtime.py", "execution_event"),
    ("retained_mode/runtime/flows.py", "execution_event"),
    ("exe_types/node_types.py", "execution_event"),
    # Progress chunks name the node's workflow (`BaseNode._event_workflow_id`).
    ("exe_types/node_types.py", "append_value_to_parameter"),
    # A chat about no workflow names none.
    ("retained_mode/managers/agent_manager.py", "emit"),
    # Publishing progress is about the engine, not a workflow.
    ("retained_mode/publishing/workflow_packager.py", "emit_progress"),
    # A subprocess forwards its events to the engine that started it, keeping a progress event's id.
    ("bootstrap/workflow_executors/local_session_workflow_executor.py", "arun"),
    ("bootstrap/workflow_executors/local_session_workflow_executor.py", "_arun"),
}


class _SiteFinder(ast.NodeVisitor):
    """Records each call to an event type by the innermost function it is in."""

    def __init__(self, relative: str) -> None:
        self.relative = relative
        self.functions: list[str] = []
        self.sites: set[tuple[str, str]] = set()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def visit_Call(self, node: ast.Call) -> None:
        if getattr(node.func, "id", None) in _EVENT_TYPES:
            self.sites.add((self.relative, self.functions[-1] if self.functions else "<module>"))
        self.generic_visit(node)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.functions.append(node.name)
        self.generic_visit(node)
        self.functions.pop()


def _construction_sites() -> set[tuple[str, str]]:
    sites: set[tuple[str, str]] = set()
    for path in _SRC.rglob("*.py"):
        finder = _SiteFinder(path.relative_to(_SRC).as_posix())
        finder.visit(ast.parse(path.read_text(encoding="utf-8")))
        sites |= finder.sites
    return sites


class TestExecutionEventConstructionSites:
    def test_events_are_built_only_by_the_factories_or_at_listed_sites(self) -> None:
        assert _construction_sites() - _ALLOWED_SITES == set()

    def test_every_listed_site_still_builds_one(self) -> None:
        assert _ALLOWED_SITES - _construction_sites() == set()
