"""Generate the saved workflows in `workflow_node_library` that the e2e tests point nodes at.

Produces `shout_workflow.py` (Start Flow -> Shout -> End Flow) and `fail_workflow.py` (Start Flow ->
a node that always fails -> End Flow).

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
import tempfile
from pathlib import Path
from typing import NamedTuple

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

LIBRARY_DIR = Path(__file__).resolve().parent / "workflow_node_library"
LIBRARY_JSON = LIBRARY_DIR / "griptape_nodes_library.json"
NODE_FILE = LIBRARY_DIR / "workflow_node_nodes.py"
# A second library holding the failing node that `fail_workflow.py` runs.
LEAF_LIBRARY_DIR = Path(__file__).resolve().parent / "worker_leaf_library"
LEAF_LIBRARY_JSON = LEAF_LIBRARY_DIR / "griptape_nodes_library.json"
LEAF_NODE_FILE = LEAF_LIBRARY_DIR / "worker_leaf_nodes.py"
LIBRARY_NAME = "Workflow Node Library"
LEAF_LIBRARY_NAME = "Worker Leaf Library"
START_NODE_NAME = "Start Flow"
END_NODE_NAME = "End Flow"


class WorkflowTarget(NamedTuple):
    """One workflow file to generate: Start Flow, one middle node, End Flow."""

    stem: str
    description: str
    middle_node_type: str
    middle_node_name: str
    middle_library_name: str
    # The middle node's output wired to the End Flow node's `result`, if it has one.
    middle_output_param: str | None

    @property
    def path(self) -> Path:
        return LIBRARY_DIR / f"{self.stem}.py"


SHOUT = WorkflowTarget(
    stem="shout_workflow",
    description="Uppercases the incoming text and appends an exclamation mark.",
    middle_node_type="ShoutNode",
    middle_node_name="Shout",
    middle_library_name=LIBRARY_NAME,
    middle_output_param="shouted",
)
FAIL = WorkflowTarget(
    stem="fail_workflow",
    description="Runs a node that always fails.",
    middle_node_type="WorkerFailingNode",
    middle_node_name="Failing",
    middle_library_name=LEAF_LIBRARY_NAME,
    middle_output_param=None,
)
TARGETS = (SHOUT, FAIL)


class FixtureGenerationError(RuntimeError):
    """Raised when a step of the fixture generation does not succeed."""


def _expect[T](result: object, expected: type[T], attempted: str) -> T:
    """Narrow `result` to `expected`, or fail with the engine's own failure details."""
    if not isinstance(result, expected):
        msg = f"Attempted to {attempted}. Failed with result: {result}"
        raise FixtureGenerationError(msg)
    return result


def _create(node_type: str, node_name: str, flow_name: str, library_name: str) -> str:
    result = current_engine().handle_request(
        CreateNodeRequest(
            node_type=node_type,
            specific_library_name=library_name,
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
    return library_json


def _materialize_leaf_library(target_dir: Path) -> Path:
    """Copy the failing-node library into `target_dir` with the running engine version."""
    target_dir.mkdir(parents=True, exist_ok=True)
    schema = json.loads(LEAF_LIBRARY_JSON.read_text())
    schema["metadata"]["engine_version"] = engine_version
    library_json = target_dir / "griptape_nodes_library.json"
    library_json.write_text(json.dumps(schema, indent=2))
    (target_dir / LEAF_NODE_FILE.name).write_text(LEAF_NODE_FILE.read_text())
    return library_json


def _generate(target: WorkflowTarget) -> None:
    """Build the target's Start -> middle node -> End graph and write it out as a saved workflow."""
    current_engine().handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))
    current_engine().context_manager.push_workflow(workflow_name=target.stem)

    flow_result = current_engine().handle_request(
        CreateFlowRequest(parent_flow_name=None, flow_name="ControlFlow_1", set_as_new_context=False)
    )
    flow_name = _expect(flow_result, CreateFlowResultSuccess, "create the workflow's flow").flow_name

    start_node = _create("TextStartNode", START_NODE_NAME, flow_name, LIBRARY_NAME)
    middle_node = _create(target.middle_node_type, target.middle_node_name, flow_name, target.middle_library_name)
    end_node = _create("TextEndNode", END_NODE_NAME, flow_name, LIBRARY_NAME)

    _connect(start_node, "exec_out", middle_node, "exec_in")
    _connect(middle_node, "exec_out", end_node, "exec_in")
    if target.middle_output_param is not None:
        _connect(start_node, "text", middle_node, "text")
        _connect(middle_node, target.middle_output_param, end_node, "result")

    # Lay the graph out before serializing so node positions are baked into the emitted file.
    # Without this every node serializes without a position and they stack on the same spot when
    # the workflow is opened or previewed.
    layout_result = current_engine().handle_request(AutoLayoutFlowRequest(flow_name=flow_name))
    _expect(layout_result, AutoLayoutFlowResultSuccess, "lay out the workflow's nodes")

    workflow_manager = current_engine().workflow_manager
    shape = workflow_manager.extract_workflow_shape(target.stem, flow_name=flow_name)

    serialize_result = current_engine().handle_request(SerializeFlowToCommandsRequest(flow_name=flow_name))
    serialized = _expect(serialize_result, SerializeFlowToCommandsResultSuccess, "serialize the workflow's flow")

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
    target.path.write_text(content, encoding="utf-8")
    print(f"Wrote {target.path}")  # noqa: T201


def main() -> None:
    """Register the fixture libraries against temp copies, then regenerate the workflow files."""
    current_engine().handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))

    with tempfile.TemporaryDirectory() as temp_dir:
        library_json = _materialize_library(Path(temp_dir) / "library")
        register_result = current_engine().handle_request(RegisterLibraryFromFileRequest(file_path=str(library_json)))
        _expect(register_result, RegisterLibraryFromFileResultSuccess, "register the fixture library")
        leaf_library_json = _materialize_leaf_library(Path(temp_dir) / "leaf_library")
        leaf_register_result = current_engine().handle_request(
            RegisterLibraryFromFileRequest(file_path=str(leaf_library_json))
        )
        _expect(leaf_register_result, RegisterLibraryFromFileResultSuccess, "register the failing-node library")
        for target in TARGETS:
            _generate(target)


if __name__ == "__main__":
    main()
