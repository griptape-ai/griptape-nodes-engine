"""Closing a workflow that holds a subflow-backed group must not warn about the group's subflow.

See https://github.com/griptape-ai/griptape-nodes-engine/issues/5746.

``DeleteFlowRequest`` deletes child flows before child nodes, so a group's subflow is already gone
when ``SubflowNodeGroup.after_node_deleted`` runs during teardown. That is the normal order, not an
inconsistency, so nothing about it belongs in the log at warning level.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from griptape_nodes.retained_mode.events.flow_events import (
    CreateFlowRequest,
    CreateFlowResultSuccess,
    DeleteFlowRequest,
    DeleteFlowResultSuccess,
)
from griptape_nodes.retained_mode.events.library_events import (
    RegisterLibraryFromFileRequest,
    RegisterLibraryFromFileResultSuccess,
)
from griptape_nodes.retained_mode.events.node_events import CreateNodeRequest, CreateNodeResultSuccess

if TYPE_CHECKING:
    from collections.abc import Callable

    from griptape_nodes.retained_mode.engine import Engine

pytestmark = pytest.mark.timeout(300, method="thread")

FIXTURE_LIBRARY_DIR = Path(__file__).parent / "fixtures" / "subflow_library"
FIXTURE_LIBRARY_JSON_TEMPLATE = FIXTURE_LIBRARY_DIR / "griptape_nodes_library.json"
FIXTURE_NODE_FILE = FIXTURE_LIBRARY_DIR / "subflow_echo_node.py"
LIBRARY_NAME = "Subflow Group Close Workflow Library"


@pytest.mark.skipif(
    not FIXTURE_LIBRARY_JSON_TEMPLATE.exists(),
    reason=f"Subflow Library fixture missing at {FIXTURE_LIBRARY_JSON_TEMPLATE}",
)
def test_deleting_parent_flow_does_not_warn_about_group_subflow(
    tmp_path: Path,
    engine: Engine,
    materialize_library: Callable[..., Path],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Deleting the top-level flow, which is what closing a workflow does, logs no subflow warning."""
    library_json = materialize_library(
        tmp_path / "library", template=FIXTURE_LIBRARY_JSON_TEMPLATE, node_file=FIXTURE_NODE_FILE, name=LIBRARY_NAME
    )
    register_result = engine.handle_request(RegisterLibraryFromFileRequest(file_path=str(library_json)))
    assert isinstance(register_result, RegisterLibraryFromFileResultSuccess), register_result

    engine.context_manager.push_workflow(workflow_name="subflow_close_wf")
    parent_result = engine.handle_request(
        CreateFlowRequest(parent_flow_name=None, flow_name="ParentFlow", set_as_new_context=False)
    )
    assert isinstance(parent_result, CreateFlowResultSuccess), parent_result

    with engine.context_manager.flow(parent_result.flow_name):
        child = engine.handle_request(
            CreateNodeRequest(node_type="EchoNode", specific_library_name=LIBRARY_NAME, node_name="Child")
        )
        assert isinstance(child, CreateNodeResultSuccess), child
        group = engine.handle_request(
            CreateNodeRequest(
                node_type="SubflowGroupNode",
                specific_library_name=LIBRARY_NAME,
                node_name="Group",
                node_names_to_add=["Child"],
            )
        )
        assert isinstance(group, CreateNodeResultSuccess), group

    with caplog.at_level(logging.WARNING):
        delete_result = engine.handle_request(DeleteFlowRequest(flow_name=parent_result.flow_name))

    assert isinstance(delete_result, DeleteFlowResultSuccess), delete_result
    assert "doesn't exist. Removing from metadata." not in caplog.text
