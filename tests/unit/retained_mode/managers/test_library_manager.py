import asyncio
import contextlib
import json
import logging
import subprocess
import sys
import threading
from collections.abc import Callable, Generator
from functools import partial
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from griptape_nodes.exe_types.core_types import (
    ControlParameterInput,
    ControlParameterOutput,
    Parameter,
    ParameterList,
    ParameterMode,
)
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
    LibraryRegistryError,
    LibrarySchema,
    NodeMetadata,
    get_declared_models,
)
from griptape_nodes.retained_mode.engine import Engine
from griptape_nodes.retained_mode.events.base_events import ResultDetails
from griptape_nodes.retained_mode.events.library_events import (
    DescribeNodeTypeRequest,
    DescribeNodeTypeResultFailure,
    DescribeNodeTypeResultSuccess,
    GetAllInfoForAllLibrariesRequest,
    GetAllInfoForAllLibrariesResultFailure,
    GetAllInfoForAllLibrariesResultSuccess,
    GetAllInfoForLibraryRequest,
    GetPortSummariesForAllLibrariesRequest,
    GetPortSummariesForAllLibrariesResultSuccess,
    InstallLibraryDependenciesRequest,
    InstallLibraryDependenciesResultFailure,
    InstallLibraryDependenciesResultSuccess,
    ListRegisteredLibrariesRequest,
    ListRegisteredLibrariesResultSuccess,
    LoadLibrariesRequest,
    LoadLibrariesResultSuccess,
    LoadLibraryMetadataFromFileResultSuccess,
    NodePortSummary,
    RegisterLibraryFromFileRequest,
    RegisterLibraryFromFileResultFailure,
    RegisterLibraryFromFileResultSuccess,
    UnloadLibraryFromRegistryRequest,
    UnloadLibraryFromRegistryResultSuccess,
)
from griptape_nodes.retained_mode.managers.config_manager import ConfigManager
from griptape_nodes.retained_mode.managers.library_manager import LibraryManager as _LibraryManager
from griptape_nodes.retained_mode.managers.library_manager import (
    LibraryVenvInitResult,
    PortSummaryCacheEntry,
    ProbeTimeoutAllowance,
)
from griptape_nodes.retained_mode.managers.project_manager import SYSTEM_DEFAULTS_KEY
from griptape_nodes.retained_mode.managers.settings import (
    LIBRARIES_TO_DOWNLOAD_KEY,
    LIBRARIES_TO_REGISTER_KEY,
    LIBRARY_WARM_PORT_SUMMARIES_KEY,
    LibraryDownload,
    LibraryRegistration,
)
from griptape_nodes.utils.file_utils import DEFAULT_MAX_SEARCH_DEPTH
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
    async def test_libraries_already_loaded_returns_success_without_reloading(self, engine: Engine) -> None:
        """Test that when libraries are already loaded, returns success without reloading."""
        library_manager = engine.library_manager

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
    async def test_empty_libraries_loads_from_config_successfully(self, engine: Engine) -> None:
        """Test successful library loading from configuration."""
        library_manager = engine.library_manager

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
    async def test_library_loading_failure_returns_failure_result(self, engine: Engine) -> None:
        """Test library loading failure returns appropriate error."""
        library_manager = engine.library_manager

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
        self, engine: Engine, lib_files: tuple[Path, Path]
    ) -> None:
        """Object-shaped entries with enabled=False produce disabled register entries."""
        library_manager = engine.library_manager
        enabled_lib, disabled_lib = lib_files

        config = [
            str(enabled_lib),
            {"path": str(disabled_lib), "enabled": False},
        ]

        with patch.object(engine.config_manager, "get_config_value", side_effect=_register_only_config(config)):
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
        self, engine: Engine, lib_files: tuple[Path, Path]
    ) -> None:
        """Bare path strings continue to be treated as enabled."""
        library_manager = engine.library_manager
        enabled_lib, _ = lib_files

        with patch.object(
            engine.config_manager, "get_config_value", side_effect=_register_only_config([str(enabled_lib)])
        ):
            result = await library_manager._discover_library_files()

        assert len(result) == 1
        assert result[0].registration.enabled is True

    @pytest.mark.asyncio
    async def test_discover_libraries_request_marks_disabled_lifecycle(
        self, engine: Engine, lib_files: tuple[Path, Path]
    ) -> None:
        """discover_libraries_request creates LibraryInfo with DISABLED lifecycle for disabled entries."""
        from griptape_nodes.retained_mode.events.library_events import DiscoverLibrariesRequest
        from griptape_nodes.retained_mode.managers.library_manager import LibraryManager

        library_manager = engine.library_manager
        enabled_lib, disabled_lib = lib_files

        config = [str(enabled_lib), {"path": str(disabled_lib), "enabled": False}]
        # Reset tracking so this test does not depend on prior state.
        library_manager._library_file_path_to_info = {}

        with patch.object(engine.config_manager, "get_config_value", side_effect=_register_only_config(config)):
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
        self, engine: Engine, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Entries that are neither strings nor dicts with a path are skipped."""
        library_manager = engine.library_manager

        config = [42, {"enabled": True}]  # missing 'path', and a bare int

        with (
            patch.object(engine.config_manager, "get_config_value", return_value=config),
            caplog.at_level(logging.WARNING, logger="griptape_nodes"),
        ):
            result = await library_manager._discover_library_files()

        assert result == []
        warnings = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert any("libraries_to_register" in m for m in warnings)

    @pytest.mark.asyncio
    async def test_rediscovery_reconciles_toggled_enabled_flag(
        self, engine: Engine, lib_files: tuple[Path, Path]
    ) -> None:
        """Re-running discovery after a refresh updates lifecycle when the user toggles enabled.

        Refreshing libraries (ReloadAllLibrariesRequest) does not unload entries that were
        never registered with LibraryRegistry, such as DISABLED entries. The follow-up
        discovery must therefore reconcile the lifecycle state itself; otherwise a library
        flipped from disabled to enabled (or back) would never get picked up.
        """
        from griptape_nodes.retained_mode.events.library_events import DiscoverLibrariesRequest
        from griptape_nodes.retained_mode.managers.library_manager import LibraryManager

        library_manager = engine.library_manager
        first_lib, second_lib = lib_files
        # Reset tracking so this test does not depend on prior state.
        library_manager._library_file_path_to_info = {}

        # Initial discovery: first_lib enabled, second_lib disabled.
        initial_config = [str(first_lib), {"path": str(second_lib), "enabled": False}]
        with patch.object(engine.config_manager, "get_config_value", side_effect=_register_only_config(initial_config)):
            await library_manager.discover_libraries_request(DiscoverLibrariesRequest(include_sandbox=False))

        first_state = library_manager._library_file_path_to_info[str(first_lib)].lifecycle_state
        second_state = library_manager._library_file_path_to_info[str(second_lib)].lifecycle_state
        assert first_state != LibraryManager.LibraryLifecycleState.DISABLED
        assert second_state == LibraryManager.LibraryLifecycleState.DISABLED

        # User flips the config: first_lib disabled, second_lib enabled, then triggers refresh.
        toggled_config = [{"path": str(first_lib), "enabled": False}, str(second_lib)]
        with patch.object(engine.config_manager, "get_config_value", side_effect=_register_only_config(toggled_config)):
            await library_manager.discover_libraries_request(DiscoverLibrariesRequest(include_sandbox=False))

        first_state_after = library_manager._library_file_path_to_info[str(first_lib)].lifecycle_state
        second_state_after = library_manager._library_file_path_to_info[str(second_lib)].lifecycle_state
        assert first_state_after == LibraryManager.LibraryLifecycleState.DISABLED
        assert second_state_after != LibraryManager.LibraryLifecycleState.DISABLED


class TestLibraryManagerMigrateOldXdgPaths:
    """Test the _migrate_old_xdg_library_paths functionality in LibraryManager."""

    def test_removes_old_xdg_paths_and_preserves_valid_paths(self, engine: Engine) -> None:
        """Test that old XDG paths are removed while valid paths are preserved."""
        library_manager = engine.library_manager

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
            patch.object(engine, "_config_manager", mock_config_manager),
            patch("griptape_nodes.utils.library_utils.xdg_data_home") as mock_xdg,
        ):
            mock_xdg.return_value = Path("/home/user/.local/share")

            library_manager._migrate_old_xdg_library_paths()

            # Verify both configs were updated
            assert mock_config_manager.set_config_value.call_count == 2  # noqa: PLR2004
            calls = mock_config_manager.set_config_value.call_args_list
            register_call = next(c for c in calls if "libraries_to_register" in c[0][0])
            assert register_call[0][1] == [valid_path]

    def test_idempotent_with_no_old_paths(self, engine: Engine) -> None:
        """Test that migration does nothing when config has no old XDG paths."""
        library_manager = engine.library_manager

        # Mock config with only valid paths (no old XDG paths)
        valid_paths = ["/custom/path/library1", "https://github.com/user/library@main"]

        mock_config_manager = MagicMock()
        mock_config_manager.get_config_value.return_value = valid_paths

        with (
            patch.object(engine, "_config_manager", mock_config_manager),
            patch("griptape_nodes.utils.library_utils.xdg_data_home") as mock_xdg,
        ):
            mock_xdg.return_value = Path("/home/user/.local/share")

            library_manager._migrate_old_xdg_library_paths()

            # Verify config was NOT updated (no old paths to remove)
            mock_config_manager.set_config_value.assert_not_called()

    def test_handles_empty_config_gracefully(self, engine: Engine) -> None:
        """Test that migration returns early when config is empty."""
        library_manager = engine.library_manager

        mock_config_manager = MagicMock()
        mock_config_manager.get_config_value.return_value = []

        with patch.object(engine, "_config_manager", mock_config_manager):
            library_manager._migrate_old_xdg_library_paths()

            # Verify config was NOT updated (empty config)
            mock_config_manager.set_config_value.assert_not_called()

    def test_handles_none_config_gracefully(self, engine: Engine) -> None:
        """Test that migration returns early when config is None."""
        library_manager = engine.library_manager

        mock_config_manager = MagicMock()
        mock_config_manager.get_config_value.return_value = None

        with patch.object(engine, "_config_manager", mock_config_manager):
            library_manager._migrate_old_xdg_library_paths()

            # Verify config was NOT updated (None config)
            mock_config_manager.set_config_value.assert_not_called()

    def test_removes_all_three_old_library_paths(self, engine: Engine) -> None:
        """Test that all three old XDG library types are removed."""
        library_manager = engine.library_manager

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
            patch.object(engine, "_config_manager", mock_config_manager),
            patch("griptape_nodes.utils.library_utils.xdg_data_home") as mock_xdg,
        ):
            mock_xdg.return_value = Path("/home/user/.local/share")

            library_manager._migrate_old_xdg_library_paths()

            # Verify all old paths removed, only valid path remains
            assert mock_config_manager.set_config_value.call_count == 2  # noqa: PLR2004
            calls = mock_config_manager.set_config_value.call_args_list
            register_call = next(c for c in calls if "libraries_to_register" in c[0][0])
            assert register_call[0][1] == [valid_path]

    def test_preserves_custom_paths_and_git_urls(self, engine: Engine) -> None:
        """Test that custom paths and git URLs are preserved during migration."""
        library_manager = engine.library_manager

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
            patch.object(engine, "_config_manager", mock_config_manager),
            patch("griptape_nodes.utils.library_utils.xdg_data_home") as mock_xdg,
        ):
            mock_xdg.return_value = Path("/home/user/.local/share")

            library_manager._migrate_old_xdg_library_paths()

            # Verify only old XDG path removed, custom and git URL preserved
            assert mock_config_manager.set_config_value.call_count == 2  # noqa: PLR2004
            calls = mock_config_manager.set_config_value.call_args_list
            register_call = next(c for c in calls if "libraries_to_register" in c[0][0])
            assert register_call[0][1] == [custom_path, git_url]

    def test_adds_git_urls_to_downloads_when_xdg_paths_removed(self, engine: Engine) -> None:
        """Test that migration adds git URLs to downloads when XDG paths are removed."""
        library_manager = engine.library_manager

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
            patch.object(engine, "_config_manager", mock_config_manager),
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

    def test_doesnt_duplicate_existing_git_urls(self, engine: Engine) -> None:
        """Test that migration doesn't add URLs already in downloads."""
        library_manager = engine.library_manager

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
            patch.object(engine, "_config_manager", mock_config_manager),
            patch("griptape_nodes.utils.library_utils.xdg_data_home") as mock_xdg,
        ):
            mock_xdg.return_value = Path("/home/user/.local/share")

            library_manager._migrate_old_xdg_library_paths()

            # Verify only register was updated, downloads unchanged (no duplicate)
            assert mock_config_manager.set_config_value.call_count == 1
            call_args = mock_config_manager.set_config_value.call_args
            assert "libraries_to_register" in call_args[0][0]
            assert call_args[0][1] == []

    def test_handles_multiple_libraries(self, engine: Engine) -> None:
        """Test migration with all three library types."""
        library_manager = engine.library_manager

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
            patch.object(engine, "_config_manager", mock_config_manager),
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

    def test_handles_partial_overlap(self, engine: Engine) -> None:
        """Test when some URLs already exist in downloads."""
        library_manager = engine.library_manager

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
            patch.object(engine, "_config_manager", mock_config_manager),
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
    async def test_always_installs_dependencies_even_when_venv_exists(self, engine: Engine) -> None:
        """Test that dependencies are always installed on library load, even when venv already exists."""
        library_manager = engine.library_manager

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
                is_registered=False,
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
    async def test_dependency_installation_failure_returns_failure(self, engine: Engine) -> None:
        """Test that dependency installation failure returns RegisterLibraryFromFileResultFailure."""
        mgr = engine.library_manager
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
                    is_registered=False,
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
            is_registered=False,
            result_details=ResultDetails(message="OK", level=20),
        )

    @pytest.mark.asyncio
    async def test_creates_venv_when_pip_dependencies_is_empty(self, engine: Engine) -> None:
        """Test that the venv is created even when pip_dependencies is empty."""
        mgr = engine.library_manager
        schema = MagicMock()
        schema.name = "test_lib"
        schema.metadata.library_version = "1.0.0"
        schema.metadata.dependencies.pip_dependencies = []
        schema.metadata.dependencies.pip_install_flags = []

        with (
            patch.object(mgr, "load_library_metadata_from_file_request", return_value=self._metadata_result(schema)),
            patch.object(mgr, "_get_library_venv_path", return_value=MagicMock()),
            patch.object(
                mgr,
                "_init_library_venv",
                new_callable=AsyncMock,
                return_value=LibraryVenvInitResult(python_path=MagicMock(), reused=False),
            ) as mock_init_venv,
            patch.object(mgr, "_can_write_to_venv_location", return_value=True),
            patch(
                "griptape_nodes.retained_mode.managers.library_manager.OSManager.check_available_disk_space",
                return_value=True,
            ),
            patch.object(engine.config_manager, "get_config_value", return_value=5.0),
        ):
            result = await mgr.install_library_dependencies_request(
                InstallLibraryDependenciesRequest(library_file_path="/mock.json")
            )

        mock_init_venv.assert_called_once()
        assert isinstance(result, InstallLibraryDependenciesResultSuccess)
        assert result.dependencies_installed == 0

    @pytest.mark.asyncio
    async def test_creates_venv_when_dependencies_is_none(self, engine: Engine) -> None:
        """Test that the venv is created even when the dependencies section is absent."""
        mgr = engine.library_manager
        schema = MagicMock()
        schema.name = "test_lib"
        schema.metadata.library_version = "1.0.0"
        schema.metadata.dependencies = None

        with (
            patch.object(mgr, "load_library_metadata_from_file_request", return_value=self._metadata_result(schema)),
            patch.object(mgr, "_get_library_venv_path", return_value=MagicMock()),
            patch.object(
                mgr,
                "_init_library_venv",
                new_callable=AsyncMock,
                return_value=LibraryVenvInitResult(python_path=MagicMock(), reused=False),
            ) as mock_init_venv,
            patch.object(mgr, "_can_write_to_venv_location", return_value=True),
            patch(
                "griptape_nodes.retained_mode.managers.library_manager.OSManager.check_available_disk_space",
                return_value=True,
            ),
            patch.object(engine.config_manager, "get_config_value", return_value=5.0),
        ):
            result = await mgr.install_library_dependencies_request(
                InstallLibraryDependenciesRequest(library_file_path="/mock.json")
            )

        mock_init_venv.assert_called_once()
        assert isinstance(result, InstallLibraryDependenciesResultSuccess)
        assert result.dependencies_installed == 0

    @pytest.mark.asyncio
    async def test_returns_failure_when_venv_creation_fails_with_no_deps(self, engine: Engine) -> None:
        """Test that venv creation failure returns failure even when pip_dependencies is empty."""
        mgr = engine.library_manager
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
    async def test_returns_failure_when_venv_unwritable_with_no_deps(self, engine: Engine) -> None:
        """Test that an unwritable venv returns failure even when pip_dependencies is empty."""
        mgr = engine.library_manager
        schema = MagicMock()
        schema.name = "test_lib"
        schema.metadata.library_version = "1.0.0"
        schema.metadata.dependencies.pip_dependencies = []
        schema.metadata.dependencies.pip_install_flags = []

        with (
            patch.object(mgr, "load_library_metadata_from_file_request", return_value=self._metadata_result(schema)),
            patch.object(mgr, "_get_library_venv_path", return_value=MagicMock()),
            patch.object(
                mgr,
                "_init_library_venv",
                new_callable=AsyncMock,
                return_value=LibraryVenvInitResult(python_path=MagicMock(), reused=False),
            ),
            patch.object(mgr, "_can_write_to_venv_location", return_value=False),
        ):
            result = await mgr.install_library_dependencies_request(
                InstallLibraryDependenciesRequest(library_file_path="/mock.json")
            )

        assert isinstance(result, InstallLibraryDependenciesResultFailure)

    @pytest.mark.asyncio
    async def test_returns_failure_when_insufficient_disk_space_with_no_deps(self, engine: Engine) -> None:
        """Test that insufficient disk space returns failure even when pip_dependencies is empty."""
        mgr = engine.library_manager
        schema = MagicMock()
        schema.name = "test_lib"
        schema.metadata.library_version = "1.0.0"
        schema.metadata.dependencies.pip_dependencies = []
        schema.metadata.dependencies.pip_install_flags = []

        with (
            patch.object(mgr, "load_library_metadata_from_file_request", return_value=self._metadata_result(schema)),
            patch.object(mgr, "_get_library_venv_path", return_value=MagicMock()),
            patch.object(
                mgr,
                "_init_library_venv",
                new_callable=AsyncMock,
                return_value=LibraryVenvInitResult(python_path=MagicMock(), reused=False),
            ),
            patch.object(mgr, "_can_write_to_venv_location", return_value=True),
            patch(
                "griptape_nodes.retained_mode.managers.library_manager.OSManager.check_available_disk_space",
                return_value=False,
            ),
            patch(
                "griptape_nodes.retained_mode.managers.library_manager.OSManager.format_disk_space_error",
                return_value="not enough space",
            ),
            patch.object(engine.config_manager, "get_config_value", return_value=5.0),
        ):
            result = await mgr.install_library_dependencies_request(
                InstallLibraryDependenciesRequest(library_file_path="/mock.json")
            )

        assert isinstance(result, InstallLibraryDependenciesResultFailure)

    @pytest.mark.asyncio
    async def test_reused_venv_with_successful_install_is_not_rebuilt(self, engine: Engine) -> None:
        """A reused venv whose first install succeeds must not be rebuilt."""
        mgr = engine.library_manager
        schema = MagicMock()
        schema.name = "test_lib"
        schema.metadata.library_version = "1.0.0"
        schema.metadata.dependencies.pip_dependencies = ["a==1"]
        schema.metadata.dependencies.pip_install_flags = []

        with (
            patch.object(mgr, "load_library_metadata_from_file_request", return_value=self._metadata_result(schema)),
            patch.object(mgr, "_get_library_venv_path", return_value=MagicMock()),
            patch.object(
                mgr,
                "_init_library_venv",
                new_callable=AsyncMock,
                return_value=LibraryVenvInitResult(python_path=MagicMock(), reused=True),
            ),
            patch.object(mgr, "_reset_and_init_library_venv", new_callable=AsyncMock) as mock_reset,
            patch.object(mgr, "_can_write_to_venv_location", return_value=True),
            patch(
                "griptape_nodes.retained_mode.managers.library_manager.OSManager.check_available_disk_space",
                return_value=True,
            ),
            patch(
                "griptape_nodes.retained_mode.managers.library_manager.subprocess_run",
                new_callable=AsyncMock,
            ) as mock_subprocess,
            patch.object(engine.config_manager, "get_config_value", side_effect=_fake_config_value),
        ):
            result = await mgr.install_library_dependencies_request(
                InstallLibraryDependenciesRequest(library_file_path="/mock.json")
            )

        assert isinstance(result, InstallLibraryDependenciesResultSuccess)
        assert result.dependencies_installed == 1
        mock_reset.assert_not_called()
        mock_subprocess.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_rebuilds_reused_venv_and_retries_when_install_fails(self, engine: Engine) -> None:
        """A reused venv that fails to install is rebuilt once and the install retried."""
        mgr = engine.library_manager
        schema = MagicMock()
        schema.name = "test_lib"
        schema.metadata.library_version = "1.0.0"
        schema.metadata.dependencies.pip_dependencies = ["a==1"]
        schema.metadata.dependencies.pip_install_flags = []
        expected_attempts = 2

        with (
            patch.object(mgr, "load_library_metadata_from_file_request", return_value=self._metadata_result(schema)),
            patch.object(mgr, "_get_library_venv_path", return_value=MagicMock()),
            patch.object(
                mgr,
                "_init_library_venv",
                new_callable=AsyncMock,
                return_value=LibraryVenvInitResult(python_path=MagicMock(), reused=True),
            ),
            patch.object(
                mgr, "_reset_and_init_library_venv", new_callable=AsyncMock, return_value=MagicMock()
            ) as mock_reset,
            patch.object(mgr, "_can_write_to_venv_location", return_value=True),
            patch(
                "griptape_nodes.retained_mode.managers.library_manager.OSManager.check_available_disk_space",
                return_value=True,
            ),
            patch(
                "griptape_nodes.retained_mode.managers.library_manager.subprocess_run",
                new_callable=AsyncMock,
                side_effect=[
                    subprocess.CalledProcessError(returncode=2, cmd=["uv"], stderr="corrupt METADATA"),
                    MagicMock(),
                ],
            ) as mock_subprocess,
            patch.object(engine.config_manager, "get_config_value", side_effect=_fake_config_value),
        ):
            result = await mgr.install_library_dependencies_request(
                InstallLibraryDependenciesRequest(library_file_path="/mock.json")
            )

        assert isinstance(result, InstallLibraryDependenciesResultSuccess)
        assert result.dependencies_installed == 1
        mock_reset.assert_called_once()
        assert mock_subprocess.await_count == expected_attempts

    @pytest.mark.asyncio
    async def test_does_not_rebuild_freshly_built_venv_on_install_failure(self, engine: Engine) -> None:
        """A freshly built venv that fails to install fails fast without a rebuild."""
        mgr = engine.library_manager
        schema = MagicMock()
        schema.name = "test_lib"
        schema.metadata.library_version = "1.0.0"
        schema.metadata.dependencies.pip_dependencies = ["a==1"]
        schema.metadata.dependencies.pip_install_flags = []

        with (
            patch.object(mgr, "load_library_metadata_from_file_request", return_value=self._metadata_result(schema)),
            patch.object(mgr, "_get_library_venv_path", return_value=MagicMock()),
            patch.object(
                mgr,
                "_init_library_venv",
                new_callable=AsyncMock,
                return_value=LibraryVenvInitResult(python_path=MagicMock(), reused=False),
            ),
            patch.object(mgr, "_reset_and_init_library_venv", new_callable=AsyncMock) as mock_reset,
            patch.object(mgr, "_can_write_to_venv_location", return_value=True),
            patch(
                "griptape_nodes.retained_mode.managers.library_manager.OSManager.check_available_disk_space",
                return_value=True,
            ),
            patch(
                "griptape_nodes.retained_mode.managers.library_manager.subprocess_run",
                new_callable=AsyncMock,
                side_effect=subprocess.CalledProcessError(returncode=2, cmd=["uv"], stderr="bad package"),
            ) as mock_subprocess,
            patch.object(engine.config_manager, "get_config_value", side_effect=_fake_config_value),
        ):
            result = await mgr.install_library_dependencies_request(
                InstallLibraryDependenciesRequest(library_file_path="/mock.json")
            )

        assert isinstance(result, InstallLibraryDependenciesResultFailure)
        mock_reset.assert_not_called()
        mock_subprocess.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_returns_failure_when_install_fails_after_rebuild(self, engine: Engine) -> None:
        """If the install still fails after the venv rebuild, the request fails."""
        mgr = engine.library_manager
        schema = MagicMock()
        schema.name = "test_lib"
        schema.metadata.library_version = "1.0.0"
        schema.metadata.dependencies.pip_dependencies = ["a==1"]
        schema.metadata.dependencies.pip_install_flags = []
        expected_attempts = 2

        with (
            patch.object(mgr, "load_library_metadata_from_file_request", return_value=self._metadata_result(schema)),
            patch.object(mgr, "_get_library_venv_path", return_value=MagicMock()),
            patch.object(
                mgr,
                "_init_library_venv",
                new_callable=AsyncMock,
                return_value=LibraryVenvInitResult(python_path=MagicMock(), reused=True),
            ),
            patch.object(
                mgr, "_reset_and_init_library_venv", new_callable=AsyncMock, return_value=MagicMock()
            ) as mock_reset,
            patch.object(mgr, "_can_write_to_venv_location", return_value=True),
            patch(
                "griptape_nodes.retained_mode.managers.library_manager.OSManager.check_available_disk_space",
                return_value=True,
            ),
            patch(
                "griptape_nodes.retained_mode.managers.library_manager.subprocess_run",
                new_callable=AsyncMock,
                side_effect=[
                    subprocess.CalledProcessError(returncode=2, cmd=["uv"], stderr="corrupt METADATA"),
                    subprocess.CalledProcessError(returncode=2, cmd=["uv"], stderr="still broken"),
                ],
            ) as mock_subprocess,
            patch.object(engine.config_manager, "get_config_value", side_effect=_fake_config_value),
        ):
            result = await mgr.install_library_dependencies_request(
                InstallLibraryDependenciesRequest(library_file_path="/mock.json")
            )

        assert isinstance(result, InstallLibraryDependenciesResultFailure)
        mock_reset.assert_called_once()
        assert mock_subprocess.await_count == expected_attempts


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
    async def test_init_reuses_functional_venv_without_running_uv(self, engine: Engine, tmp_path: Path) -> None:
        mgr = engine.library_manager
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

        assert python_path.python_path == expected_python
        assert python_path.reused is True
        mock_subprocess.assert_not_called()
        mock_find_uv.assert_not_called()
        assert (venv_path / "pyvenv.cfg").exists()

    @pytest.mark.asyncio
    async def test_init_recreates_broken_venv(self, engine: Engine, tmp_path: Path) -> None:
        """A directory at the venv path that is missing the python executable must be recreated."""
        mgr = engine.library_manager
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
            patch.object(engine.config_manager, "get_config_value", side_effect=_fake_config_value),
        ):
            python_path = await mgr._init_library_venv(venv_path)

        mock_subprocess.assert_called_once()
        assert python_path.python_path == recreated_python_path["path"]
        assert python_path.reused is False
        assert not (venv_path / "stray.txt").exists()

    @pytest.mark.asyncio
    async def test_init_creates_venv_when_directory_absent(self, engine: Engine, tmp_path: Path) -> None:
        mgr = engine.library_manager
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
            patch.object(engine.config_manager, "get_config_value", side_effect=_fake_config_value),
        ):
            python_path = await mgr._init_library_venv(venv_path)

        mock_subprocess.assert_called_once()
        assert python_path.python_path.exists()
        assert python_path.reused is False
        assert (venv_path / "pyvenv.cfg").exists()

    @pytest.mark.asyncio
    async def test_reset_wipes_functional_venv_and_recreates_it(self, engine: Engine, tmp_path: Path) -> None:
        """_reset_and_init_library_venv wipes even a functional venv, unlike _init_library_venv."""
        mgr = engine.library_manager
        venv_path = tmp_path / ".venv"
        self._make_functional_venv(venv_path)
        # A functional venv would be reused by _init_library_venv; prove reset wipes it anyway.
        (venv_path / "stray.txt").write_text("old")

        recreated_python_path: dict[str, Path] = {}

        async def fake_subprocess_run(args: list[str], **_: object) -> MagicMock:
            recreated_python_path["path"] = self._make_functional_venv(Path(args[2]))
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
            patch.object(engine.config_manager, "get_config_value", side_effect=_fake_config_value),
        ):
            python_path = await mgr._reset_and_init_library_venv(venv_path)

        mock_subprocess.assert_called_once()
        assert python_path == recreated_python_path["path"]
        assert not (venv_path / "stray.txt").exists()

    @pytest.mark.asyncio
    async def test_reset_raises_runtime_error_when_removal_fails(self, engine: Engine, tmp_path: Path) -> None:
        """A failure to remove the existing venv surfaces as RuntimeError."""
        mgr = engine.library_manager
        venv_path = tmp_path / ".venv"
        self._make_functional_venv(venv_path)

        with (
            patch(
                "griptape_nodes.retained_mode.managers.library_manager.shutil.rmtree",
                side_effect=OSError("permission denied"),
            ),
            pytest.raises(RuntimeError, match="could not be removed"),
        ):
            await mgr._reset_and_init_library_venv(venv_path)


class TestListRegisteredLibraries:
    """Test the on_list_registered_libraries_request functionality in LibraryManager."""

    @pytest.mark.asyncio
    async def test_waits_for_loading_complete_before_returning_libraries(self, engine: Engine) -> None:
        """Test that the handler blocks until _libraries_loading_complete is set."""
        library_manager = engine.library_manager

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
    async def test_returns_library_list_when_loading_already_complete(self, engine: Engine) -> None:
        """Test that the handler returns the library list immediately when loading is already done."""
        library_manager = engine.library_manager

        # Simulate loading already finished
        library_manager._libraries_loading_complete.set()

        mock_libraries = ["LibA", "LibB", "LibC"]

        with patch.object(LibraryRegistry, "list_libraries", return_value=mock_libraries):
            request = ListRegisteredLibrariesRequest()
            result = await library_manager.on_list_registered_libraries_request(request)

        assert isinstance(result, ListRegisteredLibrariesResultSuccess)
        assert result.libraries == mock_libraries

    @pytest.mark.asyncio
    async def test_returns_copy_of_library_list(self, engine: Engine) -> None:
        """Test that the returned library list is a copy and not the original reference."""
        library_manager = engine.library_manager
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

    @pytest.mark.asyncio
    async def test_calls_library_registry_directly(self, engine: Engine) -> None:
        """Test that the method reads libraries from LibraryRegistry without going through on_list_registered_libraries_request."""
        library_manager = engine.library_manager

        with (
            patch.object(LibraryRegistry, "list_libraries", return_value=[]) as mock_list,
            patch.object(library_manager, "on_list_registered_libraries_request") as mock_handler,
        ):
            request = GetAllInfoForAllLibrariesRequest()
            result = await library_manager.get_all_info_for_all_libraries_request(request)

        mock_list.assert_called_once()
        mock_handler.assert_not_called()
        assert isinstance(result, GetAllInfoForAllLibrariesResultSuccess)

    @pytest.mark.asyncio
    async def test_returns_failure_when_individual_library_info_fails(self, engine: Engine) -> None:
        """Test that the method returns failure when retrieving info for a library fails."""
        library_manager = engine.library_manager

        mock_failure = MagicMock()
        mock_failure.succeeded.return_value = False

        with (
            patch.object(LibraryRegistry, "list_libraries", return_value=["BadLib"]),
            patch.object(library_manager, "get_all_info_for_library_request", AsyncMock(return_value=mock_failure)),
        ):
            request = GetAllInfoForAllLibrariesRequest()
            result = await library_manager.get_all_info_for_all_libraries_request(request)

        assert isinstance(result, GetAllInfoForAllLibrariesResultFailure)
        assert "BadLib" in str(result.result_details)

    @pytest.mark.asyncio
    async def test_gathers_libraries_concurrently(self, engine: Engine) -> None:
        """Per-library info is gathered, so one library's bundle reads do not serialize behind another's."""
        library_manager = engine.library_manager
        in_flight = 0
        peak_in_flight = 0

        async def slow_success(request: GetAllInfoForLibraryRequest) -> MagicMock:  # noqa: ARG001
            nonlocal in_flight, peak_in_flight
            in_flight += 1
            peak_in_flight = max(peak_in_flight, in_flight)
            await asyncio.sleep(0)
            in_flight -= 1
            success = MagicMock()
            success.succeeded.return_value = True
            return success

        with (
            patch.object(LibraryRegistry, "list_libraries", return_value=["LibA", "LibB", "LibC"]),
            patch.object(library_manager, "get_all_info_for_library_request", slow_success),
        ):
            result = await library_manager.get_all_info_for_all_libraries_request(GetAllInfoForAllLibrariesRequest())

        assert isinstance(result, GetAllInfoForAllLibrariesResultSuccess)
        assert peak_in_flight > 1, "libraries were walked one at a time instead of gathered"


class TestAddLibraryPathsToSysPath:
    """Test the _add_library_paths_to_sys_path helper method."""

    @pytest.mark.asyncio
    async def test_adds_base_dir_to_sys_path(self, engine: Engine) -> None:
        """Test that the library base directory is added to sys.path."""
        library_manager = engine.library_manager
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
    async def test_adds_venv_site_packages_when_venv_exists(self, engine: Engine) -> None:
        """Test that venv site-packages are added to sys.path when the venv exists."""
        library_manager = engine.library_manager
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
    async def test_skips_venv_when_venv_does_not_exist(self, engine: Engine) -> None:
        """Test that venv site-packages are NOT added when the venv doesn't exist."""
        library_manager = engine.library_manager
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
        engine: Engine,
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

        library_manager = engine.library_manager
        # Default: return the tmp sandbox. Individual tests that need the "not configured"
        # branch override via their own patch.
        with patch.object(library_manager, "_get_sandbox_directory", return_value=sandbox_dir):
            try:
                yield sandbox_dir
            finally:
                LibraryRegistry._clear()

    def test_imports_existing_file_and_registers_node_type(
        self,
        engine: Engine,
        _isolate_registry_and_config: Path,  # noqa: PT019 - value is used to locate the source file
    ) -> None:
        from griptape_nodes.retained_mode.events.library_events import (
            RegisterSandboxNodeFromSourceRequest,
            RegisterSandboxNodeFromSourceResultSuccess,
        )

        library_manager = engine.library_manager
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
        engine: Engine,
        _isolate_registry_and_config: Path,  # noqa: PT019 - value is used to locate the source file
    ) -> None:
        from griptape_nodes.retained_mode.events.library_events import (
            RegisterSandboxNodeFromSourceRequest,
            RegisterSandboxNodeFromSourceResultSuccess,
        )

        library_manager = engine.library_manager
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
        engine: Engine,
        _isolate_registry_and_config: Path,  # noqa: PT019 - value is used to locate the source file
    ) -> None:
        from griptape_nodes.retained_mode.events.library_events import (
            RegisterSandboxNodeFromSourceRequest,
            RegisterSandboxNodeFromSourceResultSuccess,
        )

        library_manager = engine.library_manager
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

    def test_drops_cached_port_summaries_even_when_registration_bails_partway(
        self,
        engine: Engine,
        _isolate_registry_and_config: Path,  # noqa: PT019 - value is used to locate the source file
    ) -> None:
        """The registration loop mutates as it goes and can bail out midway.

        If the cache were only dropped on the success path, the library would be left mutated with
        stale summaries describing it -- for the rest of the process's life, since nothing
        invalidates again.
        """
        from griptape_nodes.retained_mode.events.library_events import (
            RegisterSandboxNodeFromSourceRequest,
            RegisterSandboxNodeFromSourceResultFailure,
            RegisterSandboxNodeFromSourceResultSuccess,
        )

        library_manager = engine.library_manager
        sandbox_dir = _isolate_registry_and_config

        existing_source = sandbox_dir / "existing_node.py"
        existing_source.write_text(
            "from griptape_nodes.exe_types.node_types import BaseNode\n"
            "\n"
            "class SandboxExisting(BaseNode):\n"
            "    def process(self) -> None:\n"
            "        return None\n"
        )
        first = library_manager.register_sandbox_node_from_source_request(
            RegisterSandboxNodeFromSourceRequest(file_path=str(existing_source))
        )
        assert isinstance(first, RegisterSandboxNodeFromSourceResultSuccess)

        # Pretend a port summary request already ran and cached a result for this library.
        library_manager._library_to_port_summary_cache[self._LIBRARY_NAME] = _empty_port_summary_cache_entry()

        # A file whose first class is new and whose second is already registered. With
        # replace_if_exists=False the loop registers the first, then bails on the second.
        conflicting_source = sandbox_dir / "conflicting_node.py"
        conflicting_source.write_text(
            "from griptape_nodes.exe_types.node_types import BaseNode\n"
            "\n"
            "class SandboxBrandNew(BaseNode):\n"
            "    def process(self) -> None:\n"
            "        return None\n"
            "\n"
            "class SandboxExisting(BaseNode):\n"
            "    def process(self) -> None:\n"
            "        return None\n"
        )
        second = library_manager.register_sandbox_node_from_source_request(
            RegisterSandboxNodeFromSourceRequest(file_path=str(conflicting_source), replace_if_exists=False)
        )

        assert isinstance(second, RegisterSandboxNodeFromSourceResultFailure)
        # The library really was mutated before the bail-out, so the cache must be gone.
        assert LibraryRegistry.get_library(self._LIBRARY_NAME).has_node_type("SandboxBrandNew")
        assert self._LIBRARY_NAME not in library_manager._library_to_port_summary_cache

    def test_fails_when_sandbox_directory_is_not_configured(
        self,
        engine: Engine,
        _isolate_registry_and_config: Path,  # noqa: PT019 - fixture installs the default sandbox stub we override here
    ) -> None:
        from unittest.mock import patch

        from griptape_nodes.retained_mode.events.library_events import (
            RegisterSandboxNodeFromSourceRequest,
            RegisterSandboxNodeFromSourceResultFailure,
        )

        library_manager = engine.library_manager
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
        engine: Engine,
        _isolate_registry_and_config: Path,  # noqa: PT019 - value is used to seed source files
        tmp_path: Path,
    ) -> None:
        from griptape_nodes.retained_mode.events.library_events import (
            RegisterSandboxNodeFromSourceRequest,
            RegisterSandboxNodeFromSourceResultFailure,
        )

        library_manager = engine.library_manager
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
        engine: Engine,
        _isolate_registry_and_config: Path,  # noqa: PT019 - fixture installs the sandbox stub
    ) -> None:
        from griptape_nodes.retained_mode.events.library_events import (
            RegisterSandboxNodeFromSourceRequest,
            RegisterSandboxNodeFromSourceResultFailure,
        )

        library_manager = engine.library_manager

        result = library_manager.register_sandbox_node_from_source_request(
            RegisterSandboxNodeFromSourceRequest(file_path="never_written.py")
        )

        assert isinstance(result, RegisterSandboxNodeFromSourceResultFailure)
        assert "never_written.py" in str(result.result_details)

    def test_fails_when_source_has_no_base_node_subclass(
        self,
        engine: Engine,
        _isolate_registry_and_config: Path,  # noqa: PT019 - value is used to locate the source file
    ) -> None:
        from griptape_nodes.retained_mode.events.library_events import (
            RegisterSandboxNodeFromSourceRequest,
            RegisterSandboxNodeFromSourceResultFailure,
        )

        library_manager = engine.library_manager
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


def _empty_port_summary_cache_entry() -> PortSummaryCacheEntry:
    """A complete-but-empty cache entry, standing in for a port summary request that already ran."""
    return PortSummaryCacheEntry(
        summaries={}, retry_node_types=frozenset(), unprobed_node_types=frozenset(), timeouts_spent=0
    )


class _RaisingProbe(BaseNode):
    """Stand-in for node types whose __init__ performs failing I/O (auth, network, disk)."""

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None) -> None:
        super().__init__(name=name, metadata=metadata)
        msg = "simulated I/O failure"
        raise RuntimeError(msg)


class _HostileElementTypesList(ParameterList):
    """Container whose element-type accessor raises, as a library's subclass is free to do.

    `get_element_input_types` is an override point nothing calls during construction -- only the
    port summary pass does -- so a subclass can raise there on a node that builds perfectly well.
    """

    def get_element_input_types(self) -> list[str]:
        msg = "simulated failure computing element types"
        raise RuntimeError(msg)


class _HostileTypesProbe(BaseNode):
    """Node type that constructs fine but cannot be summarized."""

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None) -> None:
        super().__init__(name=name, metadata=metadata)
        self.add_parameter(
            _HostileElementTypesList(
                name="hostile",
                input_types=["str"],
                tooltip="Container whose element types cannot be read.",
            )
        )


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

    def test_probe_resolves_library_model_catalog(self, engine: Engine) -> None:
        # The probe must be constructed with the node's library/type so __init__
        # logic that resolves against the model_catalog (get_declared_models)
        # works -- otherwise the catalog is invisible and the roster is empty.
        library_manager = engine.library_manager
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

    def test_returns_parameter_schema_without_touching_object_manager(self, engine: Engine) -> None:
        library_manager = engine.library_manager
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
            engine.object_manager.attempt_get_object_by_name(
                f"{_LibraryManager.PROBE_NODE_NAME_PREFIX}{_DescribeNodeTypeProbe.__name__}"
            )
            is None
        )

    def test_resolves_library_when_node_type_is_unambiguous(self, engine: Engine) -> None:
        library_manager = engine.library_manager
        self._register_probe_library()

        request = DescribeNodeTypeRequest(node_type=_DescribeNodeTypeProbe.__name__)

        result = library_manager.describe_node_type_request(request)

        assert isinstance(result, DescribeNodeTypeResultSuccess)
        assert result.library == self._LIBRARY_NAME

    def test_returns_failure_when_node_type_missing(self, engine: Engine) -> None:
        library_manager = engine.library_manager
        self._register_probe_library()

        request = DescribeNodeTypeRequest(node_type="NotARealNode", library=self._LIBRARY_NAME)

        result = library_manager.describe_node_type_request(request)

        assert isinstance(result, DescribeNodeTypeResultFailure)

    def test_returns_success_with_warning_detail_when_init_raises(self, engine: Engine) -> None:
        """Nodes whose __init__ performs I/O can raise (e.g. auth). We still want the node-level metadata."""
        library_manager = engine.library_manager

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


class _PortSummaryProbe(BaseNode):
    """Node whose ports cover every case NodePortSummary has to sort out."""

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None) -> None:
        super().__init__(name=name, metadata=metadata)

        # Control ports belong to the booleans, never to the data type unions.
        self.add_parameter(ControlParameterInput())
        self.add_parameter(ControlParameterOutput())

        # Multi-type input; "str" also appears below, so it must not be duplicated.
        self.add_parameter(
            Parameter(
                name="subject",
                input_types=["str", "ImageArtifact"],
                output_type="str",
                tooltip="Input and output.",
            )
        )

        # No input_types/output_type declared -- both unions fall back to `type`.
        self.add_parameter(Parameter(name="count", type="int", tooltip="Falls back to type."))

        # Property-only: connectable in neither direction, so it appears in neither union.
        self.add_parameter(
            Parameter(
                name="seed",
                type="float",
                tooltip="Property only.",
                allowed_modes={ParameterMode.PROPERTY},
            )
        )

        # Private parameters are engine bookkeeping, never offered as ports.
        self.add_parameter(Parameter(name="internal_state", type="dict", tooltip="Private.", private=True))


class _PortSummaryDataOnlyProbe(BaseNode):
    """Node with data ports but no control ports, e.g. a pure value provider."""

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None) -> None:
        super().__init__(name=name, metadata=metadata)
        self.add_parameter(
            Parameter(
                name="value",
                type="str",
                tooltip="Emitted value.",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )


class _PortSummaryContainerProbe(BaseNode):
    """Node whose only data port is a list container, i.e. the expander shape."""

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None) -> None:
        super().__init__(name=name, metadata=metadata)
        self.add_parameter(
            ParameterList(
                name="items",
                input_types=["str"],
                output_type="str",
                tooltip="Accepts many strings.",
            )
        )


class TestNodePortSummary:
    """Exercise NodePortSummary.from_parameters, the derivation the ranking depends on."""

    def test_derives_unions_and_control_flags(self) -> None:
        probe = _PortSummaryProbe(name="probe")

        summary = NodePortSummary.from_parameters(probe.parameters)

        # "str" appears on two parameters but is unioned once; the control type is excluded.
        assert summary.input_types == ("str", "ImageArtifact", "int")
        assert summary.output_types == ("str", "int")
        assert summary.has_control_input is True
        assert summary.has_control_output is True

        # The property-only and private parameters contribute to neither union.
        assert "float" not in summary.input_types
        assert "float" not in summary.output_types
        assert "dict" not in summary.input_types
        assert "dict" not in summary.output_types

    def test_reports_no_control_when_node_declares_none(self) -> None:
        probe = _PortSummaryDataOnlyProbe(name="probe")

        summary = NodePortSummary.from_parameters(probe.parameters)

        assert summary.has_control_input is False
        assert summary.has_control_output is False
        # Output-only mode means the parameter is emitted but not accepted.
        assert summary.input_types == ()
        assert summary.output_types == ("str",)

    def test_returns_empty_summary_for_portless_node(self) -> None:
        summary = NodePortSummary.from_parameters([])

        assert summary.input_types == ()
        assert summary.output_types == ()
        assert summary.has_control_input is False
        assert summary.has_control_output is False

    def test_container_reports_element_type_alongside_container_type(self) -> None:
        """A list container is how a node says "I take many of these", so both types must appear.

        Without the element type, dragging a single `str` would not surface expander nodes at all
        -- exactly the case the ranking exists to serve.
        """
        probe = _PortSummaryContainerProbe(name="probe")

        summary = NodePortSummary.from_parameters(probe.parameters)

        # The container's own port accepts a whole collection; its children accept one element.
        assert "list[str]" in summary.input_types
        assert "list" in summary.input_types
        assert "str" in summary.input_types
        assert "list[str]" in summary.output_types
        assert "str" in summary.output_types

    @pytest.mark.parametrize(
        "declared",
        [
            # Fully declared: nothing can drift.
            {"input_types": ["str"], "output_type": "str"},
            # Only `type`: both element sides resolve through Parameter's fallback chain.
            {"type": "int"},
            # Only one direction declared: the other inherits it.
            {"input_types": ["ImageArtifact"]},
            {"output_type": "ImageArtifact"},
            # Nothing declared: both sides fall through to "str".
            {},
        ],
    )
    def test_container_element_type_matches_what_a_real_child_declares(self, declared: dict[str, Any]) -> None:
        """Pin the element type to add_child_parameter across every declaration shape.

        The interesting cases are the ones that resolve through `Parameter`'s fallback chain --
        those are what a future change to that chain would silently break.
        """
        container = ParameterList(name="items", tooltip="Accepts many.", **declared)

        child = container.add_child_parameter()

        assert container.get_element_input_types() == child.input_types
        assert container.get_element_output_type() == child.output_type


class TestGetPortSummariesForAllLibrariesRequest:
    """Exercise LibraryManager.on_get_port_summaries_for_all_libraries_request."""

    _LIBRARY_NAME = "port-summary-test-library"

    @pytest.fixture(autouse=True)
    def _clean_registry(self) -> Generator[None, None, None]:
        """LibraryRegistry holds class-level state that survives the singleton reset fixture."""
        LibraryRegistry._clear()
        yield
        LibraryRegistry._clear()

    def _register_probe_library(self, *node_classes: type[BaseNode], library_name: str | None = None) -> None:
        schema = LibrarySchema(
            name=library_name or self._LIBRARY_NAME,
            library_schema_version=LibrarySchema.LATEST_SCHEMA_VERSION,
            metadata=LibraryMetadata(
                author="test",
                description="port summary library",
                library_version="1.0.0",
                engine_version="1.0.0",
                tags=[],
            ),
            categories=[],
            nodes=[],
        )
        library = LibraryRegistry.generate_new_library(library_data=schema)
        for node_class in node_classes:
            library.register_new_node_type(
                node_class,
                NodeMetadata(
                    category="test",
                    description=f"{node_class.__name__} used by port summary tests",
                    display_name=node_class.__name__,
                ),
            )

    @pytest.mark.asyncio
    async def test_returns_a_summary_for_every_node_type(self, engine: Engine) -> None:
        library_manager = engine.library_manager
        self._register_probe_library(_PortSummaryProbe, _PortSummaryDataOnlyProbe)

        result = await library_manager.on_get_port_summaries_for_all_libraries_request(
            GetPortSummariesForAllLibrariesRequest()
        )

        assert isinstance(result, GetPortSummariesForAllLibrariesResultSuccess)
        summaries = result.library_name_to_port_summaries[self._LIBRARY_NAME]
        assert set(summaries) == {_PortSummaryProbe.__name__, _PortSummaryDataOnlyProbe.__name__}
        assert summaries[_PortSummaryProbe.__name__].has_control_input is True
        assert summaries[_PortSummaryDataOnlyProbe.__name__].output_types == ("str",)

    @pytest.mark.asyncio
    async def test_omits_node_types_that_cannot_be_probed(self, engine: Engine) -> None:
        """A node whose __init__ raises is left out rather than reported as having no ports."""
        library_manager = engine.library_manager
        self._register_probe_library(_PortSummaryDataOnlyProbe, _RaisingProbe)

        result = await library_manager.on_get_port_summaries_for_all_libraries_request(
            GetPortSummariesForAllLibrariesRequest()
        )

        assert isinstance(result, GetPortSummariesForAllLibrariesResultSuccess)
        summaries = result.library_name_to_port_summaries[self._LIBRARY_NAME]
        assert _RaisingProbe.__name__ not in summaries
        assert _PortSummaryDataOnlyProbe.__name__ in summaries

    @pytest.mark.asyncio
    async def test_probes_each_node_type_once_across_requests(self, engine: Engine) -> None:
        """Probing is the expensive part, so a second request must be served from the cache."""
        library_manager = engine.library_manager
        self._register_probe_library(_PortSummaryDataOnlyProbe)

        with patch.object(
            _LibraryManager,
            "_construct_probe_node",
            side_effect=_LibraryManager._construct_probe_node,
            autospec=True,
        ) as probe_spy:
            await library_manager.on_get_port_summaries_for_all_libraries_request(
                GetPortSummariesForAllLibrariesRequest()
            )
            await library_manager.on_get_port_summaries_for_all_libraries_request(
                GetPortSummariesForAllLibrariesRequest()
            )

        assert probe_spy.call_count == 1

    @pytest.mark.asyncio
    async def test_resolves_node_classes_on_the_event_loop(self, engine: Engine) -> None:
        """Resolution must stay on the loop: NodeTypeEntry.resolve() is not thread-safe.

        Only construction is allowed off-thread, because a node's __init__ can block.
        """
        library_manager = engine.library_manager
        self._register_probe_library(_PortSummaryDataOnlyProbe)
        loop_thread_id = threading.get_ident()
        resolve_thread_ids: list[int] = []
        construct_thread_ids: list[int] = []
        # Bind the real implementations before patching, or the side effects recurse into the mocks.
        real_resolve = _LibraryManager._resolve_node_class_for_probe
        real_construct = _LibraryManager._construct_probe_node

        def record_resolve(manager: _LibraryManager, *args: Any, **kwargs: Any) -> Any:
            resolve_thread_ids.append(threading.get_ident())
            return real_resolve(manager, *args, **kwargs)

        def record_construct(manager: _LibraryManager, *args: Any, **kwargs: Any) -> Any:
            construct_thread_ids.append(threading.get_ident())
            return real_construct(manager, *args, **kwargs)

        with (
            patch.object(_LibraryManager, "_resolve_node_class_for_probe", side_effect=record_resolve, autospec=True),
            patch.object(_LibraryManager, "_construct_probe_node", side_effect=record_construct, autospec=True),
        ):
            await library_manager.on_get_port_summaries_for_all_libraries_request(
                GetPortSummariesForAllLibrariesRequest()
            )

        assert resolve_thread_ids == [loop_thread_id]
        assert construct_thread_ids != []
        assert loop_thread_id not in construct_thread_ids

    @pytest.mark.asyncio
    async def test_does_not_cache_summaries_measured_before_an_invalidation(self, engine: Engine) -> None:
        """The probe pass awaits, so the library can change underneath it.

        Caching a result measured before that change would pin the pre-change ports for the rest
        of the process's life, because nothing invalidates again.
        """
        library_manager = engine.library_manager
        self._register_probe_library(_PortSummaryDataOnlyProbe)

        async def invalidate_midway(
            library_name: str, allowance: ProbeTimeoutAllowance, *, throttle_s: float, **kwargs: Any
        ) -> Any:
            computation = await _LibraryManager._compute_port_summaries_for_library(
                library_manager, library_name, allowance, throttle_s=throttle_s, **kwargs
            )
            # Stand in for a reload / sandbox registration landing while the pass was awaiting.
            library_manager._invalidate_port_summaries(library_name)
            return computation

        with patch.object(
            library_manager, "_compute_port_summaries_for_library", side_effect=invalidate_midway
        ) as compute_spy:
            first = await library_manager.on_get_port_summaries_for_all_libraries_request(
                GetPortSummariesForAllLibrariesRequest()
            )
            # The stale result is still served to the caller -- it is the best answer available --
            # but it must not have been written back, so this request recomputes.
            second = await library_manager.on_get_port_summaries_for_all_libraries_request(
                GetPortSummariesForAllLibrariesRequest()
            )

        expected_compute_calls = 2  # Once per request: the first result was never cached.
        assert isinstance(first, GetPortSummariesForAllLibrariesResultSuccess)
        assert isinstance(second, GetPortSummariesForAllLibrariesResultSuccess)
        assert compute_spy.call_count == expected_compute_calls
        assert self._LIBRARY_NAME not in library_manager._library_to_port_summary_cache

    @pytest.mark.asyncio
    async def test_a_node_type_that_fails_to_resolve_does_not_fail_the_whole_response(self, engine: Engine) -> None:
        """Resolution raises more than import errors, and the response covers every library.

        `Library.get_node_class` raises `LibraryRegistryError` for a node type unregistered since
        the loop read `get_registered_nodes()`, and `NodeTypeEntry.resolve()` raises `RuntimeError`
        for an entry with neither class nor loader. Neither may take down the other libraries.
        """
        library_manager = engine.library_manager
        self._register_probe_library(_PortSummaryDataOnlyProbe, _PortSummaryProbe)
        library = LibraryRegistry.get_library(self._LIBRARY_NAME)
        real_get_node_class = library.get_node_class

        def raise_for_one_node_type(node_type: str) -> Any:
            if node_type == _PortSummaryProbe.__name__:
                msg = f"Node type '{node_type}' vanished mid-pass."
                raise LibraryRegistryError(msg)
            return real_get_node_class(node_type)

        with patch.object(library, "get_node_class", side_effect=raise_for_one_node_type):
            result = await library_manager.on_get_port_summaries_for_all_libraries_request(
                GetPortSummariesForAllLibrariesRequest()
            )

        assert isinstance(result, GetPortSummariesForAllLibrariesResultSuccess)
        summaries = result.library_name_to_port_summaries[self._LIBRARY_NAME]
        assert _PortSummaryProbe.__name__ not in summaries
        assert _PortSummaryDataOnlyProbe.__name__ in summaries

    @pytest.mark.asyncio
    async def test_retries_only_the_timed_out_node_type_and_only_once(self, engine: Engine) -> None:
        """A timeout may not recur, so it earns one retry -- and exactly one.

        `asyncio.to_thread` shares the loop's default executor, so a saturated pool can time out a
        probe that would otherwise have succeeded, which is worth a second look. Looking forever is
        not: the timed-out thread cannot be cancelled, so re-probing on every request would drain
        the executor and stall every other threaded operation in the engine.
        """
        library_manager = engine.library_manager
        self._register_probe_library(_PortSummaryDataOnlyProbe, _PortSummaryProbe)
        timed_out_node_type = _PortSummaryProbe.__name__
        real_construct = _LibraryManager._construct_probe_node
        release_blocked_probe = threading.Event()

        def block_one_node_type(
            manager: _LibraryManager, library_name: str, node_type: str, *args: Any, **kwargs: Any
        ) -> Any:
            if node_type == timed_out_node_type:
                # Stand in for an __init__ that makes a blocking call, exercising the real
                # wait_for/to_thread timeout rather than a patched one. Released below so the
                # worker thread does not outlive the test.
                release_blocked_probe.wait(timeout=30)
            return real_construct(manager, library_name, node_type, *args, **kwargs)

        try:
            with (
                patch.object(_LibraryManager, "_SCHEMA_PROBE_TIMEOUT_S", 1.0),
                patch.object(_LibraryManager, "_construct_probe_node", side_effect=block_one_node_type, autospec=True),
            ):
                first = await library_manager.on_get_port_summaries_for_all_libraries_request(
                    GetPortSummariesForAllLibrariesRequest()
                )
        finally:
            release_blocked_probe.set()

        # The node types that did probe are cached, so the retry does not re-pay for them...
        assert isinstance(first, GetPortSummariesForAllLibrariesResultSuccess)
        assert timed_out_node_type not in first.library_name_to_port_summaries[self._LIBRARY_NAME]
        assert (
            _PortSummaryDataOnlyProbe.__name__
            in library_manager._library_to_port_summary_cache[self._LIBRARY_NAME].summaries
        )
        assert library_manager._library_to_port_summary_cache[self._LIBRARY_NAME].retry_node_types == frozenset(
            {timed_out_node_type}
        )

        # ...and the retry probes that node type alone.
        real_construct = _LibraryManager._construct_probe_node
        with patch.object(
            _LibraryManager, "_construct_probe_node", side_effect=real_construct, autospec=True
        ) as probe_spy:
            second = await library_manager.on_get_port_summaries_for_all_libraries_request(
                GetPortSummariesForAllLibrariesRequest()
            )

            # A third request has no retries left to spend, so it probes nothing at all -- this is
            # what stops a permanently stuck __init__ from leaking a thread per menu open. Both
            # requests share one spy, so the single recorded call below covers that claim too.
            await library_manager.on_get_port_summaries_for_all_libraries_request(
                GetPortSummariesForAllLibrariesRequest()
            )
            # autospec passes self positionally: (manager, library_name, node_type, node_class, ...).
            probed_node_types = [call.args[2] for call in probe_spy.call_args_list]

        assert isinstance(second, GetPortSummariesForAllLibrariesResultSuccess)
        assert probed_node_types == [timed_out_node_type]
        assert timed_out_node_type in second.library_name_to_port_summaries[self._LIBRARY_NAME]
        assert not library_manager._library_to_port_summary_cache[self._LIBRARY_NAME].pending_node_types()

    @pytest.mark.asyncio
    async def test_contains_a_node_type_whose_element_types_cannot_be_read(self, engine: Engine) -> None:
        """Summarizing a container's element types runs library code construction never reached.

        `get_element_input_types` exists for this pass alone, so a subclass that raises there gets
        past construction and lands in the summarize step. One library's Parameter subclass must not
        take down the summaries for every library in the response -- the handler answers for all of
        them at once.
        """
        library_manager = engine.library_manager
        self._register_probe_library(_HostileTypesProbe, _PortSummaryDataOnlyProbe)

        result = await library_manager.on_get_port_summaries_for_all_libraries_request(
            GetPortSummariesForAllLibrariesRequest()
        )

        assert isinstance(result, GetPortSummariesForAllLibrariesResultSuccess)
        summaries = result.library_name_to_port_summaries[self._LIBRARY_NAME]
        # The unsummarizable node type is left out; the healthy one behind it is not collateral.
        assert set(summaries) == {_PortSummaryDataOnlyProbe.__name__}
        # Its getter will raise again next request, so it is a stable gap rather than a retry.
        assert not library_manager._library_to_port_summary_cache[self._LIBRARY_NAME].pending_node_types()

    @pytest.mark.asyncio
    async def test_contains_a_node_type_whose_library_metadata_cannot_be_read(self, engine: Engine) -> None:
        """Building the probe's metadata reads and serializes the library author's node metadata.

        A malformed entry raises there, outside the node's own __init__, and must be contained to
        the one node type for the same reason as an unreadable parameter.
        """
        library_manager = engine.library_manager
        self._register_probe_library(_PortSummaryProbe, _PortSummaryDataOnlyProbe)
        broken_node_type = _PortSummaryProbe.__name__
        real_metadata = _LibraryManager._library_node_metadata_for_probe

        def fail_for_one_node_type(manager: _LibraryManager, *, library: Any, node_type: str) -> Any:
            if node_type == broken_node_type:
                msg = "simulated malformed library node metadata"
                raise RuntimeError(msg)
            return real_metadata(manager, library=library, node_type=node_type)

        with patch.object(
            _LibraryManager, "_library_node_metadata_for_probe", side_effect=fail_for_one_node_type, autospec=True
        ):
            result = await library_manager.on_get_port_summaries_for_all_libraries_request(
                GetPortSummariesForAllLibrariesRequest()
            )

        assert isinstance(result, GetPortSummariesForAllLibrariesResultSuccess)
        assert set(result.library_name_to_port_summaries[self._LIBRARY_NAME]) == {_PortSummaryDataOnlyProbe.__name__}
        assert not library_manager._library_to_port_summary_cache[self._LIBRARY_NAME].pending_node_types()

    @pytest.mark.asyncio
    async def test_resumes_node_types_a_request_ran_out_of_allowance_before_reaching(self, engine: Engine) -> None:
        """One library's blocking node must not cost another library its ranking for good.

        The timeout allowance is spent per request across every library, so a library reached after
        it runs out is never attempted at all. Nothing is known about those node types, so they stay
        pending and the next request -- with a fresh allowance -- probes them.
        """
        library_manager = engine.library_manager
        blocking_library = "port-summary-blocking-library"
        # Registration order decides which library the handler walks first.
        self._register_probe_library(_PortSummaryProbe, library_name=blocking_library)
        self._register_probe_library(_PortSummaryDataOnlyProbe)
        release_blocked_probe = threading.Event()
        real_construct = _LibraryManager._construct_probe_node

        def block_the_first_library(
            manager: _LibraryManager, library_name: str, node_type: str, *args: Any, **kwargs: Any
        ) -> Any:
            if library_name == blocking_library:
                release_blocked_probe.wait(timeout=30)
            return real_construct(manager, library_name, node_type, *args, **kwargs)

        try:
            with (
                patch.object(_LibraryManager, "_SCHEMA_PROBE_TIMEOUT_S", 0.2),
                patch.object(_LibraryManager, "_PORT_SUMMARY_TIMEOUT_BUDGET", 1),
                patch.object(
                    _LibraryManager, "_construct_probe_node", side_effect=block_the_first_library, autospec=True
                ),
            ):
                first = await library_manager.on_get_port_summaries_for_all_libraries_request(
                    GetPortSummariesForAllLibrariesRequest()
                )
                # The healthy library was never reached, so it is owed a real attempt rather than
                # being recorded as unrankable.
                assert isinstance(first, GetPortSummariesForAllLibrariesResultSuccess)
                assert first.library_name_to_port_summaries[self._LIBRARY_NAME] == {}
                healthy_entry = library_manager._library_to_port_summary_cache[self._LIBRARY_NAME]
                assert healthy_entry.unprobed_node_types == frozenset({_PortSummaryDataOnlyProbe.__name__})
                assert healthy_entry.timeouts_spent == 0

                second = await library_manager.on_get_port_summaries_for_all_libraries_request(
                    GetPortSummariesForAllLibrariesRequest()
                )
        finally:
            release_blocked_probe.set()

        assert isinstance(second, GetPortSummariesForAllLibrariesResultSuccess)
        assert set(second.library_name_to_port_summaries[self._LIBRARY_NAME]) == {_PortSummaryDataOnlyProbe.__name__}
        assert not library_manager._library_to_port_summary_cache[self._LIBRARY_NAME].pending_node_types()

    @pytest.mark.asyncio
    async def test_gives_up_on_a_library_that_has_spent_its_lifetime_timeout_allowance(self, engine: Engine) -> None:
        """Carrying unreached node types forever would let a stuck library leak a thread per request.

        Each timeout permanently holds a worker of the loop's default executor, so once a library has
        cost its whole allowance, everything still pending is dropped until it reloads -- including
        node types never attempted, which cannot be told apart from the blocking ones without paying
        again. That is the trade this bound makes, and it is why the allowance is not per request
        alone.
        """
        library_manager = engine.library_manager
        # The blocking node type is registered first so a healthy one is still unreached when the
        # library's allowance runs out.
        self._register_probe_library(_PortSummaryProbe, _PortSummaryDataOnlyProbe)
        timed_out_node_type = _PortSummaryProbe.__name__
        release_blocked_probe = threading.Event()
        real_construct = _LibraryManager._construct_probe_node

        def block_one_node_type(
            manager: _LibraryManager, library_name: str, node_type: str, *args: Any, **kwargs: Any
        ) -> Any:
            if node_type == timed_out_node_type:
                release_blocked_probe.wait(timeout=30)
            return real_construct(manager, library_name, node_type, *args, **kwargs)

        try:
            with (
                patch.object(_LibraryManager, "_SCHEMA_PROBE_TIMEOUT_S", 0.2),
                patch.object(_LibraryManager, "_PORT_SUMMARY_TIMEOUT_BUDGET", 1),
                patch.object(
                    _LibraryManager, "_construct_probe_node", side_effect=block_one_node_type, autospec=True
                ) as probe_spy,
            ):
                first = await library_manager.on_get_port_summaries_for_all_libraries_request(
                    GetPortSummariesForAllLibrariesRequest()
                )
                # A second request must not spend anything more on this library, so the spy below
                # covers both requests.
                await library_manager.on_get_port_summaries_for_all_libraries_request(
                    GetPortSummariesForAllLibrariesRequest()
                )
                # autospec passes self positionally: (manager, library_name, node_type, node_class, ...).
                probed_node_types = [call.args[2] for call in probe_spy.call_args_list]
        finally:
            release_blocked_probe.set()

        assert isinstance(first, GetPortSummariesForAllLibrariesResultSuccess)
        assert probed_node_types == [timed_out_node_type]
        entry = library_manager._library_to_port_summary_cache[self._LIBRARY_NAME]
        # The empty map is cached as complete: nothing pending is what stops the next request from
        # paying for this library again.
        assert entry.summaries == {}
        assert not entry.pending_node_types()

    @pytest.mark.asyncio
    async def test_caches_a_pass_whose_only_gap_is_a_broken_node_type(self, engine: Engine) -> None:
        """A node whose __init__ raises will keep raising until the library reloads.

        That gap is stable, so the pass is still worth caching -- otherwise one broken node type in
        a library would mean re-probing every node type in it on every menu open.
        """
        library_manager = engine.library_manager
        self._register_probe_library(_PortSummaryDataOnlyProbe, _RaisingProbe)

        await library_manager.on_get_port_summaries_for_all_libraries_request(GetPortSummariesForAllLibrariesRequest())

        cached = library_manager._library_to_port_summary_cache[self._LIBRARY_NAME].summaries
        # Both halves: the working node type is cached with its ports, and the broken one is cached
        # as absent rather than earning a retry that would never succeed.
        assert set(cached) == {_PortSummaryDataOnlyProbe.__name__}
        assert cached[_PortSummaryDataOnlyProbe.__name__].output_types == ("str",)
        assert not library_manager._library_to_port_summary_cache[self._LIBRARY_NAME].pending_node_types()

    @pytest.mark.asyncio
    async def test_result_does_not_alias_the_cache(self, engine: Engine) -> None:
        """The payload must not hand out the cache's own dict, or a consumer can corrupt it."""
        library_manager = engine.library_manager
        self._register_probe_library(_PortSummaryDataOnlyProbe)

        result = await library_manager.on_get_port_summaries_for_all_libraries_request(
            GetPortSummariesForAllLibrariesRequest()
        )

        assert isinstance(result, GetPortSummariesForAllLibrariesResultSuccess)
        served = result.library_name_to_port_summaries[self._LIBRARY_NAME]
        served.pop(_PortSummaryDataOnlyProbe.__name__)

        assert (
            _PortSummaryDataOnlyProbe.__name__
            in library_manager._library_to_port_summary_cache[self._LIBRARY_NAME].summaries
        )

    @pytest.mark.asyncio
    async def test_probing_publishes_no_events(self, engine: Engine) -> None:
        """A probe node is not on anyone's canvas, so it must not reach the event bus.

        `add_parameter` publishes an AlterElementEvent per parameter, so without suppression the
        editor would receive element events naming `__node_type_probe__*` nodes that do not exist
        -- once per parameter per node type per library.
        """
        library_manager = engine.library_manager
        self._register_probe_library(_PortSummaryProbe)
        event_queue: asyncio.Queue = asyncio.Queue()
        engine.event_manager.initialize_queue(event_queue)

        await library_manager.on_get_port_summaries_for_all_libraries_request(GetPortSummariesForAllLibrariesRequest())
        # Probes run in worker threads, whose events are enqueued via call_soon_threadsafe, so let
        # the loop drain any that were scheduled before asserting none were.
        await asyncio.sleep(0.05)

        assert event_queue.empty()

        # Control: the same construction in a worker thread, differing only in that it does not
        # suppress, does publish. That is what makes the assertion above mean something -- it pins
        # the silence on the ContextVar rather than on the thread boundary swallowing events.
        def construct_without_suppression() -> None:
            with LibraryRegistry.constructing_node():
                _PortSummaryProbe(name="unsuppressed")

        await asyncio.to_thread(construct_without_suppression)
        await asyncio.sleep(0.05)

        assert not event_queue.empty()

    @pytest.mark.asyncio
    async def test_recomputes_after_library_is_unloaded(self, engine: Engine) -> None:
        """Unloading is the choke point every reload goes through, so it must drop the cache."""
        library_manager = engine.library_manager
        self._register_probe_library(_PortSummaryDataOnlyProbe)

        await library_manager.on_get_port_summaries_for_all_libraries_request(GetPortSummariesForAllLibrariesRequest())
        library_manager.unload_library_from_registry_request(
            UnloadLibraryFromRegistryRequest(library_name=self._LIBRARY_NAME)
        )

        assert self._LIBRARY_NAME not in library_manager._library_to_port_summary_cache

        # Re-registering with a different node type must be reflected, not served stale.
        self._register_probe_library(_PortSummaryProbe)
        result = await library_manager.on_get_port_summaries_for_all_libraries_request(
            GetPortSummariesForAllLibrariesRequest()
        )

        assert isinstance(result, GetPortSummariesForAllLibrariesResultSuccess)
        summaries = result.library_name_to_port_summaries[self._LIBRARY_NAME]
        assert set(summaries) == {_PortSummaryProbe.__name__}


class TestPortSummaryWarming:
    """Exercise the background pass that fills the port summary cache before anything asks.

    Warming exists to move the probe cost off the moment an artist drags a connection. These tests
    pin the two properties that make it worth having -- a warmed request probes nothing, and a
    reload cannot leave a pass running against libraries it is tearing down -- plus the two ways it
    declines to run at all.
    """

    _LIBRARY_NAME = "port-summary-warm-test-library"

    @pytest.fixture(autouse=True)
    def _clean_registry(self) -> Generator[None, None, None]:
        """LibraryRegistry holds class-level state that survives the singleton reset fixture."""
        LibraryRegistry._clear()
        yield
        LibraryRegistry._clear()

    def _register_probe_library(self, *node_classes: type[BaseNode]) -> None:
        schema = LibrarySchema(
            name=self._LIBRARY_NAME,
            library_schema_version=LibrarySchema.LATEST_SCHEMA_VERSION,
            metadata=LibraryMetadata(
                author="test",
                description="port summary warming library",
                library_version="1.0.0",
                engine_version="1.0.0",
                tags=[],
            ),
            categories=[],
            nodes=[],
        )
        library = LibraryRegistry.generate_new_library(library_data=schema)
        for node_class in node_classes:
            library.register_new_node_type(
                node_class,
                NodeMetadata(
                    category="test",
                    description=f"{node_class.__name__} used by port summary warming tests",
                    display_name=node_class.__name__,
                ),
            )

    @pytest.mark.asyncio
    async def test_a_warmed_request_probes_nothing(self, engine: Engine) -> None:
        """The whole point: after warming, the request an artist waits on constructs no nodes."""
        library_manager = engine.library_manager
        self._register_probe_library(_PortSummaryDataOnlyProbe, _PortSummaryProbe)

        await library_manager._warm_port_summaries()

        with patch.object(
            _LibraryManager,
            "_construct_probe_node",
            side_effect=_LibraryManager._construct_probe_node,
            autospec=True,
        ) as probe_spy:
            result = await library_manager.on_get_port_summaries_for_all_libraries_request(
                GetPortSummariesForAllLibrariesRequest()
            )

        assert probe_spy.call_count == 0
        assert isinstance(result, GetPortSummariesForAllLibrariesResultSuccess)
        summaries = result.library_name_to_port_summaries[self._LIBRARY_NAME]
        assert set(summaries) == {_PortSummaryDataOnlyProbe.__name__, _PortSummaryProbe.__name__}

    @pytest.mark.asyncio
    async def test_a_request_landing_mid_warm_does_not_duplicate_the_probing(self, engine: Engine) -> None:
        """Warming adds a second concurrent caller, so the per-library lock has to hold.

        Without it the two passes would each probe every node type, making warming a pessimization
        for exactly the request it is supposed to speed up.
        """
        library_manager = engine.library_manager
        self._register_probe_library(_PortSummaryDataOnlyProbe, _PortSummaryProbe)

        with patch.object(
            _LibraryManager,
            "_construct_probe_node",
            side_effect=_LibraryManager._construct_probe_node,
            autospec=True,
        ) as probe_spy:
            warm = asyncio.create_task(library_manager._warm_port_summaries())
            request = asyncio.create_task(
                library_manager.on_get_port_summaries_for_all_libraries_request(
                    GetPortSummariesForAllLibrariesRequest()
                )
            )
            await asyncio.gather(warm, request)

        expected_probe_calls = 2  # One per node type, total, across both callers.
        assert probe_spy.call_count == expected_probe_calls

    @pytest.mark.asyncio
    async def test_cancelling_stops_an_in_flight_pass_before_it_caches(self, engine: Engine) -> None:
        """A reload calls this before unloading, so the pass must really be off the loop after it.

        The pass resolves node classes, which imports their modules; one still running would pull
        a library being dismantled back into sys.modules.
        """
        library_manager = engine.library_manager
        self._register_probe_library(_PortSummaryDataOnlyProbe, _PortSummaryProbe)
        pass_reached_first_probe = asyncio.Event()
        release_first_probe = asyncio.Event()
        real_construct = _LibraryManager._construct_probe_node

        async def block_on_first_probe(*args: Any, **kwargs: Any) -> Any:
            pass_reached_first_probe.set()
            await release_first_probe.wait()
            return real_construct(*args, **kwargs)

        # Patch the awaited probe rather than the threaded construction: this test is about the
        # coroutine being cancellable at an await point, which is what cancellation acts on.
        with patch.object(library_manager, "_probe_node_type_port_summary", side_effect=block_on_first_probe):
            library_manager._start_port_summary_warm()
            warm_task = library_manager._port_summary_warm_task
            assert warm_task is not None
            await pass_reached_first_probe.wait()

            await library_manager._cancel_port_summary_warm()

        assert warm_task.cancelled()
        assert library_manager._port_summary_warm_task is None
        # Cancelled mid-library, so nothing was written back -- the next request recomputes rather
        # than serving a half-probed library as complete.
        assert self._LIBRARY_NAME not in library_manager._library_to_port_summary_cache

    @pytest.mark.asyncio
    async def test_cancelling_is_a_no_op_when_nothing_is_warming(self, engine: Engine) -> None:
        """Reload always calls this, including on a boot where warming never started."""
        library_manager = engine.library_manager

        await library_manager._cancel_port_summary_warm()

        assert library_manager._port_summary_warm_task is None

    @pytest.mark.asyncio
    async def test_reload_cancels_warming_before_it_unloads_anything(
        self, engine: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Ordering is the whole protection, so pin it rather than trusting the call's placement."""
        library_manager = engine.library_manager
        call_order: list[str] = []

        async def record_cancel() -> None:
            call_order.append("cancel")

        async def fail_after_clearing(_request: object) -> Any:
            # Stands in for the first thing _run_reload_libraries does after cancelling. Failing it
            # short-circuits the rest of the reload, which this test does not exercise.
            call_order.append("clear_state")
            failure = MagicMock()
            failure.succeeded.return_value = False
            return failure

        monkeypatch.setattr(library_manager, "_cancel_port_summary_warm", record_cancel)
        monkeypatch.setattr(library_manager.engine, "ahandle_request", fail_after_clearing)

        from griptape_nodes.retained_mode.events.library_events import ReloadAllLibrariesRequest

        await library_manager._run_reload_libraries(ReloadAllLibrariesRequest())

        assert call_order == ["cancel", "clear_state"]

    def test_does_not_warm_on_a_worker(self, engine: Engine) -> None:
        """A worker already constructs every node type to serialize its schemas."""
        library_manager = engine.library_manager
        library_manager._is_worker = True

        library_manager._start_port_summary_warm()

        assert library_manager._port_summary_warm_task is None

    def test_does_not_warm_when_the_setting_is_off(self, engine: Engine) -> None:
        """Opting out has to leave the on-demand path as the only one that probes."""
        library_manager = engine.library_manager

        def config_value(key: str, **_: object) -> object:
            if key == LIBRARY_WARM_PORT_SUMMARIES_KEY:
                return False
            return None

        with patch.object(library_manager.engine.config_manager, "get_config_value", side_effect=config_value):
            library_manager._start_port_summary_warm()

        assert library_manager._port_summary_warm_task is None


class TestPortSummaryInvalidationOnLibraryLoad:
    """The last lifecycle phase must drop cached port summaries for the library it just loaded.

    `LibraryRegistry.generate_new_library` publishes the library before its node types are
    populated, and the node loading that follows awaits. A port summary request landing in that
    window measures a library with no node types, and nothing invalidates afterwards, so that
    empty result would be served for the rest of the process's life.
    """

    _LIBRARY_NAME = "port-summary-invalidation-library"

    @pytest.fixture(autouse=True)
    def _clean_registry(self) -> Generator[None, None, None]:
        LibraryRegistry._clear()
        yield
        LibraryRegistry._clear()

    @pytest.mark.parametrize("library_kind", ["regular", "sandbox"])
    @pytest.mark.asyncio
    async def test_final_lifecycle_phase_drops_cached_port_summaries(
        self, engine: Engine, tmp_path: Path, library_kind: str
    ) -> None:
        """Both branches of the final phase reach the invalidation, sandbox and regular alike."""
        is_sandbox = library_kind == "sandbox"
        library_manager = engine.library_manager
        library_json = tmp_path / "griptape_nodes_library.json"
        library_json.write_text("{}")
        file_path = str(library_json)
        schema = LibrarySchema(
            name=self._LIBRARY_NAME,
            library_schema_version=LibrarySchema.LATEST_SCHEMA_VERSION,
            metadata=LibraryMetadata(
                author="test",
                description="port summary invalidation library",
                library_version="1.0.0",
                engine_version="1.0.0",
                tags=[],
            ),
            categories=[],
            nodes=[],
        )

        # Stand in for a port summary request that landed while the library was published but
        # empty, which is exactly the state the invalidation exists to discard.
        library_manager._library_to_port_summary_cache[self._LIBRARY_NAME] = _empty_port_summary_cache_entry()
        generation_before = library_manager._library_to_port_summaries_generation.get(self._LIBRARY_NAME, 0)

        library_info = _LibraryManager.LibraryInfo(
            lifecycle_state=_LibraryManager.LibraryLifecycleState.DEPENDENCIES_INSTALLED,
            fitness=_LibraryManager.LibraryFitness.NOT_EVALUATED,
            library_path=file_path,
            is_sandbox=is_sandbox,
        )
        library_manager._library_file_path_to_info = {file_path: library_info}

        with (
            patch.object(
                library_manager,
                "load_library_metadata_from_file_request",
                return_value=LoadLibraryMetadataFromFileResultSuccess(
                    library_schema=schema,
                    file_path=file_path,
                    git_remote=None,
                    git_ref=None,
                    enabled=True,
                    is_registered=False,
                    result_details=ResultDetails(message="OK", level=20),
                ),
            ),
            patch.object(library_manager, "_add_library_paths_to_sys_path", new=AsyncMock()),
            patch.object(library_manager, "_attempt_load_nodes_from_library"),
            patch.object(library_manager, "_attempt_generate_sandbox_library_from_schema", new=AsyncMock()),
        ):
            result = await library_manager._progress_library_through_lifecycle(
                library_info, file_path, RegisterLibraryFromFileRequest(file_path=file_path)
            )

        assert result is None
        assert self._LIBRARY_NAME not in library_manager._library_to_port_summary_cache
        # The generation bump is what lets a probe pass already in flight discard its stale result.
        assert library_manager._library_to_port_summaries_generation[self._LIBRARY_NAME] > generation_before


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

    def test_satisfied_returns_none(self, engine: Engine) -> None:
        library_manager = engine.library_manager
        with (
            patch.object(engine, "_config_manager", self._config_manager_returning(">=0.5,<1.0")),
            patch("griptape_nodes.utils.version_utils.engine_version", "0.5.3"),
        ):
            assert library_manager._check_engine_version() is None

    def test_unsatisfied_returns_detail_naming_running_version(self, engine: Engine) -> None:
        library_manager = engine.library_manager
        with (
            patch.object(engine, "_config_manager", self._config_manager_returning(">=2.0,<3.0")),
            patch("griptape_nodes.utils.version_utils.engine_version", "0.5.3"),
        ):
            detail = library_manager._check_engine_version()

        assert detail is not None
        assert "0.5.3" in detail

    def test_malformed_specifier_returns_detail(self, engine: Engine) -> None:
        library_manager = engine.library_manager
        with (
            patch.object(engine, "_config_manager", self._config_manager_returning("not-a-specifier")),
            patch("griptape_nodes.utils.version_utils.engine_version", "0.5.3"),
        ):
            detail = library_manager._check_engine_version()

        assert detail is not None
        assert "not a valid" in detail.lower()

    def test_no_key_returns_none(self, engine: Engine) -> None:
        library_manager = engine.library_manager
        with patch.object(engine, "_config_manager", self._config_manager_returning(None)):
            assert library_manager._check_engine_version() is None


class TestLibraryManagerProvisioningPlan:
    """`_plan_one_library_provisioning` is a pure decision the preview and execution share."""

    @pytest.mark.asyncio
    async def test_satisfied_git_entry_plans_skip(self, engine: Engine) -> None:
        from griptape_nodes.retained_mode.events.library_events import LibraryProvisioningActionKind

        library_manager = engine.library_manager
        download = LibraryDownload(name="git-lib", version=">=2.0,<3", git_url="griptape-ai/git-lib@v2")
        with patch.object(library_manager, "_installed_download_version", new=AsyncMock(return_value="2.1.0")):
            action = await library_manager._plan_one_library_provisioning(download)

        assert action.kind == LibraryProvisioningActionKind.SKIP
        assert action.destructive is False

    @pytest.mark.asyncio
    async def test_missing_git_entry_plans_install(self, engine: Engine) -> None:
        from griptape_nodes.retained_mode.events.library_events import LibraryProvisioningActionKind

        library_manager = engine.library_manager
        download = LibraryDownload(name="git-lib", version=">=2.0", git_url="griptape-ai/git-lib@v2.0")
        with patch.object(library_manager, "_installed_download_version", new=AsyncMock(return_value=None)):
            action = await library_manager._plan_one_library_provisioning(download)

        assert action.kind == LibraryProvisioningActionKind.INSTALL
        assert action.destructive is False

    @pytest.mark.asyncio
    async def test_wrong_git_version_plans_destructive_overwrite(self, engine: Engine) -> None:
        from griptape_nodes.retained_mode.events.library_events import LibraryProvisioningActionKind

        library_manager = engine.library_manager
        download = LibraryDownload(name="git-lib", version=">=2.0", git_url="griptape-ai/git-lib@v2.0")
        with patch.object(library_manager, "_installed_download_version", new=AsyncMock(return_value="1.0.0")):
            action = await library_manager._plan_one_library_provisioning(download)

        assert action.kind == LibraryProvisioningActionKind.OVERWRITE
        # A git overwrite deletes the local library directory before re-cloning.
        assert action.destructive is True

    @pytest.mark.asyncio
    async def test_version_pin_without_name_uses_repo_name_for_action_label(self, engine: Engine) -> None:
        # A {git_url, version} entry with no `name` still enforces its pin: the installed
        # copy is found by its repo-name directory, so a wrong version plans OVERWRITE
        # rather than silently no-opping. The action's library_name falls back to the repo name.
        from griptape_nodes.retained_mode.events.library_events import LibraryProvisioningActionKind

        library_manager = engine.library_manager
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
        config_manager.resolved_libraries_root.return_value = libraries_dir
        # find_files_recursive takes its depth ceiling from `config_manager.discovery_max_depth`,
        # so it needs a real int here or the recursive walk's depth comparison blows up on a MagicMock.
        config_manager.discovery_max_depth = DEFAULT_MAX_SEARCH_DEPTH
        return config_manager

    @pytest.mark.asyncio
    async def test_returns_version_from_matching_manifest(self, engine: Engine, tmp_path: Path) -> None:
        library_manager = engine.library_manager
        libraries_dir = tmp_path / "libraries"
        self._write_manifest(libraries_dir / "git-lib", "Griptape Nodes Library", "0.78.0")
        with patch.object(engine, "_config_manager", self._config_manager_for(libraries_dir)):
            assert await library_manager._installed_library_version("Griptape Nodes Library", libraries_dir) == "0.78.0"

    @pytest.mark.asyncio
    async def test_returns_none_when_no_manifest_matches(self, engine: Engine, tmp_path: Path) -> None:
        library_manager = engine.library_manager
        libraries_dir = tmp_path / "libraries"
        self._write_manifest(libraries_dir / "other", "Some Other Library", "1.0.0")
        with patch.object(engine, "_config_manager", self._config_manager_for(libraries_dir)):
            assert await library_manager._installed_library_version("Griptape Nodes Library", libraries_dir) is None

    @pytest.mark.asyncio
    async def test_returns_none_when_libraries_root_empty(self, engine: Engine, tmp_path: Path) -> None:
        library_manager = engine.library_manager
        libraries_dir = tmp_path / "empty-libraries"
        with patch.object(engine, "_config_manager", self._config_manager_for(libraries_dir)):
            assert await library_manager._installed_library_version("Griptape Nodes Library", libraries_dir) is None

    @pytest.mark.asyncio
    async def test_returns_none_when_manifest_has_no_version(self, engine: Engine, tmp_path: Path) -> None:
        library_manager = engine.library_manager
        libraries_dir = tmp_path / "libraries"
        self._write_manifest(libraries_dir / "git-lib", "Griptape Nodes Library", None)
        with patch.object(engine, "_config_manager", self._config_manager_for(libraries_dir)):
            assert await library_manager._installed_library_version("Griptape Nodes Library", libraries_dir) is None


class TestInstalledLibraryManifestPath:
    """The shared resolver behind both planner and loader.

    `_installed_library_manifest_path` backs both the provisioning planner
    (`_installed_library_version`) and the loader (`_discover_library_files`), so the
    file the planner reasons about is exactly the file discovery loads.
    """

    @pytest.mark.asyncio
    async def test_returns_manifest_path_for_matching_name(self, engine: Engine, tmp_path: Path) -> None:
        library_manager = engine.library_manager
        libraries_dir = tmp_path / "libraries"
        TestInstalledLibraryVersion._write_manifest(libraries_dir / "git-lib", "Griptape Nodes Library", "0.78.0")
        with patch.object(engine, "_config_manager", TestInstalledLibraryVersion._config_manager_for(libraries_dir)):
            result = await library_manager._installed_library_manifest_path("Griptape Nodes Library", libraries_dir)
        assert result == libraries_dir / "git-lib" / "griptape_nodes_library.json"

    @pytest.mark.asyncio
    async def test_returns_none_when_no_manifest_matches(self, engine: Engine, tmp_path: Path) -> None:
        library_manager = engine.library_manager
        libraries_dir = tmp_path / "libraries"
        TestInstalledLibraryVersion._write_manifest(libraries_dir / "other", "Some Other Library", "1.0.0")
        with patch.object(engine, "_config_manager", TestInstalledLibraryVersion._config_manager_for(libraries_dir)):
            assert (
                await library_manager._installed_library_manifest_path("Griptape Nodes Library", libraries_dir) is None
            )

    @pytest.mark.asyncio
    async def test_returns_none_when_libraries_root_empty(self, engine: Engine, tmp_path: Path) -> None:
        library_manager = engine.library_manager
        libraries_dir = tmp_path / "empty-libraries"
        with patch.object(
            engine,
            "_config_manager",
            TestInstalledLibraryVersion._config_manager_for(libraries_dir),
        ):
            assert (
                await library_manager._installed_library_manifest_path("Griptape Nodes Library", libraries_dir) is None
            )


class TestInstalledDownloadVersion:
    """`_installed_download_version` locates the installed copy the way the download handler lands it.

    A download entry without a `name` is matched by its repo-name directory
    (`libraries_directory/<repo-name>/`), keeping the version-check consistent
    with clone/skip/overwrite so a `version` pin works without `name`. An explicit
    `name` overrides the directory match and resolves by manifest name instead.
    """

    @pytest.mark.asyncio
    async def test_resolves_by_repo_name_directory_when_name_absent(self, engine: Engine, tmp_path: Path) -> None:
        # The clone dir is the repo name from the git URL, while the manifest's own
        # `name` differs; the lookup must key off the directory, not the manifest name.
        library_manager = engine.library_manager
        libraries_dir = tmp_path / "libraries"
        TestInstalledLibraryVersion._write_manifest(libraries_dir / "git-lib", "Griptape Nodes Library", "1.2.3")
        download = LibraryDownload(git_url="griptape-ai/git-lib@v2.0", version=">=1.0")
        with patch.object(engine, "_config_manager", TestInstalledLibraryVersion._config_manager_for(libraries_dir)):
            assert await library_manager._installed_download_version(download, libraries_dir) == "1.2.3"

    @pytest.mark.asyncio
    async def test_returns_none_when_repo_directory_absent(self, engine: Engine, tmp_path: Path) -> None:
        library_manager = engine.library_manager
        libraries_dir = tmp_path / "libraries"
        TestInstalledLibraryVersion._write_manifest(libraries_dir / "other-lib", "Other", "1.0.0")
        download = LibraryDownload(git_url="griptape-ai/git-lib@v2.0", version=">=1.0")
        with patch.object(engine, "_config_manager", TestInstalledLibraryVersion._config_manager_for(libraries_dir)):
            assert await library_manager._installed_download_version(download, libraries_dir) is None

    @pytest.mark.asyncio
    async def test_name_overrides_directory_match(self, engine: Engine, tmp_path: Path) -> None:
        # With an explicit `name`, resolve by manifest name even when the library lives
        # under a directory that does not match the repo name (e.g. legacy XDG layout).
        library_manager = engine.library_manager
        libraries_dir = tmp_path / "libraries"
        TestInstalledLibraryVersion._write_manifest(libraries_dir / "legacy-dir", "Griptape Nodes Library", "0.9.0")
        download = LibraryDownload(git_url="griptape-ai/git-lib@v2.0", version=">=1.0", name="Griptape Nodes Library")
        with patch.object(engine, "_config_manager", TestInstalledLibraryVersion._config_manager_for(libraries_dir)):
            assert await library_manager._installed_download_version(download, libraries_dir) == "0.9.0"

    @pytest.mark.asyncio
    async def test_returns_none_when_libraries_root_empty(self, engine: Engine, tmp_path: Path) -> None:
        library_manager = engine.library_manager
        download = LibraryDownload(git_url="griptape-ai/git-lib@v2.0", version=">=1.0")
        libraries_dir = tmp_path / "empty-libraries"
        with patch.object(
            engine,
            "_config_manager",
            TestInstalledLibraryVersion._config_manager_for(libraries_dir),
        ):
            assert await library_manager._installed_download_version(download, libraries_dir) is None

    @pytest.mark.asyncio
    async def test_explicit_libraries_path_probes_target_not_live(self, engine: Engine, tmp_path: Path) -> None:
        # The preview passes the TARGET project's libraries dir. The probe must read it,
        # not the live config, so it never falls back to the active workspace.
        library_manager = engine.library_manager
        target_libs = tmp_path / "target" / "libraries"
        TestInstalledLibraryVersion._write_manifest(target_libs / "git-lib", "Griptape Nodes Library", "3.3.0")
        download = LibraryDownload(git_url="griptape-ai/git-lib@v2.0", version=">=1.0")
        live_config = MagicMock()
        # Live config points elsewhere; an explicit libraries_path must win, and the live
        # libraries_directory must never be read.
        live_config.get_config_value.return_value = str(tmp_path / "live" / "libraries")
        live_config.workspace_path = str(tmp_path / "live")
        live_config.discovery_max_depth = DEFAULT_MAX_SEARCH_DEPTH
        with patch.object(engine, "_config_manager", live_config):
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
    async def test_provisioned_manifest_path_is_discovered(self, engine: Engine, tmp_path: Path) -> None:
        library_manager = engine.library_manager
        libraries_dir = tmp_path / "libraries"
        manifest_dir = libraries_dir / "griptape-nodes-library-standard"
        TestInstalledLibraryVersion._write_manifest(manifest_dir, "Griptape Nodes Library", "0.78.0")
        expected_manifest = manifest_dir / "griptape_nodes_library.json"

        # The manifest path the download handler appends to libraries_to_register after
        # provisioning the pinned library.
        config = [str(expected_manifest)]

        config_manager = TestInstalledLibraryVersion._config_manager_for(libraries_dir)
        config_manager.get_config_value.side_effect = _config_value_dispatcher(libraries_dir, config)
        with patch.object(engine, "_config_manager", config_manager):
            result = await library_manager._discover_library_files()

        discovered_paths = [Path(entry.registration.path) for entry in result if entry.registration.path is not None]
        assert expected_manifest in discovered_paths

    @pytest.mark.asyncio
    async def test_missing_register_path_is_skipped(self, engine: Engine, tmp_path: Path) -> None:
        library_manager = engine.library_manager
        libraries_dir = tmp_path / "libraries"
        libraries_dir.mkdir(parents=True, exist_ok=True)

        # A register entry whose path is not on disk yet: nothing to discover.
        config = [str(libraries_dir / "missing" / "griptape_nodes_library.json")]

        config_manager = TestInstalledLibraryVersion._config_manager_for(libraries_dir)
        config_manager.get_config_value.side_effect = _config_value_dispatcher(libraries_dir, config)
        with patch.object(engine, "_config_manager", config_manager):
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
    async def test_engine_version_failure_blocks_provisioning(self, engine: Engine) -> None:
        library_manager = engine.library_manager
        with (
            patch.object(library_manager, "_check_engine_version", return_value="engine too old"),
            patch.object(library_manager, "_provision_one_library", new=AsyncMock()) as mock_provision,
        ):
            failures = await library_manager._reconcile_libraries_from_config()

        assert failures == ["engine too old"]
        # The gate runs before any disk mutation.
        mock_provision.assert_not_called()

    @pytest.mark.asyncio
    async def test_only_download_entries_are_provisioned(self, engine: Engine) -> None:
        library_manager = engine.library_manager
        # Both shapes of a download entry: a bare git-URL string and the object form.
        download_config = [
            "griptape-ai/bare-lib@v2",
            {"name": "git-lib", "git_url": "griptape-ai/git-lib@v2", "version": ">=2.0"},
        ]
        # A path-only register entry must never be provisioned (requirement 1).
        register_config = ["griptape_nodes_library.json", {"path": "../shared/lib"}]
        config_manager = self._config_manager_for_keys(downloads=download_config, register=register_config)
        with (
            patch.object(engine, "_config_manager", config_manager),
            patch.object(library_manager, "_check_engine_version", return_value=None),
            patch.object(library_manager, "_provision_one_library", new=AsyncMock(return_value=None)) as mock_provision,
        ):
            failures = await library_manager._reconcile_libraries_from_config()

        assert failures == []
        # Only the two download entries reach provisioning; nothing from the register list does.
        assert mock_provision.await_count == 2  # noqa: PLR2004

    @pytest.mark.asyncio
    async def test_provision_failure_is_collected(self, engine: Engine) -> None:
        library_manager = engine.library_manager
        download_config = [{"name": "git-lib", "git_url": "griptape-ai/git-lib@v2", "version": ">=2.0"}]
        config_manager = self._config_manager_for_keys(downloads=download_config)
        with (
            patch.object(engine, "_config_manager", config_manager),
            patch.object(library_manager, "_check_engine_version", return_value=None),
            patch.object(library_manager, "_provision_one_library", new=AsyncMock(return_value="clone failed")),
        ):
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
    @contextlib.contextmanager
    def _patch_managers(
        engine: Engine, *, dirs: object, merged: object, libraries_root: object = None
    ) -> Generator[tuple[MagicMock, MagicMock], None, None]:
        """Wire the mocked ProjectManager/ConfigManager the new handler calls.

        `libraries_root` is what resolve_libraries_root_for_project_id returns: None (the default)
        makes the preview fall back to the merged config's workspace-relative libraries dir.
        """
        mock_project_manager = MagicMock()
        mock_project_manager.resolve_provisioning_config_dirs = AsyncMock(return_value=dirs)
        mock_project_manager.resolve_libraries_root_for_project_id = AsyncMock(return_value=libraries_root)
        # The gate that mirrors activation: these projects declare nothing unresolvable, so the
        # preview must proceed past it to the plan under test.
        mock_project_manager.unresolvable_declared_path_messages.return_value = []
        mock_config_manager = MagicMock()
        mock_config_manager.compute_project_provisioning_config.return_value = merged
        TestPreviewProjectProvisioning._use_real_libraries_root_formula(mock_config_manager)
        with (
            patch.object(engine, "_project_manager", mock_project_manager),
            patch.object(engine, "_config_manager", mock_config_manager),
        ):
            yield mock_project_manager, mock_config_manager

    @staticmethod
    def _use_real_libraries_root_formula(mock_config_manager: MagicMock) -> None:
        """Have a mocked ConfigManager compute the libraries fallback with the REAL formula.

        The mock supplies the config layers (configured_global_workspace_path); production code does
        the math. Without this, the fallback would return a MagicMock and these tests would silently
        assert nothing about where libraries actually land.
        """
        mock_config_manager.default_libraries_root.side_effect = partial(
            ConfigManager.default_libraries_root, mock_config_manager
        )

    @staticmethod
    @contextlib.contextmanager
    def _patch_system_defaults(engine: Engine, *, merged: object) -> Generator[tuple[MagicMock, MagicMock], None, None]:
        """Wire the mocked ConfigManager for the system-defaults branch.

        System defaults reads its merged config from compute_system_defaults_provisioning_config
        (defaults -> user -> env, no project-adjacent or workspace file), so the handler never
        calls ProjectManager.resolve_provisioning_config_dirs for it.
        """
        mock_project_manager = MagicMock()
        mock_config_manager = MagicMock()
        mock_config_manager.compute_system_defaults_provisioning_config.return_value = merged
        with (
            patch.object(engine, "_project_manager", mock_project_manager),
            patch.object(engine, "_config_manager", mock_config_manager),
        ):
            yield mock_project_manager, mock_config_manager

    @pytest.mark.asyncio
    async def test_not_loaded_project_is_failure(self, engine: Engine) -> None:
        from griptape_nodes.retained_mode.events.library_events import (
            PreviewProjectProvisioningRequest,
            PreviewProjectProvisioningResultFailure,
        )

        library_manager = engine.library_manager
        mock_project_manager = MagicMock()
        mock_project_manager.resolve_provisioning_config_dirs = AsyncMock(return_value=None)
        with patch.object(engine, "_project_manager", mock_project_manager):
            result = await library_manager.on_preview_project_provisioning_request(
                PreviewProjectProvisioningRequest(project_id="/nope/project.yml")
            )

        assert isinstance(result, PreviewProjectProvisioningResultFailure)

    @pytest.mark.asyncio
    async def test_no_download_entries_is_empty_success(self, engine: Engine, tmp_path: Path) -> None:
        from griptape_nodes.retained_mode.events.library_events import (
            PreviewProjectProvisioningRequest,
            PreviewProjectProvisioningResultSuccess,
        )

        library_manager = engine.library_manager
        merged = self._merged_config([])
        with self._patch_managers(engine, dirs=MagicMock(), merged=merged):
            result = await library_manager.on_preview_project_provisioning_request(
                PreviewProjectProvisioningRequest(project_id=str(tmp_path / "project.yml"))
            )

        assert isinstance(result, PreviewProjectProvisioningResultSuccess)
        assert result.actions == []
        assert result.engine_version_failure is None

    @pytest.mark.asyncio
    async def test_download_entries_preserve_order_and_flags(self, engine: Engine, tmp_path: Path) -> None:
        from griptape_nodes.retained_mode.events.library_events import (
            LibraryProvisioningActionKind,
            PreviewProjectProvisioningRequest,
            PreviewProjectProvisioningResultSuccess,
        )

        library_manager = engine.library_manager
        merged = self._merged_config(
            [
                {"name": "skip-lib", "git_url": "griptape-ai/skip-lib@v2", "version": ">=2.0"},
                {"name": "install-lib", "git_url": "griptape-ai/install-lib@v2", "version": ">=2.0"},
                {"name": "overwrite-lib", "git_url": "griptape-ai/overwrite-lib@v2", "version": ">=2.0"},
            ]
        )
        installed = {"skip-lib": "2.1.0", "install-lib": None, "overwrite-lib": "1.0.0"}
        with (
            self._patch_managers(engine, dirs=MagicMock(), merged=merged),
            patch.object(
                library_manager,
                "_installed_download_version",
                new=AsyncMock(side_effect=lambda download, _libraries_path=None: installed[download.name]),
            ),
        ):
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
    async def test_plan_reads_merged_config_for_resolved_dirs(self, engine: Engine, tmp_path: Path) -> None:
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

        library_manager = engine.library_manager
        dirs = MagicMock()
        dirs.project_dir = tmp_path / "proj"
        dirs.workspace_dir = tmp_path / "ws"
        # The merged value (e.g. from the workspace layer) differs from anything the
        # project-adjacent file alone would carry; the plan must reflect this entry.
        merged = self._merged_config([{"name": "merged-lib", "git_url": "griptape-ai/merged-lib@v2", "version": ">=2"}])
        with (
            self._patch_managers(engine, dirs=dirs, merged=merged) as (
                _mock_project_manager,
                mock_config_manager,
            ),
            patch.object(library_manager, "_installed_download_version", new=AsyncMock(return_value=None)),
        ):
            result = await library_manager.on_preview_project_provisioning_request(
                PreviewProjectProvisioningRequest(project_id=str(tmp_path / "project.yml"))
            )

        compute = mock_config_manager.compute_project_provisioning_config
        compute.assert_called_once_with(dirs.project_dir, dirs.workspace_dir, apply_override=dirs.apply_override)
        assert isinstance(result, PreviewProjectProvisioningResultSuccess)
        assert [a.library_name for a in result.actions] == ["merged-lib"]
        assert result.actions[0].kind == LibraryProvisioningActionKind.INSTALL

    @pytest.mark.asyncio
    async def test_probes_global_workspace_for_unset_libraries_fallback(self, engine: Engine, tmp_path: Path) -> None:
        """The unset-libraries_dir probe reads the GLOBAL workspace's libraries dir.

        With no own/inherited libraries_dir, the preview fallback resolves libraries_directory against
        the GLOBAL configured workspace (configured_global_workspace_path), mirroring the live
        ConfigManager.resolved_libraries_root fallback. A stale, unsatisfying version sitting in the
        GLOBAL workspace's libraries dir must be found so the plan is a destructive OVERWRITE, not a
        under-reported non-destructive INSTALL. Exercises the real on-disk probe (no mock of
        _installed_download_version).
        """
        from griptape_nodes.retained_mode.events.library_events import (
            LibraryProvisioningActionKind,
            PreviewProjectProvisioningRequest,
            PreviewProjectProvisioningResultSuccess,
        )

        library_manager = engine.library_manager
        global_ws = tmp_path / "global"
        TestInstalledLibraryVersion._write_manifest(global_ws / "libraries" / "git-lib", "git-lib", "1.0.0")
        merged = self._merged_config(
            [{"git_url": "griptape-ai/git-lib@v2.0", "version": ">=2.0"}],
            # A self-contained target pins merged workspace_directory to its own dir; the fallback must
            # NOT probe here (it holds no installed lib), it must probe the global workspace below.
            workspace_directory=str(tmp_path / "target"),
            libraries_directory="libraries",
        )
        # The live config's global workspace is where the stale version actually lives. If the probe
        # used the target/merged workspace instead, the plan would wrongly be a non-destructive INSTALL.
        live_config = MagicMock()
        live_config.configured_global_workspace_path.return_value = global_ws
        live_config.compute_project_provisioning_config.return_value = merged
        self._use_real_libraries_root_formula(live_config)
        mock_project_manager = MagicMock()
        mock_project_manager.resolve_provisioning_config_dirs = AsyncMock(return_value=MagicMock())
        mock_project_manager.resolve_libraries_root_for_project_id = AsyncMock(return_value=None)
        # This project declares nothing unresolvable, so the preview must proceed past the
        # activation-mirroring gate to the fallback probe under test.
        mock_project_manager.unresolvable_declared_path_messages.return_value = []
        with (
            patch.object(engine, "_project_manager", mock_project_manager),
            patch.object(engine, "_config_manager", live_config),
        ):
            result = await library_manager.on_preview_project_provisioning_request(
                PreviewProjectProvisioningRequest(project_id=str(tmp_path / "project.yml"))
            )

        assert isinstance(result, PreviewProjectProvisioningResultSuccess)
        assert [a.kind for a in result.actions] == [LibraryProvisioningActionKind.OVERWRITE]
        assert result.actions[0].destructive is True
        assert result.actions[0].installed_version == "1.0.0"

    @pytest.mark.asyncio
    async def test_probes_offline_resolved_libraries_root_over_workspace_default(
        self, engine: Engine, tmp_path: Path
    ) -> None:
        """A non-None libraries_root from the offline resolver overrides the workspace-relative default.

        Exercises the branch that consumes resolve_libraries_root_for_project_id: when the target
        project's own/inherited libraries_dir relocates the sink (e.g. a child sharing its parent's
        libraries tree), the probe must read THAT dir, not merged workspace/libraries_directory. Here
        the unsatisfying version lives only in the resolved root; if the preview probed the merged
        workspace default instead, the plan would wrongly be a non-destructive INSTALL.
        """
        from griptape_nodes.retained_mode.events.library_events import (
            LibraryProvisioningActionKind,
            PreviewProjectProvisioningRequest,
            PreviewProjectProvisioningResultSuccess,
        )

        library_manager = engine.library_manager
        resolved_root = tmp_path / "shared-libs"
        TestInstalledLibraryVersion._write_manifest(resolved_root / "git-lib", "git-lib", "1.0.0")
        # The merged workspace-relative default points at an empty dir; probing it would miss the
        # stale version and under-report the plan as INSTALL.
        merged = self._merged_config(
            [{"git_url": "griptape-ai/git-lib@v2.0", "version": ">=2.0"}],
            workspace_directory=str(tmp_path / "ws"),
            libraries_directory="libraries",
        )
        with self._patch_managers(engine, dirs=MagicMock(), merged=merged, libraries_root=resolved_root):
            result = await library_manager.on_preview_project_provisioning_request(
                PreviewProjectProvisioningRequest(project_id=str(tmp_path / "project.yml"))
            )

        assert isinstance(result, PreviewProjectProvisioningResultSuccess)
        assert [a.kind for a in result.actions] == [LibraryProvisioningActionKind.OVERWRITE]
        assert result.actions[0].destructive is True
        assert result.actions[0].installed_version == "1.0.0"

    @pytest.mark.asyncio
    async def test_unsatisfiable_engine_version_populates_failure(self, engine: Engine, tmp_path: Path) -> None:
        from griptape_nodes.retained_mode.events.library_events import (
            PreviewProjectProvisioningRequest,
            PreviewProjectProvisioningResultSuccess,
        )

        library_manager = engine.library_manager
        merged = self._merged_config([], engine_version=">=2.0,<3.0")
        with (
            self._patch_managers(engine, dirs=MagicMock(), merged=merged),
            patch("griptape_nodes.utils.version_utils.engine_version", "0.5.3"),
        ):
            result = await library_manager.on_preview_project_provisioning_request(
                PreviewProjectProvisioningRequest(project_id=str(tmp_path / "project.yml"))
            )

        assert isinstance(result, PreviewProjectProvisioningResultSuccess)
        assert result.engine_version_failure is not None
        # Same text the live gate produces: it names the running engine version.
        assert "0.5.3" in result.engine_version_failure

    @pytest.mark.asyncio
    async def test_satisfiable_engine_version_leaves_failure_none(self, engine: Engine, tmp_path: Path) -> None:
        from griptape_nodes.retained_mode.events.library_events import (
            PreviewProjectProvisioningRequest,
            PreviewProjectProvisioningResultSuccess,
        )

        library_manager = engine.library_manager
        merged = self._merged_config([], engine_version=">=0.5,<1.0")
        with (
            self._patch_managers(engine, dirs=MagicMock(), merged=merged),
            patch("griptape_nodes.utils.version_utils.engine_version", "0.5.3"),
        ):
            result = await library_manager.on_preview_project_provisioning_request(
                PreviewProjectProvisioningRequest(project_id=str(tmp_path / "project.yml"))
            )

        assert isinstance(result, PreviewProjectProvisioningResultSuccess)
        assert result.engine_version_failure is None

    @pytest.mark.asyncio
    async def test_system_defaults_plans_from_user_layer_without_resolving_dirs(self, engine: Engine) -> None:
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

        library_manager = engine.library_manager
        merged = self._merged_config([{"name": "user-pin", "git_url": "griptape-ai/user-pin@v2", "version": "==2.0.0"}])
        with (
            self._patch_system_defaults(engine, merged=merged) as (mock_project_manager, _mock_config_manager),
            patch.object(library_manager, "_installed_download_version", new=AsyncMock(return_value="1.0.0")),
        ):
            result = await library_manager.on_preview_project_provisioning_request(
                PreviewProjectProvisioningRequest(project_id=SYSTEM_DEFAULTS_KEY)
            )

        mock_project_manager.resolve_provisioning_config_dirs.assert_not_called()
        assert isinstance(result, PreviewProjectProvisioningResultSuccess)
        assert [a.library_name for a in result.actions] == ["user-pin"]
        assert result.actions[0].kind == LibraryProvisioningActionKind.OVERWRITE
        assert result.actions[0].destructive is True

    @pytest.mark.asyncio
    async def test_system_defaults_unsatisfiable_engine_version_populates_failure(self, engine: Engine) -> None:
        """A user-config engine_version pin gates the system-defaults switch too."""
        from griptape_nodes.retained_mode.events.library_events import (
            PreviewProjectProvisioningRequest,
            PreviewProjectProvisioningResultSuccess,
        )

        library_manager = engine.library_manager
        merged = self._merged_config([], engine_version=">=2.0,<3.0")
        with (
            self._patch_system_defaults(engine, merged=merged),
            patch("griptape_nodes.utils.version_utils.engine_version", "0.5.3"),
        ):
            result = await library_manager.on_preview_project_provisioning_request(
                PreviewProjectProvisioningRequest(project_id=SYSTEM_DEFAULTS_KEY)
            )

        assert isinstance(result, PreviewProjectProvisioningResultSuccess)
        assert result.engine_version_failure is not None
        assert "0.5.3" in result.engine_version_failure

    @pytest.mark.asyncio
    async def test_system_defaults_no_pins_is_empty_success(self, engine: Engine) -> None:
        """No user-config pins means nothing to provision: empty plan, no modal."""
        from griptape_nodes.retained_mode.events.library_events import (
            PreviewProjectProvisioningRequest,
            PreviewProjectProvisioningResultSuccess,
        )

        library_manager = engine.library_manager
        merged = self._merged_config([])
        with self._patch_system_defaults(engine, merged=merged):
            result = await library_manager.on_preview_project_provisioning_request(
                PreviewProjectProvisioningRequest(project_id=SYSTEM_DEFAULTS_KEY)
            )

        assert isinstance(result, PreviewProjectProvisioningResultSuccess)
        assert result.actions == []
        assert result.engine_version_failure is None


class TestProvisionGitLibraryOverwriteDir:
    """`_provision_git_library` aims the destructive overwrite at the installed dir."""

    @pytest.mark.asyncio
    async def test_overwrite_targets_installed_manifest_dir(self, engine: Engine, tmp_path: Path) -> None:
        """Defect #3: the overwrite deletes the manifest's dir, not libraries_path/<repo-name>.

        When the installed dir name != git repo name, `_provision_git_library` resolves the
        installed manifest and passes its parent as download_directory + target_directory_name
        so the handler's delete lands on that exact dir.
        """
        from griptape_nodes.retained_mode.events.library_events import (
            DownloadLibraryRequest,
            DownloadLibraryResultSuccess,
        )

        library_manager = engine.library_manager
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
            patch.object(engine, "ahandle_request", ahandle),
            patch.object(
                library_manager, "_installed_library_manifest_path", new=AsyncMock(return_value=manifest_path)
            ),
        ):
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
    async def test_fresh_install_leaves_dir_hints_none(self, engine: Engine) -> None:
        """A fresh install passes no dir hints, keeping the handler's repo-name default.

        When installed_version is None there is nothing to overwrite, so the manifest is
        never resolved and both directory hints stay None.
        """
        from griptape_nodes.retained_mode.events.library_events import (
            DownloadLibraryRequest,
            DownloadLibraryResultSuccess,
        )

        library_manager = engine.library_manager
        download = LibraryDownload(name="my-lib", version=">=2.0", git_url="griptape-ai/repo-name@v2.0")

        success = MagicMock(spec=DownloadLibraryResultSuccess)
        ahandle = AsyncMock(return_value=success)
        with (
            patch.object(engine, "ahandle_request", ahandle),
            patch.object(library_manager, "_installed_library_manifest_path", new=AsyncMock()) as mock_resolve,
        ):
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

    def test_not_initializing_by_default(self, engine: Engine) -> None:
        assert engine.library_manager.is_initializing() is False

    @pytest.mark.asyncio
    async def test_reload_brackets_is_initializing(self, engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
        """is_initializing() is True for the duration of the reload and False once it returns."""
        from griptape_nodes.retained_mode.events.library_events import (
            ReloadAllLibrariesRequest,
            ReloadAllLibrariesResultSuccess,
        )

        library_manager = engine.library_manager
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
        self, engine: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failure during reload still clears the flag (finally), so the GUI doesn't hang."""
        from griptape_nodes.retained_mode.events.library_events import ReloadAllLibrariesRequest

        library_manager = engine.library_manager

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
    async def test_reconcile_download_does_not_persist_to_global_config(self, engine: Engine, tmp_path: Path) -> None:
        """auto_register=False (the reconcile/provisioning path) leaves global libraries_to_register untouched."""
        from griptape_nodes.retained_mode.events.library_events import (
            DownloadLibraryRequest,
            DownloadLibraryResultSuccess,
        )

        library_manager = engine.library_manager
        config_mgr = engine.config_manager
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
    async def test_explicit_download_persists_to_global_config(self, engine: Engine, tmp_path: Path) -> None:
        """auto_register=True (the explicit CLI download) appends the clone path to global libraries_to_register."""
        from griptape_nodes.retained_mode.events.library_events import (
            DownloadLibraryRequest,
            DownloadLibraryResultSuccess,
            RegisterLibraryFromFileResultSuccess,
        )

        library_manager = engine.library_manager
        config_mgr = engine.config_manager

        # download_library_request routes registration through the engine's ahandle_request;
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
                engine,
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
    async def test_download_only_library_is_discovered_from_workspace(self, engine: Engine, tmp_path: Path) -> None:
        """A libraries_to_download entry with no libraries_to_register row is still discovered."""
        from griptape_nodes.retained_mode.managers.settings import LIBRARIES_TO_DOWNLOAD_KEY

        library_manager = engine.library_manager
        config_mgr = engine.config_manager

        libraries_dir = tmp_path / "libraries"
        manifest_path = self._install_manifest(libraries_dir, "remote_lib", "remote_lib")

        def get_config_value(key: str, **_: object) -> object:
            if key == LIBRARIES_TO_REGISTER_KEY:
                return []
            if key == LIBRARIES_TO_DOWNLOAD_KEY:
                return ["owner/remote_lib"]
            if key == "libraries_directory":
                return str(libraries_dir)
            if key == "workspace_directory":
                # libraries_directory is absolute here, so the global-workspace base is unused for
                # resolution, but configured_global_workspace_path() must get a real path, not None.
                return str(tmp_path)
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
        self, engine: Engine, isolate_user_config: Path, tmp_path: Path
    ) -> None:
        """A project-layer libraries_to_download pin must NOT be written into the global user config."""
        library_manager = engine.library_manager
        config_mgr = engine.config_manager

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

    def test_persist_creates_missing_category(self, engine: Engine, isolate_user_config: Path) -> None:
        """A library declaring a brand-new category writes its contents verbatim to global."""
        library_manager = engine.library_manager

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
    async def test_schema_validation_failure_surfaces_on_library_info(self, engine: Engine, tmp_path: Path) -> None:
        from griptape_nodes.retained_mode.managers.library_manager import LibraryManager

        library_manager = engine.library_manager

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
    async def test_missing_file_surfaces_missing_fitness(self, engine: Engine, tmp_path: Path) -> None:
        from griptape_nodes.retained_mode.managers.library_manager import LibraryManager

        library_manager = engine.library_manager

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

    def test_maps_fields_and_disabled_state(self, engine: Engine) -> None:
        from griptape_nodes.retained_mode.managers.library_manager import LibraryManager

        library_manager = engine.library_manager

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

    def test_empty_when_no_libraries(self, engine: Engine) -> None:
        library_manager = engine.library_manager
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

    def test_denied_library_is_unusable_with_permission_problem(self, engine: Engine) -> None:
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

        engine.event_manager.add_authorization_hook(deny)
        with patch(
            "griptape_nodes.retained_mode.managers.version_compatibility_manager.VersionCompatibilityManager.check_library_version_compatibility",
            return_value=[],
        ):
            result = engine.library_manager.evaluate_library_fitness_request(
                EvaluateLibraryFitnessRequest(schema=self._schema("blocked-lib", LifecycleStage.LABS))
            )

        assert seen == {"action": "LoadLibrary", "subject_id": "blocked-lib", "stage": "LABS"}
        assert isinstance(result, EvaluateLibraryFitnessResultFailure)
        assert result.fitness == _LibraryManager.LibraryFitness.UNUSABLE
        problems = [p for p in result.problems if isinstance(p, PermissionDeniedProblem)]
        assert len(problems) == 1
        assert "Ask your admin to enable Labs libraries." in problems[0].collate_problems_for_display(problems)

    def test_allowed_library_passes(self, engine: Engine) -> None:
        from griptape_nodes.retained_mode.events.library_events import (
            EvaluateLibraryFitnessRequest,
            EvaluateLibraryFitnessResultSuccess,
        )

        # No authorization hook registered -> the checkpoint allows.
        with patch(
            "griptape_nodes.retained_mode.managers.version_compatibility_manager.VersionCompatibilityManager.check_library_version_compatibility",
            return_value=[],
        ):
            result = engine.library_manager.evaluate_library_fitness_request(
                EvaluateLibraryFitnessRequest(schema=self._schema("ok-lib"))
            )
        assert isinstance(result, EvaluateLibraryFitnessResultSuccess)

    def test_denied_node_is_a_library_problem_but_library_stays_usable(self, engine: Engine) -> None:
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

        engine.event_manager.add_authorization_hook(deny)
        with patch(
            "griptape_nodes.retained_mode.managers.version_compatibility_manager.VersionCompatibilityManager.check_library_version_compatibility",
            return_value=[],
        ):
            result = engine.library_manager.evaluate_library_fitness_request(
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


class TestLibraryManagerDuplicateEntryHygiene:
    """Regression tests for issue #5039.

    A library that ends up with more than one entry in `_library_file_path_to_info` (a duplicate
    install, or a filename variation left behind by a git operation) desynchronizes the update and
    update-check paths, producing a permanent "update available" loop. The fix keeps the dict free
    of duplicates and routes both paths through the same resolver.
    """

    def _lib_info(
        self,
        library_manager: _LibraryManager,
        path: str,
        name: str,
        lifecycle_state: _LibraryManager.LibraryLifecycleState = _LibraryManager.LibraryLifecycleState.LOADED,
    ) -> _LibraryManager.LibraryInfo:
        return library_manager.LibraryInfo(
            lifecycle_state=lifecycle_state,
            library_path=path,
            is_sandbox=False,
            library_name=name,
            library_version="0.81.0",
            fitness=_LibraryManager.LibraryFitness.GOOD,
            problems=[],
        )

    def test_unload_removes_all_entries_for_library_name(self, engine: Engine) -> None:
        """Unload must drop every entry for the name, not just the first, so no stale copy lingers."""
        library_manager = engine.library_manager

        # Two on-disk copies registered under one name: one on `main`, one on `stable`. Both
        # report the same version but live at different paths (the #5039 scenario).
        entries = {
            "/libs/copyA/griptape_nodes_library.json": self._lib_info(
                library_manager, "/libs/copyA/griptape_nodes_library.json", "MyLib"
            ),
            "/libs/copyB/griptape-nodes-library.json": self._lib_info(
                library_manager, "/libs/copyB/griptape-nodes-library.json", "MyLib"
            ),
        }

        with (
            patch.object(library_manager, "_library_file_path_to_info", entries),
            patch.object(LibraryRegistry, "unregister_library"),
            patch.object(library_manager, "_unregister_all_stable_module_aliases_for_library"),
        ):
            result = library_manager.unload_library_from_registry_request(
                UnloadLibraryFromRegistryRequest(library_name="MyLib")
            )

        assert isinstance(result, UnloadLibraryFromRegistryResultSuccess)
        # No entry for MyLib should survive the unload.
        remaining = [info for info in entries.values() if info.library_name == "MyLib"]
        assert remaining == []
        assert entries == {}

    def test_reload_collapses_duplicate_entries_to_one(self, engine: Engine) -> None:
        """Reload must collapse duplicate entries to one.

        It must not leave a pre-operation entry alongside the reloaded one when the git operation
        resolves the JSON under a different filename.
        """
        library_manager = engine.library_manager

        # Pre-operation entry keyed under the dashed filename.
        old_path = "/libs/copy/griptape-nodes-library.json"
        # The git operation resolves the underscore filename on disk.
        new_path = "/libs/copy/griptape_nodes_library.json"
        entries = {old_path: self._lib_info(library_manager, old_path, "MyLib")}

        mock_library = MagicMock()
        mock_library.get_metadata.return_value = MagicMock(library_version="0.81.0")

        with (
            patch.object(library_manager, "_library_file_path_to_info", entries),
            patch.object(
                engine,
                "handle_request",
                return_value=UnloadLibraryFromRegistryResultSuccess(result_details="ok"),
            ),
            patch(
                "griptape_nodes.retained_mode.managers.library_manager.find_file_in_directory",
                return_value=Path(new_path),
            ),
            patch.object(
                engine,
                "ahandle_request",
                AsyncMock(return_value=RegisterLibraryFromFileResultSuccess(library_name="MyLib", result_details="ok")),
            ),
            patch.object(LibraryRegistry, "get_library", return_value=mock_library),
        ):
            result = asyncio.run(
                library_manager._reload_library_after_git_operation(
                    library_name="MyLib",
                    library_file_path=old_path,
                    failure_result_class=RegisterLibraryFromFileResultFailure,
                )
            )

        assert result == "0.81.0"
        # Exactly one entry for MyLib, keyed under the reloaded filename. The reload stores
        # str(Path(...)), so normalize the expected key the same way for cross-platform parity
        # (Windows renders the separators as backslashes).
        mylib_paths = [path for path, info in entries.items() if info.library_name == "MyLib"]
        assert mylib_paths == [str(Path(new_path))]

    def test_resolver_prefers_loaded_copy_over_failed_duplicate(self, engine: Engine) -> None:
        """The resolver must return the LOADED copy, not a dead duplicate.

        A duplicate install keeps a second entry marked FAILURE (DuplicateLibraryProblem) in the
        dict, inserted BEFORE the loaded copy in some orderings. First-match would resolve the
        dead copy whose on-disk state the update path can never make agree with the loaded copy's
        version, producing a permanent "update available" loop (issue #5039). Preferring the LOADED
        entry keeps path resolution consistent with the copy whose version the check reads.
        """
        library_manager = engine.library_manager

        # The FAILURE duplicate is inserted first, so first-match would pick it.
        entries = {
            "/libs/dead/griptape_nodes_library.json": self._lib_info(
                library_manager,
                "/libs/dead/griptape_nodes_library.json",
                "MyLib",
                lifecycle_state=_LibraryManager.LibraryLifecycleState.FAILURE,
            ),
            "/libs/loaded/griptape_nodes_library.json": self._lib_info(
                library_manager,
                "/libs/loaded/griptape_nodes_library.json",
                "MyLib",
                lifecycle_state=_LibraryManager.LibraryLifecycleState.LOADED,
            ),
        }

        with patch.object(library_manager, "_library_file_path_to_info", entries):
            info = library_manager.get_library_info_by_library_name("MyLib")

        assert info is not None
        assert info.library_path == "/libs/loaded/griptape_nodes_library.json"

    def test_resolver_falls_back_to_first_match_when_none_loaded(self, engine: Engine) -> None:
        """With no LOADED copy (e.g. discovery / worker-pending), the resolver keeps first-match."""
        library_manager = engine.library_manager

        entries = {
            "/libs/copyA/griptape_nodes_library.json": self._lib_info(
                library_manager,
                "/libs/copyA/griptape_nodes_library.json",
                "MyLib",
                lifecycle_state=_LibraryManager.LibraryLifecycleState.WORKER_PENDING,
            ),
            "/libs/copyB/griptape_nodes_library.json": self._lib_info(
                library_manager,
                "/libs/copyB/griptape_nodes_library.json",
                "MyLib",
                lifecycle_state=_LibraryManager.LibraryLifecycleState.DISCOVERED,
            ),
        }

        with patch.object(library_manager, "_library_file_path_to_info", entries):
            info = library_manager.get_library_info_by_library_name("MyLib")

        assert info is not None
        assert info.library_path == "/libs/copyA/griptape_nodes_library.json"
