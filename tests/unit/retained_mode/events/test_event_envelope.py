"""Tests for the serialized event envelope.

The ``{field}_type`` keys identify concrete payload classes for deserialization, and
production consumers dispatch on ``event_type`` (``subprocess_workflow_executor``,
``request_client``, ``worker_manager``) or derive success from ``result_type`` (``mcp``). Dropping
either would leave those paths hanging or silently reporting failure, so they are pinned here.
"""

import json
from pathlib import Path

import pytest

from griptape_nodes.retained_mode.events import converters
from griptape_nodes.retained_mode.events.app_events import AppInitializationComplete
from griptape_nodes.retained_mode.events.base_events import (
    AppEvent,
    BaseEvent,
    EventRequest,
    EventRequestBatch,
    EventResultFailure,
    EventResultSuccess,
    ExecutionEvent,
)
from griptape_nodes.retained_mode.events.execution_events import CurrentControlNodeEvent
from griptape_nodes.retained_mode.events.parameter_events import (
    GetParameterValueRequest,
    GetParameterValueResultFailure,
    GetParameterValueResultSuccess,
)

GOLDEN_PATH = Path(__file__).parent / "data" / "event_envelope_golden.json"


def _request() -> GetParameterValueRequest:
    return GetParameterValueRequest(parameter_name="p", node_name="n")


def _success() -> GetParameterValueResultSuccess:
    return GetParameterValueResultSuccess(
        input_types=["str"],
        type="str",
        output_type="str",
        value={"a": [1, 2, {"b": "c"}]},
        result_details="done",
    )


def _events() -> dict[str, BaseEvent]:
    return {
        "EventRequest": EventRequest(request=_request(), request_id="r1", response_topic="t"),
        "EventRequestBatch": EventRequestBatch(requests=[EventRequest(request=_request(), request_id="a")]),
        "EventResultSuccess": EventResultSuccess(
            request=_request(), result=_success(), request_id="r1", retained_mode="m"
        ),
        "EventResultFailure": EventResultFailure(
            request=_request(),
            result=GetParameterValueResultFailure(result_details="nope"),
            request_id="r2",
        ),
        "ExecutionEvent": ExecutionEvent(payload=CurrentControlNodeEvent(node_name="n")),
        "AppEvent": AppEvent(payload=AppInitializationComplete()),
    }


@pytest.mark.parametrize("name", list(_events()))
def test_event_type_is_the_class_name(name: str) -> None:
    """Consumers dispatch on event_type; it must name the concrete event class."""
    event = _events()[name]
    assert converters.engine.unstructure(event)["event_type"] == name


@pytest.mark.parametrize(
    ("name", "expected_keys"),
    [
        ("EventRequest", {"request_type"}),
        ("EventResultSuccess", {"request_type", "result_type"}),
        ("EventResultFailure", {"request_type", "result_type"}),
        ("ExecutionEvent", {"payload_type"}),
        ("AppEvent", {"payload_type"}),
    ],
)
def test_payload_type_keys_are_present(name: str, expected_keys: set[str]) -> None:
    """The converter resolves concrete payload classes from these keys."""
    serialized = converters.engine.unstructure(_events()[name])
    type_keys = {key for key in serialized if key.endswith("_type") and key != "event_type"}
    assert type_keys == expected_keys
    for key in expected_keys:
        assert serialized[key], f"{key} must name a payload class"


def test_result_type_names_the_concrete_result_class() -> None:
    """The MCP server derives ok from result_type.endswith('Success')."""
    success = converters.engine.unstructure(_events()["EventResultSuccess"])
    failure = converters.engine.unstructure(_events()["EventResultFailure"])
    assert success["result_type"] == "GetParameterValueResultSuccess"
    assert failure["result_type"] == "GetParameterValueResultFailure"
    assert success["result_type"].endswith("Success")
    assert not failure["result_type"].endswith("Success")


@pytest.mark.parametrize(
    ("name", "cls", "payload_attr", "payload_type"),
    [
        ("EventRequest", EventRequest, "request", GetParameterValueRequest),
        ("EventResultSuccess", EventResultSuccess, "result", GetParameterValueResultSuccess),
        ("ExecutionEvent", ExecutionEvent, "payload", CurrentControlNodeEvent),
        ("AppEvent", AppEvent, "payload", AppInitializationComplete),
    ],
)
def test_round_trips_through_json(name: str, cls: type, payload_attr: str, payload_type: type) -> None:
    """A serialized event survives a JSON hop and rebuilds its concrete payload type.

    Only the payload is compared: result-event structuring does not restore request_id or
    retained_mode, which is out of scope for this test.
    """
    original = _events()[name]
    restored = converters.engine.structure(
        json.loads(json.dumps(converters.engine.unstructure(original), default=str)), cls
    )
    assert type(restored) is cls
    assert type(getattr(restored, payload_attr)) is payload_type
    assert (
        converters.engine.unstructure(restored)[payload_attr] == converters.engine.unstructure(original)[payload_attr]
    )


def test_batch_round_trips_through_json() -> None:
    """The batch envelope rebuilds each inner request with its concrete payload type."""
    original = _events()["EventRequestBatch"]
    restored = converters.engine.structure(
        json.loads(json.dumps(converters.engine.unstructure(original), default=str)), EventRequestBatch
    )
    assert [type(inner.request) for inner in restored.requests] == [GetParameterValueRequest]
    assert converters.engine.unstructure(restored) == converters.engine.unstructure(original)


def test_payload_fields_hold_unstructured_output() -> None:
    """Payload fields use converter hooks, not pydantic serialization."""
    serialized = converters.engine.unstructure(_events()["EventResultSuccess"])
    assert serialized["request"]["parameter_name"] == "p"
    assert serialized["result"]["value"] == {"a": [1, 2, {"b": "c"}]}
    # result_details normalizes into structured ResultDetail entries during unstructuring.
    assert serialized["result"]["result_details"] == {"result_details": [{"level": 10, "message": "done"}]}


def test_non_payload_fields_survive_exclusion() -> None:
    """Excluding payload fields must not drop the scalar envelope fields beside them."""
    serialized = converters.engine.unstructure(_events()["EventResultSuccess"])
    assert serialized["request_id"] == "r1"
    assert serialized["retained_mode"] == "m"
    assert "response_topic" in serialized


def test_batch_inner_requests_keep_their_own_metadata() -> None:
    """Each inner request carries its own type keys."""
    serialized = converters.engine.unstructure(_events()["EventRequestBatch"])
    inner = serialized["requests"][0]
    assert inner["event_type"] == "EventRequest"
    assert inner["request_type"] == "GetParameterValueRequest"


def test_serialized_key_order_puts_payload_last() -> None:
    """The payload field is serialized last, after the envelope scalars and type metadata.

    Excluding the payload from ``model_dump`` moves it from its declared position to the end,
    where the event hook assigns it. Nothing parses the wire format positionally, so this is the
    only place key order is pinned: it documents the ordering as a deliberate, known property
    rather than treating every consumer of the golden fixture as an incidental lock on it.
    """
    assert list(converters.engine.unstructure(_events()["EventResultSuccess"])) == [
        "request_id",
        "response_topic",
        "retained_mode",
        "event_type",
        "request_type",
        "result_type",
        "request",
        "result",
    ]
    assert list(converters.engine.unstructure(_events()["ExecutionEvent"])) == [
        "event_type",
        "payload_type",
        "payload",
    ]


@pytest.mark.parametrize("name", list(_events()))
def test_matches_golden_output(name: str) -> None:
    """Serialized output matches a frozen capture of every key and value.

    Key order is not asserted here: it is not part of the wire contract and is pinned separately
    in ``test_serialized_key_order_puts_payload_last``. Regenerate with ``_write_golden`` below,
    and diff the result rather than overwriting it blind.
    """
    golden = json.loads(GOLDEN_PATH.read_text())
    serialized = json.loads(json.dumps(converters.engine.unstructure(_events()[name]), default=str))
    assert serialized == golden[name]


def _write_golden() -> None:
    """Regenerate the golden capture. Run by hand, not by the suite."""
    captured = {name: converters.engine.unstructure(event) for name, event in _events().items()}
    GOLDEN_PATH.write_text(json.dumps(captured, indent=2, default=str) + "\n")
