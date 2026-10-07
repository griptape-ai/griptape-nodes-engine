from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any

from griptape_nodes.common.strict_mode import (
    STRICT_MODE,
    StrictModeScopeKind,
)
from griptape_nodes.common.strict_mode_checks import RULES
from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
from griptape_nodes.exe_types.node_types import BaseNode
from griptape_nodes.node_library.library_declarations import (
    LibraryDeclaration,
    WorkerCompatibility,
    WorkerMode,
    WorkerModeCompatibility,
    requires_worker_process,
)
from griptape_nodes.node_library.library_registry import (
    LibraryMetadata,
    LibraryRegistry,
    NodeMetadata,
)
from griptape_nodes.retained_mode.engine import EngineScoped
from griptape_nodes.retained_mode.events.app_events import (
    AppSessionStartedEvent,
    LibraryLoadedNotification,
    ReportLibraryLoadedRequest,
    ReportLibraryLoadedResultFailure,
    ReportLibraryLoadedResultSuccess,
    WorkerNodeSchema,
    WorkerParameterSchema,
)
from griptape_nodes.retained_mode.events.worker_events import StartWorkerRequest
from griptape_nodes.retained_mode.managers.fitness_problems.libraries import (
    DependencyInstallationFailedProblem,
    IncompatibleRequirementsProblem,
)
from griptape_nodes.retained_mode.managers.library.common import LibraryFitness, LibraryLifecycleState
from griptape_nodes.retained_mode.managers.settings import (
    LIBRARIES_TO_REGISTER_KEY,
    WORKER_LIBRARY_LOAD_TIMEOUT_KEY,
    LibraryRegistration,
)
from griptape_nodes.retained_mode.request_handlers import handles
from griptape_nodes.serialization.values import Unencodable, try_encode
from griptape_nodes.utils.library_utils import (
    extract_library_path,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.events.base_events import ResultPayload
    from griptape_nodes.retained_mode.managers.event_manager import EventManager

logger = logging.getLogger("griptape_nodes")


def make_worker_stub_class(class_name: str, param_schemas: list[WorkerParameterSchema]) -> type[BaseNode]:
    """Create a dynamic BaseNode subclass from worker-reported parameter schemas.

    The stub registers the correct parameters so the GUI can display them and
    workflow loading can restore saved values. Execution always runs on the worker,
    so process() is a no-op.
    """

    def stub_init(self: BaseNode, name: str, metadata: dict | None = None) -> None:  # type: ignore[override]
        BaseNode.__init__(self, name=name, metadata=metadata)
        for schema in param_schemas:
            allowed_modes = set()
            if schema.mode_allowed_input:
                allowed_modes.add(ParameterMode.INPUT)
            if schema.mode_allowed_property:
                allowed_modes.add(ParameterMode.PROPERTY)
            if schema.mode_allowed_output:
                allowed_modes.add(ParameterMode.OUTPUT)
            self.add_parameter(
                Parameter(
                    name=schema.name,
                    default_value=schema.default_value,
                    type=schema.type or None,
                    input_types=schema.input_types or None,
                    output_type=schema.output_type or None,
                    tooltip=schema.tooltip,
                    tooltip_as_input=schema.tooltip_as_input,
                    tooltip_as_property=schema.tooltip_as_property,
                    tooltip_as_output=schema.tooltip_as_output,
                    allowed_modes=allowed_modes,
                    settable=schema.settable,
                    serializable=schema.serializable,
                    user_defined=schema.user_defined,
                    private=schema.private,
                    exclude_from_metadata=schema.exclude_from_metadata,
                    ui_options=schema.ui_options,
                )
            )

    def stub_process(_: BaseNode) -> None:
        pass

    return type(class_name, (BaseNode,), {"__init__": stub_init, "process": stub_process})


def hook_overridden_outside_engine(node_class: type, hook_name: str) -> bool:
    """Return True when ``hook_name``'s defining class lives outside the engine package.

    Walks the MRO to the first class whose ``__dict__`` defines the hook -- the
    implementation that would actually run. ``BaseNode``'s own no-op (and any
    engine-owned override) attributes to a ``griptape_nodes.`` module and is not
    the library author's code.
    """
    for klass in node_class.__mro__:
        if hook_name in klass.__dict__:
            module = klass.__module__ or ""
            return module != "griptape_nodes" and not module.startswith("griptape_nodes.")
    return False


def try_json_serialize(value: Any) -> Any:
    """Return value if it is JSON-serializable, otherwise return None."""
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return None
    else:
        return value


def encodable_or_none(value: Any) -> Any:
    """Return value if it has a plain-data form, otherwise None."""
    if isinstance(try_encode(value), Unencodable):
        return None
    return value


def resolve_executes_in_worker(*, requires_worker: bool, metadata: LibraryMetadata) -> bool:
    """Whether this library's nodes execute in a dedicated worker process.

    True for legacy worker-mode libraries (requires_worker) and for libraries that
    declare execution dependencies: their heavy packages live in .venv-exec, which is
    only on sys.path in a worker, so process() can only run there.
    """
    dependencies = metadata.dependencies
    has_exec_dependencies = bool(dependencies and dependencies.pip_dependencies_exec)
    return requires_worker or has_exec_dependencies


class LibraryWorkers(EngineScoped):
    # How a worker reaches the orchestrator to report a load. Registered by whatever owns the
    # transport, because this manager has no way to send a request to another process. Stays None
    # on the orchestrator and on a single-process engine, where there is nobody to report to.
    _library_load_reporter: Callable[[ReportLibraryLoadedRequest], Awaitable[None]] | None = None

    # Per-node timeout for the schema probe. Node __init__ methods that make
    # synchronous handle_request calls can deadlock against async handlers that
    # await init-time events (e.g. WorkflowManager.wait_for_workflows_loaded),
    # so each probe runs in a worker thread with this ceiling.
    _SCHEMA_PROBE_TIMEOUT_S: float = 10.0
    # Sentinel name passed to the throwaway node instance built for schema
    # discovery. The instance is discarded after its parameters are read.
    _SCHEMA_PROBE_NODE_NAME: str = "__schema_probe__"

    # Hook families that are consulted only on the orchestrator, where a
    # worker-hosted library is represented by a synthesized stub class (see
    # make_worker_stub_class). An author override of any of these never runs
    # for an Isolated library.
    _CONNECTION_HOOK_NAMES: tuple[str, ...] = (
        "allow_incoming_connection",
        "allow_outgoing_connection",
        "allow_incoming_connection_by_class",
        "allow_outgoing_connection_by_class",
        "before_incoming_connection",
        "after_incoming_connection",
        "before_outgoing_connection",
        "after_outgoing_connection",
        "before_incoming_connection_removed",
        "after_incoming_connection_removed",
        "before_outgoing_connection_removed",
        "after_outgoing_connection_removed",
    )
    # Value hooks fire on the orchestrator stub for editor edits (never the
    # author's code) and on the worker only during execute-time input
    # hydration, on a transient node discarded after process returns.
    _VALUE_HOOK_NAMES: tuple[str, ...] = (
        "before_value_set",
        "after_value_set",
    )

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
        """Record how a library loaded in the worker that hosts it.

        The notification raised at the end is this process's own. A peer's copy never reaches a
        listener, so the GUI hears about a worker's library from the orchestrator accepting the
        report rather than from a relayed event.
        """
        library_info = self.engine.library_manager.get_library_info_by_library_name(request.library_name)
        if library_info is None:
            details = f"Received a library load report for unknown library '{request.library_name}'."
            logger.warning(details)
            return ReportLibraryLoadedResultFailure(result_details=details)
        # Only a legacy worker-mode library takes its fitness from the worker: the orchestrator
        # never loaded it, so the worker's verdict is the only one there is. An exec-deps library
        # loaded REAL nodes here and derived its own fitness from doing so, and overwriting that
        # misreports in both directions without the reason travelling -- only the log gets
        # problem_details.
        if library_info.requires_worker:
            library_info.fitness = LibraryFitness(request.fitness)
            library_info.lifecycle_state = LibraryLifecycleState.LOADED
        if request.problem_details:
            logger.warning(
                "Worker reported problems loading library '%s': %s",
                request.library_name,
                request.problem_details,
            )
        # Stubs stand in for node classes this process cannot import, so the sidebar and workflow
        # loading have something to work with. An exec-deps library's real classes are already
        # registered here, and replacing them with stubs would throw away converters, validators,
        # traits and hooks.
        if request.node_schemas and library_info.requires_worker:
            self._register_nodes_from_worker_schemas(request.library_name, request.node_schemas)
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
            result_details=f"Recorded the worker's load of library '{request.library_name}' as {request.fitness}."
        )

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

    def on_worker_evicted(self, worker_engine_id: str, library_name: str | None) -> None:
        """Called when a worker is evicted by the orchestrator heartbeat monitor.

        Transitions WORKER_PENDING libraries to FAILURE so downstream code and the UI
        can reflect that the worker did not successfully confirm its library load.
        The library remains registered in LibraryRegistry so node stubs stay visible.
        """
        if not library_name:
            return
        library_info = self.engine.library_manager.get_library_info_by_library_name(library_name)
        if library_info is None:
            return
        if library_info.lifecycle_state == LibraryLifecycleState.WORKER_PENDING:
            library_info.lifecycle_state = LibraryLifecycleState.FAILURE
            library_info.fitness = LibraryFitness.UNUSABLE
            logger.warning(
                "Worker '%s' evicted before confirming load of library '%s'; library marked as FAILURE.",
                worker_engine_id,
                library_name,
            )

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

    async def await_pending_workers(self, wait_seconds: float | None = None) -> None:
        """Wait for all WORKER_PENDING libraries to report back via ReportLibraryLoadedRequest.

        On timeout, marks remaining pending libraries as FAILURE/UNUSABLE so the rest of
        initialization can continue.

        When wait_seconds is None, reads the worker library load timeout from config:
        first-time installs of large libraries can easily exceed the heartbeat timeout, so
        boot waits on the load deadline rather than on a heartbeat one.
        """
        # WORKER_PENDING only: an exec-dependencies library also has a worker whose readiness
        # execution routing waits on, but its nodes loaded locally already and boot must not block
        # on that worker.
        pending = {
            info.library_name: info
            for info in self.engine.library_manager._library_file_path_to_info.values()
            if info.library_name is not None and info.lifecycle_state == LibraryLifecycleState.WORKER_PENDING
        }
        if not pending:
            return

        if wait_seconds is None:
            config_mgr = self.engine.config_manager
            wait_seconds = float(
                config_mgr.get_config_value(WORKER_LIBRARY_LOAD_TIMEOUT_KEY, default=600.0, cast_type=float)
            )

        unsettled = await self.engine.library_manager._worker_manager.wait_for_libraries(list(pending), wait_seconds)
        for library_name in unsettled:
            info = pending[library_name]
            info.lifecycle_state = LibraryLifecycleState.FAILURE
            info.fitness = LibraryFitness.UNUSABLE
            # Recorded on WorkerManager, which owns why a worker is unavailable and releases whoever
            # is waiting on it -- otherwise the next run falls through to "the worker may still be
            # starting up" for a worker already given up on.
            self.engine.library_manager._worker_manager.note_worker_unavailable(
                library_name, f"its worker process did not report a library load within {wait_seconds} seconds."
            )
            logger.warning(
                "Worker for library '%s' timed out after %s seconds; marked as FAILURE.",
                library_name,
                wait_seconds,
            )

    async def serialize_library_node_schemas(self, library_name: str) -> list[WorkerNodeSchema]:
        """Serialize node parameter schemas for a loaded library.

        Called on the worker process after library nodes are loaded. Probes each
        registered node type to extract its parameter layout, so the orchestrator
        can create stub classes without importing the library's Python modules.
        """
        # Only a legacy worker-mode library is stubbed on the orchestrator: an
        # execution-dependency library is loaded there for real (its node modules are
        # base-clean by contract), and stub registration is gated on `requires_worker`.
        # The two stub-loss detectors below are gated on the same fact, or they warn that
        # converters, traits and hooks "will not execute on the orchestrator stub" for a
        # library that has no stub -- sending an author to fix code that is already correct.
        library_info = self.engine.library_manager.get_library_info_by_library_name(library_name)
        will_be_stubbed = library_info is None or library_info.requires_worker
        try:
            library = LibraryRegistry.get_library(library_name)
        except KeyError:
            logger.warning("Cannot serialize schemas: library '%s' not found in registry.", library_name)
            return []

        node_schemas: list[WorkerNodeSchema] = []
        for class_name in library.get_registered_nodes():
            # The is-constructing flag set inside create_node propagates into
            # the asyncio.to_thread worker via contextvars.copy_context().
            probe = None
            with STRICT_MODE.open_scope(
                kind=StrictModeScopeKind.LOAD_PROBE,
                subject=class_name,
                library_name=library_name,
                is_worker=self.engine.library_manager.is_worker,
            ) as scope:
                try:
                    probe = await asyncio.wait_for(
                        asyncio.to_thread(
                            LibraryRegistry.create_node,
                            node_type=class_name,
                            name=self._SCHEMA_PROBE_NODE_NAME,
                            specific_library_name=library_name,
                        ),
                        timeout=self._SCHEMA_PROBE_TIMEOUT_S,
                    )
                except TimeoutError:
                    logger.warning(
                        "Schema probe for node class '%s' in library '%s' timed out after %.1fs; "
                        "skipping. The node's __init__ likely makes a blocking call that cannot "
                        "complete during library load.",
                        class_name,
                        library_name,
                        self._SCHEMA_PROBE_TIMEOUT_S,
                    )
                    continue
                except Exception:
                    logger.debug("Could not probe node class '%s' for schema serialization.", class_name, exc_info=True)
                    continue
                # Run the parameter-behavior-drop and inert-hook detectors
                # inside the scope so warnings attach to the same LOAD_PROBE
                # scope that owns the probe attempt.
                if will_be_stubbed:
                    self._report_parameter_behavior_losses(probe)
                    self._report_inert_worker_hooks(probe)

            # Drop the class only when a rule flagged drops_class_from_schema
            # fired. Severity is a logging concern; drops_class_from_schema is
            # the lifecycle signal ("this class is broken enough to exclude
            # from the schema"). Gating on severity would also drop the class
            # for any future ergonomics rule whose worker_escalation flag is
            # left at the True default.
            blocking_rule_ids = {rid for rid, r in RULES.items() if r.drops_class_from_schema}
            if any(v.rule_id in blocking_rule_ids for v in scope.violations):
                continue

            param_schemas: list[WorkerParameterSchema] = []
            for param in probe.parameters:
                allowed_modes = param.allowed_modes
                param_schemas.append(
                    WorkerParameterSchema(
                        name=param.name,
                        type=param._type or "",
                        input_types=list(param._input_types or []),
                        output_type=param._output_type or "",
                        default_value=encodable_or_none(param.default_value),
                        tooltip=param.tooltip,
                        tooltip_as_input=param.tooltip_as_input,
                        tooltip_as_property=param.tooltip_as_property,
                        tooltip_as_output=param.tooltip_as_output,
                        mode_allowed_input=ParameterMode.INPUT in allowed_modes,
                        mode_allowed_property=ParameterMode.PROPERTY in allowed_modes,
                        mode_allowed_output=ParameterMode.OUTPUT in allowed_modes,
                        user_defined=param.user_defined,
                        settable=param.settable,
                        serializable=param.serializable,
                        private=param.private,
                        exclude_from_metadata=param.exclude_from_metadata,
                        ui_options=try_json_serialize(param.ui_options) if param.ui_options else None,
                    )
                )
            node_schemas.append(WorkerNodeSchema(class_name=class_name, parameters=param_schemas))

        return node_schemas

    def resolve_requires_worker(
        self,
        registered_path: str | None,
        declarations: list[LibraryDeclaration],
    ) -> bool:
        """Apply per-entry worker_mode_override iff the library is worker-compatible; otherwise honor the manifest.

        The override lives on the matching `LibraryRegistration` entry in
        `libraries_to_register` (keyed by the user's verbatim path, mirroring how `enabled`
        works). Lookup uses the user's *registered* path -- the same string they typed in
        config -- not the resolved absolute path, because the engine's path resolution can
        diverge between sides (workspace-relative, `~`-expansion, symlink-following).

        Returns the load-time `requires_worker` bool used by the lifecycle state machine.
        Centralized here so both writer sites stay in sync.
        """
        manifest_default = requires_worker_process(declarations)

        capability = next((d for d in declarations if isinstance(d, WorkerModeCompatibility)), None)
        is_incompatible = capability is not None and capability.compatibility is WorkerCompatibility.INCOMPATIBLE
        if is_incompatible or not registered_path:
            return manifest_default

        config_mgr = self.engine.config_manager
        raw_entries = config_mgr.get_config_value(LIBRARIES_TO_REGISTER_KEY) or []
        target_path_lower = registered_path.lower()
        for entry in raw_entries:
            entry_path = extract_library_path(entry)
            if not entry_path or entry_path.lower() != target_path_lower:
                continue
            override_raw: Any = None
            if isinstance(entry, LibraryRegistration):
                override_raw = entry.worker_mode_override
            elif isinstance(entry, dict):
                override_raw = entry.get("worker_mode_override")
            if override_raw is None:
                return manifest_default
            try:
                effective_mode = WorkerMode(override_raw)
            except ValueError:
                logger.debug(
                    "Ignoring invalid worker_mode_override %r on libraries_to_register entry %r; falling back to manifest suggested mode.",
                    override_raw,
                    registered_path,
                )
                return manifest_default
            return effective_mode is WorkerMode.WORKER

        return manifest_default

    async def _start_workers(self) -> None:
        """Issue StartWorkerRequest for every library that requires a dedicated worker.

        Sets each matching library back to WORKER_PENDING and asks WorkerManager to
        spawn a subprocess.  Used on session start (both initial and subsequent) so
        that worker creation is always tied to an active session.
        """
        for library_info in self.engine.library_manager._library_file_path_to_info.values():
            # `enabled` matters: executes_in_worker is set before the lifecycle is overwritten with
            # DISABLED, so without this a library the user turned off still got an idle worker
            # subprocess -- and a legacy worker-mode one had its DISABLED state overwritten with
            # WORKER_PENDING, then blocked boot for the full startup grace waiting on a worker that
            # was never going to report.
            if (
                library_info.executes_in_worker
                and library_info.enabled
                and library_info.library_name
                and not self.engine.library_manager.is_worker
            ):
                # A declared resource this machine does not have makes the whole spawn pointless:
                # get_worker_for_library refuses on that reason before it ever consults a worker,
                # so the process would resolve and download an entire execution environment --
                # torch, gigabytes -- to serve nothing. Checked before the lifecycle moves below,
                # because a legacy worker-mode library parked in WORKER_PENDING with no spawn
                # coming would block boot for the whole startup grace.
                #
                # Only for libraries whose nodes already exist here. A legacy worker-mode library
                # has none: the orchestrator skips its node modules entirely and its classes arrive
                # as stubs from the worker's ReportLibraryLoadedRequest. Skipping its spawn would
                # leave the library with no node types at all -- an empty entry in the sidebar and
                # placeholder nodes in any workflow using it. It still cannot execute, because the
                # reset below preserves its refusal.
                has_unmet_requirement = any(
                    isinstance(problem, (IncompatibleRequirementsProblem, DependencyInstallationFailedProblem))
                    for problem in library_info.problems
                )
                if has_unmet_requirement and not library_info.requires_worker:
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
                # Legacy worker-mode libraries load AS the worker confirms (stubs meanwhile),
                # so their lifecycle gates on the spawn. Exec-deps libraries loaded real
                # nodes locally already: the worker gates execution availability only, and
                # registration must not block on it.
                if library_info.requires_worker:
                    library_info.lifecycle_state = LibraryLifecycleState.WORKER_PENDING
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
                #
                # An unmet requirement is not an account of an attempt -- the machine still lacks the
                # resource -- and it is the ONLY gate get_worker_for_library has, so clearing it
                # would dispatch to a worker that cannot load the library.
                if not has_unmet_requirement:
                    library_info.execution_unavailable_reason = None
                await self.engine.ahandle_request(StartWorkerRequest(library_name=library_info.library_name))

    def _register_nodes_from_worker_schemas(self, library_name: str, node_schemas: list[WorkerNodeSchema]) -> None:
        """Register stub node classes on the orchestrator from worker-reported schemas.

        Creates a minimal dynamic class for each node type so that LibraryRegistry can
        instantiate nodes (for workflow loading, sidebar display, etc.) without importing
        the worker library's Python modules.
        """
        try:
            library = LibraryRegistry.get_library(library_name)
        except KeyError:
            logger.warning("Cannot register worker node schemas: library '%s' not found in registry.", library_name)
            return

        # Build a lookup from class_name -> NodeMetadata using the library JSON schema.
        library_data = library.get_library_data()
        metadata_by_class: dict[str, NodeMetadata] = {
            node_def.class_name: node_def.metadata for node_def in library_data.nodes
        }

        for node_schema in node_schemas:
            metadata = metadata_by_class.get(node_schema.class_name)
            if metadata is None:
                logger.warning(
                    "Worker reported node '%s' for library '%s' but it has no metadata entry; skipping.",
                    node_schema.class_name,
                    library_name,
                )
                continue

            stub_class = make_worker_stub_class(node_schema.class_name, node_schema.parameters)
            library_problem = library.register_new_node_type(stub_class, metadata=metadata)
            if library_problem is not None:
                logger.warning(
                    "Problem registering worker stub for node '%s': %s",
                    node_schema.class_name,
                    library_problem,
                )

        # Register widgets declared by the library.
        if library_data.widgets:
            for widget_def in library_data.widgets:
                widget_problem = LibraryRegistry.register_widget_from_library(library_name, widget_def.name)
                if widget_problem is not None:
                    logger.warning(
                        "Problem registering widget '%s' from library '%s': %s",
                        widget_def.name,
                        library_name,
                        widget_problem,
                    )

    def _report_inert_worker_hooks(self, probe: BaseNode) -> None:
        """Report lifecycle-hook overrides that will not fire for a worker-hosted library.

        Connection hooks are invoked exclusively on the orchestrator (connections are
        orchestrator-owned state), where a worker-hosted library is represented by a
        synthesized stub that does not carry the override -- the author's code never
        runs at all. Value hooks fire on the orchestrator stub for editor edits and on
        the worker only during execute-time input hydration, so value transformation
        works but editor-time reactivity and parameter-list mutation do not.

        Only overrides defined outside the engine are reported: engine-owned bases and
        components (modules under ``griptape_nodes.``) also lose these hooks under
        isolation, but that is the engine's gap to close, not something a library
        author can remediate.
        """
        node_class = type(probe)
        connection_overrides = [
            hook for hook in self._CONNECTION_HOOK_NAMES if hook_overridden_outside_engine(node_class, hook)
        ]
        if connection_overrides:
            rule = RULES["connection-hooks-inert-on-worker"]
            STRICT_MODE.report(
                rule_id=rule.rule_id,
                message=rule.render(node_class=node_class.__name__, hook_names=", ".join(connection_overrides)),
            )
        value_overrides = [hook for hook in self._VALUE_HOOK_NAMES if hook_overridden_outside_engine(node_class, hook)]
        if value_overrides:
            rule = RULES["value-hooks-execute-only-on-worker"]
            STRICT_MODE.report(
                rule_id=rule.rule_id,
                message=rule.render(node_class=node_class.__name__, hook_names=", ".join(value_overrides)),
            )

    def _report_parameter_behavior_losses(self, probe: BaseNode) -> None:
        """Report parameter-behaviors-dropped-in-schema for any probe parameter that has live behaviors.

        ``WorkerParameterSchema`` only carries the scalar-shaped fields of a
        ``Parameter``. Converters, validators, and traits cannot be
        serialized across the worker boundary and therefore will not run on
        the orchestrator stub. If a parameter has any of these attached,
        report it so the author sees a named warning during library load.
        """
        rule = RULES["parameter-behaviors-dropped-in-schema"]
        for param in probe.parameters:
            dropped: list[str] = []
            if param.has_directly_attached_converters:
                dropped.append("converters")
            if param.has_directly_attached_validators:
                dropped.append("validators")
            if param.has_traits:
                dropped.append("traits")
            if not dropped:
                continue
            STRICT_MODE.report(
                rule_id=rule.rule_id,
                message=rule.render(parameter_name=param.name, dropped_attributes=", ".join(dropped)),
            )
