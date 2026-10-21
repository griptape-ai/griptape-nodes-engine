"""GetFlowSummaryRequest returns a flow's nodes, the values set on them, and their connections."""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING, Any

import pytest
from griptape.artifacts import ImageUrlArtifact

from griptape_nodes.exe_types.core_types import Parameter, ParameterList, ParameterMode
from griptape_nodes.exe_types.local_objects import make_reference
from griptape_nodes.exe_types.node_groups import BaseNodeGroup, SubflowNodeGroup
from griptape_nodes.exe_types.node_types import ControlNode
from griptape_nodes.node_library.library_registry import LibraryMetadata, LibraryRegistry, LibrarySchema, NodeMetadata
from griptape_nodes.retained_mode.events.connection_events import (
    CreateConnectionRequest,
    CreateConnectionResultSuccess,
)
from griptape_nodes.retained_mode.events.flow_events import (
    CreateFlowRequest,
    CreateFlowResultSuccess,
    FlowSummaryConnection,
    FlowSummaryNode,
    GetFlowSummaryRequest,
    GetFlowSummaryResultFailure,
    GetFlowSummaryResultSuccess,
)
from griptape_nodes.retained_mode.events.node_events import (
    AddNodesToNodeGroupRequest,
    AddNodesToNodeGroupResultSuccess,
    CreateNodeRequest,
    CreateNodeResultSuccess,
)
from griptape_nodes.retained_mode.events.object_events import ClearAllObjectStateRequest
from griptape_nodes.retained_mode.events.parameter_events import (
    SetParameterValueRequest,
    SetParameterValueResultSuccess,
)
from griptape_nodes.retained_mode.managers.flow_summary import (
    MAX_VALUE_ITEMS,
    MAX_VALUE_TEXT_LENGTH,
    summarize_value,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from griptape_nodes.retained_mode.engine import Engine

_LIBRARY_NAME = "Flow Summary Library"


class _Size(Enum):
    LARGE = "large"


class _Thing:
    """Stands in for a runtime object such as an artifact."""


class _SummaryNode(ControlNode):
    """A node with one parameter of each kind the summary treats differently."""

    def __init__(self, name: str, metadata: dict | None = None) -> None:
        super().__init__(name, metadata=metadata)
        self.add_parameter(
            Parameter(
                name="prompt",
                tooltip="Text in",
                type="str",
                default_value="",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="count",
                tooltip="A number with a default",
                type="int",
                default_value=1,
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="thing",
                tooltip="Holds a runtime object",
                type="any",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="locked",
                tooltip="Not settable",
                type="str",
                default_value="",
                settable=False,
                allowed_modes={ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            ParameterList(
                name="items",
                tooltip="One connection per item",
                input_types=["str"],
                allowed_modes={ParameterMode.INPUT},
            )
        )
        self.add_parameter(
            ParameterList(
                name="results",
                tooltip="Built by the node",
                output_type="str",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )
        self.add_parameter(
            Parameter(
                name="output",
                tooltip="Text out",
                type="str",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )

    def process(self) -> None:
        self.parameter_output_values["output"] = self.get_parameter_value("prompt")


class _Group(SubflowNodeGroup):
    """A group that moves its members into a subflow, as loops do."""

    async def aprocess(self) -> None:
        return

    def run(self) -> None:
        pass

    def initialize(self) -> None:
        pass


class _PlainGroup(BaseNodeGroup):
    """A group that leaves its members in the flow."""

    def process(self) -> None:
        pass


@pytest.fixture
def summary_flow(engine: Engine) -> Iterator[None]:
    """Nodes "source" and "target" in a fresh flow named "main", nothing set or connected."""
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
    library.register_new_node_type(_SummaryNode, NodeMetadata(category="test", description="d", display_name="Summary"))
    library.register_new_node_type(_Group, NodeMetadata(category="test", description="d", display_name="Group"))
    library.register_new_node_type(
        _PlainGroup, NodeMetadata(category="test", description="d", display_name="Plain Group")
    )
    engine.context_manager.push_workflow(workflow_name="flow_summary_wf")
    flow = engine.handle_request(CreateFlowRequest(parent_flow_name=None, flow_name="main", set_as_new_context=True))
    assert isinstance(flow, CreateFlowResultSuccess), flow
    for name, x in (("source", 0), ("target", 400)):
        created = engine.handle_request(
            CreateNodeRequest(node_type="_SummaryNode", node_name=name, metadata={"position": {"x": x, "y": 10}})
        )
        assert isinstance(created, CreateNodeResultSuccess), created
    try:
        yield
    finally:
        engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
        LibraryRegistry._clear()


def _set(engine: Engine, node_name: str, parameter_name: str, value: Any) -> None:
    result = engine.handle_request(
        SetParameterValueRequest(node_name=node_name, parameter_name=parameter_name, value=value)
    )
    assert isinstance(result, SetParameterValueResultSuccess), result


def _connect(engine: Engine, source: str, source_parameter: str, target: str, target_parameter: str) -> None:
    result = engine.handle_request(
        CreateConnectionRequest(
            source_node_name=source,
            source_parameter_name=source_parameter,
            target_node_name=target,
            target_parameter_name=target_parameter,
        )
    )
    assert isinstance(result, CreateConnectionResultSuccess), result


def _add_item(engine: Engine, node_name: str) -> str:
    node = engine.object_manager.attempt_get_object_by_name_as_type(node_name, _SummaryNode)
    assert node is not None
    items = node.get_parameter_by_name("items")
    assert isinstance(items, ParameterList)
    return items.add_child_parameter().name


def _summary(engine: Engine, flow_name: str | None = None) -> GetFlowSummaryResultSuccess:
    result = engine.handle_request(GetFlowSummaryRequest(flow_name=flow_name))
    assert isinstance(result, GetFlowSummaryResultSuccess), result
    return result


def _node(summary: GetFlowSummaryResultSuccess, name: str) -> FlowSummaryNode:
    return next(node for node in summary.nodes if node.name == name)


@pytest.mark.usefixtures("summary_flow")
class TestGetFlowSummary:
    def test_untouched_nodes_have_no_values(self, engine: Engine) -> None:
        summary = _summary(engine)

        assert summary.workflow_name == "flow_summary_wf"
        assert summary.flow_name == "main"
        assert summary.nodes == [
            FlowSummaryNode(
                name="source",
                type="_SummaryNode",
                library=_LIBRARY_NAME,
                group=None,
                values={},
                multi_inputs=["items"],
                position={"x": 0, "y": 10},
            ),
            FlowSummaryNode(
                name="target",
                type="_SummaryNode",
                library=_LIBRARY_NAME,
                group=None,
                values={},
                multi_inputs=["items"],
                position={"x": 400, "y": 10},
            ),
        ]
        assert summary.connections == []

    def test_lists_only_values_that_differ_from_the_default(self, engine: Engine) -> None:
        _set(engine, "source", "prompt", "a red fox")
        _set(engine, "source", "count", 1)
        _set(engine, "target", "count", 3)

        summary = _summary(engine)

        assert _node(summary, "source").values == {"prompt": "a red fox"}
        assert _node(summary, "target").values == {"count": 3}

    def test_runtime_objects_and_long_text_are_shortened(self, engine: Engine) -> None:
        _set(engine, "source", "thing", _Thing())
        _set(engine, "source", "prompt", "x" * 1000)

        values = _node(_summary(engine), "source").values

        assert values["thing"] == "<_Thing>"
        assert values["prompt"].startswith("x" * (MAX_VALUE_TEXT_LENGTH - 3) + "...")
        assert "1000 characters" in values["prompt"]

    def test_connected_inputs_report_the_connection_not_a_value(self, engine: Engine) -> None:
        _set(engine, "target", "prompt", "typed before connecting")
        _connect(engine, "source", "output", "target", "prompt")
        _connect(engine, "source", "exec_out", "target", "exec_in")

        summary = _summary(engine)

        assert "prompt" not in _node(summary, "target").values
        assert sorted(summary.connections, key=lambda c: c.source) == [
            FlowSummaryConnection(source="source.exec_out", target="target.exec_in"),
            FlowSummaryConnection(source="source.output", target="target.prompt"),
        ]

    def test_connection_into_a_list_item_names_the_item(self, engine: Engine) -> None:
        item = _add_item(engine, "target")
        _connect(engine, "source", "output", "target", item)

        summary = _summary(engine)

        assert summary.connections == [FlowSummaryConnection(source="source.output", target=f"target.{item}")]
        assert "items" not in _node(summary, "target").values

    def test_list_shows_typed_items_beside_a_connected_one(self, engine: Engine) -> None:
        connected_item = _add_item(engine, "target")
        typed_item = _add_item(engine, "target")
        _connect(engine, "source", "output", "target", connected_item)
        _set(engine, "target", typed_item, "typed item")

        assert _node(_summary(engine), "target").values == {"items": {typed_item: "typed item"}}

    def test_output_only_list_is_not_a_multi_input(self, engine: Engine) -> None:
        assert _node(_summary(engine), "source").multi_inputs == ["items"]

    def test_held_object_reference_is_not_shown(self, engine: Engine) -> None:
        _set(engine, "source", "thing", make_reference(worker="w", key="k", source="source"))

        assert _node(_summary(engine), "source").values == {"thing": "<held object>"}

    def test_group_members_are_listed_under_their_group(self, engine: Engine) -> None:
        _connect(engine, "source", "output", "target", "prompt")
        created = engine.handle_request(CreateNodeRequest(node_type="_Group", node_name="loop"))
        assert isinstance(created, CreateNodeResultSuccess), created
        added = engine.handle_request(AddNodesToNodeGroupRequest(node_names=["target"], node_group_name="loop"))
        assert isinstance(added, AddNodesToNodeGroupResultSuccess), added

        summary = _summary(engine)

        assert [(node.name, node.group) for node in summary.nodes] == [
            ("source", None),
            ("loop", None),
            ("target", "loop"),
        ]
        assert sorted(summary.connections, key=lambda c: c.source) == [
            FlowSummaryConnection(source="loop.output", target="target.prompt"),
            FlowSummaryConnection(source="source.output", target="loop.output"),
        ]

    def test_members_of_a_group_that_keeps_them_in_the_flow_are_listed_under_it(self, engine: Engine) -> None:
        created = engine.handle_request(CreateNodeRequest(node_type="_PlainGroup", node_name="pg"))
        assert isinstance(created, CreateNodeResultSuccess), created
        group = engine.object_manager.attempt_get_object_by_name_as_type("pg", _PlainGroup)
        target = engine.object_manager.attempt_get_object_by_name_as_type("target", _SummaryNode)
        assert group is not None
        assert target is not None
        group.add_nodes_to_group([target])

        assert [(node.name, node.group) for node in _summary(engine).nodes] == [
            ("source", None),
            ("pg", None),
            ("target", "pg"),
        ]

    def test_summarizing_a_group_subflow_keeps_the_group_name(self, engine: Engine) -> None:
        created = engine.handle_request(CreateNodeRequest(node_type="_Group", node_name="loop"))
        assert isinstance(created, CreateNodeResultSuccess), created
        added = engine.handle_request(AddNodesToNodeGroupRequest(node_names=["target"], node_group_name="loop"))
        assert isinstance(added, AddNodesToNodeGroupResultSuccess), added
        group = engine.object_manager.attempt_get_object_by_name_as_type("loop", _Group)
        assert group is not None
        subflow_name = group.metadata["subflow_name"]

        summary = _summary(engine, flow_name=subflow_name)

        assert [(node.name, node.group) for node in summary.nodes] == [("target", "loop")]

    def test_summarizes_a_flow_by_name(self, engine: Engine) -> None:
        assert _summary(engine, flow_name="main").flow_name == "main"

    def test_unknown_flow_fails_naming_it(self, engine: Engine) -> None:
        result = engine.handle_request(GetFlowSummaryRequest(flow_name="nope"))

        assert isinstance(result, GetFlowSummaryResultFailure)
        assert "'nope'" in str(result.result_details)


class TestSummarizeValue:
    @pytest.mark.parametrize("value", [None, 0, 1.5, True, "short", [1, 2], {"a": "b"}])
    def test_small_values_pass_through(self, value: Any) -> None:
        assert summarize_value(value) == value

    def test_long_list_keeps_its_first_items(self) -> None:
        prompts = [f"prompt number {index}" for index in range(MAX_VALUE_ITEMS + 5)]

        assert summarize_value(prompts) == [*prompts[:MAX_VALUE_ITEMS], "... (5 more items)"]

    def test_deeply_nested_container_becomes_a_placeholder(self) -> None:
        assert summarize_value([[[1, 2, 3]]]) == [["<list of 3 items>"]]

    def test_object_becomes_its_type_name(self) -> None:
        assert summarize_value(_Thing()) == "<_Thing>"

    def test_artifact_becomes_its_type(self) -> None:
        assert summarize_value(ImageUrlArtifact("https://example.com/cat.png")) == "<ImageUrlArtifact>"

    def test_artifact_dict_from_a_client_becomes_its_type(self) -> None:
        artifact = {"type": "ImageUrlArtifact", "value": "https://example.com/cat.png"}

        assert summarize_value(artifact) == "<ImageUrlArtifact>"

    def test_dict_with_a_type_key_that_is_not_an_artifact_is_kept(self) -> None:
        assert summarize_value({"type": "portrait", "width": 3}) == {"type": "portrait", "width": 3}

    def test_value_with_a_simple_form_shows_that_form(self) -> None:
        assert summarize_value((1, "b")) == [1, "b"]
        assert summarize_value(_Size.LARGE) == "large"

    def test_text_that_holds_json_is_kept_as_text(self) -> None:
        text = '{"type": "ImageArtifact", "value": "..."}'

        assert summarize_value(text) == text
