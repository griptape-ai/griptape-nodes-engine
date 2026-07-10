import logging
from pathlib import Path
from unittest.mock import Mock, patch

import httpx
import pytest

from griptape_nodes.drivers.storage.griptape_cloud_storage_driver import GriptapeCloudStorageDriver
from griptape_nodes.retained_mode.events.os_events import ExistingFilePolicy

# pyright: reportAttributeAccessIssue=false

# Test data constants
TEST_FILE_PATH = Path("test_file.txt")
TEST_BUCKET_ID = "test-bucket-123"
TEST_API_KEY = "test-api-key"
REQUEST_TIMEOUT_SECONDS = 60.0


class TestGriptapeCloudStorageDriverCreateSignedUploadUrl:
    """Test GriptapeCloudStorageDriver.create_signed_upload_url() method with ExistingFilePolicy support."""

    @pytest.fixture
    def cloud_storage_driver(self) -> GriptapeCloudStorageDriver:
        """Create GriptapeCloudStorageDriver instance for testing."""
        return GriptapeCloudStorageDriver(
            workspace_directory=Path("/workspace"),
            bucket_id=TEST_BUCKET_ID,
            api_key=TEST_API_KEY,
        )

    def test_create_signed_upload_url_warns_when_policy_not_overwrite(
        self, cloud_storage_driver: GriptapeCloudStorageDriver, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test that create_signed_upload_url logs warning when policy is not OVERWRITE."""
        mock_response = Mock()
        mock_response.json.return_value = {"url": "http://test.com/upload", "headers": {}}

        with (
            patch.object(cloud_storage_driver, "_create_asset"),
            patch.object(cloud_storage_driver, "_request", return_value=mock_response),
        ):
            # Clear any existing log records and set level
            caplog.clear()
            caplog.set_level(logging.WARNING)

            # Call create_signed_upload_url with FAIL policy (not OVERWRITE)
            cloud_storage_driver.create_signed_upload_url(TEST_FILE_PATH, ExistingFilePolicy.FAIL)

            # Verify warning was logged
            assert len(caplog.records) == 1
            warning_record = caplog.records[0]
            assert warning_record.levelno == logging.WARNING
            assert "Griptape Cloud storage only supports OVERWRITE policy" in warning_record.message
            assert "fail" in warning_record.message  # Policy value
            assert str(TEST_FILE_PATH) in warning_record.message  # File path

    def test_create_signed_upload_url_warns_when_policy_create_new(
        self, cloud_storage_driver: GriptapeCloudStorageDriver, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test that create_signed_upload_url logs warning when policy is CREATE_NEW."""
        mock_response = Mock()
        mock_response.json.return_value = {"url": "http://test.com/upload", "headers": {}}

        with (
            patch.object(cloud_storage_driver, "_create_asset"),
            patch.object(cloud_storage_driver, "_request", return_value=mock_response),
        ):
            # Clear any existing log records and set level
            caplog.clear()
            caplog.set_level(logging.WARNING)

            # Call create_signed_upload_url with CREATE_NEW policy (not OVERWRITE)
            cloud_storage_driver.create_signed_upload_url(TEST_FILE_PATH, ExistingFilePolicy.CREATE_NEW)

            # Verify warning was logged
            assert len(caplog.records) == 1
            warning_record = caplog.records[0]
            assert warning_record.levelno == logging.WARNING
            assert "Griptape Cloud storage only supports OVERWRITE policy" in warning_record.message
            assert "create_new" in warning_record.message  # Policy value
            assert str(TEST_FILE_PATH) in warning_record.message  # File path

    def test_create_signed_upload_url_no_warning_when_policy_overwrite(
        self, cloud_storage_driver: GriptapeCloudStorageDriver, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test that create_signed_upload_url does NOT log warning when policy is OVERWRITE."""
        mock_response = Mock()
        mock_response.json.return_value = {"url": "http://test.com/upload", "headers": {}}

        with (
            patch.object(cloud_storage_driver, "_create_asset"),
            patch.object(cloud_storage_driver, "_request", return_value=mock_response),
        ):
            # Clear any existing log records and set level
            caplog.clear()
            caplog.set_level(logging.WARNING)

            # Call create_signed_upload_url with OVERWRITE policy (supported)
            cloud_storage_driver.create_signed_upload_url(TEST_FILE_PATH, ExistingFilePolicy.OVERWRITE)

            # Verify NO warning was logged
            assert len(caplog.records) == 0

    def test_create_signed_upload_url_no_warning_with_default_policy(
        self, cloud_storage_driver: GriptapeCloudStorageDriver, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test that create_signed_upload_url does NOT log warning with default OVERWRITE policy."""
        mock_response = Mock()
        mock_response.json.return_value = {"url": "http://test.com/upload", "headers": {}}

        with (
            patch.object(cloud_storage_driver, "_create_asset"),
            patch.object(cloud_storage_driver, "_request", return_value=mock_response),
        ):
            # Clear any existing log records and set level
            caplog.clear()
            caplog.set_level(logging.WARNING)

            # Call create_signed_upload_url WITHOUT policy parameter (defaults to OVERWRITE)
            cloud_storage_driver.create_signed_upload_url(TEST_FILE_PATH)

            # Verify NO warning was logged
            assert len(caplog.records) == 0


class TestGriptapeCloudStorageDriverParseCloudAssetPath:
    """Test GriptapeCloudStorageDriver._parse_cloud_asset_path() domain validation."""

    @pytest.fixture
    def cloud_storage_driver(self) -> GriptapeCloudStorageDriver:
        """Create GriptapeCloudStorageDriver instance for testing."""
        return GriptapeCloudStorageDriver(
            workspace_directory=Path("/workspace"),
            bucket_id="test-bucket-123",
            api_key="test-api-key",
            base_url="https://cloud.griptape.ai",
        )

    def test_parse_full_url_with_matching_domain(self, cloud_storage_driver: GriptapeCloudStorageDriver) -> None:
        """Valid full URL with matching domain should extract path correctly."""
        full_url = "https://cloud.griptape.ai/buckets/9ff5bda9-8f55-409f-a1dd-d1aba54fa233/assets/days_of_christmas.zip"
        result = cloud_storage_driver._parse_cloud_asset_path(full_url)
        assert result == Path("days_of_christmas.zip")

    def test_parse_full_url_with_mismatched_domain(self, cloud_storage_driver: GriptapeCloudStorageDriver) -> None:
        """Full URL with different domain should raise ValueError."""
        full_url = "https://evil-domain.com/buckets/test-bucket/assets/file.txt"
        with pytest.raises(ValueError, match="Invalid cloud asset URL") as exc_info:
            cloud_storage_driver._parse_cloud_asset_path(full_url)
        assert "evil-domain.com" in str(exc_info.value)
        assert "cloud.griptape.ai" in str(exc_info.value)

    def test_parse_full_url_case_insensitive_domain(self, cloud_storage_driver: GriptapeCloudStorageDriver) -> None:
        """Domain comparison should be case-insensitive."""
        full_url = "https://CLOUD.GRIPTAPE.AI/buckets/test-bucket/assets/file.txt"
        result = cloud_storage_driver._parse_cloud_asset_path(full_url)
        assert result == Path("file.txt")

    def test_parse_full_url_with_http_scheme(self, cloud_storage_driver: GriptapeCloudStorageDriver) -> None:
        """http:// URLs should work if domain matches."""
        # Update base_url to use http for this test
        cloud_storage_driver.base_url = "http://cloud.griptape.ai"
        full_url = "http://cloud.griptape.ai/buckets/test-bucket/assets/file.txt"
        result = cloud_storage_driver._parse_cloud_asset_path(full_url)
        assert result == Path("file.txt")

    def test_parse_path_only_no_domain(self, cloud_storage_driver: GriptapeCloudStorageDriver) -> None:
        """Path-only format (no domain) should work as before."""
        path_only = "/buckets/test-bucket/assets/file.txt"
        result = cloud_storage_driver._parse_cloud_asset_path(path_only)
        assert result == Path("file.txt")

    def test_parse_workspace_relative_path(self, cloud_storage_driver: GriptapeCloudStorageDriver) -> None:
        """Workspace-relative paths should pass through unchanged."""
        workspace_path = "simple/path/file.txt"
        result = cloud_storage_driver._parse_cloud_asset_path(Path(workspace_path))
        assert result == Path(workspace_path)

    def test_parse_url_with_port_in_domain(self, cloud_storage_driver: GriptapeCloudStorageDriver) -> None:
        """URLs with ports should be handled correctly."""
        # Update base_url to include port
        cloud_storage_driver.base_url = "https://cloud.griptape.ai:8443"
        full_url = "https://cloud.griptape.ai:8443/buckets/test-bucket/assets/file.txt"
        result = cloud_storage_driver._parse_cloud_asset_path(full_url)
        assert result == Path("file.txt")

    def test_parse_url_with_port_mismatch(self, cloud_storage_driver: GriptapeCloudStorageDriver) -> None:
        """URLs with different ports should raise ValueError."""
        cloud_storage_driver.base_url = "https://cloud.griptape.ai:8443"
        full_url = "https://cloud.griptape.ai:9000/buckets/test-bucket/assets/file.txt"
        with pytest.raises(ValueError, match="Invalid cloud asset URL"):
            cloud_storage_driver._parse_cloud_asset_path(full_url)

    def test_error_message_content(
        self, cloud_storage_driver: GriptapeCloudStorageDriver, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Verify error message includes all necessary context."""
        caplog.clear()
        caplog.set_level(logging.ERROR)

        full_url = "https://wrong-domain.com/buckets/test-bucket/assets/file.txt"
        with pytest.raises(ValueError, match="Invalid cloud asset URL") as exc_info:
            cloud_storage_driver._parse_cloud_asset_path(full_url)

        error_message = str(exc_info.value)
        assert "Invalid cloud asset URL" in error_message
        assert "wrong-domain.com" in error_message
        assert "https://cloud.griptape.ai" in error_message
        assert "cloud.griptape.ai" in error_message

        # Verify error was also logged
        assert len(caplog.records) == 1
        assert caplog.records[0].levelno == logging.ERROR
        assert "Invalid cloud asset URL" in caplog.records[0].message

    def test_parse_full_url_with_nested_path(self, cloud_storage_driver: GriptapeCloudStorageDriver) -> None:
        """Full URL with nested path after assets should extract correctly."""
        full_url = "https://cloud.griptape.ai/buckets/test-bucket/assets/nested/path/to/file.txt"
        result = cloud_storage_driver._parse_cloud_asset_path(full_url)
        assert result == Path("nested/path/to/file.txt")


class TestGriptapeCloudStorageDriverUploadTimeout:
    """Test timeout propagation for GriptapeCloudStorageDriver.upload_file()."""

    def test_upload_file_uses_timeout_parameter(self) -> None:
        """upload_file should pass timeout parameter to signed URL upload request."""
        driver = GriptapeCloudStorageDriver(
            workspace_directory=Path("/workspace"),
            bucket_id=TEST_BUCKET_ID,
            api_key=TEST_API_KEY,
        )

        with (
            patch.object(driver, "create_signed_upload_url") as mock_create_signed_upload_url,
            patch.object(driver, "create_signed_download_url") as mock_create_signed_download_url,
            patch("griptape_nodes.drivers.storage.base_storage_driver.httpx.request") as mock_request,
        ):
            mock_create_signed_upload_url.return_value = {
                "method": "PUT",
                "url": "https://signed-upload.example.com",
                "headers": {"x-test": "1"},
                "file_path": str(TEST_FILE_PATH),
            }
            mock_create_signed_download_url.return_value = "https://signed-download.example.com"

            mock_response = Mock()
            mock_response.raise_for_status.return_value = None
            mock_request.return_value = mock_response

            result = driver.upload_file(TEST_FILE_PATH, b"test-bytes", timeout=REQUEST_TIMEOUT_SECONDS)

            assert result == "https://signed-download.example.com"
            assert mock_request.call_count == 1
            _, call_kwargs = mock_request.call_args
            assert call_kwargs["timeout"] == REQUEST_TIMEOUT_SECONDS


class TestGriptapeCloudStorageDriverBucketExists:
    """Test GriptapeCloudStorageDriver.bucket_exists() static method.

    `bucket_exists` does a direct GET on the bucket resource so a valid bucket beyond
    the first page of `list_buckets` is still recognized. A 404 means "does not exist";
    any other HTTP error is surfaced rather than being swallowed as a missing bucket.
    """

    MODULE = "griptape_nodes.drivers.storage.griptape_cloud_storage_driver"

    def test_returns_true_when_bucket_found(self) -> None:
        with patch(f"{self.MODULE}.request_with_retry") as mock_request:
            mock_request.return_value = Mock()

            result = GriptapeCloudStorageDriver.bucket_exists(
                TEST_BUCKET_ID, base_url="https://base", api_key=TEST_API_KEY
            )

        assert result is True
        args, _ = mock_request.call_args
        assert args[0] == "GET"
        assert args[1].endswith(f"/api/buckets/{TEST_BUCKET_ID}")

    def test_returns_false_on_404(self) -> None:
        response = Mock()
        response.status_code = 404
        error = httpx.HTTPStatusError("not found", request=Mock(), response=response)

        with patch(f"{self.MODULE}.request_with_retry", side_effect=error):
            result = GriptapeCloudStorageDriver.bucket_exists(
                "missing-bucket", base_url="https://base", api_key=TEST_API_KEY
            )

        assert result is False

    def test_raises_on_non_404_error(self) -> None:
        response = Mock()
        response.status_code = 500
        error = httpx.HTTPStatusError("server error", request=Mock(), response=response)

        with (
            patch(f"{self.MODULE}.request_with_retry", side_effect=error),
            pytest.raises(RuntimeError, match="Failed to check bucket"),
        ):
            GriptapeCloudStorageDriver.bucket_exists(TEST_BUCKET_ID, base_url="https://base", api_key=TEST_API_KEY)

    def test_passes_timeout_through(self) -> None:
        with patch(f"{self.MODULE}.request_with_retry") as mock_request:
            mock_request.return_value = Mock()

            GriptapeCloudStorageDriver.bucket_exists(
                TEST_BUCKET_ID, base_url="https://base", api_key=TEST_API_KEY, timeout=REQUEST_TIMEOUT_SECONDS
            )

        _, call_kwargs = mock_request.call_args
        assert call_kwargs["timeout"] == REQUEST_TIMEOUT_SECONDS


class TestGriptapeCloudStorageDriverExtractBucketId:
    """Test GriptapeCloudStorageDriver.extract_bucket_id_from_url() static method."""

    def test_extract_bucket_id_from_full_https_url(self) -> None:
        """Extract bucket_id from full HTTPS URL."""
        url = "https://cloud.griptape.ai/buckets/test-bucket-123/assets/file.txt"
        result = GriptapeCloudStorageDriver.extract_bucket_id_from_url(url)
        assert result == "test-bucket-123"

    def test_extract_bucket_id_from_full_http_url(self) -> None:
        """Extract bucket_id from full HTTP URL."""
        url = "http://cloud.griptape.ai/buckets/my-bucket/assets/path/to/file.txt"
        result = GriptapeCloudStorageDriver.extract_bucket_id_from_url(url)
        assert result == "my-bucket"

    def test_extract_bucket_id_from_path_only(self) -> None:
        """Extract bucket_id from path-only format (no domain)."""
        url = "/buckets/bucket-456/assets/nested/file.txt"
        result = GriptapeCloudStorageDriver.extract_bucket_id_from_url(url)
        assert result == "bucket-456"

    def test_extract_bucket_id_with_uuid_format(self) -> None:
        """Extract bucket_id that uses UUID format."""
        url = "https://cloud.griptape.ai/buckets/9ff5bda9-8f55-409f-a1dd-d1aba54fa233/assets/file.zip"
        result = GriptapeCloudStorageDriver.extract_bucket_id_from_url(url)
        assert result == "9ff5bda9-8f55-409f-a1dd-d1aba54fa233"

    def test_extract_bucket_id_with_nested_asset_path(self) -> None:
        """Extract bucket_id when asset path has multiple nested directories."""
        url = "https://cloud.griptape.ai/buckets/my-bucket/assets/deeply/nested/path/to/file.txt"
        result = GriptapeCloudStorageDriver.extract_bucket_id_from_url(url)
        assert result == "my-bucket"

    def test_extract_bucket_id_returns_none_for_empty_string(self) -> None:
        """Return None for empty string input."""
        result = GriptapeCloudStorageDriver.extract_bucket_id_from_url("")
        assert result is None

    def test_extract_bucket_id_returns_none_for_non_cloud_url(self) -> None:
        """Return None for regular HTTP URL without cloud pattern."""
        url = "https://example.com/some/path/file.txt"
        result = GriptapeCloudStorageDriver.extract_bucket_id_from_url(url)
        assert result is None

    def test_extract_bucket_id_returns_none_for_missing_buckets(self) -> None:
        """Return None when /buckets/ pattern is missing."""
        url = "https://cloud.griptape.ai/assets/file.txt"
        result = GriptapeCloudStorageDriver.extract_bucket_id_from_url(url)
        assert result is None

    def test_extract_bucket_id_returns_none_for_missing_assets(self) -> None:
        """Return None when /assets/ pattern is missing."""
        url = "https://cloud.griptape.ai/buckets/my-bucket/file.txt"
        result = GriptapeCloudStorageDriver.extract_bucket_id_from_url(url)
        assert result is None

    def test_extract_bucket_id_returns_none_for_local_file_path(self) -> None:
        """Return None for local file paths."""
        url = "/home/user/documents/file.txt"
        result = GriptapeCloudStorageDriver.extract_bucket_id_from_url(url)
        assert result is None

    def test_extract_bucket_id_returns_none_for_file_uri(self) -> None:
        """Return None for file:// URIs."""
        url = "file:///home/user/file.txt"
        result = GriptapeCloudStorageDriver.extract_bucket_id_from_url(url)
        assert result is None

    def test_extract_bucket_id_returns_none_when_bucket_id_empty(self) -> None:
        """Return None when bucket_id portion is empty."""
        url = "https://cloud.griptape.ai/buckets//assets/file.txt"
        result = GriptapeCloudStorageDriver.extract_bucket_id_from_url(url)
        assert result is None

    def test_extract_bucket_id_with_query_parameters(self) -> None:
        """Extract bucket_id from URL with query parameters."""
        url = "https://cloud.griptape.ai/buckets/my-bucket/assets/file.txt?version=1&cache=false"
        result = GriptapeCloudStorageDriver.extract_bucket_id_from_url(url)
        assert result == "my-bucket"

    def test_extract_bucket_id_with_url_fragment(self) -> None:
        """Extract bucket_id from URL with fragment."""
        url = "https://cloud.griptape.ai/buckets/my-bucket/assets/file.txt#section"
        result = GriptapeCloudStorageDriver.extract_bucket_id_from_url(url)
        assert result == "my-bucket"

    def test_extract_bucket_id_with_port_number(self) -> None:
        """Extract bucket_id from URL with port number."""
        url = "https://cloud.griptape.ai:8443/buckets/my-bucket/assets/file.txt"
        result = GriptapeCloudStorageDriver.extract_bucket_id_from_url(url)
        assert result == "my-bucket"

    def test_extract_bucket_id_with_special_characters(self) -> None:
        """Extract bucket_id containing special characters (hyphens, underscores)."""
        url = "https://cloud.griptape.ai/buckets/my-bucket_test-123/assets/file.txt"
        result = GriptapeCloudStorageDriver.extract_bucket_id_from_url(url)
        assert result == "my-bucket_test-123"
