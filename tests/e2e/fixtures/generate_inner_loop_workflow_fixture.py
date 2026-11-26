"""Generate `workflow_node_loop_library/inner_loop_workflow.py`, a saved workflow that contains a loop.

Run with `uv run python tests/e2e/fixtures/generate_inner_loop_workflow_fixture.py` from the repo root
when the workflow file format changes.

The workflow is Start -> InnerLoop (a loop group) -> End, with a single InnerBody node inside the loop.
It is committed rather than built during the test run, so a change to the metadata header, the stored
workflow shape, or the workflow generator shows up as a failing test instead of silently producing a file
the node code can no longer read. This script builds the graph in-process and emits it through the real
workflow generator, so the committed fixture matches what saving from the editor produces.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from griptape_nodes.node_library.workflow_registry import WorkflowMetadata, WorkflowShape
from griptape_nodes.retained_mode.engine import current_engine
from griptape_nodes.retained_mode.events.connection_events import CreateConnectionRequest
from griptape_nodes.retained_mode.events.flow_events import (
    AutoLayoutFlowRequest,
    AutoLayoutFlowResultSuccess,
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
from griptape_nodes.utils.version_utils import engine_version

LIBRARY_DIR = Path(__file__).resolve().parent / "workflow_node_loop_library"
LIBRARY_JSON = LIBRARY_DIR / "griptape_nodes_library.json"
NODE_FILE = LIBRARY_DIR / "inner_loop_nodes.py"
GROUP_NODE_FILE = Path(__file__).resolve().parent / "iterative_group_library" / "iterative_group_nodes.py"
WORKFLOW_PATH = LIBRARY_DIR / "inner_loop_workflow.py"
LIBRARY_NAME = "Griptape Nodes Library"
WORKFLOW_NAME = "inner_loop_workflow"


class FixtureGenerationError(RuntimeError):
    """Raised when a step of the fixture generation does not succeed."""


def _expect[T](result: object, expected: type[T], attempted: str) -> T:
    """Narrow `result` to `expected`, or fail with the engine's own failure details."""
    if not isinstance(result, expected):
        msg = f"Attempted to {attempted}. Failed with result: {result}"
        raise FixtureGenerationError(msg)
    return result


def _create(node_type: str, node_name: str, parent_group_name: str | None = None) -> str:
    result = current_engine().handle_request(
        CreateNodeRequest(
            node_type=node_type,
            specific_library_name=LIBRARY_NAME,
            node_name=node_name,
            parent_group_name=parent_group_name,
        )
    )
    return _expect(result, CreateNodeResultSuccess, f"create a '{node_type}' node").node_name


def _connect(source_node: str, source_param: str, target_node: str, target_param: str) -> None:
    result = current_engine().handle_request(
        CreateConnectionRequest(
            source_node_name=source_node,
            source_parameter_name=source_param,
            target_node_name=target_node,
            target_parameter_name=target_param,
        )
    )
    if result.failed():
        msg = f"Attempted to connect {source_node}.{source_param} to {target_node}.{target_param}. Failed: {result}"
        raise FixtureGenerationError(msg)


def _materialize_library(target_dir: Path) -> Path:
    """Copy the fixture library into `target_dir` with the running engine version.

    The committed JSON pins engine_version to "0.0.0", which registration rejects. The emitted
    workflow references its libraries by name only, so generating against a copy is equivalent.
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    schema = json.loads(LIBRARY_JSON.read_text())
    schema["metadata"]["engine_version"] = engine_version
    # The workflow being generated is what this script writes; drop the declaration so registration
    # does not fail on a file that does not exist yet.
    schema.pop("workflow_nodes", None)
    library_json = target_dir / "griptape_nodes_library.json"
    library_json.write_text(json.dumps(schema, indent=2))
    (target_dir / NODE_FILE.name).write_text(NODE_FILE.read_text())
    (target_dir / GROUP_NODE_FILE.name).write_text(GROUP_NODE_FILE.read_text())
    return library_json


def _generate() -> None:
    """Build the Start -> InnerLoop(InnerBody) -> End graph and write it out as a saved workflow."""
    current_engine().context_manager.push_workflow(workflow_name=WORKFLOW_NAME)

    flow_result = current_engine().handle_request(
        CreateFlowRequest(parent_flow_name=None, flow_name="ControlFlow_1", set_as_new_context=False)
    )
    flow_name = _expect(flow_result, CreateFlowResultSuccess, "create the workflow's flow").flow_name

    # Group membership is only honoured for nodes created in the flow's own context.
    with current_engine().context_manager.flow(flow_name):
        start_node = _create("TextStartNode", "Start Flow")
        loop_node = _create("ProbeForEachGroupNode", "InnerLoop")
        body_node = _create("LoopBodyControlNode", "InnerBody", parent_group_name=loop_node)
        end_node = _create("TextEndNode", "End Flow")

    _connect(start_node, "exec_out", loop_node, "exec_in")
    _connect(loop_node, "on_each", body_node, "exec_in")
    _connect(body_node, "exec_out", loop_node, "loop_complete")
    _connect(loop_node, "exec_out", end_node, "exec_in")

    # Lay the graph out before serializing so node positions are baked into the emitted file.
    # Without this every node serializes without a position and they stack on the same spot when
    # the workflow is opened or previewed.
    layout_result = current_engine().handle_request(AutoLayoutFlowRequest(flow_name=flow_name))
    _expect(layout_result, AutoLayoutFlowResultSuccess, "lay out the workflow's nodes")

    workflow_manager = current_engine().workflow_manager
    shape = workflow_manager.extract_workflow_shape(WORKFLOW_NAME, flow_name=flow_name)

    serialize_result = current_engine().handle_request(SerializeFlowToCommandsRequest(flow_name=flow_name))
    serialized = _expect(serialize_result, SerializeFlowToCommandsResultSuccess, "serialize the workflow's flow")

    metadata = WorkflowMetadata(
        name=WORKFLOW_NAME,
        schema_version=WorkflowMetadata.LATEST_SCHEMA_VERSION,
        engine_version_created_with="0.0.0",
        node_libraries_referenced=list(serialized.serialized_flow_commands.node_dependencies.libraries),
        description="Loops over a single body node.",
        workflow_shape=WorkflowShape(inputs=shape["input"], outputs=shape["output"]),
    )
    content = workflow_manager.codegen.generate_workflow_file_content(
        serialized_flow_commands=serialized.serialized_flow_commands,
        workflow_metadata=metadata,
    )
    WORKFLOW_PATH.write_text(content, encoding="utf-8")
    print(f"Wrote {WORKFLOW_PATH}")  # noqa: T201


def main() -> None:
    """Register the fixture library against a temp copy, then regenerate the workflow file."""
    current_engine().handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))

    with tempfile.TemporaryDirectory() as temp_dir:
        library_json = _materialize_library(Path(temp_dir) / "library")
        register_result = current_engine().handle_request(RegisterLibraryFromFileRequest(file_path=str(library_json)))
        _expect(register_result, RegisterLibraryFromFileResultSuccess, "register the fixture library")
        _generate()


if __name__ == "__main__":
    main()
