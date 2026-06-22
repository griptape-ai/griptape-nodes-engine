"""Tests for unregister_library teardown and tracking-field cleanup (#4742)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from griptape_nodes.node_library.advanced_node_library import AdvancedNodeLibrary
from griptape_nodes.node_library.library_registry import Library, LibraryRegistry, LibrarySchema


def _make_schema(name: str = "TestLib") -> LibrarySchema:
    schema = MagicMock(spec=LibrarySchema)
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
        LibraryRegistry.unregister_library("TestLib")
        assert calls == [True]

    def test_exception_in_teardown_does_not_prevent_unregistration(self) -> None:
        class BoomLib(AdvancedNodeLibrary):
            def before_library_unregistered(self, _library_data: LibrarySchema, _library: Library) -> None:
                msg = "teardown exploded"
                raise RuntimeError(msg)

        _register_library(advanced_library=BoomLib())
        # Should not raise
        LibraryRegistry.unregister_library("TestLib")
        # Library should be gone
        registry = LibraryRegistry()
        assert "TestLib" not in registry._libraries

    def test_teardown_not_called_when_no_advanced_library(self) -> None:
        _register_library(advanced_library=None)
        # Should complete without error
        LibraryRegistry.unregister_library("TestLib")
        registry = LibraryRegistry()
        assert "TestLib" not in registry._libraries


class TestUnregisterLibraryEventManagerCleanup:
    def test_request_handler_types_are_deregistered(self) -> None:
        from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

        library = _register_library()
        library._registered_request_handler_types.append(str)
        library._registered_request_handler_types.append(int)

        event_manager = MagicMock()
        with patch.object(GriptapeNodes, "EventManager", return_value=event_manager):
            LibraryRegistry.unregister_library("TestLib")

        called_types = {c.args[0] for c in event_manager.remove_manager_from_request_type.call_args_list}
        assert called_types == {str, int}

    def test_app_event_listeners_are_deregistered(self) -> None:
        from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

        listener = MagicMock()
        library = _register_library()
        library._registered_app_event_listeners.append((str, listener))

        event_manager = MagicMock()
        with patch.object(GriptapeNodes, "EventManager", return_value=event_manager):
            LibraryRegistry.unregister_library("TestLib")

        event_manager.remove_listener_for_app_event.assert_called_once_with(str, listener)

    def test_pre_dispatch_hooks_are_deregistered(self) -> None:
        from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

        hook = MagicMock()
        library = _register_library()
        library._registered_pre_dispatch_hooks.append(hook)

        event_manager = MagicMock()
        with patch.object(GriptapeNodes, "EventManager", return_value=event_manager):
            LibraryRegistry.unregister_library("TestLib")

        event_manager.remove_pre_dispatch_hook.assert_called_once_with(hook)

    def test_tracking_fields_cleared_after_deregistration(self) -> None:
        from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

        library = _register_library()
        library._registered_request_handler_types.append(str)
        library._registered_app_event_listeners.append((str, MagicMock()))
        library._registered_pre_dispatch_hooks.append(MagicMock())

        event_manager = MagicMock()
        with patch.object(GriptapeNodes, "EventManager", return_value=event_manager):
            LibraryRegistry.unregister_library("TestLib")

        # Library object still exists locally; check fields are cleared
        assert library._registered_request_handler_types == []
        assert library._registered_app_event_listeners == []
        assert library._registered_pre_dispatch_hooks == []

    def test_unregister_raises_for_unknown_library(self) -> None:
        with pytest.raises(KeyError):
            LibraryRegistry.unregister_library("DoesNotExist")
