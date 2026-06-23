"""Tests for get_request_handlers() registration in _attempt_load_nodes_from_library (#4744)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Never
from unittest.mock import MagicMock, patch

from griptape_nodes.node_library.advanced_node_library import AdvancedNodeLibrary
from griptape_nodes.node_library.library_registry import Library, LibrarySchema
from griptape_nodes.retained_mode.events.base_events import RequestPayload, ResultPayload
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
from griptape_nodes.retained_mode.managers.fitness_problems.libraries import (
    RequestHandlerRegistrationProblem,
    RequestHandlersWorkerIncompatibleProblem,
)
from griptape_nodes.retained_mode.managers.library_manager import LibraryManager


@dataclass
class _TestRequest(RequestPayload):
    pass


@dataclass
class _TestResult(ResultPayload):
    def succeeded(self) -> bool:
        return True


def _handler(_req: _TestRequest) -> _TestResult:
    return _TestResult(result_details="ok")


def _make_library_info() -> LibraryManager.LibraryInfo:
    return LibraryManager.LibraryInfo(
        lifecycle_state=LibraryManager.LibraryLifecycleState.EVALUATED,
        fitness=LibraryManager.LibraryFitness.NOT_EVALUATED,
        library_path="/fake/path",
        is_sandbox=False,
        library_name="TestLib",
        library_version="1.0.0",
    )


def _make_library(advanced_library: AdvancedNodeLibrary | None = None) -> Library:
    schema = MagicMock(spec=LibrarySchema)
    schema.is_default_library = False
    schema.name = "TestLib"
    schema.nodes = []
    schema.widgets = []
    schema.config_categories = []
    return Library(library_data=schema, advanced_library=advanced_library)


class TestRequestHandlerRegistration:
    def test_handlers_registered_via_event_manager(self, griptape_nodes: GriptapeNodes) -> None:
        """get_request_handlers() pairs should be registered with EventManager."""

        class MyLib(AdvancedNodeLibrary):
            def get_request_handlers(self) -> list:
                return [(_TestRequest, _handler)]

        library = _make_library(advanced_library=MyLib())
        library_info = _make_library_info()

        lm = griptape_nodes.LibraryManager()
        event_manager = MagicMock()
        with patch.object(GriptapeNodes, "EventManager", return_value=event_manager):
            lm._attempt_load_nodes_from_library(
                library_data=library._library_data,
                library=library,
                base_dir=Path("/fake"),
                library_info=library_info,
            )

        event_manager.assign_manager_to_request_type.assert_called_once_with(_TestRequest, _handler)

    def test_handler_types_recorded_on_library(self, griptape_nodes: GriptapeNodes) -> None:
        """Registered handler types must be stored in library._registered_request_handler_types."""

        class MyLib(AdvancedNodeLibrary):
            def get_request_handlers(self) -> list:
                return [(_TestRequest, _handler)]

        library = _make_library(advanced_library=MyLib())
        library_info = _make_library_info()

        lm = griptape_nodes.LibraryManager()
        event_manager = MagicMock()
        with patch.object(GriptapeNodes, "EventManager", return_value=event_manager):
            lm._attempt_load_nodes_from_library(
                library_data=library._library_data,
                library=library,
                base_dir=Path("/fake"),
                library_info=library_info,
            )

        assert _TestRequest in library._registered_request_handler_types

    def test_exception_in_get_request_handlers_appends_problem(self, griptape_nodes: GriptapeNodes) -> None:
        """An exception from get_request_handlers() should append RequestHandlerRegistrationProblem."""

        class BoomLib(AdvancedNodeLibrary):
            def get_request_handlers(self) -> Never:
                msg = "handler registration exploded"
                raise RuntimeError(msg)

        library = _make_library(advanced_library=BoomLib())
        library_info = _make_library_info()
        # Add a dummy node so the library isn't marked UNUSABLE (any_nodes_loaded_successfully=False)
        library_info.problems = []

        lm = griptape_nodes.LibraryManager()
        event_manager = MagicMock()
        with patch.object(GriptapeNodes, "EventManager", return_value=event_manager):
            lm._attempt_load_nodes_from_library(
                library_data=library._library_data,
                library=library,
                base_dir=Path("/fake"),
                library_info=library_info,
            )

        problem_types = [type(p) for p in library_info.problems]
        assert RequestHandlerRegistrationProblem in problem_types

    def test_no_advanced_library_does_nothing(self, griptape_nodes: GriptapeNodes) -> None:
        """A library with no advanced library should not touch EventManager at all."""
        library = _make_library(advanced_library=None)
        library_info = _make_library_info()

        lm = griptape_nodes.LibraryManager()
        event_manager = MagicMock()
        with patch.object(GriptapeNodes, "EventManager", return_value=event_manager):
            lm._attempt_load_nodes_from_library(
                library_data=library._library_data,
                library=library,
                base_dir=Path("/fake"),
                library_info=library_info,
            )

        event_manager.assign_manager_to_request_type.assert_not_called()

    def test_empty_get_request_handlers_does_nothing(self, griptape_nodes: GriptapeNodes) -> None:
        """A library returning [] from get_request_handlers() should not register any handlers."""

        class EmptyLib(AdvancedNodeLibrary):
            pass  # base default returns []

        library = _make_library(advanced_library=EmptyLib())
        library_info = _make_library_info()

        lm = griptape_nodes.LibraryManager()
        event_manager = MagicMock()
        with patch.object(GriptapeNodes, "EventManager", return_value=event_manager):
            lm._attempt_load_nodes_from_library(
                library_data=library._library_data,
                library=library,
                base_dir=Path("/fake"),
                library_info=library_info,
            )

        event_manager.assign_manager_to_request_type.assert_not_called()

    def test_worker_mode_library_with_handlers_appends_incompatible_problem(
        self, griptape_nodes: GriptapeNodes
    ) -> None:
        """A library requiring worker mode that declares handlers should surface RequestHandlersWorkerIncompatibleProblem."""

        class WorkerLib(AdvancedNodeLibrary):
            def get_request_handlers(self) -> list:
                return [(_TestRequest, _handler)]

        library = _make_library(advanced_library=WorkerLib())
        library_info = _make_library_info()
        library_info.requires_worker = True

        lm = griptape_nodes.LibraryManager()
        event_manager = MagicMock()
        with patch.object(GriptapeNodes, "EventManager", return_value=event_manager):
            lm._attempt_load_nodes_from_library(
                library_data=library._library_data,
                library=library,
                base_dir=Path("/fake"),
                library_info=library_info,
            )

        problem_types = [type(p) for p in library_info.problems]
        assert RequestHandlersWorkerIncompatibleProblem in problem_types

    def test_non_worker_library_with_handlers_no_incompatible_problem(self, griptape_nodes: GriptapeNodes) -> None:
        """A non-worker library with handlers should NOT get RequestHandlersWorkerIncompatibleProblem."""

        class OrchestratorLib(AdvancedNodeLibrary):
            def get_request_handlers(self) -> list:
                return [(_TestRequest, _handler)]

        library = _make_library(advanced_library=OrchestratorLib())
        library_info = _make_library_info()
        library_info.requires_worker = False

        lm = griptape_nodes.LibraryManager()
        event_manager = MagicMock()
        with patch.object(GriptapeNodes, "EventManager", return_value=event_manager):
            lm._attempt_load_nodes_from_library(
                library_data=library._library_data,
                library=library,
                base_dir=Path("/fake"),
                library_info=library_info,
            )

        problem_types = [type(p) for p in library_info.problems]
        assert RequestHandlersWorkerIncompatibleProblem not in problem_types
