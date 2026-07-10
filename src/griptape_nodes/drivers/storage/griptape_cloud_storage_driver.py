import logging
import os
from collections.abc import Callable
from http import HTTPStatus
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from griptape_nodes.drivers.storage.base_storage_driver import BaseStorageDriver, CreateSignedUploadUrlResponse
from griptape_nodes.files.path_utils import get_workspace_relative_path
from griptape_nodes.retained_mode.events.os_events import ExistingFilePolicy
from griptape_nodes.retained_mode.file_metadata.sidecar_metadata import SidecarContent
from griptape_nodes.utils.http_utils import request_with_retry, retry_on_transient_error

logger = logging.getLogger("griptape_nodes")


class GriptapeCloudStorageDriver(BaseStorageDriver):
    """Stores files using the Griptape Cloud's Asset APIs."""

    def __init__(
        self,
        workspace_directory: Path,
        *,
        bucket_id: str,
        api_key: str | None = None,
        **kwargs,
    ) -> None:
        """Initialize the GriptapeCloudStorageDriver.

        Args:
            workspace_directory: The base workspace directory path.
            bucket_id: The ID of the bucket to use. Required.
            api_key: The API key for authentication. If not provided, it will be retrieved from the environment variable "GT_CLOUD_API_KEY".
            static_files_directory: The directory path prefix for static files. If provided, file names will be prefixed with this path.
            **kwargs: Additional keyword arguments including base_url and headers.
        """
        super().__init__(workspace_directory)

        self.base_url = kwargs.get("base_url") or os.environ.get("GT_CLOUD_BASE_URL", "https://cloud.griptape.ai")
        self.api_key = api_key if api_key is not None else os.environ.get("GT_CLOUD_API_KEY")
        self.headers = kwargs.get("headers") or {"Authorization": f"Bearer {self.api_key}"}
        self.request_timeout = kwargs.get("request_timeout")
        self.bucket_id = bucket_id

    @retry_on_transient_error
    def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        """Make an HTTP request with automatic retries on transient errors."""
        kwargs.setdefault("headers", self.headers)
        kwargs.setdefault("timeout", self.request_timeout)
        response = httpx.request(method, url, **kwargs)
        response.raise_for_status()
        return response

    def create_signed_upload_url(
        self,
        path: Path,
        existing_file_policy: ExistingFilePolicy = ExistingFilePolicy.OVERWRITE,
        *,
        file_metadata: SidecarContent | None = None,  # noqa: ARG002
    ) -> CreateSignedUploadUrlResponse:
        normalized_path = path

        if existing_file_policy != ExistingFilePolicy.OVERWRITE:
            logger.warning(
                "Griptape Cloud storage only supports OVERWRITE policy. "
                "Requested policy '%s' will be ignored for file: %s",
                existing_file_policy.value,
                normalized_path,
            )

        self._create_asset(normalized_path.as_posix())

        url = urljoin(self.base_url, f"/api/buckets/{self.bucket_id}/asset-urls/{normalized_path.as_posix()}")
        try:
            response = self._request("POST", url, json={"operation": "PUT"})
        except httpx.HTTPStatusError as e:
            msg = f"Failed to create presigned upload URL for file {normalized_path}: {e}"
            logger.error(msg)
            raise RuntimeError(msg) from e

        response_data = response.json()

        return {
            "url": response_data["url"],
            "headers": response_data.get("headers", {}),
            "method": "PUT",
            "file_path": str(normalized_path),
        }

    def _parse_cloud_asset_path(self, path: str | Path) -> Path:
        """Parse cloud asset URL path to extract workspace-relative portion.

        Handles multiple input formats:
        - Full URLs: https://cloud.griptape.ai/buckets/{id}/assets/{path}
        - Path-only: /buckets/{id}/assets/{path}
        - Workspace-relative: {path}

        When a full URL is provided, validates that the domain matches
        self.base_url to prevent cross-environment URL mixing.

        Args:
            path: String or Path object that may contain a cloud asset URL pattern

        Returns:
            Workspace-relative path

        Raises:
            ValueError: When full URL domain doesn't match configured base_url
        """
        # Convert to string for processing
        path_str = str(path) if isinstance(path, Path) else path

        # Validate domain for full URLs (only if it contains :// scheme separator)
        if "://" in path_str and path_str.startswith(("http://", "https://")):
            input_parsed = urlparse(path_str)
            input_domain = input_parsed.netloc.lower()

            base_parsed = urlparse(self.base_url)
            expected_domain = base_parsed.netloc.lower()

            if input_domain != expected_domain:
                msg = (
                    f"Invalid cloud asset URL: domain '{input_domain}' does not match "
                    f"configured base URL '{self.base_url}'. "
                    f"Expected domain: '{expected_domain}'"
                )
                logger.error(msg)
                raise ValueError(msg)

            # Extract path component for further processing
            path_str = input_parsed.path.lstrip("/")

        # Check if it's a cloud asset URL pattern
        # Handle both /buckets/ and buckets/ (after leading slash removal)
        has_buckets = "/buckets/" in path_str or path_str.startswith("buckets/")
        has_assets = "/assets/" in path_str
        if has_buckets and has_assets:
            # Extract workspace-relative path from cloud URL
            # Format: /buckets/{bucket_id}/assets/{workspace_relative_path} or buckets/{bucket_id}/assets/{workspace_relative_path}
            parts = path_str.split("/assets/", 1)
            expected_parts_count = 2
            if len(parts) == expected_parts_count:
                return Path(parts[1])  # Return the workspace-relative path after /assets/

        # For non-cloud paths, return as-is
        return Path(path_str)

    def create_signed_download_url(self, path: Path) -> str:
        # Parse cloud asset URLs before normalizing
        parsed_path = self._parse_cloud_asset_path(path)
        normalized_path = get_workspace_relative_path(parsed_path, self.workspace_directory)
        url = urljoin(self.base_url, f"/api/buckets/{self.bucket_id}/asset-urls/{normalized_path.as_posix()}")
        try:
            response = self._request("POST", url, json={"method": "GET"})
        except httpx.HTTPStatusError as e:
            msg = f"Failed to create presigned download URL for file {normalized_path}: {e}"
            logger.error(msg)
            raise RuntimeError(msg) from e

        response_data = response.json()

        return response_data["url"]

    def save_file(
        self,
        path: Path,
        file_content: bytes,
        existing_file_policy: ExistingFilePolicy = ExistingFilePolicy.OVERWRITE,
        *,
        skip_metadata_injection: bool = False,  # noqa: ARG002
        file_metadata: SidecarContent | None = None,  # noqa: ARG002
    ) -> str:
        """Save a file to cloud storage via HTTP upload.

        Args:
            path: The path of the file to save.
            file_content: The file content as bytes.
            existing_file_policy: How to handle existing files. Defaults to OVERWRITE.
            skip_metadata_injection: Unused; cloud storage does not perform metadata injection.
            file_metadata: Ignored by cloud storage driver (sidecar metadata is local-only).

        Returns:
            The full asset URL for the saved file.

        Raises:
            RuntimeError: If file upload fails.
        """
        normalized_path = get_workspace_relative_path(path, self.workspace_directory)

        if existing_file_policy != ExistingFilePolicy.OVERWRITE:
            logger.warning(
                "GriptapeCloudStorageDriver only supports OVERWRITE policy, got %s. "
                "The file will be overwritten if it exists.",
                existing_file_policy,
            )

        # Get signed upload URL
        upload_response = self.create_signed_upload_url(path, existing_file_policy)

        # Upload the file using the signed URL
        try:
            self._request(
                upload_response["method"],
                upload_response["url"],
                content=file_content,
                headers=upload_response["headers"],
            )
        except httpx.HTTPStatusError as e:
            msg = f"Failed to upload file {normalized_path}: {e}"
            logger.error(msg)
            raise RuntimeError(msg) from e

        # Return the full asset URL
        return urljoin(self.base_url, f"/buckets/{self.bucket_id}/assets/{normalized_path.as_posix()}")

    def _create_asset(self, asset_name: str) -> str:
        url = urljoin(self.base_url, f"/api/buckets/{self.bucket_id}/assets")
        try:
            response = self._request("PUT", url, json={"name": asset_name})
        except httpx.HTTPStatusError as e:
            msg = str(e)
            logger.error(msg)
            raise ValueError(msg) from e

        return response.json()["name"]

    @staticmethod
    def create_bucket(bucket_name: str, *, base_url: str, api_key: str, timeout: float | None = None) -> str:
        """Create a new bucket in Griptape Cloud.

        Args:
            bucket_name: Name for the bucket.
            base_url: The base URL for the Griptape Cloud API.
            api_key: The API key for authentication.
            timeout: Optional request timeout in seconds.

        Returns:
            The bucket ID of the created bucket.

        Raises:
            RuntimeError: If bucket creation fails.
        """
        headers = {"Authorization": f"Bearer {api_key}"}
        url = urljoin(base_url, "/api/buckets")
        payload = {"name": bucket_name}

        try:
            response = request_with_retry("POST", url, json=payload, headers=headers, timeout=timeout)
        except httpx.HTTPStatusError as e:
            msg = f"Failed to create bucket '{bucket_name}': {e}"
            logger.error(msg)
            raise RuntimeError(msg) from e

        response_data = response.json()
        bucket_id = response_data["bucket_id"]

        logger.info("Created new Griptape Cloud bucket '%s' with ID: %s", bucket_name, bucket_id)
        return bucket_id

    def list_files(self) -> list[str]:
        """List all files in storage.

        Returns:
            A list of file names in storage.

        Raises:
            RuntimeError: If file listing fails.
        """
        url = urljoin(self.base_url, f"/api/buckets/{self.bucket_id}/assets")
        try:
            response = self._request("GET", url, params={"prefix": self.workspace_directory.name or ""})
        except httpx.HTTPStatusError as e:
            msg = f"Failed to list files in bucket {self.bucket_id}: {e}"
            logger.error(msg)
            raise RuntimeError(msg) from e

        response_data = response.json()
        assets = response_data.get("assets", [])

        file_names = []
        for asset in assets:
            name = asset.get("name", "")
            # Remove the static files directory prefix if it exists
            if self.workspace_directory and name.startswith(f"{self.workspace_directory.name}/"):
                name = name[len(f"{self.workspace_directory.name}/") :]
            file_names.append(name)

        return file_names

    def get_asset_url(self, path: Path) -> str:
        """Get the permanent unsigned URL for a cloud asset.

        Returns the permanent public URL for the asset (not the presigned URL).

        Args:
            path: The path of the file

        Returns:
            Permanent cloud asset URL
        """
        # Parse cloud asset URLs before normalizing
        parsed_path = self._parse_cloud_asset_path(path)
        normalized_path = get_workspace_relative_path(parsed_path, self.workspace_directory)
        return urljoin(self.base_url, f"/buckets/{self.bucket_id}/assets/{normalized_path.as_posix()}")

    @staticmethod
    def list_buckets(*, base_url: str, api_key: str, timeout: float | None = None) -> list[dict]:
        """List all buckets in Griptape Cloud.

        Args:
            base_url: The base URL for the Griptape Cloud API.
            api_key: The API key for authentication.
            timeout: Optional request timeout in seconds.

        Returns:
            A list of dictionaries containing bucket information.
        """
        headers = {"Authorization": f"Bearer {api_key}"}
        url = urljoin(base_url, "/api/buckets")

        try:
            response = request_with_retry("GET", url, headers=headers, timeout=timeout)
        except httpx.HTTPStatusError as e:
            msg = f"Failed to list buckets: {e}"
            logger.error(msg)
            raise RuntimeError(msg) from e

        return response.json().get("buckets", [])

    @staticmethod
    def bucket_exists(bucket_id: str, *, base_url: str, api_key: str, timeout: float | None = None) -> bool:
        """Check whether a specific bucket exists in Griptape Cloud.

        Uses a direct GET on the bucket resource rather than scanning ``list_buckets``,
        which is paginated -- a valid bucket beyond the first page would otherwise look
        missing. A 404 means the bucket does not exist (or is not visible to this API key);
        any other HTTP error is surfaced so callers don't mistake it for a missing bucket.

        Args:
            bucket_id: The ID of the bucket to check.
            base_url: The base URL for the Griptape Cloud API.
            api_key: The API key for authentication.
            timeout: Optional request timeout in seconds.

        Returns:
            True if the bucket exists and is accessible, False if it does not.

        Raises:
            RuntimeError: If the existence check fails for a reason other than a 404.
        """
        headers = {"Authorization": f"Bearer {api_key}"}
        url = urljoin(base_url, f"/api/buckets/{bucket_id}")

        try:
            request_with_retry("GET", url, headers=headers, timeout=timeout)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == HTTPStatus.NOT_FOUND:
                return False
            msg = f"Failed to check bucket '{bucket_id}': {e}"
            logger.error(msg)
            raise RuntimeError(msg) from e

        return True

    def delete_file(self, path: Path) -> None:
        """Delete a file from the bucket.

        Args:
            path: The path of the file to delete.
        """
        normalized_path = get_workspace_relative_path(path, self.workspace_directory)
        url = urljoin(self.base_url, f"/api/buckets/{self.bucket_id}/assets/{normalized_path.as_posix()}")

        try:
            self._request("DELETE", url)
        except httpx.HTTPStatusError as e:
            msg = f"Failed to delete file {normalized_path}: {e}"
            logger.error(msg)
            raise RuntimeError(msg) from e

    def _is_cloud_asset_url(self, url_str: str) -> bool:
        """Check if URL is a Griptape Cloud asset URL with domain validation.

        Detects URLs matching pattern: https://cloud.griptape.ai/buckets/{id}/assets/{path}
        Validates domain matches expected cloud domain.

        Args:
            url_str: String to check

        Returns:
            True if url_str is a valid cloud asset URL
        """
        # Fast negative checks first
        if not url_str:
            return False

        # Must be a full URL with scheme
        if not url_str.startswith(("http://", "https://")):
            return False

        # Parse URL to check domain
        parsed = urlparse(url_str)
        domain = parsed.netloc.lower()

        # Get expected cloud domain from instance
        expected_parsed = urlparse(self.base_url)
        expected_domain = expected_parsed.netloc.lower()

        # Domain must match
        if domain != expected_domain:
            return False

        # Must contain both /buckets/ and /assets/ patterns
        path = parsed.path
        has_buckets = "/buckets/" in path
        has_assets = "/assets/" in path

        # Success path - valid cloud asset URL
        return has_buckets and has_assets

    def _extract_workspace_path_from_cloud_url(self, url_str: str) -> str | None:
        """Extract workspace-relative path from cloud asset URL.

        Parses URLs like: /buckets/{bucket_id}/assets/{workspace_path}
        Returns just the {workspace_path} portion.

        Args:
            url_str: Cloud asset URL

        Returns:
            Workspace-relative path, or None if parsing fails
        """
        parsed = urlparse(url_str)
        path = parsed.path

        # Extract workspace-relative path from: /buckets/{bucket_id}/assets/{workspace_path}
        expected_parts = 2
        try:
            parts = path.split("/assets/", 1)
            if len(parts) != expected_parts:
                return None
            return parts[1]
        except Exception:
            return None

    def _create_signed_download_url_from_asset_url(self, asset_url: str) -> str | None:
        """Create a signed download URL for a cloud asset.

        Args:
            asset_url: Cloud asset URL to convert

        Returns:
            Signed download URL if successful, None if fails
        """
        # Extract workspace-relative path
        workspace_path = self._extract_workspace_path_from_cloud_url(asset_url)
        if not workspace_path:
            logger.debug("Could not extract workspace path from cloud URL: %s", asset_url)
            return None

        # Build API URL for signed download URL
        api_url = urljoin(self.base_url, f"/api/buckets/{self.bucket_id}/asset-urls/{workspace_path}")

        # Make API request to get signed URL
        try:
            response = self._request("POST", api_url, json={"method": "GET"})

            response_data = response.json()
            signed_url = response_data["url"]

            logger.info("Converted cloud asset URL to signed URL: %s", asset_url)
        except Exception as e:
            if isinstance(e, httpx.HTTPStatusError):
                logger.warning(
                    "Failed to create signed download URL for %s: HTTP %s", asset_url, e.response.status_code
                )
            else:
                logger.warning("Failed to create signed download URL for %s: %s", asset_url, e)
            return None
        else:
            return signed_url

    @staticmethod
    def is_cloud_asset_url(url_str: str, base_url: str | None = None) -> bool:
        """Check if URL is a Griptape Cloud asset URL with domain validation.

        Static version for use without driver instance (e.g., httpx patching layer).
        Detects URLs matching pattern: https://cloud.griptape.ai/buckets/{id}/assets/{path}
        Validates domain matches expected cloud domain.

        Args:
            url_str: String to check
            base_url: Expected cloud domain URL. If None, reads from GT_CLOUD_BASE_URL env var.

        Returns:
            True if url_str is a valid cloud asset URL
        """
        # Fast negative checks first
        if not url_str:
            return False

        # Must be a full URL with scheme
        if not url_str.startswith(("http://", "https://")):
            return False

        # Parse URL to check domain
        parsed = urlparse(url_str)
        domain = parsed.netloc.lower()

        # Get expected cloud domain from parameter or environment
        if base_url is None:
            base_url = os.environ.get("GT_CLOUD_BASE_URL", "https://cloud.griptape.ai")

        expected_parsed = urlparse(base_url)
        expected_domain = expected_parsed.netloc.lower()

        # Domain must match
        if domain != expected_domain:
            return False

        # Must contain both /buckets/ and /assets/ patterns
        path = parsed.path
        has_buckets = "/buckets/" in path
        has_assets = "/assets/" in path

        # Success path - valid cloud asset URL
        return has_buckets and has_assets

    @staticmethod
    def extract_workspace_path_from_cloud_url(url_str: str) -> str | None:
        """Extract workspace-relative path from cloud asset URL.

        Static version for use without driver instance.
        Parses URLs like: /buckets/{bucket_id}/assets/{workspace_path}
        Returns just the {workspace_path} portion.

        Args:
            url_str: Cloud asset URL

        Returns:
            Workspace-relative path, or None if parsing fails
        """
        parsed = urlparse(url_str)
        path = parsed.path

        # Extract workspace-relative path from: /buckets/{bucket_id}/assets/{workspace_path}
        expected_parts = 2
        try:
            parts = path.split("/assets/", 1)
            if len(parts) != expected_parts:
                return None
            return parts[1]
        except Exception:
            return None

    @staticmethod
    def extract_bucket_id_from_url(url_str: str) -> str | None:
        """Extract bucket_id from a Griptape Cloud asset URL.

        Static version for use without driver instance.
        Parses URLs like: https://cloud.griptape.ai/buckets/{bucket_id}/assets/{workspace_path}
        or /buckets/{bucket_id}/assets/{workspace_path}
        Returns just the {bucket_id} portion.

        Args:
            url_str: Cloud asset URL or path string

        Returns:
            Bucket ID if URL matches cloud asset pattern, None otherwise
        """
        if not url_str:
            return None

        # Parse URL to extract path component
        parsed = urlparse(url_str)
        path = parsed.path or url_str

        # Check for required patterns
        if "/buckets/" not in path or "/assets/" not in path:
            return None

        # Extract bucket_id from: /buckets/{bucket_id}/assets/{workspace_path}
        expected_parts = 2
        try:
            parts = path.split("/buckets/", 1)
            if len(parts) != expected_parts:
                return None

            bucket_part = parts[1]
            bucket_id = bucket_part.split("/assets/")[0]

            if not bucket_id:
                return None
        except (IndexError, AttributeError):
            return None
        else:
            return bucket_id

    @staticmethod
    def create_signed_download_url_from_asset_url(
        asset_url: str,
        bucket_id: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        *,
        httpx_request_func: Callable[..., Any],
    ) -> str | None:
        """Create a signed download URL for a cloud asset.

        Static version for use without driver instance (e.g., httpx patching layer).

        Args:
            asset_url: Cloud asset URL to convert
            bucket_id: Bucket ID. If None, reads from GT_CLOUD_BUCKET_ID env var.
            api_key: API key. If None, reads from GT_CLOUD_API_KEY env var.
            base_url: Cloud base URL. If None, reads from GT_CLOUD_BASE_URL env var.
            httpx_request_func: The httpx request function to use (original, not patched)

        Returns:
            Signed download URL if successful, None if fails
        """
        # Get credentials from parameters or environment
        if bucket_id is None:
            bucket_id = os.environ.get("GT_CLOUD_BUCKET_ID")
        if api_key is None:
            api_key = os.environ.get("GT_CLOUD_API_KEY")
        if base_url is None:
            base_url = os.environ.get("GT_CLOUD_BASE_URL", "https://cloud.griptape.ai")

        # Guard: Check for required credentials
        if not bucket_id:
            logger.debug("GT_CLOUD_BUCKET_ID not set, skipping cloud URL conversion: %s", asset_url)
            return None

        if not api_key:
            logger.debug("GT_CLOUD_API_KEY not set, skipping cloud URL conversion: %s", asset_url)
            return None

        # Extract workspace-relative path
        workspace_path = GriptapeCloudStorageDriver.extract_workspace_path_from_cloud_url(asset_url)
        if not workspace_path:
            logger.debug("Could not extract workspace path from cloud URL: %s", asset_url)
            return None

        # Build API URL for signed download URL
        api_url = urljoin(base_url, f"/api/buckets/{bucket_id}/asset-urls/{workspace_path}")

        # Make API request to get signed URL
        try:
            headers = {"Authorization": f"Bearer {api_key}"}
            response = request_with_retry(
                "POST", api_url, httpx_request_func=httpx_request_func, json={"method": "GET"}, headers=headers
            )
        except Exception as e:
            logger.warning("Failed to create signed download URL for %s: %s", asset_url, e)
            return None

        signed_url = response.json()["url"]
        logger.info("Converted cloud asset URL to signed URL: %s", asset_url)
        return signed_url
