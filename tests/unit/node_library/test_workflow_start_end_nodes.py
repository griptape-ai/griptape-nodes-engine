"""Tests for resolving the start and end nodes of a packaged workflow from the library registry.

Covers the resolver itself, ``NodeExecutor._get_workflow_start_end_nodes`` (the publish and
packaging path), and the execution environment entry ``SubflowNodeGroup`` records for local runs.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest

from griptape_nodes.common.node_executor import NodeExecutor
from griptape_nodes.exe_types.node_groups.subflow_node_group import SubflowNodeGroup
from griptape_nodes.exe_types.node_types import BaseNode, EndNode, StartNode
from griptape_nodes.node_library.library_registry import Library, LibraryRegistry, LibrarySchema, NodeMetadata
from griptape_nodes.node_library.workflow_start_end_nodes import (
    WorkflowStartEndNodes,
    WorkflowStartEndNodesNotFoundError,
    resolve_workflow_start_end_nodes,
)
from griptape_nodes.retained_mode.events.workflow_events import PublishWorkflowRegisteredEventData
from griptape_nodes.retained_mode.managers.library.common import RegisteredEventHandler

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.engine import Engine

V1_LIBRARY = "Griptape Nodes Library"
V2_LIBRARY = "Griptape Nodes Library v2"
CLOUD_LIBRARY = "Cloud Publisher"

# What the engine hardcoded before resolution came from the registry; v1 must keep getting it.
V1_START_END = WorkflowStartEndNodes(
    start_flow_node_type="StartFlow",
    start_flow_node_library_name=V1_LIBRARY,
    end_flow_node_type="EndFlow",
    end_flow_node_library_name=V1_LIBRARY,
)


class StartFlow(StartNode):
    def process(self) -> None:
        pass


class EndFlow(EndNode):
    def process(self) -> None:
        pass


class FlowStartV2(StartNode):
    def process(self) -> None:
        pass


class FlowEndV2(EndNode):
    def process(self) -> None:
        pass


class CloudStart(StartNode):
    def process(self) -> None:
        pass


class CloudEnd(EndNode):
    def process(self) -> None:
        pass


class PlainNode(BaseNode):
    def process(self) -> None:
        pass


class _Registry:
    """Builds an isolated library registry and the publish handlers a library manager would hold."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(LibraryRegistry, "_libraries", {})
        self.handlers: dict[str, RegisteredEventHandler[Any]] = {}
        self.library_manager = MagicMock()
        self.library_manager.get_registered_event_handlers.return_value = self.handlers

    def add_library(self, name: str, node_classes: list[type], *, is_default: bool = False) -> Library:
        schema = MagicMock(spec=LibrarySchema)
        schema.name = name
        schema.is_default_library = is_default
        library = Library(library_data=schema)
        for node_class in node_classes:
            metadata = NodeMetadata(category="test", description="test", display_name=node_class.__name__)
            library.register_new_node_type(node_class, metadata)
        LibraryRegistry._libraries[name] = library
        return library

    def add_publish_handler(self, library_name: str, start_end: WorkflowStartEndNodes) -> None:
        self.handlers[library_name] = RegisteredEventHandler(
            handler=MagicMock(),
            library_data=LibraryRegistry.get_library(library_name).get_library_data(),
            event_data=PublishWorkflowRegisteredEventData(
                start_flow_node_type=start_end.start_flow_node_type,
                start_flow_node_library_name=start_end.start_flow_node_library_name,
                end_flow_node_type=start_end.end_flow_node_type,
                end_flow_node_library_name=start_end.end_flow_node_library_name,
            ),
        )

    def add_v1(self, *, with_handler: bool = True) -> Library:
        library = self.add_library(V1_LIBRARY, [PlainNode, StartFlow, EndFlow], is_default=True)
        if with_handler:
            self.add_publish_handler(V1_LIBRARY, V1_START_END)
        return library


CLOUD_START_END = WorkflowStartEndNodes(
    start_flow_node_type="CloudStart",
    start_flow_node_library_name=CLOUD_LIBRARY,
    end_flow_node_type="CloudEnd",
    end_flow_node_library_name=CLOUD_LIBRARY,
)

V2_START_END = WorkflowStartEndNodes(
    start_flow_node_type="FlowStartV2",
    start_flow_node_library_name=V2_LIBRARY,
    end_flow_node_type="FlowEndV2",
    end_flow_node_library_name=V2_LIBRARY,
)


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch) -> _Registry:
    """Give the test an empty library registry and publish handler table."""
    return _Registry(monkeypatch)


class TestV1Unchanged:
    """With the v1 standard library registered, resolution returns what the engine hardcoded."""

    def test_local_execution_uses_v1_handler(self, registry: _Registry) -> None:
        registry.add_v1()

        assert resolve_workflow_start_end_nodes(registry.library_manager) == V1_START_END

    def test_local_execution_uses_v1_subclasses_without_handler(self, registry: _Registry) -> None:
        registry.add_v1(with_handler=False)

        assert resolve_workflow_start_end_nodes(registry.library_manager) == V1_START_END

    def test_default_v1_wins_over_publishing_library_registered_first(self, registry: _Registry) -> None:
        registry.add_library(CLOUD_LIBRARY, [CloudStart, CloudEnd])
        registry.add_publish_handler(CLOUD_LIBRARY, CLOUD_START_END)
        registry.add_v1()

        assert resolve_workflow_start_end_nodes(registry.library_manager) == V1_START_END

    def test_target_library_handler_is_used(self, registry: _Registry) -> None:
        registry.add_v1()
        cloud = registry.add_library(CLOUD_LIBRARY, [CloudStart, CloudEnd])
        registry.add_publish_handler(CLOUD_LIBRARY, CLOUD_START_END)

        assert resolve_workflow_start_end_nodes(registry.library_manager, cloud) == CLOUD_START_END

    def test_target_handler_pointing_at_v1_nodes_is_used(self, registry: _Registry) -> None:
        registry.add_v1()
        cloud = registry.add_library(CLOUD_LIBRARY, [])
        registry.add_publish_handler(CLOUD_LIBRARY, V1_START_END)

        assert resolve_workflow_start_end_nodes(registry.library_manager, cloud) == V1_START_END


class TestReplacementLibrary:
    """Without v1, a library that provides StartNode and EndNode subclasses is used instead."""

    def test_local_execution_uses_replacement_subclasses(self, registry: _Registry) -> None:
        registry.add_library(V2_LIBRARY, [PlainNode, FlowStartV2, FlowEndV2], is_default=True)

        assert resolve_workflow_start_end_nodes(registry.library_manager) == V2_START_END

    def test_non_default_replacement_is_found(self, registry: _Registry) -> None:
        registry.add_library("Unrelated", [PlainNode], is_default=True)
        registry.add_library(V2_LIBRARY, [FlowStartV2, FlowEndV2])

        assert resolve_workflow_start_end_nodes(registry.library_manager) == V2_START_END

    def test_target_library_without_start_end_falls_back_to_registry(self, registry: _Registry) -> None:
        target = registry.add_library("Empty Publisher", [PlainNode])
        registry.add_library(V2_LIBRARY, [FlowStartV2, FlowEndV2], is_default=True)

        assert resolve_workflow_start_end_nodes(registry.library_manager, target) == V2_START_END

    def test_library_with_only_a_start_node_is_skipped(self, registry: _Registry) -> None:
        registry.add_library("Half", [FlowStartV2], is_default=True)
        registry.add_library(V2_LIBRARY, [FlowStartV2, FlowEndV2])

        assert resolve_workflow_start_end_nodes(registry.library_manager) == V2_START_END


class TestNoStartEndNodes:
    def test_raises_named_error_when_nothing_provides_them(self, registry: _Registry) -> None:
        registry.add_library("Unrelated", [PlainNode], is_default=True)

        with pytest.raises(WorkflowStartEndNodesNotFoundError, match="no installed library provides them"):
            resolve_workflow_start_end_nodes(registry.library_manager)

    def test_error_names_the_target_library(self, registry: _Registry) -> None:
        target = registry.add_library("Empty Publisher", [PlainNode])

        with pytest.raises(WorkflowStartEndNodesNotFoundError, match="'Empty Publisher'"):
            resolve_workflow_start_end_nodes(registry.library_manager, target)


class TestNodeExecutorPublishPath:
    """NodeExecutor._get_workflow_start_end_nodes feeds packaging and publishing."""

    @pytest.fixture
    def executor(self, registry: _Registry) -> NodeExecutor:
        engine = MagicMock()
        engine.library_manager = registry.library_manager
        return NodeExecutor(engine=engine)

    @pytest.mark.asyncio
    async def test_v1_local_execution_unchanged(self, registry: _Registry, executor: NodeExecutor) -> None:
        registry.add_v1()

        assert asdict(await executor._get_workflow_start_end_nodes(None)) == asdict(V1_START_END)

    @pytest.mark.asyncio
    async def test_v1_target_library_unchanged(self, registry: _Registry, executor: NodeExecutor) -> None:
        v1 = registry.add_v1()

        assert asdict(await executor._get_workflow_start_end_nodes(v1)) == asdict(V1_START_END)

    @pytest.mark.asyncio
    async def test_replacement_library_local_execution(self, registry: _Registry, executor: NodeExecutor) -> None:
        registry.add_library(V2_LIBRARY, [FlowStartV2, FlowEndV2], is_default=True)

        assert asdict(await executor._get_workflow_start_end_nodes(None)) == asdict(V2_START_END)

    @pytest.mark.asyncio
    async def test_no_library_raises(self, registry: _Registry, executor: NodeExecutor) -> None:  # noqa: ARG002
        with pytest.raises(WorkflowStartEndNodesNotFoundError):
            await executor._get_workflow_start_end_nodes(None)


class _Group(SubflowNodeGroup):
    async def aprocess(self) -> None:  # pragma: no cover - execution not exercised here
        await self.execute_subflow()

    def process(self) -> Any:  # pragma: no cover - execution not exercised here
        return None


class TestSubflowNodeGroupLocalExecutionEntry:
    """The group records the start node local execution packages it with, keyed by its library."""

    @pytest.fixture
    def patched_engine(self, engine: Engine, registry: _Registry, monkeypatch: pytest.MonkeyPatch) -> Engine:
        monkeypatch.setattr(
            engine.library_manager,
            "get_registered_event_handlers",
            registry.library_manager.get_registered_event_handlers,
        )
        return engine

    def test_v1_entry_unchanged(self, registry: _Registry, patched_engine: Engine) -> None:  # noqa: ARG002
        registry.add_v1(with_handler=False)

        group = _Group(name="G")

        assert group.metadata["execution_environment"] == {
            V1_LIBRARY: {"start_flow_node": "StartFlow", "parameter_names": {}},
        }

    def test_v1_handler_entry_lists_start_parameters(self, registry: _Registry, patched_engine: Engine) -> None:  # noqa: ARG002
        registry.add_v1()

        group = _Group(name="G")

        assert group.metadata["execution_environment"][V1_LIBRARY]["start_flow_node"] == "StartFlow"

    def test_replacement_library_entry(self, registry: _Registry, patched_engine: Engine) -> None:  # noqa: ARG002
        registry.add_library(V2_LIBRARY, [FlowStartV2, FlowEndV2], is_default=True)

        group = _Group(name="G")

        assert group.metadata["execution_environment"] == {
            V2_LIBRARY: {"start_flow_node": "FlowStartV2", "parameter_names": {}},
        }

    def test_no_library_records_nothing_and_still_builds(self, registry: _Registry, patched_engine: Engine) -> None:  # noqa: ARG002
        group = _Group(name="G")

        assert group.metadata["execution_environment"] == {}
