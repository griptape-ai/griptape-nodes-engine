"""Values whose class comes from a node library file are named by the library's stable module."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

import pytest

from griptape_nodes.retained_mode.events.node_events import CreateNodeRequest, CreateNodeResultSuccess
from griptape_nodes.serialization.values import TYPE_KEY, VALUE_KEY, decode_value, encode_value

if TYPE_CHECKING:
    from types import ModuleType

    from griptape_nodes.retained_mode.engine import Engine

_STABLE_MODULE = "griptape_nodes.node_libraries.pickle_era_fixture_library.legacy_values_node"


@pytest.fixture
def library_module(engine: Engine, library_name: str, flow_name: str) -> ModuleType:  # noqa: ARG001
    """Load the fixture library's node file, the way creating one of its nodes does."""
    result = engine.handle_request(
        CreateNodeRequest(node_type="LegacyValuesNode", specific_library_name=library_name, node_name="Holder")
    )
    assert isinstance(result, CreateNodeResultSuccess), result
    node = engine.node_manager.get_node_by_name("Holder")
    return sys.modules[type(node).__module__]


class TestLibraryValueNames:
    def test_library_enum_is_tagged_with_the_stable_module(self, library_module: ModuleType) -> None:
        encoded = encode_value(library_module.FixtureMode.SLOW)

        assert encoded == {TYPE_KEY: f"{_STABLE_MODULE}:FixtureMode", VALUE_KEY: "slow"}
        assert decode_value(encoded) is library_module.FixtureMode.SLOW

    def test_library_artifact_decodes_to_the_library_class(self, library_module: ModuleType) -> None:
        artifact = library_module.FixtureUrlArtifact("https://example.com/model.glb", name="model")

        restored = decode_value(encode_value(artifact))

        assert type(restored) is library_module.FixtureUrlArtifact
        assert restored.to_dict() == artifact.to_dict()
