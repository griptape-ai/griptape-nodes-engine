"""End-to-end coverage for node types generated from workflow files.

A library can declare a node in ``workflow_nodes`` instead of ``nodes``, pointing at a saved
workflow file that has Start Flow and End Flow nodes. Registering the library must then produce a
usable node type whose parameters mirror the workflow's saved shape, and running that node must
execute the workflow and hand its End Flow values back as node outputs.

The fixture library ships ``shout_workflow.py`` (Start -> Shout -> End), regenerate it with
``uv run python tests/e2e/fixtures/generate_shout_workflow_fixture.py`` when the workflow file format
changes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from griptape_nodes.exe_types.node_types import NodeResolutionState
from griptape_nodes.exe_types.workflow_node import WorkflowNode
from griptape_nodes.retained_mode.events.execution_events import StartFlowRequest, StartFlowResultSuccess
from griptape_nodes.retained_mode.events.flow_events import (
    CreateFlowRequest,
    CreateFlowResultSuccess,
    ListFlowsInFlowRequest,
    ListFlowsInFlowResultSuccess,
)
from griptape_nodes.retained_mode.events.library_events import (
    ListNodeTypesInLibraryRequest,
    ListNodeTypesInLibraryResultSuccess,
    RegisterLibraryFromFileRequest,
    RegisterLibraryFromFileResultSuccess,
)
from griptape_nodes.retained_mode.events.parameter_events import SetParameterValueRequest
from griptape_nodes.retained_mode.events.workflow_events import (
    CreateWorkflowFromTemplateRequest,
    CreateWorkflowFromTemplateResultSuccess,
    ListAllWorkflowsRequest,
    ListAllWorkflowsResultSuccess,
    ListCallableWorkflowsRequest,
    ListCallableWorkflowsResultSuccess,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from griptape_nodes.retained_mode.engine import Engine

# Timeout with thread dump.
pytestmark = pytest.mark.timeout(300, method="thread")

FIXTURE_LIBRARY_DIR = Path(__file__).parent / "fixtures" / "workflow_node_library"
FIXTURE_LIBRARY_JSON_TEMPLATE = FIXTURE_LIBRARY_DIR / "griptape_nodes_library.json"
FIXTURE_NODE_FILE = FIXTURE_LIBRARY_DIR / "workflow_node_nodes.py"
FIXTURE_WORKFLOW_FILE = FIXTURE_LIBRARY_DIR / "shout_workflow.py"
LIBRARY_NAME = "Workflow Node Library"
WORKFLOW_NODE_TYPE = "ShoutWorkflow"


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


@pytest.fixture
def workspace_backed_library(tmp_path: Path, engine: Engine, materialize_library: Callable[..., Path]) -> Path:
    """Register the fixture library with its backing workflow moved into the workspace; return the workflow path."""
    library_json = materialize_library(
        tmp_path / "library",
        template=FIXTURE_LIBRARY_JSON_TEMPLATE,
        node_file=FIXTURE_NODE_FILE,
    )
    workflow_directory = engine.config_manager.workspace_path / "nested"
    workflow_directory.mkdir(parents=True, exist_ok=True)
    workflow_file = workflow_directory / FIXTURE_WORKFLOW_FILE.name
    workflow_file.write_text(FIXTURE_WORKFLOW_FILE.read_text())

    schema = json.loads(library_json.read_text())
    schema["workflow_nodes"][0]["workflow_path"] = str(workflow_file)
    library_json.write_text(json.dumps(schema, indent=2))

    register_result = engine.handle_request(RegisterLibraryFromFileRequest(file_path=str(library_json)))
    assert isinstance(register_result, RegisterLibraryFromFileResultSuccess), register_result
    return workflow_file


@pytest.fixture
def symlinked_workflow_library(tmp_path: Path, engine: Engine, materialize_library: Callable[..., Path]) -> Path:
    """Register the fixture library against a workflow linked into the workspace; return the link's path."""
    library_json = materialize_library(
        tmp_path / "library",
        template=FIXTURE_LIBRARY_JSON_TEMPLATE,
        node_file=FIXTURE_NODE_FILE,
    )
    shared_directory = tmp_path / "shared"
    shared_directory.mkdir(parents=True, exist_ok=True)
    shared_workflow = shared_directory / FIXTURE_WORKFLOW_FILE.name
    shared_workflow.write_text(FIXTURE_WORKFLOW_FILE.read_text())

    link_directory = engine.config_manager.workspace_path / "linked"
    link_directory.mkdir(parents=True, exist_ok=True)
    workflow_link = link_directory / FIXTURE_WORKFLOW_FILE.name
    workflow_link.symlink_to(shared_workflow)

    schema = json.loads(library_json.read_text())
    schema["workflow_nodes"][0]["workflow_path"] = str(workflow_link)
    library_json.write_text(json.dumps(schema, indent=2))

    register_result = engine.handle_request(RegisterLibraryFromFileRequest(file_path=str(library_json)))
    assert isinstance(register_result, RegisterLibraryFromFileResultSuccess), register_result
    return workflow_link


@pytest.fixture
def parent_linked_library(tmp_path: Path, engine: Engine, materialize_library: Callable[..., Path]) -> Path:
    """Register a library through a link to the workspace folder itself; return the workflow's workspace-side path."""
    workspace_path = engine.config_manager.workspace_path
    library_json = materialize_library(
        workspace_path / "mylib",
        template=FIXTURE_LIBRARY_JSON_TEMPLATE,
        node_file=FIXTURE_NODE_FILE,
    )
    workflow_directory = workspace_path / "nested"
    workflow_directory.mkdir(parents=True, exist_ok=True)
    workflow_file = workflow_directory / FIXTURE_WORKFLOW_FILE.name
    workflow_file.write_text(FIXTURE_WORKFLOW_FILE.read_text())

    schema = json.loads(library_json.read_text())
    schema["workflow_nodes"][0]["workflow_path"] = f"../nested/{FIXTURE_WORKFLOW_FILE.name}"
    library_json.write_text(json.dumps(schema, indent=2))

    workspace_link = tmp_path / "ws-link"
    workspace_link.symlink_to(workspace_path, target_is_directory=True)
    linked_library_json = workspace_link / "mylib" / library_json.name

    register_result = engine.handle_request(RegisterLibraryFromFileRequest(file_path=str(linked_library_json)))
    assert isinstance(register_result, RegisterLibraryFromFileResultSuccess), register_result
    return workflow_file


@pytest.mark.skipif(
    not FIXTURE_WORKFLOW_FILE.exists(),
    reason=f"Workflow Node Library fixture workflow missing at {FIXTURE_WORKFLOW_FILE}",
)
def test_workflow_node_registers_with_shape_derived_parameters(
    registered_library: Path,  # noqa: ARG001
    engine: Engine,
    create_node: Callable[..., str],
) -> None:
    """A `workflow_nodes` entry becomes a real node type whose parameters mirror the shape."""
    list_result = engine.handle_request(ListNodeTypesInLibraryRequest(library=LIBRARY_NAME))
    assert isinstance(list_result, ListNodeTypesInLibraryResultSuccess), list_result
    assert WORKFLOW_NODE_TYPE in list_result.node_types

    engine.context_manager.push_workflow(workflow_name="workflow_node_e2e_shape")

    flow_result = engine.handle_request(
        CreateFlowRequest(parent_flow_name=None, flow_name="ParentFlow", set_as_new_context=False)
    )
    assert isinstance(flow_result, CreateFlowResultSuccess), flow_result

    create_node(WORKFLOW_NODE_TYPE, "Shout It", flow_result.flow_name, library_name=LIBRARY_NAME)
    node = engine.node_manager.get_node_by_name("Shout It")

    assert isinstance(node, WorkflowNode), f"Expected a workflow-backed node, got {type(node).__name__}"
    # `text` comes from the workflow's Start Flow node, `result` from its End Flow node. Control
    # parameters in the shape are dropped in favor of the node's own control flow, and so is the End
    # Flow node's Status group, which reports on that node's run rather than on the workflow's output.
    assert [parameter.name for parameter in node.parameters] == ["exec_in", "exec_out", "text", "result"]
    assert node.get_parameter_by_name("exec_out") is node.control_parameter_out


@pytest.mark.skipif(
    not FIXTURE_WORKFLOW_FILE.exists(),
    reason=f"Workflow Node Library fixture workflow missing at {FIXTURE_WORKFLOW_FILE}",
)
@pytest.mark.asyncio
async def test_workflow_node_runs_its_workflow_and_returns_outputs(
    registered_library: Path,  # noqa: ARG001
    engine: Engine,
    create_node: Callable[..., str],
) -> None:
    """Running the generated node executes the workflow and surfaces its End Flow values."""
    engine.context_manager.push_workflow(workflow_name="workflow_node_e2e")

    flow_result = engine.handle_request(
        CreateFlowRequest(parent_flow_name=None, flow_name="ParentFlow", set_as_new_context=False)
    )
    assert isinstance(flow_result, CreateFlowResultSuccess), flow_result
    parent_flow = flow_result.flow_name

    create_node(WORKFLOW_NODE_TYPE, "Shout It", parent_flow, library_name=LIBRARY_NAME)
    set_result = engine.handle_request(
        SetParameterValueRequest(parameter_name="text", node_name="Shout It", value="hello there")
    )
    assert set_result.succeeded(), set_result

    run_result = await engine.ahandle_request(
        StartFlowRequest(
            flow_name=parent_flow,
            flow_node_name="Shout It",
        )
    )
    assert isinstance(run_result, StartFlowResultSuccess), run_result

    node = engine.node_manager.get_node_by_name("Shout It")
    assert node.state == NodeResolutionState.RESOLVED
    assert node.parameter_output_values.get("result") == "HELLO THERE!"

    # The workflow was imported as a child flow of the node's own flow so it can be inspected
    # during the session, and it is tagged transient so a save never bakes it in.
    subflows_result = engine.handle_request(ListFlowsInFlowRequest(parent_flow_name=parent_flow))
    assert isinstance(subflows_result, ListFlowsInFlowResultSuccess), subflows_result
    assert node.metadata["subflow_name"] in subflows_result.flow_names


@pytest.mark.skipif(
    not FIXTURE_WORKFLOW_FILE.exists(),
    reason=f"Workflow Node Library fixture workflow missing at {FIXTURE_WORKFLOW_FILE}",
)
@pytest.mark.asyncio
async def test_two_workflow_nodes_run_independently(
    registered_library: Path,  # noqa: ARG001
    engine: Engine,
    create_node: Callable[..., str],
    connect: Callable[..., None],
) -> None:
    """Two instances of the same generated node each get their own subflow.

    The second import renames the workflow's Start/End nodes (their names are already taken), so
    this covers the path where the saved shape's node names no longer match the live subflow.
    """
    engine.context_manager.push_workflow(workflow_name="workflow_node_e2e_pair")

    flow_result = engine.handle_request(
        CreateFlowRequest(parent_flow_name=None, flow_name="ParentFlow", set_as_new_context=False)
    )
    assert isinstance(flow_result, CreateFlowResultSuccess), flow_result
    parent_flow = flow_result.flow_name

    create_node(WORKFLOW_NODE_TYPE, "First", parent_flow, library_name=LIBRARY_NAME)
    create_node(WORKFLOW_NODE_TYPE, "Second", parent_flow, library_name=LIBRARY_NAME)
    connect("First", "exec_out", "Second", "exec_in")
    connect("First", "result", "Second", "text")

    set_result = engine.handle_request(SetParameterValueRequest(parameter_name="text", node_name="First", value="hey"))
    assert set_result.succeeded(), set_result

    run_result = await engine.ahandle_request(
        StartFlowRequest(
            flow_name=parent_flow,
            flow_node_name="First",
        )
    )
    assert isinstance(run_result, StartFlowResultSuccess), run_result

    node_manager = engine.node_manager
    first = node_manager.get_node_by_name("First")
    second = node_manager.get_node_by_name("Second")

    assert first.parameter_output_values.get("result") == "HEY!"
    assert second.parameter_output_values.get("result") == "HEY!!"
    assert first.metadata["subflow_name"] != second.metadata["subflow_name"]


@pytest.mark.skipif(
    not FIXTURE_WORKFLOW_FILE.exists(),
    reason=f"Workflow Node Library fixture workflow missing at {FIXTURE_WORKFLOW_FILE}",
)
@pytest.mark.asyncio
async def test_registering_the_library_lists_the_backing_workflow(registered_library: Path, engine: Engine) -> None:
    """A workflow the library ships is listed as library-provided before any node is placed."""
    registry_key = (registered_library.parent / "shout_workflow").as_posix()

    list_result = await engine.ahandle_request(ListAllWorkflowsRequest())
    assert isinstance(list_result, ListAllWorkflowsResultSuccess), list_result
    assert list_result.workflows[registry_key]["is_griptape_provided"] is True

    callable_result = await engine.ahandle_request(ListCallableWorkflowsRequest())
    assert isinstance(callable_result, ListCallableWorkflowsResultSuccess), callable_result
    assert registry_key in callable_result.workflow_names

    # A workspace refresh clears every workflow, then lists the library's workflows again.
    await engine.workflow_manager.refresh_workflow_registry()

    list_result = await engine.ahandle_request(ListAllWorkflowsRequest())
    assert isinstance(list_result, ListAllWorkflowsResultSuccess), list_result
    assert registry_key in list_result.workflows


@pytest.fixture
def installed_library(engine: Engine, materialize_library: Callable[..., Path]) -> Path:
    """Register the fixture library from `<workspace>/libraries`, where libraries install by default."""
    library_json = materialize_library(
        engine.config_manager.workspace_path / "libraries" / "workflow-node-library",
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
@pytest.mark.asyncio
async def test_a_library_installed_in_the_workspace_lists_its_backing_workflow(
    installed_library: Path,  # noqa: ARG001
    engine: Engine,
    create_node: Callable[..., str],
) -> None:
    """A library in the default install location lists its workflow, keeps it through a refresh, and reuses it."""
    registry_key = "libraries/workflow-node-library/shout_workflow"

    list_result = await engine.ahandle_request(ListAllWorkflowsRequest())
    assert isinstance(list_result, ListAllWorkflowsResultSuccess), list_result
    assert list_result.workflows[registry_key]["is_griptape_provided"] is True

    await engine.workflow_manager.refresh_workflow_registry()

    engine.context_manager.push_workflow(workflow_name="workflow_node_e2e_installed")
    flow_result = engine.handle_request(
        CreateFlowRequest(parent_flow_name=None, flow_name="ParentFlow", set_as_new_context=False)
    )
    assert isinstance(flow_result, CreateFlowResultSuccess), flow_result
    create_node(WORKFLOW_NODE_TYPE, "Shout It", flow_result.flow_name, library_name=LIBRARY_NAME)

    listed = [key for key in engine.workflow_registry.list_workflows() if key.endswith("shout_workflow")]
    assert listed == [registry_key]
    assert engine.workflow_registry.get_workflow_by_name(registry_key).metadata.is_griptape_provided is True
    node = engine.node_manager.get_node_by_name("Shout It")
    assert node.metadata["_workflow_file_value"] == registry_key


@pytest.mark.skipif(
    not FIXTURE_WORKFLOW_FILE.exists(),
    reason=f"Workflow Node Library fixture workflow missing at {FIXTURE_WORKFLOW_FILE}",
)
@pytest.mark.asyncio
async def test_a_library_installed_in_the_workspace_lists_its_backing_workflow_after_a_project_switch(
    installed_library: Path, engine: Engine, tmp_path: Path
) -> None:
    """After the workspace changes, the backing workflow is listed under a key that resolves to its file."""
    workflow_file = installed_library.parent / FIXTURE_WORKFLOW_FILE.name
    other_workspace = tmp_path / "other_project"
    other_workspace.mkdir()

    engine.config_manager.workspace_path = other_workspace
    await engine.workflow_manager.refresh_workflow_registry()

    listed = [
        metadata
        for key, metadata in engine.workflow_registry.list_workflows().items()
        if key.endswith("shout_workflow") and metadata["is_griptape_provided"]
    ]
    assert len(listed) == 1
    assert listed[0]["file_path"] is not None
    resolved = Path(engine.workflow_registry.get_complete_file_path(listed[0]["file_path"]))
    assert resolved == workflow_file.resolve()


@pytest.fixture
def template_library(tmp_path: Path, engine: Engine, materialize_library: Callable[..., Path]) -> Path:
    """Register the fixture library with its workflow marked as a template; return the workflow path."""
    library_json = materialize_library(
        tmp_path / "library",
        template=FIXTURE_LIBRARY_JSON_TEMPLATE,
        node_file=FIXTURE_NODE_FILE,
        extra_files=[FIXTURE_WORKFLOW_FILE],
    )
    workflow_file = library_json.parent / FIXTURE_WORKFLOW_FILE.name
    workflow_file.write_text(workflow_file.read_text().replace("# is_template = false", "# is_template = true"))

    register_result = engine.handle_request(RegisterLibraryFromFileRequest(file_path=str(library_json)))
    assert isinstance(register_result, RegisterLibraryFromFileResultSuccess), register_result
    return workflow_file


@pytest.mark.skipif(
    not FIXTURE_WORKFLOW_FILE.exists(),
    reason=f"Workflow Node Library fixture workflow missing at {FIXTURE_WORKFLOW_FILE}",
)
@pytest.mark.asyncio
async def test_a_backing_workflow_marked_as_a_template_creates_a_workflow(
    template_library: Path, engine: Engine
) -> None:
    """With `is_template = true` in its header, the library's workflow works as a template."""
    template_name = template_library.with_suffix("").as_posix()

    list_result = await engine.ahandle_request(ListAllWorkflowsRequest())
    assert isinstance(list_result, ListAllWorkflowsResultSuccess), list_result
    assert list_result.workflows[template_name]["is_template"] is True

    create_result = await engine.ahandle_request(CreateWorkflowFromTemplateRequest(template_name=template_name))
    assert isinstance(create_result, CreateWorkflowFromTemplateResultSuccess), create_result

    # The copy lands in the workspace as the user's own workflow.
    created = engine.workflow_registry.get_workflow_by_name(create_result.workflow_name)
    assert created.metadata.is_griptape_provided is False
    assert created.metadata.is_template is False


@pytest.mark.skipif(
    not FIXTURE_WORKFLOW_FILE.exists(),
    reason=f"Workflow Node Library fixture workflow missing at {FIXTURE_WORKFLOW_FILE}",
)
@pytest.mark.asyncio
async def test_a_backing_workflow_without_the_template_flag_is_copied_on_create(
    registered_library: Path, engine: Engine
) -> None:
    """Without `is_template = true`, the workflow is not a template, but Create still copies it."""
    workflow_file = registered_library.parent / FIXTURE_WORKFLOW_FILE.name
    template_name = workflow_file.with_suffix("").as_posix()
    original_source = workflow_file.read_text()

    list_result = await engine.ahandle_request(ListAllWorkflowsRequest())
    assert isinstance(list_result, ListAllWorkflowsResultSuccess), list_result
    assert list_result.workflows[template_name]["is_template"] is False

    create_result = await engine.ahandle_request(CreateWorkflowFromTemplateRequest(template_name=template_name))
    assert isinstance(create_result, CreateWorkflowFromTemplateResultSuccess), create_result

    # The copy lands in the workspace as the user's own workflow; the library's file is untouched.
    created = engine.workflow_registry.get_workflow_by_name(create_result.workflow_name)
    assert created.metadata.is_griptape_provided is False
    assert workflow_file.read_text() == original_source


@pytest.mark.skipif(
    not FIXTURE_WORKFLOW_FILE.exists(),
    reason=f"Workflow Node Library fixture workflow missing at {FIXTURE_WORKFLOW_FILE}",
)
@pytest.mark.asyncio
async def test_workflow_node_reuses_the_key_the_workspace_scan_registered(
    workspace_backed_library: Path,  # noqa: ARG001
    engine: Engine,
    create_node: Callable[..., str],
) -> None:
    """A workflow inside the workspace is listed once, however the node reached it."""
    await engine.workflow_manager.refresh_workflow_registry()

    engine.context_manager.push_workflow(workflow_name="workflow_node_e2e_registry")
    flow_result = engine.handle_request(
        CreateFlowRequest(parent_flow_name=None, flow_name="ParentFlow", set_as_new_context=False)
    )
    assert isinstance(flow_result, CreateFlowResultSuccess), flow_result
    create_node(WORKFLOW_NODE_TYPE, "Shout It", flow_result.flow_name, library_name=LIBRARY_NAME)

    list_result = await engine.ahandle_request(ListAllWorkflowsRequest())
    assert isinstance(list_result, ListAllWorkflowsResultSuccess), list_result
    assert [key for key in list_result.workflows if key.endswith("shout_workflow")] == ["nested/shout_workflow"]
    # The user's own file in the workspace, so it is not marked library-provided.
    assert list_result.workflows["nested/shout_workflow"]["is_griptape_provided"] is False

    callable_result = await engine.ahandle_request(ListCallableWorkflowsRequest())
    assert isinstance(callable_result, ListCallableWorkflowsResultSuccess), callable_result
    assert [name for name in callable_result.workflow_names if name.endswith("shout_workflow")] == [
        "nested/shout_workflow"
    ]

    node = engine.node_manager.get_node_by_name("Shout It")
    assert node.metadata["_workflow_file_value"] == "nested/shout_workflow"


@pytest.mark.skipif(
    not FIXTURE_WORKFLOW_FILE.exists(),
    reason=f"Workflow Node Library fixture workflow missing at {FIXTURE_WORKFLOW_FILE}",
)
def test_workflow_node_outside_the_workspace_keeps_its_full_path_as_the_key(
    registered_library: Path,
    engine: Engine,
    create_node: Callable[..., str],
) -> None:
    """A library shipping its own workflow keeps naming it by its full path; nothing else can."""
    engine.context_manager.push_workflow(workflow_name="workflow_node_e2e_external_registry")
    flow_result = engine.handle_request(
        CreateFlowRequest(parent_flow_name=None, flow_name="ParentFlow", set_as_new_context=False)
    )
    assert isinstance(flow_result, CreateFlowResultSuccess), flow_result
    create_node(WORKFLOW_NODE_TYPE, "Shout It", flow_result.flow_name, library_name=LIBRARY_NAME)

    node = engine.node_manager.get_node_by_name("Shout It")
    registry_key = (registered_library.parent / "shout_workflow").as_posix()
    assert node.metadata["_workflow_file_value"] == registry_key

    # Placing the node reuses the key the library load registered rather than listing it again.
    listed = [key for key in engine.workflow_registry.list_workflows() if key.endswith("shout_workflow")]
    assert listed == [registry_key]


@pytest.mark.skipif(
    not FIXTURE_WORKFLOW_FILE.exists(),
    reason=f"Workflow Node Library fixture workflow missing at {FIXTURE_WORKFLOW_FILE}",
)
@pytest.mark.asyncio
async def test_workflow_node_reached_through_a_link_keeps_the_link_as_its_key(
    symlinked_workflow_library: Path,  # noqa: ARG001
    engine: Engine,
    create_node: Callable[..., str],
) -> None:
    """A workflow linked into the workspace is listed once, named by where the link sits."""
    await engine.workflow_manager.refresh_workflow_registry()

    engine.context_manager.push_workflow(workflow_name="workflow_node_e2e_linked_registry")
    flow_result = engine.handle_request(
        CreateFlowRequest(parent_flow_name=None, flow_name="ParentFlow", set_as_new_context=False)
    )
    assert isinstance(flow_result, CreateFlowResultSuccess), flow_result
    create_node(WORKFLOW_NODE_TYPE, "Shout It", flow_result.flow_name, library_name=LIBRARY_NAME)

    list_result = await engine.ahandle_request(ListAllWorkflowsRequest())
    assert isinstance(list_result, ListAllWorkflowsResultSuccess), list_result
    assert [key for key in list_result.workflows if key.endswith("shout_workflow")] == ["linked/shout_workflow"]
    # A link inside the workspace is the user's own entry, so it is not marked library-provided.
    assert list_result.workflows["linked/shout_workflow"]["is_griptape_provided"] is False

    callable_result = await engine.ahandle_request(ListCallableWorkflowsRequest())
    assert isinstance(callable_result, ListCallableWorkflowsResultSuccess), callable_result
    assert [name for name in callable_result.workflow_names if name.endswith("shout_workflow")] == [
        "linked/shout_workflow"
    ]

    node = engine.node_manager.get_node_by_name("Shout It")
    assert node.metadata["_workflow_file_value"] == "linked/shout_workflow"


@pytest.mark.skipif(
    not FIXTURE_WORKFLOW_FILE.exists(),
    reason=f"Workflow Node Library fixture workflow missing at {FIXTURE_WORKFLOW_FILE}",
)
@pytest.mark.asyncio
async def test_workflow_node_reached_through_a_linked_parent_folder_reuses_the_scan_key(
    parent_linked_library: Path,  # noqa: ARG001
    engine: Engine,
    create_node: Callable[..., str],
) -> None:
    """A workflow reached through a link to a parent folder is listed once, under the scan's key."""
    await engine.workflow_manager.refresh_workflow_registry()

    engine.context_manager.push_workflow(workflow_name="workflow_node_e2e_parent_linked_registry")
    flow_result = engine.handle_request(
        CreateFlowRequest(parent_flow_name=None, flow_name="ParentFlow", set_as_new_context=False)
    )
    assert isinstance(flow_result, CreateFlowResultSuccess), flow_result
    create_node(WORKFLOW_NODE_TYPE, "Shout It", flow_result.flow_name, library_name=LIBRARY_NAME)

    list_result = await engine.ahandle_request(ListAllWorkflowsRequest())
    assert isinstance(list_result, ListAllWorkflowsResultSuccess), list_result
    assert [key for key in list_result.workflows if key.endswith("shout_workflow")] == ["nested/shout_workflow"]
    assert list_result.workflows["nested/shout_workflow"]["is_griptape_provided"] is False

    node = engine.node_manager.get_node_by_name("Shout It")
    assert node.metadata["_workflow_file_value"] == "nested/shout_workflow"
