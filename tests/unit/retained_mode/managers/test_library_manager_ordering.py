"""Unit tests for deterministic library loading order in LibraryManager."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from griptape_nodes.retained_mode.events.library_events import (
    DiscoverLibrariesRequest,
    DiscoverLibrariesResultSuccess,
)
from griptape_nodes.retained_mode.managers.settings import LIBRARIES_TO_REGISTER_KEY

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

    from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes


def _config_value_side_effect(libraries: list[str]) -> Callable[..., object]:
    """Return a get_config_value stub that only answers the libraries_to_register key.

    find_files_recursive reads the discovery_max_depth setting through the same
    ConfigManager, so a blanket return_value would feed it the library list. This
    answers the library key with the provided list and defers every other key to
    the caller's own `default` (so the depth read gets its int fallback).
    """

    def get_config_value(key: str, *, default: object = None, **_: object) -> object:
        if key == LIBRARIES_TO_REGISTER_KEY:
            return libraries
        return default

    return get_config_value


class TestLibraryManagerDeterministicOrdering:
    """Test that library loading maintains deterministic order."""

    @pytest.fixture
    def temp_dir(self) -> Generator[Path, None, None]:
        """Create a temporary directory for testing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir).resolve()

    @pytest.mark.asyncio
    async def test_discover_library_files_preserves_config_order(
        self, griptape_nodes: GriptapeNodes, temp_dir: Path
    ) -> None:
        """Test that _discover_library_files preserves the order from libraries_to_register."""
        library_manager = griptape_nodes.LibraryManager()

        # Create library files in different directories (to have distinct paths)
        lib_z = temp_dir / "z_lib" / "griptape_nodes_library.json"
        lib_a = temp_dir / "a_lib" / "griptape_nodes_library.json"
        lib_m = temp_dir / "m_lib" / "griptape_nodes_library.json"
        lib_z.parent.mkdir()
        lib_a.parent.mkdir()
        lib_m.parent.mkdir()
        lib_z.write_text("{}")
        lib_a.write_text("{}")
        lib_m.write_text("{}")

        # Mock config to return libraries in specific order (z, a, m)
        config_order = [str(lib_z), str(lib_a), str(lib_m)]

        with patch.object(griptape_nodes.ConfigManager(), "get_config_value", return_value=config_order):
            result = await library_manager._discover_library_files()

            # Should preserve config order, not alphabetical
            assert [Path(entry.registration.path) for entry in result if entry.registration.path is not None] == [
                lib_z,
                lib_a,
                lib_m,
            ]
            assert all(entry.registration.enabled for entry in result)

    @pytest.mark.asyncio
    async def test_discover_library_files_handles_directories_deterministically(
        self, griptape_nodes: GriptapeNodes, temp_dir: Path
    ) -> None:
        """Test that files discovered from directories are sorted deterministically."""
        library_manager = griptape_nodes.LibraryManager()

        # Create subdirectories with library files (each needs the standard library filename)
        lib_dir = temp_dir / "libraries"
        zebra_dir = lib_dir / "zebra"
        apple_dir = lib_dir / "apple"
        banana_dir = lib_dir / "banana"
        zebra_dir.mkdir(parents=True)
        apple_dir.mkdir(parents=True)
        banana_dir.mkdir(parents=True)

        lib_z = zebra_dir / "griptape_nodes_library.json"
        lib_a = apple_dir / "griptape_nodes_library.json"
        lib_b = banana_dir / "griptape_nodes_library.json"
        lib_z.write_text("{}")
        lib_a.write_text("{}")
        lib_b.write_text("{}")

        # Mock config to point to the parent directory
        with patch.object(
            griptape_nodes.ConfigManager(),
            "get_config_value",
            side_effect=_config_value_side_effect([str(lib_dir)]),
        ):
            result = await library_manager._discover_library_files()

            # Files from directory should be sorted alphabetically by path
            assert [Path(entry.registration.path) for entry in result if entry.registration.path is not None] == [
                lib_a,
                lib_b,
                lib_z,
            ]

    @pytest.mark.asyncio
    async def test_discover_library_files_mixed_files_and_directories_preserves_order(
        self, griptape_nodes: GriptapeNodes, temp_dir: Path
    ) -> None:
        """Test that mixing direct files and directories preserves config order."""
        library_manager = griptape_nodes.LibraryManager()

        # Create direct library file in its own directory
        direct_dir = temp_dir / "direct"
        direct_dir.mkdir()
        direct_lib = direct_dir / "griptape_nodes_library.json"
        direct_lib.write_text("{}")

        # Create directory with library files in subdirectories
        lib_dir = temp_dir / "libraries"
        b_dir = lib_dir / "b_lib"
        a_dir = lib_dir / "a_lib"
        b_dir.mkdir(parents=True)
        a_dir.mkdir(parents=True)
        dir_lib_b = b_dir / "griptape_nodes_library.json"
        dir_lib_a = a_dir / "griptape_nodes_library.json"
        dir_lib_b.write_text("{}")
        dir_lib_a.write_text("{}")

        # Create another direct library file
        another_dir = temp_dir / "another"
        another_dir.mkdir()
        another_direct = another_dir / "griptape_nodes_library.json"
        another_direct.write_text("{}")

        # Config order: direct file, directory, another direct file
        config_order = [str(direct_lib), str(lib_dir), str(another_direct)]

        with patch.object(
            griptape_nodes.ConfigManager(),
            "get_config_value",
            side_effect=_config_value_side_effect(config_order),
        ):
            result = await library_manager._discover_library_files()

            # Should be: direct_lib, dir_lib_a, dir_lib_b, another_direct
            # (directory contents are sorted alphabetically by path)
            assert [Path(entry.registration.path) for entry in result if entry.registration.path is not None] == [
                direct_lib,
                dir_lib_a,
                dir_lib_b,
                another_direct,
            ]

    @pytest.mark.asyncio
    async def test_discover_library_files_deduplicates_preserving_first_occurrence(
        self, griptape_nodes: GriptapeNodes, temp_dir: Path
    ) -> None:
        """Test that duplicate libraries are removed, preserving first occurrence."""
        library_manager = griptape_nodes.LibraryManager()

        # Create library file
        lib_dir = temp_dir / "mylib"
        lib_dir.mkdir()
        lib = lib_dir / "griptape_nodes_library.json"
        lib.write_text("{}")

        # Config lists same library twice
        config_order = [str(lib), str(lib)]

        with patch.object(griptape_nodes.ConfigManager(), "get_config_value", return_value=config_order):
            result = await library_manager._discover_library_files()

            # Should only appear once
            assert [Path(entry.registration.path) for entry in result if entry.registration.path is not None] == [lib]
            assert len(result) == 1

    @pytest.mark.asyncio
    async def test_discover_libraries_request_returns_list_in_order(
        self, griptape_nodes: GriptapeNodes, temp_dir: Path
    ) -> None:
        """Test that discover_libraries_request returns libraries as a list in order."""
        library_manager = griptape_nodes.LibraryManager()

        # Create library files in separate directories
        z_dir = temp_dir / "z_lib"
        a_dir = temp_dir / "a_lib"
        z_dir.mkdir()
        a_dir.mkdir()
        lib1 = z_dir / "griptape_nodes_library.json"
        lib2 = a_dir / "griptape_nodes_library.json"
        lib1.write_text("{}")
        lib2.write_text("{}")

        # Mock config to return libraries in specific order
        config_order = [str(lib1), str(lib2)]

        with patch.object(griptape_nodes.ConfigManager(), "get_config_value", return_value=config_order):
            request = DiscoverLibrariesRequest(include_sandbox=False)
            result = await library_manager.discover_libraries_request(request)

            assert isinstance(result, DiscoverLibrariesResultSuccess)
            # Result should be a list, not a set
            assert isinstance(result.libraries_discovered, list)
            # Order should match config
            discovered_paths = [lib.path for lib in result.libraries_discovered]
            assert discovered_paths == [lib1, lib2]

    @pytest.mark.asyncio
    async def test_discover_libraries_request_deterministic_across_calls(
        self, griptape_nodes: GriptapeNodes, temp_dir: Path
    ) -> None:
        """Test that multiple calls return the same order."""
        library_manager = griptape_nodes.LibraryManager()

        # Create multiple library files with random-ish names in separate directories
        libs = []
        for name in ["z", "a", "m", "q", "b"]:
            lib_dir = temp_dir / f"{name}_lib"
            lib_dir.mkdir()
            lib = lib_dir / "griptape_nodes_library.json"
            lib.write_text("{}")
            libs.append(str(lib))

        with patch.object(griptape_nodes.ConfigManager(), "get_config_value", return_value=libs):
            request = DiscoverLibrariesRequest(include_sandbox=False)

            result1 = await library_manager.discover_libraries_request(request)
            result2 = await library_manager.discover_libraries_request(request)
            result3 = await library_manager.discover_libraries_request(request)

            assert isinstance(result1, DiscoverLibrariesResultSuccess)
            assert isinstance(result2, DiscoverLibrariesResultSuccess)
            assert isinstance(result3, DiscoverLibrariesResultSuccess)

            paths1 = [lib.path for lib in result1.libraries_discovered]
            paths2 = [lib.path for lib in result2.libraries_discovered]
            paths3 = [lib.path for lib in result3.libraries_discovered]

            # All three calls should return the same order
            assert paths1 == paths2 == paths3
