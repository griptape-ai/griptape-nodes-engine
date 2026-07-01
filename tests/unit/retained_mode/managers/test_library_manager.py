import asyncio
import json
import logging
import sys
from collections.abc import Callable, Generator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
from griptape_nodes.exe_types.node_types import BaseNode
from griptape_nodes.node_library.library_declarations import (
    KeySupport,
    LifecycleStage,
    LifecycleStageNodeProperty,
    Model,
    ModelCatalogLibraryProperty,
    ModelProvider,
    ModelProviderUsageNodeProperty,
    ModelUsageNodeProperty,
)
from griptape_nodes.node_library.library_registry import (
    LibraryMetadata,
    LibraryRegistry,
    LibrarySchema,
    NodeMetadata,
    get_declared_models,
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
from griptape_nodes.retained_mode.managers.project_manager import SYSTEM_DEFAULTS_KEY
from griptape_nodes.retained_mode.managers.settings import (
    LIBRARIES_TO_DOWNLOAD_KEY,
    LIBRARIES_TO_REGISTER_KEY,
    LibraryDownload,
    LibraryRegistration,
)
from griptape_nodes.utils.library_utils import extract_library_path


def _config_value_dispatcher(
    libraries_dir: Path, libraries: object, downloads: object | None = None
) -> Callable[..., object]:
    """A `get_config_value` side_effect that dispatches by key.

    `_discover_library_files` reads `libraries_to_register` and
    `libraries_to_download`; `libraries_directory` is also served so callers that
    touch all three keys share one mock. `downloads` defaults to an empty list so
    discovery's download-sourcing pass finds nothing unless a test opts in.
    """
    from griptape_nodes.retained_mode.managers.settings import (
        LIBRARIES_TO_DOWNLOAD_KEY,
        LIBRARIES_TO_REGISTER_KEY,
    )

    download_entries = downloads if downloads is not None else []

    def get_config_value(key: str, **_: object) -> object:
        if key == LIBRARIES_TO_REGISTER_KEY:
            return libraries
        if key == LIBRARIES_TO_DOWNLOAD_KEY:
            return download_entries
        if key == "libraries_directory":
            return str(libraries_dir)
        return None

    return get_config_value


def _register_only_config(libraries: object) -> Callable[..., object]:
    """A `get_config_value` side_effect serving only `libraries_to_register`.

    Discovery also reads `libraries_to_download`; this returns an empty list for it
    so tests exercising register-only behavior do not have their register entries
    misread as malformed download entries. Other keys return None.
    """
    from griptape_nodes.retained_mode.managers.settings import (
        LIBRARIES_TO_DOWNLOAD_KEY,
        LIBRARIES_TO_REGISTER_KEY,
    )

    def get_config_value(key: str, **_: object) -> object:
        if key == LIBRARIES_TO_REGISTER_KEY:
            return libraries
        if key == LIBRARIES_TO_DOWNLOAD_KEY:
            return []
        return None

    return get_config_value


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
            patch.object(library_manager, "_discover_library_files", AsyncMock(return_value=[_discovered("some_lib")])),
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
            patch.object(library_manager, "_discover_library_files", AsyncMock(return_value=[_discovered("new_lib")])),
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
            patch.object(library_manager, "_discover_library_files", AsyncMock(return_value=[_discovered("new_lib")])),
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

    @pytest.mark.asyncio
    async def test_discover_library_files_marks_disabled_entries(
        self, griptape_nodes: GriptapeNodes, lib_files: tuple[Path, Path]
    ) -> None:
        """Object-shaped entries with enabled=False produce disabled register entries."""
        library_manager = griptape_nodes.LibraryManager()
        enabled_lib, disabled_lib = lib_files

        config = [
            str(enabled_lib),
            {"path": str(disabled_lib), "enabled": False},
        ]

        with patch.object(
            griptape_nodes.ConfigManager(), "get_config_value", side_effect=_register_only_config(config)
        ):
            result = await library_manager._discover_library_files()

        by_path = {
            Path(entry.registration.path): entry.registration.enabled
            for entry in result
            if entry.registration.path is not None
        }
        assert by_path[enabled_lib] is True
        assert by_path[disabled_lib] is False

    @pytest.mark.asyncio
    async def test_discover_library_files_bare_string_defaults_to_enabled(
        self, griptape_nodes: GriptapeNodes, lib_files: tuple[Path, Path]
    ) -> None:
        """Bare path strings continue to be treated as enabled."""
        library_manager = griptape_nodes.LibraryManager()
        enabled_lib, _ = lib_files

        with patch.object(
            griptape_nodes.ConfigManager(), "get_config_value", side_effect=_register_only_config([str(enabled_lib)])
        ):
            result = await library_manager._discover_library_files()

        assert len(result) == 1
        assert result[0].registration.enabled is True

    @pytest.mark.asyncio
    async def test_discover_libraries_request_marks_disabled_lifecycle(
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

        with patch.object(
            griptape_nodes.ConfigManager(), "get_config_value", side_effect=_register_only_config(config)
        ):
            result = await library_manager.discover_libraries_request(DiscoverLibrariesRequest(include_sandbox=False))

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

    @pytest.mark.asyncio
    async def test_invalid_entry_is_skipped_with_warning(
        self, griptape_nodes: GriptapeNodes, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Entries that are neither strings nor dicts with a path are skipped."""
        library_manager = griptape_nodes.LibraryManager()

        config = [42, {"enabled": True}]  # missing 'path', and a bare int

        with (
            patch.object(griptape_nodes.ConfigManager(), "get_config_value", return_value=config),
            caplog.at_level(logging.WARNING, logger="griptape_nodes"),
        ):
            result = await library_manager._discover_library_files()

        assert result == []
        warnings = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert any("libraries_to_register" in m for m in warnings)

    @pytest.mark.asyncio
    async def test_rediscovery_reconciles_toggled_enabled_flag(
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
        with patch.object(
            griptape_nodes.ConfigManager(), "get_config_value", side_effect=_register_only_config(initial_config)
        ):
            await library_manager.discover_libraries_request(DiscoverLibrariesRequest(include_sandbox=False))

        first_state = library_manager._library_file_path_to_info[str(first_lib)].lifecycle_state
        second_state = library_manager._library_file_path_to_info[str(second_lib)].lifecycle_state
        assert first_state != LibraryManager.LibraryLifecycleState.DISABLED
        assert second_state == LibraryManager.LibraryLifecycleState.DISABLED

        # User flips the config: first_lib disabled, second_lib enabled, then triggers refresh.
        toggled_config = [{"path": str(first_lib), "enabled": False}, str(second_lib)]
        with patch.object(
            griptape_nodes.ConfigManager(), "get_config_value", side_effect=_register_only_config(toggled_config)
        ):
            await library_manager.discover_libraries_request(DiscoverLibrariesRequest(include_sandbox=False))

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

        LibraryRegistry._clear()

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
                LibraryRegistry._clear()

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


class _CatalogProbe(BaseNode):
    """Probe whose __init__ sources a parameter default from the library model_catalog.

    Exercises the describe/reference probes resolving library-backed __init__ data
    via get_declared_models -- which only works when the node is constructed with
    its library/node_type, the way create_node does it.
    """

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None) -> None:
        super().__init__(name=name, metadata=metadata)
        resolved = ",".join(r.model.provider_model_id or "" for r in get_declared_models(self))
        self.add_parameter(
            Parameter(
                name="resolved_models",
                type="str",
                input_types=["str"],
                output_type="str",
                default_value=resolved,
                tooltip="Comma-joined provider model ids resolved from the catalog.",
            )
        )


class TestDescribeNodeTypeRequest:
    """Exercise LibraryManager.describe_node_type_request."""

    _LIBRARY_NAME = "describe-node-type-test-library"

    @pytest.fixture(autouse=True)
    def _clean_registry(self) -> Generator[None, None, None]:
        """LibraryRegistry holds class-level state that survives the singleton reset fixture."""
        LibraryRegistry._clear()
        yield
        LibraryRegistry._clear()

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

    def test_probe_resolves_library_model_catalog(self, griptape_nodes: GriptapeNodes) -> None:
        # The probe must be constructed with the node's library/type so __init__
        # logic that resolves against the model_catalog (get_declared_models)
        # works -- otherwise the catalog is invisible and the roster is empty.
        library_manager = griptape_nodes.LibraryManager()
        schema = LibrarySchema(
            name=self._LIBRARY_NAME,
            library_schema_version=LibrarySchema.LATEST_SCHEMA_VERSION,
            metadata=LibraryMetadata(
                author="test",
                description="catalog probe library",
                library_version="1.0.0",
                engine_version="1.0.0",
                tags=[],
                declarations=[
                    ModelCatalogLibraryProperty(
                        providers={
                            "acme": ModelProvider(
                                display_name="Acme",
                                models={
                                    "m1": Model(
                                        display_name="M1",
                                        provider_model_id="acme-m1",
                                        key_support=KeySupport.REQUIRES_GRIPTAPE_KEY,
                                    ),
                                },
                            ),
                        },
                    ),
                ],
            ),
            categories=[],
            nodes=[],
        )
        library = LibraryRegistry.generate_new_library(library_data=schema)
        library.register_new_node_type(
            _CatalogProbe,
            NodeMetadata(
                category="test",
                description="Catalog-backed probe",
                display_name="CatalogProbe",
                declarations=[ModelUsageNodeProperty(model_ids=["m1"])],
            ),
        )

        result = library_manager.describe_node_type_request(
            DescribeNodeTypeRequest(node_type=_CatalogProbe.__name__, library=self._LIBRARY_NAME),
        )

        assert isinstance(result, DescribeNodeTypeResultSuccess)
        by_name = {param.name: param for param in result.parameters}
        # Resolved from the catalog -> non-empty. Without library context it would be "".
        assert by_name["resolved_models"].default_value == "acme-m1"

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
        LibraryRegistry._clear()
        yield
        LibraryRegistry._clear()

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


class TestLibraryManagerEngineVersionCheck:
    """`_check_engine_version` gates activation on the merged engine_version config key."""

    @staticmethod
    def _config_manager_returning(spec: str | None) -> MagicMock:
        config_manager = MagicMock()
        config_manager.get_config_value.return_value = spec
        return config_manager

    def test_satisfied_returns_none(self, griptape_nodes: GriptapeNodes) -> None:
        library_manager = griptape_nodes.LibraryManager()
        with (
            patch("griptape_nodes.retained_mode.managers.library_manager.GriptapeNodes") as mock_gn,
            patch("griptape_nodes.utils.version_utils.engine_version", "0.5.3"),
        ):
            mock_gn.ConfigManager.return_value = self._config_manager_returning(">=0.5,<1.0")
            assert library_manager._check_engine_version() is None

    def test_unsatisfied_returns_detail_naming_running_version(self, griptape_nodes: GriptapeNodes) -> None:
        library_manager = griptape_nodes.LibraryManager()
        with (
            patch("griptape_nodes.retained_mode.managers.library_manager.GriptapeNodes") as mock_gn,
            patch("griptape_nodes.utils.version_utils.engine_version", "0.5.3"),
        ):
            mock_gn.ConfigManager.return_value = self._config_manager_returning(">=2.0,<3.0")
            detail = library_manager._check_engine_version()

        assert detail is not None
        assert "0.5.3" in detail

    def test_malformed_specifier_returns_detail(self, griptape_nodes: GriptapeNodes) -> None:
        library_manager = griptape_nodes.LibraryManager()
        with (
            patch("griptape_nodes.retained_mode.managers.library_manager.GriptapeNodes") as mock_gn,
            patch("griptape_nodes.utils.version_utils.engine_version", "0.5.3"),
        ):
            mock_gn.ConfigManager.return_value = self._config_manager_returning("not-a-specifier")
            detail = library_manager._check_engine_version()

        assert detail is not None
        assert "not a valid" in detail.lower()

    def test_no_key_returns_none(self, griptape_nodes: GriptapeNodes) -> None:
        library_manager = griptape_nodes.LibraryManager()
        with patch("griptape_nodes.retained_mode.managers.library_manager.GriptapeNodes") as mock_gn:
            mock_gn.ConfigManager.return_value = self._config_manager_returning(None)
            assert library_manager._check_engine_version() is None


class TestLibraryManagerProvisioningPlan:
    """`_plan_one_library_provisioning` is a pure decision the preview and execution share."""

    @pytest.mark.asyncio
    async def test_satisfied_git_entry_plans_skip(self, griptape_nodes: GriptapeNodes) -> None:
        from griptape_nodes.retained_mode.events.library_events import LibraryProvisioningActionKind

        library_manager = griptape_nodes.LibraryManager()
        download = LibraryDownload(name="git-lib", version=">=2.0,<3", git_url="griptape-ai/git-lib@v2")
        with patch.object(library_manager, "_installed_download_version", new=AsyncMock(return_value="2.1.0")):
            action = await library_manager._plan_one_library_provisioning(download)

        assert action.kind == LibraryProvisioningActionKind.SKIP
        assert action.destructive is False

    @pytest.mark.asyncio
    async def test_missing_git_entry_plans_install(self, griptape_nodes: GriptapeNodes) -> None:
        from griptape_nodes.retained_mode.events.library_events import LibraryProvisioningActionKind

        library_manager = griptape_nodes.LibraryManager()
        download = LibraryDownload(name="git-lib", version=">=2.0", git_url="griptape-ai/git-lib@v2.0")
        with patch.object(library_manager, "_installed_download_version", new=AsyncMock(return_value=None)):
            action = await library_manager._plan_one_library_provisioning(download)

        assert action.kind == LibraryProvisioningActionKind.INSTALL
        assert action.destructive is False

    @pytest.mark.asyncio
    async def test_wrong_git_version_plans_destructive_overwrite(self, griptape_nodes: GriptapeNodes) -> None:
        from griptape_nodes.retained_mode.events.library_events import LibraryProvisioningActionKind

        library_manager = griptape_nodes.LibraryManager()
        download = LibraryDownload(name="git-lib", version=">=2.0", git_url="griptape-ai/git-lib@v2.0")
        with patch.object(library_manager, "_installed_download_version", new=AsyncMock(return_value="1.0.0")):
            action = await library_manager._plan_one_library_provisioning(download)

        assert action.kind == LibraryProvisioningActionKind.OVERWRITE
        # A git overwrite deletes the local library directory before re-cloning.
        assert action.destructive is True

    @pytest.mark.asyncio
    async def test_version_pin_without_name_uses_repo_name_for_action_label(
        self, griptape_nodes: GriptapeNodes
    ) -> None:
        # A {git_url, version} entry with no `name` still enforces its pin: the installed
        # copy is found by its repo-name directory, so a wrong version plans OVERWRITE
        # rather than silently no-opping. The action's library_name falls back to the repo name.
        from griptape_nodes.retained_mode.events.library_events import LibraryProvisioningActionKind

        library_manager = griptape_nodes.LibraryManager()
        download = LibraryDownload(version=">=2.0", git_url="griptape-ai/git-lib@v2.0")
        with patch.object(library_manager, "_installed_download_version", new=AsyncMock(return_value="1.0.0")):
            action = await library_manager._plan_one_library_provisioning(download)

        assert action.kind == LibraryProvisioningActionKind.OVERWRITE
        assert action.destructive is True
        assert action.library_name == "git-lib"


class TestInstalledLibraryVersion:
    """`_installed_library_version` reads on-disk manifests, surviving the reload's registry unload."""

    @staticmethod
    def _write_manifest(directory: Path, name: str, version: str | None) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        metadata: dict = {} if version is None else {"library_version": version}
        manifest = {"name": name, "metadata": metadata}
        (directory / "griptape_nodes_library.json").write_text(json.dumps(manifest), encoding="utf-8")

    @staticmethod
    def _config_manager_for(libraries_dir: Path) -> MagicMock:
        config_manager = MagicMock()
        config_manager.get_config_value.return_value = str(libraries_dir)
        config_manager.workspace_path = str(libraries_dir.parent)
        return config_manager

    @pytest.mark.asyncio
    async def test_returns_version_from_matching_manifest(self, griptape_nodes: GriptapeNodes, tmp_path: Path) -> None:
        library_manager = griptape_nodes.LibraryManager()
        libraries_dir = tmp_path / "libraries"
        self._write_manifest(libraries_dir / "git-lib", "Griptape Nodes Library", "0.78.0")
        with patch("griptape_nodes.retained_mode.managers.library_manager.GriptapeNodes") as mock_gn:
            mock_gn.ConfigManager.return_value = self._config_manager_for(libraries_dir)
            assert await library_manager._installed_library_version("Griptape Nodes Library") == "0.78.0"

    @pytest.mark.asyncio
    async def test_returns_none_when_no_manifest_matches(self, griptape_nodes: GriptapeNodes, tmp_path: Path) -> None:
        library_manager = griptape_nodes.LibraryManager()
        libraries_dir = tmp_path / "libraries"
        self._write_manifest(libraries_dir / "other", "Some Other Library", "1.0.0")
        with patch("griptape_nodes.retained_mode.managers.library_manager.GriptapeNodes") as mock_gn:
            mock_gn.ConfigManager.return_value = self._config_manager_for(libraries_dir)
            assert await library_manager._installed_library_version("Griptape Nodes Library") is None

    @pytest.mark.asyncio
    async def test_returns_none_when_libraries_directory_unconfigured(self, griptape_nodes: GriptapeNodes) -> None:
        library_manager = griptape_nodes.LibraryManager()
        config_manager = MagicMock()
        config_manager.get_config_value.return_value = None
        with patch("griptape_nodes.retained_mode.managers.library_manager.GriptapeNodes") as mock_gn:
            mock_gn.ConfigManager.return_value = config_manager
            assert await library_manager._installed_library_version("Griptape Nodes Library") is None

    @pytest.mark.asyncio
    async def test_returns_none_when_manifest_has_no_version(
        self, griptape_nodes: GriptapeNodes, tmp_path: Path
    ) -> None:
        library_manager = griptape_nodes.LibraryManager()
        libraries_dir = tmp_path / "libraries"
        self._write_manifest(libraries_dir / "git-lib", "Griptape Nodes Library", None)
        with patch("griptape_nodes.retained_mode.managers.library_manager.GriptapeNodes") as mock_gn:
            mock_gn.ConfigManager.return_value = self._config_manager_for(libraries_dir)
            assert await library_manager._installed_library_version("Griptape Nodes Library") is None


class TestInstalledLibraryManifestPath:
    """The shared resolver behind both planner and loader.

    `_installed_library_manifest_path` backs both the provisioning planner
    (`_installed_library_version`) and the loader (`_discover_library_files`), so the
    file the planner reasons about is exactly the file discovery loads.
    """

    @pytest.mark.asyncio
    async def test_returns_manifest_path_for_matching_name(self, griptape_nodes: GriptapeNodes, tmp_path: Path) -> None:
        library_manager = griptape_nodes.LibraryManager()
        libraries_dir = tmp_path / "libraries"
        TestInstalledLibraryVersion._write_manifest(libraries_dir / "git-lib", "Griptape Nodes Library", "0.78.0")
        with patch("griptape_nodes.retained_mode.managers.library_manager.GriptapeNodes") as mock_gn:
            mock_gn.ConfigManager.return_value = TestInstalledLibraryVersion._config_manager_for(libraries_dir)
            result = await library_manager._installed_library_manifest_path("Griptape Nodes Library")
        assert result == libraries_dir / "git-lib" / "griptape_nodes_library.json"

    @pytest.mark.asyncio
    async def test_returns_none_when_no_manifest_matches(self, griptape_nodes: GriptapeNodes, tmp_path: Path) -> None:
        library_manager = griptape_nodes.LibraryManager()
        libraries_dir = tmp_path / "libraries"
        TestInstalledLibraryVersion._write_manifest(libraries_dir / "other", "Some Other Library", "1.0.0")
        with patch("griptape_nodes.retained_mode.managers.library_manager.GriptapeNodes") as mock_gn:
            mock_gn.ConfigManager.return_value = TestInstalledLibraryVersion._config_manager_for(libraries_dir)
            assert await library_manager._installed_library_manifest_path("Griptape Nodes Library") is None

    @pytest.mark.asyncio
    async def test_returns_none_when_libraries_directory_unconfigured(self, griptape_nodes: GriptapeNodes) -> None:
        library_manager = griptape_nodes.LibraryManager()
        config_manager = MagicMock()
        config_manager.get_config_value.return_value = None
        with patch("griptape_nodes.retained_mode.managers.library_manager.GriptapeNodes") as mock_gn:
            mock_gn.ConfigManager.return_value = config_manager
            assert await library_manager._installed_library_manifest_path("Griptape Nodes Library") is None


class TestInstalledDownloadVersion:
    """`_installed_download_version` locates the installed copy the way the download handler lands it.

    A download entry without a `name` is matched by its repo-name directory
    (`libraries_directory/<repo-name>/`), keeping the version-check consistent
    with clone/skip/overwrite so a `version` pin works without `name`. An explicit
    `name` overrides the directory match and resolves by manifest name instead.
    """

    @pytest.mark.asyncio
    async def test_resolves_by_repo_name_directory_when_name_absent(
        self, griptape_nodes: GriptapeNodes, tmp_path: Path
    ) -> None:
        # The clone dir is the repo name from the git URL, while the manifest's own
        # `name` differs; the lookup must key off the directory, not the manifest name.
        library_manager = griptape_nodes.LibraryManager()
        libraries_dir = tmp_path / "libraries"
        TestInstalledLibraryVersion._write_manifest(libraries_dir / "git-lib", "Griptape Nodes Library", "1.2.3")
        download = LibraryDownload(git_url="griptape-ai/git-lib@v2.0", version=">=1.0")
        with patch("griptape_nodes.retained_mode.managers.library_manager.GriptapeNodes") as mock_gn:
            mock_gn.ConfigManager.return_value = TestInstalledLibraryVersion._config_manager_for(libraries_dir)
            assert await library_manager._installed_download_version(download) == "1.2.3"

    @pytest.mark.asyncio
    async def test_returns_none_when_repo_directory_absent(self, griptape_nodes: GriptapeNodes, tmp_path: Path) -> None:
        library_manager = griptape_nodes.LibraryManager()
        libraries_dir = tmp_path / "libraries"
        TestInstalledLibraryVersion._write_manifest(libraries_dir / "other-lib", "Other", "1.0.0")
        download = LibraryDownload(git_url="griptape-ai/git-lib@v2.0", version=">=1.0")
        with patch("griptape_nodes.retained_mode.managers.library_manager.GriptapeNodes") as mock_gn:
            mock_gn.ConfigManager.return_value = TestInstalledLibraryVersion._config_manager_for(libraries_dir)
            assert await library_manager._installed_download_version(download) is None

    @pytest.mark.asyncio
    async def test_name_overrides_directory_match(self, griptape_nodes: GriptapeNodes, tmp_path: Path) -> None:
        # With an explicit `name`, resolve by manifest name even when the library lives
        # under a directory that does not match the repo name (e.g. legacy XDG layout).
        library_manager = griptape_nodes.LibraryManager()
        libraries_dir = tmp_path / "libraries"
        TestInstalledLibraryVersion._write_manifest(libraries_dir / "legacy-dir", "Griptape Nodes Library", "0.9.0")
        download = LibraryDownload(git_url="griptape-ai/git-lib@v2.0", version=">=1.0", name="Griptape Nodes Library")
        with patch("griptape_nodes.retained_mode.managers.library_manager.GriptapeNodes") as mock_gn:
            mock_gn.ConfigManager.return_value = TestInstalledLibraryVersion._config_manager_for(libraries_dir)
            assert await library_manager._installed_download_version(download) == "0.9.0"

    @pytest.mark.asyncio
    async def test_returns_none_when_libraries_directory_unconfigured(self, griptape_nodes: GriptapeNodes) -> None:
        library_manager = griptape_nodes.LibraryManager()
        config_manager = MagicMock()
        config_manager.get_config_value.return_value = None
        download = LibraryDownload(git_url="griptape-ai/git-lib@v2.0", version=">=1.0")
        with patch("griptape_nodes.retained_mode.managers.library_manager.GriptapeNodes") as mock_gn:
            mock_gn.ConfigManager.return_value = config_manager
            assert await library_manager._installed_download_version(download) is None

    @pytest.mark.asyncio
    async def test_explicit_libraries_path_probes_target_not_live(
        self, griptape_nodes: GriptapeNodes, tmp_path: Path
    ) -> None:
        # The preview passes the TARGET project's libraries dir. The probe must read it,
        # not the live config, so it never falls back to the active workspace.
        library_manager = griptape_nodes.LibraryManager()
        target_libs = tmp_path / "target" / "libraries"
        TestInstalledLibraryVersion._write_manifest(target_libs / "git-lib", "Griptape Nodes Library", "3.3.0")
        download = LibraryDownload(git_url="griptape-ai/git-lib@v2.0", version=">=1.0")
        live_config = MagicMock()
        # Live config points elsewhere; an explicit libraries_path must win, and the live
        # libraries_directory must never be read.
        live_config.get_config_value.return_value = str(tmp_path / "live" / "libraries")
        live_config.workspace_path = str(tmp_path / "live")
        with patch("griptape_nodes.retained_mode.managers.library_manager.GriptapeNodes") as mock_gn:
            mock_gn.ConfigManager.return_value = live_config
            assert await library_manager._installed_download_version(download, target_libs) == "3.3.0"
        live_config.get_config_value.assert_not_called()


class TestDiscoverProvisionedManifestPaths:
    """Discovery loads a provisioned library from the manifest path in the register list.

    Provisioning lands a git-pinned library on disk and the download handler appends
    its resolved manifest path to `libraries_to_register`, so discovery sees an ordinary
    path-backed entry. A register entry whose path does not exist on disk is skipped.
    Without this, a pinned standard library showed up in neither the engine nor the editor
    after a project switch.
    """

    @pytest.mark.asyncio
    async def test_provisioned_manifest_path_is_discovered(self, griptape_nodes: GriptapeNodes, tmp_path: Path) -> None:
        library_manager = griptape_nodes.LibraryManager()
        libraries_dir = tmp_path / "libraries"
        manifest_dir = libraries_dir / "griptape-nodes-library-standard"
        TestInstalledLibraryVersion._write_manifest(manifest_dir, "Griptape Nodes Library", "0.78.0")
        expected_manifest = manifest_dir / "griptape_nodes_library.json"

        # The manifest path the download handler appends to libraries_to_register after
        # provisioning the pinned library.
        config = [str(expected_manifest)]

        with patch("griptape_nodes.retained_mode.managers.library_manager.GriptapeNodes") as mock_gn:
            config_manager = TestInstalledLibraryVersion._config_manager_for(libraries_dir)
            config_manager.get_config_value.side_effect = _config_value_dispatcher(libraries_dir, config)
            mock_gn.ConfigManager.return_value = config_manager
            result = await library_manager._discover_library_files()

        discovered_paths = [Path(entry.registration.path) for entry in result if entry.registration.path is not None]
        assert expected_manifest in discovered_paths

    @pytest.mark.asyncio
    async def test_missing_register_path_is_skipped(self, griptape_nodes: GriptapeNodes, tmp_path: Path) -> None:
        library_manager = griptape_nodes.LibraryManager()
        libraries_dir = tmp_path / "libraries"
        libraries_dir.mkdir(parents=True, exist_ok=True)

        # A register entry whose path is not on disk yet: nothing to discover.
        config = [str(libraries_dir / "missing" / "griptape_nodes_library.json")]

        with patch("griptape_nodes.retained_mode.managers.library_manager.GriptapeNodes") as mock_gn:
            config_manager = TestInstalledLibraryVersion._config_manager_for(libraries_dir)
            config_manager.get_config_value.side_effect = _config_value_dispatcher(libraries_dir, config)
            mock_gn.ConfigManager.return_value = config_manager
            result = await library_manager._discover_library_files()

        assert result == []


class TestRegistrationSatisfiedByInstalled:
    """The PEP 440 compare that decides whether provisioning can skip an entry."""

    def test_nothing_installed_is_never_satisfied(self) -> None:
        download = LibraryDownload(name="lib", version=">=2.0", git_url="griptape-ai/lib@v2")
        assert _LibraryManager._registration_satisfied_by_installed(download, None) is False

    def test_source_only_entry_satisfied_by_any_installed(self) -> None:
        download = LibraryDownload(name="lib", git_url="griptape-ai/lib@v2")
        assert _LibraryManager._registration_satisfied_by_installed(download, "1.0.0") is True

    def test_version_within_specifier_is_satisfied(self) -> None:
        download = LibraryDownload(name="lib", version=">=2.0,<3", git_url="griptape-ai/lib@v2")
        assert _LibraryManager._registration_satisfied_by_installed(download, "2.5.0") is True

    def test_version_outside_specifier_is_unsatisfied(self) -> None:
        download = LibraryDownload(name="lib", version=">=2.0,<3", git_url="griptape-ai/lib@v2")
        assert _LibraryManager._registration_satisfied_by_installed(download, "1.0.0") is False

    def test_malformed_spec_is_unsatisfied_so_provisioning_reruns(self) -> None:
        download = LibraryDownload(name="lib", version="not-a-spec", git_url="griptape-ai/lib@v2")
        assert _LibraryManager._registration_satisfied_by_installed(download, "2.0.0") is False


class TestReconcileLibrariesFromConfig:
    """Reconcile gates on engine_version first, then provisions libraries_to_download.

    Only `libraries_to_download` entries are provisioned. A library that is merely
    registered (`libraries_to_register`) is never overwritten by activation.
    """

    @staticmethod
    def _config_manager_for_keys(*, downloads: object, register: object = None) -> MagicMock:
        """A config mock that serves libraries_to_download and libraries_to_register by key."""
        config_manager = MagicMock()

        def get_config_value(key: str, **_: object) -> object:
            if key == LIBRARIES_TO_DOWNLOAD_KEY:
                return downloads
            if key == LIBRARIES_TO_REGISTER_KEY:
                return register
            return None

        config_manager.get_config_value.side_effect = get_config_value
        return config_manager

    @pytest.mark.asyncio
    async def test_engine_version_failure_blocks_provisioning(self, griptape_nodes: GriptapeNodes) -> None:
        library_manager = griptape_nodes.LibraryManager()
        with (
            patch.object(library_manager, "_check_engine_version", return_value="engine too old"),
            patch.object(library_manager, "_provision_one_library", new=AsyncMock()) as mock_provision,
        ):
            failures = await library_manager._reconcile_libraries_from_config()

        assert failures == ["engine too old"]
        # The gate runs before any disk mutation.
        mock_provision.assert_not_called()

    @pytest.mark.asyncio
    async def test_only_download_entries_are_provisioned(self, griptape_nodes: GriptapeNodes) -> None:
        library_manager = griptape_nodes.LibraryManager()
        # Both shapes of a download entry: a bare git-URL string and the object form.
        download_config = [
            "griptape-ai/bare-lib@v2",
            {"name": "git-lib", "git_url": "griptape-ai/git-lib@v2", "version": ">=2.0"},
        ]
        # A path-only register entry must never be provisioned (requirement 1).
        register_config = ["griptape_nodes_library.json", {"path": "../shared/lib"}]
        config_manager = self._config_manager_for_keys(downloads=download_config, register=register_config)
        with (
            patch("griptape_nodes.retained_mode.managers.library_manager.GriptapeNodes") as mock_gn,
            patch.object(library_manager, "_check_engine_version", return_value=None),
            patch.object(library_manager, "_provision_one_library", new=AsyncMock(return_value=None)) as mock_provision,
        ):
            mock_gn.ConfigManager.return_value = config_manager
            failures = await library_manager._reconcile_libraries_from_config()

        assert failures == []
        # Only the two download entries reach provisioning; nothing from the register list does.
        assert mock_provision.await_count == 2  # noqa: PLR2004

    @pytest.mark.asyncio
    async def test_provision_failure_is_collected(self, griptape_nodes: GriptapeNodes) -> None:
        library_manager = griptape_nodes.LibraryManager()
        download_config = [{"name": "git-lib", "git_url": "griptape-ai/git-lib@v2", "version": ">=2.0"}]
        config_manager = self._config_manager_for_keys(downloads=download_config)
        with (
            patch("griptape_nodes.retained_mode.managers.library_manager.GriptapeNodes") as mock_gn,
            patch.object(library_manager, "_check_engine_version", return_value=None),
            patch.object(library_manager, "_provision_one_library", new=AsyncMock(return_value="clone failed")),
        ):
            mock_gn.ConfigManager.return_value = config_manager
            failures = await library_manager._reconcile_libraries_from_config()

        assert failures == ["clone failed"]


class TestPreviewProjectProvisioning:
    """The read-only preview handler lists the plan without touching disk.

    The handler reconstructs the same effective config activation would reconcile:
    ProjectManager resolves the project (canonically) and its workspace dir, then
    ConfigManager merges every layer. These tests mock both collaborators so the
    merged config and the engine_version gate are exercised directly.
    """

    @staticmethod
    def _merged_config(
        libraries: object,
        *,
        engine_version: str | None = None,
        workspace_directory: str = "/ws/target",
        libraries_directory: str = "libraries",
    ) -> dict:
        """Build a merged-config dict shaped like compute_project_provisioning_config's output.

        Populates the nested `libraries_to_download` / `requires_engine` keys plus the
        top-level `workspace_directory` / `libraries_directory` the preview reads to
        probe the TARGET project's libraries dir.
        """
        on_init: dict[str, object] = {"libraries_to_download": libraries}
        if engine_version is not None:
            on_init["requires_engine"] = engine_version
        return {
            "workspace_directory": workspace_directory,
            "libraries_directory": libraries_directory,
            "app_events": {"on_app_initialization_complete": on_init},
        }

    @staticmethod
    def _patch_managers(mock_gn: MagicMock, *, dirs: object, merged: object) -> None:
        """Wire the mocked ProjectManager/ConfigManager the new handler calls."""
        mock_gn.ProjectManager.return_value.resolve_provisioning_config_dirs.return_value = dirs
        mock_gn.ConfigManager.return_value.compute_project_provisioning_config.return_value = merged

    @staticmethod
    def _patch_system_defaults(mock_gn: MagicMock, *, merged: object) -> None:
        """Wire the mocked ConfigManager for the system-defaults branch.

        System defaults reads its merged config from compute_system_defaults_provisioning_config
        (defaults -> user -> env, no project-adjacent or workspace file), so the handler never
        calls ProjectManager.resolve_provisioning_config_dirs for it.
        """
        mock_gn.ConfigManager.return_value.compute_system_defaults_provisioning_config.return_value = merged

    @pytest.mark.asyncio
    async def test_not_loaded_project_is_failure(self, griptape_nodes: GriptapeNodes) -> None:
        from griptape_nodes.retained_mode.events.library_events import (
            PreviewProjectProvisioningRequest,
            PreviewProjectProvisioningResultFailure,
        )

        library_manager = griptape_nodes.LibraryManager()
        with patch("griptape_nodes.retained_mode.managers.library_manager.GriptapeNodes") as mock_gn:
            mock_gn.ProjectManager.return_value.resolve_provisioning_config_dirs.return_value = None
            result = await library_manager.on_preview_project_provisioning_request(
                PreviewProjectProvisioningRequest(project_id="/nope/project.yml")
            )

        assert isinstance(result, PreviewProjectProvisioningResultFailure)

    @pytest.mark.asyncio
    async def test_no_download_entries_is_empty_success(self, griptape_nodes: GriptapeNodes, tmp_path: Path) -> None:
        from griptape_nodes.retained_mode.events.library_events import (
            PreviewProjectProvisioningRequest,
            PreviewProjectProvisioningResultSuccess,
        )

        library_manager = griptape_nodes.LibraryManager()
        merged = self._merged_config([])
        with patch("griptape_nodes.retained_mode.managers.library_manager.GriptapeNodes") as mock_gn:
            self._patch_managers(mock_gn, dirs=MagicMock(), merged=merged)
            result = await library_manager.on_preview_project_provisioning_request(
                PreviewProjectProvisioningRequest(project_id=str(tmp_path / "project.yml"))
            )

        assert isinstance(result, PreviewProjectProvisioningResultSuccess)
        assert result.actions == []
        assert result.engine_version_failure is None

    @pytest.mark.asyncio
    async def test_download_entries_preserve_order_and_flags(
        self, griptape_nodes: GriptapeNodes, tmp_path: Path
    ) -> None:
        from griptape_nodes.retained_mode.events.library_events import (
            LibraryProvisioningActionKind,
            PreviewProjectProvisioningRequest,
            PreviewProjectProvisioningResultSuccess,
        )

        library_manager = griptape_nodes.LibraryManager()
        merged = self._merged_config(
            [
                {"name": "skip-lib", "git_url": "griptape-ai/skip-lib@v2", "version": ">=2.0"},
                {"name": "install-lib", "git_url": "griptape-ai/install-lib@v2", "version": ">=2.0"},
                {"name": "overwrite-lib", "git_url": "griptape-ai/overwrite-lib@v2", "version": ">=2.0"},
            ]
        )
        installed = {"skip-lib": "2.1.0", "install-lib": None, "overwrite-lib": "1.0.0"}
        with (
            patch("griptape_nodes.retained_mode.managers.library_manager.GriptapeNodes") as mock_gn,
            patch.object(
                library_manager,
                "_installed_download_version",
                new=AsyncMock(side_effect=lambda download, _libraries_path=None: installed[download.name]),
            ),
        ):
            self._patch_managers(mock_gn, dirs=MagicMock(), merged=merged)
            result = await library_manager.on_preview_project_provisioning_request(
                PreviewProjectProvisioningRequest(project_id=str(tmp_path / "project.yml"))
            )

        assert isinstance(result, PreviewProjectProvisioningResultSuccess)
        assert [a.library_name for a in result.actions] == ["skip-lib", "install-lib", "overwrite-lib"]
        assert [a.kind for a in result.actions] == [
            LibraryProvisioningActionKind.SKIP,
            LibraryProvisioningActionKind.INSTALL,
            LibraryProvisioningActionKind.OVERWRITE,
        ]
        # Only the git OVERWRITE is destructive.
        assert [a.destructive for a in result.actions] == [False, False, True]

    @pytest.mark.asyncio
    async def test_plan_reads_merged_config_for_resolved_dirs(
        self, griptape_nodes: GriptapeNodes, tmp_path: Path
    ) -> None:
        """The preview plans from the merged config (not the project-adjacent file).

        Guards defect #2: when a higher-priority layer supplies
        `libraries_to_download`, reconcile reads the merged value, so the preview
        must compute its plan from the merged config for the dirs ProjectManager
        resolved -- otherwise the plan and the activation diverge.
        """
        from griptape_nodes.retained_mode.events.library_events import (
            LibraryProvisioningActionKind,
            PreviewProjectProvisioningRequest,
            PreviewProjectProvisioningResultSuccess,
        )

        library_manager = griptape_nodes.LibraryManager()
        dirs = MagicMock()
        dirs.project_dir = tmp_path / "proj"
        dirs.workspace_dir = tmp_path / "ws"
        # The merged value (e.g. from the workspace layer) differs from anything the
        # project-adjacent file alone would carry; the plan must reflect this entry.
        merged = self._merged_config([{"name": "merged-lib", "git_url": "griptape-ai/merged-lib@v2", "version": ">=2"}])
        with (
            patch("griptape_nodes.retained_mode.managers.library_manager.GriptapeNodes") as mock_gn,
            patch.object(library_manager, "_installed_download_version", new=AsyncMock(return_value=None)),
        ):
            self._patch_managers(mock_gn, dirs=dirs, merged=merged)
            result = await library_manager.on_preview_project_provisioning_request(
                PreviewProjectProvisioningRequest(project_id=str(tmp_path / "project.yml"))
            )

        compute = mock_gn.ConfigManager.return_value.compute_project_provisioning_config
        compute.assert_called_once_with(dirs.project_dir, dirs.workspace_dir, apply_override=dirs.apply_override)
        assert isinstance(result, PreviewProjectProvisioningResultSuccess)
        assert [a.library_name for a in result.actions] == ["merged-lib"]
        assert result.actions[0].kind == LibraryProvisioningActionKind.INSTALL

    @pytest.mark.asyncio
    async def test_probes_target_workspace_not_live_for_destructive_plan(
        self, griptape_nodes: GriptapeNodes, tmp_path: Path
    ) -> None:
        """The installed-version probe reads the TARGET project's libraries dir.

        Guards the defect where the preview resolved the probe against the live
        (active) workspace: a stale version sitting in the target workspace would
        be missed, so a destructive OVERWRITE would be under-reported as a
        non-destructive INSTALL. This exercises the real on-disk probe (no mock of
        _installed_download_version): the target workspace holds an unsatisfying
        version, the live config points at an empty dir, and the plan must still be
        a destructive OVERWRITE.
        """
        from griptape_nodes.retained_mode.events.library_events import (
            LibraryProvisioningActionKind,
            PreviewProjectProvisioningRequest,
            PreviewProjectProvisioningResultSuccess,
        )

        library_manager = griptape_nodes.LibraryManager()
        target_ws = tmp_path / "target"
        TestInstalledLibraryVersion._write_manifest(target_ws / "libraries" / "git-lib", "git-lib", "1.0.0")
        merged = self._merged_config(
            [{"git_url": "griptape-ai/git-lib@v2.0", "version": ">=2.0"}],
            workspace_directory=str(target_ws),
            libraries_directory="libraries",
        )
        # Live config points at a different, empty workspace; if the probe used it the
        # plan would wrongly be a non-destructive INSTALL.
        live_config = MagicMock()
        live_config.get_config_value.return_value = str(tmp_path / "live" / "libraries")
        live_config.workspace_path = str(tmp_path / "live")
        with patch("griptape_nodes.retained_mode.managers.library_manager.GriptapeNodes") as mock_gn:
            mock_gn.ConfigManager.return_value = live_config
            self._patch_managers(mock_gn, dirs=MagicMock(), merged=merged)
            result = await library_manager.on_preview_project_provisioning_request(
                PreviewProjectProvisioningRequest(project_id=str(tmp_path / "project.yml"))
            )

        assert isinstance(result, PreviewProjectProvisioningResultSuccess)
        assert [a.kind for a in result.actions] == [LibraryProvisioningActionKind.OVERWRITE]
        assert result.actions[0].destructive is True
        assert result.actions[0].installed_version == "1.0.0"

    @pytest.mark.asyncio
    async def test_unsatisfiable_engine_version_populates_failure(
        self, griptape_nodes: GriptapeNodes, tmp_path: Path
    ) -> None:
        from griptape_nodes.retained_mode.events.library_events import (
            PreviewProjectProvisioningRequest,
            PreviewProjectProvisioningResultSuccess,
        )

        library_manager = griptape_nodes.LibraryManager()
        merged = self._merged_config([], engine_version=">=2.0,<3.0")
        with (
            patch("griptape_nodes.retained_mode.managers.library_manager.GriptapeNodes") as mock_gn,
            patch("griptape_nodes.utils.version_utils.engine_version", "0.5.3"),
        ):
            self._patch_managers(mock_gn, dirs=MagicMock(), merged=merged)
            result = await library_manager.on_preview_project_provisioning_request(
                PreviewProjectProvisioningRequest(project_id=str(tmp_path / "project.yml"))
            )

        assert isinstance(result, PreviewProjectProvisioningResultSuccess)
        assert result.engine_version_failure is not None
        # Same text the live gate produces: it names the running engine version.
        assert "0.5.3" in result.engine_version_failure

    @pytest.mark.asyncio
    async def test_satisfiable_engine_version_leaves_failure_none(
        self, griptape_nodes: GriptapeNodes, tmp_path: Path
    ) -> None:
        from griptape_nodes.retained_mode.events.library_events import (
            PreviewProjectProvisioningRequest,
            PreviewProjectProvisioningResultSuccess,
        )

        library_manager = griptape_nodes.LibraryManager()
        merged = self._merged_config([], engine_version=">=0.5,<1.0")
        with (
            patch("griptape_nodes.retained_mode.managers.library_manager.GriptapeNodes") as mock_gn,
            patch("griptape_nodes.utils.version_utils.engine_version", "0.5.3"),
        ):
            self._patch_managers(mock_gn, dirs=MagicMock(), merged=merged)
            result = await library_manager.on_preview_project_provisioning_request(
                PreviewProjectProvisioningRequest(project_id=str(tmp_path / "project.yml"))
            )

        assert isinstance(result, PreviewProjectProvisioningResultSuccess)
        assert result.engine_version_failure is None

    @pytest.mark.asyncio
    async def test_system_defaults_plans_from_user_layer_without_resolving_dirs(
        self, griptape_nodes: GriptapeNodes
    ) -> None:
        """System defaults is previewable: it plans from the defaults->user->env merge.

        Switching to system defaults activates that merge (no project-adjacent or
        workspace file), and a user-config git pin can still force a destructive
        OVERWRITE there. The handler must match SYSTEM_DEFAULTS_KEY verbatim and read
        compute_system_defaults_provisioning_config, never resolve_provisioning_config_dirs
        (which returns None for the synthetic id and would wrongly produce a Failure).
        """
        from griptape_nodes.retained_mode.events.library_events import (
            LibraryProvisioningActionKind,
            PreviewProjectProvisioningRequest,
            PreviewProjectProvisioningResultSuccess,
        )

        library_manager = griptape_nodes.LibraryManager()
        merged = self._merged_config([{"name": "user-pin", "git_url": "griptape-ai/user-pin@v2", "version": "==2.0.0"}])
        with (
            patch("griptape_nodes.retained_mode.managers.library_manager.GriptapeNodes") as mock_gn,
            patch.object(library_manager, "_installed_download_version", new=AsyncMock(return_value="1.0.0")),
        ):
            self._patch_system_defaults(mock_gn, merged=merged)
            result = await library_manager.on_preview_project_provisioning_request(
                PreviewProjectProvisioningRequest(project_id=SYSTEM_DEFAULTS_KEY)
            )

        mock_gn.ProjectManager.return_value.resolve_provisioning_config_dirs.assert_not_called()
        assert isinstance(result, PreviewProjectProvisioningResultSuccess)
        assert [a.library_name for a in result.actions] == ["user-pin"]
        assert result.actions[0].kind == LibraryProvisioningActionKind.OVERWRITE
        assert result.actions[0].destructive is True

    @pytest.mark.asyncio
    async def test_system_defaults_unsatisfiable_engine_version_populates_failure(
        self, griptape_nodes: GriptapeNodes
    ) -> None:
        """A user-config engine_version pin gates the system-defaults switch too."""
        from griptape_nodes.retained_mode.events.library_events import (
            PreviewProjectProvisioningRequest,
            PreviewProjectProvisioningResultSuccess,
        )

        library_manager = griptape_nodes.LibraryManager()
        merged = self._merged_config([], engine_version=">=2.0,<3.0")
        with (
            patch("griptape_nodes.retained_mode.managers.library_manager.GriptapeNodes") as mock_gn,
            patch("griptape_nodes.utils.version_utils.engine_version", "0.5.3"),
        ):
            self._patch_system_defaults(mock_gn, merged=merged)
            result = await library_manager.on_preview_project_provisioning_request(
                PreviewProjectProvisioningRequest(project_id=SYSTEM_DEFAULTS_KEY)
            )

        assert isinstance(result, PreviewProjectProvisioningResultSuccess)
        assert result.engine_version_failure is not None
        assert "0.5.3" in result.engine_version_failure

    @pytest.mark.asyncio
    async def test_system_defaults_no_pins_is_empty_success(self, griptape_nodes: GriptapeNodes) -> None:
        """No user-config pins means nothing to provision: empty plan, no modal."""
        from griptape_nodes.retained_mode.events.library_events import (
            PreviewProjectProvisioningRequest,
            PreviewProjectProvisioningResultSuccess,
        )

        library_manager = griptape_nodes.LibraryManager()
        merged = self._merged_config([])
        with patch("griptape_nodes.retained_mode.managers.library_manager.GriptapeNodes") as mock_gn:
            self._patch_system_defaults(mock_gn, merged=merged)
            result = await library_manager.on_preview_project_provisioning_request(
                PreviewProjectProvisioningRequest(project_id=SYSTEM_DEFAULTS_KEY)
            )

        assert isinstance(result, PreviewProjectProvisioningResultSuccess)
        assert result.actions == []
        assert result.engine_version_failure is None


class TestProvisionGitLibraryOverwriteDir:
    """`_provision_git_library` aims the destructive overwrite at the installed dir."""

    @pytest.mark.asyncio
    async def test_overwrite_targets_installed_manifest_dir(
        self, griptape_nodes: GriptapeNodes, tmp_path: Path
    ) -> None:
        """Defect #3: the overwrite deletes the manifest's dir, not libraries_path/<repo-name>.

        When the installed dir name != git repo name, `_provision_git_library` resolves the
        installed manifest and passes its parent as download_directory + target_directory_name
        so the handler's delete lands on that exact dir.
        """
        from griptape_nodes.retained_mode.events.library_events import (
            DownloadLibraryRequest,
            DownloadLibraryResultSuccess,
        )

        library_manager = griptape_nodes.LibraryManager()
        download = LibraryDownload(name="my-lib", version=">=2.0", git_url="griptape-ai/repo-name@v2.0")
        # Installed under a directory whose name ("custom-install-dir") differs from the
        # git repo name ("repo-name") the handler would otherwise guess.
        installed_dir = tmp_path / "libraries" / "custom-install-dir"
        installed_dir.mkdir(parents=True)
        manifest_path = installed_dir / "griptape_nodes_library.json"
        manifest_path.touch()

        success = MagicMock(spec=DownloadLibraryResultSuccess)
        ahandle = AsyncMock(return_value=success)
        with (
            patch("griptape_nodes.retained_mode.managers.library_manager.GriptapeNodes") as mock_gn,
            patch.object(
                library_manager, "_installed_library_manifest_path", new=AsyncMock(return_value=manifest_path)
            ),
        ):
            mock_gn.ahandle_request = ahandle
            failure = await library_manager._provision_git_library(
                download, git_url="griptape-ai/repo-name@v2.0", installed_version="1.0.0"
            )

        assert failure is None
        assert ahandle.await_args is not None
        sent_request = ahandle.await_args.args[0]
        assert isinstance(sent_request, DownloadLibraryRequest)
        assert sent_request.overwrite_existing is True
        # The handler computes target_path = download_directory / target_directory_name;
        # both point at the installed dir, so the delete targets exactly that dir.
        assert sent_request.download_directory == str(installed_dir.parent)
        assert sent_request.target_directory_name == installed_dir.name

    @pytest.mark.asyncio
    async def test_fresh_install_leaves_dir_hints_none(self, griptape_nodes: GriptapeNodes) -> None:
        """A fresh install passes no dir hints, keeping the handler's repo-name default.

        When installed_version is None there is nothing to overwrite, so the manifest is
        never resolved and both directory hints stay None.
        """
        from griptape_nodes.retained_mode.events.library_events import (
            DownloadLibraryRequest,
            DownloadLibraryResultSuccess,
        )

        library_manager = griptape_nodes.LibraryManager()
        download = LibraryDownload(name="my-lib", version=">=2.0", git_url="griptape-ai/repo-name@v2.0")

        success = MagicMock(spec=DownloadLibraryResultSuccess)
        ahandle = AsyncMock(return_value=success)
        with (
            patch("griptape_nodes.retained_mode.managers.library_manager.GriptapeNodes") as mock_gn,
            patch.object(library_manager, "_installed_library_manifest_path", new=AsyncMock()) as mock_resolve,
        ):
            mock_gn.ahandle_request = ahandle
            failure = await library_manager._provision_git_library(
                download, git_url="griptape-ai/repo-name@v2.0", installed_version=None
            )

        assert failure is None
        mock_resolve.assert_not_called()
        assert ahandle.await_args is not None
        sent_request = ahandle.await_args.args[0]
        assert isinstance(sent_request, DownloadLibraryRequest)
        assert sent_request.overwrite_existing is False
        assert sent_request.download_directory is None
        assert sent_request.target_directory_name is None


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


class TestDownloadLibraryRegisterPersistence:
    """download_library_request must only persist to the GLOBAL config when registering now.

    A project-reconcile download passes auto_register=False: the project's own
    libraries_to_download is the per-activation source of truth, so the clone path
    must NOT be appended to the global libraries_to_register (doing so leaks the
    library into every other project's startup registration). The explicit CLI
    download (auto_register=True) keeps persisting so it loads on future startups.
    """

    @staticmethod
    def _make_clone(library_name: str) -> Callable[[str, Path, str | None], None]:
        """Return a clone_repository stand-in that writes a minimal manifest into target_path."""

        def fake_clone(_git_url: str, target_path: Path, _ref: str | None = None) -> None:
            target_path.mkdir(parents=True, exist_ok=True)
            (target_path / "griptape_nodes_library.json").write_text(
                json.dumps({"name": library_name}), encoding="utf-8"
            )

        return fake_clone

    @pytest.mark.asyncio
    async def test_reconcile_download_does_not_persist_to_global_config(
        self, griptape_nodes: GriptapeNodes, tmp_path: Path
    ) -> None:
        """auto_register=False (the reconcile/provisioning path) leaves global libraries_to_register untouched."""
        from griptape_nodes.retained_mode.events.library_events import (
            DownloadLibraryRequest,
            DownloadLibraryResultSuccess,
        )

        library_manager = griptape_nodes.LibraryManager()
        config_mgr = griptape_nodes.ConfigManager()
        before = config_mgr.get_config_value(LIBRARIES_TO_REGISTER_KEY, default=[])

        with patch(
            "griptape_nodes.retained_mode.managers.library_manager.clone_repository",
            side_effect=self._make_clone("provisioned_lib"),
        ):
            result = await library_manager.download_library_request(
                DownloadLibraryRequest(
                    git_url="owner/provisioned_lib",
                    download_directory=str(tmp_path / "libs"),
                    auto_register=False,
                )
            )

        assert isinstance(result, DownloadLibraryResultSuccess)
        after = config_mgr.get_config_value(LIBRARIES_TO_REGISTER_KEY, default=[])
        assert {extract_library_path(entry) for entry in after} == {extract_library_path(entry) for entry in before}
        assert result.library_path not in {extract_library_path(entry) for entry in after}

    @pytest.mark.asyncio
    async def test_explicit_download_persists_to_global_config(
        self, griptape_nodes: GriptapeNodes, tmp_path: Path
    ) -> None:
        """auto_register=True (the explicit CLI download) appends the clone path to global libraries_to_register."""
        from griptape_nodes.retained_mode.events.library_events import (
            DownloadLibraryRequest,
            DownloadLibraryResultSuccess,
            RegisterLibraryFromFileResultSuccess,
        )

        library_manager = griptape_nodes.LibraryManager()
        config_mgr = griptape_nodes.ConfigManager()

        # download_library_request routes registration through GriptapeNodes.ahandle_request;
        # the minimal fake manifest can't pass full LibrarySchema validation, so mock the
        # registration step to return success so the test stays focused on config persistence.
        mock_register_result = RegisterLibraryFromFileResultSuccess(
            library_name="explicit_lib",
            result_details=ResultDetails(message="OK", level=20),
        )
        with (
            patch(
                "griptape_nodes.retained_mode.managers.library_manager.clone_repository",
                side_effect=self._make_clone("explicit_lib"),
            ),
            patch.object(
                GriptapeNodes,
                "ahandle_request",
                new=AsyncMock(return_value=mock_register_result),
            ),
        ):
            result = await library_manager.download_library_request(
                DownloadLibraryRequest(
                    git_url="owner/explicit_lib",
                    download_directory=str(tmp_path / "libs"),
                    auto_register=True,
                )
            )

        assert isinstance(result, DownloadLibraryResultSuccess)
        after = config_mgr.get_config_value(LIBRARIES_TO_REGISTER_KEY, default=[])
        assert result.library_path in {extract_library_path(entry) for entry in after}


class TestDiscoverDownloadedLibraries:
    """A provisioned libraries_to_download entry must be discoverable from the workspace.

    Reconcile clones each libraries_to_download entry into the workspace
    libraries_directory; discovery resolves it there so the library loads scoped
    to the workspace that declares it, WITHOUT any libraries_to_register entry.
    This is the mechanism that replaces the global-config append, so projects that
    pin a library only via libraries_to_download (e.g. the lib-swap fixtures) still
    load it.
    """

    @staticmethod
    def _install_manifest(libraries_dir: Path, repo_name: str, library_name: str) -> Path:
        """Materialize a provisioned library manifest under <libraries_dir>/<repo_name>/."""
        manifest_dir = libraries_dir / repo_name
        manifest_dir.mkdir(parents=True)
        manifest_path = manifest_dir / "griptape_nodes_library.json"
        manifest_path.write_text(json.dumps({"name": library_name}), encoding="utf-8")
        return manifest_path

    @pytest.mark.asyncio
    async def test_download_only_library_is_discovered_from_workspace(
        self, griptape_nodes: GriptapeNodes, tmp_path: Path
    ) -> None:
        """A libraries_to_download entry with no libraries_to_register row is still discovered."""
        from griptape_nodes.retained_mode.managers.settings import LIBRARIES_TO_DOWNLOAD_KEY

        library_manager = griptape_nodes.LibraryManager()
        config_mgr = griptape_nodes.ConfigManager()

        libraries_dir = tmp_path / "libraries"
        manifest_path = self._install_manifest(libraries_dir, "remote_lib", "remote_lib")

        def get_config_value(key: str, **_: object) -> object:
            if key == LIBRARIES_TO_REGISTER_KEY:
                return []
            if key == LIBRARIES_TO_DOWNLOAD_KEY:
                return ["owner/remote_lib"]
            if key == "libraries_directory":
                return str(libraries_dir)
            return None

        with patch.object(config_mgr, "get_config_value", side_effect=get_config_value):
            entries = await library_manager._discover_library_files()

        discovered_paths = {Path(entry.registration.path) for entry in entries}
        assert manifest_path in discovered_paths


class TestPersistLibrarySettings:
    """A library's declared settings must persist to global WITHOUT leaking project-layer values.

    Library load injects each declared setting category into the user config. The
    existing category must be read from the user_config layer, not the merged
    config: the merged config folds in the active project's project/workspace/env
    layers (e.g. libraries_to_download, requires_engine), and round-tripping that
    through SetConfigCategory (which writes the GLOBAL user config) would leak the
    active project's per-activation pins into every other project's startup. This
    is the canonical repro for the duplicate-standard-library symptom seen when
    switching from a download-pinned project to one that declares no download.
    """

    @staticmethod
    def _library_with_settings(category: str, contents: dict[str, object]) -> LibrarySchema:
        """Build a minimal LibrarySchema declaring a single settings category."""
        from griptape_nodes.node_library.library_registry import Setting

        return LibrarySchema(
            name="settings_lib",
            library_schema_version=LibrarySchema.LATEST_SCHEMA_VERSION,
            metadata=LibraryMetadata(
                author="t",
                description="d",
                library_version="0.1.0",
                engine_version="0.1.0",
                tags=[],
            ),
            categories=[],
            nodes=[],
            settings=[Setting(category=category, contents=contents)],
        )

    def test_persist_does_not_leak_project_download_pin_to_global(
        self, griptape_nodes: GriptapeNodes, isolate_user_config: Path, tmp_path: Path
    ) -> None:
        """A project-layer libraries_to_download pin must NOT be written into the global user config."""
        library_manager = griptape_nodes.LibraryManager()
        config_mgr = griptape_nodes.ConfigManager()

        # Prime a project-adjacent config carrying a download pin, then load it as
        # the project layer so the MERGED config sees the pin but the global user
        # config file does not.
        project_dir = tmp_path / "pinned_project"
        project_dir.mkdir()
        (project_dir / "griptape_nodes_config.json").write_text(
            json.dumps(
                {
                    "app_events": {
                        "on_app_initialization_complete": {
                            "libraries_to_download": [{"git_url": "owner/standard@v0.79.0", "version": "==0.79.0"}]
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        config_mgr.load_project_config(project_dir)
        assert config_mgr.get_config_value(LIBRARIES_TO_DOWNLOAD_KEY, default=[]) != []

        library = self._library_with_settings(
            "app_events.on_app_initialization_complete",
            {"secrets_to_register": {"MY_LIB_KEY": ""}},
        )
        problems = library_manager._persist_library_settings(library)

        assert problems == []
        # The library's own declared setting persisted globally...
        global_config = json.loads(isolate_user_config.read_text(encoding="utf-8"))
        init = global_config.get("app_events", {}).get("on_app_initialization_complete", {})
        assert "MY_LIB_KEY" in init.get("secrets_to_register", {})
        # ...but the project-layer download pin did NOT leak into the global config.
        assert "libraries_to_download" not in init

    def test_persist_creates_missing_category(self, griptape_nodes: GriptapeNodes, isolate_user_config: Path) -> None:
        """A library declaring a brand-new category writes its contents verbatim to global."""
        library_manager = griptape_nodes.LibraryManager()

        library = self._library_with_settings(
            "my_library_category",
            {"some_setting": "value"},
        )
        problems = library_manager._persist_library_settings(library)

        assert problems == []
        global_config = json.loads(isolate_user_config.read_text(encoding="utf-8"))
        assert global_config.get("my_library_category", {}).get("some_setting") == "value"


class _ModelProbe(BaseNode):
    """Concrete BaseNode used to exercise model-catalog resolution end to end."""

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None) -> None:
        super().__init__(name=name, metadata=metadata)


class TestModelCatalogResolution:
    """Library.get_models_for_node_type and the get_declared_models helper against a registered catalog."""

    _LIBRARY_NAME = "model-catalog-test-library"

    @pytest.fixture(autouse=True)
    def _clean_registry(self) -> Generator[None, None, None]:
        LibraryRegistry._clear()
        yield
        LibraryRegistry._clear()

    @staticmethod
    def _catalog() -> ModelCatalogLibraryProperty:
        return ModelCatalogLibraryProperty(
            providers={
                "anthropic": ModelProvider(
                    display_name="Anthropic",
                    models={
                        "claude_opus_byok": Model(
                            display_name="Claude Opus 4 (BYOK)",
                            provider_model_id="claude-opus-4",
                            key_support=KeySupport.REQUIRES_CUSTOMER_KEY,
                        ),
                        "claude_sonnet_byok": Model(
                            display_name="Claude Sonnet 4 (BYOK)",
                            provider_model_id="claude-sonnet-4",
                            key_support=KeySupport.REQUIRES_CUSTOMER_KEY,
                        ),
                    },
                ),
                "ollama": ModelProvider(display_name="Ollama", key_support=KeySupport.NO_KEY_REQUIRED),
            },
        )

    def _register_library(self, *, node_metadata: NodeMetadata, with_catalog: bool = True) -> None:
        schema = LibrarySchema(
            name=self._LIBRARY_NAME,
            library_schema_version=LibrarySchema.LATEST_SCHEMA_VERSION,
            metadata=LibraryMetadata(
                author="test",
                description="model catalog probe library",
                library_version="1.0.0",
                engine_version="1.0.0",
                tags=[],
                declarations=[self._catalog()] if with_catalog else [],
            ),
            categories=[],
            nodes=[],
        )
        library = LibraryRegistry.generate_new_library(library_data=schema)
        library.register_new_node_type(_ModelProbe, node_metadata)

    def _node_metadata(self, **kwargs: Any) -> NodeMetadata:
        return NodeMetadata(category="test", description="probe", display_name="Probe", **kwargs)

    def test_resolves_model_usage_against_catalog(self) -> None:
        self._register_library(
            node_metadata=self._node_metadata(
                declarations=[ModelUsageNodeProperty(model_ids=["claude_opus_byok"])],
            ),
        )

        library = LibraryRegistry.get_library(name=self._LIBRARY_NAME)
        resolved = library.get_models_for_node_type(_ModelProbe.__name__)

        assert [r.model_id for r in resolved] == ["claude_opus_byok"]
        assert resolved[0].provider_id == "anthropic"

    def test_resolves_provider_usage_against_catalog(self) -> None:
        self._register_library(
            node_metadata=self._node_metadata(
                declarations=[ModelProviderUsageNodeProperty(provider_ids=["anthropic"])],
            ),
        )

        library = LibraryRegistry.get_library(name=self._LIBRARY_NAME)
        resolved = library.get_models_for_node_type(_ModelProbe.__name__)

        assert [r.model_id for r in resolved] == ["claude_opus_byok", "claude_sonnet_byok"]

    def test_no_catalog_returns_empty(self) -> None:
        self._register_library(
            node_metadata=self._node_metadata(
                declarations=[ModelUsageNodeProperty(model_ids=["claude_opus_byok"])],
            ),
            with_catalog=False,
        )

        library = LibraryRegistry.get_library(name=self._LIBRARY_NAME)

        assert library.get_models_for_node_type(_ModelProbe.__name__) == []

    def test_unknown_node_type_raises_key_error(self) -> None:
        self._register_library(node_metadata=self._node_metadata())

        library = LibraryRegistry.get_library(name=self._LIBRARY_NAME)

        with pytest.raises(KeyError, match="not found"):
            library.get_models_for_node_type("NotARegisteredNode")

    def test_get_declared_models_resolves_for_created_node(self) -> None:
        # The headline path: a node hands itself to the helper and gets back the
        # resolved models, carrying the display-name -> provider_model_id mapping
        # it needs to build a dropdown. No request, no self-identification.
        self._register_library(
            node_metadata=self._node_metadata(
                declarations=[ModelProviderUsageNodeProperty(provider_ids=["anthropic"])],
            ),
        )

        node = LibraryRegistry.create_node(
            node_type=_ModelProbe.__name__,
            name="probe-helper",
            specific_library_name=self._LIBRARY_NAME,
        )

        resolved = get_declared_models(node)

        assert [r.model_id for r in resolved] == ["claude_opus_byok", "claude_sonnet_byok"]
        assert resolved[0].model.display_name == "Claude Opus 4 (BYOK)"
        assert resolved[0].model.provider_model_id == "claude-opus-4"

    def test_get_declared_models_without_library_context_returns_empty(self) -> None:
        # A node constructed outside the library path has no injected library/type,
        # so the helper degrades to an empty list instead of raising.
        node = _ModelProbe(name="orphan")

        assert get_declared_models(node) == []

    def test_get_declared_models_unknown_library_returns_empty(self) -> None:
        node = _ModelProbe(name="stale", metadata={"library": "no-such-library", "node_type": _ModelProbe.__name__})

        assert get_declared_models(node) == []


class TestLibraryManagerMetadataLoadFailureSurfacing:
    """A failed metadata load must surface its real status and problems on the LibraryInfo.

    Regression guard: a metadata-load failure used to be stored back unchanged, leaving the
    library at its pre-load defaults so status output rendered it as
    "*UNKNOWN* v*UNKNOWN* (PENDING) - No problems detected." even though the load failed.
    """

    @pytest.mark.asyncio
    async def test_schema_validation_failure_surfaces_on_library_info(
        self, griptape_nodes: GriptapeNodes, tmp_path: Path
    ) -> None:
        from griptape_nodes.retained_mode.managers.library_manager import LibraryManager

        library_manager = griptape_nodes.LibraryManager()

        # A library JSON with a usable name and version but a schema-violating body (missing
        # required categories/nodes), mirroring the user's "settings.0.category" failure.
        lib_dir = tmp_path / "broken"
        lib_dir.mkdir()
        lib_json = lib_dir / "griptape_nodes_library.json"
        lib_json.write_text(
            json.dumps({"name": "broken-lib", "metadata": {"library_version": "1.2.3"}, "settings": [{"foo": "bar"}]})
        )
        file_path = str(lib_json)

        library_info = LibraryManager.LibraryInfo(
            lifecycle_state=LibraryManager.LibraryLifecycleState.DISCOVERED,
            fitness=LibraryManager.LibraryFitness.NOT_EVALUATED,
            library_path=file_path,
            is_sandbox=False,
        )
        library_manager._library_file_path_to_info = {file_path: library_info}

        result = await library_manager._progress_library_through_lifecycle(
            library_info, file_path, RegisterLibraryFromFileRequest(file_path=file_path)
        )

        assert isinstance(result, RegisterLibraryFromFileResultFailure)

        stored = library_manager._library_file_path_to_info[file_path]
        assert stored.lifecycle_state == LibraryManager.LibraryLifecycleState.FAILURE
        assert stored.fitness == LibraryManager.LibraryFitness.UNUSABLE
        # Name is extracted from the raw JSON rather than left as None (*UNKNOWN*).
        assert stored.library_name == "broken-lib"
        # Version is extracted from the raw JSON rather than left as None (v*UNKNOWN*).
        assert stored.library_version == "1.2.3"
        # Problems are recorded, so status output shows the real error instead of
        # "No problems detected."
        assert stored.problems
        assert library_manager.collate_problems_for_lib_info(stored) is not None

    @pytest.mark.asyncio
    async def test_missing_file_surfaces_missing_fitness(self, griptape_nodes: GriptapeNodes, tmp_path: Path) -> None:
        from griptape_nodes.retained_mode.managers.library_manager import LibraryManager

        library_manager = griptape_nodes.LibraryManager()

        file_path = str(tmp_path / "does_not_exist" / "griptape_nodes_library.json")
        library_info = LibraryManager.LibraryInfo(
            lifecycle_state=LibraryManager.LibraryLifecycleState.DISCOVERED,
            fitness=LibraryManager.LibraryFitness.NOT_EVALUATED,
            library_path=file_path,
            is_sandbox=False,
        )
        library_manager._library_file_path_to_info = {file_path: library_info}

        result = await library_manager._progress_library_through_lifecycle(
            library_info, file_path, RegisterLibraryFromFileRequest(file_path=file_path)
        )

        assert isinstance(result, RegisterLibraryFromFileResultFailure)

        stored = library_manager._library_file_path_to_info[file_path]
        assert stored.lifecycle_state == LibraryManager.LibraryLifecycleState.FAILURE
        assert stored.fitness == LibraryManager.LibraryFitness.MISSING
        assert stored.problems


class TestCollectLibraryLoadStatuses:
    """_collect_library_load_statuses turns LibraryInfo into serializable status data."""

    def test_maps_fields_and_disabled_state(self, griptape_nodes: GriptapeNodes) -> None:
        from griptape_nodes.retained_mode.managers.library_manager import LibraryManager

        library_manager = griptape_nodes.LibraryManager()

        good = LibraryManager.LibraryInfo(
            lifecycle_state=LibraryManager.LibraryLifecycleState.LOADED,
            fitness=LibraryManager.LibraryFitness.GOOD,
            library_path="/libs/good.json",
            is_sandbox=False,
            library_name="Good Library",
            library_version="1.2.3",
        )
        disabled = LibraryManager.LibraryInfo(
            lifecycle_state=LibraryManager.LibraryLifecycleState.DISABLED,
            fitness=LibraryManager.LibraryFitness.NOT_EVALUATED,
            library_path="/libs/off.json",
            is_sandbox=False,
            library_name="Disabled Library",
            library_version=None,
        )
        library_manager._library_file_path_to_info = {
            "/libs/good.json": good,
            "/libs/off.json": disabled,
        }

        statuses = library_manager._collect_library_load_statuses()

        # Unpacking enforces exactly two statuses were produced.
        good_status, disabled_status = statuses

        assert good_status.library_name == "Good Library"
        assert good_status.library_version == "1.2.3"
        assert good_status.library_path == "/libs/good.json"
        assert good_status.fitness == "GOOD"
        assert good_status.disabled is False
        assert good_status.problems is None

        assert disabled_status.disabled is True
        assert disabled_status.library_version is None

    def test_empty_when_no_libraries(self, griptape_nodes: GriptapeNodes) -> None:
        library_manager = griptape_nodes.LibraryManager()
        library_manager._library_file_path_to_info = {}

        assert library_manager._collect_library_load_statuses() == []


class TestLibraryFitnessAuthorizationCheckpoint:
    """The license-policy checkpoint wired into library fitness evaluation."""

    @staticmethod
    def _schema(name: str, stage: "LifecycleStage | None" = None) -> "LibrarySchema":
        from griptape_nodes.node_library.library_declarations import (
            LibraryDeclaration,
            LifecycleStageLibraryProperty,
        )

        declarations: list[LibraryDeclaration] = (
            [LifecycleStageLibraryProperty(stage=stage)] if stage is not None else []
        )
        return LibrarySchema(
            name=name,
            library_schema_version=LibrarySchema.LATEST_SCHEMA_VERSION,
            metadata=LibraryMetadata(
                author="test",
                description="d",
                library_version="1.0.0",
                engine_version="1.0.0",
                tags=[],
                declarations=declarations,
            ),
            categories=[],
            nodes=[],
        )

    def test_denied_library_is_unusable_with_permission_problem(self, griptape_nodes: GriptapeNodes) -> None:
        from griptape_nodes.retained_mode.events.library_events import (
            EvaluateLibraryFitnessRequest,
            EvaluateLibraryFitnessResultFailure,
        )
        from griptape_nodes.retained_mode.managers.authorization_checkpoint import (
            CheckpointDenial,
            CheckpointFailure,
        )
        from griptape_nodes.retained_mode.managers.fitness_problems.libraries import PermissionDeniedProblem

        seen: dict[str, object] = {}

        def deny(checkpoint: object) -> CheckpointDenial:
            seen["action"] = checkpoint.action  # type: ignore[attr-defined]
            seen["subject_id"] = checkpoint.subject_id  # type: ignore[attr-defined]
            seen["stage"] = checkpoint.attributes.get("lifecycle_stage")  # type: ignore[attr-defined]
            return CheckpointDenial(
                failures=(CheckpointFailure(detail="Ask your admin to enable Labs libraries.", capability="lib:labs"),)
            )

        griptape_nodes.EventManager().add_authorization_hook(deny)
        with patch(
            "griptape_nodes.retained_mode.managers.version_compatibility_manager.VersionCompatibilityManager.check_library_version_compatibility",
            return_value=[],
        ):
            result = griptape_nodes.LibraryManager().evaluate_library_fitness_request(
                EvaluateLibraryFitnessRequest(schema=self._schema("blocked-lib", LifecycleStage.LABS))
            )

        assert seen == {"action": "LoadLibrary", "subject_id": "blocked-lib", "stage": "LABS"}
        assert isinstance(result, EvaluateLibraryFitnessResultFailure)
        assert result.fitness == _LibraryManager.LibraryFitness.UNUSABLE
        problems = [p for p in result.problems if isinstance(p, PermissionDeniedProblem)]
        assert len(problems) == 1
        assert "Ask your admin to enable Labs libraries." in problems[0].collate_problems_for_display(problems)

    def test_allowed_library_passes(self, griptape_nodes: GriptapeNodes) -> None:
        from griptape_nodes.retained_mode.events.library_events import (
            EvaluateLibraryFitnessRequest,
            EvaluateLibraryFitnessResultSuccess,
        )

        # No authorization hook registered -> the checkpoint allows.
        with patch(
            "griptape_nodes.retained_mode.managers.version_compatibility_manager.VersionCompatibilityManager.check_library_version_compatibility",
            return_value=[],
        ):
            result = griptape_nodes.LibraryManager().evaluate_library_fitness_request(
                EvaluateLibraryFitnessRequest(schema=self._schema("ok-lib"))
            )
        assert isinstance(result, EvaluateLibraryFitnessResultSuccess)

    def test_denied_node_is_a_library_problem_but_library_stays_usable(self, griptape_nodes: GriptapeNodes) -> None:
        from griptape_nodes.node_library.library_declarations import LifecycleStage, LifecycleStageNodeProperty
        from griptape_nodes.node_library.library_registry import NodeDefinition, NodeMetadata
        from griptape_nodes.retained_mode.events.library_events import (
            EvaluateLibraryFitnessRequest,
            EvaluateLibraryFitnessResultSuccess,
        )
        from griptape_nodes.retained_mode.managers.authorization_checkpoint import CheckpointDenial, CheckpointFailure
        from griptape_nodes.retained_mode.managers.fitness_problems.libraries import NodePermissionDeniedProblem

        # Deny only the node (by lifecycle stage); the library itself is allowed.
        def deny(checkpoint: object) -> CheckpointDenial | None:
            if checkpoint.action == "InstantiateNode":  # type: ignore[attr-defined]
                return CheckpointDenial(failures=(CheckpointFailure(detail="Ask your admin to enable Labs nodes."),))
            return None

        schema = self._schema("mixed-lib")
        schema.nodes.append(
            NodeDefinition(
                class_name="LabsNode",
                file_path="labs.py",
                metadata=NodeMetadata(
                    category="t",
                    description="d",
                    display_name="Labs",
                    declarations=[LifecycleStageNodeProperty(stage=LifecycleStage.LABS)],
                ),
            )
        )

        griptape_nodes.EventManager().add_authorization_hook(deny)
        with patch(
            "griptape_nodes.retained_mode.managers.version_compatibility_manager.VersionCompatibilityManager.check_library_version_compatibility",
            return_value=[],
        ):
            result = griptape_nodes.LibraryManager().evaluate_library_fitness_request(
                EvaluateLibraryFitnessRequest(schema=schema)
            )

        # The library is permitted, so it stays usable (registered), but the denied
        # node is surfaced as a library problem rather than silently dropped.
        assert isinstance(result, EvaluateLibraryFitnessResultSuccess)
        assert result.fitness == _LibraryManager.LibraryFitness.FLAWED
        problems = [p for p in result.problems if isinstance(p, NodePermissionDeniedProblem)]
        assert len(problems) == 1
        assert problems[0].node_type == "LabsNode"
        assert "Ask your admin to enable Labs nodes." in problems[0].collate_problems_for_display(problems)
