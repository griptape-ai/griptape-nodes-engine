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
nodes are instantiated and edited) and a worker (where ``process`` runs).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from griptape_nodes.node_library.library_registry import LibraryRegistry, LibrarySchema
from griptape_nodes.retained_mode.engine import current_engine
from griptape_nodes.retained_mode.events.app_events import AppInitializationComplete
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
from griptape_nodes.retained_mode.events.parameter_events import SetParameterValueRequest
from griptape_nodes.servers.static import ORCHESTRATOR_STATIC_SERVER_BASE_URL_ENV
from griptape_nodes.utils.version_utils import engine_version

if TYPE_CHECKING:
    from griptape_nodes.exe_types.node_types import BaseNode

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "worker_behavior_library"
LIBRARY = "Worker Behavior Library"


@pytest.fixture(autouse=True)
def _register_library(tmp_path: Path) -> None:
    library_dir = tmp_path / "worker_behavior_library"
    library_dir.mkdir()
    schema = json.loads((FIXTURE_DIR / "griptape_nodes_library.json").read_text())
    schema["library_schema_version"] = LibrarySchema.LATEST_SCHEMA_VERSION
    schema["metadata"]["engine_version"] = engine_version
    (library_dir / "griptape_nodes_library.json").write_text(json.dumps(schema, indent=2))
    shutil.copy(FIXTURE_DIR / "worker_behavior_nodes.py", library_dir / "worker_behavior_nodes.py")
    result = current_engine().handle_request(
        RegisterLibraryFromFileRequest(file_path=str(library_dir / "griptape_nodes_library.json"))
    )
    assert isinstance(result, RegisterLibraryFromFileResultSuccess), getattr(result, "result_details", result)


def _make(node_type: str, name: str) -> BaseNode:
    """Create a node directly, for tests that only execute it."""
    node = LibraryRegistry.create_node(node_type=node_type, name=name, specific_library_name=LIBRARY)
    current_engine().object_manager.add_object_by_name(name, node)
    return node


def _make_in_flow(node_type: str, name: str) -> BaseNode:
    """Create a node inside a real flow.

    Editing a parameter unresolves downstream nodes, which needs a parent flow, so anything
    exercising value hooks has to go through the normal creation path.
    """
    current_engine().context_manager.push_workflow(workflow_name=f"wf_{name}")
    flow_result = current_engine().handle_request(
        CreateFlowRequest(parent_flow_name=None, flow_name=f"flow_{name}", set_as_new_context=False)
    )
    assert isinstance(flow_result, CreateFlowResultSuccess), flow_result
    create_result = current_engine().handle_request(
        CreateNodeRequest(
            node_type=node_type,
            specific_library_name=LIBRARY,
            node_name=name,
            override_parent_flow_name=flow_result.flow_name,
        )
    )
    assert isinstance(create_result, CreateNodeResultSuccess), getattr(create_result, "result_details", create_result)
    return current_engine().node_manager.get_node_by_name(create_result.node_name)


async def _execute(node_type: str, name: str, **parameter_values: object) -> ExecuteNodeResultSuccess:
    result = await current_engine().ahandle_request(
        ExecuteNodeRequest(
            node_name=name,
            parameter_values=dict(parameter_values),
            node_metadata={"node_type": node_type, "library": LIBRARY},
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
        # used 8124, and a broken adoption gate passed it anyway because the self-serve branch
        # bound the default port and produced the same URL by coincidence.
        orchestrator_url = "http://localhost:18125"
        monkeypatch.setenv(ORCHESTRATOR_STATIC_SERVER_BASE_URL_ENV, orchestrator_url)
        static_files_manager = current_engine().static_files_manager
        static_files_manager._static_server_base_url = None
        static_files_manager.on_app_initialization_complete(AppInitializationComplete(is_worker=True))

        assert static_files_manager.static_server_base_url == orchestrator_url

    def test_without_the_env_var_a_process_serves_the_workspace_itself(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The orchestrator's own path is unchanged: no env var, so it serves and advertises.

        Patched rather than actually started. Letting it bind left a uvicorn thread running for the
        rest of the session, and the next test's engine reset made that thread raise -- reported
        against whichever unrelated test happened to be running. What matters here is the branch
        taken, not that a socket got bound.
        """
        monkeypatch.delenv(ORCHESTRATOR_STATIC_SERVER_BASE_URL_ENV, raising=False)
        static_files_manager = current_engine().static_files_manager
        static_files_manager._static_server_base_url = None
        with (
            patch("griptape_nodes.retained_mode.managers.static_files_manager.start_static_server"),
            patch("griptape_nodes.retained_mode.managers.static_files_manager.bind_free_socket") as mock_bind,
        ):
            mock_bind.return_value.getsockname.return_value = ("localhost", 18124)
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
    async def test_media_bytes_are_written_from_inside_a_worker(self) -> None:
        """The same save, in the role that actually performs it.

        Saving is the one capability a worker keeps local, and the storage driver reaches the
        workspace path to do it. The manager guard that stops NODE code reading divergent state
        must not fire on that: a worker shares the workspace on disk, so the read is correct, and
        refusing it left every worker unable to write a single file.
        """
        current_engine().library_manager._is_worker = True
        _make("SaveMediaNode", "WorkerMedia")

        result = await _execute("SaveMediaNode", "WorkerMedia")

        assert "WorkerMedia.png" in result.parameter_output_values["url"]


class TestStateAccessFromAWorker:
    @pytest.mark.asyncio
    async def test_config_read_through_a_request_succeeds_in_a_worker(self) -> None:
        """The request path resolves for a node marked as running in a worker.

        Scoped honestly: this process has no forwarding configured, so the request is answered
        locally. What it pins is that the facade guard does not block the request path. That
        forwarding itself works over the wire is covered by test_worker_forwarding_reentrancy.py
        and by the real two-process verification.
        """
        current_engine().library_manager._is_worker = True
        _make("ReadConfigNode", "Reader")

        result = await _execute("ReadConfigNode", "Reader", config_key="workspace_directory")

        # A real value came back, which means the request resolved rather than being blocked.
        assert result.parameter_output_values["config_value"]

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
        from griptape_nodes.retained_mode.events.event_converter import converter
        from griptape_nodes.retained_mode.events.os_events import WriteFileRequest

        original = b"\x89PNG\r\n\x1a\n\x00\xff\xfe"
        wire = json.loads(json.dumps(converter.unstructure(WriteFileRequest(file_path="x.png", content=original))))
        round_tripped = converter.structure(wire, WriteFileRequest).content

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
    async def test_streaming_works_in_a_worker_too(self) -> None:
        current_engine().library_manager._is_worker = True
        _make("StreamingNode", "WorkerStreamer")

        result = await _execute("StreamingNode", "WorkerStreamer")

        assert result.parameter_output_values["stream"] == "alphabetagamma"


class TestMultiHopChain:
    @pytest.mark.asyncio
    async def test_three_hops_of_serializable_values(self) -> None:
        """Each hop's value round-trips through the orchestrator, as per-node dispatch requires."""
        current_engine().library_manager._is_worker = True
        for node_type, name in (
            ("ChainStartNode", "Start"),
            ("ChainMiddleNode", "Middle"),
            ("ChainEndNode", "End"),
        ):
            _make(node_type, name)

        start = await _execute("ChainStartNode", "Start")
        middle = await _execute("ChainMiddleNode", "Middle", in_value=start.parameter_output_values["out"])
        end = await _execute("ChainEndNode", "End", in_value=middle.parameter_output_values["out"])

        assert end.parameter_output_values["final"] == "start->middle->end"


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


class TestUnshippableOutputsAreRefused:
    """A value the author declared unserializable must not silently vanish across the boundary.

    Dropping it would leave the consuming node reading None with no error anywhere, which is
    the failure mode this guardrail exists to convert into a message an author can act on.
    """

    @pytest.mark.asyncio
    async def test_a_worker_refuses_to_ship_an_unserializable_output(self) -> None:
        current_engine().library_manager._is_worker = True
        _make("UnshippableOutputNode", "Unshippable")

        result = await current_engine().ahandle_request(
            ExecuteNodeRequest(
                node_name="Unshippable",
                parameter_values={},
                node_metadata={"node_type": "UnshippableOutputNode", "library": LIBRARY},
            )
        )

        assert result.failed()
        details = str(result.result_details)
        # The message has to name the parameter and tell the author what to do instead.
        assert "live_handle" in details
        assert "serializable" in details

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
    async def test_secret_read_through_a_request_reaches_the_engine(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Almost every real node needs an API key, and this is the path it must take.

        A worker's environment is frozen when it spawns, so a manager read can answer from a
        stale copy; the request is what reaches the process that owns the secret.
        """
        monkeypatch.setenv("GTN_WORKER_FIXTURE_SECRET", "fixture-secret-value")
        current_engine().library_manager._is_worker = True
        _make("ReadSecretNode", "SecretReader")

        result = await _execute("ReadSecretNode", "SecretReader")

        assert result.parameter_output_values["secret_value"] == "fixture-secret-value"  # noqa: S105

    def test_the_secret_getter_is_forwarded_from_a_worker(self) -> None:
        """The request has to be forwarded, or the read answers locally and the advice is false."""
        from griptape_nodes.app.worker_routing import LOCAL_ONLY_REQUEST_TYPES
        from griptape_nodes.retained_mode.events.secrets_events import GetSecretValueRequest

        assert GetSecretValueRequest not in LOCAL_ONLY_REQUEST_TYPES


class TestProjectReadsStayLocalInAWorker:
    """A worker answers project questions itself rather than forwarding them.

    Two reasons, and the second is why this is pinned. A worker already adopts the
    orchestrator's project and a project's base directory is shared on-disk state, so the
    local answer is correct and a round trip buys nothing on a path as hot as writing sidecar
    metadata for every saved file.

    The round trip also corrupted the answer. Forwarded results are rebuilt with cattrs, and
    GetCurrentProjectResultSuccess annotates `project_info: ProjectInfo` under TYPE_CHECKING to
    break an import cycle; cattrs cannot resolve that name, so the converter's NameError
    fallback hands the raw dict to the constructor. isinstance() then passes while
    `.project_info` is a dict, and every sidecar write in a worker failed with
    "'dict' object has no attribute 'project_base_dir'" and silently wrote nothing.

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

    Scoped honestly: this suite flips `_is_worker` but never installs the RemoteHandlers, so
    WriteFileRequest is answered locally whatever the routing says -- these do not guard the
    routing decision. What they cover is that a node writing binary through `File` works at all,
    in both roles, which the suite had no node for. The routing itself is guarded by the
    membership and converter tests above and by tests/unit/app/test_worker_routing_filesystem.py.
    """

    @pytest.mark.asyncio
    async def test_binary_survives_a_write_from_a_worker(self) -> None:
        current_engine().library_manager._is_worker = True
        _make("WriteBytesNode", "WorkerBytes")

        result = await _execute("WriteBytesNode", "WorkerBytes")

        # `bytes_survived` is the oracle: the node compares what it read against what it wrote,
        # in the one process where both values exist. The count guards against that comparison
        # passing over two empty reads.
        assert result.parameter_output_values["bytes_survived"] is True
        assert result.parameter_output_values["byte_count"] > 0

    @pytest.mark.asyncio
    async def test_binary_survives_a_write_on_the_orchestrator(self) -> None:
        current_engine().library_manager._is_worker = False
        _make("WriteBytesNode", "OrchestratorBytes")

        result = await _execute("WriteBytesNode", "OrchestratorBytes")

        assert result.parameter_output_values["bytes_survived"] is True
