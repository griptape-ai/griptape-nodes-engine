import pytest

from griptape_nodes.retained_mode.events import converters
from griptape_nodes.retained_mode.events.base_events import (
    EventRequest,
    EventResultFailure,
    EventResultSuccess,
)
from griptape_nodes.retained_mode.events.config_events import (
    GetConfigValueRequest,
    GetConfigValueResultFailure,
    GetConfigValueResultSuccess,
)
from griptape_nodes.retained_mode.events.converters import _resolve_payload_type


class TestResolvePayloadType:
    def test_resolves_registered_type(self) -> None:
        event_data = {"request_type": "GetConfigValueRequest"}
        resolved = _resolve_payload_type(event_data, "request_type")

        assert resolved is GetConfigValueRequest

    def test_pops_key_from_event_data(self) -> None:
        event_data = {"request_type": "GetConfigValueRequest", "other": "value"}
        _resolve_payload_type(event_data, "request_type")

        assert "request_type" not in event_data
        assert event_data["other"] == "value"

    def test_raises_when_key_missing(self) -> None:
        event_data = {}
        with pytest.raises(ValueError, match="not found"):
            _resolve_payload_type(event_data, "request_type")

    def test_raises_when_type_not_registered(self) -> None:
        event_data = {"request_type": "NonExistentType"}
        with pytest.raises(ValueError, match="not registered"):
            _resolve_payload_type(event_data, "request_type")


class TestEventRequestFromDict:
    def test_from_dict(self) -> None:
        data = {
            "event_type": "EventRequest",
            "request_type": "GetConfigValueRequest",
            "request": {"category_and_key": "foo.bar"},
        }
        event = converters.engine.structure(data, EventRequest)

        assert isinstance(event.request, GetConfigValueRequest)
        assert event.request.category_and_key == "foo.bar"

    def test_from_dict_missing_request_type(self) -> None:
        data = {
            "event_type": "EventRequest",
            "request": {"category_and_key": "foo.bar"},
        }
        with pytest.raises(ValueError, match="request_type"):
            converters.engine.structure(data, EventRequest)


class TestEventResultFromDict:
    def test_success_from_dict(self) -> None:
        data = {
            "event_type": "EventResultSuccess",
            "request_type": "GetConfigValueRequest",
            "result_type": "GetConfigValueResultSuccess",
            "request": {"category_and_key": "foo.bar"},
            "result": {"value": 42, "result_details": "ok"},
        }
        event = converters.engine.structure(data, EventResultSuccess)

        assert isinstance(event.request, GetConfigValueRequest)
        assert isinstance(event.result, GetConfigValueResultSuccess)
        expected_value = 42
        assert event.result.value == expected_value
        assert event.succeeded()

    def test_failure_from_dict(self) -> None:
        data = {
            "event_type": "EventResultFailure",
            "request_type": "GetConfigValueRequest",
            "result_type": "GetConfigValueResultFailure",
            "request": {"category_and_key": "foo.bar"},
            "result": {"result_details": "not found"},
        }
        event = converters.engine.structure(data, EventResultFailure)

        assert isinstance(event.request, GetConfigValueRequest)
        assert isinstance(event.result, GetConfigValueResultFailure)
        assert not event.succeeded()

    def test_from_dict_missing_result_type(self) -> None:
        data = {
            "event_type": "EventResultSuccess",
            "request_type": "GetConfigValueRequest",
            "request": {"category_and_key": "foo.bar"},
            "result": {"value": 42, "result_details": "ok"},
        }
        with pytest.raises(ValueError, match="result_type"):
            converters.engine.structure(data, EventResultSuccess)


class TestRoundTrip:
    def test_event_request_round_trip(self) -> None:
        request = GetConfigValueRequest(category_and_key="foo.bar")
        event = EventRequest(request=request)
        serialized = converters.engine.unstructure(event)

        assert serialized["request_type"] == "GetConfigValueRequest"

        restored = converters.engine.structure(serialized, EventRequest)

        assert isinstance(restored.request, GetConfigValueRequest)
        assert restored.request.category_and_key == "foo.bar"

    def test_event_request_round_trip_with_defaults(self) -> None:
        request = GetConfigValueRequest(category_and_key="a.b")
        event = EventRequest(request=request, request_id="test-123")
        serialized = converters.engine.unstructure(event)

        restored = converters.engine.structure(serialized, EventRequest)

        assert isinstance(restored.request, GetConfigValueRequest)
        assert restored.request.category_and_key == "a.b"
