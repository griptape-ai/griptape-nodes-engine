"""End-to-end coverage for opening workflows while nodes are lazily loaded.

With lazy node loading (``library.lazy_node_loading``, the default), registering a library
imports nothing: each node's module loads on first use. But saved workflows reference library
classes through their stable namespace (``griptape_nodes.node_libraries.<lib>.<file>``), both as
``from`` imports emitted into the generated Python and inside pickled parameter values. Before
the ``StableNamespaceImportFinder`` meta-path hook, those imports only resolved if the module
happened to be in ``sys.modules`` already, so opening any workflow that carried a
library-defined value failed with ``No module named 'griptape_nodes.node_libraries'`` the moment
lazy loading shipped.

This suite drives the full regression path: build a workflow whose parameter value is an object
defined in a fixture library, save it through the real generator, then execute the generated
``.py`` in a fresh subprocess whose config has lazy loading enabled. The subprocess must import
the stable namespace and unpickle the value without ever having resolved a node class first.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

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
from griptape_nodes.retained_mode.events.parameter_events import (
    SetParameterValueRequest,
    SetParameterValueResultSuccess,
)
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
from griptape_nodes.retained_mode.managers.library_manager import LibraryManager

if TYPE_CHECKING:
    from types import ModuleType

FIXTURE_LIBRARY_DIR = Path(__file__).parent / "fixtures" / "lazy_payload_library"
FIXTURE_LIBRARY_JSON_TEMPLATE = FIXTURE_LIBRARY_DIR / "griptape_nodes_library.json"
FIXTURE_NODE_FILE = FIXTURE_LIBRARY_DIR / "lazy_payload_node.py"

LIBRARY_NAME = "Lazy Payload Library"
STABLE_NAMESPACE = "griptape_nodes.node_libraries.lazy_payload_library.lazy_payload_node"
PAYLOAD_TAG = "round-trip"


def _materialize_library(target_dir: Path) -> Path:
    """Copy the on-disk fixture into ``target_dir`` and stamp the current engine version.

    Rewrites the fixture's ``engine_version`` field to the engine's running version so
    ``IncompatibleEngineVersionCheck`` never marks the library UNUSABLE on a version bump.
    """
    from griptape_nodes.utils.version_utils import engine_version

    target_dir.mkdir(parents=True, exist_ok=True)
    schema = json.loads(FIXTURE_LIBRARY_JSON_TEMPLATE.read_text())
    schema["metadata"]["engine_version"] = engine_version
    library_json = target_dir / "griptape_nodes_library.json"
    library_json.write_text(json.dumps(schema, indent=2))
    (target_dir / FIXTURE_NODE_FILE.name).write_text(FIXTURE_NODE_FILE.read_text())
    return library_json


def _write_isolated_config(config_root: Path, *, workspace: Path, library_path: Path) -> None:
    """Write an XDG-style config that registers the fixture library with lazy loading pinned on.

    ``lazy_node_loading`` is the default, but the subprocess pins it explicitly so this
    regression test keeps meaning the same thing if the default ever flips.
    """
    config_dir = config_root / "griptape_nodes"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "griptape_nodes_config.json"
    config_path.write_text(
        json.dumps(
            {
                "workspace_directory": str(workspace),
                "log_level": "WARNING",
                "library": {"lazy_node_loading": True},
                "app_events": {
                    "on_app_initialization_complete": {
                        "libraries_to_register": [str(library_path)],
                    },
                },
            }
        )
    )


def _import_stable_namespace() -> ModuleType:
    """Import the fixture's stable namespace, the exact operation the regression broke."""
    return importlib.import_module(STABLE_NAMESPACE)


def _generate_payload_workflow_source(library_json: Path) -> str:
    """Build a flow holding a LazyPayload parameter value and serialize it to a Python module.

    Mirrors the engine's save path: register the library (lazily, per this process's config
    default), create a flow, drop a node into it, set its ``payload`` parameter to an object
    defined by the library module, serialize, and generate the workflow file content.
    """
    GriptapeNodes.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))

    # Pin lazy loading on for this registration regardless of the developer's ambient config,
    # so the in-process stable-namespace import below always goes through the deferred path.
    with patch.object(LibraryManager, "_should_lazy_load_nodes", return_value=True):
        register_result = GriptapeNodes.handle_request(RegisterLibraryFromFileRequest(file_path=str(library_json)))
    assert isinstance(register_result, RegisterLibraryFromFileResultSuccess), register_result

    # The library registered lazily, so no node module has loaded yet. Importing the stable
    # namespace here must work regardless (it is what saved workflows execute), and it is the
    # only way to get at LazyPayload without resolving a node class first.
    assert STABLE_NAMESPACE not in sys.modules, "Sanity: lazy registration must not import the node module"
    module = _import_stable_namespace()
    payload = module.LazyPayload(PAYLOAD_TAG)

    GriptapeNodes.ContextManager().push_workflow(workflow_name="lazy_payload_e2e_workflow")

    flow_result = GriptapeNodes.handle_request(
        CreateFlowRequest(parent_flow_name=None, flow_name="ControlFlow_1", set_as_new_context=False)
    )
    assert isinstance(flow_result, CreateFlowResultSuccess), flow_result

    node_result = GriptapeNodes.handle_request(
        CreateNodeRequest(
            node_type="LazyPayloadNode",
            specific_library_name=LIBRARY_NAME,
            node_name="LazyPayload_1",
            override_parent_flow_name=flow_result.flow_name,
        )
    )
    assert isinstance(node_result, CreateNodeResultSuccess), node_result
    assert node_result.node_type == "LazyPayloadNode", (
        f"Sanity: in-process registration must yield real node, got {node_result.node_type!r}"
    )

    set_value_result = GriptapeNodes.handle_request(
        SetParameterValueRequest(parameter_name="payload", value=payload, node_name="LazyPayload_1")
    )
    assert isinstance(set_value_result, SetParameterValueResultSuccess), set_value_result

    serialize_result = GriptapeNodes.handle_request(SerializeFlowToCommandsRequest(flow_name=flow_result.flow_name))
    assert isinstance(serialize_result, SerializeFlowToCommandsResultSuccess), serialize_result

    metadata = WorkflowMetadata(
        name="lazy_payload_e2e_workflow",
        schema_version=WorkflowMetadata.LATEST_SCHEMA_VERSION,
        engine_version_created_with="0.0.0",
        node_libraries_referenced=list(serialize_result.serialized_flow_commands.node_dependencies.libraries),
        # Skip the executable wrapper; we only need build_workflow to run end-to-end.
        workflow_shape=None,
    )
    return GriptapeNodes.WorkflowManager()._generate_workflow_file_content(
        serialized_flow_commands=serialize_result.serialized_flow_commands,
        workflow_metadata=metadata,
    )


def _wrap_with_runtime_assertions(workflow_source: str) -> str:
    """Append a ``__main__`` block that runs build_workflow and prints the round-tripped payload.

    The subprocess this runs in has lazy node loading enabled and never resolves a node class
    before build_workflow executes, so the deferred stable-namespace import and the pickled
    parameter value inside the generated source are what exercise the import path under test.
    """
    runtime_block = """

import asyncio as _e2e_asyncio
import logging as _e2e_logging

from griptape_nodes.retained_mode.events.flow_events import (
    GetTopLevelFlowRequest as _E2EGetTopLevelFlowRequest,
    GetTopLevelFlowResultSuccess as _E2EGetTopLevelFlowResultSuccess,
    ListNodesInFlowRequest as _E2EListNodesInFlowRequest,
    ListNodesInFlowResultSuccess as _E2EListNodesInFlowResultSuccess,
)


async def _e2e_run() -> None:
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
        payload = node.get_parameter_value("payload")
        print(f"PAYLOAD type={type(payload).__name__} tag={getattr(payload, 'tag', None)}", flush=True)


if __name__ == "__main__":
    _e2e_logging.basicConfig(level=_e2e_logging.WARNING)
    _e2e_asyncio.run(_e2e_run())
"""
    return workflow_source + runtime_block


@pytest.mark.skipif(
    not FIXTURE_LIBRARY_JSON_TEMPLATE.exists(),
    reason=f"Lazy Payload Library fixture missing at {FIXTURE_LIBRARY_JSON_TEMPLATE}",
)
def test_saved_workflow_opens_under_lazy_node_loading(tmp_path: Path) -> None:
    """A saved workflow carrying a library-defined value must open with lazy loading enabled.

    Regression test for the lazy-node-loading rollback: opening any workflow failed with
    ``No module named 'griptape_nodes.node_libraries'`` because the stable namespaces its
    imports and pickles reference were only present in ``sys.modules`` after an eager load.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config_root = tmp_path / "xdg_config"
    library_json = _materialize_library(tmp_path / "library")
    _write_isolated_config(config_root, workspace=workspace, library_path=library_json)

    workflow_source = _generate_payload_workflow_source(library_json)
    runnable_source = _wrap_with_runtime_assertions(workflow_source)
    assert f"from {STABLE_NAMESPACE} import LazyPayload" in workflow_source, (
        "The generator must emit the deferred stable-namespace import for the pickled payload;"
        " without it this test would not exercise the lazy import path."
    )

    workflow_path = tmp_path / "lazy_payload_workflow.py"
    workflow_path.write_text(runnable_source)

    env = os.environ.copy()
    env["XDG_CONFIG_HOME"] = str(config_root)
    # Engine bootstrap requires GT_CLOUD_API_KEY to be set; the value never leaves the
    # subprocess so a placeholder is fine.
    env.setdefault("GT_CLOUD_API_KEY", "fake-test-key-for-bootstrap")

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
    assert "No module named 'griptape_nodes.node_libraries'" not in result.stderr, diagnostic
    assert "type=LazyPayloadNode" in result.stdout, diagnostic
    assert "type=ErrorProxyNode" not in result.stdout, diagnostic
    assert f"PAYLOAD type=LazyPayload tag={PAYLOAD_TAG}" in result.stdout, diagnostic
