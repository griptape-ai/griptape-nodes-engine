"""Tests for execution event payloads."""

import json

from griptape.artifacts import ImageUrlArtifact

from griptape_nodes.retained_mode.events import converters
from griptape_nodes.retained_mode.events.base_events import EventRequest, EventResultSuccess, SkipTheLineMixin
from griptape_nodes.retained_mode.events.execution_events import (
    CancelExecuteNodeRequest,
    ExecuteNodeRequest,
    ExecuteNodeResultSuccess,
)


class TestCancelExecuteNodeEvents:
    def test_request_is_skip_the_line(self) -> None:
        request = CancelExecuteNodeRequest(target_request_id="req-123")

        assert isinstance(request, SkipTheLineMixin)

    def test_request_broadcast_result_defaults_false(self) -> None:
        request = CancelExecuteNodeRequest(target_request_id="req-123")

        assert request.broadcast_result is False


class TestExecuteNodeWireForm:
    """Parameter values sent to and from a worker come back with their exact types."""

    def test_request_values_survive_the_wire(self) -> None:
        artifact = ImageUrlArtifact("https://example.com/a.png", name="a")
        request = ExecuteNodeRequest(node_name="n", parameter_values={"image": artifact, "pair": (1, "b")})

        received = converters.engine.structure(
            json.loads(converters.engine.dumps(EventRequest(request=request))), EventRequest
        )

        values = received.request.parameter_values
        assert type(values["image"]) is ImageUrlArtifact
        assert values["image"].to_dict() == artifact.to_dict()
        assert values["pair"] == (1, "b")

    def test_result_values_survive_the_wire(self) -> None:
        result = ExecuteNodeResultSuccess(
            parameter_output_values={"blob": b"\x00\x01", "tags": {"a", "b"}}, result_details="ok"
        )
        event = EventResultSuccess(request=ExecuteNodeRequest(node_name="n"), result=result)

        received = converters.engine.structure(json.loads(converters.engine.dumps(event)), EventResultSuccess)

        assert received.result.parameter_output_values == {"blob": b"\x00\x01", "tags": {"a", "b"}}
