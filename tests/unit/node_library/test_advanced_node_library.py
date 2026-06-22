"""Tests for AdvancedNodeLibrary hooks added in #4742 and #4744."""

from __future__ import annotations

from unittest.mock import MagicMock

from griptape_nodes.node_library.advanced_node_library import AdvancedNodeLibrary
from griptape_nodes.node_library.library_registry import Library, LibrarySchema


def _make_args() -> tuple[MagicMock, Library]:
    schema = MagicMock(spec=LibrarySchema)
    schema.is_default_library = False
    schema.name = "TestLib"
    library = Library(library_data=schema)
    return schema, library


class TestBeforeLibraryUnregistered:
    def test_base_class_is_noop(self) -> None:
        adv = AdvancedNodeLibrary()
        schema, library = _make_args()
        # Should not raise
        adv.before_library_unregistered(schema, library)

    def test_subclass_can_override(self) -> None:
        calls = []

        class MyLib(AdvancedNodeLibrary):
            def before_library_unregistered(self, library_data: LibrarySchema, library: Library) -> None:
                calls.append((library_data, library))

        adv = MyLib()
        schema, library = _make_args()
        adv.before_library_unregistered(schema, library)
        assert len(calls) == 1
        assert calls[0] == (schema, library)


class TestGetRequestHandlers:
    def test_base_class_returns_empty_list(self) -> None:
        adv = AdvancedNodeLibrary()
        assert adv.get_request_handlers() == []

    def test_subclass_can_override(self) -> None:
        from dataclasses import dataclass

        from griptape_nodes.retained_mode.events.base_events import RequestPayload, ResultPayload

        @dataclass
        class FakeRequest(RequestPayload):
            pass

        @dataclass
        class FakeResult(ResultPayload):
            def succeeded(self) -> bool:
                return True

        def handler(_req: FakeRequest) -> FakeResult:
            return FakeResult(result_details="ok")

        class MyLib(AdvancedNodeLibrary):
            def get_request_handlers(self) -> list:
                return [(FakeRequest, handler)]

        adv = MyLib()
        handlers = adv.get_request_handlers()
        assert len(handlers) == 1
        assert handlers[0][0] is FakeRequest
        assert handlers[0][1] is handler
