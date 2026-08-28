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
    @pytest.mark.asyncio
    async def test_worker_asset_url_uses_the_orchestrators_server(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A URL a worker produces must be served by something that outlives the worker.

        Without this the worker advertises its own OS-assigned port, so the URL dies with the
        worker and a saved workflow reopens with a dead link.
        """
        orchestrator_url = "http://localhost:8124"
        monkeypatch.setenv(ORCHESTRATOR_STATIC_SERVER_BASE_URL_ENV, orchestrator_url)
        # Re-resolve the static server with the worker's environment in place. The payload
        # carries no host URL, which is exactly the worker case: without the env var this
        # would start a server here and advertise its own ephemeral port.
        static_files_manager = current_engine().static_files_manager
        static_files_manager._static_server_base_url = None
        static_files_manager.on_app_initialization_complete(AppInitializationComplete(is_worker=True))

        assert static_files_manager.static_server_base_url == orchestrator_url

    def test_without_the_env_var_a_process_serves_the_workspace_itself(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The orchestrator's own path is unchanged: no env var, so it serves and advertises."""
        monkeypatch.delenv(ORCHESTRATOR_STATIC_SERVER_BASE_URL_ENV, raising=False)
        static_files_manager = current_engine().static_files_manager
        static_files_manager._static_server_base_url = None
        static_files_manager.on_app_initialization_complete(AppInitializationComplete())

        assert static_files_manager.static_server_base_url.startswith("http://")

    @pytest.mark.asyncio
    async def test_media_bytes_are_written_and_a_url_returned(self) -> None:
        """Bytes stay local (they cannot cross the boundary); the URL is what travels."""
        _make("SaveMediaNode", "Media")

        result = await _execute("SaveMediaNode", "Media")

        url = result.parameter_output_values["url"]
        assert isinstance(url, str)
        assert url  # a real URL, not an empty default
        assert "Media.png" in url


class TestStateAccessFromAWorker:
    @pytest.mark.asyncio
    async def test_config_read_through_a_request_succeeds_in_a_worker(self) -> None:
        """The sanctioned path works from inside a worker, where the manager path is refused."""
        current_engine().library_manager._is_worker = True
        _make("ReadConfigNode", "Reader")

        result = await _execute("ReadConfigNode", "Reader", config_key="workspace_directory")

        # A real value came back, which means the request resolved rather than being blocked.
        assert result.parameter_output_values["config_value"]

    def test_config_getter_forwards_from_a_worker(self) -> None:
        """The getter must be in the forwarded set, or a worker reads its own stale copy.

        The guardrail tells authors to use this request instead of the manager; that advice is
        only true if the request actually reaches the orchestrator.
        """
        from griptape_nodes.app.worker_routing import LOCAL_ONLY_REQUEST_TYPES
        from griptape_nodes.retained_mode.events.config_events import GetConfigValueRequest
        from griptape_nodes.retained_mode.events.os_events import ReadFileRequest, WriteFileRequest
        from griptape_nodes.retained_mode.events.secrets_events import GetSecretValueRequest

        # Forwarding is the default now, so the assertion is that these are NOT excluded.
        # Every request the guardrails name has to reach the orchestrator, or the advice
        # they give authors is false.
        for request_type in (GetConfigValueRequest, GetSecretValueRequest, ReadFileRequest, WriteFileRequest):
            assert request_type not in LOCAL_ONLY_REQUEST_TYPES, request_type.__name__


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
