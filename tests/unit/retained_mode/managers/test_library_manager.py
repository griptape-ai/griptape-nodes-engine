import asyncio
import logging
import sys
from collections.abc import Generator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
from griptape_nodes.exe_types.node_types import BaseNode
from griptape_nodes.node_library.library_declarations import (
    LifecycleStage,
    LifecycleStageNodeProperty,
)
from griptape_nodes.node_library.library_registry import (
    LibraryMetadata,
    LibraryRegistry,
    LibrarySchema,
    NodeMetadata,
)
from griptape_nodes.retained_mode.events.base_events import ResultDetails
from griptape_nodes.retained_mode.events.library_events import (
    DescribeNodeTypeRequest,
    DescribeNodeTypeResultFailure,
    DescribeNodeTypeResultSuccess,
    GetAllInfoForAllLibrariesRequest,
    GetAllInfoForAllLibrariesResultFailure,
    GetAllInfoForAllLibrariesResultSuccess,
    InstallLibraryDependenciesRequest,
    InstallLibraryDependenciesResultFailure,
    InstallLibraryDependenciesResultSuccess,
    ListRegisteredLibrariesRequest,
    ListRegisteredLibrariesResultSuccess,
    LoadLibrariesRequest,
    LoadLibrariesResultSuccess,
    LoadLibraryMetadataFromFileResultSuccess,
    RegisterLibraryFromFileRequest,
    RegisterLibraryFromFileResultFailure,
)
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
from griptape_nodes.retained_mode.managers.library_manager import LibraryManager as _LibraryManager
from griptape_nodes.retained_mode.managers.settings import LibraryRegistration


def _discovered(path: str, *, enabled: bool = True) -> _LibraryManager.DiscoveredLibraryEntry:
    """Test helper: build a DiscoveredLibraryEntry with `registered_path` matching `path`.

    The two paths only diverge in production when the engine resolves a workspace-relative
    or `~`-prefixed entry; tests that don't exercise resolution can keep them aligned.
    """
    return _LibraryManager.DiscoveredLibraryEntry(
        registration=LibraryRegistration(path=path, enabled=enabled),
        registered_path=path,
    )


class TestLibraryManagerLoadLibraries:
    """Test the load_libraries_request functionality in LibraryManager."""

    @pytest.mark.asyncio
    async def test_libraries_already_loaded_returns_success_without_reloading(
        self, griptape_nodes: GriptapeNodes
    ) -> None:
        """Test that when libraries are already loaded, returns success without reloading."""
        library_manager = griptape_nodes.LibraryManager()

        # Mock that libraries are already loaded and discovered libraries match loaded ones
        from griptape_nodes.node_library.library_registry import LibraryRegistry
        from griptape_nodes.retained_mode.managers.library_manager import LibraryManager

        mock_lib_info = library_manager.LibraryInfo(
            lifecycle_state=LibraryManager.LibraryLifecycleState.LOADED,
            library_path="some_lib",
            is_sandbox=False,
            library_name="SomeLib",
            library_version="1.0.0",
            fitness=LibraryManager.LibraryFitness.GOOD,
            problems=[],
        )
        mock_load_config = AsyncMock()
        mock_library = MagicMock()
        mock_library.name = "SomeLib"
        with (
            patch.object(library_manager, "_library_file_path_to_info", {"some_lib": mock_lib_info}),
            patch.object(library_manager, "_discover_library_files", return_value=[_discovered("some_lib")]),
            patch.object(library_manager, "load_all_libraries_from_config", mock_load_config),
            patch.object(LibraryRegistry, "get_library", return_value=mock_library),
        ):
            request = LoadLibrariesRequest()
            result = await library_manager.load_libraries_request(request)

            assert isinstance(result, LoadLibrariesResultSuccess)
            assert isinstance(result.result_details, ResultDetails)
            # Test that library was loaded successfully (not failed)
            assert "loaded" in result.result_details.result_details[0].message.lower()
            # Since library was already in registry, config loading shouldn't be called
            mock_load_config.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_libraries_loads_from_config_successfully(self, griptape_nodes: GriptapeNodes) -> None:
        """Test successful library loading from configuration."""
        library_manager = griptape_nodes.LibraryManager()

        # Mock empty libraries and discovered library that needs loading
        mock_load_config = AsyncMock()
        with (
            patch.object(library_manager, "_library_file_path_to_info", {}),
            patch.object(library_manager, "_discover_library_files", return_value=[_discovered("new_lib")]),
            patch.object(library_manager, "load_all_libraries_from_config", mock_load_config),
        ):
            request = LoadLibrariesRequest()
            result = await library_manager.load_libraries_request(request)

            # Can be success or failure depending on whether sandbox library exists
            # In CI without sandbox: failure (no libraries loaded)
            # Locally with sandbox: success (sandbox loaded even though new_lib failed)
            assert isinstance(result.result_details, ResultDetails)
            # Test that loading was attempted (result mentions libraries or failure)
            message = result.result_details.result_details[0].message.lower()
            assert "loaded" in message or "failed" in message
            # load_all_libraries_from_config was NOT called because libraries were discovered and loaded individually
            # (the new implementation doesn't call load_all_libraries_from_config anymore)

    @pytest.mark.asyncio
    async def test_library_loading_failure_returns_failure_result(self, griptape_nodes: GriptapeNodes) -> None:
        """Test library loading failure returns appropriate error."""
        library_manager = griptape_nodes.LibraryManager()

        # Mock empty libraries, discovered library, and failed loading
        mock_load_config = AsyncMock(side_effect=Exception("Config error"))
        with (
            patch.object(library_manager, "_library_file_path_to_info", {}),
            patch.object(library_manager, "_discover_library_files", return_value=[_discovered("new_lib")]),
            patch.object(library_manager, "load_all_libraries_from_config", mock_load_config),
        ):
            request = LoadLibrariesRequest()
            result = await library_manager.load_libraries_request(request)

            # Can be success or failure depending on whether sandbox library exists
            # In CI without sandbox: failure (no libraries loaded)
            # Locally with sandbox: success (sandbox loaded even though new_lib failed)
            assert isinstance(result.result_details, ResultDetails)
            # Test that failure was indicated in the result message
            assert "failed" in result.result_details.result_details[0].message.lower()


class TestLibraryManagerDisabledEntries:
    """Behavior when libraries_to_register entries have enabled=False."""

    @pytest.fixture
    def lib_files(self, tmp_path: Path) -> tuple[Path, Path]:
        """Two empty library JSON files in distinct directories."""
        enabled_dir = tmp_path / "enabled"
        disabled_dir = tmp_path / "disabled"
        enabled_dir.mkdir()
        disabled_dir.mkdir()
        enabled_lib = enabled_dir / "griptape_nodes_library.json"
        disabled_lib = disabled_dir / "griptape_nodes_library.json"
        enabled_lib.write_text("{}")
        disabled_lib.write_text("{}")
        return enabled_lib, disabled_lib

    def test_discover_library_files_marks_disabled_entries(
        self, griptape_nodes: GriptapeNodes, lib_files: tuple[Path, Path]
    ) -> None:
        """Object-shaped entries with enabled=False produce disabled register entries."""
        library_manager = griptape_nodes.LibraryManager()
        enabled_lib, disabled_lib = lib_files

        config = [
            str(enabled_lib),
            {"path": str(disabled_lib), "enabled": False},
        ]

        with patch.object(griptape_nodes.ConfigManager(), "get_config_value", return_value=config):
            result = library_manager._discover_library_files()

        by_path = {Path(entry.registration.path): entry.registration.enabled for entry in result}
        assert by_path[enabled_lib] is True
        assert by_path[disabled_lib] is False

    def test_discover_library_files_bare_string_defaults_to_enabled(
        self, griptape_nodes: GriptapeNodes, lib_files: tuple[Path, Path]
    ) -> None:
        """Bare path strings continue to be treated as enabled."""
        library_manager = griptape_nodes.LibraryManager()
        enabled_lib, _ = lib_files

        with patch.object(griptape_nodes.ConfigManager(), "get_config_value", return_value=[str(enabled_lib)]):
            result = library_manager._discover_library_files()

        assert len(result) == 1
        assert result[0].registration.enabled is True

    def test_discover_libraries_request_marks_disabled_lifecycle(
        self, griptape_nodes: GriptapeNodes, lib_files: tuple[Path, Path]
    ) -> None:
        """discover_libraries_request creates LibraryInfo with DISABLED lifecycle for disabled entries."""
        from griptape_nodes.retained_mode.events.library_events import DiscoverLibrariesRequest
        from griptape_nodes.retained_mode.managers.library_manager import LibraryManager

        library_manager = griptape_nodes.LibraryManager()
        enabled_lib, disabled_lib = lib_files

        config = [str(enabled_lib), {"path": str(disabled_lib), "enabled": False}]
        # Reset tracking so this test does not depend on prior state.
        library_manager._library_file_path_to_info = {}

        with patch.object(griptape_nodes.ConfigManager(), "get_config_value", return_value=config):
            result = library_manager.discover_libraries_request(DiscoverLibrariesRequest(include_sandbox=False))

        from griptape_nodes.retained_mode.events.library_events import DiscoverLibrariesResultSuccess

        assert isinstance(result, DiscoverLibrariesResultSuccess)
        states = {
            entry.path: library_manager._library_file_path_to_info[str(entry.path)].lifecycle_state
            for entry in result.libraries_discovered
        }
        assert states[enabled_lib] != LibraryManager.LibraryLifecycleState.DISABLED
        assert states[disabled_lib] == LibraryManager.LibraryLifecycleState.DISABLED
        # The discovery result also surfaces the enabled flag.
        flags = {entry.path: entry.enabled for entry in result.libraries_discovered}
        assert flags[enabled_lib] is True
        assert flags[disabled_lib] is False

    def test_invalid_entry_is_skipped_with_warning(
        self, griptape_nodes: GriptapeNodes, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Entries that are neither strings nor dicts with a path are skipped."""
        library_manager = griptape_nodes.LibraryManager()

        config = [42, {"enabled": True}]  # missing 'path', and a bare int

        with (
            patch.object(griptape_nodes.ConfigManager(), "get_config_value", return_value=config),
            caplog.at_level(logging.WARNING, logger="griptape_nodes"),
        ):
            result = library_manager._discover_library_files()

        assert result == []
        warnings = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert any("libraries_to_register" in m for m in warnings)

    def test_rediscovery_reconciles_toggled_enabled_flag(
        self, griptape_nodes: GriptapeNodes, lib_files: tuple[Path, Path]
    ) -> None:
        """Re-running discovery after a refresh updates lifecycle when the user toggles enabled.

        Refreshing libraries (ReloadAllLibrariesRequest) does not unload entries that were
        never registered with LibraryRegistry, such as DISABLED entries. The follow-up
        discovery must therefore reconcile the lifecycle state itself; otherwise a library
        flipped from disabled to enabled (or back) would never get picked up.
        """
        from griptape_nodes.retained_mode.events.library_events import DiscoverLibrariesRequest
        from griptape_nodes.retained_mode.managers.library_manager import LibraryManager

        library_manager = griptape_nodes.LibraryManager()
        first_lib, second_lib = lib_files
        # Reset tracking so this test does not depend on prior state.
        library_manager._library_file_path_to_info = {}

        # Initial discovery: first_lib enabled, second_lib disabled.
        initial_config = [str(first_lib), {"path": str(second_lib), "enabled": False}]
        with patch.object(griptape_nodes.ConfigManager(), "get_config_value", return_value=initial_config):
            library_manager.discover_libraries_request(DiscoverLibrariesRequest(include_sandbox=False))

        first_state = library_manager._library_file_path_to_info[str(first_lib)].lifecycle_state
        second_state = library_manager._library_file_path_to_info[str(second_lib)].lifecycle_state
        assert first_state != LibraryManager.LibraryLifecycleState.DISABLED
        assert second_state == LibraryManager.LibraryLifecycleState.DISABLED

        # User flips the config: first_lib disabled, second_lib enabled, then triggers refresh.
        toggled_config = [{"path": str(first_lib), "enabled": False}, str(second_lib)]
        with patch.object(griptape_nodes.ConfigManager(), "get_config_value", return_value=toggled_config):
            library_manager.discover_libraries_request(DiscoverLibrariesRequest(include_sandbox=False))

        first_state_after = library_manager._library_file_path_to_info[str(first_lib)].lifecycle_state
        second_state_after = library_manager._library_file_path_to_info[str(second_lib)].lifecycle_state
        assert first_state_after == LibraryManager.LibraryLifecycleState.DISABLED
        assert second_state_after != LibraryManager.LibraryLifecycleState.DISABLED


class TestLibraryManagerMigrateOldXdgPaths:
    """Test the _migrate_old_xdg_library_paths functionality in LibraryManager."""

    def test_removes_old_xdg_paths_and_preserves_valid_paths(self, griptape_nodes: GriptapeNodes) -> None:
        """Test that old XDG paths are removed while valid paths are preserved."""
        library_manager = griptape_nodes.LibraryManager()

        # Mock config with one old XDG path and one valid path
        old_xdg_path = "/home/user/.local/share/griptape_nodes/libraries/griptape_nodes_library"
        valid_path = "/custom/path/to/library"
        register_config = [old_xdg_path, valid_path]
        download_config = []

        mock_config_manager = MagicMock()
        mock_config_manager.get_config_value.side_effect = lambda key: (
            register_config
            if "libraries_to_register" in key
            else download_config
            if "libraries_to_download" in key
            else None
        )

        with (
            patch(
                "griptape_nodes.retained_mode.managers.library_manager.GriptapeNodes.ConfigManager",
                return_value=mock_config_manager,
            ),
            patch("griptape_nodes.utils.library_utils.xdg_data_home") as mock_xdg,
        ):
            mock_xdg.return_value = Path("/home/user/.local/share")

            library_manager._migrate_old_xdg_library_paths()

            # Verify both configs were updated
            assert mock_config_manager.set_config_value.call_count == 2  # noqa: PLR2004
            calls = mock_config_manager.set_config_value.call_args_list
            register_call = next(c for c in calls if "libraries_to_register" in c[0][0])
            assert register_call[0][1] == [valid_path]

    def test_idempotent_with_no_old_paths(self, griptape_nodes: GriptapeNodes) -> None:
        """Test that migration does nothing when config has no old XDG paths."""
        library_manager = griptape_nodes.LibraryManager()

        # Mock config with only valid paths (no old XDG paths)
        valid_paths = ["/custom/path/library1", "https://github.com/user/library@main"]

        mock_config_manager = MagicMock()
        mock_config_manager.get_config_value.return_value = valid_paths

        with (
            patch(
                "griptape_nodes.retained_mode.managers.library_manager.GriptapeNodes.ConfigManager",
                return_value=mock_config_manager,
            ),
            patch("griptape_nodes.utils.library_utils.xdg_data_home") as mock_xdg,
        ):
            mock_xdg.return_value = Path("/home/user/.local/share")

            library_manager._migrate_old_xdg_library_paths()

            # Verify config was NOT updated (no old paths to remove)
            mock_config_manager.set_config_value.assert_not_called()

    def test_handles_empty_config_gracefully(self, griptape_nodes: GriptapeNodes) -> None:
        """Test that migration returns early when config is empty."""
        library_manager = griptape_nodes.LibraryManager()

        mock_config_manager = MagicMock()
        mock_config_manager.get_config_value.return_value = []

        with patch(
            "griptape_nodes.retained_mode.managers.library_manager.GriptapeNodes.ConfigManager",
            return_value=mock_config_manager,
        ):
            library_manager._migrate_old_xdg_library_paths()

            # Verify config was NOT updated (empty config)
            mock_config_manager.set_config_value.assert_not_called()

    def test_handles_none_config_gracefully(self, griptape_nodes: GriptapeNodes) -> None:
        """Test that migration returns early when config is None."""
        library_manager = griptape_nodes.LibraryManager()

        mock_config_manager = MagicMock()
        mock_config_manager.get_config_value.return_value = None

        with patch(
            "griptape_nodes.retained_mode.managers.library_manager.GriptapeNodes.ConfigManager",
            return_value=mock_config_manager,
        ):
            library_manager._migrate_old_xdg_library_paths()

            # Verify config was NOT updated (None config)
            mock_config_manager.set_config_value.assert_not_called()

    def test_removes_all_three_old_library_paths(self, griptape_nodes: GriptapeNodes) -> None:
        """Test that all three old XDG library types are removed."""
        library_manager = griptape_nodes.LibraryManager()

        # Mock config with all three old XDG library paths
        xdg_base = "/home/user/.local/share/griptape_nodes/libraries"
        old_paths = [
            f"{xdg_base}/griptape_nodes_library/some_file.json",
            f"{xdg_base}/griptape_nodes_advanced_media_library/another.json",
            f"{xdg_base}/griptape_cloud/cloud.json",
        ]
        valid_path = "/custom/library"
        register_config = [*old_paths, valid_path]
        download_config = []

        mock_config_manager = MagicMock()
        mock_config_manager.get_config_value.side_effect = lambda key: (
            register_config
            if "libraries_to_register" in key
            else download_config
            if "libraries_to_download" in key
            else None
        )

        with (
            patch(
                "griptape_nodes.retained_mode.managers.library_manager.GriptapeNodes.ConfigManager",
                return_value=mock_config_manager,
            ),
            patch("griptape_nodes.utils.library_utils.xdg_data_home") as mock_xdg,
        ):
            mock_xdg.return_value = Path("/home/user/.local/share")

            library_manager._migrate_old_xdg_library_paths()

            # Verify all old paths removed, only valid path remains
            assert mock_config_manager.set_config_value.call_count == 2  # noqa: PLR2004
            calls = mock_config_manager.set_config_value.call_args_list
            register_call = next(c for c in calls if "libraries_to_register" in c[0][0])
            assert register_call[0][1] == [valid_path]

    def test_preserves_custom_paths_and_git_urls(self, griptape_nodes: GriptapeNodes) -> None:
        """Test that custom paths and git URLs are preserved during migration."""
        library_manager = griptape_nodes.LibraryManager()

        # Mock config with old XDG path, custom path, and git URL
        xdg_base = "/home/user/.local/share/griptape_nodes/libraries"
        old_path = f"{xdg_base}/griptape_nodes_library"
        custom_path = "/opt/custom/libraries/my_library"
        git_url = "https://github.com/user/awesome-library@stable"
        register_config = [old_path, custom_path, git_url]
        download_config = []

        mock_config_manager = MagicMock()
        mock_config_manager.get_config_value.side_effect = lambda key: (
            register_config
            if "libraries_to_register" in key
            else download_config
            if "libraries_to_download" in key
            else None
        )

        with (
            patch(
                "griptape_nodes.retained_mode.managers.library_manager.GriptapeNodes.ConfigManager",
                return_value=mock_config_manager,
            ),
            patch("griptape_nodes.utils.library_utils.xdg_data_home") as mock_xdg,
        ):
            mock_xdg.return_value = Path("/home/user/.local/share")

            library_manager._migrate_old_xdg_library_paths()

            # Verify only old XDG path removed, custom and git URL preserved
            assert mock_config_manager.set_config_value.call_count == 2  # noqa: PLR2004
            calls = mock_config_manager.set_config_value.call_args_list
            register_call = next(c for c in calls if "libraries_to_register" in c[0][0])
            assert register_call[0][1] == [custom_path, git_url]

    def test_adds_git_urls_to_downloads_when_xdg_paths_removed(self, griptape_nodes: GriptapeNodes) -> None:
        """Test that migration adds git URLs to downloads when XDG paths are removed."""
        library_manager = griptape_nodes.LibraryManager()

        # Mock config with old XDG path in register and empty downloads
        xdg_base = "/home/user/.local/share/griptape_nodes/libraries"
        old_path = f"{xdg_base}/griptape_nodes_library"
        register_config = [old_path]
        download_config = []

        mock_config_manager = MagicMock()
        mock_config_manager.get_config_value.side_effect = lambda key: (
            register_config
            if "libraries_to_register" in key
            else download_config
            if "libraries_to_download" in key
            else None
        )

        with (
            patch(
                "griptape_nodes.retained_mode.managers.library_manager.GriptapeNodes.ConfigManager",
                return_value=mock_config_manager,
            ),
            patch("griptape_nodes.utils.library_utils.xdg_data_home") as mock_xdg,
        ):
            mock_xdg.return_value = Path("/home/user/.local/share")

            library_manager._migrate_old_xdg_library_paths()

            # Verify both configs were updated
            assert mock_config_manager.set_config_value.call_count == 2  # noqa: PLR2004

            # Check that register was cleared and download was populated
            calls = mock_config_manager.set_config_value.call_args_list
            register_call = next(c for c in calls if "libraries_to_register" in c[0][0])
            download_call = next(c for c in calls if "libraries_to_download" in c[0][0])

            assert register_call[0][1] == []  # XDG path removed
            assert len(download_call[0][1]) == 1  # Git URL added
            assert "griptape-nodes-library-standard" in download_call[0][1][0]

    def test_doesnt_duplicate_existing_git_urls(self, griptape_nodes: GriptapeNodes) -> None:
        """Test that migration doesn't add URLs already in downloads."""
        library_manager = griptape_nodes.LibraryManager()

        # Mock config with XDG path in register and corresponding git URL already in downloads
        xdg_base = "/home/user/.local/share/griptape_nodes/libraries"
        old_path = f"{xdg_base}/griptape_nodes_library"
        register_config = [old_path]
        download_config = ["https://github.com/griptape-ai/griptape-nodes-library-standard@stable"]

        mock_config_manager = MagicMock()
        mock_config_manager.get_config_value.side_effect = lambda key: (
            register_config
            if "libraries_to_register" in key
            else download_config
            if "libraries_to_download" in key
            else None
        )

        with (
            patch(
                "griptape_nodes.retained_mode.managers.library_manager.GriptapeNodes.ConfigManager",
                return_value=mock_config_manager,
            ),
            patch("griptape_nodes.utils.library_utils.xdg_data_home") as mock_xdg,
        ):
            mock_xdg.return_value = Path("/home/user/.local/share")

            library_manager._migrate_old_xdg_library_paths()

            # Verify only register was updated, downloads unchanged (no duplicate)
            assert mock_config_manager.set_config_value.call_count == 1
            call_args = mock_config_manager.set_config_value.call_args
            assert "libraries_to_register" in call_args[0][0]
            assert call_args[0][1] == []

    def test_handles_multiple_libraries(self, griptape_nodes: GriptapeNodes) -> None:
        """Test migration with all three library types."""
        library_manager = griptape_nodes.LibraryManager()

        # Mock config with all 3 old XDG paths and empty downloads
        xdg_base = "/home/user/.local/share/griptape_nodes/libraries"
        old_paths = [
            f"{xdg_base}/griptape_nodes_library",
            f"{xdg_base}/griptape_nodes_advanced_media_library",
            f"{xdg_base}/griptape_cloud",
        ]
        register_config = old_paths
        download_config = []

        mock_config_manager = MagicMock()
        mock_config_manager.get_config_value.side_effect = lambda key: (
            register_config
            if "libraries_to_register" in key
            else download_config
            if "libraries_to_download" in key
            else None
        )

        with (
            patch(
                "griptape_nodes.retained_mode.managers.library_manager.GriptapeNodes.ConfigManager",
                return_value=mock_config_manager,
            ),
            patch("griptape_nodes.utils.library_utils.xdg_data_home") as mock_xdg,
        ):
            mock_xdg.return_value = Path("/home/user/.local/share")

            library_manager._migrate_old_xdg_library_paths()

            # Verify both configs were updated
            assert mock_config_manager.set_config_value.call_count == 2  # noqa: PLR2004

            # Check that all 3 git URLs were added
            calls = mock_config_manager.set_config_value.call_args_list
            download_call = next(c for c in calls if "libraries_to_download" in c[0][0])

            assert len(download_call[0][1]) == 3  # noqa: PLR2004
            assert any("griptape-nodes-library-standard" in url for url in download_call[0][1])
            assert any("griptape-nodes-library-advanced-media" in url for url in download_call[0][1])
            assert any("griptape-nodes-library-griptape-cloud" in url for url in download_call[0][1])

    def test_handles_partial_overlap(self, griptape_nodes: GriptapeNodes) -> None:
        """Test when some URLs already exist in downloads."""
        library_manager = griptape_nodes.LibraryManager()

        # Mock config with 2 XDG paths, 1 git URL already in downloads
        xdg_base = "/home/user/.local/share/griptape_nodes/libraries"
        old_paths = [
            f"{xdg_base}/griptape_nodes_library",
            f"{xdg_base}/griptape_cloud",
        ]
        register_config = old_paths
        download_config = ["https://github.com/griptape-ai/griptape-nodes-library-standard@stable"]

        mock_config_manager = MagicMock()
        mock_config_manager.get_config_value.side_effect = lambda key: (
            register_config
            if "libraries_to_register" in key
            else download_config
            if "libraries_to_download" in key
            else None
        )

        with (
            patch(
                "griptape_nodes.retained_mode.managers.library_manager.GriptapeNodes.ConfigManager",
                return_value=mock_config_manager,
            ),
            patch("griptape_nodes.utils.library_utils.xdg_data_home") as mock_xdg,
        ):
            mock_xdg.return_value = Path("/home/user/.local/share")

            library_manager._migrate_old_xdg_library_paths()

            # Verify both configs were updated
            assert mock_config_manager.set_config_value.call_count == 2  # noqa: PLR2004

            # Check that only missing git URL was added
            calls = mock_config_manager.set_config_value.call_args_list
            download_call = next(c for c in calls if "libraries_to_download" in c[0][0])

            assert len(download_call[0][1]) == 2  # Original + 1 new  # noqa: PLR2004
            assert "griptape-nodes-library-standard" in download_call[0][1][0]  # Original
            assert any("griptape-nodes-library-griptape-cloud" in url for url in download_call[0][1])


class TestLibraryManagerRegisterLibraryFromFile:
    """Test the register_library_from_file_request functionality in LibraryManager."""

    @pytest.mark.asyncio
    async def test_always_installs_dependencies_even_when_venv_exists(self, griptape_nodes: GriptapeNodes) -> None:
        """Test that dependencies are always installed on library load, even when venv already exists."""
        library_manager = griptape_nodes.LibraryManager()

        # Mock library schema with pip dependencies
        schema = MagicMock()
        schema.name = "test_lib"
        schema.metadata.library_version = "1.0.0"
        schema.metadata.dependencies.pip_dependencies = ["requests"]
        schema.advanced_library_path = None

        with (
            patch("griptape_nodes.retained_mode.managers.library_manager.Path") as mock_path,
            patch.object(library_manager, "load_library_metadata_from_file_request") as mock_load,
            # Mock that venv already exists (old code would skip installation)
            patch.object(library_manager, "_get_library_venv_path") as mock_venv,
            patch.object(library_manager, "install_library_dependencies_request") as mock_install,
            patch("griptape_nodes.retained_mode.managers.library_manager.logger"),
        ):
            mock_path.return_value.exists.return_value = True
            mock_load.return_value = LoadLibraryMetadataFromFileResultSuccess(
                library_schema=schema,
                file_path="/mock.json",
                git_remote=None,
                git_ref=None,
                enabled=True,
                result_details=ResultDetails(message="Success", level=20),
            )
            mock_venv.return_value.exists.return_value = True
            # Mock successful dependency installation
            mock_install.return_value = InstallLibraryDependenciesResultSuccess(
                library_name="test_lib", dependencies_installed=2, result_details=ResultDetails(message="OK", level=20)
            )

            await library_manager.register_library_from_file_request(
                RegisterLibraryFromFileRequest(file_path="/mock.json")
            )

            # Verify dependencies were installed despite existing venv
            mock_install.assert_called_once()

    @pytest.mark.asyncio
    async def test_dependency_installation_failure_returns_failure(self, griptape_nodes: GriptapeNodes) -> None:
        """Test that dependency installation failure returns RegisterLibraryFromFileResultFailure."""
        mgr = griptape_nodes.LibraryManager()
        schema = MagicMock()
        schema.name = "lib"
        schema.metadata.library_version = "1.0.0"
        schema.metadata.dependencies.pip_dependencies = ["req"]
        schema.advanced_library_path = None

        with (
            patch(
                "griptape_nodes.retained_mode.managers.library_manager.Path",
                return_value=MagicMock(exists=MagicMock(return_value=True)),
            ),
            patch.object(
                mgr,
                "load_library_metadata_from_file_request",
                return_value=LoadLibraryMetadataFromFileResultSuccess(
                    library_schema=schema,
                    file_path="/f",
                    git_remote=None,
                    git_ref=None,
                    enabled=True,
                    result_details=ResultDetails(message="OK", level=20),
                ),
            ),
            patch.object(mgr, "_get_library_venv_path", return_value=MagicMock(exists=MagicMock(return_value=True))),
            # Mock failed dependency installation
            patch.object(
                mgr,
                "install_library_dependencies_request",
                return_value=InstallLibraryDependenciesResultFailure(result_details="Install failed"),
            ),
        ):
            result = await mgr.register_library_from_file_request(RegisterLibraryFromFileRequest(file_path="/f"))

            # Verify failure result with expected error message
            assert isinstance(result, RegisterLibraryFromFileResultFailure)
            assert "Install failed" in str(result.result_details)


class TestLibraryManagerInstallLibraryDependencies:
    """Tests for install_library_dependencies_request."""

    def _metadata_result(self, schema: MagicMock) -> LoadLibraryMetadataFromFileResultSuccess:
        return LoadLibraryMetadataFromFileResultSuccess(
            library_schema=schema,
            file_path="/mock.json",
            git_remote=None,
            git_ref=None,
            enabled=True,
            result_details=ResultDetails(message="OK", level=20),
        )

    @pytest.mark.asyncio
    async def test_creates_venv_when_pip_dependencies_is_empty(self, griptape_nodes: GriptapeNodes) -> None:
        """Test that the venv is created even when pip_dependencies is empty."""
        mgr = griptape_nodes.LibraryManager()
        schema = MagicMock()
        schema.name = "test_lib"
        schema.metadata.library_version = "1.0.0"
        schema.metadata.dependencies.pip_dependencies = []
        schema.metadata.dependencies.pip_install_flags = []
        mock_python_path = MagicMock()

        with (
            patch.object(mgr, "load_library_metadata_from_file_request", return_value=self._metadata_result(schema)),
            patch.object(mgr, "_get_library_venv_path", return_value=MagicMock()),
            patch.object(
                mgr, "_init_library_venv", new_callable=AsyncMock, return_value=mock_python_path
            ) as mock_init_venv,
            patch.object(mgr, "_can_write_to_venv_location", return_value=True),
            patch(
                "griptape_nodes.retained_mode.managers.library_manager.OSManager.check_available_disk_space",
                return_value=True,
            ),
            patch.object(griptape_nodes.ConfigManager(), "get_config_value", return_value=5.0),
        ):
            result = await mgr.install_library_dependencies_request(
                InstallLibraryDependenciesRequest(library_file_path="/mock.json")
            )

        mock_init_venv.assert_called_once()
        assert isinstance(result, InstallLibraryDependenciesResultSuccess)
        assert result.dependencies_installed == 0

    @pytest.mark.asyncio
    async def test_creates_venv_when_dependencies_is_none(self, griptape_nodes: GriptapeNodes) -> None:
        """Test that the venv is created even when the dependencies section is absent."""
        mgr = griptape_nodes.LibraryManager()
        schema = MagicMock()
        schema.name = "test_lib"
        schema.metadata.library_version = "1.0.0"
        schema.metadata.dependencies = None
        mock_python_path = MagicMock()

        with (
            patch.object(mgr, "load_library_metadata_from_file_request", return_value=self._metadata_result(schema)),
            patch.object(mgr, "_get_library_venv_path", return_value=MagicMock()),
            patch.object(
                mgr, "_init_library_venv", new_callable=AsyncMock, return_value=mock_python_path
            ) as mock_init_venv,
            patch.object(mgr, "_can_write_to_venv_location", return_value=True),
            patch(
                "griptape_nodes.retained_mode.managers.library_manager.OSManager.check_available_disk_space",
                return_value=True,
            ),
            patch.object(griptape_nodes.ConfigManager(), "get_config_value", return_value=5.0),
        ):
            result = await mgr.install_library_dependencies_request(
                InstallLibraryDependenciesRequest(library_file_path="/mock.json")
            )

        mock_init_venv.assert_called_once()
        assert isinstance(result, InstallLibraryDependenciesResultSuccess)
        assert result.dependencies_installed == 0

    @pytest.mark.asyncio
    async def test_returns_failure_when_venv_creation_fails_with_no_deps(self, griptape_nodes: GriptapeNodes) -> None:
        """Test that venv creation failure returns failure even when pip_dependencies is empty."""
        mgr = griptape_nodes.LibraryManager()
        schema = MagicMock()
        schema.name = "test_lib"
        schema.metadata.library_version = "1.0.0"
        schema.metadata.dependencies.pip_dependencies = []
        schema.metadata.dependencies.pip_install_flags = []

        with (
            patch.object(mgr, "load_library_metadata_from_file_request", return_value=self._metadata_result(schema)),
            patch.object(mgr, "_get_library_venv_path", return_value=MagicMock()),
            patch.object(mgr, "_init_library_venv", new_callable=AsyncMock, side_effect=RuntimeError("disk full")),
        ):
            result = await mgr.install_library_dependencies_request(
                InstallLibraryDependenciesRequest(library_file_path="/mock.json")
            )

        assert isinstance(result, InstallLibraryDependenciesResultFailure)
        assert "disk full" in str(result.result_details)

    @pytest.mark.asyncio
    async def test_returns_failure_when_venv_unwritable_with_no_deps(self, griptape_nodes: GriptapeNodes) -> None:
        """Test that an unwritable venv returns failure even when pip_dependencies is empty."""
        mgr = griptape_nodes.LibraryManager()
        schema = MagicMock()
        schema.name = "test_lib"
        schema.metadata.library_version = "1.0.0"
        schema.metadata.dependencies.pip_dependencies = []
        schema.metadata.dependencies.pip_install_flags = []

        with (
            patch.object(mgr, "load_library_metadata_from_file_request", return_value=self._metadata_result(schema)),
            patch.object(mgr, "_get_library_venv_path", return_value=MagicMock()),
            patch.object(mgr, "_init_library_venv", new_callable=AsyncMock, return_value=MagicMock()),
            patch.object(mgr, "_can_write_to_venv_location", return_value=False),
        ):
            result = await mgr.install_library_dependencies_request(
                InstallLibraryDependenciesRequest(library_file_path="/mock.json")
            )

        assert isinstance(result, InstallLibraryDependenciesResultFailure)

    @pytest.mark.asyncio
    async def test_returns_failure_when_insufficient_disk_space_with_no_deps(
        self, griptape_nodes: GriptapeNodes
    ) -> None:
        """Test that insufficient disk space returns failure even when pip_dependencies is empty."""
        mgr = griptape_nodes.LibraryManager()
        schema = MagicMock()
        schema.name = "test_lib"
        schema.metadata.library_version = "1.0.0"
        schema.metadata.dependencies.pip_dependencies = []
        schema.metadata.dependencies.pip_install_flags = []

        with (
            patch.object(mgr, "load_library_metadata_from_file_request", return_value=self._metadata_result(schema)),
            patch.object(mgr, "_get_library_venv_path", return_value=MagicMock()),
            patch.object(mgr, "_init_library_venv", new_callable=AsyncMock, return_value=MagicMock()),
            patch.object(mgr, "_can_write_to_venv_location", return_value=True),
            patch(
                "griptape_nodes.retained_mode.managers.library_manager.OSManager.check_available_disk_space",
                return_value=False,
            ),
            patch(
                "griptape_nodes.retained_mode.managers.library_manager.OSManager.format_disk_space_error",
                return_value="not enough space",
            ),
            patch.object(griptape_nodes.ConfigManager(), "get_config_value", return_value=5.0),
        ):
            result = await mgr.install_library_dependencies_request(
                InstallLibraryDependenciesRequest(library_file_path="/mock.json")
            )

        assert isinstance(result, InstallLibraryDependenciesResultFailure)


def _fake_config_value(key: str, **_: object) -> object:
    """Return realistic values for config keys touched by venv initialization."""
    if key == "log_level":
        return "INFO"
    if key == "minimum_disk_space_gb_libraries":
        return 5.0
    return None


class TestLibraryManagerVenvHealth:
    """Tests for broken-venv recovery in _init_library_venv."""

    @staticmethod
    def _make_functional_venv(venv_path: Path) -> Path:
        """Create a directory layout that mimics a working venv on the current platform."""
        venv_path.mkdir(parents=True, exist_ok=True)
        (venv_path / "pyvenv.cfg").write_text("home = /fake\n")
        if sys.platform == "win32":
            python_dir = venv_path / "Scripts"
            python_path = python_dir / "python.exe"
        else:
            python_dir = venv_path / "bin"
            python_path = python_dir / "python"
        python_dir.mkdir(parents=True, exist_ok=True)
        python_path.write_text("")
        return python_path

    @pytest.mark.asyncio
    async def test_init_reuses_functional_venv_without_running_uv(
        self, griptape_nodes: GriptapeNodes, tmp_path: Path
    ) -> None:
        mgr = griptape_nodes.LibraryManager()
        venv_path = tmp_path / ".venv"
        expected_python = self._make_functional_venv(venv_path)

        with (
            patch(
                "griptape_nodes.retained_mode.managers.library_manager.subprocess_run",
                new_callable=AsyncMock,
            ) as mock_subprocess,
            patch("griptape_nodes.retained_mode.managers.library_manager.find_uv_bin") as mock_find_uv,
        ):
            python_path = await mgr._init_library_venv(venv_path)

        assert python_path == expected_python
        mock_subprocess.assert_not_called()
        mock_find_uv.assert_not_called()
        assert (venv_path / "pyvenv.cfg").exists()

    @pytest.mark.asyncio
    async def test_init_recreates_broken_venv(self, griptape_nodes: GriptapeNodes, tmp_path: Path) -> None:
        """A directory at the venv path that is missing the python executable must be recreated."""
        mgr = griptape_nodes.LibraryManager()
        venv_path = tmp_path / ".venv"
        venv_path.mkdir()
        (venv_path / "pyvenv.cfg").write_text("home = /fake\n")
        # Leave a stray file behind to prove the directory was wiped
        (venv_path / "stray.txt").write_text("old")

        recreated_python_path: dict[str, Path] = {}

        async def fake_subprocess_run(args: list[str], **_: object) -> MagicMock:
            recreated_venv = Path(args[2])
            recreated_python_path["path"] = self._make_functional_venv(recreated_venv)
            return MagicMock()

        with (
            patch(
                "griptape_nodes.retained_mode.managers.library_manager.subprocess_run",
                side_effect=fake_subprocess_run,
            ) as mock_subprocess,
            patch(
                "griptape_nodes.retained_mode.managers.library_manager.find_uv_bin",
                return_value="/fake/uv",
            ),
            patch(
                "griptape_nodes.retained_mode.managers.library_manager.OSManager.check_available_disk_space",
                return_value=True,
            ),
            patch.object(griptape_nodes.ConfigManager(), "get_config_value", side_effect=_fake_config_value),
        ):
            python_path = await mgr._init_library_venv(venv_path)

        mock_subprocess.assert_called_once()
        assert python_path == recreated_python_path["path"]
        assert not (venv_path / "stray.txt").exists()

    @pytest.mark.asyncio
    async def test_init_creates_venv_when_directory_absent(self, griptape_nodes: GriptapeNodes, tmp_path: Path) -> None:
        mgr = griptape_nodes.LibraryManager()
        venv_path = tmp_path / ".venv"

        async def fake_subprocess_run(args: list[str], **_: object) -> MagicMock:
            self._make_functional_venv(Path(args[2]))
            return MagicMock()

        with (
            patch(
                "griptape_nodes.retained_mode.managers.library_manager.subprocess_run",
                side_effect=fake_subprocess_run,
            ) as mock_subprocess,
            patch(
                "griptape_nodes.retained_mode.managers.library_manager.find_uv_bin",
                return_value="/fake/uv",
            ),
            patch(
                "griptape_nodes.retained_mode.managers.library_manager.OSManager.check_available_disk_space",
                return_value=True,
            ),
            patch.object(griptape_nodes.ConfigManager(), "get_config_value", side_effect=_fake_config_value),
        ):
            python_path = await mgr._init_library_venv(venv_path)

        mock_subprocess.assert_called_once()
        assert python_path.exists()
        assert (venv_path / "pyvenv.cfg").exists()


class TestListRegisteredLibraries:
    """Test the on_list_registered_libraries_request functionality in LibraryManager."""

    @pytest.mark.asyncio
    async def test_waits_for_loading_complete_before_returning_libraries(self, griptape_nodes: GriptapeNodes) -> None:
        """Test that the handler blocks until _libraries_loading_complete is set."""
        library_manager = griptape_nodes.LibraryManager()

        # Ensure the event is not set so the handler will block
        library_manager._libraries_loading_complete.clear()

        mock_libraries = ["LibA", "LibB"]

        with patch.object(LibraryRegistry, "list_libraries", return_value=mock_libraries):
            request = ListRegisteredLibrariesRequest()
            task = asyncio.create_task(library_manager.on_list_registered_libraries_request(request))

            # Yield control so the task can start and block on the event
            await asyncio.sleep(0)

            # The task should still be waiting because the event is not set
            assert not task.done()

            # Signal that loading is complete
            library_manager._libraries_loading_complete.set()

            result = await task

        assert isinstance(result, ListRegisteredLibrariesResultSuccess)
        assert result.libraries == mock_libraries

    @pytest.mark.asyncio
    async def test_returns_library_list_when_loading_already_complete(self, griptape_nodes: GriptapeNodes) -> None:
        """Test that the handler returns the library list immediately when loading is already done."""
        library_manager = griptape_nodes.LibraryManager()

        # Simulate loading already finished
        library_manager._libraries_loading_complete.set()

        mock_libraries = ["LibA", "LibB", "LibC"]

        with patch.object(LibraryRegistry, "list_libraries", return_value=mock_libraries):
            request = ListRegisteredLibrariesRequest()
            result = await library_manager.on_list_registered_libraries_request(request)

        assert isinstance(result, ListRegisteredLibrariesResultSuccess)
        assert result.libraries == mock_libraries

    @pytest.mark.asyncio
    async def test_returns_copy_of_library_list(self, griptape_nodes: GriptapeNodes) -> None:
        """Test that the returned library list is a copy and not the original reference."""
        library_manager = griptape_nodes.LibraryManager()
        library_manager._libraries_loading_complete.set()

        mock_libraries = ["LibA"]

        with patch.object(LibraryRegistry, "list_libraries", return_value=mock_libraries):
            request = ListRegisteredLibrariesRequest()
            result = await library_manager.on_list_registered_libraries_request(request)

        assert isinstance(result, ListRegisteredLibrariesResultSuccess)
        # Mutating the result should not affect the original list
        result.libraries.append("LibB")
        assert mock_libraries == ["LibA"]


class TestGetAllInfoForAllLibraries:
    """Test the get_all_info_for_all_libraries_request functionality in LibraryManager."""

    def test_calls_library_registry_directly(self, griptape_nodes: GriptapeNodes) -> None:
        """Test that the method reads libraries from LibraryRegistry without going through on_list_registered_libraries_request."""
        library_manager = griptape_nodes.LibraryManager()

        with (
            patch.object(LibraryRegistry, "list_libraries", return_value=[]) as mock_list,
            patch.object(library_manager, "on_list_registered_libraries_request") as mock_handler,
        ):
            request = GetAllInfoForAllLibrariesRequest()
            result = library_manager.get_all_info_for_all_libraries_request(request)

        mock_list.assert_called_once()
        mock_handler.assert_not_called()
        assert isinstance(result, GetAllInfoForAllLibrariesResultSuccess)

    def test_returns_failure_when_individual_library_info_fails(self, griptape_nodes: GriptapeNodes) -> None:
        """Test that the method returns failure when retrieving info for a library fails."""
        library_manager = griptape_nodes.LibraryManager()

        mock_failure = MagicMock()
        mock_failure.succeeded.return_value = False

        with (
            patch.object(LibraryRegistry, "list_libraries", return_value=["BadLib"]),
            patch.object(library_manager, "get_all_info_for_library_request", return_value=mock_failure),
        ):
            request = GetAllInfoForAllLibrariesRequest()
            result = library_manager.get_all_info_for_all_libraries_request(request)

        assert isinstance(result, GetAllInfoForAllLibrariesResultFailure)
        assert "BadLib" in str(result.result_details)


class TestAddLibraryPathsToSysPath:
    """Test the _add_library_paths_to_sys_path helper method."""

    @pytest.mark.asyncio
    async def test_adds_base_dir_to_sys_path(self, griptape_nodes: GriptapeNodes) -> None:
        """Test that the library base directory is added to sys.path."""
        library_manager = griptape_nodes.LibraryManager()
        base_dir = Path("/fake/library/dir")

        mock_anyio_path = MagicMock()
        mock_anyio_path.return_value.exists = AsyncMock(return_value=False)

        original_sys_path = sys.path.copy()
        try:
            with (
                patch.object(library_manager, "_get_library_venv_path", return_value=Path("/fake/venv")),
                patch("griptape_nodes.retained_mode.managers.library_manager.anyio.Path", mock_anyio_path),
            ):
                await library_manager._add_library_paths_to_sys_path("test_lib", "/fake/lib.json", base_dir)

            assert str(base_dir) in sys.path
        finally:
            sys.path[:] = original_sys_path

    @pytest.mark.asyncio
    async def test_adds_venv_site_packages_when_venv_exists(self, griptape_nodes: GriptapeNodes) -> None:
        """Test that venv site-packages are added to sys.path when the venv exists."""
        library_manager = griptape_nodes.LibraryManager()
        base_dir = Path("/fake/library/dir")
        venv_path = Path("/fake/library/dir/.venv")
        fake_site_packages = str(Path("/fake/library/dir/.venv/lib/python3.12/site-packages"))

        mock_anyio_path = MagicMock()
        mock_anyio_path.return_value.exists = AsyncMock(return_value=True)

        original_sys_path = sys.path.copy()
        try:
            with (
                patch.object(library_manager, "_get_library_venv_path", return_value=venv_path),
                patch("griptape_nodes.retained_mode.managers.library_manager.anyio.Path", mock_anyio_path),
                patch(
                    "griptape_nodes.retained_mode.managers.library_manager.sysconfig.get_path",
                    return_value=fake_site_packages,
                ),
            ):
                await library_manager._add_library_paths_to_sys_path("test_lib", "/fake/lib.json", base_dir)

            assert fake_site_packages in sys.path
            assert str(base_dir) in sys.path
        finally:
            sys.path[:] = original_sys_path

    @pytest.mark.asyncio
    async def test_skips_venv_when_venv_does_not_exist(self, griptape_nodes: GriptapeNodes) -> None:
        """Test that venv site-packages are NOT added when the venv doesn't exist."""
        library_manager = griptape_nodes.LibraryManager()
        base_dir = Path("/fake/library/dir")
        venv_path = Path("/fake/library/dir/.venv")

        mock_anyio_path = MagicMock()
        mock_anyio_path.return_value.exists = AsyncMock(return_value=False)

        original_sys_path = sys.path.copy()
        try:
            with (
                patch.object(library_manager, "_get_library_venv_path", return_value=venv_path),
                patch("griptape_nodes.retained_mode.managers.library_manager.anyio.Path", mock_anyio_path),
                patch("griptape_nodes.retained_mode.managers.library_manager.sysconfig.get_path") as mock_get_path,
            ):
                await library_manager._add_library_paths_to_sys_path("test_lib", "/fake/lib.json", base_dir)

            # sysconfig.get_path should not have been called since venv doesn't exist
            mock_get_path.assert_not_called()
            assert str(base_dir) in sys.path
        finally:
            sys.path[:] = original_sys_path


class TestRegisterSandboxNodeFromSourceRequest:
    """Tests for LibraryManager.register_sandbox_node_from_source_request."""

    _LIBRARY_NAME = "Sandbox Library"
    _FILE_NAME = "probe_sandbox_node.py"
    _SOURCE_OK = (
        "from griptape_nodes.exe_types.node_types import BaseNode\n"
        "\n"
        "class ProbeSandboxNode(BaseNode):\n"
        "    def process(self) -> None:  # noqa: D401\n"
        '        """Probe."""\n'
        "        return None\n"
    )

    @pytest.fixture(autouse=True)
    def _isolate_registry_and_config(
        self,
        griptape_nodes: GriptapeNodes,
        tmp_path: Path,
    ) -> Generator[Path, None, None]:
        """Configure a temp sandbox directory + register the Sandbox Library for this test.

        The Sandbox Library is normally created during engine startup. Our tests start from a
        bare engine, so we recreate the minimal state the handler expects.

        We stub `_get_sandbox_directory` rather than round-tripping `set_config_value`, which
        calls `load_configs` and reads the on-disk USER_CONFIG_PATH. The conftest patches
        USER_CONFIG_PATH to an empty file, so config-layer writes get clobbered between the
        fixture and the handler call. Stubbing the resolver keeps the test focused on handler
        behaviour, not config serialisation.
        """
        from unittest.mock import patch

        from griptape_nodes.node_library.library_registry import (
            CategoryDefinition,
        )
        from griptape_nodes.node_library.library_registry import (
            LibraryMetadata as _LibraryMetadata,
        )
        from griptape_nodes.node_library.library_registry import (
            LibrarySchema as _LibrarySchema,
        )
        from griptape_nodes.retained_mode.managers.library_manager import (
            LibraryManager as _LibraryManager,
        )

        LibraryRegistry._libraries.clear()
        LibraryRegistry._node_aliases.clear()
        LibraryRegistry._collision_node_names_to_library_names.clear()
        LibraryRegistry._registered_widgets.clear()

        sandbox_dir = tmp_path / "sandbox"
        sandbox_dir.mkdir()

        # Stand up a minimal Sandbox Library so the handler has somewhere to register into.
        sandbox_schema = _LibrarySchema(
            name=_LibraryManager.SANDBOX_LIBRARY_NAME,
            library_schema_version=_LibrarySchema.LATEST_SCHEMA_VERSION,
            metadata=_LibraryMetadata(
                author="test",
                description="test sandbox",
                library_version="1.0.0",
                engine_version="1.0.0",
                tags=[],
            ),
            categories=[
                {
                    _LibraryManager.SANDBOX_CATEGORY_NAME: CategoryDefinition(
                        title="Sandbox",
                        description="test",
                        color="#000",
                        icon="Folder",
                    )
                }
            ],
            nodes=[],
        )
        LibraryRegistry.generate_new_library(library_data=sandbox_schema)

        library_manager = griptape_nodes.LibraryManager()
        # Default: return the tmp sandbox. Individual tests that need the "not configured"
        # branch override via their own patch.
        with patch.object(library_manager, "_get_sandbox_directory", return_value=sandbox_dir):
            try:
                yield sandbox_dir
            finally:
                LibraryRegistry._libraries.clear()
                LibraryRegistry._node_aliases.clear()
                LibraryRegistry._collision_node_names_to_library_names.clear()
                LibraryRegistry._registered_widgets.clear()

    def test_imports_existing_file_and_registers_node_type(
        self,
        griptape_nodes: GriptapeNodes,
        _isolate_registry_and_config: Path,  # noqa: PT019 - value is used to locate the source file
    ) -> None:
        from griptape_nodes.retained_mode.events.library_events import (
            RegisterSandboxNodeFromSourceRequest,
            RegisterSandboxNodeFromSourceResultSuccess,
        )

        library_manager = griptape_nodes.LibraryManager()
        sandbox_dir = _isolate_registry_and_config
        source_file = sandbox_dir / self._FILE_NAME
        source_file.write_text(self._SOURCE_OK)

        result = library_manager.register_sandbox_node_from_source_request(
            RegisterSandboxNodeFromSourceRequest(file_path=str(source_file))
        )

        assert isinstance(result, RegisterSandboxNodeFromSourceResultSuccess)
        assert result.registered_class_names == ["ProbeSandboxNode"]
        assert result.replaced_class_names == []
        assert result.library_name == self._LIBRARY_NAME
        # Class is now registered and retrievable via the registry.
        assert LibraryRegistry.get_library(self._LIBRARY_NAME).has_node_type("ProbeSandboxNode")

    def test_accepts_path_relative_to_sandbox_directory(
        self,
        griptape_nodes: GriptapeNodes,
        _isolate_registry_and_config: Path,  # noqa: PT019 - value is used to locate the source file
    ) -> None:
        from griptape_nodes.retained_mode.events.library_events import (
            RegisterSandboxNodeFromSourceRequest,
            RegisterSandboxNodeFromSourceResultSuccess,
        )

        library_manager = griptape_nodes.LibraryManager()
        sandbox_dir = _isolate_registry_and_config
        (sandbox_dir / self._FILE_NAME).write_text(self._SOURCE_OK)

        # Bare filename, no directory component: must resolve under the sandbox dir.
        result = library_manager.register_sandbox_node_from_source_request(
            RegisterSandboxNodeFromSourceRequest(file_path=self._FILE_NAME)
        )

        assert isinstance(result, RegisterSandboxNodeFromSourceResultSuccess)
        assert result.registered_class_names == ["ProbeSandboxNode"]

    def test_replace_if_exists_swaps_the_old_class(
        self,
        griptape_nodes: GriptapeNodes,
        _isolate_registry_and_config: Path,  # noqa: PT019 - value is used to locate the source file
    ) -> None:
        from griptape_nodes.retained_mode.events.library_events import (
            RegisterSandboxNodeFromSourceRequest,
            RegisterSandboxNodeFromSourceResultSuccess,
        )

        library_manager = griptape_nodes.LibraryManager()
        sandbox_dir = _isolate_registry_and_config
        source_file = sandbox_dir / self._FILE_NAME
        source_file.write_text(self._SOURCE_OK)

        # First registration: baseline.
        first = library_manager.register_sandbox_node_from_source_request(
            RegisterSandboxNodeFromSourceRequest(file_path=str(source_file), replace_if_exists=True)
        )
        assert isinstance(first, RegisterSandboxNodeFromSourceResultSuccess)
        assert first.replaced_class_names == []

        # Second registration of the same class name should report the prior was replaced.
        second = library_manager.register_sandbox_node_from_source_request(
            RegisterSandboxNodeFromSourceRequest(file_path=str(source_file), replace_if_exists=True)
        )
        assert isinstance(second, RegisterSandboxNodeFromSourceResultSuccess)
        assert second.replaced_class_names == ["ProbeSandboxNode"]

    def test_fails_when_sandbox_directory_is_not_configured(
        self,
        griptape_nodes: GriptapeNodes,
        _isolate_registry_and_config: Path,  # noqa: PT019 - fixture installs the default sandbox stub we override here
    ) -> None:
        from unittest.mock import patch

        from griptape_nodes.retained_mode.events.library_events import (
            RegisterSandboxNodeFromSourceRequest,
            RegisterSandboxNodeFromSourceResultFailure,
        )

        library_manager = griptape_nodes.LibraryManager()
        # Override the fixture's default stub so the resolver returns None, simulating the
        # "no sandbox configured" case.
        with patch.object(library_manager, "_get_sandbox_directory", return_value=None):
            result = library_manager.register_sandbox_node_from_source_request(
                RegisterSandboxNodeFromSourceRequest(file_path=self._FILE_NAME)
            )

        assert isinstance(result, RegisterSandboxNodeFromSourceResultFailure)
        assert "sandbox_library_directory" in str(result.result_details)

    def test_rejects_paths_outside_sandbox_or_with_wrong_extension(
        self,
        griptape_nodes: GriptapeNodes,
        _isolate_registry_and_config: Path,  # noqa: PT019 - value is used to seed source files
        tmp_path: Path,
    ) -> None:
        from griptape_nodes.retained_mode.events.library_events import (
            RegisterSandboxNodeFromSourceRequest,
            RegisterSandboxNodeFromSourceResultFailure,
        )

        library_manager = griptape_nodes.LibraryManager()
        sandbox_dir = _isolate_registry_and_config

        # Create a real file outside the sandbox so the failure is about containment, not
        # about the file being missing.
        outside = tmp_path / "outside.py"
        outside.write_text(self._SOURCE_OK)

        # Wrong extension: write a real file inside the sandbox so the failure is purely
        # about the suffix check, not about existence.
        wrong_ext = sandbox_dir / "probe.txt"
        wrong_ext.write_text(self._SOURCE_OK)

        # Escape attempt: a relative path with `..` resolves outside the sandbox dir.
        escape_target = tmp_path / "escape.py"
        escape_target.write_text(self._SOURCE_OK)

        bad_paths = [str(outside), str(wrong_ext), "../escape.py"]
        for bad_path in bad_paths:
            result = library_manager.register_sandbox_node_from_source_request(
                RegisterSandboxNodeFromSourceRequest(file_path=bad_path)
            )
            assert isinstance(result, RegisterSandboxNodeFromSourceResultFailure), bad_path

    def test_fails_when_file_does_not_exist(
        self,
        griptape_nodes: GriptapeNodes,
        _isolate_registry_and_config: Path,  # noqa: PT019 - fixture installs the sandbox stub
    ) -> None:
        from griptape_nodes.retained_mode.events.library_events import (
            RegisterSandboxNodeFromSourceRequest,
            RegisterSandboxNodeFromSourceResultFailure,
        )

        library_manager = griptape_nodes.LibraryManager()

        result = library_manager.register_sandbox_node_from_source_request(
            RegisterSandboxNodeFromSourceRequest(file_path="never_written.py")
        )

        assert isinstance(result, RegisterSandboxNodeFromSourceResultFailure)
        assert "never_written.py" in str(result.result_details)

    def test_fails_when_source_has_no_base_node_subclass(
        self,
        griptape_nodes: GriptapeNodes,
        _isolate_registry_and_config: Path,  # noqa: PT019 - value is used to locate the source file
    ) -> None:
        from griptape_nodes.retained_mode.events.library_events import (
            RegisterSandboxNodeFromSourceRequest,
            RegisterSandboxNodeFromSourceResultFailure,
        )

        library_manager = griptape_nodes.LibraryManager()
        sandbox_dir = _isolate_registry_and_config
        no_node_file = sandbox_dir / "no_node.py"
        no_node_file.write_text("x = 1\n")

        result = library_manager.register_sandbox_node_from_source_request(
            RegisterSandboxNodeFromSourceRequest(file_path=str(no_node_file))
        )

        assert isinstance(result, RegisterSandboxNodeFromSourceResultFailure)
        assert "BaseNode" in str(result.result_details)


class _DescribeNodeTypeProbe(BaseNode):
    """Concrete BaseNode used to exercise describe_node_type_request."""

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None) -> None:
        super().__init__(name=name, metadata=metadata)

        prompt = Parameter(
            name="prompt",
            type="str",
            input_types=["str"],
            output_type="str",
            default_value="hello",
            tooltip="Prompt text",
            ui_options={"display_name": "Prompt"},
        )
        self.add_parameter(prompt)

        temperature = Parameter(
            name="temperature",
            type="float",
            input_types=["float"],
            output_type="float",
            default_value=0.5,
            tooltip="Sampling temperature",
            allowed_modes={ParameterMode.PROPERTY},
        )
        self.add_parameter(temperature)


class _RaisingProbe(BaseNode):
    """Stand-in for node types whose __init__ performs failing I/O (auth, network, disk)."""

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None) -> None:
        super().__init__(name=name, metadata=metadata)
        msg = "simulated I/O failure"
        raise RuntimeError(msg)


class TestDescribeNodeTypeRequest:
    """Exercise LibraryManager.describe_node_type_request."""

    _LIBRARY_NAME = "describe-node-type-test-library"

    @pytest.fixture(autouse=True)
    def _clean_registry(self) -> Generator[None, None, None]:
        """LibraryRegistry holds class-level state that survives the singleton reset fixture."""
        LibraryRegistry._libraries.clear()
        LibraryRegistry._node_aliases.clear()
        LibraryRegistry._collision_node_names_to_library_names.clear()
        LibraryRegistry._registered_widgets.clear()
        yield
        LibraryRegistry._libraries.clear()
        LibraryRegistry._node_aliases.clear()
        LibraryRegistry._collision_node_names_to_library_names.clear()
        LibraryRegistry._registered_widgets.clear()

    def _register_probe_library(self) -> None:
        schema = LibrarySchema(
            name=self._LIBRARY_NAME,
            library_schema_version=LibrarySchema.LATEST_SCHEMA_VERSION,
            metadata=LibraryMetadata(
                author="test",
                description="probe library",
                library_version="1.0.0",
                engine_version="1.0.0",
                tags=[],
            ),
            categories=[],
            nodes=[],
        )
        library = LibraryRegistry.generate_new_library(library_data=schema)
        library.register_new_node_type(
            _DescribeNodeTypeProbe,
            NodeMetadata(
                category="test",
                description="Probe node used by DescribeNodeType tests",
                display_name="Probe",
            ),
        )

    def test_returns_parameter_schema_without_touching_object_manager(self, griptape_nodes: GriptapeNodes) -> None:
        library_manager = griptape_nodes.LibraryManager()
        self._register_probe_library()

        request = DescribeNodeTypeRequest(
            node_type=_DescribeNodeTypeProbe.__name__,
            library=self._LIBRARY_NAME,
        )

        result = library_manager.describe_node_type_request(request)

        assert isinstance(result, DescribeNodeTypeResultSuccess)
        assert result.library == self._LIBRARY_NAME
        assert result.node_type == _DescribeNodeTypeProbe.__name__
        assert result.metadata.display_name == "Probe"

        by_name = {param.name: param for param in result.parameters}
        assert "prompt" in by_name
        assert "temperature" in by_name

        prompt = by_name["prompt"]
        assert prompt.type == "str"
        assert prompt.default_value == "hello"
        assert prompt.mode_allowed_input is True
        assert prompt.mode_allowed_output is True
        assert prompt.mode_allowed_property is True
        assert prompt.ui_options == {"display_name": "Prompt"}
        assert prompt.parent_container_name is None

        temperature = by_name["temperature"]
        assert temperature.default_value == pytest.approx(0.5)
        assert temperature.mode_allowed_input is False
        assert temperature.mode_allowed_output is False
        assert temperature.mode_allowed_property is True
        assert temperature.parent_container_name is None

        # Probe node must not leak into the ObjectManager.
        assert (
            griptape_nodes.ObjectManager().attempt_get_object_by_name(
                f"__describe_node_type_probe__{_DescribeNodeTypeProbe.__name__}"
            )
            is None
        )

    def test_resolves_library_when_node_type_is_unambiguous(self, griptape_nodes: GriptapeNodes) -> None:
        library_manager = griptape_nodes.LibraryManager()
        self._register_probe_library()

        request = DescribeNodeTypeRequest(node_type=_DescribeNodeTypeProbe.__name__)

        result = library_manager.describe_node_type_request(request)

        assert isinstance(result, DescribeNodeTypeResultSuccess)
        assert result.library == self._LIBRARY_NAME

    def test_returns_failure_when_node_type_missing(self, griptape_nodes: GriptapeNodes) -> None:
        library_manager = griptape_nodes.LibraryManager()
        self._register_probe_library()

        request = DescribeNodeTypeRequest(node_type="NotARealNode", library=self._LIBRARY_NAME)

        result = library_manager.describe_node_type_request(request)

        assert isinstance(result, DescribeNodeTypeResultFailure)

    def test_returns_success_with_warning_detail_when_init_raises(self, griptape_nodes: GriptapeNodes) -> None:
        """Nodes whose __init__ performs I/O can raise (e.g. auth). We still want the node-level metadata."""
        library_manager = griptape_nodes.LibraryManager()

        schema = LibrarySchema(
            name=self._LIBRARY_NAME,
            library_schema_version=LibrarySchema.LATEST_SCHEMA_VERSION,
            metadata=LibraryMetadata(
                author="test",
                description="probe library",
                library_version="1.0.0",
                engine_version="1.0.0",
                tags=[],
            ),
            categories=[],
            nodes=[],
        )
        library = LibraryRegistry.generate_new_library(library_data=schema)
        library.register_new_node_type(
            _RaisingProbe,
            NodeMetadata(
                category="test",
                description="Node that explodes during __init__",
                display_name="Raising Probe",
            ),
        )

        request = DescribeNodeTypeRequest(node_type=_RaisingProbe.__name__, library=self._LIBRARY_NAME)

        result = library_manager.describe_node_type_request(request)

        assert isinstance(result, DescribeNodeTypeResultSuccess)
        # Library-level metadata still surfaces so callers can at least show the node.
        assert result.metadata.display_name == "Raising Probe"
        # Parameters are empty because the probe failed before they could be declared.
        assert result.parameters == []
        # result_details carries the concrete reason at WARNING level so callers can tell
        # a probe failure apart from "this node legitimately has no parameters".
        assert isinstance(result.result_details, ResultDetails)
        assert any(detail.level == logging.WARNING for detail in result.result_details.result_details)
        assert "simulated I/O failure" in str(result.result_details)


class _LifecycleProbe(BaseNode):
    """Concrete BaseNode used to exercise Library.create_node's metadata injection."""

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None) -> None:
        super().__init__(name=name, metadata=metadata)


class TestLibraryNodeMetadataInjection:
    """Regression coverage for #4770.

    Before the fix, Library.create_node injected the live Pydantic NodeMetadata
    instance under metadata["library_node_metadata"]. The workflow serializer
    then emitted that model's repr (e.g. ``<LifecycleStage.BETA: 'BETA'>``)
    via ast.Constant -> ast.unparse, producing invalid Python that couldn't
    reload. Library.create_node now dumps to a JSON-safe dict at the boundary.
    """

    _LIBRARY_NAME = "lifecycle-probe-test-library"

    @pytest.fixture(autouse=True)
    def _clean_registry(self) -> Generator[None, None, None]:
        LibraryRegistry._libraries.clear()
        LibraryRegistry._node_aliases.clear()
        LibraryRegistry._collision_node_names_to_library_names.clear()
        LibraryRegistry._registered_widgets.clear()
        yield
        LibraryRegistry._libraries.clear()
        LibraryRegistry._node_aliases.clear()
        LibraryRegistry._collision_node_names_to_library_names.clear()
        LibraryRegistry._registered_widgets.clear()

    def _register_probe_library(self, node_metadata: NodeMetadata) -> None:
        schema = LibrarySchema(
            name=self._LIBRARY_NAME,
            library_schema_version=LibrarySchema.LATEST_SCHEMA_VERSION,
            metadata=LibraryMetadata(
                author="test",
                description="lifecycle probe library",
                library_version="1.0.0",
                engine_version="1.0.0",
                tags=[],
            ),
            categories=[],
            nodes=[],
        )
        library = LibraryRegistry.generate_new_library(library_data=schema)
        library.register_new_node_type(_LifecycleProbe, node_metadata)

    def test_library_node_metadata_is_dict_not_pydantic_model(self) -> None:
        """The injected value must be a plain dict so the workflow serializer never sees a Pydantic instance."""
        self._register_probe_library(
            NodeMetadata(category="test", description="probe", display_name="Probe"),
        )

        node = LibraryRegistry.create_node(
            node_type=_LifecycleProbe.__name__,
            name="probe-1",
            specific_library_name=self._LIBRARY_NAME,
        )

        injected = node.metadata["library_node_metadata"]
        assert isinstance(injected, dict)
        assert not isinstance(injected, NodeMetadata)

    def test_lifecycle_stage_strenum_dumps_to_plain_string(self) -> None:
        """The headline #4770 case: a BETA declaration must not survive as a StrEnum member."""
        self._register_probe_library(
            NodeMetadata(
                category="test",
                description="probe",
                display_name="Probe",
                declarations=[LifecycleStageNodeProperty(stage=LifecycleStage.BETA)],
            ),
        )

        node = LibraryRegistry.create_node(
            node_type=_LifecycleProbe.__name__,
            name="probe-2",
            specific_library_name=self._LIBRARY_NAME,
        )

        declarations = node.metadata["library_node_metadata"]["declarations"]
        assert declarations == [{"type": "lifecycle_stage", "stage": "BETA"}]
        # Specifically: the stage value is a plain string, not a LifecycleStage member.
        assert declarations[0]["stage"].__class__ is str

    def test_caller_provided_library_node_metadata_is_overwritten(self) -> None:
        """Loading an old workflow that emits ``library_node_metadata=NodeMetadata(...)`` still works.

        Library.create_node has always overwritten the caller-supplied value with the
        registry's authoritative copy; this test pins that behavior so old generated
        workflows continue to load after the boundary fix.
        """
        self._register_probe_library(
            NodeMetadata(category="test", description="probe", display_name="Probe"),
        )

        stale_model = NodeMetadata(category="STALE", description="STALE", display_name="STALE")
        node = LibraryRegistry.create_node(
            node_type=_LifecycleProbe.__name__,
            name="probe-3",
            specific_library_name=self._LIBRARY_NAME,
            metadata={"library_node_metadata": stale_model},
        )

        injected = node.metadata["library_node_metadata"]
        assert injected["category"] == "test"
        assert injected["description"] == "probe"


class TestLibraryManagerInitializationFlag:
    """Test the is_initializing flag reported on the engine heartbeat."""

    def test_not_initializing_by_default(self, griptape_nodes: GriptapeNodes) -> None:
        assert griptape_nodes.LibraryManager().is_initializing() is False

    @pytest.mark.asyncio
    async def test_reload_brackets_is_initializing(
        self, griptape_nodes: GriptapeNodes, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """is_initializing() is True for the duration of the reload and False once it returns."""
        from griptape_nodes.retained_mode.events.library_events import (
            ReloadAllLibrariesRequest,
            ReloadAllLibrariesResultSuccess,
        )

        library_manager = griptape_nodes.LibraryManager()
        observed: dict[str, bool] = {}

        async def fake_run(_request: ReloadAllLibrariesRequest) -> ReloadAllLibrariesResultSuccess:
            observed["during"] = library_manager.is_initializing()
            return ReloadAllLibrariesResultSuccess(result_details="ok")

        monkeypatch.setattr(library_manager, "_run_reload_libraries", fake_run)

        await library_manager.reload_libraries_request(ReloadAllLibrariesRequest())

        assert observed["during"] is True
        assert library_manager.is_initializing() is False

    @pytest.mark.asyncio
    async def test_reload_clears_is_initializing_on_exception(
        self, griptape_nodes: GriptapeNodes, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failure during reload still clears the flag (finally), so the GUI doesn't hang."""
        from griptape_nodes.retained_mode.events.library_events import ReloadAllLibrariesRequest

        library_manager = griptape_nodes.LibraryManager()

        async def boom(_request: ReloadAllLibrariesRequest) -> None:
            msg = "boom"
            raise RuntimeError(msg)

        monkeypatch.setattr(library_manager, "_run_reload_libraries", boom)

        with pytest.raises(RuntimeError, match="boom"):
            await library_manager.reload_libraries_request(ReloadAllLibrariesRequest())

        assert library_manager.is_initializing() is False
