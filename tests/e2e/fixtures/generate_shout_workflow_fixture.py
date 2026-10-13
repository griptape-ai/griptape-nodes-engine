"""Generate the saved workflows in `workflow_node_library` that the e2e tests point nodes at.

Produces `shout_workflow.py` (Start Flow -> Shout -> End Flow) and workflows that wrap a Workflow
node between Start Flow and End Flow: `recursive_workflow.py` (runs itself), `cycle_a_workflow.py` and
`cycle_b_workflow.py` (each runs the other), and `nested_workflow.py` (runs `shout_workflow.py`).
`countdown_workflow.py` runs itself only on one branch of a Countdown node, so its recursion stops.
`countdown_fail_workflow.py` does the same but runs a node that always fails where the recursion
stops. `countdown_group_workflow.py` and `countdown_loop_workflow.py` do the same too, but their
recursing Workflow node sits inside a group and inside a for-each loop group. The loop one fails where
its countdown ends. `recursive_loop_workflow.py` has a for-each loop group whose body runs this same
workflow every iteration, with no branch to stop it. `recursive_loop_parallel_workflow.py` is the same
with the loop set to run its iterations all at once.

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
from griptape_nodes.retained_mode.events.parameter_events import SetParameterValueRequest
from griptape_nodes.utils.version_utils import engine_version

LIBRARY_DIR = Path(__file__).resolve().parent / "workflow_node_library"
LIBRARY_JSON = LIBRARY_DIR / "griptape_nodes_library.json"
# The loop executor packages a loop body with the Start and End Flow nodes of a library with this name.
LOOP_LIBRARY_DIR = Path(__file__).resolve().parent / "iterative_group_library"
LOOP_LIBRARY_NAME = "Griptape Nodes Library"
NODE_FILE = LIBRARY_DIR / "workflow_node_nodes.py"
# A second library holding only a failing node, which the worker-boundary tests host in a stand-in worker.
LEAF_LIBRARY_DIR = Path(__file__).resolve().parent / "worker_leaf_library"
LEAF_LIBRARY_NAME = "Worker Leaf Library"
LIBRARY_NAME = "Workflow Node Library"
SHOUT_WORKFLOW_PATH = LIBRARY_DIR / "shout_workflow.py"
WORKFLOW_NODE_PREFIX = "Inner Workflow"
START_NODE_NAME = "Start Flow"
END_NODE_NAME = "End Flow"
GROUP_NODE_NAME = "Group"


class NodeSpec(NamedTuple):
    """A node to create in a generated workflow."""

    node_type: str
    name: str
    parent_group: str | None = None
    library_name: str = LIBRARY_NAME
    parameter_values: dict[str, str] | None = None


class ConnectionSpec(NamedTuple):
    """A connection between two nodes of a generated workflow, by node name."""

    source_node: str
    source_param: str
    target_node: str
    target_param: str


class WorkflowTarget(NamedTuple):
    """One workflow file to generate."""

    stem: str
    description: str
    nodes: list[NodeSpec]
    connections: list[ConnectionSpec]


def _wrapping_target(
    stem: str, description: str, middle_node_type: str, middle_node_name: str, middle_output_param: str
) -> WorkflowTarget:
    """Describe Start Flow -> one middle node -> End Flow, with `text` in and `middle_output_param` out."""
    return WorkflowTarget(
        stem=stem,
        description=description,
        nodes=[
            NodeSpec("TextStartNode", START_NODE_NAME),
            NodeSpec(middle_node_type, middle_node_name),
            NodeSpec("TextEndNode", END_NODE_NAME),
        ],
        connections=[
            ConnectionSpec(START_NODE_NAME, "exec_out", middle_node_name, "exec_in"),
            ConnectionSpec(middle_node_name, "exec_out", END_NODE_NAME, "exec_in"),
            ConnectionSpec(START_NODE_NAME, "text", middle_node_name, "text"),
            ConnectionSpec(middle_node_name, middle_output_param, END_NODE_NAME, "result"),
        ],
    )


SHOUT = _wrapping_target(
    stem="shout_workflow",
    description="Uppercases the incoming text and appends an exclamation mark.",
    middle_node_type="ShoutNode",
    middle_node_name="Shout",
    middle_output_param="shouted",
)
RECURSIVE = _wrapping_target(
    stem="recursive_workflow",
    description="Contains a Workflow node that runs this same workflow.",
    middle_node_type="RecursiveWorkflow",
    middle_node_name=WORKFLOW_NODE_PREFIX,
    middle_output_param="result",
)
CYCLE_A = _wrapping_target(
    stem="cycle_a_workflow",
    description="Contains a Workflow node that runs cycle B, which runs this workflow again.",
    middle_node_type="CycleBWorkflow",
    middle_node_name=WORKFLOW_NODE_PREFIX,
    middle_output_param="result",
)
CYCLE_B = _wrapping_target(
    stem="cycle_b_workflow",
    description="Contains a Workflow node that runs cycle A, which runs this workflow again.",
    middle_node_type="CycleAWorkflow",
    middle_node_name=WORKFLOW_NODE_PREFIX,
    middle_output_param="result",
)
NESTED = _wrapping_target(
    stem="nested_workflow",
    description="Contains a Workflow node that runs the shout workflow.",
    middle_node_type="ShoutWorkflow",
    middle_node_name=WORKFLOW_NODE_PREFIX,
    middle_output_param="result",
)
# The inner Workflow node's `result` stays unconnected so the End Flow does not depend on it and the
# recursion only happens on the Then branch.
COUNTDOWN = WorkflowTarget(
    stem="countdown_workflow",
    description="Counts text down one character per level and runs this workflow again while text is left.",
    nodes=[
        NodeSpec("TextStartNode", START_NODE_NAME),
        NodeSpec("CountdownNode", "Countdown"),
        NodeSpec("CountdownWorkflow", WORKFLOW_NODE_PREFIX),
        NodeSpec("TextEndNode", END_NODE_NAME),
    ],
    connections=[
        ConnectionSpec(START_NODE_NAME, "exec_out", "Countdown", "exec_in"),
        ConnectionSpec(START_NODE_NAME, "text", "Countdown", "text"),
        ConnectionSpec("Countdown", "Then", WORKFLOW_NODE_PREFIX, "exec_in"),
        ConnectionSpec("Countdown", "rest", WORKFLOW_NODE_PREFIX, "text"),
        ConnectionSpec(WORKFLOW_NODE_PREFIX, "exec_out", END_NODE_NAME, "exec_in"),
        ConnectionSpec("Countdown", "Else", END_NODE_NAME, "exec_in"),
        ConnectionSpec("Countdown", "rest", END_NODE_NAME, "result"),
    ],
)
# Like COUNTDOWN, but the branch that stops the recursion runs a node that always fails.
COUNTDOWN_FAIL = WorkflowTarget(
    stem="countdown_fail_workflow",
    description="Counts text down one character per level, then fails where the countdown ends.",
    nodes=[
        NodeSpec("TextStartNode", START_NODE_NAME),
        NodeSpec("CountdownNode", "Countdown"),
        NodeSpec("CountdownFailWorkflow", WORKFLOW_NODE_PREFIX),
        NodeSpec("FailingNode", "Failing"),
        NodeSpec("TextEndNode", END_NODE_NAME),
    ],
    connections=[
        ConnectionSpec(START_NODE_NAME, "exec_out", "Countdown", "exec_in"),
        ConnectionSpec(START_NODE_NAME, "text", "Countdown", "text"),
        ConnectionSpec("Countdown", "Then", WORKFLOW_NODE_PREFIX, "exec_in"),
        ConnectionSpec("Countdown", "rest", WORKFLOW_NODE_PREFIX, "text"),
        ConnectionSpec(WORKFLOW_NODE_PREFIX, "exec_out", END_NODE_NAME, "exec_in"),
        ConnectionSpec("Countdown", "Else", "Failing", "exec_in"),
        ConnectionSpec("Failing", "exec_out", END_NODE_NAME, "exec_in"),
        ConnectionSpec("Countdown", "rest", END_NODE_NAME, "result"),
    ],
)
# Like COUNTDOWN_FAIL, but the failing node comes from the Worker Leaf Library.
COUNTDOWN_FAIL_WORKER = WorkflowTarget(
    stem="countdown_fail_worker_workflow",
    description="Counts text down one character per level, then fails in a node from another library.",
    nodes=[
        NodeSpec("TextStartNode", START_NODE_NAME),
        NodeSpec("CountdownNode", "Countdown"),
        NodeSpec("CountdownFailWorkerWorkflow", WORKFLOW_NODE_PREFIX),
        NodeSpec("WorkerFailingNode", "Failing", library_name=LEAF_LIBRARY_NAME),
        NodeSpec("TextEndNode", END_NODE_NAME),
    ],
    connections=COUNTDOWN_FAIL.connections,
)
# Like COUNTDOWN, but the recursing Workflow node is the only member of a group.
COUNTDOWN_GROUP = WorkflowTarget(
    stem="countdown_group_workflow",
    description="Counts text down one character per level and runs this workflow again, inside a group.",
    nodes=[
        NodeSpec("TextStartNode", START_NODE_NAME),
        NodeSpec("CountdownNode", "Countdown"),
        NodeSpec("FixtureGroupNode", GROUP_NODE_NAME),
        NodeSpec("CountdownGroupWorkflow", WORKFLOW_NODE_PREFIX, parent_group=GROUP_NODE_NAME),
        NodeSpec("TextEndNode", END_NODE_NAME),
    ],
    connections=[
        ConnectionSpec(START_NODE_NAME, "exec_out", "Countdown", "exec_in"),
        ConnectionSpec(START_NODE_NAME, "text", "Countdown", "text"),
        ConnectionSpec("Countdown", "Then", WORKFLOW_NODE_PREFIX, "exec_in"),
        ConnectionSpec("Countdown", "rest", WORKFLOW_NODE_PREFIX, "text"),
        ConnectionSpec(WORKFLOW_NODE_PREFIX, "exec_out", END_NODE_NAME, "exec_in"),
        ConnectionSpec("Countdown", "Else", END_NODE_NAME, "exec_in"),
        ConnectionSpec("Countdown", "rest", END_NODE_NAME, "result"),
    ],
)
LOOP_NODE_NAME = "Loop"
# Like COUNTDOWN_FAIL, but the recursing Workflow node is the body of a for-each loop group.
COUNTDOWN_LOOP = WorkflowTarget(
    stem="countdown_loop_workflow",
    description="Counts text down one character per level inside a loop, then fails where the countdown ends.",
    nodes=[
        NodeSpec("TextStartNode", START_NODE_NAME),
        NodeSpec("CountdownNode", "Countdown"),
        NodeSpec("ProbeForEachGroupNode", LOOP_NODE_NAME, library_name=LOOP_LIBRARY_NAME),
        NodeSpec("CountdownLoopWorkflow", WORKFLOW_NODE_PREFIX, parent_group=LOOP_NODE_NAME),
        NodeSpec("FailingNode", "Failing"),
        NodeSpec("TextEndNode", END_NODE_NAME),
    ],
    connections=[
        ConnectionSpec(START_NODE_NAME, "exec_out", "Countdown", "exec_in"),
        ConnectionSpec(START_NODE_NAME, "text", "Countdown", "text"),
        ConnectionSpec("Countdown", "Then", LOOP_NODE_NAME, "exec_in"),
        ConnectionSpec("Countdown", "rest", WORKFLOW_NODE_PREFIX, "text"),
        ConnectionSpec(LOOP_NODE_NAME, "on_each", WORKFLOW_NODE_PREFIX, "exec_in"),
        ConnectionSpec(WORKFLOW_NODE_PREFIX, "exec_out", LOOP_NODE_NAME, "loop_complete"),
        ConnectionSpec(LOOP_NODE_NAME, "exec_out", END_NODE_NAME, "exec_in"),
        ConnectionSpec("Countdown", "Else", "Failing", "exec_in"),
        ConnectionSpec("Failing", "exec_out", END_NODE_NAME, "exec_in"),
        ConnectionSpec("Countdown", "rest", END_NODE_NAME, "result"),
    ],
)
# The loop body runs this same workflow on every iteration, with no branch that stops it.
RECURSIVE_LOOP = WorkflowTarget(
    stem="recursive_loop_workflow",
    description="Runs this workflow again on every iteration of a loop, with nothing to stop it.",
    nodes=[
        NodeSpec("TextStartNode", START_NODE_NAME),
        NodeSpec("ProbeForEachGroupNode", LOOP_NODE_NAME, library_name=LOOP_LIBRARY_NAME),
        NodeSpec("RecursiveLoopWorkflow", WORKFLOW_NODE_PREFIX, parent_group=LOOP_NODE_NAME),
        NodeSpec("TextEndNode", END_NODE_NAME),
    ],
    connections=[
        ConnectionSpec(START_NODE_NAME, "exec_out", LOOP_NODE_NAME, "exec_in"),
        ConnectionSpec(START_NODE_NAME, "text", WORKFLOW_NODE_PREFIX, "text"),
        ConnectionSpec(LOOP_NODE_NAME, "on_each", WORKFLOW_NODE_PREFIX, "exec_in"),
        ConnectionSpec(WORKFLOW_NODE_PREFIX, "exec_out", LOOP_NODE_NAME, "loop_complete"),
        ConnectionSpec(LOOP_NODE_NAME, "exec_out", END_NODE_NAME, "exec_in"),
        ConnectionSpec(START_NODE_NAME, "text", END_NODE_NAME, "result"),
    ],
)
# Like RECURSIVE_LOOP, but the loop runs its iterations all at once.
RECURSIVE_LOOP_PARALLEL = WorkflowTarget(
    stem="recursive_loop_parallel_workflow",
    description="Runs this workflow again on every iteration of an all-at-once loop, with nothing to stop it.",
    nodes=[
        NodeSpec("TextStartNode", START_NODE_NAME),
        NodeSpec(
            "ProbeForEachGroupNode",
            LOOP_NODE_NAME,
            library_name=LOOP_LIBRARY_NAME,
            parameter_values={"execution_mode": "Run Group Items All at Once"},
        ),
        NodeSpec("RecursiveLoopParallelWorkflow", WORKFLOW_NODE_PREFIX, parent_group=LOOP_NODE_NAME),
        NodeSpec("TextEndNode", END_NODE_NAME),
    ],
    connections=RECURSIVE_LOOP.connections,
)
# Generated in this order: nested runs shout, so shout comes first.
TARGETS = [
    SHOUT,
    RECURSIVE,
    CYCLE_A,
    CYCLE_B,
    NESTED,
    COUNTDOWN,
    COUNTDOWN_FAIL,
    COUNTDOWN_FAIL_WORKER,
    COUNTDOWN_GROUP,
    COUNTDOWN_LOOP,
    RECURSIVE_LOOP,
    RECURSIVE_LOOP_PARALLEL,
]
# Workflow node types registered against placeholder files so the real files can reference them.
EXTRA_WORKFLOW_NODES = {
    "RecursiveWorkflow": RECURSIVE.stem,
    "CycleAWorkflow": CYCLE_A.stem,
    "CycleBWorkflow": CYCLE_B.stem,
    "NestedWorkflow": NESTED.stem,
    "CountdownWorkflow": COUNTDOWN.stem,
    "CountdownFailWorkflow": COUNTDOWN_FAIL.stem,
    "CountdownFailWorkerWorkflow": COUNTDOWN_FAIL_WORKER.stem,
    "CountdownGroupWorkflow": COUNTDOWN_GROUP.stem,
    "CountdownLoopWorkflow": COUNTDOWN_LOOP.stem,
    "RecursiveLoopWorkflow": RECURSIVE_LOOP.stem,
    "RecursiveLoopParallelWorkflow": RECURSIVE_LOOP_PARALLEL.stem,
}


class FixtureGenerationError(RuntimeError):
    """Raised when a step of the fixture generation does not succeed."""


def _expect[T](result: object, expected: type[T], attempted: str) -> T:
    """Narrow `result` to `expected`, or fail with the engine's own failure details."""
    if not isinstance(result, expected):
        msg = f"Attempted to {attempted}. Failed with result: {result}"
        raise FixtureGenerationError(msg)
    return result


def _create(spec: NodeSpec, flow_name: str, parent_group_name: str | None) -> str:
    engine = current_engine()
    # A group builds its subflow under the current flow, so the flow has to be the current one.
    with engine.context_manager.flow(flow_name):
        result = engine.handle_request(
            CreateNodeRequest(
                node_type=spec.node_type,
                specific_library_name=spec.library_name,
                node_name=spec.name,
                override_parent_flow_name=flow_name,
                parent_group_name=parent_group_name,
            )
        )
    return _expect(result, CreateNodeResultSuccess, f"create a '{spec.node_type}' node").node_name


def _set_parameter(node_name: str, parameter_name: str, value: str) -> None:
    result = current_engine().handle_request(
        SetParameterValueRequest(node_name=node_name, parameter_name=parameter_name, value=value)
    )
    if result.failed():
        msg = f"Attempted to set {node_name}.{parameter_name} to {value!r}. Failed: {result}"
        raise FixtureGenerationError(msg)


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


def _materialize_loop_library(target_dir: Path) -> Path:
    """Copy the iterative group library into `target_dir` under the name the loop executor asks for."""
    target_dir.mkdir(parents=True, exist_ok=True)
    schema = json.loads((LOOP_LIBRARY_DIR / "griptape_nodes_library.json").read_text())
    schema["name"] = LOOP_LIBRARY_NAME
    schema["metadata"]["engine_version"] = engine_version
    library_json = target_dir / "griptape_nodes_library.json"
    library_json.write_text(json.dumps(schema, indent=2))
    node_file = LOOP_LIBRARY_DIR / "iterative_group_nodes.py"
    (target_dir / node_file.name).write_text(node_file.read_text())
    return library_json


def _materialize_leaf_library(target_dir: Path) -> Path:
    """Copy the worker leaf library into `target_dir` with the running engine version."""
    target_dir.mkdir(parents=True, exist_ok=True)
    schema = json.loads((LEAF_LIBRARY_DIR / "griptape_nodes_library.json").read_text())
    schema["metadata"]["engine_version"] = engine_version
    library_json = target_dir / "griptape_nodes_library.json"
    library_json.write_text(json.dumps(schema, indent=2))
    node_file = LEAF_LIBRARY_DIR / "worker_leaf_nodes.py"
    (target_dir / node_file.name).write_text(node_file.read_text())
    return library_json


def _generate(target: WorkflowTarget) -> None:
    """Build the target's nodes and connections and write them out as a saved workflow."""
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

    created_names: dict[str, str] = {}
    for spec in target.nodes:
        parent_group_name = None
        if spec.parent_group is not None:
            parent_group_name = created_names[spec.parent_group]
        created_names[spec.name] = _create(spec, flow_name, parent_group_name)
        for parameter_name, value in (spec.parameter_values or {}).items():
            _set_parameter(created_names[spec.name], parameter_name, value)
    for connection in target.connections:
        _connect(
            created_names[connection.source_node],
            connection.source_param,
            created_names[connection.target_node],
            connection.target_param,
        )

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
        node_libraries_referenced=sorted(
            serialized.serialized_flow_commands.node_dependencies.libraries, key=lambda library: library.library_name
        ),
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
    """Remove the absolute workflow path a Workflow node bakes into its creation metadata.

    Looks through sub-flows too, because the members of a group are serialized into one.
    """
    for node_commands in serialized_flow_commands.serialized_node_commands:
        metadata = node_commands.create_node_command.metadata
        if metadata is not None:
            metadata.pop(WORKFLOW_FILE_VALUE_KEY, None)
    for sub_flow_commands in serialized_flow_commands.sub_flows_commands:
        _drop_machine_specific_metadata(sub_flow_commands)


def main() -> None:
    """Register the fixture library against a temp copy, then regenerate every workflow file."""
    with tempfile.TemporaryDirectory() as temp_dir:
        library_json = _materialize_library(Path(temp_dir) / "library")
        register_result = current_engine().handle_request(RegisterLibraryFromFileRequest(file_path=str(library_json)))
        _expect(register_result, RegisterLibraryFromFileResultSuccess, "register the fixture library")
        loop_library_json = _materialize_loop_library(Path(temp_dir) / "loop_library")
        loop_register_result = current_engine().handle_request(
            RegisterLibraryFromFileRequest(file_path=str(loop_library_json))
        )
        _expect(loop_register_result, RegisterLibraryFromFileResultSuccess, "register the loop library")
        leaf_library_json = _materialize_leaf_library(Path(temp_dir) / "leaf_library")
        leaf_register_result = current_engine().handle_request(
            RegisterLibraryFromFileRequest(file_path=str(leaf_library_json))
        )
        _expect(leaf_register_result, RegisterLibraryFromFileResultSuccess, "register the worker leaf library")
        for target in TARGETS:
            _generate(target)


if __name__ == "__main__":
    main()
