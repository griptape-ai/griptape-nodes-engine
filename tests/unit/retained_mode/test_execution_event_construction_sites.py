"""Engine code builds execution and progress events only where the workflow they name is known.

`WorkflowRuntime.execution_event` and `BaseNode.execution_event` stamp the workflow when an event is
built. Anything else that builds one is listed here with the reason it may: a host event names no
workflow, and a copy carries the id of the event it copies. A new construction site fails this test
until it uses a factory or is listed, so nothing fills the id from ambient state or tags it later.
Factories must stamp the workflow they hold, and nothing re-stamps a copy with `model_copy`.
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
# The events that hold a `workflow_id`; `ExecutionGriptapeNodeEvent` only wraps an `ExecutionEvent`.
_STAMPED_EVENT_TYPES = frozenset({"ExecutionEvent", "ProgressEvent"})
# `ProgressEvent(value, node_name, parameter_name, workflow_id)`: a fourth positional argument is the id.
_PROGRESS_EVENT_POSITIONAL_FIELDS = 3


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

# What each factory stamps: the workflow it holds, written exactly so.
_FACTORY_WORKFLOW_IDS: dict[_Site, str] = {
    _Site("retained_mode/runtime/workflow_runtime.py", "WorkflowRuntime.execution_event"): "self.workflow_id",
    _Site("exe_types/node_types.py", "BaseNode.execution_event"): "self._event_workflow_id()",
    _Site("exe_types/node_types.py", "BaseNode.append_value_to_parameter"): "self._event_workflow_id()",
}

# `model_copy(update={"workflow_id": ...})` calls on things that are not events.
_NON_EVENT_WORKFLOW_ID_COPIES: frozenset[_Site] = frozenset(
    {
        # A workflow file's metadata takes the id of the registry entry it belongs to.
        _Site("node_library/workflow_registry.py", "_WorkflowRegistry.register_file"),
        _Site("node_library/workflow_registry.py", "Workflow.metadata"),
        _Site("retained_mode/managers/context_manager.py", "ContextManager._mirror_registry_entry"),
        _Site("retained_mode/managers/context_manager.py", "ContextManager._find_or_register_open_file"),
        _Site("retained_mode/managers/workflow/catalog.py", "WorkflowCatalog._merge_metadata"),
        _Site("retained_mode/managers/workflow/identity.py", "WorkflowIdentity._restamp"),
        _Site("retained_mode/managers/workflow/identity.py", "WorkflowIdentity._settle_loser"),
        _Site("retained_mode/managers/workflow/identity.py", "WorkflowIdentity._with_library_id"),
    }
)


class _Construction(NamedTuple):
    site: _Site
    call: ast.Call
    event_type: str


class _ConstructionFinder(ast.NodeVisitor):
    """Finds the calls in one module that build an event, or copy something with a new `workflow_id`."""

    def __init__(self, path: str, module: ast.Module) -> None:
        self.path = path
        self.scope: list[str] = []
        self.found: list[_Construction] = []
        self.workflow_id_copies: list[_Site] = []
        # `from ... import ExecutionEvent as EE` builds one under another name.
        self.aliases = {
            alias.asname: alias.name
            for node in ast.walk(module)
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
            if alias.name in _EVENT_TYPES and alias.asname is not None
        }

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._visit_scope(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_scope(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_scope(node)

    def visit_Call(self, node: ast.Call) -> None:
        site = _Site(self.path, ".".join(self.scope))
        called = _called_name(node)
        event_type = self.aliases.get(called or "", called)
        if event_type in _EVENT_TYPES:
            self.found.append(_Construction(site, node, event_type))
        if called == "model_copy" and _updates_workflow_id(node):
            self.workflow_id_copies.append(site)
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


def _updates_workflow_id(call: ast.Call) -> bool:
    for keyword in call.keywords:
        if keyword.arg != "update" or not isinstance(keyword.value, ast.Dict):
            continue
        for key in keyword.value.keys:
            if isinstance(key, ast.Constant) and key.value == "workflow_id":
                return True
    return False


def _workflow_id_argument(call: ast.Call) -> ast.expr | None:
    for keyword in call.keywords:
        if keyword.arg == "workflow_id":
            return keyword.value
    return None


def _scan(path: str, source: str) -> _ConstructionFinder:
    module = ast.parse(source, filename=path)
    finder = _ConstructionFinder(path, module)
    finder.visit(module)
    return finder


def _misbuilt(construction: _Construction, kind: _SiteKind | None) -> str | None:
    """Why a listed site builds its event wrongly, or None when it builds it as its kind allows."""
    call = construction.call
    if any(keyword.arg is None for keyword in call.keywords):
        return "passes unpacked keywords, which can carry a workflow_id"
    if construction.event_type == "ProgressEvent" and len(call.args) > _PROGRESS_EVENT_POSITIONAL_FIELDS:
        return "passes the workflow_id of a ProgressEvent positionally"
    if construction.event_type not in _STAMPED_EVENT_TYPES:
        return None
    return _misstamped(construction.site, kind, _workflow_id_argument(call))


def _misstamped(site: _Site, kind: _SiteKind | None, argument: ast.expr | None) -> str | None:
    """Why a listed site's `workflow_id` argument is wrong for its kind, or None when it is right."""
    if kind is _SiteKind.HOST and argument is not None:
        return "is a host site but passes a workflow_id"
    if kind is _SiteKind.COPY and not (isinstance(argument, ast.Attribute) and argument.attr == "workflow_id"):
        return "is a copy but does not carry over the copied event's workflow_id"
    if kind is _SiteKind.FACTORY:
        expected = _FACTORY_WORKFLOW_IDS[site]
        if argument is None or ast.unparse(argument) != expected:
            return f"is a factory but does not stamp {expected}"
    return None


@functools.cache
def _scans() -> tuple[_ConstructionFinder, ...]:
    return tuple(
        _scan(source.relative_to(_SOURCE_ROOT).as_posix(), source.read_text(encoding="utf-8"))
        for source in sorted(_SOURCE_ROOT.rglob("*.py"))
    )


def _constructions() -> list[_Construction]:
    return [construction for finder in _scans() for construction in finder.found]


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

    def test_each_site_stamps_the_workflow_its_kind_allows(self) -> None:
        misbuilt = [
            f"{construction.site}: {reason}"
            for construction in _constructions()
            if (reason := _misbuilt(construction, _ALLOWED_SITES.get(construction.site))) is not None
        ]

        assert misbuilt == []

    def test_every_factory_says_what_it_stamps(self) -> None:
        factories = {site for site, kind in _ALLOWED_SITES.items() if kind is _SiteKind.FACTORY}

        assert factories == set(_FACTORY_WORKFLOW_IDS)

    def test_nothing_copies_an_event_with_a_new_workflow_id(self) -> None:
        copies = {site for finder in _scans() for site in finder.workflow_id_copies}

        assert sorted(copies - _NON_EVENT_WORKFLOW_ID_COPIES) == []
        assert sorted(_NON_EVENT_WORKFLOW_ID_COPIES - copies) == []


_FACTORY = _Site("exe_types/node_types.py", "BaseNode.execution_event")
_HOST = _Site("retained_mode/publishing/workflow_packager.py", "WorkflowPackager.emit_progress")


class TestTheScanCatches:
    """Samples of each way around the factories, so the scan above is known to see them."""

    def test_a_construction_under_an_aliased_import(self) -> None:
        finder = _scan(
            "sample.py",
            "from griptape_nodes.retained_mode.events.base_events import ExecutionEvent as EE\n"
            "def emit(payload):\n"
            "    return EE(payload=payload, workflow_id=ambient())\n",
        )

        assert [(construction.site, construction.event_type) for construction in finder.found] == [
            (_Site("sample.py", "emit"), "ExecutionEvent")
        ]

    def test_a_model_copy_that_sets_the_workflow_id(self) -> None:
        finder = _scan(
            "sample.py",
            "def tag(event):\n    return event.model_copy(update={'workflow_id': ambient()})\n",
        )

        assert finder.workflow_id_copies == [_Site("sample.py", "tag")]

    def test_a_positional_workflow_id_on_a_progress_event_at_a_host_site(self) -> None:
        [construction] = _scan("sample.py", "ProgressEvent('chunk', 'node', 'out', ambient())\n").found

        reason = _misbuilt(construction._replace(site=_HOST), _SiteKind.HOST)

        assert reason == "passes the workflow_id of a ProgressEvent positionally"

    def test_a_factory_that_stamps_something_other_than_the_workflow_it_holds(self) -> None:
        [construction] = _scan("sample.py", "ExecutionEvent(payload=payload, workflow_id=ambient())\n").found

        reason = _misbuilt(construction._replace(site=_FACTORY), _SiteKind.FACTORY)

        assert reason == "is a factory but does not stamp self._event_workflow_id()"
