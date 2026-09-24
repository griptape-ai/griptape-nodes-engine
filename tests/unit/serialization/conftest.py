"""Fixtures shared by the serialization tests."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from griptape_nodes.node_library.library_registry import LibraryRegistry
from griptape_nodes.retained_mode.events.flow_events import CreateFlowRequest, CreateFlowResultSuccess
from griptape_nodes.retained_mode.events.library_events import (
    RegisterLibraryFromFileRequest,
    RegisterLibraryFromFileResultSuccess,
)
from griptape_nodes.retained_mode.events.object_events import ClearAllObjectStateRequest

if TYPE_CHECKING:
    from collections.abc import Generator

    from griptape_nodes.retained_mode.engine import Engine

FIXTURES = Path(__file__).parent / "fixtures"
_LIBRARY_JSON = FIXTURES / "pickle_era_library" / "griptape_nodes_library.json"
_LIBRARY_MODULE_PREFIXES = (
    "griptape_nodes.node_libraries.pickle_era_fixture_library",
    "gtn_dynamic_module_legacy_values_node",
)


def _forget_fixture_library() -> None:
    """Drop the fixture library from the process-global registry and module table.

    A stale module left in ``sys.modules`` would hand decoded values a different
    ``FixtureMode`` class than the one the next test's node module defines.
    """
    LibraryRegistry._clear()
    for module_name in list(sys.modules):
        if module_name.startswith(_LIBRARY_MODULE_PREFIXES):
            del sys.modules[module_name]


@pytest.fixture
def library_name(engine: Engine) -> Generator[str, None, None]:
    """Register the pickle-era fixture library into a clean engine."""
    _forget_fixture_library()
    engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
    result = engine.handle_request(RegisterLibraryFromFileRequest(file_path=str(_LIBRARY_JSON)))
    assert isinstance(result, RegisterLibraryFromFileResultSuccess), result
    yield result.library_name
    _forget_fixture_library()


@pytest.fixture
def flow_name(engine: Engine, library_name: str) -> str:  # noqa: ARG001
    """Open an empty flow in the current context."""
    engine.context_manager.push_workflow(workflow_name="serialization_fixture")
    result = engine.handle_request(
        CreateFlowRequest(parent_flow_name=None, flow_name="ControlFlow_1", set_as_new_context=True)
    )
    assert isinstance(result, CreateFlowResultSuccess), result
    return result.flow_name
