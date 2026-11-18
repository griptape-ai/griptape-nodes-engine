"""Older workflows' links to other workflows, by registry name, point at ids once loaded."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from griptape_nodes.node_library.workflow_registry import WorkflowMetadata
from griptape_nodes.version_compatibility.versions.v0_22_0.workflow_ids import WorkflowIdsMigration

if TYPE_CHECKING:
    from pathlib import Path

    from griptape_nodes.node_library.workflow_registry import Workflow
    from griptape_nodes.retained_mode.engine import Engine


def _metadata(schema_version: str, **fields: object) -> WorkflowMetadata:
    return WorkflowMetadata(
        name="flow",
        schema_version=schema_version,
        engine_version_created_with="0.0.0",
        node_libraries_referenced=[],
        **fields,  # type: ignore[arg-type]
    )


@pytest.fixture
def migration(engine: Engine) -> WorkflowIdsMigration:
    """The migration, resolving names in `engine`'s registry."""
    return WorkflowIdsMigration(engine=engine)


@pytest.fixture
def inner(engine: Engine, tmp_path: Path) -> Workflow:
    """A registered workflow named `shots/inner`, which other workflows link to."""
    engine.config_manager.workspace_path = tmp_path
    (tmp_path / "shots").mkdir()
    (tmp_path / "shots" / "inner.py").write_text("print('inner')\n", encoding="utf-8")
    return engine.workflow_registry.register_file("shots/inner.py", _metadata("0.21.0"))


class TestAppliesTo:
    def test_only_older_schemas(self, migration: WorkflowIdsMigration) -> None:
        assert migration.applies_to_workflow(_metadata("0.21.0"))
        assert not migration.applies_to_workflow(_metadata("0.22.0"))
        assert not migration.applies_to_workflow(_metadata("not a version"))


class TestMigrateMetadata:
    def test_names_become_ids_and_unknown_names_stay(self, migration: WorkflowIdsMigration, inner: Workflow) -> None:
        metadata = _metadata(
            "0.21.0", branched_from="shots/inner", workflows_referenced=["shots/inner", "gone", "shots/inner"]
        )

        migrated = migration.migrate_metadata(metadata)

        assert migrated.branched_from == inner.workflow_id
        assert migrated.workflows_referenced == [inner.workflow_id, "gone"]
        assert metadata.branched_from == "shots/inner"


class TestMigrateSource:
    def test_an_import_by_name_imports_by_id(self, migration: WorkflowIdsMigration, inner: Workflow) -> None:
        source = (
            "label = 'é'\n"
            "flow1_name = (await GriptapeNodes.ahandle_request(ImportWorkflowAsReferencedSubFlowRequest(\n"
            "    workflow_name='shots/inner', imported_flow_metadata={'é': 1})))\n"
        )

        migrated = migration.migrate_source(source)

        assert migrated == source.replace("workflow_name='shots/inner'", f"workflow_id={inner.workflow_id!r}")

    def test_an_import_of_an_unknown_name_or_one_with_an_id_is_left_alone(
        self, migration: WorkflowIdsMigration, inner: Workflow
    ) -> None:
        source = (
            "a = ImportWorkflowAsReferencedSubFlowRequest(workflow_name='gone')\n"
            f"b = ImportWorkflowAsReferencedSubFlowRequest(workflow_name='shots/inner', workflow_id={inner.workflow_id!r})\n"
            "c = SomethingElse(workflow_name='shots/inner')\n"
        )

        assert migration.migrate_source(source) == source

    def test_code_that_does_not_parse_is_left_alone(self, migration: WorkflowIdsMigration) -> None:
        assert migration.migrate_source("def (:\n") == "def (:\n"
