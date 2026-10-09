"""Creating the first node from a lazily loaded library says its nodes are loading, before the import."""

from __future__ import annotations

import sys
import types
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from griptape_nodes.node_library.library_registry import (
    CategoryDefinition,
    LibraryMetadata,
    LibraryRegistry,
    LibrarySchema,
    NodeDefinition,
    NodeMetadata,
)
from griptape_nodes.retained_mode.events.app_events import InitializationStatus, LibraryNodesLoading
from griptape_nodes.retained_mode.events.base_events import AppEvent
from griptape_nodes.retained_mode.events.flow_events import CreateFlowRequest, CreateFlowResultSuccess
from griptape_nodes.retained_mode.events.node_events import CreateNodeRequest
from griptape_nodes.retained_mode.managers.library_manager import LibraryManager
from griptape_nodes.retained_mode.managers.node_manager import NodeManager

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from griptape_nodes.retained_mode.engine import Engine

_LIBRARY_NAME = "First Import Test Library"
_PROBE_MODULE = "_first_node_import_probe"

# Calls the probe as the module executes, so a test can see which events were sent before the import.
_SLOW_NODE_SOURCE = f"""
import {_PROBE_MODULE}

from griptape_nodes.exe_types.node_types import BaseNode

{_PROBE_MODULE}.on_import()


class SlowNode(BaseNode):
    def process(self):
        return None
"""

_BROKEN_NODE_SOURCE = """
import definitely_not_a_real_module_zzz  # noqa: F401

from griptape_nodes.exe_types.node_types import BaseNode


class BrokenNode(BaseNode):
    def process(self):
        return None
"""


class _Recorder:
    """Records the library-loading events the engine sends, and those already sent at import time."""

    def __init__(self) -> None:
        self.events: list[LibraryNodesLoading] = []
        self.events_when_module_imported: list[LibraryNodesLoading] | None = None

    def put_event(self, event: object) -> None:
        if isinstance(event, AppEvent) and isinstance(event.payload, LibraryNodesLoading):
            self.events.append(event.payload)

    def on_import(self) -> None:
        self.events_when_module_imported = list(self.events)


@pytest.fixture(autouse=True)
def _clear_registry() -> Iterator[None]:
    LibraryRegistry._clear()
    yield
    LibraryRegistry._clear()


@pytest.fixture
def recorder(engine: Engine) -> Iterator[_Recorder]:
    """Capture the engine's library-loading events, and install the probe the slow node module calls."""
    recorder = _Recorder()
    sys.modules[_PROBE_MODULE] = types.SimpleNamespace(on_import=recorder.on_import)  # type: ignore[assignment]
    with patch.object(engine.event_manager, "put_event", side_effect=recorder.put_event):
        yield recorder
    sys.modules.pop(_PROBE_MODULE, None)


@pytest.fixture
def lazy_library(engine: Engine, tmp_path: Path) -> None:
    """Register a library whose node modules have not been imported yet, and a flow to create into."""
    (tmp_path / "slow_node.py").write_text(_SLOW_NODE_SOURCE)
    (tmp_path / "broken_node.py").write_text(_BROKEN_NODE_SOURCE)
    schema = LibrarySchema(
        name=_LIBRARY_NAME,
        library_schema_version=LibrarySchema.LATEST_SCHEMA_VERSION,
        metadata=LibraryMetadata(
            author="test", description="test", library_version="1.0.0", engine_version="1.0.0", tags=[]
        ),
        categories=[{"Test": CategoryDefinition(title="Test", description="test", color="#000", icon="Folder")}],
        nodes=[
            NodeDefinition(
                class_name="SlowNode",
                file_path="slow_node.py",
                metadata=NodeMetadata(category="Test", description="test", display_name="Slow"),
            ),
            NodeDefinition(
                class_name="BrokenNode",
                file_path="broken_node.py",
                metadata=NodeMetadata(category="Test", description="test", display_name="Broken"),
            ),
        ],
    )
    library = LibraryRegistry.generate_new_library(library_data=schema)
    info = LibraryManager.LibraryInfo(
        lifecycle_state=LibraryManager.LibraryLifecycleState.METADATA_LOADED,
        library_path=str(tmp_path / "griptape_nodes_library.json"),
        is_sandbox=False,
        library_name=schema.name,
        library_version="1.0.0",
        fitness=LibraryManager.LibraryFitness.NOT_EVALUATED,
        problems=[],
    )
    engine.library_manager.module_loading.attempt_load_nodes_from_library(
        library_data=schema, library=library, base_dir=tmp_path, library_info=info, lazy_loading=True
    )
    engine.context_manager.push_workflow("first_import_wf")
    flow = engine.handle_request(
        CreateFlowRequest(parent_flow_name=None, flow_name="first_import_flow", set_as_new_context=True)
    )
    assert isinstance(flow, CreateFlowResultSuccess)


def _create(node_type: str) -> CreateNodeRequest:
    return CreateNodeRequest(node_type=node_type, specific_library_name=_LIBRARY_NAME)


@pytest.mark.usefixtures("lazy_library")
class TestCreatingTheFirstNode:
    @pytest.mark.asyncio
    async def test_says_the_library_is_loading_before_the_import_then_that_it_finished(
        self, engine: Engine, recorder: _Recorder
    ) -> None:
        await engine.ahandle_request(_create("SlowNode"))

        loading = LibraryNodesLoading(
            library_name=_LIBRARY_NAME,
            node_type="SlowNode",
            status=InitializationStatus.LOADING,
            message=f"Loading {_LIBRARY_NAME} nodes for the first time. This can take a minute.",
        )
        assert recorder.events_when_module_imported == [loading]
        assert recorder.events == [
            loading,
            LibraryNodesLoading(library_name=_LIBRARY_NAME, node_type="SlowNode", status=InitializationStatus.COMPLETE),
        ]

    @pytest.mark.asyncio
    async def test_yields_after_announcing_so_the_event_is_sent_before_the_import(
        self, engine: Engine, recorder: _Recorder
    ) -> None:
        sleep = AsyncMock()

        with patch("griptape_nodes.retained_mode.managers.node_manager.asyncio.sleep", sleep):
            await engine.ahandle_request(_create("SlowNode"))

        sleep.assert_awaited_once()
        assert recorder.events_when_module_imported is not None

    @pytest.mark.asyncio
    async def test_a_second_node_sends_nothing(self, engine: Engine, recorder: _Recorder) -> None:
        await engine.ahandle_request(_create("SlowNode"))
        recorder.events.clear()

        await engine.ahandle_request(_create("SlowNode"))

        assert recorder.events == []

    @pytest.mark.asyncio
    async def test_a_failed_import_is_reported_with_its_reason(self, engine: Engine, recorder: _Recorder) -> None:
        await engine.ahandle_request(_create("BrokenNode"))

        assert [event.status for event in recorder.events] == [
            InitializationStatus.LOADING,
            InitializationStatus.FAILED,
        ]
        assert recorder.events[-1].error is not None
        assert "definitely_not_a_real_module_zzz" in recorder.events[-1].error

    def test_a_node_created_without_the_async_path_gets_only_the_result(
        self, engine: Engine, recorder: _Recorder
    ) -> None:
        engine.handle_request(_create("SlowNode"))

        assert [event.status for event in recorder.events] == [InitializationStatus.COMPLETE]

    @pytest.mark.asyncio
    async def test_a_node_the_policy_denies_sends_nothing(self, engine: Engine, recorder: _Recorder) -> None:
        with patch.object(NodeManager, "_evaluate_instantiation_checkpoint", return_value=MagicMock()):
            await engine.ahandle_request(_create("SlowNode"))

        assert recorder.events == []
        assert recorder.events_when_module_imported is None

    @pytest.mark.asyncio
    async def test_an_unknown_node_type_sends_nothing(self, engine: Engine, recorder: _Recorder) -> None:
        await engine.ahandle_request(_create("NoSuchNode"))

        assert recorder.events == []


class TestAsyncRequestPreparer:
    @pytest.mark.asyncio
    async def test_runs_before_the_handler_on_the_async_path_only(self, engine: Engine) -> None:
        calls: list[str] = []

        async def preparer(_request: object) -> None:
            calls.append("preparer")

        with patch.dict(engine.event_manager._async_request_preparers, {CreateNodeRequest: preparer}):
            await engine.ahandle_request(CreateNodeRequest(node_type="NoSuchNode"))
            engine.handle_request(CreateNodeRequest(node_type="NoSuchNode"))

        assert calls == ["preparer"]
