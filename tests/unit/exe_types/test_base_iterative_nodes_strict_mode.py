"""Connecting a loop's Start to its End, or its End's results onward, keeps state-outside-parameters quiet."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from griptape_nodes.common.strict_mode import STRICT_MODE, StrictModeScopeKind
from griptape_nodes.exe_types.base_iterative_nodes import BaseIterativeEndNode, BaseIterativeStartNode
from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
from griptape_nodes.exe_types.node_types import DataNode
from griptape_nodes.node_library.library_registry import LibraryMetadata, LibraryRegistry, LibrarySchema, NodeMetadata
from griptape_nodes.retained_mode.events.connection_events import (
    CreateConnectionRequest,
    CreateConnectionResultSuccess,
    DeleteConnectionRequest,
    DeleteConnectionResultSuccess,
)
from griptape_nodes.retained_mode.events.flow_events import CreateFlowRequest, CreateFlowResultSuccess
from griptape_nodes.retained_mode.events.node_events import CreateNodeRequest, CreateNodeResultSuccess
from griptape_nodes.retained_mode.events.object_events import ClearAllObjectStateRequest

if TYPE_CHECKING:
    from collections.abc import Iterator

    from griptape_nodes.retained_mode.engine import Engine

_LIBRARY_NAME = "Loop Strict Mode Library"


class _LoopEnd(BaseIterativeEndNode):
    @classmethod
    def _get_compatible_start_classes(cls) -> set[type]:
        return {_LoopStart}


class _LoopStart(BaseIterativeStartNode):
    @classmethod
    def _get_compatible_end_classes(cls) -> set[type]:
        return {_LoopEnd}

    def _get_parameter_group_name(self) -> str:
        return "Iteration Data"

    def _get_exec_out_display_name(self) -> str:
        return "On Each Item"

    def _get_exec_out_tooltip(self) -> str:
        return "Execute for each item"

    def _get_iteration_items(self) -> list[Any]:
        return []

    def _initialize_iteration_data(self) -> None:
        pass

    def _get_current_item_value(self) -> Any:
        return None

    def is_loop_finished(self) -> bool:
        return True

    def _get_total_iterations(self) -> int:
        return 0

    def _get_current_iteration_count(self) -> int:
        return 0

    def get_current_index(self) -> int:
        return 0

    def _advance_to_next_iteration(self) -> None:
        pass


class _ListSink(DataNode):
    """Takes a list, as a loop's results feed."""

    def __init__(self, name: str, metadata: dict | None = None) -> None:
        super().__init__(name, metadata=metadata)
        self.add_parameter(
            Parameter(name="items", tooltip="", type="list", input_types=["list"], allowed_modes={ParameterMode.INPUT})
        )

    def process(self) -> None:
        pass


@pytest.fixture
def loop_nodes(engine: Engine) -> Iterator[None]:
    """An unconnected Start "start", End "end" and list-taking "sink" in a fresh flow."""
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
    for node_class in (_LoopStart, _LoopEnd, _ListSink):
        library.register_new_node_type(
            node_class, NodeMetadata(category="test", description="d", display_name=node_class.__name__)
        )
    engine.context_manager.push_workflow(workflow_name="loop_strict_mode_wf")
    flow = engine.handle_request(CreateFlowRequest(parent_flow_name=None, flow_name="main", set_as_new_context=True))
    assert isinstance(flow, CreateFlowResultSuccess), flow
    # initial_setup keeps the Start from creating and connecting its own End.
    for node_type, name in (("_LoopStart", "start"), ("_LoopEnd", "end"), ("_ListSink", "sink")):
        created = engine.handle_request(CreateNodeRequest(node_type=node_type, node_name=name, initial_setup=True))
        assert isinstance(created, CreateNodeResultSuccess), created
    try:
        yield
    finally:
        engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
        LibraryRegistry._clear()


@pytest.mark.usefixtures("loop_nodes")
class TestLoopPairingIsNotStateOutsideParameters:
    def test_connecting_start_to_end_reports_no_violation(self, engine: Engine) -> None:
        with STRICT_MODE.open_scope(
            kind=StrictModeScopeKind.EDIT, subject="start", library_name=_LIBRARY_NAME, is_worker=False
        ) as scope:
            result = engine.handle_request(
                CreateConnectionRequest(
                    source_node_name="start",
                    source_parameter_name="loop",
                    target_node_name="end",
                    target_parameter_name="from_start",
                )
            )

        assert isinstance(result, CreateConnectionResultSuccess), result
        start = engine.node_manager.get_node_by_name("start")
        assert isinstance(start, _LoopStart)
        assert isinstance(start.end_node, _LoopEnd)
        assert start.end_node.start_node is start
        assert [v.rule_id for v in scope.violations if v.rule_id == "state-outside-parameters"] == []

    def test_connecting_and_disconnecting_results_reports_no_violation(self, engine: Engine) -> None:
        end = engine.node_manager.get_node_by_name("end")
        assert isinstance(end, _LoopEnd)

        with STRICT_MODE.open_scope(
            kind=StrictModeScopeKind.EDIT, subject="end", library_name=_LIBRARY_NAME, is_worker=False
        ) as scope:
            connected = engine.handle_request(
                CreateConnectionRequest(
                    source_node_name="end",
                    source_parameter_name="results",
                    target_node_name="sink",
                    target_parameter_name="items",
                )
            )
            assert isinstance(connected, CreateConnectionResultSuccess), connected
            assert end.results_output is not None
            disconnected = engine.handle_request(
                DeleteConnectionRequest(
                    source_node_name="end",
                    source_parameter_name="results",
                    target_node_name="sink",
                    target_parameter_name="items",
                )
            )

        assert isinstance(disconnected, DeleteConnectionResultSuccess), disconnected
        assert end.results_output is None
        assert [v.rule_id for v in scope.violations if v.rule_id == "state-outside-parameters"] == []
