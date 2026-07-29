"""Regression test for issue #5124: importing a referenced workflow that contains a node group.

A workflow that contains a node group (ForEach, etc.) serializes as MORE THAN ONE flow — its
top-level ``ControlFlow`` plus the group's body flow. When such a workflow is imported as a
referenced sub-flow, ``WorkflowManager._execute_workflow_import`` must bind to the TOP-LEVEL
imported flow (the one that holds the Start/End nodes), because that is what the Workflow node
records as its ``subflow_name`` and routes I/O through.
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from griptape_nodes.node_library.library_registry import LibraryRegistry
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
from griptape_nodes.retained_mode.events.object_events import (
    ClearAllObjectStateRequest,
    ClearAllObjectStateResultSuccess,
)
from griptape_nodes.retained_mode.events.workflow_events import (
    ImportWorkflowAsReferencedSubFlowRequest,
    ImportWorkflowAsReferencedSubFlowResultSuccess,
    ImportWorkflowRequest,
    ImportWorkflowResultSuccess,
)
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

if TYPE_CHECKING:
    from collections.abc import Iterator

FIXTURE_LIBRARY_DIR = Path(__file__).parent / "fixtures" / "subflow_library"
FIXTURE_LIBRARY_JSON_TEMPLATE = FIXTURE_LIBRARY_DIR / "griptape_nodes_library.json"
FIXTURE_NODE_FILE = FIXTURE_LIBRARY_DIR / "subflow_echo_node.py"
# Distinct name so the LibraryManager's LOADED bookkeeping doesn't collide with the other
# in-process subflow tests sharing a worker.
LIBRARY_NAME = "Subflow Import Flow Selection Library"


def _materialize_library(target_dir: Path) -> Path:
    from griptape_nodes.utils.version_utils import engine_version

    target_dir.mkdir(parents=True, exist_ok=True)
    schema = json.loads(FIXTURE_LIBRARY_JSON_TEMPLATE.read_text())
    schema["name"] = LIBRARY_NAME
    schema["metadata"]["engine_version"] = engine_version
    library_json = target_dir / "griptape_nodes_library.json"
    library_json.write_text(json.dumps(schema, indent=2))
    (target_dir / FIXTURE_NODE_FILE.name).write_text(FIXTURE_NODE_FILE.read_text())
    return library_json


@pytest.fixture
def _clean_engine_state() -> Iterator[None]:
    """Keep the shared engine singleton clean despite running in-process."""
    GriptapeNodes.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
    yield
    clear_result = GriptapeNodes.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
    assert isinstance(clear_result, ClearAllObjectStateResultSuccess), clear_result
    with contextlib.suppress(KeyError):
        LibraryRegistry.unregister_library(LIBRARY_NAME)


def _write_grouped_workflow_file(library_json: Path, workflow_path: Path) -> None:
    """Write a self-contained grouped workflow ``.py`` to ``workflow_path``.

    Builds a flow whose only node is a group (which owns a nested body flow), serializes it to a
    self-contained module, and clears the in-process build so the import starts from a clean slate.
    """
    register_result = GriptapeNodes.handle_request(RegisterLibraryFromFileRequest(file_path=str(library_json)))
    assert isinstance(register_result, RegisterLibraryFromFileResultSuccess), register_result

    GriptapeNodes.ContextManager().push_workflow(workflow_name="grouped_inner_workflow")

    flow_result = GriptapeNodes.handle_request(
        CreateFlowRequest(parent_flow_name=None, flow_name="ControlFlow_1", set_as_new_context=False)
    )
    assert isinstance(flow_result, CreateFlowResultSuccess), flow_result
    flow_name = flow_result.flow_name

    with GriptapeNodes.ContextManager().flow(flow_name):
        group_result = GriptapeNodes.handle_request(
            CreateNodeRequest(
                node_type="SubflowGroupNode",
                specific_library_name=LIBRARY_NAME,
                node_name="SubflowGroup_1",
                initial_setup=True,
            )
        )
        assert isinstance(group_result, CreateNodeResultSuccess), group_result
        echo_result = GriptapeNodes.handle_request(
            CreateNodeRequest(
                node_type="EchoNode",
                specific_library_name=LIBRARY_NAME,
                node_name="Echo_1",
                initial_setup=True,
                parent_group_name=group_result.node_name,
            )
        )
        assert isinstance(echo_result, CreateNodeResultSuccess), echo_result

    serialize_result = GriptapeNodes.handle_request(
        SerializeFlowToCommandsRequest(flow_name=flow_name, include_create_flow_command=True)
    )
    assert isinstance(serialize_result, SerializeFlowToCommandsResultSuccess), serialize_result

    metadata = WorkflowMetadata(
        name="grouped_inner_workflow",
        schema_version=WorkflowMetadata.LATEST_SCHEMA_VERSION,
        engine_version_created_with="0.0.0",
        node_libraries_referenced=list(serialize_result.serialized_flow_commands.node_dependencies.libraries),
        workflow_shape=None,
    )
    content = GriptapeNodes.WorkflowManager()._generate_workflow_file_content(
        serialized_flow_commands=serialize_result.serialized_flow_commands,
        workflow_metadata=metadata,
    )
    workflow_path.write_text(content)

    # Drop the in-process build so the import below starts from a clean parent flow.
    GriptapeNodes.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))


@pytest.mark.skipif(
    not FIXTURE_LIBRARY_JSON_TEMPLATE.exists(),
    reason=f"Subflow Library fixture missing at {FIXTURE_LIBRARY_JSON_TEMPLATE}",
)
@pytest.mark.asyncio
@pytest.mark.usefixtures("_clean_engine_state")
async def test_import_binds_to_top_level_flow_not_group_body(tmp_path: Path) -> None:
    """Importing a group-containing workflow must return the top-level imported flow."""
    library_json = _materialize_library(tmp_path / "library")
    workflow_path = tmp_path / "grouped_inner_workflow.py"
    _write_grouped_workflow_file(library_json, workflow_path)

    # Register the workflow file so it can be imported by name.
    import_workflow_result = await GriptapeNodes.ahandle_request(ImportWorkflowRequest(file_path=str(workflow_path)))
    assert isinstance(import_workflow_result, ImportWorkflowResultSuccess), import_workflow_result
    workflow_name = import_workflow_result.workflow_name

    # Fresh parent flow to import into.
    GriptapeNodes.ContextManager().push_workflow(workflow_name="parent_workflow")
    parent_result = GriptapeNodes.handle_request(
        CreateFlowRequest(parent_flow_name=None, flow_name="ParentFlow", set_as_new_context=False)
    )
    assert isinstance(parent_result, CreateFlowResultSuccess), parent_result
    parent_flow = parent_result.flow_name

    import_result = await GriptapeNodes.ahandle_request(
        ImportWorkflowAsReferencedSubFlowRequest(workflow_name=workflow_name, flow_name=parent_flow)
    )
    assert isinstance(import_result, ImportWorkflowAsReferencedSubFlowResultSuccess), import_result

    # The imported workflow created two flows (its top-level flow + the group's body flow). The
    # returned flow must be the TOP-LEVEL one, i.e. the flow whose parent is the import target.
    # The group's body flow is parented to that top-level flow, so returning it would be the bug.
    flow_manager = GriptapeNodes.FlowManager()
    created_flow = import_result.created_flow_name
    assert flow_manager.get_parent_flow(created_flow) == parent_flow, (
        f"import bound to '{created_flow}' (parent '{flow_manager.get_parent_flow(created_flow)}') "
        f"instead of the top-level flow parented to '{parent_flow}'"
    )
