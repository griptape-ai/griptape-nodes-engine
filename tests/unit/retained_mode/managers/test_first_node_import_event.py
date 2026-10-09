"""Creating the first node from a lazily loaded library says its nodes are loading, before the import."""

from __future__ import annotations

import asyncio
import logging
import sys
import types
from dataclasses import dataclass
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from griptape_nodes.exe_types.node_types import BaseNode
from griptape_nodes.node_library.library_registry import (
    CategoryDefinition,
    LibraryMetadata,
    LibraryRegistry,
    LibraryRegistryError,
    LibrarySchema,
    NodeDefinition,
    NodeMetadata,
)
from griptape_nodes.retained_mode.events.app_events import InitializationStatus, LibraryNodesLoading
from griptape_nodes.retained_mode.events.base_events import AppEvent, EventResultFailure, RequestPayload
from griptape_nodes.retained_mode.events.flow_events import CreateFlowRequest, CreateFlowResultSuccess
from griptape_nodes.retained_mode.events.node_events import CreateNodeRequest, CreateNodeResultFailure
from griptape_nodes.retained_mode.managers.event_manager import _AsyncRequestPreparer
from griptape_nodes.retained_mode.managers.library_manager import LibraryManager
from griptape_nodes.retained_mode.managers.node_manager import NodeManager

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator
    from contextlib import AbstractContextManager
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

_FAST_NODE_SOURCE = """
from griptape_nodes.exe_types.node_types import BaseNode


class FastNode(BaseNode):
    def process(self):
        return None
"""

# Imports fine, then fails to build: the library's nodes did load.
_INIT_FAILS_NODE_SOURCE = """
from griptape_nodes.exe_types.node_types import BaseNode


class InitFailsNode(BaseNode):
    def __init__(self, **kwargs):
        raise RuntimeError("this node cannot be built")

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


class _EagerNode(BaseNode):
    def process(self) -> None:
        return None


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
    (tmp_path / "fast_node.py").write_text(_FAST_NODE_SOURCE)
    (tmp_path / "init_fails_node.py").write_text(_INIT_FAILS_NODE_SOURCE)
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
            NodeDefinition(
                class_name="FastNode",
                file_path="fast_node.py",
                metadata=NodeMetadata(category="Test", description="test", display_name="Fast"),
            ),
            NodeDefinition(
                class_name="InitFailsNode",
                file_path="init_fails_node.py",
                metadata=NodeMetadata(category="Test", description="test", display_name="Init Fails"),
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
    @pytest.mark.usefixtures("recorder")
    async def test_logs_the_import_and_how_long_it_took(self, engine: Engine, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.INFO, logger="griptape_nodes"):
            await engine.ahandle_request(_create("SlowNode"))

        info_messages = [record.getMessage() for record in caplog.records if record.levelno == logging.INFO]
        assert (
            f"Loading nodes from library '{_LIBRARY_NAME}' for the first time, for a 'SlowNode' node. "
            "This can take a minute."
        ) in info_messages
        assert any(message.startswith(f"Loaded nodes from library '{_LIBRARY_NAME}' in ") for message in info_messages)

    def test_logs_the_import_on_the_sync_path_too(self, engine: Engine, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.INFO, logger="griptape_nodes"):
            engine.handle_request(_create("SlowNode"))

        assert any(
            record.getMessage().startswith(f"Loading nodes from library '{_LIBRARY_NAME}' for the first time")
            for record in caplog.records
        )

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("recorder")
    async def test_a_policy_denied_node_logs_no_loading_line(
        self, engine: Engine, caplog: pytest.LogCaptureFixture
    ) -> None:
        with (
            patch.object(NodeManager, "_evaluate_instantiation_checkpoint", return_value=MagicMock()),
            caplog.at_level(logging.INFO, logger="griptape_nodes"),
        ):
            await engine.ahandle_request(_create("SlowNode"))

        assert not any("for the first time" in record.getMessage() for record in caplog.records)

    @pytest.mark.asyncio
    async def test_a_second_node_sends_nothing(self, engine: Engine, recorder: _Recorder) -> None:
        await engine.ahandle_request(_create("SlowNode"))
        recorder.events.clear()

        await engine.ahandle_request(_create("SlowNode"))

        assert recorder.events == []

    @pytest.mark.asyncio
    async def test_another_node_type_from_the_same_library_sends_nothing(
        self, engine: Engine, recorder: _Recorder
    ) -> None:
        await engine.ahandle_request(_create("SlowNode"))
        recorder.events.clear()

        # FastNode lives in its own file, so its module still imports, but the library's
        # shared packages already have.
        await engine.ahandle_request(_create("FastNode"))

        assert recorder.events == []

    @pytest.mark.asyncio
    async def test_a_node_that_imports_but_fails_to_build_reports_the_import_complete(
        self, engine: Engine, recorder: _Recorder
    ) -> None:
        await engine.ahandle_request(_create("InitFailsNode"))

        assert [event.status for event in recorder.events] == [
            InitializationStatus.LOADING,
            InitializationStatus.COMPLETE,
        ]

    @pytest.mark.asyncio
    async def test_a_create_that_fails_before_importing_still_closes_its_announcement(
        self, engine: Engine, recorder: _Recorder
    ) -> None:
        request = CreateNodeRequest(
            node_type="SlowNode", specific_library_name=_LIBRARY_NAME, override_parent_flow_name="No Such Flow"
        )

        await engine.ahandle_request(request)

        assert [event.status for event in recorder.events] == [
            InitializationStatus.LOADING,
            InitializationStatus.FAILED,
        ]
        assert recorder.events_when_module_imported is None
        assert engine.node_manager._announced_node_imports == {}

    @pytest.mark.asyncio
    async def test_a_library_loaded_by_another_request_during_the_yield_still_closes_the_announcement(
        self, engine: Engine, recorder: _Recorder
    ) -> None:
        async def another_request_loads_the_library(_seconds: float) -> None:
            LibraryRegistry.get_library(_LIBRARY_NAME).get_node_class("FastNode")

        with patch(
            "griptape_nodes.retained_mode.managers.node_manager.asyncio.sleep",
            side_effect=another_request_loads_the_library,
        ):
            await engine.ahandle_request(_create("SlowNode"))

        assert [event.status for event in recorder.events] == [
            InitializationStatus.LOADING,
            InitializationStatus.COMPLETE,
        ]

    @pytest.mark.asyncio
    async def test_a_create_cancelled_during_the_yield_closes_its_announcement(
        self, engine: Engine, recorder: _Recorder
    ) -> None:
        with (
            patch(
                "griptape_nodes.retained_mode.managers.node_manager.asyncio.sleep",
                side_effect=asyncio.CancelledError,
            ),
            pytest.raises(asyncio.CancelledError),
        ):
            await engine.ahandle_request(_create("SlowNode"))

        assert [event.status for event in recorder.events] == [
            InitializationStatus.LOADING,
            InitializationStatus.FAILED,
        ]
        assert engine.node_manager._announced_node_imports == {}

    @pytest.mark.asyncio
    async def test_a_node_type_registered_with_its_class_is_not_announced(
        self, engine: Engine, recorder: _Recorder
    ) -> None:
        LibraryRegistry.get_library(_LIBRARY_NAME).register_new_node_type(
            _EagerNode, NodeMetadata(category="Test", description="test", display_name="Eager")
        )

        await engine.ahandle_request(_create("_EagerNode"))

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


@pytest.mark.usefixtures("lazy_library")
class TestNothingIsAnnouncedWithoutAnImport:
    @pytest.mark.asyncio
    async def test_a_library_that_is_not_registered(self, engine: Engine, recorder: _Recorder) -> None:
        await engine.ahandle_request(CreateNodeRequest(node_type="SlowNode", specific_library_name="No Such Library"))

        assert recorder.events == []

    @pytest.mark.asyncio
    async def test_a_request_that_is_not_a_node_creation(self, engine: Engine, recorder: _Recorder) -> None:
        await engine.node_manager._aprepare_create_node(CreateFlowRequest(parent_flow_name=None))

        assert recorder.events == []


@pytest.mark.usefixtures("lazy_library")
class TestIsNodeTypeLoaded:
    def test_is_false_until_the_node_type_is_first_used(self) -> None:
        library = LibraryRegistry.get_library(_LIBRARY_NAME)

        assert library.is_node_type_loaded("SlowNode") is False

    @pytest.mark.usefixtures("recorder")
    def test_is_true_once_the_node_type_has_been_used(self) -> None:
        library = LibraryRegistry.get_library(_LIBRARY_NAME)

        library.get_node_class("SlowNode")

        assert library.is_node_type_loaded("SlowNode") is True

    def test_a_node_type_registered_with_its_class_does_not_count_as_loaded(self) -> None:
        library = LibraryRegistry.get_library(_LIBRARY_NAME)

        # Like a workflow node: registered with its class in hand, so it imports nothing.
        library.register_new_node_type(
            _EagerNode, NodeMetadata(category="Test", description="test", display_name="Eager")
        )

        assert library.has_loaded_node_types() is False

    @pytest.mark.usefixtures("recorder")
    def test_a_library_counts_as_loaded_once_any_lazy_node_type_is(self) -> None:
        library = LibraryRegistry.get_library(_LIBRARY_NAME)

        library.get_node_class("FastNode")

        assert library.has_loaded_node_types() is True

    def test_raises_for_a_node_type_the_library_does_not_have(self) -> None:
        library = LibraryRegistry.get_library(_LIBRARY_NAME)

        with pytest.raises(LibraryRegistryError):
            library.is_node_type_loaded("NoSuchNode")


@dataclass
class _UnhandledRequest(RequestPayload):
    pass


def _preparer_for_node_creation(
    engine: Engine, preparer: Callable[[RequestPayload], Awaitable[None]]
) -> AbstractContextManager[object]:
    """Swap in a preparer for `CreateNodeRequest`, tied to the engine's own handler for it."""
    event_manager = engine.event_manager
    handler = event_manager._request_type_to_manager[CreateNodeRequest]
    return patch.dict(
        event_manager._async_request_preparers,
        {CreateNodeRequest: _AsyncRequestPreparer(handler=handler, preparer=preparer)},
    )


class TestAsyncRequestPreparer:
    @pytest.mark.asyncio
    async def test_runs_before_the_handler_on_the_async_path_only(self, engine: Engine) -> None:
        calls: list[str] = []

        async def preparer(_request: object) -> None:
            calls.append("preparer")

        def handler(request: CreateNodeRequest) -> CreateNodeResultFailure:
            calls.append("handler")
            return CreateNodeResultFailure(result_details=f"not creating {request.node_type}")

        event_manager = engine.event_manager
        with (
            patch.dict(
                event_manager._async_request_preparers,
                {CreateNodeRequest: _AsyncRequestPreparer(handler=handler, preparer=preparer)},
            ),
            patch.dict(event_manager._request_type_to_manager, {CreateNodeRequest: handler}),
        ):
            await engine.ahandle_request(CreateNodeRequest(node_type="AnyNode"))
            engine.handle_request(CreateNodeRequest(node_type="AnyNode"))

        assert calls == ["preparer", "handler", "handler"]

    @pytest.mark.asyncio
    async def test_a_preparer_that_raises_is_reported_as_the_requests_failure(self, engine: Engine) -> None:
        async def preparer(_request: object) -> None:
            raise RuntimeError

        with _preparer_for_node_creation(engine, preparer):
            result = await engine.event_manager.ahandle_request(CreateNodeRequest(node_type="AnyNode"))

        assert isinstance(result, EventResultFailure)

    @pytest.mark.asyncio
    async def test_a_request_a_pre_dispatch_hook_refuses_never_reaches_the_preparer(self, engine: Engine) -> None:
        calls: list[str] = []

        async def preparer(_request: object) -> None:
            calls.append("preparer")

        def refuse(_request: object, _context: object) -> CreateNodeResultFailure:
            return CreateNodeResultFailure(result_details="refused")

        event_manager = engine.event_manager
        event_manager.add_pre_dispatch_hook(refuse)
        try:
            with _preparer_for_node_creation(engine, preparer):
                await event_manager.ahandle_request(CreateNodeRequest(node_type="AnyNode"))
        finally:
            event_manager.remove_pre_dispatch_hook(refuse)

        assert calls == []

    @pytest.mark.asyncio
    async def test_is_skipped_when_another_handler_has_taken_the_request_type_over(self, engine: Engine) -> None:
        calls: list[str] = []

        async def preparer(_request: object) -> None:
            calls.append("preparer")

        def forwarding_handler(request: CreateNodeRequest) -> CreateNodeResultFailure:
            calls.append("forwarded")
            return CreateNodeResultFailure(result_details=f"forwarded {request.node_type}")

        event_manager = engine.event_manager
        # Like a worker, which forwards node creation to the orchestrator instead of handling it.
        with (
            _preparer_for_node_creation(engine, preparer),
            patch.dict(event_manager._request_type_to_manager, {CreateNodeRequest: forwarding_handler}),
        ):
            await event_manager.ahandle_request(CreateNodeRequest(node_type="AnyNode"))

        assert calls == ["forwarded"]

    def test_a_request_type_with_no_handler_cannot_get_a_preparer(self, engine: Engine) -> None:
        async def preparer(_request: object) -> None:
            return None

        with pytest.raises(ValueError, match="no handler is registered"):
            engine.event_manager.register_async_request_preparer(_UnhandledRequest, preparer)

    def test_a_second_preparer_for_the_same_request_type_is_refused(self, engine: Engine) -> None:
        async def preparer(_request: object) -> None:
            return None

        with pytest.raises(ValueError, match="already registered"):
            engine.event_manager.register_async_request_preparer(CreateNodeRequest, preparer)
