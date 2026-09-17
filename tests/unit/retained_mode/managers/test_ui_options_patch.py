"""UI option writes through ``AlterParameterDetailsRequest``."""

import pytest  # type: ignore[reportMissingImports]

from griptape_nodes.exe_types.core_types import Parameter
from griptape_nodes.exe_types.node_types import BaseNode
from griptape_nodes.retained_mode.engine import Engine
from griptape_nodes.retained_mode.events.base_events import ResultPayload
from griptape_nodes.retained_mode.events.parameter_events import (
    AlterParameterDetailsRequest,
    AlterParameterDetailsResultFailure,
    AlterParameterDetailsResultSuccess,
)

STARTING_OPTIONS = {"hide": False, "display_name": "Width", "slider": {"min_val": 0, "max_val": 100}}


class _Node(BaseNode):
    def process(self) -> None:
        pass


@pytest.fixture
def node(engine: Engine) -> _Node:
    """A node holding one user-defined parameter with several options set."""
    node = _Node(name="test_node")
    node.add_parameter(
        Parameter(
            name="width",
            input_types=["int"],
            type="int",
            output_type="int",
            tooltip="test",
            user_defined=True,
            ui_options={"hide": False, "display_name": "Width", "slider": {"min_val": 0, "max_val": 100}},
        )
    )
    engine.object_manager.add_object_by_name(node.name, node)
    return node


def _options(node: _Node) -> dict:
    parameter = node.get_parameter_by_name("width")
    assert parameter is not None
    return parameter.ui_options


def _alter(engine: Engine, **kwargs) -> ResultPayload:
    return engine.handle_request(AlterParameterDetailsRequest(parameter_name="width", node_name="test_node", **kwargs))


class TestReplace:
    def test_drops_options_the_caller_left_out(self, engine: Engine, node: _Node) -> None:
        _alter(engine, ui_options={"hide": True})

        assert _options(node) == {"hide": True}


class TestPatch:
    def test_leaves_untouched_options_alone(self, engine: Engine, node: _Node) -> None:
        _alter(engine, ui_options_patch={"hide": True})

        assert _options(node) == {**STARTING_OPTIONS, "hide": True}

    def test_none_removes_the_key(self, engine: Engine, node: _Node) -> None:
        _alter(engine, ui_options_patch={"slider": None})

        assert _options(node) == {"hide": False, "display_name": "Width"}

    def test_none_on_a_key_that_is_not_set_changes_nothing(self, engine: Engine, node: _Node) -> None:
        result = _alter(engine, ui_options_patch={"never_set": None})

        assert isinstance(result, AlterParameterDetailsResultSuccess)
        assert _options(node) == STARTING_OPTIONS

    def test_writes_and_removals_apply_together(self, engine: Engine, node: _Node) -> None:
        _alter(engine, ui_options_patch={"markdown": True, "slider": None})

        assert _options(node) == {"hide": False, "display_name": "Width", "markdown": True}

    def test_a_nested_option_is_replaced_not_merged(self, engine: Engine, node: _Node) -> None:
        _alter(engine, ui_options_patch={"slider": {"max_val": 8}})

        assert _options(node)["slider"] == {"max_val": 8}

    def test_an_empty_patch_changes_nothing(self, engine: Engine, node: _Node) -> None:
        _alter(engine, ui_options_patch={})

        assert _options(node) == STARTING_OPTIONS

    def test_neither_field_leaves_every_option_alone(self, engine: Engine, node: _Node) -> None:
        _alter(engine, tooltip="new tooltip")

        assert _options(node) == STARTING_OPTIONS


class TestConflict:
    def test_a_replacement_and_a_patch_together_are_refused(self, engine: Engine, node: _Node) -> None:
        result = _alter(engine, ui_options={"hide": True}, ui_options_patch={"markdown": True})

        assert isinstance(result, AlterParameterDetailsResultFailure)
        assert _options(node) == STARTING_OPTIONS
