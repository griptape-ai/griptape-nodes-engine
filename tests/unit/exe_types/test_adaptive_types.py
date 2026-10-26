"""Type-adaptive wildcard pins: a group of parameters takes the type of the first wire connected to it."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
from griptape_nodes.exe_types.node_types import DataNode
from griptape_nodes.node_library.library_registry import LibraryMetadata, LibraryRegistry, LibrarySchema, NodeMetadata
from griptape_nodes.retained_mode.events.base_events import ExecutionGriptapeNodeEvent
from griptape_nodes.retained_mode.events.connection_events import (
    CreateConnectionRequest,
    CreateConnectionResultFailure,
    CreateConnectionResultSuccess,
    DeleteConnectionRequest,
    DeleteConnectionResultSuccess,
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
from griptape_nodes.retained_mode.events.parameter_events import AlterElementEvent, AlterParameterDetailsRequest

if TYPE_CHECKING:
    from collections.abc import Iterator

    from griptape_nodes.retained_mode.engine import Engine

_LIBRARY_NAME = "Adaptive Types Library"


class _AdaptiveNode(DataNode):
    """Two inputs and an output that share the adaptive group "T"."""

    def __init__(self, name: str, metadata: dict | None = None) -> None:
        super().__init__(name, metadata=metadata)
        for input_name in ("a", "b"):
            self.add_parameter(
                Parameter(
                    name=input_name,
                    tooltip="Adaptive input",
                    type="any",
                    adaptive_type_group="T",
                    allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
                )
            )
        self.add_parameter(
            Parameter(
                name="out",
                tooltip="Adaptive output",
                type="any",
                adaptive_type_group="T",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )

    def process(self) -> None:
        self.parameter_output_values["out"] = self.get_parameter_value("a")


def _typed_source(type_name: str) -> type[DataNode]:
    class _Source(DataNode):
        def __init__(self, name: str, metadata: dict | None = None) -> None:
            super().__init__(name, metadata=metadata)
            self.add_parameter(
                Parameter(name="out", tooltip="Output", type=type_name, allowed_modes={ParameterMode.OUTPUT})
            )

        def process(self) -> None:
            pass

    _Source.__name__ = f"_{type_name.capitalize()}Source"
    return _Source


def _typed_sink(type_name: str) -> type[DataNode]:
    class _Sink(DataNode):
        def __init__(self, name: str, metadata: dict | None = None) -> None:
            super().__init__(name, metadata=metadata)
            self.add_parameter(
                Parameter(
                    name="in",
                    tooltip="Input",
                    type=type_name,
                    allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
                )
            )

        def process(self) -> None:
            pass

    _Sink.__name__ = f"_{type_name.capitalize()}Sink"
    return _Sink


_NODE_CLASSES: dict[str, type[DataNode]] = {
    "_AdaptiveNode": _AdaptiveNode,
    "_IntSource": _typed_source("int"),
    "_StrSource": _typed_source("str"),
    "_AnySource": _typed_source("any"),
    "_IntSink": _typed_sink("int"),
    "_StrSink": _typed_sink("str"),
}

_NODES = {
    "adapt": "_AdaptiveNode",
    "adapt2": "_AdaptiveNode",
    "int_src": "_IntSource",
    "int_src2": "_IntSource",
    "str_src": "_StrSource",
    "any_src": "_AnySource",
    "int_sink": "_IntSink",
    "str_sink": "_StrSink",
}


def _register_library() -> None:
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
    for node_type, node_class in _NODE_CLASSES.items():
        node_class.__name__ = node_type
        library.register_new_node_type(node_class, NodeMetadata(category="test", description="d", display_name="N"))


@pytest.fixture
def adaptive_nodes(engine: Engine) -> Iterator[None]:
    """Adaptive nodes "adapt" and "adapt2" plus typed sources and sinks, unconnected, in a fresh flow."""
    engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
    LibraryRegistry._clear()
    _register_library()
    engine.context_manager.push_workflow(workflow_name="adaptive_wf")
    flow = engine.handle_request(CreateFlowRequest(parent_flow_name=None, flow_name="main", set_as_new_context=True))
    assert isinstance(flow, CreateFlowResultSuccess), flow
    for node_name, node_type in _NODES.items():
        created = engine.handle_request(CreateNodeRequest(node_type=node_type, node_name=node_name))
        assert isinstance(created, CreateNodeResultSuccess), created
    try:
        yield
    finally:
        engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
        LibraryRegistry._clear()


def _connect(engine: Engine, source: str, target: str, *, initial_setup: bool = False) -> Any:
    source_node, source_param = source.split(".")
    target_node, target_param = target.split(".")
    return engine.handle_request(
        CreateConnectionRequest(
            source_node_name=source_node,
            source_parameter_name=source_param,
            target_node_name=target_node,
            target_parameter_name=target_param,
            initial_setup=initial_setup,
        )
    )


def _disconnect(engine: Engine, source: str, target: str) -> None:
    source_node, source_param = source.split(".")
    target_node, target_param = target.split(".")
    result = engine.handle_request(
        DeleteConnectionRequest(
            source_node_name=source_node,
            source_parameter_name=source_param,
            target_node_name=target_node,
            target_parameter_name=target_param,
        )
    )
    assert isinstance(result, DeleteConnectionResultSuccess), result


def _param(engine: Engine, node_name: str, param_name: str) -> Parameter:
    parameter = engine.node_manager.get_node_by_name(node_name).get_parameter_by_name(param_name)
    assert parameter is not None
    return parameter


def _group_types(engine: Engine, node_name: str = "adapt") -> dict[str, tuple[str, list[str], str, str | None]]:
    return {
        name: (
            _param(engine, node_name, name).type,
            _param(engine, node_name, name).input_types,
            _param(engine, node_name, name).output_type,
            _param(engine, node_name, name).adaptive_resolved_type,
        )
        for name in ("a", "b", "out")
    }


def _all_typed(type_name: str) -> dict[str, tuple[str, list[str], str, str | None]]:
    return {name: (type_name, [type_name], type_name, type_name) for name in ("a", "b", "out")}


_UNTYPED = {name: ("any", ["any"], "any", None) for name in ("a", "b", "out")}


@pytest.mark.usefixtures("adaptive_nodes")
class TestAdaptiveStateMachine:
    def test_group_starts_untyped(self, engine: Engine) -> None:
        assert _group_types(engine) == _UNTYPED

    def test_first_incoming_wire_types_the_whole_group(self, engine: Engine) -> None:
        result = _connect(engine, "int_src.out", "adapt.a")

        assert isinstance(result, CreateConnectionResultSuccess), result
        assert _group_types(engine) == _all_typed("int")

    def test_typed_group_rejects_an_incompatible_wire(self, engine: Engine) -> None:
        _connect(engine, "int_src.out", "adapt.a")

        result = _connect(engine, "str_src.out", "adapt.b")

        assert isinstance(result, CreateConnectionResultFailure)
        assert "took the type 'int'" in str(result.result_details)
        assert _group_types(engine) == _all_typed("int")

    def test_typed_group_accepts_a_compatible_wire(self, engine: Engine) -> None:
        _connect(engine, "int_src.out", "adapt.a")

        result = _connect(engine, "int_src2.out", "adapt.b")

        assert isinstance(result, CreateConnectionResultSuccess), result

    def test_typed_output_only_connects_where_its_type_fits(self, engine: Engine) -> None:
        _connect(engine, "int_src.out", "adapt.a")

        rejected = _connect(engine, "adapt.out", "str_sink.in")
        accepted = _connect(engine, "adapt.out", "int_sink.in")

        assert isinstance(rejected, CreateConnectionResultFailure)
        assert "took the type 'int'" in str(rejected.result_details)
        assert isinstance(accepted, CreateConnectionResultSuccess), accepted

    def test_first_outgoing_wire_types_the_group_from_the_target(self, engine: Engine) -> None:
        result = _connect(engine, "adapt.out", "str_sink.in")

        assert isinstance(result, CreateConnectionResultSuccess), result
        assert _group_types(engine) == _all_typed("str")
        assert isinstance(_connect(engine, "int_src.out", "adapt.a"), CreateConnectionResultFailure)

    def test_group_stays_typed_until_its_last_wire_is_removed(self, engine: Engine) -> None:
        _connect(engine, "int_src.out", "adapt.a")
        _connect(engine, "adapt.out", "int_sink.in")

        _disconnect(engine, "int_src.out", "adapt.a")
        assert _group_types(engine) == _all_typed("int")

        _disconnect(engine, "adapt.out", "int_sink.in")
        assert _group_types(engine) == _UNTYPED

    def test_reverted_group_takes_a_new_type(self, engine: Engine) -> None:
        _connect(engine, "int_src.out", "adapt.a")
        _disconnect(engine, "int_src.out", "adapt.a")

        result = _connect(engine, "str_src.out", "adapt.a")

        assert isinstance(result, CreateConnectionResultSuccess), result
        assert _group_types(engine) == _all_typed("str")

    def test_replacing_the_only_wire_retypes_the_group(self, engine: Engine) -> None:
        _connect(engine, "int_src.out", "adapt.a")

        result = _connect(engine, "str_src.out", "adapt.a")

        assert isinstance(result, CreateConnectionResultSuccess), result
        assert result.replaced_connection is not None
        assert _group_types(engine) == _all_typed("str")

    def test_untyped_wire_does_not_type_the_group(self, engine: Engine) -> None:
        result = _connect(engine, "any_src.out", "adapt.a")

        assert isinstance(result, CreateConnectionResultSuccess), result
        assert _group_types(engine) == _UNTYPED

    def test_typing_that_would_break_an_existing_wire_is_rejected(self, engine: Engine) -> None:
        _connect(engine, "any_src.out", "adapt.a")

        result = _connect(engine, "int_src.out", "adapt.b")

        assert isinstance(result, CreateConnectionResultFailure)
        assert "'any_src.out' to 'adapt.a'" in str(result.result_details)
        assert _group_types(engine) == _UNTYPED

    def test_two_untyped_adaptive_nodes_connect_and_stay_untyped(self, engine: Engine) -> None:
        result = _connect(engine, "adapt.out", "adapt2.a")

        assert isinstance(result, CreateConnectionResultSuccess), result
        assert _group_types(engine, "adapt") == _UNTYPED
        assert _group_types(engine, "adapt2") == _UNTYPED

    def test_typed_adaptive_output_types_a_downstream_adaptive_group(self, engine: Engine) -> None:
        _connect(engine, "int_src.out", "adapt.a")

        result = _connect(engine, "adapt.out", "adapt2.a")

        assert isinstance(result, CreateConnectionResultSuccess), result
        assert _group_types(engine, "adapt2") == _all_typed("int")


@pytest.mark.usefixtures("adaptive_nodes")
class TestAdaptiveRetypeEvents:
    def test_retype_and_revert_emit_alter_element_events(self, engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
        sent: list[dict] = []
        original_put_event = engine.event_manager.put_event

        def record(event: Any) -> None:
            if isinstance(event, ExecutionGriptapeNodeEvent) and isinstance(
                event.wrapped_event.payload, AlterElementEvent
            ):
                sent.append(event.wrapped_event.payload.element_details)
            original_put_event(event)

        monkeypatch.setattr(engine.event_manager, "put_event", record)

        _connect(engine, "int_src.out", "adapt.a")
        typed = {
            details["name"]: details for details in sent if details.get("node_name") == "adapt" and "name" in details
        }
        sent.clear()
        _disconnect(engine, "int_src.out", "adapt.a")
        reverted = {
            details["name"]: details for details in sent if details.get("node_name") == "adapt" and "name" in details
        }

        for name in ("a", "b", "out"):
            assert typed[name]["type"] == "int"
            assert typed[name]["input_types"] == ["int"]
            assert typed[name]["output_type"] == "int"
            assert typed[name]["adaptive_resolved_type"] == "int"
            assert reverted[name]["type"] == "any"
            assert reverted[name]["input_types"] == ["any"]
            assert reverted[name]["output_type"] == "any"
            assert reverted[name]["adaptive_resolved_type"] is None


@pytest.mark.usefixtures("adaptive_nodes")
class TestAdaptiveLoadReplay:
    def test_replaying_connections_restores_the_types(self, engine: Engine) -> None:
        _connect(engine, "adapt.out", "str_sink.in")
        serialized = engine.handle_request(SerializeFlowToCommandsRequest(flow_name="main"))
        assert isinstance(serialized, SerializeFlowToCommandsResultSuccess), serialized

        engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
        engine.context_manager.push_workflow(workflow_name="adaptive_wf_reloaded")
        restored = engine.handle_request(
            DeserializeFlowFromCommandsRequest(serialized_flow_commands=serialized.serialized_flow_commands)
        )

        assert isinstance(restored, DeserializeFlowFromCommandsResultSuccess), restored
        assert _group_types(engine) == _all_typed("str")

    def test_saved_workflow_does_not_pin_adaptive_types(self, engine: Engine) -> None:
        _connect(engine, "int_src.out", "adapt.a")
        serialized = engine.handle_request(SerializeFlowToCommandsRequest(flow_name="main"))
        assert isinstance(serialized, SerializeFlowToCommandsResultSuccess), serialized

        alters = [
            command
            for node_commands in serialized.serialized_flow_commands.serialized_node_commands
            for command in node_commands.element_modification_commands
            if isinstance(command, AlterParameterDetailsRequest) and command.parameter_name in ("a", "b", "out")
        ]

        assert all(command.type is None and command.output_type is None for command in alters)

    def test_initial_setup_replay_types_the_group(self, engine: Engine) -> None:
        result = _connect(engine, "int_src.out", "adapt.b", initial_setup=True)

        assert isinstance(result, CreateConnectionResultSuccess), result
        assert _group_types(engine) == _all_typed("int")


@pytest.mark.usefixtures("adaptive_nodes")
class TestPlainParametersUnchanged:
    """Parameters that don't opt in connect exactly as they always have."""

    def test_mismatched_plain_types_are_rejected_with_the_old_message(self, engine: Engine) -> None:
        result = _connect(engine, "int_src.out", "str_sink.in")

        assert isinstance(result, CreateConnectionResultFailure)
        assert "Connection failed on type mismatch" in str(result.result_details)

    def test_plain_any_source_into_plain_typed_target_is_rejected(self, engine: Engine) -> None:
        result = _connect(engine, "any_src.out", "int_sink.in")

        assert isinstance(result, CreateConnectionResultFailure)

    def test_matching_plain_types_connect(self, engine: Engine) -> None:
        result = _connect(engine, "int_src.out", "int_sink.in")

        assert isinstance(result, CreateConnectionResultSuccess), result
        assert _param(engine, "int_sink", "in").type == "int"
