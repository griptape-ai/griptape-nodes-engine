"""Converters and validators on a run-time parameter survive a save by naming node methods."""

import pytest

from griptape_nodes.exe_types.core_types import Parameter
from griptape_nodes.exe_types.node_types import BaseNode, sanctioned_parameter_mutation
from griptape_nodes.retained_mode.engine import Engine
from griptape_nodes.retained_mode.events.parameter_events import (
    AddParameterToNodeRequest,
    AddParameterToNodeResultSuccess,
)


class TaggingNode(BaseNode):
    """A node that builds a validated text parameter at run time."""

    def strip_padding(self, value: str) -> str:
        return value.strip() if isinstance(value, str) else value

    def upper_case(self, value: str) -> str:
        return value.upper() if isinstance(value, str) else value

    def reject_spaces(self, parameter: Parameter, value: str) -> None:  # noqa: ARG002
        if isinstance(value, str) and " " in value:
            msg = "A tag cannot contain spaces."
            raise ValueError(msg)

    def process(self) -> None:
        return None

    def add_tag_parameter(self, **kwargs) -> Parameter:
        parameter = Parameter(name="tag", type="str", tooltip="t", user_defined=True, default_value="", **kwargs)
        with sanctioned_parameter_mutation():
            self.add_parameter(parameter)
        return parameter


def _parameter_of(node: BaseNode, name: str) -> Parameter:
    parameter = node.get_parameter_by_name(name)
    assert parameter is not None
    return parameter


def _convert(parameter: Parameter, value: str) -> str:
    for converter in parameter.converters:
        value = converter(value)
    return value


@pytest.fixture
def source_node() -> TaggingNode:
    """A node whose run-time parameter uses two converters and a validator of its own."""
    node = TaggingNode(name="source")
    node.add_tag_parameter(
        converters=[node.strip_padding, node.upper_case],
        validators=[node.reject_spaces],
    )
    return node


class TestNamingValueCallbacks:
    def test_node_methods_are_saved_in_order(self, source_node: TaggingNode) -> None:
        names = _parameter_of(source_node, "tag").value_callback_names(source_node)

        assert names == {"converters": ["strip_padding", "upper_case"], "validators": ["reject_spaces"]}

    def test_a_list_holding_a_lambda_is_not_saved_at_all(self) -> None:
        # Converters chain, so restoring a subset would run a different pipeline than the
        # one that was saved while appearing to work.
        node = TaggingNode(name="node")
        parameter = node.add_tag_parameter(converters=[node.strip_padding, lambda value: value])

        assert parameter.value_callback_names(node) == {}
        assert parameter.unnameable_value_callbacks(node) == {"converters": 1}

    def test_a_nameable_list_is_unaffected_by_another_list_failing(self) -> None:
        node = TaggingNode(name="node")
        parameter = node.add_tag_parameter(
            converters=[lambda value: value],
            validators=[node.reject_spaces],
        )

        assert parameter.value_callback_names(node) == {"validators": ["reject_spaces"]}

    def test_nothing_attached_records_nothing(self) -> None:
        node = TaggingNode(name="node")
        parameter = node.add_tag_parameter()

        assert parameter.value_callback_names(node) == {}
        assert parameter.unnameable_value_callbacks(node) == {}


class TestRebindingValueCallbacks:
    def _reload_onto(self, engine: Engine, source: Parameter, target: TaggingNode) -> Parameter:
        engine.object_manager.add_object_by_name(target.name, target)
        parameter_dict = source.to_dict()
        parameter_dict["initial_setup"] = True
        parameter_dict["ui_options"] = source.authored_ui_options()
        parameter_dict["traits"] = source.trait_states()
        parameter_dict["value_callbacks"] = source.value_callback_names(source.get_node())
        result = engine.handle_request(AddParameterToNodeRequest.create(node_name=target.name, **parameter_dict))
        assert isinstance(result, AddParameterToNodeResultSuccess)
        return _parameter_of(target, "tag")

    def test_the_converter_chain_still_runs_in_order(self, engine: Engine, source_node: TaggingNode) -> None:
        restored = self._reload_onto(engine, _parameter_of(source_node, "tag"), TaggingNode(name="target"))

        assert _convert(restored, "  hello  ") == "HELLO"

    def test_the_validator_still_rejects(self, engine: Engine, source_node: TaggingNode) -> None:
        restored = self._reload_onto(engine, _parameter_of(source_node, "tag"), TaggingNode(name="target"))
        validator = restored.validators[0]

        with pytest.raises(ValueError, match="cannot contain spaces"):
            validator(restored, "a b")

    def test_the_callbacks_bind_to_the_loading_node(self, engine: Engine, source_node: TaggingNode) -> None:
        target = TaggingNode(name="target")

        restored = self._reload_onto(engine, _parameter_of(source_node, "tag"), target)

        assert all(converter.__self__ is target for converter in restored.converters)  # type: ignore[attr-defined]

    def test_a_saved_name_does_not_duplicate_a_declared_converter(self) -> None:
        node = TaggingNode(name="node")
        parameter = node.add_tag_parameter(converters=[node.strip_padding])

        parameter.apply_value_callback_names({"converters": ["strip_padding"]}, node)

        assert len(parameter.converters) == 1

    def test_a_missing_method_loads_without_the_converter(self, engine: Engine) -> None:
        target = TaggingNode(name="target")
        engine.object_manager.add_object_by_name(target.name, target)

        result = engine.handle_request(
            AddParameterToNodeRequest(
                node_name=target.name,
                parameter_name="tag",
                tooltip="t",
                type="str",
                value_callbacks={"converters": ["method_that_went_away"]},
            )
        )
        assert isinstance(result, AddParameterToNodeResultSuccess)

        assert _parameter_of(target, "tag").converters == []
