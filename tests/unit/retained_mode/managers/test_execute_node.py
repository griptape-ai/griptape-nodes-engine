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
from griptape_nodes.retained_mode.managers.node_manager import NodeManager

_LIBRARY_MANAGER_PATH = "griptape_nodes.retained_mode.managers.node_manager.GriptapeNodes.LibraryManager"
_WORKER_MANAGER_PATH = "griptape_nodes.retained_mode.managers.node_manager.GriptapeNodes.WorkerManager"
_OBJECT_MANAGER_PATH = "griptape_nodes.retained_mode.managers.node_manager.GriptapeNodes.ObjectManager"
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


def _make_mock_library_manager(*, is_worker: bool) -> MagicMock:
    lib_mgr = MagicMock()
    lib_mgr.is_worker = is_worker
    lib_mgr.get_worker_for_library.return_value = None
    return lib_mgr


class TestExecuteNodeOrchestratorPath:
    """Orchestrator side of ExecuteNodeRequest.

    On the orchestrator, ObjectManager is the source of truth for node identity.
    A lookup miss is a hard failure -- we do not fabricate a fresh node from
    metadata, because doing so would mask real "node dropped from the live map"
    bugs with a stub that has no connections or flow parentage.
    """

    def _get_node_manager(self) -> NodeManager:
        from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

        return GriptapeNodes.NodeManager()

    @pytest.mark.asyncio
    async def test_missing_node_fails_without_fallback_to_create(self) -> None:
        """Orchestrator lookup miss returns failure; LibraryRegistry.create_node NOT called."""
        node_manager = self._get_node_manager()
        mock_obj_mgr = _make_mock_obj_mgr(existing_node=None)
        lib_mgr = _make_mock_library_manager(is_worker=False)

        with (
            patch(_OBJECT_MANAGER_PATH, return_value=mock_obj_mgr),
            patch(_LIBRARY_MANAGER_PATH, return_value=lib_mgr),
            patch(_LIBRARY_REGISTRY_CREATE_NODE_PATH) as mock_create,
        ):
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
        node_manager = self._get_node_manager()
        mock_node = _make_mock_node()
        mock_obj_mgr = _make_mock_obj_mgr(existing_node=mock_node)
        lib_mgr = _make_mock_library_manager(is_worker=False)

        with (
            patch(_OBJECT_MANAGER_PATH, return_value=mock_obj_mgr),
            patch(_LIBRARY_MANAGER_PATH, return_value=lib_mgr),
            patch(_LIBRARY_REGISTRY_CREATE_NODE_PATH) as mock_create,
        ):
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
        node_manager = self._get_node_manager()
        mock_node = _make_mock_node()
        mock_obj_mgr = _make_mock_obj_mgr(existing_node=mock_node)
        lib_mgr = _make_mock_library_manager(is_worker=False)

        with (
            patch(_OBJECT_MANAGER_PATH, return_value=mock_obj_mgr),
            patch(_LIBRARY_MANAGER_PATH, return_value=lib_mgr),
        ):
            request = ExecuteNodeRequest(node_name="test_node", node_metadata=cast("NodeMetadata", {"node_type": "T"}))
            result = await node_manager.on_execute_node_request(request)

        assert isinstance(result, ExecuteNodeResultSuccess)
        mock_node.set_parameter_value.assert_not_called()
        mock_node.aprocess.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_set_parameter_fails(self) -> None:
        node_manager = self._get_node_manager()
        mock_node = _make_mock_node()
        mock_node.set_parameter_value.side_effect = ValueError("bad value")
        mock_obj_mgr = _make_mock_obj_mgr(existing_node=mock_node)
        lib_mgr = _make_mock_library_manager(is_worker=False)

        with (
            patch(_OBJECT_MANAGER_PATH, return_value=mock_obj_mgr),
            patch(_LIBRARY_MANAGER_PATH, return_value=lib_mgr),
        ):
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
        node_manager = self._get_node_manager()
        mock_node = _make_mock_node()
        mock_node.aprocess.side_effect = RuntimeError("process exploded")
        mock_obj_mgr = _make_mock_obj_mgr(existing_node=mock_node)
        lib_mgr = _make_mock_library_manager(is_worker=False)

        with (
            patch(_OBJECT_MANAGER_PATH, return_value=mock_obj_mgr),
            patch(_LIBRARY_MANAGER_PATH, return_value=lib_mgr),
        ):
            request = ExecuteNodeRequest(node_name="test_node")
            result = await node_manager.on_execute_node_request(request)

        assert isinstance(result, ExecuteNodeResultFailure)
        assert "process exploded" in str(result.result_details)

    @pytest.mark.asyncio
    async def test_multiple_params(self) -> None:
        node_manager = self._get_node_manager()
        mock_node = _make_mock_node()
        mock_obj_mgr = _make_mock_obj_mgr(existing_node=mock_node)
        lib_mgr = _make_mock_library_manager(is_worker=False)

        with (
            patch(_OBJECT_MANAGER_PATH, return_value=mock_obj_mgr),
            patch(_LIBRARY_MANAGER_PATH, return_value=lib_mgr),
        ):
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
        node_manager = self._get_node_manager()
        mock_node = _make_mock_node()
        # Pre-populate parameter_values so each hydrate lookup finds a match.
        mock_node.parameter_values = {"param_a": 1, "param_b": "two", "param_c": [3]}
        mock_obj_mgr = _make_mock_obj_mgr(existing_node=mock_node)
        lib_mgr = _make_mock_library_manager(is_worker=False)

        with (
            patch(_OBJECT_MANAGER_PATH, return_value=mock_obj_mgr),
            patch(_LIBRARY_MANAGER_PATH, return_value=lib_mgr),
        ):
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
        node_manager = self._get_node_manager()
        mock_node = _make_mock_node()
        mock_node.parameter_values = {"param_a": 1}  # existing, but stale
        mock_obj_mgr = _make_mock_obj_mgr(existing_node=mock_node)
        lib_mgr = _make_mock_library_manager(is_worker=False)

        with (
            patch(_OBJECT_MANAGER_PATH, return_value=mock_obj_mgr),
            patch(_LIBRARY_MANAGER_PATH, return_value=lib_mgr),
        ):
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

    def _get_node_manager(self) -> NodeManager:
        from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

        return GriptapeNodes.NodeManager()

    @pytest.mark.asyncio
    async def test_constructs_fresh_node_and_executes(self) -> None:
        """Worker path: no prior ObjectManager entry, construct from metadata, run."""
        node_manager = self._get_node_manager()
        mock_node = _make_mock_node()
        mock_obj_mgr = _make_mock_obj_mgr(existing_node=None)
        lib_mgr = _make_mock_library_manager(is_worker=True)

        with (
            patch(_OBJECT_MANAGER_PATH, return_value=mock_obj_mgr),
            patch(_LIBRARY_MANAGER_PATH, return_value=lib_mgr),
            patch(_LIBRARY_REGISTRY_CREATE_NODE_PATH, return_value=mock_node) as mock_create,
        ):
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
        node_manager = self._get_node_manager()
        mock_node = _make_mock_node()
        mock_obj_mgr = _make_mock_obj_mgr(existing_node=None)
        lib_mgr = _make_mock_library_manager(is_worker=True)

        with (
            patch(_OBJECT_MANAGER_PATH, return_value=mock_obj_mgr),
            patch(_LIBRARY_MANAGER_PATH, return_value=lib_mgr),
            patch(_LIBRARY_REGISTRY_CREATE_NODE_PATH, return_value=mock_node),
        ):
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
        node_manager = self._get_node_manager()
        stale_node = _make_mock_node(name="stale")
        fresh_node = _make_mock_node(name="test_node")
        mock_obj_mgr = _make_mock_obj_mgr(existing_node=stale_node)
        lib_mgr = _make_mock_library_manager(is_worker=True)

        with (
            patch(_OBJECT_MANAGER_PATH, return_value=mock_obj_mgr),
            patch(_LIBRARY_MANAGER_PATH, return_value=lib_mgr),
            patch(_LIBRARY_REGISTRY_CREATE_NODE_PATH, return_value=fresh_node) as mock_create,
        ):
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
        node_manager = self._get_node_manager()
        mock_obj_mgr = _make_mock_obj_mgr(existing_node=None)
        lib_mgr = _make_mock_library_manager(is_worker=True)

        with (
            patch(_OBJECT_MANAGER_PATH, return_value=mock_obj_mgr),
            patch(_LIBRARY_MANAGER_PATH, return_value=lib_mgr),
        ):
            request = ExecuteNodeRequest(node_name="nonexistent_node")
            result = await node_manager.on_execute_node_request(request)

        assert isinstance(result, ExecuteNodeResultFailure)
        assert "nonexistent_node" in str(result.result_details)

    @pytest.mark.asyncio
    async def test_missing_node_type_in_metadata_fails(self) -> None:
        node_manager = self._get_node_manager()
        mock_obj_mgr = _make_mock_obj_mgr(existing_node=None)
        lib_mgr = _make_mock_library_manager(is_worker=True)

        with (
            patch(_OBJECT_MANAGER_PATH, return_value=mock_obj_mgr),
            patch(_LIBRARY_MANAGER_PATH, return_value=lib_mgr),
        ):
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
        node_manager = self._get_node_manager()
        mock_obj_mgr = _make_mock_obj_mgr(existing_node=None)
        lib_mgr = _make_mock_library_manager(is_worker=True)

        with (
            patch(_OBJECT_MANAGER_PATH, return_value=mock_obj_mgr),
            patch(_LIBRARY_MANAGER_PATH, return_value=lib_mgr),
            patch(_LIBRARY_REGISTRY_CREATE_NODE_PATH, side_effect=RuntimeError("library not loaded")),
        ):
            request = ExecuteNodeRequest(
                node_name="test_node",
                node_metadata={"node_type": "SomeNodeType", "library": "some_library"},
            )
            result = await node_manager.on_execute_node_request(request)

        assert isinstance(result, ExecuteNodeResultFailure)
        assert "test_node" in str(result.result_details)
        assert "SomeNodeType" in str(result.result_details)
        assert "library not loaded" in str(result.result_details)


class TestExecuteNodeWorkerRoute:
    """Orchestrator-side worker routing for ExecuteNodeRequest.

    When the library is owned by a worker and we're on the orchestrator, the
    handler routes the ExecuteNodeRequest to the worker and returns the result
    without calling aprocess locally.
    """

    def _get_node_manager(self) -> NodeManager:
        from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

        return GriptapeNodes.NodeManager()

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
        node_manager = self._get_node_manager()
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
        lib_mgr.get_worker_for_library.return_value = ("eng-id", "topic")

        with (
            patch(_OBJECT_MANAGER_PATH, return_value=mock_obj_mgr),
            patch(_LIBRARY_MANAGER_PATH, return_value=lib_mgr),
            patch(_WORKER_MANAGER_PATH, return_value=wm),
        ):
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
        node_manager = self._get_node_manager()
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
        lib_mgr.get_worker_for_library.return_value = ("eng-id", "topic")

        with (
            patch(_OBJECT_MANAGER_PATH, return_value=mock_obj_mgr),
            patch(_LIBRARY_MANAGER_PATH, return_value=lib_mgr),
            patch(_WORKER_MANAGER_PATH, return_value=wm),
        ):
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
        node_manager = self._get_node_manager()
        mock_node = self._make_mock_node()
        mock_obj_mgr = self._make_mock_obj_mgr(existing_node=None)

        wm = MagicMock()
        wm.route_to_worker = AsyncMock()
        lib_mgr = MagicMock()
        lib_mgr.is_worker = True
        lib_mgr.get_worker_for_library.return_value = ("eng-id", "topic")

        with (
            patch(_OBJECT_MANAGER_PATH, return_value=mock_obj_mgr),
            patch(_LIBRARY_MANAGER_PATH, return_value=lib_mgr),
            patch(_WORKER_MANAGER_PATH, return_value=wm),
            patch(_LIBRARY_REGISTRY_CREATE_NODE_PATH, return_value=mock_node),
        ):
            request = ExecuteNodeRequest(
                node_name="worker_node",
                node_metadata=cast("NodeMetadata", {"node_type": "WorkerNode", "library": "worker_library"}),
            )
            result = await node_manager.on_execute_node_request(request)

        assert isinstance(result, ExecuteNodeResultSuccess)
        mock_node.aprocess.assert_awaited_once()
        wm.route_to_worker.assert_not_awaited()
