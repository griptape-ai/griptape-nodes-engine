"""Workflow ids: minted by the registry, kept across refresh, and never written to a file."""

from __future__ import annotations

import asyncio
import shutil
import sys
import uuid
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from griptape_nodes.node_library.workflow_registry import (
    WorkflowMetadata,
    read_workflow_metadata,
)
from griptape_nodes.retained_mode.engine import current_engine
from griptape_nodes.retained_mode.events.context_events import (
    EnsureWorkflowAndFlowRequest,
    EnsureWorkflowAndFlowResultSuccess,
)
from griptape_nodes.retained_mode.events.workflow_events import (
    ListAllWorkflowsRequest,
    ListAllWorkflowsResultSuccess,
    MoveWorkflowRequest,
    MoveWorkflowResultFailure,
    SaveWorkflowRequest,
    SaveWorkflowResultFailure,
)

if TYPE_CHECKING:
    from pathlib import Path

    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.events.base_events import ResultPayload


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """An empty workspace folder, links resolved so paths compare as the registry stores them."""
    workspace = tmp_path.resolve() / "workspace"
    workspace.mkdir()
    return workspace


def _run(engine: Engine, request: object) -> ResultPayload:
    return asyncio.run(engine.ahandle_request(request))  # type: ignore[arg-type]


def _id_at(engine: Engine, path: str | Path) -> str | None:
    """The id of the workflow registered for the file at `path`."""
    entry = engine.workflow_registry.find_by_path(path)
    if entry is None:
        return None
    return entry.workflow_id


def _write_workflow_file(path: Path, *, name: str = "flow") -> Path:
    """Write a workflow file carrying only a metadata header."""
    metadata = WorkflowMetadata(
        name=name,
        schema_version=WorkflowMetadata.LATEST_SCHEMA_VERSION,
        engine_version_created_with="0.0.0",
        node_libraries_referenced=[],
    )
    lines = ["# /// script", "# dependencies = []", "#", "# [tool.griptape-nodes]"]
    for key, value in metadata.model_dump(exclude_none=True, mode="json").items():
        if isinstance(value, str):
            lines.append(f'# {key} = "{value}"')
        elif isinstance(value, bool):
            lines.append(f"# {key} = {str(value).lower()}")
        elif isinstance(value, list):
            lines.append(f"# {key} = []")
    lines.extend(["# ///", "", "print('body')", ""])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _scan(engine: Engine, *paths: Path) -> None:
    """Refresh the registry from just these folders or files."""
    asyncio.run(engine.workflow_manager.refresh_workflow_registry(workflows_to_register=[str(p) for p in paths]))


class TestIdsAcrossRescans:
    """A rescan keeps each file's id. A file the rescan does not find again loses it."""

    def test_a_scanned_file_gets_an_id_that_is_not_written(self, workspace: Path) -> None:
        engine = current_engine()
        engine.config_manager.workspace_path = workspace
        workflow_file = _write_workflow_file(workspace / "legacy.py")
        content_before = workflow_file.read_text(encoding="utf-8")

        _scan(engine, workspace)

        entry = engine.workflow_registry.find_by_path(workflow_file)
        assert entry is not None
        assert uuid.UUID(hex=entry.workflow_id).hex == entry.workflow_id
        assert workflow_file.read_text(encoding="utf-8") == content_before

    def test_the_id_is_stable_across_refresh_and_project_switch(self, workspace: Path) -> None:
        engine = current_engine()
        engine.config_manager.workspace_path = workspace
        workflow_file = _write_workflow_file(workspace / "shots" / "legacy.py")
        _scan(engine, workspace)
        first_id = engine.workflow_registry.find_by_path(workflow_file).workflow_id  # type: ignore[union-attr]

        _scan(engine, workspace)
        after_refresh = engine.workflow_registry.find_by_path(workflow_file)
        engine.config_manager.workspace_path = workspace / "shots"
        _scan(engine, workspace / "shots")
        after_switch = engine.workflow_registry.find_by_path(workflow_file)

        assert after_refresh is not None
        assert after_switch is not None
        assert after_refresh.workflow_id == first_id
        assert after_switch.workflow_id == first_id
        assert after_switch.name == "legacy"

    def test_a_copy_of_a_file_is_a_workflow_of_its_own(self, workspace: Path) -> None:
        engine = current_engine()
        engine.config_manager.workspace_path = workspace
        original = _write_workflow_file(workspace / "original.py")
        copy = workspace / "copy.py"
        shutil.copy(original, copy)
        copy_content = copy.read_text(encoding="utf-8")

        _scan(engine, workspace)
        original_id = _id_at(engine, original)
        copy_id = _id_at(engine, copy)
        _scan(engine, workspace)

        assert original_id is not None
        assert copy_id not in (None, original_id)
        assert _id_at(engine, original) == original_id
        assert _id_at(engine, copy) == copy_id
        assert copy.read_text(encoding="utf-8") == copy_content

    def test_a_file_moved_outside_the_engine_gets_a_new_id(self, workspace: Path) -> None:
        engine = current_engine()
        engine.config_manager.workspace_path = workspace
        workflow_file = _write_workflow_file(workspace / "mover.py")
        _scan(engine, workspace)
        first_id = _id_at(engine, workflow_file)
        assert first_id is not None
        moved_file = workspace / "shots" / "mover.py"
        moved_file.parent.mkdir()
        shutil.move(workflow_file, moved_file)

        _scan(engine, workspace)

        assert engine.workflow_registry.find_by_id(first_id) is None
        assert _id_at(engine, moved_file) not in (None, first_id)

    @pytest.mark.skipif(sys.platform.startswith("win"), reason="symlink creation needs privileges on Windows")
    def test_a_file_reached_through_a_link_is_one_workflow(self, workspace: Path, tmp_path: Path) -> None:
        engine = current_engine()
        engine.config_manager.workspace_path = workspace
        real = _write_workflow_file(tmp_path / "real" / "flow.py")
        linked = workspace / "linked" / "flow.py"
        (workspace / "linked").symlink_to(tmp_path / "real", target_is_directory=True)

        _scan(engine, workspace, real)
        entry = engine.workflow_registry.find_by_path(real)
        _scan(engine, workspace, real)

        assert entry is not None
        assert engine.workflow_registry.find_by_path(linked) is engine.workflow_registry.find_by_path(real)
        assert _id_at(engine, linked) == entry.workflow_id
        assert _id_at(engine, real) == entry.workflow_id


class TestRegistryRefresh:
    def test_a_rescan_keeps_the_entry_and_reads_the_header_again(self, workspace: Path) -> None:
        engine = current_engine()
        engine.config_manager.workspace_path = workspace
        workflow_file = _write_workflow_file(workspace / "flow.py", name="Before")
        _scan(engine, workspace)
        first = engine.workflow_registry.find_by_path(workflow_file)
        assert first is not None
        _write_workflow_file(workflow_file, name="After")

        _scan(engine, workspace)

        assert engine.workflow_registry.find_by_path(workflow_file) is first
        assert first.metadata.name == "After"

    def test_a_file_whose_header_became_unreadable_drops_out(self, workspace: Path) -> None:
        engine = current_engine()
        engine.config_manager.workspace_path = workspace
        workflow_file = _write_workflow_file(workspace / "flow.py")
        _scan(engine, workspace)
        first_id = _id_at(engine, workflow_file)
        assert first_id is not None
        workflow_file.write_text(
            "# /// script\n# [tool.griptape-nodes]\n# name = \n# ///\nprint('body')\n", encoding="utf-8"
        )

        _scan(engine, workspace)

        assert engine.workflow_registry.find_by_id(first_id) is None
        assert engine.workflow_registry.find_by_path(workflow_file) is None

    def test_a_scan_that_raises_removes_nothing(self, workspace: Path) -> None:
        """A partial scan cannot tell a missing file from one it did not reach, so it keeps every entry."""
        engine = current_engine()
        engine.config_manager.workspace_path = workspace
        workflow_file = _write_workflow_file(workspace / "flow.py")
        _scan(engine, workspace)
        first_id = _id_at(engine, workflow_file)
        assert first_id is not None
        workflow_file.unlink()

        with (
            patch.object(
                engine.workflow_manager,
                "_process_workflows_for_registration",
                side_effect=RuntimeError("scan failed"),
            ),
            pytest.raises(RuntimeError, match="scan failed"),
        ):
            _scan(engine, workspace)

        assert engine.workflow_registry.find_by_id(first_id) is not None
        assert engine.workflow_manager._workflows_loading_complete.is_set()

    def test_a_workflow_registered_while_the_scan_runs_survives_it(self, workspace: Path, tmp_path: Path) -> None:
        """An import that lands mid-scan is not something the scan missed."""
        engine = current_engine()
        engine.config_manager.workspace_path = workspace
        imported_file = _write_workflow_file(tmp_path / "elsewhere" / "imported.py")
        real_scan = engine.workflow_manager._process_workflows_for_registration

        async def scan_with_an_import(workflows_to_register: list[str]) -> object:
            engine.workflow_registry.register_file(imported_file, read_workflow_metadata(imported_file))
            return await real_scan(workflows_to_register)

        with patch.object(engine.workflow_manager, "_process_workflows_for_registration", scan_with_an_import):
            _scan(engine, workspace)

        assert _id_at(engine, imported_file) is not None

    def test_a_workflow_saved_for_the_first_time_while_the_scan_runs_survives_it(
        self, workspace: Path, tmp_path: Path
    ) -> None:
        engine = current_engine()
        engine.config_manager.workspace_path = workspace
        unsaved = engine.workflow_registry.register_unsaved("Scratch")
        saved_file = _write_workflow_file(tmp_path / "elsewhere" / "scratch.py")
        real_scan = engine.workflow_manager._process_workflows_for_registration

        async def scan_with_a_first_save(workflows_to_register: list[str]) -> object:
            result = await real_scan(workflows_to_register)
            engine.workflow_registry.set_file_path(unsaved, saved_file)
            return result

        with patch.object(engine.workflow_manager, "_process_workflows_for_registration", scan_with_a_first_save):
            _scan(engine, workspace)

        assert _id_at(engine, saved_file) == unsaved.workflow_id

    def test_a_workflow_moved_while_the_scan_runs_survives_it(self, workspace: Path, tmp_path: Path) -> None:
        engine = current_engine()
        engine.config_manager.workspace_path = workspace
        workflow_file = _write_workflow_file(workspace / "flow.py")
        _scan(engine, workspace)
        workflow = engine.workflow_registry.find_by_path(workflow_file)
        assert workflow is not None
        moved_file = tmp_path / "elsewhere" / "flow.py"
        real_scan = engine.workflow_manager._process_workflows_for_registration

        async def scan_with_a_move(workflows_to_register: list[str]) -> object:
            moved_file.parent.mkdir(parents=True)
            workflow_file.rename(moved_file)
            engine.workflow_registry.set_file_path(workflow, moved_file)
            return await real_scan(workflows_to_register)

        with patch.object(engine.workflow_manager, "_process_workflows_for_registration", scan_with_a_move):
            _scan(engine, workspace)

        assert _id_at(engine, moved_file) == workflow.workflow_id

    def test_overlapping_refreshes_keep_each_others_finds(self, workspace: Path) -> None:
        engine = current_engine()
        engine.config_manager.workspace_path = workspace
        workflow_file = _write_workflow_file(workspace / "flow.py")
        _scan(engine, workspace)
        first_id = _id_at(engine, workflow_file)

        async def overlapping() -> None:
            await asyncio.gather(
                engine.workflow_manager.refresh_workflow_registry(workflows_to_register=[str(workspace)]),
                engine.workflow_manager.refresh_workflow_registry(workflows_to_register=[str(workspace)]),
            )

        asyncio.run(overlapping())

        assert _id_at(engine, workflow_file) == first_id

    def test_unsaved_workflows_survive_a_refresh(self, workspace: Path) -> None:
        engine = current_engine()
        engine.config_manager.workspace_path = workspace
        unsaved = engine.workflow_registry.register_unsaved("Scratch")

        _scan(engine, workspace)

        assert engine.workflow_registry.get(unsaved.workflow_id) is unsaved
        listing = _run(engine, ListAllWorkflowsRequest())
        assert isinstance(listing, ListAllWorkflowsResultSuccess)
        assert listing.workflows[unsaved.name]["workflow_id"] == unsaved.workflow_id


class TestMoveWorkflow:
    def test_move_onto_a_path_another_entry_holds_is_refused_before_the_file_moves(self, workspace: Path) -> None:
        engine = current_engine()
        engine.config_manager.workspace_path = workspace
        mover = _write_workflow_file(workspace / "a" / "foo.py")
        stale = _write_workflow_file(workspace / "b" / "foo.py")
        _scan(engine, workspace)
        mover_id = _id_at(engine, mover)
        assert mover_id is not None
        # Deleted outside the engine: its entry stays until the next scan.
        stale.unlink()

        moved = engine.handle_request(MoveWorkflowRequest(workflow_name="a/foo", target_directory="b"))

        assert isinstance(moved, MoveWorkflowResultFailure)
        assert "already registered" in str(moved.result_details)
        assert mover.is_file()
        assert not stale.exists()
        assert engine.workflow_registry.get(mover_id).absolute_path == mover

    def test_move_onto_a_name_another_entry_holds_is_refused_before_the_file_moves(self, workspace: Path) -> None:
        engine = current_engine()
        engine.config_manager.workspace_path = workspace
        mover = _write_workflow_file(workspace / "a" / "foo.py")
        # Another file in the target folder that gives the same name, "b/foo", at a different path.
        namesake = _write_workflow_file(workspace / "b" / "foo.json")
        _scan(engine, mover)
        engine.workflow_registry.register_file(namesake, read_workflow_metadata(namesake))
        mover_id = _id_at(engine, mover)
        assert mover_id is not None

        moved = engine.handle_request(MoveWorkflowRequest(workflow_name="a/foo", target_directory="b"))

        assert isinstance(moved, MoveWorkflowResultFailure)
        assert "already registered" in str(moved.result_details)
        assert mover.is_file()
        assert not (workspace / "b" / "foo.py").exists()
        assert engine.workflow_registry.get(mover_id).absolute_path == mover


class TestFirstSave:
    @pytest.mark.skipif(sys.platform.startswith("win"), reason="symlink creation needs privileges on Windows")
    def test_a_first_save_onto_a_file_another_entry_holds_through_a_link_writes_nothing(
        self, workspace: Path, tmp_path: Path
    ) -> None:
        engine = current_engine()
        engine.config_manager.workspace_path = workspace
        stale = _write_workflow_file(tmp_path / "elsewhere" / "sh010" / "comp.py")
        (workspace / "current").symlink_to(stale.parent, target_is_directory=True)
        _scan(engine, stale)
        # Deleted outside the engine: its entry stays until the next scan.
        stale.unlink()
        opened = engine.handle_request(EnsureWorkflowAndFlowRequest(display_name="Scratch"))
        assert isinstance(opened, EnsureWorkflowAndFlowResultSuccess)

        saved = _run(engine, SaveWorkflowRequest(file_name="current/comp"))

        assert isinstance(saved, SaveWorkflowResultFailure)
        assert "already registered" in str(saved.result_details)
        assert not stale.exists()
        assert engine.context_manager.get_current_workflow_name() == opened.workflow_name
        assert not engine.workflow_registry.get_workflow_by_name(opened.workflow_name).is_saved
