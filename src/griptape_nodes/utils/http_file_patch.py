r"""Monkey-patch httpx, httpx2, and requests to transparently handle file:// URLs, local file paths, and cloud assets.

This module patches the httpx, httpx2, and requests libraries at runtime to support:
- file:// URLs
- Absolute local file paths (e.g., /path/to/file.txt, C:\path\to\file.txt)
- Network paths (UNC paths like \\server\share\file.txt, if accessible)
- Griptape Cloud asset URLs (automatically converted to signed download URLs)

File operations are mapped to HTTP-like responses for seamless integration with
existing code. HTTP/HTTPS/FTP URLs are fast-pathed to avoid filesystem checks.
Cloud asset URLs matching pattern /buckets/{id}/assets/{path} are automatically
converted to presigned download URLs when credentials are available.
"""

import functools
import json
import logging
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import urlparse
from urllib.request import url2pathname

import httpx
import httpx2
import requests

from griptape_nodes.utils.url_utils import get_content_type_from_extension

logger = logging.getLogger("griptape_nodes")

# HTTP status code constants
HTTP_OK = 200
HTTP_MULTIPLE_CHOICES = 300
HTTP_BAD_REQUEST = 400
HTTP_FORBIDDEN = 403
HTTP_NOT_FOUND = 404
HTTP_INTERNAL_SERVER_ERROR = 500
HTTP_ERROR_THRESHOLD = 600

_HTTP_VERBS = ("GET", "POST", "PUT", "DELETE", "PATCH")

# Unpatched httpx2.request, used for cloud URL signing so the signing call does not recurse into the patch
_original_signing_request: Any = None
_original_requests_get: Any = None

_patches_installed = False


def _is_http_url(url_str: str) -> bool:
    """Quick check for http/https/ftp URLs to bypass filesystem checks."""
    return url_str.startswith(("http://", "https://", "ftp://", "ftps://"))


def _is_local_file_path(url_str: str) -> bool:
    """Check if string is an absolute file path that exists.

    Excludes URLs that already have schemes (file://, http://, etc.).
    Network paths (UNC) are allowed if they exist and are accessible.

    Args:
        url_str: String to check

    Returns:
        True if url_str is an absolute file path that exists
    """
    # Exclude URLs with schemes - handle them via existing logic
    if "://" in url_str:
        return False

    # Check if absolute path that exists
    try:
        path = Path(url_str)
        return path.is_absolute() and path.exists()
    except (ValueError, OSError):
        return False


class FileHttpxResponse:
    """Response wrapper that mimics the httpx/httpx2 Response interface for file:// URLs."""

    def __init__(self, content: bytes, status_code: int, file_path: str, *, http_module: ModuleType = httpx2):
        """Initialize file response.

        Args:
            content: File content as bytes
            status_code: HTTP status code (200 for success, 404/403/etc for errors)
            file_path: Path to the file for MIME type detection
            http_module: httpx or httpx2, whichever the caller used, so raised errors match what it catches
        """
        self.content = content
        self.status_code = status_code
        self._file_path = file_path
        self._http_module = http_module

        # Build headers dict
        headers_dict = {}
        if status_code == HTTP_OK:
            headers_dict["Content-Length"] = str(len(content))
            content_type = get_content_type_from_extension(file_path)
            if content_type:
                headers_dict["Content-Type"] = content_type

        self.headers = headers_dict

    @property
    def text(self) -> str:
        """Return content as text string."""
        return self.content.decode("utf-8", errors="replace")

    def raise_for_status(self) -> None:
        """Raise HTTPStatusError for error status codes (4xx, 5xx)."""
        if HTTP_BAD_REQUEST <= self.status_code < HTTP_ERROR_THRESHOLD:
            msg = f"File error: {self.status_code} for file:// URL: {self._file_path}"
            # Create a minimal request object for the exception
            request = self._http_module.Request("GET", self._file_path)
            raise self._http_module.HTTPStatusError(msg, request=request, response=self)

    def json(self) -> Any:
        """Parse content as JSON."""
        return json.loads(self.text)


class FileRequestsResponse:
    """Response wrapper that mimics requests.Response interface for file:// URLs."""

    def __init__(self, content: bytes, status_code: int, file_path: str):
        """Initialize file response.

        Args:
            content: File content as bytes
            status_code: HTTP status code (200 for success, 404/403/etc for errors)
            file_path: Path to the file for MIME type detection
        """
        self.content = content
        self.status_code = status_code
        self._file_path = file_path

        # Build headers dict
        headers_dict = {}
        if status_code == HTTP_OK:
            headers_dict["Content-Length"] = str(len(content))
            content_type = get_content_type_from_extension(file_path)
            if content_type:
                headers_dict["Content-Type"] = content_type

        self.headers = headers_dict
        self.ok = HTTP_OK <= status_code < HTTP_MULTIPLE_CHOICES

    @property
    def text(self) -> str:
        """Return content as text string."""
        return self.content.decode("utf-8", errors="replace")

    def raise_for_status(self) -> None:
        """Raise HTTPError for error status codes (4xx, 5xx)."""
        if HTTP_BAD_REQUEST <= self.status_code < HTTP_ERROR_THRESHOLD:
            msg = f"File error: {self.status_code} for file:// URL: {self._file_path}"
            raise requests.HTTPError(msg, response=self)  # type: ignore[arg-type]

    def json(self) -> Any:
        """Parse content as JSON."""
        return json.loads(self.text)


def _handle_file_url(url: str, *, response_type: Callable[..., Any]) -> FileHttpxResponse | FileRequestsResponse:
    """Handle file:// URL by reading local file and returning HTTP-like response.

    Args:
        url: file:// URL to handle
        response_type: Builds the response (FileHttpxResponse or FileRequestsResponse)

    Returns:
        Response wrapper with file content or error status
    """
    # Validate input
    if not url.startswith("file://"):
        return response_type(
            content=b"",
            status_code=HTTP_BAD_REQUEST,
            file_path=url,
        )

    # Extract file path from file:// URL (same pattern as static_files_manager.py:193-196)
    parsed = urlparse(url)
    file_path_str = url2pathname(parsed.path)
    file_path = Path(file_path_str)

    # Check if file exists
    if not file_path.exists():
        error_msg = f"File not found: {file_path_str}"
        logger.debug(error_msg)
        return response_type(
            content=error_msg.encode("utf-8"),
            status_code=HTTP_NOT_FOUND,
            file_path=file_path_str,
        )

    # Check if path is a directory
    if file_path.is_dir():
        error_msg = f"Path is a directory, not a file: {file_path_str}"
        logger.debug(error_msg)
        return response_type(
            content=error_msg.encode("utf-8"),
            status_code=HTTP_BAD_REQUEST,
            file_path=file_path_str,
        )

    # Try to read file
    try:
        content = file_path.read_bytes()
    except PermissionError:
        error_msg = f"Permission denied: {file_path_str}"
        logger.debug(error_msg)
        return response_type(
            content=error_msg.encode("utf-8"),
            status_code=HTTP_FORBIDDEN,
            file_path=file_path_str,
        )
    except OSError as e:
        error_msg = f"Error reading file: {file_path_str}: {e}"
        logger.debug(error_msg)
        return response_type(
            content=error_msg.encode("utf-8"),
            status_code=HTTP_INTERNAL_SERVER_ERROR,
            file_path=file_path_str,
        )

    # Success - return file content
    return response_type(
        content=content,
        status_code=HTTP_OK,
        file_path=file_path_str,
    )


def _route_request(method: str, url: Any, response_type: Callable[..., Any]) -> Any:
    """Decide where a request goes.

    Returns:
        A FileHttpxResponse for file:// URLs and local paths, otherwise the URL to send,
        swapped for a signed download URL when it is a cloud asset GET.
    """
    # Lazy import to avoid circular dependency: utils/__init__.py -> http_file_patch -> storage drivers -> os_events -> payload_registry
    from griptape_nodes.drivers.storage.griptape_cloud_storage_driver import GriptapeCloudStorageDriver

    url_str = str(url)

    # Detect and convert cloud asset URLs to signed download URLs (GET only)
    if method.upper() == "GET" and GriptapeCloudStorageDriver.is_cloud_asset_url(url_str):
        signed_url = GriptapeCloudStorageDriver.create_signed_download_url_from_asset_url(
            url_str, httpx_request_func=_original_signing_request
        )
        if signed_url:
            return signed_url
        # If conversion failed, continue with original URL

    # Fast path: Skip filesystem checks for HTTP/HTTPS/FTP URLs (99%+ of requests)
    if _is_http_url(url_str):
        return url

    if url_str.startswith("file://"):
        return _handle_file_url(url_str, response_type=response_type)

    if _is_local_file_path(url_str):
        return _handle_file_url(str(Path(url_str).as_uri()), response_type=response_type)

    return url


def _patch_httpx_module(module: ModuleType) -> None:
    """Patch an httpx-compatible module's request helpers and client `request` methods.

    Client verb methods (`Client.get`, etc.) route through `Client.request`, so patching
    `request` covers them. Module-level verbs call the unpatched internal `request`, so each
    one is patched.
    """
    original_request = module.request
    original_client_request = module.Client.request
    original_async_client_request = module.AsyncClient.request
    response_type = functools.partial(FileHttpxResponse, http_module=module)

    def patched_request(method: str, url: Any, **kwargs: Any) -> Any:
        target = _route_request(method, url, response_type)
        if isinstance(target, FileHttpxResponse):
            return target
        return original_request(method, target, **kwargs)

    def patched_client_request(self: Any, method: str, url: Any, **kwargs: Any) -> Any:
        target = _route_request(method, url, response_type)
        if isinstance(target, FileHttpxResponse):
            return target
        return original_client_request(self, method, target, **kwargs)

    async def patched_async_client_request(self: Any, method: str, url: Any, **kwargs: Any) -> Any:
        # Synchronous file read is fine in async context
        target = _route_request(method, url, response_type)
        if isinstance(target, FileHttpxResponse):
            return target
        return await original_async_client_request(self, method, target, **kwargs)

    setattr(module, "request", patched_request)  # noqa: B010
    for verb in _HTTP_VERBS:
        setattr(module, verb.lower(), functools.partial(patched_request, verb))
    module.Client.request = patched_client_request
    module.AsyncClient.request = patched_async_client_request


def _patched_requests_get(url: str, **kwargs: Any) -> requests.Response | FileRequestsResponse:
    """Patched requests.get that handles file:// URLs, local file paths, and cloud asset URLs.

    Args:
        url: URL to request (file://, http://, https://, cloud asset, etc.) or absolute file path
        **kwargs: Additional arguments for requests.get

    Returns:
        requests.Response or FileRequestsResponse
    """
    # Lazy import to avoid circular dependency: utils/__init__.py -> http_file_patch -> storage drivers -> os_events -> payload_registry
    from griptape_nodes.drivers.storage.griptape_cloud_storage_driver import GriptapeCloudStorageDriver

    # Detect and convert cloud asset URLs to signed download URLs
    if GriptapeCloudStorageDriver.is_cloud_asset_url(url):
        signed_url = GriptapeCloudStorageDriver.create_signed_download_url_from_asset_url(
            url, httpx_request_func=_original_signing_request
        )
        if signed_url:
            return _original_requests_get(signed_url, **kwargs)
        # If conversion failed, continue with original URL

    # Fast path: Skip filesystem checks for HTTP/HTTPS/FTP URLs (99%+ of requests)
    if _is_http_url(url):
        return _original_requests_get(url, **kwargs)

    # Handle existing file:// URLs
    if url.startswith("file://"):
        return _handle_file_url(url, response_type=FileRequestsResponse)  # type: ignore[return-value]

    # Detect and convert local file paths
    if _is_local_file_path(url):
        file_url = str(Path(url).as_uri())
        return _handle_file_url(file_url, response_type=FileRequestsResponse)  # type: ignore[return-value]

    # Delegate all other URLs to original requests.get
    return _original_requests_get(url, **kwargs)


def install_file_url_support() -> None:
    """Install file:// URL support by patching httpx, httpx2, and requests at module level.

    This should be called once at app initialization. Subsequent calls are no-ops.
    """
    global _patches_installed  # noqa: PLW0603
    global _original_signing_request  # noqa: PLW0603
    global _original_requests_get  # noqa: PLW0603

    # Prevent double-installation
    if _patches_installed:
        logger.debug("file:// URL support already installed, skipping")
        return

    logger.debug("Installing file:// URL support for httpx, httpx2, and requests")

    _original_signing_request = httpx2.request
    _original_requests_get = requests.get

    # httpx stays patched for node libraries that still call it directly
    _patch_httpx_module(httpx)
    _patch_httpx_module(httpx2)
    requests.get = _patched_requests_get  # type: ignore[assignment]

    _patches_installed = True
    logger.debug("file:// URL support installed successfully")
