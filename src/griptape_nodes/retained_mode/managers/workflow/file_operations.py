"""Deleting, renaming, and moving workflow files."""

from __future__ import annotations

import logging
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from griptape_nodes.files.path_utils import (
    derive_registry_key,
)
from griptape_nodes.retained_mode.engine import EngineScoped
from griptape_nodes.retained_mode.events.base_events import ResultDetail, ResultDetails
from griptape_nodes.retained_mode.events.os_events import (
    DeleteFileRequest,
    DeleteFileResultFailure,
    DeletionBehavior,
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
from griptape_nodes.retained_mode.request_handlers import handles
from griptape_nodes.utils.string_utils import normalize_display_name

if TYPE_CHECKING:
    from griptape_nodes.node_library.workflow_registry import (
        Workflow,
    )
    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.events.base_events import ResultPayload
    from griptape_nodes.retained_mode.managers.event_manager import EventManager


logger = logging.getLogger("griptape_nodes")


class WorkflowFileOperations(EngineScoped):
    def __init__(self, event_manager: EventManager, *, engine: Engine | None = None) -> None:
        super().__init__(engine)
        event_manager.register_request_handlers(self)

    @handles(DeleteWorkflowRequest)
    async def on_delete_workflows_request(self, request: DeleteWorkflowRequest) -> ResultPayload:
        # If the deleted workflow is the active one, tear down its flows/nodes and
        # pop the context stack BEFORE removing the registry entry, so downstream
        # `DeleteFlowRequest` calls can still push a flow context (they require an
        # active workflow). Non-active deletes (e.g. published-workflow subprocess
        # cleanup) skip this and go straight to the registry/file cleanup.
        context_manager = self.engine.context_manager
        if context_manager.has_current_workflow() and context_manager.get_current_workflow_name() == request.name:
            self.engine.clear_current_workflow_data()
            # clear_current_workflow_data releases what THIS process holds; the worker half must be
            # awaited, so it belongs here in the async handler rather than in that sync method.
            await self.engine.worker_manager.broadcast_local_object_teardown()
        try:
            workflow = self.engine.workflow_registry.delete_workflow_by_name(request.name)
        except Exception as e:
            details = f"Failed to remove workflow from registry with name '{request.name}'. Exception: {e}"
            return DeleteWorkflowResultFailure(result_details=details)
        # Unsaved workflows have no backing file or config entry; dropping the registry
        # entry is the entire operation.
        workflow_file_path = workflow.file_path
        if workflow_file_path is None:
            return DeleteWorkflowResultSuccess(
                result_details=ResultDetails(
                    message=f"Successfully deleted unsaved workflow: {request.name}", level=logging.INFO
                )
            )
        config_manager = self.engine.config_manager
        try:
            config_manager.delete_user_workflow(workflow_file_path)
        except Exception as e:
            details = f"Failed to remove workflow from user config with name '{request.name}'. Exception: {e}"
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
        self.engine.workflow_manager.variable_substitution.drop(request.name)
        return DeleteWorkflowResultSuccess(
            result_details=ResultDetails(message=f"Successfully deleted workflow: {request.name}", level=logging.INFO)
        )

    @handles(RenameWorkflowRequest)
    async def on_rename_workflow_request(self, request: RenameWorkflowRequest) -> ResultPayload:
        # Sanitize to a Python module-friendly name for the file stem (registry key).
        sanitized_stem = normalize_display_name(request.requested_name)
        if not sanitized_stem:
            details = f"Attempted to rename workflow '{request.workflow_name}'. The requested name '{request.requested_name}' produced an empty file name after sanitization."
            return RenameWorkflowResultFailure(result_details=details)

        display_name_error = self._validate_rename_display_name(request)
        if display_name_error is not None:
            return RenameWorkflowResultFailure(result_details=display_name_error)

        # Single source-of-truth lookup for the workflow being renamed. Threaded through the
        # display-name resolver and post-save bookkeeping so we don't re-query the registry
        # three more times (and can't disagree with ourselves mid-handler).
        source = (
            self.engine.workflow_registry.get_workflow_by_name(request.workflow_name)
            if self.engine.workflow_registry.has_workflow_with_name(request.workflow_name)
            else None
        )

        display_name = self._resolve_rename_display_name(request, source=source)

        # Rename keeps the workflow's location (unlike Move). Inherit the source workflow's
        # directory and prepend it to the sanitized stem so the renamed file stays put:
        # a workspace sub-dir ("bar/new_name") or an external absolute path ("/ext/new_name").
        # The combined name is NOT re-run through normalize_display_name, so its "/" survives.
        requested_file_name = sanitized_stem
        if source is not None and source.file_path:
            source_dir = PurePosixPath(source.file_path.replace("\\", "/")).parent
            if str(source_dir) not in ("", "."):
                requested_file_name = f"{source_dir}/{sanitized_stem}"

        save_workflow_request = await self.engine.ahandle_request(
            SaveWorkflowRequest(file_name=requested_file_name, display_name=display_name)
        )

        if not isinstance(save_workflow_request, SaveWorkflowResultSuccess):
            details = f"Attempted to rename workflow '{request.workflow_name}' to '{requested_file_name}'. Failed while attempting to save."
            return RenameWorkflowResultFailure(result_details=details)

        reconcile_error = await self._reconcile_rename_bookkeeping(
            old_workflow_name=request.workflow_name,
            save_result=save_workflow_request,
            source=source,
        )
        if reconcile_error is not None:
            return RenameWorkflowResultFailure(result_details=reconcile_error)

        new_workflow_name = save_workflow_request.workflow_name
        return RenameWorkflowResultSuccess(
            new_workflow_name=new_workflow_name,
            result_details=ResultDetails(
                message=f"Successfully renamed workflow to: {new_workflow_name}", level=logging.INFO
            ),
        )

    @handles(MoveWorkflowRequest)
    def on_move_workflow_request(self, request: MoveWorkflowRequest) -> ResultPayload:  # noqa: C901, PLR0911, PLR0915
        try:
            # Validate source workflow exists
            workflow = self.engine.workflow_registry.get_workflow_by_name(request.workflow_name)
        except KeyError:
            details = f"Failed to move workflow '{request.workflow_name}' because it does not exist."
            return MoveWorkflowResultFailure(result_details=details)

        # Move is a disk-level operation; unsaved workflows have nothing to move.
        if workflow.file_path is None:
            details = (
                f"Cannot move unsaved workflow '{request.workflow_name}' because it has no file on disk. "
                "Save the workflow before moving it."
            )
            return MoveWorkflowResultFailure(result_details=details)
        old_relative_path = workflow.file_path

        config_manager = self.engine.config_manager

        # Get current file path
        current_file_path = self.engine.workflow_registry.get_complete_file_path(old_relative_path)
        if not Path(current_file_path).exists():
            details = (
                f"Failed to move workflow '{request.workflow_name}': File path '{current_file_path}' does not exist."
            )
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
            details = (
                f"Failed to move workflow '{request.workflow_name}': Target file '{new_absolute_path}' already exists."
            )
            return MoveWorkflowResultFailure(result_details=details)

        old_registry_key = derive_registry_key(old_relative_path)
        new_registry_key = derive_registry_key(new_relative_path)

        try:
            # Move the file
            Path(current_file_path).rename(new_absolute_path)

            # Update workflow registry with new file path
            workflow.file_path = new_relative_path

            # Remove old config entry if it existed (e.g. workflow was externally imported)
            config_manager.delete_user_workflow(old_relative_path)

            # Update registry key if directory changed
            if old_registry_key != new_registry_key:
                self.engine.workflow_registry.rekey_workflow(old_registry_key, new_registry_key)
                self.engine.workflow_manager.variable_substitution.rekey(old_registry_key, new_registry_key)
                context_manager = self.engine.context_manager
                if (
                    context_manager.has_current_workflow()
                    and context_manager.get_current_workflow_name() == old_registry_key
                ):
                    context_manager.set_current_workflow_name(new_registry_key)
                    # The context also retains the workflow's path, and that is what
                    # `workflow_dir` answers with. Move is the one operation that changes the
                    # directory, so without this the builtin keeps resolving to the folder the
                    # file just left.
                    context_manager.set_current_workflow_file_path(str(new_absolute_path))

        except OSError as e:
            error_messages = []
            main_error = f"Failed to move workflow file '{current_file_path}' to '{new_absolute_path}': {e!s}"
            error_messages.append(ResultDetail(message=main_error, level=logging.ERROR))

            # Attempt to rollback if file was moved but registry update failed
            if new_absolute_path.exists() and not Path(current_file_path).exists():
                try:
                    new_absolute_path.rename(current_file_path)
                    rollback_message = f"Rolled back file move for workflow '{request.workflow_name}'"
                    error_messages.append(ResultDetail(message=rollback_message, level=logging.INFO))
                except OSError:
                    rollback_failure = f"Failed to rollback file move for workflow '{request.workflow_name}'"
                    error_messages.append(ResultDetail(message=rollback_failure, level=logging.ERROR))

            return MoveWorkflowResultFailure(result_details=ResultDetails(*error_messages))
        except Exception as e:
            details = f"Failed to move workflow '{request.workflow_name}': {e!s}"
            return MoveWorkflowResultFailure(result_details=details)
        else:
            details = f"Successfully moved workflow '{request.workflow_name}' to '{new_relative_path}'"
            return MoveWorkflowResultSuccess(
                moved_file_path=new_relative_path,
                new_workflow_name=new_registry_key,
                result_details=ResultDetails(message=details, level=logging.INFO),
            )

    def _validate_rename_display_name(self, request: RenameWorkflowRequest) -> str | None:
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
                f"Attempted to rename workflow '{request.workflow_name}' with "
                f"display_name_behavior={request.display_name_behavior.value} and display_name="
                f"{request.display_name!r}. Failed because 'display_name' is only consulted when "
                "display_name_behavior=OVERRIDE. Either switch to OVERRIDE, or remove display_name."
            )
        if request.display_name_behavior is not RenameDisplayNameBehavior.OVERRIDE:
            return None
        if request.display_name and request.display_name.strip():
            return None
        return (
            f"Attempted to rename workflow '{request.workflow_name}' with display_name_behavior=OVERRIDE. "
            "Failed because 'display_name' was not provided or was empty. Provide a non-empty display_name, "
            "or use PRESERVE_EXISTING / MATCH_FILE_NAME behavior."
        )

    def _resolve_rename_display_name(self, request: RenameWorkflowRequest, *, source: Workflow | None) -> str:
        """Compute the display name (``metadata.name``) to pass into the follow-up SaveWorkflowRequest.

        ``source`` is the already-resolved registry entry for ``request.workflow_name`` (or ``None``
        when the workflow isn't registered) — passed in so we don't re-query the registry here.

        * ``OVERRIDE`` — return the caller-supplied ``display_name`` stripped of surrounding
          whitespace. ``_validate_rename_display_name`` guarantees it's non-empty after strip;
          if this invariant is ever violated that's a genuine engine bug, so ``assert`` is
          used to document it rather than a defensive fallback.
        * ``PRESERVE_EXISTING`` — return the source workflow's current ``metadata.name`` (stripped).
          If the source isn't registered OR its ``metadata.name`` is blank, fall through to the
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
        if request.display_name_behavior is RenameDisplayNameBehavior.PRESERVE_EXISTING and source is not None:
            preserved = (source.metadata.name or "").strip()
            if preserved:
                return preserved
        return request.requested_name

    async def _reconcile_rename_bookkeeping(
        self,
        *,
        old_workflow_name: str,
        save_result: SaveWorkflowResultSuccess,
        source: Workflow | None,
    ) -> str | None:
        """Post-save bookkeeping for a rename: rekey flags, persist external reg, delete old entry, sync context.

        ``source`` is the pre-resolved registry entry for ``old_workflow_name`` (or ``None`` when
        the workflow wasn't registered). Threaded from the outer handler so we don't re-query.

        Returns a failure message when the delete step fails; otherwise None. Extracted so the outer
        handler doesn't blow past the McCabe branch limit.
        """
        new_workflow_name = save_result.workflow_name

        # Transfer the substitution flag to the new name before the delete call removes the old entry.
        if new_workflow_name != old_workflow_name:
            self.engine.workflow_manager.variable_substitution.rekey(old_workflow_name, new_workflow_name)

        # If the renamed file landed outside the workspace, keep it registered at its new path
        # (the old path's registration is stripped by the delete below).
        self.engine.workflow_manager.persist_external_workflow_registration(str(save_result.file_path))

        # If the original workflow isn't registered, treat this as a Save As and skip deletion.
        # Also skip when the key is unchanged (e.g. renaming to the same on-disk name) so we
        # don't delete the file we just saved.
        if source is not None and new_workflow_name != old_workflow_name:
            delete_workflow_result = await self.engine.ahandle_request(DeleteWorkflowRequest(name=old_workflow_name))
            if isinstance(delete_workflow_result, DeleteWorkflowResultFailure):
                return (
                    f"Attempted to rename workflow '{old_workflow_name}' to '{new_workflow_name}'. "
                    "Failed while attempting to remove the original file name from the registry."
                )

        # If the renamed workflow is the current context, update the context name so the
        # heartbeat and other callers reflect the new registry key immediately. The retained
        # path moves with it: rename keeps the directory, so `workflow_dir` is unaffected, but
        # the path itself would otherwise name a file that no longer exists.
        context_manager = self.engine.context_manager
        if context_manager.has_current_workflow() and context_manager.get_current_workflow_name() == old_workflow_name:
            context_manager.set_current_workflow_name(new_workflow_name)
            context_manager.set_current_workflow_file_path(str(save_result.file_path))

        return None
