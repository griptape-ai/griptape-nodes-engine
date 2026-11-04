"""Tests for the lookup failures the library registry raises.

Registry failures carry sentences meant for artists, and those sentences reach `result_details` on
failure results. A bare `KeyError` would repr its argument and show the sentence wrapped in quotes,
so the registry raises a `KeyError` subclass that renders plainly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from griptape_nodes.exe_types.node_types import BaseNode
from griptape_nodes.node_library.library_registry import (
    LibraryMetadata,
    LibraryRegistry,
    LibraryRegistryError,
    LibrarySchema,
    NodeMetadata,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

LIBRARY_NAME = "Registry Error Test Library"


class _SharedProbe(BaseNode):
    """A node type two libraries can both claim, so looking it up is ambiguous."""


@pytest.fixture(autouse=True)
def _clear_registry() -> Iterator[None]:
    # LibraryRegistry keeps its state in ClassVars, which the singleton reset does not touch.
    LibraryRegistry._clear()
    yield
    LibraryRegistry._clear()


def _register_library(name: str = LIBRARY_NAME) -> None:
    schema = LibrarySchema(
        name=name,
        library_schema_version=LibrarySchema.LATEST_SCHEMA_VERSION,
        metadata=LibraryMetadata(
            author="test", description="test", library_version="1.0.0", engine_version="1.0.0", tags=[]
        ),
        categories=[],
        nodes=[],
    )
    library = LibraryRegistry.generate_new_library(library_data=schema)
    library.register_new_node_type(_SharedProbe, NodeMetadata(category="t", description="d", display_name="Probe"))


class TestLibraryRegistryError:
    def test_the_message_reads_without_repr_quoting(self) -> None:
        err = LibraryRegistryError("Node type 'Agent' not found in library 'Lib'")

        assert str(err) == "Node type 'Agent' not found in library 'Lib'"

    def test_it_is_still_a_key_error(self) -> None:
        # Registry lookups are handled with `except KeyError` across the engine, and this subclass
        # must not slip past those handlers.
        msg = "boom"
        with pytest.raises(KeyError):
            raise LibraryRegistryError(msg)

    def test_a_missing_library_reports_plainly(self) -> None:
        with pytest.raises(LibraryRegistryError) as exc_info:
            LibraryRegistry.get_library("Nonexistent Library")

        assert str(exc_info.value) == "Library 'Nonexistent Library' not found"

    def test_a_missing_node_type_reports_plainly(self) -> None:
        _register_library()
        library = LibraryRegistry.get_library(LIBRARY_NAME)

        with pytest.raises(LibraryRegistryError) as exc_info:
            library.get_node_metadata("Nope")

        assert str(exc_info.value) == f"Node type 'Nope' not found in library '{LIBRARY_NAME}'"

    def test_an_unclaimed_node_type_reports_plainly(self) -> None:
        with pytest.raises(LibraryRegistryError) as exc_info:
            LibraryRegistry.get_library_for_node_type("Nope")

        assert str(exc_info.value) == "No node type 'Nope' could be found in any of the libraries registered."

    def test_an_ambiguous_node_type_names_the_libraries(self) -> None:
        _register_library("Library A")
        _register_library("Library B")

        with pytest.raises(LibraryRegistryError) as exc_info:
            LibraryRegistry.get_library_for_node_type(_SharedProbe.__name__)

        message = str(exc_info.value)
        assert "Library A" in message
        assert "Library B" in message
