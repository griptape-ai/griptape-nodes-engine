"""NodeManager handlers act on the workflow dispatch hands them; its edge methods act on the open one."""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest

from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
from griptape_nodes.exe_types.node_types import DataNode
from griptape_nodes.node_library.library_registry import LibraryMetadata, LibraryRegistry, LibrarySchema, NodeMetadata
from griptape_nodes.retained_mode.events.connection_events import (
    CreateConnectionRequest,
    CreateConnectionResultSuccess,
    DeleteConnectionRequest,
    ListConnectionsForNodeRequest,
)
from griptape_nodes.retained_mode.events.execution_events import (
    CancelExecuteNodeRequest,
    ExecuteNodeRequest,
    ExecuteNodeResultSuccess,
)
from griptape_nodes.retained_mode.events.flow_events import CreateFlowRequest, CreateFlowResultSuccess
from griptape_nodes.retained_mode.events.node_events import (
    CreateNodeRequest,
    CreateNodeResultSuccess,
    DeleteNodeRequest,
    DeleteNodeResultSuccess,
)
from griptape_nodes.retained_mode.events.object_events import ClearAllObjectStateRequest
from griptape_nodes.retained_mode.events.parameter_events import (
    GetParameterValueRequest,
    GetParameterValueResultFailure,
    GetParameterValueResultSuccess,
    RemoveParameterFromNodeRequest,
    RemoveParameterFromNodeResultFailure,
    RemoveParameterFromNodeResultSuccess,
    SetParameterValueRequest,
    SetParameterValueResultFailure,
    SetParameterValueResultSuccess,
)
from griptape_nodes.retained_mode.events.validation_events import (
    ValidateNodeDependenciesRequest,
    ValidateNodeDependenciesResultSuccess,
)
from griptape_nodes.retained_mode.managers.event_manager import ResultContext
from griptape_nodes.retained_mode.managers.node_manager import NodeManager
from griptape_nodes.retained_mode.request_handlers import RequestScope, handled_request_scope, handled_request_types
from griptape_nodes.retained_mode.runtime.binding import NO_WORKFLOW, bound_workflow
from griptape_nodes.retained_mode.runtime.workflow_runtime import WorkflowRuntime

if TYPE_CHECKING:
    from collections.abc import Iterator

    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.events.base_events import RequestPayload, ResultPayload

_LIBRARY_NAME = "Node Workflow Scope Library"

# A worker opens no workflow, so the request it runs a node for has none to hand it.
_OPTIONAL_WORKFLOW_REQUESTS = {ExecuteNodeRequest}
# The tasks it cancels belong to this process, whichever workflow sent them.
_HOST_REQUESTS = {CancelExecuteNodeRequest}


class _ValueNode(DataNode):
    """Minimal DataNode with a connectable "value" parameter."""

    def __init__(self, name: str, metadata: dict | None = None) -> None:
        super().__init__(name, metadata=metadata)
        self.add_parameter(
            Parameter(
                name="value",
                tooltip="Value to echo",
                type="str",
                default_value="",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY, ParameterMode.OUTPUT},
            )
        )

    def process(self) -> None:
        self.parameter_output_values["value"] = self.get_parameter_value("value")


@pytest.fixture(autouse=True)
def value_library(engine: Engine) -> Iterator[None]:
    """A library providing `_ValueNode`, so the nodes these tests create are real ones."""
    LibraryRegistry._clear()
    schema = LibrarySchema(
        name=_LIBRARY_NAME,
        library_schema_version=LibrarySchema.LATEST_SCHEMA_VERSION,
        metadata=LibraryMetadata(
            author="test", description="test", library_version="0.1.0", engine_version="0.0.0", tags=[]
        ),
        categories=[],
        nodes=[],
    )
    library = LibraryRegistry.generate_new_library(schema)
    library.register_new_node_type(_ValueNode, NodeMetadata(category="test", description="d", display_name="Value"))
    try:
        yield
    finally:
        engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
        LibraryRegistry._clear()


def _open_workflow(engine: Engine) -> WorkflowRuntime:
    engine.context_manager.push_workflow(workflow_name="open")
    return engine.current_workflow()


def _handle(engine: Engine, request: RequestPayload, open_workflow_id: str | None = None) -> ResultPayload:
    return engine.event_manager.handle_request(
        request, result_context=ResultContext(open_workflow_id=open_workflow_id)
    ).result


def _create_canvas(engine: Engine, open_workflow_id: str | None = None) -> str:
    result = _handle(engine, CreateFlowRequest(parent_flow_name=None, flow_name="canvas"), open_workflow_id)
    assert isinstance(result, CreateFlowResultSuccess)
    return result.flow_name


def _create_node(engine: Engine, flow_name: str, node_name: str, open_workflow_id: str | None = None) -> str:
    result = _handle(
        engine,
        CreateNodeRequest(node_type="_ValueNode", node_name=node_name, override_parent_flow_name=flow_name),
        open_workflow_id,
    )
    assert isinstance(result, CreateNodeResultSuccess)
    return result.node_name


def _other_workflow_with_connected_nodes(engine: Engine) -> WorkflowRuntime:
    """A workflow that is not the open one, holding "source" connected to "target" in flow "elsewhere"."""
    _open_workflow(engine)
    _create_canvas(engine)
    other = WorkflowRuntime("other", engine)
    engine.flow_manager.on_create_flow_request(CreateFlowRequest(parent_flow_name=None, flow_name="elsewhere"), other)
    for node_name in ("source", "target"):
        created = engine.node_manager.on_create_node_request(
            CreateNodeRequest(node_type="_ValueNode", node_name=node_name, override_parent_flow_name="elsewhere"),
            other,
        )
        assert isinstance(created, CreateNodeResultSuccess), created.result_details
    source = engine.node_manager.get_node_by_name_in(other, "source")
    target = engine.node_manager.get_node_by_name_in(other, "target")
    source_parameter = source.get_parameter_by_name("value")
    target_parameter = target.get_parameter_by_name("value")
    assert source_parameter is not None
    assert target_parameter is not None
    other.flows.connections.add_connection(source, source_parameter, target, target_parameter)
    return other


@contextmanager
def _also_open(engine: Engine, workflow: WorkflowRuntime) -> Iterator[None]:
    """Run with `workflow` open beside the open one, so nothing can fall back to a sole open workflow."""
    with patch.dict(engine.open_workflows._by_id, {workflow.workflow_id: workflow}):
        yield


class TestNodeManagerScopes:
    def test_every_handler_declares_its_scope(self) -> None:
        scopes = {
            request_type: handled_request_scope(attr)
            for attr in vars(NodeManager).values()
            for request_type in handled_request_types(attr)
        }

        assert scopes, "NodeManager registers no handlers"
        for request_type, scope in scopes.items():
            expected = RequestScope.WORKFLOW
            if request_type in _OPTIONAL_WORKFLOW_REQUESTS:
                expected = RequestScope.OPTIONAL_WORKFLOW
            if request_type in _HOST_REQUESTS:
                expected = RequestScope.HOST
            assert scope is expected, f"{request_type.__name__} is {scope}, expected {expected}"


class TestHandlersActOnTheWorkflowTheyAreHanded:
    def test_a_request_with_no_envelope_id_acts_on_the_open_workflow(self, engine: Engine) -> None:
        _open_workflow(engine)
        flow_name = _create_canvas(engine)

        node_name = _create_node(engine, flow_name, "node")

        workflow = engine.current_workflow()
        assert workflow.nodes.name_to_parent_flow_name == {node_name: flow_name}
        assert workflow.objects.has(node_name)

    def test_a_request_with_an_envelope_id_acts_on_that_workflow(self, engine: Engine) -> None:
        entry = _open_workflow(engine)
        flow_name = _create_canvas(engine, open_workflow_id=entry.workflow_id)

        node_name = _create_node(engine, flow_name, "node", open_workflow_id=entry.workflow_id)

        assert engine.open_workflows.get(entry.workflow_id).nodes.name_to_parent_flow_name == {node_name: flow_name}

    def test_a_request_naming_a_workflow_that_is_not_open_changes_nothing(self, engine: Engine) -> None:
        _open_workflow(engine)
        flow_name = _create_canvas(engine)

        result = _handle(
            engine, CreateNodeRequest(node_type="_ValueNode", override_parent_flow_name=flow_name), "not-open"
        )

        assert result.failed()
        assert "isn't open" in str(result.result_details)
        assert engine.current_workflow().nodes.name_to_parent_flow_name == {}

    def test_a_handler_uses_the_workflow_it_is_handed_rather_than_the_open_one(self, engine: Engine) -> None:
        _open_workflow(engine)
        _create_canvas(engine)
        other = WorkflowRuntime("other", engine)
        engine.flow_manager.on_create_flow_request(
            CreateFlowRequest(parent_flow_name=None, flow_name="elsewhere"), other
        )

        result = engine.node_manager.on_create_node_request(
            CreateNodeRequest(node_type="_ValueNode", node_name="node", override_parent_flow_name="elsewhere"), other
        )

        assert isinstance(result, CreateNodeResultSuccess)
        assert other.nodes.name_to_parent_flow_name == {"node": "elsewhere"}
        assert other.objects.has("node")
        assert engine.current_workflow().nodes.name_to_parent_flow_name == {}
        assert not engine.object_manager.has_object_with_name("node")

    def test_setting_a_value_passes_it_through_the_connections_of_the_workflow_it_is_handed(
        self, engine: Engine
    ) -> None:
        other = _other_workflow_with_connected_nodes(engine)

        with _also_open(engine, other):
            result = engine.node_manager.on_set_parameter_value_request_in_workflow(
                SetParameterValueRequest(parameter_name="value", value="hi", node_name="source"), other
            )

        assert isinstance(result, SetParameterValueResultSuccess), result.result_details
        assert engine.node_manager.get_node_by_name_in(other, "target").get_parameter_value("value") == "hi"

    def test_validating_dependencies_follows_the_connections_of_the_workflow_it_is_handed(self, engine: Engine) -> None:
        other = _other_workflow_with_connected_nodes(engine)
        source = engine.node_manager.get_node_by_name_in(other, "source")
        target = engine.node_manager.get_node_by_name_in(other, "target")
        problem = ValueError("source is not ready")

        with (
            _also_open(engine, other),
            patch.object(source, "validate_before_workflow_run", return_value=[problem]),
            patch.object(target, "validate_before_workflow_run", return_value=[]),
        ):
            result = engine.node_manager.on_validate_node_dependencies_request(
                ValidateNodeDependenciesRequest(node_name="target"), other
            )

        assert isinstance(result, ValidateNodeDependenciesResultSuccess), result.result_details
        assert result.exceptions == [problem]

    @pytest.mark.asyncio
    async def test_deleting_a_node_mid_run_clears_the_run_queue_of_the_workflow_it_is_handed(
        self, engine: Engine
    ) -> None:
        other = _other_workflow_with_connected_nodes(engine)
        running_machine = MagicMock()
        running_machine.context.flow_name = "elsewhere"
        other.flows.global_control_flow_machine = running_machine
        other.flows.global_flow_queue.put(MagicMock())

        with (
            _also_open(engine, other),
            patch.object(engine.node_manager, "_find_entangled_live_node", return_value=None),
        ):
            result = await engine.node_manager.on_delete_node_request(DeleteNodeRequest(node_name="target"), other)

        assert isinstance(result, DeleteNodeResultSuccess), result.result_details
        assert other.flows.global_flow_queue.empty()

    @pytest.mark.asyncio
    async def test_a_worker_runs_a_node_with_no_workflow_open(self, engine: Engine) -> None:
        request = ExecuteNodeRequest(
            node_name="node",
            node_metadata={"node_type": "_ValueNode", "library": _LIBRARY_NAME},
            orchestrator_workflow_id="00000000000000000000000000000abc",
        )

        with patch.object(engine.library_manager, "_is_worker", new=True):
            result = await engine.ahandle_request(request)

        assert isinstance(result, ExecuteNodeResultSuccess), result.result_details


class TestNestedRequestsCarryTheHandlersWorkflow:
    """The requests a NodeManager handler sends are bound to its workflow. What their own handlers send is theirs."""

    @staticmethod
    def _record_bindings(engine: Engine) -> Any:
        """Patch `Engine.handle_request` to record each request with the workflow bound when it was sent."""
        sent: list[tuple[RequestPayload, object]] = []
        handle_request = engine.handle_request

        def record(request: RequestPayload) -> ResultPayload:
            sent.append((request, bound_workflow.get()))
            return handle_request(request)

        return sent, patch.object(engine, "handle_request", side_effect=record)

    @staticmethod
    def _assert_bound(sent: list[tuple[RequestPayload, object]], request_types: set[type], workflow: object) -> None:
        nested = [(request, bound) for request, bound in sent if type(request) in request_types]
        assert {type(request) for request, _ in nested} == request_types
        for request, bound in nested:
            assert bound is workflow, request

    @pytest.mark.asyncio
    async def test_deleting_a_node_addresses_every_nested_request_to_its_workflow(self, engine: Engine) -> None:
        _open_workflow(engine)
        flow_name = _create_canvas(engine)
        source = _create_node(engine, flow_name, "source")
        target = _create_node(engine, flow_name, "target")
        connected = engine.handle_request(
            CreateConnectionRequest(
                source_node_name=source,
                source_parameter_name="value",
                target_node_name=target,
                target_parameter_name="value",
            )
        )
        assert isinstance(connected, CreateConnectionResultSuccess), connected.result_details
        workflow = engine.current_workflow()

        sent, recording = self._record_bindings(engine)
        with recording, engine.bind_workflow(NO_WORKFLOW):
            result = await engine.node_manager.on_delete_node_request(DeleteNodeRequest(node_name=target), workflow)

        assert isinstance(result, DeleteNodeResultSuccess), result.result_details
        self._assert_bound(sent, {ListConnectionsForNodeRequest, DeleteConnectionRequest}, workflow)


class TestEdgeMethodsDelegateWithTheOpenWorkflow:
    def test_get_node_by_name_hands_the_open_workflow_to_its_explicit_twin(self, engine: Engine) -> None:
        _open_workflow(engine)
        node_manager = engine.node_manager

        with patch.object(node_manager, "get_node_by_name_in", return_value=MagicMock()) as explicit:
            returned = node_manager.get_node_by_name("node")

        explicit.assert_called_once_with(engine.current_workflow(), "node")
        assert returned is explicit.return_value

    def test_get_node_by_name_fails_with_no_workflow_open(self, engine: Engine) -> None:
        with pytest.raises(ValueError, match="not found"):
            engine.node_manager.get_node_by_name("node")


class TestHandlersNodeLibrariesCallDirectly:
    """Engine, `exe_types` and node library code call these by name with only a request, so those calls keep working."""

    @pytest.mark.parametrize(
        ("handler_name", "request_payload"),
        [
            (
                "on_set_parameter_value_request",
                SetParameterValueRequest(parameter_name="value", value="hi", node_name="node"),
            ),
            (
                "on_remove_parameter_from_node_request",
                RemoveParameterFromNodeRequest(parameter_name="value", node_name="node"),
            ),
            ("on_get_parameter_value_request", GetParameterValueRequest(parameter_name="value", node_name="node")),
        ],
    )
    def test_a_handler_called_with_only_a_request_acts_on_the_open_workflow(
        self, engine: Engine, handler_name: str, request_payload: Any
    ) -> None:
        _open_workflow(engine)
        node_manager = engine.node_manager

        with patch.object(node_manager, f"{handler_name}_in_workflow", return_value=MagicMock()) as handler:
            returned = getattr(node_manager, handler_name)(request=request_payload)

        handler.assert_called_once_with(request_payload, engine.current_workflow())
        assert returned is handler.return_value

    def _workflow_with_a_node(self, engine: Engine) -> str:
        _open_workflow(engine)
        flow_name = _create_canvas(engine)
        node_name = _create_node(engine, flow_name, "node")
        set_result = engine.handle_request(
            SetParameterValueRequest(parameter_name="value", value="hi", node_name=node_name)
        )
        assert set_result.succeeded(), set_result.result_details
        return node_name

    def test_get_parameter_value_takes_only_a_request(self, engine: Engine) -> None:
        node_name = self._workflow_with_a_node(engine)

        result = engine.node_manager.on_get_parameter_value_request(
            GetParameterValueRequest(parameter_name="value", node_name=node_name)
        )

        assert isinstance(result, GetParameterValueResultSuccess)
        assert result.value == "hi"

    def test_get_parameter_value_takes_the_request_by_keyword(self, engine: Engine) -> None:
        node_name = self._workflow_with_a_node(engine)

        result = engine.node_manager.on_get_parameter_value_request(
            request=GetParameterValueRequest(parameter_name="value", node_name=node_name)
        )

        assert isinstance(result, GetParameterValueResultSuccess)
        assert result.value == "hi"

    def test_set_parameter_value_takes_only_a_request(self, engine: Engine) -> None:
        node_name = self._workflow_with_a_node(engine)

        result = engine.node_manager.on_set_parameter_value_request(
            SetParameterValueRequest(parameter_name="value", value="bye", node_name=node_name)
        )

        assert isinstance(result, SetParameterValueResultSuccess), result.result_details
        assert engine.node_manager.get_node_by_name(node_name).get_parameter_value("value") == "bye"

    def test_remove_parameter_from_node_takes_the_request_by_keyword(self, engine: Engine) -> None:
        node_name = self._workflow_with_a_node(engine)
        node = engine.node_manager.get_node_by_name(node_name)
        node.add_parameter(Parameter(name="extra", tooltip="Removable", type="str", user_defined=True))

        result = engine.node_manager.on_remove_parameter_from_node_request(
            request=RemoveParameterFromNodeRequest(parameter_name="extra", node_name=node_name)
        )

        assert isinstance(result, RemoveParameterFromNodeResultSuccess), result.result_details
        assert node.get_parameter_by_name("extra") is None

    def test_handlers_called_with_only_a_request_fail_with_no_workflow_open(self, engine: Engine) -> None:
        node_manager = engine.node_manager

        set_result = node_manager.on_set_parameter_value_request(
            SetParameterValueRequest(parameter_name="value", value="hi", node_name="node")
        )
        removed = node_manager.on_remove_parameter_from_node_request(
            RemoveParameterFromNodeRequest(parameter_name="value", node_name="node")
        )

        assert isinstance(set_result, SetParameterValueResultFailure)
        assert isinstance(removed, RemoveParameterFromNodeResultFailure)

    def test_get_parameter_value_fails_with_no_workflow_open(self, engine: Engine) -> None:
        result = engine.node_manager.on_get_parameter_value_request(
            GetParameterValueRequest(parameter_name="value", node_name="node")
        )

        assert isinstance(result, GetParameterValueResultFailure)
        assert "no workflow is open" in str(result.result_details)
