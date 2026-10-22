"""Tests for the node types and flow the chat agent is given at the start of every run."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
from griptape_nodes.exe_types.node_types import ControlNode
from griptape_nodes.node_library.library_registry import (
    LibraryMetadata,
    LibraryRegistry,
    LibrarySchema,
    NodeDeprecationMetadata,
    NodeMetadata,
)
from griptape_nodes.retained_mode.events.flow_events import (
    CreateFlowRequest,
    CreateFlowResultSuccess,
    FlowSummaryConnection,
    FlowSummaryNode,
    GetFlowSummaryResultSuccess,
)
from griptape_nodes.retained_mode.events.node_events import CreateNodeRequest, CreateNodeResultSuccess
from griptape_nodes.retained_mode.events.object_events import ClearAllObjectStateRequest
from griptape_nodes.retained_mode.managers.agent_manager import (
    _MAX_NODE_DESCRIPTION_LENGTH,
    _NO_FLOW_INSTRUCTION,
    _NODE_TYPES_NOT_LISTED_INSTRUCTION,
    _TOOL_APPROVAL_INSTRUCTIONS,
    AgentManager,
    _build_agent_instructions,
    _compose_run_instructions,
    _render_flow,
    _render_node_types,
    _WorkflowContext,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from griptape_nodes.retained_mode.engine import Engine

_LIBRARY_NAME = "Chat Context Library"


class _ContextNode(ControlNode):
    def __init__(self, name: str, metadata: dict | None = None) -> None:
        super().__init__(name, metadata)
        self.add_parameter(
            Parameter(
                name="prompt",
                type="str",
                default_value="",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
                tooltip="prompt",
            )
        )

    def process(self) -> None:
        pass


def _metadata(category: str, description: str, *, deprecated: bool = False) -> NodeMetadata:
    deprecation = NodeDeprecationMetadata() if deprecated else None
    return NodeMetadata(category=category, description=description, display_name="", deprecation=deprecation)


def _flow(nodes: list[FlowSummaryNode], connections: list[FlowSummaryConnection]) -> GetFlowSummaryResultSuccess:
    return GetFlowSummaryResultSuccess(
        workflow_name="wf", flow_name="main", nodes=nodes, connections=connections, result_details="ok"
    )


class TestAgentInstructions:
    def test_base_instructions_point_at_the_given_context_and_batching(self) -> None:
        instructions = _build_agent_instructions(include_image_tool=False)

        assert "start with discovery" not in instructions
        assert "Work from the node types and the current flow given below" in instructions
        assert "GriptapeNodes_EventRequestBatch" in instructions

    def test_base_instructions_say_what_to_do_rather_than_what_not_to(self) -> None:
        instructions = _build_agent_instructions(include_image_tool=False)

        assert "Do not" not in instructions
        assert "NEVER" not in instructions

    def test_run_instructions_put_the_node_types_first_and_the_flow_last(self) -> None:
        composed = _compose_run_instructions(_WorkflowContext(node_types="NODE TYPES", flow="FLOW"), "RULES")

        assert composed == f"NODE TYPES\n\nRULES\n\n{_TOOL_APPROVAL_INSTRUCTIONS}\n\nFLOW"


class TestRenderNodeTypes:
    def test_one_line_per_node_type_under_its_library_and_category(self) -> None:
        libraries = {
            "Images": {
                "LoadImage": _metadata("image", "Loads an\n   image"),
                "Blur": _metadata("edit", "Blurs"),
                "OldLoader": _metadata("image", "Old loader", deprecated=True),
                "Bare": _metadata("image", ""),
            }
        }

        lines = _render_node_types(libraries).splitlines()

        assert lines[1:] == [
            "Library 'Images':",
            "  image:",
            "    - LoadImage: Loads an image",
            "    - OldLoader (deprecated, do not use for new work): Old loader",
            "    - Bare",
            "  edit:",
            "    - Blur: Blurs",
        ]

    def test_long_description_is_cut(self) -> None:
        libraries = {"L": {"Wordy": _metadata("c", "word " * 100)}}

        line = _render_node_types(libraries).splitlines()[-1]

        assert line.endswith("...")
        assert len(line) == len("    - Wordy: ") + _MAX_NODE_DESCRIPTION_LENGTH


class TestRenderFlow:
    def test_empty_flow_says_so(self) -> None:
        assert _render_flow(_flow([], [])).endswith("The flow is empty.")

    def test_nodes_and_connections(self) -> None:
        nodes = [
            FlowSummaryNode(
                name="gen",
                type="Generate",
                library="L",
                group="loop",
                values={"prompt": "a fox"},
                multi_inputs=["images"],
                position={"x": 10.0, "y": 20.5},
            ),
            FlowSummaryNode(
                name="save", type="Save", library="L", group=None, values={}, multi_inputs=[], position=None
            ),
        ]
        connections = [FlowSummaryConnection(source="gen.image", target="save.image")]

        lines = _render_flow(_flow(nodes, connections)).splitlines()

        assert lines[1:] == [
            "Nodes:",
            "  - gen (Generate from 'L') in group 'loop' at (10.0, 20.5) values {\"prompt\": \"a fox\"} list inputs images",
            "  - save (Save from 'L')",
            "Connections:",
            "  - gen.image -> save.image",
        ]


@pytest.fixture
def chat_manager(engine: Engine) -> Iterator[AgentManager]:
    """An AgentManager over the real engine, with one registered node type and no flow open."""
    engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
    LibraryRegistry._clear()
    engine.library_manager._libraries_loading_complete.set()
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
    library.register_new_node_type(
        _ContextNode, NodeMetadata(category="text", description="Holds a prompt", display_name="Context")
    )
    manager = AgentManager.__new__(AgentManager)
    manager._engine = engine
    try:
        yield manager
    finally:
        engine.library_manager._libraries_loading_complete.set()
        engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
        LibraryRegistry._clear()


class TestWorkflowContext:
    @pytest.mark.asyncio
    async def test_no_flow_open_gives_the_node_types_and_says_there_is_no_flow(
        self, chat_manager: AgentManager
    ) -> None:
        context = await chat_manager._workflow_context(asyncio.Event())

        assert f"Library '{_LIBRARY_NAME}':\n  text:\n    - _ContextNode: Holds a prompt" in context.node_types
        assert context.flow == _NO_FLOW_INSTRUCTION

    @pytest.mark.asyncio
    async def test_waits_for_libraries_that_are_still_loading(self, engine: Engine, chat_manager: AgentManager) -> None:
        loading_complete = engine.library_manager._libraries_loading_complete
        loading_complete.clear()
        asyncio.get_running_loop().call_later(0.05, loading_complete.set)

        context = await chat_manager._workflow_context(asyncio.Event())

        assert f"Library '{_LIBRARY_NAME}':" in context.node_types

    @pytest.mark.asyncio
    async def test_says_how_to_get_the_node_types_when_libraries_take_too_long(
        self, engine: Engine, chat_manager: AgentManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        engine.library_manager._libraries_loading_complete.clear()
        monkeypatch.setattr(f"{AgentManager.__module__}._LIBRARY_LOADING_WAIT_SECONDS", 0.01)

        context = await chat_manager._workflow_context(asyncio.Event())

        assert context.node_types == _NODE_TYPES_NOT_LISTED_INSTRUCTION

    @pytest.mark.asyncio
    async def test_stops_waiting_for_libraries_when_the_run_is_cancelled(
        self, engine: Engine, chat_manager: AgentManager
    ) -> None:
        engine.library_manager._libraries_loading_complete.clear()
        cancel_event = asyncio.Event()
        cancel_event.set()

        context = await asyncio.wait_for(chat_manager._workflow_context(cancel_event), timeout=1)

        assert context.node_types == _NODE_TYPES_NOT_LISTED_INSTRUCTION

    @pytest.mark.asyncio
    async def test_open_flow_gives_its_summary(self, engine: Engine, chat_manager: AgentManager) -> None:
        engine.context_manager.push_workflow(workflow_name="chat_context_wf")
        flow = engine.handle_request(
            CreateFlowRequest(parent_flow_name=None, flow_name="main", set_as_new_context=True)
        )
        assert isinstance(flow, CreateFlowResultSuccess), flow
        created = engine.handle_request(CreateNodeRequest(node_type="_ContextNode", node_name="writer"))
        assert isinstance(created, CreateNodeResultSuccess), created

        context = await chat_manager._workflow_context(asyncio.Event())

        assert context.flow.startswith("Current flow: 'main',")
        assert f"  - writer (_ContextNode from '{_LIBRARY_NAME}')" in context.flow

    @pytest.mark.asyncio
    async def test_flow_that_cannot_be_rendered_is_left_out(
        self, engine: Engine, chat_manager: AgentManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        engine.context_manager.push_workflow(workflow_name="chat_context_wf")
        flow = engine.handle_request(
            CreateFlowRequest(parent_flow_name=None, flow_name="main", set_as_new_context=True)
        )
        assert isinstance(flow, CreateFlowResultSuccess), flow

        def unrenderable(_summary: object) -> str:
            msg = "Unknown format code"
            raise ValueError(msg)

        monkeypatch.setattr(f"{AgentManager.__module__}._render_flow", unrenderable)

        context = await chat_manager._workflow_context(asyncio.Event())

        assert context.flow == ""
