"""Tests for GriptapeNodes.handle_request and ahandle_request broadcast behavior, and its manager accessors."""

from __future__ import annotations

import asyncio
import contextvars
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from griptape_nodes.common.strict_mode import STRICT_MODE, StrictModeScopeKind, StrictModeSeverity
from griptape_nodes.exe_types.core_types import ParameterGroup
from griptape_nodes.retained_mode.events.base_events import RequestPayload, ResultPayload
from griptape_nodes.retained_mode.events.flow_events import CreateFlowRequest, CreateFlowResultSuccess
from griptape_nodes.retained_mode.events.generic_events import WorkflowNotNamedResultFailure
from griptape_nodes.retained_mode.events.os_events import ReadFileRequest
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
from griptape_nodes.retained_mode.managers.context_manager import ContextManager
from griptape_nodes.retained_mode.runtime.binding import NO_WORKFLOW

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.runtime.workflow_runtime import WorkflowRuntime


@dataclass(kw_only=True)
class _BroadcastingRequest(RequestPayload):
    """Minimal request with the default broadcast_result=True for testing."""


class TestHandleRequestBroadcast:
    def test_broadcasts_result_when_broadcast_result_is_true(self) -> None:
        """handle_request queues a GriptapeNodeEvent when broadcast_result=True."""
        event_mgr = GriptapeNodes.EventManager()
        mock_result_event = MagicMock()

        with (
            patch.object(event_mgr, "handle_request", return_value=mock_result_event),
            patch.object(event_mgr, "should_suppress_event", return_value=False),
            patch.object(event_mgr, "put_event") as mock_put,
            # GriptapeNodeEvent is a Pydantic model that validates its wrapped_event arg;
            # patching it here prevents the Pydantic validation from rejecting the MagicMock.
            patch("griptape_nodes.retained_mode.engine.GriptapeNodeEvent"),
        ):
            GriptapeNodes.handle_request(_BroadcastingRequest())

        mock_put.assert_called_once()

    def test_skips_broadcast_when_broadcast_result_is_false(self) -> None:
        """handle_request does not queue when request.broadcast_result=False."""
        event_mgr = GriptapeNodes.EventManager()
        mock_result_event = MagicMock()

        with (
            patch.object(event_mgr, "handle_request", return_value=mock_result_event),
            patch.object(event_mgr, "should_suppress_event", return_value=False),
            patch.object(event_mgr, "put_event") as mock_put,
        ):
            GriptapeNodes.handle_request(ReadFileRequest())

        mock_put.assert_not_called()

    def test_instance_broadcast_result_overrides_class_default(self) -> None:
        """broadcast_result set on the instance at construction time is respected."""
        event_mgr = GriptapeNodes.EventManager()
        mock_result_event = MagicMock()

        with (
            patch.object(event_mgr, "handle_request", return_value=mock_result_event),
            patch.object(event_mgr, "should_suppress_event", return_value=False),
            patch.object(event_mgr, "put_event") as mock_put,
            patch("griptape_nodes.retained_mode.engine.GriptapeNodeEvent"),
        ):
            GriptapeNodes.handle_request(ReadFileRequest(broadcast_result=True))

        mock_put.assert_called_once()

    def test_skips_broadcast_when_event_is_suppressed(self) -> None:
        """handle_request does not queue when the event is suppressed."""
        event_mgr = GriptapeNodes.EventManager()
        mock_result_event = MagicMock()

        with (
            patch.object(event_mgr, "handle_request", return_value=mock_result_event),
            patch.object(event_mgr, "should_suppress_event", return_value=True),
            patch.object(event_mgr, "put_event") as mock_put,
        ):
            GriptapeNodes.handle_request(_BroadcastingRequest())

        mock_put.assert_not_called()


class TestAHandleRequestBroadcast:
    @pytest.mark.asyncio
    async def test_broadcasts_result_when_broadcast_result_is_true(self) -> None:
        """ahandle_request queues a GriptapeNodeEvent when broadcast_result=True."""
        event_mgr = GriptapeNodes.EventManager()
        mock_result_event = MagicMock()

        with (
            patch.object(event_mgr, "ahandle_request", return_value=mock_result_event),
            patch.object(event_mgr, "should_suppress_event", return_value=False),
            patch.object(event_mgr, "aput_event") as mock_aput,
            patch("griptape_nodes.retained_mode.engine.GriptapeNodeEvent"),
        ):
            await GriptapeNodes.ahandle_request(_BroadcastingRequest())

        mock_aput.assert_called_once()

    @pytest.mark.asyncio
    async def test_skips_broadcast_when_broadcast_result_is_false(self) -> None:
        """ahandle_request does not queue when request.broadcast_result=False."""
        event_mgr = GriptapeNodes.EventManager()
        mock_result_event = MagicMock()

        with (
            patch.object(event_mgr, "ahandle_request", return_value=mock_result_event),
            patch.object(event_mgr, "should_suppress_event", return_value=False),
            patch.object(event_mgr, "aput_event") as mock_aput,
        ):
            await GriptapeNodes.ahandle_request(ReadFileRequest())

        mock_aput.assert_not_called()

    @pytest.mark.asyncio
    async def test_instance_broadcast_result_overrides_class_default(self) -> None:
        """broadcast_result set on the instance at construction time is respected."""
        event_mgr = GriptapeNodes.EventManager()
        mock_result_event = MagicMock()

        with (
            patch.object(event_mgr, "ahandle_request", return_value=mock_result_event),
            patch.object(event_mgr, "should_suppress_event", return_value=False),
            patch.object(event_mgr, "aput_event") as mock_aput,
            patch("griptape_nodes.retained_mode.engine.GriptapeNodeEvent"),
        ):
            await GriptapeNodes.ahandle_request(ReadFileRequest(broadcast_result=True))

        mock_aput.assert_called_once()

    @pytest.mark.asyncio
    async def test_skips_broadcast_when_event_is_suppressed(self) -> None:
        """ahandle_request does not queue when the event is suppressed."""
        event_mgr = GriptapeNodes.EventManager()
        mock_result_event = MagicMock()

        with (
            patch.object(event_mgr, "ahandle_request", return_value=mock_result_event),
            patch.object(event_mgr, "should_suppress_event", return_value=True),
            patch.object(event_mgr, "aput_event") as mock_aput,
        ):
            await GriptapeNodes.ahandle_request(_BroadcastingRequest())

        mock_aput.assert_not_called()


class TestStateReadBypassingRequests:
    """Strict-mode rule `state-read-bypassing-requests`: a node reaching for a manager while it runs."""

    def test_a_manager_read_while_a_node_runs_in_process_is_reported_and_allowed(self, engine: Engine) -> None:
        with (
            STRICT_MODE.open_scope(
                kind=StrictModeScopeKind.RUNTIME_EXECUTE, subject="Runner", library_name=None, is_worker=False
            ) as scope,
            engine.event_manager.node_execution_scope(),
        ):
            config_manager = GriptapeNodes.ConfigManager()

        assert config_manager is engine.config_manager
        assert [violation.rule_id for violation in scope.violations] == ["state-read-bypassing-requests"]
        assert scope.violations[0].severity is StrictModeSeverity.WARNING
        assert "ConfigManager" in scope.violations[0].message
        assert "GetConfigValueRequest" in scope.violations[0].message

    def test_a_manager_read_while_a_node_runs_in_a_worker_is_reported_and_refused(self, engine: Engine) -> None:
        engine.library_manager._is_worker = True

        with (
            STRICT_MODE.open_scope(
                kind=StrictModeScopeKind.RUNTIME_EXECUTE, subject="Runner", library_name=None, is_worker=True
            ) as scope,
            engine.event_manager.node_execution_scope(),
            pytest.raises(RuntimeError, match="GetConfigValueRequest"),
        ):
            GriptapeNodes.ConfigManager()

        assert [violation.rule_id for violation in scope.violations] == ["state-read-bypassing-requests"]
        assert scope.violations[0].severity is StrictModeSeverity.ERROR

    def test_a_manager_read_outside_a_run_is_not_reported(self, engine: Engine) -> None:
        """Engine boot and library load legitimately use their own managers."""
        with STRICT_MODE.open_scope(
            kind=StrictModeScopeKind.RUNTIME_EXECUTE, subject="Runner", library_name=None, is_worker=False
        ) as scope:
            GriptapeNodes.ConfigManager()

        assert engine.event_manager.in_node_execution() is False
        assert scope.violations == []

    def test_a_manager_read_while_a_worker_loads_its_copy_is_allowed_while_another_node_runs(
        self, engine: Engine
    ) -> None:
        """The load answers from the worker's own graph, whatever else the worker is doing."""
        engine.library_manager._is_worker = True

        with (
            STRICT_MODE.open_scope(
                kind=StrictModeScopeKind.RUNTIME_EXECUTE, subject="Loading", library_name=None, is_worker=True
            ) as scope,
            engine.event_manager.node_execution_scope(),
            engine.event_manager.rebuild_scope("Loading"),
        ):
            config_manager = GriptapeNodes.ConfigManager()

        assert config_manager is engine.config_manager
        assert scope.violations == []


def _open(engine: Engine, display_name: str) -> WorkflowRuntime:
    return engine.open_workflow(engine.workflow_registry.register_unsaved(display_name, listed=False).workflow_id)


def _create_canvas() -> ResultPayload:
    return GriptapeNodes.handle_request(CreateFlowRequest(parent_flow_name=None, flow_name="canvas"))


class TestFacadeCallersBoundToNothing:
    """Saved workflow files and node library threads can't name a workflow, so the facade acts on the only open one."""

    def test_a_request_acts_on_the_only_open_workflow(self, engine: Engine) -> None:
        only = _open(engine, "only")

        result = _create_canvas()

        assert isinstance(result, CreateFlowResultSuccess), result.result_details
        assert "canvas" in only.flows.name_to_parent_name

    @pytest.mark.asyncio
    async def test_an_awaited_request_acts_on_the_only_open_workflow(self, engine: Engine) -> None:
        only = _open(engine, "only")

        result = await GriptapeNodes.ahandle_request(CreateFlowRequest(parent_flow_name=None, flow_name="canvas"))

        assert isinstance(result, CreateFlowResultSuccess), result.result_details
        assert "canvas" in only.flows.name_to_parent_name

    def test_a_request_from_a_raw_thread_acts_on_the_only_open_workflow(self, engine: Engine) -> None:
        only = _open(engine, "only")
        results: list[ResultPayload] = []

        thread = threading.Thread(target=lambda: results.append(contextvars.Context().run(_create_canvas)))
        thread.start()
        thread.join(timeout=10)

        assert isinstance(results[0], CreateFlowResultSuccess), results[0].result_details
        assert "canvas" in only.flows.name_to_parent_name

    def test_a_manager_read_from_a_raw_thread_reads_the_only_open_workflow(self, engine: Engine) -> None:
        only = _open(engine, "only")
        created = only.handle_request(CreateFlowRequest(parent_flow_name=None, flow_name="canvas"))
        assert isinstance(created, CreateFlowResultSuccess), created.result_details
        flow_names: list[str] = []

        def read_the_flow() -> None:
            flow_names.append(GriptapeNodes.ContextManager().get_current_flow().name)
            assert GriptapeNodes.FlowManager().get_flow_by_name("canvas") is not None
            assert GriptapeNodes.ObjectManager().has_object_with_name("canvas")

        thread = threading.Thread(target=lambda: contextvars.Context().run(read_the_flow))
        thread.start()
        thread.join(timeout=10)

        assert flow_names == ["canvas"]

    def test_a_manager_context_block_from_an_unbound_caller_runs_in_the_only_open_workflow(
        self, engine: Engine
    ) -> None:
        only = _open(engine, "only")
        created = only.handle_request(CreateFlowRequest(parent_flow_name=None, flow_name="canvas"))
        assert isinstance(created, CreateFlowResultSuccess), created.result_details

        with GriptapeNodes.ContextManager().flow("canvas") as flow:
            assert flow.name == "canvas"

    def test_a_manager_hands_back_an_element_as_it_is(self, engine: Engine) -> None:
        """An element is a context manager too, but only the block methods' results are wrapped."""
        _open(engine, "only")
        element = ParameterGroup(name="group")

        with patch.object(ContextManager, "get_current_element", lambda _self: element):
            handed = GriptapeNodes.ContextManager().get_current_element()

        assert handed is element

    @pytest.mark.usefixtures("multiple_open_workflows")
    def test_a_stored_manager_follows_its_callers_binding(self, engine: Engine) -> None:
        first = _open(engine, "first")
        stored = GriptapeNodes.ContextManager()
        second = _open(engine, "second")
        for workflow, flow_name in ((first, "first_canvas"), (second, "second_canvas")):
            created = workflow.handle_request(CreateFlowRequest(parent_flow_name=None, flow_name=flow_name))
            assert isinstance(created, CreateFlowResultSuccess), created.result_details

        with engine.bind_workflow(second):
            current_id = stored.get_current_workflow_id()
            with stored.flow("second_canvas") as flow:
                entered_in = engine.current_workflow()

        assert current_id == second.workflow_id
        assert flow.name == "second_canvas"
        assert entered_in is second

    @pytest.mark.usefixtures("multiple_open_workflows")
    def test_a_manager_read_finds_no_workflow_while_several_are_open(self, engine: Engine) -> None:
        _open(engine, "first")
        _open(engine, "second")

        assert not GriptapeNodes.ContextManager().has_current_workflow()

    def test_engine_code_reading_a_manager_finds_no_workflow(self, engine: Engine) -> None:
        _open(engine, "only")

        assert not engine.context_manager.has_current_workflow()

    @pytest.mark.usefixtures("multiple_open_workflows")
    def test_a_request_fails_while_several_are_open(self, engine: Engine) -> None:
        first = _open(engine, "first")
        second = _open(engine, "second")

        result = _create_canvas()

        assert isinstance(result, WorkflowNotNamedResultFailure)
        assert not first.flows.name_to_parent_name
        assert not second.flows.name_to_parent_name

    @pytest.mark.usefixtures("multiple_open_workflows")
    def test_a_caller_bound_to_a_workflow_keeps_it(self, engine: Engine) -> None:
        first = _open(engine, "first")
        second = _open(engine, "second")

        with engine.bind_workflow(second):
            result = _create_canvas()

        assert isinstance(result, CreateFlowResultSuccess), result.result_details
        assert "canvas" in second.flows.name_to_parent_name
        assert not first.flows.name_to_parent_name

    def test_a_caller_bound_to_no_workflow_keeps_none(self, engine: Engine) -> None:
        only = _open(engine, "only")

        with engine.bind_workflow(NO_WORKFLOW):
            result = asyncio.run(GriptapeNodes.ahandle_request(CreateFlowRequest(parent_flow_name=None)))

        assert isinstance(result, WorkflowNotNamedResultFailure)
        assert not only.flows.name_to_parent_name
