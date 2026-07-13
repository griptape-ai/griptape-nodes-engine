"""Tests for lazy node-type registration on Library.

A node type registered via ``register_lazy_node_type`` records its metadata and a
loader but does not import the node's Python module until the real class is first
needed (creation, execution, or introspection). Import failures surface to that
first caller rather than at library-load time.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from griptape_nodes.exe_types.node_types import AsyncResult, BaseNode
from griptape_nodes.node_library.library_registry import Library, LibrarySchema, NodeMetadata


class _LazyMockNode(BaseNode):
    def __init__(self, name: str = "mock", **kwargs: Any) -> None:
        super().__init__(name=name, **kwargs)

    def process(self) -> AsyncResult | None:
        return None


class _AlphaNode(_LazyMockNode):
    pass


class _BetaNode(_LazyMockNode):
    pass


def _make_library(name: str = "TestLib") -> Library:
    schema = MagicMock(spec=LibrarySchema)
    schema.is_default_library = False
    schema.name = name
    return Library(library_data=schema)


def _metadata(display_name: str = "Node") -> NodeMetadata:
    return NodeMetadata(category="test", description="desc", display_name=display_name)


class TestLazyRegistration:
    def test_register_lazy_does_not_invoke_loader(self) -> None:
        lib = _make_library()
        loader = MagicMock(return_value=_LazyMockNode)

        lib.register_lazy_node_type("_LazyMockNode", metadata=_metadata(), loader=loader)

        loader.assert_not_called()
        assert lib.has_node_type("_LazyMockNode")
        assert "_LazyMockNode" in lib.get_registered_nodes()

    def test_metadata_available_without_resolution(self) -> None:
        lib = _make_library()
        loader = MagicMock(return_value=_LazyMockNode)
        metadata = _metadata(display_name="Fancy Node")

        lib.register_lazy_node_type("_LazyMockNode", metadata=metadata, loader=loader)

        assert lib.get_node_metadata("_LazyMockNode") is metadata
        loader.assert_not_called()


class TestLazyResolution:
    def test_create_node_resolves_loader_once(self) -> None:
        lib = _make_library()
        loader = MagicMock(return_value=_LazyMockNode)
        lib.register_lazy_node_type("_LazyMockNode", metadata=_metadata(), loader=loader)

        first = lib.create_node(node_type="_LazyMockNode", name="a")
        second = lib.create_node(node_type="_LazyMockNode", name="b")

        assert isinstance(first, _LazyMockNode)
        assert isinstance(second, _LazyMockNode)
        # Loader runs once; the resolved class is promoted and reused thereafter.
        loader.assert_called_once()

    def test_get_node_class_resolves_and_caches(self) -> None:
        lib = _make_library()
        loader = MagicMock(return_value=_LazyMockNode)
        lib.register_lazy_node_type("_LazyMockNode", metadata=_metadata(), loader=loader)

        assert lib.get_node_class("_LazyMockNode") is _LazyMockNode
        assert lib.get_node_class("_LazyMockNode") is _LazyMockNode
        # The module is imported at most once; the resolved class is cached.
        loader.assert_called_once()

    def test_get_node_class_unknown_type_raises_keyerror(self) -> None:
        lib = _make_library()
        with pytest.raises(KeyError):
            lib.get_node_class("DoesNotExist")

    def test_create_node_unknown_type_raises_keyerror(self) -> None:
        lib = _make_library()
        with pytest.raises(KeyError):
            lib.create_node(node_type="DoesNotExist", name="a")


class TestImportFailureSurfacesOnFirstUse:
    def test_create_node_propagates_import_error(self) -> None:
        lib = _make_library()

        def boom() -> type[BaseNode]:
            msg = "module import blew up"
            raise ImportError(msg)

        lib.register_lazy_node_type("_LazyMockNode", metadata=_metadata(), loader=boom)

        # Registration and metadata lookups succeed; the failure is deferred to first use.
        assert lib.has_node_type("_LazyMockNode")
        assert lib.get_node_metadata("_LazyMockNode").display_name == "Node"

        with pytest.raises(ImportError, match="module import blew up"):
            lib.create_node(node_type="_LazyMockNode", name="a")


class TestGetNodesByBaseType:
    def test_resolves_lazy_types(self) -> None:
        lib = _make_library()
        lib.register_lazy_node_type("_AlphaNode", metadata=_metadata(), loader=lambda: _AlphaNode)
        lib.register_lazy_node_type("_BetaNode", metadata=_metadata(), loader=lambda: _BetaNode)

        assert lib.get_nodes_by_base_type(_AlphaNode) == ["_AlphaNode"]

    def test_skips_types_whose_module_fails_to_import(self) -> None:
        lib = _make_library()

        def boom() -> type[BaseNode]:
            raise ImportError

        lib.register_lazy_node_type("_AlphaNode", metadata=_metadata(), loader=lambda: _AlphaNode)
        lib.register_lazy_node_type("_Broken", metadata=_metadata(), loader=boom)

        # The broken type is skipped; the scan still finds the importable one.
        assert lib.get_nodes_by_base_type(_LazyMockNode) == ["_AlphaNode"]


class TestSupersedeSemantics:
    def test_eager_registration_supersedes_lazy(self) -> None:
        lib = _make_library()
        loader = MagicMock(return_value=_AlphaNode)
        lib.register_lazy_node_type("_AlphaNode", metadata=_metadata(), loader=loader)

        lib.register_new_node_type(_AlphaNode, metadata=_metadata())

        assert lib.get_node_class("_AlphaNode") is _AlphaNode
        loader.assert_not_called()

    def test_lazy_registration_supersedes_eager(self) -> None:
        lib = _make_library()
        lib.register_new_node_type(_AlphaNode, metadata=_metadata())

        loader = MagicMock(return_value=_BetaNode)
        lib.register_lazy_node_type("_AlphaNode", metadata=_metadata(), loader=loader)

        assert lib.get_node_class("_AlphaNode") is _BetaNode
        loader.assert_called_once()


class TestUnregister:
    def test_unregister_removes_lazy_type(self) -> None:
        lib = _make_library()
        lib.register_lazy_node_type("_LazyMockNode", metadata=_metadata(), loader=lambda: _LazyMockNode)

        lib.unregister_node_type("_LazyMockNode")

        assert not lib.has_node_type("_LazyMockNode")

    def test_unregister_unknown_type_raises_keyerror(self) -> None:
        lib = _make_library()
        with pytest.raises(KeyError):
            lib.unregister_node_type("DoesNotExist")
