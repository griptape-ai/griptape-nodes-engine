"""RunWorkflowWithCurrentStateRequest while a flow is already open  (issue #5526).

A saved workflow file asks for its own top-level flow with
``CreateFlowRequest(parent_flow_name=None, flow_name='ControlFlow_1')``. When that file
is replayed while the session still has a flow pushed on the Current Context,
``FlowManager.on_create_flow_request`` reads the ``None`` parent as "use the current
context" rather than "I am the canvas", so the incoming flow is silently adopted as a
child of the flow already on screen and renamed by collision.

That child is invisible: the editor never renders it, yet every save re-serialises it
and every run executes it. Loading a workflow into an occupied context (i.e.
RunWorkflowWithCurrentStateRequest) must not quietly produce that -- it has to fail
loudly instead.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from griptape_nodes.retained_mode.events.flow_events import (
    CreateFlowRequest,
    CreateFlowResultSuccess,
    ListFlowsInFlowRequest,
    ListFlowsInFlowResultSuccess,
)
from griptape_nodes.retained_mode.events.library_events import (
    RegisterLibraryFromFileRequest,
    RegisterLibraryFromFileResultSuccess,
)
from griptape_nodes.retained_mode.events.workflow_events import (
    RunWorkflowWithCurrentStateRequest,
    RunWorkflowWithCurrentStateResultSuccess,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from griptape_nodes.retained_mode.engine import Engine

# Timeout with thread dump.
pytestmark = pytest.mark.timeout(300, method="thread")

FIXTURE_LIBRARY_DIR = Path(__file__).parent / "fixtures" / "workflow_node_library"
FIXTURE_LIBRARY_JSON_TEMPLATE = FIXTURE_LIBRARY_DIR / "griptape_nodes_library.json"
FIXTURE_NODE_FILE = FIXTURE_LIBRARY_DIR / "workflow_node_nodes.py"
# Workflow B: a saved workflow whose header asks for its own top-level 'ControlFlow_1'.
FIXTURE_WORKFLOW_FILE = FIXTURE_LIBRARY_DIR / "shout_workflow.py"
# The workflow file references the library by this exact name, so it must register under it.
LIBRARY_NAME = "Workflow Node Library"
NODE_TYPE = "ShoutNode"


@pytest.fixture
def registered_library(tmp_path: Path, engine: Engine, materialize_library: Callable[..., Path]) -> Path:
    """Materialize and register the fixture library, returning its JSON path."""
    library_json = materialize_library(
        tmp_path / "library",
        template=FIXTURE_LIBRARY_JSON_TEMPLATE,
        node_file=FIXTURE_NODE_FILE,
        extra_files=[FIXTURE_WORKFLOW_FILE],
    )
    register_result = engine.handle_request(RegisterLibraryFromFileRequest(file_path=str(library_json)))
    assert isinstance(register_result, RegisterLibraryFromFileResultSuccess), register_result
    return library_json


@pytest.mark.skipif(
    not FIXTURE_WORKFLOW_FILE.exists(),
    reason=f"Workflow Node Library fixture workflow missing at {FIXTURE_WORKFLOW_FILE}",
)
class TestLoadingAWorkflowIntoAnOccupiedFlowContext:
    """A workflow file's own top-level flow must never be adopted by the flow already open."""

    @pytest.mark.asyncio
    async def test_loading_a_workflow_while_a_flow_is_open_does_not_create_a_hidden_child_flow(
        self,
        registered_library: Path,
        engine: Engine,
        create_node: Callable[..., str],
    ) -> None:
        workflow_b = registered_library.parent / FIXTURE_WORKFLOW_FILE.name

        # Workflow A is the session the artist is sitting in, with its top-level flow pushed onto
        # the Current Context the way the editor leaves it.
        engine.context_manager.push_workflow(workflow_name="workflow_a")
        flow_a = engine.handle_request(
            CreateFlowRequest(parent_flow_name=None, flow_name="ControlFlow_1", set_as_new_context=True)
        )
        assert isinstance(flow_a, CreateFlowResultSuccess), flow_a
        create_node(NODE_TYPE, "NodeInA", flow_a.flow_name, library_name=LIBRARY_NAME)

        assert engine.context_manager.has_current_flow(), (
            "Test precondition failed: workflow A's flow is not on the Current Context, so loading "
            "workflow B could not possibly hit the parent-flow fallback this test exists to pin."
        )

        result = await engine.ahandle_request(RunWorkflowWithCurrentStateRequest(file_path=str(workflow_b)))

        children = engine.handle_request(ListFlowsInFlowRequest(parent_flow_name=flow_a.flow_name))
        assert isinstance(children, ListFlowsInFlowResultSuccess), children
        assert children.flow_names == [], (
            f"Loading '{workflow_b.name}' into an open flow context nested workflow B's top-level "
            f"flow underneath workflow A's flow '{flow_a.flow_name}' instead of refusing the load. "
            f"Hidden child flows found: {children.flow_names} "
            f"(metadata: {[_flow_metadata(engine, name) for name in children.flow_names]}). "
            f"The editor never renders these, but every save re-serialises them and every run "
            f"executes them."
        )
        assert not isinstance(result, RunWorkflowWithCurrentStateResultSuccess), (
            "Loading a workflow while another flow is open reported success. It must fail loudly "
            "instead, because the file's own top-level flow cannot be created while a flow is on "
            f"the Current Context. Result was: {result}"
        )
        assert flow_a.flow_name in str(result.result_details), (
            f"The load failed, but not for the reason this test pins: the failure never mentions "
            f"'{flow_a.flow_name}', the open flow that is in the way. Any breakage before exec -- a "
            f"missing fixture, an unregistered library, a path that no longer resolves -- would "
            f"satisfy the assertions above, so without this the test can pass green while the guard "
            f"is gone. Details were: {result.result_details}"
        )


def _flow_metadata(engine: Engine, flow_name: str) -> Any:
    """The metadata carried by a live flow, for the failure message."""
    return engine.flow_manager.get_flow_by_name(flow_name).metadata
