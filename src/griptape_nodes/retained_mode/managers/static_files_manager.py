import base64
import binascii
import logging
import threading
from pathlib import Path
from typing import NamedTuple

from xdg_base_dirs import xdg_config_home

from griptape_nodes.common.macro_parser import MacroSyntaxError, ParsedMacro
from griptape_nodes.common.project_templates import (
    FILE_EXTENSION_VARIABLE_NAME,
    FILE_NAME_BASE_VARIABLE_NAME,
)
from griptape_nodes.common.project_templates.situation import BuiltInSituation, SituationFilePolicy
from griptape_nodes.drivers.storage import StorageBackend
from griptape_nodes.drivers.storage.griptape_cloud_storage_driver import GriptapeCloudStorageDriver
from griptape_nodes.drivers.storage.local_storage_driver import LocalStorageDriver
from griptape_nodes.files.path_utils import FilenameParts
from griptape_nodes.retained_mode.events.app_events import AppInitializationComplete
from griptape_nodes.retained_mode.events.artifact_events import (
    GetPreviewForArtifactRequest,
    GetPreviewForArtifactResultSuccess,
    PreviewGenerationPolicy,
)
from griptape_nodes.retained_mode.events.os_events import ExistingFilePolicy
from griptape_nodes.retained_mode.events.project_events import (
    GetPathForMacroRequest,
    GetPathForMacroResultSuccess,
    GetSituationRequest,
    GetSituationResultSuccess,
    MacroPath,
)
from griptape_nodes.retained_mode.events.static_file_events import (
    CreateStaticFileDownloadUrlFromPathRequest,
    CreateStaticFileDownloadUrlFromPathResultSuccess,
    CreateStaticFileDownloadUrlRequest,
    CreateStaticFileDownloadUrlResultFailure,
    CreateStaticFileDownloadUrlResultSuccess,
    CreateStaticFileRequest,
    CreateStaticFileResultFailure,
    CreateStaticFileResultSuccess,
    CreateStaticFileUploadUrlRequest,
    CreateStaticFileUploadUrlResultFailure,
    CreateStaticFileUploadUrlResultSuccess,
)
from griptape_nodes.retained_mode.file_metadata.sidecar_metadata import (
    SidecarContent,
    SituationMetadata,
    SituationPolicy,
)
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
from griptape_nodes.retained_mode.managers.config_manager import ConfigManager
from griptape_nodes.retained_mode.managers.event_manager import EventManager
from griptape_nodes.retained_mode.managers.secrets_manager import SecretsManager
from griptape_nodes.servers import bind_free_socket
from griptape_nodes.servers.static import STATIC_SERVER_HOST, STATIC_SERVER_PORT, STATIC_SERVER_URL, start_static_server
from griptape_nodes.utils.url_utils import uri_to_path

logger = logging.getLogger("griptape_nodes")

USER_CONFIG_PATH = xdg_config_home() / "griptape_nodes" / "griptape_nodes_config.json"


class ResolvedStaticFilePath(NamedTuple):
    """Resolved static file path and its write policy.

    Attributes:
        path: Absolute path where the static file should be written.
        policy: How to handle an existing file at that path.
        file_metadata: Situation context to pass to WriteFileRequest for sidecar generation.
    """

    path: Path
    policy: ExistingFilePolicy
    file_metadata: SidecarContent | None = None


class StaticFilesManager:
    """A class to manage the creation and management of static files."""

    def __init__(
        self,
        config_manager: ConfigManager,
        secrets_manager: SecretsManager,
        event_manager: EventManager | None = None,
    ) -> None:
        """Initialize the StaticFilesManager.

        Args:
            config_manager: The ConfigManager instance to use for accessing the workspace path.
            event_manager: The EventManager instance to use for event handling.
            secrets_manager: The SecretsManager instance to use for accessing secrets.
        """
        self.config_manager = config_manager
        self.secrets_manager = secrets_manager

        self.storage_backend = config_manager.get_config_value("storage_backend", default=StorageBackend.LOCAL)
        workspace_directory = config_manager.workspace_path

        # Capture any explicit base URL override now; leave the underlying field None otherwise
        # so on_app_initialization_complete can derive the URL from the OS-assigned port. A set
        # value here (including one that equals the server defaults) is the signal for "user
        # override" and short-circuits the port refresh.
        configured_base_url = config_manager.get_config_value("static_server_base_url")
        self._static_server_base_url: str | None = (
            configured_base_url.rstrip("/") if configured_base_url is not None else None
        )
        base_url = (
            f"{self._static_server_base_url}{STATIC_SERVER_URL}" if self._static_server_base_url is not None else None
        )

        match self.storage_backend:
            case StorageBackend.GTC:
                bucket_id = secrets_manager.get_secret("GT_CLOUD_BUCKET_ID", should_error_on_not_found=False)

                if not bucket_id:
                    logger.warning(
                        "GT_CLOUD_BUCKET_ID secret is not available, falling back to local storage. Run `gtn init` to set it up."
                    )
                    self.storage_driver = LocalStorageDriver(workspace_directory, base_url=base_url)
                else:
                    static_files_directory = config_manager.get_config_value(
                        "static_files_directory", default="staticfiles"
                    )
                    self.storage_driver = GriptapeCloudStorageDriver(
                        workspace_directory,
                        bucket_id=bucket_id,
                        api_key=secrets_manager.get_secret("GT_CLOUD_API_KEY"),
                        static_files_directory=static_files_directory,
                    )
            case StorageBackend.LOCAL:
                self.storage_driver = LocalStorageDriver(workspace_directory, base_url=base_url)
            case _:
                msg = f"Invalid storage backend: {self.storage_backend}"
                raise ValueError(msg)

        if event_manager is not None:
            event_manager.assign_manager_to_request_type(
                CreateStaticFileRequest, self.on_handle_create_static_file_request
            )
            event_manager.assign_manager_to_request_type(
                CreateStaticFileUploadUrlRequest, self.on_handle_create_static_file_upload_url_request
            )
            event_manager.assign_manager_to_request_type(
                CreateStaticFileDownloadUrlRequest, self.on_handle_create_static_file_download_url_request
            )
            event_manager.assign_manager_to_request_type(
                CreateStaticFileDownloadUrlFromPathRequest,
                self.on_handle_create_static_file_download_url_from_path_request,
            )
            event_manager.add_listener_to_app_event(
                AppInitializationComplete,
                self.on_app_initialization_complete,
            )
            # TODO: Listen for shutdown event (https://github.com/griptape-ai/griptape-nodes/issues/2149) to stop static server

    @property
    def static_server_base_url(self) -> str:
        """Base URL for the static server.

        Resolved during ``on_app_initialization_complete`` once the server has bound to a
        port. Reading this before that event fires indicates a startup-ordering bug.
        """
        if self._static_server_base_url is None:
            msg = "static_server_base_url accessed before on_app_initialization_complete resolved it."
            raise RuntimeError(msg)
        return self._static_server_base_url

    async def _generate_preview_if_needed(self, file_path: Path) -> tuple[Path, dict | None]:
        """Generate preview for a file if needed.

        Returns (path, artifact_metadata) where path is the preview if generated/cached,
        or the original file path if no provider supports the format or preview generation fails.

        Args:
            file_path: Path to the original file

        Returns:
            Tuple of (path to serve, original source metadata or None)
        """
        extension = file_path.suffix.lstrip(".").lower()
        if not extension:
            return file_path, None

        registry = GriptapeNodes.ArtifactManager()._registry
        provider_classes = registry.get_provider_classes_by_format(extension)
        if not provider_classes:
            logger.debug("Skipping preview for unsupported file format: %s", file_path)
            return file_path, None

        provider_name = provider_classes[0].get_friendly_name()

        result = await GriptapeNodes.ahandle_request(
            GetPreviewForArtifactRequest(
                macro_path=MacroPath(ParsedMacro(str(file_path)), {}),
                artifact_provider_name=provider_name,
                preview_generation_policy=PreviewGenerationPolicy.ONLY_IF_STALE,
                failure_log_level=logging.DEBUG,
            )
        )

        if not isinstance(result, GetPreviewForArtifactResultSuccess) or not isinstance(result.paths_to_preview, str):
            logger.debug("Preview generation failed for %s: %s", file_path, result.result_details)
            return file_path, None

        preview_path = Path(result.paths_to_preview)
        logger.debug("Serving preview for %s -> %s", file_path, preview_path)
        return preview_path, result.artifact_metadata

    def on_handle_create_static_file_request(
        self,
        request: CreateStaticFileRequest,
    ) -> CreateStaticFileResultSuccess | CreateStaticFileResultFailure:
        file_name = request.file_name

        try:
            content_bytes = base64.b64decode(request.content)
        except (binascii.Error, ValueError) as e:
            msg = f"Failed to decode base64 content for file {file_name}: {e}"
            return CreateStaticFileResultFailure(error=msg, result_details=msg)

        try:
            url = self.save_static_file(content_bytes, file_name)
        except Exception as e:
            msg = f"Failed to create static file for file {file_name}: {e}"
            return CreateStaticFileResultFailure(error=msg, result_details=msg)

        return CreateStaticFileResultSuccess(url=url, result_details=f"Successfully created static file: {url}")

    def on_handle_create_static_file_upload_url_request(
        self,
        request: CreateStaticFileUploadUrlRequest,
    ) -> CreateStaticFileUploadUrlResultSuccess | CreateStaticFileUploadUrlResultFailure:
        """Handle the request to create a presigned URL for uploading a static file.

        Args:
            request: The request object containing the file name.

        Returns:
            A result object indicating success or failure.
        """
        file_name = request.file_name
        situation_name = request.situation_name

        resolved = self._resolve_static_file_path(file_name, situation_name)
        if resolved is None:
            msg = f"Attempted to create upload URL for '{file_name}'. Failed because the project template is missing the '{situation_name}' situation."
            return CreateStaticFileUploadUrlResultFailure(error=msg, result_details=msg)

        try:
            response = self.storage_driver.create_signed_upload_url(resolved.path, file_metadata=resolved.file_metadata)
        except Exception as e:
            msg = f"Failed to create presigned URL for file {file_name}: {e}"
            return CreateStaticFileUploadUrlResultFailure(error=msg, result_details=msg)

        return CreateStaticFileUploadUrlResultSuccess(
            url=response["url"],
            headers=response["headers"],
            method=response["method"],
            file_url=self.storage_driver.get_asset_url(Path(response["file_path"])),
            result_details="Successfully created static file upload URL",
        )

    def on_handle_create_static_file_download_url_request(
        self,
        request: CreateStaticFileDownloadUrlRequest,
    ) -> CreateStaticFileDownloadUrlResultSuccess | CreateStaticFileDownloadUrlResultFailure:
        """Handle the request to create a presigned URL for downloading a static file from the staticfiles directory.

        Args:
            request: The request object containing the file name.

        Returns:
            A result object indicating success or failure.
        """
        situation_name = request.situation_name
        resolved = self._resolve_static_file_path(request.file_name, situation_name)
        if resolved is None:
            msg = f"Attempted to create download URL for '{request.file_name}'. Failed because the project template is missing the '{situation_name}' situation."
            return CreateStaticFileDownloadUrlResultFailure(error=msg, result_details=msg)

        try:
            url = self.storage_driver.create_signed_download_url(resolved.path)
        except Exception as e:
            msg = f"Failed to create presigned URL for file {request.file_name}: {e}"
            return CreateStaticFileDownloadUrlResultFailure(error=msg, result_details=msg)

        return CreateStaticFileDownloadUrlResultSuccess(
            url=url,
            file_url=self.storage_driver.get_asset_url(resolved.path),
            result_details="Successfully created static file download URL",
        )

    def _create_cloud_storage_driver(self, bucket_id: str) -> GriptapeCloudStorageDriver | None:
        """Create a GriptapeCloudStorageDriver instance for the given bucket_id.

        Args:
            bucket_id: The bucket ID to use

        Returns:
            GriptapeCloudStorageDriver instance if API key is available, None otherwise
        """
        api_key = self.secrets_manager.get_secret("GT_CLOUD_API_KEY", should_error_on_not_found=False)

        if not api_key:
            return None

        workspace_directory = self.config_manager.workspace_path
        static_files_directory = self.config_manager.get_config_value("static_files_directory", default="staticfiles")

        return GriptapeCloudStorageDriver(
            workspace_directory,
            bucket_id=bucket_id,
            api_key=api_key,
            static_files_directory=static_files_directory,
        )

    async def _resolve_preview_path(self, file_path: Path, *, preview: bool) -> tuple[Path, dict | None]:
        """Return the path to serve and any source metadata, generating a preview when requested.

        Args:
            file_path: Path to the original file.
            preview: Whether to generate and serve a preview.

        Returns:
            Tuple of (path to serve, artifact metadata or None).
        """
        if not preview:
            logger.debug("Serving full image for %s", file_path)
            return file_path, None
        try:
            preview_path, artifact_metadata = await self._generate_preview_if_needed(file_path)
        except Exception as e:
            logger.warning("Preview generation failed for %s, using original: %s", file_path, e)
            return file_path, None
        if preview_path == file_path:
            logger.debug("Serving full image (no thumbnail available) for %s", file_path)
        return preview_path, artifact_metadata

    async def on_handle_create_static_file_download_url_from_path_request(
        self,
        request: CreateStaticFileDownloadUrlFromPathRequest,
    ) -> CreateStaticFileDownloadUrlFromPathResultSuccess | CreateStaticFileDownloadUrlResultFailure:
        """Handle request to create download URL from arbitrary file path.

        Args:
            request: Request containing file_path and preview parameters.

        Returns:
            Result with download URL or failure message.
        """
        file_path = request.file_path
        logger.debug("CreateStaticFileDownloadUrlFromPath: file_path=%s, preview=%s", file_path, request.preview)

        # Resolve macro paths (e.g. "{outputs}/file.png") before further processing
        try:
            parsed = ParsedMacro(file_path)
        except MacroSyntaxError as e:
            msg = f"Attempted to create download URL. Failed with file_path='{file_path}' because the path has invalid macro syntax: {e}"
            logger.warning(msg)
            return CreateStaticFileDownloadUrlResultFailure(error=msg, result_details=msg)

        if parsed.get_variables():
            resolve_result = GriptapeNodes.handle_request(
                GetPathForMacroRequest(parsed_macro=parsed, variables=request.macro_variables)
            )
            if not isinstance(resolve_result, GetPathForMacroResultSuccess):
                msg = f"Attempted to create download URL. Failed with file_path='{file_path}' because macro resolution failed: {resolve_result.result_details}"
                return CreateStaticFileDownloadUrlResultFailure(error=msg, result_details=msg)
            file_path = str(resolve_result.absolute_path)

        # Detect if this is a Griptape Cloud URL and extract bucket_id
        bucket_id = GriptapeCloudStorageDriver.extract_bucket_id_from_url(file_path)

        if bucket_id is not None:
            driver = self._create_cloud_storage_driver(bucket_id)
            if driver is None:
                msg = f"Attempted to create download URL for Griptape Cloud file. Failed with file_path='{file_path}' because GT_CLOUD_API_KEY secret is not available."
                return CreateStaticFileDownloadUrlResultFailure(error=msg, result_details=msg)

            # For cloud URLs, pass the full URL to the driver
            file_path_for_driver = Path(file_path)
        else:
            driver = self.storage_driver
            # For local paths, convert URI to path
            file_path_for_driver = Path(uri_to_path(file_path))

        # If preview requested, generate preview and get preview path + artifact metadata
        file_path_to_use, artifact_metadata = await self._resolve_preview_path(
            file_path_for_driver, preview=request.preview
        )

        try:
            url = driver.create_signed_download_url(file_path_to_use)
        except Exception as e:
            msg = f"Failed to create presigned URL for file {file_path}: {e}"
            return CreateStaticFileDownloadUrlResultFailure(error=msg, result_details=msg)

        return CreateStaticFileDownloadUrlFromPathResultSuccess(
            url=url,
            file_url=driver.get_asset_url(file_path_for_driver),
            artifact_metadata=artifact_metadata,
            result_details="Successfully created static file download URL",
        )

    def on_app_initialization_complete(self, _payload: AppInitializationComplete) -> None:
        # Start static server in daemon thread if enabled
        if isinstance(self.storage_driver, LocalStorageDriver):
            # Pre-bind to port 0 (or the configured port) so the OS assigns a free port before
            # the server thread starts. This lets us know the actual port immediately with no
            # race condition between discovering the port and uvicorn binding to it.
            sock = bind_free_socket(STATIC_SERVER_HOST, STATIC_SERVER_PORT)
            actual_port = sock.getsockname()[1]

            # When there's no explicit override, derive the base URL from the bind host and
            # the OS-assigned port. An override set in __init__ (e.g. an ngrok tunnel, reverse
            # proxy, or `ssh -L` tunnel on a different port) is taken verbatim.
            if self._static_server_base_url is None:
                self._static_server_base_url = f"http://{STATIC_SERVER_HOST}:{actual_port}"
            self.storage_driver.base_url = f"{self._static_server_base_url}{STATIC_SERVER_URL}"

            threading.Thread(target=start_static_server, args=(sock,), daemon=True, name="static-server").start()

    def save_static_file(
        self,
        data: bytes,
        file_name: str,
        existing_file_policy: ExistingFilePolicy | None = None,
        *,
        skip_metadata_injection: bool = False,
    ) -> str:
        """Saves a static file to the workspace directory.

        This is used to save files that are generated by the node, such as images or other artifacts.

        Args:
            data: The file data to save.
            file_name: The name of the file to save.
            existing_file_policy: How to handle existing files. When None, uses the policy from the
                save_static_file situation.
                - OVERWRITE: Replace existing file content
                - CREATE_NEW: Auto-generate unique filename (e.g., file_1.txt, file_2.txt)
                - FAIL: Raise FileExistsError if file exists
            skip_metadata_injection: If True, skip automatic workflow metadata injection.

        Returns:
            The URL of the saved file for UI display (with cache-busting). Note: the actual filename
            may differ from the requested file_name when using CREATE_NEW policy.

        Raises:
            FileExistsError: When existing_file_policy is FAIL and file already exists.
            RuntimeError: If the project template is missing the save_static_file situation, or if the file write fails.
        """
        resolved = self._resolve_static_file_path(file_name)
        if resolved is None:
            msg = f"Attempted to save static file '{file_name}'. Failed because the project template is missing the '{BuiltInSituation.SAVE_STATIC_FILE}' situation."
            raise RuntimeError(msg)

        file_path = resolved.path

        if existing_file_policy is None:
            effective_policy = resolved.policy
        else:
            effective_policy = existing_file_policy

        try:
            saved_path = self.storage_driver.save_file(
                file_path,
                data,
                effective_policy,
                skip_metadata_injection=skip_metadata_injection,
                file_metadata=resolved.file_metadata,
            )
        except FileExistsError:
            raise
        except Exception as e:
            msg = f"Failed to save static file {file_name}: {e}"
            logger.error(msg)
            raise RuntimeError(msg) from e
        return self.storage_driver.create_signed_download_url(Path(saved_path))

    def _resolve_static_file_path(
        self, file_name: str, situation_name: str = BuiltInSituation.SAVE_STATIC_FILE
    ) -> ResolvedStaticFilePath | None:
        """Resolve the file path for a static file using the given situation.

        Args:
            file_name: The name of the file (e.g., "output.png").
            situation_name: The situation to use for path resolution. Defaults to
                ``save_static_file``.

        Returns:
            ResolvedStaticFilePath if situation resolution succeeds, or None on failure.
        """
        situation_result = GriptapeNodes.handle_request(GetSituationRequest(situation_name=situation_name))
        if not isinstance(situation_result, GetSituationResultSuccess):
            logger.warning(
                "Project template does not include '%s' situation; static files will save to the default directory. "
                "Projects using StaticFilesManager.save_static_file require this situation in their project template.",
                situation_name,
            )
            return None

        situation = situation_result.situation

        parts = FilenameParts.from_filename(file_name)

        try:
            parsed_macro = ParsedMacro(situation.macro)
        except MacroSyntaxError as e:
            logger.warning("Failed to parse %s situation macro: %s", situation_name, e)
            return None

        macro_result = GriptapeNodes.handle_request(
            GetPathForMacroRequest(
                parsed_macro=parsed_macro,
                variables={FILE_NAME_BASE_VARIABLE_NAME: parts.stem, FILE_EXTENSION_VARIABLE_NAME: parts.extension},
            )
        )
        if not isinstance(macro_result, GetPathForMacroResultSuccess):
            logger.warning("Failed to resolve %s situation path: %s", situation_name, macro_result.result_details)
            return None

        workspace_dir = GriptapeNodes.ConfigManager().workspace_path
        try:
            # Resolve both sides to ensure drive letters match on Windows (drive-relative vs absolute paths).
            workspace_relative_path = macro_result.absolute_path.resolve().relative_to(workspace_dir.resolve())
        except ValueError:
            static_files_dir = self.config_manager.get_config_value("static_files_directory", default="staticfiles")
            workspace_relative_path = Path(static_files_dir) / file_name
            logger.warning(
                "Resolved %s situation path %s is outside workspace %s. "
                "Falling back to workspace staticfiles directory: %s",
                situation_name,
                macro_result.absolute_path,
                workspace_dir,
                workspace_relative_path,
            )

        policy = self._map_situation_policy(situation.policy.on_collision)
        variables = {FILE_NAME_BASE_VARIABLE_NAME: parts.stem, FILE_EXTENSION_VARIABLE_NAME: parts.extension}
        metadata = SidecarContent(
            situation=SituationMetadata(
                name=situation_name,
                macro=situation.macro,
                policy=SituationPolicy(
                    on_collision=situation.policy.on_collision,
                    create_dirs=situation.policy.create_dirs,
                ),
                variables={k: str(v) for k, v in variables.items()},
            ),
        )
        return ResolvedStaticFilePath(path=workspace_relative_path, policy=policy, file_metadata=metadata)

    @staticmethod
    def _map_situation_policy(situation_policy: SituationFilePolicy) -> ExistingFilePolicy:
        """Map a SituationFilePolicy to an ExistingFilePolicy.

        Args:
            situation_policy: The situation policy to map.

        Returns:
            The corresponding ExistingFilePolicy.
        """
        match situation_policy:
            case SituationFilePolicy.OVERWRITE:
                return ExistingFilePolicy.OVERWRITE
            case SituationFilePolicy.FAIL:
                return ExistingFilePolicy.FAIL
            case SituationFilePolicy.CREATE_NEW | SituationFilePolicy.PROMPT:
                return ExistingFilePolicy.CREATE_NEW
