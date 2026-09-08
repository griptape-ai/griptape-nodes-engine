"""Unit tests for StaticServerFileDriver."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from griptape_nodes.files.drivers.static_server_file_driver import StaticServerFileDriver

STATIC_SERVER_DRIVER_PRIORITY = 5


class TestStaticServerFileDriver:
    """Tests for StaticServerFileDriver class."""

    @pytest.fixture
    def driver(self) -> StaticServerFileDriver:
        """Create a StaticServerFileDriver instance."""
        return StaticServerFileDriver()

    @pytest.fixture
    def workspace_path(self, tmp_path: Path) -> Path:
        """Create a workspace directory with test files."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        static_files = workspace / "static_files"
        static_files.mkdir()
        test_file = static_files / "test.png"
        test_file.write_bytes(b"fake image data")
        return workspace

    @pytest.fixture
    def mock_config_manager(self, workspace_path: Path) -> MagicMock:
        """Mock the ConfigManager to return our test workspace."""
        config_manager = MagicMock()
        config_manager.workspace_path = workspace_path
        return config_manager

    # --- Priority ---

    def test_priority(self, driver: StaticServerFileDriver) -> None:
        """Test that driver has priority 5 (before HttpFileDriver at 50)."""
        assert driver.priority == STATIC_SERVER_DRIVER_PRIORITY

    # --- can_handle ---

    def test_can_handle_localhost_http_with_workspace(self, driver: StaticServerFileDriver) -> None:
        """Test that driver handles http://localhost URLs with /workspace/ path."""
        assert driver.can_handle("http://localhost:8124/workspace/static_files/test.png") is True

    def test_can_handle_localhost_https_with_workspace(self, driver: StaticServerFileDriver) -> None:
        """Test that driver handles https://localhost URLs with /workspace/ path."""
        assert driver.can_handle("https://localhost:8124/workspace/static_files/test.png") is True

    def test_can_handle_localhost_with_query_params(self, driver: StaticServerFileDriver) -> None:
        """Test that driver handles localhost URLs with cachebuster query params."""
        assert driver.can_handle("http://localhost:8124/workspace/static_files/test.png?t=123456") is True

    def test_can_handle_localhost_different_port(self, driver: StaticServerFileDriver) -> None:
        """Test that driver handles localhost URLs on different ports."""
        assert driver.can_handle("http://localhost:3000/workspace/static_files/test.png") is True

    def test_cannot_handle_localhost_without_workspace(self, driver: StaticServerFileDriver) -> None:
        """Test that driver rejects localhost URLs without /workspace/ path."""
        assert driver.can_handle("http://localhost:8124/api/health") is False

    def test_cannot_handle_remote_url(self, driver: StaticServerFileDriver) -> None:
        """Test that driver rejects remote URLs."""
        assert driver.can_handle("https://example.com/workspace/static_files/test.png") is False

    def test_cannot_handle_local_file_path(self, driver: StaticServerFileDriver) -> None:
        """Test that driver rejects local file paths."""
        assert driver.can_handle("/var/workspace/test.png") is False

    def test_cannot_handle_data_uri(self, driver: StaticServerFileDriver) -> None:
        """Test that driver rejects data URIs."""
        assert driver.can_handle("data:image/png;base64,abc") is False

    # --- read ---

    @pytest.mark.asyncio
    async def test_read_existing_file(
        self,
        driver: StaticServerFileDriver,
        mock_config_manager: MagicMock,
    ) -> None:
        """Test reading an existing file from localhost URL."""
        with patch(
            "griptape_nodes.files.drivers.static_server_file_driver.current_engine",
            return_value=MagicMock(config_manager=mock_config_manager),
        ):
            content = await driver.read(
                "http://localhost:8124/workspace/static_files/test.png",
                timeout=10.0,
            )
        assert content == b"fake image data"

    @pytest.mark.asyncio
    async def test_read_strips_query_params(
        self,
        driver: StaticServerFileDriver,
        mock_config_manager: MagicMock,
    ) -> None:
        """Test that query parameters (cachebuster) are stripped before resolving."""
        with patch(
            "griptape_nodes.files.drivers.static_server_file_driver.current_engine",
            return_value=MagicMock(config_manager=mock_config_manager),
        ):
            content = await driver.read(
                "http://localhost:8124/workspace/static_files/test.png?t=1234567890",
                timeout=10.0,
            )
        assert content == b"fake image data"

    @pytest.mark.asyncio
    async def test_read_file_not_found(
        self,
        driver: StaticServerFileDriver,
        mock_config_manager: MagicMock,
    ) -> None:
        """Test reading a non-existent file raises FileNotFoundError."""
        with (
            patch(
                "griptape_nodes.files.drivers.static_server_file_driver.current_engine",
                return_value=MagicMock(config_manager=mock_config_manager),
            ),
            pytest.raises(FileNotFoundError, match="file not found"),
        ):
            await driver.read(
                "http://localhost:8124/workspace/static_files/nonexistent.png",
                timeout=10.0,
            )

    @pytest.mark.asyncio
    async def test_read_directory_raises_error(
        self,
        driver: StaticServerFileDriver,
        mock_config_manager: MagicMock,
    ) -> None:
        """Test reading a directory raises IsADirectoryError."""
        with (
            patch(
                "griptape_nodes.files.drivers.static_server_file_driver.current_engine",
                return_value=MagicMock(config_manager=mock_config_manager),
            ),
            pytest.raises(IsADirectoryError, match="directory"),
        ):
            await driver.read(
                "http://localhost:8124/workspace/static_files",
                timeout=10.0,
            )

    @pytest.mark.asyncio
    async def test_read_invalid_url_no_workspace(self, driver: StaticServerFileDriver) -> None:
        """Test reading from localhost URL without /workspace/ raises ValueError."""
        with pytest.raises(ValueError, match="/workspace/ not found"):
            await driver.read(
                "http://localhost:8124/api/health",
                timeout=10.0,
            )

    # --- exists ---

    @pytest.mark.asyncio
    async def test_exists_true_for_existing_file(
        self,
        driver: StaticServerFileDriver,
        mock_config_manager: MagicMock,
    ) -> None:
        """Test exists returns True for existing file."""
        with patch(
            "griptape_nodes.files.drivers.static_server_file_driver.current_engine",
            return_value=MagicMock(config_manager=mock_config_manager),
        ):
            result = await driver.exists("http://localhost:8124/workspace/static_files/test.png")
        assert result is True

    @pytest.mark.asyncio
    async def test_exists_false_for_nonexistent_file(
        self,
        driver: StaticServerFileDriver,
        mock_config_manager: MagicMock,
    ) -> None:
        """Test exists returns False for non-existent file."""
        with patch(
            "griptape_nodes.files.drivers.static_server_file_driver.current_engine",
            return_value=MagicMock(config_manager=mock_config_manager),
        ):
            result = await driver.exists("http://localhost:8124/workspace/static_files/nonexistent.png")
        assert result is False

    @pytest.mark.asyncio
    async def test_exists_false_for_invalid_url(self, driver: StaticServerFileDriver) -> None:
        """Test exists returns False for URL without /workspace/ path."""
        result = await driver.exists("http://localhost:8124/api/health")
        assert result is False

    # --- get_size ---

    def test_get_size_returns_correct_size(
        self,
        driver: StaticServerFileDriver,
        mock_config_manager: MagicMock,
    ) -> None:
        """Test get_size returns the correct file size."""
        with patch(
            "griptape_nodes.files.drivers.static_server_file_driver.current_engine",
            return_value=MagicMock(config_manager=mock_config_manager),
        ):
            size = driver.get_size("http://localhost:8124/workspace/static_files/test.png")
        assert size == len(b"fake image data")

    def test_get_size_file_not_found(
        self,
        driver: StaticServerFileDriver,
        mock_config_manager: MagicMock,
    ) -> None:
        """Test get_size raises FileNotFoundError for non-existent file."""
        with (
            patch(
                "griptape_nodes.files.drivers.static_server_file_driver.current_engine",
                return_value=MagicMock(config_manager=mock_config_manager),
            ),
            pytest.raises(FileNotFoundError, match="file not found"),
        ):
            driver.get_size("http://localhost:8124/workspace/static_files/nonexistent.png")

    def test_get_size_directory_raises_error(
        self,
        driver: StaticServerFileDriver,
        mock_config_manager: MagicMock,
    ) -> None:
        """Test get_size raises IsADirectoryError for directories."""
        with (
            patch(
                "griptape_nodes.files.drivers.static_server_file_driver.current_engine",
                return_value=MagicMock(config_manager=mock_config_manager),
            ),
            pytest.raises(IsADirectoryError, match="directory"),
        ):
            driver.get_size("http://localhost:8124/workspace/static_files")
