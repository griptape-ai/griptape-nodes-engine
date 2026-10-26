"""Engine code builds execution and progress events only where the workflow they name is known.

`WorkflowRuntime.execution_event` and `BaseNode.execution_event` stamp the workflow when an event is
built. Anything else that builds one is listed here with the reason it may: a host event names no
workflow, and a copy carries the id of the event it copies. A new construction site fails this test
until it uses a factory or is listed, so nothing fills the id from ambient state or tags it later.
"""

from __future__ import annotations

import ast
import functools
from enum import StrEnum
from pathlib import Path
from typing import NamedTuple

import griptape_nodes

_SOURCE_ROOT = Path(griptape_nodes.__file__).parent
_EVENT_TYPES = frozenset({"ExecutionEvent", "ExecutionGriptapeNodeEvent", "ProgressEvent"})


class _SiteKind(StrEnum):
    FACTORY = "factory"  # Stamps the workflow it holds.
    HOST = "host"  # Holds no open workflow, so names none.
    COPY = "copy"  # Rebuilds an event and carries over its id.


class _Site(NamedTuple):
    path: str
    function: str


_ALLOWED_SITES: dict[_Site, _SiteKind] = {
    _Site("retained_mode/runtime/workflow_runtime.py", "WorkflowRuntime.execution_event"): _SiteKind.FACTORY,
    _Site("exe_types/node_types.py", "BaseNode.execution_event"): _SiteKind.FACTORY,
    # A progress chunk is not an execution payload; it names the node's workflow the same way.
    _Site("exe_types/node_types.py", "BaseNode.append_value_to_parameter"): _SiteKind.FACTORY,
    # An agent run has no workflow yet.
    _Site("retained_mode/managers/agent_manager.py", "AgentManager._run_turn.emit"): _SiteKind.HOST,
    # Publishing packages a saved workflow; no open workflow is involved.
    _Site("retained_mode/publishing/workflow_packager.py", "WorkflowPackager.emit_progress"): _SiteKind.HOST,
    # The executor fails before or outside any workflow it opens.
    _Site(
        "bootstrap/workflow_executors/local_session_workflow_executor.py", "LocalSessionWorkflowExecutor.arun"
    ): _SiteKind.HOST,
    # Turns a ProgressEvent into the execution event the session receives.
    _Site(
        "bootstrap/workflow_executors/local_session_workflow_executor.py", "LocalSessionWorkflowExecutor._arun"
    ): _SiteKind.COPY,
}


class _Construction(NamedTuple):
    site: _Site
    call: ast.Call


class _ConstructionFinder(ast.NodeVisitor):
    def __init__(self, path: str) -> None:
        self.path = path
        self.scope: list[str] = []
        self.found: list[_Construction] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._visit_scope(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_scope(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_scope(node)

    def visit_Call(self, node: ast.Call) -> None:
        if _called_name(node) in _EVENT_TYPES:
            self.found.append(_Construction(_Site(self.path, ".".join(self.scope)), node))
        self.generic_visit(node)

    def _visit_scope(self, node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()


def _called_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _workflow_id_argument(call: ast.Call) -> ast.expr | None:
    for keyword in call.keywords:
        if keyword.arg == "workflow_id":
            return keyword.value
    return None


@functools.cache
def _constructions() -> tuple[_Construction, ...]:
    found: list[_Construction] = []
    for source in sorted(_SOURCE_ROOT.rglob("*.py")):
        relative = source.relative_to(_SOURCE_ROOT).as_posix()
        finder = _ConstructionFinder(relative)
        finder.visit(ast.parse(source.read_text(encoding="utf-8"), filename=str(source)))
        found.extend(finder.found)
    return tuple(found)


class TestExecutionEventConstructionSites:
    def test_execution_events_are_built_only_at_listed_sites(self) -> None:
        unlisted = sorted({construction.site for construction in _constructions()} - set(_ALLOWED_SITES))

        assert unlisted == [], (
            "Build execution events with `workflow.execution_event(...)` or `node.execution_event(...)`, "
            f"or list the site here with the reason it holds no workflow: {unlisted}"
        )

    def test_every_listed_site_still_builds_one(self) -> None:
        stale = sorted(set(_ALLOWED_SITES) - {construction.site for construction in _constructions()})

        assert stale == []

    def test_host_sites_name_no_workflow_and_copies_carry_the_copied_id(self) -> None:
        for construction in _constructions():
            kind = _ALLOWED_SITES.get(construction.site)
            argument = _workflow_id_argument(construction.call)
            has_unpacked_keywords = any(keyword.arg is None for keyword in construction.call.keywords)
            assert not has_unpacked_keywords, construction.site
            if kind is _SiteKind.HOST:
                assert argument is None, construction.site
            if kind is _SiteKind.COPY and _called_name(construction.call) in {"ExecutionEvent", "ProgressEvent"}:
                assert isinstance(argument, ast.Attribute), construction.site
                assert argument.attr == "workflow_id", construction.site
