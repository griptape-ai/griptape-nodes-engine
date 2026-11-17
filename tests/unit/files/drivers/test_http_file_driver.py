"""Unit tests for HttpFileDriver."""

from unittest.mock import AsyncMock, Mock, patch

import pytest

from griptape_nodes.files.drivers.http_file_driver import HttpFileDriver


class TestHttpFileDriver:
    """Tests for HttpFileDriver class."""

    @pytest.fixture
    def driver(self) -> HttpFileDriver:
        """Create an HttpFileDriver instance."""
        return HttpFileDriver()

    def test_can_handle_http_urls(self, driver: HttpFileDriver) -> None:
        """Test that driver handles HTTP URLs."""
        assert driver.can_handle("http://example.com/file.txt") is True

    def test_can_handle_https_urls(self, driver: HttpFileDriver) -> None:
        """Test that driver handles HTTPS URLs."""
        assert driver.can_handle("https://example.com/file.txt") is True

    def test_can_handle_rejects_other_protocols(self, driver: HttpFileDriver) -> None:
        """Test that driver rejects non-HTTP protocols."""
        assert driver.can_handle("ftp://example.com/file.txt") is False
        assert driver.can_handle("/local/path/file.txt") is False
        assert driver.can_handle("data:image/png;base64,abc") is False

    @pytest.mark.asyncio
    async def test_read_successful_download(self, driver: HttpFileDriver) -> None:
        """Test successful file download."""
        mock_response = Mock()
        mock_response.content = b"downloaded content"
        mock_response.raise_for_status = Mock()

        with patch("httpx2.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock()
            mock_client_class.return_value = mock_client

            content = await driver.read("https://example.com/file.txt", timeout=30.0)
            assert content == b"downloaded content"
            mock_client.get.assert_called_once_with("https://example.com/file.txt", timeout=30.0)

    @pytest.mark.asyncio
    async def test_read_http_error(self, driver: HttpFileDriver) -> None:
        """Test that HTTP errors are raised as RuntimeError."""
        import httpx2

        with patch("httpx2.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=httpx2.HTTPError("Connection failed"))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            with pytest.raises(RuntimeError) as exc_info:
                await driver.read("https://example.com/file.txt", timeout=30.0)
            assert "Failed to download" in str(exc_info.value)
            assert "https://example.com/file.txt" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_read_respects_timeout(self, driver: HttpFileDriver) -> None:
        """Test that timeout parameter is passed to httpx2."""
        mock_response = Mock()
        mock_response.content = b"content"
        mock_response.raise_for_status = Mock()

        with patch("httpx2.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock()
            mock_client_class.return_value = mock_client

            await driver.read("https://example.com/file.txt", timeout=60.0)
            mock_client.get.assert_called_once_with("https://example.com/file.txt", timeout=60.0)

    @pytest.mark.asyncio
    async def test_exists_returns_true_for_accessible_url(self, driver: HttpFileDriver) -> None:
        """Test exists returns True for accessible URL (2xx status)."""
        mock_response = Mock()
        mock_response.status_code = 200

        with patch("httpx2.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.head = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock()
            mock_client_class.return_value = mock_client

            result = await driver.exists("https://example.com/file.txt")
            assert result is True

    @pytest.mark.asyncio
    async def test_exists_returns_false_for_404(self, driver: HttpFileDriver) -> None:
        """Test exists returns False for 404 status."""
        mock_response = Mock()
        mock_response.status_code = 404

        with patch("httpx2.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.head = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock()
            mock_client_class.return_value = mock_client

            result = await driver.exists("https://example.com/file.txt")
            assert result is False

    @pytest.mark.asyncio
    async def test_exists_returns_false_on_error(self, driver: HttpFileDriver) -> None:
        """Test exists returns False when HTTP error occurs."""
        import httpx2

        with patch("httpx2.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.head = AsyncMock(side_effect=httpx2.HTTPError("Connection failed"))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            result = await driver.exists("https://example.com/file.txt")
            assert result is False

    def test_get_size_from_content_length_header(self, driver: HttpFileDriver) -> None:
        """Test get_size extracts size from Content-Length header."""
        mock_response = Mock()
        mock_response.headers = {"content-length": "1234"}
        mock_response.raise_for_status = Mock()

        with patch("httpx2.Client") as mock_client_class:
            mock_client = Mock()
            mock_client.head = Mock(return_value=mock_response)
            mock_client.__enter__ = Mock(return_value=mock_client)
            mock_client.__exit__ = Mock()
            mock_client_class.return_value = mock_client

            size = driver.get_size("https://example.com/file.txt")
            expected_size = 1234
            assert size == expected_size

    def test_get_size_returns_zero_when_no_content_length(self, driver: HttpFileDriver) -> None:
        """Test get_size returns 0 when Content-Length header is missing."""
        mock_response = Mock()
        mock_response.headers = {}
        mock_response.raise_for_status = Mock()

        with patch("httpx2.Client") as mock_client_class:
            mock_client = Mock()
            mock_client.head = Mock(return_value=mock_response)
            mock_client.__enter__ = Mock(return_value=mock_client)
            mock_client.__exit__ = Mock()
            mock_client_class.return_value = mock_client

            size = driver.get_size("https://example.com/file.txt")
            assert size == 0

    def test_get_size_returns_zero_on_error(self, driver: HttpFileDriver) -> None:
        """Test get_size returns 0 when HTTP error occurs."""
        import httpx2

        with patch("httpx2.Client") as mock_client_class:
            mock_client = Mock()
            mock_client.head = Mock(side_effect=httpx2.HTTPError("Connection failed"))
            mock_client.__enter__ = Mock(return_value=mock_client)
            mock_client.__exit__ = Mock(return_value=None)
            mock_client_class.return_value = mock_client

            size = driver.get_size("https://example.com/file.txt")
            assert size == 0
