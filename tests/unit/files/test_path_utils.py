"""Unit tests for path_utils utilities."""

import os
import platform
import sys
from pathlib import Path

import pytest

from griptape_nodes.files.path_utils import (
    FilenameParts,
    canonicalize_for_identity,
    canonicalize_for_io,
    canonicalize_to_posix,
    decompose_source_path,
    expand_path,
    normalize_path_for_platform,
    parse_file_uri,
    path_needs_expansion,
    resolve_file_path,
    resolve_path_safely,
    sanitize_path_string,
    strip_surrounding_quotes,
)


class TestFilenameParts:
    """Tests for FilenameParts.from_filename classmethod."""

    def test_splits_simple_filename(self) -> None:
        """Standard filename splits into stem and extension."""
        parts = FilenameParts.from_filename("output.png")
        assert parts.stem == "output"
        assert parts.extension == "png"

    def test_extension_has_no_leading_dot(self) -> None:
        """Extension does not include the leading dot."""
        parts = FilenameParts.from_filename("file.txt")
        assert parts.extension == "txt"
        assert not parts.extension.startswith(".")

    def test_splits_compound_extension(self) -> None:
        """Only the last suffix is treated as the extension."""
        parts = FilenameParts.from_filename("archive.tar.gz")
        assert parts.stem == "archive.tar"
        assert parts.extension == "gz"

    def test_filename_with_no_extension(self) -> None:
        """Filename without an extension has an empty extension."""
        parts = FilenameParts.from_filename("Makefile")
        assert parts.stem == "Makefile"
        assert parts.extension == ""

    def test_captures_directory_component(self) -> None:
        """Directory portion of a path is captured in the directory field."""
        parts = FilenameParts.from_filename("/some/dir/output.jpg")
        assert parts.directory == Path("/some/dir")
        assert parts.stem == "output"
        assert parts.extension == "jpg"

    def test_directory_is_dot_when_no_path(self) -> None:
        """Directory is Path('.') when the input has no directory component."""
        parts = FilenameParts.from_filename("output.png")
        assert parts.directory == Path()


class TestSanitizePathString:
    """Tests for sanitize_path_string function."""

    def test_removes_shell_escapes_from_macos_finder_path(self) -> None:
        """Test removal of shell escape characters from macOS Finder paths."""
        input_path = "/Downloads/Dragon\\'s\\ Curse/screenshot.jpg"
        expected = "/Downloads/Dragon's Curse/screenshot.jpg"
        assert sanitize_path_string(input_path) == expected

    def test_removes_shell_escapes_from_complex_path(self) -> None:
        """Test removal of shell escapes from complex paths with multiple special chars."""
        input_path = "/Test\\ Images/Level\\ 1\\ -\\ Knight\\'s\\ Quest/file.png"
        expected = "/Test Images/Level 1 - Knight's Quest/file.png"
        assert sanitize_path_string(input_path) == expected

    def test_removes_surrounding_double_quotes(self) -> None:
        """Test removal of surrounding double quotes."""
        input_path = '"/path/with spaces/file.txt"'
        expected = "/path/with spaces/file.txt"
        assert sanitize_path_string(input_path) == expected

    def test_removes_surrounding_single_quotes(self) -> None:
        """Test removal of surrounding single quotes."""
        input_path = "'/path/with spaces/file.txt'"
        expected = "/path/with spaces/file.txt"
        assert sanitize_path_string(input_path) == expected

    def test_removes_newlines_and_carriage_returns(self) -> None:
        """Test removal of newlines and carriage returns from paths."""
        input_path = "C:\\Users\\file\n\n.txt"
        expected = "C:\\Users\\file.txt"
        assert sanitize_path_string(input_path) == expected

    def test_preserves_windows_backslashes(self) -> None:
        """Test that Windows path backslashes are preserved."""
        input_path = "C:\\Users\\Documents\\file.txt"
        expected = "C:\\Users\\Documents\\file.txt"
        assert sanitize_path_string(input_path) == expected

    def test_preserves_windows_extended_length_prefix(self) -> None:
        """Test that Windows extended-length path prefix is preserved."""
        input_path = r"\\?\C:\Very\ Long\ Path\file.txt"
        expected = r"\\?\C:\Very Long Path\file.txt"
        assert sanitize_path_string(input_path) == expected

    def test_handles_path_objects(self) -> None:
        """Test conversion of Path objects to strings."""
        input_path = Path("/path/to/file")
        result = sanitize_path_string(input_path)
        # Verify exact conversion using as_posix() for cross-platform comparison
        assert result == input_path.as_posix()

    def test_strips_leading_trailing_whitespace(self) -> None:
        """Test removal of leading and trailing whitespace."""
        input_path = "  /path/to/file.txt  "
        expected = "/path/to/file.txt"
        assert sanitize_path_string(input_path) == expected


class TestStripSurroundingQuotes:
    """Tests for strip_surrounding_quotes function."""

    def test_removes_double_quotes(self) -> None:
        """Test removal of surrounding double quotes."""
        assert strip_surrounding_quotes('"test"') == "test"

    def test_removes_single_quotes(self) -> None:
        """Test removal of surrounding single quotes."""
        assert strip_surrounding_quotes("'test'") == "test"

    def test_preserves_internal_quotes(self) -> None:
        """Test that internal quotes are preserved."""
        assert strip_surrounding_quotes('test"with"quotes') == 'test"with"quotes'

    def test_preserves_unmatched_quotes(self) -> None:
        """Test that unmatched quotes are preserved."""
        assert strip_surrounding_quotes('"test') == '"test'
        assert strip_surrounding_quotes("test'") == "test'"


class TestExpandPath:
    """Tests for expand_path function."""

    def test_expands_tilde(self) -> None:
        """Test expansion of tilde to user home directory."""
        result = expand_path("~/Documents")
        assert str(result).startswith(str(Path.home()))
        assert str(result).endswith("Documents")

    def test_expands_environment_variables(self) -> None:
        """Test expansion of environment variables."""
        # Set a test environment variable
        os.environ["TEST_VAR"] = "/test/path"
        result = expand_path("$TEST_VAR/file.txt")
        # Use as_posix() to get forward slashes on all platforms for comparison
        assert result.as_posix() == "/test/path/file.txt"

    def test_returns_path_object(self) -> None:
        """Test that function returns a Path object."""
        result = expand_path("~/test")
        assert isinstance(result, Path)


class TestPathNeedsExpansion:
    """Tests for path_needs_expansion function."""

    def test_detects_tilde(self) -> None:
        """Test detection of paths starting with tilde."""
        assert path_needs_expansion("~/Documents") is True

    def test_detects_unix_env_vars(self) -> None:
        """Test detection of Unix-style environment variables."""
        assert path_needs_expansion("$HOME/file.txt") is True

    def test_detects_windows_env_vars(self) -> None:
        """Test detection of Windows-style environment variables."""
        assert path_needs_expansion("%USERPROFILE%/file.txt") is True

    def test_detects_absolute_paths(self) -> None:
        """Test detection of absolute paths."""
        # Use a platform-appropriate absolute path
        if sys.platform.startswith("win"):
            test_path = "C:\\absolute\\path"
        else:
            test_path = "/absolute/path"
        assert path_needs_expansion(test_path) is True

    def test_relative_path_no_expansion(self) -> None:
        """Test that relative paths without special chars don't need expansion."""
        assert path_needs_expansion("relative/path") is False


class TestResolvePathSafely:
    """Tests for resolve_path_safely function."""

    def test_converts_relative_to_absolute(self) -> None:
        """Test conversion of relative paths to absolute."""
        result = resolve_path_safely(Path("relative/file.txt"))
        assert result.is_absolute()

    def test_preserves_absolute_paths(self, tmp_path: Path) -> None:
        """Test that absolute paths are preserved."""
        # Use a real absolute path that works on all platforms
        test_path = tmp_path / "file.txt"
        result = resolve_path_safely(test_path)
        assert result.is_absolute()
        # Verify the paths are the same using normalized comparison
        assert result.as_posix() == test_path.as_posix()

    def test_normalizes_dot_segments(self, tmp_path: Path) -> None:
        """Test removal of . and .. segments."""
        # Use a real absolute path with .. segments
        test_path = tmp_path / "subdir" / ".." / "file.txt"
        expected = tmp_path / "file.txt"
        result = resolve_path_safely(test_path)
        # Verify the .. was normalized by comparing with expected path
        assert result.as_posix() == expected.as_posix()

    def test_works_with_nonexistent_paths(self) -> None:
        """Test that function works with non-existent paths."""
        result = resolve_path_safely(Path("/nonexistent/path/file.txt"))
        assert result.is_absolute()


class TestNormalizePathForPlatform:
    """Tests for normalize_path_for_platform function."""

    def test_returns_string(self) -> None:
        """Test that function returns a string."""
        test_path = Path("/test/path")
        result = normalize_path_for_platform(test_path)
        assert isinstance(result, str)

    @pytest.mark.skipif(not sys.platform.startswith("win"), reason="Windows-specific test")
    def test_adds_long_path_prefix_on_windows(self, tmp_path: Path) -> None:
        r"""Test that long paths get \\?\ prefix on Windows."""
        # Windows MAX_PATH limit
        windows_max_path = 260

        # Create a path longer than MAX_PATH characters
        long_subpath = "a" * 250
        long_path = tmp_path / long_subpath / "file.txt"
        long_path.parent.mkdir(parents=True, exist_ok=True)
        long_path.write_text("test")

        result = normalize_path_for_platform(long_path)
        if len(str(long_path.resolve())) >= windows_max_path:
            assert result.startswith("\\\\?\\")

    def test_sanitizes_path_string(self, tmp_path: Path) -> None:
        """Test that path is sanitized during normalization."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("content")
        result = normalize_path_for_platform(test_file)
        # Check for actual newline and carriage return characters, not the string sequences
        assert "\n" not in result
        assert "\r" not in result


class TestResolveFilePath:
    """Tests for resolve_file_path function."""

    def test_expands_absolute_paths(self, tmp_path: Path) -> None:
        """Test expansion of absolute paths."""
        result = resolve_file_path("/absolute/path", tmp_path)
        assert result.is_absolute()

    def test_expands_tilde_paths(self, tmp_path: Path) -> None:
        """Test expansion of tilde paths."""
        result = resolve_file_path("~/Documents", tmp_path)
        assert result.is_absolute()
        assert str(result).startswith(str(Path.home()))

    def test_resolves_relative_paths_against_base_dir(self, tmp_path: Path) -> None:
        """Test resolution of relative paths against base directory."""
        result = resolve_file_path("relative/file.txt", tmp_path)
        assert result.is_absolute()
        assert str(result).startswith(str(tmp_path))

    def test_anchors_url_encoded_filename_to_base_dir(self, tmp_path: Path) -> None:
        """URL-encoded filenames trip path_needs_expansion via '%' but contain no env var.

        So expand_path returns the original relative string. The result must still be
        anchored to base_dir instead of being returned as a relative path.
        """
        filename = "As%20Fast%20As%20Can%20Be-thumbnail-2026-01-14.png"

        result = resolve_file_path(filename, tmp_path)

        assert result.is_absolute()
        assert result == tmp_path / filename

    def test_anchors_dollar_sign_filename_to_base_dir(self, tmp_path: Path) -> None:
        """Filenames containing '$' with no matching env var must still be joined to base_dir."""
        filename = "price$5.png"

        result = resolve_file_path(filename, tmp_path)

        assert result.is_absolute()
        assert result == tmp_path / filename

    def test_expands_env_var_path_outside_base_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A path whose env var expands to an absolute location must keep that absolute location.

        It must NOT be re-anchored to base_dir.
        """
        target = tmp_path / "external"
        target.mkdir()
        monkeypatch.setenv("RESOLVE_FILE_PATH_TEST_DIR", str(target))

        result = resolve_file_path("$RESOLVE_FILE_PATH_TEST_DIR/file.txt", Path("/unused/base"))

        assert result == target / "file.txt"


class TestParseFileUri:
    """Tests for parse_file_uri function."""

    def test_parse_unix_absolute_path(self) -> None:
        """Test parsing Unix absolute path file URI."""
        uri = "file:///path/to/file.txt"
        result = parse_file_uri(uri)
        assert result == "/path/to/file.txt"

    def test_parse_localhost_uri(self) -> None:
        """Test parsing file URI with localhost."""
        uri = "file://localhost/path/to/file.txt"
        result = parse_file_uri(uri)
        assert result == "/path/to/file.txt"

    def test_parse_localhost_case_insensitive(self) -> None:
        """Test that localhost is case-insensitive."""
        uri = "file://LOCALHOST/path/to/file.txt"
        result = parse_file_uri(uri)
        assert result == "/path/to/file.txt"

    @pytest.mark.skipif(platform.system() != "Windows", reason="Windows-specific test")
    def test_parse_windows_absolute_path(self) -> None:
        """Test parsing Windows absolute path file URI."""
        uri = "file:///C:/Users/test/file.txt"
        result = parse_file_uri(uri)
        assert result == "C:/Users/test/file.txt"

    @pytest.mark.skipif(platform.system() != "Windows", reason="Windows-specific test")
    def test_parse_windows_path_with_localhost(self) -> None:
        """Test parsing Windows path with localhost."""
        uri = "file://localhost/C:/Users/test/file.txt"
        result = parse_file_uri(uri)
        assert result == "C:/Users/test/file.txt"

    def test_parse_with_percent_encoding(self) -> None:
        """Test parsing file URI with percent-encoded characters."""
        uri = "file:///path/to/file%20with%20spaces.txt"
        result = parse_file_uri(uri)
        assert result == "/path/to/file with spaces.txt"

    def test_parse_with_special_chars(self) -> None:
        """Test parsing file URI with special characters."""
        uri = "file:///path/to/file%21%40%23.txt"
        result = parse_file_uri(uri)
        assert result == "/path/to/file!@#.txt"

    def test_rejects_remote_host(self) -> None:
        """Test that file URIs with non-localhost hosts are rejected."""
        uri = "file://remote-server/path/to/file.txt"
        result = parse_file_uri(uri)
        assert result is None

    def test_rejects_non_file_scheme(self) -> None:
        """Test that non-file:// URIs are rejected."""
        uri = "http://example.com/file.txt"
        result = parse_file_uri(uri)
        assert result is None

    def test_rejects_https_scheme(self) -> None:
        """Test that https:// URIs are rejected."""
        uri = "https://example.com/file.txt"
        result = parse_file_uri(uri)
        assert result is None

    def test_returns_none_for_regular_path(self) -> None:
        """Test that regular paths (not file:// URIs) return None."""
        result = parse_file_uri("/regular/path/file.txt")
        assert result is None

    def test_returns_none_for_relative_path(self) -> None:
        """Test that relative paths return None."""
        result = parse_file_uri("relative/path/file.txt")
        assert result is None

    def test_returns_none_for_empty_string(self) -> None:
        """Test that empty string returns None."""
        result = parse_file_uri("")
        assert result is None

    def test_parse_uri_with_nested_directories(self) -> None:
        """Test parsing file URI with nested directories."""
        uri = "file:///very/deeply/nested/directory/structure/file.txt"
        result = parse_file_uri(uri)
        assert result == "/very/deeply/nested/directory/structure/file.txt"


class TestDecomposeSourcePath:
    """Test decompose_source_path() for sidecar/preview path generation.

    These tests verify the path decomposition logic outlined in the preview path
    generation plan, covering all 15 scenarios including workspace files, external
    files, Windows drives, macOS volumes, Linux mounts, and UNC paths.
    """

    def test_workspace_file_root_level(self) -> None:
        """Test workspace file at root level (no subdirectories)."""
        workspace = Path("/Users/james/workspace")
        source = Path("/Users/james/workspace/photo.png")

        result = decompose_source_path(source, workspace)

        assert result.drive_volume_mount is None
        assert result.source_relative_path is None
        assert result.source_file_name == "photo.png"

    def test_workspace_file_single_subdir(self) -> None:
        """Test workspace file in single subdirectory (Scenario 1)."""
        workspace = Path("/Users/james/workspace")
        source = Path("/Users/james/workspace/images/photo.png")

        result = decompose_source_path(source, workspace)

        assert result.drive_volume_mount is None
        assert result.source_relative_path == "images"
        assert result.source_file_name == "photo.png"

    def test_workspace_file_nested_subdirs(self) -> None:
        """Test workspace file in nested subdirectories (Scenario 2)."""
        workspace = Path("/Users/james/workspace")
        source = Path("/Users/james/workspace/images/subdir/photo.png")

        result = decompose_source_path(source, workspace)

        assert result.drive_volume_mount is None
        assert result.source_relative_path == "images/subdir"
        assert result.source_file_name == "photo.png"

    def test_workspace_file_outputs_dir(self) -> None:
        """Test workspace file in outputs directory (Scenario 3)."""
        workspace = Path("/Users/james/workspace")
        source = Path("/Users/james/workspace/outputs/render.png")

        result = decompose_source_path(source, workspace)

        assert result.drive_volume_mount is None
        assert result.source_relative_path == "outputs"
        assert result.source_file_name == "render.png"

    def test_unix_absolute_path_single_subdir(self) -> None:
        """Test Unix absolute path with single subdirectory (Scenario 4)."""
        workspace = Path("/Users/james/workspace")
        source = Path("/tmp/external.png")  # noqa: S108

        result = decompose_source_path(source, workspace)

        assert result.drive_volume_mount is None
        assert result.source_relative_path == "tmp"
        assert result.source_file_name == "external.png"

    def test_unix_absolute_path_nested_subdirs(self) -> None:
        """Test Unix absolute path with nested subdirectories (Scenario 5)."""
        workspace = Path("/Users/james/workspace")
        source = Path("/tmp/project/images/photo.png")  # noqa: S108

        result = decompose_source_path(source, workspace)

        assert result.drive_volume_mount is None
        assert result.source_relative_path == "tmp/project/images"
        assert result.source_file_name == "photo.png"

    def test_unix_root_level_file(self) -> None:
        """Test Unix root-level file (Scenario 6)."""
        workspace = Path("/Users/james/workspace")
        source = Path("/external.png")

        result = decompose_source_path(source, workspace)

        assert result.drive_volume_mount is None
        assert result.source_relative_path is None
        assert result.source_file_name == "external.png"

    def test_windows_drive_c(self) -> None:
        """Test Windows C: drive path (Scenario 11)."""
        workspace = Path("/Users/james/workspace")
        # Simulate Windows path
        source = Path("C:/temp/external.png")

        result = decompose_source_path(source, workspace)

        assert result.drive_volume_mount == "C"
        assert result.source_relative_path == "temp"
        assert result.source_file_name == "external.png"

    def test_windows_drive_q(self) -> None:
        """Test Windows Q: drive path (Scenario 12)."""
        workspace = Path("/Users/james/workspace")
        source = Path("Q:/temp/external.png")

        result = decompose_source_path(source, workspace)

        assert result.drive_volume_mount == "Q"
        assert result.source_relative_path == "temp"
        assert result.source_file_name == "external.png"

    def test_windows_drive_case_insensitive(self) -> None:
        """Test Windows drive letter is case-insensitive."""
        workspace = Path("/Users/james/workspace")
        source_lower = Path("c:/temp/file.txt")
        source_upper = Path("C:/temp/file.txt")

        result_lower = decompose_source_path(source_lower, workspace)
        result_upper = decompose_source_path(source_upper, workspace)

        # Both should normalize to uppercase
        assert result_lower.drive_volume_mount == "C"
        assert result_upper.drive_volume_mount == "C"

    def test_macos_volume_basic(self) -> None:
        """Test macOS external volume (Scenario 13)."""
        workspace = Path("/Users/james/workspace")
        source = Path("/Volumes/Backup/files/photo.png")

        result = decompose_source_path(source, workspace)

        assert result.drive_volume_mount == "Volumes/Backup"
        assert result.source_relative_path == "files"
        assert result.source_file_name == "photo.png"

    def test_macos_volume_root_level(self) -> None:
        """Test macOS volume with file at root."""
        workspace = Path("/Users/james/workspace")
        source = Path("/Volumes/Backup/photo.png")

        result = decompose_source_path(source, workspace)

        assert result.drive_volume_mount == "Volumes/Backup"
        assert result.source_relative_path is None
        assert result.source_file_name == "photo.png"

    def test_macos_volume_nested_subdirs(self) -> None:
        """Test macOS volume with nested subdirectories."""
        workspace = Path("/Users/james/workspace")
        source = Path("/Volumes/Backup/projects/2024/images/photo.png")

        result = decompose_source_path(source, workspace)

        assert result.drive_volume_mount == "Volumes/Backup"
        assert result.source_relative_path == "projects/2024/images"
        assert result.source_file_name == "photo.png"

    def test_linux_mount_mnt(self) -> None:
        """Test Linux /mnt/ mount (Scenario 14)."""
        workspace = Path("/Users/james/workspace")
        source = Path("/mnt/backup/files/photo.png")

        result = decompose_source_path(source, workspace)

        assert result.drive_volume_mount == "mnt/backup"
        assert result.source_relative_path == "files"
        assert result.source_file_name == "photo.png"

    def test_linux_mount_media(self) -> None:
        """Test Linux /media/ mount."""
        workspace = Path("/Users/james/workspace")
        source = Path("/media/usb/documents/file.pdf")

        result = decompose_source_path(source, workspace)

        assert result.drive_volume_mount == "media/usb"
        assert result.source_relative_path == "documents"
        assert result.source_file_name == "file.pdf"

    def test_linux_mount_root_level(self) -> None:
        """Test Linux mount with file at root."""
        workspace = Path("/Users/james/workspace")
        source = Path("/mnt/backup/photo.png")

        result = decompose_source_path(source, workspace)

        assert result.drive_volume_mount == "mnt/backup"
        assert result.source_relative_path is None
        assert result.source_file_name == "photo.png"

    def test_windows_unc_path_root_level(self) -> None:
        """Test Windows UNC path with file at share root (Scenario 15)."""
        workspace = Path("/Users/james/workspace")
        # UNC paths start with //
        source = Path("//server/share/photo.png")

        result = decompose_source_path(source, workspace)

        assert result.drive_volume_mount == "server/share"
        assert result.source_relative_path is None
        assert result.source_file_name == "photo.png"

    def test_windows_unc_path_with_subdirs(self) -> None:
        """Test Windows UNC path with subdirectories."""
        workspace = Path("/Users/james/workspace")
        source = Path("//server/share/documents/2024/report.pdf")

        result = decompose_source_path(source, workspace)

        assert result.drive_volume_mount == "server/share"
        assert result.source_relative_path == "documents/2024"
        assert result.source_file_name == "report.pdf"

    def test_windows_long_path_prefix_stripped(self) -> None:
        r"""Test Windows long path prefix (\\?\) is stripped before decomposition."""
        workspace = Path("/Users/james/workspace")
        # Simulate long path with \\?\ prefix
        source = Path("//?/C:/very/long/path/file.txt")

        result = decompose_source_path(source, workspace)

        # Should strip prefix and decompose as normal C: path
        assert result.drive_volume_mount == "C"
        assert result.source_relative_path == "very/long/path"
        assert result.source_file_name == "file.txt"

    def test_windows_long_unc_prefix_stripped(self) -> None:
        r"""Test Windows long UNC prefix (\\?\UNC\) is stripped."""
        workspace = Path("/Users/james/workspace")
        # Simulate long UNC path with \\?\UNC\ prefix
        source = Path("//?/UNC/server/share/file.txt")

        result = decompose_source_path(source, workspace)

        # Should strip prefix and decompose as normal UNC path
        assert result.drive_volume_mount == "server/share"
        assert result.source_relative_path is None
        assert result.source_file_name == "file.txt"

    def test_complex_filename_preserved(self) -> None:
        """Test that complex filenames with multiple extensions are preserved."""
        workspace = Path("/Users/james/workspace")
        source = Path("/Users/james/workspace/output/archive.tar.gz")

        result = decompose_source_path(source, workspace)

        assert result.drive_volume_mount is None
        assert result.source_relative_path == "output"
        assert result.source_file_name == "archive.tar.gz"

    @pytest.mark.skipif(platform.system() != "Windows", reason="Windows-specific path handling test")
    def test_backslashes_normalized(self) -> None:
        r"""Test that backslashes in paths are normalized to forward slashes."""
        workspace = Path("/Users/james/workspace")
        # Path with backslashes
        source_str = "C:\\Users\\james\\Documents\\file.txt"
        source = Path(source_str)

        result = decompose_source_path(source, workspace)

        assert result.drive_volume_mount == "C"
        assert result.source_relative_path == "Users/james/Documents"
        assert result.source_file_name == "file.txt"


class TestCanonicalizeForIdentity:
    """Tests for canonicalize_for_identity."""

    def test_expands_tilde(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """~ is expanded to the user's home directory."""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))  # Windows
        result = canonicalize_for_identity("~/project.yml")
        assert result == (tmp_path / "project.yml").resolve()

    def test_expands_env_vars(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Environment variables in the path are expanded."""
        monkeypatch.setenv("MYDIR", str(tmp_path))
        result = (
            canonicalize_for_identity("$MYDIR/file.txt")
            if sys.platform != "win32"
            else canonicalize_for_identity("%MYDIR%/file.txt")
        )
        assert result == (tmp_path / "file.txt").resolve()

    def test_strips_surrounding_quotes(self, tmp_path: Path) -> None:
        """Quoted paths are unquoted before canonicalization."""
        quoted = f'"{tmp_path}/file.txt"'
        result = canonicalize_for_identity(quoted)
        assert result == (tmp_path / "file.txt").resolve()

    def test_anchors_relative_to_base(self, tmp_path: Path) -> None:
        """Relative paths are anchored to the provided base directory."""
        result = canonicalize_for_identity("sub/file.txt", base=tmp_path)
        assert result == (tmp_path / "sub" / "file.txt").resolve()

    def test_relative_path_defaults_to_cwd(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Relative paths default to CWD when no base is provided."""
        monkeypatch.chdir(tmp_path)
        result = canonicalize_for_identity("file.txt")
        assert result == (tmp_path / "file.txt").resolve()

    def test_nonexistent_path_does_not_raise(self, tmp_path: Path) -> None:
        """Non-existent paths canonicalize without error."""
        result = canonicalize_for_identity(tmp_path / "does" / "not" / "exist.txt")
        assert result.is_absolute()
        # The resolvable prefix is resolved; remainder appended verbatim.
        assert result.name == "exist.txt"

    def test_normalizes_dot_and_dotdot(self, tmp_path: Path) -> None:
        """. and .. components are collapsed."""
        result = canonicalize_for_identity(f"{tmp_path}/a/../b/./c.txt")
        assert result == (tmp_path / "b" / "c.txt").resolve()

    def test_equivalent_spellings_collide(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Two spellings of the same file produce identical canonical paths."""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        target = tmp_path / "project.yml"
        target.touch()

        via_tilde = canonicalize_for_identity("~/project.yml")
        via_abs = canonicalize_for_identity(str(target))
        via_dotdot = canonicalize_for_identity(str(tmp_path / "sub" / ".." / "project.yml"))

        assert via_tilde == via_abs == via_dotdot

    @pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX symlinks")
    def test_follows_symlinks_when_target_exists(self, tmp_path: Path) -> None:
        """Existing symlinks are resolved to their target."""
        target = tmp_path / "real.txt"
        target.touch()
        link = tmp_path / "link.txt"
        link.symlink_to(target)

        result = canonicalize_for_identity(link)
        assert result == target.resolve()


class TestCanonicalizeForIo:
    """Tests for canonicalize_for_io."""

    def test_expands_tilde(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """~ is expanded."""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        result = canonicalize_for_io("~/file.txt")
        assert str(result).endswith("file.txt")
        assert result.is_absolute()

    def test_anchors_relative_to_base(self, tmp_path: Path) -> None:
        """Relative paths are anchored to the provided base."""
        result = canonicalize_for_io("sub/file.txt", base=tmp_path)
        assert Path(os.path.normpath(tmp_path / "sub" / "file.txt")) == result

    def test_nonexistent_path_does_not_raise(self, tmp_path: Path) -> None:
        """Non-existent paths canonicalize without error."""
        result = canonicalize_for_io(tmp_path / "new_file.txt")
        assert result == tmp_path / "new_file.txt"

    @pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX symlinks")
    def test_does_not_follow_symlinks(self, tmp_path: Path) -> None:
        """Symlinks are preserved (not followed) so newly-created parents work."""
        target = tmp_path / "real.txt"
        target.touch()
        link = tmp_path / "link.txt"
        link.symlink_to(target)

        result = canonicalize_for_io(link)
        # The io helper should NOT resolve the symlink.
        assert result == link

    @pytest.mark.skipif(not sys.platform.startswith("win"), reason="Windows long-path prefix")
    def test_adds_long_path_prefix_on_windows(self, tmp_path: Path) -> None:
        r"""Paths exceeding MAX_PATH get the \\?\ prefix on Windows."""
        long_name = "a" * 300
        result = canonicalize_for_io(tmp_path / long_name)
        assert str(result).startswith("\\\\?\\")

    @pytest.mark.skipif(sys.platform.startswith("win"), reason="non-Windows has no long-path prefix")
    def test_no_long_path_prefix_off_windows(self, tmp_path: Path) -> None:
        r"""Long paths on non-Windows platforms don't get \\?\ prefix."""
        long_name = "a" * 300
        result = canonicalize_for_io(tmp_path / long_name)
        assert not str(result).startswith("\\\\?\\")


class TestCanonicalizeToPosix:
    """Tests for ``canonicalize_to_posix``.

    These tests run on every host — ``PureWindowsPath`` parses
    Windows-shaped strings without needing an actual Windows filesystem,
    so the Windows edge cases are exercised even from macOS/Linux CI.
    """

    def test_posix_path_is_no_op(self) -> None:
        """A path already in POSIX form is returned unchanged."""
        assert canonicalize_to_posix("/posix/path/file.txt") == "/posix/path/file.txt"

    def test_drive_letter_windows_path_normalized(self) -> None:
        r"""Drive-letter paths convert `\` to `/`, preserving the drive."""
        assert canonicalize_to_posix("C:\\Users\\name") == "C:/Users/name"

    def test_unc_path_preserved(self) -> None:
        r"""UNC paths (`\\server\share\file`) preserve their network semantics.

        `\\server\share` becomes `//server/share` — the leading double
        forward-slash marks a UNC path in POSIX form.
        """
        assert canonicalize_to_posix("\\\\server\\share\\file.txt") == "//server/share/file.txt"

    def test_long_path_prefix_preserved(self) -> None:
        r"""Windows long-path prefix (`\\?\C:\...`) survives conversion."""
        assert canonicalize_to_posix("\\\\?\\C:\\path\\file.txt") == "//?/C:/path/file.txt"

    def test_long_unc_prefix_preserved(self) -> None:
        r"""Combined long-path + UNC prefix (`\\?\UNC\...`) survives conversion.

        `PureWindowsPath` appends a trailing separator when the input is a
        share root with no file component; documented and asserted so the
        behavior is stable if someone accidentally passes a bare root.
        """
        assert canonicalize_to_posix("\\\\?\\UNC\\server\\share") == "//?/UNC/server/share/"

    def test_mixed_separators_normalized(self) -> None:
        r"""Paths mixing `\` and `/` collapse to POSIX form."""
        assert canonicalize_to_posix("C:\\a/b\\c") == "C:/a/b/c"

    def test_path_input(self) -> None:
        """Path objects are accepted; the helper strings them first."""
        assert canonicalize_to_posix(Path("/x/y/z")) == "/x/y/z"
