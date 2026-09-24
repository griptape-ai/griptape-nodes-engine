"""Manager for rez integration request handling."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING

from griptape_nodes.retained_mode.engine import Engine, EngineScoped
from griptape_nodes.retained_mode.events.app_events import RezStatusNotification
from griptape_nodes.retained_mode.events.base_events import AppEvent, ResultPayload
from griptape_nodes.retained_mode.events.rez_events import (
    CheckRezHealthRequest,
    CheckRezHealthResultSuccess,
    GetRezStatusRequest,
    GetRezStatusResultSuccess,
    RezHealthStatus,
    RezLibraryStatus,
)
from griptape_nodes.utils.rez_utils import (
    check_rez_health_detailed,
    get_rez_context_string,
    is_rez_enabled,
    rez_path_map,
    rez_unsearched_stores,
)

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.managers.event_manager import EventManager

logger = logging.getLogger(__name__)


class RezManager(EngineScoped):
    """Handles rez-related request events from the GUI."""

    _cached_health: RezHealthStatus | None

    def __init__(self, event_manager: EventManager | None = None, *, engine: Engine | None = None) -> None:
        super().__init__(engine=engine)
        self._cached_health = None
        if event_manager is not None:
            event_manager.assign_manager_to_request_type(GetRezStatusRequest, self.handle_get_rez_status)
            event_manager.assign_manager_to_request_type(CheckRezHealthRequest, self.handle_check_rez_health)

    def run_startup_health_check(self) -> None:
        """Run a synchronous health check at engine startup and cache the result.

        Called from ``_run_app_initialization`` so the cached health is populated
        before any GUI connects. The result is available immediately via
        ``GetRezStatusRequest`` — no click required to turn the dot green.
        """
        result = check_rez_health_detailed()
        self._cached_health = RezHealthStatus(
            healthy=result.healthy,
            check_duration_ms=result.check_duration_ms,
            package_count=result.package_count,
            timestamp=result.timestamp,
        )
        logger.info(
            "[Rez] startup health check: %s (%d packages in %.0fms)",
            "OK" if result.healthy else "FAILED",
            result.package_count,
            result.check_duration_ms,
        )
        self._warn_about_unsearched_stores()

    def _warn_about_unsearched_stores(self) -> None:
        """Warn when rez is not configured to search a GTN_REZ_* package store.

        Read-only: Griptape Nodes never changes rez configuration. A store rez does not
        search holds packages the engine can see but ``rez env`` cannot resolve.
        """
        for store in rez_unsearched_stores():
            logger.warning(
                "[Rez] Attempted to use the rez package store %s. Rez is not configured to search it, "
                "so libraries from it will fail to load. Add it to packages_path in your rez configuration.",
                store,
            )

    def _collect_library_statuses(self) -> list[RezLibraryStatus]:
        """Gather rez package status for all registered libraries."""
        return [
            RezLibraryStatus(
                library_name=library_info.library_name,
                library_path=library_info.library_path,
                has_rez_package=library_info.has_rez_package,
                rez_family=library_info.rez_family,
                rez_version=library_info.rez_version,
            )
            for library_info in self.engine.library_manager._library_file_path_to_info.values()
            if library_info.library_name and library_info.library_path
        ]

    def _build_notification(self) -> RezStatusNotification:
        """Build a RezStatusNotification from current state."""
        return RezStatusNotification(
            enabled=is_rez_enabled(),
            context_string=get_rez_context_string(),
            health=self._cached_health,
            library_statuses=self._collect_library_statuses(),
        )

    def _push_notification(self) -> None:
        """Push a RezStatusNotification to all connected clients."""
        notification = self._build_notification()
        self.engine.event_manager.put_event(AppEvent(payload=notification))

    def handle_get_rez_status(self, request: GetRezStatusRequest) -> ResultPayload:  # noqa: ARG002
        health = self._cached_health or RezHealthStatus()
        return GetRezStatusResultSuccess(
            enabled=is_rez_enabled(),
            context_string=get_rez_context_string(),
            studio_root=os.environ.get("GTN_REZ_ROOT", ""),
            path_map=rez_path_map(),
            health=health,
            library_statuses=self._collect_library_statuses(),
            result_details=f"Rez {'enabled' if is_rez_enabled() else 'disabled'}",
        )

    async def handle_check_rez_health(self, request: CheckRezHealthRequest) -> ResultPayload:  # noqa: ARG002
        result = await asyncio.to_thread(check_rez_health_detailed)
        self._cached_health = RezHealthStatus(
            healthy=result.healthy,
            check_duration_ms=result.check_duration_ms,
            package_count=result.package_count,
            timestamp=result.timestamp,
        )
        self._push_notification()
        return CheckRezHealthResultSuccess(
            healthy=result.healthy,
            check_duration_ms=result.check_duration_ms,
            package_count=result.package_count,
            timestamp=result.timestamp,
            result_details=f"Rez health: {'OK' if result.healthy else 'FAILED'} in {result.check_duration_ms:.0f}ms",
        )
