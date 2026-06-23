from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from huggingface_hub import get_token, list_models, scan_cache_dir, snapshot_download
from huggingface_hub import model_info as hf_model_info
from huggingface_hub.utils.tqdm import tqdm
from xdg_base_dirs import xdg_data_home

from griptape_nodes.files.file import File, FileWriteError
from griptape_nodes.retained_mode.events.app_events import AppInitializationComplete
from griptape_nodes.retained_mode.events.model_events import (
    DeclareModelInvocationRequest,
    DeclareModelInvocationResultFailure,
    DeclareModelInvocationResultSuccess,
    DeleteModelDownloadRequest,
    DeleteModelDownloadResultFailure,
    DeleteModelDownloadResultSuccess,
    DeleteModelRequest,
    DeleteModelResultFailure,
    DeleteModelResultSuccess,
    DownloadModelRequest,
    DownloadModelResultFailure,
    DownloadModelResultSuccess,
    GetModelInfoRequest,
    GetModelInfoResultFailure,
    GetModelInfoResultSuccess,
    ListModelDownloadsRequest,
    ListModelDownloadsResultFailure,
    ListModelDownloadsResultSuccess,
    ListModelsRequest,
    ListModelsResultFailure,
    ListModelsResultSuccess,
    ModelDownloadStatus,
    ModelInfo,
    QueryInfo,
    SearchModelsRequest,
    SearchModelsResultFailure,
    SearchModelsResultSuccess,
)
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
from griptape_nodes.retained_mode.managers.authorization_checkpoint import (
    AuthorizationCheckpoint,
    CheckpointAction,
    CheckpointAttribute,
    CheckpointSubjectType,
)
from griptape_nodes.retained_mode.managers.settings import MODELS_TO_DOWNLOAD_KEY
from griptape_nodes.utils.async_utils import cancel_subprocess

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.events.base_events import ResultPayload
    from griptape_nodes.retained_mode.managers.event_manager import EventManager

logger = logging.getLogger("griptape_nodes")


HTTP_UNAUTHORIZED = 401
HTTP_FORBIDDEN = 403

MIN_CACHE_DIR_PARTS = 3


@dataclass
class SearchResultsData:
    """Data class for model search results."""

    models: list[ModelInfo]
    total_results: int
    query_info: QueryInfo


@dataclass
class DownloadParams:
    """Data class for model download parameters."""

    model_id: str
    local_dir: str | None = None
    revision: str | None = None
    allow_patterns: list[str] | None = None
    ignore_patterns: list[str] | None = None


_DOWNLOAD_PROGRESS_EMIT_INTERVAL = 1.0  # seconds between stdout progress events
_PROGRESS_PIPE_ENV_VAR = "GRIPTAPE_NODES_PROGRESS_PIPE"  # set by parent to enable JSON stdout emission


def _create_progress_tracker(model_id: str) -> type[tqdm]:  # noqa: C901
    """Create a tqdm class with model_id pre-configured.

    Args:
        model_id: The model ID to track progress for

    Returns:
        A tqdm class that will track progress for the given model
    """
    logger.info("Creating progress tracker for model: %s", model_id)

    class BoundModelDownloadTracker(tqdm):
        """Tqdm subclass that emits JSON progress events to stdout for the parent process to handle."""

        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self.model_id = model_id
            self._cumulative_bytes = 0
            self._last_emit_time = 0.0
            # Only emit JSON progress events when spawned by the main process, not on direct CLI invocation
            self._emit_progress = os.environ.get(_PROGRESS_PIPE_ENV_VAR) == "1"

            # Check if this is a byte-level progress bar or file enumeration bar
            unit = getattr(self, "unit", "")
            desc = getattr(self, "desc", "")

            # Skip file enumeration bars (unit='it', desc='Fetching N files')
            # We only want to track byte-level download progress
            self._should_track = not (unit == "it" and "Fetching" in str(desc))

            if not self._should_track:
                logger.debug(
                    "ModelDownloadTracker skipping file enumeration bar - model_id: %s, desc: '%s'",
                    self.model_id,
                    desc,
                )
                return

            logger.debug(
                "ModelDownloadTracker instantiated for tracking - model_id: %s, total: %s, unit: %s, desc: '%s'",
                self.model_id,
                self.total,
                unit,
                desc,
            )

        def update(self, n: int = 1) -> None:
            """Override update to emit rate-limited JSON progress to stdout."""
            super().update(n)
            self._cumulative_bytes += n

            if not getattr(self, "_should_track", True):
                return

            if not self._emit_progress:
                logger.debug(
                    "ModelDownloadTracker update - model_id: %s, added: %s, now: %s/%s (%.1f%%)",
                    self.model_id,
                    n,
                    self._cumulative_bytes,
                    self.total,
                    (self._cumulative_bytes / self.total * 100) if self.total else 0,
                )
                return

            # Rate-limit stdout emissions to once per second to avoid overwhelming WriteFileRequest
            now = datetime.now(UTC).timestamp()
            if now - self._last_emit_time >= _DOWNLOAD_PROGRESS_EMIT_INTERVAL:
                self._last_emit_time = now
                progress_percent = (self._cumulative_bytes / self.total * 100) if self.total else 0
                # Write a JSON progress event directly to stdout so the parent process can read it
                # from the subprocess stdout pipe and write the status file via File.write_text.
                sys.stdout.write(
                    json.dumps(
                        {
                            "downloaded_bytes": self._cumulative_bytes,
                            "total_bytes": self.total or 0,
                            "progress_percent": progress_percent,
                        }
                    )
                    + "\n"
                )
                sys.stdout.flush()

        def close(self) -> None:
            """Override close to emit a final progress event to stdout."""
            super().close()

            if not getattr(self, "_should_track", True):
                return

            is_complete = self.total > 0 and self._cumulative_bytes >= self.total
            progress_percent = (self._cumulative_bytes / self.total * 100) if self.total else 0

            if not self._emit_progress:
                if is_complete:
                    logger.info(
                        "ModelDownloadTracker closed - model_id: %s, downloaded: %s/%s bytes (COMPLETE)",
                        self.model_id,
                        self._cumulative_bytes,
                        self.total,
                    )
                else:
                    logger.warning(
                        "ModelDownloadTracker closed prematurely - model_id: %s, downloaded: %s/%s bytes (%.1f%%)",
                        self.model_id,
                        self._cumulative_bytes,
                        self.total,
                        progress_percent,
                    )
                return

            # Write a final JSON progress event directly to stdout so the parent process can read it
            # from the subprocess stdout pipe and write the terminal status via File.write_text.
            sys.stdout.write(
                json.dumps(
                    {
                        "downloaded_bytes": self._cumulative_bytes,
                        "total_bytes": self.total or 0,
                        "progress_percent": progress_percent,
                        "completed": is_complete,
                    }
                )
                + "\n"
            )
            sys.stdout.flush()

    return BoundModelDownloadTracker


class ModelManager:
    """A manager for downloading models from Hugging Face Hub.

    This manager provides async handlers for downloading models using the Hugging Face Hub API.
    It supports downloading entire model repositories or specific files, with caching and
    local storage management.
    """

    def __init__(self, event_manager: EventManager | None = None) -> None:
        """Initialize the ModelManager.

        Args:
            event_manager: The EventManager instance to use for event handling.
        """
        self._download_tasks = {}
        self._download_processes = {}

        if event_manager is not None:
            event_manager.assign_manager_to_request_type(DownloadModelRequest, self.on_handle_download_model_request)
            event_manager.assign_manager_to_request_type(ListModelsRequest, self.on_handle_list_models_request)
            event_manager.assign_manager_to_request_type(DeleteModelRequest, self.on_handle_delete_model_request)
            event_manager.assign_manager_to_request_type(SearchModelsRequest, self.on_handle_search_models_request)
            event_manager.assign_manager_to_request_type(GetModelInfoRequest, self.on_handle_get_model_info_request)
            event_manager.assign_manager_to_request_type(
                DeclareModelInvocationRequest, self.on_handle_declare_model_invocation_request
            )
            event_manager.assign_manager_to_request_type(
                ListModelDownloadsRequest, self.on_handle_list_model_downloads_request
            )
            event_manager.assign_manager_to_request_type(
                DeleteModelDownloadRequest, self.on_handle_delete_model_download_request
            )

            event_manager.add_listener_to_app_event(AppInitializationComplete, self.on_app_initialization_complete)

    def download_model(
        self,
        model_id: str,
        local_dir: str | None = None,
        revision: str = "main",
        allow_patterns: list[str] | None = None,
        ignore_patterns: list[str] | None = None,
    ) -> str:
        """Direct model download method that can be used without event system.

        This method contains the core download logic without going through
        the event system, avoiding recursion issues. It leverages huggingface_hub v1.1.0+
        aggregated tqdm for clean progress tracking across parallel downloads.

        Args:
            model_id: Model ID to download
            local_dir: Optional local directory to download to
            revision: Git revision to download
            allow_patterns: Optional glob patterns to include
            ignore_patterns: Optional glob patterns to exclude

        Returns:
            str: Local path where the model was downloaded

        Raises:
            Exception: If download fails
        """
        # Build download kwargs with progress tracking
        download_kwargs = {
            "repo_id": model_id,
            "repo_type": "model",
            "revision": revision,
            "tqdm_class": _create_progress_tracker(model_id),
        }

        # Add optional parameters
        if local_dir:
            download_kwargs["local_dir"] = local_dir
        if allow_patterns:
            download_kwargs["allow_patterns"] = allow_patterns
        if ignore_patterns:
            download_kwargs["ignore_patterns"] = ignore_patterns

        logger.info("Calling snapshot_download with custom tqdm_class for model: %s", model_id)

        # Execute download with progress tracking
        local_path = snapshot_download(**download_kwargs)  # type: ignore[arg-type]

        return str(local_path)

    def _write_download_status(self, status_file: Path, data: dict) -> None:
        """Write download status data to a file using File.

        Routing all status file writes through File ensures they go through
        os_manager's centralized file I/O with exclusive locking.

        Args:
            status_file: Path to the status file to write
            data: Status data dict to serialize as JSON
        """
        try:
            File(str(status_file)).write_text(json.dumps(data, indent=2))
        except FileWriteError as e:
            logger.warning("Failed to write download status file '%s': %s", status_file, e)

    def _get_status_directory(self) -> Path:
        """Get the status directory path for model downloads.

        Returns:
            Path: Path to the status directory, creating it if needed
        """
        status_dir = xdg_data_home() / "griptape_nodes" / "model_downloads"
        status_dir.mkdir(parents=True, exist_ok=True)
        return status_dir

    async def on_handle_download_model_request(self, request: DownloadModelRequest) -> ResultPayload:
        """Handle model download requests asynchronously.

        This method downloads models from Hugging Face Hub using the provided parameters.
        It supports both model IDs and full URLs, and can download entire repositories
        or specific files based on the patterns provided.

        Args:
            request: The download request containing model ID and options

        Returns:
            ResultPayload: Success result with download completion or failure with error details
        """
        parsed_model_id = self._parse_model_id(request.model_id)
        if parsed_model_id != request.model_id:
            logger.debug("Parsed model ID '%s' from URL '%s'", parsed_model_id, request.model_id)

        if get_token() is None:
            error_msg = (
                "No Hugging Face token found. Set your HF_TOKEN environment variable "
                "or log in with `huggingface-cli login` before downloading models."
            )
            return DownloadModelResultFailure(result_details=error_msg)

        try:
            download_params = DownloadParams(
                model_id=parsed_model_id,
                local_dir=request.local_dir,
                revision=request.revision,
                allow_patterns=request.allow_patterns,
                ignore_patterns=request.ignore_patterns,
            )

            task = asyncio.create_task(self._download_model_task(download_params))
            self._download_tasks[parsed_model_id] = task

            await task
        except asyncio.CancelledError:
            # Handle task cancellation gracefully
            logger.info("Download request cancelled for model '%s'", parsed_model_id)

            return DownloadModelResultSuccess(
                model_id=parsed_model_id,
                result_details=f"Successfully downloaded model '{parsed_model_id}'",
            )

        except Exception as e:
            return DownloadModelResultFailure(
                result_details=str(e),
                exception=e,
            )
        else:
            return DownloadModelResultSuccess(
                model_id=parsed_model_id,
                result_details=f"Successfully downloaded model '{parsed_model_id}'",
            )
        finally:
            # Clean up the task reference
            if parsed_model_id in self._download_tasks:
                del self._download_tasks[parsed_model_id]

    def _get_download_local_path(self, params: DownloadParams) -> str:
        """Get the local path where the model was downloaded.

        Args:
            params: Download parameters

        Returns:
            Local path where the model is stored
        """
        if params.local_dir:
            return params.local_dir

        # Otherwise, use the HuggingFace cache directory
        from huggingface_hub import snapshot_download

        try:
            # Get the path without actually downloading (since it's already downloaded)
            return snapshot_download(
                repo_id=params.model_id,
                repo_type="model",
                revision=params.revision,
                local_files_only=True,  # Only check local cache
            )
        except Exception:
            # Fallback: construct the expected cache path
            from huggingface_hub.constants import HF_HUB_CACHE

            cache_path = Path(HF_HUB_CACHE)
            return str(cache_path / f"models--{params.model_id.replace('/', '--')}")

    async def _stream_download_stdout(
        self,
        process: asyncio.subprocess.Process,
        status_file: Path,
        initial_data: dict,
    ) -> dict | None:
        """Read JSON progress events from subprocess stdout and write to status file.

        Returns the last progress event received, or None if no events were emitted.
        """
        if process.stdout is None:
            return None
        last_progress: dict | None = None
        while True:
            line = await process.stdout.readline()
            if not line:
                break
            try:
                progress = json.loads(line.decode().strip())
                if not isinstance(progress, dict):
                    continue
                last_progress = progress
                updated = {
                    **initial_data,
                    **progress,
                    "status": "downloading",
                    "updated_at": datetime.now(UTC).isoformat(),
                }
                await asyncio.to_thread(self._write_download_status, status_file, updated)
            except json.JSONDecodeError:
                pass
        return last_progress

    async def _collect_download_stderr(self, process: asyncio.subprocess.Process) -> list[str]:
        """Collect stderr lines for error reporting."""
        if process.stderr is None:
            return []
        lines: list[str] = []
        while True:
            line = await process.stderr.readline()
            if not line:
                break
            lines.append(line.decode())
        return lines

    async def _download_model_task(self, download_params: DownloadParams) -> None:  # noqa: C901
        """Background task for downloading a model using CLI command.

        Owns the full status file lifecycle: writes initial status before launching the
        subprocess, streams JSON progress events from subprocess stdout and writes them
        via WriteFileRequest, then writes the final success/failure status after exit.

        Args:
            download_params: Download parameters

        Raises:
            ValueError: If the download subprocess exits with a non-zero return code
        """
        model_id = download_params.model_id
        logger.info("Starting download for model: %s", model_id)

        # Write initial status file before launching subprocess
        status_file = self._get_status_file_path(model_id)
        current_time = datetime.now(UTC).isoformat()
        initial_data: dict = {
            "model_id": model_id,
            "status": "downloading",
            "started_at": current_time,
            "updated_at": current_time,
            "total_bytes": 0,
            "downloaded_bytes": 0,
            "progress_percent": 0.0,
        }
        await asyncio.to_thread(self._write_download_status, status_file, initial_data)

        # Build CLI command
        cmd = [
            sys.executable,
            "-m",
            "griptape_nodes.cli.commands.models",
            "download",
            download_params.model_id,
        ]

        if download_params.local_dir:
            cmd.extend(["--local-dir", download_params.local_dir])
        if download_params.revision and download_params.revision != "main":
            cmd.extend(["--revision", download_params.revision])

        # Start subprocess with progress pipe flag so the tracker emits JSON to stdout
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, _PROGRESS_PIPE_ENV_VAR: "1"},
        )

        try:
            # Store process for cancellation
            self._download_processes[model_id] = process

            # Stream stdout and stderr concurrently to avoid pipe buffer deadlock
            last_progress, stderr_lines = await asyncio.gather(
                self._stream_download_stdout(process, status_file, initial_data),
                self._collect_download_stderr(process),
            )
            await process.wait()

            # Merge the last seen progress so final status preserves downloaded/total bytes
            last_known = {**initial_data, **(last_progress or {})}
            current_time = datetime.now(UTC).isoformat()
            if process.returncode == 0:
                logger.info("Successfully downloaded model '%s'", model_id)
                final_data = {
                    **last_known,
                    "status": "completed",
                    "updated_at": current_time,
                    "completed_at": current_time,
                    "progress_percent": 100.0,
                }
                await asyncio.to_thread(self._write_download_status, status_file, final_data)
            else:
                # Scan stderr for a structured error event emitted by the subprocess CLI
                error_type = None
                error_msg = None
                for line in stderr_lines:
                    try:
                        event = json.loads(line.strip())
                        if isinstance(event, dict) and "error_type" in event:
                            error_type = event.get("error_type")
                            error_msg = event.get("error_message")
                            break
                    except json.JSONDecodeError:
                        pass
                model_url = f"https://huggingface.co/{model_id}"
                if error_type == "gated_repo":
                    error_msg = f"Model '{model_id}' is gated and requires access approval. Visit {model_url} to request access."
                elif error_type == "repo_not_found":
                    error_msg = f"Model '{model_id}' was not found. Check that the model ID is correct at {model_url}."
                elif not error_msg:
                    error_msg = "".join(stderr_lines).strip()
                logger.error("Failed to download model '%s': %s", model_id, error_msg)
                final_data = {
                    **last_known,
                    "status": "failed",
                    "updated_at": current_time,
                    "failed_at": current_time,
                    "error_message": error_msg,
                }
                await asyncio.to_thread(self._write_download_status, status_file, final_data)
                raise ValueError(error_msg)

        finally:
            if model_id in self._download_processes:
                del self._download_processes[model_id]

    async def on_handle_list_models_request(self, request: ListModelsRequest) -> ResultPayload:  # noqa: ARG002
        """Handle model listing requests asynchronously.

        This method scans the local Hugging Face cache directory to find downloaded models
        and returns information about each model including path, size, and metadata.

        Args:
            request: The list request (no parameters needed)

        Returns:
            ResultPayload: Success result with model list or failure with error details
        """
        try:
            # Get models in a thread to avoid blocking the event loop
            models = await asyncio.to_thread(self._list_models)

            result_details = f"Found {len(models)} cached models"

            return ListModelsResultSuccess(
                models=models,
                result_details=result_details,
            )

        except Exception as e:
            error_msg = f"Failed to list models: {e}"
            return ListModelsResultFailure(
                result_details=error_msg,
                exception=e,
            )

    async def on_handle_delete_model_request(self, request: DeleteModelRequest) -> ResultPayload:
        """Handle model deletion requests asynchronously.

        This method removes a model from the local Hugging Face cache directory and
        cleans up any associated download tracking records.

        Args:
            request: The delete request containing model_id

        Returns:
            ResultPayload: Success result with deletion confirmation or failure with error details
        """
        # Parse the model ID from potential URL
        model_id = request.model_id

        deleted_items = []

        try:
            deleted_path = await asyncio.to_thread(self._delete_model, model_id)
            deleted_items.append(f"model files from '{deleted_path}'")

        except FileNotFoundError:
            logger.debug("No model files found for '%s' in cache", model_id)

        except Exception as e:
            error_msg = f"Failed to delete model files for '{model_id}': {e}"
            return DeleteModelResultFailure(
                result_details=error_msg,
                exception=e,
            )

        if not deleted_items:
            error_msg = f"Model '{model_id}' not found (no cached files or download records)"
            return DeleteModelResultFailure(
                result_details=error_msg,
                exception=FileNotFoundError(error_msg),
            )

        deleted_description = " and ".join(deleted_items)
        result_details = f"Successfully deleted {deleted_description} for model '{model_id}'"

        return DeleteModelResultSuccess(
            model_id=model_id,
            deleted_path=deleted_items[0] if deleted_items else "",
            result_details=result_details,
        )

    async def on_handle_search_models_request(self, request: SearchModelsRequest) -> ResultPayload:
        """Handle model search requests asynchronously.

        This method searches for models on Hugging Face Hub using the provided parameters.
        It supports filtering by query, task, library, author, and tags.

        Args:
            request: The search request containing search parameters

        Returns:
            ResultPayload: Success result with model list or failure with error details
        """
        try:
            # Search models in a thread to avoid blocking the event loop
            search_results = await asyncio.to_thread(self._search_models, request)
        except Exception as e:
            error_msg = f"Failed to search models: {e}"
            return SearchModelsResultFailure(
                result_details=error_msg,
                exception=e,
            )
        else:
            result_details = f"Found {len(search_results.models)} models"
            return SearchModelsResultSuccess(
                models=search_results.models,
                total_results=search_results.total_results,
                query_info=search_results.query_info,
                result_details=result_details,
            )

    async def on_handle_get_model_info_request(self, request: GetModelInfoRequest) -> ResultPayload:
        """Fetch detailed info for a specific model from Hugging Face Hub.

        Args:
            request: The request containing the model_id to look up

        Returns:
            ResultPayload: Success with exact size and metadata, or failure with error details
        """
        if get_token() is None:
            error_msg = (
                "No Hugging Face token found. Fetching info for gated models requires authentication. "
                "Set your HF_TOKEN environment variable or log in with `huggingface-cli login`."
            )
            return GetModelInfoResultFailure(result_details=error_msg)

        try:
            info = await asyncio.to_thread(hf_model_info, request.model_id)
        except Exception as e:
            error_msg = f"Attempted to get model info for '{request.model_id}'. Failed because: {e}"
            return GetModelInfoResultFailure(
                result_details=error_msg,
                exception=e,
            )

        safetensors = getattr(info, "safetensors", None)
        safetensors_parameters = dict(safetensors.parameters) if safetensors else None

        return GetModelInfoResultSuccess(
            model_id=request.model_id,
            size_bytes=getattr(info, "used_storage", None),
            safetensors_parameters=safetensors_parameters,
            author=getattr(info, "author", None),
            task=getattr(info, "pipeline_tag", None),
            library=getattr(info, "library_name", None),
            tags=getattr(info, "tags", None),
            downloads=getattr(info, "downloads", None),
            likes=getattr(info, "likes", None),
            result_details=f"Retrieved info for '{request.model_id}'",
        )

    @staticmethod
    def _model_checkpoint_attributes(request: DeclareModelInvocationRequest) -> dict[str, Any]:
        """Resolve the facts a hook may gate a model invocation on.

        `id` is the concrete model; `provider_id` is the catalog provider the call
        routes to (when the node declared one). The app maps `provider_id` onto
        the `Model in ModelProvider` hierarchy a policy walks via `in`.
        """
        attributes: dict[str, Any] = {CheckpointAttribute.ID: request.model}
        if request.provider_id:
            attributes[CheckpointAttribute.PROVIDER_ID] = request.provider_id
        return attributes

    def on_handle_declare_model_invocation_request(self, request: DeclareModelInvocationRequest) -> ResultPayload:
        """Acknowledge a node's declaration that it is about to invoke a model.

        This request is how a well-intentioned node opts into the permission
        system: it declares the invocation and the engine decides whether it is
        permitted. Enforcement runs entirely in the event manager's
        pre-dispatch hook chain before this handler is reached, so arriving here
        means the invocation is sanctioned. The node performs the actual
        inference itself; this handler does not run any backend.

        Args:
            request: The declared invocation, identifying the catalog model

        Returns:
            ResultPayload: Success, meaning the node is cleared to proceed
        """
        # License-policy checkpoint: gate the declared invocation on the model and
        # its provider. The node already opted in by declaring; a denial returns a
        # failure so the node does not invoke the model. Provider/family hierarchy
        # is resolved app-side from the declared provider_id.
        denial = GriptapeNodes.EventManager().evaluate_authorization_checkpoint(
            AuthorizationCheckpoint(
                action=CheckpointAction.INVOKE_MODEL,
                subject_type=CheckpointSubjectType.MODEL,
                subject_id=request.model,
                attributes=self._model_checkpoint_attributes(request),
            )
        )
        if denial is not None:
            reason = denial.reason()
            return DeclareModelInvocationResultFailure(
                result_details=f"Model invocation denied for '{request.model}'. {reason}"
            )
        return DeclareModelInvocationResultSuccess(
            model_id=request.model_id,
            result_details=f"Model invocation permitted for '{request.model_id}'.",
        )

    def _search_models(self, request: SearchModelsRequest) -> SearchResultsData:
        """Synchronous model search implementation.

        Searches for models on Hugging Face Hub using the huggingface_hub API.

        Args:
            request: The search request parameters

        Returns:
            SearchResultsData: Dataclass containing models list, total results, and query info
        """
        # Build search parameters
        search_params = {}

        if request.query:
            search_params["search"] = request.query
        if request.task:
            search_params["task"] = request.task
        if request.library:
            search_params["library"] = request.library
        if request.author:
            search_params["author"] = request.author
        if request.tags:
            search_params["tags"] = request.tags

        # Validate and set sort parameters
        valid_sorts = ["downloads", "likes", "updated", "created"]
        sort_param = request.sort if request.sort in valid_sorts else "downloads"
        search_params["sort"] = sort_param

        # Only add direction for sorts that support it (downloads only supports descending)
        if sort_param != "downloads":
            # Convert direction to the format expected by HF Hub API (-1 for asc, 1 for desc)
            direction_param = -1 if request.direction == "asc" else 1
            search_params["direction"] = direction_param

        # Limit results (max 100 as per HF Hub API)
        limit = min(max(1, request.limit), 100)

        # Perform the search
        models_iterator = list_models(limit=limit, **search_params)

        # Convert models to list and extract information
        models_list = []
        for model in models_iterator:
            created_at = getattr(model, "created_at", None)
            updated_at = getattr(model, "last_modified", None)

            model_info = ModelInfo(
                model_id=model.id,
                author=getattr(model, "author", None),
                downloads=getattr(model, "downloads", None),
                likes=getattr(model, "likes", None),
                created_at=created_at.isoformat() if created_at else None,
                updated_at=updated_at.isoformat() if updated_at else None,
                task=getattr(model, "pipeline_tag", None),
                library=getattr(model, "library_name", None),
                tags=getattr(model, "tags", None),
            )
            models_list.append(model_info)

        # Prepare query info for response
        query_info = QueryInfo(
            query=request.query,
            task=request.task,
            library=request.library,
            author=request.author,
            tags=request.tags,
            limit=limit,
            sort=sort_param,
            direction=request.direction,  # Keep the original user-friendly format
        )

        return SearchResultsData(
            models=models_list,
            total_results=len(models_list),
            query_info=query_info,
        )

    async def on_app_initialization_complete(self, _payload: AppInitializationComplete) -> None:
        """Handle app initialization complete event by downloading configured models and resuming unfinished downloads.

        Args:
            payload: The app initialization complete payload
        """
        # Get models to download from configuration
        config_manager = GriptapeNodes.ConfigManager()
        models_to_download = config_manager.get_config_value(MODELS_TO_DOWNLOAD_KEY, default=[])

        # Find unfinished downloads to resume
        unfinished_models = await asyncio.to_thread(self._find_unfinished_downloads)

        # Combine new downloads and unfinished ones, avoiding duplicates
        all_models = list(
            dict.fromkeys(
                [
                    *[model_id for model_id in models_to_download if model_id],  # Filter empty strings
                    *unfinished_models,
                ]
            )
        )

        if not all_models:
            logger.debug("No models to download or resume")
            return

        logger.info(
            "Starting download/resume of %d models (%d new, %d resuming)",
            len(all_models),
            len(models_to_download),
            len(unfinished_models),
        )

        # Create download tasks for concurrent execution
        download_tasks = []
        for model_id in all_models:
            task = asyncio.create_task(
                self.on_handle_download_model_request(
                    DownloadModelRequest(
                        model_id=model_id,
                        local_dir=None,
                        revision="main",
                        allow_patterns=None,
                        ignore_patterns=None,
                    )
                )
            )
            download_tasks.append(task)

        # Wait for all downloads to complete
        results = await asyncio.gather(*download_tasks, return_exceptions=True)

        # Log summary of results
        successful = sum(1 for result in results if not isinstance(result, DownloadModelResultFailure))
        failed = len(results) - successful

        logger.info("Completed automatic model downloads: %d successful, %d failed", successful, failed)

    def _get_status_file_path(self, model_id: str) -> Path:
        """Get the path to the status file for a model.

        Args:
            model_id: The model ID to get status file path for

        Returns:
            Path: Path to the status file for this model
        """
        status_dir = self._get_status_directory()

        sanitized_model_id = re.sub(r"[^\w\-_]", "--", model_id)
        return status_dir / f"{sanitized_model_id}.json"

    def _read_model_download_status(self, model_id: str) -> ModelDownloadStatus | None:
        """Read download status for a specific model.

        Args:
            model_id: The model ID to get status for

        Returns:
            ModelDownloadStatus | None: The status if found, None otherwise
        """
        status_file = self._get_status_file_path(model_id)

        if not status_file.exists():
            return None

        try:
            with status_file.open(encoding="utf-8") as f:
                data = json.load(f)

            # Get byte counts from status file
            total_bytes = data.get("total_bytes", 0)
            downloaded_bytes = data.get("downloaded_bytes", 0)

            # For simplified tracking, failed_bytes is calculated
            failed_bytes = 0
            if data.get("status") == "failed":
                failed_bytes = total_bytes - downloaded_bytes

            return ModelDownloadStatus(
                model_id=data["model_id"],
                status=data["status"],
                started_at=data["started_at"],
                updated_at=data["updated_at"],
                total_bytes=total_bytes,
                completed_bytes=downloaded_bytes,
                failed_bytes=failed_bytes,
                completed_at=data.get("completed_at"),
                local_path=data.get("local_path"),
                failed_at=data.get("failed_at"),
                error_message=data.get("error_message"),
            )

        except (json.JSONDecodeError, KeyError) as e:
            logger.warning("Failed to read status file for model '%s': %s", model_id, e)
            return None

    def _list_all_download_statuses(self) -> list[ModelDownloadStatus]:
        """List all model download statuses from status files.

        Returns:
            list[ModelDownloadStatus]: List of all download statuses
        """
        status_dir = self._get_status_directory()

        if not status_dir.exists():
            return []

        statuses = []
        for status_file in status_dir.glob("*.json"):
            try:
                with status_file.open(encoding="utf-8") as f:
                    data = json.load(f)

                model_id = data.get("model_id", "")
                if model_id:
                    status = self._read_model_download_status(model_id)
                    if status:
                        statuses.append(status)

            except (json.JSONDecodeError, KeyError) as e:
                logger.warning("Failed to read status file '%s': %s", status_file, e)
                continue

        return statuses

    def _find_unfinished_downloads(self) -> list[str]:
        """Find model IDs with unfinished downloads from status files.

        Returns:
            list[str]: List of model IDs with status 'downloading' or 'failed'
        """
        status_dir = self._get_status_directory()

        if not status_dir.exists():
            return []

        unfinished_models = []
        for status_file in status_dir.glob("*.json"):
            try:
                with status_file.open(encoding="utf-8") as f:
                    data = json.load(f)

                status = data.get("status", "")
                model_id = data.get("model_id", "")

                if model_id and status in ("downloading", "failed"):
                    unfinished_models.append(model_id)

            except (json.JSONDecodeError, KeyError) as e:
                logger.warning("Failed to read status file '%s': %s", status_file, e)
                continue

        return unfinished_models

    async def on_handle_list_model_downloads_request(self, request: ListModelDownloadsRequest) -> ResultPayload:
        """Handle model download status requests asynchronously.

        This method retrieves download status for a specific model or all models
        from the local status files stored in the XDG data directory.

        Args:
            request: The status request containing optional model_id

        Returns:
            ResultPayload: Success result with download status list or failure with error details
        """
        try:
            # Get status information in a thread to avoid blocking the event loop
            downloads = await asyncio.to_thread(self._get_download_statuses, request.model_id)

            if request.model_id and not downloads:
                result_details = f"No download status found for model '{request.model_id}'"
            elif request.model_id:
                result_details = f"Found download status for model '{request.model_id}'"
            else:
                result_details = f"Found {len(downloads)} download status records"

            return ListModelDownloadsResultSuccess(
                downloads=downloads,
                result_details=result_details,
            )

        except Exception as e:
            error_msg = f"Failed to get download status: {e}"
            return ListModelDownloadsResultFailure(
                result_details=error_msg,
                exception=e,
            )

    def _list_models(self) -> list[ModelInfo]:
        """Synchronous model listing implementation using HuggingFace Hub SDK.

        Uses scan_cache_dir to get information about cached models.

        Returns:
            list[ModelInfo]: List of model information
        """
        try:
            cache_info = scan_cache_dir()
            models = []

            for repo in cache_info.repos:
                # Calculate total size across all revisions
                total_size = sum(revision.size_on_disk for revision in repo.revisions)

                model_info = ModelInfo(
                    model_id=repo.repo_id,
                    local_path=str(repo.repo_path),
                    size_bytes=total_size,
                )
                models.append(model_info)

        except Exception as e:
            logger.warning("Failed to scan cache directory: %s", e)
            return []
        else:
            return models

    def _delete_model(self, model_id: str) -> str:
        """Synchronous model deletion implementation using HuggingFace Hub SDK.

        Uses scan_cache_dir to find and delete the model from cache.

        Args:
            model_id: The model ID to delete

        Returns:
            str: Information about what was deleted

        Raises:
            FileNotFoundError: If the model is not found in cache
        """
        cache_info = scan_cache_dir()

        # Find the repo to delete
        repo_to_delete = None
        for repo in cache_info.repos:
            if repo.repo_id == model_id:
                repo_to_delete = repo
                break

        if repo_to_delete is None:
            error_msg = f"Model '{model_id}' not found in cache"
            raise FileNotFoundError(error_msg)

        # Get all revision hashes for this repo
        revision_hashes = [revision.commit_hash for revision in repo_to_delete.revisions]

        if not revision_hashes:
            error_msg = f"No revisions found for model '{model_id}'"
            raise FileNotFoundError(error_msg)

        # Create delete strategy for all revisions of this repo
        delete_strategy = cache_info.delete_revisions(*revision_hashes)

        # Execute the deletion
        delete_strategy.execute()

        return f"Deleted model '{model_id}' (freed {delete_strategy.expected_freed_size_str})"

    def _get_model_info(self, model_dir: Path) -> dict[str, str | int | float] | None:
        """Get information about a cached model.

        Args:
            model_dir: Path to the model directory in cache

        Returns:
            dict | None: Model information or None if not a valid model directory
        """
        try:
            # Extract model_id from directory name
            # HuggingFace cache format: models--{org}--{model}--{hash}
            dir_name = model_dir.name
            if not dir_name.startswith("models--"):
                return None

            # Parse the model ID from the directory name
            parts = dir_name.split("--")
            if len(parts) >= MIN_CACHE_DIR_PARTS:
                # Reconstruct model_id as org/model
                model_id = f"{parts[1]}/{parts[2]}"
            else:
                model_id = dir_name[8:]  # Remove "models--" prefix

            # Calculate directory size
            total_size = sum(f.stat().st_size for f in model_dir.rglob("*") if f.is_file())

            return {
                "model_id": model_id,
                "local_path": str(model_dir),
                "size_bytes": total_size,
                "size_mb": round(total_size / (1024 * 1024), 2),
            }

        except Exception:
            # If we can't parse the directory, skip it
            return None

    def _parse_model_id(self, model_input: str) -> str:
        """Parse model ID from either a direct model ID or a Hugging Face URL.

        Args:
            model_input: Either a model ID (e.g., 'microsoft/DialoGPT-medium')
                        or a Hugging Face URL (e.g., 'https://huggingface.co/microsoft/DialoGPT-medium')

        Returns:
            str: The parsed model ID in the format 'namespace/repo_name' or 'repo_name'
        """
        # If it's already a simple model ID (no URL scheme), return as-is
        if not model_input.startswith(("http://", "https://")):
            return model_input

        # Parse the URL
        parsed = urlparse(model_input)

        # Check if it's a Hugging Face URL
        if parsed.netloc in ("huggingface.co", "www.huggingface.co"):
            # Extract the path and remove leading slash
            path = parsed.path.lstrip("/")

            # Remove any trailing parameters or fragments
            # The model ID should be in the format: namespace/repo_name or just repo_name
            model_id_match = re.match(r"^([^/]+/[^/?#]+|[^/?#]+)", path)
            if model_id_match:
                return model_id_match.group(1)

        # If we can't parse it, return the original input and let huggingface_hub handle the error
        return model_input

    def _get_download_statuses(self, model_id: str | None = None) -> list[ModelDownloadStatus]:
        """Get download statuses for a specific model or all models.

        Args:
            model_id: Optional model ID to get status for. If None, returns all statuses.

        Returns:
            list[ModelDownloadStatus]: List of download statuses
        """
        if model_id:
            # Get status for specific model
            status = self._read_model_download_status(model_id)
            return [status] if status else []
        # Get all download statuses
        return self._list_all_download_statuses()

    async def on_handle_delete_model_download_request(self, request: DeleteModelDownloadRequest) -> ResultPayload:
        """Handle model download status deletion requests asynchronously.

        This method removes download tracking records for a specific model
        from the local status files stored in the XDG data directory.
        If the model is currently downloading or failed, it also cancels
        the download task and deletes any cached model files.

        Args:
            request: The delete request containing model_id

        Returns:
            ResultPayload: Success result with deletion confirmation or failure with error details
        """
        model_id = request.model_id

        try:
            # Check current download status first
            download_status = await asyncio.to_thread(self._read_model_download_status, model_id)

            # Cancel active download process if it exists
            if model_id in self._download_processes:
                process = self._download_processes[model_id]
                await cancel_subprocess(process, f"download process for model '{model_id}'")
                del self._download_processes[model_id]

            # Cancel active download task if it exists
            if model_id in self._download_tasks:
                task = self._download_tasks[model_id]
                if not task.done():
                    task.cancel()
                    logger.debug("Cancelled active download task for model '%s'", model_id)
                del self._download_tasks[model_id]

            # Delete status file
            deleted_path = await asyncio.to_thread(self._delete_model_download_status, model_id)

            # Only delete cached model if it's not completed
            if download_status and download_status.status != "completed":
                try:
                    await asyncio.to_thread(self._delete_model, model_id)
                except FileNotFoundError:
                    logger.debug("No cached model files found for '%s'", model_id)

            result_details = f"Successfully deleted download status for model '{model_id}'"

            return DeleteModelDownloadResultSuccess(
                model_id=model_id,
                deleted_path=deleted_path,
                result_details=result_details,
            )

        except FileNotFoundError:
            error_msg = f"Download status for model '{model_id}' not found"
            return DeleteModelDownloadResultFailure(
                result_details=error_msg,
                exception=FileNotFoundError(error_msg),
            )

        except Exception as e:
            error_msg = f"Failed to delete download status for '{model_id}': {e}"
            return DeleteModelDownloadResultFailure(
                result_details=error_msg,
                exception=e,
            )

    def _delete_model_download_status(self, model_id: str) -> str:
        """Delete download status file for a specific model.

        Args:
            model_id: The model ID to remove download status for

        Returns:
            str: Path to the deleted status file

        Raises:
            FileNotFoundError: If the status file is not found
        """
        status_file = self._get_status_file_path(model_id)

        if not status_file.exists():
            msg = f"Download status file not found for model '{model_id}'"
            raise FileNotFoundError(msg)

        # TODO: Replace with DeleteFileRequest https://github.com/griptape-ai/griptape-nodes/issues/3765
        status_file.unlink()
        return str(status_file)
