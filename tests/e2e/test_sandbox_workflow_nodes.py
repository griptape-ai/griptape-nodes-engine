"""End-to-end coverage for saved workflows offered as nodes by the Sandbox Library.

Any saved workflow dropped into the sandbox folder is picked up whenever libraries load and offered
as a workflow-backed node type in the "Sandbox Library", under its Sandbox category, alongside the
sandbox's Python nodes. The workflow file is only read for its metadata header, never imported. The
generated node behaves exactly like a hand-declared ``workflow_nodes`` entry: its parameters mirror
the workflow's saved shape and running it runs the workflow.

The fixture workflow is ``tests/e2e/fixtures/workflow_node_library/shout_workflow.py`` (Start ->
Shout -> End); its own node types come from the fixture library next to it, which the tests
register separately.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from griptape_nodes.exe_types.node_types import ErrorProxyNode, NodeResolutionState
from griptape_nodes.exe_types.workflow_node import WORKFLOW_FILE_VALUE_KEY, WorkflowNode
from griptape_nodes.node_library.workflow_registry import read_workflow_metadata
from griptape_nodes.retained_mode.events.execution_events import StartFlowRequest, StartFlowResultSuccess
from griptape_nodes.retained_mode.events.flow_events import CreateFlowRequest, CreateFlowResultSuccess
from griptape_nodes.retained_mode.events.library_events import (
    GetAllInfoForLibraryRequest,
    GetAllInfoForLibraryResultSuccess,
    ListNodeTypesInLibraryRequest,
    ListNodeTypesInLibraryResultSuccess,
    RegisterLibraryFromFileRequest,
    RegisterLibraryFromFileResultSuccess,
    RegisterSandboxNodeFromSourceRequest,
    RegisterSandboxNodeFromSourceResultSuccess,
    ReloadAllLibrariesRequest,
    ReloadAllLibrariesResultSuccess,
    UnloadLibraryFromRegistryRequest,
    UnloadLibraryFromRegistryResultSuccess,
)
from griptape_nodes.retained_mode.events.parameter_events import SetParameterValueRequest
from griptape_nodes.retained_mode.events.workflow_events import (
    ListAllWorkflowsRequest,
    ListAllWorkflowsResultSuccess,
    RunWorkflowFromRegistryRequest,
    RunWorkflowFromRegistryResultSuccess,
    SaveWorkflowRequest,
    SaveWorkflowResultSuccess,
    WorkflowStatus,
)
from griptape_nodes.retained_mode.managers.fitness_problems.libraries import (
    DuplicateNodeRegistrationProblem,
    WorkflowNodeLoadProblem,
)
from griptape_nodes.retained_mode.managers.library_manager import LibraryManager
from griptape_nodes.retained_mode.managers.settings import LIBRARIES_TO_REGISTER_KEY, Settings

if TYPE_CHECKING:
    from collections.abc import Callable

    from griptape_nodes.retained_mode.engine import Engine

# Timeout with thread dump.
pytestmark = [
    pytest.mark.timeout(300, method="thread"),
    pytest.mark.skipif(
        not (Path(__file__).parent / "fixtures" / "workflow_node_library" / "shout_workflow.py").exists(),
        reason="Workflow Node Library fixture workflow missing",
    ),
]

FIXTURE_LIBRARY_DIR = Path(__file__).parent / "fixtures" / "workflow_node_library"
FIXTURE_LIBRARY_JSON_TEMPLATE = FIXTURE_LIBRARY_DIR / "griptape_nodes_library.json"
FIXTURE_NODE_FILE = FIXTURE_LIBRARY_DIR / "workflow_node_nodes.py"
FIXTURE_WORKFLOW_FILE = FIXTURE_LIBRARY_DIR / "shout_workflow.py"
SANDBOX_LIBRARY_NAME = LibraryManager.SANDBOX_LIBRARY_NAME
SANDBOX_DIRECTORY_NAME = Settings.model_fields["sandbox_library_directory"].default
WORKFLOW_NODE_TYPE = "ShoutWorkflow"
WORKFLOW_KEY = f"{SANDBOX_DIRECTORY_NAME}/shout_workflow"
PARENT_WORKFLOW_NAME = "uses_a_sandbox_workflow"
PYTHON_NODE_SOURCE = (
    "from griptape_nodes.exe_types.node_types import DataNode\n"
    "\n"
    "class {class_name}(DataNode):\n"
    "    def process(self) -> None:\n"
    "        return None\n"
)


async def _reload_libraries(engine: Engine) -> None:
    """Refresh Libraries, the way the editor's menu item does."""
    reload_result = await engine.ahandle_request(ReloadAllLibrariesRequest())
    assert isinstance(reload_result, ReloadAllLibrariesResultSuccess), reload_result


def _sandbox_library_info(engine: Engine) -> LibraryManager.LibraryInfo:
    library_info = engine.library_manager.get_library_info_by_library_name(SANDBOX_LIBRARY_NAME)
    assert library_info is not None
    return library_info


def _list_sandbox_node_types(engine: Engine) -> list[str]:
    list_result = engine.handle_request(ListNodeTypesInLibraryRequest(library=SANDBOX_LIBRARY_NAME))
    assert isinstance(list_result, ListNodeTypesInLibraryResultSuccess), list_result
    return list_result.node_types


def _create_parent_flow(engine: Engine, workflow_name: str) -> str:
    engine.context_manager.push_workflow(workflow_name=workflow_name)
    flow_result = engine.handle_request(
        CreateFlowRequest(parent_flow_name=None, flow_name="ControlFlow_1", set_as_new_context=False)
    )
    assert isinstance(flow_result, CreateFlowResultSuccess), flow_result
    return flow_result.flow_name


def _edit_workflow_header(workflow_file: Path, *, name: str, description: str) -> None:
    """Change a saved shout workflow's name and description, as saving it from the editor would."""
    edited = (
        workflow_file.read_text()
        .replace('# name = "shout_workflow"', f'# name = "{name}"')
        .replace(
            '# description = "Uppercases the incoming text and appends an exclamation mark."',
            f'# description = "{description}"',
        )
    )
    workflow_file.write_text(edited)


def _workflow_keys_ending_with(list_result: ListAllWorkflowsResultSuccess, file_stem: str) -> list[str]:
    return [key for key in list_result.workflows if key.endswith(file_stem)]


async def _run_shout_node(engine: Engine, parent_flow: str, node_name: str, text: str) -> None:
    """Set a workflow node's text and run it."""
    set_result = engine.handle_request(SetParameterValueRequest(parameter_name="text", node_name=node_name, value=text))
    assert set_result.succeeded(), set_result
    run_result = await engine.ahandle_request(
        StartFlowRequest(
            flow_name=parent_flow,
            flow_node_name=node_name,
            wait_for_completion=True,
            completion_timeout_ms=60000,
        )
    )
    assert isinstance(run_result, StartFlowResultSuccess), run_result


@pytest.fixture
def fixture_node_library(tmp_path: Path, engine: Engine, materialize_library: Callable[..., Path]) -> Path:
    """Register the fixture node library the shout workflow's nodes come from, surviving a full reload."""
    library_json = materialize_library(
        tmp_path / "library",
        template=FIXTURE_LIBRARY_JSON_TEMPLATE,
        node_file=FIXTURE_NODE_FILE,
    )
    engine.config_manager.set_config_value(LIBRARIES_TO_REGISTER_KEY, [str(library_json)])
    register_result = engine.handle_request(RegisterLibraryFromFileRequest(file_path=str(library_json)))
    assert isinstance(register_result, RegisterLibraryFromFileResultSuccess), register_result
    return library_json


@pytest.fixture
def sandbox_directory(engine: Engine, fixture_node_library: Path) -> Path:  # noqa: ARG001
    """Point the sandbox at an empty folder in the workspace, with the fixture node library registered."""
    sandbox_dir = engine.config_manager.workspace_path / SANDBOX_DIRECTORY_NAME
    sandbox_dir.mkdir(parents=True, exist_ok=True)
    engine.config_manager.set_config_value("sandbox_library_directory", str(sandbox_dir))
    return sandbox_dir


@pytest.fixture
def sandbox_workflow(sandbox_directory: Path) -> Path:
    """Drop the shout workflow into the sandbox folder."""
    workflow_file = sandbox_directory / FIXTURE_WORKFLOW_FILE.name
    workflow_file.write_text(FIXTURE_WORKFLOW_FILE.read_text())
    return workflow_file


@pytest.mark.asyncio
async def test_workflow_in_sandbox_becomes_a_sandbox_node_type(
    sandbox_workflow: Path,  # noqa: ARG001
    engine: Engine,
) -> None:
    """Loading libraries offers the sandbox workflow as a node type under the Sandbox category."""
    await _reload_libraries(engine)

    assert WORKFLOW_NODE_TYPE in _list_sandbox_node_types(engine)

    info_result = engine.handle_request(GetAllInfoForLibraryRequest(library=SANDBOX_LIBRARY_NAME))
    assert isinstance(info_result, GetAllInfoForLibraryResultSuccess), info_result
    category_keys = [key for category in info_result.category_details.categories for key in category]
    assert category_keys == [LibraryManager.SANDBOX_CATEGORY_NAME]
    node_metadata = info_result.node_type_name_to_node_metadata_details[WORKFLOW_NODE_TYPE].metadata
    assert node_metadata.category == LibraryManager.SANDBOX_CATEGORY_NAME
    assert node_metadata.icon == LibraryManager.SUBFLOW_NODE_ICON
    assert node_metadata.display_name == "shout_workflow"
    assert node_metadata.description == "Uppercases the incoming text and appends an exclamation mark."


@pytest.mark.asyncio
async def test_sandbox_workflow_file_is_not_imported(
    sandbox_workflow: Path,
    engine: Engine,
) -> None:
    """Only the workflow's header is read; its code never runs as node source."""
    await _reload_libraries(engine)

    assert WORKFLOW_NODE_TYPE in _list_sandbox_node_types(engine)
    stable_namespace = f"{LibraryManager.STABLE_NAMESPACE_PREFIX}sandbox_library.{sandbox_workflow.stem}"
    assert stable_namespace not in sys.modules
    imported_from_workflow = [
        module_name
        for module_name, module in list(sys.modules.items())
        if getattr(module, "__file__", None) == str(sandbox_workflow)
    ]
    assert imported_from_workflow == []


@pytest.mark.asyncio
async def test_sandbox_workflow_node_parameters_mirror_the_workflow_shape(
    sandbox_workflow: Path,  # noqa: ARG001
    engine: Engine,
    create_node: Callable[..., str],
) -> None:
    """The generated node's parameters come from the workflow's Start Flow and End Flow nodes."""
    await _reload_libraries(engine)

    parent_flow = _create_parent_flow(engine, "sandbox_workflow_e2e_shape")
    create_node(WORKFLOW_NODE_TYPE, "Shout It", parent_flow, library_name=SANDBOX_LIBRARY_NAME)
    node = engine.node_manager.get_node_by_name("Shout It")

    assert isinstance(node, WorkflowNode), f"Expected a workflow-backed node, got {type(node).__name__}"
    # `text` comes from the workflow's Start Flow node, `result` from its End Flow node.
    assert [parameter.name for parameter in node.parameters] == ["exec_in", "exec_out", "text", "result"]


@pytest.mark.asyncio
async def test_sandbox_workflow_node_runs_its_workflow_and_returns_outputs(
    sandbox_workflow: Path,  # noqa: ARG001
    engine: Engine,
    create_node: Callable[..., str],
) -> None:
    """Running the generated node executes the workflow and surfaces its End Flow values."""
    await _reload_libraries(engine)

    parent_flow = _create_parent_flow(engine, "sandbox_workflow_e2e_run")
    create_node(WORKFLOW_NODE_TYPE, "Shout It", parent_flow, library_name=SANDBOX_LIBRARY_NAME)
    set_result = engine.handle_request(
        SetParameterValueRequest(parameter_name="text", node_name="Shout It", value="hello there")
    )
    assert set_result.succeeded(), set_result

    run_result = await engine.ahandle_request(
        StartFlowRequest(
            flow_name=parent_flow,
            flow_node_name="Shout It",
            wait_for_completion=True,
            completion_timeout_ms=60000,
        )
    )
    assert isinstance(run_result, StartFlowResultSuccess), run_result

    node = engine.node_manager.get_node_by_name("Shout It")
    assert node.state == NodeResolutionState.RESOLVED
    assert node.parameter_output_values.get("result") == "HELLO THERE!"


@pytest.mark.asyncio
async def test_python_nodes_load_alongside_sandbox_workflow_nodes(
    sandbox_workflow: Path,  # noqa: ARG001
    sandbox_directory: Path,
    engine: Engine,
) -> None:
    """The sandbox's Python nodes still load normally next to its workflow nodes."""
    (sandbox_directory / "probe_node.py").write_text(PYTHON_NODE_SOURCE.format(class_name="ProbeNode"))

    await _reload_libraries(engine)

    assert sorted(_list_sandbox_node_types(engine)) == ["ProbeNode", WORKFLOW_NODE_TYPE]
    info_result = engine.handle_request(GetAllInfoForLibraryRequest(library=SANDBOX_LIBRARY_NAME))
    assert isinstance(info_result, GetAllInfoForLibraryResultSuccess), info_result
    probe_metadata = info_result.node_type_name_to_node_metadata_details["ProbeNode"].metadata
    assert probe_metadata.category == LibraryManager.SANDBOX_CATEGORY_NAME
    assert _sandbox_library_info(engine).problems == []


@pytest.mark.asyncio
async def test_sandbox_with_only_workflows_registers_them(
    sandbox_workflow: Path,  # noqa: ARG001
    engine: Engine,
) -> None:
    """A sandbox holding no Python nodes at all still offers its workflows."""
    await _reload_libraries(engine)

    assert _list_sandbox_node_types(engine) == [WORKFLOW_NODE_TYPE]
    library_info = _sandbox_library_info(engine)
    assert library_info.lifecycle_state == LibraryManager.LibraryLifecycleState.LOADED
    assert library_info.fitness == LibraryManager.LibraryFitness.GOOD


@pytest.mark.asyncio
async def test_editing_the_workflow_name_updates_the_node_on_refresh(
    sandbox_workflow: Path,
    engine: Engine,
) -> None:
    """The node's label and description follow the workflow's header, not the first scan of it."""
    await _reload_libraries(engine)
    assert _list_sandbox_node_types(engine) == [WORKFLOW_NODE_TYPE]

    _edit_workflow_header(sandbox_workflow, name="Loud Shout", description="Shouts, now louder.")
    await _reload_libraries(engine)

    assert _list_sandbox_node_types(engine) == ["LoudShout"]
    info_result = engine.handle_request(GetAllInfoForLibraryRequest(library=SANDBOX_LIBRARY_NAME))
    assert isinstance(info_result, GetAllInfoForLibraryResultSuccess), info_result
    node_metadata = info_result.node_type_name_to_node_metadata_details["LoudShout"].metadata
    assert node_metadata.display_name == "Loud Shout"
    assert node_metadata.description == "Shouts, now louder."


@pytest.mark.asyncio
async def test_saved_workflow_using_a_sandbox_workflow_node_reopens_with_the_real_node(
    sandbox_workflow: Path,  # noqa: ARG001
    engine: Engine,
    create_node: Callable[..., str],
) -> None:
    """A workflow saved with a sandbox workflow node in it gets that node back after Refresh Libraries."""
    await _reload_libraries(engine)

    parent_flow = _create_parent_flow(engine, PARENT_WORKFLOW_NAME)
    create_node(WORKFLOW_NODE_TYPE, "Shout It", parent_flow, library_name=SANDBOX_LIBRARY_NAME)
    set_result = engine.handle_request(
        SetParameterValueRequest(parameter_name="text", node_name="Shout It", value="kept across the save")
    )
    assert set_result.succeeded(), set_result

    save_result = engine.handle_request(SaveWorkflowRequest(file_name=PARENT_WORKFLOW_NAME))
    assert isinstance(save_result, SaveWorkflowResultSuccess), save_result
    saved_metadata = read_workflow_metadata(Path(save_result.file_path))
    assert SANDBOX_LIBRARY_NAME in [library.library_name for library in saved_metadata.node_libraries_referenced]

    await _reload_libraries(engine)
    await engine.workflow_manager.refresh_workflow_registry()

    run_result = await engine.ahandle_request(RunWorkflowFromRegistryRequest(workflow_name=save_result.workflow_name))
    assert isinstance(run_result, RunWorkflowFromRegistryResultSuccess), run_result
    assert run_result.status == WorkflowStatus.GOOD

    node = engine.node_manager.get_node_by_name("Shout It")
    assert not isinstance(node, ErrorProxyNode), node
    assert isinstance(node, WorkflowNode), f"Expected a workflow-backed node, got {type(node).__name__}"
    assert node.metadata["library"] == SANDBOX_LIBRARY_NAME
    assert node.get_parameter_value("text") == "kept across the save"


@pytest.mark.asyncio
async def test_saved_workflow_using_a_sandbox_workflow_node_reloads_the_sandbox_when_it_is_unloaded(
    sandbox_workflow: Path,  # noqa: ARG001
    engine: Engine,
    create_node: Callable[..., str],
) -> None:
    """Opening a workflow with the Sandbox Library unloaded finds it by name, not a placeholder (the headless path)."""
    await _reload_libraries(engine)

    parent_flow = _create_parent_flow(engine, PARENT_WORKFLOW_NAME)
    create_node(WORKFLOW_NODE_TYPE, "Shout It", parent_flow, library_name=SANDBOX_LIBRARY_NAME)
    save_result = engine.handle_request(SaveWorkflowRequest(file_name=PARENT_WORKFLOW_NAME))
    assert isinstance(save_result, SaveWorkflowResultSuccess), save_result

    unload_result = engine.handle_request(UnloadLibraryFromRegistryRequest(library_name=SANDBOX_LIBRARY_NAME))
    assert isinstance(unload_result, UnloadLibraryFromRegistryResultSuccess), unload_result
    assert engine.library_manager.get_library_info_by_library_name(SANDBOX_LIBRARY_NAME) is None

    run_result = await engine.ahandle_request(RunWorkflowFromRegistryRequest(workflow_name=save_result.workflow_name))
    assert isinstance(run_result, RunWorkflowFromRegistryResultSuccess), run_result
    assert run_result.status == WorkflowStatus.GOOD

    node = engine.node_manager.get_node_by_name("Shout It")
    assert not isinstance(node, ErrorProxyNode), node
    assert isinstance(node, WorkflowNode), f"Expected a workflow-backed node, got {type(node).__name__}"
    assert node.metadata["library"] == SANDBOX_LIBRARY_NAME


@pytest.mark.asyncio
async def test_workflow_node_clashing_with_a_python_node_records_a_duplicate(
    sandbox_workflow: Path,  # noqa: ARG001
    sandbox_directory: Path,
    engine: Engine,
) -> None:
    """A Python node and a workflow node claiming one name are reported on the Sandbox Library."""
    (sandbox_directory / "shout_node.py").write_text(PYTHON_NODE_SOURCE.format(class_name=WORKFLOW_NODE_TYPE))

    await _reload_libraries(engine)

    assert _sandbox_library_info(engine).problems == [
        DuplicateNodeRegistrationProblem(class_name=WORKFLOW_NODE_TYPE, library_name=SANDBOX_LIBRARY_NAME)
    ]


@pytest.mark.asyncio
async def test_unreadable_workflow_header_records_a_problem_and_is_not_imported(
    sandbox_directory: Path,
    engine: Engine,
) -> None:
    """A file that is clearly a workflow but whose header cannot be read is reported, never imported."""
    broken = sandbox_directory / "broken.py"
    broken.write_text(
        "# /// script\n# [tool.griptape-nodes]\n# name = \n# ///\n"
        "raise RuntimeError('A saved workflow was imported as node source.')\n",
        encoding="utf-8",
    )

    await _reload_libraries(engine)

    problems = _sandbox_library_info(engine).problems
    assert len(problems) == 1
    problem = problems[0]
    assert isinstance(problem, WorkflowNodeLoadProblem)
    assert problem.node_type == "broken"
    assert problem.workflow_path == str(broken)
    assert [
        module_name
        for module_name, module in list(sys.modules.items())
        if getattr(module, "__file__", None) == str(broken)
    ] == []


@pytest.mark.asyncio
async def test_workflow_without_start_and_end_nodes_records_a_problem(
    sandbox_directory: Path,
    engine: Engine,
) -> None:
    """A saved workflow with no Start Flow and End Flow shape cannot become a node, and says so."""
    shapeless = "\n".join(
        line for line in FIXTURE_WORKFLOW_FILE.read_text().splitlines() if not line.startswith("# workflow_shape =")
    )
    (sandbox_directory / "shout_workflow.py").write_text(shapeless)

    await _reload_libraries(engine)

    assert WORKFLOW_NODE_TYPE not in _list_sandbox_node_types(engine)
    problems = _sandbox_library_info(engine).problems
    assert len(problems) == 1
    problem = problems[0]
    assert isinstance(problem, WorkflowNodeLoadProblem)
    assert problem.node_type == WORKFLOW_NODE_TYPE


@pytest.mark.asyncio
async def test_sandbox_workflow_is_listed_once_among_the_workspace_workflows(
    sandbox_workflow: Path,  # noqa: ARG001
    engine: Engine,
    create_node: Callable[..., str],
) -> None:
    """A workflow in the sandbox is still an ordinary workflow, listed once even after a node is placed."""
    await _reload_libraries(engine)
    await engine.workflow_manager.refresh_workflow_registry()

    list_result = await engine.ahandle_request(ListAllWorkflowsRequest())
    assert isinstance(list_result, ListAllWorkflowsResultSuccess), list_result
    assert WORKFLOW_KEY in list_result.workflows

    parent_flow = _create_parent_flow(engine, "sandbox_workflow_e2e_listing")
    create_node(WORKFLOW_NODE_TYPE, "Shout It", parent_flow, library_name=SANDBOX_LIBRARY_NAME)
    node = engine.node_manager.get_node_by_name("Shout It")
    assert isinstance(node, WorkflowNode), f"Expected a workflow-backed node, got {type(node).__name__}"

    list_result = await engine.ahandle_request(ListAllWorkflowsRequest())
    assert isinstance(list_result, ListAllWorkflowsResultSuccess), list_result
    assert _workflow_keys_ending_with(list_result, "shout_workflow") == [WORKFLOW_KEY]


@pytest.mark.asyncio
async def test_saved_workflow_registered_live_becomes_a_node_that_runs(
    sandbox_directory: Path,
    engine: Engine,
    create_node: Callable[..., str],
) -> None:
    """An agent can drop a saved workflow into a running sandbox and use it as a node straight away."""
    await _reload_libraries(engine)
    assert WORKFLOW_NODE_TYPE not in _list_sandbox_node_types(engine)
    workflow_file = sandbox_directory / FIXTURE_WORKFLOW_FILE.name
    workflow_file.write_text(FIXTURE_WORKFLOW_FILE.read_text())

    register_result = engine.handle_request(RegisterSandboxNodeFromSourceRequest(file_path=workflow_file.name))

    assert isinstance(register_result, RegisterSandboxNodeFromSourceResultSuccess), register_result
    assert register_result.registered_class_names == [WORKFLOW_NODE_TYPE]
    parent_flow = _create_parent_flow(engine, "sandbox_workflow_e2e_live_registration")
    create_node(WORKFLOW_NODE_TYPE, "Shout It", parent_flow, library_name=SANDBOX_LIBRARY_NAME)
    set_result = engine.handle_request(
        SetParameterValueRequest(parameter_name="text", node_name="Shout It", value="registered live")
    )
    assert set_result.succeeded(), set_result
    run_result = await engine.ahandle_request(
        StartFlowRequest(
            flow_name=parent_flow,
            flow_node_name="Shout It",
            wait_for_completion=True,
            completion_timeout_ms=60000,
        )
    )
    assert isinstance(run_result, StartFlowResultSuccess), run_result
    node = engine.node_manager.get_node_by_name("Shout It")
    assert node.parameter_output_values.get("result") == "REGISTERED LIVE!"
    assert [
        module_name
        for module_name, module in list(sys.modules.items())
        if getattr(module, "__file__", None) == str(workflow_file)
    ] == []

    await engine.workflow_manager.refresh_workflow_registry()
    list_result = await engine.ahandle_request(ListAllWorkflowsRequest())
    assert isinstance(list_result, ListAllWorkflowsResultSuccess), list_result
    assert _workflow_keys_ending_with(list_result, "shout_workflow") == [WORKFLOW_KEY]


@pytest.mark.skipif(sys.platform.startswith("win"), reason="symlink creation needs privileges on Windows")
@pytest.mark.asyncio
async def test_workflow_in_a_linked_sandbox_folder_is_listed_once_under_its_workspace_key(
    tmp_path: Path,
    sandbox_directory: Path,
    engine: Engine,
    create_node: Callable[..., str],
) -> None:
    """A workflow reached through a linked folder in the sandbox becomes a node, keyed by the link."""
    shared_folder = tmp_path / "shared_workflows"
    shared_folder.mkdir()
    (shared_folder / "subflow.py").write_text(FIXTURE_WORKFLOW_FILE.read_text())
    (sandbox_directory / "link_name").symlink_to(shared_folder, target_is_directory=True)
    workflow_key = f"{SANDBOX_DIRECTORY_NAME}/link_name/subflow"

    await _reload_libraries(engine)
    await engine.workflow_manager.refresh_workflow_registry()

    assert WORKFLOW_NODE_TYPE in _list_sandbox_node_types(engine)
    parent_flow = _create_parent_flow(engine, "sandbox_workflow_e2e_linked_folder")
    create_node(WORKFLOW_NODE_TYPE, "Shout It", parent_flow, library_name=SANDBOX_LIBRARY_NAME)
    assert isinstance(engine.node_manager.get_node_by_name("Shout It"), WorkflowNode)

    list_result = await engine.ahandle_request(ListAllWorkflowsRequest())
    assert isinstance(list_result, ListAllWorkflowsResultSuccess), list_result
    assert _workflow_keys_ending_with(list_result, "subflow") == [workflow_key]


@pytest.mark.skipif(sys.platform.startswith("win"), reason="symlink creation needs privileges on Windows")
@pytest.mark.asyncio
async def test_workflow_in_a_linked_sandbox_folder_registered_live_becomes_a_node_that_runs(
    tmp_path: Path,
    sandbox_directory: Path,
    engine: Engine,
    create_node: Callable[..., str],
) -> None:
    """An agent can register a workflow in a linked sandbox folder, keyed by the link, matching the later scan."""
    shared_folder = tmp_path / "shared_workflows"
    shared_folder.mkdir()
    (sandbox_directory / "link_name").symlink_to(shared_folder, target_is_directory=True)
    workflow_key = f"{SANDBOX_DIRECTORY_NAME}/link_name/subflow"
    await _reload_libraries(engine)
    workflow_file = sandbox_directory / "link_name" / "subflow.py"
    workflow_file.write_text(FIXTURE_WORKFLOW_FILE.read_text())

    register_result = engine.handle_request(
        RegisterSandboxNodeFromSourceRequest(file_path=str(Path("link_name") / "subflow.py"))
    )

    assert isinstance(register_result, RegisterSandboxNodeFromSourceResultSuccess), register_result
    assert register_result.registered_class_names == [WORKFLOW_NODE_TYPE]
    parent_flow = _create_parent_flow(engine, "sandbox_workflow_e2e_linked_folder_live_registration")
    create_node(WORKFLOW_NODE_TYPE, "Shout It", parent_flow, library_name=SANDBOX_LIBRARY_NAME)
    node = engine.node_manager.get_node_by_name("Shout It")
    assert node.metadata[WORKFLOW_FILE_VALUE_KEY] == workflow_key
    list_result = await engine.ahandle_request(ListAllWorkflowsRequest())
    assert isinstance(list_result, ListAllWorkflowsResultSuccess), list_result
    assert _workflow_keys_ending_with(list_result, "subflow") == [workflow_key]

    await _run_shout_node(engine, parent_flow, "Shout It", "through the link")
    assert node.parameter_output_values.get("result") == "THROUGH THE LINK!"
    # Neither through the link nor from the folder it points to.
    workflow_file_spellings = {str(workflow_file), str(shared_folder / "subflow.py")}
    assert [
        module_name
        for module_name, module in list(sys.modules.items())
        if getattr(module, "__file__", None) in workflow_file_spellings
    ] == []

    await engine.workflow_manager.refresh_workflow_registry()
    # A node records its workflow's key only when it is created, so a new one shows the key after the scan.
    create_node(WORKFLOW_NODE_TYPE, "Shout It Again", parent_flow, library_name=SANDBOX_LIBRARY_NAME)
    node_after_scan = engine.node_manager.get_node_by_name("Shout It Again")
    assert node_after_scan.metadata[WORKFLOW_FILE_VALUE_KEY] == workflow_key
    await _run_shout_node(engine, parent_flow, "Shout It Again", "after the scan")

    assert node_after_scan.parameter_output_values.get("result") == "AFTER THE SCAN!"
    list_result = await engine.ahandle_request(ListAllWorkflowsRequest())
    assert isinstance(list_result, ListAllWorkflowsResultSuccess), list_result
    # This is what proves the fresh node derives the scan's key: a different key would add a second
    # registry entry.
    assert _workflow_keys_ending_with(list_result, "subflow") == [workflow_key]


@pytest.mark.skipif(sys.platform.startswith("win"), reason="symlink creation needs privileges on Windows")
@pytest.mark.asyncio
@pytest.mark.usefixtures("fixture_node_library")
async def test_workflow_in_a_sandbox_that_links_outside_the_workspace_is_listed_once(
    tmp_path: Path,
    engine: Engine,
    create_node: Callable[..., str],
) -> None:
    """A sandbox folder that is itself a link out of the workspace still names its workflows one way."""
    outside_sandbox = tmp_path / "outside_sandbox"
    outside_sandbox.mkdir()
    (outside_sandbox / FIXTURE_WORKFLOW_FILE.name).write_text(FIXTURE_WORKFLOW_FILE.read_text())
    workspace_path = engine.config_manager.workspace_path
    workspace_path.mkdir(parents=True, exist_ok=True)
    sandbox_link = workspace_path / SANDBOX_DIRECTORY_NAME
    sandbox_link.symlink_to(outside_sandbox, target_is_directory=True)
    engine.config_manager.set_config_value("sandbox_library_directory", str(sandbox_link))

    await _reload_libraries(engine)
    await engine.workflow_manager.refresh_workflow_registry()

    parent_flow = _create_parent_flow(engine, "sandbox_workflow_e2e_linked_sandbox")
    create_node(WORKFLOW_NODE_TYPE, "Shout It", parent_flow, library_name=SANDBOX_LIBRARY_NAME)
    assert isinstance(engine.node_manager.get_node_by_name("Shout It"), WorkflowNode)

    list_result = await engine.ahandle_request(ListAllWorkflowsRequest())
    assert isinstance(list_result, ListAllWorkflowsResultSuccess), list_result
    assert _workflow_keys_ending_with(list_result, "shout_workflow") == [WORKFLOW_KEY]
