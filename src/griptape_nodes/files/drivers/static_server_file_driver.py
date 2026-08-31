"""File driver for static file server URLs.

Intercepts http://localhost:PORT/workspace/... URLs and reads the files
directly from the workspace directory on disk, avoiding unnecessary
HTTP round-trips through the dev server.
"""

from pathlib import Path
from urllib.parse import urlparse

import anyio

from griptape_nodes.files.base_file_driver import BaseFileDriver
from griptape_nodes.files.path_utils import parse_static_server_url
from griptape_nodes.retained_mode.engine import current_engine


class StaticServerFileDriver(BaseFileDriver):
    """File driver for static file server URLs.

    Handles URLs matching http(s)://localhost:PORT/workspace/... by extracting
    the workspace-relative path and reading directly from disk.
    """

    @property
    def priority(self) -> int:
        """Return priority 5 to be checked before HttpFileDriver (50).

        Returns:
            Priority value of 5
        """
        return 5

    def can_handle(self, location: str) -> bool:
        """Check if location is a localhost URL with /workspace/ path.

        Args:
            location: Location string to check

        Returns:
            True if location is a localhost URL with /workspace/ path
        """
        if not location.startswith(("http://localhost:", "https://localhost:")):
            return False
        parsed = urlparse(location)
        return "/workspace/" in parsed.path

    def _resolve_to_local_path(self, location: str) -> Path:
        """Resolve a localhost URL to the actual file path on disk.

        Args:
            location: Localhost URL

        Returns:
            Resolved local file Path

        Raises:
            ValueError: If URL format is invalid
        """
        workspace_path = current_engine().config_manager.workspace_path
        local_path = parse_static_server_url(location, workspace_path)

        if local_path is None:
            msg = f"Attempted to resolve localhost URL. Failed with url='{location}' because /workspace/ not found in path."
            raise ValueError(msg)

        return local_path

    async def read(self, location: str, timeout: float) -> bytes:  # noqa: ARG002, ASYNC109
        """Read file from workspace path resolved from localhost URL.

        Args:
            location: Localhost workspace URL
            timeout: Ignored for local file reads

        Returns:
            File contents as bytes

        Raises:
            FileNotFoundError: File does not exist at resolved path
            IsADirectoryError: Path is a directory
        """
        anyio_path = anyio.Path(self._resolve_to_local_path(location))

        if not await anyio_path.exists():
            msg = f"Attempted to read file from localhost URL. Failed with url='{location}' because file not found at resolved path: {anyio_path}"
            raise FileNotFoundError(msg)

        if not await anyio_path.is_file():
            msg = f"Attempted to read file from localhost URL. Failed with url='{location}' because path is a directory: {anyio_path}"
            raise IsADirectoryError(msg)

        return await anyio_path.read_bytes()

    async def exists(self, location: str) -> bool:
        """Check if file exists at resolved workspace path.

        Args:
            location: Localhost workspace URL

        Returns:
            True if file exists and is a regular file
        """
        try:
            anyio_path = anyio.Path(self._resolve_to_local_path(location))
        except ValueError:
            return False
        return await anyio_path.exists() and await anyio_path.is_file()

    def get_size(self, location: str) -> int:
        """Get file size from resolved workspace path.

        Args:
            location: Localhost workspace URL

        Returns:
            File size in bytes

        Raises:
            FileNotFoundError: File does not exist
            IsADirectoryError: Path is a directory
        """
        path = self._resolve_to_local_path(location)

        if not path.exists():
            msg = f"Attempted to get file size from localhost URL. Failed with url='{location}' because file not found at resolved path: {path}"
            raise FileNotFoundError(msg)

        if not path.is_file():
            msg = f"Attempted to get file size from localhost URL. Failed with url='{location}' because path is a directory: {path}"
            raise IsADirectoryError(msg)

        return path.stat().st_size
