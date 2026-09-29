"""Unit tests for LocalSessionWorkflowExecutor's CLI surface (issue #4599) and its event sends."""

import dataclasses
import json
from argparse import ArgumentParser
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from griptape_nodes.bootstrap.workflow_executors.local_session_workflow_executor import (
    LocalSessionWorkflowExecutor,
)
from griptape_nodes.bootstrap.workflow_executors.local_workflow_executor import LocalExecutorError
from griptape_nodes.drivers.storage import StorageBackend
from griptape_nodes.retained_mode.events.base_events import (
    EventRequest,
    EventResultSuccess,
    ExecutionEvent,
    ExecutionGriptapeNodeEvent,
    ExecutionPayload,
    RequestPayload,
    ResultPayloadSuccess,
)
from griptape_nodes.retained_mode.events.execution_events import ControlFlowResolvedEvent, StartFlowRequest


class _NoJsonForm:
    """A value neither the converter nor JSON knows how to write."""


@dataclasses.dataclass
class _UnsendableRequest(RequestPayload):
    anything: Any = None


@dataclasses.dataclass
class _UnsendableExecutionPayload(ExecutionPayload):
    anything: Any = None


@dataclasses.dataclass
class _UnsendableResultSuccess(ResultPayloadSuccess):
    anything: Any = None


class TestLocalSessionWorkflowExecutorCli:
    """Tests for LocalSessionWorkflowExecutor's CLI surface."""

    def test_add_cli_arguments_includes_storage_backend(self) -> None:
        parser = ArgumentParser()
        LocalSessionWorkflowExecutor.add_cli_arguments(parser)

        args = parser.parse_args([])

        assert args.storage_backend == StorageBackend.LOCAL.value

    def test_add_cli_arguments_includes_save_on_failure(self) -> None:
        parser = ArgumentParser()
        LocalSessionWorkflowExecutor.add_cli_arguments(parser)

        args = parser.parse_args(["--save-on-failure", "/var/dump.py"])

        assert args.save_on_failure == "/var/dump.py"

    def test_add_cli_arguments_includes_session_id(self) -> None:
        parser = ArgumentParser()
        LocalSessionWorkflowExecutor.add_cli_arguments(parser)

        args = parser.parse_args(["--session-id", "abc-123"])

        assert args.session_id == "abc-123"

    def test_add_cli_arguments_session_id_defaults_to_none(self) -> None:
        parser = ArgumentParser()
        LocalSessionWorkflowExecutor.add_cli_arguments(parser)

        args = parser.parse_args([])

        assert args.session_id is None

    def test_add_cli_arguments_includes_project_file_path(self) -> None:
        parser = ArgumentParser()
        LocalSessionWorkflowExecutor.add_cli_arguments(parser)

        args = parser.parse_args(["--project-file-path", "/some/project.yaml"])

        assert args.project_file_path == "/some/project.yaml"

    def test_cli_constructor_kwargs_includes_session_id(self) -> None:
        parser = ArgumentParser()
        LocalSessionWorkflowExecutor.add_cli_arguments(parser)
        args = parser.parse_args(["--session-id", "abc-123"])

        kwargs = LocalSessionWorkflowExecutor._cli_constructor_kwargs(args)

        assert kwargs["session_id"] == "abc-123"

    def test_cli_constructor_kwargs_project_file_path_converted_to_path(self) -> None:
        parser = ArgumentParser()
        LocalSessionWorkflowExecutor.add_cli_arguments(parser)
        args = parser.parse_args(["--project-file-path", "/some/project.yaml"])

        kwargs = LocalSessionWorkflowExecutor._cli_constructor_kwargs(args)

        assert kwargs["project_file_path"] == Path("/some/project.yaml")

    def test_cli_constructor_kwargs_storage_backend_converted_to_enum(self) -> None:
        parser = ArgumentParser()
        LocalSessionWorkflowExecutor.add_cli_arguments(parser)
        args = parser.parse_args(["--storage-backend", StorageBackend.GTC.value])

        kwargs = LocalSessionWorkflowExecutor._cli_constructor_kwargs(args)

        assert kwargs["storage_backend"] == StorageBackend.GTC


class TestSendEvent:
    """Tests for LocalSessionWorkflowExecutor._send_event."""

    def test_sends_a_serializable_execution_event(self) -> None:
        executor = LocalSessionWorkflowExecutor.__new__(LocalSessionWorkflowExecutor)
        executor.send_event = MagicMock()
        event = ExecutionEvent(payload=_UnsendableExecutionPayload(anything="a plain string"))

        executor._send_event("execution_event", event)

        executor.send_event.assert_called_once()
        sent_type, sent_payload = executor.send_event.call_args.args
        assert sent_type == "execution_event"
        assert json.loads(sent_payload)["payload"]["anything"] == "a plain string"

    def test_skips_an_unsendable_execution_event_without_raising(self) -> None:
        executor = LocalSessionWorkflowExecutor.__new__(LocalSessionWorkflowExecutor)
        executor.send_event = MagicMock()
        event = ExecutionEvent(payload=_UnsendableExecutionPayload(anything=_NoJsonForm()))

        executor._send_event("execution_event", event)

        executor.send_event.assert_not_called()

    def test_skips_an_unsendable_event_request_without_raising(self) -> None:
        executor = LocalSessionWorkflowExecutor.__new__(LocalSessionWorkflowExecutor)
        executor.send_event = MagicMock()
        event = EventRequest(request=_UnsendableRequest(anything=_NoJsonForm()))

        executor._send_event("event_request", event)

        executor.send_event.assert_not_called()


class TestSendResult:
    """Tests for LocalSessionWorkflowExecutor._send_result."""

    def test_sends_a_serializable_result(self) -> None:
        executor = LocalSessionWorkflowExecutor.__new__(LocalSessionWorkflowExecutor)
        executor.send_event = MagicMock()
        request = StartFlowRequest(flow_name="wf")
        event = EventResultSuccess(request=request, result=_UnsendableResultSuccess(result_details="ok"))

        executor._send_result("success_result", event)

        executor.send_event.assert_called_once()
        sent_type, _ = executor.send_event.call_args.args
        assert sent_type == "success_result"

    def test_answers_an_unsendable_result_with_a_generic_result_failure(self) -> None:
        executor = LocalSessionWorkflowExecutor.__new__(LocalSessionWorkflowExecutor)
        executor.send_event = MagicMock()
        request = StartFlowRequest(flow_name="wf", request_id="req-1")
        event = EventResultSuccess(
            request=request,
            result=_UnsendableResultSuccess(result_details="ok", anything=_NoJsonForm()),
            request_id="req-1",
            response_topic="sessions/abc/response",
        )

        executor._send_result("success_result", event)

        executor.send_event.assert_called_once()
        sent_type, sent_payload = executor.send_event.call_args.args
        assert sent_type == "failure_result"
        data = json.loads(sent_payload)
        assert data["result_type"] == "GenericResultFailure"
        assert data["request_id"] == "req-1"
        assert data["response_topic"] == "sessions/abc/response"
        assert data["request_type"] == "StartFlowRequest"
        assert "_UnsendableResultSuccess" in data["result"]["result_details"]["result_details"][0]["message"]

    def test_answers_a_result_whose_request_cannot_be_sent(self) -> None:
        executor = LocalSessionWorkflowExecutor.__new__(LocalSessionWorkflowExecutor)
        executor.send_event = MagicMock()
        request = _UnsendableRequest(anything=_NoJsonForm(), request_id="req-1")
        event = EventResultSuccess(
            request=request, result=_UnsendableResultSuccess(result_details="ok"), request_id="req-1"
        )

        executor._send_result("success_result", event)

        sent_type, sent_payload = executor.send_event.call_args.args
        assert sent_type == "failure_result"
        data = json.loads(sent_payload)
        assert data["request"] == {"request_id": "req-1"}
        assert data["request_type"] == "_UnsendableRequest"
        assert "_NoJsonForm" in data["result"]["result_details"]["result_details"][0]["message"]


class TestSendExecutionEvent:
    """A run whose result cannot be sent fails instead of reporting success with no output."""

    def test_unsendable_resolved_event_fails_the_run(self) -> None:
        executor = LocalSessionWorkflowExecutor.__new__(LocalSessionWorkflowExecutor)
        executor.send_event = MagicMock()
        resolved = ControlFlowResolvedEvent(end_node_name="End", parameter_output_values={"out": _NoJsonForm()})
        event = ExecutionGriptapeNodeEvent(wrapped_event=ExecutionEvent(payload=resolved))

        error = executor._send_execution_event(event)

        assert isinstance(error, LocalExecutorError)
        assert "workflow's result" in str(error)
        executor.send_event.assert_not_called()

    def test_unsendable_progress_event_is_skipped(self) -> None:
        executor = LocalSessionWorkflowExecutor.__new__(LocalSessionWorkflowExecutor)
        executor.send_event = MagicMock()
        payload = _UnsendableExecutionPayload(anything=_NoJsonForm())
        event = ExecutionGriptapeNodeEvent(wrapped_event=ExecutionEvent(payload=payload))

        assert executor._send_execution_event(event) is None
