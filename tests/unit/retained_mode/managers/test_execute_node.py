from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from griptape.artifacts import ImageUrlArtifact

from griptape_nodes.common.isolated_execution_scope import isolated_execution_scope
from griptape_nodes.exe_types import node_types
from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
from griptape_nodes.exe_types.node_groups.base_node_group import BaseNodeGroup
from griptape_nodes.exe_types.node_types import BaseNode, DataNode, EndNode, StartNode
from griptape_nodes.node_library.library_registry import LibraryMetadata, LibraryRegistry, LibrarySchema, NodeMetadata
from griptape_nodes.retained_mode.events.connection_events import DeleteConnectionRequest
from griptape_nodes.retained_mode.events.execution_events import (
    ExecuteNodeRequest,
    ExecuteNodeResultFailure,
    ExecuteNodeResultSuccess,
)
from griptape_nodes.retained_mode.events.flow_events import (
    PACKAGED_FLOW_BOUNDARY_KEY,
    CreateFlowRequest,
    CreateFlowResultSuccess,
)
from griptape_nodes.retained_mode.events.node_events import CreateNodeRequest, CreateNodeResultSuccess
from griptape_nodes.retained_mode.events.parameter_events import (
    AddParameterToNodeRequest,
    AddParameterToNodeResultSuccess,
    AlterParameterDetailsRequest,
    SetParameterValueRequest,
)
from griptape_nodes.retained_mode.events.worker_events import WorkerGoneError
from griptape_nodes.retained_mode.managers.event_manager import EventManager
from griptape_nodes.retained_mode.managers.library_manager import LibraryManager
from griptape_nodes.retained_mode.managers.node_manager import NodeManager
from griptape_nodes.serialization.values import UndecodedValue, encode_value
from tests.simulated_worker import simulated_worker

if TYPE_CHECKING:
    from collections.abc import Iterator

    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.events.execution_events import NodeMetadata as ExecutionNodeMetadata


def _make_mock_node(name: str = "test_node") -> MagicMock:
    node = MagicMock(spec=BaseNode)
    node.name = name
    node.aprocess = AsyncMock()
    node.parameter_values = {}
    node.parameter_output_values = {"output_param": "output_value"}
    node.metadata = {}
    # A mock's default return value is truthy, and the executor reads this hook as "reasons not to run".
    node.validate_in_execution_environment = MagicMock(return_value=None)
    # Nothing here is a list, so no value becomes rows.
    node._list_rows = MagicMock()
    node._list_rows.set_as_rows_if_unconnected.return_value = False
    return node


class _RoutedNode(DataNode):
    """A node whose library runs in a worker, as the orchestrator holds it."""

    def __init__(self, name: str, metadata: dict | None = None) -> None:
        super().__init__(name, metadata)
        self.add_parameter(
            Parameter(
                name="handle", type="any", tooltip="", allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY}
            )
        )

    def process(self) -> None:
        pass


def _make_routed_node(
    node_type: type[BaseNode] = _RoutedNode, *, name: str = "worker_node", library: str = "worker_library"
) -> Any:
    """A real node, since the orchestrator serializes the structure of a node it forwards. Its aprocess is a mock."""
    node = node_type(name=name, metadata={"library": library, "node_type": node_type.__name__})
    node.aprocess = AsyncMock()  # type: ignore[method-assign]
    return node


def _make_mock_obj_mgr(existing_node: MagicMock | BaseNode | None = None) -> MagicMock:
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
    lib_mgr.workers.get_worker_for_library.return_value = None
    # The execute path awaits this before consulting get_worker_for_library, so a
    # plain MagicMock attribute is not awaitable and fails the call.
    if library_loaded:
        lib_mgr.get_library_info_by_library_name.return_value.lifecycle_state = (
            LibraryManager.LibraryLifecycleState.LOADED
        )
    else:
        lib_mgr.get_library_info_by_library_name.return_value.lifecycle_state = (
            LibraryManager.LibraryLifecycleState.EVALUATED
        )
        # The name the code actually calls; configuring the other one left the reason unexercised.
        lib_mgr.catalog.get_collated_problems_for_library.return_value = (
            "Dependency installation failed: no solution found"
        )
    return lib_mgr


def _make_node_manager(
    *,
    object_manager: MagicMock | None = None,
    library_manager: MagicMock | None = None,
    worker_manager: MagicMock | None = None,
    event_manager: EventManager | None = None,
) -> NodeManager:
    """Build a NodeManager wired to a mock engine instead of the process-wide facade.

    Pass a real `event_manager` to observe the node-execution scope; a mock one answers
    `in_node_execution()` with a truthy mock whether the scope was opened or not.
    """
    mock_engine = MagicMock()
    mock_engine.object_manager = object_manager
    mock_engine.library_manager = library_manager
    if event_manager is not None:
        mock_engine.event_manager = event_manager
    # Engine always has a WorkerManager, so a test that leaves it None describes a shape the engine
    # cannot be in. Routing awaits it, so it needs to be awaitable.
    worker_manager = worker_manager or MagicMock()
    worker_manager.wait_until_executable = AsyncMock()
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

        with patch.object(LibraryRegistry, "create_node") as mock_create:
            request = ExecuteNodeRequest(
                node_name="nonexistent_node",
                node_metadata=cast("ExecutionNodeMetadata", {"node_type": "SomeNodeType", "library": "some_library"}),
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

        with patch.object(LibraryRegistry, "create_node") as mock_create:
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
    async def test_a_node_runs_inside_the_execution_scope(self) -> None:
        """The scope is what holds a release hook back, so a node running here has to open it too.

        An in-process library can hold an object a parallel node is using, and only the scope keeps a
        "clear cache" node from freeing it mid-run.
        """
        event_manager = EventManager()
        mock_node = _make_mock_node()
        scope_open_during_run: list[bool] = []
        mock_node.aprocess = AsyncMock(
            side_effect=lambda: scope_open_during_run.append(event_manager.in_node_execution())
        )
        node_manager = _make_node_manager(
            object_manager=_make_mock_obj_mgr(existing_node=mock_node),
            library_manager=_make_mock_library_manager(is_worker=False),
            event_manager=event_manager,
        )

        request = ExecuteNodeRequest(
            node_name="test_node", node_metadata=cast("ExecutionNodeMetadata", {"node_type": "T"})
        )
        result = await node_manager.on_execute_node_request(request)

        assert isinstance(result, ExecuteNodeResultSuccess)
        assert scope_open_during_run == [True]
        assert event_manager.in_node_execution() is False

    @pytest.mark.asyncio
    async def test_no_params(self) -> None:
        mock_node = _make_mock_node()
        mock_obj_mgr = _make_mock_obj_mgr(existing_node=mock_node)
        lib_mgr = _make_mock_library_manager(is_worker=False)
        node_manager = _make_node_manager(object_manager=mock_obj_mgr, library_manager=lib_mgr)

        request = ExecuteNodeRequest(
            node_name="test_node", node_metadata=cast("ExecutionNodeMetadata", {"node_type": "T"})
        )
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

    @pytest.mark.asyncio
    async def test_untagged_artifact_dict_becomes_an_artifact(self) -> None:
        """An editor-set or older-saved-workflow dict shaped like an artifact hydrates before being applied."""
        mock_node = _make_mock_node()
        mock_obj_mgr = _make_mock_obj_mgr(existing_node=mock_node)
        lib_mgr = _make_mock_library_manager(is_worker=False)
        node_manager = _make_node_manager(object_manager=mock_obj_mgr, library_manager=lib_mgr)

        request = ExecuteNodeRequest(
            node_name="test_node",
            parameter_values={"image": {"type": "ImageUrlArtifact", "value": "https://example.com/a.png"}},
        )
        result = await node_manager.on_execute_node_request(request)

        assert isinstance(result, ExecuteNodeResultSuccess)
        set_name, set_value = mock_node.set_parameter_value.call_args[0]
        assert set_name == "image"
        assert isinstance(set_value, ImageUrlArtifact)
        assert set_value.value == "https://example.com/a.png"


class _ImageInputDataNode(DataNode):
    """DataNode with one image-typed input, recording what process() actually saw."""

    def __init__(self, name: str, metadata: dict | None = None) -> None:
        super().__init__(name, metadata)
        self.add_parameter(
            Parameter(
                name="image",
                type="ImageUrlArtifact",
                default_value=None,
                tooltip="",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.seen_image: object = None

    def process(self) -> None:
        self.seen_image = self.parameter_values.get("image")


class TestExecuteNodeLocalRouteHydratesUntaggedDicts:
    """Local-route regression: an untagged artifact dict from the editor reaches process() as the artifact."""

    @pytest.mark.asyncio
    async def test_data_node_sees_hydrated_artifact_not_dict(self) -> None:
        node = _ImageInputDataNode(
            "image_node", metadata={"node_type": "ImageInputDataNode", "library": "some_library"}
        )
        mock_obj_mgr = _make_mock_obj_mgr(existing_node=node)
        lib_mgr = _make_mock_library_manager(is_worker=False)
        node_manager = _make_node_manager(object_manager=mock_obj_mgr, library_manager=lib_mgr)

        request = ExecuteNodeRequest(
            node_name="image_node",
            parameter_values={"image": {"type": "ImageUrlArtifact", "value": "https://example.com/a.png"}},
        )
        result = await node_manager.on_execute_node_request(request)

        assert isinstance(result, ExecuteNodeResultSuccess)
        assert isinstance(node.seen_image, ImageUrlArtifact)
        assert node.seen_image.value == "https://example.com/a.png"


_LIBRARY_NAME = "execute-node-test-library"


class _EchoNode(DataNode):
    """Echoes its input and reports the parameters and identity of the instance that ran."""

    def __init__(self, name: str, metadata: dict | None = None) -> None:
        super().__init__(name, metadata)
        for parameter_name in ("text", "payload"):
            self.add_parameter(
                Parameter(
                    name=parameter_name,
                    type="str" if parameter_name == "text" else "any",
                    default_value="",
                    tooltip="",
                    allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
                )
            )
        for output_name, output_type in (("echo", "str"), ("parameter_names", "list"), ("source", "str")):
            self.add_parameter(
                Parameter(name=output_name, type=output_type, tooltip="", allowed_modes={ParameterMode.OUTPUT})
            )

    def process(self) -> None:
        self.parameter_output_values["echo"] = self.get_parameter_value("text")
        self.parameter_output_values["parameter_names"] = sorted(parameter.name for parameter in self.parameters)
        self.parameter_output_values["source"] = self.local_object_source


class _OutputsAnUnsendableValueNode(DataNode):
    """Outputs a value with no plain-data form beside one that has one."""

    def __init__(self, name: str, metadata: dict | None = None) -> None:
        super().__init__(name, metadata)
        self.add_parameter(Parameter(name="handle", type="any", tooltip="", allowed_modes={ParameterMode.OUTPUT}))
        self.add_parameter(Parameter(name="text", type="str", tooltip="", allowed_modes={ParameterMode.OUTPUT}))

    def process(self) -> None:
        self.parameter_output_values["handle"] = _NoPlainDataForm()
        self.parameter_output_values["text"] = "fine"


class _RefusesAValueNode(_EchoNode):
    """Refuses one value of `text`, however it is set."""

    REFUSED = "refused"

    def before_value_set(self, parameter: Parameter, value: Any) -> Any:
        if parameter.name == "text" and value == self.REFUSED:
            msg = f"'{self.REFUSED}' is not allowed"
            raise ValueError(msg)
        return super().before_value_set(parameter, value)


class _PlainGroup(BaseNodeGroup):
    """A plain visual grouping, with no subflow."""

    def run(self) -> None:
        pass

    def initialize(self) -> None:
        pass

    def process(self) -> None:
        return None


class _NoPlainDataForm:
    """A value the value codec cannot encode."""


@pytest.fixture
def library_name() -> Iterator[str]:
    """Register this module's nodes as a library. LibraryRegistry is process-global, so it is cleared around the test."""
    LibraryRegistry._clear()
    schema = LibrarySchema(
        name=_LIBRARY_NAME,
        library_schema_version=LibrarySchema.LATEST_SCHEMA_VERSION,
        metadata=LibraryMetadata(
            author="test",
            description="ExecuteNodeRequest fixtures",
            library_version="1.0.0",
            engine_version="1.0.0",
            tags=[],
        ),
        categories=[],
        nodes=[],
    )
    library = LibraryRegistry.generate_new_library(library_data=schema)
    for node_class in (_EchoNode, _RefusesAValueNode, _OutputsAnUnsendableValueNode, _PlainGroup):
        library.register_new_node_type(
            node_class, NodeMetadata(category="test", description="", display_name=node_class.__name__)
        )
    yield _LIBRARY_NAME
    LibraryRegistry._clear()


@pytest.fixture
def worker_engine(engine: Engine, library_name: str) -> Engine:  # noqa: ARG001
    """The test's engine, in the role of a worker that runs this module's library."""
    engine.library_manager._is_worker = True
    return engine


def _request_for(node_type: str, name: str = "Echo", **fields: Any) -> ExecuteNodeRequest:
    return ExecuteNodeRequest(
        node_name=name,
        node_metadata=cast("ExecutionNodeMetadata", {"node_type": node_type, "library": _LIBRARY_NAME}),
        **fields,
    )


def _what_the_worker_holds(engine: Engine) -> set[str]:
    """The names of every object left in the worker's object store."""
    return set(engine.object_manager.get_filtered_subset())


_WORKER_COPIES_FLOW = "__worker_node_copies__"


class TestTheWorkerLoadsACopy:
    """Worker side of ExecuteNodeRequest: a copy loaded from the request into the worker's own graph, run, discarded.

    Nothing persists between requests: the orchestrator is the single source of truth for the node's
    structure and values, and sends them with every request.
    """

    @pytest.mark.asyncio
    async def test_runs_a_copy_loaded_with_the_values_it_was_sent(self, worker_engine: Engine) -> None:
        result = await worker_engine.ahandle_request(
            _request_for("_EchoNode", parameter_values={"text": "hello"}, local_object_source="Echo@abc")
        )

        assert isinstance(result, ExecuteNodeResultSuccess), result.result_details
        assert result.parameter_output_values["echo"] == "hello"
        # The copy caches under the orchestrator's identity, so a run displaces its own earlier objects.
        assert result.parameter_output_values["source"] == "Echo@abc"

    @pytest.mark.asyncio
    async def test_the_copy_and_its_flow_are_gone_after_the_run(self, worker_engine: Engine) -> None:
        result = await worker_engine.ahandle_request(_request_for("_EchoNode", parameter_values={"text": "x"}))

        assert isinstance(result, ExecuteNodeResultSuccess), result.result_details
        assert _what_the_worker_holds(worker_engine) == {_WORKER_COPIES_FLOW}
        assert "Echo" not in worker_engine.node_manager._name_to_parent_flow_name

    @pytest.mark.asyncio
    async def test_the_structure_it_was_sent_is_built_before_the_values_are_set(self, worker_engine: Engine) -> None:
        """A parameter the class does not declare arrives as a command, and its value lands on it."""
        add_parameter = AddParameterToNodeRequest(
            node_name="Echo",
            parameter_name="added_in_the_editor",
            type="str",
            default_value="",
            tooltip="",
            initial_setup=True,
        )

        result = await worker_engine.ahandle_request(
            _request_for(
                "_EchoNode",
                element_modification_commands=[add_parameter],
                parameter_values={"added_in_the_editor": "kept"},
            )
        )

        assert isinstance(result, ExecuteNodeResultSuccess), result.result_details
        assert "added_in_the_editor" in result.parameter_output_values["parameter_names"]

    @pytest.mark.asyncio
    async def test_a_value_for_a_parameter_the_copy_lacks_is_skipped_with_a_warning(
        self, worker_engine: Engine, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="griptape_nodes"):
            result = await worker_engine.ahandle_request(
                _request_for("_EchoNode", parameter_values={"text": "ran", "ghost": "orphaned"})
            )

        assert isinstance(result, ExecuteNodeResultSuccess), result.result_details
        assert result.parameter_output_values["echo"] == "ran"
        assert "'ghost'" in caplog.text

    @pytest.mark.asyncio
    async def test_a_value_this_process_cannot_rebuild_is_set_and_warned(
        self, worker_engine: Engine, caplog: pytest.LogCaptureFixture
    ) -> None:
        undecoded = UndecodedValue({"$type": "other_library.mod:Thing"}, "its library is not loaded here")

        with caplog.at_level(logging.WARNING, logger="griptape_nodes"):
            result = await worker_engine.ahandle_request(
                _request_for("_EchoNode", parameter_values={"payload": undecoded})
            )

        assert isinstance(result, ExecuteNodeResultSuccess), result.result_details
        assert "'payload'" in caplog.text
        assert "its library is not loaded here" in caplog.text

    @pytest.mark.asyncio
    async def test_a_command_of_another_kind_fails_before_anything_is_created(self, worker_engine: Engine) -> None:
        """Only the element requests a save writes belong in a node's structure."""
        stray = DeleteConnectionRequest(
            source_node_name="Echo",
            source_parameter_name="echo",
            target_node_name="Elsewhere",
            target_parameter_name="text",
        )

        with patch.object(LibraryRegistry, "create_node", wraps=LibraryRegistry.create_node) as create_node:
            result = await worker_engine.ahandle_request(
                _request_for("_EchoNode", element_modification_commands=[stray])
            )

        assert isinstance(result, ExecuteNodeResultFailure)
        assert "DeleteConnectionRequest" in str(result.result_details)
        create_node.assert_not_called()
        assert _what_the_worker_holds(worker_engine) == {_WORKER_COPIES_FLOW}

    @pytest.mark.asyncio
    async def test_a_structure_command_that_fails_fails_the_run_and_leaves_nothing(self, worker_engine: Engine) -> None:
        alter_missing = AlterParameterDetailsRequest(node_name="Echo", parameter_name="no_such_parameter", type="int")

        result = await worker_engine.ahandle_request(
            _request_for("_EchoNode", element_modification_commands=[alter_missing])
        )

        assert isinstance(result, ExecuteNodeResultFailure)
        assert "Attempted to prepare node 'Echo'" in str(result.result_details)
        assert _what_the_worker_holds(worker_engine) == {_WORKER_COPIES_FLOW}

    @pytest.mark.asyncio
    async def test_a_value_the_copy_refuses_fails_the_run_and_leaves_nothing(self, worker_engine: Engine) -> None:
        result = await worker_engine.ahandle_request(
            _request_for("_RefusesAValueNode", parameter_values={"text": _RefusesAValueNode.REFUSED})
        )

        assert isinstance(result, ExecuteNodeResultFailure)
        assert "Attempted to set parameter 'text' on node 'Echo'" in str(result.result_details)
        assert _what_the_worker_holds(worker_engine) == {_WORKER_COPIES_FLOW}

    def test_discarding_a_flow_that_is_already_gone_does_nothing(self, worker_engine: Engine) -> None:
        worker_engine.node_manager._discard_worker_copy("__worker_node_copies___gone")

        assert _what_the_worker_holds(worker_engine) == set()

    @pytest.mark.asyncio
    async def test_a_node_group_is_refused(self, worker_engine: Engine) -> None:
        result = await worker_engine.ahandle_request(_request_for("_PlainGroup", name="Group"))

        assert isinstance(result, ExecuteNodeResultFailure)
        assert "node group" in str(result.result_details)
        assert _what_the_worker_holds(worker_engine) == {_WORKER_COPIES_FLOW}

    @pytest.mark.asyncio
    async def test_a_copy_that_cannot_take_the_nodes_name_is_refused(self, worker_engine: Engine) -> None:
        """Requests the node's code sends name the orchestrator's node, so a renamed copy would answer for nothing."""
        worker_engine.object_manager.add_object_by_name("Echo", object())

        result = await worker_engine.ahandle_request(_request_for("_EchoNode"))

        assert isinstance(result, ExecuteNodeResultFailure)
        assert "'Echo_1'" in str(result.result_details)

    @pytest.mark.asyncio
    async def test_missing_metadata_fails(self, worker_engine: Engine) -> None:
        result = await worker_engine.ahandle_request(ExecuteNodeRequest(node_name="nonexistent_node"))

        assert isinstance(result, ExecuteNodeResultFailure)
        assert "nonexistent_node" in str(result.result_details)

    @pytest.mark.asyncio
    async def test_missing_node_type_in_metadata_fails(self, worker_engine: Engine) -> None:
        result = await worker_engine.ahandle_request(
            ExecuteNodeRequest(
                node_name="some_node", node_metadata=cast("ExecutionNodeMetadata", {"library": _LIBRARY_NAME})
            )
        )

        assert isinstance(result, ExecuteNodeResultFailure)
        assert "node_type" in str(result.result_details)

    @pytest.mark.asyncio
    async def test_a_node_type_that_cannot_be_created_reports_why(self, worker_engine: Engine) -> None:
        result = await worker_engine.ahandle_request(_request_for("NoSuchNodeType", name="test_node"))

        assert isinstance(result, ExecuteNodeResultFailure)
        details = str(result.result_details)
        assert "test_node" in details
        assert "NoSuchNodeType" in details
        # This process holds the library, so this is one broken node type, not a library that could not start.
        assert "could not start it up" not in details

    @pytest.mark.asyncio
    async def test_a_library_that_never_loaded_explains_itself_without_the_raw_error(
        self, worker_engine: Engine
    ) -> None:
        """When the worker could not load the library, say that -- and not "not found".

        "Library 'X' not found" is actively misleading here: the orchestrator holds that library and
        is drawing its nodes, so it sends the reader hunting for something right in front of them.
        The cause belongs in the log.
        """
        library_manager = worker_engine.library_manager
        library_info = MagicMock(lifecycle_state=LibraryManager.LibraryLifecycleState.EVALUATED)
        with (
            patch.object(library_manager, "get_library_info_by_library_name", return_value=library_info),
            patch.object(
                library_manager.catalog,
                "get_collated_problems_for_library",
                return_value="Dependency installation failed: no solution found",
            ),
        ):
            result = await worker_engine.ahandle_request(
                ExecuteNodeRequest(
                    node_name="test_node",
                    node_metadata=cast(
                        "ExecutionNodeMetadata", {"node_type": "SomeNodeType", "library": "some_library"}
                    ),
                )
            )

        details = str(result.result_details)
        assert "could not start it up" in details
        assert "Editing the node still works" in details
        assert "not found" not in details

    @pytest.mark.asyncio
    async def test_output_with_no_plain_data_form_fails_on_the_worker(self, worker_engine: Engine) -> None:
        result = await worker_engine.ahandle_request(_request_for("_OutputsAnUnsendableValueNode", name="Handle"))

        assert isinstance(result, ExecuteNodeResultFailure)
        assert "'handle'" in str(result.result_details)
        assert "'text'" not in str(result.result_details)

    @pytest.mark.asyncio
    async def test_a_worker_with_a_worker_manager_still_runs_the_node_itself(self, worker_engine: Engine) -> None:
        """Inside the worker the node runs here, even though a WorkerManager exists: a worker never re-routes."""
        with patch.object(worker_engine.worker_manager, "route_to_worker", new=AsyncMock()) as route_to_worker:
            result = await worker_engine.ahandle_request(_request_for("_EchoNode", parameter_values={"text": "here"}))

        assert isinstance(result, ExecuteNodeResultSuccess), result.result_details
        route_to_worker.assert_not_awaited()


@pytest.fixture
def flow_name(engine: Engine, library_name: str) -> str:  # noqa: ARG001
    """A flow on the test's engine, in the orchestrator role, for this module's nodes."""
    engine.context_manager.push_workflow(workflow_name="execute_node_wf")
    result = engine.handle_request(CreateFlowRequest(parent_flow_name=None, flow_name="main", set_as_new_context=False))
    assert isinstance(result, CreateFlowResultSuccess), result
    return result.flow_name


def _create_echo_node(engine: Engine, flow_name: str) -> BaseNode:
    result = engine.handle_request(
        CreateNodeRequest(
            node_type="_EchoNode",
            specific_library_name=_LIBRARY_NAME,
            node_name="Echo",
            override_parent_flow_name=flow_name,
        )
    )
    assert isinstance(result, CreateNodeResultSuccess), result
    return engine.node_manager.get_node_by_name(result.node_name)


class TestPreparingTheRequestForAWorker:
    """What the orchestrator puts in an ExecuteNodeRequest before it forwards it: what a save would write."""

    def test_values_the_request_carried_are_set_on_the_live_node_first(self, engine: Engine, flow_name: str) -> None:
        node = _create_echo_node(engine, flow_name)
        request = _request_for("_EchoNode", parameter_values={"text": "from the run"})

        failure = engine.node_manager._prepare_for_worker(node, request)

        assert failure is None
        assert node.get_parameter_value("text") == "from the run"
        assert request.parameter_values["text"] == "from the run"

    def test_a_value_the_live_node_refuses_fails_before_anything_is_sent(self, engine: Engine, flow_name: str) -> None:
        result = engine.handle_request(
            CreateNodeRequest(
                node_type="_RefusesAValueNode",
                specific_library_name=_LIBRARY_NAME,
                node_name="Refuses",
                override_parent_flow_name=flow_name,
            )
        )
        assert isinstance(result, CreateNodeResultSuccess), result
        node = engine.node_manager.get_node_by_name(result.node_name)
        request = _request_for("_RefusesAValueNode", parameter_values={"text": _RefusesAValueNode.REFUSED})

        failure = engine.node_manager._prepare_for_worker(node, request)

        assert isinstance(failure, ExecuteNodeResultFailure)
        assert "'text'" in str(failure.result_details)
        assert request.element_modification_commands == []

    def test_the_values_sent_are_the_ones_the_node_stores(self, engine: Engine, flow_name: str) -> None:
        node = _create_echo_node(engine, flow_name)
        engine.handle_request(SetParameterValueRequest(node_name=node.name, parameter_name="text", value="edited"))
        request = _request_for("_EchoNode")

        engine.node_manager._prepare_for_worker(node, request)

        assert request.parameter_values == node.parameter_values

    def test_the_structure_holds_an_added_parameter_and_an_altered_one(self, engine: Engine, flow_name: str) -> None:
        node = _create_echo_node(engine, flow_name)
        added = engine.handle_request(
            AddParameterToNodeRequest(
                node_name=node.name, parameter_name="added_in_the_editor", type="str", default_value="", tooltip=""
            )
        )
        assert isinstance(added, AddParameterToNodeResultSuccess), added
        altered = engine.handle_request(
            AlterParameterDetailsRequest(node_name=node.name, parameter_name="payload", type="int")
        )
        assert altered.succeeded(), altered
        request = _request_for("_EchoNode")

        engine.node_manager._prepare_for_worker(node, request)

        commands = {
            (type(command).__name__, getattr(command, "parameter_name", None))
            for command in request.element_modification_commands
        }
        assert ("AddParameterToNodeRequest", "added_in_the_editor") in commands
        assert ("AlterParameterDetailsRequest", "payload") in commands

    def test_a_parameter_added_while_running_is_left_out_with_its_value(self, engine: Engine, flow_name: str) -> None:
        """Scratch state the run owns and tears down, which the copy must not have."""
        node = _create_echo_node(engine, flow_name)
        token = node_types._in_aprocess.set(True)
        try:
            node.add_parameter(Parameter(name="scratch", type="str", default_value="", tooltip=""))
        finally:
            node_types._in_aprocess.reset(token)
        node.parameter_values["scratch"] = "temporary"
        assert "scratch" in node.parameters_added_during_execution
        request = _request_for("_EchoNode")

        engine.node_manager._prepare_for_worker(node, request)

        assert all(
            getattr(command, "parameter_name", None) != "scratch" for command in request.element_modification_commands
        )
        assert "scratch" not in request.parameter_values

    @pytest.mark.asyncio
    async def test_the_worker_runs_the_node_as_the_orchestrator_holds_it(self, engine: Engine, flow_name: str) -> None:
        node = _create_echo_node(engine, flow_name)
        engine.handle_request(
            AddParameterToNodeRequest(
                node_name=node.name, parameter_name="added_in_the_editor", type="str", default_value="", tooltip=""
            )
        )
        engine.handle_request(SetParameterValueRequest(node_name=node.name, parameter_name="text", value="edited"))

        with simulated_worker(engine, _LIBRARY_NAME) as worker:
            result = await engine.ahandle_request(
                ExecuteNodeRequest(
                    node_name=node.name,
                    node_metadata=cast("ExecutionNodeMetadata", dict(node.metadata)),
                    local_object_source=node.local_object_source,
                )
            )

        assert isinstance(result, ExecuteNodeResultSuccess), result.result_details
        assert result.parameter_output_values["echo"] == "edited"
        assert "added_in_the_editor" in result.parameter_output_values["parameter_names"]
        assert result.parameter_output_values["source"] == node.local_object_source
        assert _what_the_worker_holds(worker.engine) == {_WORKER_COPIES_FLOW}


class TestExecuteNodeWorkerRoute:
    """Orchestrator-side worker routing for ExecuteNodeRequest.

    When the library is owned by a worker and we're on the orchestrator, the
    handler routes the ExecuteNodeRequest to the worker and returns the result
    without calling aprocess locally.
    """

    @pytest.mark.asyncio
    async def test_routes_to_worker_and_returns_worker_result(self) -> None:
        node = _make_routed_node()
        obj_mgr = _make_mock_obj_mgr(existing_node=node)

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
        lib_mgr.workers.get_worker_for_library.return_value = ("eng-id", "topic")

        node_manager = _make_node_manager(object_manager=obj_mgr, library_manager=lib_mgr, worker_manager=wm)

        request = ExecuteNodeRequest(
            node_name="worker_node",
            node_metadata=cast("ExecutionNodeMetadata", {"node_type": "WorkerNode", "library": "worker_library"}),
        )
        result = await node_manager.on_execute_node_request(request)

        assert isinstance(result, ExecuteNodeResultSuccess)
        assert result.parameter_output_values == {"out": 42}
        wm.route_to_worker.assert_awaited_once()
        # The orchestrator stub must not have run aprocess; the worker did.
        node.aprocess.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_worker_going_away_becomes_a_failure_carrying_the_reason(self) -> None:
        """A retired worker must fail the node, not cancel it, and say why.

        Letting the cancellation through would have the resolution machine reap it as CANCELED:
        no NodeErrorEvent, one unnamed log line, and the node handed back UNRESOLVED with nothing
        anywhere explaining it. The reason is authored by whoever retired the worker, so it has to
        survive onto the result rather than being reconstructed here.
        """
        node = _make_routed_node()
        obj_mgr = _make_mock_obj_mgr(existing_node=node)

        gone = WorkerGoneError("worker 'eng-id' stopped responding and was shut down")
        wm = MagicMock()
        wm.route_to_worker = AsyncMock(side_effect=gone)
        lib_mgr = MagicMock()
        lib_mgr.is_worker = False
        lib_mgr._is_worker = False
        lib_mgr.workers.get_worker_for_library.return_value = ("eng-id", "topic")

        node_manager = _make_node_manager(object_manager=obj_mgr, library_manager=lib_mgr, worker_manager=wm)

        request = ExecuteNodeRequest(
            node_name="worker_node",
            node_metadata=cast("ExecutionNodeMetadata", {"node_type": "WorkerNode", "library": "worker_library"}),
        )
        result = await node_manager.on_execute_node_request(request)

        assert isinstance(result, ExecuteNodeResultFailure)
        assert "stopped responding and was shut down" in str(result.result_details)
        # Rides on `exception` so the node-failure formatting can reach it.
        assert result.exception is gone

    @pytest.mark.asyncio
    async def test_worker_failure_returns_failure(self) -> None:
        node = _make_routed_node()
        obj_mgr = _make_mock_obj_mgr(existing_node=node)

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
        lib_mgr.workers.get_worker_for_library.return_value = ("eng-id", "topic")

        node_manager = _make_node_manager(object_manager=obj_mgr, library_manager=lib_mgr, worker_manager=wm)

        request = ExecuteNodeRequest(
            node_name="worker_node",
            node_metadata=cast("ExecutionNodeMetadata", {"node_type": "WorkerNode", "library": "worker_library"}),
        )
        result = await node_manager.on_execute_node_request(request)

        assert isinstance(result, ExecuteNodeResultFailure)
        assert "worker exploded" in str(result.result_details)
        node.aprocess.assert_not_awaited()


def _make_orchestrator_library_manager() -> MagicMock:
    lib_mgr = MagicMock()
    lib_mgr.is_worker = False
    lib_mgr._is_worker = False
    lib_mgr.workers.get_worker_for_library.return_value = ("eng-id", "topic")
    return lib_mgr


class TestExecuteNodeWorkerValues:
    """Values cross the worker boundary as tagged plain data and arrive as the same values."""

    @pytest.mark.asyncio
    async def test_worker_outputs_arrive_as_values(self) -> None:
        artifact = ImageUrlArtifact("https://example.com/a.png", name="a")
        wm = MagicMock()
        wm.route_to_worker = AsyncMock(
            return_value={
                "result_type": ExecuteNodeResultSuccess.__name__,
                "result": {
                    "parameter_output_values": {"image": encode_value(artifact), "pair": encode_value((1, 2))},
                    "result_details": "ok",
                },
            }
        )
        lib_mgr = _make_orchestrator_library_manager()
        node_manager = _make_node_manager(
            object_manager=_make_mock_obj_mgr(existing_node=_make_routed_node()),
            library_manager=lib_mgr,
            worker_manager=wm,
        )

        result = await node_manager.on_execute_node_request(
            ExecuteNodeRequest(
                node_name="worker_node",
                node_metadata=cast("ExecutionNodeMetadata", {"node_type": "WorkerNode", "library": "worker_library"}),
            )
        )

        assert isinstance(result, ExecuteNodeResultSuccess)
        assert type(result.parameter_output_values["image"]) is ImageUrlArtifact
        assert result.parameter_output_values["image"].to_dict() == artifact.to_dict()
        assert result.parameter_output_values["pair"] == (1, 2)

    @pytest.mark.asyncio
    async def test_input_with_no_plain_data_form_fails_before_reaching_the_worker(self) -> None:
        wm = MagicMock()
        wm.route_to_worker = AsyncMock()
        lib_mgr = _make_orchestrator_library_manager()
        node_manager = _make_node_manager(
            object_manager=_make_mock_obj_mgr(existing_node=_make_routed_node()),
            library_manager=lib_mgr,
            worker_manager=wm,
        )

        result = await node_manager.on_execute_node_request(
            ExecuteNodeRequest(
                node_name="worker_node",
                parameter_values={"handle": _NoPlainDataForm()},
                node_metadata=cast("ExecutionNodeMetadata", {"node_type": "WorkerNode", "library": "worker_library"}),
            )
        )

        assert isinstance(result, ExecuteNodeResultFailure)
        assert "'handle'" in str(result.result_details)
        assert "_NoPlainDataForm" in str(result.result_details)
        wm.route_to_worker.assert_not_awaited()


class TestExecuteNodePrivateExecutionRoute:
    """Orchestrator-side routing of a node run inside an Isolated Execution scope to the worker it names."""

    _LIBRARY_WORKER = ("library-eng-id", "library-topic")
    _PRIVATE_WORKER = ("private-eng-id", "private-topic")

    def _make_node(self) -> Any:
        return _make_routed_node(name="test_node", library="some_library")

    def _make_worker_manager(
        self, private_worker: tuple[str, str] | None, unavailable_reason: str | None = None
    ) -> MagicMock:
        wm = MagicMock()
        wm.worker_unavailable_reason.return_value = unavailable_reason
        wm.get_worker_for_key.return_value = private_worker
        wm.route_to_worker = AsyncMock(
            return_value={
                "result_type": ExecuteNodeResultSuccess.__name__,
                "result": {"parameter_output_values": {"out": 1}, "result_details": "ok"},
            }
        )
        return wm

    async def _execute(
        self,
        node: BaseNode,
        wm: MagicMock,
        *,
        scope_owner: str | None = "My Group",
        library_worker: tuple[str, str] | None = None,
        load_failure: str | None = None,
    ) -> ExecuteNodeResultSuccess | ExecuteNodeResultFailure:
        lib_mgr = _make_mock_library_manager(is_worker=False)
        lib_mgr.workers.get_worker_for_library.return_value = library_worker
        node_manager = _make_node_manager(
            object_manager=_make_mock_obj_mgr(existing_node=node), library_manager=lib_mgr, worker_manager=wm
        )

        # Set after the node manager is built, which installs its own mock. The library's own wait
        # comes first and passes; only the Isolated Execution worker's wait can fail here.
        async def wait_until_executable(key: str | None) -> None:
            if key is None and load_failure is not None:
                raise RuntimeError(load_failure)

        wm.wait_until_executable = AsyncMock(side_effect=wait_until_executable)
        request = ExecuteNodeRequest(
            node_name=node.name,
            node_metadata=cast("ExecutionNodeMetadata", {"node_type": "SomeNodeType", "library": "some_library"}),
        )
        if scope_owner is None:
            result = await node_manager.on_execute_node_request(request)
        else:
            with isolated_execution_scope(scope_owner):
                result = await node_manager.on_execute_node_request(request)
        assert isinstance(result, ExecuteNodeResultSuccess | ExecuteNodeResultFailure)
        return result

    @pytest.mark.asyncio
    async def test_a_node_run_in_the_scope_goes_to_the_general_purpose_worker(self) -> None:
        node = self._make_node()
        wm = self._make_worker_manager(self._PRIVATE_WORKER)

        result = await self._execute(node, wm)

        assert isinstance(result, ExecuteNodeResultSuccess)
        wm.wait_until_executable.assert_any_await(None)
        wm.get_worker_for_key.assert_called_once_with(None)
        assert wm.route_to_worker.await_args.args[1:] == self._PRIVATE_WORKER
        node.aprocess.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_library_worker_takes_precedence_over_the_private_execution_worker(self) -> None:
        node = self._make_node()
        wm = self._make_worker_manager(self._PRIVATE_WORKER)

        result = await self._execute(node, wm, library_worker=self._LIBRARY_WORKER)

        assert isinstance(result, ExecuteNodeResultSuccess)
        assert wm.route_to_worker.await_args.args[1:] == self._LIBRARY_WORKER
        wm.get_worker_for_key.assert_not_called()

    @pytest.mark.asyncio
    async def test_fails_naming_the_owner_when_the_worker_is_not_running(self) -> None:
        node = self._make_node()
        wm = self._make_worker_manager(None)

        result = await self._execute(node, wm)

        assert isinstance(result, ExecuteNodeResultFailure)
        assert "'My Group'" in str(result.result_details)
        assert f"'{node.name}'" in str(result.result_details)
        wm.route_to_worker.assert_not_awaited()
        node.aprocess.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_fails_with_the_recorded_reason_when_the_worker_cannot_run(self) -> None:
        node = self._make_node()
        wm = self._make_worker_manager(self._PRIVATE_WORKER, unavailable_reason="it stopped responding.")

        result = await self._execute(node, wm)

        assert isinstance(result, ExecuteNodeResultFailure)
        assert "it stopped responding." in str(result.result_details)
        wm.route_to_worker.assert_not_awaited()
        node.aprocess.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_fails_when_the_worker_never_finishes_loading(self) -> None:
        node = self._make_node()
        wm = self._make_worker_manager(self._PRIVATE_WORKER)
        result = await self._execute(node, wm, load_failure="did not finish loading libraries")

        assert isinstance(result, ExecuteNodeResultFailure)
        assert "did not finish loading libraries" in str(result.result_details)
        wm.route_to_worker.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_node_run_outside_the_scope_runs_locally(self) -> None:
        """Where a node sits in the graph does not matter; only the run that set the scope does."""
        node = self._make_node()
        wm = self._make_worker_manager(self._PRIVATE_WORKER)

        result = await self._execute(node, wm, scope_owner=None)

        assert isinstance(result, ExecuteNodeResultSuccess)
        wm.route_to_worker.assert_not_awaited()
        node.aprocess.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_the_packagers_boundary_nodes_run_locally_inside_the_scope(self) -> None:
        """The packager's StartFlow/EndFlow are the flow's boundary and only pass values in and out."""
        node = self._make_node()
        node.metadata[PACKAGED_FLOW_BOUNDARY_KEY] = True
        wm = self._make_worker_manager(self._PRIVATE_WORKER)

        result = await self._execute(node, wm)

        assert isinstance(result, ExecuteNodeResultSuccess)
        wm.route_to_worker.assert_not_awaited()
        node.aprocess.assert_awaited_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("node_type", [StartNode, EndNode])
    async def test_a_start_or_end_node_a_user_placed_goes_to_the_worker(self, node_type: type[BaseNode]) -> None:
        """Only the packager's boundary nodes stay: a library's Start/End subclass may do real work."""
        node = _make_routed_node(node_type, name="Users_Start_Node", library="some_library")
        wm = self._make_worker_manager(self._PRIVATE_WORKER)

        result = await self._execute(node, wm)

        assert isinstance(result, ExecuteNodeResultSuccess)
        assert wm.route_to_worker.await_args.args[1:] == self._PRIVATE_WORKER
        node.aprocess.assert_not_awaited()
