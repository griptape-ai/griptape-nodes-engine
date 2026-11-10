from __future__ import annotations

import logging
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from griptape_nodes.files.project_file import ProjectFileDestination
from griptape_nodes.node_library.workflow_registry import (
    OpenWorkflowRemovalError,
    WorkflowAddressError,
    WorkflowPathTakenError,
)
from griptape_nodes.retained_mode.engine import EngineScoped
from griptape_nodes.retained_mode.events.base_events import ResultDetail, ResultDetails
from griptape_nodes.retained_mode.events.os_events import (
    DeleteFileRequest,
    DeleteFileResultFailure,
    DeletionBehavior,
    ExistingFilePolicy,
    FileIOFailureReason,
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
    SaveWorkflowResultFailure,
    SaveWorkflowResultSuccess,
)
from griptape_nodes.retained_mode.request_handlers import handles
from griptape_nodes.utils.string_utils import normalize_display_name

if TYPE_CHECKING:
    from griptape_nodes.node_library.workflow_registry import (
        Workflow,
    )
    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.events.base_events import ResultPayload
    from griptape_nodes.retained_mode.managers.event_manager import EventManager
    from griptape_nodes.retained_mode.runtime.workflow_runtime import WorkflowRuntime


logger = logging.getLogger("griptape_nodes")


class WorkflowFileOperations(EngineScoped):
    def __init__(self, event_manager: EventManager, *, engine: Engine | None = None) -> None:
        super().__init__(engine)
        event_manager.register_request_handlers(self)

    @handles(DeleteWorkflowRequest)
    async def on_delete_workflows_request(self, request: DeleteWorkflowRequest) -> ResultPayload:
        registry = self.engine.workflow_registry
        try:
            workflow = registry.resolve_address(
                workflow_id=request.workflow_id, workflow_name=request.name, action="delete"
            )
        except WorkflowAddressError as err:
            return DeleteWorkflowResultFailure(result_details=str(err))
        workflow_name = workflow.name
        # The registry refuses to drop an open workflow's entry, so close it first. Deleting a
        # workflow that isn't open leaves every open workflow alone.
        open_target = self.engine.open_workflows.find(workflow.workflow_id)
        if open_target is not None:
            await self.engine.aclose_workflow(open_target)
        try:
            registry.remove(workflow.workflow_id)
        except (KeyError, OpenWorkflowRemovalError) as e:
            details = f"Failed to remove workflow from registry with name '{workflow_name}'. Exception: {e}"
            return DeleteWorkflowResultFailure(result_details=details)
        # Unsaved workflows have no backing file or config entry; dropping the registry
        # entry is the entire operation.
        workflow_file_path = workflow.file_path
        if workflow_file_path is None:
            return DeleteWorkflowResultSuccess(
                workflow_id=workflow.workflow_id,
                result_details=ResultDetails(
                    message=f"Successfully deleted unsaved workflow: {workflow_name}", level=logging.INFO
                ),
            )
        config_manager = self.engine.config_manager
        try:
            config_manager.delete_user_workflow(workflow_file_path)
        except Exception as e:
            details = f"Failed to remove workflow from user config with name '{workflow_name}'. Exception: {e}"
            return DeleteWorkflowResultFailure(result_details=details)
        # delete the actual file
        full_path = config_manager.workspace_path.joinpath(workflow_file_path)

        delete_request = DeleteFileRequest(
            path=str(full_path),
            workspace_only=False,
            deletion_behavior=DeletionBehavior.PREFER_RECYCLE_BIN,
        )
        delete_result = await self.engine.ahandle_request(delete_request)
        if isinstance(delete_result, DeleteFileResultFailure):
            details = f"Failed to delete workflow file with path '{workflow_file_path}'. {delete_result.result_details}"
            return DeleteWorkflowResultFailure(result_details=details)
        return DeleteWorkflowResultSuccess(
            workflow_id=workflow.workflow_id,
            result_details=ResultDetails(message=f"Successfully deleted workflow: {workflow_name}", level=logging.INFO),
        )

    @handles(RenameWorkflowRequest)
    async def on_rename_workflow_request(self, request: RenameWorkflowRequest) -> ResultPayload:
        """Rename the workflow the request names, keeping its id.

        An open workflow saves its own graph to the new file. A closed one moves its file and
        rewrites the name in its header.
        """
        try:
            source = self.engine.workflow_registry.resolve_address(
                workflow_id=request.workflow_id, workflow_name=request.workflow_name, action="rename"
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

        open_source = self.engine.open_workflows.find(source.workflow_id)
        if open_source is not None:
            return await self._rename_open_workflow(
                open_source, source=source, sanitized_stem=sanitized_stem, display_name=display_name
            )
        return await self._rename_closed_workflow(source, sanitized_stem=sanitized_stem, display_name=display_name)

    @handles(MoveWorkflowRequest)
    def on_move_workflow_request(self, request: MoveWorkflowRequest) -> ResultPayload:  # noqa: C901, PLR0911
        try:
            workflow = self.engine.workflow_registry.resolve_address(
                workflow_id=request.workflow_id, workflow_name=request.workflow_name, action="move"
            )
        except WorkflowAddressError as err:
            return MoveWorkflowResultFailure(result_details=str(err))
        # The name before the move, for messages: the entry's name follows its file.
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

        # A stale entry can still hold the target path, or the name it gives, after its file is
        # gone, and the registry refuses to give one path or name to two workflows. Ask before
        # the file moves, so a refusal leaves it where it is.
        try:
            self.engine.workflow_registry.raise_if_path_taken(workflow, new_relative_path)
        except WorkflowPathTakenError as err:
            details = f"Failed to move workflow '{workflow_name}': Workflow '{err.existing.name}' is already registered at '{new_absolute_path}'."
            return MoveWorkflowResultFailure(result_details=details)

        try:
            # Move the file
            Path(current_file_path).rename(new_absolute_path)

            # The workflow keeps its id; its path and name follow the file. The context reads
            # both from this entry, so `workflow_dir` follows the move too.
            self.engine.workflow_registry.set_file_path(workflow, new_relative_path)

            # Remove old config entry if it existed (e.g. workflow was externally imported)
            config_manager.delete_user_workflow(old_relative_path)

        except OSError as e:
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

    async def _rename_open_workflow(
        self,
        open_source: WorkflowRuntime,
        *,
        source: Workflow,
        sanitized_stem: str,
        display_name: str,
    ) -> ResultPayload:
        """Rename an open workflow: save its live graph to the new file under its own id, then delete the old file.

        `open_source` is the runtime of `source`. The workflow stays open and keeps its id; only its
        file and name change.
        """
        previous_name = source.name
        previous_absolute_path = source.absolute_path
        previous_file_path = source.file_path

        # Rename keeps the workflow's location (unlike Move). Inherit the source workflow's
        # directory and prepend it to the sanitized stem so the renamed file stays put:
        # a workspace sub-dir ("bar/new_name") or an external absolute path ("/ext/new_name").
        # The combined name is NOT re-run through normalize_display_name, so its "/" survives.
        requested_file_name = sanitized_stem
        if previous_file_path:
            source_dir = PurePosixPath(previous_file_path.replace("\\", "/")).parent
            if str(source_dir) not in ("", "."):
                requested_file_name = f"{source_dir}/{sanitized_stem}"

        # Bound as dispatch binds a SaveWorkflowRequest's workflow: codegen reads some of its
        # settings, such as variable substitution, through the binding. `overwrite_existing=False`
        # refuses a new name that is another workflow's.
        with self.engine.bind_workflow(open_source):
            save_result = await self.engine.workflow_manager.saver.save_open_workflow(
                open_source,
                SaveWorkflowRequest(file_name=requested_file_name, display_name=display_name, overwrite_existing=False),
                keep_open_workflow_identity=True,
            )
        if not isinstance(save_result, SaveWorkflowResultSuccess):
            reason = f"saving it there failed: {save_result.result_details}"
            if (
                isinstance(save_result, SaveWorkflowResultFailure)
                and save_result.failure_reason == FileIOFailureReason.POLICY_NO_OVERWRITE
            ):
                reason = "another workflow already has that name"
            details = (
                f"Attempted to rename workflow '{previous_name}' to '{requested_file_name}'. Failed because {reason}."
            )
            return RenameWorkflowResultFailure(result_details=details)

        # If the renamed file landed outside the workspace, keep it registered at its new path.
        self.engine.workflow_manager.persist_external_workflow_registration(str(save_result.file_path))

        cleanup_warnings: list[ResultDetail] = []
        if previous_file_path is not None and previous_absolute_path is not None:
            cleanup_warnings = await self._remove_file_renamed_from(
                source,
                previous_name=previous_name,
                previous_file_path=previous_file_path,
                previous_absolute_path=previous_absolute_path,
            )

        return RenameWorkflowResultSuccess(
            new_workflow_name=save_result.workflow_name,
            workflow_id=source.workflow_id,
            result_details=ResultDetails(
                ResultDetail(
                    message=f"Successfully renamed workflow to: {save_result.workflow_name}", level=logging.INFO
                ),
                *cleanup_warnings,
            ),
        )

    async def _rename_closed_workflow(
        self, source: Workflow, *, sanitized_stem: str, display_name: str
    ) -> ResultPayload:
        """Rename a workflow that isn't open: move its file and rewrite the name in its header.

        The file stays in its folder. No graph is involved, so the file's content is unchanged
        apart from its header.
        """
        previous_name = source.name
        previous_absolute_path = source.absolute_path
        previous_file_path = source.file_path
        if previous_absolute_path is None or previous_file_path is None:
            details = (
                f"Attempted to rename workflow '{previous_name}'. Failed because it is neither saved nor open, "
                "so it has no file or graph to rename."
            )
            return RenameWorkflowResultFailure(result_details=details)
        new_absolute_path = previous_absolute_path.with_name(f"{sanitized_stem}{previous_absolute_path.suffix}")
        moves_file = new_absolute_path != previous_absolute_path
        refusal = self._refuse_closed_rename(source, sanitized_stem=sanitized_stem, new_absolute_path=new_absolute_path)
        if refusal is not None:
            return RenameWorkflowResultFailure(result_details=refusal)

        try:
            content = previous_absolute_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as err:
            details = (
                f"Attempted to rename workflow '{previous_name}'. Failed because its file could not be read: {err}"
            )
            return RenameWorkflowResultFailure(result_details=details)
        renamed_metadata = source.metadata.model_copy(update={"name": display_name})
        renamed_content = self.engine.workflow_manager.codegen.replace_workflow_metadata_header(
            content, renamed_metadata
        )
        if renamed_content is None:
            details = f"Attempted to rename workflow '{previous_name}'. Failed because its metadata header could not be rewritten."
            return RenameWorkflowResultFailure(result_details=details)

        # FAIL refuses to replace a file that appeared since the check above.
        existing_file_policy = ExistingFilePolicy.FAIL if moves_file else ExistingFilePolicy.OVERWRITE
        write_result = self.engine.workflow_manager.saver.write_workflow_file(
            ProjectFileDestination(str(new_absolute_path), existing_file_policy=existing_file_policy),
            renamed_content,
            sanitized_stem,
        )
        if not write_result.success:
            details = (
                f"Attempted to rename workflow '{previous_name}' to '{sanitized_stem}'. {write_result.error_details}"
            )
            return RenameWorkflowResultFailure(result_details=details)

        if moves_file:
            self.engine.workflow_registry.set_file_path(source, new_absolute_path)
            # If the workflow lives outside the workspace, keep it registered at its new path.
            self.engine.workflow_manager.persist_external_workflow_registration(str(new_absolute_path))
        source.metadata = renamed_metadata

        cleanup_warnings = await self._remove_file_renamed_from(
            source,
            previous_name=previous_name,
            previous_file_path=previous_file_path,
            previous_absolute_path=previous_absolute_path,
        )

        return RenameWorkflowResultSuccess(
            new_workflow_name=source.name,
            workflow_id=source.workflow_id,
            result_details=ResultDetails(
                ResultDetail(message=f"Successfully renamed workflow to: {source.name}", level=logging.INFO),
                *cleanup_warnings,
            ),
        )

    def _refuse_closed_rename(self, source: Workflow, *, sanitized_stem: str, new_absolute_path: Path) -> str | None:
        """Return why the closed, saved `source` can't be renamed to `new_absolute_path`, or None if it can."""
        # Rewriting the header would replace what it holds with made-up values.
        if source.metadata_is_placeholder:
            return (
                f"Attempted to rename workflow '{source.name}'. Failed because its file's metadata header "
                "could not be read. Open the workflow and save it to regenerate the header."
            )
        # A library update would put the library's own file back.
        if source.metadata.is_griptape_provided:
            return (
                f"Attempted to rename workflow '{source.name}'. Failed because a library provides it. "
                "Create a workflow from it and rename that one instead."
            )
        if new_absolute_path == source.absolute_path:
            return None
        if new_absolute_path.exists():
            return (
                f"Attempted to rename workflow '{source.name}' to '{sanitized_stem}'. "
                f"Failed because a file already exists at '{new_absolute_path}'."
            )
        try:
            self.engine.workflow_registry.raise_if_path_taken(source, new_absolute_path)
        except WorkflowPathTakenError as err:
            return (
                f"Attempted to rename workflow '{source.name}' to '{sanitized_stem}'. Failed because "
                f"workflow '{err.existing.name}' is already registered at '{new_absolute_path}'."
            )
        return None

    async def _remove_file_renamed_from(
        self,
        source: Workflow,
        *,
        previous_name: str,
        previous_file_path: str,
        previous_absolute_path: Path,
    ) -> list[ResultDetail]:
        """Delete the file `source` was renamed away from, and its config entry. Returns a warning for each step that failed.

        By now the workflow is renamed, so a failure here leaves a stale file or config entry for
        the user to remove, not a failed rename. Nothing is deleted when the rename kept the file
        where it was.
        """
        if previous_absolute_path == source.absolute_path:
            return []
        warnings: list[ResultDetail] = []
        # Remove the config entry an externally imported workflow has.
        if not self.engine.config_manager.delete_user_workflow(previous_file_path):
            message = (
                f"Renamed workflow '{previous_name}' to '{source.name}', but could not remove '{previous_file_path}' "
                "from the workflow files your settings register. Remove it there, or it will be registered again."
            )
            warnings.append(ResultDetail(message=message, level=logging.WARNING))
        delete_result = await self.engine.ahandle_request(
            DeleteFileRequest(
                path=str(previous_absolute_path),
                workspace_only=False,
                deletion_behavior=DeletionBehavior.PREFER_RECYCLE_BIN,
            )
        )
        if isinstance(delete_result, DeleteFileResultFailure):
            message = (
                f"Renamed workflow '{previous_name}' to '{source.name}', but could not delete the original file "
                f"'{previous_absolute_path}': {delete_result.result_details}. Delete it yourself, or it will show up "
                "as a second workflow."
            )
            warnings.append(ResultDetail(message=message, level=logging.WARNING))
        return warnings

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

        * ``OVERRIDE`` — return the caller-supplied ``display_name`` stripped of surrounding
          whitespace. ``_validate_rename_display_name`` guarantees it's non-empty after strip;
          if this invariant is ever violated that's a genuine engine bug, so ``assert`` is
          used to document it rather than a defensive fallback.
        * ``PRESERVE_EXISTING`` — return the source workflow's current ``metadata.name`` (stripped).
          If it is blank, fall through to the requested name — better a sensible file-stem-derived
          label than propagating a corrupt empty display name onto the renamed workflow.
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
