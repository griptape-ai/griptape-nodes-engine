"""Weights are declared, not installed -- and never fetched at library load.

Libraries have been acquiring model weights three ways: bundled inside a pip-installed source
tree, downloaded by a `before_library_nodes_loaded` hook, or fetched ad hoc by node code. Only the
last runs in the right process. A declaration lets the engine own the fetch, the cache location
and the revision pin, and lets a node ask for a path.

These tests never touch the network: the fetch request is intercepted, which also lets them assert
exactly what the engine would have asked for.
"""

from __future__ import annotations

import json
import pathlib
import time
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

import griptape_nodes.retained_mode.managers.library_manager as library_manager_module
from griptape_nodes.node_library.library_registry import LibrarySchema, ModelAsset
from griptape_nodes.retained_mode.engine import current_engine
from griptape_nodes.retained_mode.events.library_events import (
    RegisterLibraryFromFileRequest,
    RegisterLibraryFromFileResultSuccess,
)
from griptape_nodes.retained_mode.events.model_events import DownloadModelRequest, DownloadModelResultSuccess
from griptape_nodes.utils.version_utils import engine_version

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path

LIBRARY_PREFIX = "Model Asset Library"


@pytest.fixture(autouse=True)
def _isolated_asset_root(tmp_path: Path) -> Generator[Path, None, None]:
    """Point the engine-owned asset cache at a scratch dir.

    The real root is under xdg_data_home, which is correct for production and wrong for a test
    suite: without this, cases leak state into each other AND write into the developer's home.
    """
    root = tmp_path / "asset_root"
    root.mkdir()
    with patch.object(library_manager_module, "xdg_data_home", return_value=root):
        yield root


def _register(tmp_path: Path, assets: dict[str, dict[str, object]] | None, name: str) -> str:
    """A minimal library declaring model assets and nothing else.

    Takes a distinct name per test: LibraryRegistry is process-global and holds the schema first
    registered under a given name, so reusing one lets a later test read an earlier declaration.
    """
    library_name = f"{LIBRARY_PREFIX} {name}"
    library_dir = tmp_path / "model_asset_library"
    library_dir.mkdir(parents=True, exist_ok=True)
    (library_dir / "node.py").write_text(
        "from griptape_nodes.exe_types.node_types import DataNode\n\n\n"
        "class AssetNode(DataNode):\n"
        "    def process(self) -> None:\n"
        "        return\n"
    )
    manifest = {
        "name": library_name,
        "library_schema_version": LibrarySchema.LATEST_SCHEMA_VERSION,
        "metadata": {
            "author": "test",
            "description": "declares model assets",
            "library_version": "0.1.0",
            "engine_version": engine_version,
            "tags": ["test"],
        },
        "categories": [{"test": {"title": "t", "description": "d", "color": "border-gray-500", "icon": "Folder"}}],
        "nodes": [
            {
                "class_name": "AssetNode",
                "file_path": "node.py",
                "metadata": {"category": "test", "description": "d", "display_name": "AssetNode"},
            }
        ],
    }
    if assets is not None:
        manifest["model_assets"] = assets
    manifest_path = library_dir / "griptape_nodes_library.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))
    result = current_engine().handle_request(RegisterLibraryFromFileRequest(file_path=str(manifest_path)))
    assert isinstance(result, RegisterLibraryFromFileResultSuccess), getattr(result, "result_details", result)
    return library_name


class TestNothingIsFetchedAtLibraryLoad:
    def test_registering_a_library_downloads_nothing(self, tmp_path: Path) -> None:
        """The whole point of declaring: loading a library must not pull gigabytes."""
        with patch.object(current_engine(), "handle_request", wraps=current_engine().handle_request) as spy:
            _register(tmp_path, {"weights": {"source": "hf:owner/repo", "revision": "abc123"}}, "load")

        downloads = [
            call for call in spy.call_args_list if isinstance(call.args[0] if call.args else None, DownloadModelRequest)
        ]
        assert downloads == [], "registering the library issued a model download"


class TestResolvingAnAsset:
    def test_it_asks_for_exactly_what_was_declared(self, tmp_path: Path) -> None:
        """Revision and file patterns are the author's pin; the engine must not drop them."""
        library = _register(
            tmp_path,
            {"weights": {"source": "hf:owner/repo", "revision": "abc123", "files": ["*.safetensors"]}},
            "declared",
        )
        captured: list[DownloadModelRequest] = []

        def fake_download(request: DownloadModelRequest) -> DownloadModelResultSuccess:
            captured.append(request)
            # Stand in for the real fetch by leaving a file where it would have written one.
            # Stand in for the real fetch by leaving a file where it would have written one.
            assert request.local_dir is not None
            local = pathlib.Path(request.local_dir)
            local.mkdir(parents=True, exist_ok=True)
            (local / "model.safetensors").write_text("weights")
            return DownloadModelResultSuccess(model_id=request.model_id, result_details="ok")

        manager = current_engine().library_manager
        with patch.object(current_engine(), "handle_request", side_effect=fake_download):
            path = manager.get_model_asset(library, "weights")

        assert len(captured) == 1
        request = captured[0]
        assert request.model_id == "owner/repo"
        assert request.revision == "abc123"
        assert request.allow_patterns == ["*.safetensors"]
        assert path.exists()
        assert (path / "model.safetensors").exists()

    def test_the_path_is_keyed_by_revision(self, tmp_path: Path) -> None:
        """Re-pinning must not overwrite the weights a saved workflow still refers to."""
        library = _register(tmp_path, {"weights": {"source": "hf:owner/repo", "revision": "abc123"}}, "revkey")
        manager = current_engine().library_manager
        schema = manager  # readability: the helper below is a static method on the manager

        first = schema._model_asset_path(library, "weights", ModelAsset(source="hf:owner/repo", revision="abc123"))
        second = schema._model_asset_path(library, "weights", ModelAsset(source="hf:owner/repo", revision="def456"))

        assert first != second
        assert first.name == "abc123"
        assert second.name == "def456"

    def test_an_already_present_asset_is_not_refetched(self, tmp_path: Path) -> None:
        library = _register(tmp_path, {"weights": {"source": "hf:owner/repo", "revision": "abc123"}}, "cached")
        manager = current_engine().library_manager

        target = manager._model_asset_path(library, "weights", ModelAsset(source="hf:owner/repo", revision="abc123"))
        target.mkdir(parents=True, exist_ok=True)
        (target / "model.safetensors").write_text("already here")
        # The completion marker is what "already present" means. Files alone do not qualify: the
        # directory is created before the download starts, so a partial fetch also leaves files.
        (target / ".griptape-nodes-complete").touch()

        with patch.object(current_engine(), "handle_request", side_effect=AssertionError("must not fetch")):
            path = manager.get_model_asset(library, "weights")

        assert path == target

    def test_a_partial_download_is_refetched_rather_than_served(self, tmp_path: Path) -> None:
        """Weights on disk without the marker are the shape an interrupted fetch leaves behind.

        Serving them handed back a truncated model that failed in a way pointing at the model
        rather than the cache, and nothing short of deleting the directory by hand recovered.
        """
        library = _register(tmp_path, {"weights": {"source": "hf:owner/repo", "revision": "abc123"}}, "partial")
        manager = current_engine().library_manager

        target = manager._model_asset_path(library, "weights", ModelAsset(source="hf:owner/repo", revision="abc123"))
        target.mkdir(parents=True, exist_ok=True)
        (target / "model.safetensors.incomplete").write_text("half a file")

        fetched: list[str] = []

        def _record(request: object) -> object:
            fetched.append(type(request).__name__)
            return DownloadModelResultSuccess(model_id="owner/repo", result_details="ok")

        with patch.object(current_engine(), "handle_request", side_effect=_record):
            manager.get_model_asset(library, "weights")

        assert fetched == ["DownloadModelRequest"], "a partial download must be refetched"
        assert (target / ".griptape-nodes-complete").exists(), "a successful fetch must mark completion"

    def test_two_nodes_asking_at_once_download_once(self, tmp_path: Path) -> None:
        """Node execution is concurrent, and the check-fetch-mark window is not atomic.

        Both callers saw no marker, both downloaded into the same directory, and the first to return
        marked it complete while the second was still writing -- so every later call short-circuited
        on the marker and got a truncated model, the exact outcome the marker exists to prevent.
        """
        library = _register(tmp_path, {"weights": {"source": "hf:owner/repo", "revision": "abc123"}}, "raced")
        manager = current_engine().library_manager
        in_flight = 0
        overlaps: list[int] = []
        fetches = 0

        def _slow_download(_request: object) -> object:
            nonlocal in_flight, fetches
            fetches += 1
            in_flight += 1
            overlaps.append(in_flight)
            time.sleep(0.15)
            in_flight -= 1
            return DownloadModelResultSuccess(model_id="owner/repo", result_details="ok")

        with (
            patch.object(current_engine(), "handle_request", side_effect=_slow_download),
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            futures = [pool.submit(manager.get_model_asset, library, "weights") for _ in range(2)]
            paths = [future.result(timeout=30) for future in futures]

        assert max(overlaps) == 1, f"two fetches overlapped in one directory: {overlaps}"
        assert fetches == 1, f"expected a single download, got {fetches}"
        assert len(set(paths)) == 1


class TestHonestFailures:
    def test_an_undeclared_asset_lists_what_is_declared(self, tmp_path: Path) -> None:
        library = _register(tmp_path, {"weights": {"source": "hf:owner/repo"}}, "undeclared")

        with pytest.raises(RuntimeError, match="declares no model asset named 'missing'") as excinfo:
            current_engine().library_manager.get_model_asset(library, "missing")

        assert "weights" in str(excinfo.value), "the message should say what IS declared"

    def test_an_unsupported_source_scheme_says_which_are_supported(self, tmp_path: Path) -> None:
        library = _register(tmp_path, {"weights": {"source": "s3://bucket/key"}}, "scheme")

        with pytest.raises(RuntimeError, match="Supported schemes"):
            current_engine().library_manager.get_model_asset(library, "weights")

    def test_a_library_with_no_declaration_at_all_still_errors_clearly(self, tmp_path: Path) -> None:
        library = _register(tmp_path, None, "nodecl")

        with pytest.raises(RuntimeError, match="declares no model asset"):
            current_engine().library_manager.get_model_asset(library, "weights")
