"""Unit tests for library_utils module."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from griptape_nodes.retained_mode.managers.settings import LibraryDownload, LibraryRegistration
from griptape_nodes.utils.git_utils import GitCloneError
from griptape_nodes.utils.library_utils import (
    clone_and_get_library_version,
    extract_library_path,
    filter_old_xdg_library_paths,
    is_monorepo,
    normalize_library_downloads,
)

if TYPE_CHECKING:
    from collections.abc import Generator


class TestIsMonorepo:
    """Test is_monorepo function."""

    @pytest.fixture
    def temp_dir(self) -> Generator[Path, None, None]:
        """Create a temporary directory for testing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    def test_is_monorepo_returns_false_when_not_git_repository(self, temp_dir: Path) -> None:
        """Test that False is returned when path is not a git repository."""
        with patch("griptape_nodes.utils.library_utils.get_git_repository_root") as mock_get_root:
            mock_get_root.return_value = None

            result = is_monorepo(temp_dir)

            assert result is False

    def test_is_monorepo_returns_false_when_only_one_library_json_found(self, temp_dir: Path) -> None:
        """Test that False is returned when only one library JSON file exists."""
        with (
            patch("griptape_nodes.utils.library_utils.get_git_repository_root") as mock_get_root,
            patch("griptape_nodes.utils.library_utils.find_all_files_in_directory") as mock_find,
        ):
            mock_get_root.return_value = temp_dir
            mock_find.return_value = [temp_dir / "griptape_nodes_library.json"]

            result = is_monorepo(temp_dir)

            assert result is False

    def test_is_monorepo_returns_false_when_no_library_json_found(self, temp_dir: Path) -> None:
        """Test that False is returned when no library JSON files exist."""
        with (
            patch("griptape_nodes.utils.library_utils.get_git_repository_root") as mock_get_root,
            patch("griptape_nodes.utils.library_utils.find_all_files_in_directory") as mock_find,
        ):
            mock_get_root.return_value = temp_dir
            mock_find.return_value = []

            result = is_monorepo(temp_dir)

            assert result is False

    def test_is_monorepo_returns_true_when_multiple_library_json_found(self, temp_dir: Path) -> None:
        """Test that True is returned when multiple library JSON files exist."""
        with (
            patch("griptape_nodes.utils.library_utils.get_git_repository_root") as mock_get_root,
            patch("griptape_nodes.utils.library_utils.find_all_files_in_directory") as mock_find,
        ):
            mock_get_root.return_value = temp_dir
            mock_find.return_value = [
                temp_dir / "lib1" / "griptape_nodes_library.json",
                temp_dir / "lib2" / "griptape-nodes-library.json",
            ]

            result = is_monorepo(temp_dir)

            assert result is True

    def test_is_monorepo_searches_with_correct_pattern(self, temp_dir: Path) -> None:
        """Test that correct glob pattern is used for searching."""
        with (
            patch("griptape_nodes.utils.library_utils.get_git_repository_root") as mock_get_root,
            patch("griptape_nodes.utils.library_utils.find_all_files_in_directory") as mock_find,
        ):
            mock_get_root.return_value = temp_dir
            mock_find.return_value = []

            is_monorepo(temp_dir)

            mock_find.assert_called_once_with(temp_dir, "griptape[-_]nodes[-_]library.json")


class TestCloneAndGetLibraryVersion:
    """Test clone_and_get_library_version function."""

    def test_clone_and_get_library_version_calls_sparse_checkout(self) -> None:
        """Test that clone_and_get_library_version delegates to sparse_checkout_library_json."""
        with patch("griptape_nodes.utils.library_utils.sparse_checkout_library_json") as mock_sparse:
            mock_sparse.return_value = ("1.0.0", "abc123def456", {"metadata": {"engine_version": "0.70.0"}})

            result = clone_and_get_library_version("https://github.com/user/repo.git")

            mock_sparse.assert_called_once_with("https://github.com/user/repo.git", ref="HEAD")
            assert result == ("1.0.0", "abc123def456", "0.70.0")

    def test_clone_and_get_library_version_returns_version_commit_and_engine_version(self) -> None:
        """Test that clone_and_get_library_version returns version, commit, and engine_version."""
        with patch("griptape_nodes.utils.library_utils.sparse_checkout_library_json") as mock_sparse:
            mock_sparse.return_value = ("2.5.1", "def789ghi012", {"metadata": {"engine_version": "0.71.0"}})

            result = clone_and_get_library_version("https://github.com/user/repo.git")

            # Verify returns tuple of (version, commit, engine_version)
            assert result == ("2.5.1", "def789ghi012", "0.71.0")
            assert isinstance(result, tuple)
            version, commit, engine_version = result
            assert version == "2.5.1"
            assert commit == "def789ghi012"
            assert engine_version == "0.71.0"

    def test_clone_and_get_library_version_propagates_errors(self) -> None:
        """Test that errors from sparse_checkout_library_json are propagated."""
        with patch("griptape_nodes.utils.library_utils.sparse_checkout_library_json") as mock_sparse:
            mock_sparse.side_effect = GitCloneError("sparse checkout failed")

            with pytest.raises(GitCloneError) as exc_info:
                clone_and_get_library_version("https://github.com/user/repo.git")

            assert "sparse checkout failed" in str(exc_info.value)


class TestFilterOldXdgLibraryPaths:
    """Test filter_old_xdg_library_paths function."""

    def test_filter_returns_tuple(self) -> None:
        """Test that filter returns tuple with filtered paths and removed library names."""
        with patch("griptape_nodes.utils.library_utils.xdg_data_home") as mock_xdg:
            mock_xdg.return_value = Path("/home/user/.local/share")

            paths = [
                "/home/user/.local/share/griptape_nodes/libraries/griptape_nodes_library",
                "/home/user/.local/share/griptape_nodes/libraries/griptape_cloud",
                "/custom/path",
            ]

            filtered, removed = filter_old_xdg_library_paths(paths)

            assert isinstance((filtered, removed), tuple)
            assert len((filtered, removed)) == 2  # noqa: PLR2004
            assert filtered == ["/custom/path"]
            assert removed == {"griptape_nodes_library", "griptape_cloud"}

    def test_filter_no_removals(self) -> None:
        """Test filter when no old paths are present."""
        with patch("griptape_nodes.utils.library_utils.xdg_data_home") as mock_xdg:
            mock_xdg.return_value = Path("/home/user/.local/share")

            paths = ["/custom/path", "https://github.com/user/lib@main"]

            filtered, removed = filter_old_xdg_library_paths(paths)

            assert filtered == paths
            assert removed == set()

    def test_filter_empty_list(self) -> None:
        """Test filter with empty list."""
        filtered, removed = filter_old_xdg_library_paths([])

        assert filtered == []
        assert removed == set()

    def test_filter_removes_all_three_library_types(self) -> None:
        """Test that all three old library types are removed."""
        with patch("griptape_nodes.utils.library_utils.xdg_data_home") as mock_xdg:
            mock_xdg.return_value = Path("/home/user/.local/share")

            xdg_base = "/home/user/.local/share/griptape_nodes/libraries"
            paths = [
                f"{xdg_base}/griptape_nodes_library/lib.json",
                f"{xdg_base}/griptape_nodes_advanced_media_library/lib.json",
                f"{xdg_base}/griptape_cloud/lib.json",
                "/custom/library",
            ]

            filtered, removed = filter_old_xdg_library_paths(paths)

            assert filtered == ["/custom/library"]
            assert removed == {
                "griptape_nodes_library",
                "griptape_nodes_advanced_media_library",
                "griptape_cloud",
            }

    def test_filter_preserves_custom_paths_and_git_urls(self) -> None:
        """Test that custom paths and git URLs are preserved."""
        with patch("griptape_nodes.utils.library_utils.xdg_data_home") as mock_xdg:
            mock_xdg.return_value = Path("/home/user/.local/share")

            xdg_base = "/home/user/.local/share/griptape_nodes/libraries"
            old_path = f"{xdg_base}/griptape_nodes_library"
            custom_path = "/opt/custom/libraries/my_library"
            git_url = "https://github.com/user/awesome-library@stable"

            paths = [old_path, custom_path, git_url]

            filtered, removed = filter_old_xdg_library_paths(paths)

            assert filtered == [custom_path, git_url]
            assert removed == {"griptape_nodes_library"}

    def test_filter_handles_object_form_dict_entries(self) -> None:
        """Object-form dict entries are filtered by their `path` and preserved otherwise."""
        with patch("griptape_nodes.utils.library_utils.xdg_data_home") as mock_xdg:
            mock_xdg.return_value = Path("/home/user/.local/share")

            xdg_base = "/home/user/.local/share/griptape_nodes/libraries"
            old_entry = {"path": f"{xdg_base}/griptape_cloud/lib.json", "enabled": True}
            custom_entry = {"path": "/custom/path/lib.json", "enabled": False}

            paths = [old_entry, custom_entry]

            filtered, removed = filter_old_xdg_library_paths(paths)

            assert filtered == [custom_entry]
            assert removed == {"griptape_cloud"}

    def test_filter_handles_library_registration_entries(self) -> None:
        """Already parsed LibraryRegistration entries are filtered by their `path`."""
        with patch("griptape_nodes.utils.library_utils.xdg_data_home") as mock_xdg:
            mock_xdg.return_value = Path("/home/user/.local/share")

            xdg_base = "/home/user/.local/share/griptape_nodes/libraries"
            old_entry = LibraryRegistration(path=f"{xdg_base}/griptape_nodes_library/lib.json")
            custom_entry = LibraryRegistration(path="/custom/path/lib.json")

            paths = [old_entry, custom_entry]

            filtered, removed = filter_old_xdg_library_paths(paths)

            assert filtered == [custom_entry]
            assert removed == {"griptape_nodes_library"}

    def test_filter_mixed_string_and_object_entries(self) -> None:
        """A mix of bare strings and object-form entries is handled without error."""
        with patch("griptape_nodes.utils.library_utils.xdg_data_home") as mock_xdg:
            mock_xdg.return_value = Path("/home/user/.local/share")

            xdg_base = "/home/user/.local/share/griptape_nodes/libraries"
            old_string = f"{xdg_base}/griptape_nodes_advanced_media_library/lib.json"
            old_dict = {"path": f"{xdg_base}/griptape_cloud/lib.json"}
            custom_string = "/custom/path"

            paths = [old_string, old_dict, custom_string]

            filtered, removed = filter_old_xdg_library_paths(paths)

            assert filtered == [custom_string]
            assert removed == {"griptape_nodes_advanced_media_library", "griptape_cloud"}


class TestNormalizeLibraryDownloads:
    """`normalize_library_downloads` turns a raw config list into LibraryDownload entries."""

    def test_bare_string_becomes_git_url_only_entry(self) -> None:
        entries = normalize_library_downloads(["griptape-ai/git-lib@v2.0"])

        assert len(entries) == 1
        assert entries[0] == LibraryDownload(git_url="griptape-ai/git-lib@v2.0")
        assert entries[0].version is None
        assert entries[0].name is None

    def test_object_form_is_validated(self) -> None:
        entries = normalize_library_downloads(
            [{"git_url": "griptape-ai/git-lib@v2.0", "version": ">=2.0", "name": "git-lib"}]
        )

        assert entries == [LibraryDownload(git_url="griptape-ai/git-lib@v2.0", version=">=2.0", name="git-lib")]

    def test_already_parsed_instance_passes_through(self) -> None:
        download = LibraryDownload(git_url="griptape-ai/git-lib@v2.0")

        entries = normalize_library_downloads([download])

        assert entries == [download]

    def test_empty_string_is_skipped(self) -> None:
        assert normalize_library_downloads([""]) == []

    def test_malformed_object_is_skipped_with_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        # Missing the required git_url -> ValidationError -> skipped, not raised.
        with caplog.at_level("WARNING"):
            entries = normalize_library_downloads([{"version": ">=1.0"}])

        assert entries == []
        assert any("libraries_to_download" in message for message in caplog.messages)

    def test_unexpected_type_is_skipped_with_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level("WARNING"):
            entries = normalize_library_downloads([123])

        assert entries == []
        assert any("libraries_to_download" in message for message in caplog.messages)


class TestExtractLibraryPath:
    """Test extract_library_path function."""

    def test_bare_string_returns_itself(self) -> None:
        assert extract_library_path("lib.json") == "lib.json"

    def test_dict_entry_returns_path(self) -> None:
        assert extract_library_path({"path": "lib.json", "enabled": True}) == "lib.json"

    def test_library_registration_returns_path(self) -> None:
        registration = LibraryRegistration(path="lib.json")

        assert extract_library_path(registration) == "lib.json"

    def test_dict_without_path_returns_empty_string(self) -> None:
        assert extract_library_path({"enabled": True}) == ""

    def test_dict_with_non_string_path_returns_empty_string(self) -> None:
        assert extract_library_path({"path": 123}) == ""

    def test_unexpected_type_returns_empty_string(self) -> None:
        assert extract_library_path(123) == ""

    def test_mixed_list_joins_without_error(self) -> None:
        # Regression: the init status line previously joined libraries_to_register
        # raw and crashed on object-form (dict) entries with a TypeError.
        entries = ["lib_a.json", {"path": "lib_b.json", "enabled": True}, "lib_c.json"]

        register_paths = [path for entry in entries if (path := extract_library_path(entry))]

        assert ", ".join(register_paths) == "lib_a.json, lib_b.json, lib_c.json"
