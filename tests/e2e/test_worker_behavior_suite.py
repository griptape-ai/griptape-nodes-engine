"""End-to-end tests for the behaviors a heavy library needs when it executes in a worker.

The minimal fixtures proved routing and isolation. These prove the things a real library
(diffusers, advanced media) actually does all day, each of which has its own way of breaking
across a process boundary:

- saving media and handing back a URL that outlives the worker that wrote it
- reading engine state through requests rather than local managers
- streaming progress and using the yield-a-callable pattern
- chaining serializable values across several nodes, each hop crossing the boundary
- converters, validators, and dynamic parameters, which run on the orchestrator's real class

Run against the ``worker_behavior_library`` fixture, in both roles: the orchestrator (where
nodes are instantiated and edited) and a worker (where ``process`` runs). The worker is a second
engine in this process (``tests.simulated_worker``): the orchestrator's node is sent to it the way
a real worker is sent one, it loads a copy into its own graph, and the requests the copy makes while
it runs are forwarded back.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

import pytest

from griptape_nodes.exe_types.local_objects import is_reference
from griptape_nodes.node_library.library_registry import LibraryRegistry
from griptape_nodes.retained_mode.engine import current_engine
from griptape_nodes.retained_mode.events import converters
from griptape_nodes.retained_mode.events.app_events import AppInitializationComplete
from griptape_nodes.retained_mode.events.base_events import ResultPayloadFailure
from griptape_nodes.retained_mode.events.config_events import GetConfigValueRequest
from griptape_nodes.retained_mode.events.execution_events import (
    ExecuteNodeRequest,
    ExecuteNodeResultSuccess,
)
from griptape_nodes.retained_mode.events.flow_events import CreateFlowRequest, CreateFlowResultSuccess
from griptape_nodes.retained_mode.events.library_events import (
    RegisterLibraryFromFileRequest,
    RegisterLibraryFromFileResultSuccess,
)
from griptape_nodes.retained_mode.events.node_events import CreateNodeRequest, CreateNodeResultSuccess
from griptape_nodes.retained_mode.events.os_events import ReadFileRequest, WriteFileRequest
from griptape_nodes.retained_mode.events.parameter_events import (
    AddParameterToNodeRequest,
    AddParameterToNodeResultSuccess,
    RemoveParameterFromNodeRequest,
    SetParameterValueRequest,
)
from griptape_nodes.retained_mode.events.project_events import ActivateWorkspaceProjectRequest
from griptape_nodes.servers.static import ORCHESTRATOR_STATIC_SERVER_BASE_URL_ENV
from griptape_nodes.testing.libraries import WORKER_BEHAVIOR_LIBRARY_NAME, materialize_worker_behavior_library
from tests.simulated_worker import SimulatedWorker, simulated_worker

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from griptape_nodes.exe_types.node_types import BaseNode


_CANVAS_FLOW_NAME = "worker_behavior_canvas"


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


def _make(node_type: str, name: str) -> BaseNode:
    """Create a node directly, for tests that only execute it."""
    node = LibraryRegistry.create_node(
        node_type=node_type, name=name, specific_library_name=WORKER_BEHAVIOR_LIBRARY_NAME
    )
    current_engine().object_manager.add_object_by_name(name, node)
    return node


def _make_in_flow(node_type: str, name: str) -> BaseNode:
    """Create a node inside a real flow, the one canvas every node of a test shares.

    Editing a parameter unresolves downstream nodes, which needs a parent flow, so anything
    exercising value hooks has to go through the normal creation path.
    """
    engine = current_engine()
    if not engine.context_manager.has_current_workflow():
        engine.context_manager.push_workflow(workflow_name="worker_behavior_workflow")
    if engine.object_manager.attempt_get_object_by_name(_CANVAS_FLOW_NAME) is None:
        flow_result = engine.handle_request(
            CreateFlowRequest(parent_flow_name=None, flow_name=_CANVAS_FLOW_NAME, set_as_new_context=False)
        )
        assert isinstance(flow_result, CreateFlowResultSuccess), flow_result
    create_result = engine.handle_request(
        CreateNodeRequest(
            node_type=node_type,
            specific_library_name=WORKER_BEHAVIOR_LIBRARY_NAME,
            node_name=name,
            override_parent_flow_name=_CANVAS_FLOW_NAME,
        )
    )
    assert isinstance(create_result, CreateNodeResultSuccess), getattr(create_result, "result_details", create_result)
    return engine.node_manager.get_node_by_name(create_result.node_name)


async def _execute(node_type: str, name: str, **parameter_values: object) -> ExecuteNodeResultSuccess:
    result = await current_engine().ahandle_request(
        ExecuteNodeRequest(
            node_name=name,
            parameter_values=dict(parameter_values),
            node_metadata={"node_type": node_type, "library": WORKER_BEHAVIOR_LIBRARY_NAME},
        )
    )
    assert isinstance(result, ExecuteNodeResultSuccess), getattr(result, "result_details", result)
    return result


class TestMediaFromAWorker:
    """Where a worker's asset URLs point, which decides whether they survive the worker.

    Both branches below reach the same place, and BOTH are needed. An earlier version of this
    class tested only the env-var branch, with a payload carrying no URL -- a state the real
    host never produces, because it starts a static server for every role and announces it.
    So the assertion passed while a live worker still advertised its own ephemeral port. A
    precondition no caller can produce proves nothing about the caller.
    """

    @pytest.mark.asyncio
    async def test_worker_adopts_the_url_the_host_announces(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The live path: the host resolves the orchestrator's server and rides it on the payload."""
        monkeypatch.delenv(ORCHESTRATOR_STATIC_SERVER_BASE_URL_ENV, raising=False)
        orchestrator_url = "http://localhost:8124"
        static_files_manager = current_engine().static_files_manager
        static_files_manager._static_server_base_url = None
        static_files_manager.on_app_initialization_complete(
            AppInitializationComplete(is_worker=True, static_server_base_url=orchestrator_url)
        )

        assert static_files_manager.static_server_base_url == orchestrator_url

    @pytest.mark.asyncio
    async def test_worker_falls_back_to_the_spawn_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The fallback: a host that spawns a worker without announcing a server.

        WorkerManager puts the orchestrator's URL on the spawn environment, so a worker whose
        host stays silent still has somewhere durable to point.
        """
        # Deliberately NOT the static server's default port: an earlier version of this test
        # used 8124, and a broken adoption gate passed it anyway because the fallback branch
        # produced the same default URL by coincidence.
        orchestrator_url = "http://localhost:18125"
        monkeypatch.setenv(ORCHESTRATOR_STATIC_SERVER_BASE_URL_ENV, orchestrator_url)
        static_files_manager = current_engine().static_files_manager
        static_files_manager._static_server_base_url = None
        static_files_manager.on_app_initialization_complete(AppInitializationComplete(is_worker=True))

        assert static_files_manager.static_server_base_url == orchestrator_url

    def test_without_the_env_var_or_a_host_url_the_default_address_is_assumed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The engine serves nothing itself, so with no source it points where the host listens by default."""
        monkeypatch.delenv(ORCHESTRATOR_STATIC_SERVER_BASE_URL_ENV, raising=False)
        static_files_manager = current_engine().static_files_manager
        static_files_manager._static_server_base_url = None
        static_files_manager.on_app_initialization_complete(AppInitializationComplete())

        assert static_files_manager.static_server_base_url.startswith("http://")

    @pytest.mark.asyncio
    async def test_the_spawn_environment_outranks_the_hosts_own_announcement(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Both sources present, different values: the parent's durable server must win.

        This is the ordering, and it is load-bearing. The host announces a URL for every role, so
        checking the payload first made the environment branch unreachable -- leaving one repo's
        code as the only thing preventing a worker from advertising its own ephemeral port. With
        only one source set at a time, flipping the branches back passes.
        """
        orchestrator_url = "http://localhost:8124"
        worker_own_url = "http://localhost:59999"
        monkeypatch.setenv(ORCHESTRATOR_STATIC_SERVER_BASE_URL_ENV, orchestrator_url)
        static_files_manager = current_engine().static_files_manager
        static_files_manager._static_server_base_url = None
        static_files_manager.on_app_initialization_complete(
            AppInitializationComplete(is_worker=True, static_server_base_url=worker_own_url)
        )

        assert static_files_manager.static_server_base_url == orchestrator_url

    @pytest.mark.asyncio
    async def test_a_leaked_environment_variable_does_not_redirect_an_orchestrator(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The env var only means something in a worker the orchestrator spawned.

        An orchestrator started from a shell where the variable leaked (an export left over
        from debugging, a stale wrapper script) must not silently point every asset URL it
        mints at an address nothing in this process controls.
        """
        monkeypatch.setenv(ORCHESTRATOR_STATIC_SERVER_BASE_URL_ENV, "http://localhost:4444")
        static_files_manager = current_engine().static_files_manager
        static_files_manager._static_server_base_url = None
        static_files_manager.on_app_initialization_complete(
            AppInitializationComplete(static_server_base_url="http://localhost:8124")
        )

        assert static_files_manager.static_server_base_url == "http://localhost:8124"

    @pytest.mark.asyncio
    async def test_media_bytes_are_written_and_a_url_returned(self) -> None:
        """Bytes stay local (they cannot cross the boundary); the URL is what travels."""
        _make("SaveMediaNode", "Media")

        result = await _execute("SaveMediaNode", "Media")

        url = result.parameter_output_values["url"]
        assert isinstance(url, str)
        assert url  # a real URL, not an empty default
        assert "Media.png" in url

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("worker")
    async def test_media_bytes_are_written_from_inside_a_worker(self) -> None:
        """The same save, in the role that actually performs it.

        Saving is the one capability a worker keeps local, and the storage driver reaches the
        workspace path to do it. The manager guard that stops NODE code reading divergent state
        must not fire on that: a worker shares the workspace on disk, so the read is correct, and
        refusing it left every worker unable to write a single file.
        """
        _make_in_flow("SaveMediaNode", "WorkerMedia")

        result = await _execute("SaveMediaNode", "WorkerMedia")

        assert "WorkerMedia.png" in result.parameter_output_values["url"]


class TestStateAccessFromAWorker:
    @pytest.mark.asyncio
    async def test_config_read_through_a_request_succeeds_in_a_worker(self, worker: SimulatedWorker) -> None:
        """The request path resolves for a node running in a worker, and the orchestrator answers it.

        The transport is stood in for; that forwarding works over the real wire is covered by
        test_worker_forwarding_reentrancy.py and by the real two-process verification.
        """
        _make_in_flow("ReadConfigNode", "Reader")

        result = await _execute("ReadConfigNode", "Reader", config_key="workspace_directory")

        # A real value came back, which means the request resolved rather than being blocked.
        assert result.parameter_output_values["config_value"]
        assert any(isinstance(request, GetConfigValueRequest) for request in worker.forwarded)

    def test_config_getter_forwards_from_a_worker(self) -> None:
        """Config and secrets must forward; file I/O must NOT.

        Config and secrets can differ between the two processes -- a worker's environment is
        frozen when it spawns -- so the guardrail's advice to use a request is only true if the
        request reaches the orchestrator.

        Files are the opposite, and the reason is concrete rather than philosophical. The
        workspace is shared on disk, so the local answer is already right; and forwarding
        actively corrupted it, because `content` is `str | bytes` and the wire form base64s
        bytes into a JSON string that cattrs resolves back to `str`. A path carrying macro
        variables could not be structured at all.
        """
        from griptape_nodes.app.worker_routing import LOCAL_ONLY_REQUEST_TYPES
        from griptape_nodes.retained_mode.events.config_events import GetConfigValueRequest
        from griptape_nodes.retained_mode.events.os_events import ReadFileRequest, WriteFileRequest
        from griptape_nodes.retained_mode.events.secrets_events import GetSecretValueRequest

        for request_type in (GetConfigValueRequest, GetSecretValueRequest):
            assert request_type not in LOCAL_ONLY_REQUEST_TYPES, request_type.__name__
        for request_type in (ReadFileRequest, WriteFileRequest):
            assert request_type in LOCAL_ONLY_REQUEST_TYPES, request_type.__name__

    def test_forwarding_a_file_write_would_corrupt_the_bytes(self) -> None:
        """Pin the mechanism, so nobody moves file I/O back into the forwarded set.

        This is the round trip `forward_to_orchestrator` performs on a result: unstructure to
        JSON, structure back. `content` is `str | bytes`, and the union resolves to `str`, so the
        bytes come back as mojibake rather than raising -- which is why the corruption was silent.
        """
        from griptape_nodes.retained_mode.events.os_events import WriteFileRequest

        converter = converters.engine

        original = b"\x89PNG\r\n\x1a\n\x00\xff\xfe"
        sent = json.loads(json.dumps(converter.unstructure(WriteFileRequest(file_path="x.png", content=original))))
        round_tripped = converter.structure(sent, WriteFileRequest).content

        assert round_tripped != original, "if this now round-trips, file I/O could safely forward"
        assert isinstance(round_tripped, str)


class TestStreamingAndAsyncResult:
    @pytest.mark.asyncio
    async def test_streaming_node_yields_work_and_accumulates_output(self) -> None:
        """The AsyncResult yield pattern (24 standard-library files use it) works here."""
        _make("StreamingNode", "Streamer")

        result = await _execute("StreamingNode", "Streamer")

        assert result.parameter_output_values["stream"] == "alphabetagamma"

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("worker")
    async def test_streaming_works_in_a_worker_too(self) -> None:
        _make_in_flow("StreamingNode", "WorkerStreamer")

        result = await _execute("StreamingNode", "WorkerStreamer")

        assert result.parameter_output_values["stream"] == "alphabetagamma"


class TestMultiHopChain:
    @pytest.mark.asyncio
    @pytest.mark.usefixtures("worker")
    async def test_three_hops_of_serializable_values(self) -> None:
        """Each hop's value round-trips through the orchestrator, as per-node dispatch requires."""
        for node_type, name in (
            ("ChainStartNode", "Start"),
            ("ChainMiddleNode", "Middle"),
            ("ChainEndNode", "End"),
        ):
            _make_in_flow(node_type, name)

        start = await _execute("ChainStartNode", "Start")
        middle = await _execute("ChainMiddleNode", "Middle", in_value=start.parameter_output_values["out"])
        end = await _execute("ChainEndNode", "End", in_value=middle.parameter_output_values["out"])

        assert end.parameter_output_values["final"] == "start->middle->end"


class TestResultsReportedWithTheSetter:
    """`set_parameter_value` on an output is how plenty of libraries report a result.

    Only produced values travel back from a worker, so a result the setter recorded nowhere else was
    left behind and the output read empty on the orchestrator. What decides whether the setter also
    records one is whether the parameter has an OUTPUT to publish on, not whether OUTPUT is the only
    mode it allows.
    """

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("worker")
    async def test_a_worker_ships_a_result_the_setter_stored(self) -> None:
        _make_in_flow("ReportsWithSetterNode", "SetterReporter")

        result = await _execute("ReportsWithSetterNode", "SetterReporter")

        assert result.parameter_output_values["status"] == "reported-readback"

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("worker")
    async def test_a_worker_ships_a_result_on_a_parameter_the_editor_can_also_type_into(self) -> None:
        """Allowing PROPERTY as well does not stop the parameter publishing, so the result travels."""
        _make_in_flow("ReportsWithSetterNode", "SetterDisplayReporter")

        result = await _execute("ReportsWithSetterNode", "SetterDisplayReporter")

        assert result.parameter_output_values["on_display"] == "shown"

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("worker")
    async def test_a_worker_ships_a_result_on_a_parameter_that_declared_no_modes(self) -> None:
        """The default is every mode, and that is most of the parameters a library declares."""
        _make_in_flow("ReportsWithSetterNode", "SetterDefaultReporter")

        result = await _execute("ReportsWithSetterNode", "SetterDefaultReporter")

        assert result.parameter_output_values["defaulted"] == "defaulted-result"

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("worker")
    async def test_a_parameter_with_no_output_is_left_behind(self) -> None:
        """With no OUTPUT there is no port to publish on, so a run's write to it is scratch."""
        _make_in_flow("ReportsWithSetterNode", "SetterScratchReporter")

        result = await _execute("ReportsWithSetterNode", "SetterScratchReporter")

        assert "scratch" not in result.parameter_output_values

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("worker")
    async def test_the_node_reads_back_what_it_just_set(self) -> None:
        """A library that sets then reads through the same API has to see its own write."""
        _make_in_flow("ReportsWithSetterNode", "SetterReadbackReporter")

        result = await _execute("ReportsWithSetterNode", "SetterReadbackReporter")

        assert result.parameter_output_values["status"] == "reported-readback"

    @pytest.mark.asyncio
    async def test_the_same_node_reports_the_same_way_on_the_orchestrator(self) -> None:
        current_engine().library_manager._is_worker = False
        _make("ReportsWithSetterNode", "LocalSetterReporter")

        result = await _execute("ReportsWithSetterNode", "LocalSetterReporter")

        assert result.parameter_output_values["status"] == "reported-readback"
        assert result.parameter_output_values["on_display"] == "shown"
        assert result.parameter_output_values["defaulted"] == "defaulted-result"
        assert "scratch" not in result.parameter_output_values


class TestAnOutputListGrownWhileRunning:
    """Split Video's shape: an output-only `ParameterList` whose children are set as the node runs.

    The children are rebuilt into the list by `handle_container_parameter`, which reads them raw, so
    they stay authored and only the rebuilt list is a result.
    """

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("worker")
    async def test_a_worker_ships_the_whole_list(self) -> None:
        _make_in_flow("GrowsAnOutputListNode", "ListGrower")

        result = await _execute("GrowsAnOutputListNode", "ListGrower")

        assert result.parameter_output_values["clips"] == ["clip0", "clip1"]

    @pytest.mark.asyncio
    async def test_the_same_node_on_the_orchestrator(self) -> None:
        current_engine().library_manager._is_worker = False
        node = _make("GrowsAnOutputListNode", "LocalListGrower")

        await _execute("GrowsAnOutputListNode", "LocalListGrower")

        assert node.get_parameter_value("clips") == ["clip0", "clip1"]


class TestAnInputListFilledInTheEditor:
    """A list the artist filled row by row, then edited, run where its library runs."""

    @pytest.mark.parametrize("in_a_worker", [False, True])
    @pytest.mark.asyncio
    async def test_the_node_reads_the_rows_it_has_now(
        self, request: pytest.FixtureRequest, caplog: pytest.LogCaptureFixture, *, in_a_worker: bool
    ) -> None:
        node = _make_in_flow("ReadsAnInputListNode", f"InputLister{in_a_worker}")
        rows = []
        for value in ("a", "b", "c"):
            added = current_engine().handle_request(
                AddParameterToNodeRequest(node_name=node.name, parent_container_name="items")
            )
            assert isinstance(added, AddParameterToNodeResultSuccess)
            current_engine().handle_request(
                SetParameterValueRequest(node_name=node.name, parameter_name=added.parameter_name, value=value)
            )
            rows.append(added.parameter_name)
        current_engine().handle_request(RemoveParameterFromNodeRequest(node_name=node.name, parameter_name=rows[1]))
        if in_a_worker:
            request.getfixturevalue("worker")

        with caplog.at_level(logging.WARNING, logger="griptape_nodes"):
            result = await _execute("ReadsAnInputListNode", node.name)

        assert result.parameter_output_values["echo"] == ["a", "c"]
        assert "which it does not have" not in caplog.text

    @pytest.mark.parametrize("in_a_worker", [False, True])
    @pytest.mark.asyncio
    async def test_a_list_set_whole_by_request(self, request: pytest.FixtureRequest, *, in_a_worker: bool) -> None:
        """How an MCP client or a script sets a list."""
        node = _make_in_flow("ReadsAnInputListNode", f"WholeLister{in_a_worker}")
        current_engine().handle_request(
            SetParameterValueRequest(node_name=node.name, parameter_name="items", value=["a", "b"])
        )
        if in_a_worker:
            request.getfixturevalue("worker")

        result = await _execute("ReadsAnInputListNode", node.name)

        assert result.parameter_output_values["echo"] == ["a", "b"]


class TestEditorTimeBehaviorOnRealNodes:
    """These are exactly what a schema stub would have dropped."""

    def test_converter_runs_on_the_orchestrator(self) -> None:
        node = _make_in_flow("EditorBehaviorNode", "Editor")

        current_engine().handle_request(
            SetParameterValueRequest(node_name="Editor", parameter_name="mode", value="expand")
        )

        # The converter uppercased the value; a stub would have stored it verbatim.
        assert node.get_parameter_value("mode") == "EXPAND"

    def test_validator_runs_on_the_orchestrator(self) -> None:
        _make_in_flow("EditorBehaviorNode", "Validated")

        result = current_engine().handle_request(
            SetParameterValueRequest(node_name="Validated", parameter_name="mode", value="forbidden")
        )

        # The validator rejected it; a stub carries no validators at all.
        assert result.failed()

    def test_dynamic_parameter_grows_and_shrinks_from_a_value_hook(self) -> None:
        """after_value_set mutating the parameter set, which diffusers relies on heavily."""
        node = _make_in_flow("EditorBehaviorNode", "Dynamic")
        assert node.get_parameter_by_name("dynamic_extra") is None

        current_engine().handle_request(
            SetParameterValueRequest(node_name="Dynamic", parameter_name="mode", value="expand")
        )
        assert node.get_parameter_by_name("dynamic_extra") is not None

        current_engine().handle_request(
            SetParameterValueRequest(node_name="Dynamic", parameter_name="mode", value="plain")
        )
        assert node.get_parameter_by_name("dynamic_extra") is None


class TestUnshippableOutputsAreKept:
    """A value the author declared unserializable stays in the worker and a reference travels."""

    @pytest.mark.asyncio
    async def test_a_worker_keeps_an_unserializable_output_and_ships_a_reference(self, worker: SimulatedWorker) -> None:
        _make_in_flow("UnshippableOutputNode", "Unshippable")

        result = await _execute("UnshippableOutputNode", "Unshippable")

        sent = result.parameter_output_values["live_handle"]
        assert is_reference(sent), sent
        # The object itself never left the worker. Its copy of the node is gone after the run, so the
        # entry is held under the identity the orchestrator's node lent it.
        entry = worker.engine.resource_manager.entry_for(sent["key"])
        assert entry is not None
        assert not isinstance(entry.value, str)

    @pytest.mark.asyncio
    async def test_the_same_node_is_fine_on_the_orchestrator(self) -> None:
        """Nothing crosses a boundary in-process, so the value is legal there.

        This is what keeps the guardrail from being a blanket ban on unserializable outputs:
        they are only a problem when they have to travel.
        """
        current_engine().library_manager._is_worker = False
        _make("UnshippableOutputNode", "LocalHandle")

        result = await _execute("UnshippableOutputNode", "LocalHandle")

        assert result.parameter_output_values["summary"] == "handle-1"


class TestSecretsFromAWorker:
    @pytest.mark.asyncio
    @pytest.mark.usefixtures("worker")
    async def test_secret_read_through_a_request_reaches_the_engine(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Almost every real node needs an API key, and this is the path it must take.

        A worker's environment is frozen when it spawns, so a manager read can answer from a
        stale copy; the request is what reaches the process that owns the secret.
        """
        monkeypatch.setenv("GTN_WORKER_FIXTURE_SECRET", "fixture-secret-value")
        _make_in_flow("ReadSecretNode", "SecretReader")

        result = await _execute("ReadSecretNode", "SecretReader")

        assert result.parameter_output_values["secret_value"] == "fixture-secret-value"  # noqa: S105

    def test_the_secret_getter_is_forwarded_from_a_worker(self) -> None:
        """The request has to be forwarded, or the read answers locally and the advice is false."""
        from griptape_nodes.app.worker_routing import LOCAL_ONLY_REQUEST_TYPES
        from griptape_nodes.retained_mode.events.secrets_events import GetSecretValueRequest

        assert GetSecretValueRequest not in LOCAL_ONLY_REQUEST_TYPES


class TestProjectReadsStayLocalInAWorker:
    """A worker answers project questions itself rather than forwarding them.

    A worker already adopts the orchestrator's project and a project's base directory is shared
    on-disk state, so the local answer is correct and a round trip buys nothing on a path as hot
    as writing sidecar metadata for every saved file.

    Forwarding also corrupts the answer: GetCurrentProjectResultSuccess annotates
    `project_info: ProjectInfo` under TYPE_CHECKING to break an import cycle, and cattrs cannot
    resolve that name, so its NameError fallback hands the raw dict to the constructor.
    isinstance() passes while `.project_info` is a dict.

    This asserts the routing decision, which is what an in-process test can reach. That the
    sidecar file actually lands is checked by the real two-process verification, since the
    corruption only happens when a result crosses the wire.
    """

    def test_get_current_project_is_not_forwarded(self) -> None:
        from griptape_nodes.app.worker_routing import LOCAL_ONLY_REQUEST_TYPES
        from griptape_nodes.retained_mode.events.project_events import GetCurrentProjectRequest

        assert GetCurrentProjectRequest in LOCAL_ONLY_REQUEST_TYPES


class TestBinaryFileWritesFromAWorker:
    """A node's file output must survive being written from a worker.

    The worker here has the RemoteHandlers a real worker installs, so a WriteFileRequest that
    forwarded would be answered by the orchestrator's engine. The wire corruption itself is guarded
    by the membership and converter tests above and by tests/unit/app/test_worker_routing_filesystem.py.
    """

    @pytest.mark.asyncio
    async def test_binary_survives_a_write_from_a_worker(self, worker: SimulatedWorker) -> None:
        _make_in_flow("WriteBytesNode", "WorkerBytes")

        result = await _execute("WriteBytesNode", "WorkerBytes")

        # `bytes_survived` is the oracle: the node compares what it read against what it wrote,
        # in the one process where both values exist. The count guards against that comparison
        # passing over two empty reads.
        assert result.parameter_output_values["bytes_survived"] is True
        assert result.parameter_output_values["byte_count"] > 0
        assert not any(isinstance(request, WriteFileRequest | ReadFileRequest) for request in worker.forwarded)

    @pytest.mark.asyncio
    async def test_binary_survives_a_write_on_the_orchestrator(self) -> None:
        current_engine().library_manager._is_worker = False
        _make("WriteBytesNode", "OrchestratorBytes")

        result = await _execute("WriteBytesNode", "OrchestratorBytes")

        assert result.parameter_output_values["bytes_survived"] is True

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("worker")
    async def test_a_parameter_a_value_hook_added_by_request_reaches_the_worker_copy(self) -> None:
        """EditorBehaviorNode's hook adds its dynamic parameter with AddParameterToNodeRequest as it is edited.

        The request lands on the orchestrator's node, and the worker's copy is loaded from that node's
        structure, so the parameter exists on the copy that runs process() too.
        """
        node = _make_in_flow("EditorBehaviorNode", "RequestPathNode")
        current_engine().handle_request(
            SetParameterValueRequest(node_name=node.name, parameter_name="mode", value="expand")
        )

        result = await _execute("EditorBehaviorNode", node.name)

        assert node.get_parameter_by_name("dynamic_extra") is not None
        assert result.parameter_output_values["dynamic_visible_in_process"] is True
        assert result.parameter_output_values["observed_mode"] == "EXPAND"


class TestTheRebuildBeforeARun:
    """A worker loads its copy without running value hooks, and the rebuild ends before process() runs.

    Requests made during the rebuild are answered by the worker's own graph, so running process() inside it
    would answer the node's questions about connections and other nodes from a graph that has neither.
    """

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("worker")
    async def test_loading_the_copy_runs_no_value_hook_and_process_runs_outside_the_rebuild(self) -> None:
        node = _make_in_flow("ReportsHydrationNode", "Rebuilt")
        current_engine().handle_request(
            SetParameterValueRequest(node_name=node.name, parameter_name="trigger", value="set")
        )

        result = await _execute("ReportsHydrationNode", node.name)

        assert result.parameter_output_values["hook_ran"] is False
        assert result.parameter_output_values["process_saw_rebuild"] is False


class TestStructureDerivedFromValuesReachesTheWorker:
    """A worker's copy has the structure the orchestrator's node has, however that structure was made.

    The copy is loaded the way a saved workflow loads: created from its class, given the parameters and
    alterations the node has beyond that, then given its values. No value hook runs on the copy, so a
    parameter derived by a hook arrives as structure rather than being derived again.
    """

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("worker")
    async def test_a_parameter_derived_by_a_value_hook_reaches_the_copy_with_its_value(self) -> None:
        node = _make_in_flow("DerivedStructureNode", "DerivedOnce")
        for parameter_name, value in (("shape", "expanded"), ("derived_in", "payload")):
            current_engine().handle_request(
                SetParameterValueRequest(node_name=node.name, parameter_name=parameter_name, value=value)
            )

        result = await _execute("DerivedStructureNode", node.name)

        assert result.parameter_output_values["echo"] == "payload"

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("worker")
    async def test_a_chain_of_derived_parameters_reaches_the_copy(self) -> None:
        """`shape` derives `derived_in`, whose own hook derives `derived_deep`: both levels arrive."""
        node = _make_in_flow("DerivedStructureNode", "DerivedChain")
        for parameter_name, value in (("shape", "expanded"), ("derived_in", "deeper"), ("derived_deep", "bottom")):
            current_engine().handle_request(
                SetParameterValueRequest(node_name=node.name, parameter_name=parameter_name, value=value)
            )

        result = await _execute("DerivedStructureNode", node.name)

        assert result.parameter_output_values["echo"] == "deeper"
        assert result.parameter_output_values["deep_echo"] == "bottom"

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("worker")
    async def test_a_parameter_the_user_added_reaches_the_copy_with_its_value(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A parameter added in the editor is part of the node's structure, so the copy has it too."""
        node = _make_in_flow("DerivedStructureNode", "UserAdded")
        added = current_engine().handle_request(
            AddParameterToNodeRequest(
                node_name=node.name, parameter_name="user_note", type="str", default_value="", tooltip="Added by hand"
            )
        )
        assert isinstance(added, AddParameterToNodeResultSuccess), added
        current_engine().handle_request(
            SetParameterValueRequest(node_name=node.name, parameter_name="user_note", value="kept")
        )

        with caplog.at_level(logging.WARNING, logger="griptape_nodes"):
            await _execute("DerivedStructureNode", node.name)

        assert "user_note" not in caplog.text


class TestProjectSavesFromAWorker:
    """A node saving through the project system works in a worker, and stays portable.

    `ProjectFileDestination` is how a node emits a user-visible output, and it is the case the
    manager guard raises the most questions about: the guard refuses `GriptapeNodes.OSManager()`
    during node execution in a worker, so the obvious reading is that saving a file is refused
    too. It is not -- the destination composes four requests, and a request is exactly what the
    guard's message directs authors to.

    Asserting on the macro rather than just the bytes: mapping the written path back to
    `{outputs}/...` is what lets a saved workflow reopen somewhere else, and it is the step that
    degrades quietly. If the project reads stopped being answered, the save would still succeed
    and still round-trip its bytes, while every reference stored in the workflow silently became
    an absolute path from the machine that ran it.
    """

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("worker")
    async def test_project_save_works_and_stays_portable_in_a_worker(self) -> None:
        activation = current_engine().handle_request(ActivateWorkspaceProjectRequest())
        assert not isinstance(activation, ResultPayloadFailure), getattr(activation, "result_details", activation)
        _make_in_flow("WriteProjectFileNode", "WorkerProjectSave")

        result = await _execute("WriteProjectFileNode", "WorkerProjectSave")

        assert result.parameter_output_values["bytes_survived"] is True
        assert result.parameter_output_values["saved_as"].startswith("{outputs}")

    @pytest.mark.asyncio
    async def test_project_save_works_the_same_on_the_orchestrator(self) -> None:
        activation = current_engine().handle_request(ActivateWorkspaceProjectRequest())
        assert not isinstance(activation, ResultPayloadFailure), getattr(activation, "result_details", activation)
        current_engine().library_manager._is_worker = False
        _make("WriteProjectFileNode", "OrchestratorProjectSave")

        result = await _execute("WriteProjectFileNode", "OrchestratorProjectSave")

        assert result.parameter_output_values["bytes_survived"] is True
        assert result.parameter_output_values["saved_as"].startswith("{outputs}")
