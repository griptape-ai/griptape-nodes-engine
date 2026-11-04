"""Tests for get_execution_event_listeners() and get_app_event_listeners() registration.

Both are registered in attempt_load_nodes_from_library the same way, so each test runs once for
each kind of listener.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Never
from unittest.mock import MagicMock, patch

import pytest

from griptape_nodes.node_library.advanced_node_library import AdvancedNodeLibrary
from griptape_nodes.node_library.library_registry import Library, LibrarySchema
from griptape_nodes.retained_mode.events.app_events import ConfigChanged
from griptape_nodes.retained_mode.events.execution_events import NodeFinishProcessEvent
from griptape_nodes.retained_mode.managers.event_manager import EventManager
from griptape_nodes.retained_mode.managers.fitness_problems.libraries import (
    AppEventListenerRegistrationProblem,
    ExecutionEventListenerRegistrationProblem,
)
from griptape_nodes.retained_mode.managers.library_manager import LibraryManager

if TYPE_CHECKING:
    from collections.abc import Callable

    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.managers.fitness_problems.libraries.library_problem import LibraryProblem


@dataclass(frozen=True)
class ListenerKind:
    """Everything that differs between registering execution and app event listeners."""

    getter_name: str
    event_type: type
    wrong_family_event_type: type
    add_method_name: str
    problem_type: type[LibraryProblem]
    tracked: Callable[[Library], list]


EXECUTION = ListenerKind(
    getter_name="get_execution_event_listeners",
    event_type=NodeFinishProcessEvent,
    wrong_family_event_type=ConfigChanged,
    add_method_name="add_listener_to_execution_event",
    problem_type=ExecutionEventListenerRegistrationProblem,
    tracked=Library.get_registered_execution_event_listeners,
)
APP = ListenerKind(
    getter_name="get_app_event_listeners",
    event_type=ConfigChanged,
    wrong_family_event_type=NodeFinishProcessEvent,
    add_method_name="add_listener_to_app_event",
    problem_type=AppEventListenerRegistrationProblem,
    tracked=Library.get_registered_app_event_listeners,
)
KINDS = pytest.mark.parametrize("kind", [EXECUTION, APP], ids=["execution", "app"])


def _listener(_payload: Any) -> None:
    return None


def _library_returning(kind: ListenerKind, listeners: Callable[[], list]) -> AdvancedNodeLibrary:
    advanced_library = AdvancedNodeLibrary()
    setattr(advanced_library, kind.getter_name, listeners)
    return advanced_library


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
    schema.workflow_nodes = []
    schema.widgets = []
    schema.config_categories = []
    return Library(library_data=schema, advanced_library=advanced_library)


def _load(engine: Engine, library: Library, library_info: LibraryManager.LibraryInfo, event_manager: Any) -> None:
    with patch.object(engine, "_event_manager", event_manager):
        engine.library_manager.module_loading.attempt_load_nodes_from_library(
            library_data=library._library_data,
            library=library,
            base_dir=Path("/fake"),
            library_info=library_info,
        )


def _problem_types(library_info: LibraryManager.LibraryInfo) -> list[type]:
    return [type(problem) for problem in library_info.problems]


class TestEventListenerRegistration:
    @KINDS
    def test_listeners_are_registered_and_tracked(self, engine: Engine, kind: ListenerKind) -> None:
        library = _make_library(_library_returning(kind, lambda: [(kind.event_type, _listener)]))
        library_info = _make_library_info()
        event_manager = MagicMock()

        _load(engine, library, library_info, event_manager)

        getattr(event_manager, kind.add_method_name).assert_called_once_with(kind.event_type, _listener)
        assert kind.tracked(library) == [(kind.event_type, _listener)]
        assert library_info.problems == []

    @KINDS
    def test_exception_from_the_getter_appends_problem(self, engine: Engine, kind: ListenerKind) -> None:
        def explode() -> Never:
            msg = "listener registration exploded"
            raise RuntimeError(msg)

        library = _make_library(_library_returning(kind, explode))
        library_info = _make_library_info()

        _load(engine, library, library_info, MagicMock())

        assert kind.problem_type in _problem_types(library_info)

    @KINDS
    def test_non_type_key_appends_problem(self, engine: Engine, kind: ListenerKind) -> None:
        library = _make_library(_library_returning(kind, lambda: [("NodeFinishProcessEvent", _listener)]))
        library_info = _make_library_info()
        event_manager = MagicMock()

        _load(engine, library, library_info, event_manager)

        assert kind.problem_type in _problem_types(library_info)
        getattr(event_manager, kind.add_method_name).assert_not_called()
        assert kind.tracked(library) == []

    @KINDS
    def test_event_type_of_the_other_family_appends_problem(self, engine: Engine, kind: ListenerKind) -> None:
        """An app event listed as an execution event, or the reverse, could never fire."""
        library = _make_library(_library_returning(kind, lambda: [(kind.wrong_family_event_type, _listener)]))
        library_info = _make_library_info()
        event_manager = MagicMock()

        _load(engine, library, library_info, event_manager)

        assert kind.problem_type in _problem_types(library_info)
        getattr(event_manager, kind.add_method_name).assert_not_called()

    @KINDS
    def test_non_callable_listener_appends_problem(self, engine: Engine, kind: ListenerKind) -> None:
        library = _make_library(_library_returning(kind, lambda: [(kind.event_type, "not a callable")]))
        library_info = _make_library_info()
        event_manager = MagicMock()

        _load(engine, library, library_info, event_manager)

        assert kind.problem_type in _problem_types(library_info)
        getattr(event_manager, kind.add_method_name).assert_not_called()

    @KINDS
    def test_a_bad_pair_stops_registration_but_leaves_earlier_pairs_tracked(
        self, engine: Engine, kind: ListenerKind
    ) -> None:
        """What did register must be tracked, or unloading would leave it calling into the library."""

        def first(_payload: Any) -> None:
            return

        def never_reached(_payload: Any) -> None:
            return

        library = _make_library(
            _library_returning(
                kind,
                lambda: [
                    (kind.event_type, first),
                    (kind.event_type, "not a callable"),
                    (kind.event_type, never_reached),
                ],
            )
        )
        library_info = _make_library_info()
        event_manager = MagicMock()

        _load(engine, library, library_info, event_manager)

        assert kind.problem_type in _problem_types(library_info)
        registered = [call.args[1] for call in getattr(event_manager, kind.add_method_name).call_args_list]
        assert registered == [first]
        assert kind.tracked(library) == [(kind.event_type, first)]

    def test_async_execution_listener_appends_problem(self, engine: Engine) -> None:
        """Execution listeners run inline on the emitting thread, so an async one is refused."""

        async def async_listener(_payload: Any) -> None:
            return

        library = _make_library(_library_returning(EXECUTION, lambda: [(NodeFinishProcessEvent, async_listener)]))
        library_info = _make_library_info()

        _load(engine, library, library_info, EventManager())

        assert ExecutionEventListenerRegistrationProblem in _problem_types(library_info)
        assert library.get_registered_execution_event_listeners() == []

    def test_async_app_listener_is_registered(self, engine: Engine) -> None:
        async def async_listener(_payload: Any) -> None:
            return

        library = _make_library(_library_returning(APP, lambda: [(ConfigChanged, async_listener)]))
        library_info = _make_library_info()

        _load(engine, library, library_info, EventManager())

        assert library_info.problems == []
        assert library.get_registered_app_event_listeners() == [(ConfigChanged, async_listener)]

    def test_empty_listener_lists_register_nothing(self, engine: Engine) -> None:
        library = _make_library(AdvancedNodeLibrary())
        library_info = _make_library_info()
        event_manager = MagicMock()

        _load(engine, library, library_info, event_manager)

        event_manager.add_listener_to_execution_event.assert_not_called()
        event_manager.add_listener_to_app_event.assert_not_called()
