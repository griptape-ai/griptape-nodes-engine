"""Unit tests for LocalFileDriver."""

import platform
import sys
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import Mock

import pytest

from griptape_nodes.files.drivers.local_file_driver import LocalFileDriver
from griptape_nodes.files.path_utils import parse_file_uri
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
from griptape_nodes.retained_mode.managers.config_manager import ConfigManager


class TestLocalFileDriver:
    """Tests for LocalFileDriver class."""

    @pytest.fixture
    def driver(self) -> LocalFileDriver:
        """Create a LocalFileDriver instance."""
        return LocalFileDriver()

    def test_can_handle_always_returns_true(self, driver: LocalFileDriver, tmp_path: Path) -> None:
        """Test that driver always returns True (fallback driver).

        As the fallback driver (priority 100, checked last), LocalFileDriver
        handles any location not matched by a more specific driver.
        """
        absolute_path = tmp_path / "file.txt"
        assert driver.can_handle(str(absolute_path)) is True
        assert driver.can_handle("relative/path/file.txt") is True
        assert driver.can_handle("http://example.com/file.txt") is True
        assert driver.can_handle("data:image/png;base64,abc") is True

    @pytest.mark.asyncio
    async def test_read_existing_file(self, driver: LocalFileDriver, temp_file: Path) -> None:
        """Test reading an existing file."""
        content = await driver.read(str(temp_file), timeout=10.0)
        assert content == b"test content"

    @pytest.mark.asyncio
    async def test_read_file_not_found(self, driver: LocalFileDriver, tmp_path: Path) -> None:
        """Test reading a non-existent file raises FileNotFoundError."""
        nonexistent = tmp_path / "nonexistent.txt"
        with pytest.raises(FileNotFoundError) as exc_info:
            await driver.read(str(nonexistent), timeout=10.0)
        assert "File not found" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_read_directory_raises_error(self, driver: LocalFileDriver, tmp_path: Path) -> None:
        """Test reading a directory raises IsADirectoryError."""
        with pytest.raises(IsADirectoryError) as exc_info:
            await driver.read(str(tmp_path), timeout=10.0)
        assert "directory" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_read_with_shell_escapes(self, driver: LocalFileDriver, tmp_path: Path) -> None:
        """Test reading file with shell escapes in path (macOS Finder)."""
        # Create file with spaces in name
        file_with_spaces = tmp_path / "test file.txt"
        file_with_spaces.write_text("content")

        # Simulate macOS Finder path with shell escapes
        escaped_path = str(file_with_spaces).replace(" ", "\\ ")
        content = await driver.read(escaped_path, timeout=10.0)
        assert content == b"content"

    @pytest.mark.asyncio
    async def test_read_with_tilde_expansion(
        self,
        driver: LocalFileDriver,
        tmp_path: Path,  # noqa: ARG002
    ) -> None:
        """Test reading file with tilde in path."""
        # This test assumes home directory exists
        home_file = Path.home() / ".bashrc"
        if home_file.exists():
            content = await driver.read("~/.bashrc", timeout=10.0)
            assert isinstance(content, bytes)

    @pytest.mark.asyncio
    async def test_exists_for_existing_file(self, driver: LocalFileDriver, temp_file: Path) -> None:
        """Test exists returns True for existing file."""
        assert await driver.exists(str(temp_file)) is True

    @pytest.mark.asyncio
    async def test_exists_for_nonexistent_file(self, driver: LocalFileDriver, tmp_path: Path) -> None:
        """Test exists returns False for non-existent file."""
        nonexistent = tmp_path / "nonexistent.txt"
        assert await driver.exists(str(nonexistent)) is False

    @pytest.mark.asyncio
    async def test_exists_for_directory(self, driver: LocalFileDriver, tmp_path: Path) -> None:
        """Test exists returns False for directory."""
        assert await driver.exists(str(tmp_path)) is False

    def test_get_size_for_existing_file(self, driver: LocalFileDriver, temp_file: Path) -> None:
        """Test get_size returns correct size for existing file."""
        size = driver.get_size(str(temp_file))
        assert size == len("test content")

    def test_get_size_for_nonexistent_file(self, driver: LocalFileDriver, tmp_path: Path) -> None:
        """Test get_size raises FileNotFoundError for non-existent file."""
        nonexistent = tmp_path / "nonexistent.txt"
        with pytest.raises(FileNotFoundError) as exc_info:
            driver.get_size(str(nonexistent))
        assert "File not found" in str(exc_info.value)

    def test_get_size_for_directory(self, driver: LocalFileDriver, tmp_path: Path) -> None:
        """Test get_size raises IsADirectoryError for directory."""
        with pytest.raises(IsADirectoryError) as exc_info:
            driver.get_size(str(tmp_path))
        assert "directory" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_read_binary_file(self, driver: LocalFileDriver, tmp_path: Path) -> None:
        """Test reading binary file content."""
        binary_file = tmp_path / "binary.dat"
        binary_content = bytes([0, 1, 2, 3, 255, 254, 253])
        binary_file.write_bytes(binary_content)

        content = await driver.read(str(binary_file), timeout=10.0)
        assert content == binary_content


class TestLocalFileDriverFileURI:
    """Tests for LocalFileDriver file:// URI support."""

    @pytest.fixture
    def driver(self) -> LocalFileDriver:
        """Create a LocalFileDriver instance."""
        return LocalFileDriver()

    def test_parse_file_uri_unix_absolute(self, driver: LocalFileDriver) -> None:  # noqa: ARG002
        """Test parsing Unix absolute path file URI."""
        uri = "file:///path/to/file.txt"
        result = parse_file_uri(uri)
        assert result == "/path/to/file.txt"

    def test_parse_file_uri_localhost(self, driver: LocalFileDriver) -> None:  # noqa: ARG002
        """Test parsing file URI with localhost."""
        uri = "file://localhost/path/to/file.txt"
        result = parse_file_uri(uri)
        assert result == "/path/to/file.txt"

    @pytest.mark.skipif(platform.system() != "Windows", reason="Windows-specific test")
    def test_parse_file_uri_windows_absolute(self, driver: LocalFileDriver) -> None:  # noqa: ARG002
        """Test parsing Windows absolute path file URI."""
        uri = "file:///C:/Users/test/file.txt"
        result = parse_file_uri(uri)
        assert result == "C:/Users/test/file.txt"

    def test_parse_file_uri_with_percent_encoding(self, driver: LocalFileDriver) -> None:  # noqa: ARG002
        """Test parsing file URI with percent-encoded characters."""
        uri = "file:///path/to/file%20with%20spaces.txt"
        result = parse_file_uri(uri)
        assert result == "/path/to/file with spaces.txt"

    def test_parse_file_uri_rejects_remote_host(self, driver: LocalFileDriver) -> None:  # noqa: ARG002
        """Test that file URIs with non-localhost hosts are rejected."""
        uri = "file://remote-server/path/to/file.txt"
        result = parse_file_uri(uri)
        assert result is None

    def test_parse_file_uri_rejects_non_file_scheme(self, driver: LocalFileDriver) -> None:  # noqa: ARG002
        """Test that non-file:// URIs are rejected."""
        uri = "http://example.com/file.txt"
        result = parse_file_uri(uri)
        assert result is None

    def test_parse_file_uri_returns_none_for_regular_path(self, driver: LocalFileDriver) -> None:  # noqa: ARG002
        """Test that regular paths (not file:// URIs) return None."""
        result = parse_file_uri("/regular/path/file.txt")
        assert result is None

    def test_can_handle_file_uri_unix(self, driver: LocalFileDriver) -> None:
        """Test that driver handles Unix file:// URIs."""
        uri = "file:///path/to/file.txt"
        assert driver.can_handle(uri) is True

    def test_can_handle_file_uri_localhost(self, driver: LocalFileDriver) -> None:
        """Test that driver handles localhost file:// URIs."""
        uri = "file://localhost/path/to/file.txt"
        assert driver.can_handle(uri) is True

    @pytest.mark.skipif(platform.system() != "Windows", reason="Windows-specific test")
    def test_can_handle_file_uri_windows(self, driver: LocalFileDriver) -> None:
        """Test that driver handles Windows file:// URIs."""
        uri = "file:///C:/Users/test/file.txt"
        assert driver.can_handle(uri) is True

    def test_can_handle_accepts_remote_file_uri(self, driver: LocalFileDriver) -> None:
        """Test that driver accepts file:// URIs with remote hosts (fallback).

        The driver accepts all locations; invalid URIs fail at read time, not can_handle.
        """
        uri = "file://remote-server/path/to/file.txt"
        assert driver.can_handle(uri) is True

    @pytest.mark.asyncio
    async def test_read_file_uri(self, driver: LocalFileDriver, temp_file: Path) -> None:
        """Test reading file via file:// URI."""
        # Convert path to file:// URI
        file_uri = temp_file.as_uri()

        content = await driver.read(file_uri, timeout=10.0)
        assert content == b"test content"

    @pytest.mark.asyncio
    async def test_read_file_uri_with_spaces(self, driver: LocalFileDriver, tmp_path: Path) -> None:
        """Test reading file with spaces in name via file:// URI."""
        file_with_spaces = tmp_path / "test file.txt"
        file_with_spaces.write_text("content with spaces")

        file_uri = file_with_spaces.as_uri()

        content = await driver.read(file_uri, timeout=10.0)
        assert content == b"content with spaces"

    @pytest.mark.asyncio
    async def test_read_file_uri_not_found(self, driver: LocalFileDriver, tmp_path: Path) -> None:
        """Test reading non-existent file via file:// URI raises FileNotFoundError."""
        nonexistent = tmp_path / "nonexistent.txt"
        file_uri = nonexistent.as_uri()

        with pytest.raises(FileNotFoundError) as exc_info:
            await driver.read(file_uri, timeout=10.0)
        assert "File not found" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_read_invalid_file_uri(self, driver: LocalFileDriver) -> None:
        """Test reading file with invalid file:// URI raises ValueError."""
        invalid_uri = "file://remote-server/path/to/file.txt"

        with pytest.raises(ValueError, match="Invalid file:// URI"):
            await driver.read(invalid_uri, timeout=10.0)

    @pytest.mark.asyncio
    async def test_exists_file_uri(self, driver: LocalFileDriver, temp_file: Path) -> None:
        """Test exists with file:// URI for existing file."""
        file_uri = temp_file.as_uri()
        assert await driver.exists(file_uri) is True

    @pytest.mark.asyncio
    async def test_exists_file_uri_nonexistent(self, driver: LocalFileDriver, tmp_path: Path) -> None:
        """Test exists with file:// URI for non-existent file."""
        nonexistent = tmp_path / "nonexistent.txt"
        file_uri = nonexistent.as_uri()
        assert await driver.exists(file_uri) is False

    @pytest.mark.asyncio
    async def test_exists_invalid_file_uri(self, driver: LocalFileDriver) -> None:
        """Test exists with invalid file:// URI returns False."""
        invalid_uri = "file://remote-server/path/to/file.txt"
        assert await driver.exists(invalid_uri) is False

    def test_get_size_file_uri(self, driver: LocalFileDriver, temp_file: Path) -> None:
        """Test get_size with file:// URI."""
        file_uri = temp_file.as_uri()
        size = driver.get_size(file_uri)
        assert size == len("test content")

    def test_get_size_file_uri_not_found(self, driver: LocalFileDriver, tmp_path: Path) -> None:
        """Test get_size with file:// URI for non-existent file raises FileNotFoundError."""
        nonexistent = tmp_path / "nonexistent.txt"
        file_uri = nonexistent.as_uri()

        with pytest.raises(FileNotFoundError) as exc_info:
            driver.get_size(file_uri)
        assert "File not found" in str(exc_info.value)

    def test_get_size_invalid_file_uri(self, driver: LocalFileDriver) -> None:
        """Test get_size with invalid file:// URI raises ValueError."""
        invalid_uri = "file://remote-server/path/to/file.txt"

        with pytest.raises(ValueError, match="Invalid file:// URI"):
            driver.get_size(invalid_uri)


class TestLocalFileDriverRelativePaths:
    """Tests that relative paths anchor on the workspace directory, never the process CWD."""

    @pytest.fixture
    def driver(self) -> LocalFileDriver:
        """Create a LocalFileDriver instance."""
        return LocalFileDriver()

    @pytest.fixture
    def workspace_path(self, tmp_path: Path) -> Path:
        """Create a workspace directory holding the files the driver should find."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "config.json").write_text("workspace copy")
        (workspace / "foo%20bar.png").write_text("workspace percent copy")
        (workspace / "report$.txt").write_text("workspace dollar copy")
        (workspace / "workspace_only.json").write_text("workspace only copy")
        nested_dir = workspace / "data"
        nested_dir.mkdir()
        (nested_dir / "nested.json").write_text("workspace nested copy")
        return workspace

    @pytest.fixture
    def cwd_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        """Create a decoy directory with same-named, different-content files, and chdir into it."""
        cwd = tmp_path / "cwd"
        cwd.mkdir()
        (cwd / "config.json").write_text("cwd copy")
        (cwd / "foo%20bar.png").write_text("cwd percent copy")
        (cwd / "report$.txt").write_text("cwd dollar copy")
        (cwd / "cwd_only.json").write_text("cwd only copy")
        nested_dir = cwd / "data"
        nested_dir.mkdir()
        (nested_dir / "nested.json").write_text("cwd nested copy")
        monkeypatch.chdir(cwd)
        return cwd

    @pytest.fixture
    def home_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        """Create a fake home directory and point ~ expansion at it."""
        home = tmp_path / "home"
        home.mkdir()
        (home / "config.json").write_text("home copy")
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))
        return home

    @pytest.fixture
    def mock_config_manager(self, workspace_path: Path) -> Mock:
        """Mock the ConfigManager so its workspace_path is our test workspace."""
        config_manager = Mock(spec=ConfigManager)
        config_manager.workspace_path = workspace_path
        return config_manager

    @pytest.fixture
    def mock_config_manager_accessor(self, mock_config_manager: Mock) -> Iterator[Mock]:
        """Patch the facade accessor the driver uses to reach the ConfigManager.

        Deliberately not `monkeypatch.setattr`: it snapshots `getattr(GriptapeNodes,
        "ConfigManager")`, which for a classmethod is the *bound* method, and on teardown
        writes that bound method into `GriptapeNodes.__dict__` instead of the original
        classmethod descriptor. That permanently mutates a process-global facade class every
        other test shares. Saving and restoring `__dict__` puts the real descriptor back.
        """
        original_descriptor = GriptapeNodes.__dict__["ConfigManager"]
        accessor = Mock(spec=GriptapeNodes.ConfigManager, return_value=mock_config_manager)
        GriptapeNodes.ConfigManager = accessor  # type: ignore[method-assign]
        try:
            yield accessor
        finally:
            GriptapeNodes.ConfigManager = original_descriptor  # type: ignore[method-assign]

    @pytest.mark.asyncio
    async def test_read_bare_relative_path_uses_workspace_not_cwd(
        self,
        driver: LocalFileDriver,
        cwd_path: Path,  # noqa: ARG002
        mock_config_manager_accessor: Mock,
    ) -> None:
        """Test a bare relative path reads the workspace copy, not the same-named CWD copy."""
        content = await driver.read("config.json", timeout=10.0)

        assert content == b"workspace copy"
        mock_config_manager_accessor.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_read_relative_path_with_subdirectories_uses_workspace(
        self,
        driver: LocalFileDriver,
        cwd_path: Path,  # noqa: ARG002
        mock_config_manager_accessor: Mock,
    ) -> None:
        """Test a relative path with subdirectories anchors under the workspace."""
        content = await driver.read("data/nested.json", timeout=10.0)

        assert content == b"workspace nested copy"
        mock_config_manager_accessor.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_read_percent_encoded_relative_path_uses_workspace(
        self,
        driver: LocalFileDriver,
        cwd_path: Path,  # noqa: ARG002
        mock_config_manager_accessor: Mock,
    ) -> None:
        """Test a relative name containing '%' still anchors on the workspace after expansion.

        '%' makes `path_needs_expansion` True, so expansion runs, but a URL-encoded filename
        has no env var to substitute and comes back relative. It must still be anchored.
        """
        content = await driver.read("foo%20bar.png", timeout=10.0)

        assert content == b"workspace percent copy"
        mock_config_manager_accessor.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_read_dollar_relative_path_with_no_matching_env_var_uses_workspace(
        self,
        driver: LocalFileDriver,
        cwd_path: Path,  # noqa: ARG002
        mock_config_manager_accessor: Mock,
    ) -> None:
        """Test a relative name containing '$' with no matching env var anchors on the workspace."""
        content = await driver.read("report$.txt", timeout=10.0)

        assert content == b"workspace dollar copy"
        mock_config_manager_accessor.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_exists_finds_relative_path_present_only_in_workspace(
        self,
        driver: LocalFileDriver,
        cwd_path: Path,  # noqa: ARG002
        mock_config_manager_accessor: Mock,
    ) -> None:
        """Test exists() is True for a relative name that exists only in the workspace."""
        assert await driver.exists("workspace_only.json") is True
        mock_config_manager_accessor.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_exists_misses_relative_path_present_only_in_cwd(
        self,
        driver: LocalFileDriver,
        cwd_path: Path,  # noqa: ARG002
        mock_config_manager_accessor: Mock,
    ) -> None:
        """Test exists() is False for a relative name that exists only in the process CWD."""
        assert await driver.exists("cwd_only.json") is False
        mock_config_manager_accessor.assert_called_once_with()

    def test_get_size_bare_relative_path_measures_workspace_not_cwd(
        self,
        driver: LocalFileDriver,
        cwd_path: Path,  # noqa: ARG002
        mock_config_manager_accessor: Mock,
    ) -> None:
        """Test get_size() measures the workspace copy, not the shorter same-named CWD copy."""
        size = driver.get_size("config.json")

        assert size == len("workspace copy")
        mock_config_manager_accessor.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_read_absolute_path_does_not_consult_workspace(
        self,
        driver: LocalFileDriver,
        tmp_path: Path,
        mock_config_manager_accessor: Mock,
    ) -> None:
        """Test an absolute path reads as-is without ever asking for the workspace directory."""
        absolute_file = tmp_path / "absolute.json"
        absolute_file.write_text("absolute copy")

        content = await driver.read(str(absolute_file), timeout=10.0)

        assert content == b"absolute copy"
        mock_config_manager_accessor.assert_not_called()

    @pytest.mark.asyncio
    async def test_read_tilde_path_expands_to_home_not_workspace(
        self,
        driver: LocalFileDriver,
        home_path: Path,  # noqa: ARG002
        mock_config_manager_accessor: Mock,
    ) -> None:
        """Test a ~ path expands to the home directory without the workspace prefixed onto it."""
        content = await driver.read("~/config.json", timeout=10.0)

        assert content == b"home copy"
        mock_config_manager_accessor.assert_not_called()

    @pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX symlinks")
    @pytest.mark.asyncio
    async def test_read_relative_path_through_symlink_collapses_dotdot_lexically(
        self,
        driver: LocalFileDriver,
        tmp_path: Path,
        workspace_path: Path,
        mock_config_manager_accessor: Mock,
    ) -> None:
        """Test '..' after a symlinked directory cancels the link name instead of walking through it."""
        outside_dir = tmp_path / "mnt"
        symlink_target = outside_dir / "target"
        symlink_target.mkdir(parents=True)
        (outside_dir / "b").write_bytes(b"mnt b")
        (workspace_path / "b").write_bytes(b"workspace b")
        (workspace_path / "link").symlink_to(symlink_target, target_is_directory=True)

        content = await driver.read("link/../b", timeout=10.0)

        assert content == b"workspace b"
        mock_config_manager_accessor.assert_called_once_with()
