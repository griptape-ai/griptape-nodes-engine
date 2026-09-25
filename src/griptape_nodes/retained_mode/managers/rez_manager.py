"""Manager for rez integration request handling."""

from __future__ import annotations

import asyncio
import logging
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
    ENV_CONFIG_FILE,
    check_rez_health_detailed,
    choose_torch_backend,
    get_rez_context_string,
    rez_config_dropped_paths,
    rez_path_map,
    rez_setup,
    rez_unsearched_stores,
)

if TYPE_CHECKING:
    from pathlib import Path

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

    def log_startup_configuration(self) -> None:
        """Log whether rez is active and, when it was configured but is not, why.

        Called at engine startup whether or not rez is on, so a misconfigured studio sees
        the reason once, up front, instead of as missing-package errors later.
        """
        setup = rez_setup()
        if setup.enabled:
            logger.info("Rez integration: enabled (tools in %s)", setup.bin_path)
        else:
            logger.info("Rez integration: disabled")
        if setup.disabled_reason:
            logger.warning("[Rez] %s", setup.disabled_reason)
        for warning in setup.warnings:
            logger.warning("[Rez] %s", warning)

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
        self._warn_about_replaced_search_path()

    def _warn_about_unsearched_stores(self) -> None:
        """Warn when rez is not configured to search the local package store.

        Read-only: Griptape Nodes never changes rez configuration. A store rez does not
        search holds packages ``rez env`` cannot resolve.
        """
        for store in rez_unsearched_stores():
            logger.warning(
                "[Rez] Attempted to use the local rez package store %s. Rez is not configured to search it, "
                "so libraries built there will fail to load. Add it to packages_path in your rez configuration.",
                store,
            )

    def _warn_about_replaced_search_path(self) -> None:
        """Warn when Griptape Nodes' rezconfig replaces the studio's search path instead of extending it."""
        dropped = rez_config_dropped_paths()
        if not dropped:
            return
        logger.warning(
            "[Rez] Your Griptape Nodes rezconfig (%s) replaces your studio's package search path instead of "
            "adding to it, so these studio package folders are no longer searched: %s. "
            "Use packages_path = ModifyList(append=[...]) in that file.",
            ENV_CONFIG_FILE,
            ", ".join(str(path) for path in dropped),
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
        setup = rez_setup()
        return RezStatusNotification(
            enabled=setup.enabled,
            disabled_reason=setup.disabled_reason,
            local_packages_path=_path_or_none(setup.local_packages_path),
            torch_backend=_torch_backend_label(),
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
        setup = rez_setup()
        if setup.enabled:
            details = "Rez enabled"
        elif setup.disabled_reason:
            details = setup.disabled_reason
        else:
            details = "Rez disabled"
        return GetRezStatusResultSuccess(
            enabled=setup.enabled,
            disabled_reason=setup.disabled_reason,
            context_string=get_rez_context_string(),
            studio_root=str(setup.base.path) if setup.base is not None else "",
            path_map=rez_path_map(),
            local_packages_path=_path_or_none(setup.local_packages_path),
            torch_backend=_torch_backend_label(),
            health=health,
            library_statuses=self._collect_library_statuses(),
            result_details=details,
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


def _path_or_none(path: Path | None) -> str | None:
    if path is None:
        return None
    return str(path)


def _torch_backend_label() -> str | None:
    """The torch build this workstation uses, for the status: ``cu128``, or None when none applies."""
    return choose_torch_backend().backend
