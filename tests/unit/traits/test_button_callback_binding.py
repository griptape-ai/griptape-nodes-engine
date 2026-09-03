"""Callbacks on a run-time parameter survive a save by naming a method on the owning node."""

import pytest

from griptape_nodes.exe_types.core_types import NodeMessageResult, Parameter, Trait
from griptape_nodes.exe_types.node_types import BaseNode, sanctioned_parameter_mutation
from griptape_nodes.retained_mode.engine import Engine
from griptape_nodes.retained_mode.events.parameter_events import (
    AddParameterToNodeRequest,
    AddParameterToNodeResultSuccess,
)
from griptape_nodes.traits.button import Button, ButtonDetailsMessagePayload


class ButtonNode(BaseNode):
    """A node that grows a button at run time, as a model picker or refresh control does."""

    def __init__(self, name: str = "button_node", metadata: dict | None = None, **kwargs) -> None:
        super().__init__(name=name, metadata=metadata or {}, **kwargs)
        self.clicked_labels: list[str] = []

    def refresh(self, button: Button, button_payload: ButtonDetailsMessagePayload) -> NodeMessageResult:  # noqa: ARG002
        self.clicked_labels.append(button.label)
        return NodeMessageResult(success=True, details="refreshed", altered_workflow_state=False)

    def process(self) -> None:
        return None

    def add_button(self, parameter_name: str, button: Button) -> Parameter:
        parameter = Parameter(
            name=parameter_name, type="str", tooltip="t", user_defined=True, default_value="", traits={button}
        )
        with sanctioned_parameter_mutation():
            self.add_parameter(parameter)
        return parameter


def _button_of(parameter: Parameter) -> Button:
    return next(trait for trait in parameter.find_elements_by_type(Trait) if isinstance(trait, Button))


def _parameter_of(node: BaseNode, name: str) -> Parameter:
    parameter = node.get_parameter_by_name(name)
    assert parameter is not None
    return parameter


@pytest.fixture
def source_node() -> ButtonNode:
    """A node whose run-time button calls one of the node's own methods."""
    node = ButtonNode(name="source")
    node.add_button("models", Button(label="Refresh", on_click=node.refresh))
    return node


class TestNamingCallbacks:
    def test_a_node_method_is_saved_by_name(self, source_node: ButtonNode) -> None:
        states = _parameter_of(source_node, "models").trait_states()

        assert states[0]["trait_callbacks"] == {"on_click": "refresh"}

    def test_the_callback_itself_is_never_saved(self, source_node: ButtonNode) -> None:
        state = _parameter_of(source_node, "models").trait_states()[0]["trait_state"]

        assert "on_click" not in state
        assert all(not callable(value) for value in state.values())

    def test_a_lambda_cannot_be_named(self) -> None:
        node = ButtonNode()
        parameter = node.add_button("lam", Button(label="L", on_click=lambda _button, _payload: None))

        assert _button_of(parameter).unnameable_callbacks(node) == ["on_click"]
        assert "trait_callbacks" not in parameter.trait_states()[0]

    def test_a_handler_the_trait_derives_from_state_needs_no_name(self) -> None:
        # button_link is saved as state, so the constructor rebuilds its handler on load.
        node = ButtonNode()
        parameter = node.add_button("link", Button(label="K", button_link="https://example.test"))

        assert _button_of(parameter).unnameable_callbacks(node) == []


class TestRebindingCallbacks:
    def test_a_saved_button_still_fires_after_a_reload(self, engine: Engine, source_node: ButtonNode) -> None:
        target = ButtonNode(name="target")
        engine.object_manager.add_object_by_name(target.name, target)
        source = _parameter_of(source_node, "models")

        parameter_dict = source.to_dict()
        parameter_dict["initial_setup"] = True
        parameter_dict["ui_options"] = source.authored_ui_options()
        parameter_dict["traits"] = source.trait_states()
        result = engine.handle_request(AddParameterToNodeRequest.create(node_name=target.name, **parameter_dict))
        assert isinstance(result, AddParameterToNodeResultSuccess)

        click = target.on_node_message_received(
            optional_element_name="models", message_type=Button.ON_CLICK_MESSAGE_TYPE, message=None
        )

        assert click.success
        assert target.clicked_labels == ["Refresh"]

    def test_the_callback_binds_to_the_loading_node_not_the_saved_one(
        self, engine: Engine, source_node: ButtonNode
    ) -> None:
        target = ButtonNode(name="target")
        engine.object_manager.add_object_by_name(target.name, target)
        source = _parameter_of(source_node, "models")

        parameter_dict = source.to_dict()
        parameter_dict["initial_setup"] = True
        parameter_dict["ui_options"] = source.authored_ui_options()
        parameter_dict["traits"] = source.trait_states()
        engine.handle_request(AddParameterToNodeRequest.create(node_name=target.name, **parameter_dict))

        restored = _parameter_of(target, "models")
        callback = _button_of(restored).on_click_callback
        assert callback is not None
        assert callback.__self__ is target  # type: ignore[attr-defined]

    def test_a_missing_method_loads_without_the_behavior(self, engine: Engine) -> None:
        target = ButtonNode(name="target")
        engine.object_manager.add_object_by_name(target.name, target)

        result = engine.handle_request(
            AddParameterToNodeRequest(
                node_name=target.name,
                parameter_name="models",
                tooltip="t",
                type="str",
                traits=[
                    {
                        "trait_name": "Button",
                        "trait_state": {"label": "Refresh"},
                        "trait_callbacks": {"on_click": "method_that_went_away"},
                    }
                ],
            )
        )
        assert isinstance(result, AddParameterToNodeResultSuccess)

        restored = _parameter_of(target, "models")
        assert _button_of(restored).label == "Refresh"
        assert _button_of(restored).on_click_callback is None

    def test_a_constructor_supplied_callback_wins_over_a_saved_name(self) -> None:
        """A node that rewires a button in a new version keeps the new wiring."""
        from griptape_nodes.retained_mode.managers.node_manager import NodeManager

        target = ButtonNode(name="target")
        parameter = target.add_button("models", Button(label="Refresh", on_click=target.refresh))
        live_callback = _button_of(parameter).on_click_callback

        NodeManager._apply_trait_callbacks(
            parameter,
            [{"trait_name": "Button", "trait_callbacks": {"on_click": "process"}}],
        )

        assert _button_of(parameter).on_click_callback is live_callback
