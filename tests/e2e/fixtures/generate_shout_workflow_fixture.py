"""Generate the saved workflows in `workflow_node_library` that the e2e tests point nodes at.

Produces `shout_workflow.py` (Start Flow -> Shout -> End Flow) and four workflows that wrap a Workflow
node between Start Flow and End Flow: `recursive_workflow.py` (runs itself), `cycle_a_workflow.py` and
`cycle_b_workflow.py` (each runs the other), and `nested_workflow.py` (runs `shout_workflow.py`).

Run with `uv run python tests/e2e/fixtures/generate_shout_workflow_fixture.py` from the repo root when
the workflow file format changes.

The fixture is committed rather than built during the test run on purpose: it pins what a *saved*
workflow looks like on disk, so a change to the metadata header, the stored workflow shape, or the
workflow generator shows up as a failing test instead of silently producing a file the node code can
no longer read. This script builds each graph in-process and emits it through
the real workflow generator, so the committed fixture always matches what saving from the editor
produces.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from typing import NamedTuple

from griptape_nodes.exe_types.workflow_node import WORKFLOW_FILE_VALUE_KEY
from griptape_nodes.node_library.workflow_registry import WorkflowMetadata, WorkflowShape
from griptape_nodes.retained_mode.engine import current_engine
from griptape_nodes.retained_mode.events.connection_events import CreateConnectionRequest
from griptape_nodes.retained_mode.events.flow_events import (
    AutoLayoutFlowRequest,
    AutoLayoutFlowResultSuccess,
    CreateFlowRequest,
    CreateFlowResultSuccess,
    SerializedFlowCommands,
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
from griptape_nodes.utils.version_utils import engine_version

LIBRARY_DIR = Path(__file__).resolve().parent / "workflow_node_library"
LIBRARY_JSON = LIBRARY_DIR / "griptape_nodes_library.json"
NODE_FILE = LIBRARY_DIR / "workflow_node_nodes.py"
LIBRARY_NAME = "Workflow Node Library"
SHOUT_WORKFLOW_PATH = LIBRARY_DIR / "shout_workflow.py"
WORKFLOW_NODE_PREFIX = "Inner Workflow"


class WorkflowTarget(NamedTuple):
    """One workflow file to generate."""

    stem: str
    description: str
    # The node between Start Flow and End Flow, and the parameter on it that feeds End Flow's `result`.
    middle_node_type: str
    middle_node_name: str
    middle_output_param: str


SHOUT = WorkflowTarget(
    stem="shout_workflow",
    description="Uppercases the incoming text and appends an exclamation mark.",
    middle_node_type="ShoutNode",
    middle_node_name="Shout",
    middle_output_param="shouted",
)
RECURSIVE = WorkflowTarget(
    stem="recursive_workflow",
    description="Contains a Workflow node that runs this same workflow.",
    middle_node_type="RecursiveWorkflow",
    middle_node_name=WORKFLOW_NODE_PREFIX,
    middle_output_param="result",
)
CYCLE_A = WorkflowTarget(
    stem="cycle_a_workflow",
    description="Contains a Workflow node that runs cycle B, which runs this workflow again.",
    middle_node_type="CycleBWorkflow",
    middle_node_name=WORKFLOW_NODE_PREFIX,
    middle_output_param="result",
)
CYCLE_B = WorkflowTarget(
    stem="cycle_b_workflow",
    description="Contains a Workflow node that runs cycle A, which runs this workflow again.",
    middle_node_type="CycleAWorkflow",
    middle_node_name=WORKFLOW_NODE_PREFIX,
    middle_output_param="result",
)
NESTED = WorkflowTarget(
    stem="nested_workflow",
    description="Contains a Workflow node that runs the shout workflow.",
    middle_node_type="ShoutWorkflow",
    middle_node_name=WORKFLOW_NODE_PREFIX,
    middle_output_param="result",
)
# Generated in this order: nested runs shout, so shout comes first.
TARGETS = [SHOUT, RECURSIVE, CYCLE_A, CYCLE_B, NESTED]
# Workflow node types registered against placeholder files so the real files can reference them.
EXTRA_WORKFLOW_NODES = {
    "RecursiveWorkflow": RECURSIVE.stem,
    "CycleAWorkflow": CYCLE_A.stem,
    "CycleBWorkflow": CYCLE_B.stem,
    "NestedWorkflow": NESTED.stem,
}


class FixtureGenerationError(RuntimeError):
    """Raised when a step of the fixture generation does not succeed."""


def _expect[T](result: object, expected: type[T], attempted: str) -> T:
    """Narrow `result` to `expected`, or fail with the engine's own failure details."""
    if not isinstance(result, expected):
        msg = f"Attempted to {attempted}. Failed with result: {result}"
        raise FixtureGenerationError(msg)
    return result


def _create(node_type: str, node_name: str, flow_name: str) -> str:
    result = current_engine().handle_request(
        CreateNodeRequest(
            node_type=node_type,
            specific_library_name=LIBRARY_NAME,
            node_name=node_name,
            override_parent_flow_name=flow_name,
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
    Every Workflow node type is declared against a copy of `shout_workflow.py`, which has the same
    Start Flow `text` -> End Flow `result` shape as the real files, so the types can register before
    the real files exist. Those placeholders are copied from the committed `shout_workflow.py` before
    shout is regenerated, so if shout's surface (its Start/End parameters) ever changes, run the script
    twice for the recursive, cycle, and nested fixtures to pick up the new shape.
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    schema = json.loads(LIBRARY_JSON.read_text())
    schema["metadata"]["engine_version"] = engine_version
    for node_type, stem in EXTRA_WORKFLOW_NODES.items():
        schema["workflow_nodes"].append(
            {
                "node_type": node_type,
                "workflow_path": f"{stem}.py",
                "metadata": {"category": "test", "description": node_type, "display_name": node_type},
            }
        )
    library_json = target_dir / "griptape_nodes_library.json"
    library_json.write_text(json.dumps(schema, indent=2))
    (target_dir / NODE_FILE.name).write_text(NODE_FILE.read_text())
    for stem in [SHOUT.stem, *EXTRA_WORKFLOW_NODES.values()]:
        shutil.copy(SHOUT_WORKFLOW_PATH, target_dir / f"{stem}.py")
    return library_json


def _generate(target: WorkflowTarget) -> None:
    """Build Start Flow -> middle node -> End Flow and write it out as a saved workflow."""
    engine = current_engine()
    _expect(
        engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True)),
        ClearAllObjectStateResultSuccess,
        "clear the engine's state before building the next workflow",
    )
    engine.context_manager.push_workflow(workflow_name=target.stem)

    flow_result = engine.handle_request(
        CreateFlowRequest(parent_flow_name=None, flow_name="ControlFlow_1", set_as_new_context=False)
    )
    flow_name = _expect(flow_result, CreateFlowResultSuccess, "create the workflow's flow").flow_name

    start_node = _create("TextStartNode", "Start Flow", flow_name)
    middle_node = _create(target.middle_node_type, target.middle_node_name, flow_name)
    end_node = _create("TextEndNode", "End Flow", flow_name)

    _connect(start_node, "exec_out", middle_node, "exec_in")
    _connect(middle_node, "exec_out", end_node, "exec_in")
    _connect(start_node, "text", middle_node, "text")
    _connect(middle_node, target.middle_output_param, end_node, "result")

    # Lay the graph out before serializing so node positions are baked into the emitted file.
    # Without this every node serializes without a position and they stack on the same spot when
    # the workflow is opened or previewed.
    layout_result = engine.handle_request(AutoLayoutFlowRequest(flow_name=flow_name))
    _expect(layout_result, AutoLayoutFlowResultSuccess, "lay out the workflow's nodes")

    workflow_manager = engine.workflow_manager
    shape = workflow_manager.extract_workflow_shape(target.stem, flow_name=flow_name)

    serialize_result = engine.handle_request(SerializeFlowToCommandsRequest(flow_name=flow_name))
    serialized = _expect(serialize_result, SerializeFlowToCommandsResultSuccess, "serialize the workflow's flow")
    _drop_machine_specific_metadata(serialized.serialized_flow_commands)

    metadata = WorkflowMetadata(
        name=target.stem,
        schema_version=WorkflowMetadata.LATEST_SCHEMA_VERSION,
        engine_version_created_with="0.0.0",
        node_libraries_referenced=list(serialized.serialized_flow_commands.node_dependencies.libraries),
        description=target.description,
        workflow_shape=WorkflowShape(inputs=shape["input"], outputs=shape["output"]),
    )
    content = workflow_manager.codegen.generate_workflow_file_content(
        serialized_flow_commands=serialized.serialized_flow_commands,
        workflow_metadata=metadata,
    )
    path = LIBRARY_DIR / f"{target.stem}.py"
    path.write_text(content, encoding="utf-8")
    print(f"Wrote {path}")  # noqa: T201


def _drop_machine_specific_metadata(serialized_flow_commands: SerializedFlowCommands) -> None:
    """Remove the absolute workflow path a Workflow node bakes into its creation metadata."""
    for node_commands in serialized_flow_commands.serialized_node_commands:
        metadata = node_commands.create_node_command.metadata
        if metadata is not None:
            metadata.pop(WORKFLOW_FILE_VALUE_KEY, None)


def main() -> None:
    """Register the fixture library against a temp copy, then regenerate every workflow file."""
    with tempfile.TemporaryDirectory() as temp_dir:
        library_json = _materialize_library(Path(temp_dir) / "library")
        register_result = current_engine().handle_request(RegisterLibraryFromFileRequest(file_path=str(library_json)))
        _expect(register_result, RegisterLibraryFromFileResultSuccess, "register the fixture library")
        for target in TARGETS:
            _generate(target)


if __name__ == "__main__":
    main()
