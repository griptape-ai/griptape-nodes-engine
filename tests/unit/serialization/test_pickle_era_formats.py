"""Data saved by pickle-era engines keeps loading.

Up to engine 0.102.0, parameter values were stored as pickle in three places: saved workflow
files, the flow commands embedded in PNG metadata, and the copy/paste clipboard payload. The
files in ``fixtures/`` were captured from that engine and stand in for files already on
artists' disks. Never regenerate them; a later engine writes a different format.

Every fixture holds the same node, ``Holder``, whose values are listed in ``_expected_values``.
``pickle_era_image.png`` leaves out the two library-defined values; see
``test_image_metadata_restores_library_defined_values`` for why.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from griptape.artifacts import ImageUrlArtifact
from griptape.mixins.serializable_mixin import SerializableMixin
from griptape.rules import Rule, Ruleset

from griptape_nodes.node_library.library_registry import LibraryRegistry
from griptape_nodes.retained_mode.events.flow_events import (
    CreateFlowRequest,
    CreateFlowResultSuccess,
    ExtractFlowCommandsFromImageMetadataRequest,
    ExtractFlowCommandsFromImageMetadataResultSuccess,
)
from griptape_nodes.retained_mode.events.library_events import (
    RegisterLibraryFromFileRequest,
    RegisterLibraryFromFileResultSuccess,
)
from griptape_nodes.retained_mode.events.node_events import (
    DeserializeSelectedNodesFromCommandsRequest,
    DeserializeSelectedNodesFromCommandsResultSuccess,
)
from griptape_nodes.retained_mode.events.object_events import ClearAllObjectStateRequest

if TYPE_CHECKING:
    from collections.abc import Generator
    from types import ModuleType

    from griptape_nodes.exe_types.node_types import BaseNode
    from griptape_nodes.retained_mode.engine import Engine

_FIXTURES = Path(__file__).parent / "fixtures"
_LIBRARY_JSON = _FIXTURES / "pickle_era_library" / "griptape_nodes_library.json"
_LIBRARY_MODULE_PREFIXES = (
    "griptape_nodes.node_libraries.pickle_era_fixture_library",
    "gtn_dynamic_module_legacy_values_node",
)


def _forget_fixture_library() -> None:
    """Drop the fixture library from the process-global registry and module table.

    A stale module left in ``sys.modules`` would hand unpickled values a different
    ``FixtureMode`` class than the one the next test's node module defines.
    """
    LibraryRegistry._clear()
    for module_name in list(sys.modules):
        if module_name.startswith(_LIBRARY_MODULE_PREFIXES):
            del sys.modules[module_name]


@pytest.fixture(autouse=True)
def _isolate_fixture_library() -> Generator[None, None, None]:
    _forget_fixture_library()
    yield
    _forget_fixture_library()


@pytest.fixture
def library_name(engine: Engine) -> str:
    """Register the fixture library into a clean engine."""
    engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
    result = engine.handle_request(RegisterLibraryFromFileRequest(file_path=str(_LIBRARY_JSON)))
    assert isinstance(result, RegisterLibraryFromFileResultSuccess), result
    return result.library_name


@pytest.fixture
def flow_name(engine: Engine, library_name: str) -> str:  # noqa: ARG001
    """Open an empty flow for fixtures that paste into the current context."""
    engine.context_manager.push_workflow(workflow_name="pickle_era")
    result = engine.handle_request(
        CreateFlowRequest(parent_flow_name=None, flow_name="ControlFlow_1", set_as_new_context=True)
    )
    assert isinstance(result, CreateFlowResultSuccess), result
    return result.flow_name


def _expected_values(library_module: ModuleType) -> dict[str, Any]:
    return {
        "text": "hello",
        "count": 7,
        "flag": True,
        "ratio": 0.5,
        "items": [1, "two", 3.0],
        "mapping": {"a": 1, "b": [True, None]},
        "int_keyed": {1: "one", 2: "two"},
        "pair": (1, "b"),
        "blob": b"\x00\x01\xff",
        "image": ImageUrlArtifact("https://example.com/cat.png", id="image-id", name="cat"),
        "images": [
            ImageUrlArtifact("https://example.com/a.png", id="a-id", name="a"),
            ImageUrlArtifact("https://example.com/b.png", id="b-id", name="b"),
        ],
        "ruleset": Ruleset(id="ruleset-id", name="style", rules=[Rule("Be concise")]),
        "mode": library_module.FixtureMode.SLOW,
        "custom_artifact": library_module.FixtureUrlArtifact(
            "https://example.com/model.glb", id="model-id", name="model"
        ),
    }


def _expected_output() -> ImageUrlArtifact:
    return ImageUrlArtifact("https://example.com/result.png", id="result-id", name="result")


def _assert_same(actual: Any, expected: Any) -> None:
    """Assert exact type, then content; griptape objects compare by their serialized form."""
    assert type(actual) is type(expected)
    if isinstance(expected, SerializableMixin):
        assert actual.to_dict() == expected.to_dict()
    elif isinstance(expected, list):
        assert len(actual) == len(expected)
        for actual_item, expected_item in zip(actual, expected, strict=True):
            _assert_same(actual_item, expected_item)
    else:
        assert actual == expected


_LIBRARY_DEFINED_VALUES = frozenset({"mode", "custom_artifact"})


def _assert_holder_restored(node: BaseNode, *, skip: frozenset[str] = frozenset()) -> None:
    library_module = sys.modules[type(node).__module__]
    for parameter_name, expected in _expected_values(library_module).items():
        if parameter_name in skip:
            continue
        _assert_same(node.parameter_values.get(parameter_name), expected)
    _assert_same(node.parameter_output_values.get("result"), _expected_output())


def _restore_from_image(engine: Engine, file_name: str) -> BaseNode:
    result = engine.handle_request(
        ExtractFlowCommandsFromImageMetadataRequest(file_url_or_path=str(_FIXTURES / file_name), deserialize=True)
    )
    assert isinstance(result, ExtractFlowCommandsFromImageMetadataResultSuccess), result
    return engine.node_manager.get_node_by_name(result.node_name_mappings["Holder"])


class TestPickleEraFormats:
    @pytest.mark.usefixtures("library_name")
    def test_saved_workflow_restores_every_value(self, engine: Engine) -> None:
        workflow_path = _FIXTURES / "pickle_era_workflow.py"
        exec_globals: dict[str, object] = {"__file__": str(workflow_path)}
        exec(compile(workflow_path.read_text(), str(workflow_path), "exec"), exec_globals)  # noqa: S102
        asyncio.run(exec_globals["build_workflow"]())  # type: ignore[operator]

        _assert_holder_restored(engine.node_manager.get_node_by_name("Holder"))

    @pytest.mark.usefixtures("flow_name")
    def test_image_metadata_restores_every_value(self, engine: Engine) -> None:
        node = _restore_from_image(engine, "pickle_era_image.png")

        _assert_holder_restored(node, skip=_LIBRARY_DEFINED_VALUES)

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "DATA-LOSS: image metadata pickles library-defined values under the saving process's "
            "gtn_dynamic_module_* name, which no other process can import, so the whole flow fails "
            "to restore. Saved workflows are unaffected: they patch in the stable name first. "
            "Tracked under #5442."
        ),
    )
    @pytest.mark.usefixtures("flow_name")
    def test_image_metadata_restores_library_defined_values(self, engine: Engine) -> None:
        node = _restore_from_image(engine, "pickle_era_image_library_values.png")

        _assert_holder_restored(node)

    @pytest.mark.usefixtures("flow_name")
    def test_clipboard_paste_restores_every_value(self, engine: Engine) -> None:
        clipboard = json.loads((_FIXTURES / "pickle_era_clipboard.json").read_text())
        result = engine.handle_request(
            DeserializeSelectedNodesFromCommandsRequest(
                deserialize_commands=clipboard["serialized_selected_node_commands"],
                pickled_values=clipboard["pickled_values"],
            )
        )
        assert isinstance(result, DeserializeSelectedNodesFromCommandsResultSuccess), result

        _assert_holder_restored(engine.node_manager.get_node_by_name(result.node_names[0]))
