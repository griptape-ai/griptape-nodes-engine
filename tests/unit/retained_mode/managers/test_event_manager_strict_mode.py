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
"""

from __future__ import annotations

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
    ResultPayloadSuccess,
)
from griptape_nodes.retained_mode.managers.event_manager import EventManager


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
