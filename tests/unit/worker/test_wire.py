"""Reading what crosses a worker's channel: anything that is not what its type promises is refused, saying why."""

from __future__ import annotations

import pytest

from griptape_nodes.retained_mode.events.app_events import GetEngineVersionRequest
from griptape_nodes.retained_mode.events.base_events import EventRequest
from griptape_nodes.worker.wire import (
    WireFormatError,
    WorkerHello,
    event_request_envelope,
    read_event_request,
    read_payload,
    request_failure,
)


class TestReadEventRequest:
    def test_a_request_reads_back_as_itself(self) -> None:
        envelope = event_request_envelope(EventRequest(request=GetEngineVersionRequest()))

        assert isinstance(read_event_request(envelope).request, GetEngineVersionRequest)
        assert envelope["payload"]["request_id"]

    def test_a_request_type_nothing_registered_is_refused(self) -> None:
        envelope = event_request_envelope(EventRequest(request=GetEngineVersionRequest()))
        envelope["payload"]["request_type"] = "NoSuchRequest"

        with pytest.raises(WireFormatError, match="'NoSuchRequest' could not be read"):
            read_event_request(envelope)

    def test_a_payload_that_is_not_a_request_is_refused(self) -> None:
        envelope = event_request_envelope(EventRequest(request=GetEngineVersionRequest()))
        envelope["payload"]["request_type"] = "GetEngineVersionResultSuccess"
        envelope["payload"]["request"] = {"major": 1, "minor": 2, "patch": 3, "result_details": "ok"}

        with pytest.raises(WireFormatError, match="is not a request"):
            read_event_request(envelope)


def test_a_failure_answers_even_an_envelope_with_no_payload() -> None:
    """The requester still hears back, with what little there is to echo."""
    failure = request_failure({"type": "EventRequest"}, "it was not a single request")

    assert failure["event_type"] == "EventResultFailure"
    assert failure["result_type"] == "GenericResultFailure"
    assert failure["request"] == {}


def test_a_payload_missing_its_fields_is_refused() -> None:
    """A hello that names no engine version cannot be checked, so it is not read as one."""
    with pytest.raises(WireFormatError, match="'WorkerHello' could not be read"):
        read_payload({"type": "WorkerHello", "payload": {"loaded": True}}, WorkerHello)
