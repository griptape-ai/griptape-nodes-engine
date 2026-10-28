"""The detector for strict-mode rule `graph-access-while-loading`, at its call site in EventManager.

End-to-end behavior, through real nodes loaded in- and out of process, is in tests/e2e/test_worker_copy_load.py.
"""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING

import pytest

from griptape_nodes.common.strict_mode import STRICT_MODE, StrictModeScopeKind
from griptape_nodes.exe_types.node_loading import node_loading
from griptape_nodes.retained_mode.events import (
    connection_events,
    context_events,
    execution_events,
    flow_events,
    node_events,
    parameter_events,
    validation_events,
    variable_events,
)
from griptape_nodes.retained_mode.events.base_events import GraphAccessMixin, RequestPayload
from griptape_nodes.retained_mode.events.connection_events import (
    ListConnectionsForNodeRequest,
    ListConnectionsForNodeResultSuccess,
)
from griptape_nodes.retained_mode.events.parameter_events import (
    GetParameterValueRequest,
    GetParameterValueResultSuccess,
)
from griptape_nodes.retained_mode.managers.event_manager import EventManager

if TYPE_CHECKING:
    from types import ModuleType

    from griptape_nodes.common.strict_mode import StrictModeScope
    from griptape_nodes.retained_mode.events.base_events import ResultPayload

LOADING = "Loading Node"

# Every request in these modules is about the graph.
GRAPH_MODULES = (connection_events, flow_events, variable_events)

GRAPH_REQUESTS_ELSEWHERE = (
    context_events.EnsureWorkflowAndFlowRequest,
    execution_events.CancelFlowRequest,
    execution_events.ContinueExecutionStepRequest,
    execution_events.GetFlowStateRequest,
    execution_events.GetIsFlowRunningRequest,
    execution_events.ResolveNodeRequest,
    execution_events.SingleExecutionStepRequest,
    execution_events.SingleNodeStepRequest,
    execution_events.StartFlowFromNodeRequest,
    execution_events.StartFlowRequest,
    execution_events.StartLocalSubflowRequest,
    execution_events.UnresolveFlowRequest,
    node_events.AddNodesToNodeGroupRequest,
    node_events.DeleteNodeRequest,
    node_events.DeserializeSelectedNodesFromCommandsRequest,
    node_events.DuplicateSelectedNodesRequest,
    node_events.GetAllNodeInfoRequest,
    node_events.GetFlowForNodeRequest,
    node_events.MoveNodeToNewFlowRequest,
    node_events.RemoveNodeFromNodeGroupRequest,
    node_events.SerializeSelectedNodesToCommandsRequest,
    node_events.UnresolveNodeRequest,
    parameter_events.GetConnectionsForParameterRequest,
    parameter_events.RemoveParameterFromNodeRequest,
    validation_events.ValidateFlowDependenciesRequest,
)


def _requests_defined_in(module: ModuleType) -> list[type[RequestPayload]]:
    return [
        member
        for _, member in inspect.getmembers(module, inspect.isclass)
        if issubclass(member, RequestPayload) and member is not RequestPayload and member.__module__ == module.__name__
    ]


GRAPH_REQUESTS = sorted(
    {*GRAPH_REQUESTS_ELSEWHERE, *(request for module in GRAPH_MODULES for request in _requests_defined_in(module))},
    key=lambda request: request.__name__,
)


def _answering(event_manager: EventManager) -> EventManager:
    """Register stub handlers for one graph request and one request about a node's own value."""

    def list_connections(_request: RequestPayload) -> ResultPayload:
        return ListConnectionsForNodeResultSuccess(incoming_connections=[], outgoing_connections=[], result_details="")

    def get_value(_request: RequestPayload) -> ResultPayload:
        return GetParameterValueResultSuccess(
            type="str", value=None, input_types=["str"], output_type="str", result_details=""
        )

    event_manager.assign_manager_to_request_type(ListConnectionsForNodeRequest, list_connections)
    event_manager.assign_manager_to_request_type(GetParameterValueRequest, get_value)
    return event_manager


def _rule_ids(scope: StrictModeScope) -> list[str]:
    return [violation.rule_id for violation in scope.violations]


class TestGraphAccessWhileLoading:
    def test_a_graph_request_while_a_node_loads_is_reported(self) -> None:
        event_manager = _answering(EventManager())

        with (
            STRICT_MODE.open_scope(
                kind=StrictModeScopeKind.LOAD, subject=LOADING, library_name=None, is_worker=False
            ) as scope,
            node_loading(LOADING),
        ):
            event_manager.handle_request(ListConnectionsForNodeRequest(node_name=LOADING))

        assert _rule_ids(scope) == ["graph-access-while-loading"]
        assert "ListConnectionsForNodeRequest" in scope.violations[0].message

    @pytest.mark.asyncio
    async def test_an_async_graph_request_while_a_node_loads_is_reported(self) -> None:
        event_manager = _answering(EventManager())

        with (
            STRICT_MODE.open_scope(
                kind=StrictModeScopeKind.LOAD, subject=LOADING, library_name=None, is_worker=False
            ) as scope,
            node_loading(LOADING),
        ):
            await event_manager.ahandle_request(ListConnectionsForNodeRequest(node_name=LOADING))

        assert _rule_ids(scope) == ["graph-access-while-loading"]

    def test_a_request_naming_another_node_is_reported(self) -> None:
        event_manager = _answering(EventManager())

        with (
            STRICT_MODE.open_scope(
                kind=StrictModeScopeKind.LOAD, subject=LOADING, library_name=None, is_worker=False
            ) as scope,
            node_loading(LOADING),
        ):
            event_manager.handle_request(GetParameterValueRequest(node_name="Another Node", parameter_name="text"))

        assert _rule_ids(scope) == ["graph-access-while-loading"]

    @pytest.mark.parametrize("node_name", [LOADING, None])
    def test_a_request_about_the_loading_node_itself_is_not_reported(self, node_name: str | None) -> None:
        """A request naming no node acts on the current context, which while a node loads is that node."""
        event_manager = _answering(EventManager())

        with (
            STRICT_MODE.open_scope(
                kind=StrictModeScopeKind.LOAD, subject=LOADING, library_name=None, is_worker=False
            ) as scope,
            node_loading(LOADING),
        ):
            event_manager.handle_request(GetParameterValueRequest(node_name=node_name, parameter_name="text"))

        assert scope.violations == []

    def test_a_graph_request_when_no_node_loads_is_not_reported(self) -> None:
        event_manager = _answering(EventManager())

        with STRICT_MODE.open_scope(
            kind=StrictModeScopeKind.LOAD, subject=LOADING, library_name=None, is_worker=False
        ) as scope:
            event_manager.handle_request(ListConnectionsForNodeRequest(node_name=LOADING))

        assert scope.violations == []


class TestGraphRequestsAreMarked:
    @pytest.mark.parametrize("request_type", GRAPH_REQUESTS, ids=lambda request_type: request_type.__name__)
    def test_every_graph_request_carries_the_marker(self, request_type: type[RequestPayload]) -> None:
        assert issubclass(request_type, GraphAccessMixin)
