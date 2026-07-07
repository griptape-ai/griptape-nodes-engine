import tempfile
from collections.abc import Generator
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest

from griptape_nodes.retained_mode.events.os_events import ExistingFilePolicy
from griptape_nodes.retained_mode.managers.static_files_manager import ResolvedStaticFilePath, StaticFilesManager

# pyright: reportAttributeAccessIssue=false

# Test data constants
TEST_FILE_DATA = b"test image content"
TEST_FILE_NAME = "test_image.jpg"
TEST_ALTERNATIVE_NAME = "test_image_1.jpg"


class TestStaticFilesManagerSaveStaticFile:
    """Test StaticFilesManager.save_static_file() method with ExistingFilePolicy support."""

    @pytest.fixture
    def temp_dir(self) -> Generator[Path, None, None]:
        """Create a temporary directory for testing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    @pytest.fixture
    def mock_config_manager(self) -> Mock:
        """Mock ConfigManager for StaticFilesManager initialization."""
        mock_config = Mock()
        mock_config.get_config_value.return_value = "local"
        mock_config.workspace_path = Path("/mock/workspace")
        return mock_config

    @pytest.fixture
    def mock_secrets_manager(self) -> Mock:
        """Mock SecretsManager for StaticFilesManager initialization."""
        return Mock()

    @pytest.fixture
    def mock_static_files_manager(self, mock_config_manager: Mock, mock_secrets_manager: Mock) -> StaticFilesManager:
        """Create StaticFilesManager instance with mocked dependencies."""
        with patch("griptape_nodes.retained_mode.managers.static_files_manager.LocalStorageDriver"):
            manager = StaticFilesManager(
                config_manager=mock_config_manager, secrets_manager=mock_secrets_manager, event_manager=None
            )
            # Mock the storage driver methods
            manager.storage_driver = Mock()  # type: ignore[assignment]
            return manager

    def test_save_static_file_raises_when_situation_missing(
        self,
        mock_static_files_manager: StaticFilesManager,
    ) -> None:
        """Raises RuntimeError when the save_static_file situation is not in the project template."""
        with (
            patch.object(mock_static_files_manager, "_resolve_static_file_path", return_value=None),
            pytest.raises(RuntimeError, match="save_static_file"),
        ):
            mock_static_files_manager.save_static_file(
                TEST_FILE_DATA,
                TEST_FILE_NAME,
            )

    def test_save_static_file_explicit_overwrite_policy(
        self,
        mock_static_files_manager: StaticFilesManager,
    ) -> None:
        """Explicitly passed OVERWRITE policy overrides situation policy."""
        expected_file_path = "/mock/workspace/staticfiles/test_image.jpg"
        expected_url = f"http://localhost/workspace/staticfiles/{TEST_FILE_NAME}?t=123"
        mock_static_files_manager.storage_driver.save_file.return_value = expected_file_path
        mock_static_files_manager.storage_driver.create_signed_download_url.return_value = expected_url
        situation_path = Path("/mock/workspace/staticfiles/test_image.jpg")

        with patch.object(
            mock_static_files_manager,
            "_resolve_static_file_path",
            return_value=ResolvedStaticFilePath(path=situation_path, policy=ExistingFilePolicy.CREATE_NEW),
        ):
            result = mock_static_files_manager.save_static_file(
                TEST_FILE_DATA, TEST_FILE_NAME, ExistingFilePolicy.OVERWRITE
            )

        mock_static_files_manager.storage_driver.save_file.assert_called_once()
        call_args = mock_static_files_manager.storage_driver.save_file.call_args
        assert call_args[0][1] == TEST_FILE_DATA
        assert call_args[0][2] == ExistingFilePolicy.OVERWRITE
        assert result == expected_url

    def test_save_static_file_fail_policy_success(
        self,
        mock_static_files_manager: StaticFilesManager,
    ) -> None:
        """Explicitly passed FAIL policy succeeds when file doesn't exist."""
        expected_file_path = "/mock/workspace/staticfiles/test_image.jpg"
        expected_url = f"http://localhost/workspace/staticfiles/{TEST_FILE_NAME}?t=123"
        mock_static_files_manager.storage_driver.save_file.return_value = expected_file_path
        mock_static_files_manager.storage_driver.create_signed_download_url.return_value = expected_url
        situation_path = Path("/mock/workspace/staticfiles/test_image.jpg")

        with patch.object(
            mock_static_files_manager,
            "_resolve_static_file_path",
            return_value=ResolvedStaticFilePath(path=situation_path, policy=ExistingFilePolicy.OVERWRITE),
        ):
            result = mock_static_files_manager.save_static_file(TEST_FILE_DATA, TEST_FILE_NAME, ExistingFilePolicy.FAIL)

        mock_static_files_manager.storage_driver.save_file.assert_called_once()
        call_args = mock_static_files_manager.storage_driver.save_file.call_args
        assert call_args[0][1] == TEST_FILE_DATA
        assert call_args[0][2] == ExistingFilePolicy.FAIL
        assert result == expected_url

    def test_save_static_file_fail_policy_raises_file_exists_error(
        self, mock_static_files_manager: StaticFilesManager
    ) -> None:
        """Explicitly passed FAIL policy raises FileExistsError when file exists."""
        mock_static_files_manager.storage_driver.save_file.side_effect = FileExistsError(
            f"File {TEST_FILE_NAME} already exists"
        )
        situation_path = Path("/mock/workspace/staticfiles/test_image.jpg")

        with (
            patch.object(
                mock_static_files_manager,
                "_resolve_static_file_path",
                return_value=ResolvedStaticFilePath(path=situation_path, policy=ExistingFilePolicy.OVERWRITE),
            ),
            pytest.raises(FileExistsError, match=f"File {TEST_FILE_NAME} already exists"),
        ):
            mock_static_files_manager.save_static_file(TEST_FILE_DATA, TEST_FILE_NAME, ExistingFilePolicy.FAIL)

        mock_static_files_manager.storage_driver.save_file.assert_called_once()
        call_args = mock_static_files_manager.storage_driver.save_file.call_args
        assert call_args[0][1] == TEST_FILE_DATA
        assert call_args[0][2] == ExistingFilePolicy.FAIL

    def test_save_static_file_create_new_policy(self, mock_static_files_manager: StaticFilesManager) -> None:
        """Explicitly passed CREATE_NEW policy is forwarded to the storage driver."""
        expected_file_path = f"/mock/workspace/staticfiles/{TEST_ALTERNATIVE_NAME}"
        expected_url = f"http://localhost/workspace/staticfiles/{TEST_ALTERNATIVE_NAME}?t=123"
        mock_static_files_manager.storage_driver.save_file.return_value = expected_file_path
        mock_static_files_manager.storage_driver.create_signed_download_url.return_value = expected_url
        situation_path = Path(f"/mock/workspace/staticfiles/{TEST_FILE_NAME}")

        with patch.object(
            mock_static_files_manager,
            "_resolve_static_file_path",
            return_value=ResolvedStaticFilePath(path=situation_path, policy=ExistingFilePolicy.OVERWRITE),
        ):
            result = mock_static_files_manager.save_static_file(
                TEST_FILE_DATA, TEST_FILE_NAME, ExistingFilePolicy.CREATE_NEW
            )

        mock_static_files_manager.storage_driver.save_file.assert_called_once()
        call_args = mock_static_files_manager.storage_driver.save_file.call_args
        assert call_args[0][1] == TEST_FILE_DATA
        assert call_args[0][2] == ExistingFilePolicy.CREATE_NEW
        assert TEST_ALTERNATIVE_NAME in result

    def test_save_static_file_storage_driver_exception_propagation(
        self, mock_static_files_manager: StaticFilesManager
    ) -> None:
        """Storage driver exceptions are wrapped in RuntimeError."""
        mock_static_files_manager.storage_driver.save_file.side_effect = RuntimeError(
            "Storage driver connection failed"
        )
        situation_path = Path("/mock/workspace/staticfiles/test_image.jpg")

        with (
            patch.object(
                mock_static_files_manager,
                "_resolve_static_file_path",
                return_value=ResolvedStaticFilePath(path=situation_path, policy=ExistingFilePolicy.OVERWRITE),
            ),
            pytest.raises(RuntimeError, match="Failed to save static file"),
        ):
            mock_static_files_manager.save_static_file(TEST_FILE_DATA, TEST_FILE_NAME, ExistingFilePolicy.OVERWRITE)

        mock_static_files_manager.storage_driver.save_file.assert_called_once()

    def test_save_static_file_non_file_exists_error_wrapped_in_runtime_error(
        self, mock_static_files_manager: StaticFilesManager
    ) -> None:
        """Non-FileExistsError exceptions from the storage driver are wrapped in RuntimeError."""
        mock_static_files_manager.storage_driver.save_file.side_effect = ValueError("Upload failed")
        situation_path = Path("/mock/workspace/staticfiles/test_image.jpg")

        with (
            patch.object(
                mock_static_files_manager,
                "_resolve_static_file_path",
                return_value=ResolvedStaticFilePath(path=situation_path, policy=ExistingFilePolicy.OVERWRITE),
            ),
            pytest.raises(RuntimeError, match="Failed to save static file"),
        ):
            mock_static_files_manager.save_static_file(TEST_FILE_DATA, TEST_FILE_NAME, ExistingFilePolicy.OVERWRITE)

        mock_static_files_manager.storage_driver.save_file.assert_called_once()

    def test_save_static_file_complete_success_flow(
        self,
        mock_static_files_manager: StaticFilesManager,
    ) -> None:
        """End-to-end success path: situation resolution, explicit policy, direct save."""
        expected_file_path = f"/mock/workspace/staticfiles/{TEST_ALTERNATIVE_NAME}"
        expected_url = f"http://localhost/workspace/staticfiles/{TEST_ALTERNATIVE_NAME}?t=123"
        mock_static_files_manager.storage_driver.save_file.return_value = expected_file_path
        mock_static_files_manager.storage_driver.create_signed_download_url.return_value = expected_url
        situation_path = Path(f"/mock/workspace/staticfiles/{TEST_FILE_NAME}")

        with patch.object(
            mock_static_files_manager,
            "_resolve_static_file_path",
            return_value=ResolvedStaticFilePath(path=situation_path, policy=ExistingFilePolicy.OVERWRITE),
        ):
            result = mock_static_files_manager.save_static_file(
                TEST_FILE_DATA, TEST_FILE_NAME, ExistingFilePolicy.CREATE_NEW
            )

        mock_static_files_manager.storage_driver.save_file.assert_called_once()
        call_args = mock_static_files_manager.storage_driver.save_file.call_args
        assert call_args[0][0] == situation_path
        assert call_args[0][1] == TEST_FILE_DATA
        assert call_args[0][2] == ExistingFilePolicy.CREATE_NEW
        assert result == expected_url
        assert TEST_ALTERNATIVE_NAME in result

    def test_save_static_file_situation_path_used_when_resolved(
        self, mock_static_files_manager: StaticFilesManager
    ) -> None:
        """When situation resolution succeeds, its path and policy are used."""
        expected_file_path = "/workflow/dir/staticfiles/test_image.jpg"
        expected_url = f"http://localhost/workflow/dir/staticfiles/{TEST_FILE_NAME}?t=123"
        mock_static_files_manager.storage_driver.save_file.return_value = expected_file_path
        mock_static_files_manager.storage_driver.create_signed_download_url.return_value = expected_url

        situation_path = Path("/workflow/dir/staticfiles/test_image.jpg")
        situation_policy = ExistingFilePolicy.OVERWRITE

        with patch.object(
            mock_static_files_manager,
            "_resolve_static_file_path",
            return_value=ResolvedStaticFilePath(path=situation_path, policy=situation_policy),
        ):
            result = mock_static_files_manager.save_static_file(
                TEST_FILE_DATA,
                TEST_FILE_NAME,
            )

        mock_static_files_manager.storage_driver.save_file.assert_called_once()
        call_args = mock_static_files_manager.storage_driver.save_file.call_args
        assert call_args[0][0] == situation_path
        assert call_args[0][1] == TEST_FILE_DATA
        assert call_args[0][2] == ExistingFilePolicy.OVERWRITE
        assert result == expected_url

    def test_save_static_file_explicit_policy_overrides_situation_policy(
        self, mock_static_files_manager: StaticFilesManager
    ) -> None:
        """An explicitly passed existing_file_policy overrides the situation policy."""
        expected_file_path = "/workflow/dir/staticfiles/test_image.jpg"
        expected_url = f"http://localhost/workflow/dir/staticfiles/{TEST_FILE_NAME}?t=123"
        mock_static_files_manager.storage_driver.save_file.return_value = expected_file_path
        mock_static_files_manager.storage_driver.create_signed_download_url.return_value = expected_url

        situation_path = Path("/workflow/dir/staticfiles/test_image.jpg")
        situation_policy = ExistingFilePolicy.OVERWRITE

        with patch.object(
            mock_static_files_manager,
            "_resolve_static_file_path",
            return_value=ResolvedStaticFilePath(path=situation_path, policy=situation_policy),
        ):
            result = mock_static_files_manager.save_static_file(TEST_FILE_DATA, TEST_FILE_NAME, ExistingFilePolicy.FAIL)

        mock_static_files_manager.storage_driver.save_file.assert_called_once()
        call_args = mock_static_files_manager.storage_driver.save_file.call_args
        assert call_args[0][2] == ExistingFilePolicy.FAIL
        assert result == expected_url

    def test_save_static_file_none_policy_uses_situation_policy(
        self, mock_static_files_manager: StaticFilesManager
    ) -> None:
        """When existing_file_policy is None, the situation policy is used."""
        expected_file_path = "/workflow/dir/staticfiles/test_image.jpg"
        mock_static_files_manager.storage_driver.save_file.return_value = expected_file_path

        situation_path = Path("/workflow/dir/staticfiles/test_image.jpg")
        situation_policy = ExistingFilePolicy.CREATE_NEW

        with patch.object(
            mock_static_files_manager,
            "_resolve_static_file_path",
            return_value=ResolvedStaticFilePath(path=situation_path, policy=situation_policy),
        ):
            mock_static_files_manager.save_static_file(
                TEST_FILE_DATA,
                TEST_FILE_NAME,
            )

        call_args = mock_static_files_manager.storage_driver.save_file.call_args
        assert call_args[0][2] == ExistingFilePolicy.CREATE_NEW


class TestStaticFilesManagerMapSituationPolicy:
    """Test StaticFilesManager._map_situation_policy() static method."""

    def test_map_overwrite(self) -> None:
        """SituationFilePolicy.OVERWRITE maps to ExistingFilePolicy.OVERWRITE."""
        from griptape_nodes.common.project_templates.situation import SituationFilePolicy

        result = StaticFilesManager._map_situation_policy(SituationFilePolicy.OVERWRITE)
        assert result == ExistingFilePolicy.OVERWRITE

    def test_map_fail(self) -> None:
        """SituationFilePolicy.FAIL maps to ExistingFilePolicy.FAIL."""
        from griptape_nodes.common.project_templates.situation import SituationFilePolicy

        result = StaticFilesManager._map_situation_policy(SituationFilePolicy.FAIL)
        assert result == ExistingFilePolicy.FAIL

    def test_map_create_new(self) -> None:
        """SituationFilePolicy.CREATE_NEW maps to ExistingFilePolicy.CREATE_NEW."""
        from griptape_nodes.common.project_templates.situation import SituationFilePolicy

        result = StaticFilesManager._map_situation_policy(SituationFilePolicy.CREATE_NEW)
        assert result == ExistingFilePolicy.CREATE_NEW

    def test_map_prompt(self) -> None:
        """SituationFilePolicy.PROMPT maps to ExistingFilePolicy.CREATE_NEW."""
        from griptape_nodes.common.project_templates.situation import SituationFilePolicy

        result = StaticFilesManager._map_situation_policy(SituationFilePolicy.PROMPT)
        assert result == ExistingFilePolicy.CREATE_NEW


class TestStaticFilesManagerCreateDownloadUrlFromPath:
    """Test StaticFilesManager.on_handle_create_static_file_download_url_from_path_request() method."""

    @pytest.fixture
    def mock_config_manager(self) -> Mock:
        """Mock ConfigManager for StaticFilesManager initialization."""
        mock_config = Mock()
        mock_config.get_config_value.side_effect = lambda key, default=None: {
            "storage_backend": "local",
            "workspace_directory": "/mock/workspace",
            "static_files_directory": "staticfiles",
            "static_server_base_url": "http://localhost:8124",
        }.get(key, default)
        mock_config.workspace_path = Path("/mock/workspace")
        return mock_config

    @pytest.fixture
    def mock_secrets_manager(self) -> Mock:
        """Mock SecretsManager for StaticFilesManager initialization."""
        mock = Mock()
        mock.get_secret.return_value = "test-api-key"
        return mock

    @pytest.fixture
    def mock_static_files_manager(self, mock_config_manager: Mock, mock_secrets_manager: Mock) -> StaticFilesManager:
        """Create StaticFilesManager instance with mocked dependencies."""
        with patch("griptape_nodes.retained_mode.managers.static_files_manager.LocalStorageDriver"):
            manager = StaticFilesManager(
                config_manager=mock_config_manager, secrets_manager=mock_secrets_manager, event_manager=None
            )
            manager.storage_driver = Mock()
            return manager

    @pytest.mark.asyncio
    async def test_create_download_url_from_path_local_file(
        self, mock_static_files_manager: StaticFilesManager
    ) -> None:
        """Test creating download URL from local file path."""
        from griptape_nodes.retained_mode.events.static_file_events import CreateStaticFileDownloadUrlFromPathRequest

        # Mock storage driver response
        mock_static_files_manager.storage_driver.create_signed_download_url.return_value = "http://signed-url.com"
        mock_static_files_manager.storage_driver.get_asset_url.return_value = "http://asset-url.com"

        # Test with local file path
        request = CreateStaticFileDownloadUrlFromPathRequest(file_path="file:///path/to/file.txt")

        result = await mock_static_files_manager.on_handle_create_static_file_download_url_from_path_request(request)

        # Verify success
        from griptape_nodes.retained_mode.events.static_file_events import CreateStaticFileDownloadUrlResultSuccess

        assert isinstance(result, CreateStaticFileDownloadUrlResultSuccess)
        assert result.url == "http://signed-url.com"
        assert result.file_url == "http://asset-url.com"

        # Verify local storage driver was used
        assert mock_static_files_manager.storage_driver.create_signed_download_url.called

    @pytest.mark.asyncio
    async def test_create_download_url_from_path_cloud_url(self, mock_static_files_manager: StaticFilesManager) -> None:
        """Test creating download URL from Griptape Cloud URL."""
        from griptape_nodes.drivers.storage.griptape_cloud_storage_driver import GriptapeCloudStorageDriver
        from griptape_nodes.retained_mode.events.static_file_events import CreateStaticFileDownloadUrlFromPathRequest

        # Test with cloud URL
        cloud_url = "https://cloud.griptape.ai/buckets/test-bucket-123/assets/file.txt"
        request = CreateStaticFileDownloadUrlFromPathRequest(file_path=cloud_url)

        with (
            patch.object(
                GriptapeCloudStorageDriver, "extract_bucket_id_from_url", return_value="test-bucket-123"
            ) as mock_extract,
            patch.object(mock_static_files_manager, "_create_cloud_storage_driver") as mock_create_driver,
        ):
            # Mock cloud storage driver
            mock_cloud_driver = Mock()
            mock_cloud_driver.create_signed_download_url.return_value = "http://cloud-signed-url.com"
            mock_cloud_driver.get_asset_url.return_value = "http://cloud-asset-url.com"
            mock_create_driver.return_value = mock_cloud_driver

            result = await mock_static_files_manager.on_handle_create_static_file_download_url_from_path_request(
                request
            )

            # Verify extract_bucket_id_from_url was called
            mock_extract.assert_called_once_with(cloud_url)

            # Verify cloud driver was created
            mock_create_driver.assert_called_once_with("test-bucket-123")

            # Verify success
            from griptape_nodes.retained_mode.events.static_file_events import (
                CreateStaticFileDownloadUrlResultSuccess,
            )

            assert isinstance(result, CreateStaticFileDownloadUrlResultSuccess)
            assert result.url == "http://cloud-signed-url.com"
            assert result.file_url == "http://cloud-asset-url.com"

    @pytest.mark.asyncio
    async def test_create_download_url_from_path_cloud_url_no_api_key(
        self, mock_static_files_manager: StaticFilesManager
    ) -> None:
        """Test failure when cloud URL is used but API key is not available."""
        from griptape_nodes.drivers.storage.griptape_cloud_storage_driver import GriptapeCloudStorageDriver
        from griptape_nodes.retained_mode.events.static_file_events import CreateStaticFileDownloadUrlFromPathRequest

        # Test with cloud URL
        cloud_url = "https://cloud.griptape.ai/buckets/test-bucket-123/assets/file.txt"
        request = CreateStaticFileDownloadUrlFromPathRequest(file_path=cloud_url)

        with (
            patch.object(
                GriptapeCloudStorageDriver, "extract_bucket_id_from_url", return_value="test-bucket-123"
            ) as mock_extract,
            patch.object(mock_static_files_manager, "_create_cloud_storage_driver") as mock_create_driver,
        ):
            # Mock that API key is not available
            mock_create_driver.return_value = None

            result = await mock_static_files_manager.on_handle_create_static_file_download_url_from_path_request(
                request
            )

            # Verify extract_bucket_id_from_url was called
            mock_extract.assert_called_once_with(cloud_url)

            # Verify failure
            from griptape_nodes.retained_mode.events.static_file_events import (
                CreateStaticFileDownloadUrlResultFailure,
            )

            assert isinstance(result, CreateStaticFileDownloadUrlResultFailure)
            assert "GT_CLOUD_API_KEY secret is not available" in result.error

    @pytest.mark.asyncio
    async def test_create_download_url_from_path_non_cloud_url(
        self, mock_static_files_manager: StaticFilesManager
    ) -> None:
        """Test that non-cloud URLs use local storage driver."""
        from griptape_nodes.drivers.storage.griptape_cloud_storage_driver import GriptapeCloudStorageDriver
        from griptape_nodes.retained_mode.events.static_file_events import CreateStaticFileDownloadUrlFromPathRequest

        # Mock storage driver response
        mock_static_files_manager.storage_driver.create_signed_download_url.return_value = "http://signed-url.com"
        mock_static_files_manager.storage_driver.get_asset_url.return_value = "http://asset-url.com"

        # Test with non-cloud URL (regular http URL)
        request = CreateStaticFileDownloadUrlFromPathRequest(file_path="http://example.com/file.txt")

        with patch.object(GriptapeCloudStorageDriver, "extract_bucket_id_from_url", return_value=None) as mock_extract:
            result = await mock_static_files_manager.on_handle_create_static_file_download_url_from_path_request(
                request
            )

            # Verify extract_bucket_id_from_url was called
            mock_extract.assert_called_once_with("http://example.com/file.txt")

            # Verify success with local driver
            from griptape_nodes.retained_mode.events.static_file_events import (
                CreateStaticFileDownloadUrlResultSuccess,
            )

            assert isinstance(result, CreateStaticFileDownloadUrlResultSuccess)
            assert result.url == "http://signed-url.com"

            # Verify local storage driver was used
            assert mock_static_files_manager.storage_driver.create_signed_download_url.called

    @pytest.mark.asyncio
    async def test_create_download_url_from_path_exception_handling(
        self, mock_static_files_manager: StaticFilesManager
    ) -> None:
        """Test exception handling when creating download URL fails."""
        from griptape_nodes.retained_mode.events.static_file_events import CreateStaticFileDownloadUrlFromPathRequest

        # Mock storage driver to raise exception
        mock_static_files_manager.storage_driver.create_signed_download_url.side_effect = RuntimeError(
            "Failed to create URL"
        )

        # Test with local file path
        request = CreateStaticFileDownloadUrlFromPathRequest(file_path="file:///path/to/file.txt")

        result = await mock_static_files_manager.on_handle_create_static_file_download_url_from_path_request(request)

        # Verify failure
        from griptape_nodes.retained_mode.events.static_file_events import CreateStaticFileDownloadUrlResultFailure

        assert isinstance(result, CreateStaticFileDownloadUrlResultFailure)
        assert "Failed to create presigned URL" in result.error

    @pytest.mark.asyncio
    async def test_create_download_url_from_path_macro_path(
        self, mock_static_files_manager: StaticFilesManager
    ) -> None:
        """Test creating download URL from a macro path like {outputs}/file.png."""
        from pathlib import Path

        from griptape_nodes.retained_mode.events.project_events import GetPathForMacroResultSuccess
        from griptape_nodes.retained_mode.events.static_file_events import (
            CreateStaticFileDownloadUrlFromPathRequest,
            CreateStaticFileDownloadUrlResultSuccess,
        )
        from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

        resolved_path = Path("/mock/workspace/outputs/file.png")

        mock_static_files_manager.storage_driver.create_signed_download_url.return_value = "http://signed-url.com"
        mock_static_files_manager.storage_driver.get_asset_url.return_value = "http://asset-url.com"

        request = CreateStaticFileDownloadUrlFromPathRequest(file_path="{outputs}/file.png")

        with patch.object(
            GriptapeNodes,
            "handle_request",
            return_value=GetPathForMacroResultSuccess(
                result_details="resolved",
                resolved_path=Path("outputs/file.png"),
                absolute_path=resolved_path,
            ),
        ):
            result = await mock_static_files_manager.on_handle_create_static_file_download_url_from_path_request(
                request
            )

        assert isinstance(result, CreateStaticFileDownloadUrlResultSuccess)
        # Verify the storage driver was called with the resolved path
        call_args = mock_static_files_manager.storage_driver.create_signed_download_url.call_args
        assert call_args[0][0] == Path("/mock/workspace/outputs/file.png")

    @pytest.mark.asyncio
    async def test_create_download_url_from_path_macro_path_resolution_failure(
        self, mock_static_files_manager: StaticFilesManager
    ) -> None:
        """Test failure when macro path resolution fails."""
        from griptape_nodes.retained_mode.events.project_events import (
            GetPathForMacroResultFailure,
            PathResolutionFailureReason,
        )
        from griptape_nodes.retained_mode.events.static_file_events import (
            CreateStaticFileDownloadUrlFromPathRequest,
            CreateStaticFileDownloadUrlResultFailure,
        )
        from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

        request = CreateStaticFileDownloadUrlFromPathRequest(file_path="{outputs}/file.png")

        with patch.object(
            GriptapeNodes,
            "handle_request",
            return_value=GetPathForMacroResultFailure(
                result_details="missing variables",
                failure_reason=PathResolutionFailureReason.MISSING_REQUIRED_VARIABLES,
                missing_variables={"outputs"},
            ),
        ):
            result = await mock_static_files_manager.on_handle_create_static_file_download_url_from_path_request(
                request
            )

        assert isinstance(result, CreateStaticFileDownloadUrlResultFailure)
        assert "macro resolution failed" in result.error

    @pytest.mark.asyncio
    async def test_create_download_url_from_path_macro_syntax_error(
        self, mock_static_files_manager: StaticFilesManager
    ) -> None:
        """Test failure when the file path has invalid macro syntax (e.g. unclosed brace)."""
        from griptape_nodes.retained_mode.events.static_file_events import (
            CreateStaticFileDownloadUrlFromPathRequest,
            CreateStaticFileDownloadUrlResultFailure,
        )

        # Unclosed brace triggers MacroSyntaxError
        request = CreateStaticFileDownloadUrlFromPathRequest(file_path="{outputs/file.png")

        result = await mock_static_files_manager.on_handle_create_static_file_download_url_from_path_request(request)

        assert isinstance(result, CreateStaticFileDownloadUrlResultFailure)
        assert "invalid macro syntax" in result.error

    @pytest.mark.asyncio
    async def test_create_download_url_metadata_only_returns_original_url_with_metadata(
        self, mock_static_files_manager: StaticFilesManager
    ) -> None:
        """Test that metadata_only=True returns the original URL with artifact_metadata populated."""
        from griptape_nodes.retained_mode.events.static_file_events import (
            CreateStaticFileDownloadUrlFromPathRequest,
            CreateStaticFileDownloadUrlFromPathResultSuccess,
        )

        mock_static_files_manager.storage_driver.create_signed_download_url.return_value = "http://signed-url.com"
        mock_static_files_manager.storage_driver.get_asset_url.return_value = "http://asset-url.com"

        expected_metadata = {"width": 1920, "height": 1080, "codec": "h264", "frame_rate": 29.97}

        request = CreateStaticFileDownloadUrlFromPathRequest(file_path="file:///path/to/video.mp4", metadata_only=True)

        with patch.object(
            mock_static_files_manager,
            "_extract_metadata_only",
            new_callable=AsyncMock,
            return_value=(Path("/path/to/video.mp4"), expected_metadata),
        ):
            result = await mock_static_files_manager.on_handle_create_static_file_download_url_from_path_request(
                request
            )

        assert isinstance(result, CreateStaticFileDownloadUrlFromPathResultSuccess)
        assert result.url == "http://signed-url.com"
        assert result.artifact_metadata == expected_metadata

    @pytest.mark.asyncio
    async def test_create_download_url_metadata_only_takes_precedence_over_preview(
        self, mock_static_files_manager: StaticFilesManager
    ) -> None:
        """Test that metadata_only=True wins over preview=True: original URL is returned, not a preview."""
        from griptape_nodes.retained_mode.events.static_file_events import (
            CreateStaticFileDownloadUrlFromPathRequest,
            CreateStaticFileDownloadUrlFromPathResultSuccess,
        )

        mock_static_files_manager.storage_driver.create_signed_download_url.return_value = "http://signed-url.com"
        mock_static_files_manager.storage_driver.get_asset_url.return_value = "http://asset-url.com"

        request = CreateStaticFileDownloadUrlFromPathRequest(
            file_path="file:///path/to/video.mp4", metadata_only=True, preview=True
        )

        mock_extract = AsyncMock(return_value=(Path("/path/to/video.mp4"), {"codec": "h264"}))
        mock_generate = AsyncMock(return_value=(Path("/path/to/preview.mp4"), {"codec": "h264"}))

        with (
            patch.object(mock_static_files_manager, "_extract_metadata_only", mock_extract),
            patch.object(mock_static_files_manager, "_generate_preview_if_needed", mock_generate),
        ):
            result = await mock_static_files_manager.on_handle_create_static_file_download_url_from_path_request(
                request
            )

        assert isinstance(result, CreateStaticFileDownloadUrlFromPathResultSuccess)
        mock_extract.assert_called_once()
        mock_generate.assert_not_called()
        # URL must be the original, not the preview
        call_args = mock_static_files_manager.storage_driver.create_signed_download_url.call_args
        assert call_args[0][0] == Path("/path/to/video.mp4")

    @pytest.mark.asyncio
    async def test_create_download_url_metadata_only_graceful_fallback_on_extraction_error(
        self, mock_static_files_manager: StaticFilesManager
    ) -> None:
        """Test that a metadata extraction failure still returns a valid URL with artifact_metadata=None."""
        from griptape_nodes.retained_mode.events.static_file_events import (
            CreateStaticFileDownloadUrlFromPathRequest,
            CreateStaticFileDownloadUrlFromPathResultSuccess,
        )

        mock_static_files_manager.storage_driver.create_signed_download_url.return_value = "http://signed-url.com"
        mock_static_files_manager.storage_driver.get_asset_url.return_value = "http://asset-url.com"

        request = CreateStaticFileDownloadUrlFromPathRequest(file_path="file:///path/to/video.mp4", metadata_only=True)

        with patch.object(
            mock_static_files_manager,
            "_extract_metadata_only",
            new_callable=AsyncMock,
            side_effect=RuntimeError("ffprobe not found"),
        ):
            result = await mock_static_files_manager.on_handle_create_static_file_download_url_from_path_request(
                request
            )

        assert isinstance(result, CreateStaticFileDownloadUrlFromPathResultSuccess)
        assert result.url == "http://signed-url.com"
        assert result.artifact_metadata is None


class TestStaticFilesManagerExtractMetadataOnly:
    """Test StaticFilesManager._extract_metadata_only() method."""

    @pytest.fixture
    def mock_static_files_manager(self) -> StaticFilesManager:
        mock_config = Mock()
        mock_config.get_config_value.return_value = "local"
        mock_config.workspace_path = Path("/mock/workspace")
        with patch("griptape_nodes.retained_mode.managers.static_files_manager.LocalStorageDriver"):
            manager = StaticFilesManager(config_manager=mock_config, secrets_manager=Mock(), event_manager=None)
            manager.storage_driver = Mock()
            return manager

    @pytest.mark.asyncio
    async def test_no_extension_returns_none(self, mock_static_files_manager: StaticFilesManager) -> None:
        """Files without an extension produce (original_path, None) immediately."""
        file_path = Path("/some/file_without_extension")
        result_path, metadata = await mock_static_files_manager._extract_metadata_only(file_path)
        assert result_path == file_path
        assert metadata is None

    @pytest.mark.asyncio
    async def test_unsupported_format_returns_none(self, mock_static_files_manager: StaticFilesManager) -> None:
        """An extension with no registered provider produces (original_path, None)."""
        from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

        file_path = Path("/some/document.txt")
        mock_registry = Mock()
        mock_registry.get_provider_classes_by_format.return_value = []

        with patch.object(GriptapeNodes, "ArtifactManager") as mock_artifact_manager_cls:
            mock_artifact_manager_cls.return_value._registry = mock_registry
            result_path, metadata = await mock_static_files_manager._extract_metadata_only(file_path)

        assert result_path == file_path
        assert metadata is None
        mock_registry.get_provider_classes_by_format.assert_called_once_with("txt")

    @pytest.mark.asyncio
    async def test_supported_format_returns_metadata_dict(self, mock_static_files_manager: StaticFilesManager) -> None:
        """A supported format calls get_artifact_metadata via to_thread and returns its model_dump()."""
        from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

        file_path = Path("/some/video.mp4")
        expected = {"width": 1920, "height": 1080, "codec": "h264", "frame_rate": 29.97}

        mock_metadata = Mock()
        mock_metadata.model_dump.return_value = expected

        mock_provider_class = Mock()
        mock_registry = Mock()
        mock_registry.get_provider_classes_by_format.return_value = [mock_provider_class]

        with (
            patch.object(GriptapeNodes, "ArtifactManager") as mock_artifact_manager_cls,
            patch(
                "griptape_nodes.retained_mode.managers.static_files_manager.to_thread",
                new_callable=AsyncMock,
                return_value=mock_metadata,
            ) as mock_to_thread,
        ):
            mock_artifact_manager_cls.return_value._registry = mock_registry
            result_path, metadata = await mock_static_files_manager._extract_metadata_only(file_path)

        assert result_path == file_path
        assert metadata == expected
        mock_to_thread.assert_called_once_with(mock_provider_class.get_artifact_metadata, str(file_path))

    @pytest.mark.asyncio
    async def test_provider_returning_none_yields_none_metadata(
        self, mock_static_files_manager: StaticFilesManager
    ) -> None:
        """When get_artifact_metadata returns None the method returns (original_path, None)."""
        from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

        file_path = Path("/some/image.png")

        mock_provider_class = Mock()
        mock_registry = Mock()
        mock_registry.get_provider_classes_by_format.return_value = [mock_provider_class]

        with (
            patch.object(GriptapeNodes, "ArtifactManager") as mock_artifact_manager_cls,
            patch(
                "griptape_nodes.retained_mode.managers.static_files_manager.to_thread",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            mock_artifact_manager_cls.return_value._registry = mock_registry
            result_path, metadata = await mock_static_files_manager._extract_metadata_only(file_path)

        assert result_path == file_path
        assert metadata is None


class TestStaticFilesManagerResolveStaticFilePath:
    """Test StaticFilesManager._resolve_static_file_path() method."""

    @pytest.fixture
    def mock_static_files_manager(self) -> StaticFilesManager:
        """Create a StaticFilesManager with mocked dependencies."""
        mock_config = Mock()
        mock_config.get_config_value.return_value = "local"
        mock_config.workspace_path = Path("/mock/workspace")
        with patch("griptape_nodes.retained_mode.managers.static_files_manager.LocalStorageDriver"):
            manager = StaticFilesManager(config_manager=mock_config, secrets_manager=Mock(), event_manager=None)
        return manager

    def test_resolve_returns_path_and_policy_on_success(self, mock_static_files_manager: StaticFilesManager) -> None:
        """Returns ResolvedStaticFilePath with the resolved absolute path and mapped policy."""
        from griptape_nodes.common.project_templates.situation import (
            SituationFilePolicy,
            SituationPolicy,
            SituationTemplate,
        )
        from griptape_nodes.retained_mode.events.project_events import (
            GetPathForMacroResultSuccess,
            GetSituationResultSuccess,
        )
        from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

        situation = SituationTemplate(
            name="save_static_file",
            macro="{workflow_dir?:/}staticfiles/{file_name_base}.{file_extension}",
            policy=SituationPolicy(on_collision=SituationFilePolicy.OVERWRITE, create_dirs=True),
        )
        workspace_dir = Path("/workflow")
        absolute_path = workspace_dir / "staticfiles/output.png"
        expected_relative_path = Path("staticfiles/output.png")

        def handle_request(request: object) -> object:
            from griptape_nodes.retained_mode.events.project_events import (
                GetPathForMacroRequest,
                GetSituationRequest,
            )

            if isinstance(request, GetSituationRequest):
                return GetSituationResultSuccess(situation=situation, result_details="ok")
            if isinstance(request, GetPathForMacroRequest):
                return GetPathForMacroResultSuccess(
                    resolved_path=absolute_path,
                    absolute_path=absolute_path,
                    result_details="ok",
                )
            msg = f"Unexpected request: {request}"
            raise AssertionError(msg)

        mock_config_manager = Mock()
        mock_config_manager.get_config_value.return_value = str(workspace_dir)
        mock_config_manager.workspace_path = workspace_dir

        with (
            patch.object(GriptapeNodes, "handle_request", side_effect=handle_request),
            patch.object(GriptapeNodes, "ConfigManager", return_value=mock_config_manager),
        ):
            result = mock_static_files_manager._resolve_static_file_path("output.png")

        assert result is not None
        assert result.path == expected_relative_path
        assert result.policy == ExistingFilePolicy.OVERWRITE

    def test_resolve_returns_none_and_warns_when_situation_not_found(
        self, mock_static_files_manager: StaticFilesManager, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Returns None and logs a warning when the save_static_file situation is missing."""
        import logging

        from griptape_nodes.retained_mode.events.project_events import GetSituationResultFailure
        from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

        with (
            patch.object(
                GriptapeNodes,
                "handle_request",
                return_value=GetSituationResultFailure(result_details="situation not found"),
            ),
            caplog.at_level(logging.WARNING, logger="griptape_nodes"),
        ):
            result = mock_static_files_manager._resolve_static_file_path("output.png")

        assert result is None
        assert "save_static_file" in caplog.text
        assert "StaticFilesManager.save_static_file" in caplog.text

    def test_resolve_returns_none_and_warns_when_macro_parsing_fails(
        self, mock_static_files_manager: StaticFilesManager, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Returns None and logs a warning when the situation macro cannot be parsed."""
        import logging

        from griptape_nodes.common.macro_parser import MacroSyntaxError
        from griptape_nodes.common.project_templates.situation import (
            SituationFilePolicy,
            SituationPolicy,
            SituationTemplate,
        )
        from griptape_nodes.retained_mode.events.project_events import GetSituationResultSuccess
        from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

        situation = SituationTemplate(
            name="save_static_file",
            macro="valid/{file_name_base}.{file_extension}",
            policy=SituationPolicy(on_collision=SituationFilePolicy.OVERWRITE, create_dirs=True),
        )

        with (
            patch.object(
                GriptapeNodes,
                "handle_request",
                return_value=GetSituationResultSuccess(situation=situation, result_details="ok"),
            ),
            patch(
                "griptape_nodes.retained_mode.managers.static_files_manager.ParsedMacro",
                side_effect=MacroSyntaxError("bad macro"),
            ),
            caplog.at_level(logging.WARNING, logger="griptape_nodes"),
        ):
            result = mock_static_files_manager._resolve_static_file_path("output.png")

        assert result is None
        assert "save_static_file" in caplog.text

    def test_resolve_returns_none_and_warns_when_path_resolution_fails(
        self, mock_static_files_manager: StaticFilesManager, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Returns None and logs a warning when macro path resolution fails."""
        import logging

        from griptape_nodes.common.project_templates.situation import (
            SituationFilePolicy,
            SituationPolicy,
            SituationTemplate,
        )
        from griptape_nodes.retained_mode.events.project_events import (
            GetPathForMacroResultFailure,
            GetSituationResultSuccess,
            PathResolutionFailureReason,
        )
        from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

        situation = SituationTemplate(
            name="save_static_file",
            macro="{workflow_dir?:/}staticfiles/{file_name_base}.{file_extension}",
            policy=SituationPolicy(on_collision=SituationFilePolicy.OVERWRITE, create_dirs=True),
        )

        def handle_request(request: object) -> object:
            from griptape_nodes.retained_mode.events.project_events import (
                GetPathForMacroRequest,
                GetSituationRequest,
            )

            if isinstance(request, GetSituationRequest):
                return GetSituationResultSuccess(situation=situation, result_details="ok")
            if isinstance(request, GetPathForMacroRequest):
                return GetPathForMacroResultFailure(
                    result_details="could not resolve",
                    failure_reason=PathResolutionFailureReason.MACRO_RESOLUTION_ERROR,
                    missing_variables=set(),
                )
            msg = f"Unexpected request: {request}"
            raise AssertionError(msg)

        with (
            patch.object(GriptapeNodes, "handle_request", side_effect=handle_request),
            caplog.at_level(logging.WARNING, logger="griptape_nodes"),
        ):
            result = mock_static_files_manager._resolve_static_file_path("output.png")

        assert result is None
        assert "save_static_file" in caplog.text

    def test_resolve_falls_back_to_workspace_staticfiles_when_path_is_outside_workspace(
        self, mock_static_files_manager: StaticFilesManager, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Falls back to workspace staticfiles directory when resolved path is outside workspace."""
        import logging

        from griptape_nodes.common.project_templates.situation import (
            SituationFilePolicy,
            SituationPolicy,
            SituationTemplate,
        )
        from griptape_nodes.retained_mode.events.project_events import (
            GetPathForMacroResultSuccess,
            GetSituationResultSuccess,
        )
        from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

        situation = SituationTemplate(
            name="save_static_file",
            macro="{workflow_dir?:/}staticfiles/{file_name_base}.{file_extension}",
            policy=SituationPolicy(on_collision=SituationFilePolicy.OVERWRITE, create_dirs=True),
        )
        workspace_dir = Path("/mock/workspace")
        # Workflow is outside the workspace (e.g., saved in Downloads)
        outside_path = Path("/Users/user/Downloads/staticfiles/output.png")

        def handle_request(request: object) -> object:
            from griptape_nodes.retained_mode.events.project_events import (
                GetPathForMacroRequest,
                GetSituationRequest,
            )

            if isinstance(request, GetSituationRequest):
                return GetSituationResultSuccess(situation=situation, result_details="ok")
            if isinstance(request, GetPathForMacroRequest):
                return GetPathForMacroResultSuccess(
                    resolved_path=outside_path,
                    absolute_path=outside_path,
                    result_details="ok",
                )
            msg = f"Unexpected request: {request}"
            raise AssertionError(msg)

        mock_config_manager = Mock()
        mock_config_manager.get_config_value.side_effect = lambda key, **kwargs: (
            str(workspace_dir) if key == "workspace_directory" else kwargs.get("default", "staticfiles")
        )
        mock_config_manager.workspace_path = workspace_dir
        mock_static_files_manager.config_manager = mock_config_manager

        with (
            patch.object(GriptapeNodes, "handle_request", side_effect=handle_request),
            patch.object(GriptapeNodes, "ConfigManager", return_value=mock_config_manager),
            caplog.at_level(logging.WARNING, logger="griptape_nodes"),
        ):
            result = mock_static_files_manager._resolve_static_file_path("output.png")

        assert result is not None
        assert result.path == Path("staticfiles/output.png")
        assert result.policy == ExistingFilePolicy.OVERWRITE
        assert "outside workspace" in caplog.text


class TestStaticFilesManagerCreateUploadUrl:
    """Test StaticFilesManager.on_handle_create_static_file_upload_url_request()."""

    @pytest.fixture
    def mock_static_files_manager(self) -> StaticFilesManager:
        """Create a StaticFilesManager with mocked dependencies."""
        mock_config = Mock()
        mock_config.get_config_value.return_value = "local"
        mock_config.workspace_path = Path("/mock/workspace")
        with patch("griptape_nodes.retained_mode.managers.static_files_manager.LocalStorageDriver"):
            manager = StaticFilesManager(config_manager=mock_config, secrets_manager=Mock(), event_manager=None)
        manager.storage_driver = Mock()  # type: ignore[assignment]
        return manager

    def test_upload_url_success_passes_file_metadata_to_storage_driver(
        self, mock_static_files_manager: StaticFilesManager
    ) -> None:
        """on_handle_create_static_file_upload_url_request passes file_metadata to create_signed_upload_url."""
        from griptape_nodes.retained_mode.events.static_file_events import (
            CreateStaticFileUploadUrlRequest,
            CreateStaticFileUploadUrlResultSuccess,
        )
        from griptape_nodes.retained_mode.file_metadata.sidecar_metadata import SidecarContent
        from griptape_nodes.retained_mode.managers.static_files_manager import ResolvedStaticFilePath

        resolved_path = Path("staticfiles/image.png")
        mock_metadata = SidecarContent()
        resolved = ResolvedStaticFilePath(
            path=resolved_path, policy=ExistingFilePolicy.OVERWRITE, file_metadata=mock_metadata
        )

        mock_static_files_manager.storage_driver.create_signed_upload_url.return_value = {
            "url": "https://example.com/upload",
            "headers": {},
            "method": "PUT",
            "file_path": "staticfiles/image.png",
        }
        mock_static_files_manager.storage_driver.get_asset_url.return_value = "https://example.com/image.png"

        with patch.object(mock_static_files_manager, "_resolve_static_file_path", return_value=resolved):
            result = mock_static_files_manager.on_handle_create_static_file_upload_url_request(
                CreateStaticFileUploadUrlRequest(file_name="image.png")
            )

        assert isinstance(result, CreateStaticFileUploadUrlResultSuccess)
        mock_static_files_manager.storage_driver.create_signed_upload_url.assert_called_once_with(
            resolved_path, file_metadata=mock_metadata
        )


class TestStaticFilesManagerBaseUrlTrailingSlash:
    """Test that static_server_base_url trailing slashes are stripped."""

    @pytest.fixture
    def mock_secrets_manager(self) -> Mock:
        """Mock SecretsManager for StaticFilesManager initialization."""
        return Mock()

    @pytest.mark.parametrize(
        ("configured_url", "expected_url"),
        [
            ("http://localhost:8124", "http://localhost:8124"),
            ("http://localhost:8124/", "http://localhost:8124"),
            ("http://localhost:8124///", "http://localhost:8124"),
            ("https://my-tunnel.ngrok.io/", "https://my-tunnel.ngrok.io"),
        ],
    )
    def test_init_strips_trailing_slashes(
        self, mock_secrets_manager: Mock, configured_url: str, expected_url: str
    ) -> None:
        """Trailing slashes on static_server_base_url are stripped during init."""
        mock_config = Mock()
        mock_config.get_config_value.side_effect = lambda key, default=None: {
            "storage_backend": "local",
            "static_server_base_url": configured_url,
        }.get(key, default)
        mock_config.workspace_path = Path("/mock/workspace")

        with patch("griptape_nodes.retained_mode.managers.static_files_manager.LocalStorageDriver"):
            manager = StaticFilesManager(
                config_manager=mock_config, secrets_manager=mock_secrets_manager, event_manager=None
            )

        assert manager.static_server_base_url == expected_url


class TestStaticFilesManagerOnAppInitializationComplete:
    """Test port handling in on_app_initialization_complete.

    The engine binds a free port at startup and may rewrite `static_server_base_url`
    to reflect the OS-assigned port. That rewrite must only happen when the user has
    not provided an explicit override, otherwise it clobbers tunnel configurations
    (e.g. `ssh -L 8888:localhost:8124`, ngrok, reverse proxies).
    """

    @pytest.fixture
    def mock_secrets_manager(self) -> Mock:
        return Mock()

    def _build_manager(self, mock_secrets_manager: Mock, configured_url: str | None) -> StaticFilesManager:
        from griptape_nodes.drivers.storage.local_storage_driver import LocalStorageDriver

        mock_config = Mock()
        mock_config.get_config_value.side_effect = lambda key, default=None: {
            "storage_backend": "local",
            "static_server_base_url": configured_url,
        }.get(key, default)
        mock_config.workspace_path = Path("/mock/workspace")

        with patch("griptape_nodes.retained_mode.managers.static_files_manager.LocalStorageDriver") as driver_cls:
            driver_cls.return_value = Mock(spec=LocalStorageDriver)
            manager = StaticFilesManager(
                config_manager=mock_config, secrets_manager=mock_secrets_manager, event_manager=None
            )
        return manager

    def _invoke_initialization(self, manager: StaticFilesManager, actual_port: int) -> None:
        from griptape_nodes.retained_mode.events.app_events import AppInitializationComplete

        mock_sock = Mock()
        mock_sock.getsockname.return_value = ("127.0.0.1", actual_port)

        with (
            patch(
                "griptape_nodes.retained_mode.managers.static_files_manager.bind_free_socket",
                return_value=mock_sock,
            ),
            patch("griptape_nodes.retained_mode.managers.static_files_manager.threading.Thread"),
        ):
            manager.on_app_initialization_complete(AppInitializationComplete())

    def test_unset_base_url_rewritten_to_actual_port(self, mock_secrets_manager: Mock) -> None:
        """When no override is configured, the OS-assigned port replaces the server's default port."""
        manager = self._build_manager(mock_secrets_manager, None)

        self._invoke_initialization(manager, actual_port=54321)

        assert manager.static_server_base_url == "http://localhost:54321"
        assert manager.storage_driver.base_url == "http://localhost:54321/workspace"

    def test_custom_port_on_localhost_preserved(self, mock_secrets_manager: Mock) -> None:
        """A custom port (e.g. from an `ssh -L 8888:localhost:8124` tunnel) must not be overwritten."""
        manager = self._build_manager(mock_secrets_manager, "http://localhost:8888")

        self._invoke_initialization(manager, actual_port=54321)

        assert manager.static_server_base_url == "http://localhost:8888"
        assert manager.storage_driver.base_url == "http://localhost:8888/workspace"

    def test_custom_hostname_preserved(self, mock_secrets_manager: Mock) -> None:
        """A custom host (e.g. an ngrok tunnel) must not be overwritten."""
        manager = self._build_manager(mock_secrets_manager, "https://my-tunnel.ngrok.io")

        self._invoke_initialization(manager, actual_port=54321)

        assert manager.static_server_base_url == "https://my-tunnel.ngrok.io"
        assert manager.storage_driver.base_url == "https://my-tunnel.ngrok.io/workspace"

    def test_override_matching_defaults_is_still_preserved(self, mock_secrets_manager: Mock) -> None:
        """An explicit override is respected even when it happens to equal the server defaults."""
        manager = self._build_manager(mock_secrets_manager, "http://localhost:8124")

        self._invoke_initialization(manager, actual_port=54321)

        assert manager.static_server_base_url == "http://localhost:8124"
        assert manager.storage_driver.base_url == "http://localhost:8124/workspace"

    def test_access_before_initialization_complete_raises(self, mock_secrets_manager: Mock) -> None:
        """Reading the property before on_app_initialization_complete resolves it is a startup bug."""
        manager = self._build_manager(mock_secrets_manager, None)

        with pytest.raises(RuntimeError, match="static_server_base_url accessed before"):
            _ = manager.static_server_base_url
