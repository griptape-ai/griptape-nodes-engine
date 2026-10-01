"""Collecting the library dependencies of the workflows a workflow references.

A workflow's metadata header records only the libraries its own nodes need. The libraries behind a
referenced sub-workflow live in that sub-workflow's header, and it can be edited after the
referencing workflow was saved -- so they are read at check time rather than copied at save time.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from griptape_nodes.node_library.library_registry import LibraryNameAndVersion
from griptape_nodes.node_library.workflow_registry import WorkflowMetadata, WorkflowRegistry
from griptape_nodes.retained_mode.engine import Engine
from griptape_nodes.retained_mode.managers.fitness_problems.workflows import (
    ReferencedWorkflowUnresolvableProblem,
)


def _metadata(
    name: str,
    *,
    libraries: list[tuple[str, str]] | None = None,
    workflows_referenced: list[str] | None = None,
) -> WorkflowMetadata:
    return WorkflowMetadata(
        name=name,
        schema_version=WorkflowMetadata.LATEST_SCHEMA_VERSION,
        engine_version_created_with="1.0.0",
        node_libraries_referenced=[
            LibraryNameAndVersion(library_name=lib, library_version=version) for lib, version in (libraries or [])
        ],
        workflows_referenced=workflows_referenced,
        creation_date=datetime.now(tz=UTC),
    )


def _write_workflow(directory: Path, metadata: WorkflowMetadata) -> str:
    """Write a workflow file carrying `metadata` in its header, and register it. Returns its key."""
    file_path = directory / f"{metadata.name}.py"
    # The walk reads the header off disk, so write the fields it needs as the TOML it parses.
    libraries = ", ".join(
        f'{{ library_name = "{lib.library_name}", library_version = "{lib.library_version}" }}'
        for lib in metadata.node_libraries_referenced
    )
    lines = [
        "# /// script",
        "# [tool.griptape-nodes]",
        f'# name = "{metadata.name}"',
        f'# schema_version = "{metadata.schema_version}"',
        f'# engine_version_created_with = "{metadata.engine_version_created_with}"',
        f"# node_libraries_referenced = [{libraries}]",
    ]
    if metadata.workflows_referenced is not None:
        referenced = ", ".join(f'"{name}"' for name in metadata.workflows_referenced)
        lines.append(f"# workflows_referenced = [{referenced}]")
    lines.append("# ///")
    file_path.write_text("\n".join(lines), encoding="utf-8")

    WorkflowRegistry.generate_new_workflow(registry_key=metadata.name, metadata=metadata, file_path=str(file_path))
    return metadata.name


@pytest.fixture(autouse=True)
def _workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Resolve registry file paths as written, so the tests need no workspace config.

    The registry is a singleton, so each test also starts from an empty one -- otherwise a
    workflow one test registers collides with the same name in the next.
    """
    monkeypatch.setattr(
        WorkflowRegistry,
        "get_complete_file_path",
        classmethod(lambda _cls, relative_file_path: relative_file_path),
    )
    monkeypatch.setattr(WorkflowRegistry(), "_workflows", {})
    return tmp_path


class TestCollectReferencedWorkflowDependencies:
    def test_no_referenced_workflows_collects_nothing(self, engine: Engine) -> None:
        metadata = _metadata("solo", libraries=[("LibA", "1.0.0")])

        collected = engine.workflow_manager.collect_referenced_workflow_dependencies(metadata)

        assert collected.libraries == []
        assert collected.problems == []

    def test_collects_a_referenced_workflows_libraries(self, engine: Engine, tmp_path: Path) -> None:
        _write_workflow(tmp_path, _metadata("child", libraries=[("LibX", "1.0.0"), ("LibY", "2.0.0")]))
        parent = _metadata("parent", libraries=[("LibA", "1.0.0")], workflows_referenced=["child"])

        collected = engine.workflow_manager.collect_referenced_workflow_dependencies(parent)

        assert {lib.library_name for lib in collected.libraries} == {"LibX", "LibY"}
        assert collected.problems == []

    def test_reads_the_current_child_header_not_a_saved_copy(self, engine: Engine, tmp_path: Path) -> None:
        """The scenario this exists for: the child changed which libraries it needs after the parent was saved."""
        _write_workflow(tmp_path, _metadata("child", libraries=[("LibX", "1.0.0"), ("LibZ", "3.0.0")]))
        # The parent still records the child's old set (LibX, LibY) as its own merged snapshot.
        parent = _metadata("parent", libraries=[], workflows_referenced=["child"])

        collected = engine.workflow_manager.collect_referenced_workflow_dependencies(parent)

        library_names = {lib.library_name for lib in collected.libraries}
        assert library_names == {"LibX", "LibZ"}
        assert "LibY" not in library_names

    def test_walks_transitively(self, engine: Engine, tmp_path: Path) -> None:
        _write_workflow(tmp_path, _metadata("grandchild", libraries=[("LibDeep", "1.0.0")]))
        _write_workflow(
            tmp_path, _metadata("child", libraries=[("LibX", "1.0.0")], workflows_referenced=["grandchild"])
        )
        parent = _metadata("parent", workflows_referenced=["child"])

        collected = engine.workflow_manager.collect_referenced_workflow_dependencies(parent)

        assert {lib.library_name for lib in collected.libraries} == {"LibX", "LibDeep"}

    def test_a_cycle_terminates(self, engine: Engine, tmp_path: Path) -> None:
        """Two workflows referencing each other is ordinary, and must not hang or report a problem."""
        _write_workflow(tmp_path, _metadata("b", libraries=[("LibB", "1.0.0")], workflows_referenced=["a"]))
        parent = _metadata("a", libraries=[("LibA", "1.0.0")], workflows_referenced=["b"])
        _write_workflow(tmp_path, parent)

        collected = engine.workflow_manager.collect_referenced_workflow_dependencies(parent)

        assert {lib.library_name for lib in collected.libraries} == {"LibB", "LibA"}
        assert collected.problems == []

    def test_unregistered_child_is_reported_and_siblings_still_collected(self, engine: Engine, tmp_path: Path) -> None:
        _write_workflow(tmp_path, _metadata("present", libraries=[("LibPresent", "1.0.0")]))
        parent = _metadata("parent", workflows_referenced=["present", "vanished"])

        collected = engine.workflow_manager.collect_referenced_workflow_dependencies(parent)

        assert {lib.library_name for lib in collected.libraries} == {"LibPresent"}
        assert len(collected.problems) == 1
        problem = collected.problems[0]
        assert isinstance(problem, ReferencedWorkflowUnresolvableProblem)
        assert problem.workflow_name == "vanished"

    def test_duplicate_library_across_children_collected_once(self, engine: Engine, tmp_path: Path) -> None:
        _write_workflow(tmp_path, _metadata("child_one", libraries=[("Shared", "1.0.0")]))
        _write_workflow(tmp_path, _metadata("child_two", libraries=[("Shared", "2.0.0")]))
        parent = _metadata("parent", workflows_referenced=["child_one", "child_two"])

        collected = engine.workflow_manager.collect_referenced_workflow_dependencies(parent)

        assert [lib.library_name for lib in collected.libraries] == ["Shared"]
