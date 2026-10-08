"""Loading metadata for every library keeps the event loop responsive.

Each library's metadata includes its git remote and ref, which costs several git
subprocesses per library. Git process spawn is slow on Windows (~100ms), and the editor
polls `LoadMetadataForAllLibrariesRequest` repeatedly while the engine starts up, so
running those subprocesses on the event loop starved engine initialization for minutes.
"""

from __future__ import annotations

import asyncio
import json
import threading
from typing import TYPE_CHECKING

import pytest

from griptape_nodes.node_library.library_registry import LibraryMetadata, LibrarySchema
from griptape_nodes.retained_mode.events.library_events import (
    LoadMetadataForAllLibrariesRequest,
    LoadMetadataForAllLibrariesResultSuccess,
)
from griptape_nodes.retained_mode.managers.library import metadata_loading
from griptape_nodes.retained_mode.managers.settings import (
    LIBRARIES_TO_DOWNLOAD_KEY,
    LIBRARIES_TO_REGISTER_KEY,
)

if TYPE_CHECKING:
    from pathlib import Path

    from griptape_nodes.retained_mode.engine import Engine

LIBRARY_COUNT = 3
# How long a git lookup waits for the event loop to answer it. Generous, because a lookup
# running off the loop is answered at once and only one blocking the loop waits it out.
LOOP_ANSWER_TIMEOUT_SECONDS = 2.0


def _write_library(directory: Path, name: str) -> str:
    schema = LibrarySchema(
        name=name,
        library_schema_version=LibrarySchema.LATEST_SCHEMA_VERSION,
        metadata=LibraryMetadata(
            author="test",
            description=f"{name} manifest",
            library_version="0.1.0",
            engine_version="0.98.0",
            tags=[],
        ),
        categories=[],
        nodes=[],
    )
    directory.mkdir(parents=True, exist_ok=True)
    manifest = directory / "griptape_nodes_library.json"
    manifest.write_text(json.dumps(schema.model_dump(mode="json")), encoding="utf-8")
    return str(manifest)


@pytest.mark.asyncio
async def test_git_lookups_do_not_block_the_event_loop(engine: Engine, tmp_path: Path) -> None:
    """Other coroutines keep running while each library's git details are looked up."""
    manifests = [_write_library(tmp_path / f"lib-{i}", f"Library {i}") for i in range(LIBRARY_COUNT)]

    def get_config_value(key: str, *, default: object = None, **_: object) -> object:
        if key == LIBRARIES_TO_REGISTER_KEY:
            return manifests
        if key == LIBRARIES_TO_DOWNLOAD_KEY:
            return []
        return default

    loop = asyncio.get_running_loop()
    loop_answered = threading.Event()
    answered_per_lookup: list[bool] = []

    def git_info_needing_the_loop(_library_path: Path) -> tuple[str | None, str | None]:
        """Stands in for slow git: completes only if the event loop runs meanwhile."""
        loop.call_soon_threadsafe(loop_answered.set)
        answered_per_lookup.append(loop_answered.wait(timeout=LOOP_ANSWER_TIMEOUT_SECONDS))
        return "https://example.com/library.git", "main"

    with pytest.MonkeyPatch.context() as patcher:
        patcher.setattr(engine.config_manager, "get_config_value", get_config_value)
        patcher.setattr(metadata_loading, "get_git_info", git_info_needing_the_loop)
        result = await engine.library_manager.metadata_loading.load_metadata_for_all_libraries_request(
            LoadMetadataForAllLibrariesRequest()
        )

    assert isinstance(result, LoadMetadataForAllLibrariesResultSuccess)
    assert [entry.library_schema.name for entry in result.successful_libraries] == [
        f"Library {i}" for i in range(LIBRARY_COUNT)
    ]
    assert all(entry.git_ref == "main" for entry in result.successful_libraries)
    assert answered_per_lookup == [True] * LIBRARY_COUNT
