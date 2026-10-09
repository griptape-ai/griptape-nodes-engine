import json
from dataclasses import dataclass

from griptape_nodes.retained_mode.events import converters
from griptape_nodes.retained_mode.events.base_events import EventRequest, EventRequestBatch, RequestPayload
from griptape_nodes.retained_mode.events.execution_events import ParameterValueUpdateEvent
from griptape_nodes.retained_mode.events.parameter_events import SetParameterValueRequest
from griptape_nodes.serialization.converter import ElementDocument


@dataclass
class _HoldsAnyRequest:
    request: RequestPayload


@dataclass
class _Shape:
    name: str


@dataclass
class _Circle(_Shape):
    radius: float


class TestEngineAndClient:
    def test_engine_tags_values_and_client_does_not(self) -> None:
        event = ParameterValueUpdateEvent(node_name="n", parameter_name="p", data_type="any", value=(1, 2))

        engine = json.loads(converters.engine.dumps(event))
        client = json.loads(converters.client.dumps(event))

        assert engine["value"] == {"$type": "builtins:tuple", "$value": [1, 2]}
        assert client["value"] == [1, 2]

    def test_element_document_values_use_the_converter_writing_them(self) -> None:
        document = {"element_id": "p1", "value": (1, 2), "children": [{"element_id": "c", "value": (3,)}]}

        sent = converters.client.unstructure(document, ElementDocument)

        assert sent == {"element_id": "p1", "value": [1, 2], "children": [{"element_id": "c", "value": [3]}]}

    def test_request_inside_a_payload_uses_the_converter_writing_it(self) -> None:
        holder = _HoldsAnyRequest(request=SetParameterValueRequest(parameter_name="p", value=(1,)))

        sent = converters.client.unstructure(holder)

        assert sent["request"]["request_type"] == "SetParameterValueRequest"
        assert sent["request"]["request"]["value"] == [1]

    def test_events_inside_a_batch_use_the_converter_writing_it(self) -> None:
        batch = EventRequestBatch(
            requests=[EventRequest(request=SetParameterValueRequest(parameter_name="p", value=(1,)))]
        )

        sent = converters.client.unstructure(batch)

        assert sent["requests"][0]["request"]["value"] == [1]


class TestEventConverter:
    def test_has_every_event_hook(self) -> None:
        conv = converters.EventConverter(value=lambda _: "value", display=lambda _: "display")

        sent = conv.unstructure(_HoldsAnyRequest(request=SetParameterValueRequest(parameter_name="p", value=1)))

        assert sent["request"]["request_type"] == "SetParameterValueRequest"
        assert sent["request"]["request"]["value"] == "display"

    def test_polymorphic_dataclass_registers_on_every_converter(self) -> None:
        conv = converters.EventConverter(value=lambda v: v, display=lambda v: v)

        converters.register_polymorphic_dataclass(_Shape)

        for converter in (conv, converters.engine, converters.client):
            assert isinstance(converter.structure({"name": "c", "radius": 1.0}, _Shape), _Circle)
