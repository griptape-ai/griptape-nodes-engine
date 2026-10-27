"""A worker loads a copy of the node it is sent the way a saved workflow loads, runs it, and discards it.

The orchestrator holds the real node. When a node of a library that runs in a worker executes, the
orchestrator fills the ExecuteNodeRequest with the node's structure (the commands a save writes) and its
stored values, and the worker creates its copy from them, inside a flow of its own, answering every request
the load makes locally. These tests hold that contract from both ends, with the worker played by a second
engine in this process (``tests.simulated_worker``):

- the copy has the shape and values of the node it was copied from
- nothing the load sends reaches the orchestrator, even while another node runs in the same worker
- lists reach the node as the orchestrator holds them, however they were filled
- discarding the copy keeps what it cached, and leaves nothing of it behind, however the run ended
- requests a worker must not act on are refused before anything is created
- the strict-mode rules this contract relies on warn in-process and fail a run in a worker
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

from griptape_nodes.exe_types.core_types import Parameter, ParameterList
from griptape_nodes.exe_types.local_objects import is_reference
from griptape_nodes.node_library.library_registry import LibraryRegistry
from griptape_nodes.retained_mode.engine import current_engine
from griptape_nodes.retained_mode.events.base_events import ResultDetails, StrictModeViolationDetail
from griptape_nodes.retained_mode.events.connection_events import (
    CreateConnectionRequest,
    CreateConnectionResultSuccess,
    DeleteConnectionRequest,
    ListConnectionsForNodeRequest,
)
from griptape_nodes.retained_mode.events.execution_events import (
    ExecuteNodeRequest,
    ExecuteNodeResultFailure,
    ExecuteNodeResultSuccess,
    StartFlowRequest,
)
from griptape_nodes.retained_mode.events.flow_events import CreateFlowRequest, CreateFlowResultSuccess
from griptape_nodes.retained_mode.events.library_events import (
    RegisterLibraryFromFileRequest,
    RegisterLibraryFromFileResultSuccess,
)
from griptape_nodes.retained_mode.events.node_events import (
    CreateNodeRequest,
    CreateNodeResultSuccess,
    DeserializeSelectedNodesFromCommandsRequest,
    DeserializeSelectedNodesFromCommandsResultSuccess,
    SerializeSelectedNodesToCommandsRequest,
    SerializeSelectedNodesToCommandsResultSuccess,
)
from griptape_nodes.retained_mode.events.object_events import ClearAllObjectStateRequest
from griptape_nodes.retained_mode.events.parameter_events import (
    AddParameterToNodeRequest,
    AddParameterToNodeResultSuccess,
    AlterParameterDetailsRequest,
    RemoveParameterFromNodeRequest,
    SetParameterValueRequest,
)
from griptape_nodes.retained_mode.events.workflow_events import (
    RunWorkflowFromRegistryRequest,
    RunWorkflowFromRegistryResultSuccess,
    SaveWorkflowRequest,
    SaveWorkflowResultSuccess,
)
from griptape_nodes.testing.libraries import WORKER_BEHAVIOR_LIBRARY_NAME, materialize_worker_behavior_library
from tests.simulated_worker import SimulatedWorker, simulated_worker

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from griptape_nodes.exe_types.node_types import BaseNode
    from griptape_nodes.retained_mode.events.base_events import ResultPayload

_CANVAS_FLOW_NAME = "worker_copy_canvas"
# The worker's own top-level flow, which outlives the copies it holds.
_WORKER_COPIES_FLOW_NAME = "__worker_node_copies__"
_STRICT_MODE_LOGGER = "griptape_nodes.strict_mode"
# The parameter EditorBehaviorNode adds by request when its mode is expand.
_DYNAMIC_PARAMETER = "dynamic_extra"


@pytest.fixture(autouse=True)
def _register_library(tmp_path: Path) -> None:
    library_json = materialize_worker_behavior_library(tmp_path)
    result = current_engine().handle_request(RegisterLibraryFromFileRequest(file_path=str(library_json)))
    assert isinstance(result, RegisterLibraryFromFileResultSuccess), getattr(result, "result_details", result)


@pytest.fixture
def worker() -> Iterator[SimulatedWorker]:
    """A worker, with an engine of its own, that every node of the library this engine runs is sent to."""
    with simulated_worker(current_engine(), WORKER_BEHAVIOR_LIBRARY_NAME) as simulated:
        yield simulated


def _create(node_type: str, name: str) -> BaseNode:
    """Create a node on the orchestrator's canvas through CreateNodeRequest."""
    engine = current_engine()
    if not engine.context_manager.has_current_workflow():
        engine.context_manager.push_workflow(workflow_name="worker_copy_workflow")
    if engine.object_manager.attempt_get_object_by_name(_CANVAS_FLOW_NAME) is None:
        flow_result = engine.handle_request(
            CreateFlowRequest(parent_flow_name=None, flow_name=_CANVAS_FLOW_NAME, set_as_new_context=False)
        )
        assert isinstance(flow_result, CreateFlowResultSuccess), flow_result
    result = engine.handle_request(
        CreateNodeRequest(
            node_type=node_type,
            specific_library_name=WORKER_BEHAVIOR_LIBRARY_NAME,
            node_name=name,
            override_parent_flow_name=_CANVAS_FLOW_NAME,
        )
    )
    assert isinstance(result, CreateNodeResultSuccess), getattr(result, "result_details", result)
    return engine.node_manager.get_node_by_name(result.node_name)


def _set(node_name: str, parameter_name: str, value: Any) -> None:
    result = current_engine().handle_request(
        SetParameterValueRequest(node_name=node_name, parameter_name=parameter_name, value=value)
    )
    assert result.succeeded(), result


def _connect(source_node: str, source_parameter: str, target_node: str, target_parameter: str) -> None:
    result = current_engine().handle_request(
        CreateConnectionRequest(
            source_node_name=source_node,
            source_parameter_name=source_parameter,
            target_node_name=target_node,
            target_parameter_name=target_parameter,
        )
    )
    assert isinstance(result, CreateConnectionResultSuccess), result


async def _run(node: BaseNode) -> ResultPayload:
    """Run `node` on the orchestrator, which sends it to the worker. The executor sends no values of its own."""
    return await current_engine().ahandle_request(
        ExecuteNodeRequest(node_name=node.name, node_metadata=dict(node.metadata))  # type: ignore[arg-type]
    )


async def _run_successfully(node: BaseNode) -> ExecuteNodeResultSuccess:
    result = await _run(node)
    assert isinstance(result, ExecuteNodeResultSuccess), getattr(result, "result_details", result)
    return result


@contextmanager
def _capturing_copies(worker: SimulatedWorker) -> Iterator[list[BaseNode]]:
    """Record each copy the worker runs, as it is about to run."""
    copies: list[BaseNode] = []
    node_manager = worker.engine.node_manager
    run = node_manager._hydrate_and_run_node

    async def capture(node: BaseNode, request: ExecuteNodeRequest) -> ResultPayload:
        copies.append(node)
        return await run(node, request)

    with patch.object(node_manager, "_hydrate_and_run_node", side_effect=capture):
        yield copies


def _shape(node: BaseNode) -> dict[str, tuple[Any, ...]]:
    """Each parameter of `node`, rows included, by name: its types, modes, and the value it holds."""
    return {
        parameter.name: (
            parameter.type,
            parameter.output_type,
            tuple(parameter.input_types or ()),
            tuple(sorted(mode.name for mode in parameter.allowed_modes)),
            node.get_parameter_value(parameter.name),
        )
        for parameter in node.root_ui_element.find_elements_by_type(Parameter)
    }


def _what_the_worker_still_holds(worker: SimulatedWorker) -> dict[str, list[str]]:
    """Everything a discarded copy could leave behind in the worker's graph and bookkeeping."""
    engine = worker.engine
    return {
        "objects": sorted(
            name for name in engine.object_manager.get_filtered_subset() if name != _WORKER_COPIES_FLOW_NAME
        ),
        "node parents": sorted(engine.node_manager._name_to_parent_flow_name),
        "flow parents": sorted(
            name for name in engine.flow_manager._name_to_parent_name if name != _WORKER_COPIES_FLOW_NAME
        ),
    }


_NOTHING_LEFT = {"objects": [], "node parents": [], "flow parents": []}


def _connections_of(node_name: str) -> set[str]:
    engine = current_engine()
    node = engine.node_manager.get_node_by_name(node_name)
    connections = engine.flow_manager.get_connections()
    edges = list(connections.get_all_outgoing_connections(node)) + list(connections.get_all_incoming_connections(node))
    return {
        f"{edge.source_node.name}.{edge.source_parameter.name}->{edge.target_node.name}.{edge.target_parameter.name}"
        for edge in edges
    }


def _violations(result: ResultPayload) -> list[StrictModeViolationDetail]:
    details = result.result_details
    assert isinstance(details, ResultDetails), details
    return [detail for detail in details.result_details if isinstance(detail, StrictModeViolationDetail)]


def _rule_ids(result: ResultPayload) -> set[str]:
    return {violation.rule_id for violation in _violations(result)}


class TestTheCopyMatchesTheNodeItWasCopiedFrom:
    """The copy is loaded from the orchestrator's node, so it has that node's shape and values, not its class's."""

    @pytest.mark.asyncio
    async def test_parameters_a_value_hook_derived_reach_the_copy(self, worker: SimulatedWorker) -> None:
        node = _create("DerivedStructureNode", "Derived")
        _set("Derived", "shape", "expanded")
        _set("Derived", "derived_in", "deeper")
        _set("Derived", "derived_deep", "from the bottom")

        with _capturing_copies(worker) as copies:
            await _run_successfully(node)

        assert _shape(copies[0]) == _shape(node)
        assert copies[0].get_parameter_by_name("derived_deep") is not None

    @pytest.mark.asyncio
    async def test_a_parameter_a_hook_added_by_request_reaches_the_copy(self, worker: SimulatedWorker) -> None:
        node = _create("EditorBehaviorNode", "Editor")
        _set("Editor", "mode", "expand")
        _set("Editor", _DYNAMIC_PARAMETER, "edited")

        with _capturing_copies(worker) as copies:
            await _run_successfully(node)

        assert _shape(copies[0]) == _shape(node)
        assert copies[0].get_parameter_value(_DYNAMIC_PARAMETER) == "edited"

    @pytest.mark.asyncio
    async def test_a_parameter_added_in_the_editor_and_a_changed_type_reach_the_copy(
        self, worker: SimulatedWorker
    ) -> None:
        node = _create("ReadsAnInputListNode", "Lister")
        added = current_engine().handle_request(
            AddParameterToNodeRequest(
                node_name="Lister",
                parameter_name="added_in_editor",
                type="str",
                default_value="",
                tooltip="Added by the artist",
                mode_allowed_input=True,
                mode_allowed_property=True,
                mode_allowed_output=False,
                is_user_defined=True,
            )
        )
        assert isinstance(added, AddParameterToNodeResultSuccess), added
        _set("Lister", "added_in_editor", "artist value")
        altered = current_engine().handle_request(
            AlterParameterDetailsRequest(
                node_name="Lister", parameter_name="echo", type="list[str]", output_type="list[str]"
            )
        )
        assert altered.succeeded(), altered
        _set("Lister", "items", ["a", "b"])

        with _capturing_copies(worker) as copies:
            result = await _run_successfully(node)

        assert _shape(copies[0]) == _shape(node)
        echo = copies[0].get_parameter_by_name("echo")
        assert echo is not None
        assert echo.output_type == "list[str]"
        assert copies[0].get_parameter_value("added_in_editor") == "artist value"
        assert result.parameter_output_values["echo"] == ["a", "b"]


_GRAPH_EDITS = (RemoveParameterFromNodeRequest, DeleteConnectionRequest, CreateConnectionRequest)


def _build_the_graph_the_edits_aim_at() -> BaseNode:
    """The orchestrator's graph EditsTheGraphWhileLoadingNode's requests would change, and that node."""
    _create("ChainStartNode", "Graph Edit Upstream")
    _create("ChainMiddleNode", "Graph Edit Target")
    _connect("Graph Edit Upstream", "out", "Graph Edit Target", "in_value")
    editor = _create("EditsTheGraphWhileLoadingNode", "Graph Editor")
    _set("Graph Editor", "trigger", "set in the editor")
    return editor


def _orchestrator_graph() -> dict[str, Any]:
    target = current_engine().node_manager.get_node_by_name("Graph Edit Target")
    return {
        "target parameters": sorted(parameter.name for parameter in target.parameters),
        "target connections": _connections_of("Graph Edit Target"),
        "editor connections": _connections_of("Graph Editor"),
    }


class TestNothingTheLoadSendsReachesTheOrchestrator:
    """Loading the copy is answered in the worker's own graph; the orchestrator's graph never hears of it."""

    @pytest.mark.asyncio
    async def test_graph_edits_sent_while_loading_leave_the_orchestrators_graph_unchanged(
        self, worker: SimulatedWorker
    ) -> None:
        editor = _build_the_graph_the_edits_aim_at()
        before = _orchestrator_graph()

        result = await _run(editor)

        assert _orchestrator_graph() == before
        assert [request for request in worker.forwarded if isinstance(request, _GRAPH_EDITS)] == []
        assert isinstance(result, ExecuteNodeResultFailure), result
        assert "graph-access-while-loading" in str(result.result_details)

    @pytest.mark.asyncio
    async def test_graph_edits_sent_while_loading_stay_local_while_another_node_runs_in_the_worker(
        self, worker: SimulatedWorker
    ) -> None:
        editor = _build_the_graph_the_edits_aim_at()
        waiter = _create("WaitsUntilReleasedNode", "Long Runner")
        # The library's module is loaded from a copy, so its classes are reached through their nodes.
        waiter_type: Any = type(waiter)
        waiter_type.release = asyncio.Event()
        waiter_type.running.clear()
        before = _orchestrator_graph()

        long_run = asyncio.create_task(_run(waiter))
        await _until(lambda: "Long Runner" in waiter_type.running)
        assert worker.engine.event_manager.in_node_execution()
        result = await _run(editor)
        waiter_type.release.set()
        long_result = await long_run

        assert _orchestrator_graph() == before
        assert [request for request in worker.forwarded if isinstance(request, _GRAPH_EDITS)] == []
        assert isinstance(result, ExecuteNodeResultFailure), result
        assert "graph-access-while-loading" in str(result.result_details)
        assert isinstance(long_result, ExecuteNodeResultSuccess), long_result.result_details


async def _until(condition: Any) -> None:
    """Yield to the loop until `condition()` holds, which a node running in the worker makes true."""
    async with asyncio.timeout(5):
        while not condition():  # noqa: ASYNC110 -- the state is the node's, not an event this test owns
            await asyncio.sleep(0.01)


class TestDiscardingTheCopy:
    """A copy is discarded after every run, without deleting it: what it cached stays, and it leaves nothing behind."""

    @pytest.mark.asyncio
    async def test_an_unserializable_output_stays_resolvable_after_the_copy_is_discarded(
        self, worker: SimulatedWorker
    ) -> None:
        node = _create("UnshippableOutputNode", "Unshippable")

        result = await _run_successfully(node)

        sent = result.parameter_output_values["live_handle"]
        assert is_reference(sent), sent
        assert _what_the_worker_still_holds(worker) == _NOTHING_LEFT
        entry = worker.engine.resource_manager.entry_for(sent["key"])
        assert entry is not None, "discarding the copy released the object its output refers to"
        assert type(entry.value) is object

    @pytest.mark.usefixtures("worker")
    @pytest.mark.asyncio
    async def test_discarding_the_copy_does_not_run_after_node_deleted(self) -> None:
        node = _create("RecordsDeletionNode", "Recorder")
        recorder_type: Any = type(node)
        recorder_type.deleted.clear()

        result = await _run_successfully(node)

        assert result.parameter_output_values["ran"] is True
        assert recorder_type.deleted == []

    @pytest.mark.asyncio
    async def test_nothing_of_the_copy_is_left_after_a_run_that_succeeds(self, worker: SimulatedWorker) -> None:
        await _run_successfully(_create("ChainStartNode", "Succeeds"))

        assert _what_the_worker_still_holds(worker) == _NOTHING_LEFT

    @pytest.mark.asyncio
    async def test_nothing_of_the_copy_is_left_after_a_run_that_fails(self, worker: SimulatedWorker) -> None:
        result = await _run(_create("FailsInProcessNode", "Fails"))

        assert isinstance(result, ExecuteNodeResultFailure), result
        assert "this node always fails" in str(result.result_details)
        assert _what_the_worker_still_holds(worker) == _NOTHING_LEFT

    @pytest.mark.asyncio
    async def test_nothing_of_the_copy_is_left_after_a_run_that_is_refused(self, worker: SimulatedWorker) -> None:
        result = await _run(_create("RefusesToRunNode", "Refused"))

        assert isinstance(result, ExecuteNodeResultFailure), result
        assert "this node never runs here" in str(result.result_details)
        assert _what_the_worker_still_holds(worker) == _NOTHING_LEFT

    @pytest.mark.asyncio
    async def test_nothing_of_the_copy_is_left_after_a_run_that_is_cancelled(self, worker: SimulatedWorker) -> None:
        waiter = _create("WaitsUntilReleasedNode", "Cancelled")
        # The library's module is loaded from a copy, so its classes are reached through their nodes.
        waiter_type: Any = type(waiter)
        waiter_type.release = asyncio.Event()
        waiter_type.running.clear()

        run = asyncio.create_task(_run(waiter))
        await _until(lambda: "Cancelled" in waiter_type.running)
        run.cancel()
        with pytest.raises(asyncio.CancelledError):
            await run
        await _until(lambda: not worker.engine.event_manager.in_node_execution())

        assert _what_the_worker_still_holds(worker) == _NOTHING_LEFT


class TestRequestsAWorkerRefuses:
    """Some requests a worker must not load from, and it refuses them before creating anything."""

    @pytest.mark.asyncio
    async def test_a_structure_holding_another_kind_of_request_fails_before_anything_is_created(
        self, worker: SimulatedWorker
    ) -> None:
        request = ExecuteNodeRequest(
            node_name="Smuggler",
            node_metadata={"node_type": "ChainStartNode", "library": WORKER_BEHAVIOR_LIBRARY_NAME},
            element_modification_commands=[
                DeleteConnectionRequest(
                    source_node_name="Elsewhere",
                    source_parameter_name="out",
                    target_node_name="Smuggler",
                    target_parameter_name="in_value",
                )
            ],
        )
        with patch.object(LibraryRegistry, "create_node", wraps=LibraryRegistry.create_node) as create_node:
            result = await worker.execute(request)

        assert isinstance(result, ExecuteNodeResultFailure), result
        assert "DeleteConnectionRequest" in str(result.result_details)
        create_node.assert_not_called()
        assert _what_the_worker_still_holds(worker) == _NOTHING_LEFT

    @pytest.mark.asyncio
    async def test_a_node_group_is_refused(self, worker: SimulatedWorker) -> None:
        request = ExecuteNodeRequest(
            node_name="Group",
            node_metadata={"node_type": "PlainGroupNode", "library": WORKER_BEHAVIOR_LIBRARY_NAME},
        )

        result = await worker.execute(request)

        assert isinstance(result, ExecuteNodeResultFailure), result
        assert "node group" in str(result.result_details)
        assert _what_the_worker_still_holds(worker) == _NOTHING_LEFT


class TestListsReachTheNodeAsTheOrchestratorHoldsThem:
    """However a list was filled, the copy's process() reads the items the orchestrator's node holds."""

    @pytest.mark.usefixtures("worker")
    @pytest.mark.asyncio
    async def test_rows_set_and_removed_in_the_editor(self) -> None:
        node = _create("ReadsAnInputListNode", "Rows")
        rows = []
        for value in ("a", "b", "c"):
            added = current_engine().handle_request(
                AddParameterToNodeRequest(node_name="Rows", parent_container_name="items")
            )
            assert isinstance(added, AddParameterToNodeResultSuccess), added
            _set("Rows", added.parameter_name, value)
            rows.append(added.parameter_name)
        removed = current_engine().handle_request(
            RemoveParameterFromNodeRequest(node_name="Rows", parameter_name=rows[1])
        )
        assert removed.succeeded(), removed

        result = await _run_successfully(node)

        assert result.parameter_output_values["echo"] == node.get_parameter_list_value("items") == ["a", "c"]

    @pytest.mark.usefixtures("worker")
    @pytest.mark.asyncio
    async def test_a_list_set_whole_by_request(self) -> None:
        node = _create("ReadsAnInputListNode", "Whole")
        _set("Whole", "items", ["a", "b"])

        result = await _run_successfully(node)

        assert result.parameter_output_values["echo"] == ["a", "b"]

    @pytest.mark.usefixtures("worker")
    @pytest.mark.asyncio
    async def test_a_row_holding_a_list(self) -> None:
        """A row that holds a list adds its items, on the copy as on the orchestrator."""
        node = _create("ReadsAnInputListNode", "Nested")
        _set("Nested", "items", [["a", "b"], "c"])
        assert node.get_parameter_list_value("items") == ["a", "b", "c"]

        result = await _run_successfully(node)

        assert result.parameter_output_values["echo"] == ["a", "b", "c"]

    @pytest.mark.parametrize(
        ("node_type", "description"),
        [("ReadsAnInputListNode", "within its rows"), ("ReadsABoundedListNode", "past its max_items")],
    )
    @pytest.mark.asyncio
    async def test_a_list_fed_by_a_connection(self, worker: SimulatedWorker, node_type: str, description: str) -> None:
        """The flow runs both nodes in the worker; the list arrives whole, as its connection delivered it."""
        _create("ListSourceNode", "Source")
        node = _create(node_type, "Fed")
        _connect("Source", "items", "Fed", "items")

        result = await current_engine().ahandle_request(StartFlowRequest(flow_name=_CANVAS_FLOW_NAME))

        assert result.succeeded(), result
        assert [request.node_name for request in worker.received] == ["Source", "Fed"]
        assert node.parameter_output_values["echo"] == ["first", "second", "third"], description

    @pytest.mark.parametrize("node_type", ["ReadsAnInputListNode", "ReadsABoundedListNode"])
    @pytest.mark.asyncio
    async def test_pasting_a_connected_list_with_its_upstream_keeps_its_whole_value(
        self, worker: SimulatedWorker, node_type: str
    ) -> None:
        """A paste sets the values before it makes the connections, so the load must not ask whether the list is connected."""
        _create("ListSourceNode", "Source")
        node = _create(node_type, "Fed")
        _connect("Source", "items", "Fed", "items")
        run = await current_engine().ahandle_request(StartFlowRequest(flow_name=_CANVAS_FLOW_NAME))
        assert run.succeeded(), run
        assert node.get_parameter_list_value("items") == ["first", "second", "third"]
        worker.received.clear()

        pasted_names = _paste(["Source", "Fed"])

        pasted = current_engine().node_manager.get_node_by_name(pasted_names["Fed"])
        assert _connections_of(pasted.name) == {f"{pasted_names['Source']}.items->{pasted.name}.items"}
        assert pasted.get_parameter_list_value("items") == ["first", "second", "third"]
        pasted_list = pasted.get_parameter_by_name("items")
        assert isinstance(pasted_list, ParameterList)
        assert pasted_list.get_child_parameters() == []


def _paste(node_names: list[str]) -> dict[str, str]:
    """Copy `node_names` and paste them onto the canvas. Returns the pasted name of each."""
    engine = current_engine()
    with engine.context_manager.flow(_CANVAS_FLOW_NAME):
        copied = engine.handle_request(
            SerializeSelectedNodesToCommandsRequest(nodes_to_serialize=[[name, "0"] for name in node_names])
        )
        assert isinstance(copied, SerializeSelectedNodesToCommandsResultSuccess), copied
        before = set(engine.object_manager.get_filtered_subset())
        pasted = engine.handle_request(
            DeserializeSelectedNodesFromCommandsRequest(
                deserialize_commands=copied.serialized_selected_node_commands,
                pickled_values=copied.pickled_values,
            )
        )
    assert isinstance(pasted, DeserializeSelectedNodesFromCommandsResultSuccess), pasted
    new_names = sorted(set(engine.object_manager.get_filtered_subset()) - before)
    return {name: next(new for new in new_names if new.startswith(name)) for name in node_names}


class TestGraphAccessWhileLoading:
    """Strict-mode rule graph-access-while-loading: a loading node asks the graph nothing."""

    def test_warns_when_a_node_asks_about_its_connections_as_it_is_pasted(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        _create("ReadsConnectionsWhileLoadingNode", "Asker")
        _set("Asker", "trigger", "set in the editor")

        with caplog.at_level(logging.WARNING, logger=_STRICT_MODE_LOGGER):
            _paste(["Asker"])

        reports = [record for record in caplog.records if "while it was loading" in record.getMessage()]
        assert reports, caplog.text
        assert all(record.levelno == logging.WARNING for record in reports)
        assert "ListConnectionsForNodeRequest" in reports[0].getMessage()

    @pytest.mark.asyncio
    async def test_fails_the_run_of_a_node_that_asks_about_its_connections_as_its_copy_loads(
        self, worker: SimulatedWorker
    ) -> None:
        node = _create("ReadsConnectionsWhileLoadingNode", "Asker")
        _set("Asker", "trigger", "set in the editor")

        result = await _run(node)

        assert isinstance(result, ExecuteNodeResultFailure), result
        assert "graph-access-while-loading" in str(result.result_details)
        assert [request for request in worker.forwarded if isinstance(request, ListConnectionsForNodeRequest)] == []

    @pytest.mark.usefixtures("worker")
    @pytest.mark.asyncio
    async def test_is_silent_for_loading_and_running_ordinary_library_nodes(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Covers the engine's own load code too: every request these loads make comes from the engine."""
        nodes = _build_an_ordinary_workflow()

        with caplog.at_level(logging.WARNING, logger=_STRICT_MODE_LOGGER):
            _paste([node.name for node in nodes])
            results = [await _run(node) for node in nodes]

        assert "while it was loading" not in caplog.text
        for node, result in zip(nodes, results, strict=True):
            assert isinstance(result, ExecuteNodeResultSuccess), (node.name, result.result_details)
            assert "graph-access-while-loading" not in _rule_ids(result), node.name

    @pytest.mark.asyncio
    async def test_is_silent_for_reopening_a_saved_workflow_of_ordinary_library_nodes(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A saved workflow is a Python file that loads through the same requests a worker's copy does."""
        engine = current_engine()
        names = [node.name for node in _build_an_ordinary_workflow()]
        saved = engine.handle_request(SaveWorkflowRequest(file_name="ordinary_worker_nodes"))
        assert isinstance(saved, SaveWorkflowResultSuccess), saved
        cleared = engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
        assert cleared.succeeded(), cleared
        await engine.workflow_manager.refresh_workflow_registry()

        with caplog.at_level(logging.WARNING, logger=_STRICT_MODE_LOGGER):
            reopened = await engine.ahandle_request(RunWorkflowFromRegistryRequest(workflow_name=saved.workflow_name))

        assert isinstance(reopened, RunWorkflowFromRegistryResultSuccess), reopened
        assert "while it was loading" not in caplog.text
        for name in names:
            assert engine.node_manager.get_node_by_name(name) is not None, name
        assert _connections_of("Lister") == {"Source.items->Lister.items"}
        assert engine.node_manager.get_node_by_name("Lister").get_parameter_list_value("items") == [
            "first",
            "second",
            "third",
        ]


def _build_an_ordinary_workflow() -> list[BaseNode]:
    """Nodes of the library with derived and editor-added parameters, rows, and a connected list holding a value."""
    derived = _create("DerivedStructureNode", "Derived")
    _set("Derived", "shape", "expanded")
    _set("Derived", "derived_in", "deeper")
    editor = _create("EditorBehaviorNode", "Editor")
    _set("Editor", "mode", "expand")
    rows = _create("ReadsAnInputListNode", "Rows")
    _set("Rows", "items", ["a", "b"])
    added = current_engine().handle_request(
        AddParameterToNodeRequest(
            node_name="Rows",
            parameter_name="added_in_editor",
            type="str",
            default_value="",
            tooltip="Added by the artist",
            is_user_defined=True,
        )
    )
    assert isinstance(added, AddParameterToNodeResultSuccess), added
    source = _create("ListSourceNode", "Source")
    lister = _create("ReadsAnInputListNode", "Lister")
    _connect("Source", "items", "Lister", "items")
    # As the connection delivers it when the flow runs.
    lister.set_parameter_value("items", list(source.ITEMS))  # type: ignore[attr-defined]
    return [derived, editor, rows, source, lister]


class TestStateReadBypassingRequests:
    """Strict-mode rule state-read-bypassing-requests: a running node reads engine state with requests."""

    @pytest.mark.asyncio
    async def test_warns_when_a_node_running_in_process_reads_a_manager(self, caplog: pytest.LogCaptureFixture) -> None:
        node = _create("ReadsAManagerNode", "Reader")

        with caplog.at_level(logging.WARNING, logger=_STRICT_MODE_LOGGER):
            result = await _run_successfully(node)

        assert "state-read-bypassing-requests" in _rule_ids(result)
        assert result.parameter_output_values["config_value"]
        assert "ConfigManager" in caplog.text
        assert "runtime_execute/warning" in caplog.text

    @pytest.mark.usefixtures("worker")
    @pytest.mark.asyncio
    async def test_still_refuses_the_read_in_a_worker(self) -> None:
        result = await _run(_create("ReadsAManagerNode", "Reader"))

        assert isinstance(result, ExecuteNodeResultFailure), result
        assert "ConfigManager" in str(result.result_details)
        assert "GetConfigValueRequest" in str(result.result_details)


class TestStateOutsideParameters:
    """Strict-mode rule state-outside-parameters: a value hook keeps its state in parameters."""

    @pytest.mark.asyncio
    async def test_warns_when_a_hook_sets_an_attribute_on_a_node_running_in_process(self) -> None:
        node = _create("SetsAnAttributeInItsHookNode", "Stasher")

        result = await _run_successfully(node)

        violations = [violation for violation in _violations(result) if violation.rule_id == "state-outside-parameters"]
        assert violations, result.result_details
        assert all(detail.severity == "warning" for detail in violations)
        assert "'stash'" in violations[0].message

    @pytest.mark.usefixtures("worker")
    @pytest.mark.asyncio
    async def test_fails_the_run_when_a_hook_sets_an_attribute_on_a_worker(self) -> None:
        result = await _run(_create("SetsAnAttributeInItsHookNode", "Stasher"))

        assert isinstance(result, ExecuteNodeResultFailure), result
        assert "state-outside-parameters" in str(result.result_details)

    @pytest.mark.parametrize("mode", ["declared", "in_place"])
    @pytest.mark.usefixtures("worker")
    @pytest.mark.asyncio
    async def test_leaves_declared_attributes_and_changes_made_in_place_alone(self, mode: str) -> None:
        """Setting a value also changes BaseNode's own attributes, which the rule ignores too."""
        node = _create("SetsAnAttributeInItsHookNode", "Stasher")
        _set("Stasher", "mode", mode)

        result = await _run_successfully(node)

        assert "state-outside-parameters" not in _rule_ids(result)

    def test_reports_an_attribute_a_hook_sets_as_the_node_is_edited(self, caplog: pytest.LogCaptureFixture) -> None:
        _create("SetsAnAttributeInItsHookNode", "Stasher")

        with caplog.at_level(logging.WARNING, logger=_STRICT_MODE_LOGGER):
            _set("Stasher", "note", "edited")

        reports = [record for record in caplog.records if "'stash'" in record.getMessage()]
        assert reports, caplog.text
        assert "edit/warning" in reports[0].getMessage()
