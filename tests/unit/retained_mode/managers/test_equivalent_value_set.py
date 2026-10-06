"""Setting a parameter to a value equal in content to the one it holds is not an edit."""

from __future__ import annotations

from typing import TYPE_CHECKING

from griptape_nodes.exe_types.node_types import DataNode
from griptape_nodes.exe_types.param_types.parameter_image import ParameterImage
from griptape_nodes.retained_mode.events.parameter_events import SetParameterValueRequest

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.engine import Engine

IMAGE_URL = "https://example.com/cat.png"


class _ImageNode(DataNode):
    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.add_parameter(ParameterImage(name="image", tooltip=""))

    def process(self) -> None:
        pass


def _set(engine: Engine, node: _ImageNode, value: object) -> bool:
    request = SetParameterValueRequest(parameter_name="image", node_name=node.name, value=value)
    return engine.node_manager._set_and_pass_through_values(request, node).modified


class TestEquivalentMediaValue:
    def test_same_url_twice_is_not_a_change(self, engine: Engine) -> None:
        """Each set builds a fresh artifact with a random id; that alone must not unresolve downstream."""
        node = _ImageNode("image_node")
        _set(engine, node, IMAGE_URL)

        assert _set(engine, node, IMAGE_URL) is False

    def test_a_different_url_is_a_change(self, engine: Engine) -> None:
        node = _ImageNode("image_node")
        _set(engine, node, IMAGE_URL)

        assert _set(engine, node, "https://example.com/dog.png") is True
