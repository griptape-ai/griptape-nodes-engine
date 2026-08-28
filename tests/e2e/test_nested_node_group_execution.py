"""End-to-end coverage for node groups nested inside other node groups.

Builds Source -> Outer[ Inner[ Leaf ] ], connects Source directly to the doubly-nested Leaf so the
data has to cross two group boundaries, then saves the graph to a self-contained .py and runs it in
a fresh subprocess.

Nesting is what these tests are about, so they guard the places where "inside a group" has to mean
transitively inside rather than directly inside: the proxy parameters that carry a connection across
each boundary, the flow hierarchy the subflows are built into, and the generated file's record of
which nodes belong to which group.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from griptape_nodes.node_library.workflow_registry import WorkflowMetadata
from griptape_nodes.retained_mode.events.connection_events import CreateConnectionRequest, CreateConnectionResultSuccess
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
from griptape_nodes.retained_mode.events.node_events import (
    AddNodesToNodeGroupRequest,
    AddNodesToNodeGroupResultSuccess,
    CreateNodeRequest,
    CreateNodeResultSuccess,
)
from griptape_nodes.retained_mode.events.object_events import ClearAllObjectStateRequest
from griptape_nodes.retained_mode.events.parameter_events import SetParameterValueRequest

if TYPE_CHECKING:
    from collections.abc import Callable

    from griptape_nodes.retained_mode.engine import Engine

# Timeout with thread dump.
pytestmark = pytest.mark.timeout(300, method="thread")

FIXTURE_LIBRARY_DIR = Path(__file__).parent / "fixtures" / "subflow_library"
FIXTURE_LIBRARY_JSON_TEMPLATE = FIXTURE_LIBRARY_DIR / "griptape_nodes_library.json"
FIXTURE_NODE_FILE = FIXTURE_LIBRARY_DIR / "subflow_echo_node.py"

_EXPECTED_TEXT = "hello from a nested subflow"


def _build_nested_graph(engine: Engine, library_name: str) -> str:
    """Build Source -> Outer[ Inner[ Leaf ] ] and return the top-level flow name."""
    flow_result = engine.handle_request(
        CreateFlowRequest(parent_flow_name=None, flow_name="NestedParentFlow", set_as_new_context=False)
    )
    assert isinstance(flow_result, CreateFlowResultSuccess), flow_result
    flow_name = flow_result.flow_name

    with engine.context_manager.flow(flow_name):
        outer = engine.handle_request(
            CreateNodeRequest(node_type="SubflowGroupNode", specific_library_name=library_name, node_name="OuterGroup")
        )
        assert isinstance(outer, CreateNodeResultSuccess), outer

        inner = engine.handle_request(
            CreateNodeRequest(node_type="SubflowGroupNode", specific_library_name=library_name, node_name="InnerGroup")
        )
        assert isinstance(inner, CreateNodeResultSuccess), inner

        leaf = engine.handle_request(
            CreateNodeRequest(
                node_type="EchoNode",
                specific_library_name=library_name,
                node_name="Leaf",
                parent_group_name=inner.node_name,
            )
        )
        assert isinstance(leaf, CreateNodeResultSuccess), leaf

        source = engine.handle_request(
            CreateNodeRequest(node_type="EchoNode", specific_library_name=library_name, node_name="Source")
        )
        assert isinstance(source, CreateNodeResultSuccess), source

        # Nest the inner group (which already holds Leaf) inside the outer group.
        nest_result = engine.handle_request(
            AddNodesToNodeGroupRequest(node_names=[inner.node_name], node_group_name=outer.node_name)
        )
        assert isinstance(nest_result, AddNodesToNodeGroupResultSuccess), nest_result

        engine.handle_request(
            SetParameterValueRequest(parameter_name="text", node_name=source.node_name, value=_EXPECTED_TEXT)
        )

        # Crossing two boundaries at once is the case that has to keep working: the engine routes
        # this through a proxy parameter on each group it passes through.
        connect_result = engine.handle_request(
            CreateConnectionRequest(
                source_node_name=source.node_name,
                source_parameter_name="text",
                target_node_name=leaf.node_name,
                target_parameter_name="text",
            )
        )
        assert isinstance(connect_result, CreateConnectionResultSuccess), connect_result

    return flow_name


def _generate_nested_workflow_source(engine: Engine, library_json: Path) -> str:
    """Build the nested graph and serialize it to standalone workflow source."""
    engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))

    register_result = engine.handle_request(RegisterLibraryFromFileRequest(file_path=str(library_json)))
    assert isinstance(register_result, RegisterLibraryFromFileResultSuccess), register_result
    library_name = register_result.library_name

    engine.context_manager.push_workflow(workflow_name="nested_group_e2e_workflow")
    flow_name = _build_nested_graph(engine, library_name)

    serialize_result = engine.handle_request(SerializeFlowToCommandsRequest(flow_name=flow_name))
    assert isinstance(serialize_result, SerializeFlowToCommandsResultSuccess), serialize_result

    metadata = WorkflowMetadata(
        name="nested_group_e2e_workflow",
        schema_version=WorkflowMetadata.LATEST_SCHEMA_VERSION,
        engine_version_created_with="0.0.0",
        node_libraries_referenced=list(serialize_result.serialized_flow_commands.node_dependencies.libraries),
        workflow_shape=None,
    )
    return engine.workflow_manager._generate_workflow_file_content(
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


def _check_nesting_rebuilt() -> None:
    node_manager = _E2EGriptapeNodes.NodeManager()
    outer = node_manager.get_node_by_name("OuterGroup")
    inner = node_manager.get_node_by_name("InnerGroup")
    leaf = node_manager.get_node_by_name("Leaf")

    # Membership has to survive the save: the inner group belongs to the outer one, and the leaf
    # belongs to the inner one.
    if inner.name not in outer.nodes:
        raise RuntimeError(
            f"E2E_FAIL: OuterGroup lost its nested group; members={{sorted(outer.nodes)}}"
        )
    if leaf.name not in inner.nodes:
        raise RuntimeError(f"E2E_FAIL: InnerGroup lost its child; members={{sorted(inner.nodes)}}")
    if not outer.contains_node(leaf):
        raise RuntimeError("E2E_FAIL: OuterGroup does not transitively contain Leaf")

    # The subflows have to be nested the same way the groups are.
    flow_manager = _E2EGriptapeNodes.FlowManager()
    inner_subflow = inner.metadata.get("subflow_name")
    outer_subflow = outer.metadata.get("subflow_name")
    if flow_manager.get_parent_flow(inner_subflow) != outer_subflow:
        raise RuntimeError(
            f"E2E_FAIL: {{inner_subflow}} parent is {{flow_manager.get_parent_flow(inner_subflow)!r}}, "
            f"expected {{outer_subflow!r}}"
        )


async def _e2e_run() -> None:
    await build_workflow()  # noqa: F821 - defined by the generated workflow source above
    _ensure_workflow_context()
    _check_nesting_rebuilt()
    workflow_executor = _E2ELocalWorkflowExecutor(
        storage_backend=_E2EStorageBackend.LOCAL,
        skip_library_loading=True,
        workflows_to_register=[__file__],
    )
    async with workflow_executor as executor:
        await executor.arun(flow_input={{}})

    node_manager = _E2EGriptapeNodes.NodeManager()
    leaf_node = node_manager.get_node_by_name("Leaf")
    text_value = leaf_node.parameter_output_values.get("text")
    if text_value != _EXPECTED_TEXT:
        raise RuntimeError(
            f"E2E_FAIL: expected {{_EXPECTED_TEXT!r}}, got {{text_value!r}} - "
            "text did not survive two nested group boundaries"
        )
    print(f"NESTED_SUBFLOW_TEXT_OK text={{text_value!r}}", flush=True)


if __name__ == "__main__":
    _e2e_logging.basicConfig(level=_e2e_logging.WARNING)
    _e2e_asyncio.run(_e2e_run())
"""
    return workflow_source + runtime_block


@pytest.mark.skipif(
    not FIXTURE_LIBRARY_JSON_TEMPLATE.exists(),
    reason=f"Subflow Library fixture missing at {FIXTURE_LIBRARY_JSON_TEMPLATE}",
)
def test_nested_node_groups_survive_save_and_execute(
    tmp_path: Path,
    engine: Engine,
    engine_subprocess_env: Callable[..., dict[str, str]],
    materialize_library: Callable[..., Path],
    write_isolated_config: Callable[..., None],
) -> None:
    """A group nested in another group must save, reload, and pass data to its deepest node."""
    workspace = tmp_path / "workspace"
    # The engine's ConfigManager creates the configured workspace on init, so this only has to
    # cover the case where it has not.
    workspace.mkdir(exist_ok=True)
    config_root = tmp_path / "xdg_config"
    library_json = materialize_library(
        tmp_path / "library", template=FIXTURE_LIBRARY_JSON_TEMPLATE, node_file=FIXTURE_NODE_FILE
    )
    write_isolated_config(config_root, workspace=workspace, library_path=library_json)

    workflow_source = _generate_nested_workflow_source(engine, library_json)
    runnable_source = _wrap_with_runtime_assertions(workflow_source)

    workflow_path = tmp_path / "nested_group_workflow.py"
    workflow_path.write_text(runnable_source)

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
    assert "NESTED_SUBFLOW_TEXT_OK" in result.stdout, diagnostic
    assert "E2E_FAIL" not in result.stdout, diagnostic
    assert "E2E_FAIL" not in result.stderr, diagnostic
