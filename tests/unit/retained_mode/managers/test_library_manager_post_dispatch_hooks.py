"""Tests for get_post_dispatch_hooks() registration in _attempt_load_nodes_from_library."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Never
from unittest.mock import MagicMock, patch

from griptape_nodes.node_library.advanced_node_library import AdvancedNodeLibrary
from griptape_nodes.node_library.library_registry import Library, LibrarySchema
from griptape_nodes.retained_mode.events.base_events import RequestPayload, ResultPayload
from griptape_nodes.retained_mode.managers.fitness_problems.libraries import (
    PostDispatchHookRegistrationProblem,
    PostDispatchHooksWorkerIncompatibleProblem,
)
from griptape_nodes.retained_mode.managers.library_manager import LibraryManager

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.engine import Engine


@dataclass
class _TestRequest(RequestPayload):
    pass


def _hook(_request: RequestPayload, _result: ResultPayload) -> None:
    return None


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


def _load(lm: LibraryManager, library: Library, library_info: LibraryManager.LibraryInfo) -> None:
    lm._attempt_load_nodes_from_library(
        library_data=library._library_data,
        library=library,
        base_dir=Path("/fake"),
        library_info=library_info,
    )


class TestPostDispatchHookRegistration:
    def test_hooks_registered_via_event_manager(self, engine: Engine) -> None:
        """get_post_dispatch_hooks() pairs should be registered with EventManager."""

        class MyLib(AdvancedNodeLibrary):
            def get_post_dispatch_hooks(self) -> list:
                return [(_TestRequest, _hook)]

        library = _make_library(advanced_library=MyLib())
        library_info = _make_library_info()

        lm = engine.library_manager
        event_manager = MagicMock()
        with patch.object(engine, "_event_manager", event_manager):
            _load(lm, library, library_info)

        event_manager.add_post_dispatch_hook.assert_called_once_with(_TestRequest, _hook)

    def test_hook_pairs_recorded_on_library(self, engine: Engine) -> None:
        """The request type is tracked alongside the callback, because removal needs both."""

        class MyLib(AdvancedNodeLibrary):
            def get_post_dispatch_hooks(self) -> list:
                return [(_TestRequest, _hook)]

        library = _make_library(advanced_library=MyLib())
        library_info = _make_library_info()

        lm = engine.library_manager
        event_manager = MagicMock()
        with patch.object(engine, "_event_manager", event_manager):
            _load(lm, library, library_info)

        assert library.get_registered_post_dispatch_hooks() == [(_TestRequest, _hook)]

    def test_exception_in_get_post_dispatch_hooks_appends_problem(self, engine: Engine) -> None:
        """An exception from get_post_dispatch_hooks() should append PostDispatchHookRegistrationProblem."""

        class BoomLib(AdvancedNodeLibrary):
            def get_post_dispatch_hooks(self) -> Never:
                msg = "hook registration exploded"
                raise RuntimeError(msg)

        library = _make_library(advanced_library=BoomLib())
        library_info = _make_library_info()

        lm = engine.library_manager
        event_manager = MagicMock()
        with patch.object(engine, "_event_manager", event_manager):
            _load(lm, library, library_info)

        problem_types = [type(p) for p in library_info.problems]
        assert PostDispatchHookRegistrationProblem in problem_types

    def test_non_callable_hook_appends_problem(self, engine: Engine) -> None:
        """Library code is untyped at this boundary, so a bad pair must fail loudly here."""

        class BadLib(AdvancedNodeLibrary):
            def get_post_dispatch_hooks(self) -> list:
                return [(_TestRequest, "not a callable")]

        library = _make_library(advanced_library=BadLib())
        library_info = _make_library_info()

        lm = engine.library_manager
        event_manager = MagicMock()
        with patch.object(engine, "_event_manager", event_manager):
            _load(lm, library, library_info)

        problem_types = [type(p) for p in library_info.problems]
        assert PostDispatchHookRegistrationProblem in problem_types
        event_manager.add_post_dispatch_hook.assert_not_called()
        assert library.get_registered_post_dispatch_hooks() == []

    def test_a_bad_pair_stops_registration_but_leaves_earlier_pairs_tracked(self, engine: Engine) -> None:
        """A bad pair abandons the rest of the list, so what did register must still be tracked.

        Registration aborts on the first bad entry, matching the request-handler block
        above it, which also fails the whole block rather than skipping one entry. That
        leaves a library partially registered, which is only safe because every pair that
        did register is in the tracking list and so gets removed on unload. An untracked
        live hook would keep calling into a module the unload was supposed to retire.
        """

        def first_hook(_request: RequestPayload, _result: ResultPayload) -> None:
            return

        def never_reached(_request: RequestPayload, _result: ResultPayload) -> None:
            return

        class PartlyBadLib(AdvancedNodeLibrary):
            def get_post_dispatch_hooks(self) -> list:
                return [(_TestRequest, first_hook), (_TestRequest, "not a callable"), (_TestRequest, never_reached)]

        library = _make_library(advanced_library=PartlyBadLib())
        library_info = _make_library_info()

        lm = engine.library_manager
        event_manager = MagicMock()
        with patch.object(engine, "_event_manager", event_manager):
            _load(lm, library, library_info)

        problem_types = [type(p) for p in library_info.problems]
        assert PostDispatchHookRegistrationProblem in problem_types

        registered = [c.args[1] for c in event_manager.add_post_dispatch_hook.call_args_list]
        assert registered == [first_hook]
        assert library.get_registered_post_dispatch_hooks() == [(_TestRequest, first_hook)]

    def test_non_type_request_key_appends_problem(self, engine: Engine) -> None:
        """A key that is not a class would register and then never fire, silently.

        `_fire_post_dispatch_hooks` looks the callbacks up by `type(request)`, so a key
        like an instance or a string matches nothing. Without this check the library
        author gets a hook that does nothing and no diagnostic anywhere.
        """

        class BadKeyLib(AdvancedNodeLibrary):
            def get_post_dispatch_hooks(self) -> list:
                return [(_TestRequest(), _hook)]  # an instance, not the class

        library = _make_library(advanced_library=BadKeyLib())
        library_info = _make_library_info()

        lm = engine.library_manager
        event_manager = MagicMock()
        with patch.object(engine, "_event_manager", event_manager):
            _load(lm, library, library_info)

        problem_types = [type(p) for p in library_info.problems]
        assert PostDispatchHookRegistrationProblem in problem_types
        event_manager.add_post_dispatch_hook.assert_not_called()
        assert library.get_registered_post_dispatch_hooks() == []

    def test_no_advanced_library_does_nothing(self, engine: Engine) -> None:
        """A library with no advanced library should not touch EventManager at all."""
        library = _make_library(advanced_library=None)
        library_info = _make_library_info()

        lm = engine.library_manager
        event_manager = MagicMock()
        with patch.object(engine, "_event_manager", event_manager):
            _load(lm, library, library_info)

        event_manager.add_post_dispatch_hook.assert_not_called()

    def test_empty_get_post_dispatch_hooks_does_nothing(self, engine: Engine) -> None:
        """A library returning [] should not register any hooks."""

        class EmptyLib(AdvancedNodeLibrary):
            pass  # base default returns []

        library = _make_library(advanced_library=EmptyLib())
        library_info = _make_library_info()

        lm = engine.library_manager
        event_manager = MagicMock()
        with patch.object(engine, "_event_manager", event_manager):
            _load(lm, library, library_info)

        event_manager.add_post_dispatch_hook.assert_not_called()

    def test_worker_mode_library_with_hooks_appends_incompatible_problem(self, engine: Engine) -> None:
        """Hooks registered in a worker process never see requests the orchestrator handles."""

        class WorkerLib(AdvancedNodeLibrary):
            def get_post_dispatch_hooks(self) -> list:
                return [(_TestRequest, _hook)]

        library = _make_library(advanced_library=WorkerLib())
        library_info = _make_library_info()
        library_info.requires_worker = True

        lm = engine.library_manager
        event_manager = MagicMock()
        with patch.object(engine, "_event_manager", event_manager):
            _load(lm, library, library_info)

        problem_types = [type(p) for p in library_info.problems]
        assert PostDispatchHooksWorkerIncompatibleProblem in problem_types

    def test_non_worker_library_with_hooks_no_incompatible_problem(self, engine: Engine) -> None:
        """A non-worker library with hooks is the supported case and must load clean."""

        class OrchestratorLib(AdvancedNodeLibrary):
            def get_post_dispatch_hooks(self) -> list:
                return [(_TestRequest, _hook)]

        library = _make_library(advanced_library=OrchestratorLib())
        library_info = _make_library_info()
        library_info.requires_worker = False

        lm = engine.library_manager
        event_manager = MagicMock()
        with patch.object(engine, "_event_manager", event_manager):
            _load(lm, library, library_info)

        problem_types = [type(p) for p in library_info.problems]
        assert PostDispatchHooksWorkerIncompatibleProblem not in problem_types
