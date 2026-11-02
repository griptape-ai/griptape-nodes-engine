"""Tests for FlowManager.on_extract_flow_commands_from_image_metadata."""

import asyncio
import contextlib
import itertools
import json
import tempfile
from collections.abc import Awaitable, Callable, Generator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest
from asyncio_thread_runner import ThreadRunner
from PIL import Image, ImageFile
from PIL.PngImagePlugin import PngInfo

from griptape_nodes.exe_types.connections import Connections
from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
from griptape_nodes.exe_types.flow import ControlFlow
from griptape_nodes.exe_types.node_groups.base_node_group import BaseNodeGroup
from griptape_nodes.exe_types.node_types import BaseNode, ControlNode, DataNode, StartNode
from griptape_nodes.machines.dag_builder import DagNodeCategories
from griptape_nodes.retained_mode.engine import Engine
from griptape_nodes.retained_mode.events.execution_events import (
    CancelAllRunsRequest,
    CancelAllRunsResultSuccess,
    CancelRunRequest,
    CancelRunResultFailure,
    CancelRunResultSuccess,
    FlowRunEndedEvent,
    FlowRunStartedEvent,
    ListRunsRequest,
    ListRunsResultSuccess,
    ResolveNodeRequest,
    ResolveNodeResultFailure,
    StartFlowFromNodeRequest,
    StartFlowFromNodeResultFailure,
    StartFlowRequest,
    StartFlowResultFailure,
    StartFlowResultSuccess,
)
from griptape_nodes.retained_mode.events.flow_events import (
    TRANSIENT_KEY,
    CreateFlowRequest,
    CreateFlowResultSuccess,
    DeleteFlowRequest,
    ExtractFlowCommandsFromImageMetadataRequest,
    ExtractFlowCommandsFromImageMetadataResultFailure,
    ExtractFlowCommandsFromImageMetadataResultSuccess,
    SerializedFlowCommands,
    SerializeFlowToCommandsRequest,
    SerializeFlowToCommandsResultSuccess,
)
from griptape_nodes.retained_mode.events.object_events import ClearAllObjectStateRequest
from griptape_nodes.retained_mode.events.validation_events import ValidateFlowDependenciesResultSuccess
from griptape_nodes.retained_mode.file_metadata.workflow_metadata import FLOW_COMMANDS_KEY
from griptape_nodes.retained_mode.managers.flow_manager import StartFlowFromNodeSpec, StartFlowSpec
from griptape_nodes.retained_mode.managers.node_manager import ResolveNodeSpec
from griptape_nodes.serialization.commands import encode_commands


def _data_parameter(name: str = "value") -> Parameter:
    """A plain data Parameter usable as input, property, and output."""
    return Parameter(
        name=name,
        type="str",
        default_value="",
        tooltip="",
        allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY, ParameterMode.OUTPUT},
    )


def _param(node: BaseNode, name: str) -> Parameter:
    """Fetch a parameter by name, asserting it exists (keeps the type checker happy)."""
    parameter = node.get_parameter_by_name(name)
    assert parameter is not None, f"{node.name} is missing parameter {name!r}"
    return parameter


class _ClassifyStartNode(StartNode):
    """StartNode carrying a passthrough ``value`` for classifier tests."""

    def __init__(self, name: str, metadata: dict | None = None) -> None:
        super().__init__(name, metadata)
        self.add_parameter(_data_parameter())

    def process(self) -> None: ...


class _ClassifyDataNode(DataNode):
    """DataNode carrying a passthrough ``value`` (its control params stay unconnected)."""

    def __init__(self, name: str, metadata: dict | None = None) -> None:
        super().__init__(name, metadata)
        self.add_parameter(_data_parameter())

    def process(self) -> None: ...


class _ClassifyControlNode(ControlNode):
    """ControlNode with exec_in/exec_out plus a passthrough ``value``."""

    def __init__(self, name: str, metadata: dict | None = None) -> None:
        super().__init__(name, metadata)
        self.add_parameter(_data_parameter())

    def process(self) -> None: ...


@pytest.fixture
def image_without_metadata() -> Generator[str, None, None]:
    """A plain PNG with no embedded text chunks."""
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        Image.new("RGB", (4, 4), color="red").save(f, format="PNG")
        path = f.name
    try:
        yield path
    finally:
        Path(path).unlink(missing_ok=True)


@pytest.fixture
def image_with_unrelated_metadata() -> Generator[str, None, None]:
    """A PNG that has metadata but no gtn flow commands key."""
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        info = PngInfo()
        info.add_text("Description", "not a workflow")
        Image.new("RGB", (4, 4), color="green").save(f, format="PNG", pnginfo=info)
        path = f.name
    try:
        yield path
    finally:
        Path(path).unlink(missing_ok=True)


def _image_with_flow_commands_text(text: str) -> str:
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        info = PngInfo()
        info.add_text(FLOW_COMMANDS_KEY, text)
        Image.new("RGB", (4, 4), color="blue").save(f, format="PNG", pnginfo=info)
        return f.name


@pytest.fixture
def empty_flow_commands(engine: Engine) -> SerializedFlowCommands:
    """Commands for a flow with nothing in it."""
    engine.context_manager.push_workflow(workflow_name="image_metadata_workflow")
    create_result = engine.handle_request(CreateFlowRequest(parent_flow_name=None, set_as_new_context=False))
    assert isinstance(create_result, CreateFlowResultSuccess)
    serialize_result = engine.handle_request(SerializeFlowToCommandsRequest(flow_name=create_result.flow_name))
    assert isinstance(serialize_result, SerializeFlowToCommandsResultSuccess)
    return serialize_result.serialized_flow_commands


@pytest.fixture
def image_with_flow_commands(empty_flow_commands: SerializedFlowCommands) -> Generator[str, None, None]:
    """A PNG whose FLOW_COMMANDS_KEY payload is flow commands encoded as JSON."""
    path = _image_with_flow_commands_text(json.dumps(encode_commands(empty_flow_commands)))
    try:
        yield path
    finally:
        Path(path).unlink(missing_ok=True)


class TestExtractFlowCommandsFromImageMetadata:
    """Covers the non-error success paths for images that carry no workflow payload."""

    def test_returns_success_with_none_when_image_has_no_metadata(
        self, engine: Engine, image_without_metadata: str
    ) -> None:
        flow_manager = engine.flow_manager
        request = ExtractFlowCommandsFromImageMetadataRequest(file_url_or_path=image_without_metadata)

        result = flow_manager.serialization.on_extract_flow_commands_from_image_metadata(request)

        assert isinstance(result, ExtractFlowCommandsFromImageMetadataResultSuccess)
        assert result.serialized_flow_commands is None
        assert result.altered_workflow_state is False

    def test_returns_success_with_none_when_flow_commands_key_missing(
        self, engine: Engine, image_with_unrelated_metadata: str
    ) -> None:
        flow_manager = engine.flow_manager
        request = ExtractFlowCommandsFromImageMetadataRequest(file_url_or_path=image_with_unrelated_metadata)

        result = flow_manager.serialization.on_extract_flow_commands_from_image_metadata(request)

        assert isinstance(result, ExtractFlowCommandsFromImageMetadataResultSuccess)
        assert result.serialized_flow_commands is None
        assert result.altered_workflow_state is False

    def test_returns_failure_when_file_missing(self, engine: Engine) -> None:
        flow_manager = engine.flow_manager
        request = ExtractFlowCommandsFromImageMetadataRequest(file_url_or_path="/does/not/exist.png")

        result = flow_manager.serialization.on_extract_flow_commands_from_image_metadata(request)

        assert isinstance(result, ExtractFlowCommandsFromImageMetadataResultFailure)

    def test_returns_commands_when_flow_commands_key_present(
        self, engine: Engine, image_with_flow_commands: str, empty_flow_commands: SerializedFlowCommands
    ) -> None:
        flow_manager = engine.flow_manager
        request = ExtractFlowCommandsFromImageMetadataRequest(file_url_or_path=image_with_flow_commands)

        result = flow_manager.serialization.on_extract_flow_commands_from_image_metadata(request)

        assert isinstance(result, ExtractFlowCommandsFromImageMetadataResultSuccess)
        assert result.serialized_flow_commands == empty_flow_commands
        assert result.altered_workflow_state is False

    @pytest.mark.parametrize(
        ("text", "reason"),
        [
            (json.dumps({"sentinel": "flow"}), "not in a layout Griptape Nodes writes"),
            (json.dumps({"version": 1, "commands": {"flow_name": "x"}}), "incomplete or damaged"),
            (json.dumps({"version": 2, "commands": {}}), "saved by a later version"),
            ("not json or base64!", "neither JSON nor base64"),
        ],
    )
    def test_returns_failure_when_flow_commands_are_unreadable(self, engine: Engine, text: str, reason: str) -> None:
        path = _image_with_flow_commands_text(text)
        try:
            result = engine.flow_manager.serialization.on_extract_flow_commands_from_image_metadata(
                ExtractFlowCommandsFromImageMetadataRequest(file_url_or_path=path)
            )
        finally:
            Path(path).unlink(missing_ok=True)

        assert isinstance(result, ExtractFlowCommandsFromImageMetadataResultFailure)
        assert reason in str(result.result_details)

    def test_closes_the_image_when_flow_commands_are_damaged(self, engine: Engine) -> None:
        path = _image_with_flow_commands_text(json.dumps({"version": 1, "commands": {"flow_name": "x"}}))
        opened: list[ImageFile.ImageFile] = []
        real_open = Image.open

        def _open(*args: Any, **kwargs: Any) -> ImageFile.ImageFile:
            image = real_open(*args, **kwargs)
            opened.append(image)
            return image

        with patch.object(Image, "open", _open):
            engine.flow_manager.serialization.on_extract_flow_commands_from_image_metadata(
                ExtractFlowCommandsFromImageMetadataRequest(file_url_or_path=path)
            )

        (image,) = opened
        assert image.fp is None
        Path(path).unlink()


class TestStartFlowRequestDefaultsToCurrentContext:
    """Tests for StartFlowRequest / StartFlowFromNodeRequest current-context fallback."""

    @pytest.mark.asyncio
    async def test_start_flow_fails_cleanly_when_no_flow_and_no_context(self, engine: Engine) -> None:
        from griptape_nodes.retained_mode.events.execution_events import (
            StartFlowRequest,
            StartFlowResultFailure,
        )

        flow_manager = engine.flow_manager
        engine.handle_request(
            __import__(
                "griptape_nodes.retained_mode.events.object_events",
                fromlist=["ClearAllObjectStateRequest"],
            ).ClearAllObjectStateRequest(i_know_what_im_doing=True)
        )

        assert not engine.context_manager.has_current_flow()

        result = await flow_manager.on_start_flow_request(StartFlowRequest())

        assert isinstance(result, StartFlowResultFailure)
        # Message should now name a concrete remediation, not the old generic one.
        assert "Current Context" in str(result.result_details)

    @pytest.mark.asyncio
    async def test_start_flow_uses_current_context_flow_when_name_omitted(self, engine: Engine) -> None:
        from unittest.mock import patch

        from griptape_nodes.retained_mode.events.execution_events import (
            StartFlowRequest,
            StartFlowResultFailure,
        )
        from griptape_nodes.retained_mode.events.flow_events import CreateFlowRequest, CreateFlowResultSuccess
        from griptape_nodes.retained_mode.events.object_events import ClearAllObjectStateRequest

        flow_manager = engine.flow_manager
        engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
        # Bootstrap manually via push_workflow + CreateFlowRequest so this test does not
        # depend on any sibling MCP-bootstrap PR landing first.
        engine.context_manager.push_workflow("wf")
        create_flow_result = engine.handle_request(
            CreateFlowRequest(parent_flow_name=None, flow_name="flow_in_ctx", set_as_new_context=True)
        )
        assert isinstance(create_flow_result, CreateFlowResultSuccess)

        # Short-circuit get_flow_by_name so we can assert on the resolved name without
        # actually running a control flow.
        with patch.object(flow_manager, "get_flow_by_name", side_effect=KeyError("stop here")) as get_flow:
            result = await flow_manager.on_start_flow_request(StartFlowRequest())

        # The handler should have looked up the current-context flow name, not bailed with
        # the "must provide flow name" error.
        assert isinstance(result, StartFlowResultFailure)
        get_flow.assert_called_once_with("flow_in_ctx")

        engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))

    @pytest.mark.asyncio
    async def test_start_flow_from_node_fails_cleanly_when_no_node_and_no_context(self, engine: Engine) -> None:
        from griptape_nodes.retained_mode.events.execution_events import (
            StartFlowFromNodeRequest,
            StartFlowFromNodeResultFailure,
        )
        from griptape_nodes.retained_mode.events.object_events import ClearAllObjectStateRequest

        flow_manager = engine.flow_manager
        engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))

        assert not engine.context_manager.has_current_node()

        result = await flow_manager.on_start_flow_from_node_request(StartFlowFromNodeRequest())

        assert isinstance(result, StartFlowFromNodeResultFailure)
        assert "Current Context" in str(result.result_details)

    @pytest.mark.asyncio
    async def test_start_flow_from_node_uses_current_context_node_and_derives_parent_flow(self, engine: Engine) -> None:
        from unittest.mock import MagicMock, patch

        from griptape_nodes.exe_types.node_types import BaseNode
        from griptape_nodes.retained_mode.events.execution_events import (
            StartFlowFromNodeRequest,
            StartFlowFromNodeResultFailure,
        )
        from griptape_nodes.retained_mode.events.object_events import ClearAllObjectStateRequest

        flow_manager = engine.flow_manager
        engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))

        # Stand in a current node so the handler can fall back to it. The node itself only
        # needs to expose `.name`; we short-circuit object lookup below.
        fake_node = MagicMock(spec=BaseNode)
        fake_node.name = "node_in_ctx"
        ctx = engine.context_manager
        with (
            patch.object(ctx, "has_current_node", return_value=True),
            patch.object(ctx, "get_current_node", return_value=fake_node),
            patch.object(
                engine.object_manager,
                "attempt_get_object_by_name_as_type",
                return_value=fake_node,
            ),
            patch.object(
                engine.node_manager,
                "get_node_parent_flow_by_name",
                return_value="derived_parent_flow",
            ) as get_parent_flow,
            patch.object(flow_manager, "get_flow_by_name", side_effect=KeyError("stop here")) as get_flow,
        ):
            result = await flow_manager.on_start_flow_from_node_request(StartFlowFromNodeRequest())

        # The handler should have used the current-context node and derived its parent flow,
        # not bailed with the "must provide node name" error.
        assert isinstance(result, StartFlowFromNodeResultFailure)
        get_parent_flow.assert_called_once_with("node_in_ctx")
        get_flow.assert_called_once_with("derived_parent_flow")

        engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))


class TestStartFlowFailureMessage:
    """A failed run reports which flow failed and why."""

    @pytest.fixture
    def ready_flow_manager(self, engine: Engine) -> Generator[Any, None, None]:
        flow_manager = engine.flow_manager
        validated = ValidateFlowDependenciesResultSuccess(validation_succeeded=True, exceptions=[], result_details="ok")
        with (
            patch.object(flow_manager, "get_flow_by_name", return_value=MagicMock()),
            patch.object(flow_manager, "check_for_existing_running_flow", return_value=False),
            patch.object(flow_manager, "get_start_node_queue"),
            patch.object(flow_manager, "on_validate_flow_dependencies_request", return_value=validated),
        ):
            yield flow_manager

    @pytest.mark.asyncio
    async def test_start_flow_reports_exception(self, ready_flow_manager: Any) -> None:
        with patch.object(ready_flow_manager, "start_flow", side_effect=RuntimeError("boom")):
            result = await ready_flow_manager.on_start_flow_request(StartFlowRequest(flow_name="f"))

        assert isinstance(result, StartFlowResultFailure)
        assert str(result.result_details) == "Attempted to run flow 'f'. Failed due to: boom"

    @pytest.mark.asyncio
    async def test_start_flow_reports_resolution_error(self, ready_flow_manager: Any) -> None:
        machine = MagicMock()
        machine.resolution_machine.is_errored.return_value = True
        machine.resolution_machine.get_error_message.return_value = "Node 'n' encountered a problem: boom"
        with (
            patch.object(ready_flow_manager, "start_flow"),
            patch.object(ready_flow_manager, "_global_control_flow_machine", machine),
        ):
            result = await ready_flow_manager.on_start_flow_request(StartFlowRequest(flow_name="f"))

        assert isinstance(result, StartFlowResultFailure)
        assert (
            str(result.result_details)
            == "Attempted to run flow 'f'. Failed due to: Node 'n' encountered a problem: boom"
        )

    @pytest.mark.asyncio
    async def test_start_flow_from_node_reports_exception(self, ready_flow_manager: Any, engine: Engine) -> None:
        with (
            patch.object(engine.object_manager, "attempt_get_object_by_name_as_type", return_value=MagicMock()),
            patch.object(ready_flow_manager, "start_flow", side_effect=RuntimeError("boom")),
        ):
            result = await ready_flow_manager.on_start_flow_from_node_request(
                StartFlowFromNodeRequest(node_name="n", flow_name="f")
            )

        assert isinstance(result, StartFlowFromNodeResultFailure)
        assert str(result.result_details) == "Attempted to run flow 'f'. Failed due to: boom"


class TestListNodesInFlowRequest:
    """Tests for FlowManager.on_list_nodes_in_flow_request node_types filter."""

    def _make_flow(self, nodes: dict) -> object:
        from unittest.mock import MagicMock

        fake_flow = MagicMock()
        fake_flow.name = "test_flow"
        fake_flow.nodes = nodes
        return fake_flow

    def _run_request(self, engine: Engine, flow: object, request: object) -> object:
        from unittest.mock import patch

        from griptape_nodes.retained_mode.managers.flow_manager import ControlFlow

        flow_manager = engine.flow_manager
        with patch.object(
            engine.object_manager,
            "attempt_get_object_by_name_as_type",
            side_effect=lambda _name, typ: flow if typ is ControlFlow else None,
        ):
            return flow_manager.on_list_nodes_in_flow_request(request)  # type: ignore[arg-type]

    def test_no_filter_returns_all_nodes(self, engine: Engine) -> None:
        from griptape_nodes.retained_mode.events.flow_events import ListNodesInFlowRequest, ListNodesInFlowResultSuccess

        class NoteNode:
            pass

        flow = self._make_flow({"Note_1": NoteNode(), "Note_2": NoteNode()})
        result = self._run_request(engine, flow, ListNodesInFlowRequest(flow_name="test_flow"))

        assert isinstance(result, ListNodesInFlowResultSuccess)
        assert set(result.node_names) == {"Note_1", "Note_2"}

    def test_filter_by_matching_class_name_returns_subset(self, engine: Engine) -> None:
        from griptape_nodes.retained_mode.events.flow_events import ListNodesInFlowRequest, ListNodesInFlowResultSuccess

        class NoteNode:
            pass

        class AgentNode:
            pass

        flow = self._make_flow({"note_1": NoteNode(), "agent_1": AgentNode(), "note_2": NoteNode()})
        result = self._run_request(engine, flow, ListNodesInFlowRequest(flow_name="test_flow", node_types=["NoteNode"]))

        assert isinstance(result, ListNodesInFlowResultSuccess)
        assert set(result.node_names) == {"note_1", "note_2"}

    def test_filter_by_nonexistent_class_name_returns_empty(self, engine: Engine) -> None:
        from griptape_nodes.retained_mode.events.flow_events import ListNodesInFlowRequest, ListNodesInFlowResultSuccess

        class NoteNode:
            pass

        flow = self._make_flow({"note_1": NoteNode()})
        result = self._run_request(
            engine, flow, ListNodesInFlowRequest(flow_name="test_flow", node_types=["NonExistentClass"])
        )

        assert isinstance(result, ListNodesInFlowResultSuccess)
        assert result.node_names == []

    def test_filter_with_empty_list_returns_empty(self, engine: Engine) -> None:
        from griptape_nodes.retained_mode.events.flow_events import ListNodesInFlowRequest, ListNodesInFlowResultSuccess

        class NoteNode:
            pass

        flow = self._make_flow({"note_1": NoteNode()})
        result = self._run_request(engine, flow, ListNodesInFlowRequest(flow_name="test_flow", node_types=[]))

        assert isinstance(result, ListNodesInFlowResultSuccess)
        assert result.node_names == []

    def test_filter_with_multiple_types_returns_union(self, engine: Engine) -> None:
        from griptape_nodes.retained_mode.events.flow_events import ListNodesInFlowRequest, ListNodesInFlowResultSuccess

        class NoteNode:
            pass

        class AgentNode:
            pass

        class OtherNode:
            pass

        flow = self._make_flow({"note_1": NoteNode(), "agent_1": AgentNode(), "other_1": OtherNode()})
        result = self._run_request(
            engine,
            flow,
            ListNodesInFlowRequest(flow_name="test_flow", node_types=["NoteNode", "AgentNode"]),
        )

        assert isinstance(result, ListNodesInFlowResultSuccess)
        assert set(result.node_names) == {"note_1", "agent_1"}


class TestAutoLayoutFlowRequest:
    """Tests for FlowManager.on_auto_layout_flow_request."""

    def _cleanup(self, engine: Engine) -> None:
        from griptape_nodes.retained_mode.events.object_events import ClearAllObjectStateRequest

        engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))

    def _bootstrap_workflow_and_flow(self, engine: Engine, workflow: str, flow: str) -> None:
        """Push a workflow + create a flow without depending on any sibling bootstrap PR."""
        from griptape_nodes.retained_mode.events.flow_events import CreateFlowRequest, CreateFlowResultSuccess

        self._cleanup(engine)
        engine.context_manager.push_workflow(workflow)
        result = engine.handle_request(
            CreateFlowRequest(parent_flow_name=None, flow_name=flow, set_as_new_context=True)
        )
        assert isinstance(result, CreateFlowResultSuccess)

    def _bootstrap_graph(self, engine: Engine) -> str:
        """Create a small Workflow + Flow + 3 chained Note nodes (A -> B -> C) for layout tests.

        Uses `Note` from the registered Griptape Nodes Library because it has data params that
        can actually be connected end to end without triggering LLM calls or external deps.
        """
        from griptape_nodes.retained_mode.events.connection_events import CreateConnectionRequest
        from griptape_nodes.retained_mode.events.node_events import CreateNodeRequest, CreateNodeResultSuccess

        self._bootstrap_workflow_and_flow(engine, workflow="layout_wf", flow="layout_flow")

        names = []
        for desired in ("A", "B", "C"):
            result = engine.handle_request(CreateNodeRequest(node_type="Note", node_name=desired))
            assert isinstance(result, CreateNodeResultSuccess)
            names.append(result.node_name)

        # Wire A -> B -> C on the Note node's text parameter.
        for source, target in itertools.pairwise(names):
            conn_result = engine.handle_request(
                CreateConnectionRequest(
                    source_node_name=source,
                    source_parameter_name="note",
                    target_node_name=target,
                    target_parameter_name="note",
                )
            )
            # We don't strictly need this to succeed for layout tests (layout runs even without edges),
            # but the chain is what makes the topological case interesting.
            _ = conn_result

        return "layout_flow"

    @pytest.mark.asyncio
    async def test_fails_cleanly_when_no_flow_in_context_and_no_name(self, engine: Engine) -> None:
        from griptape_nodes.retained_mode.events.flow_events import (
            AutoLayoutFlowRequest,
            AutoLayoutFlowResultFailure,
        )

        self._cleanup(engine)
        flow_manager = engine.flow_manager

        result = await flow_manager.on_auto_layout_flow_request(AutoLayoutFlowRequest())

        assert isinstance(result, AutoLayoutFlowResultFailure)

    @pytest.mark.asyncio
    async def test_lays_out_linear_chain_into_columns(self, engine: Engine) -> None:
        from griptape_nodes.retained_mode.events.flow_events import (
            AutoLayoutFlowRequest,
            AutoLayoutFlowResultSuccess,
        )

        flow_name = self._bootstrap_graph(engine)
        flow_manager = engine.flow_manager

        result = await flow_manager.on_auto_layout_flow_request(
            AutoLayoutFlowRequest(
                flow_name=flow_name,
                origin_x=10.0,
                origin_y=20.0,
                layer_spacing=100.0,
                row_spacing=50.0,
            )
        )

        assert isinstance(result, AutoLayoutFlowResultSuccess)

        # A -> B -> C on the single data edge makes them land in three separate columns at y=20.
        positions = {p.node_name: (p.x, p.y) for p in result.positioned_nodes}
        assert positions["A"] == (10.0, 20.0)
        assert positions["B"] == (110.0, 20.0)
        assert positions["C"] == (210.0, 20.0)

        # Metadata was actually written on the live node objects.
        flow = flow_manager.get_flow_by_name(flow_name)
        assert flow.nodes["A"].metadata["position"] == {"x": 10.0, "y": 20.0}
        assert flow.nodes["C"].metadata["position"] == {"x": 210.0, "y": 20.0}

        self._cleanup(engine)

    @pytest.mark.asyncio
    async def test_empty_flow_is_handled_gracefully(self, engine: Engine) -> None:
        from griptape_nodes.retained_mode.events.flow_events import (
            AutoLayoutFlowRequest,
            AutoLayoutFlowResultSuccess,
        )

        self._bootstrap_workflow_and_flow(engine, workflow="empty_wf", flow="empty_flow")

        flow_manager = engine.flow_manager
        result = await flow_manager.on_auto_layout_flow_request(AutoLayoutFlowRequest(flow_name="empty_flow"))

        assert isinstance(result, AutoLayoutFlowResultSuccess)
        assert result.positioned_nodes == []

        self._cleanup(engine)


class TestExcludeSubflowGroupChildren:
    """Tests for FlowManager.exclude_subflow_group_children.

    This scope/ownership filter drops nodes owned by a SubflowNodeGroup so they are not seeded
    directly into a DAG (they run inside their group's own subflow). The top-level queue applies
    it to keep group members out of the cross-flow run; the isolated-subflow path deliberately
    does NOT, because within a group's own subflow those members are exactly what must resolve.
    """

    def test_drops_only_subflow_group_children(self, engine: Engine) -> None:
        from unittest.mock import MagicMock

        from griptape_nodes.exe_types.node_groups.subflow_node_group import SubflowNodeGroup
        from griptape_nodes.exe_types.node_types import BaseNode

        flow_manager = engine.flow_manager

        child = MagicMock(spec=BaseNode)
        child.name = "child"
        child.parent_group = MagicMock(spec=SubflowNodeGroup)

        free = MagicMock(spec=BaseNode)
        free.name = "free"
        free.parent_group = None

        # A parent_group that is not a SubflowNodeGroup must not be excluded.
        other_group_child = MagicMock(spec=BaseNode)
        other_group_child.name = "other_group_child"
        other_group_child.parent_group = MagicMock(spec=BaseNode)

        kept = flow_manager.exclude_subflow_group_children([child, free, other_group_child])

        assert [node.name for node in kept] == ["free", "other_group_child"]

    def test_empty_input_returns_empty(self, engine: Engine) -> None:
        flow_manager = engine.flow_manager

        assert flow_manager.exclude_subflow_group_children([]) == []


class TestGetInvolvedNodeNames:
    """Tests for FlowManager.get_involved_node_names.

    A group's children live in the group's own ``nodes`` dict, not the flow's, so the editor --
    which gates a node's run status on involvement -- could never light up the inside of a running
    group. This walk expands groups to any depth so the announcement names every node a viewer can
    see on the canvas.
    """

    def test_expands_group_children_to_any_depth(self, engine: Engine) -> None:
        def plain_node(name: str) -> MagicMock:
            node = MagicMock(spec=BaseNode)
            node.name = name
            return node

        def group(name: str, *children: MagicMock) -> MagicMock:
            node = MagicMock(spec=BaseNodeGroup)
            node.name = name
            node.nodes = {child.name: child for child in children}
            return node

        inner_group = group("inner_group", plain_node("deep_child"))
        outer_group = group("outer_group", plain_node("child"), inner_group)
        flow = MagicMock(spec=ControlFlow)
        flow.nodes = {"loose": plain_node("loose"), "outer_group": outer_group}

        involved = engine.flow_manager.get_involved_node_names(flow)

        assert sorted(involved) == ["child", "deep_child", "inner_group", "loose", "outer_group"]

    def test_a_child_the_flow_still_holds_is_named_once(self, engine: Engine) -> None:
        """A plain group leaves its children in the flow's ``nodes`` too, so the walk meets them twice.

        Only a SubflowNodeGroup relocates its children into a subflow; a plain BaseNodeGroup takes a
        node into its own dict and the flow goes on holding it. That is the ordinary, well-formed
        shape for such a group, and the announcement must not name the child twice because of it.
        """
        child = MagicMock(spec=BaseNode)
        child.name = "child"
        group = MagicMock(spec=BaseNodeGroup)
        group.name = "group"
        group.nodes = {"child": child}
        flow = MagicMock(spec=ControlFlow)
        flow.nodes = {"group": group, "child": child}

        involved = engine.flow_manager.get_involved_node_names(flow)

        assert sorted(involved) == ["child", "group"]

    def test_group_cycle_terminates(self, engine: Engine) -> None:
        """A malformed group that contains itself must not hang the walk."""
        cyclic_group = MagicMock(spec=BaseNodeGroup)
        cyclic_group.name = "cyclic_group"
        cyclic_group.nodes = {"cyclic_group": cyclic_group}
        flow = MagicMock(spec=ControlFlow)
        flow.nodes = {"cyclic_group": cyclic_group}

        assert engine.flow_manager.get_involved_node_names(flow) == ["cyclic_group"]

    def test_empty_flow_returns_empty(self, engine: Engine) -> None:
        flow = MagicMock(spec=ControlFlow)
        flow.nodes = {}

        assert engine.flow_manager.get_involved_node_names(flow) == []


class TestClassifyNodesForDag:
    """Tests for FlowManager.classify_nodes_for_dag.

    The classifier is scope-agnostic: it buckets an arbitrary list of nodes into start /
    control-entry / data-sink roles based purely on the connection graph. It backs both the
    top-level queue and isolated subflow seeding, so these tests lock in each branch. Real node
    subclasses and a real Connections object are used so the Parameter/Connection semantics match
    production; only get_connections is patched to hand the classifier the crafted graph.
    """

    @staticmethod
    def _classify(engine: Engine, nodes: list, connections: Connections) -> DagNodeCategories:
        from unittest.mock import patch

        flow_manager = engine.flow_manager
        with patch.object(flow_manager, "get_connections", return_value=connections):
            return flow_manager.classify_nodes_for_dag(nodes)

    def test_start_node_is_a_start_node(self, engine: Engine) -> None:
        start = _ClassifyStartNode("Start")
        data = _ClassifyDataNode("Data")
        connections = Connections()
        connections.add_connection(start, _param(start, "value"), data, _param(data, "value"))

        categories = self._classify(engine, [start, data], connections)

        assert [node.name for node in categories.start_nodes] == ["Start"]
        # Data has an incoming data connection and no outgoing one, so it is a terminal sink.
        assert [node.name for node in categories.data_sink_nodes] == ["Data"]
        assert categories.control_nodes == []

    def test_data_node_with_external_outgoing_is_not_a_sink(self, engine: Engine) -> None:
        upstream = _ClassifyDataNode("Upstream")
        downstream = _ClassifyDataNode("Downstream")
        connections = Connections()
        connections.add_connection(upstream, _param(upstream, "value"), downstream, _param(downstream, "value"))

        categories = self._classify(engine, [upstream, downstream], connections)

        # Only the leaf (Downstream) is a sink; Upstream feeds a downstream node so it is skipped.
        assert [node.name for node in categories.data_sink_nodes] == ["Downstream"]
        assert categories.start_nodes == []
        assert categories.control_nodes == []

    def test_control_chain_first_node_is_control_entry(self, engine: Engine) -> None:
        first = _ClassifyControlNode("First")
        second = _ClassifyControlNode("Second")
        connections = Connections()
        connections.add_connection(first, _param(first, "exec_out"), second, _param(second, "exec_in"))

        categories = self._classify(engine, [first, second], connections)

        # First drives the control flow; Second has an external incoming control edge so the
        # forward control walk reaches it and it is not seeded as an entry node.
        assert [node.name for node in categories.control_nodes] == ["First"]
        assert categories.start_nodes == []
        assert categories.data_sink_nodes == []

    def test_control_node_without_control_connections_is_treated_as_data_sink(self, engine: Engine) -> None:
        lone = _ClassifyControlNode("Lone")
        connections = Connections()

        categories = self._classify(engine, [lone], connections)

        # Control params exist but are unused, so the node is a plain data node with no outgoing
        # connection, i.e. a terminal sink.
        assert [node.name for node in categories.data_sink_nodes] == ["Lone"]
        assert categories.control_nodes == []
        assert categories.start_nodes == []

    def test_internal_node_group_outgoing_does_not_disqualify_sink(self, engine: Engine) -> None:
        source = _ClassifyDataNode("Source")
        target = _ClassifyDataNode("Target")
        connections = Connections()
        connections.add_connection(
            source,
            _param(source, "value"),
            target,
            _param(target, "value"),
            is_node_group_internal=True,
        )

        categories = self._classify(engine, [source, target], connections)

        # Internal NodeGroup connections do not count as external outgoing, so both nodes remain
        # terminal sinks.
        assert sorted(node.name for node in categories.data_sink_nodes) == ["Source", "Target"]

    def test_empty_scope_returns_empty_categories(self, engine: Engine) -> None:
        categories = self._classify(engine, [], Connections())

        assert categories.start_nodes == []
        assert categories.control_nodes == []
        assert categories.data_sink_nodes == []


@pytest.fixture
def clean_object_state(engine: Engine) -> Generator[None, None, None]:
    """Clear all object state around a test so leftover flows never bleed across tests.

    Yield-based so the teardown clear runs even when the test body fails.
    """
    engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
    try:
        yield
    finally:
        engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))


class TestSerializeFlowSkipsTransientChildFlows:
    """A child flow flagged ``transient`` in its metadata is omitted from serialization.

    Transient flows are runtime-only artifacts (e.g. a subflow a node imports at execution time);
    they must not be baked into the saved workflow, so ``on_serialize_flow_to_commands`` skips them
    while still serializing ordinary child flows.
    """

    @pytest.mark.usefixtures("clean_object_state")
    def test_transient_child_flow_is_not_serialized(self, engine: Engine) -> None:
        engine.context_manager.push_workflow("transient_wf")

        parent = engine.handle_request(
            CreateFlowRequest(parent_flow_name=None, flow_name="parent", set_as_new_context=True)
        )
        assert isinstance(parent, CreateFlowResultSuccess)
        keep = engine.handle_request(
            CreateFlowRequest(parent_flow_name=parent.flow_name, flow_name="child_keep", set_as_new_context=False)
        )
        assert isinstance(keep, CreateFlowResultSuccess)
        transient = engine.handle_request(
            CreateFlowRequest(parent_flow_name=parent.flow_name, flow_name="child_transient", set_as_new_context=False)
        )
        assert isinstance(transient, CreateFlowResultSuccess)

        flow_manager = engine.flow_manager
        flow_manager.get_flow_by_name(transient.flow_name).metadata[TRANSIENT_KEY] = True

        result = flow_manager.serialization.on_serialize_flow_to_commands(
            SerializeFlowToCommandsRequest(flow_name=parent.flow_name, include_create_flow_command=True)
        )
        assert isinstance(result, SerializeFlowToCommandsResultSuccess)

        serialized_child_flows = {sub.flow_name for sub in result.serialized_flow_commands.sub_flows_commands}
        assert keep.flow_name in serialized_child_flows
        assert transient.flow_name not in serialized_child_flows

    @pytest.mark.usefixtures("clean_object_state")
    def test_transient_child_flow_internal_connections_do_not_fail_serialization(self, engine: Engine) -> None:
        """A transient child flow with internal connections must not break the parent save.

        Transient flows are skipped by node serialization, so their nodes never enter the UUID map.
        Their connections must be skipped too — otherwise the connection pass would look up a node
        that isn't in the map and fail the whole save with "node not found in UUID map".
        """
        from griptape_nodes.retained_mode.events.connection_events import CreateConnectionRequest
        from griptape_nodes.retained_mode.events.node_events import CreateNodeRequest, CreateNodeResultSuccess

        engine.context_manager.push_workflow("transient_conn_wf")
        parent = engine.handle_request(
            CreateFlowRequest(parent_flow_name=None, flow_name="parent", set_as_new_context=True)
        )
        assert isinstance(parent, CreateFlowResultSuccess)
        transient = engine.handle_request(
            CreateFlowRequest(parent_flow_name=parent.flow_name, flow_name="child_transient", set_as_new_context=False)
        )
        assert isinstance(transient, CreateFlowResultSuccess)

        flow_manager = engine.flow_manager
        flow_manager.get_flow_by_name(transient.flow_name).metadata[TRANSIENT_KEY] = True

        # Two connected Note nodes INSIDE the transient flow (Note has connectable data params
        # and no external deps). This mirrors a per-iteration loop-body flow's internal wiring.
        with engine.context_manager.flow(transient.flow_name):
            node_names = []
            for desired in ("A", "B"):
                created = engine.handle_request(CreateNodeRequest(node_type="Note", node_name=desired))
                assert isinstance(created, CreateNodeResultSuccess)
                node_names.append(created.node_name)
            engine.handle_request(
                CreateConnectionRequest(
                    source_node_name=node_names[0],
                    source_parameter_name="note",
                    target_node_name=node_names[1],
                    target_parameter_name="note",
                )
            )

        # Serialization must succeed (not fail on the transient flow's internal connection) and
        # must not include the transient flow.
        result = flow_manager.serialization.on_serialize_flow_to_commands(
            SerializeFlowToCommandsRequest(flow_name=parent.flow_name, include_create_flow_command=True)
        )
        assert isinstance(result, SerializeFlowToCommandsResultSuccess), result

        serialized_child_flows = {sub.flow_name for sub in result.serialized_flow_commands.sub_flows_commands}
        assert transient.flow_name not in serialized_child_flows
        # No connection referencing the transient flow's nodes should have been emitted.
        assert result.serialized_flow_commands.serialized_connections == []


class TestDeleteIterationFlows:
    """NodeExecutor._delete_iteration_flows tears down every tracked iteration flow.

    This is the cleanup guarantee for iterative execution: whatever was deserialized must be gone
    once the loop is no longer running, on every exit path. The helper must also tolerate a flow
    that is already gone (e.g. a partially-run iteration cleaned itself up) without erroring.
    """

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("clean_object_state")
    async def test_deletes_all_tracked_flows(self, engine: Engine) -> None:
        from griptape_nodes.common.node_executor import NodeExecutor

        engine.context_manager.push_workflow("cleanup_wf")
        engine.handle_request(CreateFlowRequest(parent_flow_name=None, flow_name="parent", set_as_new_context=True))

        object_manager = engine.object_manager
        deserialized_flows: list[tuple[int, str, dict[str, str]]] = []
        for i in range(3):
            created = engine.handle_request(
                CreateFlowRequest(parent_flow_name="parent", flow_name=f"iter_{i}", set_as_new_context=False)
            )
            assert isinstance(created, CreateFlowResultSuccess)
            deserialized_flows.append((i, created.flow_name, {}))

        # Delete one flow out from under the helper to prove it tolerates an already-gone flow.
        already_gone = deserialized_flows[1][1]
        engine.handle_request(DeleteFlowRequest(flow_name=already_gone))
        assert object_manager.attempt_get_object_by_name(already_gone) is None

        executor = NodeExecutor(engine=engine)
        await executor._delete_iteration_flows(deserialized_flows, engine.event_manager)

        for _, flow_name, _ in deserialized_flows:
            assert object_manager.attempt_get_object_by_name(flow_name) is None


class TestReparentFlow:
    """Moving a Flow under a new parent, which is how a nested group's subflow finds its home.

    When a node group is nested inside another, the inner group's subflow has to become a child of
    the outer group's subflow. Serialization walks parent/child links, so a subflow left parented to
    the top-level flow is written outside its enclosing group and its members vanish on load.
    """

    @pytest.mark.usefixtures("clean_object_state")
    def test_moves_flow_under_new_parent(self, engine: Engine) -> None:
        engine.context_manager.push_workflow("reparent_wf")
        top = engine.handle_request(CreateFlowRequest(parent_flow_name=None, flow_name="top", set_as_new_context=False))
        assert isinstance(top, CreateFlowResultSuccess)
        outer = engine.handle_request(
            CreateFlowRequest(parent_flow_name=top.flow_name, flow_name="outer", set_as_new_context=False)
        )
        assert isinstance(outer, CreateFlowResultSuccess)
        inner = engine.handle_request(
            CreateFlowRequest(parent_flow_name=top.flow_name, flow_name="inner", set_as_new_context=False)
        )
        assert isinstance(inner, CreateFlowResultSuccess)

        flow_manager = engine.flow_manager
        flow_manager.reparent_flow(inner.flow_name, outer.flow_name)

        assert flow_manager.get_parent_flow(inner.flow_name) == outer.flow_name

    @pytest.mark.usefixtures("clean_object_state")
    def test_rejects_unknown_flows(self, engine: Engine) -> None:
        engine.context_manager.push_workflow("reparent_unknown_wf")
        real = engine.handle_request(
            CreateFlowRequest(parent_flow_name=None, flow_name="real", set_as_new_context=False)
        )
        assert isinstance(real, CreateFlowResultSuccess)
        flow_manager = engine.flow_manager

        with pytest.raises(ValueError, match="doesn't exist"):
            flow_manager.reparent_flow("ghost", real.flow_name)
        with pytest.raises(ValueError, match="doesn't exist"):
            flow_manager.reparent_flow(real.flow_name, "ghost")

    @pytest.mark.usefixtures("clean_object_state")
    def test_rejects_making_a_flow_its_own_parent(self, engine: Engine) -> None:
        engine.context_manager.push_workflow("reparent_self_wf")
        solo = engine.handle_request(
            CreateFlowRequest(parent_flow_name=None, flow_name="solo", set_as_new_context=False)
        )
        assert isinstance(solo, CreateFlowResultSuccess)

        with pytest.raises(ValueError, match="its own parent"):
            engine.flow_manager.reparent_flow(solo.flow_name, solo.flow_name)

    @pytest.mark.usefixtures("clean_object_state")
    def test_rejects_moving_a_flow_inside_its_own_descendant(self, engine: Engine) -> None:
        """That move would detach the branch and leave the ancestor walk with no way out."""
        engine.context_manager.push_workflow("reparent_cycle_wf")
        outer = engine.handle_request(
            CreateFlowRequest(parent_flow_name=None, flow_name="outer", set_as_new_context=False)
        )
        assert isinstance(outer, CreateFlowResultSuccess)
        inner = engine.handle_request(
            CreateFlowRequest(parent_flow_name=outer.flow_name, flow_name="inner", set_as_new_context=False)
        )
        assert isinstance(inner, CreateFlowResultSuccess)

        flow_manager = engine.flow_manager
        with pytest.raises(ValueError, match="already inside"):
            flow_manager.reparent_flow(outer.flow_name, inner.flow_name)

        # The rejected move must leave the hierarchy untouched.
        assert flow_manager.get_parent_flow(inner.flow_name) == outer.flow_name
        assert flow_manager.get_parent_flow(outer.flow_name) is None


@dataclass
class _Spec:
    """A run that executes `body`, standing in for a request's run."""

    flow_name: str
    body: Callable[[], Awaitable[Any]]

    async def execute(self, engine: Engine) -> Any:  # noqa: ARG002
        return await self.body()

    def failure(self, details: str) -> Any:
        return StartFlowResultFailure(validation_exceptions=[], result_details=details)


def _ran(name: str) -> StartFlowResultSuccess:
    return StartFlowResultSuccess(result_details=f"{name} ran")


class TestRunsTakeTurns:
    """A run started while another is in progress waits its turn; a request from inside a run does not."""

    @pytest.mark.asyncio
    async def test_a_run_started_while_another_is_in_progress_starts_once_it_ends(self, engine: Engine) -> None:
        flow_manager = engine.flow_manager
        first_may_end = asyncio.Event()
        order: list[str] = []

        async def first() -> Any:
            order.append("first started")
            await first_may_end.wait()
            order.append("first ended")
            return _ran("first")

        async def second() -> Any:
            order.append("second started")
            return _ran("second")

        running = asyncio.ensure_future(flow_manager.start_run(_Spec("first", first)))
        await asyncio.sleep(0)
        waiting = asyncio.ensure_future(flow_manager.start_run(_Spec("second", second)))
        await asyncio.sleep(0.05)
        assert order == ["first started"]

        first_may_end.set()
        await asyncio.wait_for(asyncio.gather(running, waiting), timeout=1.0)

        assert order == ["first started", "first ended", "second started"]

    @pytest.mark.asyncio
    async def test_a_cancelled_waiting_run_never_executes_and_its_request_says_so(self, engine: Engine) -> None:
        flow_manager = engine.flow_manager
        first_may_end = asyncio.Event()
        executed: list[str] = []

        async def first() -> Any:
            await first_may_end.wait()
            return _ran("first")

        async def second() -> Any:
            executed.append("second")
            return _ran("second")

        running = asyncio.ensure_future(flow_manager.start_run(_Spec("first", first)))
        await asyncio.sleep(0)
        waiting_run = flow_manager.submit_run(_Spec("second", second))

        assert await flow_manager.cancel_run(waiting_run.run_id) is True
        first_may_end.set()
        await asyncio.wait_for(running, timeout=1.0)

        result = await asyncio.wait_for(waiting_run.outcome, timeout=1.0)
        assert "did not start: it was cancelled" in str(result.result_details)
        assert executed == []

    @pytest.mark.asyncio
    async def test_a_request_from_inside_the_run_in_progress_does_not_wait_for_it(self, engine: Engine) -> None:
        flow_manager = engine.flow_manager

        async def nested() -> Any:
            return _ran("nested")

        async def outer() -> Any:
            return await flow_manager.start_run(_Spec("flow", nested))

        result = await asyncio.wait_for(flow_manager.start_run(_Spec("flow", outer)), timeout=1.0)

        assert str(result.result_details) == "nested ran"

    @pytest.mark.asyncio
    async def test_a_task_that_outlives_its_run_waits_like_any_other_request(self, engine: Engine) -> None:
        """A task copies the run it was created in, so its run having ended must not count as being inside one."""
        flow_manager = engine.flow_manager
        later_run_holds = asyncio.Event()
        later_run_may_end = asyncio.Event()
        leftover: list[asyncio.Task] = []

        async def first_run() -> Any:
            leftover.append(asyncio.ensure_future(_after(later_run_holds, flow_manager)))
            return _ran("first")

        async def later_run() -> Any:
            later_run_holds.set()
            await later_run_may_end.wait()
            return _ran("later")

        await flow_manager.start_run(_Spec("first", first_run))
        holding = asyncio.ensure_future(flow_manager.start_run(_Spec("later", later_run)))
        await asyncio.sleep(0.05)

        assert not leftover[0].done(), "a task left over from an ended run skipped the line"
        later_run_may_end.set()
        await asyncio.wait_for(holding, timeout=1.0)
        result = await asyncio.wait_for(leftover[0], timeout=1.0)
        assert str(result.result_details) == "left over ran"


async def _after(event: asyncio.Event, flow_manager: Any) -> Any:
    await event.wait()

    async def left_over() -> Any:
        return _ran("left over")

    return await flow_manager.start_run(_Spec("left over", left_over))


class TestRunsEndCleanly:
    """A run ends once, on the engine's loop, and waiting runs never outlive their workflow."""

    @pytest.mark.asyncio
    async def test_a_run_cancelled_while_it_validates_stops_before_starting_its_flow(self, engine: Engine) -> None:
        """Before its flow starts there is no flow to stop, so the run stops itself where it would start one."""
        flow_manager = engine.flow_manager
        may_start = asyncio.Event()

        async def first() -> Any:
            await may_start.wait()
            try:
                await flow_manager.start_flow(MagicMock())
            except RuntimeError as e:
                return StartFlowResultFailure(validation_exceptions=[e], result_details=str(e))
            return _ran("first")

        async def second() -> Any:
            return _ran("second")

        first_request = asyncio.ensure_future(flow_manager.start_run(_Spec("flow", first)))
        await asyncio.sleep(0)
        first_run = flow_manager._run_in_progress
        assert first_run is not None
        waiting = asyncio.ensure_future(flow_manager.start_run(_Spec("flow", second)))
        await asyncio.sleep(0)

        assert await flow_manager.cancel_run(first_run.run_id) is True
        may_start.set()

        first_result = await asyncio.wait_for(first_request, timeout=1.0)
        assert "did not start: it was cancelled" in str(first_result.result_details)
        second_result = await asyncio.wait_for(waiting, timeout=1.0)
        assert str(second_result.result_details) == "second ran"

    @pytest.mark.asyncio
    async def test_a_run_cancelled_before_its_driver_runs_never_executes(self, engine: Engine) -> None:
        flow_manager = engine.flow_manager
        executed: list[str] = []

        async def body() -> Any:
            executed.append("body")
            return _ran("body")

        run = flow_manager.submit_run(_Spec("flow", body))
        assert await flow_manager.cancel_run(run.run_id) is True

        result = await asyncio.wait_for(run.outcome, timeout=1.0)
        assert "did not start: it was cancelled" in str(result.result_details)
        assert executed == []

    @pytest.mark.asyncio
    async def test_a_run_submitted_while_all_runs_are_cancelled_is_turned_away_too(self, engine: Engine) -> None:
        flow_manager = engine.flow_manager
        flow_running = {"value": False}
        late: list[Any] = []
        executed: list[str] = []

        async def late_body() -> Any:
            executed.append("late")
            return _ran("late")

        async def stop_the_flow() -> None:
            late.append(flow_manager.submit_run(_Spec("late", late_body)))
            flow_running["value"] = False
            flow_manager._finish_run_if_over()

        async def first() -> Any:
            flow_running["value"] = True
            return _ran("first")

        with (
            patch.object(flow_manager, "check_for_existing_running_flow", side_effect=lambda: flow_running["value"]),
            patch.object(flow_manager, "cancel_flow_run", side_effect=stop_the_flow),
        ):
            await flow_manager.start_run(_Spec("flow", first))
            await flow_manager.cancel_all_runs()

        result = await asyncio.wait_for(late[0].outcome, timeout=1.0)
        assert "did not start: it was cancelled" in str(result.result_details)
        assert executed == []

    @pytest.mark.asyncio
    async def test_clearing_the_workflows_turns_away_the_run_waiting_behind_a_paused_one(self, engine: Engine) -> None:
        """Clearing cancels the paused run first, which would otherwise start the waiting one against deleted flows."""
        flow_manager = engine.flow_manager
        flow_running = {"value": False}
        executed: list[str] = []

        async def stop_the_flow() -> None:
            flow_running["value"] = False
            flow_manager._finish_run_if_over()

        async def first() -> Any:
            flow_running["value"] = True
            return _ran("first")

        async def second() -> Any:
            executed.append("second")
            return _ran("second")

        with (
            patch.object(flow_manager, "check_for_existing_running_flow", side_effect=lambda: flow_running["value"]),
            patch.object(flow_manager, "cancel_flow_run", side_effect=stop_the_flow),
        ):
            await flow_manager.start_run(_Spec("flow", first))
            waiting = asyncio.ensure_future(flow_manager.start_run(_Spec("flow", second)))
            await asyncio.sleep(0)

            await engine.ahandle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
            result = await asyncio.wait_for(waiting, timeout=1.0)

        assert "did not start: it was cancelled" in str(result.result_details)
        assert executed == []

    @pytest.mark.asyncio
    async def test_the_next_run_starts_on_the_engine_loop_when_the_last_one_ends_on_another(
        self, engine: Engine
    ) -> None:
        """A synchronous request, such as deleting a flow, runs its async handler on a short-lived loop."""
        flow_manager = engine.flow_manager
        flow_running = {"value": False}

        async def stop_the_flow() -> None:
            flow_running["value"] = False
            flow_manager._finish_run_if_over()

        async def first() -> Any:
            flow_running["value"] = True
            return _ran("first")

        async def second() -> Any:
            await asyncio.sleep(0.05)
            return _ran("second")

        with (
            patch.object(flow_manager, "check_for_existing_running_flow", side_effect=lambda: flow_running["value"]),
            patch.object(flow_manager, "cancel_flow_run", side_effect=stop_the_flow),
        ):
            await flow_manager.start_run(_Spec("flow", first))
            first_run = flow_manager._run_in_progress
            assert first_run is not None
            waiting = asyncio.ensure_future(flow_manager.start_run(_Spec("flow", second)))
            await asyncio.sleep(0)

            with ThreadRunner() as runner:
                runner.run(flow_manager.cancel_run(first_run.run_id))

            result = await asyncio.wait_for(waiting, timeout=1.0)
        assert str(result.result_details) == "second ran"


class TestRunsEndInTheRightState:
    """The state a run ends in says how it ended: the run queue reads it once the run is over."""

    @pytest.mark.asyncio
    async def test_a_run_whose_task_is_cancelled_ends_cancelled(self, engine: Engine) -> None:
        flow_manager = engine.flow_manager
        ended = _record_end_states(flow_manager)
        drivers: list[asyncio.Task] = []

        async def body() -> Any:
            # The run's body executes in the task that drives the run.
            drivers.append(cast("asyncio.Task", asyncio.current_task()))
            await asyncio.Event().wait()

        run = flow_manager.submit_run(_Spec("flow", body))
        for _ in range(10):
            if drivers:
                break
            await asyncio.sleep(0)
        (driver,) = drivers
        driver.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(driver), timeout=1.0)

        assert run.outcome.cancelled()
        assert ended == ["cancelled"]

    @pytest.mark.parametrize("cancel_all", [False, True])
    @pytest.mark.asyncio
    async def test_a_run_whose_cancel_fails_ends_as_its_flow_did(self, engine: Engine, *, cancel_all: bool) -> None:
        """Stopping the flow raised, so the cancel failed and the run went on to finish."""
        flow_manager = engine.flow_manager
        ended = _record_end_states(flow_manager)
        may_end = asyncio.Event()

        async def body() -> Any:
            await may_end.wait()
            return _ran("run")

        running = asyncio.ensure_future(flow_manager.start_run(_Spec("flow", body)))
        await asyncio.sleep(0)
        run = flow_manager._run_in_progress
        assert run is not None

        async def cancel() -> None:
            if cancel_all:
                await flow_manager.cancel_all_runs()
            else:
                await flow_manager.cancel_run(run.run_id)

        with (
            patch.object(flow_manager, "check_for_existing_running_flow", return_value=True),
            patch.object(flow_manager, "cancel_flow_run", side_effect=RuntimeError("worker gone")),
            pytest.raises(RuntimeError, match="worker gone"),
        ):
            await cancel()
        may_end.set()
        await asyncio.wait_for(running, timeout=1.0)

        assert ended == ["finished"]


def _record_end_states(flow_manager: Any) -> list[str]:
    """Record the state each run is in as the run queue ends it, which is how it says the run ended."""
    ended: list[str] = []
    finish_run = flow_manager._finish_run

    def recording_finish_run(run: Any) -> None:
        if flow_manager._run_in_progress is run:
            ended.append(run.state.value)
        finish_run(run)

    flow_manager._finish_run = recording_finish_run
    return ended


class TestRunSpecs:
    """A queued run looks up what it runs when it starts, so something deleted while it waited fails cleanly."""

    @pytest.mark.asyncio
    async def test_a_flow_run_whose_flow_is_gone_fails(self, engine: Engine) -> None:
        result = await StartFlowSpec(request=StartFlowRequest(flow_name="gone"), flow_name="gone").execute(engine)

        assert isinstance(result, StartFlowResultFailure)
        assert "Cannot start flow" in str(result.result_details)

    @pytest.mark.asyncio
    async def test_a_run_from_a_node_that_is_gone_fails(self, engine: Engine) -> None:
        spec = StartFlowFromNodeSpec(
            request=StartFlowFromNodeRequest(node_name="gone"), flow_name="flow", node_name="gone"
        )

        result = await spec.execute(engine)

        assert isinstance(result, StartFlowFromNodeResultFailure)
        assert "does not exist" in str(result.result_details)

    @pytest.mark.asyncio
    async def test_a_node_run_whose_node_is_gone_fails(self, engine: Engine) -> None:
        spec = ResolveNodeSpec(request=ResolveNodeRequest(node_name="gone"), flow_name="flow", node_name="gone")

        result = await spec.execute(engine)

        assert isinstance(result, ResolveNodeResultFailure)
        assert "does not exist" in str(result.result_details)


class TestRunRequests:
    """A script can see the runs and withdraw one that waits, through requests."""

    @pytest.mark.asyncio
    async def test_runs_are_listed_in_progress_first_then_waiting_in_order(self, engine: Engine) -> None:
        flow_manager = engine.flow_manager
        first_may_end = asyncio.Event()

        async def first() -> Any:
            await first_may_end.wait()
            return _ran("first")

        async def waits() -> Any:
            return _ran("waiting")

        running = asyncio.ensure_future(flow_manager.start_run(_Spec("first", first)))
        await asyncio.sleep(0)
        waiting = [asyncio.ensure_future(flow_manager.start_run(_Spec(name, waits))) for name in ("second", "third")]
        await asyncio.sleep(0)

        result = flow_manager.on_list_runs_request(ListRunsRequest())

        assert isinstance(result, ListRunsResultSuccess)
        assert [(run.flow_name, run.state, run.position) for run in result.runs] == [
            ("first", "running", 0),
            ("second", "queued", 1),
            ("third", "queued", 2),
        ]
        first_may_end.set()
        await asyncio.wait_for(asyncio.gather(running, *waiting), timeout=1.0)

    @pytest.mark.asyncio
    async def test_cancelling_a_waiting_run_by_request_withdraws_it(self, engine: Engine) -> None:
        flow_manager = engine.flow_manager
        first_may_end = asyncio.Event()
        executed: list[str] = []

        async def first() -> Any:
            await first_may_end.wait()
            return _ran("first")

        async def second() -> Any:
            executed.append("second")
            return _ran("second")

        running = asyncio.ensure_future(flow_manager.start_run(_Spec("first", first)))
        await asyncio.sleep(0)
        waiting_run = flow_manager.submit_run(_Spec("second", second))

        result = await flow_manager.on_cancel_run_request(CancelRunRequest(run_id=waiting_run.run_id))
        first_may_end.set()
        await asyncio.wait_for(running, timeout=1.0)

        assert isinstance(result, CancelRunResultSuccess)
        outcome = await asyncio.wait_for(waiting_run.outcome, timeout=1.0)
        assert "did not start: it was cancelled" in str(outcome.result_details)
        assert executed == []

    @pytest.mark.asyncio
    async def test_cancelling_a_run_that_does_not_exist_fails(self, engine: Engine) -> None:
        result = await engine.flow_manager.on_cancel_run_request(CancelRunRequest(run_id="no-such-run"))

        assert isinstance(result, CancelRunResultFailure)
        assert "no run with that id" in str(result.result_details)


class TestRunResultsNameTheirRun:
    """A run request's result says which run it started or joined."""

    @pytest.mark.asyncio
    async def test_a_runs_result_carries_its_run_id(self, engine: Engine) -> None:
        flow_manager = engine.flow_manager
        seen: list[str | None] = []

        async def body() -> Any:
            run = flow_manager._run_in_progress
            assert run is not None
            seen.append(run.run_id)
            return _ran("run")

        result = await flow_manager.start_run(_Spec("flow", body))

        assert isinstance(result, StartFlowResultSuccess)
        assert result.run_id == seen[0]

    @pytest.mark.asyncio
    async def test_a_request_from_inside_a_run_names_the_run_it_joined(self, engine: Engine) -> None:
        flow_manager = engine.flow_manager
        nested_results: list[Any] = []

        async def nested() -> Any:
            return _ran("nested")

        async def outer() -> Any:
            nested_results.append(await flow_manager.start_run(_Spec("flow", nested)))
            return _ran("outer")

        outer_result = await flow_manager.start_run(_Spec("flow", outer))

        assert isinstance(outer_result, StartFlowResultSuccess)
        assert nested_results[0].run_id == outer_result.run_id

    @pytest.mark.asyncio
    async def test_a_turned_away_runs_failure_names_the_run(self, engine: Engine) -> None:
        flow_manager = engine.flow_manager
        first_may_end = asyncio.Event()

        async def first() -> Any:
            await first_may_end.wait()
            return _ran("first")

        async def second() -> Any:
            return _ran("second")

        running = asyncio.ensure_future(flow_manager.start_run(_Spec("first", first)))
        await asyncio.sleep(0)
        waiting_run = flow_manager.submit_run(_Spec("second", second))

        await flow_manager.cancel_run(waiting_run.run_id)
        first_may_end.set()
        await asyncio.wait_for(running, timeout=1.0)

        failure = await asyncio.wait_for(waiting_run.outcome, timeout=1.0)
        assert isinstance(failure, StartFlowResultFailure)
        assert failure.run_id == waiting_run.run_id

    @pytest.mark.asyncio
    async def test_cancelling_all_runs_turns_away_the_waiting_ones_and_starts_none(self, engine: Engine) -> None:
        flow_manager = engine.flow_manager
        first_may_end = asyncio.Event()
        executed: list[str] = []

        async def first() -> Any:
            await first_may_end.wait()
            return _ran("first")

        async def second() -> Any:
            executed.append("second")
            return _ran("second")

        running = asyncio.ensure_future(flow_manager.start_run(_Spec("first", first)))
        await asyncio.sleep(0)
        waiting = asyncio.ensure_future(flow_manager.start_run(_Spec("second", second)))
        await asyncio.sleep(0)

        result = await flow_manager.on_cancel_all_runs_request(CancelAllRunsRequest())
        first_may_end.set()
        await asyncio.wait_for(running, timeout=1.0)

        assert isinstance(result, CancelAllRunsResultSuccess)
        turned_away = await asyncio.wait_for(waiting, timeout=1.0)
        assert "did not start: it was cancelled" in str(turned_away.result_details)
        assert executed == []


class TestSpendNamesTheRunInProgress:
    """Spend tags, the start event and the end events all name the run its request started."""

    @pytest.mark.asyncio
    async def test_the_run_in_progress_is_the_one_its_request_names(self, engine: Engine) -> None:
        flow_manager = engine.flow_manager
        seen: list[str | None] = []

        async def body() -> Any:
            seen.append(flow_manager.current_run_id)
            return _ran("run")

        result = await flow_manager.start_run(_Spec("flow", body))

        assert isinstance(result, StartFlowResultSuccess)

        assert seen == [result.run_id]
        assert flow_manager.current_run_id is None, "Spend made between runs belongs to no run."

    @pytest.mark.asyncio
    async def test_a_run_announces_its_id_when_it_starts(self, engine: Engine) -> None:
        flow_manager = engine.flow_manager
        engine.event_manager.put_event = MagicMock()  # type: ignore[method-assign]

        result = await flow_manager.start_run(_Spec("flow", lambda: _async(_ran("run"))))

        assert isinstance(result, StartFlowResultSuccess)

        started = _payloads(engine, FlowRunStartedEvent)
        assert [(event.run_id, event.flow_name) for event in started] == [(result.run_id, "flow")]

    @pytest.mark.asyncio
    async def test_a_node_group_run_for_another_engine_counts_toward_that_engine_s_run(self, engine: Engine) -> None:
        """It spends under the parent's id, and leaves the run's start and end events to the parent engine."""
        flow_manager = engine.flow_manager
        flow_manager.parent_run_id = "parent-run"
        engine.event_manager.put_event = MagicMock()  # type: ignore[method-assign]
        seen: list[str | None] = []

        async def body() -> Any:
            seen.append(flow_manager.current_run_id)
            return _ran("run")

        try:
            await flow_manager.start_run(_Spec("flow", body))
        finally:
            flow_manager.parent_run_id = None

        assert seen == ["parent-run"]
        assert _payloads(engine, FlowRunStartedEvent) == []
        assert _payloads(engine, FlowRunEndedEvent) == []


class TestEachRunEndsOnce:
    """Every run that announced a start announces one end, under the same id, saying how it ended."""

    @pytest.mark.asyncio
    async def test_a_run_ends_once_with_its_state(self, engine: Engine) -> None:
        flow_manager = engine.flow_manager
        engine.event_manager.put_event = MagicMock()  # type: ignore[method-assign]

        result = await flow_manager.start_run(_Spec("flow", lambda: _async(_ran("run"))))
        assert isinstance(result, StartFlowResultSuccess)

        ended = [(event.run_id, event.state) for event in _payloads(engine, FlowRunEndedEvent)]
        assert ended == [(result.run_id, "finished")]

    @pytest.mark.asyncio
    async def test_a_run_whose_request_fails_ends_failed(self, engine: Engine) -> None:
        """Covers a flow that fails before it starts: the run's request fails, and the run still ends."""
        flow_manager = engine.flow_manager
        engine.event_manager.put_event = MagicMock()  # type: ignore[method-assign]

        async def body() -> Any:
            return StartFlowResultFailure(validation_exceptions=[], result_details="nothing to run")

        result = await flow_manager.start_run(_Spec("flow", body))
        assert isinstance(result, StartFlowResultFailure)

        ended = [(event.run_id, event.state) for event in _payloads(engine, FlowRunEndedEvent)]
        assert ended == [(result.run_id, "failed")]

    @pytest.mark.asyncio
    async def test_a_run_that_raises_ends_failed(self, engine: Engine) -> None:
        flow_manager = engine.flow_manager
        engine.event_manager.put_event = MagicMock()  # type: ignore[method-assign]

        async def body() -> Any:
            msg = "boom"
            raise RuntimeError(msg)

        run = flow_manager.submit_run(_Spec("flow", body))
        with pytest.raises(RuntimeError, match="boom"):
            await asyncio.wait_for(run.outcome, timeout=1.0)

        ended = [(event.run_id, event.state) for event in _payloads(engine, FlowRunEndedEvent)]
        assert ended == [(run.run_id, "failed")]

    @pytest.mark.asyncio
    async def test_a_run_cancelled_before_it_starts_its_flow_ends_cancelled(self, engine: Engine) -> None:
        """It got the machine and announced a start, then was cancelled before executing."""
        flow_manager = engine.flow_manager
        engine.event_manager.put_event = MagicMock()  # type: ignore[method-assign]
        first_may_end = asyncio.Event()

        async def first() -> Any:
            await first_may_end.wait()
            return _ran("first")

        running = asyncio.ensure_future(flow_manager.start_run(_Spec("first", first)))
        await asyncio.sleep(0)
        run = flow_manager._run_in_progress
        assert run is not None
        await flow_manager.cancel_run(run.run_id)
        first_may_end.set()
        await asyncio.wait_for(running, timeout=1.0)

        ended = [(event.run_id, event.state) for event in _payloads(engine, FlowRunEndedEvent)]
        assert ended == [(run.run_id, "cancelled")]

    @pytest.mark.asyncio
    async def test_a_waiting_run_that_is_turned_away_neither_starts_nor_ends(self, engine: Engine) -> None:
        flow_manager = engine.flow_manager
        engine.event_manager.put_event = MagicMock()  # type: ignore[method-assign]
        first_may_end = asyncio.Event()

        async def first() -> Any:
            await first_may_end.wait()
            return _ran("first")

        running = asyncio.ensure_future(flow_manager.start_run(_Spec("first", first)))
        await asyncio.sleep(0)
        waiting = flow_manager.submit_run(_Spec("second", lambda: _async(_ran("second"))))
        await flow_manager.cancel_run(waiting.run_id)
        first_may_end.set()
        await asyncio.wait_for(running, timeout=1.0)

        assert waiting.run_id not in [event.run_id for event in _payloads(engine, FlowRunStartedEvent)]
        assert waiting.run_id not in [event.run_id for event in _payloads(engine, FlowRunEndedEvent)]

    @pytest.mark.asyncio
    async def test_an_idle_engine_has_nothing_to_cancel(self, engine: Engine) -> None:
        flow_manager = engine.flow_manager

        with patch.object(flow_manager, "cancel_flow_run") as cancel_flow_run:
            await flow_manager.cancel_all_runs()

        cancel_flow_run.assert_not_called()


def _payloads(engine: Engine, payload_type: type) -> list[Any]:
    calls = engine.event_manager.put_event.call_args_list  # type: ignore[attr-defined]
    payloads = [getattr(getattr(call.args[0], "wrapped_event", None), "payload", None) for call in calls]
    return [payload for payload in payloads if isinstance(payload, payload_type)]


async def _async(value: Any) -> Any:
    return value


class TestCancelAllRunsWhenIdle:
    @pytest.mark.asyncio
    async def test_an_idle_engine_has_nothing_to_cancel(self, engine: Engine) -> None:
        flow_manager = engine.flow_manager

        with patch.object(flow_manager, "cancel_flow_run") as cancel_flow_run:
            await flow_manager.cancel_all_runs()

        cancel_flow_run.assert_not_called()


class TestRunsNameTheirRequest:
    @pytest.mark.asyncio
    async def test_a_waiting_run_carries_the_id_of_the_request_that_submitted_it(self, engine: Engine) -> None:
        """A client matches its own waiting run by the id it gave its request, even when another queued the same flow."""
        flow_manager = engine.flow_manager
        first_may_end = asyncio.Event()

        async def first() -> Any:
            await first_may_end.wait()
            return _ran("first")

        async def second() -> Any:
            return _ran("second")

        running = asyncio.ensure_future(flow_manager.start_run(_Spec("flow", first)))
        await asyncio.sleep(0)
        with patch("griptape_nodes.retained_mode.managers.flow_manager.current_request_id", return_value="client-7"):
            waiting_run = flow_manager.submit_run(_Spec("flow", second))

        listed = flow_manager.on_list_runs_request(ListRunsRequest())

        assert isinstance(listed, ListRunsResultSuccess)
        assert [run.request_id for run in listed.runs if run.run_id == waiting_run.run_id] == ["client-7"]
        first_may_end.set()
        await asyncio.wait_for(asyncio.gather(running, waiting_run.outcome), timeout=1.0)
