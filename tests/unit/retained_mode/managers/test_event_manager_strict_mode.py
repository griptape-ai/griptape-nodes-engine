"""Tests for the reentrant-bus-in-init strict-mode tripwire.

EventManager.handle_request / ahandle_request consult
``LibraryRegistry.is_constructing_node()`` on every dispatch. When that
flag is set (a node ``__init__`` is currently running on the calling
task) and a strict-mode scope is open, the manager records a
``reentrant-bus-in-init`` violation against the active scope. The
detector is an ergonomics rule (``correctness=False``,
``worker_escalation=True``): severity is WARNING on the orchestrator and
ERROR on the worker. It still drops the class from the worker schema
during library load via ``drops_class_from_schema``.

Outside of node construction, dispatch must not record a violation.
Outside of any strict-mode scope, ``STRICT_MODE.report`` is a no-op so
the reentrant call still goes through without crashing.

``reentrant_bus_in_init_would_report`` exposes that same condition for
callers that legitimately read engine state from a node ``__init__`` and
want to skip the request rather than commit the violation. It has its own
class below, including a test that it and the detector cannot disagree.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from griptape_nodes.common.strict_mode import (
    STRICT_MODE,
    StrictModeScopeKind,
    StrictModeSeverity,
)

# Intentional reach into a private module symbol: the public read API is
# LibraryRegistry.is_constructing_node(), but there is no public setter --
# the flag is set only inside LibraryRegistry.create_node. Tests need to
# simulate "we are inside __init__" without calling create_node, so they
# manipulate the underlying ContextVar directly. Keeping this private
# avoids growing the registry's public surface for test-only plumbing.
from griptape_nodes.node_library.library_registry import _constructing_node
from griptape_nodes.retained_mode.events.base_events import (
    RequestPayload,
    ResultPayload,
    ResultPayloadSuccess,
)
from griptape_nodes.retained_mode.managers.event_manager import (
    EventManager,
    current_request_type,
    reentrant_bus_in_init_would_report,
)


@dataclass(kw_only=True)
class _ProbeRequest(RequestPayload):
    """Minimal request used to exercise the dispatch path."""

    marker: str


@dataclass(kw_only=True)
class _ProbeResult(ResultPayloadSuccess):
    seen_by: str


def _make_event_manager_with_probe_handler() -> EventManager:
    event_manager = EventManager()

    async def handler(request: _ProbeRequest) -> _ProbeResult:
        return _ProbeResult(seen_by=request.marker, result_details="ok")

    event_manager.assign_manager_to_request_type(_ProbeRequest, handler)
    return event_manager


# Kept in step with the copy in test_event_manager.py; see the note there.
_HOOK_DRAIN_TICKS = 50


async def _drain_post_dispatch_hooks(event_manager: EventManager) -> None:
    """Run the loop until every detached hook task has finished.

    Hooks reach the loop via ``call_soon_threadsafe``, so the ``asyncio.Task`` does not
    exist yet when the dispatch call returns.
    """
    for _ in range(_HOOK_DRAIN_TICKS):
        await asyncio.sleep(0)
        pending = list(event_manager._inflight_post_dispatch_hook_tasks)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


class TestReentrantBusInInit:
    """The tripwire records one violation per request dispatched during __init__."""

    @pytest.mark.asyncio
    async def test_dispatch_during_node_init_records_violation(self) -> None:
        event_manager = _make_event_manager_with_probe_handler()

        token = _constructing_node.set(True)
        try:
            with STRICT_MODE.open_scope(
                kind=StrictModeScopeKind.LOAD_PROBE,
                subject="MyNodeClass",
                library_name="libA",
                is_worker=True,
            ) as scope:
                result = await event_manager.ahandle_request(_ProbeRequest(marker="m1"))
        finally:
            _constructing_node.reset(token)

        assert result.succeeded()
        assert len(scope.violations) == 1
        violation = scope.violations[0]
        assert violation.rule_id == "reentrant-bus-in-init"
        assert violation.severity is StrictModeSeverity.ERROR
        assert violation.subject == "MyNodeClass"
        assert violation.library_name == "libA"
        assert "_ProbeRequest" in violation.message

    def test_sync_dispatch_during_node_init_records_violation(self) -> None:
        event_manager = _make_event_manager_with_probe_handler()

        token = _constructing_node.set(True)
        try:
            with STRICT_MODE.open_scope(
                kind=StrictModeScopeKind.LOAD_PROBE,
                subject="MyNodeClass",
                library_name="libA",
                is_worker=True,
            ) as scope:
                result = event_manager.handle_request(_ProbeRequest(marker="m2"))
        finally:
            _constructing_node.reset(token)

        assert result.succeeded()
        assert len(scope.violations) == 1
        assert scope.violations[0].rule_id == "reentrant-bus-in-init"

    @pytest.mark.asyncio
    async def test_dispatch_outside_node_init_records_no_violation(self) -> None:
        event_manager = _make_event_manager_with_probe_handler()

        with STRICT_MODE.open_scope(
            kind=StrictModeScopeKind.LOAD_PROBE,
            subject="MyNodeClass",
            library_name="libA",
            is_worker=True,
        ) as scope:
            result = await event_manager.ahandle_request(_ProbeRequest(marker="m3"))

        assert result.succeeded()
        assert scope.violations == []

    @pytest.mark.asyncio
    async def test_dispatch_during_node_init_with_no_scope_does_not_crash(self) -> None:
        event_manager = _make_event_manager_with_probe_handler()

        token = _constructing_node.set(True)
        try:
            result = await event_manager.ahandle_request(_ProbeRequest(marker="m4"))
        finally:
            _constructing_node.reset(token)

        assert result.succeeded()

    @pytest.mark.asyncio
    async def test_severity_is_warning_on_orchestrator(self) -> None:
        """Ergonomics rule: the orchestrator scope warns rather than failing."""
        event_manager = _make_event_manager_with_probe_handler()

        token = _constructing_node.set(True)
        try:
            with STRICT_MODE.open_scope(
                kind=StrictModeScopeKind.RUNTIME_EXECUTE,
                subject="node-1",
                library_name="libA",
                is_worker=False,
            ) as scope:
                await event_manager.ahandle_request(_ProbeRequest(marker="m5"))
        finally:
            _constructing_node.reset(token)

        assert scope.violations[0].severity is StrictModeSeverity.WARNING

    @pytest.mark.asyncio
    async def test_severity_is_error_on_worker(self) -> None:
        """Worker escalation: the same rule fails on the worker side."""
        event_manager = _make_event_manager_with_probe_handler()

        token = _constructing_node.set(True)
        try:
            with STRICT_MODE.open_scope(
                kind=StrictModeScopeKind.RUNTIME_EXECUTE,
                subject="node-1",
                library_name="libA",
                is_worker=True,
            ) as scope:
                await event_manager.ahandle_request(_ProbeRequest(marker="m6"))
        finally:
            _constructing_node.reset(token)

        assert scope.violations[0].severity is StrictModeSeverity.ERROR


class TestReentrantBusInInitWouldReport:
    """The predicate components consult before issuing a request from a node __init__.

    Being inside ``__init__`` is not enough: with no scope open the detector's report is a
    no-op, so the request is free and the caller should just make it. Components keyed their
    deferral off ``is_constructing_node()`` alone once, which made every node in every
    deployment pay for a hazard only the worker's probe and node execution have -- model
    dropdowns lost their denial rows and download subtitles until the node was run.
    """

    def test_false_outside_node_init_even_inside_a_scope(self) -> None:
        with STRICT_MODE.open_scope(
            kind=StrictModeScopeKind.LOAD_PROBE,
            subject="MyNodeClass",
            library_name="libA",
            is_worker=True,
        ):
            assert reentrant_bus_in_init_would_report() is False

    def test_false_during_node_init_with_no_scope_open(self) -> None:
        """The restored case: an editor drop, a workflow load, any single-process engine."""
        token = _constructing_node.set(True)
        try:
            assert reentrant_bus_in_init_would_report() is False
        finally:
            _constructing_node.reset(token)

    @pytest.mark.parametrize("kind", [StrictModeScopeKind.LOAD_PROBE, StrictModeScopeKind.RUNTIME_EXECUTE])
    def test_true_during_node_init_inside_a_scope(self, kind: StrictModeScopeKind) -> None:
        token = _constructing_node.set(True)
        try:
            with STRICT_MODE.open_scope(kind=kind, subject="s", library_name="libA", is_worker=True):
                assert reentrant_bus_in_init_would_report() is True
        finally:
            _constructing_node.reset(token)

    def test_true_during_node_init_when_strict_mode_is_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A disabled reporter detaches its scopes, so a probe is indistinguishable from a drop.

        Reaches for the private ``_enabled`` because ``enabled`` is a read-only property and the
        env var is read once at construction; the reporter is a module singleton, so re-creating
        it would not be the object the predicate consults.
        """
        monkeypatch.setattr(STRICT_MODE, "_enabled", False)
        token = _constructing_node.set(True)
        try:
            assert reentrant_bus_in_init_would_report() is True
        finally:
            _constructing_node.reset(token)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("constructing", [True, False])
    @pytest.mark.parametrize("open_a_scope", [True, False])
    async def test_it_agrees_with_the_detector(self, *, constructing: bool, open_a_scope: bool) -> None:
        """The drift guard: "we deferred" and "it would have been reported" are one condition.

        A caller that skips a request the detector would have ignored loses functionality for
        nothing; one that issues a request the detector reports gets its class dropped from the
        worker schema. Both halves live in the predicate so neither can happen.
        """
        event_manager = _make_event_manager_with_probe_handler()
        scope_kind = StrictModeScopeKind.LOAD_PROBE

        token = _constructing_node.set(constructing)
        try:
            if not open_a_scope:
                predicted = reentrant_bus_in_init_would_report()
                await event_manager.ahandle_request(_ProbeRequest(marker="m7"))
                assert predicted is False  # nothing to compare against; report() no-ops
                return
            with STRICT_MODE.open_scope(
                kind=scope_kind, subject="MyNodeClass", library_name="libA", is_worker=True
            ) as scope:
                predicted = reentrant_bus_in_init_would_report()
                await event_manager.ahandle_request(_ProbeRequest(marker="m7"))
        finally:
            _constructing_node.reset(token)

        assert predicted is (len(scope.violations) == 1)


class TestPostDispatchHookContextIsolation:
    """A post-dispatch hook must not inherit the dispatching task's strict-mode scope.

    Hook tasks are created with a *fresh* ``contextvars.Context()`` rather than a copy of
    the caller's. The scope stack holds mutable ``StrictModeScope`` objects, so a hook
    that inherited a node's ``RUNTIME_EXECUTE`` scope could append to ``scope.violations``
    after the node's own scope had already been evaluated -- and on a worker an
    ERROR-severity violation promotes ``ExecuteNodeResultSuccess`` to a failure. A
    library's telemetry hook must not be able to fail a user's node.

    The fresh context also clears the dispatching request type, so
    ``current_request_type()`` does not lie inside the hook.
    """

    @pytest.mark.asyncio
    async def test_detached_hook_cannot_report_into_the_dispatching_scope(self) -> None:
        event_manager = _make_event_manager_with_probe_handler()
        event_manager.initialize_queue(asyncio.Queue())

        seen_request_types: list[type | None] = []

        async def hook(_request: RequestPayload, _result: ResultPayload) -> None:
            seen_request_types.append(current_request_type())
            STRICT_MODE.report(rule_id="reentrant-bus-in-init", message="from the hook")

        event_manager.add_post_dispatch_hook(_ProbeRequest, hook)

        with STRICT_MODE.open_scope(
            kind=StrictModeScopeKind.RUNTIME_EXECUTE,
            subject="MyNode",
            library_name="libA",
            is_worker=True,
        ) as scope:
            await event_manager.ahandle_request(_ProbeRequest(marker="hook-detached"))
            await _drain_post_dispatch_hooks(event_manager)

        assert seen_request_types == [None]
        assert scope.violations == []

    def test_inline_hook_cannot_report_into_the_dispatching_scope(self) -> None:
        """Same guarantee on the fallback path, which blocks the caller until the hook returns.

        The hook does not run *on* the caller's thread: a sync callback goes through
        ``to_thread``, onto a worker of the transient loop ``asyncio.run`` creates here.
        The isolation has to survive that hop, which is why the fresh context is entered
        before the loop is started rather than inside the callback.
        """
        event_manager = _make_event_manager_with_probe_handler()

        seen_request_types: list[type | None] = []

        def hook(_request: RequestPayload, _result: ResultPayload) -> None:
            seen_request_types.append(current_request_type())
            STRICT_MODE.report(rule_id="reentrant-bus-in-init", message="from the hook")

        event_manager.add_post_dispatch_hook(_ProbeRequest, hook)

        with STRICT_MODE.open_scope(
            kind=StrictModeScopeKind.RUNTIME_EXECUTE,
            subject="MyNode",
            library_name="libA",
            is_worker=True,
        ) as scope:
            event_manager.handle_request(_ProbeRequest(marker="hook-inline"))

        assert seen_request_types == [None]
        assert scope.violations == []

    @pytest.mark.asyncio
    async def test_inline_hook_on_the_side_loop_cannot_report_into_the_dispatching_scope(self) -> None:
        """The inline fallback has two sub-branches; this covers the one taken inside a loop.

        Running the hook on another thread is not isolation by itself. ``ThreadRunner.run``
        hands the coroutine over with ``run_coroutine_threadsafe``, which has no
        ``context=`` parameter, and the ``call_soon_threadsafe`` underneath it captures
        ``copy_context()`` on the *submitting* thread -- so without an explicit fresh
        context the hook inherits this scope and a library hook can fail a user's node.
        The sync-callback hop to a worker thread does not save it either, because
        ``to_thread`` propagates the context it was called in.

        A bare ``EventManager`` has no ``_event_loop``, so a sync dispatch from inside this
        test's running loop takes the fallback and then its ThreadRunner sub-branch.
        """
        event_manager = _make_event_manager_with_probe_handler()
        assert event_manager._event_loop is None

        seen_request_types: list[type | None] = []

        def hook(_request: RequestPayload, _result: ResultPayload) -> None:
            seen_request_types.append(current_request_type())
            STRICT_MODE.report(rule_id="reentrant-bus-in-init", message="from the hook")

        event_manager.add_post_dispatch_hook(_ProbeRequest, hook)

        with STRICT_MODE.open_scope(
            kind=StrictModeScopeKind.RUNTIME_EXECUTE,
            subject="MyNode",
            library_name="libA",
            is_worker=True,
        ) as scope:
            event_manager.handle_request(_ProbeRequest(marker="hook-side-loop"))

        assert seen_request_types == [None]
        assert scope.violations == []
