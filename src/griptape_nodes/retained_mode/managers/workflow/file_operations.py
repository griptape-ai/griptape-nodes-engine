from __future__ import annotations

import logging
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

import anyio

from griptape_nodes.files.path_utils import is_same_file
from griptape_nodes.files.project_file import ProjectFileDestination
from griptape_nodes.node_library.workflow_registry import (
    UnreadableWorkflowHeaderError,
    WorkflowAddressError,
    WorkflowPathTakenError,
)
from griptape_nodes.retained_mode.engine import EngineScoped
from griptape_nodes.retained_mode.events.base_events import ResultDetail, ResultDetails
from griptape_nodes.retained_mode.events.os_events import (
    DeleteFileRequest,
    DeletionBehavior,
    ExistingFilePolicy,
    RenameFileRequest,
)
from griptape_nodes.retained_mode.events.workflow_events import (
    DeleteWorkflowRequest,
    DeleteWorkflowResultFailure,
    DeleteWorkflowResultSuccess,
    MoveWorkflowRequest,
    MoveWorkflowResultFailure,
    MoveWorkflowResultSuccess,
    RenameDisplayNameBehavior,
    RenameWorkflowRequest,
    RenameWorkflowResultFailure,
    RenameWorkflowResultSuccess,
    SaveWorkflowRequest,
    SaveWorkflowResultSuccess,
)
from griptape_nodes.retained_mode.request_handlers import RequestScope, handles
from griptape_nodes.utils.string_utils import normalize_display_name

if TYPE_CHECKING:
    from griptape_nodes.node_library.workflow_registry import Workflow
    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.events.base_events import ResultPayload
    from griptape_nodes.retained_mode.managers.event_manager import EventManager
    from griptape_nodes.retained_mode.runtime.workflow_runtime import WorkflowRuntime


logger = logging.getLogger("griptape_nodes")


class WorkflowFileOperations(EngineScoped):
    def __init__(self, event_manager: EventManager, *, engine: Engine | None = None) -> None:
        super().__init__(engine)
        event_manager.register_request_handlers(self)

    @handles(DeleteWorkflowRequest, scope=RequestScope.HOST)
    async def on_delete_workflows_request(self, request: DeleteWorkflowRequest) -> ResultPayload:
        # If the deleted workflow is open, tear down its flows/nodes and close it BEFORE
        # removing the registry entry, which refuses to drop an open workflow's entry.
        # Deleting a workflow that isn't open (e.g. published-workflow subprocess cleanup)
        # goes straight to the registry/file cleanup and leaves any open workflow alone.
        registry = self.engine.workflow_registry
        try:
            workflow = registry.resolve_address(
                workflow_id=request.workflow_id, workflow_name=request.name, operation="delete", name_field="name"
            )
        except WorkflowAddressError as err:
            return DeleteWorkflowResultFailure(result_details=str(err))

        open_target = self.engine.open_workflows.find_by_id(workflow.workflow_id)
        if open_target is not None:
            await self.engine.close_workflow_state(open_target)
        try:
            registry.remove(workflow.workflow_id)
        except Exception as e:
            details = f"Failed to remove workflow from registry with name '{workflow.name}'. Exception: {e}"
            return DeleteWorkflowResultFailure(result_details=details)
        # Unsaved workflows have no backing file or config entry; dropping the registry
        # entry is the entire operation.
        workflow_file_path = workflow.file_path
        if workflow_file_path is None or workflow.absolute_path is None:
            return DeleteWorkflowResultSuccess(
                result_details=ResultDetails(
                    message=f"Successfully deleted unsaved workflow: {workflow.name}", level=logging.INFO
                )
            )
        delete_failure = await self._delete_workflow_file(workflow_file_path, workflow.absolute_path)
        if delete_failure is not None:
            details = f"Failed to delete workflow file with path '{workflow_file_path}'. {delete_failure}"
            return DeleteWorkflowResultFailure(result_details=details)
        return DeleteWorkflowResultSuccess(
            result_details=ResultDetails(message=f"Successfully deleted workflow: {workflow.name}", level=logging.INFO)
        )

    @handles(RenameWorkflowRequest, scope=RequestScope.HOST)
    async def on_rename_workflow_request(self, request: RenameWorkflowRequest) -> ResultPayload:
        # The one lookup of the workflow being renamed. Everything below acts on this entry, so a
        # request addressed to one workflow can never save or delete another.
        try:
            source = self.engine.workflow_registry.resolve_address(
                workflow_id=request.workflow_id, workflow_name=request.workflow_name, operation="rename"
            )
        except WorkflowAddressError as err:
            return RenameWorkflowResultFailure(result_details=str(err))

        # Sanitize to a Python module-friendly name for the file stem (registry key).
        sanitized_stem = normalize_display_name(request.requested_name)
        if not sanitized_stem:
            details = f"Attempted to rename workflow '{source.name}'. The requested name '{request.requested_name}' produced an empty file name after sanitization."
            return RenameWorkflowResultFailure(result_details=details)

        display_name_error = self._validate_rename_display_name(request, source=source)
        if display_name_error is not None:
            return RenameWorkflowResultFailure(result_details=display_name_error)

        display_name = self._resolve_rename_display_name(request, source=source)

        # Rename keeps the workflow's location (unlike Move). Inherit the source workflow's
        # directory and prepend it to the sanitized stem so the renamed file stays put:
        # a workspace sub-dir ("bar/new_name") or an external absolute path ("/ext/new_name").
        # The combined name is NOT re-run through normalize_display_name, so its "/" survives.
        requested_file_name = sanitized_stem
        if source.file_path:
            source_dir = PurePosixPath(source.file_path.replace("\\", "/")).parent
            if str(source_dir) not in ("", "."):
                requested_file_name = f"{source_dir}/{sanitized_stem}"

        open_source = self.engine.open_workflows.find_by_id(source.workflow_id)
        if open_source is not None:
            return await self._rename_open_workflow(
                open_source,
                source=source,
                requested_file_name=requested_file_name,
                display_name=display_name,
            )
        return await self._rename_closed_workflow(
            source=source, sanitized_stem=sanitized_stem, display_name=display_name
        )

    async def _rename_open_workflow(
        self,
        open_source: WorkflowRuntime,
        *,
        source: Workflow,
        requested_file_name: str,
        display_name: str,
    ) -> ResultPayload:
        """Rename an open workflow: save its live graph to the new file under its own id, then delete the old file.

        `open_source` is the runtime of `source`. The workflow stays open and keeps its id; only its
        file and name change. An open workflow is never a library's: a library workflow opens as an
        unsaved copy, which a rename saves.
        """
        previous_absolute_path = source.absolute_path
        previous_file_path = source.file_path
        previous_name = source.name

        save_result = await self.engine.workflow_manager.saver.save_open_workflow(
            open_source,
            SaveWorkflowRequest(file_name=requested_file_name, display_name=display_name),
            keep_open_workflow_identity=True,
        )
        if not isinstance(save_result, SaveWorkflowResultSuccess):
            details = f"Attempted to rename workflow '{previous_name}' to '{requested_file_name}'. Failed while attempting to save."
            return RenameWorkflowResultFailure(result_details=details)

        # If the renamed file landed outside the workspace, keep it registered at its new path.
        self.engine.workflow_manager.persist_external_workflow_registration(str(save_result.file_path))

        new_absolute_path = source.absolute_path
        if previous_absolute_path is not None and new_absolute_path is not None:
            cleanup_failure = await self._retire_renamed_file(
                previous_file_path, previous_absolute_path, new_absolute_path
            )
            if cleanup_failure is not None:
                details = (
                    f"Attempted to rename workflow '{previous_name}' to '{save_result.workflow_name}'. "
                    f"Failed while updating the original file '{previous_absolute_path}': {cleanup_failure}"
                )
                return RenameWorkflowResultFailure(result_details=details)

        return RenameWorkflowResultSuccess(
            new_workflow_name=save_result.workflow_name,
            workflow_id=source.workflow_id,
            result_details=ResultDetails(
                message=f"Successfully renamed workflow to: {save_result.workflow_name}", level=logging.INFO
            ),
        )

    async def _rename_closed_workflow(  # noqa: PLR0911
        self,
        *,
        source: Workflow,
        sanitized_stem: str,
        display_name: str,
    ) -> ResultPayload:
        """Rename a workflow that is not open: move its file and rewrite the display name in its header.

        The workflow keeps its id. Nothing is saved: the open graph belongs to another workflow,
        and neither it nor its file is touched.
        """
        previous_name = source.name
        previous_file_path = source.file_path
        previous_absolute_path = source.absolute_path
        if previous_absolute_path is None or previous_file_path is None:
            details = (
                f"Attempted to rename workflow '{previous_name}'. Failed because it is unsaved and not open, "
                "so it has no file to rename. Open the workflow and save it first."
            )
            return RenameWorkflowResultFailure(result_details=details)
        if source.metadata.is_griptape_provided:
            details = (
                f"Attempted to rename workflow '{previous_name}'. Failed because a library provides it, and a "
                "library's files are never changed. Create a workflow from it, then rename that."
            )
            return RenameWorkflowResultFailure(result_details=details)
        # A rename keeps the file where it is; only its name changes.
        new_absolute_path = previous_absolute_path.with_name(f"{sanitized_stem}{previous_absolute_path.suffix}")
        # Checked before touching the disk. The move below refuses only a file that is there, and an
        # entry can outlive its file when the file is deleted outside the engine.
        holder = self.engine.workflow_registry.find_by_path(new_absolute_path)
        if holder is not None and holder is not source:
            details = (
                f"Attempted to rename workflow '{previous_name}' to '{sanitized_stem}'. Failed because workflow "
                f"'{holder.name}' is registered at '{new_absolute_path}'. Delete or rename that workflow first."
            )
            return RenameWorkflowResultFailure(result_details=details)
        try:
            renamed_metadata = source.metadata_for_header().model_copy(update={"name": display_name})
        except UnreadableWorkflowHeaderError as err:
            details = f"Attempted to rename workflow '{previous_name}'. Failed because {err}"
            return RenameWorkflowResultFailure(result_details=details)

        try:
            previous_content = await anyio.Path(previous_absolute_path).read_text(encoding="utf-8")
        except OSError as err:
            details = (
                f"Attempted to rename workflow '{previous_name}'. "
                f"Failed because its file '{previous_absolute_path}' could not be read: {err}"
            )
            return RenameWorkflowResultFailure(result_details=details)
        renamed_content = self.engine.workflow_manager.codegen.replace_workflow_metadata_header(
            previous_content, renamed_metadata
        )
        if renamed_content is None:
            details = (
                f"Attempted to rename workflow '{previous_name}'. "
                f"Failed because the metadata header in '{previous_absolute_path}' could not be rewritten."
            )
            return RenameWorkflowResultFailure(result_details=details)

        if new_absolute_path.name != previous_absolute_path.name:
            move_result = await self.engine.ahandle_request(
                RenameFileRequest(
                    old_path=str(previous_absolute_path), new_path=str(new_absolute_path), workspace_only=False
                )
            )
            if move_result.failed():
                details = (
                    f"Attempted to rename workflow '{previous_name}' to '{sanitized_stem}'. "
                    f"Failed while moving its file: {move_result.result_details}"
                )
                return RenameWorkflowResultFailure(result_details=details)

        write_result = self.engine.workflow_manager.saver.write_workflow_file(
            destination=ProjectFileDestination(
                str(new_absolute_path), existing_file_policy=ExistingFilePolicy.OVERWRITE
            ),
            content=renamed_content,
            file_name=sanitized_stem,
        )
        if not write_result.success:
            restore_failure = await self._restore_renamed_file(new_absolute_path, previous_absolute_path)
            details = (
                f"Attempted to rename workflow '{previous_name}' to '{sanitized_stem}'. "
                f"Failed while writing its new display name: {write_result.error_details}{restore_failure}"
            )
            return RenameWorkflowResultFailure(result_details=details)

        self.engine.workflow_registry.set_file_path(source, new_absolute_path)
        source.metadata = renamed_metadata
        # An externally imported workflow is remembered in the user config by its path.
        self.engine.config_manager.delete_user_workflow(previous_file_path)
        self.engine.workflow_manager.persist_external_workflow_registration(str(new_absolute_path))

        return RenameWorkflowResultSuccess(
            new_workflow_name=source.name,
            workflow_id=source.workflow_id,
            result_details=ResultDetails(
                message=f"Successfully renamed workflow to: {source.name}", level=logging.INFO
            ),
        )

    async def _restore_renamed_file(self, new_absolute_path: Path, previous_absolute_path: Path) -> str:
        """Move a renamed file back after its header could not be rewritten. Returns what to add to the failure."""
        if new_absolute_path.name == previous_absolute_path.name:
            return ""
        restore_result = await self.engine.ahandle_request(
            RenameFileRequest(
                old_path=str(new_absolute_path), new_path=str(previous_absolute_path), workspace_only=False
            )
        )
        if restore_result.failed():
            return (
                f" The file could not be moved back to '{previous_absolute_path}' and is now at '{new_absolute_path}'."
            )
        return ""

    async def _retire_renamed_file(
        self, previous_file_path: str | None, previous_absolute_path: Path, new_absolute_path: Path
    ) -> str | None:
        """Remove the file a rename saved away from, or give it the new name's letter case.

        Compared as files, not paths: on a case-insensitive filesystem a rename that changes only
        the letter case saved into the same file, and deleting the "old" one would delete it.
        Returns why the file could not be updated, or None once it is.
        """
        if not is_same_file(previous_absolute_path, new_absolute_path):
            return await self._delete_workflow_file(previous_file_path, previous_absolute_path)
        if previous_absolute_path.name == new_absolute_path.name:
            return None
        rename_result = await self.engine.ahandle_request(
            RenameFileRequest(
                old_path=str(previous_absolute_path), new_path=str(new_absolute_path), workspace_only=False
            )
        )
        if rename_result.failed():
            return str(rename_result.result_details)
        return None

    async def _delete_workflow_file(self, workflow_file_path: str | None, absolute_path: Path) -> str | None:
        """Forget a workflow file in the user config and move it to the recycle bin.

        Returns why the file could not be deleted, or None once it is.
        """
        if workflow_file_path is not None:
            # Remove the config entry an externally imported workflow has.
            self.engine.config_manager.delete_user_workflow(workflow_file_path)
        delete_result = await self.engine.ahandle_request(
            DeleteFileRequest(
                path=str(absolute_path),
                workspace_only=False,
                deletion_behavior=DeletionBehavior.PREFER_RECYCLE_BIN,
            )
        )
        if delete_result.failed():
            return str(delete_result.result_details)
        return None

    @handles(MoveWorkflowRequest, scope=RequestScope.HOST)
    def on_move_workflow_request(self, request: MoveWorkflowRequest) -> ResultPayload:  # noqa: C901, PLR0911
        try:
            workflow = self.engine.workflow_registry.resolve_address(
                workflow_id=request.workflow_id, workflow_name=request.workflow_name, operation="move"
            )
        except WorkflowAddressError as err:
            return MoveWorkflowResultFailure(result_details=str(err))
        workflow_name = workflow.name

        # Move is a disk-level operation; unsaved workflows have nothing to move.
        if workflow.file_path is None:
            details = (
                f"Cannot move unsaved workflow '{workflow_name}' because it has no file on disk. "
                "Save the workflow before moving it."
            )
            return MoveWorkflowResultFailure(result_details=details)
        old_relative_path = workflow.file_path

        config_manager = self.engine.config_manager

        # Get current file path
        current_file_path = self.engine.workflow_registry.get_complete_file_path(old_relative_path)
        if not Path(current_file_path).exists():
            details = f"Failed to move workflow '{workflow_name}': File path '{current_file_path}' does not exist."
            return MoveWorkflowResultFailure(result_details=details)

        # Clean and validate target directory
        target_directory = request.target_directory.strip().replace("\\", "/")
        target_directory = target_directory.removeprefix("/")  # Remove leading slash

        # Create target directory path
        target_dir_path = config_manager.workspace_path / target_directory

        try:
            # Create target directory if it doesn't exist
            target_dir_path.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            details = f"Failed to create target directory '{target_dir_path}': {e!s}"
            return MoveWorkflowResultFailure(result_details=details)

        # Create new file path
        workflow_filename = Path(old_relative_path).name
        new_relative_path = (Path(target_directory) / workflow_filename).as_posix()
        new_absolute_path = config_manager.workspace_path / new_relative_path

        # Check if target file already exists
        if new_absolute_path.exists():
            details = f"Failed to move workflow '{workflow_name}': Target file '{new_absolute_path}' already exists."
            return MoveWorkflowResultFailure(result_details=details)

        try:
            # Move the file
            Path(current_file_path).rename(new_absolute_path)

            # The workflow keeps its id; its path and name follow the file. The context reads
            # both from this entry, so `workflow_dir` follows the move too.
            self.engine.workflow_registry.set_file_path(workflow, new_absolute_path)

            # Remove old config entry if it existed (e.g. workflow was externally imported)
            config_manager.delete_user_workflow(old_relative_path)

        # The registry can refuse the new path only once the file is there (a stale entry may
        # still hold it), so its refusal rolls the file back like a failed move.
        except (OSError, WorkflowPathTakenError) as e:
            error_messages = []
            main_error = f"Failed to move workflow file '{current_file_path}' to '{new_absolute_path}': {e!s}"
            error_messages.append(ResultDetail(message=main_error, level=logging.ERROR))

            # Attempt to rollback if file was moved but registry update failed
            if new_absolute_path.exists() and not Path(current_file_path).exists():
                try:
                    new_absolute_path.rename(current_file_path)
                    rollback_message = f"Rolled back file move for workflow '{workflow_name}'"
                    error_messages.append(ResultDetail(message=rollback_message, level=logging.INFO))
                except OSError:
                    rollback_failure = f"Failed to rollback file move for workflow '{workflow_name}'"
                    error_messages.append(ResultDetail(message=rollback_failure, level=logging.ERROR))

            return MoveWorkflowResultFailure(result_details=ResultDetails(*error_messages))
        except Exception as e:
            details = f"Failed to move workflow '{workflow_name}': {e!s}"
            return MoveWorkflowResultFailure(result_details=details)
        else:
            details = f"Successfully moved workflow '{workflow_name}' to '{new_relative_path}'"
            return MoveWorkflowResultSuccess(
                moved_file_path=new_relative_path,
                new_workflow_name=workflow.name,
                workflow_id=workflow.workflow_id,
                result_details=ResultDetails(message=details, level=logging.INFO),
            )

    def _validate_rename_display_name(self, request: RenameWorkflowRequest, *, source: Workflow) -> str | None:
        """Return a failure message when the request's display-name arguments are invalid, else None.

        Two failure conditions:
        * ``display_name`` supplied with a non-OVERRIDE behavior — the field would be silently
          ignored, which is almost always a caller mistake (e.g. picked OVERRIDE mentally but
          forgot to switch the enum).
        * OVERRIDE with a missing or blank ``display_name`` — the field is the whole point of
          OVERRIDE mode and must carry a non-empty value.

        PRESERVE_EXISTING and MATCH_FILE_NAME with ``display_name=None`` derive the value from
        existing state or the requested file name and have nothing to check.
        """
        if request.display_name is not None and request.display_name_behavior is not RenameDisplayNameBehavior.OVERRIDE:
            return (
                f"Attempted to rename workflow '{source.name}' with "
                f"display_name_behavior={request.display_name_behavior.value} and display_name="
                f"{request.display_name!r}. Failed because 'display_name' is only consulted when "
                "display_name_behavior=OVERRIDE. Either switch to OVERRIDE, or remove display_name."
            )
        if request.display_name_behavior is not RenameDisplayNameBehavior.OVERRIDE:
            return None
        if request.display_name and request.display_name.strip():
            return None
        return (
            f"Attempted to rename workflow '{source.name}' with display_name_behavior=OVERRIDE. "
            "Failed because 'display_name' was not provided or was empty. Provide a non-empty display_name, "
            "or use PRESERVE_EXISTING / MATCH_FILE_NAME behavior."
        )

    def _resolve_rename_display_name(self, request: RenameWorkflowRequest, *, source: Workflow) -> str:
        """Compute the display name (``metadata.name``) the renamed workflow gets.

        ``source`` is the registry entry the request addressed, resolved once by the handler.

        * ``OVERRIDE`` — return the caller-supplied ``display_name`` stripped of surrounding
          whitespace. ``_validate_rename_display_name`` guarantees it's non-empty after strip;
          if this invariant is ever violated that's a genuine engine bug, so ``assert`` is
          used to document it rather than a defensive fallback.
        * ``PRESERVE_EXISTING`` — return the source workflow's current ``metadata.name`` (stripped).
          If its ``metadata.name`` is blank, fall through to the
          requested name — better a sensible file-stem-derived label than propagating a corrupt
          empty display name onto the renamed workflow.
        * ``MATCH_FILE_NAME`` — return the raw ``requested_name`` (legacy behavior; display name
          tracks the new file name).
        """
        if request.display_name_behavior is RenameDisplayNameBehavior.OVERRIDE:
            # Invariant established by _validate_rename_display_name — reaching this branch
            # with display_name=None is a genuine engine bug, so surface it loudly.
            if request.display_name is None:
                msg = "OVERRIDE reached _resolve_rename_display_name with display_name=None"
                raise RuntimeError(msg)
            return request.display_name.strip()
        if request.display_name_behavior is RenameDisplayNameBehavior.PRESERVE_EXISTING:
            preserved = (source.metadata.name or "").strip()
            if preserved:
                return preserved
        return request.requested_name
