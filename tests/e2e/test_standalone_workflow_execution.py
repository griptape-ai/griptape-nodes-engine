"""End-to-end coverage for self-executable workflow files.

Drives the real workflow generator end-to-end: registers a fixture library inside
the test process, builds a flow that uses one of its nodes, serializes the flow
through ``WorkflowManager._generate_workflow_file_content``, writes the emitted
``.py`` to disk, and runs it in a fresh ``python <file>`` subprocess.

This is the bootstrap path issue #4584 describes: ``LocalWorkflowExecutor`` with
``skip_library_loading=True``, ``build_workflow()`` running before the executor's
``__aenter__``, and no engine-driven preregistration. If the generator stops emitting
``RegisterLibraryFromFileRequest`` calls inside ``build_workflow()``, every
``CreateNodeRequest`` in the subprocess collapses into ``ErrorProxyNode`` and this
test fails.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from griptape_nodes.node_library.workflow_registry import WorkflowMetadata
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
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

if TYPE_CHECKING:
    from collections.abc import Callable

# Timeout with thread dump.
pytestmark = pytest.mark.timeout(300, method="thread")

FIXTURE_LIBRARY_DIR = Path(__file__).parent / "fixtures" / "echo_library"
FIXTURE_LIBRARY_JSON_TEMPLATE = FIXTURE_LIBRARY_DIR / "griptape_nodes_library.json"
FIXTURE_NODE_FILE = FIXTURE_LIBRARY_DIR / "echo_node.py"


def _generate_echo_workflow_source(library_json: Path) -> str:
    """Build a flow with one EchoNode and serialize it to a Python module.

    Uses the same path the engine uses when saving a workflow: register the library,
    create a flow, drop a node into it, ask FlowManager to serialize, then run the
    serialized commands through ``WorkflowManager._generate_workflow_file_content``.
    The resulting source is what a user would see saved to disk.
    """
    GriptapeNodes.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))

    register_result = GriptapeNodes.handle_request(RegisterLibraryFromFileRequest(file_path=str(library_json)))
    assert isinstance(register_result, RegisterLibraryFromFileResultSuccess), register_result

    GriptapeNodes.ContextManager().push_workflow(workflow_name="echo_e2e_workflow")

    flow_result = GriptapeNodes.handle_request(
        CreateFlowRequest(parent_flow_name=None, flow_name="ControlFlow_1", set_as_new_context=False)
    )
    assert isinstance(flow_result, CreateFlowResultSuccess), flow_result

    node_result = GriptapeNodes.handle_request(
        CreateNodeRequest(
            node_type="EchoNode",
            specific_library_name="Echo Library",
            node_name="Echo_1",
            override_parent_flow_name=flow_result.flow_name,
        )
    )
    assert isinstance(node_result, CreateNodeResultSuccess), node_result
    assert node_result.node_type == "EchoNode", (
        f"Sanity: in-process registration must yield real node, got {node_result.node_type!r}"
    )

    serialize_result = GriptapeNodes.handle_request(SerializeFlowToCommandsRequest(flow_name=flow_result.flow_name))
    assert isinstance(serialize_result, SerializeFlowToCommandsResultSuccess), serialize_result

    metadata = WorkflowMetadata(
        name="echo_e2e_workflow",
        schema_version=WorkflowMetadata.LATEST_SCHEMA_VERSION,
        engine_version_created_with="0.0.0",
        node_libraries_referenced=list(serialize_result.serialized_flow_commands.node_dependencies.libraries),
        # Skip the executable wrapper; we only need build_workflow to run end-to-end.
        # _generate_workflow_execution gates the __main__ block on workflow_shape, so
        # workflow_shape=None keeps the file inert at import time.
        workflow_shape=None,
    )
    return GriptapeNodes.WorkflowManager()._generate_workflow_file_content(
        serialized_flow_commands=serialize_result.serialized_flow_commands,
        workflow_metadata=metadata,
    )


def _wrap_with_runtime_assertions(workflow_source: str) -> str:
    """Append a ``__main__`` block that runs build_workflow and prints the resulting node type.

    The generator only emits ``__main__`` for shape-bearing workflows (those with
    StartFlow/EndFlow), and the fixture library has neither. Glue on a tiny driver
    that mirrors how the generator's ``aexecute_workflow`` invokes build_workflow
    inside ``LocalWorkflowExecutor``, then prints the actual node type so the test
    process can observe whether the registration step worked.
    """
    runtime_block = """

import asyncio as _e2e_asyncio
import logging as _e2e_logging

from griptape_nodes.bootstrap.workflow_executors.local_workflow_executor import (
    LocalWorkflowExecutor as _E2ELocalWorkflowExecutor,
)
from griptape_nodes.drivers.storage.storage_backend import StorageBackend as _E2EStorageBackend
from griptape_nodes.retained_mode.events.flow_events import (
    GetTopLevelFlowRequest as _E2EGetTopLevelFlowRequest,
    GetTopLevelFlowResultSuccess as _E2EGetTopLevelFlowResultSuccess,
    ListNodesInFlowRequest as _E2EListNodesInFlowRequest,
    ListNodesInFlowResultSuccess as _E2EListNodesInFlowResultSuccess,
)


async def _e2e_run() -> None:
    # Mirror generator-emitted aexecute_workflow ordering: build the graph first, then
    # enter the executor. This is the path #4584 describes, where the standalone
    # subprocess has no engine bootstrap registering libraries before build_workflow.
    await build_workflow()  # noqa: F821 - defined by exec'd workflow source above
    top_level = await GriptapeNodes.ahandle_request(_E2EGetTopLevelFlowRequest())  # noqa: F821
    if not isinstance(top_level, _E2EGetTopLevelFlowResultSuccess) or top_level.flow_name is None:
        raise RuntimeError(f"E2E_FAIL: no top-level flow after build_workflow: {top_level}")
    list_nodes = await GriptapeNodes.ahandle_request(  # noqa: F821
        _E2EListNodesInFlowRequest(flow_name=top_level.flow_name)
    )
    if not isinstance(list_nodes, _E2EListNodesInFlowResultSuccess):
        raise RuntimeError(f"E2E_FAIL: could not list nodes: {list_nodes}")
    node_manager = GriptapeNodes.NodeManager()  # noqa: F821
    for node_name in list_nodes.node_names:
        node = node_manager.get_node_by_name(node_name)
        print(f"NODE_TYPE name={node_name} type={type(node).__name__}", flush=True)
    workflow_executor = _E2ELocalWorkflowExecutor(
        storage_backend=_E2EStorageBackend.LOCAL,
        skip_library_loading=True,
        workflows_to_register=[__file__],
    )
    async with workflow_executor:
        pass


if __name__ == "__main__":
    _e2e_logging.basicConfig(level=_e2e_logging.WARNING)
    _e2e_asyncio.run(_e2e_run())
"""
    return workflow_source + runtime_block


@pytest.mark.skipif(
    not FIXTURE_LIBRARY_JSON_TEMPLATE.exists(),
    reason=f"Echo Library fixture missing at {FIXTURE_LIBRARY_JSON_TEMPLATE}",
)
def test_standalone_workflow_registers_declared_libraries(
    tmp_path: Path,
    engine_subprocess_env: Callable[..., dict[str, str]],
    materialize_library: Callable[..., Path],
    write_isolated_config: Callable[..., None],
) -> None:
    """A generated workflow run via subprocess must produce real nodes, not proxies.

    Drives the real generator (no hand-crafted source) so a regression that drops
    ``RegisterLibraryFromFileRequest`` from emitted ``build_workflow()`` bodies fails
    this test, just like the in-process unit test asserts the same contract via AST
    inspection plus a recording stub.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config_root = tmp_path / "xdg_config"
    library_json = materialize_library(
        tmp_path / "library", template=FIXTURE_LIBRARY_JSON_TEMPLATE, node_file=FIXTURE_NODE_FILE
    )
    write_isolated_config(config_root, workspace=workspace, library_path=library_json)

    # Generator runs in the test process where Echo Library can be registered locally,
    # so SerializeFlowToCommandsRequest can build a faithful command list.
    workflow_source = _generate_echo_workflow_source(library_json)
    runnable_source = _wrap_with_runtime_assertions(workflow_source)
    assert "RegisterLibraryFromFileRequest(library_name='Echo Library'" in workflow_source, (
        "build_workflow must emit the registration call; if this assertion fails the unit"
        " AST tests for #4584 should fail too"
    )

    workflow_path = tmp_path / "echo_workflow.py"
    workflow_path.write_text(runnable_source)

    # Isolated config dir + the GT_CLOUD_API_KEY placeholder the bootstrap needs. The child
    # runs hermetically in LOCAL storage because conftest.py's _isolated_engine_env keeps real
    # cloud secrets out of os.environ.
    env = engine_subprocess_env(XDG_CONFIG_HOME=str(config_root))

    result = subprocess.run(  # noqa: S603 - subprocess input is constructed inside the test
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
    assert "type=EchoNode" in result.stdout, diagnostic
    assert "type=ErrorProxyNode" not in result.stdout, diagnostic
    # The placeholder substitution warning is the engine's single most reliable signal
    # that a library was missing at CreateNode time. If it appears, build_workflow was
    # not registering libraries before creating nodes regardless of any other output.
    assert "Created Error Proxy" not in result.stderr, diagnostic
