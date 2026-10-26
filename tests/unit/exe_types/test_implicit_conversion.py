"""Implicit conversion on connect: opted-in targets take converted values, every other target is unchanged."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

import pytest

from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
from griptape_nodes.exe_types.implicit_conversion import ImplicitConversionError, find_implicit_conversion
from griptape_nodes.exe_types.node_types import DataNode
from griptape_nodes.machines.dag_builder import DagNode
from griptape_nodes.machines.parallel_resolution import ExecuteDagState
from griptape_nodes.node_library.library_registry import LibraryMetadata, LibraryRegistry, LibrarySchema, NodeMetadata
from griptape_nodes.retained_mode.events.connection_events import (
    ConnectionConversion,
    CreateConnectionRequest,
    CreateConnectionResultFailure,
    CreateConnectionResultSuccess,
    ListConnectionsForNodeRequest,
    ListConnectionsForNodeResultSuccess,
)
from griptape_nodes.retained_mode.events.flow_events import (
    CreateFlowRequest,
    CreateFlowResultSuccess,
    DeserializeFlowFromCommandsRequest,
    DeserializeFlowFromCommandsResultSuccess,
    SerializeFlowToCommandsRequest,
    SerializeFlowToCommandsResultSuccess,
)
from griptape_nodes.retained_mode.events.node_events import CreateNodeRequest, CreateNodeResultSuccess
from griptape_nodes.retained_mode.events.object_events import ClearAllObjectStateRequest
from griptape_nodes.retained_mode.events.parameter_events import (
    GetConnectionsForParameterRequest,
    GetConnectionsForParameterResultSuccess,
    SetParameterValueRequest,
    SetParameterValueResultFailure,
    SetParameterValueResultSuccess,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from griptape_nodes.retained_mode.engine import Engine


class TestConversionRules:
    @pytest.mark.parametrize(
        ("source_type", "target_type", "value", "expected"),
        [
            ("int", "float", 3, 3.0),
            ("float", "int", 3.9, 3),
            ("float", "int", -3.9, -3),
            ("int", "str", 42, "42"),
            ("float", "str", 1.5, "1.5"),
            ("bool", "str", True, "True"),
            ("dict", "json", {"a": 1}, {"a": 1}),
            ("list", "json", [1, 2], [1, 2]),
            ("dict", "str", {"a": 1}, '{"a": 1}'),
            ("list", "str", [1, "x"], '[1, "x"]'),
            ("json", "str", {"a": [1]}, '{"a": [1]}'),
            ("json", "dict", '{"a": 1}', {"a": 1}),
            ("json", "dict", {"a": 1}, {"a": 1}),
            ("json", "list", "[1, 2]", [1, 2]),
            ("str", "dict", '{"a": 1}', {"a": 1}),
            ("str", "list", "[1]", [1]),
            ("str", "json", '{"a": null}', {"a": None}),
            ("str", "int", " 12 ", 12),
            ("str", "float", "2.5", 2.5),
            ("list[str]", "str", ["a"], '["a"]'),
        ],
    )
    def test_rule_converts(self, source_type: str, target_type: str, value: Any, expected: Any) -> None:
        conversion = find_implicit_conversion(source_type, [target_type])

        assert conversion is not None
        converted = conversion.apply(value)
        assert converted == expected
        assert type(converted) is type(expected)

    @pytest.mark.parametrize(
        ("source_type", "target_type", "value", "message"),
        [
            ("str", "int", "abc", "isn't a whole number"),
            ("str", "int", "1.5", "isn't a whole number"),
            ("str", "float", "twelve", "isn't a number"),
            ("str", "dict", "{not json", "isn't valid JSON"),
            ("str", "dict", "[1, 2]", "isn't a JSON object"),
            ("str", "list", '{"a": 1}', "isn't a JSON list"),
            ("json", "dict", "nope", "isn't valid JSON"),
            ("dict", "json", {"a": object()}, "isn't valid JSON"),
            ("dict", "str", {"a": object()}, "can't be written as JSON text"),
            ("float", "int", math.inf, "has no whole-number equivalent"),
            ("float", "int", "3.5", "isn't a number"),
        ],
    )
    def test_rule_failure_raises_and_never_returns_none(
        self, source_type: str, target_type: str, value: Any, message: str
    ) -> None:
        conversion = find_implicit_conversion(source_type, [target_type])
        assert conversion is not None

        with pytest.raises(ImplicitConversionError, match=message):
            conversion.apply(value)

    def test_none_passes_through(self) -> None:
        conversion = find_implicit_conversion("str", ["int"])
        assert conversion is not None

        assert conversion.apply(None) is None

    def test_float_to_int_is_marked_lossy(self) -> None:
        conversion = find_implicit_conversion("float", ["int"])

        assert conversion is not None
        assert conversion.lossy is True
        assert conversion.may_fail is False

    @pytest.mark.parametrize(("source_type", "target_type"), [("str", "int"), ("str", "float"), ("str", "dict")])
    def test_text_parsing_is_marked_may_fail(self, source_type: str, target_type: str) -> None:
        conversion = find_implicit_conversion(source_type, [target_type])

        assert conversion is not None
        assert conversion.may_fail is True

    @pytest.mark.parametrize(
        ("source_type", "target_type"),
        [("int", "bool"), ("str", "bool"), ("ImageArtifact", "str"), ("any", "any"), ("dict", "list"), ("bool", "int")],
    )
    def test_no_rule(self, source_type: str, target_type: str) -> None:
        assert find_implicit_conversion(source_type, [target_type]) is None

    def test_first_target_type_with_a_rule_wins(self) -> None:
        conversion = find_implicit_conversion("int", ["ImageArtifact", "str", "float"])

        assert conversion is not None
        assert conversion.target_type == "str"


_LIBRARY_NAME = "Implicit Conversion Library"


def _node_class(class_name: str, parameters: list[Parameter]) -> type[DataNode]:
    def __init__(self: DataNode, name: str, metadata: dict | None = None) -> None:  # noqa: N807
        DataNode.__init__(self, name, metadata=metadata)
        for parameter in parameters:
            self.add_parameter(parameter.copy())

    def process(self: DataNode) -> None:
        pass

    return type(class_name, (DataNode,), {"__init__": __init__, "process": process})


def _source(type_name: str) -> Parameter:
    return Parameter(name="out", tooltip="Output", type=type_name, allowed_modes={ParameterMode.OUTPUT})


def _sink(type_name: str, *, implicit_conversion: bool) -> Parameter:
    return Parameter(
        name="in",
        tooltip="Input",
        type=type_name,
        allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
        implicit_conversion=implicit_conversion,
    )


def _adaptive_input(name: str) -> Parameter:
    return Parameter(
        name=name,
        tooltip="Adaptive input",
        type="any",
        adaptive_type_group="T",
        implicit_conversion=True,
        allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
    )


_NODE_TYPES: dict[str, list[Parameter]] = {
    "Adapt_conv": [_adaptive_input("in"), _adaptive_input("in2")],
    **{f"Src_{t}": [_source(t)] for t in ("int", "float", "str", "bool", "dict", "list", "any")},
    **{f"Conv_{t}": [_sink(t, implicit_conversion=True)] for t in ("int", "float", "str", "json", "dict")},
    **{f"Plain_{t}": [_sink(t, implicit_conversion=False)] for t in ("int", "float", "str", "json", "dict")},
}


@pytest.fixture
def conversion_nodes(engine: Engine) -> Iterator[None]:
    """One node of every source and sink type, named after its type, unconnected, in a fresh flow."""
    engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
    LibraryRegistry._clear()
    schema = LibrarySchema(
        name=_LIBRARY_NAME,
        library_schema_version=LibrarySchema.LATEST_SCHEMA_VERSION,
        metadata=LibraryMetadata(
            author="test", description="test", library_version="0.1.0", engine_version="0.0.0", tags=[]
        ),
        categories=[],
        nodes=[],
    )
    library = LibraryRegistry.generate_new_library(schema)
    for node_type, parameters in _NODE_TYPES.items():
        library.register_new_node_type(
            _node_class(node_type, parameters), NodeMetadata(category="test", description="d", display_name="N")
        )
    engine.context_manager.push_workflow(workflow_name="conversion_wf")
    flow = engine.handle_request(CreateFlowRequest(parent_flow_name=None, flow_name="main", set_as_new_context=True))
    assert isinstance(flow, CreateFlowResultSuccess), flow
    for node_type in _NODE_TYPES:
        created = engine.handle_request(CreateNodeRequest(node_type=node_type, node_name=node_type.lower()))
        assert isinstance(created, CreateNodeResultSuccess), created
    try:
        yield
    finally:
        engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
        LibraryRegistry._clear()


def _connect(engine: Engine, source: str, target: str, *, initial_setup: bool = False, target_param: str = "in") -> Any:
    return engine.handle_request(
        CreateConnectionRequest(
            source_node_name=source,
            source_parameter_name="out",
            target_node_name=target,
            target_parameter_name=target_param,
            initial_setup=initial_setup,
        )
    )


def _pass_value(engine: Engine, source: str, target: str, value: Any, source_type: str) -> Any:
    return engine.handle_request(
        SetParameterValueRequest(
            node_name=target,
            parameter_name="in",
            value=value,
            data_type=source_type,
            incoming_connection_source_node_name=source,
            incoming_connection_source_parameter_name="out",
        )
    )


@pytest.mark.usefixtures("conversion_nodes")
class TestPlainTargetsUnchanged:
    """A target that didn't opt in rejects every wire it rejected before, even where a rule exists."""

    @pytest.mark.parametrize(
        ("source", "target"),
        [
            ("src_int", "plain_str"),
            ("src_float", "plain_str"),
            ("src_bool", "plain_str"),
            ("src_int", "plain_float"),
            ("src_float", "plain_int"),
            ("src_str", "plain_int"),
            ("src_str", "plain_float"),
            ("src_dict", "plain_json"),
            ("src_list", "plain_json"),
            ("src_dict", "plain_str"),
            ("src_str", "plain_dict"),
        ],
    )
    def test_mismatched_wire_is_rejected(self, engine: Engine, source: str, target: str) -> None:
        result = _connect(engine, source, target)

        assert isinstance(result, CreateConnectionResultFailure)
        assert "Connection failed on type mismatch" in str(result.result_details)

    def test_matching_wire_has_no_conversion(self, engine: Engine) -> None:
        result = _connect(engine, "src_int", "plain_int")

        assert isinstance(result, CreateConnectionResultSuccess), result
        assert result.conversion is None


@pytest.mark.usefixtures("conversion_nodes")
class TestUntypedSources:
    """An output typed ``any`` (its type is only known when it runs) feeding a converting input."""

    def test_untyped_output_connects_to_an_opted_in_target_as_can_fail(self, engine: Engine) -> None:
        result = _connect(engine, "src_any", "conv_str")

        assert isinstance(result, CreateConnectionResultSuccess), result
        assert result.conversion == ConnectionConversion(source_type="any", target_type="str", may_fail=True)

    def test_untyped_output_into_a_plain_target_is_still_rejected(self, engine: Engine) -> None:
        result = _connect(engine, "src_any", "plain_str")

        assert isinstance(result, CreateConnectionResultFailure)

    @pytest.mark.parametrize(
        ("target", "value", "expected"),
        [
            ("conv_str", "already text", "already text"),
            ("conv_str", 3, "3"),
            ("conv_str", {"k": 1}, '{"k": 1}'),
            ("conv_int", "41", 41),
            ("conv_float", 2, 2.0),
            ("conv_dict", '{"k": 1}', {"k": 1}),
        ],
    )
    def test_the_arriving_value_picks_the_rule(self, engine: Engine, target: str, value: Any, expected: Any) -> None:
        _connect(engine, "src_any", target)

        result = _pass_value(engine, "src_any", target, value, "any")

        assert isinstance(result, SetParameterValueResultSuccess), result
        assert engine.node_manager.get_node_by_name(target).get_parameter_value("in") == expected

    def test_a_value_no_rule_converts_fails_with_a_named_error(self, engine: Engine) -> None:
        _connect(engine, "src_any", "conv_int")

        result = _pass_value(engine, "src_any", "conv_int", "not a number", "any")

        assert isinstance(result, SetParameterValueResultFailure), result


@pytest.mark.usefixtures("conversion_nodes")
class TestConvertingConnections:
    @pytest.mark.parametrize(
        ("source", "target", "expected"),
        [
            ("src_int", "conv_float", ConnectionConversion(source_type="int", target_type="float")),
            ("src_float", "conv_int", ConnectionConversion(source_type="float", target_type="int", lossy=True)),
            ("src_int", "conv_str", ConnectionConversion(source_type="int", target_type="str")),
            ("src_bool", "conv_str", ConnectionConversion(source_type="bool", target_type="str")),
            ("src_dict", "conv_json", ConnectionConversion(source_type="dict", target_type="json")),
            ("src_list", "conv_json", ConnectionConversion(source_type="list", target_type="json")),
            ("src_str", "conv_int", ConnectionConversion(source_type="str", target_type="int", may_fail=True)),
            ("src_str", "conv_dict", ConnectionConversion(source_type="str", target_type="dict", may_fail=True)),
        ],
    )
    def test_opted_in_target_accepts_and_marks_the_wire(
        self, engine: Engine, source: str, target: str, expected: ConnectionConversion
    ) -> None:
        result = _connect(engine, source, target)

        assert isinstance(result, CreateConnectionResultSuccess), result
        assert result.conversion == expected

    def test_matching_wire_into_opted_in_target_has_no_conversion(self, engine: Engine) -> None:
        result = _connect(engine, "src_str", "conv_str")

        assert isinstance(result, CreateConnectionResultSuccess), result
        assert result.conversion is None

    def test_opted_in_target_still_rejects_wires_without_a_rule(self, engine: Engine) -> None:
        result = _connect(engine, "src_dict", "conv_int")

        assert isinstance(result, CreateConnectionResultFailure)

    def test_connection_listings_mark_the_converted_wire(self, engine: Engine) -> None:
        _connect(engine, "src_int", "conv_str")
        expected = ConnectionConversion(source_type="int", target_type="str")

        for_target = engine.handle_request(ListConnectionsForNodeRequest(node_name="conv_str"))
        for_source = engine.handle_request(ListConnectionsForNodeRequest(node_name="src_int"))
        for_parameter = engine.handle_request(
            GetConnectionsForParameterRequest(node_name="conv_str", parameter_name="in")
        )

        assert isinstance(for_target, ListConnectionsForNodeResultSuccess), for_target
        assert isinstance(for_source, ListConnectionsForNodeResultSuccess), for_source
        assert isinstance(for_parameter, GetConnectionsForParameterResultSuccess), for_parameter
        assert for_target.incoming_connections[0].conversion == expected
        assert for_source.outgoing_connections[0].conversion == expected
        assert for_parameter.incoming_connections[0].conversion == expected

    def test_plain_wire_listing_has_no_conversion(self, engine: Engine) -> None:
        _connect(engine, "src_int", "plain_int")

        listed = engine.handle_request(ListConnectionsForNodeRequest(node_name="plain_int"))

        assert isinstance(listed, ListConnectionsForNodeResultSuccess), listed
        assert listed.incoming_connections[0].conversion is None

    def test_reloaded_workflow_marks_the_wire_again(self, engine: Engine) -> None:
        _connect(engine, "src_float", "conv_int")
        serialized = engine.handle_request(SerializeFlowToCommandsRequest(flow_name="main"))
        assert isinstance(serialized, SerializeFlowToCommandsResultSuccess), serialized

        engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
        engine.context_manager.push_workflow(workflow_name="conversion_wf_reloaded")
        restored = engine.handle_request(
            DeserializeFlowFromCommandsRequest(serialized_flow_commands=serialized.serialized_flow_commands)
        )
        assert isinstance(restored, DeserializeFlowFromCommandsResultSuccess), restored
        listed = engine.handle_request(ListConnectionsForNodeRequest(node_name="conv_int"))

        assert isinstance(listed, ListConnectionsForNodeResultSuccess), listed
        assert listed.incoming_connections[0].conversion == ConnectionConversion(
            source_type="float", target_type="int", lossy=True
        )


@pytest.mark.usefixtures("conversion_nodes")
class TestValuePassing:
    @pytest.mark.parametrize(
        ("source", "target", "value", "source_type", "expected"),
        [
            ("src_int", "conv_float", 2, "int", 2.0),
            ("src_float", "conv_int", 7.8, "float", 7),
            ("src_int", "conv_str", 5, "int", "5"),
            ("src_bool", "conv_str", False, "bool", "False"),
            ("src_dict", "conv_json", {"k": [1]}, "dict", {"k": [1]}),
            ("src_str", "conv_int", "41", "str", 41),
            ("src_str", "conv_float", "0.25", "str", 0.25),
            ("src_str", "conv_dict", '{"k": 1}', "str", {"k": 1}),
        ],
    )
    def test_value_is_converted_as_it_passes(  # noqa: PLR0913
        self, engine: Engine, *, source: str, target: str, value: Any, source_type: str, expected: Any
    ) -> None:
        connected = _connect(engine, source, target)
        assert isinstance(connected, CreateConnectionResultSuccess), connected

        result = _pass_value(engine, source, target, value, source_type)

        assert isinstance(result, SetParameterValueResultSuccess), result
        stored = engine.node_manager.get_node_by_name(target).get_parameter_value("in")
        assert stored == expected
        assert type(stored) is type(expected)

    def test_failed_conversion_names_the_source_and_value(self, engine: Engine) -> None:
        _connect(engine, "src_str", "conv_int")

        result = _pass_value(engine, "src_str", "conv_int", "abc", "str")

        assert isinstance(result, SetParameterValueResultFailure)
        details = str(result.result_details)
        assert "'src_str.out'" in details
        assert "'abc'" in details
        assert "'conv_int.in'" in details
        assert engine.node_manager.get_node_by_name("conv_int").get_parameter_value("in") is None

    def test_plain_connection_value_is_not_converted(self, engine: Engine) -> None:
        _connect(engine, "src_str", "plain_str")

        result = _pass_value(engine, "src_str", "plain_str", "41", "str")

        assert isinstance(result, SetParameterValueResultSuccess), result
        assert engine.node_manager.get_node_by_name("plain_str").get_parameter_value("in") == "41"

    @pytest.mark.asyncio
    async def test_run_time_failure_raises_naming_the_source(self, engine: Engine) -> None:
        _connect(engine, "src_str", "conv_int")
        source = engine.node_manager.get_node_by_name("src_str")
        source.parameter_output_values["out"] = "not a number"
        target = engine.node_manager.get_node_by_name("conv_int")

        with pytest.raises(RuntimeError, match=r"'src_str\.out'.*'not a number'"):
            await ExecuteDagState.collect_values_from_upstream_nodes(engine, DagNode(node_reference=target))

    @pytest.mark.asyncio
    async def test_run_time_pass_converts(self, engine: Engine) -> None:
        _connect(engine, "src_float", "conv_int")
        source = engine.node_manager.get_node_by_name("src_float")
        source.parameter_output_values["out"] = 9.99
        target = engine.node_manager.get_node_by_name("conv_int")

        await ExecuteDagState.collect_values_from_upstream_nodes(engine, DagNode(node_reference=target))

        assert target.get_parameter_value("in") == int(9.99)


@pytest.mark.usefixtures("conversion_nodes")
class TestAdaptiveTargetsConvert:
    def test_typed_adaptive_target_that_opted_in_converts(self, engine: Engine) -> None:
        typed = _connect(engine, "src_int", "adapt_conv")
        assert isinstance(typed, CreateConnectionResultSuccess), typed
        assert typed.conversion is None

        replaced = _connect(engine, "src_str", "adapt_conv")

        # The only wire was replaced, so the group took the new wire's type rather than converting.
        assert isinstance(replaced, CreateConnectionResultSuccess), replaced
        assert replaced.conversion is None
        adaptive_input = engine.node_manager.get_node_by_name("adapt_conv").get_parameter_by_name("in")
        assert adaptive_input is not None
        assert adaptive_input.type == "str"

    def test_typed_adaptive_group_converts_a_second_wire(self, engine: Engine) -> None:
        _connect(engine, "src_int", "adapt_conv")

        second = _connect(engine, "src_str", "adapt_conv", target_param="in2")

        assert isinstance(second, CreateConnectionResultSuccess), second
        assert second.conversion == ConnectionConversion(source_type="str", target_type="int", may_fail=True)
