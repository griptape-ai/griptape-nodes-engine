import logging
from collections.abc import Callable, Generator
from typing import Any

import pytest

from griptape_nodes.exe_types.core_types import Parameter, ParameterMode, Trait
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
    AlterParameterDetailsRequest,
)
from griptape_nodes.traits.button import Button
from griptape_nodes.traits.options import Options
from griptape_nodes.traits.slider import Slider

_LIBRARY_NAME = "trait-state-serialization-test-library"

NARROWED_MAX = 50


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


class _DeclaredControls(BaseNode):
    """A node whose own code declares its controls, which is the common case."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        width = Parameter(name="width", type="int", input_types=["int"], output_type="int", tooltip="t")
        width.add_trait(Slider(min_val=0, max_val=100))
        self.add_parameter(width)

        model = Parameter(name="model", type="str", input_types=["str"], output_type="str", tooltip="t")
        model.add_trait(Options(choices=["a", "b"]))
        self.add_parameter(model)

        reload_button = Parameter(name="reload", type="str", input_types=["str"], output_type="str", tooltip="t")
        reload_button.add_trait(Button(label="Reload", on_click=self.reload_models))
        self.add_parameter(reload_button)

    def reload_models(self, button: Button, button_payload) -> None:  # noqa: ANN001, ARG002
        return None

    def process(self) -> None:
        return None


@pytest.fixture(autouse=True)
def _registered_node_type() -> Generator[None, None, None]:
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
    library.register_new_node_type(
        _DeclaredControls, NodeMetadata(category="t", description="d", display_name="Declared Controls")
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


class TestSerializeThenReplay:
    def test_emitted_commands_carry_trait_state(self, engine: Engine) -> None:
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

    def test_a_dynamic_trait_module_is_saved_by_stable_name(
        self, engine: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        node = _add_node(engine, "picker")
        node.discover()
        monkeypatch.setattr(Options, "__module__", "gtn_dynamic_module_options_test")
        monkeypatch.setattr(
            engine.library_manager,
            "get_stable_namespace_for_dynamic_module",
            lambda _module: "stable_library.options",
        )

        result = engine.node_manager.on_serialize_node_to_commands(SerializeNodeToCommandsRequest(node_name=node.name))

        assert isinstance(result, SerializeNodeToCommandsResultSuccess)
        commands = _added_parameter_commands(result.serialized_node_commands.element_modification_commands)
        traits = commands["model"].traits
        assert traits is not None
        assert traits[0]["trait_module"] == "stable_library.options"

    def test_a_dynamic_trait_without_a_stable_name_omits_its_module(
        self, engine: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        node = _add_node(engine, "picker")
        node.discover()
        monkeypatch.setattr(Options, "__module__", "gtn_dynamic_module_options_test")
        monkeypatch.setattr(engine.library_manager, "get_stable_namespace_for_dynamic_module", lambda _module: None)

        result = engine.node_manager.on_serialize_node_to_commands(SerializeNodeToCommandsRequest(node_name=node.name))

        assert isinstance(result, SerializeNodeToCommandsResultSuccess)
        commands = _added_parameter_commands(result.serialized_node_commands.element_modification_commands)
        traits = commands["model"].traits
        assert traits is not None
        assert traits[0]["trait_module"] is None

    def test_replaying_the_commands_restores_state(self, engine: Engine) -> None:
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

        # A replayed command carries state, not behavior. Behavior comes from the node's own
        # code, which is why a declared parameter's button still fires: its trait is built by
        # __init__ and only updated from the save. A bare replay onto a node whose code never
        # built this button has nothing to supply the handler.
        reload_button = next(
            trait
            for trait in target.get_parameter_by_name("reload").find_elements_by_type(Button)  # type: ignore[union-attr]
        )
        assert reload_button.label == "Reload"
        assert reload_button.on_click_callback is None


class _UnsaveableValueTrait(Trait):
    """Stands in for a trait returning something no saved workflow can express."""

    def __init__(self, items: list[Any]) -> None:
        super().__init__()
        self.items = items

    @classmethod
    def get_trait_keys(cls) -> list[str]:
        return []

    def to_state(self) -> dict[str, Any]:
        return {"items": self.items}

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
                "trait_module": _UnsaveableValueTrait.__module__,
                "trait_state": {},
            }
        ]
        assert any("items" in r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)


class _RequiredCallbackTrait(Trait):
    def __init__(self, on_ping: Callable, label: str = "hi") -> None:
        super().__init__()
        self.on_ping = on_ping
        self.label = label

    @classmethod
    def get_trait_keys(cls) -> list[str]:
        return []

    def to_state(self) -> dict[str, Any]:
        return {"label": self.label}

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


def _add_declared_node(engine: Engine, name: str) -> _DeclaredControls:
    context = engine.handle_request(
        EnsureWorkflowAndFlowRequest(workflow_name="trait_state_test", display_name="trait_state_test")
    )
    assert isinstance(context, EnsureWorkflowAndFlowResultSuccess)
    node = _DeclaredControls(name=name, metadata={"library": _LIBRARY_NAME, "node_type": _DeclaredControls.__name__})
    engine.object_manager.add_object_by_name(name, node)
    return node


def _control(node: BaseNode, parameter: str, kind: type) -> Any:
    found = node.get_parameter_by_name(parameter)
    assert found is not None
    return found.find_elements_by_type(kind)[0]


def _round_trip(engine: Engine, source: _DeclaredControls) -> _DeclaredControls:
    """Serialize, then replay onto a fresh node the same code built."""
    result = engine.node_manager.on_serialize_node_to_commands(SerializeNodeToCommandsRequest(node_name=source.name))
    assert isinstance(result, SerializeNodeToCommandsResultSuccess)

    target = _add_declared_node(engine, "reloaded")
    for command in result.serialized_node_commands.element_modification_commands:
        if not isinstance(command, (AddParameterToNodeRequest, AlterParameterDetailsRequest)):
            continue
        command.node_name = target.name
        command.initial_setup = True
        engine.handle_request(command)
    return target


def _altered_trait_states(engine: Engine, node: BaseNode, parameter: str) -> list[dict[str, Any]] | None:
    result = engine.node_manager.on_serialize_node_to_commands(SerializeNodeToCommandsRequest(node_name=node.name))
    assert isinstance(result, SerializeNodeToCommandsResultSuccess)
    for command in result.serialized_node_commands.element_modification_commands:
        if isinstance(command, AlterParameterDetailsRequest) and command.parameter_name == parameter:
            return command.traits
    return None


class TestOnlyChangedTraitStateIsSaved:
    """A key the node still builds the same way is left to node code, so a library release reaches it."""

    def test_a_narrowed_slider_saves_only_the_moved_bound(self, engine: Engine) -> None:
        node = _add_declared_node(engine, "source")
        _control(node, "width", Slider).max = NARROWED_MAX

        traits = _altered_trait_states(engine, node, "width")

        assert traits == [
            {
                "trait_name": "Slider",
                "trait_module": "griptape_nodes.traits.slider",
                "trait_state": {"max_val": NARROWED_MAX},
            }
        ]

    def test_changed_choices_leave_constructor_config_out(self, engine: Engine) -> None:
        node = _add_declared_node(engine, "source")
        _control(node, "model", Options).choices = ["x", "y"]

        traits = _altered_trait_states(engine, node, "model")

        assert traits == [
            {
                "trait_name": "Options",
                "trait_module": "griptape_nodes.traits.options",
                "trait_state": {"choices": ["x", "y"]},
            }
        ]


class TestRunTimeStateReachesTheTrait:
    def test_a_narrowed_slider_still_enforces_its_new_bound(self, engine: Engine) -> None:
        node = _add_declared_node(engine, "source")
        _control(node, "width", Slider).max = NARROWED_MAX

        reloaded = _round_trip(engine, node)

        parameter = reloaded.get_parameter_by_name("width")
        assert parameter is not None
        assert _control(reloaded, "width", Slider).max == NARROWED_MAX
        assert parameter.ui_options["slider"] == {"min_val": 0, "max_val": NARROWED_MAX}

        def enforce(value: int) -> None:
            for validator in parameter.validators:
                validator(parameter, value)

        with pytest.raises(ValueError, match="must be between"):
            enforce(NARROWED_MAX + 30)

    def test_a_dropdown_filled_at_run_time_keeps_its_choices(self, engine: Engine) -> None:
        node = _add_declared_node(engine, "source")
        _control(node, "model", Options).choices = ["x", "y"]

        reloaded = _round_trip(engine, node)

        assert _control(reloaded, "model", Options).choices == ["x", "y"]

    def test_a_value_the_node_chose_at_run_time_is_not_snapped_away(self, engine: Engine) -> None:
        """The converter reads the trait, so a stale trait would snap 'x' back to 'a'."""
        node = _add_declared_node(engine, "source")
        _control(node, "model", Options).choices = ["x", "y"]

        reloaded = _round_trip(engine, node)

        parameter = reloaded.get_parameter_by_name("model")
        assert parameter is not None
        value = "x"
        for converter in parameter.converters:
            value = converter(value)
        assert value == "x"


class TestBehaviorComesFromTheNode:
    """Updating the trait the node built, rather than replacing it, keeps the handler."""

    def test_a_button_still_fires_after_a_round_trip(self, engine: Engine) -> None:
        node = _add_declared_node(engine, "source")

        reloaded = _round_trip(engine, node)

        handler = _control(reloaded, "reload", Button).on_click_callback
        assert handler is not None
        assert handler.__self__ is reloaded  # type: ignore[attr-defined]

    def test_the_handler_binds_to_the_loading_node_not_the_saved_one(self, engine: Engine) -> None:
        node = _add_declared_node(engine, "source")

        reloaded = _round_trip(engine, node)

        assert _control(reloaded, "reload", Button).on_click_callback.__self__ is not node  # type: ignore[union-attr]
