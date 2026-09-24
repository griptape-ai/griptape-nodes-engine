from __future__ import annotations

import hashlib
import logging
import threading
import uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING

from watchfiles import Change, PythonFilter, watch

from griptape_nodes.drivers.cloud_credentials import MISSING_CREDENTIAL_MESSAGE, resolve_cloud_credential
from griptape_nodes.drivers.storage.griptape_cloud_storage_driver import GriptapeCloudStorageDriver
from griptape_nodes.files.path_utils import canonicalize_for_identity
from griptape_nodes.retained_mode.engine import EngineScoped
from griptape_nodes.retained_mode.events.app_events import AppInitializationComplete
from griptape_nodes.retained_mode.events.base_events import AppEvent, ResultDetails
from griptape_nodes.retained_mode.events.sync_events import (
    StartSyncAllCloudWorkflowsRequest,
    StartSyncAllCloudWorkflowsResultFailure,
    StartSyncAllCloudWorkflowsResultSuccess,
    SyncComplete,
)
from griptape_nodes.retained_mode.events.workflow_events import (
    RegisterWorkflowsFromConfigRequest,
    RegisterWorkflowsFromConfigResultSuccess,
)
from griptape_nodes.retained_mode.managers.settings import WORKFLOWS_TO_REGISTER_KEY

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.events.base_events import ResultPayload
    from griptape_nodes.retained_mode.managers.config_manager import ConfigManager
    from griptape_nodes.retained_mode.managers.event_manager import EventManager


logger = logging.getLogger("griptape_nodes")


class SyncManager(EngineScoped):
    """Manager for syncing workflows with cloud storage."""

    def __init__(
        self, event_manager: EventManager, config_manager: ConfigManager, *, engine: Engine | None = None
    ) -> None:
        super().__init__(engine)
        self._active_sync_tasks: dict[str, threading.Thread] = {}
        self._watch_task: threading.Thread | None = None
        self._watching_stopped = threading.Event()
        self._config_manager = config_manager
        self._event_manager = event_manager

        # Hash tracking to prevent sync loops
        self._file_hashes = defaultdict(str)
        self._hash_lock = threading.Lock()

        # Initialize sync directory
        self._sync_dir = self._config_manager.workspace_path / self._config_manager.get_config_value(
            "synced_workflows_directory"
        )
        self._sync_dir.mkdir(parents=True, exist_ok=True)

        event_manager.assign_manager_to_request_type(
            StartSyncAllCloudWorkflowsRequest,
            self.on_start_sync_all_cloud_workflows_request,
        )

        event_manager.add_listener_to_app_event(
            AppInitializationComplete,
            self.on_app_initialization_complete,
        )

    def _set_expected_hash(self, path: str | Path, content: bytes) -> None:
        """Set the expected SHA256 hash for a file we're about to write.

        This should be called before writing a file to establish what content
        we expect to see, allowing us to distinguish our own writes from
        external modifications.

        Args:
            path: Path to the file that will be written
            content: The exact bytes that will be written to the file
        """
        path_str = str(canonicalize_for_identity(path))
        file_hash = hashlib.sha256(content).hexdigest()
        with self._hash_lock:
            self._file_hashes[path_str] = file_hash
            logger.debug("Set expected hash for %s: %s", path_str, file_hash[:8])

    def _is_expected_content(self, path: str) -> bool:
        """Check if file content matches what we wrote (indicating a self-triggered event).

        This prevents sync loops by identifying when a file system event was caused
        by our own write operation rather than an external modification.

        Args:
            path: Path to the file to check

        Returns:
            True if the file content matches our expected hash (self-triggered event),
            False if it doesn't match or no expected hash exists (external change)
        """
        path_str = str(canonicalize_for_identity(path))
        with self._hash_lock:
            expected_hash = self._file_hashes.get(path_str)

        if not expected_hash:
            # No expected hash means this wasn't a self-triggered event
            return False

        try:
            actual_hash = hashlib.sha256(Path(path).read_bytes()).hexdigest()

            if actual_hash == expected_hash:
                # Content matches - this was our own write, clean up and ignore
                with self._hash_lock:
                    self._file_hashes.pop(path_str, None)
                logger.debug("File content matches expected hash for %s", path_str)
                return True
            # Content doesn't match - this is an external modification
            logger.debug("File content does not match expected hash for %s", path_str)
        except Exception as e:
            logger.debug("Error checking file hash for %s: %s", path_str, str(e))

        return False

    def on_start_sync_all_cloud_workflows_request(self, _request: StartSyncAllCloudWorkflowsRequest) -> ResultPayload:
        """Start syncing all cloud workflows to local synced_workflows directory."""
        try:
            storage_driver = self._get_cloud_storage_driver()
            sync_dir = self._sync_dir

            # List all assets in the bucket to get count
            files = storage_driver.list_files()
            workflow_files = [file for file in files if file.endswith(".py")]

            if not workflow_files:
                return StartSyncAllCloudWorkflowsResultSuccess(
                    sync_directory=str(sync_dir),
                    total_workflows=0,
                    result_details=ResultDetails(
                        message="No workflow files found in cloud storage.", level=logging.DEBUG
                    ),
                )

            # Start background sync with unique ID
            sync_task_id = str(uuid.uuid4())
            sync_thread = threading.Thread(
                target=self._sync_workflows_background,
                args=(sync_task_id, workflow_files, storage_driver, sync_dir),
                name=f"SyncWorkflows-{sync_task_id}",
                daemon=True,
            )

            self._active_sync_tasks[sync_task_id] = sync_thread
            sync_thread.start()
        except Exception as e:
            details = f"Failed to start cloud workflow sync: {e!s}"
            logger.error(details)
            return StartSyncAllCloudWorkflowsResultFailure(result_details=details)
        else:
            details = f"Started background sync for {len(workflow_files)} workflow files"
            return StartSyncAllCloudWorkflowsResultSuccess(
                sync_directory=str(sync_dir), total_workflows=len(workflow_files), result_details=details
            )

    def on_app_initialization_complete(self, _payload: AppInitializationComplete) -> None:
        """Automatically start syncing cloud workflows when the app initializes."""
        try:
            # Check if cloud storage is configured before attempting sync
            self._get_cloud_storage_driver()

            # Check if file watching is enabled before starting it
            enable_file_watching = self._config_manager.get_config_value(
                "enable_workspace_file_watching", default=True, cast_type=bool
            )
            if enable_file_watching:
                # Start file watching after successful sync
                self._start_file_watching()
                logger.debug("File watching enabled - started watching synced workflows directory")
            else:
                logger.debug("File watching disabled - skipping file watching startup")

            logger.debug("App initialization complete - starting automatic cloud workflow sync")

            # Create and handle the sync request
            sync_request = StartSyncAllCloudWorkflowsRequest()

            # Use handle_request to process through normal event system
            result = self.engine.handle_request(sync_request)

            if isinstance(result, StartSyncAllCloudWorkflowsResultSuccess):
                logger.debug(
                    "Automatic cloud workflow sync started successfully - %d workflows will be synced to %s",
                    result.total_workflows,
                    result.sync_directory,
                )

            else:
                logger.debug("Automatic cloud workflow sync failed to start (likely cloud not configured)")

        except Exception as e:
            logger.debug("Automatic cloud workflow sync skipped: %s", str(e))

    def _get_cloud_storage_driver(self) -> GriptapeCloudStorageDriver:
        """Get configured cloud storage driver.

        Returns:
            Configured GriptapeCloudStorageDriver instance.

        Raises:
            RuntimeError: If required cloud configuration is missing.
        """
        secrets_manager = self.engine.secrets_manager

        # Get cloud storage configuration from secrets
        bucket_id = secrets_manager.get_secret("GT_CLOUD_BUCKET_ID", should_error_on_not_found=False)
        base_url = secrets_manager.get_secret("GT_CLOUD_BASE_URL", should_error_on_not_found=False)
        api_key = resolve_cloud_credential(secrets_manager)

        if not bucket_id:
            msg = "Cloud storage bucket_id not configured. Set GT_CLOUD_BUCKET_ID secret."
            raise RuntimeError(msg)
        if not api_key:
            msg = f"Attempted to reach cloud storage. Failed because {MISSING_CREDENTIAL_MESSAGE}"
            raise RuntimeError(msg)

        return GriptapeCloudStorageDriver(
            self._config_manager,
            bucket_id=bucket_id,
            base_url=base_url,
            api_key=api_key,
            static_files_directory=self._config_manager.get_config_value(
                "synced_workflows_directory", default="synced_workflows"
            ),
        )

    def _download_cloud_workflow_to_sync_dir(self, filename: str) -> bool:
        """Download a workflow file from cloud storage to the sync directory.

        Args:
            filename: Name of the workflow file to download from cloud

        Returns:
            True if download was successful, False otherwise
        """
        try:
            storage_driver = self._get_cloud_storage_driver()
            sync_dir = self._sync_dir

            # Download file content from cloud
            file_content = storage_driver.download_file(Path(filename))

            # Write to local sync directory
            local_file_path = sync_dir / filename

            # Check if file exists and has same content hash
            if local_file_path.exists():
                try:
                    existing_content = local_file_path.read_bytes()

                    existing_hash = hashlib.sha256(existing_content).hexdigest()
                    new_hash = hashlib.sha256(file_content).hexdigest()

                    if existing_hash == new_hash:
                        logger.debug("Skipping write - file already has same content hash: %s", filename)
                        return True
                except Exception as e:
                    logger.debug("Error checking existing file hash for %s: %s", filename, str(e))

            # Set expected hash before writing to prevent sync loops
            self._set_expected_hash(local_file_path, file_content)

            local_file_path.write_bytes(file_content)

            logger.info("Successfully downloaded cloud workflow to sync directory: %s", filename)
        except Exception as e:
            logger.error("Failed to download cloud workflow '%s': %s", filename, str(e))
            return False
        else:
            return True

    def _upload_workflow_file(self, file_path: Path) -> None:
        """Upload a single workflow file to cloud storage.

        Args:
            file_path: Path to the workflow file to upload.
        """
        try:
            # Check if valid workflow file
            if not file_path.name.endswith(".py"):
                logger.error("Invalid workflow file path: %s", file_path)
                return

            # Proceed with upload
            storage_driver = self._get_cloud_storage_driver()

            # Read file content
            file_content = file_path.read_bytes()

            # Upload to cloud storage using the upload_file method
            filename = file_path.name
            storage_driver.upload_file(Path(filename), file_content)

            logger.info("Successfully uploaded workflow file to cloud: %s", filename)

        except Exception as e:
            logger.error("Failed to upload workflow file '%s': %s", file_path.name, str(e))

    def _delete_workflow_file(self, file_path: Path) -> None:
        """Delete a workflow file from cloud storage.

        Args:
            file_path: Path to the workflow file that was deleted locally.
        """
        try:
            storage_driver = self._get_cloud_storage_driver()
            filename = file_path.name

            # Use the storage driver's delete method
            storage_driver.delete_file(Path(filename))
            logger.info("Successfully deleted workflow file from cloud: %s", filename)

        except Exception as e:
            logger.error("Failed to delete workflow file '%s' from cloud: %s", file_path.name, str(e))

    def _start_file_watching(self) -> None:
        """Start watching the synced_workflows directory for changes."""
        try:
            sync_dir = self._sync_dir

            # Stop any existing watching
            if self._watch_task and self._watch_task.is_alive():
                self._watching_stopped.set()
                self._watch_task.join(timeout=2.0)

            # Reset the stop event for new watching
            self._watching_stopped.clear()

            # Start new watching thread
            self._watch_task = threading.Thread(
                target=self._watch_files_thread,
                args=(str(sync_dir),),
                name="WatchFiles-SyncManager",
                daemon=True,
            )
            self._watch_task.start()

            logger.debug("Started watching synced workflows directory: %s", sync_dir)

        except Exception as e:
            logger.error("Failed to start file watching: %s", str(e))

    def _watch_files_thread(self, sync_dir: str) -> None:
        """Background thread that watches for file changes using watchfiles."""
        try:
            logger.debug("File watching thread started for directory: %s", sync_dir)

            # Watch for changes in the sync directory using PythonFilter
            for changes in watch(sync_dir, watch_filter=PythonFilter(), stop_event=self._watching_stopped):
                if self._watching_stopped.is_set():
                    break

                for change, path_str in changes:
                    path = Path(path_str)

                    # Check if this was a self-triggered event for add/modify
                    if change in (Change.added, Change.modified) and self._is_expected_content(path_str):
                        logger.debug("Ignoring self-triggered %s event for: %s", change.name, path_str)
                        continue

                    # Handle the file change
                    self._handle_file_change(change, path, path_str)

        except Exception as e:
            if not self._watching_stopped.is_set():
                logger.error("Error in file watching thread: %s", str(e))
        finally:
            logger.debug("File watching thread stopped")

    def _handle_file_change(self, change: Change, path: Path, path_str: str) -> None:
        """Handle a file system change event."""
        if change == Change.added:
            logger.info("Detected external creation of workflow file: %s", path_str)
            self._upload_workflow_file(path)
        elif change == Change.modified:
            logger.info("Detected external modification of workflow file: %s", path_str)
            self._upload_workflow_file(path)
        elif change == Change.deleted:
            logger.info("Detected deletion of workflow file: %s", path_str)
            self._delete_workflow_file(path)

    def _download_single_workflow(
        self, file_name: str, storage_driver: GriptapeCloudStorageDriver, sync_dir: Path
    ) -> tuple[str, bool, str | None]:
        """Download a single workflow file.

        Args:
            file_name: Name of the workflow file to download from cloud
            storage_driver: Griptape Cloud storage driver instance for downloading
            sync_dir: Local directory path where the workflow file will be saved

        Returns:
            (filename, success, error_message)
        """
        try:
            # Download file content
            file_content = storage_driver.download_file(Path(file_name))

            # Extract just the filename (remove any directory prefixes)
            local_filename = Path(file_name).name
            local_file_path = sync_dir / local_filename

            # Check if file exists and has same content hash
            should_write = True
            if local_file_path.exists():
                try:
                    existing_content = local_file_path.read_bytes()

                    existing_hash = hashlib.sha256(existing_content).hexdigest()
                    new_hash = hashlib.sha256(file_content).hexdigest()

                    if existing_hash == new_hash:
                        logger.debug("Skipping write - file already has same content hash: %s", local_filename)
                        should_write = False
                except Exception as e:
                    logger.debug("Error checking existing file hash for %s: %s", local_filename, str(e))

            if should_write:
                # Set expected hash before writing to prevent sync loops
                self._set_expected_hash(local_file_path, file_content)

                # Write to local file
                local_file_path.write_bytes(file_content)

        except Exception as e:
            error_msg = str(e)
            logger.warning("Failed to sync workflow '%s': %s", file_name, error_msg)
            return file_name, False, error_msg
        else:
            logger.debug("Successfully synced workflow: %s", local_filename)
            return local_filename, True, None

    def _sync_workflows_background(
        self, sync_id: str, workflow_files: list[str], storage_driver: GriptapeCloudStorageDriver, sync_dir: Path
    ) -> None:
        """Background thread function to sync workflows."""
        synced_workflows = []
        failed_downloads = []
        total_workflows = len(workflow_files)

        logger.info("Starting background sync of %d workflows (sync_id: %s)", total_workflows, sync_id)

        # Use thread pool for concurrent downloads
        with ThreadPoolExecutor() as executor:
            # Submit all download tasks
            future_to_filename = {
                executor.submit(self._download_single_workflow, filename, storage_driver, sync_dir): filename
                for filename in workflow_files
            }

            # Collect results as they complete
            for future in future_to_filename:
                filename, success, _error = future.result()
                if success:
                    synced_workflows.append(filename)
                else:
                    failed_downloads.append(filename)

        if failed_downloads:
            logger.warning("Failed to sync %d workflows: %s", len(failed_downloads), failed_downloads)

        logger.info(
            "Background sync completed: %d of %d workflows synced to %s (sync_id: %s)",
            len(synced_workflows),
            len(workflow_files),
            sync_dir,
            sync_id,
        )

        # Emit sync complete event
        sync_complete_event = SyncComplete(
            sync_directory=str(sync_dir),
            synced_workflows=synced_workflows,
            failed_workflows=failed_downloads,
            total_workflows=total_workflows,
        )

        self.engine.event_manager.put_event(AppEvent(payload=sync_complete_event))

        # Register workflows from the synced directory
        if synced_workflows:
            logger.info("Registering %d synced workflows from configuration", len(synced_workflows))
            try:
                register_request = RegisterWorkflowsFromConfigRequest(config_section=WORKFLOWS_TO_REGISTER_KEY)
                register_result = self.engine.handle_request(register_request)

                if isinstance(register_result, RegisterWorkflowsFromConfigResultSuccess):
                    logger.info(
                        "Successfully registered %d workflows after sync completion: %s",
                        len(register_result.succeeded_workflows),
                        register_result.succeeded_workflows,
                    )
                else:
                    logger.warning("Failed to register workflows after sync completion")
            except Exception as e:
                logger.error("Error registering workflows after sync: %s", str(e))

        # Clean up task tracking
        if sync_id in self._active_sync_tasks:
            del self._active_sync_tasks[sync_id]
