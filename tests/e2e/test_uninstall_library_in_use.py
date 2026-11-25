"""Uninstalling a library whose files are still open.

Python cannot unload code it has imported, and Windows will not delete a file that is open at
all, so the folder an uninstall wants to remove can still be in use when it gets there. The
uninstall has to reach the same end either way: the library unregistered and unloaded now, and
the folder either gone or recorded for removal at the next engine start. What it must never do
is report a failure after the config entry is already gone.

The open handle below is what makes this deterministic. On Windows it locks the folder, so the
deferred path runs; off Windows the delete succeeds. Both are asserted, so this test covers the
Windows behavior from CI without needing a Windows-only case.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from griptape_nodes.retained_mode.events.library_events import (
    RegisterLibraryFromFileRequest,
    RegisterLibraryFromFileResultSuccess,
    UninstallLibraryRequest,
    UninstallLibraryResultSuccess,
)
from griptape_nodes.retained_mode.managers.settings import LIBRARIES_TO_REGISTER_KEY

if TYPE_CHECKING:
    from collections.abc import Callable

    from griptape_nodes.retained_mode.engine import Engine

FIXTURE_LIBRARY_DIR = Path(__file__).parent / "fixtures" / "echo_library"
FIXTURE_LIBRARY_JSON_TEMPLATE = FIXTURE_LIBRARY_DIR / "griptape_nodes_library.json"
FIXTURE_NODE_FILE = FIXTURE_LIBRARY_DIR / "echo_node.py"
LIBRARY_NAME = "In Use Library"


@pytest.mark.asyncio
async def test_uninstalling_a_library_whose_files_are_open_still_finishes(
    engine: Engine, materialize_library: Callable[..., Path]
) -> None:
    """The library goes either way; only the folder's removal can wait for a restart."""
    # Inside the libraries root, or the uninstall refuses it as a folder the engine did not install.
    install_directory = engine.config_manager.resolved_libraries_root() / "in-use-library"
    library_json = materialize_library(
        install_directory,
        template=FIXTURE_LIBRARY_JSON_TEMPLATE,
        node_file=FIXTURE_NODE_FILE,
        name=LIBRARY_NAME,
    )
    register_result = engine.handle_request(RegisterLibraryFromFileRequest(file_path=str(library_json)))
    assert isinstance(register_result, RegisterLibraryFromFileResultSuccess), register_result
    # Registering loads it; the config entry is what an uninstall removes.
    engine.config_manager.set_config_value(LIBRARIES_TO_REGISTER_KEY, [str(library_json)])

    held_open = install_directory / "held_open.txt"
    held_open.write_text("held", encoding="utf-8")
    with held_open.open("r", encoding="utf-8"):
        result = await engine.ahandle_request(UninstallLibraryRequest(library_path=str(library_json)))

    # Never a failure: the config entry and the registry load are already undone by this point.
    assert isinstance(result, UninstallLibraryResultSuccess), result.result_details
    assert result.unloaded is True

    if result.directory_removal_deferred:
        # Windows: the open handle blocks the delete, so the folder waits for the next start.
        assert result.restart_required is True
        assert result.directory_deleted is False
        assert install_directory.exists()
    else:
        assert result.directory_deleted is True
        assert not install_directory.exists()
