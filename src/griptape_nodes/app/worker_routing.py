"""Worker-side dispatch overrides for orchestrator-owned request types.

On a worker, a handful of request types must be serviced by the orchestrator
because the authoritative state (flow graph, connections, node registry) lives
there. This module provides:

- ``LOCAL_ONLY_REQUEST_TYPES``: the request classes a worker answers ITSELF. Every other
  registered type gets a ``RemoteHandler`` that forwards to the orchestrator.
- ``RemoteHandler``: an async callable that replaces the original manager
  handler for those request types on the worker. While the worker is actively
  executing a node it forwards; outside that scope it delegates back to the
  original local handler (which preserves bootstrap / library-load behavior).
- ``register_remote_handlers``: swaps the dispatch table entries on a
  just-configured worker after ``configure_worker_forwarding`` has wired up
  the RequestClient and loop references.
- ``ReloadConfigRequest`` / ``RefreshSecretsRequest`` and their Success/Failure
  payloads: orchestrator-originated broadcasts that every worker handles
  locally to re-read shared on-disk state. They live here, not in
  ``worker_events.py``, because their reason for existing is a routing
  decision (orchestrator fan-out to all workers); the names are deliberately
  free of any "Worker" prefix because, by this module's principle, an event's
  type carries no routing metadata.

The routing decision lives entirely on the worker. Events themselves carry no
routing metadata.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from dataclasses import fields as dc_fields
from typing import TYPE_CHECKING, Any, cast

from griptape_nodes.retained_mode.events import artifact_events, os_events
from griptape_nodes.retained_mode.events.base_events import (
    RequestPayload,
    ResultPayload,
    ResultPayloadFailure,
    ResultPayloadSuccess,
    SkipTheLineMixin,
    WorkflowNotAlteredMixin,
)
from griptape_nodes.retained_mode.events.execution_events import (
    CancelExecuteNodeRequest,
    ExecuteNodeRequest,
)
from griptape_nodes.retained_mode.events.library_events import ReloadAllLibrariesRequest
from griptape_nodes.retained_mode.events.parameter_events import MigrateParameterRequest
from griptape_nodes.retained_mode.events.payload_registry import PayloadRegistry
from griptape_nodes.retained_mode.events.project_events import (
    AttemptMapAbsolutePathToProjectRequest,
    GetCurrentProjectRequest,
    GetPathForMacroRequest,
    GetSituationRequest,
    SetCurrentProjectRequest,
)
from griptape_nodes.retained_mode.events.resource_events import (
    GetExecutionDeviceRequest,
    RegisterResourceTypeRequest,
)
from griptape_nodes.retained_mode.events.static_file_events import (
    CreateStaticFileDownloadUrlFromPathRequest,
    CreateStaticFileDownloadUrlRequest,
    CreateStaticFileRequest,
    CreateStaticFileUploadUrlRequest,
)
from griptape_nodes.retained_mode.events.worker_events import (
    RegisterWorkerRequest,
    StartWorkerRequest,
    UnregisterWorkerRequest,
    WorkerHeartbeatRequest,
)
from griptape_nodes.retained_mode.managers.event_manager import ResultContext
from griptape_nodes.utils.async_utils import call_function

logger = logging.getLogger("griptape_nodes")

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.managers.config_manager import ConfigManager
    from griptape_nodes.retained_mode.managers.event_manager import EventManager
    from griptape_nodes.retained_mode.managers.project_manager import ProjectManager
    from griptape_nodes.retained_mode.managers.secrets_manager import SecretsManager


HandlerCallback = "Callable[[RequestPayload], ResultPayload | Awaitable[ResultPayload]]"


@dataclass
@PayloadRegistry.register
class ReloadConfigRequest(RequestPayload, SkipTheLineMixin):
    """Sent by the orchestrator to each registered worker after a config mutation succeeds.

    On the same machine orchestrator and workers share
    ~/.config/griptape_nodes/griptape_nodes_config.json, but a worker's
    in-memory merged_config only reflects what it read on boot. This tells
    the worker to re-read the file so subsequent get_config_value calls
    see the new value.

    Uses SkipTheLineMixin so the worker processes it immediately, ahead of
    any queued ExecuteNodeRequest that would otherwise observe stale config.
    """


@dataclass
@PayloadRegistry.register
class ReloadConfigResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Worker reloaded its config from disk."""


@dataclass
@PayloadRegistry.register
class ReloadConfigResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Worker failed to reload its config from disk."""


@dataclass
@PayloadRegistry.register
class RefreshSecretsRequest(RequestPayload, SkipTheLineMixin):
    """Sent by the orchestrator to each registered worker after a secret mutation succeeds.

    The global .env at ~/.config/griptape_nodes/.env is shared across
    processes on the same machine, but the worker's os.environ snapshot
    was populated at boot from the file as it existed then. Without this
    refresh, get_secret() would see the stale env-var shadow (its highest
    priority source) even after the orchestrator updated the file.

    Uses SkipTheLineMixin to avoid a queued ExecuteNodeRequest reading
    the stale secret before the refresh lands.
    """


@dataclass
@PayloadRegistry.register
class RefreshSecretsResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Worker refreshed its secrets from the shared .env file."""


@dataclass
@PayloadRegistry.register
class RefreshSecretsResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Worker failed to refresh its secrets."""


@dataclass
@PayloadRegistry.register
class ActivateProjectRequest(RequestPayload, SkipTheLineMixin):
    """Sent by the orchestrator to each registered worker after it switches projects.

    The orchestrator is the single source of truth for the current project, but a
    worker is only restarted on a switch that changes library config. A switch that
    keeps the same workspace and library config (only environment / directories /
    situations differ) leaves the worker on a stale project. This tells the worker
    to adopt the orchestrator's new project so env vars, directory macros, and
    situation/path macros resolve against the right project.

    project_id is the opaque id of the new current project (SYSTEM_DEFAULTS_KEY for
    system defaults). A worker boots like an engine off the same shared on-disk
    config, so the orchestrator's registry id is already loaded in the worker.

    Uses SkipTheLineMixin so the worker activates the new project immediately, ahead
    of any queued ExecuteNodeRequest that would otherwise run against the stale one.
    """

    project_id: str
    # Generation of the orchestrator's committed activation. The worker adopts strictly
    # increasing generations only, which orders a switch fan-out against the registration
    # reply deterministically -- the two arrive on different loops in arbitrary order.
    generation: int = 0


@dataclass
@PayloadRegistry.register
class ActivateProjectResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Worker adopted the orchestrator's current project."""


@dataclass
@PayloadRegistry.register
class ActivateProjectResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Worker failed to adopt the orchestrator's current project."""


def _registers_a_python_class(payload: type) -> bool:
    """Whether a field is declared as a bare ``type``.

    Two reasons such a request must stay local, and either alone is sufficient. cattrs has no
    structure hook for ``type``, so the orchestrator's ingress raises and the worker blocks until
    the forward times out. And the point of the request is to put a *class* into a process-local
    registry: forwarding it would register something in the wrong process while the worker -- the
    one that needs the provider while running a node -- registers nothing.
    """
    return any(
        _annotation_text(field.type).strip().startswith(("type[", "type "))
        or _annotation_text(field.type).strip() == "type"
        for field in dc_fields(payload)
    )


def _annotation_text(annotation: object) -> str:
    """The annotation as text, whichever form the module stored it in.

    `str(annotation)` rather than `__name__`, because composite shapes have no useful name:
    `MacroPath | None` is a UnionType whose `__name__` does not exist, and `list[MacroPath]`
    is named just `list` -- both would silently defeat a matcher that only reads names, and
    a missed MacroPath means a forwarded request that dies on the wire instead of answering
    locally.
    """
    if isinstance(annotation, str):
        return annotation
    if isinstance(annotation, type):
        # str() of a plain class is "<class 'x.Y'>", which matches nothing a matcher looks
        # for -- and `provider_class: type` (a bare builtin) is exactly the shape the
        # type-registration predicate exists to catch.
        return annotation.__name__
    return str(annotation)


def _carries_a_macro_path(payload: type) -> bool:
    """Whether any field of ``payload`` is declared as a ``MacroPath``.

    A MacroPath wraps a ParsedMacro, which will not serialize, so a request carrying one cannot be
    forwarded at all: the send raises and the worker blocks until the forward times out. Matched on
    the declared annotation text rather than a resolved type, because these modules annotate under
    `from __future__ import annotations` and several cannot be resolved at runtime.

    Applied to the two modules swept below, which are the only ones defining a MacroPath carrier
    today. The invariant is wider than that scope, so a test sweeps the whole payload registry
    and fails if a carrier appears elsewhere -- rather than this widening to the registry, where
    a false positive would silently answer a request in the wrong process.
    """
    return any("MacroPath" in _annotation_text(field.type) for field in dc_fields(payload))


# Requests a worker must answer itself, derived from the CAUSE rather than listed, so a request
# added later is covered without anyone remembering this file. Two independent reasons: filesystem
# work, where the shared-on-disk workspace makes the worker's own answer the authoritative one and
# forwarding a write corrupts it (`content` is `str | bytes` and the wire form resolves back to
# `str`); and carrying a MacroPath, which cannot serialize at all.
#
# OpenAssociatedFileRequest is the one filesystem request deliberately NOT local: it hands a path to
# the OS to open in the user's default application, and that side effect belongs where the user is,
# not in a headless subprocess. It carries no MacroPath, so nothing else claims it.
_FORWARDING_FILESYSTEM_REQUESTS: frozenset[type[RequestPayload]] = frozenset({os_events.OpenAssociatedFileRequest})


def _local_only_by_derivation() -> frozenset[type[RequestPayload]]:
    derived: set[type[RequestPayload]] = set()
    for module in (os_events, artifact_events):
        for payload in vars(module).values():
            # `__module__` rather than mere namespace membership: these modules import request
            # types from each other, and a re-exported CreateNodeRequest becoming local-only would
            # silently let a worker mutate its own non-authoritative graph.
            if (
                not isinstance(payload, type)
                or not issubclass(payload, RequestPayload)
                or payload is RequestPayload
                or payload.__module__ != module.__name__
            ):
                continue
            if payload in _FORWARDING_FILESYSTEM_REQUESTS:
                continue
            if module is os_events or _carries_a_macro_path(payload) or _registers_a_python_class(payload):
                derived.add(payload)
    return frozenset(derived)


_LOCAL_ONLY_FILESYSTEM_REQUESTS: frozenset[type[RequestPayload]] = _local_only_by_derivation()


LOCAL_ONLY_REQUEST_TYPES: frozenset[type[RequestPayload]] = frozenset(
    {
        # Requests a worker must answer ITSELF while executing a node. Everything else
        # forwards to the orchestrator, which owns the authoritative state.
        #
        # This list is deliberately an exclusion list rather than an allowlist. With an
        # allowlist, every newly added request type silently resolved against the worker's
        # own managers -- a non-authoritative copy -- and every migration note that told
        # authors to "use a request instead" was wrong for any type nobody remembered to
        # add. Defaulting to forwarding makes that class of mistake impossible.
        #
        # execution_events: forwarding a worker's own execution request would send it
        # straight back to the orchestrator, which would route it here again.
        ExecuteNodeRequest,
        CancelExecuteNodeRequest,
        # static_file_events: the payload is bytes. A worker writes them to the shared
        # workspace itself and forwards only the resulting registration; shipping a 4K
        # video across the boundary is the one thing storage must never do.
        CreateStaticFileRequest,
        CreateStaticFileUploadUrlRequest,
        CreateStaticFileDownloadUrlRequest,
        CreateStaticFileDownloadUrlFromPathRequest,
        # worker_events + broadcasts: these travel orchestrator -> worker. Sending them back
        # is the same loop as execution.
        RegisterWorkerRequest,
        UnregisterWorkerRequest,
        WorkerHeartbeatRequest,
        StartWorkerRequest,
        ReloadConfigRequest,
        RefreshSecretsRequest,
        ActivateProjectRequest,
        # Adopting a project reloads this worker's libraries, and that reload must stay here. The
        # orchestrator decides WHEN libraries reload -- it is mid-reload already, which is what sent
        # the activation -- so forwarding would have the worker dictate to it. Worse, the
        # orchestrator's pre-reload callback is reset_workers, which terminates the very worker that
        # asked, mid-node. Reached because in_node_execution() is a process-wide refcount, so a
        # reload dispatched by a broadcast handler forwards whenever any node happens to be running.
        ReloadAllLibrariesRequest,
        # os_events: a worker does its own filesystem work. The workspace is shared on disk, so
        # the local answer is already the right one -- the same argument as the project reads
        # below -- and forwarding was actively destructive. `content` is `str | bytes`, and the
        # wire form base64s bytes into a JSON string that cattrs resolves back to `str`, so a
        # worker's write landed corrupted with no error anywhere. Requests carrying a `MacroPath`
        # (`File("{outputs}/out.png")`, `Directory(...).with_versioning()`) cannot be serialized
        # at all, so the worker blocked until the forward timed out. And four of the failure
        # results declare `SequenceScanFailureReason | FileIOFailureReason`, a union cattrs cannot
        # disambiguate, so even the error could not come back.
        #
        # Stated as a rule over the whole module rather than a list of the ones found broken,
        # because the list kept being incomplete: `_LOCAL_ONLY_FILESYSTEM_REQUESTS` is derived
        # from os_events itself, so a filesystem request added later is covered by construction.
        # `tests/unit/app/test_worker_routing_filesystem.py` fails if a new one appears without a
        # routing decision.
        *_LOCAL_ONLY_FILESYSTEM_REQUESTS,
        # project_events: a worker already adopts the orchestrator's project (at spawn, and
        # again on a switch), and a project's base directory is shared on-disk state -- the
        # same class as the workspace path. So the worker's own answer is correct by
        # construction, and forwarding buys nothing while costing a round trip on a path as
        # hot as writing sidecar metadata for every saved file.
        #
        # It also cost correctness. Forwarded results are rebuilt with cattrs, and
        # GetCurrentProjectResultSuccess annotates `project_info: ProjectInfo` under
        # TYPE_CHECKING to break an import cycle. cattrs cannot resolve that name from this
        # module's namespace, so the converter's documented NameError fallback passes the raw
        # dict into the constructor: isinstance() passes while `.project_info` is a dict, and
        # the first attribute access fails. In a worker that meant every sidecar write logged
        # "'dict' object has no attribute 'project_base_dir'" and silently wrote nothing.
        GetCurrentProjectRequest,
        # The same argument, for the reads that resolve a path against that project. All three are
        # pure template reads carrying serializable payloads, and all three sit on the per-file
        # write path, so forwarding them costs a round trip per saved file.
        # GetPathForMacroResultFailure also declares `missing_variables: set[str] | None`, the same
        # shape cattrs cannot round-trip that broke the os_events forwards.
        #
        # AttemptMapAbsolutePathToProjectRequest is the write-side counterpart and hid longest:
        # every file written through a ProjectFileDestination asks whether its absolute path maps
        # back into the project so the caller can store a portable macro reference instead. It sits
        # AFTER the write rather than before it, which is the only reason it read as a different
        # case; its handler is a pure read of the same project template.
        GetSituationRequest,
        GetPathForMacroRequest,
        AttemptMapAbsolutePathToProjectRequest,
        # resource_events: which device to run on describes the machine that will run the model,
        # which is THIS one. Forwarding asked the orchestrator about its own hardware --
        # indistinguishable while both processes share a machine, and wrong the moment a venue runs
        # anywhere else. Detection is torch-free and reads local hardware, so the worker's answer is
        # authoritative by construction.
        GetExecutionDeviceRequest,
        # Two requests carry a live Python object in a field, and both were local under the
        # allowlist this exclusion list replaces -- the flip is what started forwarding them.
        # `_registers_a_python_class` does not catch either: it matches a `type[...]` annotation,
        # and these carry an INSTANCE (a ResourceType, a Callable). Forwarding puts the object
        # through `json.dumps(default=str)`, so the orchestrator registers a string and the worker
        # -- the process that needs it while running a node -- registers nothing, with no error on
        # either side.
        RegisterResourceTypeRequest,
        MigrateParameterRequest,
    }
)


@dataclass
class RemoteHandler:
    """Worker-side dispatch shim.

    Registered in place of the original manager handler for every registered type except
    LOCAL_ONLY_REQUEST_TYPES. Forwards to the orchestrator while the worker is
    inside a ``worker_node_execution_scope``; delegates to the original
    handler otherwise (so bootstrap / library-load paths keep running locally).

    ``original`` is the handler this shim replaced and MUST be retained so the
    out-of-scope fallback can still service requests that bootstrap code makes
    (e.g. ``self.add_parameter(...)`` issuing ``AddParameterToNodeRequest``
    from a node's ``__init__`` under a LOAD_PROBE scope).
    """

    original: Any  # HandlerCallback; typed loosely to avoid a runtime import cycle
    event_manager: EventManager

    async def __call__(self, request: RequestPayload) -> ResultPayload:
        if self.event_manager.in_node_execution():
            event_result = await self.event_manager.forward_to_orchestrator(request, ResultContext())
            return cast("ResultPayload", event_result.result)
        return await call_function(self.original, request)


def schedule_broadcast(broadcast_type: type[RequestPayload]) -> None:
    """Ask the orchestrator's WorkerManager to fan ``broadcast_type`` out to every worker.

    Use this from a manager's request handler (orchestrator-side) to fire the
    matching broadcast after a successful local mutation -- e.g. ``ConfigManager``
    calls ``schedule_broadcast(ReloadConfigRequest)`` after persisting a config
    write. No-op when no engine has been built yet (isolated unit tests that construct
    managers on their own) or when no workers are registered.

    Imports the engine lazily because this module is loaded during engine boot,
    before the accessor is ready.
    """
    from griptape_nodes.retained_mode.engine import current_engine, has_current_engine

    if not has_current_engine():
        return
    current_engine().worker_manager.schedule_broadcast(broadcast_type)


def register_remote_handlers(event_manager: EventManager) -> None:
    """Route requests made during node execution to the orchestrator.

    Swaps a RemoteHandler in for every registered request type except those in
    LOCAL_ONLY_REQUEST_TYPES. The handler forwards only while the worker is inside a
    ``worker_node_execution_scope`` and delegates to the original handler otherwise, so
    engine boot and library load -- which legitimately need this process's own managers --
    are unaffected.

    Must be called after every manager has finished registering (i.e. after the engine is
    constructed) AND after ``configure_worker_forwarding`` has supplied the RequestClient,
    topic, and loop references. See ``_run_worker`` in app.py.
    """
    for request_type in event_manager.registered_request_types():
        if request_type in LOCAL_ONLY_REQUEST_TYPES:
            continue
        original = event_manager.get_manager_for_request_type(request_type)
        if original is None:
            continue
        remote = RemoteHandler(original=original, event_manager=event_manager)
        event_manager.remove_manager_from_request_type(request_type)
        event_manager.assign_manager_to_request_type(request_type, remote)


def register_broadcast_handlers(
    event_manager: EventManager,
    *,
    config_manager: ConfigManager,
    secrets_manager: SecretsManager,
    project_manager: ProjectManager,
) -> None:
    """Install worker-side handlers for orchestrator-originated broadcasts.

    Workers receive ``ReloadConfigRequest`` / ``RefreshSecretsRequest`` /
    ``ActivateProjectRequest`` from the orchestrator and respond by re-reading
    the shared on-disk state or adopting the orchestrator's current project. The
    actual work is delegated to the corresponding manager so domain logic stays
    in the manager and routing decisions stay here.
    """

    def handle_reload_config(request: ReloadConfigRequest) -> ResultPayload:  # noqa: ARG001
        try:
            config_manager.load_configs()
        except Exception as e:
            details = f"Attempted to reload config from disk. Failed because of {type(e).__name__}: {e}."
            logger.error(details)
            return ReloadConfigResultFailure(result_details=details)
        return ReloadConfigResultSuccess(result_details="Reloaded config from disk.")

    def handle_refresh_secrets(request: RefreshSecretsRequest) -> ResultPayload:  # noqa: ARG001
        try:
            secrets_manager.refresh_from_env_file()
        except Exception as e:
            details = f"Attempted to refresh secrets from shared .env file. Failed because of {type(e).__name__}: {e}."
            logger.error(details)
            return RefreshSecretsResultFailure(result_details=details)
        return RefreshSecretsResultSuccess(result_details="Refreshed secrets from shared .env file.")

    # Serializes adoptions against each other. The registration-reply adoption and a switch
    # fan-out run as separate tasks on this loop, and without the lock an older activation can
    # pass the staleness check, suspend at an await inside activation, and FINISH after a newer
    # one -- leaving the worker on the older project while both replies report success. The
    # staleness check must not be hoisted out of the lock: it reads the generation the previous
    # holder records.
    adoption_lock = asyncio.Lock()

    async def handle_activate_project(request: ActivateProjectRequest) -> ResultPayload:
        # A ReloadConfigRequest may land concurrently: a post-init orchestrator switch
        # persists project_file, which emits ConfigChanged -> ReloadConfigRequest to every
        # worker, right alongside this activation. Both are SkipTheLine and run as separate
        # tasks, so they interleave. It is safe because _activate_project below does
        # clear_project_layers() + a full re-merge, so a concurrent load_configs() only
        # refreshes the user layer idempotently and cannot leave layers half-applied.
        #
        # A worker boots like an engine off the same shared on-disk config, so the
        # orchestrator's project id is usually already loaded in the worker's registry.
        # But a worker's registry is frozen at boot: if the orchestrator switched to a
        # project it registered AFTER this worker spawned, the id is absent here. Re-read
        # the shared config and re-run registered-project discovery (engine-style) so the
        # worker learns it. Fail loud if the id is still unknown -- silently landing on a
        # stale project while reporting success is exactly the divergence we must avoid.
        async with adoption_lock:
            if project_manager.is_stale_adoption(request.project_id, request.generation):
                return ActivateProjectResultSuccess(
                    result_details=(
                        f"Skipped adopting project '{request.project_id}' (generation "
                        f"{request.generation}): a newer activation was already adopted."
                    )
                )
            if not await project_manager.ensure_project_loaded(request.project_id):
                details = (
                    f"Attempted to adopt orchestrator project '{request.project_id}'. "
                    f"Failed because the id is absent from the worker's registry even after "
                    f"reloading config and re-running registered-project discovery."
                )
                logger.error(details)
                return ActivateProjectResultFailure(result_details=details)

            set_result = await project_manager.on_set_current_project_request(
                SetCurrentProjectRequest(project_id=request.project_id)
            )
            if set_result.failed():
                details = (
                    f"Attempted to adopt orchestrator project '{request.project_id}'. "
                    f"Failed with result: {set_result.result_details}"
                )
                logger.error(details)
                return ActivateProjectResultFailure(result_details=details)
            project_manager.record_adopted_generation(request.generation)
        return ActivateProjectResultSuccess(result_details=f"Adopted project from orchestrator: {request.project_id}.")

    event_manager.assign_manager_to_request_type(ReloadConfigRequest, handle_reload_config)
    event_manager.assign_manager_to_request_type(RefreshSecretsRequest, handle_refresh_secrets)
    event_manager.assign_manager_to_request_type(ActivateProjectRequest, handle_activate_project)
