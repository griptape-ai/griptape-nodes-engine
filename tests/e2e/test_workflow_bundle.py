"""End-to-end coverage for the self-contained bundle `WorkflowPackager` produces.

``package_to_folder`` is what every publisher composes with: it copies the workflow file and
its libraries into a destination folder and writes the ``griptape_nodes_config.json``,
``project.yml``, ``.env`` and ``pyproject.toml`` that make the folder runnable on its own.
These tests package a real saved workflow through it and then run the bundle the way a
publisher's entrypoint would.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

import pytest
import yaml

from griptape_nodes.node_library.workflow_registry import WorkflowRegistry
from griptape_nodes.retained_mode.events.connection_events import CreateConnectionRequest
from griptape_nodes.retained_mode.events.flow_events import CreateFlowRequest, CreateFlowResultSuccess
from griptape_nodes.retained_mode.events.library_events import (
    RegisterLibraryFromFileRequest,
    RegisterLibraryFromFileResultSuccess,
)
from griptape_nodes.retained_mode.events.node_events import CreateNodeRequest, CreateNodeResultSuccess
from griptape_nodes.retained_mode.events.workflow_events import SaveWorkflowRequest, SaveWorkflowResultSuccess
from griptape_nodes.retained_mode.publishing.workflow_packager import WorkflowPackager

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
WORKFLOW_NAME = "bundle_e2e_workflow"


class PackagedBundle(NamedTuple):
    """What a packaging run left on disk, plus the values needed to run it."""

    directory: Path
    workflow_file_name: str
    library_paths: list[str]


@pytest.fixture
def published_bundle(package_bundle: Callable[..., PackagedBundle]) -> PackagedBundle:
    """Package the Start -> Shout -> End workflow into a bundle folder."""
    return package_bundle(WORKFLOW_NAME, _build_shout_flow)


@pytest.fixture
def package_bundle(
    tmp_path: Path, engine: Engine, materialize_library: Callable[..., Path]
) -> Callable[..., PackagedBundle]:
    """Return a factory that registers the fixture library, saves a workflow, and packages it.

    ``build_flow`` receives the engine and the name of a freshly created top-level flow and is
    responsible for populating it; everything around it (library registration, save, packaging)
    is the same for any graph.
    """

    def _package(workflow_name: str, build_flow: Callable[[Engine, str], None]) -> PackagedBundle:
        library_json = materialize_library(
            tmp_path / "library",
            template=FIXTURE_LIBRARY_JSON_TEMPLATE,
            node_file=FIXTURE_NODE_FILE,
            extra_files=[FIXTURE_WORKFLOW_FILE],
        )
        register_result = engine.handle_request(RegisterLibraryFromFileRequest(file_path=str(library_json)))
        assert isinstance(register_result, RegisterLibraryFromFileResultSuccess), register_result

        engine.context_manager.push_workflow(workflow_name=workflow_name)
        flow_result = engine.handle_request(
            CreateFlowRequest(parent_flow_name=None, flow_name="ControlFlow_1", set_as_new_context=False)
        )
        assert isinstance(flow_result, CreateFlowResultSuccess), flow_result
        build_flow(engine, flow_result.flow_name)

        # package_to_folder reads the workflow off the registry and refuses an unsaved one, so
        # the save is what gives the packager a file_path to copy.
        save_result = engine.handle_request(SaveWorkflowRequest(file_name=workflow_name))
        assert isinstance(save_result, SaveWorkflowResultSuccess), save_result

        workflow = WorkflowRegistry.get_workflow_by_name(save_result.workflow_name)
        bundle_dir = tmp_path / "bundle"
        library_paths = WorkflowPackager(save_result.workflow_name).package_to_folder(bundle_dir, workflow)
        return PackagedBundle(
            directory=bundle_dir,
            workflow_file_name=Path(save_result.file_path).name,
            library_paths=library_paths,
        )

    return _package


def _build_shout_flow(engine: Engine, flow_name: str) -> None:
    """Wire Start -> Shout -> End into `flow_name`.

    The Start/End pair is what gives the saved workflow a shape, and only a shape-bearing
    workflow gets the ``__main__`` block that makes the packaged file runnable.
    """

    def _create(node_type: str, node_name: str) -> str:
        result = engine.handle_request(
            CreateNodeRequest(
                node_type=node_type,
                specific_library_name=LIBRARY_NAME,
                node_name=node_name,
                override_parent_flow_name=flow_name,
            )
        )
        assert isinstance(result, CreateNodeResultSuccess), result
        return result.node_name

    def _connect(source_node: str, source_param: str, target_node: str, target_param: str) -> None:
        result = engine.handle_request(
            CreateConnectionRequest(
                source_node_name=source_node,
                source_parameter_name=source_param,
                target_node_name=target_node,
                target_parameter_name=target_param,
            )
        )
        assert result.succeeded(), result

    start_node = _create("TextStartNode", "Start Flow")
    shout_node = _create("ShoutNode", "Shout")
    end_node = _create("TextEndNode", "End Flow")

    _connect(start_node, "exec_out", shout_node, "exec_in")
    _connect(shout_node, "exec_out", end_node, "exec_in")
    _connect(start_node, "text", shout_node, "text")
    _connect(shout_node, "shouted", end_node, "result")


def test_package_to_folder_writes_a_complete_bundle(published_bundle: PackagedBundle) -> None:
    """Every file a standalone bundle needs is present and internally consistent."""
    bundle = published_bundle.directory

    assert (bundle / published_bundle.workflow_file_name).is_file()
    assert (bundle / "libraries").is_dir()
    config_path = bundle / "griptape_nodes_config.json"
    assert config_path.is_file()
    project_path = bundle / "project.yml"
    assert project_path.is_file()
    env_path = bundle / ".env"
    assert env_path.is_file()
    pyproject_path = bundle / "pyproject.toml"
    assert pyproject_path.is_file()

    config = json.loads(config_path.read_text())
    assert config["workspace_directory"] == "."
    assert config["enable_workspace_file_watching"] is False
    app_init = config["app_events"]["on_app_initialization_complete"]
    assert app_init["workflows_to_register"] == []
    assert app_init["libraries_to_register"] == published_bundle.library_paths

    # Bundle-relative, so the folder stays runnable after being copied to another machine.
    for library_path in app_init["libraries_to_register"]:
        assert not Path(library_path).is_absolute(), library_path
        assert library_path == Path(library_path).as_posix(), library_path
        assert (bundle / library_path).is_file(), library_path

    project = yaml.safe_load(project_path.read_text())
    assert project, "project.yml must not be empty"

    env_lines = env_path.read_text().splitlines()
    assert "GTN_CONFIG_WORKSPACE_DIRECTORY='.'" in env_lines
    assert "GTN_ENABLE_WORKSPACE_FILE_WATCHING='false'" in env_lines

    pyproject = tomllib.loads(pyproject_path.read_text())
    dependencies = pyproject["project"]["dependencies"]
    assert any(dependency.startswith("griptape-nodes-engine") for dependency in dependencies), dependencies


def test_packaged_bundle_registers_libraries_on_a_clean_machine(
    tmp_path: Path,
    published_bundle: PackagedBundle,
    engine_subprocess_env: Callable[..., dict[str, str]],
) -> None:
    """Running a bundle where nothing else is registered must still load its own libraries.

    The engine writes no entrypoint of its own, so this replicates what the standard library's
    ``LocalPublisher._write_entrypoint`` puts around a bundle: point
    ``GTN_CONFIG_WORKSPACE_DIRECTORY`` at the bundle folder, pass ``--project-file-path``, and
    run the workflow file from inside the folder.

    Deliberately run under ``sys.executable`` with no ``uv sync`` and no venv: the bundle's
    ``pyproject.toml`` pins ``griptape-nodes-engine`` to a GitHub commit sha, so syncing would
    exercise the published engine instead of this working tree.
    """
    # An empty XDG config home is the whole point: with the library registered at the user
    # level the bundle's own config layer would never be needed and the test proves nothing.
    clean_config_home = tmp_path / "clean_xdg_config"
    clean_config_home.mkdir()
    env = engine_subprocess_env(
        XDG_CONFIG_HOME=str(clean_config_home),
        GTN_CONFIG_WORKSPACE_DIRECTORY=str(published_bundle.directory),
    )

    result = subprocess.run(  # noqa: S603 - subprocess input is constructed inside the test
        [
            sys.executable,
            str(published_bundle.directory / published_bundle.workflow_file_name),
            "--project-file-path",
            str(published_bundle.directory / "project.yml"),
        ],
        cwd=published_bundle.directory,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )

    diagnostic = (
        f"bundle exit code: {result.returncode}\n--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    assert result.returncode == 0, diagnostic
    assert "not found (discovery was attempted)" not in result.stderr, diagnostic
    assert "Error Proxy" not in result.stderr, diagnostic
