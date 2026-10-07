"""Parameter values reach the editor and API clients tagged with their type.

Fields that carry a value to a client are typed ``DisplayValue``, and element trees are
``ElementDocument``. Both encode at the wire, and decode when they come back, so a value
keeps its type across the trip and is never encoded twice.
"""

from __future__ import annotations

import json
from enum import StrEnum
from typing import TYPE_CHECKING, Any

import pytest
from griptape.artifacts import ImageUrlArtifact

from griptape_nodes.retained_mode.events.base_events import (
    EventRequest,
    EventResultSuccess,
    ExecutionEvent,
)
from griptape_nodes.retained_mode.events.execution_events import NodeResolvedEvent, ParameterValueUpdateEvent
from griptape_nodes.retained_mode.events.node_events import (
    CreateNodeRequest,
    CreateNodeResultSuccess,
    GetAllNodeInfoRequest,
    GetAllNodeInfoResultSuccess,
)
from griptape_nodes.retained_mode.events.parameter_events import (
    AlterElementEvent,
    GetNodeElementDetailsRequest,
    GetNodeElementDetailsResultSuccess,
    GetParameterValueRequest,
    GetParameterValueResultSuccess,
    SetParameterValueRequest,
)
from griptape_nodes.retained_mode.events.variable_events import (
    GetVariableRequest,
    GetVariableResultSuccess,
    SetVariableValueRequest,
)
from griptape_nodes.retained_mode.variable_types import FlowVariable
from griptape_nodes.serialization.converter import converter
from griptape_nodes.serialization.values import TYPE_KEY, VALUE_KEY, encode_value

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.engine import Engine


class Speed(StrEnum):
    FAST = "fast"


class _Opaque:
    def __str__(self) -> str:
        return "opaque thing"


def _element_id(engine: Engine, node_name: str, parameter_name: str) -> str:
    parameter = engine.node_manager.get_node_by_name(node_name).get_parameter_by_name(parameter_name)
    assert parameter is not None
    return parameter.element_id


def _wire(event: Any) -> dict[str, Any]:
    return json.loads(event.json())


def _execution_payload(payload: Any) -> dict[str, Any]:
    return _wire(ExecutionEvent(payload=payload))["payload"]


class TestDisplayValueFields:
    def test_parameter_update_is_tagged(self) -> None:
        payload = _execution_payload(
            ParameterValueUpdateEvent(node_name="n", parameter_name="p", data_type="any", value=(1, Speed.FAST))
        )

        assert payload["value"] == encode_value((1, Speed.FAST))

    def test_value_with_no_plain_data_form_is_sent_as_text(self) -> None:
        payload = _execution_payload(
            ParameterValueUpdateEvent(node_name="n", parameter_name="p", data_type="any", value=_Opaque())
        )

        assert payload["value"] == "opaque thing"

    def test_node_outputs_are_tagged(self) -> None:
        image = ImageUrlArtifact("https://example.com/a.png", name="a")

        payload = _execution_payload(
            NodeResolvedEvent(node_name="n", parameter_output_values={"image": image}, node_type="T")
        )

        assert payload["parameter_output_values"]["image"][TYPE_KEY] == (
            "griptape.artifacts.image_url_artifact:ImageUrlArtifact"
        )

    def test_tagged_value_sent_by_a_client_arrives_with_its_type(self) -> None:
        request = EventRequest.from_dict(
            {
                "request_type": "SetParameterValueRequest",
                "request": {"parameter_name": "p", "value": {TYPE_KEY: "builtins:tuple", VALUE_KEY: [1, 2]}},
            }
        )

        assert request.request.value == (1, 2)

    def test_untagged_artifact_dict_sent_by_the_editor_stays_a_dict(self) -> None:
        artifact = {"type": "ImageUrlArtifact", "value": "https://example.com/a.png"}

        request = EventRequest.from_dict(
            {"request_type": "SetParameterValueRequest", "request": {"parameter_name": "p", "value": artifact}}
        )

        assert request.request.value == artifact


class TestVariableValueFields:
    def test_variable_value_is_tagged(self) -> None:
        image = ImageUrlArtifact("https://example.com/a.png", name="a")

        request = _wire(EventRequest(request=SetVariableValueRequest(name="v", value=image)))["request"]

        assert request["value"] == encode_value(image)

    def test_variable_in_a_result_is_tagged(self) -> None:
        request = GetVariableRequest(name="v")
        result = GetVariableResultSuccess(
            result_details="found",
            variable=FlowVariable(name="v", owning_flow_name=None, type="any", value=(1, Speed.FAST)),
        )

        wire = _wire(EventResultSuccess(request=request, result=result))

        assert wire["result"]["variable"]["value"] == encode_value((1, Speed.FAST))

    def test_tagged_value_sent_by_a_client_arrives_with_its_type(self) -> None:
        request = EventRequest.from_dict(
            {
                "request_type": "SetVariableValueRequest",
                "request": {"name": "v", "value": {TYPE_KEY: "builtins:tuple", VALUE_KEY: [1, 2]}},
            }
        )

        assert request.request.value == (1, 2)


class TestElementDocuments:
    def _document(self) -> dict[str, Any]:
        return {
            "element_id": "root",
            "ui_options": {"speed": Speed.FAST},
            "children": [
                {"element_id": "a", "value": (1, 2), "default_value": Speed.FAST, "children": []},
            ],
            "element_id_to_value": {"a": (1, 2)},
        }

    def test_values_are_tagged_and_other_entries_are_not(self) -> None:
        payload = _execution_payload(AlterElementEvent(element_details=self._document()))["element_details"]

        (child,) = payload["children"]
        assert child["value"] == encode_value((1, 2))
        assert child["default_value"] == encode_value(Speed.FAST)
        assert payload["element_id_to_value"] == {"a": encode_value((1, 2))}
        assert payload["ui_options"] == {"speed": "fast"}

    def test_forwarding_a_document_does_not_encode_its_values_twice(self) -> None:
        once = converter.unstructure(AlterElementEvent(element_details=self._document()))

        forwarded = converter.unstructure(converter.structure(once, AlterElementEvent))

        assert forwarded == once


@pytest.mark.usefixtures("flow_name")
class TestHandlers:
    @pytest.fixture
    def node_name(self, engine: Engine, library_name: str) -> str:
        result = engine.handle_request(
            CreateNodeRequest(node_type="LegacyValuesNode", specific_library_name=library_name, node_name="Holder")
        )
        assert isinstance(result, CreateNodeResultSuccess), result
        set_result = engine.handle_request(
            SetParameterValueRequest(node_name=result.node_name, parameter_name="pair", value=(1, "b"))
        )
        assert set_result.succeeded(), set_result
        return result.node_name

    def test_get_parameter_value_in_process_returns_the_value_itself(self, engine: Engine, node_name: str) -> None:
        result = engine.handle_request(GetParameterValueRequest(node_name=node_name, parameter_name="pair"))

        assert isinstance(result, GetParameterValueResultSuccess)
        assert result.value == (1, "b")

    def test_get_parameter_value_over_the_wire_is_tagged(self, engine: Engine, node_name: str) -> None:
        request = GetParameterValueRequest(node_name=node_name, parameter_name="pair")
        result = engine.handle_request(request)

        wire = _wire(EventResultSuccess(request=request, result=result))

        assert wire["result"]["value"] == encode_value((1, "b"))

    def test_element_details_carry_tagged_values(self, engine: Engine, node_name: str) -> None:
        request = GetNodeElementDetailsRequest(node_name=node_name)
        result = engine.handle_request(request)
        assert isinstance(result, GetNodeElementDetailsResultSuccess)
        pair_id = _element_id(engine, node_name, "pair")

        wire = _wire(EventResultSuccess(request=request, result=result))

        assert wire["result"]["element_details"]["element_id_to_value"][pair_id] == encode_value((1, "b"))

    def test_all_node_info_carries_tagged_values(self, engine: Engine, node_name: str) -> None:
        request = GetAllNodeInfoRequest(node_name=node_name)
        result = engine.handle_request(request)
        assert isinstance(result, GetAllNodeInfoResultSuccess)
        pair_id = _element_id(engine, node_name, "pair")

        wire = _wire(EventResultSuccess(request=request, result=result))

        assert wire["result"]["element_id_to_value"][pair_id] == encode_value((1, "b"))
