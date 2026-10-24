"""Workflow ids: written into every file, kept across save, rename, move, refresh and restart."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sys
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple
from unittest.mock import MagicMock, patch

import pytest

from griptape_nodes.bootstrap.workflow_executors.local_workflow_executor import LocalWorkflowExecutor
from griptape_nodes.bootstrap.workflow_publishers.local_workflow_publisher import LocalWorkflowPublisher
from griptape_nodes.common.project_templates.default_project_template import DEFAULT_PROJECT_TEMPLATE
from griptape_nodes.files.path_utils import canonicalize_for_identity_preserving_symlinks
from griptape_nodes.node_library.workflow_registry import (
    WorkflowMetadata,
    WorkflowShape,
    derive_library_workflow_id,
    derive_workflow_id_for_path,
    read_workflow_metadata,
)
from griptape_nodes.retained_mode.engine import current_engine, reset_root_engine
from griptape_nodes.retained_mode.events.app_events import (
    AppInitializationComplete,
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
    SetWorkflowContextSuccess,
)
from griptape_nodes.retained_mode.events.flow_events import (
    CreateFlowRequest,
    CreateFlowResultSuccess,
    GetTopLevelFlowRequest,
    GetTopLevelFlowResultSuccess,
    ImportWorkflowAsReferencedSubFlowRequest,
)
from griptape_nodes.retained_mode.events.object_events import ClearAllObjectStateRequest
from griptape_nodes.retained_mode.events.project_events import (
    LoadProjectTemplateRequest,
    LoadProjectTemplateResultSuccess,
    SetCurrentProjectRequest,
)
from griptape_nodes.retained_mode.events.workflow_events import (
    BranchWorkflowRequest,
    BranchWorkflowResultSuccess,
    CompareWorkflowsRequest,
    CompareWorkflowsResultSuccess,
    CreateWorkflowFromTemplateRequest,
    CreateWorkflowFromTemplateResultFailure,
    CreateWorkflowFromTemplateResultSuccess,
    DeleteWorkflowRequest,
    DeleteWorkflowResultFailure,
    DeleteWorkflowResultSuccess,
    GetPublishOptionsRequest,
    GetPublishOptionsResultFailure,
    GetPublishOptionsResultSuccess,
    GetWorkflowInfoRequest,
    GetWorkflowInfoResultSuccess,
    GetWorkflowMetadataRequest,
    GetWorkflowMetadataResultSuccess,
    GetWorkflowRunCommandRequest,
    GetWorkflowRunCommandResultFailure,
    GetWorkflowRunCommandResultSuccess,
    ImportWorkflowAsReferencedSubFlowResultFailure,
    ImportWorkflowAsReferencedSubFlowResultSuccess,
    ListAllWorkflowsRequest,
    ListAllWorkflowsResultSuccess,
    ListCallableWorkflowsRequest,
    ListCallableWorkflowsResultSuccess,
    MergeWorkflowBranchRequest,
    MergeWorkflowBranchResultFailure,
    MergeWorkflowBranchResultSuccess,
    MoveWorkflowRequest,
    MoveWorkflowResultFailure,
    MoveWorkflowResultSuccess,
    PublishWorkflowRegisteredEventData,
    PublishWorkflowRequest,
    PublishWorkflowResultFailure,
    PublishWorkflowResultSuccess,
    RenameDisplayNameBehavior,
    RenameWorkflowRequest,
    RenameWorkflowResultFailure,
    RenameWorkflowResultSuccess,
    ResetWorkflowBranchRequest,
    ResetWorkflowBranchResultFailure,
    ResetWorkflowBranchResultSuccess,
    RunWorkflowFromRegistryRequest,
    RunWorkflowFromRegistryResultFailure,
    RunWorkflowFromRegistryResultSuccess,
    RunWorkflowFromScratchRequest,
    RunWorkflowFromScratchResultFailure,
    RunWorkflowFromScratchResultSuccess,
    RunWorkflowWithCurrentStateRequest,
    RunWorkflowWithCurrentStateResultSuccess,
    SaveSubflowToWorkflowRequest,
    SaveSubflowToWorkflowResultSuccess,
    SaveWorkflowFileFromSerializedFlowRequest,
    SaveWorkflowRequest,
    SaveWorkflowResultFailure,
    SaveWorkflowResultSuccess,
    SetVariableSubstitutionEnabledRequest,
    SetWorkflowMetadataRequest,
    SetWorkflowMetadataResultFailure,
    SetWorkflowMetadataResultSuccess,
)
from griptape_nodes.retained_mode.managers.context_manager import ContextManager, read_metadata_for_open_file
from griptape_nodes.retained_mode.managers.library.common import (
    LibraryFitness,
    LibraryInfo,
    LibraryLifecycleState,
    RegisteredEventHandler,
)
from griptape_nodes.retained_mode.managers.workflow.saving import WriteWorkflowFileResult

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

    from griptape_nodes.node_library.workflow_registry import Workflow
    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.events.base_events import ResultPayload


# Fixed workflow ids for files the tests write themselves.
MOVER_ID = "00000000000000000000000000000001"
STALE_ID = "00000000000000000000000000000002"
LINEAGE_ID = "00000000000000000000000000000003"
SHARED_ID = "00000000000000000000000000000004"
LIBRARY_ID = "00000000000000000000000000000005"


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


def _header_id(path: str | Path) -> str | None:
    return read_workflow_metadata(Path(path)).workflow_id


def _write_workflow_file(path: Path, *, workflow_id: str | None, name: str = "flow") -> Path:
    """Write a workflow file carrying only a metadata header."""
    metadata = WorkflowMetadata(
        workflow_id=workflow_id,
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


class TestWorkflowIdHeader:
    def test_header_round_trips_the_id(self, tmp_path: Path) -> None:
        engine = current_engine()
        metadata = WorkflowMetadata(
            workflow_id="0123456789abcdef0123456789abcdef",
            name="flow",
            schema_version=WorkflowMetadata.LATEST_SCHEMA_VERSION,
            engine_version_created_with="0.0.0",
            node_libraries_referenced=[],
        )
        header = engine.workflow_manager.codegen._generate_workflow_metadata_header(metadata)
        assert header is not None
        workflow_file = tmp_path / "flow.py"
        workflow_file.write_text(f"{header}\n\nprint('body')\n", encoding="utf-8")

        read_back = read_workflow_metadata(workflow_file)

        assert read_back.workflow_id == "0123456789abcdef0123456789abcdef"
        assert read_back.schema_version == "0.22.0"

    def test_header_without_an_id_parses(self, tmp_path: Path) -> None:
        workflow_file = _write_workflow_file(tmp_path / "old.py", workflow_id=None)

        assert read_workflow_metadata(workflow_file).workflow_id is None

    def test_malformed_id_is_treated_as_absent(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        workflow_file = _write_workflow_file(tmp_path / "bad.py", workflow_id=None)
        content = workflow_file.read_text(encoding="utf-8")
        workflow_file.write_text(
            content.replace("# name = ", '# workflow_id = "not a valid id!"\n# name = '), encoding="utf-8"
        )

        with caplog.at_level(logging.WARNING, logger="griptape_nodes"):
            metadata = read_workflow_metadata(workflow_file)

        assert metadata.workflow_id is None
        assert any("malformed workflow_id" in record.getMessage() for record in caplog.records)

    def test_a_dashed_id_is_read_as_its_hex_form(self, tmp_path: Path) -> None:
        dashed = "01234567-89ab-cdef-0123-456789abcdef"
        workflow_file = _write_workflow_file(tmp_path / "dashed.py", workflow_id=None)
        content = workflow_file.read_text(encoding="utf-8")
        workflow_file.write_text(
            content.replace("# name = ", f'# workflow_id = "{dashed}"\n# name = '), encoding="utf-8"
        )

        assert read_workflow_metadata(workflow_file).workflow_id == "0123456789abcdef0123456789abcdef"

    def test_both_header_writers_refuse_a_missing_id(self) -> None:
        codegen = current_engine().workflow_manager.codegen
        metadata = WorkflowMetadata(
            name="flow",
            schema_version=WorkflowMetadata.LATEST_SCHEMA_VERSION,
            engine_version_created_with="0.0.0",
            node_libraries_referenced=[],
        )

        with pytest.raises(ValueError, match="no workflow id"):
            codegen._generate_workflow_metadata_header(metadata)
        with pytest.raises(ValueError, match="no workflow id"):
            codegen.replace_workflow_metadata_header("# /// script\n# ///\n", metadata)


class TestWritePathsStampAnId:
    """Every write path puts an id in the file: the lineage's own, or a new one for a copy."""

    def test_first_save_writes_the_unsaved_workflows_id(self, project_engine: Engine) -> None:
        workflow_id = _open_new_workflow(project_engine, "first")

        saved = _save(project_engine)

        assert saved.workflow_id == workflow_id
        assert _header_id(saved.file_path) == workflow_id
        assert project_engine.context_manager.get_current_workflow_id() == workflow_id

    def test_saving_again_keeps_the_id(self, project_engine: Engine) -> None:
        workflow_id = _open_new_workflow(project_engine, "again")
        first = _save(project_engine)

        second = _save(project_engine, SaveWorkflowRequest(file_name=first.workflow_name))

        assert second.file_path == first.file_path
        assert second.workflow_id == workflow_id
        assert _header_id(second.file_path) == workflow_id

    def test_save_as_writes_a_copy_with_a_new_id(self, project_engine: Engine) -> None:
        workflow_id = _open_new_workflow(project_engine, "original")
        _save(project_engine)

        copy = _save(project_engine, SaveWorkflowRequest(file_name="the_copy"))

        assert copy.workflow_id != workflow_id
        assert _header_id(copy.file_path) == copy.workflow_id
        # The open workflow stays on the original.
        assert project_engine.context_manager.get_current_workflow_id() == workflow_id
        assert project_engine.workflow_registry.find_by_name("the_copy").workflow_id == copy.workflow_id  # type: ignore[union-attr]

    def test_versioned_save_writes_a_copy_with_a_new_id(self, project_engine: Engine) -> None:
        workflow_id = _open_new_workflow(project_engine, "versioned")
        _save(project_engine)

        version = _save(project_engine, SaveWorkflowRequest(create_versioned=True))

        assert version.workflow_id != workflow_id
        assert _header_id(version.file_path) == version.workflow_id
        assert project_engine.context_manager.get_current_workflow_id() == workflow_id

    def test_first_save_onto_another_workflows_file_takes_it_over(self, project_engine: Engine) -> None:
        """The open workflow keeps its id; the workflow whose file it overwrote ends."""
        other = _saved_workflow(project_engine, "other")
        workflow_id = _open_new_workflow(project_engine, "unsaved")

        saved = _save(project_engine, SaveWorkflowRequest(file_name=other.workflow_name))

        assert saved.file_path == other.file_path
        assert saved.workflow_id == workflow_id
        assert _header_id(saved.file_path) == workflow_id
        assert all(workflow["workflow_id"] != other.workflow_id for workflow in _listed(project_engine).values())
        assert project_engine.workflow_registry.find_by_name(other.workflow_name).workflow_id == workflow_id  # type: ignore[union-attr]

    def test_saving_over_another_saved_workflow_ends_its_identity(self, project_engine: Engine) -> None:
        """A saved workflow saved over another's file is a new workflow there; the open one stays itself."""
        other = _saved_workflow(project_engine, "other")
        workflow_id = _open_new_workflow(project_engine, "mine")
        _save(project_engine)

        written = _save(project_engine, SaveWorkflowRequest(file_name=other.workflow_name))

        assert written.workflow_id not in (workflow_id, other.workflow_id)
        assert _header_id(other.file_path) == written.workflow_id
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
        assert _header_id(copy.file_path) == copy.workflow_id
        assert _header_id(library.file_path) == library.workflow_id
        # The open workflow stays itself: its identity never changes while it is open.
        assert project_engine.context_manager.get_current_workflow_id() == workflow_id

    def test_first_save_onto_a_library_workflow_keeps_the_open_id(self, project_engine: Engine) -> None:
        library = _saved_workflow(project_engine, "library_workflow")
        project_engine.workflow_registry.get(library.workflow_id).metadata.is_griptape_provided = True  # type: ignore[arg-type]
        library_content = Path(library.file_path).read_bytes()
        workflow_id = _open_new_workflow(project_engine, "mine")

        saved = _save(project_engine, SaveWorkflowRequest(file_name=library.workflow_name))

        assert saved.workflow_id == workflow_id
        assert Path(saved.file_path) != Path(library.file_path)
        assert _header_id(saved.file_path) == workflow_id
        assert Path(library.file_path).read_bytes() == library_content
        assert project_engine.context_manager.get_current_workflow().absolute_path == Path(saved.file_path)

    def test_branch_merge_and_reset(self, project_engine: Engine) -> None:
        source = _saved_workflow(project_engine, "source")

        branch = project_engine.handle_request(BranchWorkflowRequest(workflow_name=source.workflow_name))
        assert isinstance(branch, BranchWorkflowResultSuccess), branch.result_details
        assert branch.original_workflow_id == source.workflow_id
        assert branch.branched_workflow_id not in (None, source.workflow_id)
        branch_entry = project_engine.workflow_registry.get(branch.branched_workflow_id)  # type: ignore[arg-type]
        assert _header_id(branch_entry.absolute_path) == branch.branched_workflow_id  # type: ignore[arg-type]

        reset = project_engine.handle_request(ResetWorkflowBranchRequest(workflow_name=branch.branched_workflow_name))
        assert isinstance(reset, ResetWorkflowBranchResultSuccess), reset.result_details
        assert reset.workflow_id == branch.branched_workflow_id
        assert _header_id(branch_entry.absolute_path) == branch.branched_workflow_id  # type: ignore[arg-type]

        merged = project_engine.handle_request(MergeWorkflowBranchRequest(workflow_name=branch.branched_workflow_name))
        assert isinstance(merged, MergeWorkflowBranchResultSuccess), merged.result_details
        assert merged.workflow_id == source.workflow_id
        assert _header_id(source.file_path) == source.workflow_id

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
            open_id = branch_entry.workflow_id
        else:
            open_id = source.workflow_id
        project_engine.context_manager.push_workflow(workflow_id=open_id)

        # Addressed by id, so the message must name the branch from its entry, not the request.
        merged = project_engine.handle_request(MergeWorkflowBranchRequest(workflow_id=branch.branched_workflow_id))

        assert isinstance(merged, MergeWorkflowBranchResultFailure)
        assert "is open" in str(merged.result_details)
        assert f"'{branch.branched_workflow_name}'" in str(merged.result_details)
        assert Path(source.file_path).read_text(encoding="utf-8") == source_content
        assert branch_path.read_text(encoding="utf-8") == branch_content
        assert project_engine.workflow_registry.get(branch_entry.workflow_id) is branch_entry
        assert project_engine.context_manager.get_current_workflow_id() == open_id

    def test_create_from_template_gets_a_new_id(self, project_engine: Engine) -> None:
        template = _saved_workflow(project_engine, "template")
        project_engine.workflow_registry.get(template.workflow_id).metadata.is_template = True  # type: ignore[arg-type]

        created = project_engine.handle_request(CreateWorkflowFromTemplateRequest(template_name=template.workflow_name))

        assert isinstance(created, CreateWorkflowFromTemplateResultSuccess), created.result_details
        assert created.workflow_id not in (None, template.workflow_id)
        assert _header_id(created.file_path) == created.workflow_id

    def test_set_metadata_keeps_the_id_and_refuses_a_new_one(self, project_engine: Engine) -> None:
        saved = _saved_workflow(project_engine, "described")

        updated = _run(
            project_engine,
            SetWorkflowMetadataRequest(
                workflow_name=saved.workflow_name,
                workflow_metadata={"description": "now described"},  # type: ignore[arg-type]
            ),
        )
        refused = _run(
            project_engine,
            SetWorkflowMetadataRequest(
                workflow_name=saved.workflow_name,
                workflow_metadata={"workflow_id": "someone_else"},  # type: ignore[arg-type]
            ),
        )

        assert isinstance(updated, SetWorkflowMetadataResultSuccess), updated.result_details
        assert read_workflow_metadata(Path(saved.file_path)).description == "now described"
        assert _header_id(saved.file_path) == saved.workflow_id
        assert isinstance(refused, SetWorkflowMetadataResultFailure)
        assert "never changes" in str(refused.result_details)
        assert _header_id(saved.file_path) == saved.workflow_id

    def test_move_keeps_the_id(self, project_engine: Engine, workspace: Path) -> None:
        saved = _saved_workflow(project_engine, "mover")

        moved = project_engine.handle_request(
            MoveWorkflowRequest(workflow_name=saved.workflow_name, target_directory="elsewhere")
        )

        assert isinstance(moved, MoveWorkflowResultSuccess), moved.result_details
        assert moved.workflow_id == saved.workflow_id
        assert _header_id(workspace / moved.moved_file_path) == saved.workflow_id

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
        assert _header_id(entry.absolute_path) == workflow_id
        # Still open, graph and all: renaming does not close the workflow.
        assert project_engine.context_manager.get_current_workflow_id() == workflow_id
        assert project_engine.context_manager.get_current_workflow_name() == "after"
        assert project_engine.context_manager.get_current_flow().name == flow_name

    def test_rename_of_an_open_library_workflow_copy_saves_the_copy(
        self, project_engine: Engine, tmp_path: Path, workspace: Path
    ) -> None:
        library = _library_workflow(project_engine, tmp_path / "library")
        library_file = library.absolute_path
        assert library_file is not None
        library_content = library_file.read_bytes()
        opened = _run(project_engine, RunWorkflowFromRegistryRequest(workflow_id=LIBRARY_ID))
        assert isinstance(opened, RunWorkflowFromRegistryResultSuccess), opened.result_details

        renamed = _run(project_engine, RenameWorkflowRequest(workflow_id=opened.workflow_id, requested_name="mine"))

        assert isinstance(renamed, RenameWorkflowResultSuccess), renamed.result_details
        assert renamed.workflow_id == opened.workflow_id
        assert _header_id(workspace / "mine.py") == opened.workflow_id
        assert project_engine.context_manager.get_current_workflow_id() == opened.workflow_id
        assert library_file.read_bytes() == library_content
        assert project_engine.workflow_registry.get(LIBRARY_ID).absolute_path == library_file

    def test_renaming_only_the_letter_case_keeps_the_file(self, project_engine: Engine, workspace: Path) -> None:
        """On a case-insensitive filesystem the new name is the same file, which must not be deleted."""
        probe = workspace / "Case_Probe.txt"
        probe.write_text("probe")
        case_insensitive = (workspace / "case_probe.txt").exists()
        probe.unlink()
        if not case_insensitive:
            pytest.skip("the filesystem is case-sensitive, so the new name is a different file")
        workflow_id = _open_new_workflow(project_engine, "Shot")
        saved = _save(project_engine, SaveWorkflowRequest(file_name="Shot"))

        renamed = _run(project_engine, RenameWorkflowRequest(workflow_name=saved.workflow_name, requested_name="shot"))

        assert isinstance(renamed, RenameWorkflowResultSuccess), renamed.result_details
        entry = project_engine.workflow_registry.get(workflow_id)
        assert entry.absolute_path is not None
        assert entry.absolute_path.exists()
        assert _header_id(entry.absolute_path) == workflow_id
        # The file on disk takes the new letter case, so a rescan finds the renamed workflow.
        assert entry.absolute_path.name in [path.name for path in entry.absolute_path.parent.iterdir()]
        _close(project_engine)
        _scan(project_engine, workspace)
        rescanned = project_engine.workflow_registry.find_by_name(renamed.new_workflow_name)
        assert rescanned is not None
        assert rescanned.workflow_id == workflow_id

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
        assert result.workflow_metadata.workflow_id == workflow_id
        assert _header_id(saved.file_path) == workflow_id


class TestUnreadableHeaderRefusesBranchRequests:
    @pytest.mark.parametrize(
        ("request_type", "failure_type", "name_field"),
        [
            (CreateWorkflowFromTemplateRequest, CreateWorkflowFromTemplateResultFailure, "template_name"),
            (MergeWorkflowBranchRequest, MergeWorkflowBranchResultFailure, "workflow_name"),
            (ResetWorkflowBranchRequest, ResetWorkflowBranchResultFailure, "workflow_name"),
        ],
    )
    def test_each_fails_without_touching_files(
        self,
        project_engine: Engine,
        workspace: Path,
        request_type: type,
        failure_type: type,
        name_field: str,
    ) -> None:
        workflow_file = workspace / "damaged.py"
        workflow_file.write_text("print('no header')\n", encoding="utf-8")
        opened = read_metadata_for_open_file(workflow_file)
        assert opened.metadata_error is not None
        workflow = project_engine.workflow_registry.register_file(
            workflow_file, opened.metadata, metadata_error=opened.metadata_error
        )
        content = workflow_file.read_text(encoding="utf-8")
        files_before = sorted(workspace.rglob("*"))

        result = project_engine.handle_request(request_type(**{name_field: workflow.name}))

        assert isinstance(result, failure_type)
        assert "could not be read" in str(result.result_details)
        assert workflow_file.read_text(encoding="utf-8") == content
        assert sorted(workspace.rglob("*")) == files_before
        assert project_engine.workflow_registry.get(workflow.workflow_id) is workflow


class TestSaveWorkflowFileFromSerializedFlowStampsAnId:
    """Writing back into a registered workflow's file keeps its id; any other file gets a new one."""

    @staticmethod
    def _written_id(engine: Engine, request: SaveWorkflowFileFromSerializedFlowRequest) -> str:
        with patch.object(engine.workflow_manager.saver, "_save_workflow_file_inline") as save_inline:
            asyncio.run(engine.workflow_manager.saver.on_save_workflow_file_from_serialized_flow_request(request))
        return save_inline.call_args.kwargs["workflow_id"]

    def test_a_registered_file_keeps_its_workflows_id(self, workspace: Path) -> None:
        engine = current_engine()
        engine.config_manager.workspace_path = workspace
        workflow_file = _write_workflow_file(workspace / "lineage.py", workflow_id=LINEAGE_ID)
        engine.workflow_registry.register_file(workflow_file, read_workflow_metadata(workflow_file))

        written_id = self._written_id(
            engine,
            SaveWorkflowFileFromSerializedFlowRequest(
                serialized_flow_commands=MagicMock(), file_name="lineage", file_path=str(workflow_file)
            ),
        )

        assert written_id == LINEAGE_ID

    def test_any_other_file_gets_a_new_id(self, workspace: Path) -> None:
        engine = current_engine()
        engine.config_manager.workspace_path = workspace
        engine.workflow_registry.register_file(
            _write_workflow_file(workspace / "lineage.py", workflow_id=LINEAGE_ID),
            read_workflow_metadata(workspace / "lineage.py"),
        )

        to_new_path = self._written_id(
            engine,
            SaveWorkflowFileFromSerializedFlowRequest(
                serialized_flow_commands=MagicMock(), file_name="packaged", file_path=str(workspace / "packaged.py")
            ),
        )
        to_situation = self._written_id(
            engine, SaveWorkflowFileFromSerializedFlowRequest(serialized_flow_commands=MagicMock(), file_name="named")
        )

        assert to_new_path not in (None, LINEAGE_ID)
        assert to_situation not in (None, LINEAGE_ID, to_new_path)


class TestDerivedIds:
    def test_derived_ids_name_what_they_are_derived_from(self, tmp_path: Path) -> None:
        """Derived ids are uuid5 names in uuid.NAMESPACE_URL, so tabs that remembered one keep finding it."""
        workflow_file = tmp_path / "flow.py"
        canonical_path = canonicalize_for_identity_preserving_symlinks(workflow_file)

        assert derive_workflow_id_for_path(workflow_file) == (
            uuid.uuid5(uuid.NAMESPACE_URL, f"griptape-nodes:workflow:{canonical_path}").hex
        )
        assert derive_library_workflow_id("My Library", "workflows/demo.py") == "f0ff9161aa0251a8a7e2671abbca8a8d"

    def test_a_file_without_an_id_gets_a_derived_one_that_is_not_written(self, workspace: Path) -> None:
        engine = current_engine()
        engine.config_manager.workspace_path = workspace
        workflow_file = _write_workflow_file(workspace / "legacy.py", workflow_id=None)
        content_before = workflow_file.read_text(encoding="utf-8")

        _scan(engine, workspace)

        entry = engine.workflow_registry.find_by_path(workflow_file)
        assert entry is not None
        assert entry.workflow_id == derive_workflow_id_for_path(workflow_file)
        assert workflow_file.read_text(encoding="utf-8") == content_before

    def test_the_derived_id_is_stable_across_refresh_and_project_switch(self, workspace: Path) -> None:
        engine = current_engine()
        engine.config_manager.workspace_path = workspace
        workflow_file = _write_workflow_file(workspace / "shots" / "legacy.py", workflow_id=None)
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

    def test_the_derived_id_is_written_on_the_next_save(self, project_engine: Engine, workspace: Path) -> None:
        workflow_file = _write_workflow_file(workspace / "legacy.py", workflow_id=None, name="legacy")
        _scan(project_engine, workspace)
        entry = project_engine.workflow_registry.find_by_path(workflow_file)
        assert entry is not None
        project_engine.context_manager.push_workflow(workflow_id=entry.workflow_id)
        created = project_engine.handle_request(CreateFlowRequest(parent_flow_name=None, set_as_new_context=True))
        assert isinstance(created, CreateFlowResultSuccess), created.result_details

        saved = _save(project_engine, SaveWorkflowRequest(file_name=entry.name))

        assert saved.workflow_id == entry.workflow_id
        assert _header_id(workflow_file) == entry.workflow_id

    def test_a_library_workflow_id_survives_the_library_moving(self, tmp_path: Path) -> None:
        """Derived from the library's name and the path inside it, so a reinstall elsewhere keeps it."""
        engine = current_engine()
        first_root = tmp_path / "install_a" / "my_library"
        second_root = tmp_path / "install_b" / "my_library"
        for root in (first_root, second_root):
            _write_workflow_file(root / "workflows" / "demo.py", workflow_id=None)
            (root / "griptape_nodes_library.json").write_text("{}")

        ids = []
        for root in (first_root, second_root):
            library_info = LibraryInfo(
                lifecycle_state=LibraryLifecycleState.LOADED,
                fitness=LibraryFitness.GOOD,
                library_path=str(root / "griptape_nodes_library.json"),
                is_sandbox=False,
                library_name="My Library",
            )
            with patch.dict(engine.library_manager._library_file_path_to_info, {"lib": library_info}, clear=True):
                workflow_file = root / "workflows" / "demo.py"
                workflow = engine.workflow_manager.identity.register_file(
                    workflow_file, read_workflow_metadata(workflow_file)
                )
                ids.append(workflow.workflow_id)
                engine.workflow_registry.remove(workflow.workflow_id)

        assert ids[0] == ids[1] == derive_library_workflow_id("My Library", "workflows/demo.py")


class TestDuplicateIds:
    """A copy of a stamped file is settled once: one file keeps the id, the other is restamped."""

    def test_the_copy_is_restamped_at_detection(self, workspace: Path) -> None:
        engine = current_engine()
        engine.config_manager.workspace_path = workspace
        original = _write_workflow_file(workspace / "original.py", workflow_id=SHARED_ID)
        copy = workspace / "copy.py"
        shutil.copy(original, copy)
        birth_times = {original: 1.0, copy: 2.0}

        with patch(
            "griptape_nodes.retained_mode.managers.workflow.identity.file_birth_time",
            side_effect=lambda path: birth_times[path],
        ):
            _scan(engine, workspace)

        copy_id = _header_id(copy)
        assert _header_id(original) == SHARED_ID
        assert copy_id not in (None, SHARED_ID)
        assert engine.workflow_registry.find_by_path(original).workflow_id == SHARED_ID  # type: ignore[union-attr]
        assert engine.workflow_registry.find_by_path(copy).workflow_id == copy_id  # type: ignore[union-attr]

        # Settled: the next scan finds two different ids and writes nothing.
        _scan(engine, workspace)
        assert _header_id(copy) == copy_id

    def test_without_birth_times_the_smallest_path_keeps_the_id(self, workspace: Path) -> None:
        engine = current_engine()
        engine.config_manager.workspace_path = workspace
        first = _write_workflow_file(workspace / "a.py", workflow_id=SHARED_ID)
        second = workspace / "b.py"
        shutil.copy(first, second)

        with patch("griptape_nodes.retained_mode.managers.workflow.identity.file_birth_time", return_value=None):
            _scan(engine, workspace)

        assert _header_id(first) == SHARED_ID
        assert _header_id(second) != SHARED_ID

    def test_on_refresh_the_file_that_had_the_id_keeps_it(self, workspace: Path) -> None:
        engine = current_engine()
        engine.config_manager.workspace_path = workspace
        original = _write_workflow_file(workspace / "z_original.py", workflow_id=SHARED_ID)
        _scan(engine, workspace)
        copy = workspace / "a_copy.py"
        shutil.copy(original, copy)

        # The copy looks older and sorts first, so only the refresh rule keeps the id on the original.
        birth_times = {original: 2.0, copy: 1.0}
        with patch(
            "griptape_nodes.retained_mode.managers.workflow.identity.file_birth_time",
            side_effect=lambda path: birth_times[path],
        ):
            _scan(engine, workspace)

        assert _header_id(original) == SHARED_ID
        assert _header_id(copy) != SHARED_ID

    def test_the_open_workflow_keeps_its_id(self, workspace: Path) -> None:
        engine = current_engine()
        engine.config_manager.workspace_path = workspace
        opened = _write_workflow_file(workspace / "z_opened.py", workflow_id=SHARED_ID)
        _scan(engine, workspace)
        engine.context_manager.push_workflow(file_path=str(opened))
        copy = workspace / "a_copy.py"
        shutil.copy(opened, copy)

        with patch("griptape_nodes.retained_mode.managers.workflow.identity.file_birth_time", return_value=None):
            _scan(engine, workspace)

        assert engine.context_manager.get_current_workflow_id() == SHARED_ID
        assert _header_id(opened) == SHARED_ID
        assert _header_id(copy) != SHARED_ID

    @pytest.mark.parametrize(
        "initialization",
        [
            pytest.param(AppInitializationComplete(skip_library_loading=True, is_worker=True), id="worker"),
            pytest.param(
                AppInitializationComplete(skip_library_loading=True, defer_pending_library_resets=True),
                id="beside-an-orchestrator",
            ),
        ],
    )
    def test_an_engine_that_does_not_own_the_workspace_never_restamps(
        self, workspace: Path, initialization: AppInitializationComplete
    ) -> None:
        """Workers and short-lived engines scan the same workspace; only its owner writes, so ids cannot drift."""
        engine = current_engine()
        engine.config_manager.workspace_path = workspace
        # Headless initialization registers secrets, which writes the user's real `.env`.
        with patch.object(engine.secrets_manager, "register_all_secrets"):
            asyncio.run(engine.library_manager._run_app_initialization(initialization))
        assert engine.library_manager.owns_workspace is False
        original = _write_workflow_file(workspace / "a.py", workflow_id=SHARED_ID)
        copy = workspace / "b.py"
        shutil.copy(original, copy)
        copy_content = copy.read_text(encoding="utf-8")

        with patch("griptape_nodes.retained_mode.managers.workflow.identity.file_birth_time", return_value=None):
            _scan(engine, workspace)

        assert copy.read_text(encoding="utf-8") == copy_content
        assert _header_id(original) == SHARED_ID
        assert engine.workflow_registry.find_by_path(original).workflow_id == SHARED_ID  # type: ignore[union-attr]
        assert engine.workflow_registry.find_by_path(copy).workflow_id == derive_workflow_id_for_path(copy)  # type: ignore[union-attr]

    @pytest.mark.skipif(os.name == "nt", reason="read-only permission bits are not enforced the same way on Windows")
    def test_engines_that_do_not_own_the_workspace_pick_the_same_winner(self, workspace: Path) -> None:
        """Writability is a property of the file, so a worker and the orchestrator agree on who keeps the id."""
        engine = current_engine()
        engine.config_manager.workspace_path = workspace
        writable = _write_workflow_file(workspace / "a.py", workflow_id=SHARED_ID)
        read_only = workspace / "b.py"
        shutil.copy(writable, read_only)
        read_only.chmod(0o444)
        try:
            with (
                patch.object(engine.library_manager, "_owns_workspace", new=False),
                patch("griptape_nodes.retained_mode.managers.workflow.identity.file_birth_time", return_value=None),
            ):
                _scan(engine, workspace)
        finally:
            read_only.chmod(0o644)

        assert engine.workflow_registry.find_by_path(read_only).workflow_id == SHARED_ID  # type: ignore[union-attr]
        assert _header_id(writable) == SHARED_ID

    @pytest.mark.skipif(sys.platform.startswith("win"), reason="symlink creation needs privileges on Windows")
    def test_a_file_reached_through_a_link_is_never_restamped(self, workspace: Path, tmp_path: Path) -> None:
        engine = current_engine()
        engine.config_manager.workspace_path = workspace
        real = _write_workflow_file(tmp_path / "real" / "flow.py", workflow_id=SHARED_ID)
        (workspace / "linked").symlink_to(tmp_path / "real", target_is_directory=True)

        _scan(engine, workspace, real)

        assert _header_id(real) == SHARED_ID
        entry = engine.workflow_registry.get(SHARED_ID)
        assert engine.workflow_registry.find_by_path(workspace / "linked" / "flow.py") is entry
        assert engine.workflow_registry.find_by_path(real) is entry

    @pytest.mark.skipif(os.name == "nt", reason="read-only permission bits are not enforced the same way on Windows")
    def test_a_read_only_loser_gets_the_id_derived_from_its_path(
        self, workspace: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        engine = current_engine()
        engine.config_manager.workspace_path = workspace
        first = _write_workflow_file(workspace / "a.py", workflow_id=SHARED_ID)
        second = workspace / "b.py"
        shutil.copy(first, second)
        first.chmod(0o444)
        second.chmod(0o444)
        try:
            with (
                patch("griptape_nodes.retained_mode.managers.workflow.identity.file_birth_time", return_value=None),
                caplog.at_level(logging.WARNING, logger="griptape_nodes"),
            ):
                _scan(engine, workspace)
        finally:
            first.chmod(0o644)
            second.chmod(0o644)

        assert _header_id(second) == SHARED_ID
        assert engine.workflow_registry.find_by_path(second).workflow_id == derive_workflow_id_for_path(second)  # type: ignore[union-attr]
        assert any("could not be given a new id" in record.getMessage() for record in caplog.records)

    @pytest.mark.skipif(os.name == "nt", reason="read-only permission bits are not enforced the same way on Windows")
    def test_when_one_file_is_read_only_it_keeps_the_id(self, workspace: Path) -> None:
        engine = current_engine()
        engine.config_manager.workspace_path = workspace
        writable = _write_workflow_file(workspace / "a.py", workflow_id=SHARED_ID)
        read_only = workspace / "b.py"
        shutil.copy(writable, read_only)
        read_only.chmod(0o444)
        try:
            with patch("griptape_nodes.retained_mode.managers.workflow.identity.file_birth_time", return_value=None):
                _scan(engine, workspace)
        finally:
            read_only.chmod(0o644)

        assert _header_id(read_only) == SHARED_ID
        assert _header_id(writable) != SHARED_ID


class TestRegistryRefresh:
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
    def test_move_onto_a_path_another_entry_holds_puts_the_file_back(self, workspace: Path) -> None:
        engine = current_engine()
        engine.config_manager.workspace_path = workspace
        mover = _write_workflow_file(workspace / "a" / "foo.py", workflow_id=MOVER_ID)
        stale = _write_workflow_file(workspace / "b" / "foo.py", workflow_id=STALE_ID)
        _scan(engine, workspace)
        # Deleted outside the engine: its entry stays until the next scan.
        stale.unlink()

        moved = engine.handle_request(MoveWorkflowRequest(workflow_name="a/foo", target_directory="b"))

        assert isinstance(moved, MoveWorkflowResultFailure)
        assert "Rolled back" in str(moved.result_details)
        assert mover.is_file()
        assert not stale.exists()
        assert engine.workflow_registry.get(MOVER_ID).absolute_path == mover


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


class TestIdentityAcrossTheWorkflowLifecycle:
    """One workflow keeps one id through create, save, rename, move, reopen, restart and refresh."""

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
        engine.handle_request(SetCurrentProjectRequest(project_id=None))

        # Restart: a new engine finds the workflow by scanning, and reads the id from its header.
        reset_root_engine()
        restarted = current_engine()
        _use_default_project(restarted, workspace)
        _scan(restarted, workspace)
        found = restarted.workflow_registry.get(workflow_id)
        assert found.name == "shots/renamed"

        _scan(restarted, workspace)
        assert restarted.workflow_registry.get(workflow_id).name == "shots/renamed"
        restarted.handle_request(SetCurrentProjectRequest(project_id=None))


class _OpenAndOther(NamedTuple):
    """Workflow A, saved and open, and workflow B, saved and closed."""

    open_id: str
    open_file: Path
    open_flow_name: str
    other: SaveWorkflowResultSuccess


def _open_a_and_save_b(engine: Engine, *, other: SaveWorkflowResultSuccess | None = None) -> _OpenAndOther:
    """Save B and close it, then open and save A. A request for B must leave A alone."""
    if other is None:
        other = _saved_workflow(engine, "other")
    open_id = _open_new_workflow(engine, "open")
    open_saved = _save(engine)
    return _OpenAndOther(
        open_id=open_id,
        open_file=Path(open_saved.file_path),
        open_flow_name=engine.context_manager.get_current_flow().name,
        other=other,
    )


def _assert_open_workflow_untouched(engine: Engine, setup: _OpenAndOther, open_file_content: str) -> None:
    assert engine.context_manager.get_current_workflow_id() == setup.open_id
    assert engine.context_manager.get_current_flow().name == setup.open_flow_name
    assert engine.workflow_registry.get(setup.open_id).absolute_path == setup.open_file
    assert setup.open_file.read_text(encoding="utf-8") == open_file_content


class TestRequestsForAnotherWorkflowLeaveTheOpenOneAlone:
    """A request addressed to workflow B while A is open never saves, moves, or deletes A or its file."""

    def test_rename(self, project_engine: Engine) -> None:
        setup = _open_a_and_save_b(project_engine)
        open_file_content = setup.open_file.read_text(encoding="utf-8")

        renamed = _run(
            project_engine, RenameWorkflowRequest(workflow_id=setup.other.workflow_id, requested_name="renamed")
        )

        assert isinstance(renamed, RenameWorkflowResultSuccess), renamed.result_details
        assert renamed.workflow_id == setup.other.workflow_id
        assert renamed.new_workflow_name == "renamed"
        assert not Path(setup.other.file_path).exists()
        renamed_file = Path(setup.other.file_path).with_name("renamed.py")
        assert read_workflow_metadata(renamed_file).workflow_id == setup.other.workflow_id
        _assert_open_workflow_untouched(project_engine, setup, open_file_content)

    def test_rename_by_name(self, project_engine: Engine) -> None:
        """The deprecated name address takes the same path: B's file is renamed, and A is left alone."""
        setup = _open_a_and_save_b(project_engine)
        open_file_content = setup.open_file.read_text(encoding="utf-8")

        renamed = _run(
            project_engine, RenameWorkflowRequest(workflow_name=setup.other.workflow_name, requested_name="renamed")
        )

        assert isinstance(renamed, RenameWorkflowResultSuccess), renamed.result_details
        assert renamed.workflow_id == setup.other.workflow_id
        assert not Path(setup.other.file_path).exists()
        renamed_file = Path(setup.other.file_path).with_name("renamed.py")
        assert read_workflow_metadata(renamed_file).workflow_id == setup.other.workflow_id
        _assert_open_workflow_untouched(project_engine, setup, open_file_content)

    def test_move(self, project_engine: Engine, workspace: Path) -> None:
        setup = _open_a_and_save_b(project_engine)
        open_file_content = setup.open_file.read_text(encoding="utf-8")

        moved = project_engine.handle_request(
            MoveWorkflowRequest(workflow_id=setup.other.workflow_id, target_directory="elsewhere")
        )

        assert isinstance(moved, MoveWorkflowResultSuccess), moved.result_details
        assert moved.workflow_id == setup.other.workflow_id
        assert not Path(setup.other.file_path).exists()
        assert _header_id(workspace / moved.moved_file_path) == setup.other.workflow_id
        _assert_open_workflow_untouched(project_engine, setup, open_file_content)

    def test_delete(self, project_engine: Engine) -> None:
        setup = _open_a_and_save_b(project_engine)
        open_file_content = setup.open_file.read_text(encoding="utf-8")

        deleted = _run(project_engine, DeleteWorkflowRequest(workflow_id=setup.other.workflow_id))

        assert isinstance(deleted, DeleteWorkflowResultSuccess), deleted.result_details
        assert not Path(setup.other.file_path).exists()
        assert project_engine.workflow_registry.find_by_id(setup.other.workflow_id or "") is None
        _assert_open_workflow_untouched(project_engine, setup, open_file_content)

    def test_merge(self, project_engine: Engine) -> None:
        source = _saved_workflow(project_engine, "source")
        branch = project_engine.handle_request(BranchWorkflowRequest(workflow_id=source.workflow_id))
        assert isinstance(branch, BranchWorkflowResultSuccess), branch.result_details
        setup = _open_a_and_save_b(project_engine, other=source)
        open_file_content = setup.open_file.read_text(encoding="utf-8")

        merged = project_engine.handle_request(MergeWorkflowBranchRequest(workflow_id=branch.branched_workflow_id))

        assert isinstance(merged, MergeWorkflowBranchResultSuccess), merged.result_details
        assert merged.workflow_id == source.workflow_id
        assert project_engine.workflow_registry.find_by_id(branch.branched_workflow_id or "") is None
        _assert_open_workflow_untouched(project_engine, setup, open_file_content)

    def test_reset(self, project_engine: Engine) -> None:
        source = _saved_workflow(project_engine, "source")
        branch = project_engine.handle_request(BranchWorkflowRequest(workflow_id=source.workflow_id))
        assert isinstance(branch, BranchWorkflowResultSuccess), branch.result_details
        setup = _open_a_and_save_b(project_engine, other=source)
        open_file_content = setup.open_file.read_text(encoding="utf-8")

        reset = project_engine.handle_request(ResetWorkflowBranchRequest(workflow_id=branch.branched_workflow_id))

        assert isinstance(reset, ResetWorkflowBranchResultSuccess), reset.result_details
        assert reset.workflow_id == branch.branched_workflow_id
        _assert_open_workflow_untouched(project_engine, setup, open_file_content)

    def test_set_metadata(self, project_engine: Engine) -> None:
        setup = _open_a_and_save_b(project_engine)
        open_file_content = setup.open_file.read_text(encoding="utf-8")
        open_description = project_engine.workflow_registry.get(setup.open_id).metadata.description

        updated = _run(
            project_engine,
            SetWorkflowMetadataRequest(
                workflow_id=setup.other.workflow_id,
                workflow_metadata={"description": "only the other one"},  # type: ignore[arg-type]
            ),
        )

        assert isinstance(updated, SetWorkflowMetadataResultSuccess), updated.result_details
        assert read_workflow_metadata(Path(setup.other.file_path)).description == "only the other one"
        assert project_engine.workflow_registry.get(setup.open_id).metadata.description == open_description
        _assert_open_workflow_untouched(project_engine, setup, open_file_content)

    def test_save_subflow(self, project_engine: Engine) -> None:
        setup = _open_a_and_save_b(project_engine)
        open_file_content = setup.open_file.read_text(encoding="utf-8")
        subflow = project_engine.handle_request(
            CreateFlowRequest(parent_flow_name=setup.open_flow_name, flow_name="Sub", set_as_new_context=False)
        )
        assert isinstance(subflow, CreateFlowResultSuccess), subflow.result_details

        result = _run(
            project_engine, SaveSubflowToWorkflowRequest(flow_name="Sub", workflow_id=setup.other.workflow_id)
        )

        assert isinstance(result, SaveSubflowToWorkflowResultSuccess), result.result_details
        assert result.file_path == setup.other.file_path
        assert _header_id(setup.other.file_path) == setup.other.workflow_id
        _assert_open_workflow_untouched(project_engine, setup, open_file_content)


class TestRequestsAddressAWorkflowById:
    """Every request that acts on a saved workflow finds it by `workflow_id`."""

    def test_read_requests(self, project_engine: Engine) -> None:
        # Unsaved, so the info is the engine's own stub rather than a record of loading a file.
        workflow_id = _open_new_workflow(project_engine, "unsaved")

        metadata = _run(project_engine, GetWorkflowMetadataRequest(workflow_id=workflow_id))
        info = _run(project_engine, GetWorkflowInfoRequest(workflow_id=workflow_id))

        assert isinstance(metadata, GetWorkflowMetadataResultSuccess), metadata.result_details
        assert metadata.workflow_metadata.workflow_id == workflow_id
        assert isinstance(info, GetWorkflowInfoResultSuccess), info.result_details
        assert info.workflow_id == workflow_id

    def test_run_from_registry(self, project_engine: Engine) -> None:
        saved = _saved_workflow(project_engine, "runner")

        reopened = _run(project_engine, RunWorkflowFromRegistryRequest(workflow_id=saved.workflow_id))

        assert isinstance(reopened, RunWorkflowFromRegistryResultSuccess), reopened.result_details
        assert reopened.workflow_id == saved.workflow_id
        assert project_engine.context_manager.get_current_workflow_id() == saved.workflow_id

    def test_branch_compare_and_create_from_template(self, project_engine: Engine) -> None:
        source = _saved_workflow(project_engine, "source")

        branch = project_engine.handle_request(BranchWorkflowRequest(workflow_id=source.workflow_id))
        assert isinstance(branch, BranchWorkflowResultSuccess), branch.result_details
        assert branch.original_workflow_id == source.workflow_id
        compared = project_engine.handle_request(
            CompareWorkflowsRequest(workflow_id=branch.branched_workflow_id, compare_workflow_id=source.workflow_id)
        )
        assert isinstance(compared, CompareWorkflowsResultSuccess), compared.result_details
        assert compared.workflow_name == branch.branched_workflow_name
        assert compared.compare_workflow_name == source.workflow_name

        project_engine.workflow_registry.get(source.workflow_id).metadata.is_template = True  # type: ignore[arg-type]
        created = project_engine.handle_request(
            CreateWorkflowFromTemplateRequest(template_workflow_id=source.workflow_id)
        )
        assert isinstance(created, CreateWorkflowFromTemplateResultSuccess), created.result_details
        assert created.workflow_id not in (None, source.workflow_id)

    def test_compare_against_a_deleted_source_reports_no_source(self, project_engine: Engine) -> None:
        saved = _saved_workflow(project_engine, "orphan")

        compared = project_engine.handle_request(
            CompareWorkflowsRequest(workflow_id=saved.workflow_id, compare_workflow_id="deleted_source")
        )

        assert isinstance(compared, CompareWorkflowsResultSuccess), compared.result_details
        assert compared.status == "no_source"

    def test_set_workflow_context_and_ensure(self, project_engine: Engine) -> None:
        saved = _saved_workflow(project_engine, "context")

        entered = project_engine.handle_request(SetWorkflowContextRequest(workflow_id=saved.workflow_id))
        assert isinstance(entered, SetWorkflowContextSuccess), entered.result_details
        assert entered.workflow_id == saved.workflow_id
        assert entered.workflow_name == saved.workflow_name
        _close(project_engine)

        ensured = project_engine.handle_request(EnsureWorkflowAndFlowRequest(workflow_id=saved.workflow_id))
        assert isinstance(ensured, EnsureWorkflowAndFlowResultSuccess), ensured.result_details
        assert ensured.workflow_id == saved.workflow_id
        assert ensured.created_workflow

    def test_ensure_with_the_id_of_another_workflow_than_the_open_one_fails(self, project_engine: Engine) -> None:
        setup = _open_a_and_save_b(project_engine)

        refused = project_engine.handle_request(EnsureWorkflowAndFlowRequest(workflow_id=setup.other.workflow_id))
        reused = project_engine.handle_request(EnsureWorkflowAndFlowRequest(workflow_id=setup.open_id))
        reused_by_dashed_id = project_engine.handle_request(
            EnsureWorkflowAndFlowRequest(workflow_id=str(uuid.UUID(hex=setup.open_id)))
        )
        by_name = project_engine.handle_request(EnsureWorkflowAndFlowRequest(workflow_name=setup.other.workflow_name))

        assert isinstance(refused, EnsureWorkflowAndFlowResultFailure)
        assert str(refused.result_details) == (
            f"Attempted to ensure workflow '{setup.other.workflow_id}'. Failed because workflow "
            f"'{project_engine.context_manager.get_current_workflow_name()}' is in the Current Context."
        )
        assert isinstance(reused, EnsureWorkflowAndFlowResultSuccess), reused.result_details
        assert reused.workflow_id == setup.open_id
        assert not reused.created_workflow
        assert isinstance(reused_by_dashed_id, EnsureWorkflowAndFlowResultSuccess), reused_by_dashed_id.result_details
        assert reused_by_dashed_id.workflow_id == setup.open_id
        # A name keeps the lenient rule: the workflow in context is reused.
        assert isinstance(by_name, EnsureWorkflowAndFlowResultSuccess), by_name.result_details
        assert by_name.workflow_id == setup.open_id
        assert project_engine.context_manager.get_current_workflow_id() == setup.open_id

    def test_set_workflow_context_refuses_an_unknown_id(self, project_engine: Engine) -> None:
        refused = project_engine.handle_request(SetWorkflowContextRequest(workflow_id="nobody"))

        assert isinstance(refused, SetWorkflowContextFailure)
        assert str(refused.result_details) == (
            "Attempted to set the Current Context to workflow 'nobody'. Failed because no workflow has that id."
        )
        assert not project_engine.context_manager.has_current_workflow()

    def test_run_command(self, project_engine: Engine) -> None:
        saved = _saved_workflow(project_engine, "commanded")
        project_engine.workflow_registry.get(saved.workflow_id).metadata.workflow_shape = WorkflowShape()  # type: ignore[arg-type]

        command = _run(project_engine, GetWorkflowRunCommandRequest(workflow_id=saved.workflow_id))

        assert isinstance(command, GetWorkflowRunCommandResultSuccess), command.result_details
        assert Path(saved.file_path).name in command.run_command

    def test_run_command_with_no_address_uses_the_workflow_in_context(self, project_engine: Engine) -> None:
        workflow_id = _open_new_workflow(project_engine, "in_context")
        saved = _save(project_engine)
        project_engine.workflow_registry.get(workflow_id).metadata.workflow_shape = WorkflowShape()  # type: ignore[arg-type]

        command = _run(project_engine, GetWorkflowRunCommandRequest())

        assert isinstance(command, GetWorkflowRunCommandResultSuccess), command.result_details
        assert Path(saved.file_path).name in command.run_command

    def test_run_command_names_the_field_that_was_given(self, project_engine: Engine) -> None:
        refused = _run(project_engine, GetWorkflowRunCommandRequest(workflow_name="shot", file_path="shot.py"))

        assert isinstance(refused, GetWorkflowRunCommandResultFailure)
        assert "both workflow_name and file_path" in str(refused.result_details)

    def test_import_of_an_unknown_workflow_names_it(self, project_engine: Engine) -> None:
        _open_new_workflow(project_engine, "host")

        refused = _run(project_engine, ImportWorkflowAsReferencedSubFlowRequest(workflow_id="nobody"))

        assert isinstance(refused, ImportWorkflowAsReferencedSubFlowResultFailure)
        assert (
            str(refused.result_details)
            == "Attempted to import workflow 'nobody'. Failed because no workflow has that id."
        )

    def test_import_as_referenced_subflow(self, project_engine: Engine) -> None:
        referenced = _saved_workflow(project_engine, "referenced")
        _open_new_workflow(project_engine, "host")

        imported = _run(project_engine, ImportWorkflowAsReferencedSubFlowRequest(workflow_id=referenced.workflow_id))

        assert isinstance(imported, ImportWorkflowAsReferencedSubFlowResultSuccess), imported.result_details

    def test_id_and_name_of_different_workflows_fail(self, project_engine: Engine) -> None:
        first = _saved_workflow(project_engine, "first")
        second = _saved_workflow(project_engine, "second")

        refused = project_engine.handle_request(
            MoveWorkflowRequest(
                workflow_id=first.workflow_id, workflow_name=second.workflow_name, target_directory="elsewhere"
            )
        )

        assert isinstance(refused, MoveWorkflowResultFailure)
        assert Path(first.file_path).exists()
        assert Path(second.file_path).exists()

    def test_a_request_naming_no_workflow_fails(self, project_engine: Engine) -> None:
        _open_new_workflow(project_engine, "open")

        refused = _run(project_engine, DeleteWorkflowRequest())

        assert isinstance(refused, DeleteWorkflowResultFailure)
        assert "names no workflow" in str(refused.result_details)
        assert project_engine.context_manager.has_current_workflow()


# Requests that resolve an address, each built with the id no workflow has. CompareWorkflowsRequest's
# compare_workflow_id is left out: an unknown one is a "no_source" status, not a failure
# (test_compare_against_a_deleted_source_reports_no_source).
UNKNOWN_ID_REQUESTS: dict[str, Callable[[str], object]] = {
    "branch": lambda unknown_id: BranchWorkflowRequest(workflow_id=unknown_id),
    "merge": lambda unknown_id: MergeWorkflowBranchRequest(workflow_id=unknown_id),
    "reset": lambda unknown_id: ResetWorkflowBranchRequest(workflow_id=unknown_id),
    "compare": lambda unknown_id: CompareWorkflowsRequest(workflow_id=unknown_id, compare_workflow_id=unknown_id),
    "set_metadata": lambda unknown_id: SetWorkflowMetadataRequest(
        workflow_id=unknown_id,
        workflow_metadata={"description": "never written"},  # type: ignore[arg-type]
    ),
    "publish_options": lambda unknown_id: GetPublishOptionsRequest(workflow_id=unknown_id, publisher_name="lib"),
    "publish": lambda unknown_id: PublishWorkflowRequest(workflow_id=unknown_id, publisher_name="lib"),
    "run_from_registry": lambda unknown_id: RunWorkflowFromRegistryRequest(workflow_id=unknown_id),
    "run_command": lambda unknown_id: GetWorkflowRunCommandRequest(workflow_id=unknown_id),
    "save_subflow": lambda unknown_id: SaveSubflowToWorkflowRequest(flow_name="Sub", workflow_id=unknown_id),
    "set_workflow_context": lambda unknown_id: SetWorkflowContextRequest(workflow_id=unknown_id),
}


class TestRequestsForAnUnknownWorkflowIdFailAndLeaveTheOpenOneAlone:
    @pytest.mark.parametrize("make_request", UNKNOWN_ID_REQUESTS.values(), ids=UNKNOWN_ID_REQUESTS.keys())
    def test_unknown_id(self, project_engine: Engine, make_request: Callable[[str], object]) -> None:
        setup = _open_a_and_save_b(project_engine)
        open_file_content = setup.open_file.read_text(encoding="utf-8")
        other_file_content = Path(setup.other.file_path).read_text(encoding="utf-8")
        unknown_id = uuid.uuid4().hex

        result = _run(project_engine, make_request(unknown_id))

        assert result.failed(), result.result_details
        assert unknown_id in str(result.result_details)
        assert project_engine.workflow_registry.find_by_id(unknown_id) is None
        assert Path(setup.other.file_path).read_text(encoding="utf-8") == other_file_content
        _assert_open_workflow_untouched(project_engine, setup, open_file_content)


class TestPublishingAddressesAWorkflowById:
    """Publishers still receive the workflow's name, filled in on a copy of the request."""

    def _publisher(self, received: list[object]) -> RegisteredEventHandler:
        def get_publish_options(request: GetPublishOptionsRequest) -> GetPublishOptionsResultSuccess:
            received.append(request)
            return GetPublishOptionsResultSuccess(fields=[], result_details="ok")

        def publish(request: object) -> PublishWorkflowResultSuccess:
            received.append(request)
            return PublishWorkflowResultSuccess(
                published_workflow_file_path="", skip_published_workflow_registration=True, result_details="ok"
            )

        event_data = PublishWorkflowRegisteredEventData(
            start_flow_node_type="Start",
            start_flow_node_library_name="lib",
            end_flow_node_type="End",
            end_flow_node_library_name="lib",
            get_publish_options=get_publish_options,
        )
        return RegisteredEventHandler(handler=publish, library_data=MagicMock(), event_data=event_data)

    def _saved_result(self) -> SaveWorkflowResultSuccess:
        return SaveWorkflowResultSuccess(file_path="", workflow_name="", result_details="ok")

    def test_publish_options_and_publish_by_id(self, project_engine: Engine) -> None:
        workflow_id = _open_new_workflow(project_engine, "published")
        saved = _save(project_engine)
        received: list[object] = []
        publisher = self._publisher(received)

        with (
            patch.object(
                project_engine.library_manager, "get_registered_event_handlers", return_value={"lib": publisher}
            ),
            patch.object(
                project_engine.workflow_manager.saver, "save_open_workflow", return_value=self._saved_result()
            ) as save_open_workflow,
        ):
            options = project_engine.handle_request(
                GetPublishOptionsRequest(workflow_id=workflow_id, publisher_name="lib")
            )
            published = _run(project_engine, PublishWorkflowRequest(workflow_id=workflow_id, publisher_name="lib"))

        assert isinstance(options, GetPublishOptionsResultSuccess), options.result_details
        assert isinstance(published, PublishWorkflowResultSuccess), published.result_details
        assert [(request.workflow_name, request.workflow_id) for request in received] == [  # type: ignore[attr-defined]
            (saved.workflow_name, workflow_id),
            (saved.workflow_name, workflow_id),
        ]
        # The open workflow is saved first, so the latest changes are published.
        save_open_workflow.assert_called_once()

    def test_a_workflow_that_is_not_open_is_refused(self, project_engine: Engine) -> None:
        setup = _open_a_and_save_b(project_engine)
        other_content = Path(setup.other.file_path).read_text(encoding="utf-8")
        received: list[object] = []
        publisher = self._publisher(received)

        with (
            patch.object(
                project_engine.library_manager, "get_registered_event_handlers", return_value={"lib": publisher}
            ),
            patch.object(project_engine.workflow_manager.saver, "save_open_workflow") as save_open_workflow,
        ):
            options = project_engine.handle_request(
                GetPublishOptionsRequest(workflow_id=setup.other.workflow_id, publisher_name="lib")
            )
            published = _run(
                project_engine, PublishWorkflowRequest(workflow_id=setup.other.workflow_id, publisher_name="lib")
            )

        assert isinstance(options, GetPublishOptionsResultFailure)
        assert isinstance(published, PublishWorkflowResultFailure)
        assert str(published.result_details) == (
            f"Attempted to publish workflow '{setup.other.workflow_name}'. "
            "Failed because it is not open. Open it, then publish it."
        )
        # Publishers read the open graph, so neither ran, and nothing was saved over the other file.
        assert received == []
        save_open_workflow.assert_not_called()
        assert Path(setup.other.file_path).read_text(encoding="utf-8") == other_content

    def test_a_failed_save_fails_the_publish(self, project_engine: Engine) -> None:
        workflow_id = _open_new_workflow(project_engine, "published")
        _save(project_engine)
        received: list[object] = []
        publisher = self._publisher(received)

        with (
            patch.object(
                project_engine.library_manager, "get_registered_event_handlers", return_value={"lib": publisher}
            ),
            patch.object(
                project_engine.workflow_manager.saver,
                "save_open_workflow",
                return_value=SaveWorkflowResultFailure(result_details="disk full"),
            ),
        ):
            published = _run(project_engine, PublishWorkflowRequest(workflow_id=workflow_id, publisher_name="lib"))

        assert isinstance(published, PublishWorkflowResultFailure)
        assert "could not be saved first" in str(published.result_details)
        assert received == []

    def test_the_local_publisher_publishes_the_workflow_it_loaded(self, project_engine: Engine, tmp_path: Path) -> None:
        # A copy loaded from outside the workspace is registered under its absolute path, not the
        # name of the file it was copied from, so the publisher addresses the loaded workflow by id.
        saved = _saved_workflow(project_engine, "packaged")
        saved_content = Path(saved.file_path).read_text(encoding="utf-8")
        copied_file = tmp_path / "subprocess" / "workflow.py"
        copied_file.parent.mkdir()
        shutil.copy(saved.file_path, copied_file)
        received: list[object] = []
        publisher = self._publisher(received)

        with patch.object(
            project_engine.library_manager, "get_registered_event_handlers", return_value={"lib": publisher}
        ):
            asyncio.run(
                LocalWorkflowPublisher().arun(
                    workflow_path=str(copied_file),
                    publisher_name="lib",
                    published_workflow_file_name="packaged_published",
                )
            )

        loaded = project_engine.workflow_registry.find_by_path(copied_file)
        assert loaded is not None
        assert project_engine.context_manager.get_current_workflow_id() == loaded.workflow_id
        assert [(request.workflow_name, request.workflow_id) for request in received] == [  # type: ignore[attr-defined]
            (loaded.name, loaded.workflow_id)
        ]
        # The copy was saved where it was loaded from; the file it was copied from is untouched.
        assert sorted(path.name for path in Path(saved.file_path).parent.glob("*.py")) == [Path(saved.file_path).name]
        assert Path(saved.file_path).read_text(encoding="utf-8") == saved_content


class TestRenameOfAWorkflowThatIsNotOpen:
    """Renaming a closed workflow moves its file and rewrites its header's display name; it saves nothing."""

    def test_the_file_moves_and_keeps_the_id(self, project_engine: Engine) -> None:
        saved = _saved_workflow(project_engine, "Before")
        body = Path(saved.file_path).read_text(encoding="utf-8").split("# ///", 2)[2]

        renamed = _run(project_engine, RenameWorkflowRequest(workflow_id=saved.workflow_id, requested_name="after"))

        assert isinstance(renamed, RenameWorkflowResultSuccess), renamed.result_details
        renamed_file = Path(saved.file_path).with_name("after.py")
        assert not Path(saved.file_path).exists()
        metadata = read_workflow_metadata(renamed_file)
        assert metadata.workflow_id == saved.workflow_id
        # MATCH_FILE_NAME, the default: the display name follows the requested name.
        assert metadata.name == "after"
        # Only the header changed: the graph below it is the one that was saved.
        assert renamed_file.read_text(encoding="utf-8").split("# ///", 2)[2] == body
        entry = project_engine.workflow_registry.get(saved.workflow_id)  # type: ignore[arg-type]
        assert entry.name == renamed.new_workflow_name == "after"
        assert entry.absolute_path == renamed_file
        assert entry.metadata.name == "after"
        assert not project_engine.context_manager.has_current_workflow()

    def test_display_name_behavior_is_honored(self, project_engine: Engine) -> None:
        kept = _saved_workflow(project_engine, "kept")
        kept_title = read_workflow_metadata(Path(kept.file_path)).name
        overridden = _saved_workflow(project_engine, "overridden")

        kept_result = _run(
            project_engine,
            RenameWorkflowRequest(
                workflow_id=kept.workflow_id,
                requested_name="kept_file",
                display_name_behavior=RenameDisplayNameBehavior.PRESERVE_EXISTING,
            ),
        )
        overridden_result = _run(
            project_engine,
            RenameWorkflowRequest(
                workflow_id=overridden.workflow_id,
                requested_name="overridden_file",
                display_name_behavior=RenameDisplayNameBehavior.OVERRIDE,
                display_name="New Title",
            ),
        )

        assert isinstance(kept_result, RenameWorkflowResultSuccess), kept_result.result_details
        assert isinstance(overridden_result, RenameWorkflowResultSuccess), overridden_result.result_details
        assert read_workflow_metadata(Path(kept.file_path).with_name("kept_file.py")).name == kept_title
        assert read_workflow_metadata(Path(overridden.file_path).with_name("overridden_file.py")).name == "New Title"

    def test_the_file_stays_in_its_folder(self, project_engine: Engine, workspace: Path) -> None:
        saved = _saved_workflow(project_engine, "nested")
        moved = project_engine.handle_request(
            MoveWorkflowRequest(workflow_id=saved.workflow_id, target_directory="shots")
        )
        assert isinstance(moved, MoveWorkflowResultSuccess), moved.result_details

        renamed = _run(project_engine, RenameWorkflowRequest(workflow_id=saved.workflow_id, requested_name="renamed"))

        assert isinstance(renamed, RenameWorkflowResultSuccess), renamed.result_details
        assert renamed.new_workflow_name == "shots/renamed"
        assert _header_id(workspace / "shots" / "renamed.py") == saved.workflow_id

    def test_a_name_another_file_has_is_refused(self, project_engine: Engine) -> None:
        saved = _saved_workflow(project_engine, "first")
        taken = _saved_workflow(project_engine, "second")
        taken_content = Path(taken.file_path).read_text(encoding="utf-8")

        refused = _run(project_engine, RenameWorkflowRequest(workflow_id=saved.workflow_id, requested_name="second"))

        assert isinstance(refused, RenameWorkflowResultFailure)
        assert _header_id(saved.file_path) == saved.workflow_id
        assert Path(taken.file_path).read_text(encoding="utf-8") == taken_content
        assert project_engine.workflow_registry.get(saved.workflow_id).name == saved.workflow_name  # type: ignore[arg-type]

    def test_a_name_whose_file_was_deleted_outside_the_engine_is_refused(self, project_engine: Engine) -> None:
        """The stale entry still holds the path, so the rename fails before moving anything."""
        target = _saved_workflow(project_engine, "target")
        stale = _saved_workflow(project_engine, "stale")
        Path(stale.file_path).unlink()

        refused = _run(project_engine, RenameWorkflowRequest(workflow_id=target.workflow_id, requested_name="stale"))

        assert isinstance(refused, RenameWorkflowResultFailure)
        assert f"workflow '{stale.workflow_name}' is registered at" in str(refused.result_details)
        assert _header_id(target.file_path) == target.workflow_id
        assert not Path(stale.file_path).exists()
        entry = project_engine.workflow_registry.get(target.workflow_id)  # type: ignore[arg-type]
        assert entry.absolute_path == Path(target.file_path)

    def test_a_library_workflow_is_refused(self, project_engine: Engine) -> None:
        saved = _saved_workflow(project_engine, "library")
        project_engine.workflow_registry.get(saved.workflow_id).metadata.is_griptape_provided = True  # type: ignore[arg-type]

        refused = _run(project_engine, RenameWorkflowRequest(workflow_id=saved.workflow_id, requested_name="moved"))

        assert isinstance(refused, RenameWorkflowResultFailure)
        assert "a library provides it" in str(refused.result_details)
        assert Path(saved.file_path).exists()

    def test_an_unsaved_workflow_that_is_not_open_is_refused(self, project_engine: Engine) -> None:
        unsaved = project_engine.workflow_registry.register_unsaved("Scratch")

        refused = _run(project_engine, RenameWorkflowRequest(workflow_id=unsaved.workflow_id, requested_name="named"))

        assert isinstance(refused, RenameWorkflowResultFailure)
        assert "unsaved and not open" in str(refused.result_details)

    def test_renaming_only_the_letter_case_keeps_the_file(self, project_engine: Engine, workspace: Path) -> None:
        probe = workspace / "Case_Probe.txt"
        probe.write_text("probe")
        case_insensitive = (workspace / "case_probe.txt").exists()
        probe.unlink()
        if not case_insensitive:
            pytest.skip("the filesystem is case-sensitive, so the new name is a different file")
        _open_new_workflow(project_engine, "Shot")
        saved = _save(project_engine, SaveWorkflowRequest(file_name="Shot"))
        _close(project_engine)

        renamed = _run(project_engine, RenameWorkflowRequest(workflow_id=saved.workflow_id, requested_name="shot"))

        assert isinstance(renamed, RenameWorkflowResultSuccess), renamed.result_details
        entry = project_engine.workflow_registry.get(saved.workflow_id)  # type: ignore[arg-type]
        assert entry.absolute_path is not None
        assert _header_id(entry.absolute_path) == saved.workflow_id
        assert "shot.py" in [path.name for path in entry.absolute_path.parent.iterdir()]

    def test_a_failed_header_write_moves_the_file_back(self, project_engine: Engine) -> None:
        saved = _saved_workflow(project_engine, "Before")
        original_content = Path(saved.file_path).read_text(encoding="utf-8")
        renamed_file = Path(saved.file_path).with_name("after.py")

        with patch.object(
            project_engine.workflow_manager.saver,
            "write_workflow_file",
            return_value=WriteWorkflowFileResult(success=False, error_details="disk full"),
        ):
            refused = _run(project_engine, RenameWorkflowRequest(workflow_id=saved.workflow_id, requested_name="after"))

        assert isinstance(refused, RenameWorkflowResultFailure)
        assert "disk full" in str(refused.result_details)
        assert Path(saved.file_path).read_text(encoding="utf-8") == original_content
        assert not renamed_file.exists()
        entry = project_engine.workflow_registry.get(saved.workflow_id)  # type: ignore[arg-type]
        assert entry.absolute_path == Path(saved.file_path)
        assert entry.name == saved.workflow_name


def _library_workflow(engine: Engine, library_dir: Path, *, substitution_enabled: bool = True) -> Workflow:
    """Register a workflow a library provides, at `library_dir/collage_poster.py`, outside the workspace.

    Its display name differs from its file's stem, so a test can tell which one a save is named from.
    """
    _open_new_workflow(engine, "collage")
    if not substitution_enabled:
        engine.handle_request(SetVariableSubstitutionEnabledRequest(enabled=False))
    saved = _save(engine)
    _close(engine)
    source_file = Path(saved.file_path)
    library_metadata = read_workflow_metadata(source_file).model_copy(
        update={
            "workflow_id": LIBRARY_ID,
            "name": "Poster Collage",
            "description": "Lays out a poster.",
            "image": "thumbnail.webp",
            "is_griptape_provided": True,
            "is_template": True,
            "workflow_shape": WorkflowShape(inputs={"Start Flow": {}}, outputs={"End Flow": {}}),
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


class TestLibraryWorkflowsOpenAsUnsavedCopies:
    """Opening a workflow a library provides opens a new unsaved copy; the library's entry is never open."""

    def test_run_from_registry_opens_an_unsaved_copy(self, project_engine: Engine, tmp_path: Path) -> None:
        library = _library_workflow(project_engine, tmp_path / "library")
        library_file = library.absolute_path
        assert library_file is not None
        library_content = library_file.read_bytes()

        opened = _run(project_engine, RunWorkflowFromRegistryRequest(workflow_id=LIBRARY_ID))

        assert isinstance(opened, RunWorkflowFromRegistryResultSuccess), opened.result_details
        assert opened.workflow_id not in (None, LIBRARY_ID)
        assert opened.seeded_from_workflow_id == LIBRARY_ID
        assert project_engine.context_manager.get_current_workflow_id() == opened.workflow_id
        assert project_engine.context_manager.has_current_flow()
        copy = project_engine.workflow_registry.get(opened.workflow_id)  # type: ignore[arg-type]
        assert not copy.is_saved
        assert copy.seeded_from_workflow_id == LIBRARY_ID
        assert copy.metadata.name == "Poster Collage"
        assert copy.metadata.description == "Lays out a poster."
        assert copy.metadata.image == "thumbnail.webp"
        assert copy.metadata.workflow_shape == library.metadata.workflow_shape
        assert copy.metadata.is_griptape_provided is False
        assert copy.metadata.is_template is False
        # The library's entry and file are untouched, and it is not the open workflow.
        assert not project_engine.workflow_registry.is_open(LIBRARY_ID)
        assert project_engine.workflow_registry.get(LIBRARY_ID) is library
        assert library.absolute_path == library_file
        assert library.metadata.is_griptape_provided is True
        assert library_file.read_bytes() == library_content

    def test_each_open_is_a_new_copy(self, project_engine: Engine, tmp_path: Path) -> None:
        _library_workflow(project_engine, tmp_path / "library")

        first = _run(project_engine, RunWorkflowFromRegistryRequest(workflow_id=LIBRARY_ID))
        second = _run(project_engine, RunWorkflowFromRegistryRequest(workflow_id=LIBRARY_ID))

        assert isinstance(first, RunWorkflowFromRegistryResultSuccess), first.result_details
        assert isinstance(second, RunWorkflowFromRegistryResultSuccess), second.result_details
        assert second.workflow_id not in (first.workflow_id, LIBRARY_ID)
        assert second.seeded_from_workflow_id == LIBRARY_ID

    def test_a_run_without_a_clean_slate_while_one_is_open_is_refused(
        self, project_engine: Engine, tmp_path: Path
    ) -> None:
        """Workflows do not nest: the open one stays, and no copy is registered."""
        _library_workflow(project_engine, tmp_path / "library")
        open_id = _open_new_workflow(project_engine, "Open")
        registered_before = set(project_engine.workflow_registry._by_id)

        refused = _run(
            project_engine, RunWorkflowFromRegistryRequest(workflow_id=LIBRARY_ID, run_with_clean_slate=False)
        )

        assert isinstance(refused, RunWorkflowFromRegistryResultFailure)
        assert "Clear the open workflow first" in str(refused.result_details)
        assert project_engine.context_manager.get_current_workflow_id() == open_id
        assert project_engine.context_manager.has_current_flow()
        assert set(project_engine.workflow_registry._by_id) == registered_before

    def test_a_failed_open_leaves_no_copy_behind(self, project_engine: Engine, tmp_path: Path) -> None:
        # A file with only a header builds no flow, so running it fails.
        library_file = _write_workflow_file(tmp_path / "library" / "broken.py", workflow_id=LIBRARY_ID)
        library = project_engine.workflow_registry.register_file(library_file, read_workflow_metadata(library_file))
        library.metadata.is_griptape_provided = True
        listed_before = set(_listed(project_engine))

        failed = _run(project_engine, RunWorkflowFromRegistryRequest(workflow_id=LIBRARY_ID))

        assert isinstance(failed, RunWorkflowFromRegistryResultFailure)
        assert not project_engine.context_manager.has_current_workflow()
        assert set(_listed(project_engine)) == listed_before

    def test_a_failed_open_whose_file_closed_its_workflow_leaves_no_copy_behind(
        self, project_engine: Engine, tmp_path: Path
    ) -> None:
        library_file = _write_workflow_file(tmp_path / "library" / "closes_itself.py", workflow_id=LIBRARY_ID)
        # The file closes the workflow it runs in, so the run fails with nothing open.
        with library_file.open("a", encoding="utf-8") as file:
            file.write(
                "from griptape_nodes.retained_mode.events.object_events import ClearAllObjectStateRequest\n"
                "from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes\n"
                "async def build_workflow():\n"
                "    await GriptapeNodes.ahandle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))\n"
            )
        library = project_engine.workflow_registry.register_file(library_file, read_workflow_metadata(library_file))
        library.metadata.is_griptape_provided = True
        registered_before = set(project_engine.workflow_registry._by_id)

        failed = _run(project_engine, RunWorkflowFromRegistryRequest(workflow_id=LIBRARY_ID))

        assert isinstance(failed, RunWorkflowFromRegistryResultFailure)
        assert "no current workflow" in str(failed.result_details)
        assert not project_engine.context_manager.has_current_workflow()
        assert set(project_engine.workflow_registry._by_id) == registered_before

    def test_listing_and_reading_a_library_workflow_creates_no_copy(
        self, project_engine: Engine, tmp_path: Path
    ) -> None:
        _library_workflow(project_engine, tmp_path / "library")
        listed_before = set(_listed(project_engine))

        metadata = _run(project_engine, GetWorkflowMetadataRequest(workflow_id=LIBRARY_ID))

        assert isinstance(metadata, GetWorkflowMetadataResultSuccess), metadata.result_details
        assert set(_listed(project_engine)) == listed_before
        assert not project_engine.context_manager.has_current_workflow()

    def test_an_open_copy_is_not_a_callable_workflow(self, project_engine: Engine, tmp_path: Path) -> None:
        """The copy has the library's shape but no file to import, so a subflow cannot call it."""
        _library_workflow(project_engine, tmp_path / "library")
        opened = _run(project_engine, RunWorkflowFromRegistryRequest(workflow_id=LIBRARY_ID))
        assert isinstance(opened, RunWorkflowFromRegistryResultSuccess), opened.result_details

        callable_result = _run(project_engine, ListCallableWorkflowsRequest())

        assert isinstance(callable_result, ListCallableWorkflowsResultSuccess), callable_result.result_details
        callable_ids = {callable_workflow.workflow_id for callable_workflow in callable_result.callable_workflows}
        assert LIBRARY_ID in callable_ids
        assert opened.workflow_id not in callable_ids

    def test_run_from_scratch_on_a_library_file_opens_a_copy(self, project_engine: Engine, tmp_path: Path) -> None:
        library = _library_workflow(project_engine, tmp_path / "library")

        opened = _run(project_engine, RunWorkflowFromScratchRequest(file_path=str(library.absolute_path)))

        assert isinstance(opened, RunWorkflowFromScratchResultSuccess), opened.result_details
        assert opened.workflow_id not in (None, LIBRARY_ID)
        assert opened.seeded_from_workflow_id == LIBRARY_ID
        assert project_engine.context_manager.get_current_workflow_id() == opened.workflow_id
        assert not project_engine.workflow_registry.is_open(LIBRARY_ID)

    def test_a_failed_run_from_scratch_on_a_library_file_leaves_no_copy_behind(
        self, project_engine: Engine, tmp_path: Path
    ) -> None:
        library = _library_workflow(project_engine, tmp_path / "library")
        library_file = library.absolute_path
        assert library_file is not None
        # The copy opens and the graph builds; then the file fails.
        library_file.write_text(
            library_file.read_text(encoding="utf-8")
            + "\n_build_library_workflow = build_workflow\n\n"
            + "async def build_workflow():\n"
            + "    await _build_library_workflow()\n"
            + "    raise RuntimeError('The library workflow is broken.')\n",
            encoding="utf-8",
        )
        listed_before = set(_listed(project_engine))

        failed = _run(project_engine, RunWorkflowFromScratchRequest(file_path=str(library_file)))

        assert isinstance(failed, RunWorkflowFromScratchResultFailure)
        assert "The library workflow is broken." in str(failed.result_details)
        assert not project_engine.context_manager.has_current_workflow()
        assert set(_listed(project_engine)) == listed_before

    def test_a_failed_script_run_of_a_library_file_is_saved_under_the_library_file_name(
        self, project_engine: Engine, tmp_path: Path, workspace: Path
    ) -> None:
        """The copy's registry name holds a colon, which no file name on Windows may."""
        library = _library_workflow(project_engine, tmp_path / "library")
        opened = _run(project_engine, RunWorkflowFromScratchRequest(file_path=str(library.absolute_path)))
        assert isinstance(opened, RunWorkflowFromScratchResultSuccess), opened.result_details
        executor = LocalWorkflowExecutor(save_on_failure_path="")

        asyncio.run(executor._save_failed_workflow(RuntimeError("The run failed.")))

        failure_files = list(workspace.rglob("*_failed_*.py"))
        assert len(failure_files) == 1
        assert failure_files[0].name.startswith("collage_poster_failed_")

    def test_run_with_current_state_and_nothing_open_opens_a_copy(self, project_engine: Engine, tmp_path: Path) -> None:
        library = _library_workflow(project_engine, tmp_path / "library")

        opened = _run(project_engine, RunWorkflowWithCurrentStateRequest(file_path=str(library.absolute_path)))

        assert isinstance(opened, RunWorkflowWithCurrentStateResultSuccess), opened.result_details
        assert opened.workflow_id not in (None, LIBRARY_ID)
        assert opened.seeded_from_workflow_id == LIBRARY_ID
        assert not project_engine.workflow_registry.is_open(LIBRARY_ID)

    def test_a_library_workflow_file_a_script_pushes_opens_as_a_copy(
        self, project_engine: Engine, tmp_path: Path
    ) -> None:
        """Saved files push themselves with `push_workflow(file_path=__file__)` when run as a script."""
        library = _library_workflow(project_engine, tmp_path / "library")
        context_manager = project_engine.context_manager

        entered_name = context_manager.push_workflow(file_path=str(library.absolute_path))

        copy = context_manager.get_current_workflow()
        assert copy.workflow_id != LIBRARY_ID
        assert copy.name == entered_name
        assert copy.seeded_from_workflow_id == LIBRARY_ID
        assert not project_engine.workflow_registry.is_open(LIBRARY_ID)

    @pytest.mark.parametrize("address", ["workflow_id", "workflow_name"])
    def test_pushing_a_library_workflow_by_id_or_name_is_refused(
        self, project_engine: Engine, tmp_path: Path, address: str
    ) -> None:
        library = _library_workflow(project_engine, tmp_path / "library")
        push_arguments = {"workflow_id": LIBRARY_ID}
        if address == "workflow_name":
            push_arguments = {"workflow_name": library.name}

        with pytest.raises(ContextManager.LibraryWorkflowOpenError, match="a library provides it"):
            project_engine.context_manager.push_workflow(**push_arguments)

        assert not project_engine.context_manager.has_current_workflow()
        assert not project_engine.workflow_registry.is_open(LIBRARY_ID)

    def test_set_workflow_context_and_ensure_refuse_a_library_workflow(
        self, project_engine: Engine, tmp_path: Path
    ) -> None:
        library = _library_workflow(project_engine, tmp_path / "library")

        by_id = project_engine.handle_request(SetWorkflowContextRequest(workflow_id=LIBRARY_ID))
        by_name = project_engine.handle_request(SetWorkflowContextRequest(workflow_name=library.name))
        ensured = project_engine.handle_request(EnsureWorkflowAndFlowRequest(workflow_id=LIBRARY_ID))

        assert isinstance(by_id, SetWorkflowContextFailure)
        assert "a library provides it" in str(by_id.result_details)
        assert isinstance(by_name, SetWorkflowContextFailure)
        assert isinstance(ensured, EnsureWorkflowAndFlowResultFailure)
        assert "a library provides it" in str(ensured.result_details)
        assert not project_engine.context_manager.has_current_workflow()


class TestFirstSaveOfALibraryWorkflowCopy:
    """A copy's first save is an unsaved workflow's first save, named after the library's file."""

    def test_first_save_writes_a_new_file_named_after_the_library_file(
        self, project_engine: Engine, tmp_path: Path, workspace: Path
    ) -> None:
        library = _library_workflow(project_engine, tmp_path / "library")
        library_file = library.absolute_path
        assert library_file is not None
        library_content = library_file.read_bytes()
        opened = _run(project_engine, RunWorkflowFromRegistryRequest(workflow_id=LIBRARY_ID))
        assert isinstance(opened, RunWorkflowFromRegistryResultSuccess), opened.result_details

        saved = _save(project_engine)

        assert Path(saved.file_path) == workspace / "collage_poster.py"
        assert saved.workflow_id == opened.workflow_id
        saved_metadata = read_workflow_metadata(Path(saved.file_path))
        assert saved_metadata.workflow_id == opened.workflow_id
        assert saved_metadata.is_griptape_provided is False
        # The copy stays the open workflow, now saved; the library is untouched.
        assert project_engine.context_manager.get_current_workflow_id() == opened.workflow_id
        assert project_engine.context_manager.get_current_workflow().absolute_path == Path(saved.file_path)
        assert library_file.read_bytes() == library_content
        assert project_engine.workflow_registry.get(LIBRARY_ID).absolute_path == library_file

    def test_the_context_reports_the_library_workflow_a_copy_came_from(
        self, project_engine: Engine, tmp_path: Path
    ) -> None:
        _library_workflow(project_engine, tmp_path / "library")
        opened = _run(project_engine, RunWorkflowFromRegistryRequest(workflow_id=LIBRARY_ID))
        assert isinstance(opened, RunWorkflowFromRegistryResultSuccess), opened.result_details

        context = _run(project_engine, GetWorkflowContextRequest())
        assert isinstance(context, GetWorkflowContextSuccess)
        assert context.workflow_id == opened.workflow_id
        assert context.seeded_from_workflow_id == LIBRARY_ID

        _save(project_engine)
        saved_context = _run(project_engine, GetWorkflowContextRequest())
        assert isinstance(saved_context, GetWorkflowContextSuccess)
        assert saved_context.seeded_from_workflow_id is None

    def test_a_saved_copy_is_no_longer_seeded_from_the_library(self, project_engine: Engine, tmp_path: Path) -> None:
        _library_workflow(project_engine, tmp_path / "library")
        opened = _run(project_engine, RunWorkflowFromRegistryRequest(workflow_id=LIBRARY_ID))
        assert isinstance(opened, RunWorkflowFromRegistryResultSuccess), opened.result_details
        saved = _save(project_engine)
        assert project_engine.workflow_registry.get(saved.workflow_id).seeded_from_workflow_id is None  # type: ignore[arg-type]
        _close(project_engine)

        reopened = _run(project_engine, RunWorkflowFromRegistryRequest(workflow_id=saved.workflow_id))

        assert isinstance(reopened, RunWorkflowFromRegistryResultSuccess), reopened.result_details
        assert reopened.workflow_id == saved.workflow_id
        assert reopened.seeded_from_workflow_id is None

    def test_second_save_writes_the_same_file(self, project_engine: Engine, tmp_path: Path) -> None:
        _library_workflow(project_engine, tmp_path / "library")
        opened = _run(project_engine, RunWorkflowFromRegistryRequest(workflow_id=LIBRARY_ID))
        assert isinstance(opened, RunWorkflowFromRegistryResultSuccess), opened.result_details
        first = _save(project_engine)

        second = _save(project_engine, SaveWorkflowRequest(file_name=first.workflow_name))

        assert second.file_path == first.file_path
        assert second.workflow_id == opened.workflow_id
        assert _header_id(second.file_path) == opened.workflow_id

    def test_the_first_save_of_a_second_copy_never_overwrites_the_first(
        self, project_engine: Engine, tmp_path: Path, workspace: Path
    ) -> None:
        _library_workflow(project_engine, tmp_path / "library")
        _run(project_engine, RunWorkflowFromRegistryRequest(workflow_id=LIBRARY_ID))
        first = _save(project_engine)
        _run(project_engine, RunWorkflowFromRegistryRequest(workflow_id=LIBRARY_ID))

        second = _save(project_engine)

        assert Path(first.file_path) == workspace / "collage_poster.py"
        assert Path(second.file_path) == workspace / "collage_poster_1.py"
        assert _header_id(first.file_path) == first.workflow_id
        assert second.workflow_id != first.workflow_id

    def test_a_versioned_first_save_is_named_after_the_library_file(
        self, project_engine: Engine, tmp_path: Path
    ) -> None:
        _library_workflow(project_engine, tmp_path / "library")
        opened = _run(project_engine, RunWorkflowFromRegistryRequest(workflow_id=LIBRARY_ID))
        assert isinstance(opened, RunWorkflowFromRegistryResultSuccess), opened.result_details

        saved = _save(project_engine, SaveWorkflowRequest(create_versioned=True))

        assert Path(saved.file_path).name.startswith("collage_poster")
        assert saved.workflow_id == opened.workflow_id

    def test_variable_substitution_stays_off_across_the_first_save(
        self, project_engine: Engine, tmp_path: Path
    ) -> None:
        _library_workflow(project_engine, tmp_path / "library", substitution_enabled=False)
        opened = _run(project_engine, RunWorkflowFromRegistryRequest(workflow_id=LIBRARY_ID))
        assert isinstance(opened, RunWorkflowFromRegistryResultSuccess), opened.result_details
        assert project_engine.workflow_manager.variable_substitution.is_enabled() is False

        saved = _save(project_engine)

        assert project_engine.workflow_manager.variable_substitution.is_enabled() is False
        assert "SetVariableSubstitutionEnabledRequest" in Path(saved.file_path).read_text(encoding="utf-8")
