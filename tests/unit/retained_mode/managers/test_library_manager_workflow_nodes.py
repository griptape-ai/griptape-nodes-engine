"""Tests for registering `workflow_nodes` entries as node types (library manager side)."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import pytest

from griptape_nodes.exe_types.workflow_node import WorkflowNode
from griptape_nodes.files.path_utils import derive_registry_key
from griptape_nodes.node_library.library_registry import (
    Library,
    LibraryMetadata,
    LibrarySchema,
    NodeMetadata,
    WorkflowNodeDefinition,
)
from griptape_nodes.node_library.workflow_registry import WorkflowMetadata
from griptape_nodes.retained_mode.managers.fitness_problems.libraries import WorkflowNodeLoadProblem
from griptape_nodes.retained_mode.managers.library_manager import LibraryManager

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from griptape_nodes.retained_mode.engine import Engine

NODE_TYPE = "ShoutWorkflow"

_SHAPE = {
    "inputs": {"Start Flow": {"text": {"name": "text", "type": "str", "default_value": ""}}},
    "outputs": {"End Flow": {"result": {"name": "result", "type": "str", "default_value": ""}}},
}


def _library_info(library_path: str = "/fake/path", *, is_sandbox: bool = False) -> LibraryManager.LibraryInfo:
    return LibraryManager.LibraryInfo(
        lifecycle_state=LibraryManager.LibraryLifecycleState.EVALUATED,
        fitness=LibraryManager.LibraryFitness.NOT_EVALUATED,
        library_path=library_path,
        is_sandbox=is_sandbox,
        library_name="TestLib",
        library_version="1.0.0",
    )


def _library(workflow_nodes: list[WorkflowNodeDefinition]) -> Library:
    schema = LibrarySchema(
        name="TestLib",
        library_schema_version=LibrarySchema.LATEST_SCHEMA_VERSION,
        metadata=LibraryMetadata(
            author="Test",
            description="Test",
            library_version="1.0.0",
            engine_version="0.0.0",
            tags=[],
        ),
        categories=[],
        nodes=[],
        workflow_nodes=workflow_nodes,
    )
    return Library(library_data=schema)


def _definition(workflow_path: str) -> WorkflowNodeDefinition:
    return WorkflowNodeDefinition(
        node_type=NODE_TYPE,
        workflow_path=workflow_path,
        metadata=NodeMetadata(category="test", description="Runs a workflow", display_name="Shout Workflow"),
    )


def _write_workflow(tmp_path: Path, *, shape: dict[str, Any] | None, is_template: bool = False) -> Path:
    lines = [
        "# /// script",
        "# [tool.griptape-nodes]",
        '# name = "demo"',
        f'# schema_version = "{WorkflowMetadata.LATEST_SCHEMA_VERSION}"',
        '# engine_version_created_with = "0.0.0"',
        "# node_libraries_referenced = []",
        "# is_griptape_provided = false",
        f"# is_template = {str(is_template).lower()}",
    ]
    if shape is not None:
        encoded = json.dumps(json.dumps(shape, separators=(",", ":")))
        lines.append(f"# workflow_shape = {encoded}")
    lines.append("# ///")
    workflow_path = tmp_path / "demo_workflow.py"
    workflow_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return workflow_path


class TestRegisterWorkflowNode:
    def test_registers_generated_node_type(self, engine: Engine, tmp_path: Path) -> None:
        workflow_path = _write_workflow(tmp_path, shape=_SHAPE)
        library = _library([_definition(workflow_path.name)])
        library_info = _library_info()

        registered = engine.library_manager.module_loading._register_workflow_node(
            library.get_library_data().workflow_nodes[0],  # type: ignore[index]
            tmp_path,
            library,
            library_info,
        )

        assert registered is True
        assert library_info.problems == []
        assert library.has_node_type(NODE_TYPE)
        node_class = library.get_node_class(NODE_TYPE)
        assert issubclass(node_class, WorkflowNode)
        assert node_class.workflow_file_path == workflow_path

    def test_metadata_from_the_json_wins(self, engine: Engine, tmp_path: Path) -> None:
        workflow_path = _write_workflow(tmp_path, shape=_SHAPE)
        library = _library([_definition(workflow_path.name)])

        engine.library_manager.module_loading._register_workflow_node(
            library.get_library_data().workflow_nodes[0],  # type: ignore[index]
            tmp_path,
            library,
            _library_info(),
        )

        assert library.get_node_metadata(NODE_TYPE).display_name == "Shout Workflow"

    def test_missing_workflow_file_records_problem(self, engine: Engine, tmp_path: Path) -> None:
        library = _library([_definition("absent_workflow.py")])
        library_info = _library_info()

        registered = engine.library_manager.module_loading._register_workflow_node(
            library.get_library_data().workflow_nodes[0],  # type: ignore[index]
            tmp_path,
            library,
            library_info,
        )

        assert registered is False
        assert not library.has_node_type(NODE_TYPE)
        assert [type(problem) for problem in library_info.problems] == [WorkflowNodeLoadProblem]

    def test_workflow_without_shape_records_problem(self, engine: Engine, tmp_path: Path) -> None:
        workflow_path = _write_workflow(tmp_path, shape=None)
        library = _library([_definition(workflow_path.name)])
        library_info = _library_info()

        registered = engine.library_manager.module_loading._register_workflow_node(
            library.get_library_data().workflow_nodes[0],  # type: ignore[index]
            tmp_path,
            library,
            library_info,
        )

        assert registered is False
        assert not library.has_node_type(NODE_TYPE)
        problem = library_info.problems[0]
        assert isinstance(problem, WorkflowNodeLoadProblem)
        assert "no saved input and output shape" in problem.error_message


@pytest.fixture
def workspace(engine: Engine, tmp_path: Path) -> Iterator[Path]:
    """Point the engine's workspace at a fresh directory beside the test's library files."""
    original = engine.config_manager.workspace_path
    workspace_path = tmp_path / "workspace"
    workspace_path.mkdir()
    engine.config_manager.workspace_path = workspace_path
    yield workspace_path
    engine.config_manager.workspace_path = original


def _register(
    engine: Engine, workflow_dir: Path, library_info: LibraryManager.LibraryInfo | None = None
) -> LibraryManager.LibraryInfo:
    library = _library([_definition("demo_workflow.py")])
    if library_info is None:
        library_info = _library_info()
    engine.library_manager.module_loading._register_workflow_node(
        library.get_library_data().workflow_nodes[0],  # type: ignore[index]
        workflow_dir,
        library,
        library_info,
    )
    return library_info


class TestListWorkflowAtLoad:
    """The workflow behind a `workflow_nodes` entry is listed when its library loads."""

    def test_workflow_outside_the_workspace_is_listed_as_library_provided(
        self, engine: Engine, tmp_path: Path, workspace: Path
    ) -> None:
        library_dir = tmp_path / "library"
        library_dir.mkdir()
        workflow_path = _write_workflow(library_dir, shape=_SHAPE)
        assert not workflow_path.is_relative_to(workspace)

        _register(engine, library_dir)

        workflow = engine.workflow_registry.get_workflow_by_name(derive_registry_key(str(workflow_path)))
        assert workflow.metadata.is_griptape_provided is True

    def test_the_file_on_disk_is_not_rewritten(self, engine: Engine, tmp_path: Path, workspace: Path) -> None:  # noqa: ARG002
        library_dir = tmp_path / "library"
        library_dir.mkdir()
        workflow_path = _write_workflow(library_dir, shape=_SHAPE)
        original_text = workflow_path.read_text(encoding="utf-8")

        _register(engine, library_dir)

        assert workflow_path.read_text(encoding="utf-8") == original_text

    @pytest.mark.parametrize("is_template", [True, False])
    def test_template_status_comes_from_the_header(
        self,
        engine: Engine,
        tmp_path: Path,
        workspace: Path,  # noqa: ARG002
        *,
        is_template: bool,
    ) -> None:
        library_dir = tmp_path / "library"
        library_dir.mkdir()
        workflow_path = _write_workflow(library_dir, shape=_SHAPE, is_template=is_template)

        _register(engine, library_dir)

        workflow = engine.workflow_registry.get_workflow_by_name(derive_registry_key(str(workflow_path)))
        assert workflow.metadata.is_template is is_template

    def test_workflow_in_the_workspace_is_left_to_the_workspace_scan(self, engine: Engine, workspace: Path) -> None:
        workflow_path = _write_workflow(workspace, shape=_SHAPE)

        _register(engine, workspace)

        registry_keys = engine.workflow_registry.list_workflows()
        assert derive_registry_key(workflow_path.name) not in registry_keys
        assert derive_registry_key(str(workflow_path)) not in registry_keys

    def test_library_installed_in_the_workspace_is_listed_as_library_provided(
        self, engine: Engine, workspace: Path
    ) -> None:
        """Libraries install to `<workspace>/libraries` by default, and the workspace scan skips them."""
        library_dir = workspace / "libraries" / "demo-library"
        library_dir.mkdir(parents=True)
        _write_workflow(library_dir, shape=_SHAPE)

        _register(engine, library_dir, _library_info(str(library_dir / "griptape_nodes_library.json")))

        workflow = engine.workflow_registry.get_workflow_by_name("libraries/demo-library/demo_workflow")
        assert workflow.metadata.is_griptape_provided is True

    def test_sandbox_library_workflow_is_left_to_the_workspace_scan(self, engine: Engine, workspace: Path) -> None:
        """The scan lists sandbox workflows as the user's own, so the library load leaves them to it."""
        library_dir = workspace / "sandbox_library"
        library_dir.mkdir()
        _write_workflow(library_dir, shape=_SHAPE)

        _register(
            engine,
            library_dir,
            _library_info(str(library_dir / "griptape_nodes_library.json"), is_sandbox=True),
        )

        assert "sandbox_library/demo_workflow" not in engine.workflow_registry.list_workflows()

    def test_loading_the_library_again_keeps_one_listing(
        self,
        engine: Engine,
        tmp_path: Path,
        workspace: Path,  # noqa: ARG002
    ) -> None:
        library_dir = tmp_path / "library"
        library_dir.mkdir()
        workflow_path = _write_workflow(library_dir, shape=_SHAPE)

        _register(engine, library_dir)
        second_info = _register(engine, library_dir)

        assert second_info.problems == []
        registry_key = derive_registry_key(str(workflow_path))
        assert [key for key in engine.workflow_registry.list_workflows() if key == registry_key] == [registry_key]

    def test_a_listing_failure_keeps_the_node_type(
        self,
        engine: Engine,
        tmp_path: Path,
        workspace: Path,  # noqa: ARG002
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        library_dir = tmp_path / "library"
        library_dir.mkdir()
        _write_workflow(library_dir, shape=_SHAPE)

        def _vanished(*_args: object, **_kwargs: object) -> str:
            msg = "File path does not exist."
            raise ValueError(msg)

        monkeypatch.setattr(engine.workflow_registry, "refresh_file", _vanished)
        library = _library([_definition("demo_workflow.py")])
        library_info = _library_info()

        with caplog.at_level(logging.WARNING, logger="griptape_nodes"):
            registered = engine.library_manager.module_loading._register_workflow_node(
                library.get_library_data().workflow_nodes[0],  # type: ignore[index]
                library_dir,
                library,
                library_info,
            )

        assert registered is True
        assert library.has_node_type(NODE_TYPE)
        assert library_info.problems == []
        assert "could not list the workflow" in caplog.text


class TestWorkflowNodeLoadProblemDisplay:
    def test_single_problem_names_the_node_and_reason(self) -> None:
        problem = WorkflowNodeLoadProblem(
            node_type="ShoutWorkflow", workflow_path="/lib/shout.py", error_message="boom"
        )

        message = WorkflowNodeLoadProblem.collate_problems_for_display([problem])

        assert "ShoutWorkflow" in message
        assert "/lib/shout.py" in message
        assert "boom" in message

    def test_multiple_problems_are_listed_alphabetically(self) -> None:
        problems = [
            WorkflowNodeLoadProblem(node_type="Zed", workflow_path="/lib/z.py", error_message="z failed"),
            WorkflowNodeLoadProblem(node_type="Alpha", workflow_path="/lib/a.py", error_message="a failed"),
        ]

        message = WorkflowNodeLoadProblem.collate_problems_for_display(problems)

        assert message.index("Alpha") < message.index("Zed")
        assert "Encountered 2 workflow-backed node failures" in message


@pytest.mark.usefixtures("engine")
class TestSchemaDefaults:
    def test_workflow_nodes_defaults_to_none(self) -> None:
        schema = LibrarySchema(
            name="TestLib",
            library_schema_version=LibrarySchema.LATEST_SCHEMA_VERSION,
            metadata=LibraryMetadata(
                author="Test", description="Test", library_version="1.0.0", engine_version="0.0.0", tags=[]
            ),
            categories=[],
            nodes=[],
        )

        assert schema.workflow_nodes is None


class TestListWorkflowOnRegistryRefresh:
    """A registry refresh lists the workflows behind workflow nodes again, keyed for the current workspace."""

    def test_relists_against_the_current_workspace(
        self, engine: Engine, tmp_path: Path, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        library_dir = workspace / "libraries" / "demo-library"
        library_dir.mkdir(parents=True)
        workflow_path = _write_workflow(library_dir, shape=_SHAPE)
        library = _library([_definition("demo_workflow.py")])
        library_info = _library_info(str(library_dir / "griptape_nodes_library.json"))
        engine.library_manager.module_loading._register_workflow_node(
            library.get_library_data().workflow_nodes[0],  # type: ignore[index]
            library_dir,
            library,
            library_info,
        )
        monkeypatch.setitem(engine.library_manager._library_file_path_to_info, library_info.library_path, library_info)
        other_workspace = tmp_path / "other_workspace"
        other_workspace.mkdir()
        engine.config_manager.workspace_path = other_workspace
        engine.workflow_registry.reindex_workflows()

        engine.library_manager.register_library_workflow_node_workflows(found_workflow_ids=set())

        workflow = engine.workflow_registry.get_workflow_by_name(derive_registry_key(str(workflow_path)))
        assert workflow.metadata.is_griptape_provided is True

    def test_leaves_sandbox_workflows_to_the_workspace_scan(
        self, engine: Engine, tmp_path: Path, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        library_dir = workspace / "sandbox_library"
        library_dir.mkdir()
        workflow_path = _write_workflow(library_dir, shape=_SHAPE)
        library_info = _register(
            engine,
            library_dir,
            _library_info(str(library_dir / "griptape_nodes_library.json"), is_sandbox=True),
        )
        monkeypatch.setitem(engine.library_manager._library_file_path_to_info, library_info.library_path, library_info)
        other_workspace = tmp_path / "other_workspace"
        other_workspace.mkdir()
        engine.config_manager.workspace_path = other_workspace
        engine.workflow_registry.reindex_workflows()

        engine.library_manager.register_library_workflow_node_workflows(found_workflow_ids=set())

        assert derive_registry_key(str(workflow_path)) not in engine.workflow_registry.list_workflows()

    @pytest.mark.usefixtures("workspace")
    def test_lists_sandbox_workflows_when_the_sandbox_is_outside_the_workspace(
        self, engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        library_dir = tmp_path / "local_sandbox"
        library_dir.mkdir()
        workflow_path = _write_workflow(library_dir, shape=_SHAPE)
        engine.config_manager.set_config_value("sandbox_library_directory", str(library_dir))
        library_info = _register(
            engine,
            library_dir,
            _library_info(str(library_dir / "griptape_nodes_library.json"), is_sandbox=True),
        )
        monkeypatch.setitem(engine.library_manager._library_file_path_to_info, library_info.library_path, library_info)
        engine.workflow_registry.reindex_workflows()

        engine.library_manager.register_library_workflow_node_workflows(found_workflow_ids=set())

        workflow = engine.workflow_registry.get_workflow_by_name(derive_registry_key(str(workflow_path)))
        assert workflow.metadata.is_griptape_provided is True


class TestWorkflowIdAcrossALibraryReload:
    """A library reload loads its node types again and refreshes the registry; their workflows keep their ids."""

    def test_the_workflow_behind_a_workflow_node_keeps_its_id(
        self, engine: Engine, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        library_dir = workspace / "libraries" / "demo-library"
        library_dir.mkdir(parents=True)
        workflow_path = _write_workflow(library_dir, shape=_SHAPE)
        library_json = str(library_dir / "griptape_nodes_library.json")
        _register(engine, library_dir, _library_info(library_json))
        first = engine.workflow_registry.find_by_path(workflow_path)
        assert first is not None

        # The reload builds the node type again, with metadata of its own, then refreshes the registry.
        reloaded_info = _register(engine, library_dir, _library_info(library_json))
        monkeypatch.setitem(engine.library_manager._library_file_path_to_info, library_json, reloaded_info)
        with patch.object(engine.library_manager, "collect_library_workflow_files", AsyncMock(return_value=[])):
            asyncio.run(engine.workflow_manager.refresh_workflow_registry())

        again = engine.workflow_registry.find_by_path(workflow_path)
        assert again is not None
        assert again is first
        assert again.workflow_id == first.workflow_id
        assert again.metadata is reloaded_info.workflow_node_classes[0].workflow_metadata
        assert again.metadata.is_griptape_provided is True

    @pytest.mark.usefixtures("workspace")
    def test_the_workflow_of_an_unloaded_library_drops_out_on_refresh(
        self, engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Outside the workspace: once the library is gone, the workspace scan would list a file in it.
        library_dir = tmp_path / "libraries" / "demo-library"
        library_dir.mkdir(parents=True)
        workflow_path = _write_workflow(library_dir, shape=_SHAPE)
        library_json = str(library_dir / "griptape_nodes_library.json")
        library_info = _register(engine, library_dir, _library_info(library_json))
        monkeypatch.setitem(engine.library_manager._library_file_path_to_info, library_json, library_info)
        first = engine.workflow_registry.find_by_path(workflow_path)
        assert first is not None

        monkeypatch.delitem(engine.library_manager._library_file_path_to_info, library_json)
        with patch.object(engine.library_manager, "collect_library_workflow_files", AsyncMock(return_value=[])):
            asyncio.run(engine.workflow_manager.refresh_workflow_registry())

        assert engine.workflow_registry.find_by_id(first.workflow_id) is None
        assert engine.workflow_registry.find_by_path(workflow_path) is None

    def test_a_workflow_the_library_also_lists_keeps_the_metadata_read_from_its_header(
        self, engine: Engine, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The library's `workflows` list is scanned first in a refresh, and its read of the header stands."""
        library_dir = workspace / "libraries" / "demo-library"
        library_dir.mkdir(parents=True)
        workflow_path = _write_workflow(library_dir, shape=_SHAPE)
        library_json = str(library_dir / "griptape_nodes_library.json")
        library_info = _register(engine, library_dir, _library_info(library_json))
        monkeypatch.setitem(engine.library_manager._library_file_path_to_info, library_json, library_info)
        node_metadata = library_info.workflow_node_classes[0].workflow_metadata

        with patch.object(
            engine.library_manager, "collect_library_workflow_files", AsyncMock(return_value=[str(workflow_path)])
        ):
            asyncio.run(engine.workflow_manager.refresh_workflow_registry())

        workflow = engine.workflow_registry.find_by_path(workflow_path)
        assert workflow is not None
        assert workflow.metadata is not node_metadata
        assert workflow.metadata.is_griptape_provided is False
