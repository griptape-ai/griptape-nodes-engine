"""Dispatch binds, and hands a handler, the workflow its signature's scope takes."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest

from griptape_nodes.app.worker_routing import RemoteHandler, register_broadcast_handlers
from griptape_nodes.retained_mode.events import converters
from griptape_nodes.retained_mode.events.access_events import QueryModelAccessForNodeRequest
from griptape_nodes.retained_mode.events.app_events import (
    EngineHeartbeatRequest,
    EngineHeartbeatResultSuccess,
    GetEngineVersionRequest,
)
from griptape_nodes.retained_mode.events.base_events import RequestPayload, ResultPayload, ResultPayloadSuccess
from griptape_nodes.retained_mode.events.budget_events import BudgetAccessRequest, ReportUsageRequest
from griptape_nodes.retained_mode.events.config_events import (
    GetConfigCategoryRequest,
    GetWorkspaceRequest,
    ListBetaFeaturesRequest,
)
from griptape_nodes.retained_mode.events.diagnostics_events import CollectDiagnosticsRequest
from griptape_nodes.retained_mode.events.generic_events import (
    WorkflowNotNamedResultFailure,
    WorkflowNotOpenResultFailure,
)
from griptape_nodes.retained_mode.events.library_events import ListRegisteredLibrariesRequest
from griptape_nodes.retained_mode.events.model_events import DeclareModelInvocationRequest
from griptape_nodes.retained_mode.events.object_events import RenameObjectRequest
from griptape_nodes.retained_mode.events.payload_registry import PayloadRegistry
from griptape_nodes.retained_mode.events.project_events import GetCurrentProjectRequest, ListProjectTemplatesRequest
from griptape_nodes.retained_mode.events.variable_events import (
    CreateVariableRequest,
    CreateVariableResultSuccess,
    DeleteVariableRequest,
    GetVariableDetailsRequest,
    GetVariableRequest,
    GetVariablesRequest,
    GetVariableTypeRequest,
    GetVariableValueRequest,
    HasVariableRequest,
    ListSubstitutablesRequest,
    ListVariablesRequest,
    RenameVariableRequest,
    ResolveSubstitutionRequest,
    SetVariablesRequest,
    SetVariableTypeRequest,
    SetVariableValueRequest,
)
from griptape_nodes.retained_mode.events.workflow_events import (
    GetVariableSubstitutionEnabledRequest,
    ListAllWorkflowInfoRequest,
    ListAllWorkflowsRequest,
    ListCallableWorkflowsRequest,
    SetVariableSubstitutionEnabledRequest,
)
from griptape_nodes.retained_mode.managers import event_manager as event_manager_module
from griptape_nodes.retained_mode.managers.event_manager import EventManager, ResultContext
from griptape_nodes.retained_mode.request_handlers import ForwardingHandler, RequestScope, scope_from_signature
from griptape_nodes.retained_mode.runtime.binding import NO_WORKFLOW, bound_workflow
from griptape_nodes.retained_mode.runtime.open_workflow_set import OpenWorkflowSet
from griptape_nodes.retained_mode.unmigrated_requests import AMBIENT_UNTIL_MIGRATED, ONLY_OPEN_UNTIL_CLIENTS_NAME
from tests.unit.host_workflow_reads import HostWorkflowRead

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
        self.current.append(_bound())
        return _ScopeProbeResult(result_details="ran")

    async def optional_handler(
        self,
        request: RequestPayload,  # noqa: ARG002
        workflow: WorkflowRuntime | None = None,
    ) -> ResultPayload:
        self.workflows.append(workflow)
        self.current.append(_bound())
        return _ScopeProbeResult(result_details="ran")

    def host_handler(self, request: RequestPayload) -> ResultPayload:  # noqa: ARG002
        self.current.append(_bound())
        return _ScopeProbeResult(result_details="ran")


def _bound() -> WorkflowRuntime | None:
    """The workflow the current work is bound to, read without counting as a workflow read."""
    binding = bound_workflow.get()
    if binding is NO_WORKFLOW:
        return None
    return binding


@pytest.fixture
def calls(engine: Engine) -> _Calls:
    """A fresh record of probe handler calls."""
    return _Calls(engine)


def _register(engine: Engine, calls: _Calls, scope: RequestScope, request_type: type[RequestPayload]) -> None:
    event_manager = engine.event_manager
    match scope:
        case RequestScope.WORKFLOW:
            event_manager.assign_manager_to_request_type(request_type, calls.workflow_handler)
        case RequestScope.OPTIONAL_WORKFLOW:
            event_manager.assign_manager_to_request_type(request_type, calls.optional_handler)
        case RequestScope.HOST:
            event_manager.assign_manager_to_request_type(request_type, calls.host_handler)


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

    @pytest.mark.asyncio
    async def test_an_addressing_failure_logs_at_the_request_s_failure_log_level(
        self, engine: Engine, calls: _Calls, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A caller that expects the miss can quiet it, as for any failure its handler returns."""
        _register(engine, calls, RequestScope.WORKFLOW, _ScopeProbeRequest)

        with caplog.at_level(logging.DEBUG, logger="griptape_nodes"):
            result_event = await engine.event_manager.ahandle_request(
                _ScopeProbeRequest(failure_log_level=logging.DEBUG)
            )

        assert isinstance(result_event.result, WorkflowNotNamedResultFailure)
        records = [record for record in caplog.records if "none is open" in record.getMessage()]
        assert [record.levelno for record in records] == [logging.DEBUG]

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
            engine.event_manager.assign_manager_to_request_type(_ScopeProbeRequest, workflow_close_handler)
        else:
            engine.event_manager.assign_manager_to_request_type(_ScopeProbeRequest, close_handler)

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

        assert isinstance(result, WorkflowNotNamedResultFailure)
        assert "names no workflow and none is open" in str(result.result_details)
        assert calls.workflows == []

    def test_no_envelope_id_and_several_open_fails(self, engine: Engine, calls: _Calls) -> None:
        _open_two(engine)
        _register(engine, calls, RequestScope.WORKFLOW, _ScopeProbeRequest)

        result = engine.handle_request(_ScopeProbeRequest())

        assert isinstance(result, WorkflowNotNamedResultFailure)
        assert "2 workflows are open" in str(result.result_details)
        assert calls.workflows == []

    def test_a_caller_bound_to_no_workflow_fails_it_even_with_one_open(self, engine: Engine, calls: _Calls) -> None:
        _open_workflow(engine)
        _register(engine, calls, RequestScope.WORKFLOW, _ScopeProbeRequest)

        with engine.bind_workflow(NO_WORKFLOW):
            result = engine.handle_request(_ScopeProbeRequest())

        assert isinstance(result, WorkflowNotNamedResultFailure)
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


@pytest.fixture
def probe_falls_back_to_the_only_open_workflow(monkeypatch: pytest.MonkeyPatch) -> None:
    """List `_ScopeProbeRequest` in `ONLY_OPEN_UNTIL_CLIENTS_NAME`."""
    monkeypatch.setattr(event_manager_module, "ONLY_OPEN_UNTIL_CLIENTS_NAME", frozenset({"_ScopeProbeRequest"}))


@pytest.mark.usefixtures("probe_falls_back_to_the_only_open_workflow")
class TestOnlyOpenUntilClientsName:
    """A listed OPTIONAL_WORKFLOW request that names nothing gets the only open workflow, None with none open."""

    @pytest.mark.asyncio
    async def test_no_envelope_id_while_one_is_open_gets_it(self, engine: Engine, calls: _Calls) -> None:
        workflow = _open_workflow(engine)
        _register(engine, calls, RequestScope.OPTIONAL_WORKFLOW, _ScopeProbeRequest)

        result_event = await engine.event_manager.ahandle_request(_ScopeProbeRequest())

        assert result_event.succeeded()
        assert calls.workflows == [workflow]
        assert calls.current == [workflow]

    def test_no_envelope_id_while_none_is_open_gets_none(self, engine: Engine, calls: _Calls) -> None:
        _register(engine, calls, RequestScope.OPTIONAL_WORKFLOW, _ScopeProbeRequest)

        result = engine.handle_request(_ScopeProbeRequest())

        assert result.succeeded()
        assert calls.workflows == [None]

    def test_no_envelope_id_while_several_are_open_fails(self, engine: Engine, calls: _Calls) -> None:
        """Its client means the open one, and which that is can't be told."""
        _open_two(engine)
        _register(engine, calls, RequestScope.OPTIONAL_WORKFLOW, _ScopeProbeRequest)

        result = engine.handle_request(_ScopeProbeRequest())

        assert isinstance(result, WorkflowNotNamedResultFailure)
        assert "2 workflows are open" in str(result.result_details)
        assert calls.workflows == []

    def test_the_envelope_id_wins_over_the_only_open_workflow(self, engine: Engine, calls: _Calls) -> None:
        a, b = _open_two(engine)
        _register(engine, calls, RequestScope.OPTIONAL_WORKFLOW, _ScopeProbeRequest)

        result_event = engine.event_manager.handle_request(
            _ScopeProbeRequest(), result_context=ResultContext(open_workflow_id=b.workflow_id)
        )

        assert result_event.succeeded()
        assert calls.workflows == [b]
        assert a not in calls.workflows

    def test_a_caller_bound_to_no_workflow_gets_none_while_one_is_open(self, engine: Engine, calls: _Calls) -> None:
        """Naming no workflow is naming something: only a request that names nothing falls back."""
        _open_workflow(engine)
        _register(engine, calls, RequestScope.OPTIONAL_WORKFLOW, _ScopeProbeRequest)

        with engine.bind_workflow(NO_WORKFLOW):
            result = engine.handle_request(_ScopeProbeRequest())

        assert result.succeeded()
        assert calls.workflows == [None]

    @pytest.mark.parametrize("scope", [RequestScope.WORKFLOW, RequestScope.HOST])
    def test_a_handler_that_is_not_optional_workflow_cannot_register(
        self, engine: Engine, calls: _Calls, scope: RequestScope
    ) -> None:
        with pytest.raises(TypeError, match="ONLY_OPEN_UNTIL_CLIENTS_NAME"):
            _register(engine, calls, scope, _ScopeProbeRequest)


class TestOnlyOpenUntilClientsNameEntries:
    """Each entry is a request type whose OPTIONAL_WORKFLOW handler's clients don't name a workflow yet."""

    @pytest.mark.parametrize("name", sorted(ONLY_OPEN_UNTIL_CLIENTS_NAME))
    def test_its_handler_is_optional_workflow(self, engine: Engine, name: str) -> None:
        request_type = PayloadRegistry.get_type(name)
        assert request_type is not None
        assert issubclass(request_type, RequestPayload)

        assert engine.event_manager.get_scope_for_request_type(request_type) is RequestScope.OPTIONAL_WORKFLOW


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

        engine.event_manager.assign_manager_to_request_type(_ScopeProbeRequest, host_handler)

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
    # Reports on the engine, and has to work when the editor's id is stale.
    CollectDiagnosticsRequest,
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


# Requests whose handlers act on the workflow the envelope names.
_OBJECT_AND_VARIABLE_REQUESTS: list[type[RequestPayload]] = [
    RenameObjectRequest,
    CreateVariableRequest,
    GetVariableRequest,
    GetVariableValueRequest,
    SetVariableValueRequest,
    GetVariableTypeRequest,
    SetVariableTypeRequest,
    DeleteVariableRequest,
    RenameVariableRequest,
    HasVariableRequest,
    ListVariablesRequest,
    ListSubstitutablesRequest,
    GetVariablesRequest,
    ResolveSubstitutionRequest,
    SetVariablesRequest,
    GetVariableDetailsRequest,
    GetVariableSubstitutionEnabledRequest,
    SetVariableSubstitutionEnabledRequest,
]


class TestObjectAndVariableRequests:
    @pytest.mark.parametrize("request_type", _OBJECT_AND_VARIABLE_REQUESTS)
    def test_is_registered_with_workflow_scope(self, engine: Engine, request_type: type[RequestPayload]) -> None:
        assert engine.event_manager.get_scope_for_request_type(request_type) is RequestScope.WORKFLOW

    def test_a_variable_is_created_in_the_workflow_the_envelope_names(self, engine: Engine) -> None:
        a, b = _open_two(engine)

        result = engine.event_manager.handle_request(
            CreateVariableRequest(name="greeting", type="str", is_global=True, value="hi"),
            result_context=ResultContext(open_workflow_id=b.workflow_id),
        ).result

        assert isinstance(result, CreateVariableResultSuccess)
        assert b.variables.global_layer.has("greeting")
        assert not a.variables.global_layer.has("greeting")

    def test_a_variable_request_for_a_workflow_that_is_not_open_changes_nothing(self, engine: Engine) -> None:
        workflow = _open_workflow(engine)

        result = engine.event_manager.handle_request(
            CreateVariableRequest(name="greeting", type="str", is_global=True, value="hi"),
            result_context=ResultContext(open_workflow_id=NOT_OPEN_ID),
        ).result

        assert isinstance(result, WorkflowNotOpenResultFailure)
        assert not workflow.variables.global_layer.has("greeting")

    def test_naming_no_workflow_while_several_are_open_reaches_the_requests_own_failure_subscribers(
        self, engine: Engine
    ) -> None:
        """The editor undoes an optimistic edit on the request's failure type, which the bridge reads from `request_type`."""
        a, b = _open_two(engine)

        result_event = engine.event_manager.handle_request(
            CreateVariableRequest(name="greeting", type="str", is_global=True, value="hi")
        )

        assert isinstance(result_event.result, WorkflowNotNamedResultFailure)
        assert "2 workflows are open" in str(result_event.result.result_details)
        sent = json.loads(converters.engine.dumps_result(result_event).text)
        assert sent["request_type"] == "CreateVariableRequest"
        assert not a.variables.global_layer.has("greeting")
        assert not b.variables.global_layer.has("greeting")


class _FlippingRemoteHandler(RemoteHandler):
    """Answers `forwards_now` differently each time, as the process-wide counter it reads can between two reads.

    Dispatch must ask once and act on that answer.
    """

    def __init__(self, original: Any, engine: Engine, *, forwards_first: bool) -> None:
        super().__init__(original=original, event_manager=engine.event_manager)
        self.forwards_first = forwards_first
        self.forwards_now_calls = 0
        self.forwarded: list[RequestPayload] = []

    def forwards_now(self) -> bool:
        self.forwards_now_calls += 1
        if self.forwards_now_calls == 1:
            return self.forwards_first
        return not self.forwards_first

    async def forward(self, request: RequestPayload) -> ResultPayload:
        self.forwarded.append(request)
        return _ScopeProbeResult(result_details="forwarded")


class TestForwardingDecidedOnce:
    """Dispatch asks a worker's forwarding handler once whether it forwards, then acts on that answer alone."""

    @staticmethod
    def _install(engine: Engine, calls: _Calls, *, forwards_first: bool) -> _FlippingRemoteHandler:
        event_manager = engine.event_manager
        _register(engine, calls, RequestScope.WORKFLOW, _ScopeProbeRequest)
        original = event_manager.get_manager_for_request_type(_ScopeProbeRequest)
        handler = _FlippingRemoteHandler(original, engine, forwards_first=forwards_first)
        event_manager.replace_manager_for_request_type(_ScopeProbeRequest, handler)
        return handler

    @pytest.mark.asyncio
    async def test_a_forwarded_request_resolves_no_workflow_here(self, engine: Engine, calls: _Calls) -> None:
        """A worker opens no workflow, so resolving one here would fail a request the orchestrator can answer."""
        handler = self._install(engine, calls, forwards_first=True)

        result_event = await engine.event_manager.ahandle_request(_ScopeProbeRequest())

        assert result_event.succeeded()
        assert len(handler.forwarded) == 1
        assert handler.forwards_now_calls == 1
        assert calls.workflows == []

    @pytest.mark.asyncio
    async def test_a_local_request_gets_its_workflow(self, engine: Engine, calls: _Calls) -> None:
        workflow = _open_workflow(engine)
        handler = self._install(engine, calls, forwards_first=False)

        result_event = await engine.event_manager.ahandle_request(
            _ScopeProbeRequest(), result_context=ResultContext(open_workflow_id=workflow.workflow_id)
        )

        assert result_event.succeeded()
        assert handler.forwarded == []
        assert handler.forwards_now_calls == 1
        assert calls.workflows == [workflow]

    def test_the_sync_path_decides_once_too(self, engine: Engine, calls: _Calls) -> None:
        handler = self._install(engine, calls, forwards_first=True)

        result_event = engine.event_manager.handle_request(_ScopeProbeRequest())

        assert result_event.succeeded()
        assert len(handler.forwarded) == 1
        assert handler.forwards_now_calls == 1
        assert calls.workflows == []


class TestAddressingFailures:
    def test_post_dispatch_hooks_see_an_addressing_failure(self, engine: Engine, calls: _Calls) -> None:
        _register(engine, calls, RequestScope.WORKFLOW, _ScopeProbeRequest)
        seen: list[ResultPayload] = []

        def hook(request: RequestPayload, result: ResultPayload) -> None:  # noqa: ARG001
            seen.append(result)

        engine.event_manager.add_post_dispatch_hook(_ScopeProbeRequest, hook)
        try:
            result_event = engine.event_manager.handle_request(
                _ScopeProbeRequest(), result_context=ResultContext(open_workflow_id=NOT_OPEN_ID)
            )
        finally:
            engine.event_manager.remove_post_dispatch_hook(_ScopeProbeRequest, hook)

        assert seen == [result_event.result]
        assert isinstance(result_event.result, WorkflowNotOpenResultFailure)


class TestRemoteHandlerOutOfNodeExecution:
    def test_on_its_sync_local_path_it_passes_the_workflow_through(self, engine: Engine, calls: _Calls) -> None:
        event_manager = engine.event_manager
        workflow = _open_workflow(engine)
        _register(engine, calls, RequestScope.WORKFLOW, _ScopeProbeRequest)
        original = event_manager.get_manager_for_request_type(_ScopeProbeRequest)
        event_manager.replace_manager_for_request_type(
            _ScopeProbeRequest, RemoteHandler(original=original, event_manager=event_manager)
        )

        result_event = event_manager.handle_request(
            _ScopeProbeRequest(), result_context=ResultContext(open_workflow_id=workflow.workflow_id)
        )

        assert result_event.succeeded()
        assert calls.workflows == [workflow]


class TestHostWorkflowReads:
    """The autouse fixture records a HOST handler that reads the current workflow while one is open."""

    def test_a_host_handler_reading_the_current_workflow_is_recorded(
        self, engine: Engine, fail_on_host_workflow_reads: list[HostWorkflowRead]
    ) -> None:
        _open_workflow(engine)

        def host_handler(request: _ScopeProbeRequest) -> ResultPayload:  # noqa: ARG001
            engine.current_workflow_or_none()
            return _ScopeProbeResult(result_details="ran")

        engine.event_manager.assign_manager_to_request_type(_ScopeProbeRequest, host_handler)

        engine.handle_request(_ScopeProbeRequest())

        assert fail_on_host_workflow_reads == [
            HostWorkflowRead(request_type_name="_ScopeProbeRequest", read="read the current workflow")
        ]
        fail_on_host_workflow_reads.clear()

    def test_a_read_in_an_unscoped_request_a_host_handler_sends_is_not(
        self, engine: Engine, fail_on_host_workflow_reads: list[HostWorkflowRead]
    ) -> None:
        """An unmigrated handler's reads are its own debt, not a dependence of the HOST handler that sent it."""
        _open_workflow(engine)

        def ambient_handler(request: _InnerProbeRequest) -> ResultPayload:  # noqa: ARG001
            engine.current_workflow_or_none()
            return _ScopeProbeResult(result_details="ran")

        def host_handler(request: _ScopeProbeRequest) -> ResultPayload:  # noqa: ARG001
            return engine.handle_request(_InnerProbeRequest())

        engine.event_manager.assign_manager_to_request_type(_InnerProbeRequest, ambient_handler, without_workflow=None)
        engine.event_manager.assign_manager_to_request_type(_ScopeProbeRequest, host_handler)

        engine.handle_request(_ScopeProbeRequest())

        assert fail_on_host_workflow_reads == []

    def test_a_workflow_handler_reading_the_current_workflow_is_not(
        self, engine: Engine, fail_on_host_workflow_reads: list[HostWorkflowRead]
    ) -> None:
        _open_workflow(engine)

        def workflow_handler(request: _ScopeProbeRequest, workflow: WorkflowRuntime) -> ResultPayload:  # noqa: ARG001
            engine.current_workflow_or_none()
            return _ScopeProbeResult(result_details="ran")

        engine.event_manager.assign_manager_to_request_type(_ScopeProbeRequest, workflow_handler)

        engine.handle_request(_ScopeProbeRequest())

        assert fail_on_host_workflow_reads == []


class TestAmbientUntilMigrated:
    """Each entry is a request type whose engine handler still reads the ambient workflow."""

    @pytest.mark.parametrize("name", sorted(AMBIENT_UNTIL_MIGRATED))
    def test_names_a_request_type(self, name: str) -> None:
        request_type = PayloadRegistry.get_type(name)

        assert request_type is not None
        assert issubclass(request_type, RequestPayload)

    @pytest.mark.parametrize("name", sorted(AMBIENT_UNTIL_MIGRATED))
    def test_its_handler_takes_no_workflow(self, engine: Engine, name: str) -> None:
        """A handler that takes a workflow is migrated, so its entry should be removed."""
        request_type = PayloadRegistry.get_type(name)
        assert request_type is not None
        assert issubclass(request_type, RequestPayload)
        handler = engine.event_manager.get_manager_for_request_type(request_type)
        if handler is None:
            pytest.skip(f"{name} is registered only in a worker.")

        assert not isinstance(handler, ForwardingHandler)
        assert scope_from_signature(handler) is None


# Entry points that open, run into or report the open workflow from the ambient binding, so they stay in
# `AMBIENT_UNTIL_MIGRATED`. They get a scope once entry points bind or close workflows by id.
_ENTRY_POINTS_LEFT_UNSCOPED = {
    "GetWorkflowContextRequest",
    "RunWorkflowFromRegistryRequest",
    "RunWorkflowFromScratchRequest",
    "RunWorkflowWithCurrentStateRequest",
    "SetWorkflowContextRequest",
}


class TestEveryEngineHandlerDeclaresAScope:
    """Library handlers registered at library load (`module_loading.py`) are not covered: none load here."""

    def test_every_handler_the_engine_registers_declares_a_scope(self, engine: Engine) -> None:
        event_manager = engine.event_manager

        undeclared = {
            request_type.__name__
            for request_type in event_manager.registered_request_types()
            if event_manager.get_scope_for_request_type(request_type) is None
        }

        assert undeclared == _ENTRY_POINTS_LEFT_UNSCOPED

    def test_only_the_entry_points_keep_the_ambient_workflow(self) -> None:
        assert _ENTRY_POINTS_LEFT_UNSCOPED == AMBIENT_UNTIL_MIGRATED

    def test_every_broadcast_handler_a_worker_registers_declares_host_scope(self) -> None:
        event_manager = EventManager()
        register_broadcast_handlers(
            event_manager, config_manager=MagicMock(), secrets_manager=MagicMock(), project_manager=MagicMock()
        )

        scopes = {
            request_type.__name__: event_manager.get_scope_for_request_type(request_type)
            for request_type in event_manager.registered_request_types()
        }

        assert scopes
        assert set(scopes.values()) == {RequestScope.HOST}


# Host-state handlers that read a node, or tag a call, in the workflow they are handed when there is one.
OPTIONAL_WORKFLOW_HOST_STATE_REQUESTS = [
    DeclareModelInvocationRequest,
    BudgetAccessRequest,
    ReportUsageRequest,
]


class TestHostStateRequestsThatTakeAWorkflow:
    @pytest.mark.parametrize("request_type", OPTIONAL_WORKFLOW_HOST_STATE_REQUESTS)
    def test_is_registered_with_optional_workflow_scope(
        self, engine: Engine, request_type: type[RequestPayload]
    ) -> None:
        assert engine.event_manager.get_scope_for_request_type(request_type) is RequestScope.OPTIONAL_WORKFLOW
