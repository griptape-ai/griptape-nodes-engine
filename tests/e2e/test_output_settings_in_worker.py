"""A node saving through a project output parameter honors the settings node wired to it, wherever it runs.

The settings node lives on the orchestrator. A worker's copy of the saving node has no settings node
beside it, so it asks for each destination with a ``GetConnected...DestinationRequest``, answered
where the settings node lives. Each test runs the same graph in-process and in a worker
(``tests.simulated_worker``) and expects the same files.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from griptape_nodes.retained_mode.engine import current_engine
from griptape_nodes.retained_mode.events.base_events import ResultPayloadFailure
from griptape_nodes.retained_mode.events.connection_events import CreateConnectionRequest, CreateConnectionResultSuccess
from griptape_nodes.retained_mode.events.destination_events import (
    GetConnectedDirectoryDestinationRequest,
    GetConnectedFileDestinationRequest,
    GetConnectedFileSequenceDestinationRequest,
)
from griptape_nodes.retained_mode.events.execution_events import (
    ExecuteNodeRequest,
    ExecuteNodeResultFailure,
    ExecuteNodeResultSuccess,
)
from griptape_nodes.retained_mode.events.flow_events import CreateFlowRequest, CreateFlowResultSuccess
from griptape_nodes.retained_mode.events.library_events import (
    RegisterLibraryFromFileRequest,
    RegisterLibraryFromFileResultSuccess,
)
from griptape_nodes.retained_mode.events.node_events import CreateNodeRequest, CreateNodeResultSuccess
from griptape_nodes.retained_mode.events.parameter_events import SetParameterValueRequest
from griptape_nodes.retained_mode.events.project_events import ActivateWorkspaceProjectRequest
from griptape_nodes.testing.libraries import WORKER_BEHAVIOR_LIBRARY_NAME, materialize_worker_behavior_library
from tests.simulated_worker import SimulatedWorker, simulated_worker

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from griptape_nodes.retained_mode.events.base_events import ResultPayload

_CANVAS_FLOW_NAME = "output_settings_canvas"
_SETTINGS = "Settings"
_SAVER = "Saver"
_WIRES = (
    ("file_destination", "output_file"),
    ("directory_destination", "output_dir"),
    ("file_sequence_destination", "output_sequence"),
)


@pytest.fixture(autouse=True)
def _register_library(tmp_path: Path) -> None:
    library_json = materialize_worker_behavior_library(tmp_path)
    result = current_engine().handle_request(RegisterLibraryFromFileRequest(file_path=str(library_json)))
    assert isinstance(result, RegisterLibraryFromFileResultSuccess), getattr(result, "result_details", result)
    activation = current_engine().handle_request(ActivateWorkspaceProjectRequest())
    assert not isinstance(activation, ResultPayloadFailure), getattr(activation, "result_details", activation)


@pytest.fixture(params=["in_process", "worker"])
def venue(request: pytest.FixtureRequest) -> Iterator[None]:
    """Run the saving node in-process, or send it to a worker."""
    if request.param == "in_process":
        yield
        return
    with simulated_worker(current_engine(), WORKER_BEHAVIOR_LIBRARY_NAME):
        yield


@pytest.fixture
def worker() -> Iterator[SimulatedWorker]:
    """A worker every node of the library is sent to."""
    with simulated_worker(current_engine(), WORKER_BEHAVIOR_LIBRARY_NAME) as simulated:
        yield simulated


def _create(node_type: str, name: str) -> None:
    engine = current_engine()
    if not engine.context_manager.has_current_workflow():
        engine.context_manager.push_workflow(workflow_name="output_settings_workflow")
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
    assert result.node_name == name


def _set(node_name: str, parameter_name: str, value: str) -> None:
    result = current_engine().handle_request(
        SetParameterValueRequest(node_name=node_name, parameter_name=parameter_name, value=value)
    )
    assert result.succeeded(), result


def _build_graph(name_base: str, if_exists: str = "overwrite") -> None:
    """A settings node wired into all three output parameters of a saving node."""
    _create("OutputSettingsNode", _SETTINGS)
    _create("SavesThroughOutputParametersNode", _SAVER)
    _set(_SETTINGS, "name_base", name_base)
    _set(_SETTINGS, "if_exists", if_exists)
    for source_parameter, target_parameter in _WIRES:
        result = current_engine().handle_request(
            CreateConnectionRequest(
                source_node_name=_SETTINGS,
                source_parameter_name=source_parameter,
                target_node_name=_SAVER,
                target_parameter_name=target_parameter,
            )
        )
        assert isinstance(result, CreateConnectionResultSuccess), result


async def _run_saver() -> ResultPayload:
    return await current_engine().ahandle_request(
        ExecuteNodeRequest(
            node_name=_SAVER,
            node_metadata={"node_type": "SavesThroughOutputParametersNode", "library": WORKER_BEHAVIOR_LIBRARY_NAME},
        )
    )


@pytest.mark.usefixtures("venue")
class TestTheSettingsNodeDecidesWhereFilesLand:
    @pytest.mark.asyncio
    async def test_each_output_lands_where_the_settings_node_says(self) -> None:
        _build_graph("Custom")

        result = await _run_saver()

        assert isinstance(result, ExecuteNodeResultSuccess), getattr(result, "result_details", result)
        outputs = result.parameter_output_values
        # The macro form proves the file destination stayed a ProjectFileDestination across the wire.
        assert outputs["saved_file"] == "{outputs}/settings/Custom.bin"
        assert outputs["created_dir"] == "{outputs}/settings/Custom_dir"
        assert str(outputs["written_frame"]).replace("\\", "/").endswith("/settings/Custom_seq/Custom.0001.bin")

    @pytest.mark.asyncio
    async def test_the_existing_file_policy_holds(self) -> None:
        _build_graph("Refused", if_exists="fail")
        first = await _run_saver()
        assert isinstance(first, ExecuteNodeResultSuccess), getattr(first, "result_details", first)

        second = await _run_saver()

        assert isinstance(second, ExecuteNodeResultFailure), second

    @pytest.mark.asyncio
    async def test_a_settings_node_with_no_destination_fails_the_run(self) -> None:
        _build_graph("")

        result = await _run_saver()

        assert isinstance(result, ExecuteNodeResultFailure), result
        assert f"'{_SETTINGS}' has no destination set" in str(result.result_details)


class TestAWorkerAsksWhereTheSettingsNodeLives:
    @pytest.mark.asyncio
    async def test_each_destination_is_read_on_the_orchestrator(self, worker: SimulatedWorker) -> None:
        _build_graph("Asked")

        result = await _run_saver()

        assert isinstance(result, ExecuteNodeResultSuccess), getattr(result, "result_details", result)
        forwarded = {type(request) for request in worker.forwarded}
        assert {
            GetConnectedFileDestinationRequest,
            GetConnectedDirectoryDestinationRequest,
            GetConnectedFileSequenceDestinationRequest,
        } <= forwarded
