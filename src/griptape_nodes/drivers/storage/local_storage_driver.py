import logging
import time
from pathlib import Path
from urllib.parse import urljoin

import httpx

from griptape_nodes.common.project_templates.situation import BuiltInSituation
from griptape_nodes.drivers.storage.base_storage_driver import BaseStorageDriver, CreateSignedUploadUrlResponse
from griptape_nodes.files.file import FileLoadError
from griptape_nodes.files.project_file import ProjectFileDestination
from griptape_nodes.retained_mode.events.os_events import ExistingFilePolicy, WriteFileRequest, WriteFileResultSuccess
from griptape_nodes.retained_mode.file_metadata.sidecar_metadata import SidecarContent
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
from griptape_nodes.utils import resolve_workspace_path

logger = logging.getLogger("griptape_nodes")


class LocalStorageDriver(BaseStorageDriver):
    """Stores files using the engine's local static server."""

    def __init__(self, workspace_directory: Path, base_url: str | None = None) -> None:
        """Initialize the LocalStorageDriver.

        Args:
            workspace_directory: The base workspace directory path.
            base_url: The base URL for the static file server. If not provided, it will be constructed
        """
        super().__init__(workspace_directory)

        from griptape_nodes.servers.static import (
            STATIC_SERVER_ENABLED,
            STATIC_SERVER_HOST,
            STATIC_SERVER_PORT,
            STATIC_SERVER_URL,
        )

        if not STATIC_SERVER_ENABLED:
            msg = "Static server is not enabled. Please set STATIC_SERVER_ENABLED to True."
            raise ValueError(msg)
        if base_url is None:
            # Default to localhost - the storage driver creator can pass a proxy URL if needed
            self.base_url = f"http://{STATIC_SERVER_HOST}:{STATIC_SERVER_PORT}{STATIC_SERVER_URL}"
        else:
            self.base_url = base_url

    def create_signed_upload_url(
        self,
        path: Path,
        existing_file_policy: ExistingFilePolicy = ExistingFilePolicy.OVERWRITE,
        *,
        file_metadata: SidecarContent | None = None,  # noqa: ARG002
    ) -> CreateSignedUploadUrlResponse:
        # Read current workspace path fresh from ConfigManager
        current_workspace = GriptapeNodes.ConfigManager().workspace_path

        # on_write_file_request seems to work most reliably with an absolute path.
        absolute_path = resolve_workspace_path(path, current_workspace)

        # Always delegate to OSManager for file path resolution and policy handling.
        # Creating an empty file before the upload url gives us a chance to claim ownership
        # of that particular file when creating the upload url. The file policy is not
        # checked when actually uploading the file, it will always overwrite.
        os_manager = GriptapeNodes.OSManager()
        write_request = WriteFileRequest(
            file_path=str(absolute_path),
            content=b"",  # Empty content for URL generation
            existing_file_policy=existing_file_policy,
            skip_metadata_injection=True,
        )
        result = os_manager.on_write_file_request(write_request)

        if not result.succeeded():
            msg = f"WriteFileRequest failed: {result.result_details}"
            raise FileExistsError(msg)

        # Use the resolved filename from OSManager
        # Type checker: result is WriteFileResultSuccess when succeeded() is True
        resolved_path = Path(result.final_file_path)  # type: ignore[attr-defined]

        # WriteFileRequest always returns an absolute path; convert back to workspace-relative
        # since the static server upload handler prepends the workspace directory itself.
        # Resolve both sides to ensure drive letters match on Windows (drive-relative vs absolute paths).
        resolved_path = resolved_path.resolve().relative_to(current_workspace.resolve())

        static_url = urljoin(self.base_url, "/static-upload-urls")
        try:
            response = httpx.post(static_url, json={"file_path": str(resolved_path)})
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            msg = f"Failed to create upload URL for file {resolved_path}: {e}"
            logger.error(msg)
            raise RuntimeError(msg) from e

        response_data = response.json()
        url = response_data.get("url")
        if url is None:
            msg = f"Failed to get upload URL for file {resolved_path}: {response_data}"
            logger.error(msg)
            raise ValueError(msg)

        return {
            "url": url,
            "headers": response_data.get("headers", {}),
            "method": "PUT",
            "file_path": str(resolved_path),
        }

    def save_file(
        self,
        path: Path,
        file_content: bytes,
        existing_file_policy: ExistingFilePolicy = ExistingFilePolicy.OVERWRITE,
        *,
        skip_metadata_injection: bool = False,
        file_metadata: SidecarContent | None = None,
    ) -> str:
        """Save a file to local storage by writing directly to disk.

        Args:
            path: The path of the file to save.
            file_content: The file content as bytes.
            existing_file_policy: How to handle existing files. Defaults to OVERWRITE.
            skip_metadata_injection: If True, skip automatic workflow metadata injection.
            file_metadata: Optional caller-provided context for sidecar metadata generation.

        Returns:
            The absolute file path where the file was saved.

        Raises:
            FileExistsError: When existing_file_policy is FAIL and file already exists.
            RuntimeError: If file write fails.
        """
        # Read current workspace path fresh from ConfigManager
        current_workspace = GriptapeNodes.ConfigManager().workspace_path
        absolute_path = resolve_workspace_path(path, current_workspace)

        result = GriptapeNodes.OSManager().on_write_file_request(
            WriteFileRequest(
                file_path=str(absolute_path),
                content=file_content,
                existing_file_policy=existing_file_policy,
                skip_metadata_injection=skip_metadata_injection,
                file_metadata=file_metadata,
            )
        )

        if not isinstance(result, WriteFileResultSuccess):
            msg = f"Failed to write file {path}: {result.result_details}"
            raise ValueError(msg)  # noqa: TRY004

        return result.final_file_path

    def create_signed_download_url(self, path: Path) -> str:
        # Read current workspace path fresh from ConfigManager instead of using cached value
        # This ensures the URL is resolved against the current workspace, even if the workspace
        # changed after this driver was initialized (e.g., project switch, workspace_dir override)
        current_workspace = GriptapeNodes.ConfigManager().workspace_path

        # Resolve path, treating relative paths as workspace-relative
        absolute_path = resolve_workspace_path(path, current_workspace)

        # Automatically determine if the file is external to the workspace
        try:
            workspace_relative_path = absolute_path.relative_to(current_workspace.resolve())
            # Internal files: use workspace-relative path
            url = f"{self.base_url}/{workspace_relative_path.as_posix()}"
        except ValueError:
            # For external files, use /external path and strip leading slash from absolute path.
            # Use as_posix() to normalize backslashes to forward slashes for URLs (important on Windows).
            path_str = absolute_path.as_posix().removeprefix("/")
            # Build URL with /external prefix, replacing the /workspace part of base_url
            base_without_workspace = self.base_url.rsplit("/workspace", 1)[0]
            url = f"{base_without_workspace}/external/{path_str}"

        # Add a cache-busting query parameter to the URL so that the browser always reloads the file
        cache_busted_url = f"{url}?t={int(time.time())}"
        return cache_busted_url

    def delete_file(self, path: Path) -> None:
        """Delete a file from local storage.

        Args:
            path: The path of the file to delete.
        """
        # Use the static server's delete endpoint
        delete_url = urljoin(self.base_url, f"/static-files/{path.as_posix()}")

        try:
            response = httpx.delete(delete_url)
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            msg = f"Failed to delete file {path}: {e}"
            logger.error(msg)
            raise RuntimeError(msg) from e

    def list_files(self) -> list[str]:
        """List all files in local storage.

        Returns:
            A list of file names in storage.
        """
        # Use the static server's list endpoint
        list_url = urljoin(self.base_url, "/static-uploads/")

        try:
            response = httpx.get(list_url)
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            msg = f"Failed to list files: {e}"
            logger.error(msg)
            raise RuntimeError(msg) from e

        response_data = response.json()
        return response_data.get("files", [])

    def get_asset_url(self, path: Path) -> str:
        """Get the permanent URL for a local asset.

        Builds the canonical path using the ``copy_external_file`` situation and returns
        it as an absolute path string.  Falls back to the absolute path of the original
        file if the situation cannot be resolved (e.g. no project loaded).

        Args:
            path: The path of the file

        Returns:
            Absolute path string for the resolved asset path
        """
        destination = ProjectFileDestination.from_situation(path.name, BuiltInSituation.COPY_EXTERNAL_FILE)
        try:
            resolved_path = Path(destination.resolve())
        except FileLoadError:
            # Read current workspace path fresh from ConfigManager
            current_workspace = GriptapeNodes.ConfigManager().workspace_path
            return str(resolve_workspace_path(path, current_workspace))
        return str(resolved_path)
