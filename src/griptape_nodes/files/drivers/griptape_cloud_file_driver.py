"""File driver for Griptape Cloud asset locations."""

import os
from urllib.parse import urljoin, urlparse

import httpx

from griptape_nodes.drivers.cloud_credentials import resolve_cloud_credential
from griptape_nodes.drivers.storage.griptape_cloud_storage_driver import GriptapeCloudStorageDriver
from griptape_nodes.files.base_file_driver import BaseFileDriver

# HTTP status code threshold for success
_HTTP_SUCCESS_THRESHOLD = 400


class GriptapeCloudFileDriver(BaseFileDriver):
    """Read-only file driver for Griptape Cloud asset locations.

    Handles locations matching: https://cloud.griptape.ai/buckets/{id}/assets/{path}
    Reads files via signed URLs. For writing files, use storage drivers directly.
    """

    @property
    def priority(self) -> int:
        """Return priority 10 to be checked before HttpFileDriver (50).

        Returns:
            Priority value of 10
        """
        return 10

    def __init__(self, bucket_id: str, api_key: str, base_url: str = "https://cloud.griptape.ai") -> None:
        """Initialize GriptapeCloudFileDriver.

        Args:
            bucket_id: Griptape Cloud bucket ID
            api_key: API key for authentication
            base_url: Base URL for Griptape Cloud API (default: https://cloud.griptape.ai)
        """
        self.bucket_id = bucket_id
        self.api_key = api_key
        self.base_url = base_url
        self.headers = {"Authorization": f"Bearer {api_key}"}

    @classmethod
    def create_from_env(cls) -> "GriptapeCloudFileDriver | None":
        """Create driver from environment variables if available.

        Checks for a GT_CLOUD_BUCKET_ID and a Griptape Cloud credential (the
        license, else GT_CLOUD_API_KEY). If both are present, creates and returns
        a driver instance.

        Returns:
            GriptapeCloudFileDriver instance if credentials available, None otherwise
        """
        bucket_id = os.environ.get("GT_CLOUD_BUCKET_ID")
        api_key = resolve_cloud_credential()
        base_url = os.environ.get("GT_CLOUD_BASE_URL", "https://cloud.griptape.ai")

        if bucket_id and api_key:
            return cls(bucket_id, api_key, base_url)

        return None

    def can_handle(self, location: str) -> bool:
        """Check if location is a Griptape Cloud asset URL.

        Args:
            location: Location string to check

        Returns:
            True if location is a cloud asset URL
        """
        return GriptapeCloudStorageDriver.is_cloud_asset_url(location, self.base_url)

    def _extract_bucket_id_from_url(self, location: str) -> str | None:
        """Extract bucket ID from Griptape Cloud asset URL.

        Args:
            location: Cloud asset URL (e.g., https://cloud.griptape.ai/buckets/{id}/assets/{path})

        Returns:
            Bucket ID string, or None if not found
        """
        parsed = urlparse(location)
        path = parsed.path
        # Pattern: /buckets/{bucket_id}/assets/...
        if "/buckets/" in path and "/assets/" in path:
            parts = path.split("/buckets/", 1)
            if len(parts) > 1:
                # Get everything after /buckets/ up to /assets/
                bucket_part = parts[1].split("/assets/", 1)[0]
                return bucket_part
        return None

    async def read(self, location: str, timeout: float) -> bytes:  # noqa: ASYNC109
        """Download file from Griptape Cloud storage.

        Args:
            location: Cloud asset URL
            timeout: Timeout in seconds for HTTP request

        Returns:
            Downloaded bytes

        Raises:
            RuntimeError: If download fails or URL conversion fails
        """
        workspace_path = GriptapeCloudStorageDriver.extract_workspace_path_from_cloud_url(location)

        if not workspace_path:
            msg = f"Failed to extract workspace path from cloud URL: {location}"
            raise RuntimeError(msg)

        # Extract bucket ID from URL, fallback to configured bucket_id
        bucket_id = self._extract_bucket_id_from_url(location) or self.bucket_id

        api_url = urljoin(self.base_url, f"/api/buckets/{bucket_id}/asset-urls/{workspace_path}")

        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(api_url, json={"method": "GET"}, headers=self.headers, timeout=timeout)
                response.raise_for_status()
                signed_url = response.json()["url"]

                download_response = await client.get(signed_url, timeout=timeout)
                download_response.raise_for_status()
                return download_response.content

        except httpx.HTTPError as e:
            msg = f"Failed to download from cloud storage at {location}: {e}"
            raise RuntimeError(msg) from e

    async def exists(self, location: str) -> bool:
        """Check if cloud asset exists.

        Args:
            location: Cloud asset URL

        Returns:
            True if asset exists (can get signed URL successfully)
        """
        workspace_path = GriptapeCloudStorageDriver.extract_workspace_path_from_cloud_url(location)

        if not workspace_path:
            return False

        # Extract bucket ID from URL, fallback to configured bucket_id
        bucket_id = self._extract_bucket_id_from_url(location) or self.bucket_id

        api_url = urljoin(self.base_url, f"/api/buckets/{bucket_id}/asset-urls/{workspace_path}")

        try:
            async with httpx.AsyncClient() as client:
                # TODO: Standardize timeout values https://github.com/griptape-ai/griptape-nodes/issues/3958
                response = await client.post(api_url, json={"method": "GET"}, headers=self.headers, timeout=10.0)
                return response.status_code < _HTTP_SUCCESS_THRESHOLD
        except (httpx.HTTPError, Exception):
            return False

    def get_size(self, location: str) -> int:
        """Get file size from cloud storage.

        Args:
            location: Cloud asset URL

        Returns:
            File size in bytes, or 0 if unavailable

        Note:
            This makes a synchronous HEAD request to get Content-Length.
        """
        workspace_path = GriptapeCloudStorageDriver.extract_workspace_path_from_cloud_url(location)

        if not workspace_path:
            return 0

        # Extract bucket ID from URL, fallback to configured bucket_id
        bucket_id = self._extract_bucket_id_from_url(location) or self.bucket_id

        api_url = urljoin(self.base_url, f"/api/buckets/{bucket_id}/asset-urls/{workspace_path}")

        try:
            with httpx.Client() as client:
                response = client.post(api_url, json={"method": "GET"}, headers=self.headers, timeout=10.0)
                response.raise_for_status()
                signed_url = response.json()["url"]

                head_response = client.head(signed_url, timeout=10.0)
                head_response.raise_for_status()
                content_length = head_response.headers.get("content-length")
                return int(content_length) if content_length else 0

        except (httpx.HTTPError, ValueError, Exception):
            return 0
