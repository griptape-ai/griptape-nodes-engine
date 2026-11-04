"""Unit tests for LocalSessionWorkflowPublisher's event sends."""

import dataclasses
import json
from typing import Any
from unittest.mock import MagicMock

from griptape_nodes.bootstrap.workflow_publishers.local_session_workflow_publisher import (
    LocalSessionWorkflowPublisher,
)
from griptape_nodes.retained_mode.events.base_events import (
    EventResultSuccess,
    ExecutionEvent,
    ExecutionPayload,
    ResultPayloadSuccess,
)
from griptape_nodes.retained_mode.events.workflow_events import PublishWorkflowRequest


class _NoJsonForm:
    """A value neither the converter nor JSON knows how to write."""


@dataclasses.dataclass
class _UnsendableExecutionPayload(ExecutionPayload):
    anything: Any = None


@dataclasses.dataclass
class _UnsendableResultSuccess(ResultPayloadSuccess):
    anything: Any = None


class TestSendEvent:
    """Tests for LocalSessionWorkflowPublisher._send_event."""

    def test_sends_a_serializable_execution_event(self) -> None:
        publisher = LocalSessionWorkflowPublisher.__new__(LocalSessionWorkflowPublisher)
        publisher.send_event = MagicMock()
        event = ExecutionEvent(payload=_UnsendableExecutionPayload(anything="a plain string"))

        publisher._send_event("execution_event", event)

        publisher.send_event.assert_called_once()
        sent_type, sent_payload = publisher.send_event.call_args.args
        assert sent_type == "execution_event"
        assert json.loads(sent_payload)["payload"]["anything"] == "a plain string"

    def test_skips_an_unsendable_execution_event_without_raising(self) -> None:
        publisher = LocalSessionWorkflowPublisher.__new__(LocalSessionWorkflowPublisher)
        publisher.send_event = MagicMock()
        event = ExecutionEvent(payload=_UnsendableExecutionPayload(anything=_NoJsonForm()))

        publisher._send_event("execution_event", event)

        publisher.send_event.assert_not_called()


class TestSendResult:
    """Tests for LocalSessionWorkflowPublisher._send_result."""

    def test_sends_a_serializable_result(self) -> None:
        publisher = LocalSessionWorkflowPublisher.__new__(LocalSessionWorkflowPublisher)
        publisher.send_event = MagicMock()
        request = PublishWorkflowRequest(workflow_name="wf", publisher_name="pub")
        event = EventResultSuccess(request=request, result=_UnsendableResultSuccess(result_details="ok"))

        publisher._send_result("success_result", event)

        publisher.send_event.assert_called_once()
        sent_type, _ = publisher.send_event.call_args.args
        assert sent_type == "success_result"

    def test_answers_an_unsendable_result_with_a_generic_result_failure(self) -> None:
        publisher = LocalSessionWorkflowPublisher.__new__(LocalSessionWorkflowPublisher)
        publisher.send_event = MagicMock()
        request = PublishWorkflowRequest(workflow_name="wf", publisher_name="pub", request_id="req-1")
        event = EventResultSuccess(
            request=request,
            result=_UnsendableResultSuccess(result_details="ok", anything=_NoJsonForm()),
            request_id="req-1",
            response_topic="sessions/abc/response",
        )

        publisher._send_result("success_result", event)

        publisher.send_event.assert_called_once()
        sent_type, sent_payload = publisher.send_event.call_args.args
        assert sent_type == "failure_result"
        data = json.loads(sent_payload)
        assert data["result_type"] == "GenericResultFailure"
        assert data["request_id"] == "req-1"
        assert data["response_topic"] == "sessions/abc/response"
        assert data["request_type"] == "PublishWorkflowRequest"
