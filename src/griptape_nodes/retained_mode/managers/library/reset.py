"""Resetting a library: deleting its Python environments so the next registration builds them fresh.

A library's environments are the part of an install that goes bad without anyone touching the
library itself: a half-finished install, a package changed by hand, an interpreter uv has since
removed. Resetting them is the in-product recovery for that, in place of deleting folders by hand.

A directory this process cannot delete now is recorded instead, and removed the next time the
engine starts, before any library is loaded. Two cases need that. This process imported packages
from the library's `.venv`, and Python cannot unload them, so rebuilding under it would leave old
modules in memory over new files. And on Windows a file that is loaded cannot be deleted at all.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import sys
import sysconfig
from pathlib import Path
from typing import TYPE_CHECKING

import anyio

from griptape_nodes.retained_mode.engine import EngineScoped
from griptape_nodes.retained_mode.events.base_events import ResultDetails
from griptape_nodes.retained_mode.events.library_events import (
    ResetLibraryRequest,
    ResetLibraryResultFailure,
    ResetLibraryResultSuccess,
)
from griptape_nodes.retained_mode.managers.library.common import LibraryLifecycleState
from griptape_nodes.retained_mode.managers.os_manager import OSManager
from griptape_nodes.retained_mode.request_handlers import handles
from griptape_nodes.utils.engine_dirs import engine_state_dir

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.events.base_events import ResultPayload
    from griptape_nodes.retained_mode.managers.event_manager import EventManager

logger = logging.getLogger("griptape_nodes")

PENDING_RESETS_FILENAME = "pending_library_resets.json"
# Only directories with these names are ever removed from the pending file, so a hand-edited or
# corrupted entry cannot point the startup removal at anything but a library environment.
LIBRARY_ENV_DIR_NAMES = frozenset({".venv", ".venv-exec"})


class LibraryReset(EngineScoped):
    def __init__(self, event_manager: EventManager, *, engine: Engine | None = None) -> None:
        super().__init__(engine)
        event_manager.register_request_handlers(self)

    @handles(ResetLibraryRequest)
    async def reset_library_request(self, request: ResetLibraryRequest) -> ResultPayload:  # noqa: C901, PLR0911 (each refusal returns its own result)
        library_name = request.library_name
        library_manager = self.engine.library_manager

        managed = library_manager.managed_environment
        if managed.provisioned_by_environment():
            return ResetLibraryResultFailure(
                result_details=managed.environment_provides_libraries_message(f"reset Library '{library_name}'")
            )

        library_info = library_manager.get_library_info_by_library_name(library_name)
        if library_info is None:
            details = (
                f"Attempted to reset Library '{library_name}'. Failed because no Library with that name was found."
            )
            return ResetLibraryResultFailure(result_details=details)

        # The sandbox reloads through its own request, not by file path.
        if library_info.is_sandbox:
            details = f"Attempted to reset Library '{library_name}'. Failed because the sandbox library has no environment of its own to reset."
            return ResetLibraryResultFailure(result_details=details)

        # Registering it again afterwards never consults libraries_to_register, so a disabled
        # library would come back enabled for the session.
        if library_info.lifecycle_state == LibraryLifecycleState.DISABLED:
            details = f"Attempted to reset Library '{library_name}'. Failed because the Library is disabled. Enable it in Library Management and try again."
            return ResetLibraryResultFailure(result_details=details)

        environment = library_manager.environment
        env_paths = [
            environment.get_library_venv_path(library_name, library_info.library_path, execution=False),
            environment.get_library_venv_path(library_name, library_info.library_path, execution=True),
        ]

        if self.reset_requires_restart(library_name, library_info.library_path):
            self.schedule_removal(env_paths)
            details = (
                f"Library '{library_name}' will be reset the next time the engine starts. This engine has "
                f"already loaded packages from its environment and cannot let go of them, so it keeps "
                f"running on them until then. Restart the engine to finish the reset."
            )
            return ResetLibraryResultSuccess(
                library_name=library_name,
                restart_required=True,
                result_details=ResultDetails(message=details, level=logging.WARNING),
            )

        # The worker has the execution environment on its import path.
        await library_manager.workers.stop_worker_for_library(library_name)

        removed_paths: list[str] = []
        locked_paths: list[Path] = []
        for env_path in env_paths:
            if not await anyio.Path(env_path).exists():
                continue
            removal_error = await self._remove_env_dir(env_path)
            if removal_error is not None:
                logger.warning(
                    "Attempted to remove the environment at %s for Library '%s'. Failed due to: %s. "
                    "It will be removed the next time the engine starts.",
                    env_path,
                    library_name,
                    removal_error,
                )
                locked_paths.append(env_path)
            else:
                removed_paths.append(str(env_path))
        if locked_paths:
            self.schedule_removal(locked_paths)

        reload_result = await library_manager.git_operations._reload_library_after_git_operation(
            library_name,
            library_info.library_path,
            failure_result_class=ResetLibraryResultFailure,
        )
        if isinstance(reload_result, ResetLibraryResultFailure):
            details = self._reload_failure_details(library_name, has_locked_paths=bool(locked_paths))
            return ResetLibraryResultFailure(result_details=details)

        await library_manager.workers.start_worker_for_library(library_name)

        if locked_paths:
            details = (
                f"Reset Library '{library_name}', except for files that are in use. Those are removed the "
                f"next time the engine starts. Restart the engine to finish the reset."
            )
            return ResetLibraryResultSuccess(
                library_name=library_name,
                restart_required=True,
                removed_paths=removed_paths,
                result_details=ResultDetails(message=details, level=logging.WARNING),
            )

        details = f"Successfully reset Library '{library_name}'. Its environments were rebuilt."
        return ResetLibraryResultSuccess(
            library_name=library_name,
            removed_paths=removed_paths,
            result_details=ResultDetails(message=details, level=logging.INFO),
        )

    async def apply_pending_resets(self) -> None:
        """Remove the environments recorded by earlier resets that could not finish in their process.

        Must run before any library is loaded, since a loaded library holds its `.venv` open. A
        directory that still cannot be removed stays recorded for the next start.
        """
        pending_path = self.pending_resets_path()
        if not await anyio.Path(pending_path).exists():
            return

        # The engine never deletes anything an environment provides. The record waits for an
        # engine-provisioned start rather than being dropped.
        if self.engine.library_manager.managed_environment.provisioned_by_environment():
            logger.info(
                "Library resets are waiting in %s; they finish the next time the engine provisions its own libraries.",
                pending_path,
            )
            return

        remaining: list[str] = []
        for env_path_str in self._read_pending(pending_path):
            env_path = Path(env_path_str)
            if env_path.name not in LIBRARY_ENV_DIR_NAMES:
                logger.warning("Ignoring '%s' in %s: it is not a library environment.", env_path, pending_path)
                continue
            if not await anyio.Path(env_path).exists():
                continue
            removal_error = await self._remove_env_dir(env_path)
            if removal_error is not None:
                logger.error(
                    "Attempted to remove the library environment at %s to finish a reset. Failed due to: %s. "
                    "It will be tried again the next time the engine starts.",
                    env_path,
                    removal_error,
                )
                remaining.append(env_path_str)
            else:
                logger.info("Removed the library environment at %s to finish a reset.", env_path)

        self._write_pending(pending_path, remaining)

    def schedule_removal(self, env_paths: list[Path]) -> None:
        """Record environment directories to remove the next time the engine starts."""
        pending_path = self.pending_resets_path()
        pending = self._read_pending(pending_path)
        for env_path in env_paths:
            env_path_str = str(env_path)
            if env_path_str not in pending:
                pending.append(env_path_str)
        self._write_pending(pending_path, pending)

    def reset_requires_restart(self, library_name: str, library_path: str) -> bool:
        """Whether resetting this library now would wait for the next engine start.

        Both the reset and `LoadLibraryMetadataFromFileResultSuccess.reset_requires_restart` use this
        check, so they agree.
        """
        edit_env_path = self.engine.library_manager.environment.get_library_venv_path(
            library_name, library_path, execution=False
        )
        return self._edit_env_is_imported_from(edit_env_path)

    def pending_resets_path(self) -> Path:
        return engine_state_dir() / PENDING_RESETS_FILENAME

    def _reload_failure_details(self, library_name: str, *, has_locked_paths: bool) -> str:
        """Why the library did not load again, and whether part of the reset is still waiting for a restart."""
        problems = self.engine.library_manager.catalog.get_collated_problems_for_library(library_name)
        reason = problems if problems is not None else "the engine log has the details"
        if has_locked_paths:
            return (
                f"Attempted to reset Library '{library_name}'. Failed because it did not load again after its "
                f"environments were rebuilt: {reason} Some of its files were in use and are removed the next "
                f"time the engine starts, so restart the engine to finish the reset."
            )
        return f"Attempted to reset Library '{library_name}'. Its environments were removed, but it failed to load again: {reason}"

    async def _remove_env_dir(self, env_path: Path) -> str | None:
        """Delete an environment directory, returning why it is still there, or None once it is gone.

        Checked by looking afterwards: off Windows, `remove_readonly` returns without raising, so
        rmtree carries on past a file it could not delete and finishes without an error.
        """
        try:
            await asyncio.to_thread(shutil.rmtree, env_path, onexc=OSManager.remove_readonly)
        except OSError as e:
            return str(e)
        if await anyio.Path(env_path).exists():
            return "some of its files could not be deleted"
        return None

    def _edit_env_is_imported_from(self, edit_env_path: Path) -> bool:
        """Whether this process put the library's `.venv` site-packages on its import path.

        The engine splices it there when it loads the library's nodes, and anything imported from it
        stays in memory for the life of the process.
        """
        site_packages = Path(
            sysconfig.get_path("purelib", vars={"base": str(edit_env_path), "platbase": str(edit_env_path)})
        ).resolve()
        return any(Path(entry).resolve() == site_packages for entry in sys.path if entry)

    @staticmethod
    def _read_pending(pending_path: Path) -> list[str]:
        if not pending_path.exists():
            return []
        try:
            contents = json.loads(pending_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            logger.error("Attempted to read pending library resets from %s. Failed due to: %s", pending_path, e)
            return []
        env_paths = contents.get("env_paths") if isinstance(contents, dict) else None
        if not isinstance(env_paths, list):
            return []
        return [env_path for env_path in env_paths if isinstance(env_path, str)]

    @staticmethod
    def _write_pending(pending_path: Path, env_paths: list[str]) -> None:
        if not env_paths:
            pending_path.unlink(missing_ok=True)
            return
        pending_path.parent.mkdir(parents=True, exist_ok=True)
        pending_path.write_text(json.dumps({"env_paths": env_paths}, indent=2), encoding="utf-8")
