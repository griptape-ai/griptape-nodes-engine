import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import TypedDict

import httpx

from griptape_nodes.retained_mode.events.os_events import ExistingFilePolicy
from griptape_nodes.retained_mode.file_metadata.sidecar_metadata import SidecarContent

logger = logging.getLogger("griptape_nodes")


class CreateSignedUploadUrlResponse(TypedDict):
    """Response type for create_signed_upload_url method."""

    url: str
    file_path: str
    headers: dict
    method: str


class BaseStorageDriver(ABC):
    """Base class for storage drivers.

    All path arguments accepted by this driver's methods must be workspace-relative
    (e.g., ``outputs/image.png``), not absolute paths. Callers are responsible for
    converting absolute paths to workspace-relative before calling driver methods.
    """

    def __init__(self, workspace_directory: Path) -> None:  # noqa: B027
        """Initialize the storage driver with a workspace directory.

        Args:
            workspace_directory: The base workspace directory path (unused, kept for API compatibility).
                Drivers now fetch the current workspace dynamically via the workspace_directory property.
        """
        # workspace_directory parameter is ignored - the property reads from ConfigManager instead.
        # This ensures drivers always use the current workspace, even if it changes after initialization.

    @property
    def workspace_directory(self) -> Path:
        """Get the current workspace directory from ConfigManager.

        Returns the workspace path fresh on each access to handle dynamic workspace
        changes (e.g., project switches, workspace_dir overrides).
        """
        from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

        return GriptapeNodes.ConfigManager().workspace_path

    @abstractmethod
    def create_signed_upload_url(
        self,
        path: Path,
        existing_file_policy: ExistingFilePolicy = ExistingFilePolicy.OVERWRITE,
        *,
        file_metadata: SidecarContent | None = None,
    ) -> CreateSignedUploadUrlResponse:
        """Create a signed upload URL for the given path.

        Args:
            path: Workspace-relative path of the file (e.g., ``outputs/image.png``).
            existing_file_policy: How to handle existing files. Defaults to OVERWRITE for backward compatibility.
            file_metadata: Optional caller-provided context for sidecar metadata generation.

        Returns:
            CreateSignedUploadUrlResponse: A dictionary containing the signed URL, headers, and operation type.
        """
        ...

    @abstractmethod
    def create_signed_download_url(self, path: Path) -> str:
        """Create a signed download URL for the given path.

        Args:
            path: Workspace-relative path of the file (e.g., ``outputs/image.png``).

        Returns:
            str: The signed URL for downloading the file.
        """
        ...

    @abstractmethod
    def delete_file(self, path: Path) -> None:
        """Delete a file from storage.

        Args:
            path: Workspace-relative path of the file (e.g., ``outputs/image.png``).
        """
        ...

    @abstractmethod
    def list_files(self) -> list[str]:
        """List all files in storage.

        Returns:
            A list of file names in storage.
        """
        ...

    @abstractmethod
    def get_asset_url(self, path: Path) -> str:
        """Get the permanent unsigned URL for an asset.

        Args:
            path: Workspace-relative path of the file (e.g., ``outputs/image.png``).

        Returns:
            Permanent URL for accessing the asset
        """
        ...

    @abstractmethod
    def save_file(
        self,
        path: Path,
        file_content: bytes,
        existing_file_policy: ExistingFilePolicy = ExistingFilePolicy.OVERWRITE,
        *,
        skip_metadata_injection: bool = False,
        file_metadata: SidecarContent | None = None,
    ) -> str:
        """Save a file to storage.

        Args:
            path: Workspace-relative path of the file (e.g., ``outputs/image.png``).
            file_content: The file content as bytes.
            existing_file_policy: How to handle existing files. Defaults to OVERWRITE.
            skip_metadata_injection: If True, skip automatic workflow metadata injection.
            file_metadata: Optional caller-provided context for sidecar metadata generation.
                           Passed through to WriteFileRequest; ignored by cloud storage drivers.

        Returns:
            The absolute file path where the file was saved.

        Raises:
            FileExistsError: When existing_file_policy is FAIL and file already exists.
            RuntimeError: If file save fails.
        """
        ...

    def upload_file(
        self,
        path: Path,
        file_content: bytes,
        existing_file_policy: ExistingFilePolicy = ExistingFilePolicy.OVERWRITE,
        timeout: float | None = None,
    ) -> str:
        """Upload a file to storage.

        Args:
            path: Workspace-relative path of the file (e.g., ``outputs/image.png``).
            file_content: The file content as bytes.
            existing_file_policy: How to handle existing files. Defaults to OVERWRITE for backward compatibility.
            timeout: Optional timeout in seconds for upload request, None falls back to the httpx default.

        Returns:
            The URL where the file can be accessed.

        Raises:
            RuntimeError: If file upload fails.
        """
        try:
            # Get signed upload URL
            upload_response = self.create_signed_upload_url(path, existing_file_policy)

            # Upload the file using the signed URL
            response = httpx.request(
                upload_response["method"],
                upload_response["url"],
                content=file_content,
                headers=upload_response["headers"],
                timeout=timeout,
            )
            response.raise_for_status()

            # Return the download URL
            return self.create_signed_download_url(path)
        except httpx.HTTPStatusError as e:
            msg = f"Failed to upload file {path}: {e}"
            logger.error(msg)
            raise RuntimeError(msg) from e
        except Exception as e:
            msg = f"Unexpected error uploading file {path}: {e}"
            logger.error(msg)
            raise RuntimeError(msg) from e

    def download_file(self, path: Path, timeout: float | None = None) -> bytes:
        """Download a file from storage.

        Args:
            path: Workspace-relative path of the file (e.g., ``outputs/image.png``).
            timeout: Optional timeout in seconds for download request, None falls back to the httpx default.

        Returns:
            The file content as bytes.

        Raises:
            RuntimeError: If file download fails.
        """
        try:
            # Get signed download URL
            download_url = self.create_signed_download_url(path)

            # Download the file
            response = httpx.get(download_url, timeout=timeout)
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            msg = f"Failed to download file {path}: {e}"
            logger.error(msg)
            raise RuntimeError(msg) from e
        except Exception as e:
            msg = f"Unexpected error downloading file {path}: {e}"
            logger.error(msg)
            raise RuntimeError(msg) from e
        else:
            return response.content
