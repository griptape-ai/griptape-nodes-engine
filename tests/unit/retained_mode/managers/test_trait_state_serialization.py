"""The production save path.

On_serialize_node_to_commands emits traits/value_callbacks that AddParameterToNodeRequest
can replay onto a fresh node.
"""

import logging
from collections.abc import Callable, Generator
from typing import Any
from unittest.mock import patch

import attrs
import pytest

from griptape_nodes.exe_types.core_types import BEHAVIOR, Parameter, ParameterMode, Trait
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
                traits=[
                    {
                        "trait_name": "NoSuchTrait",
                        "trait_module": "griptape_nodes.node_libraries.a_library_that_is_gone.traits",
                        "trait_state": {},
                    }
                ],
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
                traits=[
                    {
                        "trait_name": "NoSuchTrait",
                        "trait_module": "griptape_nodes.node_libraries.a_library_that_is_gone.traits",
                        "trait_state": {},
                    }
                ],
            )
        )

        assert any("NoSuchTrait" in record.getMessage() for record in caplog.records)

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
                "trait_module": "griptape_nodes.traits.options",
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
                "trait_module": "griptape_nodes.traits.button",
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
            }
        ]

    def test_replaying_the_commands_restores_trait_state(self, engine: Engine) -> None:
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
        assert reload_button.label == "Reload"


class TestTraitModuleStabilization:
    """A trait's module name is only stable within the process that dynamically loaded it."""

    def test_a_dynamic_module_is_rewritten_to_its_stable_namespace(self, engine: Engine) -> None:
        trait_states = [{"trait_name": "Options", "trait_module": "gtn_dynamic_module_foo_py_123", "trait_state": {}}]

        with (
            patch.object(engine.library_manager, "is_dynamic_module", return_value=True),
            patch.object(
                engine.library_manager,
                "get_stable_namespace_for_dynamic_module",
                return_value="griptape_nodes.node_libraries.some_library.traits",
            ),
        ):
            stabilized = engine.node_manager._stabilize_trait_modules(trait_states)

        assert stabilized[0]["trait_module"] == "griptape_nodes.node_libraries.some_library.traits"

    def test_an_in_tree_module_is_left_untouched(self, engine: Engine) -> None:
        trait_states = [{"trait_name": "Options", "trait_module": "griptape_nodes.traits.options", "trait_state": {}}]

        stabilized = engine.node_manager._stabilize_trait_modules(trait_states)

        assert stabilized[0]["trait_module"] == "griptape_nodes.traits.options"

    def test_serializing_a_library_trait_saves_the_stable_namespace(self, engine: Engine) -> None:
        node = _add_node(engine, "picker")
        node.discover()

        with (
            patch.object(engine.library_manager, "is_dynamic_module", return_value=True),
            patch.object(
                engine.library_manager,
                "get_stable_namespace_for_dynamic_module",
                return_value="griptape_nodes.node_libraries.some_library.traits",
            ),
        ):
            result = engine.node_manager.on_serialize_node_to_commands(
                SerializeNodeToCommandsRequest(node_name=node.name)
            )

        assert isinstance(result, SerializeNodeToCommandsResultSuccess)
        commands = _added_parameter_commands(result.serialized_node_commands.element_modification_commands)
        model_traits = commands["model"].traits
        assert model_traits is not None
        assert model_traits[0]["trait_module"] == "griptape_nodes.node_libraries.some_library.traits"


class _UnsaveableValueTrait(Trait):
    """Stands in for a trait holding something no saved workflow can express.

    A declared type is checked when the class is built, so the way through that check is a
    container the check cannot judge: ``list[Any]`` holding a callable.
    """

    items: list[Any] = attrs.field(factory=list)

    def ui_options_for_trait(self) -> dict:
        return {}


class _MisdeclaredTraitNode(BaseNode):
    """A node that grows a parameter with a trait a third-party library forgot to declare correctly."""

    def process(self) -> None:
        return None

    def discover(self) -> None:
        with sanctioned_parameter_mutation():
            self.add_parameter(
                Parameter(
                    name="broken",
                    type="str",
                    default_value="",
                    tooltip="t",
                    user_defined=True,
                    allowed_modes={ParameterMode.PROPERTY},
                    traits={_UnsaveableValueTrait(items=[lambda: None])},
                )
            )


@pytest.fixture(autouse=True)
def _registered_misdeclared_node_type(_registered_node_type: None) -> None:
    """Register _MisdeclaredTraitNode alongside _ModelPicker under the same test library."""
    library = LibraryRegistry.get_library(_LIBRARY_NAME)
    library.register_new_node_type(
        _MisdeclaredTraitNode, NodeMetadata(category="t", description="d", display_name="Misdeclared Trait Node")
    )


class TestAnUnsaveableTraitValueDegradesTheSaveInsteadOfFailingIt:
    """A value with no saved form costs that one value, not the whole save."""

    def test_serializing_still_succeeds(self, engine: Engine, caplog: pytest.LogCaptureFixture) -> None:
        context = engine.handle_request(
            EnsureWorkflowAndFlowRequest(workflow_name="trait_state_test", display_name="trait_state_test")
        )
        assert isinstance(context, EnsureWorkflowAndFlowResultSuccess)
        node = _MisdeclaredTraitNode(
            name="broken_node", metadata={"library": _LIBRARY_NAME, "node_type": _MisdeclaredTraitNode.__name__}
        )
        engine.object_manager.add_object_by_name("broken_node", node)
        node.discover()
        caplog.set_level(logging.WARNING, logger="griptape_nodes")

        result = engine.node_manager.on_serialize_node_to_commands(SerializeNodeToCommandsRequest(node_name=node.name))

        assert isinstance(result, SerializeNodeToCommandsResultSuccess)
        commands = _added_parameter_commands(result.serialized_node_commands.element_modification_commands)
        broken_command = commands["broken"]
        assert broken_command.traits == [
            {
                "trait_name": "_UnsaveableValueTrait",
                "trait_module": "tests.unit.retained_mode.managers.test_trait_state_serialization",
                "trait_state": {},
            }
        ]
        assert any("items" in r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)


class _RequiredCallbackTrait(Trait):
    """Stands in for a trait whose saved state is short a required constructor argument.

    ``on_ping`` is behavior, so ``to_state()`` omits it, but it is also mandatory, so the
    resulting saved state can never satisfy this constructor on load.
    """

    on_ping: Callable = attrs.field(metadata=BEHAVIOR)
    label: str = attrs.field(default="hi")

    def ui_options_for_trait(self) -> dict:
        return {}


class TestTraitStateMissingARequiredArgument:
    """A saved state short a required constructor argument loads without that trait, not a crash."""

    def test_the_parameter_loads_without_the_trait(self, engine: Engine) -> None:
        target = _add_node(engine, "target")

        result = engine.handle_request(
            AddParameterToNodeRequest(
                node_name=target.name,
                parameter_name="model",
                tooltip="t",
                type="str",
                traits=[
                    {
                        "trait_name": "_RequiredCallbackTrait",
                        "trait_module": _RequiredCallbackTrait.__module__,
                        "trait_state": {"label": "x"},
                    }
                ],
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
                traits=[
                    {
                        "trait_name": "_RequiredCallbackTrait",
                        "trait_module": _RequiredCallbackTrait.__module__,
                        "trait_state": {"label": "x"},
                    }
                ],
            )
        )

        assert any(
            "_RequiredCallbackTrait" in record.getMessage()
            for record in caplog.records
            if record.levelno == logging.WARNING
        )
