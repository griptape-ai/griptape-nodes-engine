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

import logging
from dataclasses import dataclass
from dataclasses import fields as dc_fields
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
from griptape_nodes.utils.async_utils import call_function

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


# Every request in these modules is local unless named in _FORWARDING_FILESYSTEM_REQUESTS, which
# _candidate_request_types subtracts. os_events is swept wholesale because it is filesystem work
# almost throughout, and the workspace is shared on disk -- stated as a module-wide rule rather than a
# list because the list kept being incomplete. "Almost": DeduceSequencesFromFileListRequest does no
# I/O and is local for a different reason (category 3 below), so the sweep is a good default rather
# than a guarantee, which is why the pinning test makes each member a reviewed decision.
_WHOLESALE_LOCAL_MODULES = (os_events,)

# These modules hold requests that are NOT all filesystem work, so membership is earned rather than
# assumed: only the ones carrying something the wire cannot deliver qualify. Everything else in them
# forwards like any other request.
_SELECTIVE_LOCAL_MODULES = (artifact_events,)


def _local_only_by_derivation() -> frozenset[type[RequestPayload]]:
    """The request types routed local by rule rather than by name.

    Two policies, because the two module groups differ. A wholesale module is local in full. A
    selective module contributes only its undeliverable carriers -- a MacroPath field, or a
    bare `type` field whose class has to land in the process that will instantiate it.

    Deriving rather than listing means a request added to one of these modules later is covered
    without anyone remembering this file. The cost is that it also ROUTES that request without
    anyone deciding, so `tests/unit/app/test_worker_routing_filesystem.py` pins the exact
    membership: a new one fails that test and forces the call.
    """
    derived: set[type[RequestPayload]] = set()
    for module in _WHOLESALE_LOCAL_MODULES:
        derived.update(_candidate_request_types(module))
    for module in _SELECTIVE_LOCAL_MODULES:
        derived.update(
            payload
            for payload in _candidate_request_types(module)
            if _carries_a_macro_path(payload) or _registers_a_python_class(payload)
        )
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
        # Grouped by the reason that BINDS each entry, since the reasons expire differently: 1, 2 and
        # 4 are permanent, 3 goes away if serialization improves. An entry with several reasons sits
        # under the one that would still keep it local once the others were solved.
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
        # Derived, not enumerated, so a request added to a swept module is covered by construction;
        # `test_worker_routing_filesystem.py` pins the membership and comments it by group. All of
        # os_events, because the workspace is shared on disk (OpenAssociatedFileRequest excepted --
        # opening a file in the user's app belongs where the user is), plus the artifact_events
        # requests that carry a MacroPath or a bare `type` for a process-local registry.
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
        return ActivateProjectResultSuccess(result_details=f"Adopted project from orchestrator: {request.project_id}.")

    event_manager.assign_manager_to_request_type(ReloadConfigRequest, handle_reload_config)
    event_manager.assign_manager_to_request_type(RefreshSecretsRequest, handle_refresh_secrets)
    event_manager.assign_manager_to_request_type(ActivateProjectRequest, handle_activate_project)
