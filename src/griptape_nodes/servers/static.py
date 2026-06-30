from __future__ import annotations

import asyncio
import binascii
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urljoin

if TYPE_CHECKING:
    import socket

import anyio
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from rich.logging import RichHandler

from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

# Whether to enable the static server
STATIC_SERVER_ENABLED = os.getenv("STATIC_SERVER_ENABLED", "true").lower() == "true"
# Host of the static server (where uvicorn binds)
STATIC_SERVER_HOST = os.getenv("STATIC_SERVER_HOST", "localhost")
# Port of the static server (where uvicorn binds). Falls back to an OS-assigned free port if unavailable.
STATIC_SERVER_PORT = int(os.getenv("STATIC_SERVER_PORT", "8124"))
# URL path for the static server
STATIC_SERVER_URL = os.getenv("STATIC_SERVER_URL", "/workspace")
# Log level for the static server
STATIC_SERVER_LOG_LEVEL = os.getenv("STATIC_SERVER_LOG_LEVEL", "ERROR").lower()

logger = logging.getLogger("griptape_nodes_api")
logging.getLogger("uvicorn").addHandler(RichHandler(show_time=True, show_path=False, markup=True, rich_tracebacks=True))


async def _create_static_file_upload_url(request: Request) -> dict:
    """Create a URL for uploading a static file.

    Similar to a presigned URL, but for uploading files to the static server.
    """
    base_url = GriptapeNodes.StaticFilesManager().static_server_base_url

    body = await request.json()
    file_path = body["file_path"].lstrip("/")
    url = urljoin(base_url, f"/static-uploads/{file_path}")

    return {"url": url}


async def _create_static_file(request: Request, file_path: str) -> dict:
    """Upload a static file to the static server."""
    if not STATIC_SERVER_ENABLED:
        msg = "Static server is not enabled. Please set STATIC_SERVER_ENABLED to True."
        raise ValueError(msg)

    workspace_directory = GriptapeNodes.ConfigManager().workspace_path
    full_file_path = workspace_directory / file_path

    # Create parent directories if they don't exist
    await anyio.Path(full_file_path.parent).mkdir(parents=True, exist_ok=True)

    data = await request.body()
    try:
        await anyio.Path(full_file_path).write_bytes(data)
    except binascii.Error as e:
        msg = f"Invalid base64 encoding for file {file_path}."
        logger.error(msg)
        raise HTTPException(status_code=400, detail=msg) from e
    except (OSError, PermissionError) as e:
        msg = f"Failed to write file {full_file_path}: {e}"
        logger.error(msg)
        raise HTTPException(status_code=500, detail=msg) from e

    base_url = GriptapeNodes.StaticFilesManager().static_server_base_url
    static_url = urljoin(f"{base_url}{STATIC_SERVER_URL}/", file_path)
    return {"url": static_url}


async def _list_static_files(file_path_prefix: str = "") -> dict:
    """List static files in the static server under the specified path prefix."""
    if not STATIC_SERVER_ENABLED:
        msg = "Static server is not enabled. Please set STATIC_SERVER_ENABLED to True."
        raise HTTPException(status_code=500, detail=msg)

    workspace_directory = GriptapeNodes.ConfigManager().workspace_path

    # Handle the prefix path
    if file_path_prefix:
        target_directory = workspace_directory / file_path_prefix
    else:
        target_directory = workspace_directory

    try:
        anyio_target = anyio.Path(target_directory)
        file_names = []
        if await anyio_target.exists() and await anyio_target.is_dir():
            async for file_path in anyio_target.rglob("*"):
                if await file_path.is_file():
                    relative_path = file_path.relative_to(workspace_directory)
                    file_names.append(str(relative_path))
    except (OSError, PermissionError) as e:
        msg = f"Failed to list files in static directory: {e}"
        logger.error(msg)
        raise HTTPException(status_code=500, detail=msg) from e
    else:
        return {"files": file_names}


async def _delete_static_file(file_path: str) -> dict:
    """Delete a static file from the static server."""
    if not STATIC_SERVER_ENABLED:
        msg = "Static server is not enabled. Please set STATIC_SERVER_ENABLED to True."
        raise HTTPException(status_code=500, detail=msg)

    workspace_directory = GriptapeNodes.ConfigManager().workspace_path
    file_full_path = workspace_directory / file_path

    anyio_file_path = anyio.Path(file_full_path)

    # Check if file exists
    if not await anyio_file_path.exists():
        logger.warning("File not found for deletion: %s", file_path)
        raise HTTPException(status_code=404, detail=f"File {file_path} not found")

    # Check if it's actually a file (not a directory)
    if not await anyio_file_path.is_file():
        msg = f"Path {file_path} is not a file"
        logger.error(msg)
        raise HTTPException(status_code=400, detail=msg)

    try:
        # TODO: Replace with DeleteFileRequest https://github.com/griptape-ai/griptape-nodes/issues/3765
        await anyio_file_path.unlink()
    except (OSError, PermissionError) as e:
        msg = f"Failed to delete file {file_path}: {e}"
        logger.error(msg)
        raise HTTPException(status_code=500, detail=msg) from e
    else:
        logger.info("Successfully deleted static file: %s", file_path)
        return {"message": f"File {file_path} deleted successfully"}


async def _serve_library_widget(library_name: str, file_path: str) -> FileResponse:
    """Serve a widget bundle file from a library.

    Widgets are pre-built ES module bundles that libraries can provide
    for custom parameter UI rendering in the frontend.

    Args:
        library_name: Name of the library containing the widget
        file_path: Relative path to the widget bundle within the library directory

    Returns:
        FileResponse containing the JavaScript bundle

    Raises:
        HTTPException: If library not found, file not found, or path traversal detected
    """
    library_manager = GriptapeNodes.LibraryManager()

    # Find the library's directory by looking up its info
    library_info = library_manager.get_library_info_by_library_name(library_name)
    if library_info is None:
        raise HTTPException(
            status_code=404,
            detail=f"Failed to load widget '{file_path}': library '{library_name}' not found",
        )
    library_dir = Path(library_info.library_path).parent

    # Construct full path to the widget file
    full_path = library_dir / file_path

    # Security: Ensure the resolved path is within the library directory
    try:
        resolved_path = await anyio.Path(full_path).resolve()
        resolved_library_dir = await anyio.Path(library_dir).resolve()
        if not resolved_path.is_relative_to(resolved_library_dir):
            logger.warning(
                "Path traversal attempt detected while loading widget from library '%s': %s",
                library_name,
                file_path,
            )
            raise HTTPException(status_code=403, detail="Access denied")
    except ValueError:
        raise HTTPException(status_code=403, detail="Access denied") from None

    # Check if file exists
    if not await resolved_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Widget file '{file_path}' not found in library '{library_name}'",
        )

    if not await resolved_path.is_file():
        raise HTTPException(
            status_code=400,
            detail=f"Widget path '{file_path}' in library '{library_name}' is not a file",
        )

    # Determine content type based on file extension
    content_type = "application/javascript"
    if file_path.endswith(".css"):
        content_type = "text/css"
    elif file_path.endswith(".json"):
        content_type = "application/json"

    return FileResponse(
        path=resolved_path,
        media_type=content_type,
        headers={
            "Cache-Control": "public, max-age=3600",  # Cache for 1 hour
        },
    )


async def _serve_external_file(file_path: str) -> FileResponse:
    """Serve a file from outside the workspace.

    Args:
        file_path: The file path without leading slash (e.g., "tmp/video.mp4" for "/tmp/video.mp4")
    """
    if not STATIC_SERVER_ENABLED:
        msg = "Static server is not enabled. Please set STATIC_SERVER_ENABLED to True."
        raise HTTPException(status_code=500, detail=msg)

    # Reconstruct absolute path.
    # On Windows, the path already starts with a drive letter (e.g., "C:/Users/...") and is absolute.
    # On Unix, the leading slash was stripped, so it needs to be re-added.
    candidate = Path(file_path)
    if candidate.is_absolute():
        absolute_path = candidate
    else:
        absolute_path = Path(f"/{file_path}")

    anyio_absolute_path = anyio.Path(absolute_path)

    # Check if file exists
    if not await anyio_absolute_path.exists():
        logger.debug("External file not found: %s", absolute_path)
        raise HTTPException(status_code=404, detail=f"File {absolute_path} not found")

    # Check if it's actually a file (not a directory)
    if not await anyio_absolute_path.is_file():
        msg = f"Path {absolute_path} is not a file"
        logger.error(msg)
        raise HTTPException(status_code=400, detail=msg)

    # Serve the file
    return FileResponse(absolute_path)


class WorkspaceStaticFiles(StaticFiles):
    """A StaticFiles mount whose served directory tracks the active workspace.

    Starlette's StaticFiles freezes its directory list at construction. The engine's
    workspace can change at runtime (project switches, workspace_dir overrides), so this
    subclass recomputes the served directory from ConfigManager on every lookup, mirroring
    how the storage drivers read workspace_directory live rather than caching it.
    """

    def __init__(self, *, subdirectory: str | None = None) -> None:
        self._subdirectory = subdirectory
        # check_dir=False: the live workspace directory may not exist yet at construction
        # time, and it can change after the server starts.
        super().__init__(directory=self._current_directory(), check_dir=False)

    def lookup_path(self, path: str) -> tuple[str, os.stat_result | None]:
        # Refresh the served directory so it follows the active workspace before delegating
        # to Starlette's path-traversal-safe lookup.
        self.all_directories = [self._current_directory()]
        return super().lookup_path(path)

    def _current_directory(self) -> Path:
        workspace_directory = GriptapeNodes.ConfigManager().workspace_path
        if self._subdirectory is None:
            return workspace_directory
        return workspace_directory / self._subdirectory


def start_static_server(sock: socket.socket) -> None:
    """Run uvicorn server synchronously using a pre-bound socket.

    The socket should already be bound to the desired address and port before calling
    this function. Using a pre-bound socket avoids race conditions when discovering
    the actual port assigned by the OS.
    """
    logger.debug("Starting static server...")

    # Create FastAPI app
    app = FastAPI()

    # Register routes
    app.add_api_route("/static-upload-urls", _create_static_file_upload_url, methods=["POST"])
    app.add_api_route("/static-uploads/{file_path:path}", _create_static_file, methods=["PUT"])
    app.add_api_route("/static-uploads/{file_path_prefix:path}", _list_static_files, methods=["GET"])
    app.add_api_route("/static-uploads/", _list_static_files, methods=["GET"])
    app.add_api_route("/static-files/{file_path:path}", _delete_static_file, methods=["DELETE"])
    # Route for serving widget bundles from libraries
    # The file_path is relative to the library directory (e.g., "widgets/dist/MyWidget.js")
    app.add_api_route(
        "/api/libraries/{library_name}/widgets/{file_path:path}",
        _serve_library_widget,
        methods=["GET"],
    )
    app.add_api_route("/external/{file_path:path}", _serve_external_file, methods=["GET"])

    # Build CORS allowed origins list
    allowed_origins = [
        os.getenv("GRIPTAPE_NODES_UI_BASE_URL", "https://app.nodes.griptape.ai"),
        "https://app.nodes-staging.griptape.ai",
        "https://app-nightly.nodes.griptape.ai",
        "https://editor.nodes.griptape.ai",
        "https://editor-nightly.nodes.griptape.ai",
        "http://localhost:5173",
        "http://localhost:5174",
        "gtn-editor://editor",
        GriptapeNodes.StaticFilesManager().static_server_base_url,
    ]

    # Add CORS middleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_credentials=True,
        allow_methods=["OPTIONS", "GET", "POST", "PUT", "DELETE"],
        allow_headers=["*"],
        allow_private_network=True,  # Required for Starlette 0.51+ to allow localhost access from public origins
    )

    # Mount static files. The served directories resolve the workspace live on each request
    # (see WorkspaceStaticFiles) so they follow runtime workspace changes such as project switches.
    workspace_directory = GriptapeNodes.ConfigManager().workspace_path
    static_files_directory = GriptapeNodes.ConfigManager().get_config_value("static_files_directory")

    app.mount(
        STATIC_SERVER_URL,
        WorkspaceStaticFiles(),
        name="workspace",
    )
    static_files_path = workspace_directory / static_files_directory
    static_files_path.mkdir(parents=True, exist_ok=True)
    # For legacy urls
    app.mount(
        "/static",
        WorkspaceStaticFiles(subdirectory=static_files_directory),
        name="static",
    )

    try:
        config = uvicorn.Config(app, log_level=STATIC_SERVER_LOG_LEVEL, log_config=None)
        server = uvicorn.Server(config)
        asyncio.run(server.serve(sockets=[sock]))
    except Exception as e:
        logger.error("API server failed: %s", e)
        raise
