"""Resending the input a parameter last received is not an edit, whatever its converters build."""

from __future__ import annotations

from typing import TYPE_CHECKING

from griptape_nodes.exe_types.core_types import Parameter
from griptape_nodes.exe_types.node_types import DataNode
from griptape_nodes.exe_types.param_types.parameter_image import ParameterImage
from griptape_nodes.retained_mode.events.parameter_events import SetParameterValueRequest

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.engine import Engine

IMAGE_URL = "https://example.com/cat.png"


class _Wrapped:
    """No `__eq__`, so two instances are never equal."""

    def __init__(self, value: object) -> None:
        self.value = value


class _Node(DataNode):
    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.add_parameter(ParameterImage(name="image", tooltip=""))
        self.add_parameter(Parameter(name="wrapped", type="any", tooltip="", converters=[_Wrapped]))

    def process(self) -> None:
        pass


def _set(engine: Engine, node: _Node, parameter_name: str, value: object) -> bool:
    request = SetParameterValueRequest(parameter_name=parameter_name, node_name=node.name, value=value)
    return engine.node_manager._set_and_pass_through_values(request, node).modified


class TestResentValue:
    def test_same_url_twice_is_not_a_change(self, engine: Engine) -> None:
        node = _Node("node")
        _set(engine, node, "image", IMAGE_URL)

        assert _set(engine, node, "image", IMAGE_URL) is False

    def test_a_different_url_is_a_change(self, engine: Engine) -> None:
        node = _Node("node")
        _set(engine, node, "image", IMAGE_URL)

        assert _set(engine, node, "image", "https://example.com/dog.png") is True

    def test_a_converter_building_identity_equal_objects_is_not_a_change(self, engine: Engine) -> None:
        node = _Node("node")
        _set(engine, node, "wrapped", "a")

        assert _set(engine, node, "wrapped", "a") is False
        assert _set(engine, node, "wrapped", "b") is True

    def test_resend_after_the_value_was_set_another_way_is_a_change(self, engine: Engine) -> None:
        node = _Node("node")
        _set(engine, node, "wrapped", "a")
        node.parameter_values["wrapped"] = _Wrapped("b")

        assert _set(engine, node, "wrapped", "a") is True
