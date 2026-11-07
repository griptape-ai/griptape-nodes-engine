"""A general-purpose worker loads a library for real but does not report it as that library's worker.

The orchestrator reads a library load report as news from the library's own worker: it releases
whatever is waiting to route execution there. A general-purpose worker loads every library, so a
report from it would release a library's gate before that library's worker had loaded it.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from griptape_nodes.node_library.library_registry import LibrarySchema
from griptape_nodes.retained_mode.engine import current_engine
from griptape_nodes.retained_mode.events.library_events import (
    RegisterLibraryFromFileRequest,
    RegisterLibraryFromFileResultSuccess,
    UnloadLibraryFromRegistryRequest,
)
from griptape_nodes.retained_mode.managers.library_manager import LibraryManager
from griptape_nodes.utils.version_utils import engine_version

if TYPE_CHECKING:
    from collections.abc import Iterator

ECHO_FIXTURE = Path(__file__).parent / "fixtures" / "echo_library"


@pytest.fixture(autouse=True)
def _restore_worker_role() -> Iterator[None]:
    library_manager = current_engine().library_manager
    original = (
        library_manager._is_worker,
        library_manager._is_general_purpose_worker,
        library_manager._target_library_names,
    )
    original_path = list(sys.path)
    yield
    sys.path[:] = original_path
    (
        library_manager._is_worker,
        library_manager._is_general_purpose_worker,
        library_manager._target_library_names,
    ) = original


def _register_echo_library(tmp_path: Path, name: str) -> str:
    library_dir = tmp_path / name.replace(" ", "_")
    library_dir.mkdir(parents=True)
    schema = json.loads((ECHO_FIXTURE / "griptape_nodes_library.json").read_text())
    schema["name"] = name
    schema["library_schema_version"] = LibrarySchema.LATEST_SCHEMA_VERSION
    schema["metadata"]["engine_version"] = engine_version
    library_json = library_dir / "griptape_nodes_library.json"
    library_json.write_text(json.dumps(schema, indent=2))
    shutil.copy(ECHO_FIXTURE / "echo_node.py", library_dir / "echo_node.py")
    result = current_engine().handle_request(RegisterLibraryFromFileRequest(file_path=str(library_json)))
    assert isinstance(result, RegisterLibraryFromFileResultSuccess), getattr(result, "result_details", result)
    return str(library_json)


def test_a_general_purpose_worker_loads_the_library_without_reporting_it(tmp_path: Path) -> None:
    """It needs the library's nodes, but its load is not the one the orchestrator waits on."""
    library_manager = current_engine().library_manager
    library_manager._is_worker = True
    library_manager._is_general_purpose_worker = True
    library_manager._target_library_names = None
    report = AsyncMock()

    with patch.object(library_manager.workers, "report_library_loaded", report):
        library_json = _register_echo_library(tmp_path, "General Echo")

    info = library_manager._library_file_path_to_info[library_json]
    assert info.lifecycle_state is LibraryManager.LibraryLifecycleState.LOADED
    report.assert_not_awaited()


def test_a_library_worker_still_reports_the_library_it_serves(tmp_path: Path) -> None:
    """The guard is about the general-purpose worker only; a library's own worker reports as before."""
    library_manager = current_engine().library_manager
    library_manager._is_worker = True
    library_manager._is_general_purpose_worker = False
    library_manager._target_library_names = ["Dedicated Echo"]
    report = AsyncMock()

    with patch.object(library_manager.workers, "report_library_loaded", report):
        _register_echo_library(tmp_path, "Dedicated Echo")

    report.assert_awaited_once()
    assert report.call_args.args[0].library_name == "Dedicated Echo"


def test_registering_and_unloading_a_library_here_records_the_change(tmp_path: Path) -> None:
    """Every way a library changes mid-session ends in one of these two requests, so they are where it is noted."""
    library_manager = current_engine().library_manager
    library_manager._is_worker = False
    note = MagicMock()

    with patch.object(library_manager.workers, "note_library_set_changed", note):
        _register_echo_library(tmp_path, "Changing Echo")
        registered = note.call_count
        result = current_engine().handle_request(UnloadLibraryFromRegistryRequest(library_name="Changing Echo"))

    assert result.succeeded(), result
    assert registered == 1
    assert note.call_count == 2  # noqa: PLR2004 (once for the register, once for the unload)
