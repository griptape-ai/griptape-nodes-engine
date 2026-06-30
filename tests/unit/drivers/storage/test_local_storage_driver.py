from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

import pytest

from griptape_nodes.drivers.storage.local_storage_driver import LocalStorageDriver
from griptape_nodes.retained_mode.events.os_events import (
    ExistingFilePolicy,
    WriteFileRequest,
    WriteFileResultFailure,
    WriteFileResultSuccess,
)

# pyright: reportAttributeAccessIssue=false

# Test data constants
TEST_FILE_PATH = Path("test_file.txt")
TEST_RESOLVED_PATH = Path("test_file_1.txt")


class TestLocalStorageDriverCreateSignedUploadUrl:
    """Test LocalStorageDriver.create_signed_upload_url() method with ExistingFilePolicy support."""

    @pytest.fixture
    def local_storage_driver(self) -> LocalStorageDriver:
        """Create LocalStorageDriver instance for testing."""
        return LocalStorageDriver(Path("/workspace"))

    @pytest.fixture
    def mock_os_manager(self) -> Mock:
        """Mock OSManager for testing."""
        return Mock()

    @pytest.fixture
    def mock_write_success_result(self) -> WriteFileResultSuccess:
        """Mock successful WriteFileRequest result."""
        # OSManager always returns absolute paths, so return absolute path that will resolve to expected relative path
        absolute_resolved_path = Path("/workspace") / TEST_RESOLVED_PATH
        return WriteFileResultSuccess(
            final_file_path=str(absolute_resolved_path), bytes_written=0, result_details="Success"
        )

    @pytest.fixture
    def mock_write_failure_result(self) -> WriteFileResultFailure:
        """Mock failed WriteFileRequest result."""
        from griptape_nodes.retained_mode.events.base_events import ResultDetails
        from griptape_nodes.retained_mode.events.os_events import FileIOFailureReason

        return WriteFileResultFailure(
            failure_reason=FileIOFailureReason.POLICY_NO_OVERWRITE,
            result_details=ResultDetails(message="File already exists", level=40),
        )

    def test_create_signed_upload_url_delegates_to_os_manager_with_policy(
        self, local_storage_driver: LocalStorageDriver, mock_os_manager: Mock, mock_write_success_result: Any
    ) -> None:
        """Test that create_signed_upload_url delegates to OSManager with correct policy."""
        with (
            patch("griptape_nodes.drivers.storage.local_storage_driver.GriptapeNodes") as mock_griptape,
            patch("griptape_nodes.drivers.storage.local_storage_driver.httpx.post") as mock_post,
        ):
            # Setup mocks
            mock_griptape.OSManager.return_value = mock_os_manager
            mock_os_manager.on_write_file_request.return_value = mock_write_success_result
            mock_post_response = Mock()
            mock_post_response.raise_for_status.return_value = None
            mock_post_response.json.return_value = {"url": "http://test.com/upload"}
            mock_post.return_value = mock_post_response

            # Call create_signed_upload_url with FAIL policy
            local_storage_driver.create_signed_upload_url(TEST_FILE_PATH, ExistingFilePolicy.FAIL)

            # Verify OSManager was called with correct WriteFileRequest
            mock_os_manager.on_write_file_request.assert_called_once()
            call_args = mock_os_manager.on_write_file_request.call_args[0][0]
            assert isinstance(call_args, WriteFileRequest)
            # LocalStorageDriver converts relative paths to absolute before calling OSManager
            # resolve_workspace_path normalizes the path by calling .resolve()
            expected_absolute_path = (Path("/workspace") / TEST_FILE_PATH).resolve()
            assert call_args.file_path == str(expected_absolute_path)
            assert call_args.content == b""  # Empty content for URL generation
            assert call_args.existing_file_policy == ExistingFilePolicy.FAIL

    def test_create_signed_upload_url_uses_resolved_path_from_os_manager(
        self, local_storage_driver: LocalStorageDriver, mock_os_manager: Mock, mock_write_success_result: Any
    ) -> None:
        """Test that create_signed_upload_url uses resolved filename from OSManager."""
        with (
            patch("griptape_nodes.drivers.storage.local_storage_driver.GriptapeNodes") as mock_griptape,
            patch("griptape_nodes.drivers.storage.local_storage_driver.httpx.post") as mock_post,
        ):
            # Setup mocks
            mock_griptape.OSManager.return_value = mock_os_manager
            mock_os_manager.on_write_file_request.return_value = mock_write_success_result
            mock_post_response = Mock()
            mock_post_response.raise_for_status.return_value = None
            mock_post_response.json.return_value = {"url": "http://test.com/upload"}
            mock_post.return_value = mock_post_response

            # Call create_signed_upload_url with CREATE_NEW policy (will get resolved filename)
            local_storage_driver.create_signed_upload_url(TEST_FILE_PATH, ExistingFilePolicy.CREATE_NEW)

            # Verify resolved path was used in HTTP request
            mock_post.assert_called_once()
            call_kwargs = mock_post.call_args[1]
            assert call_kwargs["json"]["file_path"] == str(TEST_RESOLVED_PATH)

    def test_create_signed_upload_url_raises_file_exists_error_on_write_failure(
        self, local_storage_driver: LocalStorageDriver, mock_os_manager: Mock, mock_write_failure_result: Any
    ) -> None:
        """Test that create_signed_upload_url raises FileExistsError when WriteFileRequest fails."""
        with patch("griptape_nodes.drivers.storage.local_storage_driver.GriptapeNodes") as mock_griptape:
            # Setup mocks
            mock_griptape.OSManager.return_value = mock_os_manager
            mock_os_manager.on_write_file_request.return_value = mock_write_failure_result

            # Call create_signed_upload_url with FAIL policy on existing file
            with pytest.raises(FileExistsError, match="WriteFileRequest failed"):
                local_storage_driver.create_signed_upload_url(TEST_FILE_PATH, ExistingFilePolicy.FAIL)

            # Verify OSManager was called but HTTP request was not made
            mock_os_manager.on_write_file_request.assert_called_once()

    def test_create_signed_upload_url_default_overwrite_policy(
        self, local_storage_driver: LocalStorageDriver, mock_os_manager: Mock, mock_write_success_result: Any
    ) -> None:
        """Test that create_signed_upload_url defaults to OVERWRITE policy."""
        with (
            patch("griptape_nodes.drivers.storage.local_storage_driver.GriptapeNodes") as mock_griptape,
            patch("griptape_nodes.drivers.storage.local_storage_driver.httpx.post") as mock_post,
        ):
            # Setup mocks
            mock_griptape.OSManager.return_value = mock_os_manager
            mock_os_manager.on_write_file_request.return_value = mock_write_success_result
            mock_post_response = Mock()
            mock_post_response.raise_for_status.return_value = None
            mock_post_response.json.return_value = {"url": "http://test.com/upload"}
            mock_post.return_value = mock_post_response

            # Call create_signed_upload_url WITHOUT policy parameter
            local_storage_driver.create_signed_upload_url(TEST_FILE_PATH)

            # Verify OSManager was called with default OVERWRITE policy
            mock_os_manager.on_write_file_request.assert_called_once()
            call_args = mock_os_manager.on_write_file_request.call_args[0][0]
            assert call_args.existing_file_policy == ExistingFilePolicy.OVERWRITE


class TestLocalStorageDriverCreateSignedDownloadUrl:
    """Test LocalStorageDriver.create_signed_download_url() method."""

    @pytest.fixture
    def local_storage_driver(self) -> LocalStorageDriver:
        """Create LocalStorageDriver instance for testing."""
        return LocalStorageDriver(Path("/workspace"))

    def test_internal_file_uses_workspace_relative_url(self, local_storage_driver: LocalStorageDriver) -> None:
        """Internal files should produce a workspace-relative URL."""
        with patch("griptape_nodes.drivers.storage.local_storage_driver.time") as mock_time:
            mock_time.time.return_value = 1000
            url = local_storage_driver.create_signed_download_url(Path("/workspace/images/photo.png"))

        assert url == "http://localhost:8124/workspace/images/photo.png?t=1000"

    def test_external_unix_file_uses_external_url(self, local_storage_driver: LocalStorageDriver) -> None:
        """External Unix files should produce a /external/ URL with forward slashes."""
        with (
            patch("griptape_nodes.drivers.storage.local_storage_driver.time") as mock_time,
            patch("griptape_nodes.drivers.storage.local_storage_driver.resolve_workspace_path") as mock_resolve,
        ):
            mock_time.time.return_value = 1000
            external_path = Path("/external/video.mp4")
            mock_resolve.return_value = external_path
            url = local_storage_driver.create_signed_download_url(external_path)

        assert url == "http://localhost:8124/external/external/video.mp4?t=1000"

    def test_external_windows_file_uses_forward_slashes_in_url(self, local_storage_driver: LocalStorageDriver) -> None:
        """External Windows-style files should produce a URL with forward slashes, not backslashes."""
        with patch("griptape_nodes.drivers.storage.local_storage_driver.time") as mock_time:
            mock_time.time.return_value = 1000

            # Simulate a Windows absolute path by patching resolve_workspace_path
            # to return a PurePosixPath that mimics what a Windows Path would look like after as_posix()
            with patch("griptape_nodes.drivers.storage.local_storage_driver.resolve_workspace_path") as mock_resolve:
                # On Windows, Path("C:/Users/foo/image.png") has str() = "C:\\Users\\foo\\image.png"
                # but .as_posix() = "C:/Users/foo/image.png"
                mock_path = Mock()
                mock_path.relative_to.side_effect = ValueError("not relative")
                mock_path.as_posix.return_value = "C:/Users/foo/image.png"
                mock_path.__str__ = lambda _self: "C:\\Users\\foo\\image.png"
                mock_resolve.return_value = mock_path
                url = local_storage_driver.create_signed_download_url(Path("C:/Users/foo/image.png"))

        # The URL must use forward slashes and not have backslashes
        assert "\\" not in url
        assert "C:/Users/foo/image.png" in url
        assert url == "http://localhost:8124/external/C:/Users/foo/image.png?t=1000"
