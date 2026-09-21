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
from typing import TYPE_CHECKING, Any, TypeGuard, cast

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
from griptape_nodes.utils.async_utils import call_function, to_thread

logger = logging.getLogger("griptape_nodes")

if TYPE_CHECKING:
    from collections.abc import Iterator
    from types import ModuleType

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
class DropAllLocalObjectsRequest(RequestPayload, SkipTheLineMixin):
    """Sent by the orchestrator to each registered worker when workflow object state is cleared.

    Clearing workflow state deletes every node and so every key referring to a held object, leaving
    those objects unreachable while still holding what they hold. A broadcast rather than a local hook
    because the worker, not the orchestrator, is the process holding them.

    SkipTheLineMixin for the same reason as its siblings: the alternative is a queued ExecuteNodeRequest
    running against objects belonging to a workflow that is already gone.
    """


@dataclass
@PayloadRegistry.register
class DropLocalObjectsRequest(RequestPayload, SkipTheLineMixin):
    """Sent by the orchestrator when named held objects stop being referenced.

    A handle parameter's value is a key. When that value is replaced or the node carrying it is deleted,
    the object behind the old key is unreachable, and the process holding it is usually a worker.

    Carries a list because releases queue up on sync paths and drain together, and SkipTheLineMixin for
    the same reason as its sibling: a queued execution would otherwise run first and could rebuild what
    this is about to release.
    """

    keys: list[str]


@dataclass
@PayloadRegistry.register
class DropLocalObjectsResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Worker released whichever of the named objects it was holding."""


@dataclass
@PayloadRegistry.register
class DropLocalObjectsResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Worker failed while releasing the named objects."""


@dataclass
@PayloadRegistry.register
class DropAllLocalObjectsResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Worker released every object it was holding for its libraries."""


@dataclass
@PayloadRegistry.register
class DropAllLocalObjectsResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Worker failed to release the objects it was holding."""


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
    # Generation of the orchestrator's committed activation. The worker adopts strictly increasing
    # generations only, so two activations that overlap resolve to the newer one regardless of the
    # order they arrive or finish in.
    generation: int = 0


@dataclass
@PayloadRegistry.register
class ActivateProjectResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Worker adopted the orchestrator's current project."""


@dataclass
@PayloadRegistry.register
class ActivateProjectResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Worker failed to adopt the orchestrator's current project."""


# The artifact_events requests a worker answers itself, named rather than detected. The reason is
# never serialization -- these all cross the wire fine -- so a rule that reads field annotations
# would state the wrong cause and would silently re-route them the moment serialization changed.
_LOCAL_ONLY_ARTIFACT_REQUESTS: frozenset[type[RequestPayload]] = frozenset(
    {
        # Registration puts a *class* into this process's provider registry. Forwarding would
        # register it in the orchestrator while the worker, the process that needs the provider to
        # run a node, registers nothing.
        artifact_events.RegisterArtifactProviderRequest,
        artifact_events.RegisterPreviewGeneratorRequest,
        # Preview generation resolves a provider out of that same process-local registry and writes
        # into the project's previews directory, so it belongs with the registrations above.
        artifact_events.GeneratePreviewRequest,
        artifact_events.GeneratePreviewFromDefaultsRequest,
        artifact_events.GetPreviewForArtifactRequest,
    }
)


# OpenAssociatedFileRequest is the one filesystem request deliberately NOT local: it hands a path to
# the OS to open in the user's default application, and that side effect belongs where the user is,
# not in a headless subprocess.
_FORWARDING_FILESYSTEM_REQUESTS: frozenset[type[RequestPayload]] = frozenset({os_events.OpenAssociatedFileRequest})


# Swept wholesale, minus _FORWARDING_FILESYSTEM_REQUESTS: the workspace is shared on disk, so the
# worker's own answer is the authoritative one and forwarding a write corrupts it (`content` is
# `str | bytes` and the wire form resolves back to `str`). A rule rather than a list because the
# list kept being incomplete. It is a good default and not a guarantee -- DeduceSequencesFromFileList
# does no I/O and is local for another reason -- so the pinning test makes each member a decision.
_WHOLESALE_LOCAL_MODULES = (os_events,)


def _local_only_by_derivation() -> frozenset[type[RequestPayload]]:
    """The request types routed local by rule rather than one at a time.

    A request added to a swept module is routed without anyone deciding, so
    `tests/unit/app/test_worker_routing_filesystem.py` pins the membership and a new one fails it.

    artifact_events is not swept: what binds its local members is what each request DOES, which no
    rule over field types can see, so they are named in _LOCAL_ONLY_ARTIFACT_REQUESTS.
    """
    derived: set[type[RequestPayload]] = set(_LOCAL_ONLY_ARTIFACT_REQUESTS)
    for module in _WHOLESALE_LOCAL_MODULES:
        derived.update(_candidate_request_types(module))
    return frozenset(derived)


def _candidate_request_types(module: ModuleType) -> Iterator[type[RequestPayload]]:
    """Request types ``module`` defines, minus any deliberately left forwarding."""
    for payload in vars(module).values():
        if _is_own_request_type(payload, module) and payload not in _FORWARDING_FILESYSTEM_REQUESTS:
            yield payload


def _is_own_request_type(payload: object, module: ModuleType) -> TypeGuard[type[RequestPayload]]:
    """Whether ``payload`` is a request type this module DEFINES, not one it imported.

    `__module__` rather than mere namespace membership. Neither swept module re-exports a request
    type today, so this is prospective: it keeps a future `from ... import SomeRequest` in one of them
    from silently becoming local-only, which for anything graph-mutating would let a worker act on its
    own non-authoritative copy.
    """
    return (
        isinstance(payload, type)
        and issubclass(payload, RequestPayload)
        and payload is not RequestPayload
        and payload.__module__ == module.__name__
    )


_LOCAL_ONLY_FILESYSTEM_REQUESTS: frozenset[type[RequestPayload]] = _local_only_by_derivation()


LOCAL_ONLY_REQUEST_TYPES: frozenset[type[RequestPayload]] = frozenset(
    {
        # Requests a worker answers ITSELF while executing a node; everything else forwards to the
        # orchestrator, which owns the authoritative state. An exclusion list rather than an
        # allowlist, so the cost of forgetting a new request type is a round trip, not a wrong answer
        # resolved against the worker's own copy.
        #
        # Grouped by the reason that BINDS each entry. An entry with several sits under the one that
        # would still keep it local once the others were solved.
        #
        # --- 1. Belongs to this process ---------------------------------------------------------
        #
        # A worker's own execution; forwarding would route it straight back here.
        ExecuteNodeRequest,
        # Cancels that execution, so it belongs to the process running it.
        CancelExecuteNodeRequest,
        # Published to the orchestrator, never dispatched here, so this entry is inert. Listed to keep
        # the worker wire out of the forwarding path by construction rather than by luck.
        RegisterWorkerRequest,
        # Same, on graceful shutdown.
        UnregisterWorkerRequest,
        # Liveness challenge addressed to this worker; a forwarded answer would prove nothing about it.
        WorkerHeartbeatRequest,
        # Orchestrator-internal, issued and handled there, so it never crosses the boundary. Inert here.
        StartWorkerRequest,
        # Addressed to this worker: re-read the config file both processes share. The orchestrator
        # installs no handler, so forwarding would not find one.
        ReloadConfigRequest,
        # Addressed to this worker: refresh its env-var view of the shared .env.
        RefreshSecretsRequest,
        # Addressed to this worker: adopt the project the orchestrator switched to.
        ActivateProjectRequest,
        # Adopting a project reloads THIS worker's libraries. Forwarding would instead reload the
        # orchestrator's, and its pre-reload callback is reset_workers -- which terminates the very
        # worker that asked, mid-node. Reachable because in_node_execution() is a process-wide
        # refcount, so a broadcast handler forwards whenever any node happens to be running.
        ReloadAllLibrariesRequest,
        #
        # --- 2. The worker's own answer is the correct one ---------------------------------------
        #
        # All of os_events, because the workspace is shared on disk (OpenAssociatedFileRequest
        # excepted), plus the named artifact_events requests that answer out of this process's
        # provider registry.
        *_LOCAL_ONLY_FILESYSTEM_REQUESTS,
        # The payload IS the file body, so forwarding would base64 a whole generated asset across
        # the boundary on every save. The worker writes it through its own storage driver instead and
        # forwards only the registration.
        CreateStaticFileRequest,
        # These two mint URLs from `storage_driver.base_url`, which on a worker is the orchestrator's
        # server adopted at spawn -- so the answer matches the orchestrator's and forwarding would
        # only add a round trip. If that handover fails the worker answers with its own ephemeral
        # port, and forwarding these two would have been the better answer.
        CreateStaticFileUploadUrlRequest,
        CreateStaticFileDownloadUrlRequest,
        # NOT just a URL mint, so the note above does not apply: with preview or metadata_only this
        # reaches artifact_manager's process-local provider registry. Forwarding would look for a
        # worker library's provider on the orchestrator and silently find nothing.
        CreateStaticFileDownloadUrlFromPathRequest,
        # The worker already adopted this project and its base directory is shared on disk, so the
        # local answer is correct. Forwarding also costs a round trip per saved file.
        GetCurrentProjectRequest,
        # Reads the situation template out of that same project.
        GetSituationRequest,
        # Resolves a macro against it, on the per-file write path.
        GetPathForMacroRequest,
        # The write-side counterpart: maps a written path back to a portable macro reference.
        AttemptMapAbsolutePathToProjectRequest,
        # Which device to run on describes the machine that will run the model, and that is this one.
        # Forwarding asked the orchestrator about its own hardware -- indistinguishable while both
        # share a machine, and wrong the moment a venue runs anywhere else.
        GetExecutionDeviceRequest,
        #
        # --- 3. The wire cannot carry it today --------------------------------------------------
        #
        # One member, reaching the set through the splat rather than by name.
        # DeduceSequencesFromFileListRequest does no filesystem I/O -- it groups a caller-supplied
        # path list -- so shared-disk authority does not bind it; what does is its failure result
        # declaring `SequenceScanFailureReason | FileIOFailureReason`, a union cattrs cannot
        # disambiguate. Fix that and it can forward.
        #
        # Everything else the wire cannot carry also has a permanent reason and is filed under it.
        #
        # --- 4. Carries a live Python object ----------------------------------------------------
        #
        # Carries a ResourceType instance, which `json.dumps(default=str)` turns into a string: the
        # orchestrator would register that string and the worker nothing, with no error either side.
        # `_registers_a_python_class` does not catch it, since that matches a bare `type` annotation.
        RegisterResourceTypeRequest,
        # `value_transform` is an optional Callable, and routing is per type, so the type stays local.
        # A stringified transform would corrupt the migration rather than misplace a registration.
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


async def _handle_drop_all_local_objects(
    request: DropAllLocalObjectsRequest,  # noqa: ARG001
    *,
    event_manager: EventManager,
) -> ResultPayload:
    """Release every object this process is holding for its libraries.

    Takes the event manager because that is how it reaches this engine; the process-global engine would
    silently no-op for an engine an embedder built directly.
    """
    resource_manager = event_manager.engine.resource_manager

    # Refuse while this worker is executing a node: this request skips the line, so releasing here
    # would free the pipeline under a forward pass already running on a worker thread. Reachable by
    # closing a workflow mid-render, since a torch inference does not cancel. Nothing re-issues the
    # drop afterwards, so what is skipped stays until a later teardown.
    if event_manager.in_node_execution():
        details = "Declined to release held objects: this worker is executing a node. They will be released at a later teardown."
        logger.info(details)
        return DropAllLocalObjectsResultSuccess(result_details=details)

    try:
        # Off the loop: a release hook is `del model` plus a CUDA cache flush, and blocking the
        # worker's loop past the heartbeat timeout gets it evicted mid-load.
        dropped = await to_thread(resource_manager.drop_all_local_objects)
    except Exception as e:
        details = (
            f"Attempted to release objects held for this worker's libraries. Failed because of {type(e).__name__}: {e}."
        )
        logger.error(details)
        return DropAllLocalObjectsResultFailure(result_details=details)
    return DropAllLocalObjectsResultSuccess(result_details=f"Released {dropped} held object(s).")


async def _handle_drop_local_objects(
    request: DropLocalObjectsRequest,
    *,
    event_manager: EventManager,
) -> ResultPayload:
    """Release the named objects, whichever of them this process is holding.

    Unlike its drop-all sibling this does not decline mid-execution: the keys named here were already
    replaced or deleted on the orchestrator, so a node executing now cannot be using them -- it was given
    the new value. Waiting would keep the memory for the length of a render.

    Drops parked entries only. The orchestrator broadcasts keys it cannot check locally -- the entry lives
    here -- so the never-release-a-library-named-key rule is enforced on this side.
    """
    resource_manager = event_manager.engine.resource_manager

    def release_all() -> int:
        # Parked entries only: the orchestrator broadcasts keys it holds no entry for, so provenance is
        # checked here, in the process with the entry. A key a library named itself is never dropped.
        return sum(1 for key in request.keys if resource_manager.drop_parked_local_object(key))

    try:
        # Off the loop for the same reason as its sibling: a release hook is `del model` plus a CUDA cache
        # flush, and a worker whose loop is blocked past the heartbeat timeout is evicted mid-load.
        dropped = await to_thread(release_all)
    except Exception as e:
        details = f"Attempted to release {len(request.keys)} held object(s). Failed because of {type(e).__name__}: {e}."
        logger.error(details)
        return DropLocalObjectsResultFailure(result_details=details)
    return DropLocalObjectsResultSuccess(result_details=f"Released {dropped} of {len(request.keys)} named object(s).")


def register_broadcast_handlers(
    event_manager: EventManager,
    *,
    config_manager: ConfigManager,
    secrets_manager: SecretsManager,
    project_manager: ProjectManager,
) -> asyncio.Event:
    """Install worker-side handlers for orchestrator-originated broadcasts.

    Workers receive ``ReloadConfigRequest`` / ``RefreshSecretsRequest`` /
    ``ActivateProjectRequest`` from the orchestrator and respond by re-reading
    the shared on-disk state or adopting the orchestrator's current project. The
    actual work is delegated to the corresponding manager so domain logic stays
    in the manager and routing decisions stay here.

    Returns:
        An event set once this worker has settled the startup state the orchestrator sent it, and
        may begin loading libraries. Today that state is the project, settled either by adopting an
        activation or by finding it already stale -- libraries resolve against the workspace a
        project decides, and the orchestrator sends the activation rather than answering
        registration with it, so nothing else orders the two.

        Deliberately named for the worker rather than the project: a second piece of startup state
        should gate this same event rather than introduce a second one for the caller to wait on.
    """
    worker_settled = asyncio.Event()

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

    # Activation awaits internally, so two arriving close together interleave: the older can pass
    # the staleness check, suspend, and finish after the newer, leaving the worker on the older
    # project while both report success. Overlap is a property of the await, not of who sent it. The
    # staleness check must stay inside the lock -- it reads the generation the previous holder wrote.
    #
    # TODO(griptape-ai/internal#266): a single-consumer queue replaces this and the generations,
    # making the ordering structural rather than a rule every future caller has to remember.
    adoption_lock = asyncio.Lock()

    async def handle_activate_project(request: ActivateProjectRequest) -> ResultPayload:
        # A concurrent ReloadConfigRequest is safe to interleave with this: _activate_project does
        # clear_project_layers() plus a full re-merge, so a load_configs() landing alongside it only
        # refreshes the user layer idempotently.
        #
        # A worker's project registry is frozen at boot, so a project the orchestrator registered
        # after this worker spawned is absent here. Re-read the shared config and re-run discovery so
        # the worker learns it, and fail loud if the id is still unknown: landing on a stale project
        # while reporting success is the divergence this whole path exists to prevent.
        async with adoption_lock:
            if project_manager.is_stale_adoption(request.project_id, request.generation):
                # Settled, not adopted: a newer activation already landed, so whatever is waiting
                # on this has the answer it needs.
                worker_settled.set()
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
            worker_settled.set()
        return ActivateProjectResultSuccess(result_details=f"Adopted project from orchestrator: {request.project_id}.")

    event_manager.assign_manager_to_request_type(ReloadConfigRequest, handle_reload_config)
    event_manager.assign_manager_to_request_type(RefreshSecretsRequest, handle_refresh_secrets)
    event_manager.assign_manager_to_request_type(ActivateProjectRequest, handle_activate_project)

    _register_local_object_handlers(event_manager)

    return worker_settled


def _register_local_object_handlers(event_manager: EventManager) -> None:
    """Wire the two teardown requests that free objects this worker is holding.

    Separate from the rest so the registrations sit with each other rather than adding two more branches
    to a function that already carries every other broadcast.
    """

    async def handle_drop_all_local_objects(request: DropAllLocalObjectsRequest) -> ResultPayload:
        return await _handle_drop_all_local_objects(request, event_manager=event_manager)

    event_manager.assign_manager_to_request_type(DropAllLocalObjectsRequest, handle_drop_all_local_objects)

    async def handle_drop_local_objects(request: DropLocalObjectsRequest) -> ResultPayload:
        return await _handle_drop_local_objects(request, event_manager=event_manager)

    event_manager.assign_manager_to_request_type(DropLocalObjectsRequest, handle_drop_local_objects)
