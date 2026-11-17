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

from griptape_nodes.node_library.library_registry import (
    LibraryMetadata,
    LibrarySchema,
    NodeDefinition,
    NodeMetadata,
)
from griptape_nodes.retained_mode.events.library_events import (
    LoadMetadataForAllLibrariesRequest,
    LoadMetadataForAllLibrariesResultSuccess,
    RegisterLibraryFromFileRequest,
    RegisterLibraryFromFileResultSuccess,
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


def _write_library(directory: Path, name: str, nodes: list[NodeDefinition] | None = None) -> str:
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
        nodes=nodes or [],
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


@pytest.mark.asyncio
async def test_git_lookups_run_concurrently(engine: Engine, tmp_path: Path) -> None:
    """Every library's git lookup is in flight at once rather than one after another."""
    manifests = [_write_library(tmp_path / f"lib-{i}", f"Library {i}") for i in range(LIBRARY_COUNT)]

    def get_config_value(key: str, *, default: object = None, **_: object) -> object:
        if key == LIBRARIES_TO_REGISTER_KEY:
            return manifests
        if key == LIBRARIES_TO_DOWNLOAD_KEY:
            return []
        return default

    all_lookups_started = threading.Barrier(LIBRARY_COUNT, timeout=LOOP_ANSWER_TIMEOUT_SECONDS)
    met_per_lookup: list[bool] = []

    def git_info_waiting_for_the_others(_library_path: Path) -> tuple[str | None, str | None]:
        """Stands in for slow git: completes only once every library's lookup has started."""
        try:
            all_lookups_started.wait()
        except threading.BrokenBarrierError:
            met_per_lookup.append(False)
        else:
            met_per_lookup.append(True)
        return "https://example.com/library.git", "main"

    with pytest.MonkeyPatch.context() as patcher:
        patcher.setattr(engine.config_manager, "get_config_value", get_config_value)
        patcher.setattr(metadata_loading, "get_git_info", git_info_waiting_for_the_others)
        result = await engine.library_manager.metadata_loading.load_metadata_for_all_libraries_request(
            LoadMetadataForAllLibrariesRequest()
        )

    assert isinstance(result, LoadMetadataForAllLibrariesResultSuccess)
    assert [entry.library_schema.name for entry in result.successful_libraries] == [
        f"Library {i}" for i in range(LIBRARY_COUNT)
    ]
    assert met_per_lookup == [True] * LIBRARY_COUNT


@pytest.mark.asyncio
async def test_registering_a_library_runs_no_git(engine: Engine, tmp_path: Path) -> None:
    """Registration reads only the library's schema; git details are for the request handlers."""
    library_dir = tmp_path / "lib"
    library_dir.mkdir()
    (library_dir / "plain_node.py").write_text(
        "from griptape_nodes.exe_types.node_types import DataNode\n\n\nclass PlainNode(DataNode):\n    pass\n"
    )
    node = NodeDefinition(
        class_name="PlainNode",
        file_path="plain_node.py",
        metadata=NodeMetadata(category="test", description="A node", display_name="Plain Node"),
    )
    manifest = _write_library(library_dir, "Library Without Git", nodes=[node])

    def no_git(_library_path: Path) -> tuple[str | None, str | None]:
        msg = "registering a library must not look up its git details"
        raise AssertionError(msg)

    with pytest.MonkeyPatch.context() as patcher:
        patcher.setattr(metadata_loading, "get_git_info", no_git)
        result = await engine.ahandle_request(RegisterLibraryFromFileRequest(file_path=manifest))

    assert isinstance(result, RegisterLibraryFromFileResultSuccess), result.result_details


@pytest.mark.asyncio
async def test_metadata_without_git_info_runs_no_git(engine: Engine, tmp_path: Path) -> None:
    """A caller that opts out of git details gets every schema without any git lookup."""
    manifests = [_write_library(tmp_path / f"lib-{i}", f"Library {i}") for i in range(LIBRARY_COUNT)]

    def get_config_value(key: str, *, default: object = None, **_: object) -> object:
        if key == LIBRARIES_TO_REGISTER_KEY:
            return manifests
        if key == LIBRARIES_TO_DOWNLOAD_KEY:
            return []
        return default

    def no_git(_library_path: Path) -> tuple[str | None, str | None]:
        msg = "metadata requested without git info must not look up git details"
        raise AssertionError(msg)

    with pytest.MonkeyPatch.context() as patcher:
        patcher.setattr(engine.config_manager, "get_config_value", get_config_value)
        patcher.setattr(metadata_loading, "get_git_info", no_git)
        result = await engine.library_manager.metadata_loading.load_metadata_for_all_libraries_request(
            LoadMetadataForAllLibrariesRequest(include_git_info=False)
        )

    assert isinstance(result, LoadMetadataForAllLibrariesResultSuccess)
    assert [entry.library_schema.name for entry in result.successful_libraries] == [
        f"Library {i}" for i in range(LIBRARY_COUNT)
    ]
    assert all(entry.git_remote is None and entry.git_ref is None for entry in result.successful_libraries)
