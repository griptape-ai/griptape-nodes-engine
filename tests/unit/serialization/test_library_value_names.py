"""Values whose class comes from a node library file are named by the library's stable module."""

from __future__ import annotations

import importlib.machinery
import sys
from typing import TYPE_CHECKING

import pytest

from griptape_nodes.retained_mode.events.flow_events import (
    SerializeFlowToCommandsRequest,
    SerializeFlowToCommandsResultSuccess,
)
from griptape_nodes.retained_mode.events.library_events import (
    RegisterLibraryFromFileRequest,
    RegisterLibraryFromFileResultSuccess,
    UnloadLibraryFromRegistryRequest,
    UnloadLibraryFromRegistryResultSuccess,
)
from griptape_nodes.retained_mode.events.node_events import CreateNodeRequest, CreateNodeResultSuccess
from griptape_nodes.retained_mode.events.parameter_events import (
    SetParameterValueRequest,
    SetParameterValueResultSuccess,
)
from griptape_nodes.serialization.values import TYPE_KEY, VALUE_KEY, decode_value, encode_value

if TYPE_CHECKING:
    from pathlib import Path
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

    def test_failed_reload_keeps_the_stable_module_name(
        self, engine: Engine, library_name: str, library_module: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A library file saved with a syntax error must not strand values of its live classes."""

        def fail(_loader: object, _module: object) -> None:
            msg = "invalid syntax"
            raise SyntaxError(msg)

        file_path = library_module.__file__
        assert file_path is not None
        monkeypatch.setattr(importlib.machinery.SourceFileLoader, "exec_module", fail)

        with pytest.raises(ImportError):
            engine.library_manager.module_loading.load_module_from_file(file_path, library_name)

        assert encode_value(library_module.FixtureMode.SLOW) == {
            TYPE_KEY: f"{_STABLE_MODULE}:FixtureMode",
            VALUE_KEY: "slow",
        }

    def test_value_saves_after_its_library_reloads(
        self, engine: Engine, library_name: str, library_json: Path, library_module: ModuleType
    ) -> None:
        """Updating a library reloads it under the open workflow, whose values keep the old classes."""
        result = engine.handle_request(
            SetParameterValueRequest(node_name="Holder", parameter_name="mode", value=library_module.FixtureMode.SLOW)
        )
        assert isinstance(result, SetParameterValueResultSuccess), result
        result = engine.handle_request(UnloadLibraryFromRegistryRequest(library_name=library_name))
        assert isinstance(result, UnloadLibraryFromRegistryResultSuccess), result
        result = engine.handle_request(RegisterLibraryFromFileRequest(file_path=str(library_json)))
        assert isinstance(result, RegisterLibraryFromFileResultSuccess), result

        result = engine.handle_request(SerializeFlowToCommandsRequest())

        assert isinstance(result, SerializeFlowToCommandsResultSuccess), result
        saved_values = list(result.serialized_flow_commands.unique_parameter_uuid_to_values.values())
        assert {TYPE_KEY: f"{_STABLE_MODULE}:FixtureMode", VALUE_KEY: "slow"} in saved_values
