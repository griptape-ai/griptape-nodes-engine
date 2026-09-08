"""The production save path.

On_serialize_node_to_commands emits traits/value_callbacks that AddParameterToNodeRequest
can replay onto a fresh node.
"""

import logging
from collections.abc import Generator

import pytest

from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
from griptape_nodes.exe_types.node_types import BaseNode, sanctioned_parameter_mutation
from griptape_nodes.node_library.library_registry import (
    LibraryMetadata,
    LibraryRegistry,
    LibrarySchema,
    NodeMetadata,
)
from griptape_nodes.retained_mode.engine import Engine
from griptape_nodes.retained_mode.events.context_events import (
    EnsureWorkflowAndFlowRequest,
    EnsureWorkflowAndFlowResultSuccess,
)
from griptape_nodes.retained_mode.events.node_events import (
    SerializeNodeToCommandsRequest,
    SerializeNodeToCommandsResultSuccess,
)
from griptape_nodes.retained_mode.events.parameter_events import (
    AddParameterToNodeRequest,
    AddParameterToNodeResultSuccess,
)
from griptape_nodes.traits.button import Button
from griptape_nodes.traits.options import Options

_LIBRARY_NAME = "trait-state-serialization-test-library"


class _ModelPicker(BaseNode):
    """A node that grows a dropdown and a button at run time, as a model picker does."""

    def process(self) -> None:
        return None

    def reload_models(self, button: Button, button_payload) -> None:  # noqa: ANN001, ARG002
        return None

    def strip_padding(self, value: str) -> str:
        return value.strip() if isinstance(value, str) else value

    def discover(self) -> None:
        with sanctioned_parameter_mutation():
            self.add_parameter(
                Parameter(
                    name="model",
                    type="str",
                    default_value="sd3",
                    tooltip="t",
                    user_defined=True,
                    allowed_modes={ParameterMode.PROPERTY},
                    traits={Options(choices=["sdxl", "sd3", "flux"])},
                    converters=[self.strip_padding],
                )
            )
            self.add_parameter(
                Parameter(
                    name="reload",
                    type="str",
                    default_value="",
                    tooltip="t",
                    user_defined=True,
                    allowed_modes={ParameterMode.PROPERTY},
                    traits={Button(label="Reload", on_click=self.reload_models)},
                )
            )
            self.add_parameter(
                Parameter(
                    name="unsaveable",
                    type="str",
                    default_value="",
                    tooltip="t",
                    user_defined=True,
                    allowed_modes={ParameterMode.PROPERTY},
                    traits={Button(label="Lambda", on_click=lambda _button, _payload: None)},
                )
            )


@pytest.fixture(autouse=True)
def _registered_node_type() -> Generator[None, None, None]:
    """Register _ModelPicker under a real library, matching what CreateNodeRequest sets up."""
    LibraryRegistry._clear()
    schema = LibrarySchema(
        name=_LIBRARY_NAME,
        library_schema_version=LibrarySchema.LATEST_SCHEMA_VERSION,
        metadata=LibraryMetadata(author="t", description="d", library_version="1.0.0", engine_version="1.0.0", tags=[]),
        categories=[],
        nodes=[],
    )
    library = LibraryRegistry.generate_new_library(library_data=schema)
    library.register_new_node_type(
        _ModelPicker, NodeMetadata(category="t", description="d", display_name="Model Picker")
    )
    yield
    LibraryRegistry._clear()


def _add_node(engine: Engine, name: str) -> _ModelPicker:
    context = engine.handle_request(
        EnsureWorkflowAndFlowRequest(workflow_name="trait_state_test", display_name="trait_state_test")
    )
    assert isinstance(context, EnsureWorkflowAndFlowResultSuccess)
    node = _ModelPicker(name=name, metadata={"library": _LIBRARY_NAME, "node_type": _ModelPicker.__name__})
    engine.object_manager.add_object_by_name(name, node)
    return node


def _added_parameter_commands(commands: list) -> dict[str, AddParameterToNodeRequest]:
    return {
        command.parameter_name: command
        for command in commands
        if isinstance(command, AddParameterToNodeRequest) and command.parameter_name is not None
    }


class TestUnresolvableTraitName:
    def test_the_parameter_loads_without_the_missing_trait(self, engine: Engine) -> None:
        target = _add_node(engine, "target")

        result = engine.handle_request(
            AddParameterToNodeRequest(
                node_name=target.name,
                parameter_name="model",
                tooltip="t",
                type="str",
                traits=[{"trait_name": "NoSuchTrait", "trait_state": {}}],
            )
        )

        assert isinstance(result, AddParameterToNodeResultSuccess)
        parameter = target.get_parameter_by_name("model")
        assert parameter is not None
        assert parameter.trait_states() == []

    def test_a_warning_is_logged(self, engine: Engine, caplog: pytest.LogCaptureFixture) -> None:
        target = _add_node(engine, "target")
        caplog.set_level(logging.WARNING, logger="griptape_nodes")

        engine.handle_request(
            AddParameterToNodeRequest(
                node_name=target.name,
                parameter_name="model",
                tooltip="t",
                type="str",
                traits=[{"trait_name": "NoSuchTrait", "trait_state": {}}],
            )
        )

        assert any("NoSuchTrait" in record.getMessage() for record in caplog.records)


class TestUnsaveableCallbackWarning:
    def test_serializing_a_lambda_callback_logs_a_warning(
        self, engine: Engine, caplog: pytest.LogCaptureFixture
    ) -> None:
        node = _add_node(engine, "picker")
        node.discover()
        caplog.set_level(logging.WARNING, logger="griptape_nodes")

        result = engine.node_manager.on_serialize_node_to_commands(SerializeNodeToCommandsRequest(node_name=node.name))

        assert isinstance(result, SerializeNodeToCommandsResultSuccess)
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any("unsaveable" in r.getMessage() and "cannot be saved" in r.getMessage() for r in warnings)

    def test_a_nameable_callback_logs_nothing(self, engine: Engine, caplog: pytest.LogCaptureFixture) -> None:
        node = _add_node(engine, "picker")
        node.discover()
        caplog.set_level(logging.WARNING, logger="griptape_nodes")

        engine.node_manager.on_serialize_node_to_commands(SerializeNodeToCommandsRequest(node_name=node.name))

        assert not any("'reload'" in r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)


class TestSerializeThenReplay:
    """The scenario the deleted demo script covered: build, serialize, replay onto a fresh node."""

    def test_emitted_commands_carry_traits_and_value_callbacks(self, engine: Engine) -> None:
        node = _add_node(engine, "picker")
        node.discover()

        result = engine.node_manager.on_serialize_node_to_commands(SerializeNodeToCommandsRequest(node_name=node.name))
        assert isinstance(result, SerializeNodeToCommandsResultSuccess)

        commands = _added_parameter_commands(result.serialized_node_commands.element_modification_commands)
        model_command = commands["model"]
        assert model_command.traits == [
            {
                "trait_name": "Options",
                "trait_state": {
                    "choices": ["sdxl", "sd3", "flux"],
                    "show_search": True,
                    "search_filter": "",
                    "allow_custom": False,
                },
            }
        ]
        assert model_command.value_callbacks == {"converters": ["strip_padding"]}

        reload_command = commands["reload"]
        assert reload_command.traits == [
            {
                "trait_name": "Button",
                "trait_state": {
                    "label": "Reload",
                    "variant": "secondary",
                    "size": "default",
                    "state": "normal",
                    "icon": None,
                    "icon_class": None,
                    "icon_position": None,
                    "full_width": False,
                    "loading_label": None,
                    "loading_icon": None,
                    "loading_icon_class": None,
                    "tooltip": None,
                    "button_link": None,
                },
                "trait_callbacks": {"on_click": "reload_models"},
            }
        ]

    def test_replaying_the_commands_restores_state_and_callbacks(self, engine: Engine) -> None:
        node = _add_node(engine, "picker")
        node.discover()
        node.set_parameter_value("model", "flux")

        result = engine.node_manager.on_serialize_node_to_commands(SerializeNodeToCommandsRequest(node_name=node.name))
        assert isinstance(result, SerializeNodeToCommandsResultSuccess)
        commands = _added_parameter_commands(result.serialized_node_commands.element_modification_commands)

        target = _add_node(engine, "reloaded")
        for command in commands.values():
            command.node_name = target.name
            command.initial_setup = True
            replay_result = engine.handle_request(command)
            assert isinstance(replay_result, AddParameterToNodeResultSuccess)

        model = target.get_parameter_by_name("model")
        assert model is not None
        assert model.ui_options["simple_dropdown"] == ["sdxl", "sd3", "flux"]
        converted = "not-a-model"
        for converter in model.converters:
            converted = converter(converted)
        assert converted == "sdxl"  # Options snaps an invalid value to its first choice.

        reload_button = next(
            trait
            for trait in target.get_parameter_by_name("reload").find_elements_by_type(Button)  # type: ignore[union-attr]
        )
        assert reload_button.on_click_callback is not None
        assert reload_button.on_click_callback.__self__ is target  # type: ignore[attr-defined]

        unsaveable_button = next(
            trait
            for trait in target.get_parameter_by_name("unsaveable").find_elements_by_type(Button)  # type: ignore[union-attr]
        )
        assert unsaveable_button.on_click_callback is None
        assert unsaveable_button.label == "Lambda"
