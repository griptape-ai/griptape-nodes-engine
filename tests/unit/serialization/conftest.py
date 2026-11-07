"""Fixtures shared by the serialization tests."""

from __future__ import annotations

import shutil
import sys
from typing import TYPE_CHECKING

import pytest

from griptape_nodes.node_library.library_registry import LibraryRegistry
from griptape_nodes.retained_mode.events.flow_events import CreateFlowRequest, CreateFlowResultSuccess
from griptape_nodes.retained_mode.events.library_events import (
    RegisterLibraryFromFileRequest,
    RegisterLibraryFromFileResultSuccess,
)
from griptape_nodes.retained_mode.events.object_events import ClearAllObjectStateRequest
from tests.unit.serialization.fixture_paths import FIXTURES

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path

    from griptape_nodes.retained_mode.engine import Engine

_LIBRARY_DIR = FIXTURES / "pickle_era_library"
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


@pytest.fixture(scope="session")
def library_json(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Copy the fixture library into a per-worker directory.

    Registering creates a ``.venv`` beside the library file, so xdist workers sharing the
    source copy race to create the same venv.
    """
    library_dir = tmp_path_factory.mktemp("pickle_era_library")
    shutil.copytree(_LIBRARY_DIR, library_dir, dirs_exist_ok=True, ignore=shutil.ignore_patterns(".venv*"))
    return library_dir / "griptape_nodes_library.json"


@pytest.fixture
def library_name(engine: Engine, library_json: Path) -> Generator[str, None, None]:
    """Register the pickle-era fixture library into a clean engine."""
    _forget_fixture_library()
    engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
    result = engine.handle_request(RegisterLibraryFromFileRequest(file_path=str(library_json)))
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
