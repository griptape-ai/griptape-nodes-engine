"""Unit tests for module-level helpers in `library_registry`.

Focused on:
- resolve_provider_model_id resolves one of a node's declared catalog models
  to the upstream provider's id for it, and returns None for an id the node
  doesn't declare.
"""

from __future__ import annotations

import pytest

from griptape_nodes.exe_types.node_types import BaseNode
from griptape_nodes.node_library.library_declarations import (
    KeySupport,
    Model,
    ModelCatalogLibraryProperty,
    ModelProvider,
    ModelUsageNodeProperty,
)
from griptape_nodes.node_library.library_registry import (
    Library,
    LibraryMetadata,
    LibraryRegistry,
    LibrarySchema,
    NodeMetadata,
    resolve_provider_model_id,
)

_LIBRARY_NAME = "library-registry-test-library"


class _ResolveProbeNode(BaseNode):
    """Concrete BaseNode used to exercise resolve_provider_model_id."""

    def __init__(self, name: str, metadata=None) -> None:  # noqa: ANN001
        super().__init__(name=name, metadata=metadata)


@pytest.fixture(autouse=True)
def _clean_registry():  # noqa: ANN202
    """LibraryRegistry holds class-level state that survives the singleton reset fixture."""
    LibraryRegistry._clear()
    yield
    LibraryRegistry._clear()


def _register_probe_library() -> Library:
    schema = LibrarySchema(
        name=_LIBRARY_NAME,
        library_schema_version=LibrarySchema.LATEST_SCHEMA_VERSION,
        metadata=LibraryMetadata(
            author="t",
            description="d",
            library_version="1.0.0",
            engine_version="1.0.0",
            tags=[],
            declarations=[
                ModelCatalogLibraryProperty(
                    providers={
                        "provider": ModelProvider(
                            display_name="Provider",
                            models={
                                "gtc_test_alpha": Model(
                                    display_name="Alpha",
                                    provider_model_id="alpha",
                                    key_support=KeySupport.REQUIRES_GRIPTAPE_KEY,
                                ),
                                "gtc_test_no_provider_id": Model(
                                    display_name="No Provider Id",
                                    key_support=KeySupport.REQUIRES_GRIPTAPE_KEY,
                                ),
                            },
                        ),
                    },
                ),
            ],
        ),
        categories=[],
        nodes=[],
    )
    library = LibraryRegistry.generate_new_library(library_data=schema)
    library.register_new_node_type(
        _ResolveProbeNode,
        NodeMetadata(
            category="t",
            description="d",
            display_name="Probe",
            declarations=[ModelUsageNodeProperty(model_ids=["gtc_test_alpha", "gtc_test_no_provider_id"])],
        ),
    )
    return library


class TestResolveProviderModelId:
    def test_resolves_the_provider_id_of_a_declared_model(self) -> None:
        library = _register_probe_library()
        node = library.create_node(node_type=_ResolveProbeNode.__name__, name="probe")

        assert resolve_provider_model_id(node, "gtc_test_alpha") == "alpha"

    def test_returns_none_for_an_undeclared_model_id(self) -> None:
        library = _register_probe_library()
        node = library.create_node(node_type=_ResolveProbeNode.__name__, name="probe")

        assert resolve_provider_model_id(node, "gtc_never_declared") is None

    def test_returns_none_when_the_declared_entry_has_no_provider_model_id(self) -> None:
        library = _register_probe_library()
        node = library.create_node(node_type=_ResolveProbeNode.__name__, name="probe")

        assert resolve_provider_model_id(node, "gtc_test_no_provider_id") is None
