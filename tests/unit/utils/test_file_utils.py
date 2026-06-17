"""Unit tests for file_utils module."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from griptape_nodes.utils.file_utils import (
    afind_files_recursive,
    atomic_write_bytes,
    find_all_files_in_directory,
    find_file_in_directory,
)

if TYPE_CHECKING:
    from collections.abc import Generator


class TestFindFileInDirectory:
    """Test find_file_in_directory function."""

    @pytest.fixture
    def temp_dir(self) -> Generator[Path, None, None]:
        """Create a temporary directory for testing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    def test_find_file_when_directory_does_not_exist(self) -> None:
        """Test that None is returned when directory doesn't exist."""
        non_existent = Path("/non/existent/directory")
        result = find_file_in_directory(non_existent, "*.json")

        assert result is None

    def test_find_file_when_path_is_not_directory(self, temp_dir: Path) -> None:
        """Test that None is returned when path is a file, not a directory."""
        test_file = temp_dir / "test.txt"
        test_file.write_text("content")

        result = find_file_in_directory(test_file, "*.json")

        assert result is None

    def test_find_file_when_no_files_match_pattern(self, temp_dir: Path) -> None:
        """Test that None is returned when no files match the pattern."""
        (temp_dir / "test.txt").write_text("content")
        (temp_dir / "another.py").write_text("content")

        result = find_file_in_directory(temp_dir, "*.json")

        assert result is None

    def test_find_file_returns_first_match_in_root_directory(self, temp_dir: Path) -> None:
        """Test that first matching file is returned from root directory."""
        expected_file = temp_dir / "config.json"
        expected_file.write_text("{}")

        result = find_file_in_directory(temp_dir, "config.json")

        assert result == expected_file

    def test_find_file_searches_subdirectories_recursively(self, temp_dir: Path) -> None:
        """Test that function searches subdirectories recursively."""
        subdir = temp_dir / "subdir" / "nested"
        subdir.mkdir(parents=True)
        expected_file = subdir / "config.json"
        expected_file.write_text("{}")

        result = find_file_in_directory(temp_dir, "config.json")

        assert result == expected_file

    def test_find_file_matches_glob_pattern(self, temp_dir: Path) -> None:
        """Test that function matches files using glob patterns."""
        expected_file = temp_dir / "my_library.json"
        expected_file.write_text("{}")
        (temp_dir / "other.txt").write_text("content")

        result = find_file_in_directory(temp_dir, "*library*.json")

        assert result == expected_file

    def test_find_file_logs_warning_when_multiple_matches_found(self, temp_dir: Path) -> None:
        """Test that warning is logged when multiple files match pattern."""
        file1 = temp_dir / "config1.json"
        file2 = temp_dir / "config2.json"
        file1.write_text("{}")
        file2.write_text("{}")

        with patch("griptape_nodes.utils.file_utils.logger") as mock_logger:
            result = find_file_in_directory(temp_dir, "*.json")

            assert result in [file1, file2]
            mock_logger.warning.assert_called_once()
            warning_call = mock_logger.warning.call_args[0][0]
            assert "multiple files" in warning_call.lower()

    def test_find_file_returns_first_match_when_multiple_found(self, temp_dir: Path) -> None:
        """Test that first match is returned when multiple files match."""
        file1 = temp_dir / "a_config.json"
        file2 = temp_dir / "b_config.json"
        file1.write_text("{}")
        file2.write_text("{}")

        result = find_file_in_directory(temp_dir, "*.json")

        # Should return one of them (first found by os.walk)
        assert result is not None
        assert result.exists()
        assert result.suffix == ".json"

    def test_find_file_matches_character_class_pattern_underscore(self, temp_dir: Path) -> None:
        """Test that character class pattern matches underscore version."""
        expected_file = temp_dir / "griptape_nodes_library.json"
        expected_file.write_text("{}")

        result = find_file_in_directory(temp_dir, "griptape[-_]nodes[-_]library.json")

        assert result == expected_file

    def test_find_file_matches_character_class_pattern_hyphen(self, temp_dir: Path) -> None:
        """Test that character class pattern matches hyphen version."""
        expected_file = temp_dir / "griptape-nodes-library.json"
        expected_file.write_text("{}")

        result = find_file_in_directory(temp_dir, "griptape[-_]nodes[-_]library.json")

        assert result == expected_file


class TestFindAllFilesInDirectory:
    """Test find_all_files_in_directory function."""

    @pytest.fixture
    def temp_dir(self) -> Generator[Path, None, None]:
        """Create a temporary directory for testing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    def test_find_all_files_when_directory_does_not_exist(self) -> None:
        """Test that empty list is returned when directory doesn't exist."""
        non_existent = Path("/non/existent/directory")
        result = find_all_files_in_directory(non_existent, "*.json")

        assert result == []

    def test_find_all_files_when_path_is_not_directory(self, temp_dir: Path) -> None:
        """Test that empty list is returned when path is a file, not a directory."""
        test_file = temp_dir / "test.txt"
        test_file.write_text("content")

        result = find_all_files_in_directory(test_file, "*.json")

        assert result == []

    def test_find_all_files_when_no_files_match_pattern(self, temp_dir: Path) -> None:
        """Test that empty list is returned when no files match the pattern."""
        (temp_dir / "test.txt").write_text("content")
        (temp_dir / "another.py").write_text("content")

        result = find_all_files_in_directory(temp_dir, "*.json")

        assert result == []

    def test_find_all_files_returns_single_match(self, temp_dir: Path) -> None:
        """Test that list with single file is returned when one file matches."""
        expected_file = temp_dir / "config.json"
        expected_file.write_text("{}")
        (temp_dir / "other.txt").write_text("content")

        result = find_all_files_in_directory(temp_dir, "*.json")

        assert len(result) == 1
        assert result[0] == expected_file

    def test_find_all_files_returns_multiple_matches(self, temp_dir: Path) -> None:
        """Test that all matching files are returned."""
        file1 = temp_dir / "config1.json"
        file2 = temp_dir / "config2.json"
        file3 = temp_dir / "data.json"
        file1.write_text("{}")
        file2.write_text("{}")
        file3.write_text("{}")
        (temp_dir / "other.txt").write_text("content")

        result = find_all_files_in_directory(temp_dir, "*.json")

        assert len(result) == 3  # noqa: PLR2004
        assert set(result) == {file1, file2, file3}

    def test_find_all_files_searches_subdirectories_recursively(self, temp_dir: Path) -> None:
        """Test that function searches subdirectories recursively."""
        subdir1 = temp_dir / "sub1"
        subdir2 = temp_dir / "sub1" / "sub2"
        subdir1.mkdir()
        subdir2.mkdir()

        file1 = temp_dir / "root.json"
        file2 = subdir1 / "sub1.json"
        file3 = subdir2 / "sub2.json"
        file1.write_text("{}")
        file2.write_text("{}")
        file3.write_text("{}")

        result = find_all_files_in_directory(temp_dir, "*.json")

        assert len(result) == 3  # noqa: PLR2004
        assert set(result) == {file1, file2, file3}

    def test_find_all_files_matches_glob_pattern(self, temp_dir: Path) -> None:
        """Test that function matches files using glob patterns."""
        file1 = temp_dir / "my_library.json"
        file2 = temp_dir / "your_library.json"
        file3 = temp_dir / "config.json"
        file1.write_text("{}")
        file2.write_text("{}")
        file3.write_text("{}")

        result = find_all_files_in_directory(temp_dir, "*library*.json")

        assert len(result) == 2  # noqa: PLR2004
        assert set(result) == {file1, file2}

    def test_find_all_files_with_complex_glob_pattern(self, temp_dir: Path) -> None:
        """Test that function handles complex glob patterns."""
        file1 = temp_dir / "griptape_nodes_library.json"
        file2 = temp_dir / "griptape-nodes-library.json"
        file3 = temp_dir / "other_library.json"
        file1.write_text("{}")
        file2.write_text("{}")
        file3.write_text("{}")

        # Pattern with character class matches both underscore and hyphen versions
        result = find_all_files_in_directory(temp_dir, "griptape[-_]nodes[-_]library.json")

        assert len(result) == 2  # noqa: PLR2004
        assert set(result) == {file1, file2}


class TestAfindFilesRecursive:
    """Test afind_files_recursive: the async, depth-bounded finder."""

    @pytest.fixture
    def temp_dir(self) -> Generator[Path, None, None]:
        """Create a temporary directory for testing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    @pytest.mark.asyncio
    async def test_when_directory_does_not_exist(self) -> None:
        """Empty list is returned when directory doesn't exist."""
        result = await afind_files_recursive(Path("/non/existent/directory"), "*.json")

        assert result == []

    @pytest.mark.asyncio
    async def test_when_path_is_not_directory(self, temp_dir: Path) -> None:
        """Empty list is returned when path is a file, not a directory."""
        test_file = temp_dir / "test.txt"
        test_file.write_text("content")

        result = await afind_files_recursive(test_file, "*.json")

        assert result == []

    @pytest.mark.asyncio
    async def test_when_no_files_match_pattern(self, temp_dir: Path) -> None:
        """Empty list is returned when no files match the pattern."""
        (temp_dir / "test.txt").write_text("content")
        (temp_dir / "another.py").write_text("content")

        result = await afind_files_recursive(temp_dir, "*.json")

        assert result == []

    @pytest.mark.asyncio
    async def test_returns_sorted_results(self, temp_dir: Path) -> None:
        """Results are returned in sorted order."""
        file3 = temp_dir / "zebra.json"
        file1 = temp_dir / "apple.json"
        file2 = temp_dir / "banana.json"
        file3.write_text("{}")
        file1.write_text("{}")
        file2.write_text("{}")

        result = await afind_files_recursive(temp_dir, "*.json")

        assert result == [file1, file2, file3]

    @pytest.mark.asyncio
    async def test_returns_sorted_results_with_subdirectories(self, temp_dir: Path) -> None:
        """Results from subdirectories are also sorted."""
        subdir_z = temp_dir / "z_dir"
        subdir_a = temp_dir / "a_dir"
        subdir_z.mkdir()
        subdir_a.mkdir()

        file1 = subdir_a / "config.json"
        file2 = subdir_z / "config.json"
        file3 = temp_dir / "root.json"
        file1.write_text("{}")
        file2.write_text("{}")
        file3.write_text("{}")

        result = await afind_files_recursive(temp_dir, "*.json")

        assert result == [file1, file3, file2]

    @pytest.mark.asyncio
    async def test_skips_hidden_directories_by_default(self, temp_dir: Path) -> None:
        """Hidden directories are skipped by default."""
        hidden_dir = temp_dir / ".hidden"
        hidden_dir.mkdir()
        hidden_file = hidden_dir / "config.json"
        visible_file = temp_dir / "visible.json"
        hidden_file.write_text("{}")
        visible_file.write_text("{}")

        result = await afind_files_recursive(temp_dir, "*.json")

        assert result == [visible_file]

    @pytest.mark.asyncio
    async def test_includes_hidden_directories_when_requested(self, temp_dir: Path) -> None:
        """Hidden directories are included when skip_hidden=False."""
        hidden_dir = temp_dir / ".hidden"
        hidden_dir.mkdir()
        hidden_file = hidden_dir / "config.json"
        visible_file = temp_dir / "visible.json"
        hidden_file.write_text("{}")
        visible_file.write_text("{}")

        result = await afind_files_recursive(temp_dir, "*.json", skip_hidden=False)

        assert set(result) == {hidden_file, visible_file}

    @pytest.mark.asyncio
    async def test_matches_glob_pattern(self, temp_dir: Path) -> None:
        """Files are matched using glob patterns."""
        file1 = temp_dir / "my_library.json"
        file2 = temp_dir / "your_library.json"
        (temp_dir / "config.json").write_text("{}")
        file1.write_text("{}")
        file2.write_text("{}")

        result = await afind_files_recursive(temp_dir, "*library*.json")

        assert result == sorted([file1, file2])

    @pytest.mark.asyncio
    async def test_max_depth_zero_scans_only_top_level(self, temp_dir: Path) -> None:
        """max_depth=0 returns only matches directly in the directory."""
        root_file = temp_dir / "root.json"
        nested_file = temp_dir / "sub" / "nested.json"
        nested_file.parent.mkdir()
        root_file.write_text("{}")
        nested_file.write_text("{}")

        result = await afind_files_recursive(temp_dir, "*.json", max_depth=0)

        assert result == [root_file]

    @pytest.mark.asyncio
    async def test_max_depth_excludes_files_below_cap(self, temp_dir: Path) -> None:
        """Files at the depth cap are included; deeper files are excluded."""
        at_cap = temp_dir / "a" / "at_cap.json"
        below_cap = temp_dir / "a" / "b" / "below_cap.json"
        below_cap.parent.mkdir(parents=True)
        at_cap.write_text("{}")
        below_cap.write_text("{}")

        result = await afind_files_recursive(temp_dir, "*.json", max_depth=1)

        assert result == [at_cap]

    @pytest.mark.asyncio
    async def test_max_files_limits_results(self, temp_dir: Path) -> None:
        """At most max_files matches are returned."""
        for name in ["a.json", "b.json", "c.json", "d.json"]:
            (temp_dir / name).write_text("{}")

        result = await afind_files_recursive(temp_dir, "*.json", max_files=2)

        assert len(result) == 2  # noqa: PLR2004


class TestReadMaxSearchDepth:
    """Test _read_max_search_depth: env-var parsing for the discovery depth cap."""

    def test_default_when_unset(self) -> None:
        """Falls back to the default when the env var is absent."""
        from griptape_nodes.utils.file_utils import _DEFAULT_MAX_SEARCH_DEPTH_FALLBACK, _read_max_search_depth

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GTN_DISCOVERY_MAX_DEPTH", None)
            assert _read_max_search_depth() == _DEFAULT_MAX_SEARCH_DEPTH_FALLBACK

    def test_valid_value_is_used(self) -> None:
        """A valid integer is parsed and returned."""
        from griptape_nodes.utils.file_utils import _read_max_search_depth

        with patch.dict(os.environ, {"GTN_DISCOVERY_MAX_DEPTH": "12"}):
            assert _read_max_search_depth() == 12  # noqa: PLR2004

    def test_invalid_value_falls_back_to_default(self) -> None:
        """A non-integer value falls back to the default instead of crashing."""
        from griptape_nodes.utils.file_utils import _DEFAULT_MAX_SEARCH_DEPTH_FALLBACK, _read_max_search_depth

        with patch.dict(os.environ, {"GTN_DISCOVERY_MAX_DEPTH": "oops"}):
            assert _read_max_search_depth() == _DEFAULT_MAX_SEARCH_DEPTH_FALLBACK

    def test_negative_value_is_clamped_to_zero(self) -> None:
        """A negative value is clamped to 0 (top-level only)."""
        from griptape_nodes.utils.file_utils import _read_max_search_depth

        with patch.dict(os.environ, {"GTN_DISCOVERY_MAX_DEPTH": "-3"}):
            assert _read_max_search_depth() == 0


class TestAtomicWriteBytes:
    """Test atomic_write_bytes function."""

    @pytest.fixture
    def temp_dir(self) -> Generator[Path, None, None]:
        """Create a temporary directory for testing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    def test_writes_new_file(self, temp_dir: Path) -> None:
        """A new file is created with the given bytes."""
        target = temp_dir / "data.bin"
        atomic_write_bytes(target, b"hello")
        assert target.read_bytes() == b"hello"

    def test_overwrites_existing_file(self, temp_dir: Path) -> None:
        """An existing file is replaced with the new contents."""
        target = temp_dir / "data.bin"
        target.write_bytes(b"old")
        atomic_write_bytes(target, b"new")
        assert target.read_bytes() == b"new"

    def test_leaves_no_temp_files_behind(self, temp_dir: Path) -> None:
        """The temp file is renamed into place, not left in the directory."""
        target = temp_dir / "data.bin"
        atomic_write_bytes(target, b"payload")
        assert [p.name for p in temp_dir.iterdir()] == ["data.bin"]

    def test_failed_rename_removes_temp_and_preserves_original(self, temp_dir: Path) -> None:
        """A rename failure cleans up the temp file and leaves the original intact."""
        target = temp_dir / "data.bin"
        target.write_bytes(b"original")
        with (
            patch("griptape_nodes.utils.file_utils.Path.replace", side_effect=OSError("boom")),
            pytest.raises(OSError, match="boom"),
        ):
            atomic_write_bytes(target, b"new")
        assert target.read_bytes() == b"original"
        # No stray temp file survives the failure.
        assert sorted(p.name for p in temp_dir.iterdir()) == ["data.bin"]
