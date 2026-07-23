"""Tests for FlowManager.on_extract_flow_commands_from_image_metadata."""

import base64
import itertools
import pickle
import tempfile
from collections.abc import Generator
from pathlib import Path

import pytest
from PIL import Image
from PIL.PngImagePlugin import PngInfo

from griptape_nodes.exe_types.connections import Connections
from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
from griptape_nodes.exe_types.node_types import BaseNode, ControlNode, DataNode, StartNode
from griptape_nodes.machines.dag_builder import DagNodeCategories
from griptape_nodes.retained_mode.events.flow_events import (
    CreateFlowRequest,
    CreateFlowResultSuccess,
    ExtractFlowCommandsFromImageMetadataRequest,
    ExtractFlowCommandsFromImageMetadataResultFailure,
    ExtractFlowCommandsFromImageMetadataResultSuccess,
    SerializeFlowToCommandsRequest,
    SerializeFlowToCommandsResultSuccess,
)
from griptape_nodes.retained_mode.events.object_events import ClearAllObjectStateRequest
from griptape_nodes.retained_mode.file_metadata.workflow_metadata import FLOW_COMMANDS_KEY
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes


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


@pytest.fixture
def image_with_flow_commands() -> Generator[str, None, None]:
    """A PNG whose FLOW_COMMANDS_KEY payload is a valid pickle."""
    payload = base64.b64encode(pickle.dumps({"sentinel": "flow"})).decode("ascii")
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        info = PngInfo()
        info.add_text(FLOW_COMMANDS_KEY, payload)
        Image.new("RGB", (4, 4), color="blue").save(f, format="PNG", pnginfo=info)
        path = f.name
    try:
        yield path
    finally:
        Path(path).unlink(missing_ok=True)


class TestExtractFlowCommandsFromImageMetadata:
    """Covers the non-error success paths for images that carry no workflow payload."""

    def test_returns_success_with_none_when_image_has_no_metadata(
        self, griptape_nodes: GriptapeNodes, image_without_metadata: str
    ) -> None:
        flow_manager = griptape_nodes.FlowManager()
        request = ExtractFlowCommandsFromImageMetadataRequest(file_url_or_path=image_without_metadata)

        result = flow_manager.on_extract_flow_commands_from_image_metadata(request)

        assert isinstance(result, ExtractFlowCommandsFromImageMetadataResultSuccess)
        assert result.serialized_flow_commands is None
        assert result.altered_workflow_state is False

    def test_returns_success_with_none_when_flow_commands_key_missing(
        self, griptape_nodes: GriptapeNodes, image_with_unrelated_metadata: str
    ) -> None:
        flow_manager = griptape_nodes.FlowManager()
        request = ExtractFlowCommandsFromImageMetadataRequest(file_url_or_path=image_with_unrelated_metadata)

        result = flow_manager.on_extract_flow_commands_from_image_metadata(request)

        assert isinstance(result, ExtractFlowCommandsFromImageMetadataResultSuccess)
        assert result.serialized_flow_commands is None
        assert result.altered_workflow_state is False

    def test_returns_failure_when_file_missing(self, griptape_nodes: GriptapeNodes) -> None:
        flow_manager = griptape_nodes.FlowManager()
        request = ExtractFlowCommandsFromImageMetadataRequest(file_url_or_path="/does/not/exist.png")

        result = flow_manager.on_extract_flow_commands_from_image_metadata(request)

        assert isinstance(result, ExtractFlowCommandsFromImageMetadataResultFailure)

    def test_returns_commands_when_flow_commands_key_present(
        self, griptape_nodes: GriptapeNodes, image_with_flow_commands: str
    ) -> None:
        flow_manager = griptape_nodes.FlowManager()
        request = ExtractFlowCommandsFromImageMetadataRequest(file_url_or_path=image_with_flow_commands)

        result = flow_manager.on_extract_flow_commands_from_image_metadata(request)

        assert isinstance(result, ExtractFlowCommandsFromImageMetadataResultSuccess)
        assert result.serialized_flow_commands == {"sentinel": "flow"}
        assert result.altered_workflow_state is False


class TestAwaitFlowCompletion:
    """Tests for FlowManager._await_flow_completion (the wait_for_completion helper)."""

    @pytest.mark.asyncio
    async def test_returns_none_when_flow_finishes_cleanly(self, griptape_nodes: GriptapeNodes) -> None:
        from unittest.mock import patch

        flow_manager = griptape_nodes.FlowManager()

        # No control flow machine -> no error to report; simulate "flow finished" with a single False.
        with patch.object(flow_manager, "check_for_existing_running_flow", return_value=False):
            result = await flow_manager._await_flow_completion(timeout_ms=None)

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_timeout_string_when_timeout_exceeded(self, griptape_nodes: GriptapeNodes) -> None:
        from unittest.mock import patch

        flow_manager = griptape_nodes.FlowManager()

        # Keep reporting "still running" so the timeout path fires quickly.
        with patch.object(flow_manager, "check_for_existing_running_flow", return_value=True):
            result = await flow_manager._await_flow_completion(timeout_ms=10)

        assert result is not None
        assert "Timed out" in result

    @pytest.mark.asyncio
    async def test_returns_error_message_when_resolution_machine_errored(self, griptape_nodes: GriptapeNodes) -> None:
        from unittest.mock import MagicMock, patch

        flow_manager = griptape_nodes.FlowManager()

        fake_resolution_machine = MagicMock()
        fake_resolution_machine.is_errored.return_value = True
        fake_resolution_machine.get_error_message.return_value = "boom"
        fake_machine = MagicMock()
        fake_machine.resolution_machine = fake_resolution_machine

        with (
            patch.object(flow_manager, "check_for_existing_running_flow", return_value=False),
            patch.object(flow_manager, "_global_control_flow_machine", fake_machine),
        ):
            result = await flow_manager._await_flow_completion(timeout_ms=None)

        assert result == "boom"


class TestStartFlowRequestDefaultsToCurrentContext:
    """Tests for StartFlowRequest / StartFlowFromNodeRequest current-context fallback."""

    @pytest.mark.asyncio
    async def test_start_flow_fails_cleanly_when_no_flow_and_no_context(self, griptape_nodes: GriptapeNodes) -> None:
        from griptape_nodes.retained_mode.events.execution_events import (
            StartFlowRequest,
            StartFlowResultFailure,
        )

        flow_manager = griptape_nodes.FlowManager()
        griptape_nodes.handle_request(
            __import__(
                "griptape_nodes.retained_mode.events.object_events",
                fromlist=["ClearAllObjectStateRequest"],
            ).ClearAllObjectStateRequest(i_know_what_im_doing=True)
        )

        assert not griptape_nodes.ContextManager().has_current_flow()

        result = await flow_manager.on_start_flow_request(StartFlowRequest())

        assert isinstance(result, StartFlowResultFailure)
        # Message should now name a concrete remediation, not the old generic one.
        assert "Current Context" in str(result.result_details)

    @pytest.mark.asyncio
    async def test_start_flow_uses_current_context_flow_when_name_omitted(self, griptape_nodes: GriptapeNodes) -> None:
        from unittest.mock import patch

        from griptape_nodes.retained_mode.events.execution_events import (
            StartFlowRequest,
            StartFlowResultFailure,
        )
        from griptape_nodes.retained_mode.events.flow_events import CreateFlowRequest, CreateFlowResultSuccess
        from griptape_nodes.retained_mode.events.object_events import ClearAllObjectStateRequest

        flow_manager = griptape_nodes.FlowManager()
        griptape_nodes.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
        # Bootstrap manually via push_workflow + CreateFlowRequest so this test does not
        # depend on any sibling MCP-bootstrap PR landing first.
        griptape_nodes.ContextManager().push_workflow("wf")
        create_flow_result = griptape_nodes.handle_request(
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

        griptape_nodes.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))

    @pytest.mark.asyncio
    async def test_start_flow_from_node_fails_cleanly_when_no_node_and_no_context(
        self, griptape_nodes: GriptapeNodes
    ) -> None:
        from griptape_nodes.retained_mode.events.execution_events import (
            StartFlowFromNodeRequest,
            StartFlowFromNodeResultFailure,
        )
        from griptape_nodes.retained_mode.events.object_events import ClearAllObjectStateRequest

        flow_manager = griptape_nodes.FlowManager()
        griptape_nodes.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))

        assert not griptape_nodes.ContextManager().has_current_node()

        result = await flow_manager.on_start_flow_from_node_request(StartFlowFromNodeRequest())

        assert isinstance(result, StartFlowFromNodeResultFailure)
        assert "Current Context" in str(result.result_details)

    @pytest.mark.asyncio
    async def test_start_flow_from_node_uses_current_context_node_and_derives_parent_flow(
        self, griptape_nodes: GriptapeNodes
    ) -> None:
        from unittest.mock import MagicMock, patch

        from griptape_nodes.exe_types.node_types import BaseNode
        from griptape_nodes.retained_mode.events.execution_events import (
            StartFlowFromNodeRequest,
            StartFlowFromNodeResultFailure,
        )
        from griptape_nodes.retained_mode.events.object_events import ClearAllObjectStateRequest

        flow_manager = griptape_nodes.FlowManager()
        griptape_nodes.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))

        # Stand in a current node so the handler can fall back to it. The node itself only
        # needs to expose `.name`; we short-circuit object lookup below.
        fake_node = MagicMock(spec=BaseNode)
        fake_node.name = "node_in_ctx"
        ctx = griptape_nodes.ContextManager()
        with (
            patch.object(ctx, "has_current_node", return_value=True),
            patch.object(ctx, "get_current_node", return_value=fake_node),
            patch.object(
                griptape_nodes.ObjectManager(),
                "attempt_get_object_by_name_as_type",
                return_value=fake_node,
            ),
            patch.object(
                griptape_nodes.NodeManager(),
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

        griptape_nodes.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))


class TestStartFlowCancelsOnWaitTimeout:
    """Tests for the wait_for_completion cancel-on-timeout cleanup in on_start_flow_request."""

    @pytest.mark.asyncio
    async def test_cancels_running_flow_when_wait_for_completion_times_out(self, griptape_nodes: GriptapeNodes) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        from griptape_nodes.retained_mode.events.execution_events import (
            StartFlowRequest,
            StartFlowResultFailure,
        )
        from griptape_nodes.retained_mode.events.validation_events import (
            ValidateFlowDependenciesResultSuccess,
        )

        flow_manager = griptape_nodes.FlowManager()

        fake_flow = MagicMock()
        fake_flow.name = "timeout_flow"
        validate_success = ValidateFlowDependenciesResultSuccess(
            validation_succeeded=True, exceptions=[], result_details="validated"
        )
        cancel_mock = AsyncMock()

        # check_for_existing_running_flow is consulted twice along the wait path:
        # once before kicking off (must be False), and once after the timeout to decide
        # whether to cancel (must be True since the flow is still churning).
        running_flow_states = iter([False, True])

        with (
            patch.object(flow_manager, "get_flow_by_name", return_value=fake_flow),
            patch.object(
                flow_manager,
                "check_for_existing_running_flow",
                side_effect=lambda: next(running_flow_states),
            ),
            patch.object(
                flow_manager,
                "on_validate_flow_dependencies_request",
                AsyncMock(return_value=validate_success),
            ),
            patch.object(flow_manager, "start_flow", AsyncMock()),
            patch.object(flow_manager, "_global_control_flow_machine", None),
            patch.object(
                flow_manager,
                "_await_flow_completion",
                AsyncMock(return_value="Timed out waiting for flow completion after 10 ms."),
            ),
            patch.object(flow_manager, "cancel_flow_run", cancel_mock),
        ):
            result = await flow_manager.on_start_flow_request(
                StartFlowRequest(flow_name="timeout_flow", wait_for_completion=True, completion_timeout_ms=10)
            )

        assert isinstance(result, StartFlowResultFailure)
        assert "did not complete cleanly" in str(result.result_details)
        cancel_mock.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_does_not_cancel_when_flow_already_finished_with_error(self, griptape_nodes: GriptapeNodes) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        from griptape_nodes.retained_mode.events.execution_events import (
            StartFlowRequest,
            StartFlowResultFailure,
        )
        from griptape_nodes.retained_mode.events.validation_events import (
            ValidateFlowDependenciesResultSuccess,
        )

        flow_manager = griptape_nodes.FlowManager()

        fake_flow = MagicMock()
        fake_flow.name = "errored_flow"
        validate_success = ValidateFlowDependenciesResultSuccess(
            validation_succeeded=True, exceptions=[], result_details="validated"
        )
        cancel_mock = AsyncMock()

        # First call (kickoff gate) returns False; second call (post-wait cancel gate) also
        # returns False because the flow already finished with an error.
        with (
            patch.object(flow_manager, "get_flow_by_name", return_value=fake_flow),
            patch.object(flow_manager, "check_for_existing_running_flow", return_value=False),
            patch.object(
                flow_manager,
                "on_validate_flow_dependencies_request",
                AsyncMock(return_value=validate_success),
            ),
            patch.object(flow_manager, "start_flow", AsyncMock()),
            patch.object(flow_manager, "_global_control_flow_machine", None),
            patch.object(
                flow_manager,
                "_await_flow_completion",
                AsyncMock(return_value="boom"),
            ),
            patch.object(flow_manager, "cancel_flow_run", cancel_mock),
        ):
            result = await flow_manager.on_start_flow_request(
                StartFlowRequest(flow_name="errored_flow", wait_for_completion=True)
            )

        assert isinstance(result, StartFlowResultFailure)
        cancel_mock.assert_not_called()


class TestListNodesInFlowRequest:
    """Tests for FlowManager.on_list_nodes_in_flow_request node_types filter."""

    def _make_flow(self, nodes: dict) -> object:
        from unittest.mock import MagicMock

        fake_flow = MagicMock()
        fake_flow.name = "test_flow"
        fake_flow.nodes = nodes
        return fake_flow

    def _run_request(self, griptape_nodes: GriptapeNodes, flow: object, request: object) -> object:
        from unittest.mock import patch

        from griptape_nodes.retained_mode.managers.flow_manager import ControlFlow

        flow_manager = griptape_nodes.FlowManager()
        with patch.object(
            griptape_nodes.ObjectManager(),
            "attempt_get_object_by_name_as_type",
            side_effect=lambda _name, typ: flow if typ is ControlFlow else None,
        ):
            return flow_manager.on_list_nodes_in_flow_request(request)  # type: ignore[arg-type]

    def test_no_filter_returns_all_nodes(self, griptape_nodes: GriptapeNodes) -> None:
        from griptape_nodes.retained_mode.events.flow_events import ListNodesInFlowRequest, ListNodesInFlowResultSuccess

        class NoteNode:
            pass

        flow = self._make_flow({"Note_1": NoteNode(), "Note_2": NoteNode()})
        result = self._run_request(griptape_nodes, flow, ListNodesInFlowRequest(flow_name="test_flow"))

        assert isinstance(result, ListNodesInFlowResultSuccess)
        assert set(result.node_names) == {"Note_1", "Note_2"}

    def test_filter_by_matching_class_name_returns_subset(self, griptape_nodes: GriptapeNodes) -> None:
        from griptape_nodes.retained_mode.events.flow_events import ListNodesInFlowRequest, ListNodesInFlowResultSuccess

        class NoteNode:
            pass

        class AgentNode:
            pass

        flow = self._make_flow({"note_1": NoteNode(), "agent_1": AgentNode(), "note_2": NoteNode()})
        result = self._run_request(
            griptape_nodes, flow, ListNodesInFlowRequest(flow_name="test_flow", node_types=["NoteNode"])
        )

        assert isinstance(result, ListNodesInFlowResultSuccess)
        assert set(result.node_names) == {"note_1", "note_2"}

    def test_filter_by_nonexistent_class_name_returns_empty(self, griptape_nodes: GriptapeNodes) -> None:
        from griptape_nodes.retained_mode.events.flow_events import ListNodesInFlowRequest, ListNodesInFlowResultSuccess

        class NoteNode:
            pass

        flow = self._make_flow({"note_1": NoteNode()})
        result = self._run_request(
            griptape_nodes, flow, ListNodesInFlowRequest(flow_name="test_flow", node_types=["NonExistentClass"])
        )

        assert isinstance(result, ListNodesInFlowResultSuccess)
        assert result.node_names == []

    def test_filter_with_empty_list_returns_empty(self, griptape_nodes: GriptapeNodes) -> None:
        from griptape_nodes.retained_mode.events.flow_events import ListNodesInFlowRequest, ListNodesInFlowResultSuccess

        class NoteNode:
            pass

        flow = self._make_flow({"note_1": NoteNode()})
        result = self._run_request(griptape_nodes, flow, ListNodesInFlowRequest(flow_name="test_flow", node_types=[]))

        assert isinstance(result, ListNodesInFlowResultSuccess)
        assert result.node_names == []

    def test_filter_with_multiple_types_returns_union(self, griptape_nodes: GriptapeNodes) -> None:
        from griptape_nodes.retained_mode.events.flow_events import ListNodesInFlowRequest, ListNodesInFlowResultSuccess

        class NoteNode:
            pass

        class AgentNode:
            pass

        class OtherNode:
            pass

        flow = self._make_flow({"note_1": NoteNode(), "agent_1": AgentNode(), "other_1": OtherNode()})
        result = self._run_request(
            griptape_nodes,
            flow,
            ListNodesInFlowRequest(flow_name="test_flow", node_types=["NoteNode", "AgentNode"]),
        )

        assert isinstance(result, ListNodesInFlowResultSuccess)
        assert set(result.node_names) == {"note_1", "agent_1"}


class TestAutoLayoutFlowRequest:
    """Tests for FlowManager.on_auto_layout_flow_request."""

    def _cleanup(self, griptape_nodes: GriptapeNodes) -> None:
        from griptape_nodes.retained_mode.events.object_events import ClearAllObjectStateRequest

        griptape_nodes.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))

    def _bootstrap_workflow_and_flow(self, griptape_nodes: GriptapeNodes, workflow: str, flow: str) -> None:
        """Push a workflow + create a flow without depending on any sibling bootstrap PR."""
        from griptape_nodes.retained_mode.events.flow_events import CreateFlowRequest, CreateFlowResultSuccess

        self._cleanup(griptape_nodes)
        griptape_nodes.ContextManager().push_workflow(workflow)
        result = griptape_nodes.handle_request(
            CreateFlowRequest(parent_flow_name=None, flow_name=flow, set_as_new_context=True)
        )
        assert isinstance(result, CreateFlowResultSuccess)

    def _bootstrap_graph(self, griptape_nodes: GriptapeNodes) -> str:
        """Create a small Workflow + Flow + 3 chained Note nodes (A -> B -> C) for layout tests.

        Uses `Note` from the registered Griptape Nodes Library because it has data params that
        can actually be connected end to end without triggering LLM calls or external deps.
        """
        from griptape_nodes.retained_mode.events.connection_events import CreateConnectionRequest
        from griptape_nodes.retained_mode.events.node_events import CreateNodeRequest, CreateNodeResultSuccess

        self._bootstrap_workflow_and_flow(griptape_nodes, workflow="layout_wf", flow="layout_flow")

        names = []
        for desired in ("A", "B", "C"):
            result = griptape_nodes.handle_request(CreateNodeRequest(node_type="Note", node_name=desired))
            assert isinstance(result, CreateNodeResultSuccess)
            names.append(result.node_name)

        # Wire A -> B -> C on the Note node's text parameter.
        for source, target in itertools.pairwise(names):
            conn_result = griptape_nodes.handle_request(
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
    async def test_fails_cleanly_when_no_flow_in_context_and_no_name(self, griptape_nodes: GriptapeNodes) -> None:
        from griptape_nodes.retained_mode.events.flow_events import (
            AutoLayoutFlowRequest,
            AutoLayoutFlowResultFailure,
        )

        self._cleanup(griptape_nodes)
        flow_manager = griptape_nodes.FlowManager()

        result = await flow_manager.on_auto_layout_flow_request(AutoLayoutFlowRequest())

        assert isinstance(result, AutoLayoutFlowResultFailure)

    @pytest.mark.asyncio
    async def test_lays_out_linear_chain_into_columns(self, griptape_nodes: GriptapeNodes) -> None:
        from griptape_nodes.retained_mode.events.flow_events import (
            AutoLayoutFlowRequest,
            AutoLayoutFlowResultSuccess,
        )

        flow_name = self._bootstrap_graph(griptape_nodes)
        flow_manager = griptape_nodes.FlowManager()

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

        self._cleanup(griptape_nodes)

    @pytest.mark.asyncio
    async def test_empty_flow_is_handled_gracefully(self, griptape_nodes: GriptapeNodes) -> None:
        from griptape_nodes.retained_mode.events.flow_events import (
            AutoLayoutFlowRequest,
            AutoLayoutFlowResultSuccess,
        )

        self._bootstrap_workflow_and_flow(griptape_nodes, workflow="empty_wf", flow="empty_flow")

        flow_manager = griptape_nodes.FlowManager()
        result = await flow_manager.on_auto_layout_flow_request(AutoLayoutFlowRequest(flow_name="empty_flow"))

        assert isinstance(result, AutoLayoutFlowResultSuccess)
        assert result.positioned_nodes == []

        self._cleanup(griptape_nodes)


class TestExcludeSubflowGroupChildren:
    """Tests for FlowManager.exclude_subflow_group_children.

    This scope/ownership filter drops nodes owned by a SubflowNodeGroup so they are not seeded
    directly into a DAG (they run inside their group's own subflow). The top-level queue applies
    it to keep group members out of the cross-flow run; the isolated-subflow path deliberately
    does NOT, because within a group's own subflow those members are exactly what must resolve.
    """

    def test_drops_only_subflow_group_children(self, griptape_nodes: GriptapeNodes) -> None:
        from unittest.mock import MagicMock

        from griptape_nodes.exe_types.node_groups.subflow_node_group import SubflowNodeGroup
        from griptape_nodes.exe_types.node_types import BaseNode

        flow_manager = griptape_nodes.FlowManager()

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

    def test_empty_input_returns_empty(self, griptape_nodes: GriptapeNodes) -> None:
        flow_manager = griptape_nodes.FlowManager()

        assert flow_manager.exclude_subflow_group_children([]) == []


class TestClassifyNodesForDag:
    """Tests for FlowManager.classify_nodes_for_dag.

    The classifier is scope-agnostic: it buckets an arbitrary list of nodes into start /
    control-entry / data-sink roles based purely on the connection graph. It backs both the
    top-level queue and isolated subflow seeding, so these tests lock in each branch. Real node
    subclasses and a real Connections object are used so the Parameter/Connection semantics match
    production; only get_connections is patched to hand the classifier the crafted graph.
    """

    @staticmethod
    def _classify(griptape_nodes: GriptapeNodes, nodes: list, connections: Connections) -> DagNodeCategories:
        from unittest.mock import patch

        flow_manager = griptape_nodes.FlowManager()
        with patch.object(flow_manager, "get_connections", return_value=connections):
            return flow_manager.classify_nodes_for_dag(nodes)

    def test_start_node_is_a_start_node(self, griptape_nodes: GriptapeNodes) -> None:
        start = _ClassifyStartNode("Start")
        data = _ClassifyDataNode("Data")
        connections = Connections()
        connections.add_connection(start, _param(start, "value"), data, _param(data, "value"))

        categories = self._classify(griptape_nodes, [start, data], connections)

        assert [node.name for node in categories.start_nodes] == ["Start"]
        # Data has an incoming data connection and no outgoing one, so it is a terminal sink.
        assert [node.name for node in categories.data_sink_nodes] == ["Data"]
        assert categories.control_nodes == []

    def test_data_node_with_external_outgoing_is_not_a_sink(self, griptape_nodes: GriptapeNodes) -> None:
        upstream = _ClassifyDataNode("Upstream")
        downstream = _ClassifyDataNode("Downstream")
        connections = Connections()
        connections.add_connection(upstream, _param(upstream, "value"), downstream, _param(downstream, "value"))

        categories = self._classify(griptape_nodes, [upstream, downstream], connections)

        # Only the leaf (Downstream) is a sink; Upstream feeds a downstream node so it is skipped.
        assert [node.name for node in categories.data_sink_nodes] == ["Downstream"]
        assert categories.start_nodes == []
        assert categories.control_nodes == []

    def test_control_chain_first_node_is_control_entry(self, griptape_nodes: GriptapeNodes) -> None:
        first = _ClassifyControlNode("First")
        second = _ClassifyControlNode("Second")
        connections = Connections()
        connections.add_connection(first, _param(first, "exec_out"), second, _param(second, "exec_in"))

        categories = self._classify(griptape_nodes, [first, second], connections)

        # First drives the control flow; Second has an external incoming control edge so the
        # forward control walk reaches it and it is not seeded as an entry node.
        assert [node.name for node in categories.control_nodes] == ["First"]
        assert categories.start_nodes == []
        assert categories.data_sink_nodes == []

    def test_control_node_without_control_connections_is_treated_as_data_sink(
        self, griptape_nodes: GriptapeNodes
    ) -> None:
        lone = _ClassifyControlNode("Lone")
        connections = Connections()

        categories = self._classify(griptape_nodes, [lone], connections)

        # Control params exist but are unused, so the node is a plain data node with no outgoing
        # connection, i.e. a terminal sink.
        assert [node.name for node in categories.data_sink_nodes] == ["Lone"]
        assert categories.control_nodes == []
        assert categories.start_nodes == []

    def test_internal_node_group_outgoing_does_not_disqualify_sink(self, griptape_nodes: GriptapeNodes) -> None:
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

        categories = self._classify(griptape_nodes, [source, target], connections)

        # Internal NodeGroup connections do not count as external outgoing, so both nodes remain
        # terminal sinks.
        assert sorted(node.name for node in categories.data_sink_nodes) == ["Source", "Target"]

    def test_empty_scope_returns_empty_categories(self, griptape_nodes: GriptapeNodes) -> None:
        categories = self._classify(griptape_nodes, [], Connections())

        assert categories.start_nodes == []
        assert categories.control_nodes == []
        assert categories.data_sink_nodes == []


@pytest.fixture
def clean_object_state(griptape_nodes: GriptapeNodes) -> Generator[None, None, None]:
    """Clear all object state around a test so leftover flows never bleed across tests.

    Yield-based so the teardown clear runs even when the test body fails.
    """
    griptape_nodes.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
    try:
        yield
    finally:
        griptape_nodes.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))


class TestSerializeFlowSkipsTransientChildFlows:
    """A child flow flagged ``transient`` in its metadata is omitted from serialization.

    Transient flows are runtime-only artifacts (e.g. a subflow a node imports at execution time);
    they must not be baked into the saved workflow, so ``on_serialize_flow_to_commands`` skips them
    while still serializing ordinary child flows.
    """

    @pytest.mark.usefixtures("clean_object_state")
    def test_transient_child_flow_is_not_serialized(self, griptape_nodes: GriptapeNodes) -> None:
        griptape_nodes.ContextManager().push_workflow("transient_wf")

        parent = griptape_nodes.handle_request(
            CreateFlowRequest(parent_flow_name=None, flow_name="parent", set_as_new_context=True)
        )
        assert isinstance(parent, CreateFlowResultSuccess)
        keep = griptape_nodes.handle_request(
            CreateFlowRequest(parent_flow_name=parent.flow_name, flow_name="child_keep", set_as_new_context=False)
        )
        assert isinstance(keep, CreateFlowResultSuccess)
        transient = griptape_nodes.handle_request(
            CreateFlowRequest(parent_flow_name=parent.flow_name, flow_name="child_transient", set_as_new_context=False)
        )
        assert isinstance(transient, CreateFlowResultSuccess)

        flow_manager = griptape_nodes.FlowManager()
        flow_manager.get_flow_by_name(transient.flow_name).metadata["transient"] = True

        result = flow_manager.on_serialize_flow_to_commands(
            SerializeFlowToCommandsRequest(flow_name=parent.flow_name, include_create_flow_command=True)
        )
        assert isinstance(result, SerializeFlowToCommandsResultSuccess)

        serialized_child_flows = {sub.flow_name for sub in result.serialized_flow_commands.sub_flows_commands}
        assert keep.flow_name in serialized_child_flows
        assert transient.flow_name not in serialized_child_flows
