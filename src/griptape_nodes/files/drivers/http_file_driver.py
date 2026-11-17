"""File driver for HTTP/HTTPS locations."""

import httpx2

from griptape_nodes.files.base_file_driver import BaseFileDriver

# HTTP status code threshold for success
_HTTP_SUCCESS_THRESHOLD = 400


class HttpFileDriver(BaseFileDriver):
    """Read-only file driver for HTTP/HTTPS locations.

    Handles locations starting with "http://" or "https://" prefix,
    downloading content via async HTTP requests.
    """

    def can_handle(self, location: str) -> bool:
        """Check if location is an HTTP/HTTPS URL.

        Args:
            location: Location string to check

        Returns:
            True if location starts with "http://" or "https://"
        """
        return location.startswith(("http://", "https://"))

    async def read(self, location: str, timeout: float) -> bytes:  # noqa: ASYNC109
        """Download file from HTTP/HTTPS URL.

        Args:
            location: HTTP/HTTPS URL to download from
            timeout: Timeout in seconds for HTTP request

        Returns:
            Downloaded bytes

        Raises:
            RuntimeError: If download fails or HTTP error occurs
        """
        try:
            async with httpx2.AsyncClient() as client:
                response = await client.get(location, timeout=timeout)
                response.raise_for_status()
                return response.content
        except httpx2.HTTPError as e:
            msg = f"Failed to download from {location}: {e}"
            raise RuntimeError(msg) from e

    async def exists(self, location: str) -> bool:
        """Check if HTTP URL is accessible (HEAD request).

        Args:
            location: HTTP/HTTPS URL to check

        Returns:
            True if URL returns 2xx status code
        """
        try:
            async with httpx2.AsyncClient() as client:
                response = await client.head(location, timeout=10.0)
                return response.status_code < _HTTP_SUCCESS_THRESHOLD
        except (httpx2.HTTPError, Exception):
            return False

    def get_size(self, location: str) -> int:
        """Get size of HTTP resource (Content-Length header).

        Args:
            location: HTTP/HTTPS URL

        Returns:
            Size in bytes from Content-Length header, or 0 if unavailable

        Note:
            This is a synchronous operation but uses httpx2 sync client.
            Returns 0 if Content-Length header is not available.
        """
        try:
            with httpx2.Client() as client:
                response = client.head(location, timeout=10.0)
                response.raise_for_status()
                content_length = response.headers.get("content-length")
                return int(content_length) if content_length else 0
        except (httpx2.HTTPError, ValueError, Exception):
            return 0
