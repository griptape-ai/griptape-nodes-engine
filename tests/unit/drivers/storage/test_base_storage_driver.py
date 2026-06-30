from collections.abc import Generator
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

import pytest

from griptape_nodes.drivers.storage.base_storage_driver import BaseStorageDriver, CreateSignedUploadUrlResponse
from griptape_nodes.retained_mode.events.os_events import ExistingFilePolicy

# pyright: reportAttributeAccessIssue=false

# Test data constants
TEST_FILE_DATA = b"test file content"
TEST_FILE_PATH = Path("/test/file.txt")
REQUEST_TIMEOUT_SECONDS = 45.0


class TestBaseStorageDriverUploadFile:
    """Test BaseStorageDriver.upload_file() method with ExistingFilePolicy support."""

    class ConcreteStorageDriver(BaseStorageDriver):
        """Concrete implementation for testing the abstract BaseStorageDriver."""

        def create_signed_upload_url(
            self,
            path: Path,
            _existing_file_policy: ExistingFilePolicy = ExistingFilePolicy.OVERWRITE,
            *,
            file_metadata: dict[str, str] | None = None,  # noqa: ARG002
        ) -> CreateSignedUploadUrlResponse:
            """Mock implementation of abstract method."""
            return {
                "url": f"http://test.com/upload/{path.name}",
                "file_path": str(path),
                "headers": {"Authorization": "Bearer token"},
                "method": "PUT",
            }

        def create_signed_download_url(self, path: Path) -> str:
            """Mock implementation of abstract method."""
            return f"http://test.com/download/{path.name}"

        def delete_file(self, path: Path) -> None:
            """Mock implementation of abstract method."""

        def list_files(self) -> list[str]:
            """Mock implementation of abstract method."""
            return []

        def get_asset_url(self, path: Path) -> str:
            """Mock implementation of abstract method."""
            return f"http://test.com/assets/{path.name}"

        def save_file(
            self,
            path: Path,
            _file_content: bytes,
            _existing_file_policy: ExistingFilePolicy = ExistingFilePolicy.OVERWRITE,
        ) -> str:
            """Mock implementation of abstract method."""
            return str(path)

    @pytest.fixture
    def base_storage_driver(self) -> ConcreteStorageDriver:
        """Create a concrete BaseStorageDriver instance for testing."""
        return self.ConcreteStorageDriver(Path("/workspace"))

    @pytest.fixture
    def mock_workspace_path(self) -> Generator[None, None, None]:
        """Mock ConfigManager to return /workspace for all tests."""
        with patch("griptape_nodes.drivers.storage.base_storage_driver.GriptapeNodes") as mock_griptape:
            mock_config_manager = Mock()
            mock_config_manager.workspace_path = Path("/workspace")
            mock_griptape.ConfigManager.return_value = mock_config_manager
            yield

    @pytest.fixture
    def mock_upload_response(self) -> dict[str, Any]:
        """Standard mock response for create_signed_upload_url."""
        return {
            "url": "http://test.com/upload/file.txt",
            "file_path": "file.txt",
            "headers": {"Authorization": "Bearer token"},
            "method": "PUT",
        }

    def test_upload_file_passes_existing_file_policy_to_create_signed_upload_url(
        self,
        base_storage_driver: ConcreteStorageDriver,
        mock_workspace_path: Any,  # noqa: ARG002
    ) -> None:
        """Test line 95: upload_file passes existing_file_policy to create_signed_upload_url."""
        with (
            patch.object(base_storage_driver, "create_signed_upload_url") as mock_create_url,
            patch("griptape_nodes.drivers.storage.base_storage_driver.httpx.request") as mock_request,
        ):
            # Setup mocks
            mock_create_url.return_value = {
                "url": "http://test.com/upload",
                "file_path": str(TEST_FILE_PATH),
                "headers": {"Authorization": "Bearer token"},
                "method": "PUT",
            }
            mock_response = Mock()
            mock_response.raise_for_status.return_value = None
            mock_request.return_value = mock_response

            # Call upload_file WITH existing_file_policy (tests line 95)
            base_storage_driver.upload_file(TEST_FILE_PATH, TEST_FILE_DATA, ExistingFilePolicy.FAIL)

            # Verify create_signed_upload_url was called with correct policy (line 95)
            mock_create_url.assert_called_once_with(TEST_FILE_PATH, ExistingFilePolicy.FAIL)

    def test_upload_file_default_policy_is_overwrite(
        self,
        base_storage_driver: ConcreteStorageDriver,
        mock_workspace_path: Any,  # noqa: ARG002
    ) -> None:
        """Test line 95: upload_file defaults to OVERWRITE policy when not specified."""
        with (
            patch.object(base_storage_driver, "create_signed_upload_url") as mock_create_url,
            patch("griptape_nodes.drivers.storage.base_storage_driver.httpx.request") as mock_request,
        ):
            # Setup mocks
            mock_create_url.return_value = {
                "url": "http://test.com/upload",
                "file_path": str(TEST_FILE_PATH),
                "headers": {"Authorization": "Bearer token"},
                "method": "PUT",
            }
            mock_response = Mock()
            mock_response.raise_for_status.return_value = None
            mock_request.return_value = mock_response

            # Call upload_file WITHOUT policy parameter (tests line 95 default)
            base_storage_driver.upload_file(TEST_FILE_PATH, TEST_FILE_DATA)

            # Verify create_signed_upload_url was called with default OVERWRITE policy (line 95)
            mock_create_url.assert_called_once_with(TEST_FILE_PATH, ExistingFilePolicy.OVERWRITE)

    def test_upload_file_create_new_policy(
        self,
        base_storage_driver: ConcreteStorageDriver,
        mock_workspace_path: Any,  # noqa: ARG002
    ) -> None:
        """Test line 95: upload_file passes CREATE_NEW policy correctly."""
        with (
            patch.object(base_storage_driver, "create_signed_upload_url") as mock_create_url,
            patch("griptape_nodes.drivers.storage.base_storage_driver.httpx.request") as mock_request,
        ):
            # Setup mocks
            mock_create_url.return_value = {
                "url": "http://test.com/upload",
                "file_path": str(TEST_FILE_PATH),
                "headers": {"Authorization": "Bearer token"},
                "method": "PUT",
            }
            mock_response = Mock()
            mock_response.raise_for_status.return_value = None
            mock_request.return_value = mock_response

            # Call upload_file WITH CREATE_NEW policy (tests line 95)
            base_storage_driver.upload_file(TEST_FILE_PATH, TEST_FILE_DATA, ExistingFilePolicy.CREATE_NEW)

            # Verify create_signed_upload_url was called with CREATE_NEW policy (line 95)
            mock_create_url.assert_called_once_with(TEST_FILE_PATH, ExistingFilePolicy.CREATE_NEW)

    def test_upload_file_uses_timeout_parameter(self, mock_workspace_path: Any) -> None:  # noqa: ARG002
        """upload_file should pass timeout parameter to httpx.request."""
        driver = self.ConcreteStorageDriver(Path("/workspace"))

        with (
            patch.object(driver, "create_signed_upload_url") as mock_create_url,
            patch.object(driver, "create_signed_download_url") as mock_create_download_url,
            patch("griptape_nodes.drivers.storage.base_storage_driver.httpx.request") as mock_request,
        ):
            mock_create_url.return_value = {
                "url": "http://test.com/upload",
                "file_path": str(TEST_FILE_PATH),
                "headers": {"Authorization": "Bearer token"},
                "method": "PUT",
            }
            mock_create_download_url.return_value = "http://test.com/download/file.txt"

            mock_response = Mock()
            mock_response.raise_for_status.return_value = None
            mock_request.return_value = mock_response

            result = driver.upload_file(TEST_FILE_PATH, TEST_FILE_DATA, timeout=REQUEST_TIMEOUT_SECONDS)

            assert result == "http://test.com/download/file.txt"
            _, call_kwargs = mock_request.call_args
            assert call_kwargs["timeout"] == REQUEST_TIMEOUT_SECONDS
