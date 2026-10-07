"""End-to-end coverage for SubflowNodeGroup local execution.

Builds a flow that contains a SubflowGroupNode (concrete SubflowNodeGroup) with
an EchoNode inside it, serializes to a self-contained .py, and runs it in a
fresh subprocess.  Asserts that the EchoNode's text output survives the subflow
round-trip intact.

This guards the serialization/deserialization path exercised by
NodeExecutor._extract_parameter_output_values and
NodeExecutor._apply_parameter_values_to_node - the code fixed in
fix/subprocess-cattrs-bytes-deserialization.

test_private_execution_returns_result_without_griptape_cloud covers the Private
Execution (SubprocessWorkflowExecutor) path: the result must come back from the
child process with Griptape Cloud unreachable.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from griptape_nodes.node_library.workflow_registry import WorkflowMetadata, WorkflowShape
from griptape_nodes.retained_mode.events.flow_events import (
    CreateFlowRequest,
    CreateFlowResultSuccess,
    SerializeFlowToCommandsRequest,
    SerializeFlowToCommandsResultSuccess,
)
from griptape_nodes.retained_mode.events.library_events import (
    RegisterLibraryFromFileRequest,
    RegisterLibraryFromFileResultSuccess,
)
from griptape_nodes.retained_mode.events.node_events import CreateNodeRequest, CreateNodeResultSuccess
from griptape_nodes.retained_mode.events.object_events import ClearAllObjectStateRequest
from griptape_nodes.retained_mode.events.parameter_events import SetParameterValueRequest

if TYPE_CHECKING:
    from collections.abc import Callable

    from griptape_nodes.retained_mode.engine import Engine

# Timeout with thread dump.
pytestmark = pytest.mark.timeout(300, method="thread")

FIXTURE_LIBRARY_DIR = Path(__file__).parent / "fixtures" / "subflow_library"
FIXTURE_LIBRARY_JSON_TEMPLATE = FIXTURE_LIBRARY_DIR / "griptape_nodes_library.json"

_EXPECTED_TEXT = "hello from subflow"


def _generate_subflow_workflow_source(
    engine: Engine, library_json: Path, workflow_shape: WorkflowShape | None = None
) -> str:
    """Build a flow with a SubflowGroupNode containing an EchoNode and serialize it.

    A workflow_shape makes the file define execute_workflow(), which is how a subprocess runs it.
    """
    engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))

    register_result = engine.handle_request(RegisterLibraryFromFileRequest(file_path=str(library_json)))
    assert isinstance(register_result, RegisterLibraryFromFileResultSuccess), register_result

    engine.context_manager.push_workflow(workflow_name="subflow_e2e_workflow")

    flow_result = engine.handle_request(
        CreateFlowRequest(parent_flow_name=None, flow_name="ControlFlow_1", set_as_new_context=False)
    )
    assert isinstance(flow_result, CreateFlowResultSuccess), flow_result
    flow_name = flow_result.flow_name

    with engine.context_manager.flow(flow_name):
        group_result = engine.handle_request(
            CreateNodeRequest(
                node_type="SubflowGroupNode",
                specific_library_name="Subflow Library",
                node_name="SubflowGroup_1",
                initial_setup=True,
            )
        )
        assert isinstance(group_result, CreateNodeResultSuccess), group_result
        assert group_result.node_type == "SubflowGroupNode", (
            f"Expected SubflowGroupNode, got {group_result.node_type!r}"
        )

        echo_result = engine.handle_request(
            CreateNodeRequest(
                node_type="EchoNode",
                specific_library_name="Subflow Library",
                node_name="Echo_1",
                initial_setup=True,
                parent_group_name=group_result.node_name,
            )
        )
        assert isinstance(echo_result, CreateNodeResultSuccess), echo_result

    engine.handle_request(
        SetParameterValueRequest(
            parameter_name="text",
            value=_EXPECTED_TEXT,
            node_name=echo_result.node_name,
        )
    )

    serialize_result = engine.handle_request(SerializeFlowToCommandsRequest(flow_name=flow_name))
    assert isinstance(serialize_result, SerializeFlowToCommandsResultSuccess), serialize_result

    metadata = WorkflowMetadata(
        name="subflow_e2e_workflow",
        schema_version=WorkflowMetadata.LATEST_SCHEMA_VERSION,
        engine_version_created_with="0.0.0",
        node_libraries_referenced=list(serialize_result.serialized_flow_commands.node_dependencies.libraries),
        workflow_shape=workflow_shape,
    )
    return engine.workflow_manager.codegen.generate_workflow_file_content(
        serialized_flow_commands=serialize_result.serialized_flow_commands,
        workflow_metadata=metadata,
    )


def _wrap_with_runtime_assertions(workflow_source: str) -> str:
    runtime_block = f"""

import asyncio as _e2e_asyncio
import logging as _e2e_logging

from griptape_nodes.bootstrap.workflow_executors.local_workflow_executor import (
    LocalWorkflowExecutor as _E2ELocalWorkflowExecutor,
)
from griptape_nodes.drivers.storage.storage_backend import StorageBackend as _E2EStorageBackend
from griptape_nodes.retained_mode.events.flow_events import (
    GetTopLevelFlowRequest as _E2EGetTopLevelFlowRequest,
    GetTopLevelFlowResultSuccess as _E2EGetTopLevelFlowResultSuccess,
)
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes as _E2EGriptapeNodes

_EXPECTED_TEXT = {_EXPECTED_TEXT!r}


def _ensure_workflow_context() -> None:
    context_manager = _E2EGriptapeNodes.ContextManager()
    if not context_manager.has_current_flow():
        top_level = _E2EGriptapeNodes.handle_request(_E2EGetTopLevelFlowRequest())
        if isinstance(top_level, _E2EGetTopLevelFlowResultSuccess) and top_level.flow_name is not None:
            flow_obj = _E2EGriptapeNodes.FlowManager().get_flow_by_name(top_level.flow_name)
            context_manager.push_flow(flow_obj)


async def _e2e_run() -> None:
    await build_workflow()  # noqa: F821 - defined by the generated workflow source above
    _ensure_workflow_context()
    workflow_executor = _E2ELocalWorkflowExecutor(
        storage_backend=_E2EStorageBackend.LOCAL,
        skip_library_loading=True,
        workflows_to_register=[__file__],
    )
    async with workflow_executor as executor:
        await executor.arun(flow_input={{}})

    node_manager = _E2EGriptapeNodes.NodeManager()
    echo_node = node_manager.get_node_by_name("Echo_1")
    if echo_node is None:
        raise RuntimeError("E2E_FAIL: Echo_1 not found after subflow execution")
    text_value = echo_node.parameter_output_values.get("text")
    if text_value != _EXPECTED_TEXT:
        raise RuntimeError(
            f"E2E_FAIL: expected {{_EXPECTED_TEXT!r}}, got {{text_value!r}} - "
            "text was corrupted across the subflow boundary"
        )
    print(f"SUBFLOW_TEXT_OK text={{text_value!r}}", flush=True)


if __name__ == "__main__":
    _e2e_logging.basicConfig(level=_e2e_logging.WARNING)
    _e2e_asyncio.run(_e2e_run())
"""
    return workflow_source + runtime_block


@pytest.mark.skipif(
    not FIXTURE_LIBRARY_JSON_TEMPLATE.exists(),
    reason=f"Subflow Library fixture missing at {FIXTURE_LIBRARY_JSON_TEMPLATE}",
)
def test_subflow_node_group_propagates_output_values(
    tmp_path: Path,
    engine: Engine,
    engine_subprocess_env: Callable[..., dict[str, str]],
    materialize_library: Callable[..., Path],
    write_isolated_config: Callable[..., None],
) -> None:
    """A SubflowNodeGroup must propagate child node outputs back to the parent flow.

    Drives the real serializer + LocalWorkflowExecutor so a regression in
    SubflowNodeGroup._propagate_output_values_from_internal_nodes or the
    parameter-value round-trip fails this test.
    """
    workspace = tmp_path / "workspace"
    # The engine's ConfigManager creates the configured workspace on init, so this only has to
    # cover the case where it has not.
    workspace.mkdir(exist_ok=True)
    config_root = tmp_path / "xdg_config"
    library_json = materialize_library(
        tmp_path / "library",
        template=FIXTURE_LIBRARY_JSON_TEMPLATE,
        node_file=FIXTURE_LIBRARY_DIR / "subflow_echo_node.py",
    )
    write_isolated_config(config_root, workspace=workspace, library_path=library_json)

    workflow_source = _generate_subflow_workflow_source(engine, library_json)
    runnable_source = _wrap_with_runtime_assertions(workflow_source)

    workflow_path = tmp_path / "subflow_workflow.py"
    workflow_path.write_text(runnable_source)

    # Isolated config dir + the GT_CLOUD_API_KEY placeholder the bootstrap needs. The child
    # runs hermetically in LOCAL storage because conftest.py's _isolated_engine_env keeps real
    # cloud secrets out of os.environ.
    env = engine_subprocess_env(XDG_CONFIG_HOME=str(config_root))

    result = subprocess.run(  # noqa: S603
        [sys.executable, str(workflow_path)],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    diagnostic = (
        f"workflow exit code: {result.returncode}\n--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    assert result.returncode == 0, diagnostic
    assert "SUBFLOW_TEXT_OK" in result.stdout, diagnostic
    assert "E2E_FAIL" not in result.stdout, diagnostic
    assert "E2E_FAIL" not in result.stderr, diagnostic


# Runs as the parent engine: starts SubprocessWorkflowExecutor, the Private Execution path,
# which runs the workflow in a child process and reads its result back over the event channel.
_PRIVATE_EXECUTION_DRIVER = """
import asyncio
import json
import sys

from griptape_nodes.bootstrap.workflow_executors.subprocess_workflow_executor import SubprocessWorkflowExecutor


async def _main() -> None:
    executor = SubprocessWorkflowExecutor(workflow_path=sys.argv[1])
    async with executor:
        await executor.arun(flow_input={})
    print("PRIVATE_EXECUTION_OUTPUT=" + json.dumps(executor.output, default=str), flush=True)


asyncio.run(_main())
"""


@pytest.mark.skipif(
    not FIXTURE_LIBRARY_JSON_TEMPLATE.exists(),
    reason=f"Subflow Library fixture missing at {FIXTURE_LIBRARY_JSON_TEMPLATE}",
)
def test_private_execution_returns_result_without_griptape_cloud(
    tmp_path: Path,
    engine: Engine,
    engine_subprocess_env: Callable[..., dict[str, str]],
    materialize_library: Callable[..., Path],
    write_isolated_config: Callable[..., None],
) -> None:
    """A Private Execution run must hand its result back to the parent with no Griptape Cloud.

    The Cloud API URL points at a closed local port, so any attempt to reach Griptape Cloud
    fails. The result has to travel over a channel the parent owns.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    config_root = tmp_path / "xdg_config"
    library_json = materialize_library(
        tmp_path / "library",
        template=FIXTURE_LIBRARY_JSON_TEMPLATE,
        node_file=FIXTURE_LIBRARY_DIR / "subflow_echo_node.py",
    )
    write_isolated_config(config_root, workspace=workspace, library_path=library_json)

    workflow_path = tmp_path / "subflow_workflow.py"
    workflow_path.write_text(
        _generate_subflow_workflow_source(engine, library_json, WorkflowShape(inputs={}, outputs={}))
    )
    driver_path = tmp_path / "private_execution_driver.py"
    driver_path.write_text(_PRIVATE_EXECUTION_DRIVER)

    env = engine_subprocess_env(
        XDG_CONFIG_HOME=str(config_root),
        GRIPTAPE_NODES_API_BASE_URL="http://127.0.0.1:9",
    )

    result = subprocess.run(  # noqa: S603
        [sys.executable, str(driver_path), str(workflow_path)],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    diagnostic = (
        f"driver exit code: {result.returncode}\n--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    assert result.returncode == 0, diagnostic
    output_lines = [line for line in result.stdout.splitlines() if line.startswith("PRIVATE_EXECUTION_OUTPUT=")]
    assert output_lines, diagnostic
    # executor.output is only ever set from the result event the child sends over the channel.
    output = json.loads(output_lines[-1].removeprefix("PRIVATE_EXECUTION_OUTPUT="))
    assert output is not None, diagnostic
    assert output["SubflowGroup_1"]["execution_environment"] == "Local Execution", diagnostic
