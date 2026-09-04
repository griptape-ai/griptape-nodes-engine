"""Tests for unregister_library teardown and tracking-field cleanup (#4742)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from griptape_nodes.node_library.advanced_node_library import AdvancedNodeLibrary
from griptape_nodes.node_library.library_registry import Library, LibraryRegistry, LibrarySchema


def _make_schema(name: str = "TestLib") -> LibrarySchema:
    schema = MagicMock(spec=LibrarySchema)
    schema.execution_modules = None
    schema.is_default_library = False
    schema.name = name
    return schema


def _register_library(name: str = "TestLib", advanced_library: AdvancedNodeLibrary | None = None) -> Library:
    registry = LibraryRegistry()
    schema = _make_schema(name)
    library = Library(library_data=schema, advanced_library=advanced_library)
    registry._libraries[name] = library
    return library


class TestUnregisterLibraryTeardownHook:
    def test_before_library_unregistered_is_called(self) -> None:
        calls = []

        class MyLib(AdvancedNodeLibrary):
            def before_library_unregistered(self, _library_data: LibrarySchema, _library: Library) -> None:
                calls.append(True)

        _register_library(advanced_library=MyLib())
        LibraryRegistry.unregister_library("TestLib", event_manager=MagicMock())
        assert calls == [True]

    def test_exception_in_teardown_does_not_prevent_unregistration(self) -> None:
        class BoomLib(AdvancedNodeLibrary):
            def before_library_unregistered(self, _library_data: LibrarySchema, _library: Library) -> None:
                msg = "teardown exploded"
                raise RuntimeError(msg)

        _register_library(advanced_library=BoomLib())
        # Should not raise
        LibraryRegistry.unregister_library("TestLib", event_manager=MagicMock())
        # Library should be gone
        registry = LibraryRegistry()
        assert "TestLib" not in registry._libraries

    def test_teardown_not_called_when_no_advanced_library(self) -> None:
        _register_library(advanced_library=None)
        # Should complete without error
        LibraryRegistry.unregister_library("TestLib", event_manager=MagicMock())
        registry = LibraryRegistry()
        assert "TestLib" not in registry._libraries


class TestUnregisterLibraryEventManagerCleanup:
    def test_request_handler_types_are_deregistered(self) -> None:
        library = _register_library()
        library._registered_request_handler_types.append(str)
        library._registered_request_handler_types.append(int)

        event_manager = MagicMock()
        LibraryRegistry.unregister_library("TestLib", event_manager=event_manager)

        called_types = {c.args[0] for c in event_manager.remove_manager_from_request_type.call_args_list}
        assert called_types == {str, int}

    def test_app_event_listeners_are_deregistered(self) -> None:
        listener = MagicMock()
        library = _register_library()
        library._registered_app_event_listeners.append((str, listener))

        event_manager = MagicMock()
        LibraryRegistry.unregister_library("TestLib", event_manager=event_manager)

        event_manager.remove_listener_for_app_event.assert_called_once_with(str, listener)

    def test_pre_dispatch_hooks_are_deregistered(self) -> None:
        hook = MagicMock()
        library = _register_library()
        library._registered_pre_dispatch_hooks.append(hook)

        event_manager = MagicMock()
        LibraryRegistry.unregister_library("TestLib", event_manager=event_manager)

        event_manager.remove_pre_dispatch_hook.assert_called_once_with(hook)

    def test_post_dispatch_hooks_are_deregistered(self) -> None:
        hook = MagicMock()
        library = _register_library()
        library._registered_post_dispatch_hooks.append((str, hook))

        event_manager = MagicMock()
        LibraryRegistry.unregister_library("TestLib", event_manager=event_manager)

        # Removal is per request type, so both halves of the pair are needed.
        event_manager.remove_post_dispatch_hook.assert_called_once_with(str, hook)

    def test_every_post_dispatch_hook_is_deregistered(self) -> None:
        """A library may hook several request types; leaving any behind leaks a live callback.

        The single-hook test above cannot see a teardown that stops after the first pair,
        and a leaked hook keeps calling into a module the unload was supposed to retire.
        """
        first_hook = MagicMock()
        second_hook = MagicMock()
        library = _register_library()
        library._registered_post_dispatch_hooks.append((str, first_hook))
        library._registered_post_dispatch_hooks.append((int, second_hook))

        event_manager = MagicMock()
        LibraryRegistry.unregister_library("TestLib", event_manager=event_manager)

        removed = [(c.args[0], c.args[1]) for c in event_manager.remove_post_dispatch_hook.call_args_list]
        assert removed == [(str, first_hook), (int, second_hook)]

    def test_tracking_fields_cleared_after_deregistration(self) -> None:
        library = _register_library()
        library._registered_request_handler_types.append(str)
        library._registered_app_event_listeners.append((str, MagicMock()))
        library._registered_pre_dispatch_hooks.append(MagicMock())
        library._registered_post_dispatch_hooks.append((str, MagicMock()))

        event_manager = MagicMock()
        LibraryRegistry.unregister_library("TestLib", event_manager=event_manager)

        # Library object still exists locally; check fields are cleared
        assert library._registered_request_handler_types == []
        assert library._registered_app_event_listeners == []
        assert library._registered_pre_dispatch_hooks == []
        assert library._registered_post_dispatch_hooks == []

    def test_unregister_raises_for_unknown_library(self) -> None:
        with pytest.raises(KeyError):
            LibraryRegistry.unregister_library("DoesNotExist", event_manager=MagicMock())
