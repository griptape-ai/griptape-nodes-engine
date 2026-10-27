"""Workflow ids: minted by the registry, kept across save, rename, move and refresh, and never written to a file."""

from __future__ import annotations

import asyncio
import shutil
import sys
import uuid
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from griptape_nodes.bootstrap.workflow_executors.local_workflow_executor import LocalWorkflowExecutor
from griptape_nodes.common.project_templates.default_project_template import DEFAULT_PROJECT_TEMPLATE
from griptape_nodes.node_library.workflow_registry import (
    WorkflowMetadata,
    read_workflow_metadata,
)
from griptape_nodes.retained_mode.engine import current_engine, reset_root_engine
from griptape_nodes.retained_mode.events.app_events import (
    EngineHeartbeatRequest,
    EngineHeartbeatResultSuccess,
)
from griptape_nodes.retained_mode.events.context_events import (
    EnsureWorkflowAndFlowRequest,
    EnsureWorkflowAndFlowResultFailure,
    EnsureWorkflowAndFlowResultSuccess,
    GetWorkflowContextRequest,
    GetWorkflowContextSuccess,
    SetWorkflowContextFailure,
    SetWorkflowContextRequest,
)
from griptape_nodes.retained_mode.events.flow_events import (
    CreateFlowRequest,
    CreateFlowResultSuccess,
    GetTopLevelFlowRequest,
    GetTopLevelFlowResultSuccess,
)
from griptape_nodes.retained_mode.events.object_events import ClearAllObjectStateRequest
from griptape_nodes.retained_mode.events.project_events import (
    LoadProjectTemplateRequest,
    LoadProjectTemplateResultSuccess,
    SetCurrentProjectRequest,
)
from griptape_nodes.retained_mode.events.workflow_events import (
    BranchWorkflowRequest,
    BranchWorkflowResultFailure,
    BranchWorkflowResultSuccess,
    CreateWorkflowFromTemplateRequest,
    CreateWorkflowFromTemplateResultSuccess,
    DeleteWorkflowRequest,
    DeleteWorkflowResultSuccess,
    ImportWorkflowRequest,
    ImportWorkflowResultSuccess,
    ListAllWorkflowsRequest,
    ListAllWorkflowsResultSuccess,
    MergeWorkflowBranchRequest,
    MergeWorkflowBranchResultFailure,
    MergeWorkflowBranchResultSuccess,
    MoveWorkflowRequest,
    MoveWorkflowResultFailure,
    MoveWorkflowResultSuccess,
    RegisterWorkflowRequest,
    RegisterWorkflowResultSuccess,
    RenameWorkflowRequest,
    RenameWorkflowResultSuccess,
    ResetWorkflowBranchRequest,
    ResetWorkflowBranchResultSuccess,
    RunWorkflowFromRegistryRequest,
    RunWorkflowFromRegistryResultFailure,
    RunWorkflowFromRegistryResultSuccess,
    RunWorkflowFromScratchRequest,
    RunWorkflowFromScratchResultFailure,
    RunWorkflowFromScratchResultSuccess,
    RunWorkflowWithCurrentStateRequest,
    RunWorkflowWithCurrentStateResultFailure,
    SaveSubflowToWorkflowRequest,
    SaveSubflowToWorkflowResultSuccess,
    SaveWorkflowRequest,
    SaveWorkflowResultSuccess,
    SetWorkflowMetadataRequest,
    SetWorkflowMetadataResultFailure,
    SetWorkflowMetadataResultSuccess,
)
from griptape_nodes.retained_mode.managers.context_manager import ContextManager

if TYPE_CHECKING:
    from collections.abc import Generator

    from griptape_nodes.node_library.workflow_registry import Workflow
    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.events.base_events import ResultPayload


def _use_default_project(engine: Engine, workspace: Path) -> None:
    project_yml = workspace / "project_template.yml"
    project_yml.write_text(DEFAULT_PROJECT_TEMPLATE.to_overlay_yaml(DEFAULT_PROJECT_TEMPLATE))
    load_result = engine.handle_request(LoadProjectTemplateRequest(project_path=project_yml))
    assert isinstance(load_result, LoadProjectTemplateResultSuccess)
    engine.handle_request(SetCurrentProjectRequest(project_id=load_result.project_id))
    engine.config_manager.workspace_path = workspace


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """An empty workspace folder, links resolved so paths compare as the registry stores them."""
    workspace = tmp_path.resolve() / "workspace"
    workspace.mkdir()
    return workspace


@pytest.fixture
def project_engine(workspace: Path) -> Generator[Engine, None, None]:
    """The current engine on the default project, with `workspace` as its workspace."""
    engine = current_engine()
    _use_default_project(engine, workspace)
    yield engine
    current_engine().handle_request(SetCurrentProjectRequest(project_id=None))


def _run(engine: Engine, request: object) -> ResultPayload:
    return asyncio.run(engine.ahandle_request(request))  # type: ignore[arg-type]


def _open_new_workflow(engine: Engine, display_name: str) -> str:
    result = engine.handle_request(EnsureWorkflowAndFlowRequest(display_name=display_name))
    assert isinstance(result, EnsureWorkflowAndFlowResultSuccess), result.result_details
    assert result.workflow_id is not None
    return result.workflow_id


def _save(engine: Engine, request: SaveWorkflowRequest | None = None) -> SaveWorkflowResultSuccess:
    result = _run(engine, request or SaveWorkflowRequest())
    assert isinstance(result, SaveWorkflowResultSuccess), result.result_details
    return result


def _close(engine: Engine) -> None:
    engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))


def _saved_workflow(engine: Engine, display_name: str) -> SaveWorkflowResultSuccess:
    """Create, save and close a workflow; return its save result."""
    _open_new_workflow(engine, display_name)
    saved = _save(engine)
    _close(engine)
    return saved


def _listed(engine: Engine) -> dict[str, dict]:
    listing = _run(engine, ListAllWorkflowsRequest())
    assert isinstance(listing, ListAllWorkflowsResultSuccess)
    return listing.workflows


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


class TestWritePathsKeepOrMintAnId:
    """Every write path leaves the file holding a workflow: the open one under its id, or a copy with a new one."""

    def test_first_save_writes_the_unsaved_workflows_id(self, project_engine: Engine) -> None:
        workflow_id = _open_new_workflow(project_engine, "first")

        saved = _save(project_engine)

        assert saved.workflow_id == workflow_id
        assert _id_at(project_engine, saved.file_path) == workflow_id
        assert project_engine.context_manager.get_current_workflow_id() == workflow_id

    def test_saving_again_keeps_the_id(self, project_engine: Engine) -> None:
        workflow_id = _open_new_workflow(project_engine, "again")
        first = _save(project_engine)

        second = _save(project_engine, SaveWorkflowRequest(file_name=first.workflow_name))

        assert second.file_path == first.file_path
        assert second.workflow_id == workflow_id
        assert _id_at(project_engine, second.file_path) == workflow_id

    def test_save_as_writes_a_copy_with_a_new_id(self, project_engine: Engine) -> None:
        workflow_id = _open_new_workflow(project_engine, "original")
        _save(project_engine)

        copy = _save(project_engine, SaveWorkflowRequest(file_name="the_copy"))

        assert copy.workflow_id != workflow_id
        assert _id_at(project_engine, copy.file_path) == copy.workflow_id
        # The open workflow stays on the original.
        assert project_engine.context_manager.get_current_workflow_id() == workflow_id
        assert project_engine.workflow_registry.find_by_name("the_copy").workflow_id == copy.workflow_id  # type: ignore[union-attr]

    def test_versioned_save_writes_a_copy_with_a_new_id(self, project_engine: Engine) -> None:
        workflow_id = _open_new_workflow(project_engine, "versioned")
        _save(project_engine)

        version = _save(project_engine, SaveWorkflowRequest(create_versioned=True))

        assert version.workflow_id != workflow_id
        assert _id_at(project_engine, version.file_path) == version.workflow_id
        assert project_engine.context_manager.get_current_workflow_id() == workflow_id

    def test_first_save_onto_another_workflows_file_takes_it_over(self, project_engine: Engine) -> None:
        """The open workflow keeps its id; the workflow whose file it overwrote ends."""
        other = _saved_workflow(project_engine, "other")
        workflow_id = _open_new_workflow(project_engine, "unsaved")

        saved = _save(project_engine, SaveWorkflowRequest(file_name=other.workflow_name))

        assert saved.file_path == other.file_path
        assert saved.workflow_id == workflow_id
        assert _id_at(project_engine, saved.file_path) == workflow_id
        assert all(workflow["workflow_id"] != other.workflow_id for workflow in _listed(project_engine).values())
        assert project_engine.workflow_registry.find_by_name(other.workflow_name).workflow_id == workflow_id  # type: ignore[union-attr]

    def test_saving_over_another_saved_workflow_ends_its_identity(self, project_engine: Engine) -> None:
        """A saved workflow saved over another's file is a new workflow there; the open one stays itself."""
        other = _saved_workflow(project_engine, "other")
        workflow_id = _open_new_workflow(project_engine, "mine")
        _save(project_engine)

        written = _save(project_engine, SaveWorkflowRequest(file_name=other.workflow_name))

        assert written.workflow_id not in (workflow_id, other.workflow_id)
        assert _id_at(project_engine, other.file_path) == written.workflow_id
        assert all(workflow["workflow_id"] != other.workflow_id for workflow in _listed(project_engine).values())
        assert project_engine.context_manager.get_current_workflow_id() == workflow_id

    def test_saving_onto_a_library_workflow_writes_a_copy_with_a_new_id(self, project_engine: Engine) -> None:
        library = _saved_workflow(project_engine, "library_workflow")
        project_engine.workflow_registry.get(library.workflow_id).metadata.is_griptape_provided = True  # type: ignore[arg-type]
        workflow_id = _open_new_workflow(project_engine, "mine")
        _save(project_engine)

        copy = _save(project_engine, SaveWorkflowRequest(file_name=library.workflow_name))

        assert copy.workflow_id not in (workflow_id, library.workflow_id)
        assert Path(copy.file_path) != Path(library.file_path)
        assert _id_at(project_engine, copy.file_path) == copy.workflow_id
        assert _id_at(project_engine, library.file_path) == library.workflow_id
        # The open workflow stays itself: its identity never changes while it is open.
        assert project_engine.context_manager.get_current_workflow_id() == workflow_id

    def test_branch_merge_and_reset(self, project_engine: Engine) -> None:
        source = _saved_workflow(project_engine, "source")

        branch = project_engine.handle_request(BranchWorkflowRequest(workflow_name=source.workflow_name))
        assert isinstance(branch, BranchWorkflowResultSuccess), branch.result_details
        assert branch.original_workflow_id == source.workflow_id
        assert branch.branched_workflow_id not in (None, source.workflow_id)
        branch_entry = project_engine.workflow_registry.get(branch.branched_workflow_id)  # type: ignore[arg-type]
        assert _id_at(project_engine, branch_entry.absolute_path) == branch.branched_workflow_id  # type: ignore[arg-type]

        reset = project_engine.handle_request(ResetWorkflowBranchRequest(workflow_name=branch.branched_workflow_name))
        assert isinstance(reset, ResetWorkflowBranchResultSuccess), reset.result_details
        assert reset.workflow_id == branch.branched_workflow_id
        assert _id_at(project_engine, branch_entry.absolute_path) == branch.branched_workflow_id  # type: ignore[arg-type]

        merged = project_engine.handle_request(MergeWorkflowBranchRequest(workflow_name=branch.branched_workflow_name))
        assert isinstance(merged, MergeWorkflowBranchResultSuccess), merged.result_details
        assert merged.workflow_id == source.workflow_id
        assert _id_at(project_engine, source.file_path) == source.workflow_id

    @pytest.mark.parametrize("open_side", ["branch", "source"])
    def test_merging_with_either_side_open_is_refused_before_anything_is_written(
        self, project_engine: Engine, open_side: str
    ) -> None:
        source = _saved_workflow(project_engine, "source")
        branch = project_engine.handle_request(BranchWorkflowRequest(workflow_name=source.workflow_name))
        assert isinstance(branch, BranchWorkflowResultSuccess), branch.result_details
        branch_entry = project_engine.workflow_registry.get(branch.branched_workflow_id)  # type: ignore[arg-type]
        branch_path = branch_entry.absolute_path
        assert branch_path is not None
        source_content = Path(source.file_path).read_text(encoding="utf-8")
        branch_content = branch_path.read_text(encoding="utf-8")
        if open_side == "branch":
            open_entry = branch_entry
        else:
            open_entry = project_engine.workflow_registry.get(source.workflow_id)  # type: ignore[arg-type]
        project_engine.context_manager.push_workflow(open_entry.name)

        merged = project_engine.handle_request(MergeWorkflowBranchRequest(workflow_name=branch.branched_workflow_name))

        assert isinstance(merged, MergeWorkflowBranchResultFailure)
        assert "one of them is open" in str(merged.result_details)
        assert Path(source.file_path).read_text(encoding="utf-8") == source_content
        assert branch_path.read_text(encoding="utf-8") == branch_content
        assert project_engine.workflow_registry.get(branch_entry.workflow_id) is branch_entry
        assert project_engine.context_manager.get_current_workflow_id() == open_entry.workflow_id

    def test_create_from_template_gets_a_new_id(self, project_engine: Engine) -> None:
        template = _saved_workflow(project_engine, "template")
        project_engine.workflow_registry.get(template.workflow_id).metadata.is_template = True  # type: ignore[arg-type]

        created = project_engine.handle_request(CreateWorkflowFromTemplateRequest(template_name=template.workflow_name))

        assert isinstance(created, CreateWorkflowFromTemplateResultSuccess), created.result_details
        assert created.workflow_id not in (None, template.workflow_id)
        assert _id_at(project_engine, created.file_path) == created.workflow_id

    def test_set_metadata_keeps_the_id_and_ignores_one_it_is_sent(self, project_engine: Engine) -> None:
        saved = _saved_workflow(project_engine, "described")

        updated = _run(
            project_engine,
            SetWorkflowMetadataRequest(
                workflow_name=saved.workflow_name,
                workflow_metadata={"description": "now described"},  # type: ignore[arg-type]
            ),
        )
        ignored = _run(
            project_engine,
            SetWorkflowMetadataRequest(
                workflow_name=saved.workflow_name,
                workflow_metadata={"workflow_id": "someone_else"},  # type: ignore[arg-type]
            ),
        )

        assert isinstance(updated, SetWorkflowMetadataResultSuccess), updated.result_details
        assert read_workflow_metadata(Path(saved.file_path)).description == "now described"
        assert _id_at(project_engine, saved.file_path) == saved.workflow_id
        assert isinstance(ignored, SetWorkflowMetadataResultSuccess), ignored.result_details
        assert _id_at(project_engine, saved.file_path) == saved.workflow_id
        assert project_engine.workflow_registry.find_by_id("someone_else") is None

    def test_move_keeps_the_id(self, project_engine: Engine, workspace: Path) -> None:
        saved = _saved_workflow(project_engine, "mover")

        moved = project_engine.handle_request(
            MoveWorkflowRequest(workflow_name=saved.workflow_name, target_directory="elsewhere")
        )

        assert isinstance(moved, MoveWorkflowResultSuccess), moved.result_details
        assert moved.workflow_id == saved.workflow_id
        assert _id_at(project_engine, workspace / moved.moved_file_path) == saved.workflow_id

    def test_rename_of_the_open_workflow_keeps_its_id_and_stays_open(self, project_engine: Engine) -> None:
        workflow_id = _open_new_workflow(project_engine, "before")
        saved = _save(project_engine)
        flow_name = project_engine.context_manager.get_current_flow().name

        renamed = _run(project_engine, RenameWorkflowRequest(workflow_name=saved.workflow_name, requested_name="after"))

        assert isinstance(renamed, RenameWorkflowResultSuccess), renamed.result_details
        assert renamed.workflow_id == workflow_id
        assert renamed.new_workflow_name == "after"
        assert not Path(saved.file_path).exists()
        entry = project_engine.workflow_registry.get(workflow_id)
        assert entry.absolute_path is not None
        assert _id_at(project_engine, entry.absolute_path) == workflow_id
        # Still open, graph and all: renaming does not close the workflow.
        assert project_engine.context_manager.get_current_workflow_id() == workflow_id
        assert project_engine.context_manager.get_current_workflow_name() == "after"
        assert project_engine.context_manager.get_current_flow().name == flow_name

    def test_save_subflow_keeps_the_parent_id(self, project_engine: Engine) -> None:
        workflow_id = _open_new_workflow(project_engine, "parent")
        saved = _save(project_engine)
        top = project_engine.handle_request(GetTopLevelFlowRequest())
        assert isinstance(top, GetTopLevelFlowResultSuccess)
        subflow = project_engine.handle_request(
            CreateFlowRequest(parent_flow_name=top.flow_name, flow_name="Sub", set_as_new_context=False)
        )
        assert isinstance(subflow, CreateFlowResultSuccess), subflow.result_details

        result = _run(project_engine, SaveSubflowToWorkflowRequest(flow_name="Sub", workflow_name=saved.workflow_name))

        assert isinstance(result, SaveSubflowToWorkflowResultSuccess), result.result_details
        assert result.workflow_metadata == project_engine.workflow_registry.get(workflow_id).metadata
        assert _id_at(project_engine, saved.file_path) == workflow_id


class TestPlaceholderMetadataIsNeverWrittenBack:
    """A file whose header could not be read opens with placeholder metadata, which no write path copies."""

    def _open_damaged_file(self, engine: Engine, workspace: Path) -> Path:
        workflow_file = workspace / "damaged.py"
        workflow_file.write_text("print('no header')\n", encoding="utf-8")
        engine.context_manager.push_workflow(file_path=str(workflow_file))
        assert engine.context_manager.get_current_workflow().metadata_is_placeholder
        return workflow_file

    def test_branch_and_set_metadata_fail_without_touching_files(self, project_engine: Engine, workspace: Path) -> None:
        workflow_file = self._open_damaged_file(project_engine, workspace)
        workflow = project_engine.context_manager.get_current_workflow()
        files_before = sorted(workspace.rglob("*"))

        branched = project_engine.handle_request(BranchWorkflowRequest(workflow_name=workflow.name))
        updated = _run(
            project_engine,
            SetWorkflowMetadataRequest(workflow_name=workflow.name, workflow_metadata={"description": "x"}),  # type: ignore[arg-type]
        )

        assert isinstance(branched, BranchWorkflowResultFailure)
        assert "could not be read" in str(branched.result_details)
        assert isinstance(updated, SetWorkflowMetadataResultFailure)
        assert "could not be read" in str(updated.result_details)
        assert workflow_file.read_text(encoding="utf-8") == "print('no header')\n"
        assert sorted(workspace.rglob("*")) == files_before

    def test_a_save_writes_real_metadata_and_clears_the_flag(self, project_engine: Engine, workspace: Path) -> None:
        workflow_file = self._open_damaged_file(project_engine, workspace)
        workflow = project_engine.context_manager.get_current_workflow()
        created = project_engine.handle_request(CreateFlowRequest(parent_flow_name=None, set_as_new_context=True))
        assert isinstance(created, CreateFlowResultSuccess), created.result_details

        saved = _save(project_engine, SaveWorkflowRequest(file_name=workflow.name))

        assert Path(saved.file_path) == workflow_file
        assert saved.workflow_id == workflow.workflow_id
        assert not workflow.metadata_is_placeholder
        assert read_workflow_metadata(workflow_file).name == "damaged"


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

    def test_a_project_switch_keeps_the_ids_of_the_workflows_it_finds_again(self, workspace: Path) -> None:
        """The switch closes the open workflow and refreshes the registry for the new workspace."""
        engine = current_engine()
        engine.config_manager.workspace_path = workspace
        opened_file = _write_workflow_file(workspace / "shots" / "opened.py")
        closed_file = _write_workflow_file(workspace / "shots" / "closed.py")
        _scan(engine, workspace)
        closed_id = _id_at(engine, closed_file)
        engine.context_manager.push_workflow(file_path=str(opened_file))
        opened_id = engine.context_manager.get_current_workflow_id()

        engine.config_manager.workspace_path = workspace / "shots"
        failure = asyncio.run(
            engine.project_manager._reload_after_project_switch(
                "shots", workspace_changed=True, library_config_changed=False
            )
        )

        assert failure is None
        assert not engine.context_manager.has_current_workflow()
        assert _id_at(engine, opened_file) == opened_id
        assert _id_at(engine, closed_file) == closed_id
        assert engine.workflow_registry.get(closed_id or "").name == "closed"

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

    def test_a_workflow_deleted_through_the_engine_and_recreated_gets_a_new_id(self, project_engine: Engine) -> None:
        saved = _saved_workflow(project_engine, "again")
        deleted = _run(project_engine, DeleteWorkflowRequest(name=saved.workflow_name))
        assert isinstance(deleted, DeleteWorkflowResultSuccess), deleted.result_details

        recreated = _write_workflow_file(Path(saved.file_path))
        _scan(project_engine, recreated.parent)

        assert project_engine.workflow_registry.find_by_id(saved.workflow_id or "") is None
        assert _id_at(project_engine, recreated) not in (None, saved.workflow_id)

    def test_the_open_workflow_keeps_its_id_through_a_refresh(self, workspace: Path) -> None:
        engine = current_engine()
        engine.config_manager.workspace_path = workspace
        opened_file = _write_workflow_file(workspace / "opened.py")
        engine.context_manager.push_workflow(file_path=str(opened_file))
        opened_id = engine.context_manager.get_current_workflow_id()

        _scan(engine, workspace)

        assert engine.context_manager.get_current_workflow_id() == opened_id
        assert _id_at(engine, opened_file) == opened_id

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

    def test_a_rescan_keeps_the_open_workflows_metadata(self, workspace: Path) -> None:
        engine = current_engine()
        engine.config_manager.workspace_path = workspace
        workflow_file = _write_workflow_file(workspace / "flow.py", name="Before")
        engine.context_manager.push_workflow(file_path=str(workflow_file))
        opened = engine.workflow_registry.find_by_path(workflow_file)
        assert opened is not None
        in_memory = opened.metadata
        _write_workflow_file(workflow_file, name="After")

        _scan(engine, workspace)

        assert engine.workflow_registry.find_by_path(workflow_file) is opened
        assert opened.metadata is in_memory
        assert opened.listed

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


class TestRegisterWorkflow:
    def test_registering_a_file_that_was_only_opened_lists_it(self, workspace: Path, tmp_path: Path) -> None:
        engine = current_engine()
        engine.config_manager.workspace_path = workspace
        # Outside the workspace, so opening it registers it unlisted.
        workflow_file = _write_workflow_file(tmp_path / "elsewhere" / "flow.py")
        engine.context_manager.push_workflow(file_path=str(workflow_file))
        engine.context_manager.pop_workflow()
        opened_id = _id_at(engine, workflow_file)
        assert opened_id is not None
        assert all(workflow["workflow_id"] != opened_id for workflow in _listed(engine).values())

        registered = engine.handle_request(
            RegisterWorkflowRequest(metadata=read_workflow_metadata(workflow_file), file_name=str(workflow_file))
        )

        assert isinstance(registered, RegisterWorkflowResultSuccess), registered.result_details
        assert registered.workflow_id == opened_id
        assert any(workflow["workflow_id"] == opened_id for workflow in _listed(engine).values())

    def test_importing_a_file_that_was_only_opened_lists_it(self, workspace: Path, tmp_path: Path) -> None:
        engine = current_engine()
        engine.config_manager.workspace_path = workspace
        workflow_file = _write_workflow_file(tmp_path / "elsewhere" / "flow.py")
        engine.context_manager.push_workflow(file_path=str(workflow_file))
        engine.context_manager.pop_workflow()
        opened_id = _id_at(engine, workflow_file)
        assert opened_id is not None

        imported = _run(engine, ImportWorkflowRequest(file_path=str(workflow_file)))

        assert isinstance(imported, ImportWorkflowResultSuccess), imported.result_details
        assert imported.workflow_id == opened_id
        assert any(workflow["workflow_id"] == opened_id for workflow in _listed(engine).values())


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
    def test_a_first_save_onto_a_file_a_stale_entry_holds_through_a_link_replaces_the_entry(
        self, workspace: Path, tmp_path: Path
    ) -> None:
        engine = current_engine()
        engine.config_manager.workspace_path = workspace
        stale = _write_workflow_file(tmp_path / "elsewhere" / "sh010" / "comp.py")
        (workspace / "current").symlink_to(stale.parent, target_is_directory=True)
        _scan(engine, stale)
        stale_id = _id_at(engine, stale)
        assert stale_id is not None
        # Deleted outside the engine: its entry stays until the next scan.
        stale.unlink()
        opened = engine.handle_request(EnsureWorkflowAndFlowRequest(display_name="Scratch"))
        assert isinstance(opened, EnsureWorkflowAndFlowResultSuccess)

        saved = _run(engine, SaveWorkflowRequest(file_name="current/comp"))

        assert isinstance(saved, SaveWorkflowResultSuccess), saved.result_details
        assert saved.workflow_id == opened.workflow_id
        assert stale.is_file()
        assert engine.workflow_registry.find_by_id(stale_id) is None

    def test_a_description_and_image_set_before_the_first_save_survive_it(self, project_engine: Engine) -> None:
        """The display name still follows the file a first save names, as it always has."""
        workflow_id = _open_new_workflow(project_engine, "Shot 10")
        edited = _run(
            project_engine,
            SetWorkflowMetadataRequest(
                workflow_name=project_engine.workflow_registry.get(workflow_id).name,
                workflow_metadata={"description": "Comp for shot 10.", "image": "shot_10.webp"},  # type: ignore[arg-type]
            ),
        )
        assert isinstance(edited, SetWorkflowMetadataResultSuccess), edited.result_details

        saved = _save(project_engine, SaveWorkflowRequest(file_name="shot_010"))

        written = read_workflow_metadata(Path(saved.file_path))
        assert saved.workflow_id == workflow_id
        assert written.name == "shot_010"
        assert written.description == "Comp for shot 10."
        assert written.image == "shot_10.webp"


class TestRenamingTheOpenWorkflow:
    def test_renaming_keeps_its_description_and_image(self, project_engine: Engine) -> None:
        workflow_id = _open_new_workflow(project_engine, "Shot 10")
        saved = _save(project_engine, SaveWorkflowRequest(file_name="shot_010"))
        edited = _run(
            project_engine,
            SetWorkflowMetadataRequest(
                workflow_name=saved.workflow_name,
                workflow_metadata={"description": "Comp for shot 10.", "image": "shot_10.webp"},  # type: ignore[arg-type]
            ),
        )
        assert isinstance(edited, SetWorkflowMetadataResultSuccess), edited.result_details

        renamed = _run(
            project_engine, RenameWorkflowRequest(workflow_name=saved.workflow_name, requested_name="shot_011")
        )

        assert isinstance(renamed, RenameWorkflowResultSuccess), renamed.result_details
        renamed_file = project_engine.workflow_registry.get(workflow_id).absolute_path
        assert renamed_file is not None
        written = read_workflow_metadata(renamed_file)
        assert written.name == "shot_011"
        assert written.description == "Comp for shot 10."
        assert written.image == "shot_10.webp"


class TestIdsOnResults:
    def test_context_and_heartbeat_report_the_id(self) -> None:
        engine = current_engine()
        workflow_id = _open_new_workflow(engine, "reported")

        context = engine.handle_request(GetWorkflowContextRequest())
        heartbeat = engine.handle_request(EngineHeartbeatRequest(heartbeat_id="beat"))

        assert isinstance(context, GetWorkflowContextSuccess)
        assert context.workflow_id == workflow_id
        assert isinstance(heartbeat, EngineHeartbeatResultSuccess)
        assert heartbeat.current_workflow_id == workflow_id


class TestClosingAnUnsavedWorkflow:
    """An unsaved workflow's nodes live only while it is open, so closing it removes its entry."""

    def test_closing_forgets_it(self, project_engine: Engine) -> None:
        workflow_id = _open_new_workflow(project_engine, "scratch")
        name = project_engine.workflow_registry.get(workflow_id).name

        _close(project_engine)

        assert project_engine.workflow_registry.find_by_id(workflow_id) is None
        assert name not in _listed(project_engine)

    def test_deleting_it_while_open_succeeds(self, project_engine: Engine) -> None:
        workflow_id = _open_new_workflow(project_engine, "scratch")
        name = project_engine.workflow_registry.get(workflow_id).name

        deleted = _run(project_engine, DeleteWorkflowRequest(name=name))

        assert isinstance(deleted, DeleteWorkflowResultSuccess), deleted.result_details
        assert not project_engine.context_manager.has_current_workflow()
        assert project_engine.workflow_registry.find_by_id(workflow_id) is None


class TestIdentityAcrossTheWorkflowLifecycle:
    """One workflow keeps one id through create, save, rename, move, reopen and refresh, until the engine stops."""

    def test_the_id_never_changes(self, workspace: Path) -> None:
        engine = current_engine()
        _use_default_project(engine, workspace)
        workflow_id = _open_new_workflow(engine, "lifecycle")

        saved = _save(engine)
        assert saved.workflow_id == workflow_id

        renamed = _run(engine, RenameWorkflowRequest(workflow_name=saved.workflow_name, requested_name="renamed"))
        assert isinstance(renamed, RenameWorkflowResultSuccess), renamed.result_details
        assert renamed.workflow_id == workflow_id
        _close(engine)

        moved = engine.handle_request(MoveWorkflowRequest(workflow_name="renamed", target_directory="shots"))
        assert isinstance(moved, MoveWorkflowResultSuccess), moved.result_details
        assert moved.workflow_id == workflow_id

        reopened = _run(engine, RunWorkflowFromRegistryRequest(workflow_name=moved.new_workflow_name))
        assert isinstance(reopened, RunWorkflowFromRegistryResultSuccess), reopened.result_details
        assert reopened.workflow_id == workflow_id
        assert engine.context_manager.get_current_workflow_id() == workflow_id
        _close(engine)

        _scan(engine, workspace)
        assert engine.workflow_registry.get(workflow_id).name == "shots/renamed"
        engine.handle_request(SetCurrentProjectRequest(project_id=None))

        # Restart: a new engine finds the workflow by scanning, and gives it an id of its own,
        # which then lasts while that engine runs.
        reset_root_engine()
        restarted = current_engine()
        _use_default_project(restarted, workspace)
        _scan(restarted, workspace)
        found = restarted.workflow_registry.find_by_name("shots/renamed")
        assert found is not None
        assert found.workflow_id != workflow_id
        assert restarted.workflow_registry.find_by_id(workflow_id) is None

        _scan(restarted, workspace)
        assert restarted.workflow_registry.get(found.workflow_id).name == "shots/renamed"
        restarted.handle_request(SetCurrentProjectRequest(project_id=None))


class TestWorkflowsDoNotNest:
    """Only one workflow is open at a time, so opening another one on top of it is refused."""

    def test_pushing_a_workflow_while_one_is_open_is_refused(self, project_engine: Engine) -> None:
        other = _saved_workflow(project_engine, "other")
        open_id = _open_new_workflow(project_engine, "open")

        with pytest.raises(ContextManager.WorkflowAlreadyOpenError, match="is already open"):
            project_engine.context_manager.push_workflow(other.workflow_name)

        assert project_engine.context_manager.get_current_workflow_id() == open_id
        assert not project_engine.workflow_registry.is_open(other.workflow_id or "")

    def test_a_run_without_a_clean_slate_while_one_is_open_is_refused(self, project_engine: Engine) -> None:
        other = _saved_workflow(project_engine, "other")
        open_id = _open_new_workflow(project_engine, "open")
        registered_before = set(project_engine.workflow_registry._by_id)

        refused = _run(
            project_engine,
            RunWorkflowFromRegistryRequest(workflow_name=other.workflow_name, run_with_clean_slate=False),
        )

        assert isinstance(refused, RunWorkflowFromRegistryResultFailure)
        assert "Clear the open workflow first" in str(refused.result_details)
        assert project_engine.context_manager.get_current_workflow_id() == open_id
        assert set(project_engine.workflow_registry._by_id) == registered_before


class TestASavedFileOpensTheWorkflowItLoads:
    """A saved file pushes itself only when no workflow is current (`codegen.py`'s guard)."""

    @pytest.mark.parametrize("load", ["from_registry", "from_scratch"])
    def test_a_load_bound_to_the_workflow_it_replaces_builds_into_the_one_it_opens(
        self, project_engine: Engine, load: str
    ) -> None:
        saved = _saved_workflow(project_engine, "saved")
        replaced_id = _open_new_workflow(project_engine, "replaced")
        replaced = project_engine.current_workflow()
        if load == "from_registry":
            request: object = RunWorkflowFromRegistryRequest(workflow_name=saved.workflow_name)
        else:
            request = RunWorkflowFromScratchRequest(file_path=saved.file_path)

        loaded = asyncio.run(project_engine.event_manager.ahandle_request(request, workflow_id=replaced_id))  # type: ignore[arg-type]

        assert loaded.result.succeeded(), loaded.result.result_details
        opened = project_engine.current_workflow()
        assert opened is not replaced
        assert opened.workflow_id == saved.workflow_id
        assert project_engine.context_manager.has_current_flow()
        assert len(opened.objects) > 0
        assert len(replaced.objects) == 0


def _library_workflow(engine: Engine, library_dir: Path) -> Workflow:
    """Register a workflow a library provides, at `library_dir/collage_poster.py`, outside the workspace.

    Its display name differs from its file's stem, so a test can tell which one a save is named from.
    """
    saved = _saved_workflow(engine, "collage")
    source_file = Path(saved.file_path)
    library_metadata = read_workflow_metadata(source_file).model_copy(
        update={
            "name": "Poster Collage",
            "description": "Lays out a poster.",
            "image": "thumbnail.webp",
            "is_griptape_provided": True,
            "is_template": True,
        }
    )
    library_content = engine.workflow_manager.codegen.replace_workflow_metadata_header(
        source_file.read_text(encoding="utf-8"), library_metadata
    )
    assert library_content is not None
    library_file = library_dir / "collage_poster.py"
    library_file.parent.mkdir(parents=True, exist_ok=True)
    library_file.write_text(library_content, encoding="utf-8")
    return engine.workflow_registry.register_file(library_file, library_metadata)


def _open_library_workflow(engine: Engine, library: Workflow) -> str:
    """Open `library` from the registry, as the editor does; return the id of the workflow that opened."""
    opened = _run(engine, RunWorkflowFromRegistryRequest(workflow_name=library.name))
    assert isinstance(opened, RunWorkflowFromRegistryResultSuccess), opened.result_details
    assert opened.workflow_id is not None
    return opened.workflow_id


def _copies_of(engine: Engine, library: Workflow) -> list[Workflow]:
    """The registry's unsaved copies of `library`."""
    registry = engine.workflow_registry
    entries = [registry.get(workflow_id) for workflow_id in registry.workflow_ids()]
    return [entry for entry in entries if entry.seeded_from_workflow_id == library.workflow_id]


def _make_library_file_fail_after_building(library: Workflow) -> Path:
    """Make `library`'s file raise once its graph is built, so the copy it opens is half built; return the file."""
    library_file = library.absolute_path
    assert library_file is not None
    library_file.write_text(
        library_file.read_text(encoding="utf-8")
        + "\n_build_library_workflow = build_workflow\n\n"
        + "async def build_workflow():\n"
        + "    await _build_library_workflow()\n"
        + "    raise RuntimeError('The library workflow is broken.')\n",
        encoding="utf-8",
    )
    return library_file


class TestLibraryWorkflowsOpenAsUnsavedCopies:
    """Opening a workflow a library provides opens a new unsaved copy; the library's entry is never open."""

    def test_run_from_registry_opens_an_unsaved_copy(self, project_engine: Engine, tmp_path: Path) -> None:
        library = _library_workflow(project_engine, tmp_path / "library")
        library_file = library.absolute_path
        assert library_file is not None
        library_content = library_file.read_bytes()

        copy_id = _open_library_workflow(project_engine, library)

        assert copy_id != library.workflow_id
        assert project_engine.context_manager.get_current_workflow_id() == copy_id
        assert project_engine.context_manager.has_current_flow()
        copy = project_engine.workflow_registry.get(copy_id)
        assert not copy.is_saved
        assert copy.seeded_from_workflow_id == library.workflow_id
        assert copy.metadata.name == "Poster Collage"
        assert copy.metadata.description == "Lays out a poster."
        assert copy.metadata.image == "thumbnail.webp"
        assert copy.metadata.is_griptape_provided is False
        assert copy.metadata.is_template is False
        # The library's entry and file are untouched, and it is not the open workflow.
        assert not project_engine.workflow_registry.is_open(library.workflow_id)
        assert project_engine.workflow_registry.get(library.workflow_id) is library
        assert library.absolute_path == library_file
        assert library.metadata.is_griptape_provided is True
        assert library_file.read_bytes() == library_content

    def test_a_failed_open_leaves_no_copy_behind(self, project_engine: Engine, tmp_path: Path) -> None:
        # A file with only a header builds no flow, so running it fails.
        library_file = _write_workflow_file(tmp_path / "library" / "broken.py")
        library = project_engine.workflow_registry.register_file(library_file, read_workflow_metadata(library_file))
        library.metadata.is_griptape_provided = True
        listed_before = set(_listed(project_engine))

        failed = _run(project_engine, RunWorkflowFromRegistryRequest(workflow_name=library.name))

        assert isinstance(failed, RunWorkflowFromRegistryResultFailure)
        assert not project_engine.context_manager.has_current_workflow()
        assert set(_listed(project_engine)) == listed_before

    def test_run_from_scratch_on_a_library_file_opens_a_copy(self, project_engine: Engine, tmp_path: Path) -> None:
        """The file pushes itself with `push_workflow(file_path=__file__)`, which opens a copy of a library's file."""
        library = _library_workflow(project_engine, tmp_path / "library")

        opened = _run(project_engine, RunWorkflowFromScratchRequest(file_path=str(library.absolute_path)))

        assert isinstance(opened, RunWorkflowFromScratchResultSuccess), opened.result_details
        assert opened.workflow_id not in (None, library.workflow_id)
        assert project_engine.context_manager.get_current_workflow_id() == opened.workflow_id
        assert project_engine.workflow_registry.get(opened.workflow_id).seeded_from_workflow_id == library.workflow_id  # type: ignore[arg-type]
        assert not project_engine.workflow_registry.is_open(library.workflow_id)

    def test_a_failed_run_from_scratch_on_a_library_file_leaves_no_copy_behind(
        self, project_engine: Engine, tmp_path: Path
    ) -> None:
        library = _library_workflow(project_engine, tmp_path / "library")
        library_file = _make_library_file_fail_after_building(library)
        listed_before = set(_listed(project_engine))

        failed = _run(project_engine, RunWorkflowFromScratchRequest(file_path=str(library_file)))

        assert isinstance(failed, RunWorkflowFromScratchResultFailure)
        assert "The library workflow is broken." in str(failed.result_details)
        assert not project_engine.context_manager.has_current_workflow()
        assert set(_listed(project_engine)) == listed_before
        assert _copies_of(project_engine, library) == []

    def test_a_failed_run_with_current_state_on_a_library_file_leaves_no_copy_behind(
        self, project_engine: Engine, tmp_path: Path
    ) -> None:
        library = _library_workflow(project_engine, tmp_path / "library")
        library_file = _make_library_file_fail_after_building(library)

        failed = _run(project_engine, RunWorkflowWithCurrentStateRequest(file_path=str(library_file)))

        assert isinstance(failed, RunWorkflowWithCurrentStateResultFailure)
        assert "The library workflow is broken." in str(failed.result_details)
        assert not project_engine.context_manager.has_current_workflow()
        assert _copies_of(project_engine, library) == []

    def test_a_copy_is_unlisted_until_its_first_save(self, project_engine: Engine, tmp_path: Path) -> None:
        """The workflow list shows the library workflow once, not a second entry for the open copy."""
        library = _library_workflow(project_engine, tmp_path / "library")
        listed_before = set(_listed(project_engine))
        copy_id = _open_library_workflow(project_engine, library)

        listed_while_open = set(_listed(project_engine))
        saved = _save(project_engine)

        assert listed_while_open == listed_before
        assert saved.workflow_id == copy_id
        assert set(_listed(project_engine)) == listed_before | {saved.workflow_name}

    def test_the_first_save_of_a_copy_keeps_its_name_description_and_image(
        self, project_engine: Engine, tmp_path: Path
    ) -> None:
        library = _library_workflow(project_engine, tmp_path / "library")
        _open_library_workflow(project_engine, library)

        saved = _save(project_engine)

        written = read_workflow_metadata(Path(saved.file_path))
        assert written.name == "Poster Collage"
        assert written.description == "Lays out a poster."
        assert written.image == "thumbnail.webp"
        assert project_engine.context_manager.get_current_workflow().metadata.name == "Poster Collage"

    def test_the_first_save_of_a_copy_keeps_a_description_and_image_set_before_it(
        self, project_engine: Engine, tmp_path: Path
    ) -> None:
        library = _library_workflow(project_engine, tmp_path / "library")
        copy_id = _open_library_workflow(project_engine, library)
        edited = _run(
            project_engine,
            SetWorkflowMetadataRequest(
                workflow_name=project_engine.workflow_registry.get(copy_id).name,
                workflow_metadata={"description": "My poster.", "image": "mine.webp"},  # type: ignore[arg-type]
            ),
        )
        assert isinstance(edited, SetWorkflowMetadataResultSuccess), edited.result_details

        saved = _save(project_engine)

        written = read_workflow_metadata(Path(saved.file_path))
        assert written.name == "Poster Collage"
        assert written.description == "My poster."
        assert written.image == "mine.webp"

    def test_closing_an_unsaved_copy_forgets_it(self, project_engine: Engine, tmp_path: Path) -> None:
        library = _library_workflow(project_engine, tmp_path / "library")
        listed_before = set(_listed(project_engine))
        copy_id = _open_library_workflow(project_engine, library)

        _close(project_engine)

        assert project_engine.workflow_registry.find_by_id(copy_id) is None
        assert set(_listed(project_engine)) == listed_before
        assert project_engine.workflow_registry.get(library.workflow_id) is library

    def test_closing_a_saved_copy_keeps_it(self, project_engine: Engine, tmp_path: Path) -> None:
        library = _library_workflow(project_engine, tmp_path / "library")
        copy_id = _open_library_workflow(project_engine, library)
        _save(project_engine)

        _close(project_engine)

        assert project_engine.workflow_registry.get(copy_id).is_saved

    def test_pushing_a_library_workflow_by_name_is_refused(self, project_engine: Engine, tmp_path: Path) -> None:
        library = _library_workflow(project_engine, tmp_path / "library")

        with pytest.raises(ContextManager.LibraryWorkflowOpenError, match="a library provides it"):
            project_engine.context_manager.push_workflow(library.name)

        assert not project_engine.context_manager.has_current_workflow()
        assert not project_engine.workflow_registry.is_open(library.workflow_id)

    def test_set_workflow_context_and_ensure_refuse_a_library_workflow(
        self, project_engine: Engine, tmp_path: Path
    ) -> None:
        library = _library_workflow(project_engine, tmp_path / "library")

        entered = project_engine.handle_request(SetWorkflowContextRequest(workflow_name=library.name))
        ensured = project_engine.handle_request(EnsureWorkflowAndFlowRequest(workflow_name=library.name))

        assert isinstance(entered, SetWorkflowContextFailure)
        assert "a library provides it" in str(entered.result_details)
        assert isinstance(ensured, EnsureWorkflowAndFlowResultFailure)
        assert "a library provides it" in str(ensured.result_details)
        assert not project_engine.context_manager.has_current_workflow()

    def test_rename_of_an_open_copy_saves_the_copy_and_leaves_the_library_alone(
        self, project_engine: Engine, tmp_path: Path, workspace: Path
    ) -> None:
        library = _library_workflow(project_engine, tmp_path / "library")
        library_file = library.absolute_path
        assert library_file is not None
        library_content = library_file.read_bytes()
        copy_id = _open_library_workflow(project_engine, library)

        copy_name = project_engine.workflow_registry.get(copy_id).name
        renamed = _run(project_engine, RenameWorkflowRequest(workflow_name=copy_name, requested_name="mine"))

        assert isinstance(renamed, RenameWorkflowResultSuccess), renamed.result_details
        assert renamed.workflow_id == copy_id
        assert _id_at(project_engine, workspace / "mine.py") == copy_id
        assert project_engine.context_manager.get_current_workflow_id() == copy_id
        assert library_file.read_bytes() == library_content
        assert project_engine.workflow_registry.get(library.workflow_id).absolute_path == library_file


class TestFirstSaveOfALibraryWorkflowCopy:
    """A copy's first save is an unsaved workflow's first save, named after the library's file."""

    def test_first_save_writes_a_new_file_named_after_the_library_file(
        self, project_engine: Engine, tmp_path: Path, workspace: Path
    ) -> None:
        library = _library_workflow(project_engine, tmp_path / "library")
        library_file = library.absolute_path
        assert library_file is not None
        library_content = library_file.read_bytes()
        copy_id = _open_library_workflow(project_engine, library)

        saved = _save(project_engine)

        assert Path(saved.file_path) == workspace / "collage_poster.py"
        assert saved.workflow_id == copy_id
        assert _id_at(project_engine, saved.file_path) == copy_id
        # Named after the library's file, the copy keeps the library workflow's display name.
        written = read_workflow_metadata(Path(saved.file_path))
        assert written.name == "Poster Collage"
        assert written.description == "Lays out a poster."
        assert written.image == "thumbnail.webp"
        assert written.is_griptape_provided is False
        # The copy stays the open workflow, now saved and no longer a copy; the library is untouched.
        assert project_engine.context_manager.get_current_workflow_id() == copy_id
        saved_workflow = project_engine.context_manager.get_current_workflow()
        assert saved_workflow.absolute_path == Path(saved.file_path)
        assert saved_workflow.seeded_from_workflow_id is None
        assert library_file.read_bytes() == library_content
        assert project_engine.workflow_registry.get(library.workflow_id).absolute_path == library_file

    def test_the_first_save_of_a_second_copy_never_overwrites_the_first(
        self, project_engine: Engine, tmp_path: Path, workspace: Path
    ) -> None:
        library = _library_workflow(project_engine, tmp_path / "library")
        _open_library_workflow(project_engine, library)
        first = _save(project_engine)
        _open_library_workflow(project_engine, library)

        second = _save(project_engine)

        assert Path(first.file_path) == workspace / "collage_poster.py"
        assert Path(second.file_path) == workspace / "collage_poster_1.py"
        assert _id_at(project_engine, first.file_path) == first.workflow_id
        assert second.workflow_id != first.workflow_id

    def test_a_versioned_first_save_is_named_after_the_library_file(
        self, project_engine: Engine, tmp_path: Path
    ) -> None:
        library = _library_workflow(project_engine, tmp_path / "library")
        copy_id = _open_library_workflow(project_engine, library)

        saved = _save(project_engine, SaveWorkflowRequest(create_versioned=True))

        assert Path(saved.file_path).name.startswith("collage_poster")
        assert saved.workflow_id == copy_id

    def test_a_failed_script_run_of_a_library_file_is_saved_under_the_library_file_name(
        self, project_engine: Engine, tmp_path: Path, workspace: Path
    ) -> None:
        """The copy's registry name holds a colon, which no file name on Windows may."""
        library = _library_workflow(project_engine, tmp_path / "library")
        _open_library_workflow(project_engine, library)
        executor = LocalWorkflowExecutor(save_on_failure_path="")

        asyncio.run(executor._save_failed_workflow(RuntimeError("The run failed.")))

        failure_files = list(workspace.rglob("*_failed_*.py"))
        assert len(failure_files) == 1
        assert failure_files[0].name.startswith("collage_poster_failed_")


class TestAFailedScriptRunsFileName:
    def test_a_failed_script_run_of_an_unsaved_workflow_is_saved_under_its_display_name(
        self, project_engine: Engine, workspace: Path
    ) -> None:
        """Not under its "unsaved:<id>" registry name, which holds a colon."""
        _open_new_workflow(project_engine, "Shot 10")
        executor = LocalWorkflowExecutor(save_on_failure_path="")

        asyncio.run(executor._save_failed_workflow(RuntimeError("The run failed.")))

        failure_files = list(workspace.rglob("*_failed_*.py"))
        assert len(failure_files) == 1
        assert failure_files[0].name.startswith("Shot_10_failed_")
