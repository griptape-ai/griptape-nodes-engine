"""Tests for registering `workflow_nodes` entries as node types (library manager side)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from griptape_nodes.exe_types.workflow_node import WorkflowNode
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
    from pathlib import Path

    from griptape_nodes.retained_mode.engine import Engine

NODE_TYPE = "ShoutWorkflow"

_SHAPE = {
    "inputs": {"Start Flow": {"text": {"name": "text", "type": "str", "default_value": ""}}},
    "outputs": {"End Flow": {"result": {"name": "result", "type": "str", "default_value": ""}}},
}


def _library_info() -> LibraryManager.LibraryInfo:
    return LibraryManager.LibraryInfo(
        lifecycle_state=LibraryManager.LibraryLifecycleState.EVALUATED,
        fitness=LibraryManager.LibraryFitness.NOT_EVALUATED,
        library_path="/fake/path",
        is_sandbox=False,
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


def _write_workflow(tmp_path: Path, *, shape: dict[str, Any] | None) -> Path:
    lines = [
        "# /// script",
        "# [tool.griptape-nodes]",
        '# name = "demo"',
        f'# schema_version = "{WorkflowMetadata.LATEST_SCHEMA_VERSION}"',
        '# engine_version_created_with = "0.0.0"',
        "# node_libraries_referenced = []",
    ]
    if shape is not None:
        encoded = json.dumps(json.dumps(shape, separators=(",", ":")))
        lines.append(f"# workflow_shape = {encoded}")
    lines.append("# ///")
    workflow_path = tmp_path / "demo_workflow.py"
    workflow_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return workflow_path


class TestRegisterWorkflowNode:
    def test_registers_generated_node_type(self, griptape_nodes: Engine, tmp_path: Path) -> None:
        workflow_path = _write_workflow(tmp_path, shape=_SHAPE)
        library = _library([_definition(workflow_path.name)])
        library_info = _library_info()

        registered = griptape_nodes.library_manager._register_workflow_node(
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

    def test_metadata_from_the_json_wins(self, griptape_nodes: Engine, tmp_path: Path) -> None:
        workflow_path = _write_workflow(tmp_path, shape=_SHAPE)
        library = _library([_definition(workflow_path.name)])

        griptape_nodes.library_manager._register_workflow_node(
            library.get_library_data().workflow_nodes[0],  # type: ignore[index]
            tmp_path,
            library,
            _library_info(),
        )

        assert library.get_node_metadata(NODE_TYPE).display_name == "Shout Workflow"

    def test_missing_workflow_file_records_problem(self, griptape_nodes: Engine, tmp_path: Path) -> None:
        library = _library([_definition("absent_workflow.py")])
        library_info = _library_info()

        registered = griptape_nodes.library_manager._register_workflow_node(
            library.get_library_data().workflow_nodes[0],  # type: ignore[index]
            tmp_path,
            library,
            library_info,
        )

        assert registered is False
        assert not library.has_node_type(NODE_TYPE)
        assert [type(problem) for problem in library_info.problems] == [WorkflowNodeLoadProblem]

    def test_workflow_without_shape_records_problem(self, griptape_nodes: Engine, tmp_path: Path) -> None:
        workflow_path = _write_workflow(tmp_path, shape=None)
        library = _library([_definition(workflow_path.name)])
        library_info = _library_info()

        registered = griptape_nodes.library_manager._register_workflow_node(
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


@pytest.mark.usefixtures("griptape_nodes")
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
