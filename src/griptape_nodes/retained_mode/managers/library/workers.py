from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from griptape_nodes.node_library.library_declarations import (
    SuggestedWorkerMode,
    WorkerMode,
)
from griptape_nodes.retained_mode.engine import EngineScoped
from griptape_nodes.retained_mode.events.app_events import (
    AppSessionStartedEvent,
    LibraryLoadedNotification,
    ReportLibraryLoadedRequest,
    ReportLibraryLoadedResultFailure,
    ReportLibraryLoadedResultSuccess,
)
from griptape_nodes.retained_mode.events.worker_events import StartWorkerRequest
from griptape_nodes.retained_mode.managers.fitness_problems.libraries import (
    DependencyInstallationFailedProblem,
    IncompatibleRequirementsProblem,
)
from griptape_nodes.retained_mode.managers.settings import (
    LIBRARIES_TO_REGISTER_KEY,
    LibraryRegistration,
)
from griptape_nodes.retained_mode.request_handlers import handles
from griptape_nodes.utils.library_utils import extract_library_path

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from griptape_nodes.node_library.library_declarations import LibraryDeclaration
    from griptape_nodes.node_library.library_registry import LibraryMetadata
    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.events.base_events import ResultPayload
    from griptape_nodes.retained_mode.managers.event_manager import EventManager

logger = logging.getLogger("griptape_nodes")


def resolve_executes_in_worker(*, metadata: LibraryMetadata) -> bool:
    """Whether this library's nodes execute in a dedicated worker process.

    True for libraries that declare execution dependencies: their heavy packages live
    in .venv-exec, which is only on sys.path in a worker, so process() can only run
    there.
    """
    dependencies = metadata.dependencies
    return bool(dependencies and dependencies.pip_dependencies_exec)


class LibraryWorkers(EngineScoped):
    # How a worker reaches the orchestrator to report a load. Registered by whatever owns the
    # transport, because this manager has no way to send a request to another process. Stays None
    # on the orchestrator and on a single-process engine, where there is nobody to report to.
    _library_load_reporter: Callable[[ReportLibraryLoadedRequest], Awaitable[None]] | None = None

    def __init__(self, event_manager: EventManager, *, engine: Engine | None = None) -> None:
        super().__init__(engine)
        event_manager.register_request_handlers(self)

    def register_library_load_reporter(self, reporter: Callable[[ReportLibraryLoadedRequest], Awaitable[None]]) -> None:
        """Register how this worker reports a library load to the orchestrator.

        Registered by whatever owns the transport between the two processes, since this manager can
        build the report but has no way to send it. Absent on the orchestrator and on a
        single-process engine, where there is nobody to report to.
        """
        self._library_load_reporter = reporter

    async def report_library_loaded(self, request: ReportLibraryLoadedRequest) -> None:
        """Tell the orchestrator how a library loaded here, or say why it never heard."""
        if self._library_load_reporter is None:
            logger.error(
                "No load reporter is registered, so the orchestrator will not learn that library "
                "'%s' loaded here and will wait out its startup grace before giving up on it.",
                request.library_name,
            )
            return
        await self._library_load_reporter(request)

    @handles(ReportLibraryLoadedRequest)
    async def on_report_library_loaded_request(self, request: ReportLibraryLoadedRequest) -> ResultPayload:
        """Note that a library loaded in the worker that executes its nodes.

        The report does not displace the orchestrator's own fitness verdict, which came from
        loading this library's real node modules here; the worker's view would misreport in
        both directions. What the report does is unblock whoever is waiting to route
        execution, and carry the worker's side out to listeners on the notification.

        The notification raised at the end is this process's own. A peer's copy never reaches a
        listener, so the GUI hears about a worker's library from the orchestrator accepting the
        report rather than from a relayed event.
        """
        library_info = self.engine.library_manager.get_library_info_by_library_name(request.library_name)
        if library_info is None:
            details = f"Received a library load report for unknown library '{request.library_name}'."
            return ReportLibraryLoadedResultFailure(result_details=details)
        if request.problem_details:
            logger.warning(
                "Worker reported problems loading library '%s': %s",
                request.library_name,
                request.problem_details,
            )
        # Whoever is waiting to route execution here is waiting on WorkerManager, which owns
        # whether a process is available; this is only the news that it loaded.
        self.engine.library_manager._worker_manager.note_library_loaded(request.library_name)
        await self.engine.abroadcast_app_event(
            LibraryLoadedNotification(
                library_name=request.library_name,
                fitness=request.fitness,
                problem_details=request.problem_details,
            )
        )
        return ReportLibraryLoadedResultSuccess(
            result_details=(
                f"Noted the worker's load of library '{request.library_name}' as {request.fitness}; "
                f"the orchestrator keeps its own fitness verdict."
            )
        )

    def log_legacy_worker_mode_advisory(
        self,
        *,
        library_name: str,
        registered_path: str | None,
        declarations: Sequence[LibraryDeclaration],
        executes_in_worker: bool,
    ) -> None:
        """Note that a legacy worker-mode request no longer routes this library to a worker.

        INFO rather than a warning: the library loads and runs correctly, so there is
        nothing for the author to chase unless its dependencies genuinely conflict. Called
        wherever a record first reaches METADATA_LOADED, so it is one line per library per
        load rather than one per registration attempt.

        Silent on a worker, which discovers every configured library and would otherwise
        repeat the advice once per worker while being the process already executing those
        nodes. Silent too once the library declares execution dependencies, so taking the
        advice is what stops it rather than deleting a declaration the engine still accepts.

        An explicit `worker_mode_override: ORCHESTRATOR` outranked the manifest under legacy
        worker mode too, so that library already ran in-process and nothing about it changed.
        """
        if executes_in_worker or self.engine.library_manager.is_worker:
            return
        config_override = self._config_worker_mode_override(registered_path)
        if config_override is WorkerMode.ORCHESTRATOR:
            return
        declares_worker = any(
            isinstance(declaration, SuggestedWorkerMode) and declaration.mode is WorkerMode.WORKER
            for declaration in declarations
        )
        if not declares_worker and config_override is not WorkerMode.WORKER:
            return
        logger.info(
            "Library '%s' asks for a dedicated worker process (legacy worker mode). Where a library's nodes "
            "execute is now decided by its dependencies: declare pip_dependencies_exec to run them in a "
            "worker, which isolates those packages rather than the whole library.",
            library_name,
        )

    def _config_worker_mode_override(self, registered_path: str | None) -> WorkerMode | None:
        """This library's `worker_mode_override` from `libraries_to_register`, if it sets one.

        Keyed by the user's verbatim path, mirroring how `enabled` is read, because the
        engine's path resolution can diverge between sides (workspace-relative,
        `~`-expansion, symlink-following).
        """
        if not registered_path:
            return None
        entries = self.engine.config_manager.get_config_value(LIBRARIES_TO_REGISTER_KEY) or []
        target_path_lower = registered_path.lower()
        for entry in entries:
            entry_path = extract_library_path(entry)
            if not entry_path or entry_path.lower() != target_path_lower:
                continue
            if isinstance(entry, LibraryRegistration):
                return entry.worker_mode_override
            if isinstance(entry, dict):
                # Compared rather than constructed: this runs on the registration path, and an
                # unrecognized value must not be what stops a library from loading.
                override = entry.get("worker_mode_override")
                return next((mode for mode in WorkerMode if override == mode), None)
            return None
        return None

    def get_worker_for_library(self, library_name: str | None) -> tuple[str, str] | None:
        """Return (worker_engine_id, worker_request_topic) for the worker serving library_name, or None.

        Raises RuntimeError if the library requires a dedicated worker but none is registered yet.
        Returns None if no worker is registered and none is required.
        """
        if library_name:
            library_info = self.engine.library_manager.get_library_info_by_library_name(library_name)
            # Composed from both owners: this manager knows library-level reasons -- a declared
            # resource the machine lacks, an execution environment that would not build -- and
            # WorkerManager knows process-level ones. Library reasons come first because they apply
            # to an in-process library too, which never reaches the worker branch below.
            worker_reason = (
                self.engine.library_manager._worker_manager.worker_unavailable_reason(library_name)
                if library_info and library_info.executes_in_worker
                else None
            )
            unavailable = (library_info.execution_unavailable_reason if library_info else None) or worker_reason
            if unavailable:
                msg = (
                    f"Library '{library_name}' cannot run right now: "
                    f"{unavailable} Editing its nodes still works, and a saved workflow keeps them."
                )
                raise RuntimeError(msg)
            if library_info and library_info.executes_in_worker:
                wm = self.engine.library_manager._worker_manager
                if wm:
                    worker = wm.get_worker_for_key(library_name)
                    if worker:
                        return worker
                    # A reason set on the library was already reported above, so reaching here
                    # means there is none: the worker genuinely has not registered yet.
                    msg = (
                        f"Library '{library_name}' requires a dedicated worker process "
                        "that is not yet registered. The worker may still be starting up."
                    )
                    raise RuntimeError(msg)
                msg = (
                    f"Library '{library_name}' requires a dedicated worker process. "
                    "The Worker Manager is not available."
                )
                raise RuntimeError(msg)
        return None

    async def on_session_started(self, _event: AppSessionStartedEvent) -> None:
        """Spawn workers for all libraries that require one now that a session is active.

        Two cases:
        1. Fresh start: libraries finished loading before a session existed, so their
           worker spawns were blocked on _session_ready_event. WorkerManager.set_session_ready()
           unblocks them, but _start_workers() catches any that slipped through.
        2. Session restart: workers were terminated by AppEndSession and need to be
           re-spawned now that a new session is available.
        """
        if self.engine.library_manager.is_worker:
            return
        await self._start_workers()

    async def maybe_start_workers_for_existing_session(self) -> None:
        """Start workers if the orchestrator restarted into an already-active session.

        In a normal fresh start the GUI sends AppStartSessionRequest which triggers worker
        spawning via on_session_started. When the engine restarts mid-session the GUI does
        not send that request, so this method handles the case at the end of library
        initialization.
        """
        if self.engine.library_manager.is_worker or not self.engine.get_session_id():
            return
        worker_manager = self.engine.library_manager._worker_manager
        if worker_manager is not None:
            worker_manager.set_session_ready()
            await self._start_workers()

    async def _start_workers(self) -> None:
        """Issue StartWorkerRequest for every library whose nodes execute in a worker.

        Asks WorkerManager to spawn a subprocess. Used on session start (both initial and
        subsequent) so that worker creation is always tied to an active session.
        """
        for library_info in self.engine.library_manager._library_file_path_to_info.values():
            # `enabled` matters: executes_in_worker is set before the lifecycle is overwritten with
            # DISABLED, so without this a library the user turned off still got an idle worker
            # subprocess.
            if (
                library_info.executes_in_worker
                and library_info.enabled
                and library_info.library_name
                and not self.engine.library_manager.is_worker
            ):
                # A declared resource this machine does not have makes the whole spawn pointless:
                # get_worker_for_library refuses on that reason before it ever consults a worker,
                # so the process would resolve and download an entire execution environment --
                # torch, gigabytes -- to serve nothing.
                has_unmet_requirement = any(
                    isinstance(problem, (IncompatibleRequirementsProblem, DependencyInstallationFailedProblem))
                    for problem in library_info.problems
                )
                if has_unmet_requirement:
                    logger.debug(
                        "Not starting a worker for library '%s': %s",
                        library_info.library_name,
                        library_info.execution_unavailable_reason,
                    )
                    continue
                # A worker already serving this library makes the whole block below wrong, not
                # merely redundant: spawn_worker refuses the duplicate without raising, so the
                # reset event below would never be set again and every later run would wait out
                # the startup grace against a live, loaded worker. Reached whenever _start_workers
                # runs twice for one session -- a second GUI client joining is enough.
                if self.engine.worker_manager.get_worker_for_key(library_info.library_name) is not None:
                    logger.debug(
                        "Not restarting a worker for library '%s': one is already registered.",
                        library_info.library_name,
                    )
                    continue
                # The library's own nodes loaded locally already: the worker gates execution
                # availability only, so registration must not block on the spawn.
                #
                # A library whose execution environment failed to build is never asked for a
                # worker: the venv directory is left behind, so spawning anyway would front the
                # worker's import path with a partial site-packages -- the unpinned execution the
                # edit/exec split exists to prevent -- and the raw ModuleNotFoundError would bury
                # the recorded uv error. Decided here because this manager built it and knows.
                build_failure = self.engine.library_manager.environment.execution_env_failure_reason(
                    library_info.library_name
                )
                if build_failure is not None:
                    logger.debug(
                        "Not requesting a worker for library '%s': %s", library_info.library_name, build_failure
                    )
                    self.engine.library_manager._worker_manager.note_worker_unavailable(
                        library_info.library_name, build_failure
                    )
                    continue
                # WorkerManager owns the gate execution routing waits on, and clears its own
                # account of any previous attempt.
                self.engine.library_manager._worker_manager.expect_worker(library_info.library_name)
                # A fresh attempt, so an account of a previous one no longer applies. Not
                # conditioned on the result: StartWorkerRequest only SCHEDULES the spawn, so one
                # that dies records its own reason from _log_spawn_error.
                library_info.execution_unavailable_reason = None
                await self.engine.ahandle_request(StartWorkerRequest(library_name=library_info.library_name))
