"""Dispatch hands a handler the workflow its scope takes, from the request envelope's workflow id."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest
import pytest_asyncio

from griptape_nodes.app.worker_routing import RemoteHandler, register_broadcast_handlers, register_remote_handlers
from griptape_nodes.retained_mode.events import converters
from griptape_nodes.retained_mode.events.access_events import QueryModelAccessForNodeRequest
from griptape_nodes.retained_mode.events.app_events import EngineHeartbeatRequest
from griptape_nodes.retained_mode.events.base_events import (
    EventRequest,
    EventResultSuccess,
    RequestPayload,
    ResultPayload,
    ResultPayloadFailure,
    ResultPayloadSuccess,
)
from griptape_nodes.retained_mode.events.budget_events import BudgetAccessRequest, ReportUsageRequest
from griptape_nodes.retained_mode.events.connection_events import (
    CreateConnectionRequest,
    CreateConnectionResultFailure,
    DeleteConnectionRequest,
    DeleteConnectionResultFailure,
)
from griptape_nodes.retained_mode.events.context_events import (
    GetWorkflowContextRequest,
    GetWorkflowContextSuccess,
    SetWorkflowContextRequest,
)
from griptape_nodes.retained_mode.events.diagnostics_events import CollectDiagnosticsRequest
from griptape_nodes.retained_mode.events.execution_events import StartFlowRequest
from griptape_nodes.retained_mode.events.generic_events import GenericResultFailure
from griptape_nodes.retained_mode.events.model_events import DeclareModelInvocationRequest
from griptape_nodes.retained_mode.events.object_events import (
    ClearAllObjectStateRequest,
    ClearAllObjectStateResultSuccess,
    RenameObjectRequest,
)
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
    ListAllWorkflowsRequest,
    RunWorkflowFromRegistryRequest,
    RunWorkflowFromScratchRequest,
    RunWorkflowWithCurrentStateRequest,
    SetVariableSubstitutionEnabledRequest,
)
from griptape_nodes.retained_mode.managers.event_manager import EventManager, ResultContext
from griptape_nodes.retained_mode.request_handlers import RequestScope
from griptape_nodes.utils.async_utils import call_function
from tests.workflow_context import push_unsaved_workflow, without_sole_workflow_fallback

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.runtime.workflow_runtime import WorkflowRuntime

NOT_OPEN_ID = "0000000000000000000000000000dead"


@dataclass(kw_only=True)
class _ScopeProbeRequest(RequestPayload):
    pass


@dataclass(kw_only=True)
class _ScopeProbeResult(ResultPayloadSuccess):
    workflow: WorkflowRuntime | None = None


@dataclass(kw_only=True)
class _DetailedProbeRequest(RequestPayload):
    pass


@dataclass(kw_only=True)
class _DetailedProbeResultFailure(ResultPayloadFailure):
    """A request's own failure type that needs more than `result_details`."""

    reason_code: int


class _Calls:
    """Records what each probe handler was called with."""

    def __init__(self) -> None:
        self.workflows: list[WorkflowRuntime | None] = []
        self.calls = 0

    def workflow_handler(self, request: _ScopeProbeRequest, workflow: WorkflowRuntime) -> ResultPayload:  # noqa: ARG002
        self.workflows.append(workflow)
        return _ScopeProbeResult(workflow=workflow, result_details="ran")

    async def optional_handler(
        self,
        request: _ScopeProbeRequest,  # noqa: ARG002
        workflow: WorkflowRuntime | None = None,
    ) -> ResultPayload:
        self.workflows.append(workflow)
        return _ScopeProbeResult(workflow=workflow, result_details="ran")

    def unscoped_handler(self, request: _ScopeProbeRequest) -> ResultPayload:  # noqa: ARG002
        self.calls += 1
        return _ScopeProbeResult(result_details="ran")


@pytest.fixture
def calls() -> _Calls:
    """A fresh record of probe handler calls."""
    return _Calls()


def _register(engine: Engine, calls: _Calls, scope: RequestScope | None) -> None:
    event_manager = engine.event_manager
    match scope:
        case RequestScope.WORKFLOW:
            event_manager.assign_manager_to_request_type(_ScopeProbeRequest, calls.workflow_handler, scope=scope)
        case RequestScope.OPTIONAL_WORKFLOW:
            event_manager.assign_manager_to_request_type(_ScopeProbeRequest, calls.optional_handler, scope=scope)
        case _:
            event_manager.assign_manager_to_request_type(_ScopeProbeRequest, calls.unscoped_handler, scope=scope)


class TestWorkflowScopes:
    @pytest.mark.parametrize("scope", [RequestScope.WORKFLOW, RequestScope.OPTIONAL_WORKFLOW])
    @pytest.mark.asyncio
    async def test_the_envelope_id_names_the_workflow_passed(
        self, engine: Engine, calls: _Calls, scope: RequestScope
    ) -> None:
        workflow = push_unsaved_workflow(engine)
        _register(engine, calls, scope)

        result_event = await engine.event_manager.ahandle_request(
            _ScopeProbeRequest(), workflow_id=workflow.workflow_id
        )

        assert result_event.succeeded()
        assert calls.workflows == [engine.open_workflows.get(workflow.workflow_id)]

    @pytest.mark.parametrize("scope", [RequestScope.WORKFLOW, RequestScope.OPTIONAL_WORKFLOW])
    @pytest.mark.asyncio
    async def test_an_envelope_id_that_is_not_open_fails(
        self, engine: Engine, calls: _Calls, scope: RequestScope
    ) -> None:
        push_unsaved_workflow(engine)
        _register(engine, calls, scope)

        result_event = await engine.event_manager.ahandle_request(_ScopeProbeRequest(), workflow_id=NOT_OPEN_ID)

        assert isinstance(result_event.result, GenericResultFailure)
        assert str(result_event.result.result_details) == (
            f"Attempted to run _ScopeProbeRequest in workflow '{NOT_OPEN_ID}'. Failed because that workflow isn't open."
        )
        assert result_event.workflow_id == NOT_OPEN_ID
        assert calls.workflows == []

    @pytest.mark.asyncio
    async def test_an_addressing_failure_logs_at_the_request_s_failure_log_level(
        self, engine: Engine, calls: _Calls, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A caller that expects the miss can quiet it, as for any failure its handler returns."""
        _register(engine, calls, RequestScope.WORKFLOW)

        with caplog.at_level(logging.DEBUG, logger="griptape_nodes"):
            await engine.event_manager.ahandle_request(
                _ScopeProbeRequest(failure_log_level=logging.DEBUG), workflow_id=NOT_OPEN_ID
            )

        records = [record for record in caplog.records if "that workflow isn't open" in record.getMessage()]
        assert [record.levelno for record in records] == [logging.DEBUG]

    def test_post_dispatch_hooks_see_an_addressing_failure(self, engine: Engine, calls: _Calls) -> None:
        _register(engine, calls, RequestScope.WORKFLOW)
        seen: list[ResultPayload] = []

        def hook(request: RequestPayload, result: ResultPayload) -> None:  # noqa: ARG001
            seen.append(result)

        engine.event_manager.add_post_dispatch_hook(_ScopeProbeRequest, hook)
        try:
            result_event = engine.event_manager.handle_request(_ScopeProbeRequest(), workflow_id=NOT_OPEN_ID)
        finally:
            engine.event_manager.remove_post_dispatch_hook(_ScopeProbeRequest, hook)

        assert seen == [result_event.result]
        assert isinstance(result_event.result, GenericResultFailure)

    @pytest.mark.parametrize("scope", [RequestScope.WORKFLOW, RequestScope.OPTIONAL_WORKFLOW])
    @pytest.mark.asyncio
    async def test_no_envelope_id_gets_the_open_workflow(
        self, engine: Engine, calls: _Calls, scope: RequestScope
    ) -> None:
        workflow = push_unsaved_workflow(engine)
        _register(engine, calls, scope)

        result_event = await engine.event_manager.ahandle_request(_ScopeProbeRequest())

        assert result_event.succeeded()
        assert calls.workflows == [engine.open_workflows.get(workflow.workflow_id)]

    @pytest.mark.asyncio
    async def test_workflow_scope_with_no_envelope_id_and_none_open_fails(self, engine: Engine, calls: _Calls) -> None:
        _register(engine, calls, RequestScope.WORKFLOW)

        result_event = await engine.event_manager.ahandle_request(_ScopeProbeRequest())

        assert isinstance(result_event.result, GenericResultFailure)
        assert "names no workflow and none is open" in str(result_event.result.result_details)
        assert calls.workflows == []

    @pytest.mark.asyncio
    async def test_optional_workflow_scope_with_no_envelope_id_and_none_open_gets_none(
        self, engine: Engine, calls: _Calls
    ) -> None:
        _register(engine, calls, RequestScope.OPTIONAL_WORKFLOW)

        result_event = await engine.event_manager.ahandle_request(_ScopeProbeRequest())

        assert result_event.succeeded()
        assert calls.workflows == [None]

    def test_the_sync_path_passes_the_workflow_too(self, engine: Engine, calls: _Calls) -> None:
        workflow = push_unsaved_workflow(engine)
        _register(engine, calls, RequestScope.OPTIONAL_WORKFLOW)

        result_event = engine.event_manager.handle_request(_ScopeProbeRequest(), workflow_id=workflow.workflow_id)

        assert result_event.succeeded()
        assert calls.workflows == [engine.open_workflows.get(workflow.workflow_id)]

    def test_the_sync_path_fails_an_envelope_id_that_is_not_open(self, engine: Engine, calls: _Calls) -> None:
        _register(engine, calls, RequestScope.WORKFLOW)

        result_event = engine.event_manager.handle_request(_ScopeProbeRequest(), workflow_id=NOT_OPEN_ID)

        assert isinstance(result_event.result, GenericResultFailure)
        assert calls.workflows == []


class TestAddressingFailureType:
    """A request that names no workflow it can act on fails with its own failure type where it can."""

    @staticmethod
    def _connection_request(**kwargs: Any) -> CreateConnectionRequest:
        return CreateConnectionRequest(
            source_node_name="a",
            source_parameter_name="out",
            target_node_name="b",
            target_parameter_name="in",
            **kwargs,
        )

    @pytest.mark.parametrize(
        ("request_type", "failure_type"),
        [
            (CreateConnectionRequest, CreateConnectionResultFailure),
            (DeleteConnectionRequest, DeleteConnectionResultFailure),
        ],
    )
    @pytest.mark.asyncio
    async def test_a_connection_request_fails_with_its_own_failure_type(
        self, engine: Engine, request_type: type, failure_type: type
    ) -> None:
        push_unsaved_workflow(engine)
        request = request_type(
            source_node_name="a", source_parameter_name="out", target_node_name="b", target_parameter_name="in"
        )

        result_event = await engine.event_manager.ahandle_request(request, workflow_id=NOT_OPEN_ID)

        assert type(result_event.result) is failure_type
        assert "that workflow isn't open" in str(result_event.result.result_details)
        assert result_event.workflow_id == NOT_OPEN_ID

    def test_the_sync_path_uses_the_request_s_own_failure_type_too(self, engine: Engine) -> None:
        result_event = engine.event_manager.handle_request(self._connection_request())

        assert type(result_event.result) is CreateConnectionResultFailure
        assert "names no workflow and none is open" in str(result_event.result.result_details)

    def test_its_own_failure_type_logs_at_the_request_s_failure_log_level(
        self, engine: Engine, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.DEBUG, logger="griptape_nodes"):
            engine.event_manager.handle_request(
                self._connection_request(failure_log_level=logging.DEBUG), workflow_id=NOT_OPEN_ID
            )

        records = [record for record in caplog.records if "that workflow isn't open" in record.getMessage()]
        assert [record.levelno for record in records] == [logging.DEBUG]

    def test_a_failure_type_that_needs_more_than_details_falls_back_to_generic(
        self, engine: Engine, calls: _Calls
    ) -> None:
        engine.event_manager.assign_manager_to_request_type(
            _DetailedProbeRequest, calls.workflow_handler, scope=RequestScope.WORKFLOW
        )

        result_event = engine.event_manager.handle_request(_DetailedProbeRequest(), workflow_id=NOT_OPEN_ID)

        assert type(result_event.result) is GenericResultFailure
        assert "that workflow isn't open" in str(result_event.result.result_details)

    def test_start_flow_whose_failure_type_needs_validation_exceptions_falls_back_to_generic(
        self, engine: Engine
    ) -> None:
        result_event = engine.event_manager.handle_request(
            StartFlowRequest(flow_name="canvas"), workflow_id=NOT_OPEN_ID
        )

        assert type(result_event.result) is GenericResultFailure


class TestHostAndUndeclaredScopes:
    @pytest.mark.parametrize("scope", [RequestScope.HOST, None])
    @pytest.mark.asyncio
    async def test_an_envelope_id_that_is_not_open_is_never_rejected(
        self, engine: Engine, calls: _Calls, scope: RequestScope | None
    ) -> None:
        _register(engine, calls, scope)

        result_event = await engine.event_manager.ahandle_request(_ScopeProbeRequest(), workflow_id=NOT_OPEN_ID)

        assert result_event.succeeded()
        assert result_event.workflow_id == NOT_OPEN_ID
        assert calls.calls == 1

    @pytest.mark.asyncio
    async def test_host_scope_ignores_the_envelope_id_silently(
        self, engine: Engine, calls: _Calls, caplog: pytest.LogCaptureFixture
    ) -> None:
        _register(engine, calls, RequestScope.HOST)

        with caplog.at_level(logging.DEBUG, logger="griptape_nodes"):
            await engine.event_manager.ahandle_request(_ScopeProbeRequest(), workflow_id=NOT_OPEN_ID)

        assert not [record for record in caplog.records if NOT_OPEN_ID in record.getMessage()]

    @pytest.mark.asyncio
    async def test_an_undeclared_scope_logs_an_id_other_than_the_open_workflow(
        self, engine: Engine, calls: _Calls, caplog: pytest.LogCaptureFixture
    ) -> None:
        push_unsaved_workflow(engine)
        _register(engine, calls, None)

        with caplog.at_level(logging.DEBUG, logger="griptape_nodes"):
            await engine.event_manager.ahandle_request(_ScopeProbeRequest(), workflow_id=NOT_OPEN_ID)

        mismatches = [record for record in caplog.records if NOT_OPEN_ID in record.getMessage()]
        assert [record.levelno for record in mismatches] == [logging.DEBUG]

    @pytest.mark.asyncio
    async def test_an_undeclared_scope_logs_nothing_for_the_open_workflow(
        self, engine: Engine, calls: _Calls, caplog: pytest.LogCaptureFixture
    ) -> None:
        workflow = push_unsaved_workflow(engine)
        _register(engine, calls, None)

        with caplog.at_level(logging.DEBUG, logger="griptape_nodes"):
            await engine.event_manager.ahandle_request(_ScopeProbeRequest(), workflow_id=workflow.workflow_id)

        assert not [record for record in caplog.records if "was addressed to workflow" in record.getMessage()]


class TestForwardedRequests:
    """On a worker, which opens no workflow, a forwarded request is resolved where it is forwarded to."""

    @pytest.mark.asyncio
    async def test_a_workflow_request_forwarded_from_node_code_does_not_fail_at_dispatch(
        self, engine: Engine, calls: _Calls
    ) -> None:
        event_manager = engine.event_manager
        _register(engine, calls, RequestScope.WORKFLOW)
        forwarded: list[RequestPayload] = []

        async def fake_forward(request: RequestPayload, result_context: ResultContext) -> EventResultSuccess:  # noqa: ARG001
            forwarded.append(request)
            return EventResultSuccess(request=request, result=_ScopeProbeResult(result_details="forwarded"))

        event_manager.forward_to_orchestrator = fake_forward  # type: ignore[method-assign]
        original = event_manager.get_manager_for_request_type(_ScopeProbeRequest)
        event_manager.replace_manager_for_request_type(
            _ScopeProbeRequest, RemoteHandler(original=original, event_manager=event_manager)
        )

        with event_manager.node_execution_scope():
            result_event = await event_manager.ahandle_request(_ScopeProbeRequest())

        assert result_event.succeeded()
        assert len(forwarded) == 1
        assert calls.workflows == []

    @pytest.mark.asyncio
    async def test_outside_node_execution_it_takes_the_local_path_and_fails_at_dispatch(
        self, engine: Engine, calls: _Calls
    ) -> None:
        event_manager = engine.event_manager
        _register(engine, calls, RequestScope.WORKFLOW)
        original = event_manager.get_manager_for_request_type(_ScopeProbeRequest)
        event_manager.replace_manager_for_request_type(
            _ScopeProbeRequest, RemoteHandler(original=original, event_manager=event_manager)
        )

        result_event = await event_manager.ahandle_request(_ScopeProbeRequest())

        assert isinstance(result_event.result, GenericResultFailure)
        assert "names no workflow" in str(result_event.result.result_details)
        assert calls.workflows == []

    @pytest.mark.asyncio
    async def test_on_its_local_path_the_wrapper_passes_the_workflow_through(
        self, engine: Engine, calls: _Calls
    ) -> None:
        event_manager = engine.event_manager
        workflow = push_unsaved_workflow(engine)
        _register(engine, calls, RequestScope.WORKFLOW)
        original = event_manager.get_manager_for_request_type(_ScopeProbeRequest)
        event_manager.replace_manager_for_request_type(
            _ScopeProbeRequest, RemoteHandler(original=original, event_manager=event_manager)
        )

        result_event = await event_manager.ahandle_request(_ScopeProbeRequest(), workflow_id=workflow.workflow_id)

        assert result_event.succeeded()
        assert calls.workflows == [engine.open_workflows.get(workflow.workflow_id)]

    def test_on_its_sync_local_path_the_wrapper_passes_the_workflow_through(
        self, engine: Engine, calls: _Calls
    ) -> None:
        event_manager = engine.event_manager
        workflow = push_unsaved_workflow(engine)
        _register(engine, calls, RequestScope.WORKFLOW)
        original = event_manager.get_manager_for_request_type(_ScopeProbeRequest)
        event_manager.replace_manager_for_request_type(
            _ScopeProbeRequest, RemoteHandler(original=original, event_manager=event_manager)
        )

        result_event = event_manager.handle_request(_ScopeProbeRequest(), workflow_id=workflow.workflow_id)

        assert result_event.succeeded()
        assert calls.workflows == [engine.open_workflows.get(workflow.workflow_id)]


class _FlippingForwardingHandler:
    """Answers `forwards_now` differently on each call, as a process-wide flag read twice can.

    Dispatch must ask once and act on that answer, so it never calls `__call__`, which would ask again.
    """

    def __init__(self, original: Any, *, forwards_first: bool) -> None:
        self.original = original
        self.forwards_first = forwards_first
        self.forwards_now_calls = 0
        self.forwarded: list[RequestPayload] = []

    async def __call__(self, request: RequestPayload, **kwargs: Any) -> ResultPayload:  # noqa: ARG002
        msg = "Dispatch decides once whether to forward, so it calls forward or handle_locally."
        raise AssertionError(msg)

    def forwards_now(self) -> bool:
        self.forwards_now_calls += 1
        if self.forwards_now_calls == 1:
            return self.forwards_first
        return not self.forwards_first

    async def forward(self, request: RequestPayload) -> ResultPayload:
        self.forwarded.append(request)
        return _ScopeProbeResult(result_details="forwarded")

    async def handle_locally(self, request: RequestPayload, **kwargs: Any) -> ResultPayload:
        return await call_function(self.original, request, **kwargs)


class TestForwardingDecidedOnce:
    """Dispatch asks a forwarding handler once whether it forwards, then acts on that answer alone."""

    def _install(self, engine: Engine, calls: _Calls, *, forwards_first: bool) -> _FlippingForwardingHandler:
        event_manager = engine.event_manager
        _register(engine, calls, RequestScope.WORKFLOW)
        original = event_manager.get_manager_for_request_type(_ScopeProbeRequest)
        handler = _FlippingForwardingHandler(original, forwards_first=forwards_first)
        event_manager.replace_manager_for_request_type(_ScopeProbeRequest, handler)
        return handler

    @pytest.mark.asyncio
    async def test_a_forwarded_request_is_forwarded_even_if_the_answer_flips(
        self, engine: Engine, calls: _Calls
    ) -> None:
        handler = self._install(engine, calls, forwards_first=True)

        result_event = await engine.event_manager.ahandle_request(_ScopeProbeRequest())

        assert result_event.succeeded()
        assert len(handler.forwarded) == 1
        assert handler.forwards_now_calls == 1
        assert calls.workflows == []

    @pytest.mark.asyncio
    async def test_a_local_request_gets_its_workflow_even_if_the_answer_flips(
        self, engine: Engine, calls: _Calls
    ) -> None:
        workflow = push_unsaved_workflow(engine)
        handler = self._install(engine, calls, forwards_first=False)

        result_event = await engine.event_manager.ahandle_request(
            _ScopeProbeRequest(), workflow_id=workflow.workflow_id
        )

        assert result_event.succeeded()
        assert handler.forwarded == []
        assert handler.forwards_now_calls == 1
        assert calls.workflows == [engine.open_workflows.get(workflow.workflow_id)]

    def test_the_sync_path_decides_once_too(self, engine: Engine, calls: _Calls) -> None:
        handler = self._install(engine, calls, forwards_first=True)

        result_event = engine.event_manager.handle_request(_ScopeProbeRequest())

        assert result_event.succeeded()
        assert len(handler.forwarded) == 1
        assert handler.forwards_now_calls == 1
        assert calls.workflows == []


class TestNestedRequests:
    """A runtime's requests name that runtime's workflow on their envelope."""

    @pytest.mark.asyncio
    async def test_a_runtime_addresses_its_requests_to_its_workflow(self, engine: Engine, calls: _Calls) -> None:
        workflow = engine.open_workflows.get(push_unsaved_workflow(engine).workflow_id)
        _register(engine, calls, RequestScope.WORKFLOW)

        sync_result = workflow.handle_request(_ScopeProbeRequest())
        async_result = await workflow.ahandle_request(_ScopeProbeRequest())

        assert sync_result.succeeded()
        assert async_result.succeeded()
        assert calls.workflows == [workflow, workflow]

    @pytest.mark.asyncio
    async def test_a_closed_runtime_does_not_reach_the_open_workflow(self, engine: Engine, calls: _Calls) -> None:
        """Its requests name its own workflow, so they never fall through to whichever one is open."""
        closed = engine.open_workflows.get(push_unsaved_workflow(engine).workflow_id)
        await engine.close_workflow_state(closed)
        push_unsaved_workflow(engine)
        _register(engine, calls, RequestScope.WORKFLOW)

        result = await closed.ahandle_request(_ScopeProbeRequest())

        assert isinstance(result, GenericResultFailure)
        assert f"in workflow '{closed.workflow_id}'" in str(result.result_details)
        assert calls.workflows == []


class TestWorkflowBinding:
    """A request with no envelope id acts on the workflow `Engine.bind_workflow` bound, before any fallback."""

    @pytest.mark.parametrize("scope", [RequestScope.WORKFLOW, RequestScope.OPTIONAL_WORKFLOW])
    @pytest.mark.asyncio
    async def test_the_binding_names_the_workflow_passed_before_the_fallback(
        self, engine: Engine, calls: _Calls, scope: RequestScope
    ) -> None:
        workflow = engine.open_workflows.get(push_unsaved_workflow(engine).workflow_id)
        _register(engine, calls, scope)

        with (
            patch("griptape_nodes.retained_mode.managers.event_manager._workflow_for_unaddressed_request") as fallback,
            engine.bind_workflow(workflow),
        ):
            result_event = await engine.event_manager.ahandle_request(_ScopeProbeRequest())

        assert result_event.succeeded()
        assert calls.workflows == [workflow]
        fallback.assert_not_called()

    def test_the_sync_path_uses_the_binding_too(self, engine: Engine, calls: _Calls) -> None:
        workflow = engine.open_workflows.get(push_unsaved_workflow(engine).workflow_id)
        _register(engine, calls, RequestScope.WORKFLOW)

        with without_sole_workflow_fallback(), engine.bind_workflow(workflow):
            result_event = engine.event_manager.handle_request(_ScopeProbeRequest())

        assert result_event.succeeded()
        assert calls.workflows == [workflow]

    @pytest.mark.asyncio
    async def test_the_envelope_id_wins_over_the_binding(self, engine: Engine, calls: _Calls) -> None:
        workflow = engine.open_workflows.get(push_unsaved_workflow(engine).workflow_id)
        _register(engine, calls, RequestScope.WORKFLOW)

        with engine.bind_workflow(workflow):
            result_event = await engine.event_manager.ahandle_request(_ScopeProbeRequest(), workflow_id=NOT_OPEN_ID)

        assert isinstance(result_event.result, GenericResultFailure)
        assert f"in workflow '{NOT_OPEN_ID}'" in str(result_event.result.result_details)
        assert calls.workflows == []

    @pytest.mark.parametrize("scope", [RequestScope.WORKFLOW, RequestScope.OPTIONAL_WORKFLOW])
    @pytest.mark.asyncio
    async def test_a_binding_to_a_closed_workflow_fails_like_a_stale_envelope_id(
        self, engine: Engine, calls: _Calls, scope: RequestScope
    ) -> None:
        """It never falls through to the workflow open now."""
        closed = engine.open_workflows.get(push_unsaved_workflow(engine).workflow_id)
        await engine.close_workflow_state(closed)
        push_unsaved_workflow(engine)
        _register(engine, calls, scope)

        with engine.bind_workflow(closed):
            result_event = await engine.event_manager.ahandle_request(_ScopeProbeRequest())

        assert isinstance(result_event.result, GenericResultFailure)
        assert str(result_event.result.result_details) == (
            f"Attempted to run _ScopeProbeRequest in workflow '{closed.workflow_id}'. "
            "Failed because that workflow isn't open."
        )
        assert calls.workflows == []

    @pytest.mark.parametrize("scope", [RequestScope.HOST, None])
    @pytest.mark.asyncio
    async def test_host_and_undeclared_scopes_ignore_the_binding(
        self, engine: Engine, calls: _Calls, scope: RequestScope | None
    ) -> None:
        closed = engine.open_workflows.get(push_unsaved_workflow(engine).workflow_id)
        await engine.close_workflow_state(closed)
        _register(engine, calls, scope)

        with engine.bind_workflow(closed):
            result_event = await engine.event_manager.ahandle_request(_ScopeProbeRequest())

        assert result_event.succeeded()
        assert calls.calls == 1

    @pytest.mark.asyncio
    async def test_binding_none_runs_the_block_unbound(self, engine: Engine, calls: _Calls) -> None:
        workflow = engine.open_workflows.get(push_unsaved_workflow(engine).workflow_id)
        _register(engine, calls, RequestScope.OPTIONAL_WORKFLOW)

        with without_sole_workflow_fallback(), engine.bind_workflow(workflow), engine.bind_workflow(None):
            await engine.event_manager.ahandle_request(_ScopeProbeRequest())

        assert calls.workflows == [None]

    @pytest.mark.asyncio
    async def test_requests_from_tasks_and_to_thread_calls_made_in_the_block_carry_the_binding(
        self, engine: Engine, calls: _Calls
    ) -> None:
        workflow = engine.open_workflows.get(push_unsaved_workflow(engine).workflow_id)
        _register(engine, calls, RequestScope.WORKFLOW)

        with without_sole_workflow_fallback(), engine.bind_workflow(workflow):
            task = asyncio.create_task(engine.ahandle_request(_ScopeProbeRequest()))
            from_thread = await asyncio.to_thread(engine.handle_request, _ScopeProbeRequest())
            from_task = await task

        assert from_task.succeeded()
        assert from_thread.succeeded()
        assert calls.workflows == [workflow, workflow]

    def test_the_binding_ends_with_the_block(self, engine: Engine) -> None:
        workflow = engine.open_workflows.get(push_unsaved_workflow(engine).workflow_id)

        with engine.bind_workflow(workflow):
            assert engine.bound_workflow is workflow

        assert engine.bound_workflow is None


class _RecordingRequestClient:
    """Stands in for a worker's RequestClient: records what it is asked to send and answers with a failure."""

    def __init__(self) -> None:
        self.sent: list[EventRequest] = []

    async def request_to_orchestrator(
        self,
        event_request: EventRequest,
        orchestrator_request_topic: str,  # noqa: ARG002
        worker_response_topic: str,  # noqa: ARG002
        timeout_ms: int | None = None,  # noqa: ARG002
    ) -> dict[str, Any]:
        self.sent.append(event_request)
        return {
            "event_type": "EventResultFailure",
            "result_type": "GenericResultFailure",
            "result": {"result_details": "answered by the orchestrator"},
        }


class TestWorkerForwarding:
    """A worker names the orchestrator's workflow it mirrors on every request it forwards."""

    PEER_ID = "00000000000000000000000000000abc"

    @pytest_asyncio.fixture
    async def request_client(self, engine: Engine) -> _RecordingRequestClient:
        """Configure `engine` to forward as a worker does, through a recording client."""
        client = _RecordingRequestClient()
        engine.event_manager.configure_worker_forwarding(
            request_client=client,  # type: ignore[arg-type]
            orchestrator_request_topic="orchestrator",
            worker_response_topic="worker",
            websocket_event_loop=asyncio.get_running_loop(),
        )
        return client

    @pytest.mark.asyncio
    async def test_a_workflow_request_from_node_code_carries_the_mirrored_workflow_id(
        self, engine: Engine, calls: _Calls, request_client: _RecordingRequestClient
    ) -> None:
        event_manager = engine.event_manager
        engine.context_manager.mirror_workflow_context(self.PEER_ID, "peer", None, None)
        _register(engine, calls, RequestScope.WORKFLOW)
        register_remote_handlers(event_manager)

        with event_manager.node_execution_scope():
            result_event = await event_manager.ahandle_request(_ScopeProbeRequest())

        assert str(result_event.result.result_details) == "answered by the orchestrator"
        assert [(type(sent.request), sent.workflow_id) for sent in request_client.sent] == [
            (_ScopeProbeRequest, self.PEER_ID)
        ]
        assert calls.workflows == []

    @pytest.mark.asyncio
    async def test_a_host_request_it_forwards_is_never_rejected_by_the_orchestrator(
        self, engine: Engine, request_client: _RecordingRequestClient
    ) -> None:
        """The envelope names the mirrored workflow, which a host request ignores wherever it lands.

        The same engine stands in for the orchestrator once it stops mirroring, with a workflow of its
        own open that the mirrored id does not name.
        """
        engine.context_manager.mirror_workflow_context(self.PEER_ID, "peer", None, None)
        await engine.event_manager.forward_to_orchestrator(GetWorkflowContextRequest(), ResultContext())
        wire = converters.engine.dumps(request_client.sent[0])
        received = converters.engine.structure(json.loads(wire), EventRequest)
        engine.context_manager.mirror_workflow_context(None, None, None, None)
        open_workflow = push_unsaved_workflow(engine)

        result_event = await engine.event_manager.ahandle_request(received.request, workflow_id=received.workflow_id)

        assert received.workflow_id == self.PEER_ID
        assert isinstance(result_event.result, GetWorkflowContextSuccess)
        assert result_event.result.workflow_id == open_workflow.workflow_id
        assert result_event.workflow_id == self.PEER_ID

    @pytest.mark.asyncio
    async def test_with_no_mirrored_workflow_the_envelope_names_none(
        self, engine: Engine, request_client: _RecordingRequestClient
    ) -> None:
        await engine.event_manager.forward_to_orchestrator(_ScopeProbeRequest(), ResultContext())

        assert [sent.workflow_id for sent in request_client.sent] == [None]


# The editor sends these with no envelope id, or an id it has not confirmed open, because they
# report or replace what is open. QueryModelAccessForNodeRequest asks about the host's license.
# CollectDiagnosticsRequest reports on the engine, and has to work when the editor's id is stale.
HOST_REQUESTS = [
    GetWorkflowContextRequest,
    ListAllWorkflowsRequest,
    EngineHeartbeatRequest,
    SetWorkflowContextRequest,
    RunWorkflowFromRegistryRequest,
    RunWorkflowFromScratchRequest,
    RunWorkflowWithCurrentStateRequest,
    QueryModelAccessForNodeRequest,
    CollectDiagnosticsRequest,
]


class TestHostRequests:
    @pytest.mark.parametrize("request_type", HOST_REQUESTS)
    def test_is_registered_with_host_scope(self, engine: Engine, request_type: type[RequestPayload]) -> None:
        assert engine.event_manager.get_scope_for_request_type(request_type) is RequestScope.HOST

    def test_the_context_is_reported_for_an_id_that_is_not_open(self, engine: Engine) -> None:
        workflow = push_unsaved_workflow(engine)

        result_event = engine.event_manager.handle_request(GetWorkflowContextRequest(), workflow_id=NOT_OPEN_ID)

        assert isinstance(result_event.result, GetWorkflowContextSuccess)
        assert result_event.result.workflow_id == workflow.workflow_id


# Requests whose handlers act on the workflow the envelope names.
WORKFLOW_REQUESTS = [
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
    @pytest.mark.parametrize("request_type", WORKFLOW_REQUESTS)
    def test_is_registered_with_workflow_scope(self, engine: Engine, request_type: type[RequestPayload]) -> None:
        assert engine.event_manager.get_scope_for_request_type(request_type) is RequestScope.WORKFLOW

    def test_clearing_all_object_state_takes_an_optional_workflow(self, engine: Engine) -> None:
        scope = engine.event_manager.get_scope_for_request_type(ClearAllObjectStateRequest)

        assert scope is RequestScope.OPTIONAL_WORKFLOW

    @pytest.mark.asyncio
    async def test_clearing_all_object_state_succeeds_with_nothing_open(self, engine: Engine) -> None:
        result = await engine.ahandle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))

        assert isinstance(result, ClearAllObjectStateResultSuccess)

    @pytest.mark.asyncio
    async def test_clearing_all_object_state_closes_the_workflow_the_envelope_names(self, engine: Engine) -> None:
        workflow = push_unsaved_workflow(engine)

        result = await engine.ahandle_request(
            ClearAllObjectStateRequest(i_know_what_im_doing=True), workflow_id=workflow.workflow_id
        )

        assert isinstance(result, ClearAllObjectStateResultSuccess)
        assert not engine.open_workflows.is_open(workflow.workflow_id)

    def test_a_variable_is_created_in_the_workflow_the_envelope_names(self, engine: Engine) -> None:
        workflow = push_unsaved_workflow(engine)

        result = engine.handle_request(
            CreateVariableRequest(name="greeting", type="str", is_global=True, value="hi"),
            workflow_id=workflow.workflow_id,
        )

        assert isinstance(result, CreateVariableResultSuccess)
        assert engine.open_workflows.get(workflow.workflow_id).variables.global_layer.has("greeting")

    def test_a_variable_request_for_a_workflow_that_is_not_open_changes_nothing(self, engine: Engine) -> None:
        workflow = push_unsaved_workflow(engine)

        result = engine.handle_request(
            CreateVariableRequest(name="greeting", type="str", is_global=True, value="hi"), workflow_id=NOT_OPEN_ID
        )

        assert result.failed()
        assert not engine.open_workflows.get(workflow.workflow_id).variables.global_layer.has("greeting")


class TestEveryEngineHandlerDeclaresAScope:
    """Library handlers registered at library load (`module_loading.py`) are not covered: none load here."""

    def test_every_handler_the_engine_registers_declares_a_scope(self, engine: Engine) -> None:
        event_manager = engine.event_manager

        undeclared = sorted(
            request_type.__name__
            for request_type in event_manager.registered_request_types()
            if event_manager.get_scope_for_request_type(request_type) is None
        )

        assert undeclared == []

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
