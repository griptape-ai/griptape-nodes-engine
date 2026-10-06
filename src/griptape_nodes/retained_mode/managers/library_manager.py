from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from griptape_nodes.node_library.library_registry import (
    LibraryRegistry,
    LibrarySchema,
)
from griptape_nodes.retained_mode.engine import EngineScoped
from griptape_nodes.retained_mode.events.app_events import (
    AppInitializationComplete,
    AppSessionStartedEvent,
    EngineInitializationProgress,
    EngineReadyEvent,
    InitializationPhase,
    InitializationStatus,
    LibraryLoadStatus,
    ReportLibraryLoadedRequest,
)

# Runtime imports for ResultDetails since it's used at runtime
from griptape_nodes.retained_mode.events.base_events import AppEvent, ResultDetails
from griptape_nodes.retained_mode.events.library_events import (
    DiscoverLibrariesRequest,
    DiscoverLibrariesResultFailure,
    GetAllInfoForAllLibrariesRequest,
    ListRegisteredLibrariesRequest,
    ListRegisteredLibrariesResultSuccess,
    RegisterLibraryFromFileRequest,
    RegisterLibraryFromFileResultFailure,
    RegisterLibraryFromFileResultSuccess,
    ReloadAllLibrariesRequest,
    ReloadAllLibrariesResultFailure,
    ReloadAllLibrariesResultSuccess,
    UnloadLibraryFromRegistryRequest,
)
from griptape_nodes.retained_mode.events.object_events import ClearAllObjectStateRequest
from griptape_nodes.retained_mode.managers.library.catalog import LibraryCatalog
from griptape_nodes.retained_mode.managers.library.common import (
    LIBRARY_CONFIG_FILENAME,
    LIBRARY_CONFIG_GLOB_PATTERN,
    LibraryFitness,
    LibraryInfo,
    LibraryLifecycleState,
    RegisteredEventHandler,
)
from griptape_nodes.retained_mode.managers.library.dependencies import LibraryDependencies
from griptape_nodes.retained_mode.managers.library.discovery import (
    DiscoveredLibraryEntry,
    LibraryDiscovery,
    ResolvedDiscoveryPath,
)
from griptape_nodes.retained_mode.managers.library.environment import LibraryEnvironment
from griptape_nodes.retained_mode.managers.library.git_operations import LibraryGitOperations
from griptape_nodes.retained_mode.managers.library.metadata_loading import LibraryMetadataLoading
from griptape_nodes.retained_mode.managers.library.module_loading import STABLE_NAMESPACE_PREFIX, LibraryModuleLoading
from griptape_nodes.retained_mode.managers.library.provisioning import LibraryProvisioning
from griptape_nodes.retained_mode.managers.library.registration import LibraryRegistrar, RegisterLibraryPrerequisites
from griptape_nodes.retained_mode.managers.library.sandbox import (
    SANDBOX_CATEGORY_NAME,
    SANDBOX_LIBRARY_NAME,
    UNRESOLVED_SANDBOX_CLASS_NAME,
    LibrarySandbox,
)
from griptape_nodes.retained_mode.managers.library.sync import LibrarySync
from griptape_nodes.retained_mode.managers.library.workers import LibraryWorkers
from griptape_nodes.retained_mode.managers.settings import (
    LIBRARIES_TO_REGISTER_KEY,
)
from griptape_nodes.retained_mode.request_handlers import handles

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.events.base_events import Payload, RequestPayload, ResultPayload
    from griptape_nodes.retained_mode.managers.event_manager import EventManager
    from griptape_nodes.retained_mode.managers.worker_manager import WorkerManager

logger = logging.getLogger("griptape_nodes")


class LibraryManager(EngineScoped):
    SANDBOX_LIBRARY_NAME = SANDBOX_LIBRARY_NAME
    STABLE_NAMESPACE_PREFIX = STABLE_NAMESPACE_PREFIX
    LIBRARY_CONFIG_FILENAME = LIBRARY_CONFIG_FILENAME
    LIBRARY_CONFIG_GLOB_PATTERN = LIBRARY_CONFIG_GLOB_PATTERN

    # Sandbox library constants
    UNRESOLVED_SANDBOX_CLASS_NAME = UNRESOLVED_SANDBOX_CLASS_NAME
    SANDBOX_CATEGORY_NAME = SANDBOX_CATEGORY_NAME

    LibraryLifecycleState = LibraryLifecycleState
    LibraryFitness = LibraryFitness
    RegisteredEventHandler = RegisteredEventHandler
    LibraryInfo = LibraryInfo
    RegisterLibraryPrerequisites = RegisterLibraryPrerequisites
    DiscoveredLibraryEntry = DiscoveredLibraryEntry
    ResolvedDiscoveryPath = ResolvedDiscoveryPath

    _library_file_path_to_info: dict[str, LibraryInfo]

    # Libraries whose node modules were already imported when they were unloaded. Python caches
    # modules process-wide, so re-registering such a library cannot replace the code already in
    # memory: the helper packages its node files import stay at the version this process first
    # imported. Only an engine restart clears that, so these names are remembered for the life of
    # the process and used to explain import failures the artist would otherwise see bare.
    _libraries_reloaded_after_import: set[str]
    # Callbacks invoked immediately before all libraries are reloaded.
    _pre_reload_callbacks: list[Callable[[], Awaitable[None]]]

    def __init__(
        self, event_manager: EventManager, *, worker_manager: WorkerManager, engine: Engine | None = None
    ) -> None:
        super().__init__(engine)
        self._worker_manager = worker_manager
        self._library_file_path_to_info = {}
        self._libraries_reloaded_after_import = set()
        # Two separate handler registration systems exist in this manager:
        #
        # 1. EventManager.assign_manager_to_request_type() — singleton (one handler per
        #    type globally), populated via AdvancedNodeLibrary.get_request_handlers().
        #    For library-owned services where exactly one library is the provider.
        #
        # 2. _library_event_handler_mappings / on_register_event_handler() — multi-provider
        #    (many libraries can handle the same type, selected by name at dispatch).
        #    Used by publishing libraries registering PublishWorkflowRequest handlers.
        #    Libraries call on_register_event_handler() in after_library_nodes_loaded().
        #
        # The two systems coexist without conflict. TODO(GH#4785): https://github.com/griptape-ai/griptape-nodes-engine/issues/4785
        self._library_event_handler_mappings: dict[type[Payload], dict[str, RegisteredEventHandler[Any]]] = {}
        self._libraries_loading_complete = asyncio.Event()
        self._libraries_loading_complete.set()  # Not loading initially; load_all_libraries_from_config will clear/set this
        # True for the duration of the engine's initialization sequence (library + workflow
        # loading) driven by on_app_initialization_complete. Reported on the engine heartbeat so
        # a client connecting mid-startup can render a loading state instead of an empty list.
        self._is_initializing: bool = False
        self._pre_reload_callbacks: list[Callable[[], Awaitable[None]]] = []
        # True when this process is a dedicated worker
        self._is_worker: bool = False
        # The libraries this process is restricted to loading (set on workers).
        self._target_library_names: list[str] | None = None
        self.module_loading = LibraryModuleLoading(engine)
        self.workers = LibraryWorkers(event_manager, engine=engine)
        self.catalog = LibraryCatalog(event_manager, engine=engine)
        self.metadata_loading = LibraryMetadataLoading(event_manager, engine=engine)
        self.sandbox = LibrarySandbox(event_manager, engine=engine)
        self.registration = LibraryRegistrar(event_manager, engine=engine)
        self.environment = LibraryEnvironment(engine)
        self.dependencies = LibraryDependencies(event_manager, engine=engine)
        self.provisioning = LibraryProvisioning(event_manager, engine=engine)
        self.git_operations = LibraryGitOperations(event_manager, engine=engine)
        self.sync = LibrarySync(event_manager, engine=engine)
        self.discovery = LibraryDiscovery(event_manager, engine=engine)
        event_manager.register_request_handlers(self)

        event_manager.add_listener_to_app_event(
            AppInitializationComplete,
            self.on_app_initialization_complete,
        )
        event_manager.add_listener_to_app_event(
            AppSessionStartedEvent,
            self.workers.on_session_started,
        )

        worker_manager.register_worker_evicted_callback(self.workers.on_worker_evicted)
        self._pre_reload_callbacks.append(worker_manager.reset_workers)

    def is_initializing(self) -> bool:
        """Return True while the engine is running its initialization sequence.

        Covers the full on_app_initialization_complete / reload flow (library and workflow
        loading), not just the library phase. Reported on the engine heartbeat.
        """
        return self._is_initializing

    def register_pre_reload_callback(self, callback: Callable[[], Awaitable[None]]) -> None:
        """Register a callback invoked immediately before all libraries are reloaded.

        Callbacks fire after all libraries have been unloaded, before
        load_all_libraries_from_config runs. Use this to clean up state
        (e.g. terminate worker processes) that must be reset for the reload to
        succeed.
        """
        self._pre_reload_callbacks.append(callback)

    @property
    def is_worker(self) -> bool:
        """True when this process was started as a dedicated worker.

        Set by ``LibrariesInitializeStartRequest`` once the worker bootstrap
        has identified the process role. Callers outside this manager that
        need the role (e.g. node-execution strict-mode attribution, request
        forwarding decisions) should consult this accessor rather than
        reaching into ``_is_worker``.
        """
        return self._is_worker

    def get_libraries_attempted_to_load(self) -> list[str]:
        return list(self._library_file_path_to_info.keys())

    def get_library_info_for_attempted_load(self, library_file_path: str) -> LibraryInfo:
        return self._library_file_path_to_info[library_file_path]

    def get_library_info_by_library_name(self, library_name: str) -> LibraryInfo | None:
        # A library name can have more than one entry: a duplicate install registers a second
        # copy that fails with DuplicateLibraryProblem but is deliberately kept in the dict
        # (marked FAILURE) so the GUI can surface it, and filename variations / download +
        # discovery collisions can key the same name under two paths. Prefer the copy that is
        # actually LOADED (the one in LibraryRegistry, whose version the update-check reads) so
        # every caller resolves the live copy rather than a dead duplicate. This keeps the
        # update path, the update-check path, and the on-disk copy consistent (issue #5039).
        # Fall back to first-match when nothing is loaded yet (e.g. discovery / worker-pending).
        matches = [info for info in self._library_file_path_to_info.values() if info.library_name == library_name]
        if not matches:
            return None
        for library_info in matches:
            if library_info.lifecycle_state == LibraryLifecycleState.LOADED:
                return library_info
        return matches[0]

    def on_register_event_handler(
        self,
        request_type: type[RequestPayload],
        handler: Callable[[RequestPayload], ResultPayload],
        library_data: LibrarySchema,
        event_data: object | None = None,
    ) -> None:
        """Register an event handler for a specific request type from a library.

        Args:
            request_type: The type of request payload this handler processes
            handler: The callable handler function
            library_data: Schema data for the library registering this handler
            event_data: Optional structured data specific to this event type
        """
        if self._library_event_handler_mappings.get(request_type) is None:
            self._library_event_handler_mappings[request_type] = {}
        self._library_event_handler_mappings[request_type][library_data.name] = RegisteredEventHandler(
            handler=handler, library_data=library_data, event_data=event_data
        )

    def get_registered_event_handlers(self, request_type: type[Payload]) -> dict[str, RegisteredEventHandler[Any]]:
        """Get all registered event handlers for a specific request type."""
        return self._library_event_handler_mappings.get(request_type, {})

    async def load_all_libraries_from_config(self, target_library_names: list[str] | None = None) -> list[str]:
        """Reconcile sourced libraries, then discover and load every enabled library.

        Reconcile runs first (engine_version gate + provision of git-sourced
        entries) so freshly provisioned libraries are present on disk for the
        discovery pass below. Reconcile failure details are returned, not raised:
        the boot caller logs and continues so a bad pin cannot brick startup,
        while the interactive reload caller turns non-empty details into a
        failure result. Library discovery/loading always proceeds regardless so
        the engine comes up with whatever libraries it can.

        Returns the reconcile failure details (empty list on success).
        """
        # Close the gate for the duration of the rebuild. A reload has already closed it
        # before unloading, in which case this is a no-op and its waiters are preserved.
        # The finally below reopens it on every exit path, so "closed" always means a load
        # is in flight on the current loop.
        self._close_libraries_loading_gate()
        try:
            reconcile_failures = await self.provisioning.reconcile_libraries_from_config()

            # Discover all available libraries (config + sandbox)
            discover_result = await self.discovery.discover_libraries_request(DiscoverLibrariesRequest())
            if isinstance(discover_result, DiscoverLibrariesResultFailure):
                logger.error("Failed to discover libraries: %s", discover_result.result_details)
                return reconcile_failures

            # A worker is told which library it serves, but that library's declared library
            # dependencies are part of what it needs to run: CorridorKey's OCIO path reaches into
            # the OpenEXR library, which loaded on the orchestrator and was absent from the worker,
            # so the feature failed only under worker execution. Runs directly after discovery,
            # which is what populates the info map it reads -- resolve_transitive_library_deps
            # cannot serve here because it reads LibraryRegistry and nothing is loaded yet.
            if target_library_names is not None:
                target_library_names = self.dependencies.expand_targets_with_library_dependencies(target_library_names)

            # Build list of library paths to load
            libraries_to_load = []
            for discovered_lib in discover_result.libraries_discovered:
                lib_path = str(discovered_lib.path)
                lib_info = self._library_file_path_to_info.get(lib_path)

                if lib_info and lib_info.lifecycle_state != LibraryLifecycleState.DISABLED:
                    libraries_to_load.append(lib_path)

            if not libraries_to_load:
                logger.info("No libraries found in configuration.")
                return reconcile_failures

            # Calculate total libraries for progress tracking
            total_libraries = len(libraries_to_load)

            for current_library_index, lib_path in enumerate(libraries_to_load, start=1):
                # When running as a dedicated library worker, skip libraries that don't match the target.
                # library_name is already populated in _library_file_path_to_info from the discovery phase.
                lib_info = self._library_file_path_to_info.get(lib_path)
                if target_library_names is not None and (
                    lib_info is None or lib_info.library_name not in target_library_names
                ):
                    continue

                await self._load_and_track_library(lib_path, current_library_index, total_libraries)

            # Remove any missing libraries AFTER we've loaded them for the user.
            user_libraries_section = LIBRARIES_TO_REGISTER_KEY
            self.discovery.remove_missing_libraries_from_config(config_category=user_libraries_section)

            return reconcile_failures
        finally:
            self._libraries_loading_complete.set()

    async def on_app_initialization_complete(self, payload: AppInitializationComplete) -> None:
        # Bracket the whole init sequence so the heartbeat can report is_initializing to clients
        # that connect mid-startup. finally guarantees the flag clears even if init raises.
        self._is_initializing = True
        try:
            await self._run_app_initialization(payload)
        finally:
            self._is_initializing = False

    @handles(ReloadAllLibrariesRequest)
    async def reload_libraries_request(self, request: ReloadAllLibrariesRequest) -> ResultPayload:
        # Bracket the reload like on_app_initialization_complete so the heartbeat reports
        # is_initializing during a mid-session reload too. finally clears it even on failure.
        self._is_initializing = True
        try:
            return await self._run_reload_libraries(request)
        finally:
            self._is_initializing = False

    def register_library_load_reporter(self, reporter: Callable[[ReportLibraryLoadedRequest], Awaitable[None]]) -> None:
        """Kept for the app that calls it. See `LibraryWorkers.register_library_load_reporter`."""
        self.workers.register_library_load_reporter(reporter)

    async def get_all_info_for_all_libraries_request(self, request: GetAllInfoForAllLibrariesRequest) -> ResultPayload:
        """Kept for node libraries that call it. See `LibraryCatalog.get_all_info_for_all_libraries_request`."""
        return await self.catalog.get_all_info_for_all_libraries_request(request)

    def _collect_library_load_statuses(self) -> list[LibraryLoadStatus]:
        """Gather the load outcome for every attempted library as serializable data.

        Presentation (icons, colors, table layout) is owned by the application
        layer; this returns raw data for it to render.
        """
        statuses: list[LibraryLoadStatus] = []
        for library_file_path in self.get_libraries_attempted_to_load():
            lib_info = self.get_library_info_for_attempted_load(library_file_path)
            statuses.append(
                LibraryLoadStatus(
                    library_name=lib_info.library_name,
                    library_version=str(lib_info.library_version) if lib_info.library_version else None,
                    library_path=lib_info.library_path,
                    fitness=lib_info.fitness.value,
                    disabled=lib_info.lifecycle_state == LibraryLifecycleState.DISABLED,
                    problems=self.catalog.collate_problems_for_lib_info(lib_info),
                )
            )
        return statuses

    async def _load_and_track_library(self, lib_path: str, index: int, total: int) -> None:
        """Load a single library and emit the corresponding progress event."""
        # Emit the LOADING event BEFORE registering: register_library_from_file_request installs
        # the library's dependencies (a slow pip/uv step), so emitting after it would leave the
        # GUI with no progress signal during the longest part of startup. The library name is
        # populated during discovery; fall back to a path-derived name if it isn't set yet.
        pre_register_info = self._library_file_path_to_info.get(lib_path)
        pending_library_name = (
            pre_register_info.library_name
            if pre_register_info and pre_register_info.library_name
            else Path(lib_path).stem
        )
        self.engine.event_manager.put_event(
            AppEvent(
                payload=EngineInitializationProgress(
                    phase=InitializationPhase.LIBRARIES,
                    item_name=pending_library_name,
                    status=InitializationStatus.LOADING,
                    current=index,
                    total=total,
                    is_worker=self._is_worker,
                )
            )
        )

        load_result = await self.registration.register_library_from_file_request(
            RegisterLibraryFromFileRequest(
                file_path=lib_path,
                load_as_default_library=False,
            )
        )

        if isinstance(load_result, RegisterLibraryFromFileResultFailure):
            logger.warning("Failed to load library at '%s': %s", lib_path, load_result.result_details)
            error_message = (
                load_result.result_details.result_details[0].message
                if isinstance(load_result.result_details, ResultDetails)
                else str(load_result.result_details)
            )
            self.engine.event_manager.put_event(
                AppEvent(
                    payload=EngineInitializationProgress(
                        phase=InitializationPhase.LIBRARIES,
                        item_name=lib_path,
                        status=InitializationStatus.FAILED,
                        current=index,
                        total=total,
                        error=error_message,
                        is_worker=self._is_worker,
                    )
                )
            )
        elif isinstance(load_result, RegisterLibraryFromFileResultSuccess):
            self.engine.event_manager.put_event(
                AppEvent(
                    payload=EngineInitializationProgress(
                        phase=InitializationPhase.LIBRARIES,
                        item_name=load_result.library_name,
                        status=InitializationStatus.COMPLETE,
                        current=index,
                        total=total,
                        is_worker=self._is_worker,
                    )
                )
            )

    def _close_libraries_loading_gate(self) -> None:
        """Close the gate that library queries wait on while the registry is being rebuilt.

        Recreates the Event rather than calling .clear(): an Event created by a previous
        asyncio.run() call raises RuntimeError when awaited from a new loop (asyncio.Event
        objects are bound to the loop they were created on).

        An already-closed gate is left alone. A reload closes the gate before it unloads
        libraries, so by the time load_all_libraries_from_config runs there may already be
        callers suspended on this Event; replacing it would orphan them, because the
        matching set() would fire on a different object.
        """
        if not self._libraries_loading_complete.is_set():
            return
        self._libraries_loading_complete = asyncio.Event()

    async def _run_app_initialization(self, payload: AppInitializationComplete) -> None:
        if payload.skip_library_loading:
            # Register all secrets even in headless mode
            self.engine.secrets_manager.register_all_secrets()

            # Still need to tell WorkflowManager to register workflows
            # Pass the specific workflows if provided, otherwise it will scan workspace
            await self.engine.workflow_manager.refresh_workflow_registry(
                workflows_to_register=payload.workflows_to_register
            )
            return

        # Automatically migrate old XDG library paths from config
        # TODO: Remove https://github.com/griptape-ai/griptape-nodes/issues/3348
        self.discovery.migrate_old_xdg_library_paths()

        # App just got init'd. First download any missing libraries from git URLs.
        await self.provisioning.ensure_libraries_from_config()

        # Now load all libraries from config (including newly downloaded ones).
        # When running as a dedicated library worker, restrict loading to those libraries.
        self._is_worker = payload.is_worker
        self._target_library_names = payload.libraries_to_register if payload.is_worker else None
        reconcile_failures = await self.load_all_libraries_from_config(target_library_names=self._target_library_names)
        # Soft boot: log reconcile failures and continue so the engine still starts and the
        # user can switch to a working project. Interactive activation hard-fails instead
        # (see reload_libraries_request).
        if reconcile_failures:
            logger.warning(
                "Library reconcile reported %d problem(s) at startup; continuing so the engine can start:\n%s",
                len(reconcile_failures),
                "\n".join(reconcile_failures),
            )

        # When the orchestrator restarts into an already-active session, the GUI will not
        # send AppStartSessionRequest again, so workers must be started here.
        await self.workers.maybe_start_workers_for_existing_session()

        # Wait for workers only when restarting into an existing session (workers were just
        # spawned above). On a fresh boot there is no session yet -- workers start later
        # when AppStartSessionRequest arrives and will finish before the user can open a
        # workflow. Awaiting here on fresh boot would wait the full worker-startup grace
        # period for workers that haven't started yet.
        if self.engine.get_session_id():
            await self.workers.await_pending_workers()

        # Register all secrets now that libraries are loaded and settings are merged
        self.engine.secrets_manager.register_all_secrets()

        # We have to load all libraries before we attempt to load workflows. The refresh registers
        # library-declared workflows first, then user workflows.
        await self.engine.workflow_manager.refresh_workflow_registry()

        # Signal readiness so the application layer can render its library status
        # table and "engine ready" banner, and other consumers (the GUI, the
        # desktop app) can react to the same event. Only the orchestrator
        # announces readiness; dedicated library workers do not. Presentation is
        # owned by the app, not the engine. The library statuses reflect real
        # fitness because workers have already reported back above.
        if not self._is_worker:
            self.engine.event_manager.put_event(
                AppEvent(
                    payload=EngineReadyEvent(
                        libraries=self._collect_library_load_statuses(),
                        is_initial_start=True,
                    )
                )
            )

    async def _collect_library_workflow_files(self) -> list[str]:
        """Collect workflow file paths declared by all registered libraries.

        Returns absolute paths to workflow files, adding each library's base directory
        to sys.path so relative imports work when the workflow is loaded.
        """
        workflow_files: list[str] = []
        library_result = await self.engine.ahandle_request(ListRegisteredLibrariesRequest(broadcast_result=False))
        if not isinstance(library_result, ListRegisteredLibrariesResultSuccess):
            return workflow_files
        for library_name in library_result.libraries:
            try:
                library = LibraryRegistry.get_library(name=library_name)
            except KeyError:
                logger.error("Could not find library '%s'", library_name)
                continue
            library_data = library.get_library_data()
            if not library_data.workflows:
                continue
            # Workflows are stored relative to the library JSON; find the library's path.
            for library_info in self._library_file_path_to_info.values():
                if library_info.library_name == library_name:
                    library_path = Path(library_info.library_path)
                    base_dir = library_path.parent.absolute()
                    # Put the directory first on the Python path to allow for relative imports.
                    # Every registry refresh collects again, so drop an earlier entry rather
                    # than stacking duplicates.
                    if str(base_dir) in sys.path:
                        sys.path.remove(str(base_dir))
                    sys.path.insert(0, str(base_dir))
                    workflow_files.extend(str(base_dir / workflow) for workflow in library_data.workflows)
                    break
        return workflow_files

    async def _run_reload_libraries(self, request: ReloadAllLibrariesRequest) -> ResultPayload:  # noqa: ARG002
        # Start with a clean slate.
        clear_all_request = ClearAllObjectStateRequest(i_know_what_im_doing=True)
        clear_all_result = await self.engine.ahandle_request(clear_all_request)
        if not clear_all_result.succeeded():
            details = "Failed to clear the existing object state when preparing to reload all libraries."
            return ReloadAllLibrariesResultFailure(result_details=details)

        # Unload all libraries now.
        all_libraries_request = ListRegisteredLibrariesRequest(broadcast_result=False)
        all_libraries_result = await self.engine.ahandle_request(all_libraries_request)
        if not isinstance(all_libraries_result, ListRegisteredLibrariesResultSuccess):
            details = "When preparing to reload all libraries, failed to get registered libraries."
            logger.error(details)
            return ReloadAllLibrariesResultFailure(result_details=details)

        # Close the gate before the registry is emptied, and not any earlier: the
        # enumeration above goes through on_list_registered_libraries_request, which waits on
        # this same gate, so closing it first deadlocks the reload against itself.
        #
        # A single flag cannot describe two rebuilds at once, so overlapping reloads are not
        # supported: the second shares this Event and the first to finish reopens it.
        self._close_libraries_loading_gate()

        try:
            for library_name in all_libraries_result.libraries:
                unload_library_request = UnloadLibraryFromRegistryRequest(library_name=library_name)
                unload_library_result = self.engine.handle_request(unload_library_request)
                if not unload_library_result.succeeded():
                    details = f"When preparing to reload all libraries, failed to unload library '{library_name}'."
                    logger.error(details)
                    return ReloadAllLibrariesResultFailure(result_details=details)

            # Notify pre-reload callbacks (e.g. to terminate worker processes) before
            # load_all_libraries_from_config runs so that workers can be cleanly restarted.
            for callback in self._pre_reload_callbacks:
                try:
                    await callback()
                except Exception as e:
                    logger.warning("Pre-reload callback raised an exception: %s", e)

            # Load (or reload, which should trigger a hot reload) all libraries.
            # Pass _target_library_names so workers reload only their designated libraries.
            reconcile_failures = await self.load_all_libraries_from_config(
                target_library_names=self._target_library_names
            )
        finally:
            # Bailing out above (a failed unload, or a raise) would otherwise leave the gate
            # closed and every gated query waiting for the life of the process. Only reopen a
            # gate still closed at this point: once load_all_libraries_from_config has run,
            # it has already reopened this one, and re-setting it could open a gate a
            # concurrent rebuild closed after that.
            if not self._libraries_loading_complete.is_set():
                self._libraries_loading_complete.set()

        # Re-spawn workers for libraries that require them; reset_workers terminated them above.
        await self.workers.maybe_start_workers_for_existing_session()

        # Wait for worker-delegated libraries to finish loading before returning. The GUI
        # opens the current workflow immediately on receiving the result, so all node types
        # must be registered before we respond.
        await self.workers.await_pending_workers()

        # Signal readiness again so the app re-renders the library status table with real
        # fitness now that workers have reported back. is_initial_start=False so the app
        # refreshes the table without re-showing the startup banner. Orchestrator only.
        if not self._is_worker:
            self.engine.event_manager.put_event(
                AppEvent(
                    payload=EngineReadyEvent(
                        libraries=self._collect_library_load_statuses(),
                        is_initial_start=False,
                    )
                )
            )

        # Hard activation: a reload is interactive (project switch / explicit reload), so a
        # reconcile failure (bad engine_version gate or a sourced library that could not be
        # provisioned) surfaces to the caller. ProjectManager turns this into a
        # SetCurrentProjectResultFailure the GUI can show.
        if reconcile_failures:
            details = "Reloaded libraries but reconcile reported problem(s): " + "; ".join(reconcile_failures)
            return ReloadAllLibrariesResultFailure(result_details=details)

        details = (
            "Successfully reloaded all libraries. All object state was cleared and previous libraries were unloaded."
        )
        return ReloadAllLibrariesResultSuccess(result_details=ResultDetails(message=details, level=logging.INFO))
