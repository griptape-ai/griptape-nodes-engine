"""Tests for Library tracking fields added in #4742."""

from __future__ import annotations

from unittest.mock import MagicMock

from griptape_nodes.node_library.library_registry import Library, LibrarySchema


def _make_library() -> Library:
    schema = MagicMock(spec=LibrarySchema)
    schema.is_default_library = False
    schema.name = "TestLib"
    return Library(library_data=schema)


class TestLibraryTrackingFields:
    def test_registered_app_event_listeners_starts_empty(self) -> None:
        lib = _make_library()
        assert lib.get_registered_app_event_listeners() == []

    def test_registered_pre_dispatch_hooks_starts_empty(self) -> None:
        lib = _make_library()
        assert lib.get_registered_pre_dispatch_hooks() == []

    def test_registered_request_handler_types_starts_empty(self) -> None:
        lib = _make_library()
        assert lib.get_registered_request_handler_types() == []

    def test_accessors_return_copies(self) -> None:
        lib = _make_library()
        listeners = lib.get_registered_app_event_listeners()
        listeners.append((str, lambda: None))
        assert lib.get_registered_app_event_listeners() == []

    def test_can_append_to_internal_lists(self) -> None:
        lib = _make_library()
        lib._registered_request_handler_types.append(str)
        assert lib.get_registered_request_handler_types() == [str]
