"""Unit tests for GriptapeCloudFileDriver."""

import os
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest

from griptape_nodes.files.drivers.griptape_cloud_file_driver import GriptapeCloudFileDriver


class TestGriptapeCloudFileDriver:
    """Tests for GriptapeCloudFileDriver class."""

    @pytest.fixture
    def driver(self) -> GriptapeCloudFileDriver:
        """Create a GriptapeCloudFileDriver instance."""
        return GriptapeCloudFileDriver(
            bucket_id="test-bucket-123", api_key="test-api-key", base_url="https://cloud.griptape.ai"
        )

    @pytest.fixture
    def mock_cloud_storage_driver(self) -> Any:
        """Mock GriptapeCloudStorageDriver static methods."""
        with patch("griptape_nodes.files.drivers.griptape_cloud_file_driver.GriptapeCloudStorageDriver") as mock:
            mock.is_cloud_asset_url = Mock(return_value=True)
            mock.extract_workspace_path_from_cloud_url = Mock(return_value="assets/test.txt")
            yield mock

    def test_initialization(self, driver: GriptapeCloudFileDriver) -> None:
        """Test driver initialization with credentials."""
        assert driver.bucket_id == "test-bucket-123"
        assert driver.api_key == "test-api-key"
        assert driver.base_url == "https://cloud.griptape.ai"
        assert driver.headers == {"Authorization": "Bearer test-api-key"}

    def test_create_from_env_with_credentials(self) -> None:
        """Test creating driver from environment variables."""
        # clear=True so a Griptape license in the developer's own environment (or
        # hydrated from their .env by SecretsManager) can't win credential
        # resolution and fail this test on behavior it isn't covering.
        with patch.dict(
            os.environ,
            {"GT_CLOUD_BUCKET_ID": "env-bucket", "GT_CLOUD_API_KEY": "env-key", "GT_CLOUD_BASE_URL": "https://test.ai"},
            clear=True,
        ):
            driver = GriptapeCloudFileDriver.create_from_env()
            assert driver is not None
            assert driver.bucket_id == "env-bucket"
            assert driver.api_key == "env-key"
            assert driver.base_url == "https://test.ai"

    def test_create_from_env_without_credentials(self) -> None:
        """Test creating driver from env returns None when credentials missing."""
        with patch.dict(os.environ, {}, clear=True):
            driver = GriptapeCloudFileDriver.create_from_env()
            assert driver is None

    def test_create_from_env_default_base_url(self) -> None:
        """Test creating driver uses default base URL when not provided."""
        with patch.dict(os.environ, {"GT_CLOUD_BUCKET_ID": "bucket", "GT_CLOUD_API_KEY": "key"}, clear=True):
            driver = GriptapeCloudFileDriver.create_from_env()
            assert driver is not None
            assert driver.base_url == "https://cloud.griptape.ai"

    def test_can_handle_cloud_urls(self, driver: GriptapeCloudFileDriver, mock_cloud_storage_driver: Any) -> None:
        """Test that driver handles Griptape Cloud URLs."""
        mock_cloud_storage_driver.is_cloud_asset_url.return_value = True
        result = driver.can_handle("https://cloud.griptape.ai/buckets/123/assets/test.txt")
        assert result is True
        mock_cloud_storage_driver.is_cloud_asset_url.assert_called_once()

    def test_can_handle_rejects_other_urls(
        self, driver: GriptapeCloudFileDriver, mock_cloud_storage_driver: Any
    ) -> None:
        """Test that driver rejects non-cloud URLs."""
        mock_cloud_storage_driver.is_cloud_asset_url.return_value = False
        result = driver.can_handle("https://example.com/file.txt")
        assert result is False

    @pytest.mark.asyncio
    async def test_read_successful_download(
        self,
        driver: GriptapeCloudFileDriver,
        mock_cloud_storage_driver: Any,  # noqa: ARG002
    ) -> None:
        """Test successful file download from cloud storage."""
        # Mock API response with signed URL
        mock_api_response = Mock()
        mock_api_response.json = Mock(return_value={"url": "https://signed.url/file.txt"})
        mock_api_response.raise_for_status = Mock()

        # Mock download response
        mock_download_response = Mock()
        mock_download_response.content = b"cloud file content"
        mock_download_response.raise_for_status = Mock()

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_api_response)
            mock_client.get = AsyncMock(return_value=mock_download_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock()
            mock_client_class.return_value = mock_client

            content = await driver.read("https://cloud.griptape.ai/buckets/123/assets/test.txt", timeout=30.0)
            assert content == b"cloud file content"

    @pytest.mark.asyncio
    async def test_read_url_extraction_failure(
        self, driver: GriptapeCloudFileDriver, mock_cloud_storage_driver: Any
    ) -> None:
        """Test read raises error when URL extraction fails."""
        mock_cloud_storage_driver.extract_workspace_path_from_cloud_url.return_value = None

        with pytest.raises(RuntimeError) as exc_info:
            await driver.read("https://cloud.griptape.ai/invalid/url", timeout=30.0)
        assert "Failed to extract workspace path" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_read_http_error(
        self,
        driver: GriptapeCloudFileDriver,
        mock_cloud_storage_driver: Any,  # noqa: ARG002
    ) -> None:
        """Test read raises RuntimeError on HTTP error."""
        import httpx

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(side_effect=httpx.HTTPError("Connection failed"))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            with pytest.raises(RuntimeError) as exc_info:
                await driver.read("https://cloud.griptape.ai/buckets/123/assets/test.txt", timeout=30.0)
            assert "Failed to download from cloud storage" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_exists_returns_true_for_accessible_asset(
        self,
        driver: GriptapeCloudFileDriver,
        mock_cloud_storage_driver: Any,  # noqa: ARG002
    ) -> None:
        """Test exists returns True for accessible cloud asset."""
        mock_response = Mock()
        mock_response.status_code = 200

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock()
            mock_client_class.return_value = mock_client

            result = await driver.exists("https://cloud.griptape.ai/buckets/123/assets/test.txt")
            assert result is True

    @pytest.mark.asyncio
    async def test_exists_returns_false_for_404(
        self,
        driver: GriptapeCloudFileDriver,
        mock_cloud_storage_driver: Any,  # noqa: ARG002
    ) -> None:
        """Test exists returns False for 404 status."""
        mock_response = Mock()
        mock_response.status_code = 404

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock()
            mock_client_class.return_value = mock_client

            result = await driver.exists("https://cloud.griptape.ai/buckets/123/assets/test.txt")
            assert result is False

    @pytest.mark.asyncio
    async def test_exists_returns_false_on_url_extraction_failure(
        self, driver: GriptapeCloudFileDriver, mock_cloud_storage_driver: Any
    ) -> None:
        """Test exists returns False when URL extraction fails."""
        mock_cloud_storage_driver.extract_workspace_path_from_cloud_url.return_value = None
        result = await driver.exists("https://cloud.griptape.ai/invalid/url")
        assert result is False

    def test_get_size_from_content_length(
        self,
        driver: GriptapeCloudFileDriver,
        mock_cloud_storage_driver: Any,  # noqa: ARG002
    ) -> None:
        """Test get_size extracts size from Content-Length header."""
        # Mock API response with signed URL
        mock_api_response = Mock()
        mock_api_response.json = Mock(return_value={"url": "https://signed.url/file.txt"})
        mock_api_response.raise_for_status = Mock()

        # Mock HEAD response
        mock_head_response = Mock()
        mock_head_response.headers = {"content-length": "5678"}
        mock_head_response.raise_for_status = Mock()

        with patch("httpx.Client") as mock_client_class:
            mock_client = Mock()
            mock_client.post = Mock(return_value=mock_api_response)
            mock_client.head = Mock(return_value=mock_head_response)
            mock_client.__enter__ = Mock(return_value=mock_client)
            mock_client.__exit__ = Mock()
            mock_client_class.return_value = mock_client

            size = driver.get_size("https://cloud.griptape.ai/buckets/123/assets/test.txt")
            expected_size = 5678
            assert size == expected_size

    def test_get_size_returns_zero_on_error(
        self,
        driver: GriptapeCloudFileDriver,
        mock_cloud_storage_driver: Any,  # noqa: ARG002
    ) -> None:
        """Test get_size returns 0 on HTTP error."""
        import httpx

        with patch("httpx.Client") as mock_client_class:
            mock_client = Mock()
            mock_client.post = Mock(side_effect=httpx.HTTPError("Connection failed"))
            mock_client.__enter__ = Mock(return_value=mock_client)
            mock_client.__exit__ = Mock(return_value=None)
            mock_client_class.return_value = mock_client

            size = driver.get_size("https://cloud.griptape.ai/buckets/123/assets/test.txt")
            assert size == 0

    def test_get_size_returns_zero_on_url_extraction_failure(
        self, driver: GriptapeCloudFileDriver, mock_cloud_storage_driver: Any
    ) -> None:
        """Test get_size returns 0 when URL extraction fails."""
        mock_cloud_storage_driver.extract_workspace_path_from_cloud_url.return_value = None
        size = driver.get_size("https://cloud.griptape.ai/invalid/url")
        assert size == 0
