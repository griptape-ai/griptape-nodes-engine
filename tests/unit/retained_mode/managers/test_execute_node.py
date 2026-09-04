from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from griptape_nodes.exe_types.node_types import BaseNode
from griptape_nodes.retained_mode.events.execution_events import (
    ExecuteNodeRequest,
    ExecuteNodeResultFailure,
    ExecuteNodeResultSuccess,
    NodeMetadata,
)
from griptape_nodes.retained_mode.managers.library_manager import LibraryManager
from griptape_nodes.retained_mode.managers.node_manager import NodeManager

_LIBRARY_REGISTRY_CREATE_NODE_PATH = "griptape_nodes.retained_mode.managers.node_manager.LibraryRegistry.create_node"


def _make_mock_node(name: str = "test_node") -> MagicMock:
    node = MagicMock(spec=BaseNode)
    node.name = name
    node.aprocess = AsyncMock()
    node.parameter_values = {}
    node.parameter_output_values = {"output_param": "output_value"}
    node.metadata = {}
    return node


def _make_mock_obj_mgr(existing_node: MagicMock | None = None) -> MagicMock:
    mock_obj_mgr = MagicMock()
    mock_obj_mgr.attempt_get_object_by_name_as_type.return_value = existing_node
    return mock_obj_mgr


def _make_mock_library_manager(*, is_worker: bool, library_loaded: bool = True) -> MagicMock:
    """A stub LibraryManager.

    `library_loaded` matters because the worker-side failure path asks whether the library
    actually LOADED before blaming the process for not starting. Left as a bare MagicMock, that
    question answers with a truthy mock and every failure claims the process died -- so the state
    is set explicitly here rather than left to mock defaults.
    """
    lib_mgr = MagicMock()
    lib_mgr.is_worker = is_worker
    lib_mgr._is_worker = is_worker
    lib_mgr.get_worker_for_library.return_value = None
    # The execute path awaits this before consulting get_worker_for_library; a plain
    # MagicMock attribute is not awaitable.
    lib_mgr.wait_for_worker_library_load = AsyncMock()
    if library_loaded:
        lib_mgr.get_library_info_by_library_name.return_value.lifecycle_state = (
            LibraryManager.LibraryLifecycleState.LOADED
        )
    else:
        lib_mgr.get_library_info_by_library_name.return_value.lifecycle_state = (
            LibraryManager.LibraryLifecycleState.EVALUATED
        )
        # The name the code actually calls; configuring the other one left the reason unexercised.
        lib_mgr.get_collated_problems_for_library.return_value = "Dependency installation failed: no solution found"
    return lib_mgr


def _make_node_manager(
    *,
    object_manager: MagicMock | None = None,
    library_manager: MagicMock | None = None,
    worker_manager: MagicMock | None = None,
) -> NodeManager:
    """Build a NodeManager wired to a mock engine instead of the process-wide facade."""
    mock_engine = MagicMock()
    mock_engine.object_manager = object_manager
    mock_engine.library_manager = library_manager
    mock_engine.worker_manager = worker_manager
    return NodeManager(MagicMock(), engine=mock_engine)


class TestExecuteNodeOrchestratorPath:
    """Orchestrator side of ExecuteNodeRequest.

    On the orchestrator, ObjectManager is the source of truth for node identity.
    A lookup miss is a hard failure -- we do not fabricate a fresh node from
    metadata, because doing so would mask real "node dropped from the live map"
    bugs with a stub that has no connections or flow parentage.
    """

    @pytest.mark.asyncio
    async def test_missing_node_fails_without_fallback_to_create(self) -> None:
        """Orchestrator lookup miss returns failure; LibraryRegistry.create_node NOT called."""
        mock_obj_mgr = _make_mock_obj_mgr(existing_node=None)
        lib_mgr = _make_mock_library_manager(is_worker=False)
        node_manager = _make_node_manager(object_manager=mock_obj_mgr, library_manager=lib_mgr)

        with patch(_LIBRARY_REGISTRY_CREATE_NODE_PATH) as mock_create:
            request = ExecuteNodeRequest(
                node_name="nonexistent_node",
                node_metadata=cast("NodeMetadata", {"node_type": "SomeNodeType", "library": "some_library"}),
            )
            result = await node_manager.on_execute_node_request(request)

        assert isinstance(result, ExecuteNodeResultFailure)
        assert "nonexistent_node" in str(result.result_details)
        assert "not found" in str(result.result_details).lower()
        mock_create.assert_not_called()
        mock_obj_mgr.add_object_by_name.assert_not_called()

    @pytest.mark.asyncio
    async def test_reuses_existing_node(self) -> None:
        """Node already in ObjectManager: skip creation, execute in place."""
        mock_node = _make_mock_node()
        mock_obj_mgr = _make_mock_obj_mgr(existing_node=mock_node)
        lib_mgr = _make_mock_library_manager(is_worker=False)
        node_manager = _make_node_manager(object_manager=mock_obj_mgr, library_manager=lib_mgr)

        with patch(_LIBRARY_REGISTRY_CREATE_NODE_PATH) as mock_create:
            request = ExecuteNodeRequest(
                node_name="test_node",
                parameter_values={"input_param": "input_value"},
                node_metadata={"node_type": "SomeNodeType", "library": "some_library"},
            )
            result = await node_manager.on_execute_node_request(request)

        assert isinstance(result, ExecuteNodeResultSuccess)
        mock_create.assert_not_called()
        mock_obj_mgr.add_object_by_name.assert_not_called()
        mock_node.set_parameter_value.assert_called_once_with("input_param", "input_value")
        mock_node.aprocess.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_params(self) -> None:
        mock_node = _make_mock_node()
        mock_obj_mgr = _make_mock_obj_mgr(existing_node=mock_node)
        lib_mgr = _make_mock_library_manager(is_worker=False)
        node_manager = _make_node_manager(object_manager=mock_obj_mgr, library_manager=lib_mgr)

        request = ExecuteNodeRequest(node_name="test_node", node_metadata=cast("NodeMetadata", {"node_type": "T"}))
        result = await node_manager.on_execute_node_request(request)

        assert isinstance(result, ExecuteNodeResultSuccess)
        mock_node.set_parameter_value.assert_not_called()
        mock_node.aprocess.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_set_parameter_fails(self) -> None:
        mock_node = _make_mock_node()
        mock_node.set_parameter_value.side_effect = ValueError("bad value")
        mock_obj_mgr = _make_mock_obj_mgr(existing_node=mock_node)
        lib_mgr = _make_mock_library_manager(is_worker=False)
        node_manager = _make_node_manager(object_manager=mock_obj_mgr, library_manager=lib_mgr)

        request = ExecuteNodeRequest(
            node_name="test_node",
            parameter_values={"bad_param": "bad_value"},
        )
        result = await node_manager.on_execute_node_request(request)

        assert isinstance(result, ExecuteNodeResultFailure)
        assert "bad_param" in str(result.result_details)
        mock_node.aprocess.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_aprocess_fails(self) -> None:
        mock_node = _make_mock_node()
        mock_node.aprocess.side_effect = RuntimeError("process exploded")
        mock_obj_mgr = _make_mock_obj_mgr(existing_node=mock_node)
        lib_mgr = _make_mock_library_manager(is_worker=False)
        node_manager = _make_node_manager(object_manager=mock_obj_mgr, library_manager=lib_mgr)

        request = ExecuteNodeRequest(node_name="test_node")
        result = await node_manager.on_execute_node_request(request)

        assert isinstance(result, ExecuteNodeResultFailure)
        assert "process exploded" in str(result.result_details)

    @pytest.mark.asyncio
    async def test_multiple_params(self) -> None:
        mock_node = _make_mock_node()
        mock_obj_mgr = _make_mock_obj_mgr(existing_node=mock_node)
        lib_mgr = _make_mock_library_manager(is_worker=False)
        node_manager = _make_node_manager(object_manager=mock_obj_mgr, library_manager=lib_mgr)

        request = ExecuteNodeRequest(
            node_name="test_node",
            parameter_values={"param_a": 1, "param_b": "two", "param_c": [3]},
        )
        result = await node_manager.on_execute_node_request(request)

        assert isinstance(result, ExecuteNodeResultSuccess)
        expected_param_count = 3
        assert mock_node.set_parameter_value.call_count == expected_param_count

    @pytest.mark.asyncio
    async def test_hydrate_skips_identical_values(self) -> None:
        """Identity-skip guard: hydrate does not re-call set_parameter_value for matching values."""
        mock_node = _make_mock_node()
        # Pre-populate parameter_values so each hydrate lookup finds a match.
        mock_node.parameter_values = {"param_a": 1, "param_b": "two", "param_c": [3]}
        mock_obj_mgr = _make_mock_obj_mgr(existing_node=mock_node)
        lib_mgr = _make_mock_library_manager(is_worker=False)
        node_manager = _make_node_manager(object_manager=mock_obj_mgr, library_manager=lib_mgr)

        request = ExecuteNodeRequest(
            node_name="test_node",
            parameter_values=dict(mock_node.parameter_values),
        )
        result = await node_manager.on_execute_node_request(request)

        assert isinstance(result, ExecuteNodeResultSuccess)
        mock_node.set_parameter_value.assert_not_called()
        mock_node.aprocess.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_hydrate_calls_set_for_differing_values(self) -> None:
        """Identity-skip does not fire when the incoming value differs from current."""
        mock_node = _make_mock_node()
        mock_node.parameter_values = {"param_a": 1}  # existing, but stale
        mock_obj_mgr = _make_mock_obj_mgr(existing_node=mock_node)
        lib_mgr = _make_mock_library_manager(is_worker=False)
        node_manager = _make_node_manager(object_manager=mock_obj_mgr, library_manager=lib_mgr)

        request = ExecuteNodeRequest(
            node_name="test_node",
            parameter_values={"param_a": 999},  # differs from current
        )
        result = await node_manager.on_execute_node_request(request)

        assert isinstance(result, ExecuteNodeResultSuccess)
        mock_node.set_parameter_value.assert_called_once_with("param_a", 999)


class TestExecuteNodeWorkerPathStateless:
    """Worker side of ExecuteNodeRequest: pure RPC, no persistence.

    Each ExecuteNodeRequest on the worker constructs a fresh node from the
    request metadata, hydrates, runs aprocess, and discards. ObjectManager is
    never populated on the worker side -- the orchestrator is the single source
    of truth for node identity and parameter values.
    """

    @pytest.mark.asyncio
    async def test_constructs_fresh_node_and_executes(self) -> None:
        """Worker path: no prior ObjectManager entry, construct from metadata, run."""
        mock_node = _make_mock_node()
        mock_obj_mgr = _make_mock_obj_mgr(existing_node=None)
        lib_mgr = _make_mock_library_manager(is_worker=True)
        node_manager = _make_node_manager(object_manager=mock_obj_mgr, library_manager=lib_mgr)

        with patch(_LIBRARY_REGISTRY_CREATE_NODE_PATH, return_value=mock_node) as mock_create:
            request = ExecuteNodeRequest(
                node_name="test_node",
                parameter_values={"input_param": "value"},
                node_metadata={"node_type": "SomeNodeType", "library": "some_library"},
            )
            result = await node_manager.on_execute_node_request(request)

        assert isinstance(result, ExecuteNodeResultSuccess)
        mock_create.assert_called_once_with(
            node_type="SomeNodeType",
            name="test_node",
            metadata={"node_type": "SomeNodeType", "library": "some_library"},
            specific_library_name="some_library",
        )
        mock_node.set_parameter_value.assert_called_once_with("input_param", "value")
        mock_node.aprocess.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_never_persists_node_in_object_manager(self) -> None:
        """Worker path never calls add_object_by_name; node is transient per request."""
        mock_node = _make_mock_node()
        mock_obj_mgr = _make_mock_obj_mgr(existing_node=None)
        lib_mgr = _make_mock_library_manager(is_worker=True)
        node_manager = _make_node_manager(object_manager=mock_obj_mgr, library_manager=lib_mgr)

        with patch(_LIBRARY_REGISTRY_CREATE_NODE_PATH, return_value=mock_node):
            request = ExecuteNodeRequest(
                node_name="test_node",
                node_metadata={"node_type": "SomeNodeType", "library": "some_library"},
            )
            await node_manager.on_execute_node_request(request)

        mock_obj_mgr.add_object_by_name.assert_not_called()

    @pytest.mark.asyncio
    async def test_constructs_fresh_node_even_when_name_exists_in_object_manager(self) -> None:
        """Worker ignores any stale ObjectManager entry: always constructs fresh.

        This is the retry-idempotency property. A prior request may have left an
        entry in ObjectManager (from pre-stateless code, a manual test, or the
        future mid-transition case where old code paths coexist); the worker must
        not trust it.
        """
        stale_node = _make_mock_node(name="stale")
        fresh_node = _make_mock_node(name="test_node")
        mock_obj_mgr = _make_mock_obj_mgr(existing_node=stale_node)
        lib_mgr = _make_mock_library_manager(is_worker=True)
        node_manager = _make_node_manager(object_manager=mock_obj_mgr, library_manager=lib_mgr)

        with patch(_LIBRARY_REGISTRY_CREATE_NODE_PATH, return_value=fresh_node) as mock_create:
            request = ExecuteNodeRequest(
                node_name="test_node",
                node_metadata={"node_type": "SomeNodeType", "library": "some_library"},
            )
            result = await node_manager.on_execute_node_request(request)

        assert isinstance(result, ExecuteNodeResultSuccess)
        mock_create.assert_called_once()
        # Fresh node ran, stale one didn't.
        fresh_node.aprocess.assert_awaited_once()
        stale_node.aprocess.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_missing_metadata_fails(self) -> None:
        """Worker path requires node_metadata; absent → failure."""
        mock_obj_mgr = _make_mock_obj_mgr(existing_node=None)
        lib_mgr = _make_mock_library_manager(is_worker=True)
        node_manager = _make_node_manager(object_manager=mock_obj_mgr, library_manager=lib_mgr)

        request = ExecuteNodeRequest(node_name="nonexistent_node")
        result = await node_manager.on_execute_node_request(request)

        assert isinstance(result, ExecuteNodeResultFailure)
        assert "nonexistent_node" in str(result.result_details)

    @pytest.mark.asyncio
    async def test_missing_node_type_in_metadata_fails(self) -> None:
        mock_obj_mgr = _make_mock_obj_mgr(existing_node=None)
        lib_mgr = _make_mock_library_manager(is_worker=True)
        node_manager = _make_node_manager(object_manager=mock_obj_mgr, library_manager=lib_mgr)

        request = ExecuteNodeRequest(
            node_name="some_node",
            node_metadata=cast("NodeMetadata", {"library": "some_library"}),
        )
        result = await node_manager.on_execute_node_request(request)

        assert isinstance(result, ExecuteNodeResultFailure)
        assert "node_type" in str(result.result_details)

    @pytest.mark.asyncio
    async def test_creation_failure_returns_failure(self) -> None:
        """LibraryRegistry.create_node raises → ExecuteNodeResultFailure."""
        mock_obj_mgr = _make_mock_obj_mgr(existing_node=None)
        lib_mgr = _make_mock_library_manager(is_worker=True)
        node_manager = _make_node_manager(object_manager=mock_obj_mgr, library_manager=lib_mgr)

        with patch(_LIBRARY_REGISTRY_CREATE_NODE_PATH, side_effect=RuntimeError("library not loaded")):
            request = ExecuteNodeRequest(
                node_name="test_node",
                node_metadata={"node_type": "SomeNodeType", "library": "some_library"},
            )
            result = await node_manager.on_execute_node_request(request)

        assert isinstance(result, ExecuteNodeResultFailure)
        details = str(result.result_details)
        assert "test_node" in details
        assert "SomeNodeType" in details
        # The library itself is LOADED, so this is one broken node type -- not a library that
        # could not start. The underlying error is the useful thing to report.
        assert "library not loaded" in details
        assert "could not start it up" not in details

    @pytest.mark.asyncio
    async def test_a_library_that_never_loaded_explains_itself_without_the_raw_error(self) -> None:
        """When the worker could not load the library, say that -- and not "not found".

        "Library 'X' not found" is what LibraryRegistry raises, and it is actively misleading
        here: the orchestrator holds that library and is drawing its nodes, so it sends the reader
        hunting for something that is right in front of them. The cause belongs in the log.
        """
        mock_obj_mgr = _make_mock_obj_mgr(existing_node=None)
        lib_mgr = _make_mock_library_manager(is_worker=True, library_loaded=False)
        node_manager = _make_node_manager(object_manager=mock_obj_mgr, library_manager=lib_mgr)

        with patch(_LIBRARY_REGISTRY_CREATE_NODE_PATH, side_effect=RuntimeError("Library 'some_library' not found")):
            request = ExecuteNodeRequest(
                node_name="test_node",
                node_metadata={"node_type": "SomeNodeType", "library": "some_library"},
            )
            result = await node_manager.on_execute_node_request(request)

        details = str(result.result_details)
        assert "could not start it up" in details
        assert "Editing the node still works" in details
        assert "not found" not in details


class TestExecuteNodeWorkerRoute:
    """Orchestrator-side worker routing for ExecuteNodeRequest.

    When the library is owned by a worker and we're on the orchestrator, the
    handler routes the ExecuteNodeRequest to the worker and returns the result
    without calling aprocess locally.
    """

    def _make_mock_node(self, name: str = "worker_node") -> MagicMock:
        node = MagicMock(spec=BaseNode)
        node.name = name
        node.aprocess = AsyncMock()
        node.metadata = {"library": "worker_library"}
        node.parameter_values = {}
        node.parameter_output_values = {}
        return node

    def _make_mock_obj_mgr(self, existing_node: MagicMock | None = None) -> MagicMock:
        mock_obj_mgr = MagicMock()
        mock_obj_mgr.attempt_get_object_by_name_as_type.return_value = existing_node
        return mock_obj_mgr

    @pytest.mark.asyncio
    async def test_routes_to_worker_and_returns_worker_result(self) -> None:
        mock_node = self._make_mock_node()
        mock_obj_mgr = self._make_mock_obj_mgr(existing_node=mock_node)

        wm = MagicMock()
        wm.route_to_worker = AsyncMock(
            return_value={
                "result_type": ExecuteNodeResultSuccess.__name__,
                "result": {"parameter_output_values": {"out": 42}, "result_details": "ok"},
            }
        )
        lib_mgr = MagicMock()
        lib_mgr.is_worker = False
        lib_mgr._is_worker = False
        lib_mgr.wait_for_worker_library_load = AsyncMock()
        lib_mgr.get_worker_for_library.return_value = ("eng-id", "topic")

        node_manager = _make_node_manager(object_manager=mock_obj_mgr, library_manager=lib_mgr, worker_manager=wm)

        request = ExecuteNodeRequest(
            node_name="worker_node",
            node_metadata=cast("NodeMetadata", {"node_type": "WorkerNode", "library": "worker_library"}),
        )
        result = await node_manager.on_execute_node_request(request)

        assert isinstance(result, ExecuteNodeResultSuccess)
        assert result.parameter_output_values == {"out": 42}
        wm.route_to_worker.assert_awaited_once()
        # The orchestrator stub must not have run aprocess; the worker did.
        mock_node.aprocess.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_worker_failure_returns_failure(self) -> None:
        mock_node = self._make_mock_node()
        mock_obj_mgr = self._make_mock_obj_mgr(existing_node=mock_node)

        wm = MagicMock()
        wm.route_to_worker = AsyncMock(
            return_value={
                "result_type": ExecuteNodeResultFailure.__name__,
                "result": {"result_details": "worker exploded"},
            }
        )
        lib_mgr = MagicMock()
        lib_mgr.is_worker = False
        lib_mgr._is_worker = False
        lib_mgr.wait_for_worker_library_load = AsyncMock()
        lib_mgr.get_worker_for_library.return_value = ("eng-id", "topic")

        node_manager = _make_node_manager(object_manager=mock_obj_mgr, library_manager=lib_mgr, worker_manager=wm)

        request = ExecuteNodeRequest(
            node_name="worker_node",
            node_metadata=cast("NodeMetadata", {"node_type": "WorkerNode", "library": "worker_library"}),
        )
        result = await node_manager.on_execute_node_request(request)

        assert isinstance(result, ExecuteNodeResultFailure)
        assert "worker exploded" in str(result.result_details)
        mock_node.aprocess.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_worker_subprocess_runs_locally_without_re_routing(self) -> None:
        """Worker subprocess runs the node locally without re-routing.

        Inside the worker subprocess itself (_is_worker=True), the handler must
        run the node locally even if WorkerManager happens to be available. The
        worker constructs the node from metadata; it does not consult
        ObjectManager at all, so any existing entry there is irrelevant.
        """
        mock_node = self._make_mock_node()
        mock_obj_mgr = self._make_mock_obj_mgr(existing_node=None)

        wm = MagicMock()
        wm.route_to_worker = AsyncMock()
        lib_mgr = MagicMock()
        lib_mgr.is_worker = True
        lib_mgr._is_worker = True
        lib_mgr.get_worker_for_library.return_value = ("eng-id", "topic")

        node_manager = _make_node_manager(object_manager=mock_obj_mgr, library_manager=lib_mgr, worker_manager=wm)

        with patch(_LIBRARY_REGISTRY_CREATE_NODE_PATH, return_value=mock_node):
            request = ExecuteNodeRequest(
                node_name="worker_node",
                node_metadata=cast("NodeMetadata", {"node_type": "WorkerNode", "library": "worker_library"}),
            )
            result = await node_manager.on_execute_node_request(request)

        assert isinstance(result, ExecuteNodeResultSuccess)
        mock_node.aprocess.assert_awaited_once()
        wm.route_to_worker.assert_not_awaited()
