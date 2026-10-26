"""Tests for get_mcp_tools() registration in attempt_load_nodes_from_library."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Never
from unittest.mock import MagicMock, patch

from griptape_nodes.node_library.advanced_node_library import AdvancedNodeLibrary, MCPTool
from griptape_nodes.node_library.library_registry import Library, LibraryRegistry, LibrarySchema
from griptape_nodes.retained_mode.events.base_events import RequestPayload, ResultPayload
from griptape_nodes.retained_mode.managers.fitness_problems.libraries import MCPToolRegistrationProblem
from griptape_nodes.retained_mode.managers.library_manager import LibraryManager

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.engine import Engine


@dataclass
class _HandledRequest(RequestPayload):
    pass


@dataclass
class _OtherHandledRequest(RequestPayload):
    pass


@dataclass
class _UnhandledRequest(RequestPayload):
    pass


def _handler(_request: RequestPayload) -> ResultPayload:
    raise NotImplementedError


class _HandlingLib(AdvancedNodeLibrary):
    """Handles _HandledRequest and _OtherHandledRequest, and exposes the tools it is given."""

    def __init__(self, tools: list) -> None:
        self._tools = tools

    def get_request_handlers(self) -> list:
        return [(_HandledRequest, _handler), (_OtherHandledRequest, _handler)]

    def get_mcp_tools(self) -> list:
        return self._tools


def _make_library_info() -> LibraryManager.LibraryInfo:
    return LibraryManager.LibraryInfo(
        lifecycle_state=LibraryManager.LibraryLifecycleState.EVALUATED,
        fitness=LibraryManager.LibraryFitness.NOT_EVALUATED,
        library_path="/fake/path",
        is_sandbox=False,
        library_name="TestLib",
        library_version="1.0.0",
    )


def _make_library(advanced_library: AdvancedNodeLibrary) -> Library:
    schema = MagicMock(spec=LibrarySchema)
    schema.is_default_library = False
    schema.name = "TestLib"
    schema.nodes = []
    schema.workflow_nodes = []
    schema.widgets = []
    schema.config_categories = []
    return Library(library_data=schema, advanced_library=advanced_library)


def _load(engine: Engine, library: Library, library_info: LibraryManager.LibraryInfo) -> None:
    with patch.object(engine, "_event_manager", MagicMock()):
        engine.library_manager.module_loading.attempt_load_nodes_from_library(
            library_data=library._library_data,
            library=library,
            base_dir=Path("/fake"),
            library_info=library_info,
        )


def _problem_types(library_info: LibraryManager.LibraryInfo) -> list[type]:
    return [type(problem) for problem in library_info.problems]


class TestMCPToolRegistration:
    def test_tools_for_handled_requests_are_recorded_on_library(self, engine: Engine) -> None:
        tools = [MCPTool(_HandledRequest, read_only=True), MCPTool(_OtherHandledRequest)]
        library = _make_library(_HandlingLib(tools))
        library_info = _make_library_info()

        _load(engine, library, library_info)

        assert library.get_mcp_tools() == tools
        assert MCPToolRegistrationProblem not in _problem_types(library_info)

    def test_tool_for_unhandled_request_exposes_no_tools(self, engine: Engine) -> None:
        tools = [MCPTool(_HandledRequest), MCPTool(_UnhandledRequest)]
        library = _make_library(_HandlingLib(tools))
        library_info = _make_library_info()

        _load(engine, library, library_info)

        assert library.get_mcp_tools() == []
        problem = next(p for p in library_info.problems if isinstance(p, MCPToolRegistrationProblem))
        assert "_UnhandledRequest" in problem.error_message
        assert "get_request_handlers()" in problem.error_message

    def test_entry_that_is_not_an_mcp_tool_exposes_no_tools(self, engine: Engine) -> None:
        library = _make_library(_HandlingLib([_HandledRequest]))
        library_info = _make_library_info()

        _load(engine, library, library_info)

        assert library.get_mcp_tools() == []
        assert MCPToolRegistrationProblem in _problem_types(library_info)

    def test_exception_in_get_mcp_tools_appends_problem(self, engine: Engine) -> None:
        class BoomLib(AdvancedNodeLibrary):
            def get_mcp_tools(self) -> Never:
                msg = "tool registration exploded"
                raise RuntimeError(msg)

        library = _make_library(BoomLib())
        library_info = _make_library_info()

        _load(engine, library, library_info)

        assert MCPToolRegistrationProblem in _problem_types(library_info)

    def test_unregistering_the_library_removes_its_tools(self, engine: Engine) -> None:
        library = _make_library(_HandlingLib([MCPTool(_HandledRequest)]))
        _load(engine, library, _make_library_info())

        LibraryRegistry._deregister_tracked_handlers(library, event_manager=MagicMock())

        assert library.get_mcp_tools() == []
