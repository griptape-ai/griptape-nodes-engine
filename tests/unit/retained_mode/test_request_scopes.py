"""Dispatch binds, and hands a handler, the workflow its declared scope takes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest

from griptape_nodes.retained_mode.events.access_events import QueryModelAccessForNodeRequest
from griptape_nodes.retained_mode.events.app_events import (
    EngineHeartbeatRequest,
    EngineHeartbeatResultSuccess,
    GetEngineVersionRequest,
)
from griptape_nodes.retained_mode.events.base_events import RequestPayload, ResultPayload, ResultPayloadSuccess
from griptape_nodes.retained_mode.events.config_events import (
    GetConfigCategoryRequest,
    GetWorkspaceRequest,
    ListBetaFeaturesRequest,
)
from griptape_nodes.retained_mode.events.generic_events import GenericResultFailure, WorkflowNotOpenResultFailure
from griptape_nodes.retained_mode.events.library_events import ListRegisteredLibrariesRequest
from griptape_nodes.retained_mode.events.project_events import GetCurrentProjectRequest, ListProjectTemplatesRequest
from griptape_nodes.retained_mode.events.workflow_events import (
    ListAllWorkflowInfoRequest,
    ListAllWorkflowsRequest,
    ListCallableWorkflowsRequest,
)
from griptape_nodes.retained_mode.managers.event_manager import ResultContext
from griptape_nodes.retained_mode.request_handlers import RequestScope
from griptape_nodes.retained_mode.runtime.binding import NO_WORKFLOW, bound_workflow
from griptape_nodes.retained_mode.runtime.open_workflow_set import OpenWorkflowSet

if TYPE_CHECKING:
    from collections.abc import Callable

    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.runtime.workflow_runtime import WorkflowRuntime

NOT_OPEN_ID = "0000000000000000000000000000dead"


@dataclass(kw_only=True)
class _ScopeProbeRequest(RequestPayload):
    pass


@dataclass(kw_only=True)
class _InnerProbeRequest(RequestPayload):
    pass


@dataclass(kw_only=True)
class _ScopeProbeResult(ResultPayloadSuccess):
    pass


class _Calls:
    """Records the workflow each probe handler was handed, and the workflow current while it ran."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.workflows: list[WorkflowRuntime | None] = []
        self.current: list[WorkflowRuntime | None] = []

    def workflow_handler(self, request: RequestPayload, workflow: WorkflowRuntime) -> ResultPayload:  # noqa: ARG002
        self.workflows.append(workflow)
        self.current.append(self.engine.current_workflow_or_none())
        return _ScopeProbeResult(result_details="ran")

    async def optional_handler(
        self,
        request: RequestPayload,  # noqa: ARG002
        workflow: WorkflowRuntime | None = None,
    ) -> ResultPayload:
        self.workflows.append(workflow)
        self.current.append(self.engine.current_workflow_or_none())
        return _ScopeProbeResult(result_details="ran")

    def host_handler(self, request: RequestPayload) -> ResultPayload:  # noqa: ARG002
        self.current.append(self.engine.current_workflow_or_none())
        return _ScopeProbeResult(result_details="ran")


@pytest.fixture
def calls(engine: Engine) -> _Calls:
    """A fresh record of probe handler calls."""
    return _Calls(engine)


def _register(engine: Engine, calls: _Calls, scope: RequestScope, request_type: type[RequestPayload]) -> None:
    event_manager = engine.event_manager
    match scope:
        case RequestScope.WORKFLOW:
            event_manager.assign_manager_to_request_type(request_type, calls.workflow_handler, scope=scope)
        case RequestScope.OPTIONAL_WORKFLOW:
            event_manager.assign_manager_to_request_type(request_type, calls.optional_handler, scope=scope)
        case RequestScope.HOST:
            event_manager.assign_manager_to_request_type(request_type, calls.host_handler, scope=scope)


def _open_workflow(engine: Engine, display_name: str = "open") -> WorkflowRuntime:
    engine.context_manager.push_workflow(workflow_name=display_name)
    return engine.open_workflows.get(engine.context_manager.get_current_workflow_id())


def _open_two(engine: Engine) -> list[WorkflowRuntime]:
    """Two workflows open at once, through the test seam that allows it."""
    engine._open_workflows = OpenWorkflowSet(engine, allow_several_open=True)
    opened = []
    for display_name in ("A", "B"):
        workflow = engine.workflow_registry.register_unsaved(display_name, listed=False)
        opened.append(engine.open_workflows.open(workflow.workflow_id))
    return opened


class TestScopesThatTakeAWorkflow:
    @pytest.mark.parametrize("scope", [RequestScope.WORKFLOW, RequestScope.OPTIONAL_WORKFLOW])
    @pytest.mark.asyncio
    async def test_the_envelope_id_names_the_workflow_passed_and_bound(
        self, engine: Engine, calls: _Calls, scope: RequestScope
    ) -> None:
        workflow = _open_workflow(engine)
        _register(engine, calls, scope, _ScopeProbeRequest)

        with engine.bind_workflow(NO_WORKFLOW):
            result_event = await engine.event_manager.ahandle_request(
                _ScopeProbeRequest(), result_context=ResultContext(open_workflow_id=workflow.workflow_id)
            )

        assert result_event.succeeded()
        assert calls.workflows == [workflow]
        assert calls.current == [workflow]

    @pytest.mark.parametrize("scope", [RequestScope.WORKFLOW, RequestScope.OPTIONAL_WORKFLOW])
    def test_an_envelope_id_that_is_not_open_fails_without_running(
        self, engine: Engine, calls: _Calls, scope: RequestScope
    ) -> None:
        _open_workflow(engine)
        _register(engine, calls, scope, _ScopeProbeRequest)

        result_event = engine.event_manager.handle_request(
            _ScopeProbeRequest(), result_context=ResultContext(open_workflow_id=NOT_OPEN_ID)
        )

        assert isinstance(result_event.result, WorkflowNotOpenResultFailure)
        assert result_event.open_workflow_id == NOT_OPEN_ID
        assert calls.workflows == []

    @pytest.mark.parametrize("scope", [RequestScope.WORKFLOW, RequestScope.OPTIONAL_WORKFLOW])
    def test_no_envelope_id_takes_the_callers_binding(self, engine: Engine, calls: _Calls, scope: RequestScope) -> None:
        _a, b = _open_two(engine)
        _register(engine, calls, scope, _ScopeProbeRequest)

        with engine.bind_workflow(b):
            result = engine.handle_request(_ScopeProbeRequest())

        assert result.succeeded()
        assert calls.workflows == [b]

    @pytest.mark.parametrize("scope", [RequestScope.WORKFLOW, RequestScope.OPTIONAL_WORKFLOW])
    def test_a_binding_the_handler_makes_reaches_a_caller_it_inherited_from(
        self, engine: Engine, scope: RequestScope
    ) -> None:
        """Closing the caller's workflow inside a nested request leaves the caller bound to no workflow too."""
        workflow = _open_workflow(engine)

        def close_handler(request: RequestPayload, workflow: WorkflowRuntime | None = None) -> ResultPayload:  # noqa: ARG001
            bound_workflow.set(NO_WORKFLOW)
            return _ScopeProbeResult(result_details="ran")

        def workflow_close_handler(request: RequestPayload, workflow: WorkflowRuntime) -> ResultPayload:
            return close_handler(request, workflow)

        if scope is RequestScope.WORKFLOW:
            engine.event_manager.assign_manager_to_request_type(_ScopeProbeRequest, workflow_close_handler, scope=scope)
        else:
            engine.event_manager.assign_manager_to_request_type(_ScopeProbeRequest, close_handler, scope=scope)

        with engine.bind_workflow(workflow):
            engine.handle_request(_ScopeProbeRequest())
            assert engine.current_workflow_or_none() is None


class TestWorkflowScope:
    def test_no_envelope_id_and_unbound_gets_the_only_open_workflow(self, engine: Engine, calls: _Calls) -> None:
        workflow = _open_workflow(engine)
        _register(engine, calls, RequestScope.WORKFLOW, _ScopeProbeRequest)

        result = engine.handle_request(_ScopeProbeRequest())

        assert result.succeeded()
        assert calls.workflows == [workflow]

    def test_no_envelope_id_and_none_open_fails(self, engine: Engine, calls: _Calls) -> None:
        _register(engine, calls, RequestScope.WORKFLOW, _ScopeProbeRequest)

        result = engine.handle_request(_ScopeProbeRequest())

        assert isinstance(result, GenericResultFailure)
        assert "names no workflow and none is open" in str(result.result_details)
        assert calls.workflows == []

    def test_no_envelope_id_and_several_open_fails(self, engine: Engine, calls: _Calls) -> None:
        _open_two(engine)
        _register(engine, calls, RequestScope.WORKFLOW, _ScopeProbeRequest)

        result = engine.handle_request(_ScopeProbeRequest())

        assert isinstance(result, GenericResultFailure)
        assert "2 workflows are open" in str(result.result_details)
        assert calls.workflows == []

    def test_a_caller_bound_to_no_workflow_fails_it_even_with_one_open(self, engine: Engine, calls: _Calls) -> None:
        _open_workflow(engine)
        _register(engine, calls, RequestScope.WORKFLOW, _ScopeProbeRequest)

        with engine.bind_workflow(NO_WORKFLOW):
            result = engine.handle_request(_ScopeProbeRequest())

        assert isinstance(result, GenericResultFailure)
        assert str(result.result_details) == (
            "Attempted to run _ScopeProbeRequest. Failed because the request names no workflow."
        )
        assert calls.workflows == []


class TestOptionalWorkflowScope:
    @pytest.mark.asyncio
    async def test_no_envelope_id_while_one_is_open_gets_none(self, engine: Engine, calls: _Calls) -> None:
        """It never falls back to the only open workflow: an unaddressed request is not one about it."""
        _open_workflow(engine)
        _register(engine, calls, RequestScope.OPTIONAL_WORKFLOW, _ScopeProbeRequest)

        result_event = await engine.event_manager.ahandle_request(_ScopeProbeRequest())

        assert result_event.succeeded()
        assert calls.workflows == [None]
        assert calls.current == [None]
        assert result_event.open_workflow_id is None

    def test_no_envelope_id_and_none_open_gets_none(self, engine: Engine, calls: _Calls) -> None:
        _register(engine, calls, RequestScope.OPTIONAL_WORKFLOW, _ScopeProbeRequest)

        result = engine.handle_request(_ScopeProbeRequest())

        assert result.succeeded()
        assert calls.workflows == [None]

    def test_a_caller_bound_to_no_workflow_gets_none_while_one_is_open(self, engine: Engine, calls: _Calls) -> None:
        _open_workflow(engine)
        _register(engine, calls, RequestScope.OPTIONAL_WORKFLOW, _ScopeProbeRequest)

        with engine.bind_workflow(NO_WORKFLOW):
            result = engine.handle_request(_ScopeProbeRequest())

        assert result.succeeded()
        assert calls.workflows == [None]


class TestHostScope:
    @pytest.mark.asyncio
    async def test_a_stale_envelope_id_succeeds_instead_of_failing(self, engine: Engine, calls: _Calls) -> None:
        _open_workflow(engine)
        _register(engine, calls, RequestScope.HOST, _ScopeProbeRequest)

        result_event = await engine.event_manager.ahandle_request(
            _ScopeProbeRequest(), result_context=ResultContext(open_workflow_id=NOT_OPEN_ID)
        )

        assert result_event.succeeded()
        assert not isinstance(result_event.result, WorkflowNotOpenResultFailure)
        assert result_event.open_workflow_id == NOT_OPEN_ID
        assert calls.current == [None]

    def test_it_acts_on_no_workflow_even_when_the_envelope_names_the_open_one(
        self, engine: Engine, calls: _Calls
    ) -> None:
        workflow = _open_workflow(engine)
        _register(engine, calls, RequestScope.HOST, _ScopeProbeRequest)

        result_event = engine.event_manager.handle_request(
            _ScopeProbeRequest(), result_context=ResultContext(open_workflow_id=workflow.workflow_id)
        )

        assert result_event.succeeded()
        assert calls.current == [None]

    def test_an_optional_request_it_sends_gets_none(self, engine: Engine, calls: _Calls) -> None:
        """What a host request sends with no envelope id acts on no workflow, whatever the client named."""
        workflow = _open_workflow(engine)
        _register(engine, calls, RequestScope.OPTIONAL_WORKFLOW, _InnerProbeRequest)

        def host_handler(request: _ScopeProbeRequest) -> ResultPayload:  # noqa: ARG001
            return engine.handle_request(_InnerProbeRequest())

        engine.event_manager.assign_manager_to_request_type(_ScopeProbeRequest, host_handler, scope=RequestScope.HOST)

        result_event = engine.event_manager.handle_request(
            _ScopeProbeRequest(), result_context=ResultContext(open_workflow_id=workflow.workflow_id)
        )

        assert result_event.succeeded()
        assert calls.workflows == [None]


# Reads about the engine, not a workflow: each works with no workflow open, so a stale id cannot fail it.
_HOST_READS: list[Callable[[], RequestPayload]] = [
    lambda: EngineHeartbeatRequest(heartbeat_id="hb"),
    GetEngineVersionRequest,
    ListRegisteredLibrariesRequest,
    ListAllWorkflowsRequest,
    ListAllWorkflowInfoRequest,
    ListCallableWorkflowsRequest,
    GetConfigCategoryRequest,
    GetWorkspaceRequest,
    ListBetaFeaturesRequest,
    GetCurrentProjectRequest,
    ListProjectTemplatesRequest,
]


class TestHostReads:
    @pytest.mark.parametrize("make_request", _HOST_READS)
    @pytest.mark.asyncio
    async def test_a_stale_envelope_id_does_not_fail_it(
        self, engine: Engine, make_request: Callable[[], RequestPayload]
    ) -> None:
        _open_workflow(engine)
        request = make_request()

        result_event = await engine.event_manager.ahandle_request(
            request, result_context=ResultContext(open_workflow_id=NOT_OPEN_ID)
        )

        assert engine.event_manager.get_scope_for_request_type(type(request)) is RequestScope.HOST
        assert not isinstance(result_event.result, WorkflowNotOpenResultFailure)
        assert result_event.succeeded(), result_event.result.result_details

    def test_the_heartbeat_still_reports_the_open_workflow(self, engine: Engine) -> None:
        workflow = _open_workflow(engine)

        result = engine.event_manager.handle_request(
            EngineHeartbeatRequest(heartbeat_id="hb"), result_context=ResultContext(open_workflow_id=NOT_OPEN_ID)
        ).result

        assert isinstance(result, EngineHeartbeatResultSuccess)
        assert result.current_workflow_id == workflow.workflow_id

    def test_a_model_access_query_is_a_host_request(self, engine: Engine) -> None:
        """It asks about a node type and the host's license, not a node in a workflow."""
        assert engine.event_manager.get_scope_for_request_type(QueryModelAccessForNodeRequest) is RequestScope.HOST


class TestRequestsARuntimeSends:
    """A runtime's requests act on that runtime's workflow, whatever its caller has bound."""

    @pytest.mark.asyncio
    async def test_they_act_on_the_runtimes_workflow(self, engine: Engine, calls: _Calls) -> None:
        a, b = _open_two(engine)
        _register(engine, calls, RequestScope.WORKFLOW, _ScopeProbeRequest)

        with engine.bind_workflow(a):
            sync_result = b.handle_request(_ScopeProbeRequest())
            async_result = await b.ahandle_request(_ScopeProbeRequest())

        assert sync_result.succeeded()
        assert async_result.succeeded()
        assert calls.workflows == [b, b]
        assert calls.current == [b, b]

    @pytest.mark.asyncio
    async def test_a_closed_runtimes_requests_are_refused_not_sent_to_the_open_one(
        self, engine: Engine, calls: _Calls
    ) -> None:
        closed = _open_workflow(engine, "closed")
        engine.close_workflow(closed)
        _open_workflow(engine)
        _register(engine, calls, RequestScope.WORKFLOW, _ScopeProbeRequest)

        result = await closed.ahandle_request(_ScopeProbeRequest())

        assert isinstance(result, WorkflowNotOpenResultFailure)
        assert calls.workflows == []
