from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from griptape_nodes.retained_mode.engine import EngineScoped
from griptape_nodes.retained_mode.events.library_events import (
    CheckLibraryUpdateRequest,
    CheckLibraryUpdateResultSuccess,
    ListRegisteredLibrariesRequest,
    ListRegisteredLibrariesResultSuccess,
    LoadLibrariesRequest,
    LoadLibrariesResultSuccess,
    SyncLibrariesRequest,
    SyncLibrariesResultFailure,
    SyncLibrariesResultSuccess,
    UpdateLibraryRequest,
    UpdateLibraryResultFailure,
    UpdateLibraryResultSuccess,
)
from griptape_nodes.retained_mode.managers.library.git_operations import LibraryUpdateInfo, LibraryUpdateResult
from griptape_nodes.retained_mode.managers.settings import (
    LIBRARIES_TO_DOWNLOAD_KEY,
    LIBRARIES_TO_REGISTER_KEY,
)
from griptape_nodes.retained_mode.request_handlers import handles
from griptape_nodes.utils.git_utils import (
    is_git_url,
)
from griptape_nodes.utils.library_utils import (
    extract_library_path,
    normalize_library_downloads,
)

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.events.base_events import ResultPayload
    from griptape_nodes.retained_mode.managers.event_manager import EventManager

logger = logging.getLogger("griptape_nodes")


class LibrarySync(EngineScoped):
    def __init__(self, event_manager: EventManager, *, engine: Engine | None = None) -> None:
        super().__init__(engine)
        event_manager.register_request_handlers(self)

    @handles(SyncLibrariesRequest)
    async def sync_libraries_request(self, request: SyncLibrariesRequest) -> ResultPayload:  # noqa: C901, PLR0912, PLR0915
        """Sync all libraries to latest versions and ensure dependencies are installed."""
        # Phase 1: Download missing libraries from both config keys
        config_mgr = self.engine.config_manager

        # Collect git URLs from both config keys
        download_config = config_mgr.get_config_value(LIBRARIES_TO_DOWNLOAD_KEY, default=[])
        register_config = config_mgr.get_config_value(LIBRARIES_TO_REGISTER_KEY, default=[])
        # libraries_to_download entries carry the git URL (bare string or object form).
        git_urls_from_download = [download.git_url for download in normalize_library_downloads(download_config)]
        # Disabled entries are still synced; disabling only affects loading.
        git_urls_from_register = [path for entry in register_config if is_git_url(path := extract_library_path(entry))]

        # Combine and deduplicate
        all_git_urls = list(set(git_urls_from_download + git_urls_from_register))

        # Use shared download method
        update_summary = {}
        libraries_downloaded = 0

        if all_git_urls:
            logger.debug("Found %d git URLs, downloading missing libraries", len(all_git_urls))
            download_results = await self.engine.library_manager.provisioning.download_libraries_from_git_urls(
                all_git_urls
            )

            # Process results for summary
            for git_url, result in download_results.items():
                if result["success"]:
                    libraries_downloaded += 1
                    update_summary[result["library_name"]] = {
                        "status": "downloaded",
                        "git_url": git_url,
                    }
                elif result.get("error"):
                    logger.warning("Download failed for '%s': %s", git_url, result["error"])

        logger.debug("Downloaded %d new libraries", libraries_downloaded)

        # Phase 2: Load libraries to ensure newly downloaded ones are registered
        logger.debug("Loading libraries to register newly downloaded ones")
        load_request = LoadLibrariesRequest()
        load_result = await self.engine.ahandle_request(load_request)

        if not isinstance(load_result, LoadLibrariesResultSuccess):
            logger.warning("Failed to load libraries after download: %s", load_result.result_details)
            # Continue anyway - we can still update previously registered libraries

        # Phase 3: Check and update all registered libraries
        # Get all registered libraries
        list_result = await self.engine.ahandle_request(ListRegisteredLibrariesRequest(broadcast_result=False))
        if not isinstance(list_result, ListRegisteredLibrariesResultSuccess):
            details = "Failed to list registered libraries for sync"
            return SyncLibrariesResultFailure(result_details=details)

        libraries_to_check = list_result.libraries

        logger.debug("Checking %d registered libraries for updates", len(libraries_to_check))

        # Check all libraries for updates concurrently using task group
        async def check_library_for_update(library_name: str) -> tuple[str, ResultPayload]:
            """Check a single library for updates."""
            logger.debug("Checking library '%s' for updates", library_name)
            check_result = await self.engine.ahandle_request(
                CheckLibraryUpdateRequest(library_name=library_name, failure_log_level=logging.DEBUG)
            )
            return library_name, check_result

        # Gather all check results concurrently
        check_results: dict[str, ResultPayload] = {}
        async with asyncio.TaskGroup() as tg:
            tasks = [tg.create_task(check_library_for_update(lib)) for lib in libraries_to_check]

        # Collect results from completed tasks
        for task in tasks:
            library_name, result = task.result()
            check_results[library_name] = result

        # Process check results and determine which libraries need updates
        libraries_checked = len(libraries_to_check)
        libraries_updated = 0
        libraries_deferred = 0
        libraries_to_update: list[LibraryUpdateInfo] = []

        for library_name, check_result in check_results.items():
            if not isinstance(check_result, CheckLibraryUpdateResultSuccess):
                logger.warning(
                    "Failed to check for updates for library '%s', skipping: %s",
                    library_name,
                    str(check_result.result_details),
                )
                continue

            if not check_result.has_update:
                logger.debug("Library '%s' is up to date (version %s)", library_name, check_result.current_version)
                continue

            # An update exists but is withheld by the age gate: skip it this cycle rather than
            # attempting an update that update_library_request would refuse. It will apply on a
            # later sync once the target commit reaches the minimum age.
            if check_result.update_gated_by_age:
                libraries_deferred += 1
                logger.debug(
                    "Library '%s' has an update (%s -> %s) withheld by the age gate; skipping this sync.",
                    library_name,
                    check_result.current_version,
                    check_result.latest_version,
                )
                # Record the pending target version (not the current one) so consumers can surface
                # "held at X, pending Y". The `deferred_age_gate` status disambiguates this from an
                # applied update where old != new.
                update_summary[library_name] = {
                    "old_version": check_result.current_version or "unknown",
                    "new_version": check_result.latest_version or "unknown",
                    "status": "deferred_age_gate",
                }
                continue

            # Library has an update available
            old_version = check_result.current_version or "unknown"
            new_version = check_result.latest_version or "unknown"
            logger.debug("Library '%s' has update available: %s -> %s", library_name, old_version, new_version)
            libraries_to_update.append(
                LibraryUpdateInfo(library_name=library_name, old_version=old_version, new_version=new_version)
            )

        # Update libraries concurrently using task group
        async def update_library(library_name: str, old_version: str, new_version: str) -> LibraryUpdateResult:
            """Update a single library."""
            logger.debug("Updating library '%s' from %s to %s", library_name, old_version, new_version)
            update_result = await self.engine.ahandle_request(
                UpdateLibraryRequest(
                    library_name=library_name,
                    overwrite_existing=request.overwrite_existing,
                )
            )
            return LibraryUpdateResult(
                library_name=library_name,
                old_version=old_version,
                new_version=new_version,
                result=update_result,
            )

        # Gather all update results concurrently
        async with asyncio.TaskGroup() as tg:
            update_tasks = [
                tg.create_task(update_library(info.library_name, info.old_version, info.new_version))
                for info in libraries_to_update
            ]

        # Collect update results
        for task in update_tasks:
            result = task.result()
            library_name = result.library_name
            old_version = result.old_version
            new_version = result.new_version
            update_result = result.result

            if not isinstance(update_result, UpdateLibraryResultSuccess):
                # A commit that was old enough during the check pass can still be refused at update
                # time if a newer commit landed on the remote in between; that comes back as an
                # age-gate refusal, not a hard failure. Count it as a deferral (it applies on a later
                # sync once the target ages) so the summary and counts stay accurate.
                if isinstance(update_result, UpdateLibraryResultFailure) and update_result.age_gated:
                    libraries_deferred += 1
                    logger.debug(
                        "Library '%s' update (%s -> %s) was withheld by the age gate at update time; deferring.",
                        library_name,
                        old_version,
                        new_version,
                    )
                    update_summary[library_name] = {
                        "old_version": old_version,
                        "new_version": new_version,
                        "status": "deferred_age_gate",
                    }
                    continue
                logger.error("Failed to update library '%s': %s", library_name, update_result.result_details)
                update_summary[library_name] = {
                    "old_version": old_version,
                    "new_version": old_version,
                    "status": "failed",
                    "error": update_result.result_details,
                }
                continue

            libraries_updated += 1
            update_summary[library_name] = {
                "old_version": update_result.old_version,
                "new_version": update_result.new_version,
                "status": "updated",
            }
            logger.info(
                "Successfully updated library '%s' from %s to %s",
                library_name,
                update_result.old_version,
                update_result.new_version,
            )

        # Build result details
        details = f"Downloaded {libraries_downloaded} libraries. Checked {libraries_checked} libraries. {libraries_updated} updated."
        if libraries_deferred:
            details += f" {libraries_deferred} withheld by age gate."
        logger.info(details)
        return SyncLibrariesResultSuccess(
            libraries_downloaded=libraries_downloaded,
            libraries_checked=libraries_checked,
            libraries_updated=libraries_updated,
            libraries_deferred=libraries_deferred,
            update_summary=update_summary,
            result_details=details,
        )
