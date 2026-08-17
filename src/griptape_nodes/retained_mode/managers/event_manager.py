from __future__ import annotations

import asyncio
import contextvars
import inspect
import logging
import threading
from collections import defaultdict
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import fields
from typing import TYPE_CHECKING, Any, cast

from asyncio_thread_runner import ThreadRunner
from typing_extensions import TypedDict, TypeVar

from griptape_nodes.common.strict_mode import STRICT_MODE
from griptape_nodes.common.strict_mode_checks import RULES
from griptape_nodes.exe_types.node_types import BaseNode
from griptape_nodes.node_library.library_registry import LibraryRegistry
from griptape_nodes.retained_mode.engine import EngineScoped, engine_scope
from griptape_nodes.retained_mode.events.base_events import (
    AppPayload,
    BaseEvent,
    EventRequest,
    EventResultFailure,
    EventResultSuccess,
    ExecutionGriptapeNodeEvent,
    ExecutionPayload,
    ProgressEvent,
    RequestPayload,
    ResultDetails,
    ResultPayload,
    StrictModeViolationDetail,
)
from griptape_nodes.retained_mode.events.event_converter import converter
from griptape_nodes.retained_mode.events.generic_events import GenericResultFailure
from griptape_nodes.retained_mode.events.payload_registry import PayloadRegistry
from griptape_nodes.retained_mode.managers.authorization_checkpoint import (
    AuthorizationCheckpoint,
    CheckpointDenial,
    CheckpointFailure,
)
from griptape_nodes.utils.async_utils import call_function, to_thread

if TYPE_CHECKING:
    import types
    from collections.abc import Awaitable, Callable, Iterator

    from griptape_nodes.api_client.request_client import RequestClient
    from griptape_nodes.retained_mode.engine import Engine

    # A post-dispatch hook observes a completed request. It may be sync or async;
    # its return value is ignored because nothing awaits the notification.
    PostDispatchHook = Callable[[Any, ResultPayload], None] | Callable[[Any, ResultPayload], Awaitable[None]]


RP = TypeVar("RP", bound=RequestPayload, default=RequestPayload)
AP = TypeVar("AP", bound=AppPayload, default=AppPayload)
EP = TypeVar("EP", bound=ExecutionPayload, default=ExecutionPayload)


_active_request_type: ContextVar[type[RequestPayload] | None] = ContextVar(
    "_event_manager_active_request_type", default=None
)

# (request_type, callback) pairs for the post-dispatch hooks running above this point
# in the current logical chain. A hook task is created with this marker set, and every
# request the hook issues inherits it, so any hook already running further up the chain
# is skipped on a re-entrant dispatch instead of re-triggering without bound. That covers
# both a hook re-issuing the type it subscribes to and a cycle between hooks on different
# types. The chain is threaded explicitly into `_run_post_dispatch_hook` rather than read
# from the context there, because each hook starts from a *fresh* context (see that
# method) which would otherwise reset the marker to empty and make every generation
# look like the first. Scoped to the chain rather than to the process, so it cannot
# suppress unrelated dispatches and has no state that can leak.
_active_post_dispatch_hooks: ContextVar[tuple[tuple[type[RequestPayload], Any], ...]] = ContextVar(
    "_event_manager_active_post_dispatch_hooks", default=()
)

# Post-dispatch hooks are deliberately unbounded -- every result gets its own task so a
# notification hook never misses a request. This is purely a "something is wrong" signal
# for a hook that runs slower than requests arrive.
POST_DISPATCH_HOOK_INFLIGHT_WARNING_THRESHOLD = 100


def _is_async_callable(callback: Any) -> bool:
    """Whether calling `callback` returns a coroutine.

    `inspect.iscoroutinefunction` alone misses callable objects whose `__call__` is
    async (worker_routing.RemoteHandler is one), which would otherwise be handed to a
    thread where the coroutine it returns is never awaited.
    """
    if inspect.iscoroutinefunction(callback):
        return True
    if not callable(callback):
        return False
    return inspect.iscoroutinefunction(type(callback).__call__)


def current_request_type() -> type[RequestPayload] | None:
    """Return the request type currently being dispatched on this task, or None.

    Detectors that need to know "what request is the active handler servicing?"
    (e.g. parameter-mutation-during-aprocess, which exempts the sanctioned
    AddParameterToNodeRequest / RemoveParameterFromNodeRequest paths) read
    this ContextVar.
    """
    return _active_request_type.get()


def reentrant_bus_in_init_would_report() -> bool:
    """Whether a bus request issued right now would fire ``reentrant-bus-in-init``.

    Both halves of the detector's condition (see
    ``EventManager._report_reentrant_bus_in_init``), exposed so code that runs
    inside a node ``__init__`` can ask BEFORE issuing a request and skip it
    rather than commit the violation. The detector and every such caller read
    this one predicate, so "we deferred" and "it would have been reported"
    cannot drift apart as the scopes change.

    Being inside a node ``__init__`` is not sufficient on its own. ``STRICT_MODE.report``
    no-ops when no scope is active, and scopes open in exactly two places: around the
    worker's schema probe (LOAD_PROBE, where a violation drops the class from the worker
    schema) and around node execution (RUNTIME_EXECUTE, where a worker violation promotes
    the result to a failure). An ordinary ``CreateNodeRequest`` -- an editor drop, a
    workflow load, any single-process engine, where no probe runs at all -- opens neither,
    so a read from ``__init__`` there is free and callers should just do it. Keying a
    deferral off ``is_constructing_node()`` alone instead makes every node in every
    deployment pay for a hazard only the worker's probe has.

    When strict mode is disabled the scope is detached (``open_scope`` keeps it off the
    stack), so this cannot tell a probe from an editor drop. It answers True while
    constructing in that case: with the checker off, the conservative answer is the
    safe one.
    """
    if not LibraryRegistry.is_constructing_node():
        return False
    if not STRICT_MODE.enabled:
        return True
    return STRICT_MODE.current_scope() is not None


# Result types that should NOT trigger a flush request.
#
# Add result types to this set if they should never trigger a flush (typically because they ARE
# the flush operation itself, or other internal operations that don't modify workflow state).
RESULT_TYPES_THAT_SKIP_FLUSH = {}


def _running_loop() -> asyncio.AbstractEventLoop | None:
    """Return the currently running event loop, or None if not inside one."""
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


class ResultContext(TypedDict, total=False):
    response_topic: str | None
    request_id: str | None


class EventManager(EngineScoped):
    def __init__(self, *, engine: Engine | None = None) -> None:
        super().__init__(engine)
        # Dictionary to store the SPECIFIC manager for each request type
        self._request_type_to_manager: dict[type[RequestPayload], Callable] = defaultdict(list)  # pyright: ignore[reportAttributeAccessIssue]
        # Dictionary to store ALL SUBSCRIBERS to app events.
        self._app_event_listeners: dict[type[AppPayload], set[Callable]] = {}
        # Dictionary to store ALL SUBSCRIBERS to execution events (the live feed of
        # ExecutionPayloads emitted during a run, e.g. AgentStreamEvent). Lets a node
        # tap the feed while it runs and react (e.g. stream tokens to a parameter).
        self._execution_event_listeners: dict[type[ExecutionPayload], set[Callable]] = {}
        # put_event/aput_event dispatch this feed on whatever thread emitted the event
        # (often a worker thread), while subscribe/unsubscribe happen on the run's
        # thread, so guard the dict and snapshot the target set before iterating.
        self._execution_event_listeners_lock = threading.Lock()
        # Event queue for publishing events
        self._event_queue: asyncio.Queue | None = None
        # Keep track of which thread the event loop runs on
        self._loop_thread_id: int | None = None
        # Keep a reference to the event loop for thread-safe operations
        self._event_loop: asyncio.AbstractEventLoop | None = None
        # Per-event reference counting for event suppression
        self._event_suppression_counts: dict[type, int] = {}
        # Worker-to-orchestrator forwarding state. Inert until
        # configure_worker_forwarding() is called at worker startup.
        self._worker_forwarding_enabled: bool = False
        self._worker_request_client: RequestClient | None = None
        self._orchestrator_request_topic: str | None = None
        self._worker_response_topic: str | None = None
        self._websocket_event_loop: asyncio.AbstractEventLoop | None = None
        self._forward_timeout_ms: int | None = None
        # Node-execution refcount. Incremented on worker_node_execution_scope entry,
        # decremented on exit. Plain instance state guarded by a lock so any thread
        # -- including threads spawned inside third-party libraries (diffusers,
        # transformers, etc.) during node execution -- can observe it via
        # in_node_execution(). ContextVar was tried first and lost the flag when
        # library-internal ThreadPoolExecutors ran node-emitted requests.
        self._node_execution_depth: int = 0
        self._node_execution_lock = threading.Lock()
        # Pre-dispatch hook chain consulted before every request callback. Each
        # hook returns None (fall through) or a ResultPayload (short-circuit the
        # dispatcher). Lets PermissionManager enforce policy without instrumenting
        # every manager.
        self._pre_dispatch_hooks: list[Callable[[RequestPayload, ResultContext], ResultPayload | None]] = []
        # handle_request runs on arbitrary threads, so guard the list and snapshot
        # it before iteration.
        self._pre_dispatch_hooks_lock = threading.Lock()
        # Post-dispatch hooks, keyed by exact request type. Notification-only: they run
        # after the result exists and cannot change it. Lists rather than sets because a
        # callback need not be hashable -- a callable dataclass (RemoteHandler) has
        # __hash__ set to None and would raise on set.add.
        self._post_dispatch_hooks: dict[type[RequestPayload], list[PostDispatchHook]] = {}
        self._post_dispatch_hooks_lock = threading.Lock()
        # Strong references to in-flight hook tasks; asyncio only holds weak ones, so a
        # task that is not retained here can be garbage collected mid-run (also RUF006).
        self._inflight_post_dispatch_hook_tasks: set[asyncio.Task] = set()
        # Thread-local flags: if a hook re-enters an engine operation on this
        # thread, the corresponding chain is skipped so the hook can't keep
        # re-triggering itself into unbounded recursion. `active` guards the
        # pre-dispatch chain; `authorizing` guards the authorization chain.
        self._hook_evaluation = threading.local()
        # Authorization checkpoint hooks. The engine calls
        # evaluate_authorization_checkpoint at privileged operations (library
        # load, node instantiation, ...); a hook returns a CheckpointDenial to
        # block or None to allow. Separate from pre-dispatch hooks because a
        # checkpoint carries a resolved domain subject rather than a raw request.
        # The engine itself registers nothing here; the app installs the policy.
        self._authorization_hooks: list[Callable[[AuthorizationCheckpoint], CheckpointDenial | None]] = []
        self._authorization_hooks_lock = threading.Lock()

    @property
    def event_queue(self) -> asyncio.Queue:
        if self._event_queue is None:
            msg = "Event queue has not been initialized. Please call 'initialize_queue' with an asyncio.Queue instance before accessing the event queue."
            raise ValueError(msg)
        return self._event_queue

    @property
    def event_loop(self) -> asyncio.AbstractEventLoop | None:
        """The event loop that owns request handling, or None before the queue is initialized.

        In-process callers running on another thread (e.g. the bundled MCP server) use this to
        dispatch coroutines onto the engine loop via asyncio.run_coroutine_threadsafe.
        """
        return self._event_loop

    def should_suppress_event(self, event: BaseEvent | ProgressEvent) -> bool:
        """Check if events should be suppressed from being sent to websockets.

        This method checks both the wrapper event type and the payload type for wrapped events.
        For example, if InvolvedNodesEvent is in the suppression set, an ExecutionGriptapeNodeEvent
        that wraps an InvolvedNodesEvent will be suppressed.
        """
        event_type = type(event)

        # Check wrapper type first
        if self._event_suppression_counts.get(event_type, 0) > 0:
            return True

        # For wrapped events (like ExecutionGriptapeNodeEvent), also check the payload type
        wrapped_event = getattr(event, "wrapped_event", None)
        if wrapped_event is not None:
            payload = getattr(wrapped_event, "payload", None)
            if payload is not None:
                payload_type = type(payload)
                if self._event_suppression_counts.get(payload_type, 0) > 0:
                    return True

        return False

    def clear_event_suppression(self) -> None:
        """Clear all event suppression counts."""
        self._event_suppression_counts.clear()

    def initialize_queue(self, queue: asyncio.Queue | None = None) -> None:
        """Set the event queue for this manager.

        Args:
            queue: The asyncio.Queue to use for events, or None to clear
        """
        if queue is not None:
            self._event_queue = queue
            # Track which thread the event loop is running on and store loop reference
            try:
                self._event_loop = asyncio.get_running_loop()
                self._loop_thread_id = threading.get_ident()
            except RuntimeError:
                self._event_loop = None
                self._loop_thread_id = None
        else:
            try:
                self._event_queue = asyncio.Queue()
                self._event_loop = asyncio.get_running_loop()
                self._loop_thread_id = threading.get_ident()
            except RuntimeError:
                # Defer queue creation until we're in an event loop
                self._event_queue = None
                self._event_loop = None
                self._loop_thread_id = None

    def _is_cross_thread_call(self) -> bool:
        """Check if the current call is from a different thread than the event loop.

        Returns:
            True if we're on a different thread and need thread-safe operations
        """
        current_thread_id = threading.get_ident()
        return (
            self._loop_thread_id is not None
            and current_thread_id != self._loop_thread_id
            and self._event_loop is not None
        )

    def put_event(self, event: Any) -> None:
        """Put event into async queue from sync context (non-blocking).

        Automatically detects if we're in a different thread and uses thread-safe operations.

        Args:
            event: The event to publish to the queue
        """
        if self._event_queue is None:
            return

        if self._is_cross_thread_call() and self._event_loop is not None:
            # We're in a different thread from the event loop, use thread-safe method
            # _is_cross_thread_call() guarantees _event_loop is not None
            self._event_loop.call_soon_threadsafe(self._event_queue.put_nowait, event)
        else:
            # We're on the same thread as the event loop or no loop thread tracked, use direct method
            self._event_queue.put_nowait(event)

        # Dispatch after enqueuing so a callback that re-enters put_event (e.g. writing a
        # streamed token to a parameter) enqueues its own events *after* the triggering
        # event, preserving source order on the queue.
        self._dispatch_to_execution_listeners(event)

    async def aput_event(self, event: Any) -> None:
        """Put event into async queue from async context.

        Automatically detects if we're in a different thread and uses thread-safe operations.

        Args:
            event: The event to publish to the queue
        """
        if self._event_queue is None:
            return

        if self._is_cross_thread_call() and self._event_loop is not None:
            # We're in a different thread from the event loop, use thread-safe method
            # _is_cross_thread_call() guarantees _event_loop is not None
            self._event_loop.call_soon_threadsafe(self._event_queue.put_nowait, event)
        else:
            # We're on the same thread as the event loop or no loop thread tracked, use async method
            await self._event_queue.put(event)

        # Dispatch after enqueuing so a re-entrant emission from a callback lands on the
        # queue after its triggering event (see put_event).
        self._dispatch_to_execution_listeners(event)

    def add_pre_dispatch_hook(
        self,
        hook: Callable[[RequestPayload, ResultContext], ResultPayload | None],
    ) -> None:
        """Register a pre-dispatch hook.

        Hooks run in registration order before the request's manager callback.
        Returning a ResultPayload short-circuits the dispatcher with that result;
        returning None lets dispatch continue.

        Hooks should be cheap and sync. A hook that re-enters `handle_request`
        (directly or transitively) is bypassed on the re-entrant call rather
        than recursing, but subscribing to AppPayload events for state is still
        preferred. Registering the same hook twice is a no-op.
        """
        with self._pre_dispatch_hooks_lock:
            if hook not in self._pre_dispatch_hooks:
                self._pre_dispatch_hooks.append(hook)

    def remove_pre_dispatch_hook(
        self,
        hook: Callable[[RequestPayload, ResultContext], ResultPayload | None],
    ) -> None:
        with self._pre_dispatch_hooks_lock:
            try:
                self._pre_dispatch_hooks.remove(hook)
            except ValueError:
                return

    def _run_pre_dispatch_hooks(
        self,
        request: RequestPayload,
        context: ResultContext,
    ) -> ResultPayload | None:
        # Bypass the chain when a hook is already running on this thread. A hook
        # that re-enters handle_request would otherwise re-trigger itself and
        # recurse without bound.
        if getattr(self._hook_evaluation, "active", False):
            return None

        # Snapshot under the lock so concurrent add/remove on another thread
        # cannot mutate the list mid-iteration.
        with self._pre_dispatch_hooks_lock:
            hooks = list(self._pre_dispatch_hooks)

        if not hooks:
            return None

        self._hook_evaluation.active = True
        try:
            for hook in hooks:
                try:
                    short_circuit = hook(request, context)
                except Exception as exc:
                    # Fail closed: the chain is an enforcement boundary, so a
                    # hook that errors denies the request. Return a failure
                    # result rather than raising, so the dispatcher still
                    # delivers an EventResultFailure to the caller instead of
                    # leaving its response future to hang.
                    msg = (
                        f"Attempted to evaluate pre-dispatch hooks for request "
                        f"'{type(request).__name__}'. Failed because hook "
                        f"'{getattr(hook, '__name__', hook)}' raised {type(exc).__name__}: {exc}"
                    )
                    logging.getLogger("griptape_nodes").exception(msg)
                    return GenericResultFailure(exception=exc, result_details=msg)
                if short_circuit is not None:
                    return short_circuit
            return None
        finally:
            self._hook_evaluation.active = False

    def add_post_dispatch_hook(self, request_type: type[RP], callback: PostDispatchHook) -> None:
        """Register a callback to run after `request_type`'s handler has produced a result.

        The callback receives `(request, result)`. Whenever the engine has a live event
        loop it is scheduled as a detached task, so it does not delay the result reaching
        the caller and may run as long as it needs. On paths with no live engine loop --
        CLI commands, bootstrap workflow runs, worker threads -- there is nothing to
        detach onto, so the hook runs inline and *does* block the dispatching caller
        until it finishes. Sync and async callbacks are both accepted. Registering the
        same callback object twice for the same request type is a no-op.

        The callback is notification-only -- it cannot alter the result or fail the
        operation, because the result has already been returned. Specifically:

        - It fires for both success and failure results, including when the handler
          raised (in that case the payload is an equivalent `GenericResultFailure`, not
          the identical object the client received).
        - `request` and `result` must be treated as read-only. The same `request` object
          is referenced by the result event still queued for the client, so mutating it
          corrupts what the client sees.
        - Fields marked `omit_from_result` are already `None` on the request the callback
          sees. That scrub exists to keep sensitive and bulky values out of results, so
          it is deliberately not undone for hooks.
        - Matching is on the exact request type; subclasses of `request_type` do not
          fire it.
        - There is no ordering guarantee relative to the result reaching the client, nor
          between separate invocations of the same hook.
        - Hooks run regardless of `request.broadcast_result` or event suppression.
        - A raising callback is logged and ignored; it does not stop sibling callbacks.
        - Callbacks must not issue engine requests. Any hook already running further up
          the same chain is skipped rather than recursing, but requests from a hook also
          perturb operation-depth tracking for concurrent requests.

        Async callbacks run on the engine event loop (or, on the inline path, a transient
        loop that may itself be on another thread) and must not block it. Sync callbacks are handed to a
        worker thread, so blocking in one cannot stall the loop -- but that thread comes
        from the loop's default executor, which is shared with every other `to_thread`
        caller in the engine. Because hook tasks are unbounded, a slow sync hook firing on
        a high-frequency request type can occupy the pool and make unrelated engine work
        that also needs a thread queue behind it. Keep sync callbacks quick.
        """
        with self._post_dispatch_hooks_lock:
            hooks = self._post_dispatch_hooks.setdefault(request_type, [])
            # Identity, not `in`: `in` runs `__eq__`, and a callable `@dataclass` compares
            # by field value, so two separate libraries registering field-equal hook
            # objects would collapse into one silently registered hook.
            if not any(hook is callback for hook in hooks):
                hooks.append(callback)

    def remove_post_dispatch_hook(self, request_type: type[RP], callback: PostDispatchHook) -> None:
        """Unregister a callback previously registered with `add_post_dispatch_hook`.

        Removing a callback that was never registered is a no-op. Because hooks run
        detached, an already-scheduled invocation still runs to completion -- callbacks
        must tolerate firing once after removal.

        Only the object that was registered is removed. `list.remove` would match by
        `__eq__` and could unregister a different, field-equal hook belonging to another
        library.
        """
        with self._post_dispatch_hooks_lock:
            hooks = self._post_dispatch_hooks.get(request_type)
            if hooks is None:
                return
            remaining = [hook for hook in hooks if hook is not callback]
            if len(remaining) == len(hooks):
                return
            if not remaining:
                del self._post_dispatch_hooks[request_type]
                return
            self._post_dispatch_hooks[request_type] = remaining

    def _fire_post_dispatch_hooks(self, request: RequestPayload, result: ResultPayload) -> None:
        """Schedule every hook registered for this request's exact type."""
        request_type = type(request)

        # Snapshot under the lock so a concurrent add/remove on another thread cannot
        # mutate the list mid-iteration.
        with self._post_dispatch_hooks_lock:
            hooks = list(self._post_dispatch_hooks.get(request_type, ()))

        if not hooks:
            return

        active = _active_post_dispatch_hooks.get()
        for callback in hooks:
            # Identity, not `(request_type, callback) in active`: membership compares with
            # `__eq__`, and a callable `@dataclass` compares by field value, so a hook
            # running up-chain would suppress a *different* field-equal hook belonging to
            # another library. Same reason `add_post_dispatch_hook` dedupes by identity.
            if any(active_type is request_type and active_cb is callback for active_type, active_cb in active):
                logging.getLogger("griptape_nodes").debug(
                    "Skipping post-dispatch hook '%s' for %s: it is already running further up this chain.",
                    getattr(callback, "__name__", callback),
                    request_type.__name__,
                )
                continue
            self._schedule_post_dispatch_hook(request_type, callback, request, result, active)

    def _fire_post_dispatch_hooks_for_handler_exception(self, request: RequestPayload, exception: Exception) -> None:
        """Notify hooks that no result event will be built for this request.

        Neither dispatch method catches handler exceptions -- they propagate to
        `Engine.handle_request`, which logs and returns a synthesized failure. Without
        this the most interesting failure mode would be invisible to hooks. The payload is
        equivalent to the one the client receives, not the identical object, which is why
        `add_post_dispatch_hook` says so explicitly.

        The caller's `try` covers the parameter-change flush as well as the handler, so a
        handler that returned a result and then had `_flush_tracked_parameter_changes`
        raise also lands here. That is deliberate: the exception escapes either way, the
        client gets a synthesized failure either way, and hooks reporting success for a
        request the client saw fail would be worse than the coarser attribution.
        """
        result_details = f"Unhandled exception while processing {type(request).__name__}: {exception}"
        # `_handle_request_core` scrubs the request on its way to building the result event,
        # but a raising handler never gets there. Without this, hooks would be the one place
        # an omitted field still surfaces -- and those fields are omitted precisely because
        # they are sensitive or bulky.
        self._scrub_omitted_request_fields(request)
        self._fire_post_dispatch_hooks(
            request, GenericResultFailure(exception=exception, result_details=result_details)
        )

    def _scrub_omitted_request_fields(self, request: RequestPayload) -> None:
        """Null out the request fields marked `omit_from_result`, in place.

        Mutates the request rather than copying it: the result event carries this same
        object, so the scrub has to be visible through every reference to it.
        """
        for field in fields(request):
            if field.metadata.get("omit_from_result", False):
                setattr(request, field.name, None)

    def _schedule_post_dispatch_hook(
        self,
        request_type: type[RequestPayload],
        callback: PostDispatchHook,
        request: RequestPayload,
        result: ResultPayload,
        inherited_chain: tuple[tuple[type[RequestPayload], Any], ...],
    ) -> None:
        """Hand one hook to the engine loop, or run it inline if there is no live loop.

        Always targets `self._event_loop` rather than the running loop. The sync
        `handle_request` drives async handlers on a transient `ThreadRunner` side loop
        that stops as soon as the handler returns, so a task detached onto the *running*
        loop can silently never run (see the ThreadRunner regression test in
        tests/unit/app/test_app_worker.py).

        `is_running()` is as load-bearing as `is_closed()`: `create_task` on an open but
        undriven loop succeeds, returns a pending task, and never executes it. Executors
        re-`initialize_queue` per run and nothing ever clears `_event_loop`, so a stale
        loop from a finished `asyncio.run` is a real possibility here.
        """
        loop = self._event_loop
        if loop is not None and not loop.is_closed() and loop.is_running():
            try:
                # call_soon_threadsafe is legal from the loop's own thread as well as
                # from any other, so this is one path instead of two. The task itself is
                # created on the loop thread by _spawn_post_dispatch_hook_task.
                loop.call_soon_threadsafe(
                    self._spawn_post_dispatch_hook_task,
                    loop,
                    request_type,
                    callback,
                    request,
                    result,
                    inherited_chain,
                )
            except RuntimeError:
                # Lost the race with loop shutdown between the check and the call; fall
                # through and run inline rather than dropping the notification.
                pass
            else:
                return

        self._run_post_dispatch_hook_inline(request_type, callback, request, result, inherited_chain)

    def _spawn_post_dispatch_hook_task(  # noqa: PLR0913, PLR0917
        self,
        loop: asyncio.AbstractEventLoop,
        request_type: type[RequestPayload],
        callback: PostDispatchHook,
        request: RequestPayload,
        result: ResultPayload,
        inherited_chain: tuple[tuple[type[RequestPayload], Any], ...],
    ) -> None:
        """Create the detached hook task. Runs on `loop`'s own thread."""
        task = loop.create_task(
            self._run_post_dispatch_hook(request_type, callback, request, result, inherited_chain),
            context=contextvars.Context(),
        )
        self._inflight_post_dispatch_hook_tasks.add(task)
        task.add_done_callback(self._on_post_dispatch_hook_done)

        inflight = len(self._inflight_post_dispatch_hook_tasks)
        if inflight > POST_DISPATCH_HOOK_INFLIGHT_WARNING_THRESHOLD:
            logging.getLogger("griptape_nodes").warning(
                "%d post-dispatch hooks are still running. Hooks are never dropped, so a hook "
                "that runs slower than requests arrive will keep accumulating. Most recent: '%s' for %s.",
                inflight,
                getattr(callback, "__name__", callback),
                request_type.__name__,
            )

    def _on_post_dispatch_hook_done(self, task: asyncio.Task) -> None:
        """Release the task reference and surface anything the wrapper did not catch."""
        self._inflight_post_dispatch_hook_tasks.discard(task)
        if task.cancelled():
            return
        exception = task.exception()
        if exception is not None:
            logging.getLogger("griptape_nodes").error("Post-dispatch hook task failed.", exc_info=exception)

    async def _run_post_dispatch_hook(
        self,
        request_type: type[RequestPayload],
        callback: PostDispatchHook,
        request: RequestPayload,
        result: ResultPayload,
        inherited_chain: tuple[tuple[type[RequestPayload], Any], ...],
    ) -> None:
        """Invoke one hook, isolated from the context that dispatched the request.

        The task is created with a *fresh* context rather than a copy of the caller's, so
        the hook cannot inherit the dispatching task's strict-mode scope stack. That
        stack holds mutable scope objects: a hook that reported a violation into an
        inherited RUNTIME_EXECUTE scope could append to `scope.violations` after (or
        while) the node's own scope is being evaluated, and on a worker an ERROR-severity
        violation promotes a node's success to a failure. A library's telemetry hook must
        not be able to fail a user's node. The fresh context also keeps
        `current_request_type()` from reporting the dispatching request inside the hook.

        A fresh context drops the engine binding too, so it is re-established here from
        the manager's own engine reference. It also drops the re-entrancy marker, which is
        why the chain is passed in explicitly rather than read from the context here.
        """
        _active_post_dispatch_hooks.set((*inherited_chain, (request_type, callback)))

        with engine_scope(self.engine):
            try:
                if _is_async_callable(callback):
                    await callback(request, result)  # type: ignore[misc]
                else:
                    # Sync callbacks go to a thread: they would otherwise run inline on
                    # the engine loop, where blocking stalls the event queue and every
                    # in-flight request.
                    await to_thread(callback, request, result)
            except Exception:
                # Fail open. The result has already been returned, so there is nothing
                # to fail; the only correct response is to log and move on.
                logging.getLogger("griptape_nodes").exception(
                    "Post-dispatch hook '%s' for %s raised. The request result is unaffected.",
                    getattr(callback, "__name__", callback),
                    request_type.__name__,
                )

    def _run_post_dispatch_hook_inline(
        self,
        request_type: type[RequestPayload],
        callback: PostDispatchHook,
        request: RequestPayload,
        result: ResultPayload,
        inherited_chain: tuple[tuple[type[RequestPayload], Any], ...],
    ) -> None:
        """Run a hook synchronously, blocking the caller's thread until it finishes.

        Blocking the caller is the guarantee; running *on* the caller's thread is not. A
        sync callback always lands in a threadpool worker via `to_thread`, and when this
        is reached from inside a running loop the coroutine is driven by a `ThreadRunner`
        on a thread of its own.

        The fallback for paths with no live engine loop -- CLI commands, bootstrap
        workflow runs, worker threads. Blocking is the lesser evil: no editor is waiting
        on those paths, and dropping the hook would make the feature "fires, except
        sometimes."
        """
        coro = self._run_post_dispatch_hook(request_type, callback, request, result, inherited_chain)
        try:
            if _running_loop() is not None:
                # Both branches must start the hook from an empty context, for the
                # strict-mode reason in `_run_post_dispatch_hook`. Running on another
                # thread is not enough on its own: ThreadRunner.run hands the coroutine
                # over with `run_coroutine_threadsafe`, which has no `context=` parameter
                # and whose underlying `call_soon_threadsafe` captures
                # `copy_context()` on *this* thread. Entering a fresh context first is
                # what makes the copy it takes an empty one.
                with ThreadRunner() as runner:
                    contextvars.Context().run(runner.run, coro)
            else:
                contextvars.Context().run(asyncio.run, coro)
        except Exception:
            # _run_post_dispatch_hook swallows callback errors, so reaching here means the
            # scheduling machinery itself failed. Still fail open.
            logging.getLogger("griptape_nodes").exception(
                "Failed to run post-dispatch hook '%s' for %s inline.",
                getattr(callback, "__name__", callback),
                request_type.__name__,
            )

    def add_authorization_hook(
        self,
        hook: Callable[[AuthorizationCheckpoint], CheckpointDenial | None],
    ) -> None:
        """Register an authorization-checkpoint hook.

        The engine calls `evaluate_authorization_checkpoint` at privileged
        operations; each registered hook returns a `CheckpointDenial` to block the
        operation or `None` to allow it. Hooks run in registration order and the
        first denial wins. The engine registers nothing itself -- this is how the
        app installs license policy without the engine depending on it.
        Registering the same hook twice is a no-op.
        """
        with self._authorization_hooks_lock:
            if hook not in self._authorization_hooks:
                self._authorization_hooks.append(hook)

    def remove_authorization_hook(
        self,
        hook: Callable[[AuthorizationCheckpoint], CheckpointDenial | None],
    ) -> None:
        with self._authorization_hooks_lock:
            try:
                self._authorization_hooks.remove(hook)
            except ValueError:
                return

    def evaluate_authorization_checkpoint(self, checkpoint: AuthorizationCheckpoint) -> CheckpointDenial | None:
        """Ask registered hooks whether a resolved operation is permitted.

        Returns the first hook's `CheckpointDenial`, or `None` when every hook
        allows (including when none are registered, so an engine with no policy
        installed runs unrestricted). Fails closed: a hook that raises is treated
        as a denial rather than letting the exception escape into the calling
        operation, mirroring the pre-dispatch chain. A re-entrant call on the same
        thread (a hook that triggers another guarded operation) bypasses the chain
        and allows, so the chain cannot recurse without bound.
        """
        # Bypass the chain when a hook is already evaluating on this thread. A
        # hook that re-enters an engine operation guarded by a checkpoint would
        # otherwise re-trigger itself and recurse without bound. Mirrors the
        # pre-dispatch chain's recursion guard. Returning None allows the nested
        # operation unconditionally -- the bypass is coarse and permits a nested
        # checkpoint with a different subject too -- which is acceptable because
        # the policy code itself triggered it; the alternative is the recursion.
        if getattr(self._hook_evaluation, "authorizing", False):
            return None

        with self._authorization_hooks_lock:
            hooks = list(self._authorization_hooks)

        if not hooks:
            return None

        self._hook_evaluation.authorizing = True
        try:
            for hook in hooks:
                try:
                    denial = hook(checkpoint)
                except Exception as exc:
                    logging.getLogger("griptape_nodes").exception(
                        "Authorization hook '%s' raised on checkpoint '%s'; denying.",
                        getattr(hook, "__name__", hook),
                        checkpoint.action,
                    )
                    return CheckpointDenial(
                        failures=(
                            CheckpointFailure(
                                detail=f"Authorization could not be evaluated: {type(exc).__name__}: {exc}"
                            ),
                        )
                    )
                if denial is not None:
                    return denial
            return None
        finally:
            self._hook_evaluation.authorizing = False

    def assign_manager_to_request_type(
        self,
        request_type: type[RP],
        callback: Callable[[RP], ResultPayload] | Callable[[RP], Awaitable[ResultPayload]],
    ) -> None:
        """Assign a manager to handle a request.

        Args:
            request_type: The type of request to assign the manager to
            callback: Function to be called when event occurs
        """
        existing_manager = self._request_type_to_manager.get(request_type)
        if existing_manager is not None:
            msg = f"Attempted to assign an event of type {request_type} to manager {callback.__name__}, but that request is already assigned to manager {existing_manager.__name__}."
            raise ValueError(msg)
        self._request_type_to_manager[request_type] = callback

    def configure_worker_forwarding(
        self,
        *,
        request_client: RequestClient,
        orchestrator_request_topic: str,
        worker_response_topic: str,
        websocket_event_loop: asyncio.AbstractEventLoop,
        timeout_ms: int | None = None,
    ) -> None:
        """Enable worker -> orchestrator forwarding for requests originated from node execution.

        Called once at worker startup after the RequestClient is constructed and topics
        are subscribed. Inert on the orchestrator (never called there).

        websocket_event_loop is the loop that owns the Client/RequestClient (the daemon
        thread's loop). All RequestClient primitives -- its asyncio.Lock, the pending-
        request Future, and the _try_match filter that claims responses -- are bound to
        that loop. Forwarding calls must be dispatched there via run_coroutine_threadsafe;
        awaiting RequestClient methods directly from the main loop or a ThreadRunner loop
        causes cross-loop contention that stalls for seconds per request.
        """
        self._worker_request_client = request_client
        self._orchestrator_request_topic = orchestrator_request_topic
        self._worker_response_topic = worker_response_topic
        self._websocket_event_loop = websocket_event_loop
        self._forward_timeout_ms = timeout_ms
        self._worker_forwarding_enabled = True

    @contextmanager
    def worker_node_execution_scope(self) -> Iterator[None]:
        """Mark this worker as actively executing a node.

        Increments a thread-safe refcount on entry and decrements on exit.
        While the refcount is > 0, in_node_execution() returns True; the
        worker-side RemoteHandler consults that flag to decide whether to
        forward a request to the orchestrator or delegate to the original
        local handler.

        The refcount is plain instance state guarded by a lock, so any
        thread -- including threads spawned internally by third-party
        libraries (diffusers, transformers) during node execution --
        observes the same value. A ContextVar was tried first and lost
        the flag when library-internal ThreadPoolExecutors emitted
        requests.

        Opened by NodeManager._hydrate_and_run_node around node.aprocess()
        inside the ExecuteNodeRequest handler. Bootstrap paths and
        AppInitializationComplete fan-out are not wrapped and therefore
        never forward.
        """
        with self._node_execution_lock:
            self._node_execution_depth += 1
        try:
            yield
        finally:
            with self._node_execution_lock:
                self._node_execution_depth -= 1

    def in_node_execution(self) -> bool:
        """Return True when this worker is currently inside a node-execution scope."""
        with self._node_execution_lock:
            return self._node_execution_depth > 0

    def _report_reentrant_bus_in_init(self, request: RequestPayload) -> None:
        """Detect the reentrant-bus-in-init rule.

        A node class that issues an event-bus request from its __init__
        deadlocks the worker's schema probe, which calls __init__ on
        the worker thread during library load. LibraryRegistry sets a
        ContextVar around create_node so every __init__ body in the
        hierarchy is covered.

        The condition lives in ``reentrant_bus_in_init_would_report`` rather
        than inline, because engine components that legitimately read state
        from a node ``__init__`` consult the same predicate to decide whether
        to defer. One owner, so the two answers cannot disagree.
        """
        if not reentrant_bus_in_init_would_report():
            return
        rule = RULES["reentrant-bus-in-init"]
        # Subject attribution lives on the violation's ``subject`` field,
        # set from the surrounding strict-mode scope (class name under
        # LOAD_PROBE, instance name under RUNTIME_EXECUTE). The message
        # only needs the request type.
        STRICT_MODE.report(
            rule_id=rule.rule_id,
            message=rule.render(request_type=type(request).__name__),
        )

    def get_manager_for_request_type(self, request_type: type[RP]) -> Callable | None:
        """Return the currently-registered handler callback for a request type, or None."""
        return self._request_type_to_manager.get(request_type)

    async def forward_to_orchestrator(
        self,
        request: RP,
        result_context: ResultContext,
    ) -> EventResultSuccess | EventResultFailure:
        """Forward a worker-originated request to the orchestrator and structure its reply.

        Wraps the request in an EventRequest, awaits the orchestrator's EventResult
        payload, and reconstructs it as an EventResultSuccess/EventResultFailure whose
        shape matches the locally-dispatched path.

        The RequestClient send/track/await happens on the websocket event loop
        (configured via configure_worker_forwarding) so that its asyncio.Lock and
        the pending-request Future live on the same loop as the _try_match filter
        that resolves them. Awaiting those primitives from any other loop causes
        cross-loop contention that stalls for seconds.
        """
        if (
            self._worker_request_client is None
            or self._orchestrator_request_topic is None
            or self._worker_response_topic is None
            or self._websocket_event_loop is None
        ):
            msg = "Worker forwarding is enabled but not fully configured."
            raise RuntimeError(msg)

        event_request: EventRequest = EventRequest(request=request)

        response_future = asyncio.run_coroutine_threadsafe(
            self._worker_request_client.request_to_orchestrator(
                event_request=event_request,
                orchestrator_request_topic=self._orchestrator_request_topic,
                worker_response_topic=self._worker_response_topic,
                timeout_ms=self._forward_timeout_ms,
            ),
            self._websocket_event_loop,
        )
        response_payload = await asyncio.wrap_future(response_future)

        event_type = response_payload.get("event_type", "")
        result_type_name = response_payload.get("result_type")
        result_data = response_payload.get("result", {})

        if not result_type_name:
            msg = f"Forwarded response for {type(request).__name__} missing 'result_type'."
            raise RuntimeError(msg)

        resolved_result_type = PayloadRegistry.get_type(result_type_name)
        if resolved_result_type is None:
            msg = f"Forwarded response 'result_type' is not registered: {result_type_name}"
            raise RuntimeError(msg)

        result_payload = cast("ResultPayload", converter.structure(result_data, resolved_result_type))

        event_cls: type[EventResultSuccess | EventResultFailure]
        event_cls = EventResultSuccess if event_type == "EventResultSuccess" else EventResultFailure

        return event_cls(
            request=request,
            request_id=result_context.get("request_id"),
            result=result_payload,
            response_topic=result_context.get("response_topic"),
        )

    def remove_manager_from_request_type(self, request_type: type[RP]) -> None:
        """Unsubscribe the manager from the request of a specific type.

        Args:
            request_type: The type of request to unsubscribe from
        """
        if request_type in self._request_type_to_manager:
            del self._request_type_to_manager[request_type]

    def _override_result_log_level(self, result: ResultPayload, level: int) -> None:
        """Override the log level on all result details.

        Args:
            result: The result payload to modify
            level: The new log level to set
        """
        if isinstance(result.result_details, ResultDetails):
            for detail in result.result_details.result_details:
                detail.level = level

    def _log_result_details(self, result: ResultPayload) -> None:
        """Log the result details at their specified levels.

        Strict-mode violations are skipped here because
        ``StrictModeReporter.report`` has already logged them at
        detection time with the scope's ``node=... library=...``
        prefix, which is more informative than the bare message
        repeated here. Without the skip every violation would log
        twice -- once from the reporter and once from this loop.

        Args:
            result: The result payload containing details to log
        """
        if isinstance(result.result_details, ResultDetails):
            logger = logging.getLogger("griptape_nodes")
            for detail in result.result_details.result_details:
                if isinstance(detail, StrictModeViolationDetail):
                    continue
                logger.log(detail.level, detail.message)

    def _handle_request_core(
        self,
        request: RP,
        callback_result: ResultPayload,
        *,
        context: ResultContext,
    ) -> EventResultSuccess | EventResultFailure:
        """Core logic for handling requests, shared between sync and async methods."""
        operation_depth_mgr = self.engine.operation_depth_manager
        workflow_mgr = self.engine.workflow_manager

        with operation_depth_mgr as depth_manager:
            # Now see if the WorkflowManager was asking us to squelch altered_workflow_state commands
            # This prevents situations like loading a workflow (which naturally alters the workflow state)
            # from coming in and immediately being flagged as being dirty.
            if workflow_mgr.should_squelch_workflow_altered():
                callback_result.altered_workflow_state = False

            # Override failure log level if requested
            if callback_result.failed() and request.failure_log_level is not None:
                self._override_result_log_level(callback_result, request.failure_log_level)

            # Log result details (after potential level override)
            self._log_result_details(callback_result)

            retained_mode_str = None
            # If request_id exists, that means it's a direct request from the GUI (not internal), and should be echoed by retained mode.
            if depth_manager.is_top_level() and context.get("request_id") is not None:
                retained_mode_str = depth_manager.request_retained_mode_translation(request)

            # Some requests have fields marked as "omit_from_result" which should be removed from the request
            self._scrub_omitted_request_fields(request)
            if callback_result.succeeded():
                result_event = EventResultSuccess(
                    request=request,
                    request_id=context.get("request_id"),
                    result=callback_result,
                    retained_mode=retained_mode_str,
                    response_topic=context.get("response_topic"),
                )
            else:
                result_event = EventResultFailure(
                    request=request,
                    request_id=context.get("request_id"),
                    result=callback_result,
                    retained_mode=retained_mode_str,
                    response_topic=context.get("response_topic"),
                )

        # Fired here rather than in the two dispatch methods because every path that
        # produces a result event -- sync, async, and pre-dispatch short-circuit -- runs
        # through this method. Deliberately outside the operation-depth context above, so
        # a hook that does issue a request nests cleanly instead of inflating the depth.
        self._fire_post_dispatch_hooks(request, callback_result)

        return result_event

    async def ahandle_request(
        self,
        request: RP,
        *,
        result_context: ResultContext | None = None,
    ) -> EventResultSuccess | EventResultFailure:
        """Publish an event to the manager assigned to its type.

        Args:
            request: The request to handle
            result_context: The result context containing response_topic and request_id
        """
        operation_depth_mgr = self.engine.operation_depth_manager
        if result_context is None:
            result_context = ResultContext()

        self._report_reentrant_bus_in_init(request)

        # Notify the manager of the event type
        request_type = type(request)
        callback = self._request_type_to_manager.get(request_type)
        if not callback:
            msg = f"No manager found to handle request of type '{request_type.__name__}'."
            raise TypeError(msg)

        # Pre-dispatch hooks (e.g. PermissionManager) may short-circuit before
        # the manager callback runs.
        short_circuit = self._run_pre_dispatch_hooks(request, result_context)
        if short_circuit is not None:
            return self._handle_request_core(
                request,
                short_circuit,
                context=result_context,
            )

        # Expose the dispatching request type to detectors (see current_request_type).
        token = _active_request_type.set(request_type)
        try:
            try:
                # Actually make the handler callback (support both sync and async):
                result_payload: ResultPayload = await call_function(callback, request)

                # Queue flush request for async context (unless result type should skip flush)
                with operation_depth_mgr:
                    if type(result_payload) not in RESULT_TYPES_THAT_SKIP_FLUSH:
                        self._flush_tracked_parameter_changes()
            except Exception as exc:
                self._fire_post_dispatch_hooks_for_handler_exception(request, exc)
                raise

            return self._handle_request_core(
                request,
                cast("ResultPayload", result_payload),
                context=result_context,
            )
        finally:
            _active_request_type.reset(token)

    def handle_request(
        self,
        request: RP,
        *,
        result_context: ResultContext | None = None,
    ) -> EventResultSuccess | EventResultFailure:
        """Publish an event to the manager assigned to its type (sync version).

        Args:
            request: The request to handle
            result_context: The result context containing response_topic and request_id
        """
        operation_depth_mgr = self.engine.operation_depth_manager
        if result_context is None:
            result_context = ResultContext()

        self._report_reentrant_bus_in_init(request)

        # Notify the manager of the event type
        request_type = type(request)
        callback = self._request_type_to_manager.get(request_type)
        if not callback:
            msg = f"No manager found to handle request of type '{request_type.__name__}'."
            raise TypeError(msg)

        # Pre-dispatch hooks (e.g. PermissionManager) may short-circuit before
        # the manager callback runs.
        short_circuit = self._run_pre_dispatch_hooks(request, result_context)
        if short_circuit is not None:
            return self._handle_request_core(
                request,
                short_circuit,
                context=result_context,
            )

        # Expose the dispatching request type to detectors (see current_request_type).
        token = _active_request_type.set(request_type)
        try:
            try:
                result_payload = self._invoke_handler_from_sync(callback, request)

                # Queue flush request for sync context (unless result type should skip flush)
                with operation_depth_mgr:
                    if type(result_payload) not in RESULT_TYPES_THAT_SKIP_FLUSH:
                        self._flush_tracked_parameter_changes()
            except Exception as exc:
                self._fire_post_dispatch_hooks_for_handler_exception(request, exc)
                raise

            return self._handle_request_core(
                request,
                cast("ResultPayload", result_payload),
                context=result_context,
            )
        finally:
            _active_request_type.reset(token)

    def _invoke_handler_from_sync(self, callback: Callable, request: RequestPayload) -> ResultPayload:
        """Call a request handler from sync code, bridging async handlers onto a loop."""
        # Worker-side RemoteHandler callbacks are async but safe to invoke from a
        # running loop: forward_to_orchestrator dispatches onto the WS loop via
        # run_coroutine_threadsafe, which runs on a different thread than the caller's
        # loop, so no primitives are shared and the #4469 deadlock shape does not apply.
        # Hop the callback onto the WS loop here and block the caller's thread on the
        # concurrent.futures.Future so RemoteHandler itself stays a plain async callable.
        #
        # Lazy import: worker_routing imports ResultContext from this module at
        # runtime, so a top-level import here would cycle through event_manager
        # -> worker_routing -> event_manager during module load.
        from griptape_nodes.app.worker_routing import RemoteHandler

        if isinstance(callback, RemoteHandler):
            if _running_loop() is None:
                return asyncio.run(callback(request))
            if self._websocket_event_loop is None:
                msg = (
                    f"Cannot forward '{type(request).__name__}' from a running event loop: "
                    "the websocket event loop is not configured. This indicates a bootstrap order bug."
                )
                raise RuntimeError(msg)
            future = asyncio.run_coroutine_threadsafe(callback(request), self._websocket_event_loop)
            return future.result()

        # Support async callbacks invoked from sync code. If no loop is running
        # (bootstrap, worker threads) asyncio.run drives the coroutine directly.
        # If a loop IS running (pre-#4449 workflow files exec'd from inside the
        # engine loop), dispatch onto a side loop via ThreadRunner. The #4469
        # deadlock shape is specific to callbacks whose coroutines share
        # primitives with the caller's loop; RemoteHandler is the only such case
        # and is handled above via run_coroutine_threadsafe onto the WS loop.
        # For all other async handlers the side-loop path is safe.
        if inspect.iscoroutinefunction(callback):
            if _running_loop() is None:
                return asyncio.run(callback(request))
            with ThreadRunner() as runner:
                return runner.run(callback(request))

        return callback(request)

    def add_listener_to_app_event(
        self, app_event_type: type[AP], callback: Callable[[AP], None] | Callable[[AP], Awaitable[None]]
    ) -> None:
        listener_set = self._app_event_listeners.get(app_event_type)
        if listener_set is None:
            listener_set = set()
            self._app_event_listeners[app_event_type] = listener_set

        listener_set.add(callback)

    def add_listener_to_execution_event(self, execution_event_type: type[EP], callback: Callable[[EP], None]) -> None:
        """Subscribe to a type of execution event on the live event feed.

        Execution events (``ExecutionPayload`` subclasses such as ``AgentStreamEvent``
        and ``AgentToolCallEvent``) are emitted as events flow through
        ``put_event``/``aput_event`` on their way to the UI. This lets a node tap that
        feed while it runs and react in real time -- for example, appending streamed
        agent tokens onto one of its own parameters.

        The callback is invoked synchronously with the payload as each matching event
        is emitted, on whatever thread emitted it. Keep callbacks cheap and non-blocking;
        an exception in a callback is logged and does not interrupt event delivery. Unlike
        ``add_listener_to_app_event``, async callbacks are not supported and are rejected
        here rather than silently dropped at dispatch time.

        Only the exact payload type is matched -- subscribing to a base class such as
        ``ExecutionPayload`` does not receive its subclasses (this mirrors
        ``add_listener_to_app_event``). Execution events reach subscribers even when the
        UI consumer suppresses them (suppression is applied downstream, not here), so
        this feed is deliberately independent of ``should_suppress_event``.

        Most events carry no run identifier, so a subscriber that only wants its own
        run's events should subscribe immediately before it triggers the run and
        unsubscribe as soon as the run returns (see
        ``remove_listener_for_execution_event``). The agent payloads are the exception:
        they carry the ``thread_id`` of their conversation, so a subscriber can filter
        on that instead.
        """
        callback_call = type(callback).__call__
        if inspect.iscoroutinefunction(callback) or inspect.iscoroutinefunction(callback_call):
            msg = (
                f"Attempted to subscribe to execution event '{execution_event_type.__name__}'. "
                f"Failed because callback '{getattr(callback, '__name__', callback)}' is a coroutine "
                f"function; execution-event listeners are invoked synchronously on the emitting "
                f"thread and must be plain (non-async) callables."
            )
            raise TypeError(msg)
        with self._execution_event_listeners_lock:
            listener_set = self._execution_event_listeners.get(execution_event_type)
            if listener_set is None:
                listener_set = set()
                self._execution_event_listeners[execution_event_type] = listener_set
            listener_set.add(callback)

    def remove_listener_for_execution_event(
        self, execution_event_type: type[EP], callback: Callable[[EP], None]
    ) -> None:
        """Unsubscribe a callback previously registered with ``add_listener_to_execution_event``.

        Because dispatch invokes callbacks outside the lock (a callback may re-enter
        ``put_event``), a callback can still fire once more if a concurrent emission on
        another thread already snapshotted the listener set before this call. Callbacks
        must tolerate a late invocation after they have been removed.
        """
        with self._execution_event_listeners_lock:
            listener_set = self._execution_event_listeners.get(execution_event_type)
            if listener_set is not None:
                listener_set.discard(callback)
                if not listener_set:
                    del self._execution_event_listeners[execution_event_type]

    def _dispatch_to_execution_listeners(self, event: Any) -> None:
        """Fan a queued execution event out to any subscribers before it reaches the UI.

        Only ``ExecutionGriptapeNodeEvent``s carry an ``ExecutionPayload``; everything
        else on the queue is ignored here. Dispatch is synchronous and best-effort so a
        misbehaving subscriber never blocks or breaks the event queue.

        Runs on the emitting thread (frequently a worker thread), so the listener set is
        snapshotted under the lock and callbacks are invoked outside it -- holding the
        lock across a callback that re-enters ``put_event`` would deadlock.
        """
        if not isinstance(event, ExecutionGriptapeNodeEvent):
            return
        payload = event.wrapped_event.payload
        with self._execution_event_listeners_lock:
            listener_set = self._execution_event_listeners.get(type(payload))
            if not listener_set:
                return
            callbacks = list(listener_set)
        for callback in callbacks:
            try:
                callback(payload)
            except Exception:
                logging.getLogger("griptape_nodes").exception(
                    "Execution-event listener for %s raised; continuing event delivery.",
                    type(payload).__name__,
                )

    def remove_listener_for_app_event(
        self, app_event_type: type[AP], callback: Callable[[AP], None] | Callable[[AP], Awaitable[None]]
    ) -> None:
        listener_set = self._app_event_listeners[app_event_type]
        listener_set.remove(callback)

    def broadcast_app_event(self, app_event: AP) -> None:
        """Broadcast an app event to all registered listeners (sync version).

        Args:
            app_event: The app event to broadcast
        """
        app_event_type = type(app_event)
        if app_event_type in self._app_event_listeners:
            listener_set = self._app_event_listeners[app_event_type]

            # Support async callbacks for sync method. See the matching comment
            # in handle_request for the ThreadRunner rationale: listeners here
            # are user-supplied callbacks that do not share primitives with the
            # caller's loop, so the side-loop path is safe.
            async def _broadcast_async() -> None:
                async with asyncio.TaskGroup() as tg:
                    for listener_callback in listener_set:
                        tg.create_task(call_function(listener_callback, app_event))

            if _running_loop() is not None:
                with ThreadRunner() as runner:
                    runner.run(_broadcast_async())
            else:
                asyncio.run(_broadcast_async())

    async def abroadcast_app_event(self, app_event: AP) -> None:
        """Broadcast an app event to all registered listeners (async version).

        Args:
            app_event: The app event to broadcast
        """
        app_event_type = type(app_event)
        if app_event_type in self._app_event_listeners:
            listener_set = self._app_event_listeners[app_event_type]

            async with asyncio.TaskGroup() as tg:
                for listener_callback in listener_set:
                    tg.create_task(call_function(listener_callback, app_event))

    def _flush_tracked_parameter_changes(self) -> None:
        obj_manager = self.engine.object_manager
        # Get all flows and their nodes
        nodes = obj_manager.get_filtered_subset(type=BaseNode)
        for node in nodes.values():
            # Only flush if there are actually tracked parameters
            if node._tracked_parameters:
                node.emit_parameter_changes()


class EventSuppressionContext:
    """Context manager to suppress events from being sent to websockets.

    Use this to prevent internal operations (like deserialization/deletion of iteration flows)
    from sending events to the GUI while still allowing the operations to complete normally.

    Uses per-event reference counting to track nested suppression contexts.
    Each event type maintains its own reference count, and is only unsuppressed
    when its count reaches zero.
    """

    events_to_suppress: set[type]

    def __init__(self, manager: EventManager, events_to_suppress: set[type]):
        self.manager = manager
        self.events_to_suppress = events_to_suppress

    def __enter__(self) -> None:
        for event_type in self.events_to_suppress:
            current_count = self.manager._event_suppression_counts.get(event_type, 0)
            self.manager._event_suppression_counts[event_type] = current_count + 1

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        exc_traceback: types.TracebackType | None,
    ) -> None:
        for event_type in self.events_to_suppress:
            current_count = self.manager._event_suppression_counts.get(event_type, 0)
            if current_count <= 1:
                self.manager._event_suppression_counts.pop(event_type, None)
            else:
                self.manager._event_suppression_counts[event_type] = current_count - 1


class EventTranslationContext:
    """Context manager to translate node names in events from packaged to original names.

    Use this to make loop execution events reference the original nodes that the user placed,
    rather than the packaged node copies. This allows the UI to highlight the correct nodes
    during loop execution.
    """

    def __init__(self, manager: EventManager, node_name_mapping: dict[str, str]):
        """Initialize the event translation context.

        Args:
            manager: The EventManager to intercept events from
            node_name_mapping: Dict mapping packaged node names to original node names
        """
        self.manager = manager
        self.node_name_mapping = node_name_mapping
        self.original_put_event: Any = None
        self.original_aput_event: Any = None

    def __enter__(self) -> None:
        """Enter the context and start translating events."""
        self.original_put_event = self.manager.put_event
        self.original_aput_event = self.manager.aput_event
        self.manager.put_event = self._translate_and_put  # type: ignore[method-assign]
        self.manager.aput_event = self._translate_and_aput  # type: ignore[method-assign]

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        exc_traceback: types.TracebackType | None,
    ) -> None:
        """Exit the context and restore original event sending."""
        self.manager.put_event = self.original_put_event  # type: ignore[method-assign]
        self.manager.aput_event = self.original_aput_event  # type: ignore[method-assign]

    def _translate_event(self, event: Any) -> Any:
        """Translate node names in an event.

        Args:
            event: The event to potentially translate

        Returns:
            The translated event, or the original if no translation needed
        """
        # Handle wrapped events (like ExecutionGriptapeNodeEvent)
        wrapped_event = getattr(event, "wrapped_event", None)
        if wrapped_event is not None:
            payload = getattr(wrapped_event, "payload", None)
            if payload is not None:
                translated_payload = self._translate_payload(payload)
                if translated_payload is not payload:
                    # Create a new wrapped event with the translated payload
                    translated_event = self._create_translated_wrapped_event(event, translated_payload)
                    if translated_event is not None:
                        return translated_event

        # Check if event has node_name attribute and needs translation
        if hasattr(event, "node_name"):
            node_name = event.node_name
            if node_name in self.node_name_mapping:
                # Create a copy of the event with the translated node name
                return self._copy_event_with_translated_name(event)

        return event

    def _translate_and_put(self, event: Any) -> None:
        """Translate node names in events and put them in the queue (sync version).

        Args:
            event: The event to potentially translate and send
        """
        translated_event = self._translate_event(event)
        self.original_put_event(translated_event)

    async def _translate_and_aput(self, event: Any) -> None:
        """Translate node names in events and put them in the queue (async version).

        Args:
            event: The event to potentially translate and send
        """
        translated_event = self._translate_event(event)
        await self.original_aput_event(translated_event)

    def _translate_payload(self, payload: Any) -> Any:
        """Translate node names in a payload.

        Handles both single node_name and involved_nodes list.

        Args:
            payload: The payload to translate

        Returns:
            A new payload with translated names, or the original if no translation needed
        """
        # Handle involved_nodes list (e.g., InvolvedNodesEvent)
        involved_nodes = getattr(payload, "involved_nodes", None)
        if involved_nodes is not None and isinstance(involved_nodes, list):
            translated_nodes: list[str] = []
            any_translated = False
            for node_name in involved_nodes:
                if node_name in self.node_name_mapping:
                    translated_nodes.append(self.node_name_mapping[node_name])
                    any_translated = True
                else:
                    translated_nodes.append(node_name)
            # Only create new payload if something was translated
            if any_translated:
                return self._copy_payload_with_translated_involved_nodes(payload, translated_nodes)

        # Handle single node_name
        node_name = getattr(payload, "node_name", None)
        if node_name is not None and node_name in self.node_name_mapping:
            return self._copy_payload_with_translated_node_name(payload, self.node_name_mapping[node_name])

        return payload

    def _copy_payload_with_translated_involved_nodes(self, payload: Any, translated_nodes: list[str]) -> Any:
        """Create a copy of a payload with translated involved_nodes.

        Args:
            payload: The payload to copy
            translated_nodes: The translated list of node names

        Returns:
            A new payload instance with translated involved_nodes
        """
        payload_class = type(payload)

        if hasattr(payload, "model_dump"):
            payload_dict = payload.model_dump()
        elif hasattr(payload, "__dict__"):
            payload_dict = payload.__dict__.copy()
        else:
            return payload

        payload_dict["involved_nodes"] = translated_nodes

        try:
            return payload_class(**payload_dict)
        except Exception:
            return payload

    def _copy_payload_with_translated_node_name(self, payload: Any, translated_name: str) -> Any:
        """Create a copy of a payload with a translated node_name.

        Args:
            payload: The payload to copy
            translated_name: The translated node name

        Returns:
            A new payload instance with translated node_name
        """
        payload_class = type(payload)

        if hasattr(payload, "model_dump"):
            payload_dict = payload.model_dump()
        elif hasattr(payload, "__dict__"):
            payload_dict = payload.__dict__.copy()
        else:
            return payload

        payload_dict["node_name"] = translated_name

        try:
            return payload_class(**payload_dict)
        except Exception:
            return payload

    def _create_translated_wrapped_event(self, event: Any, translated_payload: Any) -> Any | None:
        """Create a new wrapped event with a translated payload.

        Args:
            event: The original wrapped event (e.g., ExecutionGriptapeNodeEvent)
            translated_payload: The translated payload

        Returns:
            A new wrapped event with the translated payload, or None if creation fails
        """
        wrapped_event = getattr(event, "wrapped_event", None)
        if wrapped_event is None:
            return None

        # Create new wrapped_event with translated payload
        wrapped_class = type(wrapped_event)
        try:
            new_wrapped = wrapped_class(payload=translated_payload)
        except Exception:
            return None

        # Create new outer event with new wrapped_event
        event_class = type(event)
        try:
            return event_class(wrapped_event=new_wrapped)
        except Exception:
            return None

    def _copy_event_with_translated_name(self, event: Any) -> Any:
        """Create a copy of an event with the node name translated to the original name.

        Args:
            event: The event to copy and translate

        Returns:
            A new event instance with the translated node name
        """
        # Get the original node name from the mapping
        node_name = event.node_name
        original_node_name = self.node_name_mapping[node_name]

        # Get the event class
        event_class = type(event)

        # Create a dict of all event attributes
        if hasattr(event, "model_dump"):
            event_dict = event.model_dump()
        elif hasattr(event, "__dict__"):
            event_dict = event.__dict__.copy()
        else:
            # Can't copy this event, return as-is
            return event

        # Replace the node name with the original name
        event_dict["node_name"] = original_node_name

        # Create a new event instance with the translated name
        try:
            return event_class(**event_dict)
        except Exception:
            # If we can't create a new instance, return the original
            return event
