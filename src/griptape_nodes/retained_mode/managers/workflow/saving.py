from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from griptape_nodes.common.macro_parser import MacroSyntaxError, MacroVariables, ParsedMacro
from griptape_nodes.common.project_templates.situation import BuiltInSituation, SituationFilePolicy
from griptape_nodes.files.file import File, FileLoadError, FileWriteError
from griptape_nodes.files.path_utils import (
    FilenameParts,
    canonicalize_for_identity,
    derive_registry_key,
)
from griptape_nodes.files.project_file import ProjectFileDestination
from griptape_nodes.files.situation_resolver import SITUATION_TO_FILE_POLICY
from griptape_nodes.node_library.workflow_registry import (
    Workflow,
    WorkflowShape,
)
from griptape_nodes.retained_mode.engine import EngineScoped
from griptape_nodes.retained_mode.events.flow_events import (
    CreateFlowRequest,
    GetTopLevelFlowRequest,
    GetTopLevelFlowResultSuccess,
    ImportWorkflowAsReferencedSubFlowRequest,
    SerializedFlowCommands,
    SerializeFlowToCommandsRequest,
    SerializeFlowToCommandsResultSuccess,
)
from griptape_nodes.retained_mode.events.os_events import (
    ExistingFilePolicy,
    FileIOFailureReason,
)
from griptape_nodes.retained_mode.events.project_events import (
    AttemptMatchPathAgainstMacroRequest,
    AttemptMatchPathAgainstMacroResultSuccess,
    GetSituationRequest,
    GetSituationResultSuccess,
    MacroPath,
)
from griptape_nodes.retained_mode.events.workflow_events import (
    SaveSubflowToWorkflowRequest,
    SaveSubflowToWorkflowResultFailure,
    SaveSubflowToWorkflowResultSuccess,
    SaveWorkflowFileFromSerializedFlowRequest,
    SaveWorkflowFileFromSerializedFlowResultFailure,
    SaveWorkflowFileFromSerializedFlowResultSuccess,
    SaveWorkflowRequest,
    SaveWorkflowResultFailure,
    SaveWorkflowResultSuccess,
)
from griptape_nodes.retained_mode.managers.os_manager import OSManager
from griptape_nodes.retained_mode.managers.project_manager import BUILTIN_VARIABLES
from griptape_nodes.retained_mode.managers.workflow.loading import EPOCH_START
from griptape_nodes.retained_mode.managers.workflow.shape import WorkflowShapeType
from griptape_nodes.retained_mode.request_handlers import handles

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.events.base_events import ResultPayload
    from griptape_nodes.retained_mode.managers.event_manager import EventManager


logger = logging.getLogger("griptape_nodes")


class SaveWorkflowScenario(StrEnum):
    """Scenarios for saving workflows."""

    FIRST_SAVE = "first_save"  # First save of new workflow
    OVERWRITE_EXISTING = "overwrite_existing"  # Save existing workflow to same name
    SAVE_AS = "save_as"  # Save existing workflow with new name
    SAVE_FROM_TEMPLATE = "save_from_template"  # Save from a template
    CREATE_VERSIONED = "create_versioned"  # Save a new version via create_versioned_workflow situation


@dataclass
class SaveWorkflowTargetInfo:
    """Target information for saving a workflow.

    Exactly one of ``destination`` or ``file_path`` is populated:

    - ``destination`` is set for FIRST_SAVE, SAVE_AS, and SAVE_FROM_TEMPLATE
      scenarios. It carries the unresolved ``ProjectFileDestination`` from
      the ``save_workflow`` situation so the macro resolves inside
      ``OSManager.on_write_file_request`` (seed-and-retry for unresolved
      required ``{x:NN}`` slots; situation policy honored).
    - ``file_path`` is set for OVERWRITE_EXISTING. The registry already
      knows the workflow's on-disk location, so an in-place overwrite is
      the correct behavior — the situation macro does NOT re-resolve when
      updating an existing file.
    """

    scenario: SaveWorkflowScenario  # Which save scenario we're in
    file_name: str  # Final resolved name to use
    destination: ProjectFileDestination | None  # Unresolved destination for new saves
    file_path: Path | None  # Absolute path for in-place overwrite (OVERWRITE_EXISTING)
    relative_file_path: str  # Relative path for registry
    creation_date: datetime  # When workflow was originally created
    branched_from: str | None  # Workflow this was branched from (if any)


class WriteWorkflowFileResult(NamedTuple):
    """Result of writing a workflow file.

    ``written_file`` is populated on success and carries the post-write
    location (which may differ from the requested path when CREATE_NEW
    seeded an index slot or walked past a collision).
    """

    success: bool
    error_details: str
    written_file: File | None = None
    failure_reason: FileIOFailureReason | None = None


class WorkflowSavePath(NamedTuple):
    """Unresolved workflow save destination plus its registry-relative form.

    ``destination`` carries the unresolved ``MacroPath`` so it can be passed
    through to the OSManager write handler intact. The handler seeds and
    walks an unresolved required ``{x:NN}`` slot on CREATE_NEW writes —
    pre-resolving here would strip that context.
    """

    destination: ProjectFileDestination
    relative_file_path: str


class NamedSavePath(NamedTuple):
    """Save destination for a user-supplied name, plus the bare file stem."""

    file_name: str
    destination: ProjectFileDestination
    relative_file_path: str


class CreatedWorkflowFile(NamedTuple):
    """Where a newly created workflow file landed, or why it could not be written.

    ``relative_file_path`` is the registry's form of ``absolute_path`` (workspace-relative
    while the situation keeps the file inside the workspace, absolute otherwise), so the
    registry key always names the file that is actually on disk.
    """

    success: bool
    error_details: str
    absolute_path: str = ""
    relative_file_path: str = ""


class _ExistingMetadata(NamedTuple):
    display_name: str | None
    description: str | None
    image: str | None
    is_template: bool | None


class WorkflowSaver(EngineScoped):
    def __init__(self, event_manager: EventManager, *, engine: Engine | None = None) -> None:
        super().__init__(engine)
        event_manager.register_request_handlers(self)

    def create_workflow_file(self, file_name: str, content: str) -> CreatedWorkflowFile:
        """Write ready-made content as a NEW workflow file, wherever ``save_workflow`` says.

        Creating a workflow is a workflow save like any other, so it resolves through the same
        situation: a project that redirects ``save_workflow`` would otherwise be honored when
        the user saves and ignored when the engine creates the file on their behalf (branching,
        or copying a template), leaving those workflows stranded in the workspace root with no
        way to migrate -- every later save overwrites them in place.

        Callers hold content they generated themselves (a rewritten metadata header over a
        source file's body), which is why this writes verbatim rather than going through
        ``_save_workflow_file_inline``'s serialize-and-generate path.
        """
        destination, _relative = self._build_workflow_save_path(f"{file_name}.py")
        write_result = self.write_workflow_file(destination, content, file_name)
        if not write_result.success:
            return CreatedWorkflowFile(success=False, error_details=write_result.error_details)

        # The written location, not the requested one: the situation's macro decides the
        # directory, and a CREATE_NEW policy may have walked the filename past a collision.
        written_file = write_result.written_file
        if written_file is None:
            return CreatedWorkflowFile(
                success=False,
                error_details=f"Attempted to create workflow file '{file_name}'. Failed because the write reported no location.",
            )
        try:
            absolute_path = written_file.resolve()
        except FileLoadError as err:
            return CreatedWorkflowFile(
                success=False,
                error_details=f"Attempted to create workflow file '{file_name}'. Failed resolving the written location: {err}",
            )

        return CreatedWorkflowFile(
            success=True,
            error_details="",
            absolute_path=str(absolute_path),
            relative_file_path=self._workspace_relative_path(str(absolute_path), self.engine),
        )

    def write_workflow_file(
        self, destination: ProjectFileDestination, content: str, file_name: str
    ) -> WriteWorkflowFileResult:
        """Write workflow content via a ``ProjectFileDestination``.

        The unresolved macro (when the destination carries one) is threaded
        through to the OSManager write handler so the seed-and-retry contract
        for unresolved required ``{x:NN}`` slots applies (#4941) and the
        situation's collision policy is honored. Callers with a plain on-disk
        path wrap it as ``ProjectFileDestination(str(path), ...)`` — the
        ``File`` constructor stores literal paths verbatim, so the write
        behaves as an in-place overwrite (used by header-only metadata
        updates).
        """
        # Best-effort disk-space probe. When the destination's macro can't yet
        # resolve (e.g. an unresolved required `{_index:03}` slot is waiting to
        # be seeded inside OSManager), skip the proactive check and let any
        # actual disk-full surface as IO_ERROR from the write.
        check_dir = self._probe_parent_for_disk_check(destination)
        if check_dir is not None:
            config_manager = self.engine.config_manager
            min_space_gb = config_manager.get_config_value("minimum_disk_space_gb_workflows")
            if not OSManager.check_available_disk_space(check_dir, min_space_gb):
                error_msg = OSManager.format_disk_space_error(check_dir)
                details = f"Attempted to save workflow '{file_name}' (requires {min_space_gb:.1f} GB). Failed due to insufficient disk space: {error_msg}"
                return WriteWorkflowFileResult(success=False, error_details=details)

        try:
            written_file = destination.write_text(content, encoding="utf-8")
        except FileWriteError as err:
            details = self._format_workflow_write_error(file_name, err.failure_reason, err.result_details)
            return WriteWorkflowFileResult(
                success=False,
                error_details=details,
                failure_reason=err.failure_reason,
            )
        return WriteWorkflowFileResult(success=True, error_details="", written_file=written_file)

    @handles(SaveWorkflowRequest)
    async def on_save_workflow_request(self, request: SaveWorkflowRequest) -> ResultPayload:  # noqa: C901, PLR0912, PLR0915
        # Determine save target (file path, name, metadata)
        context_manager = self.engine.context_manager
        current_workflow_name = (
            context_manager.get_current_workflow_name() if context_manager.has_current_workflow() else None
        )

        try:
            save_target = self._determine_save_target(
                requested_file_name=request.file_name,
                current_workflow_name=current_workflow_name,
                create_versioned=request.create_versioned,
            )
        except ValueError as e:
            details = f"Attempted to save workflow. Failed when determining save target: {e}"
            return SaveWorkflowResultFailure(result_details=details)

        file_name = save_target.file_name
        relative_file_path = save_target.relative_file_path
        creation_date = save_target.creation_date
        branched_from = save_target.branched_from
        registry_key = derive_registry_key(relative_file_path)

        # Re-saving the workflow that's currently open is always allowed; writing over a
        # *different* workflow's file needs the caller's explicit say-so. Both sides are
        # already registry keys (extension stripped, separators normalized), so compare
        # them directly — re-deriving would truncate at the first dot in names like
        # "03.07_18.30".
        is_self_save = current_workflow_name == registry_key
        if request.overwrite_existing or is_self_save:
            policy = ExistingFilePolicy.OVERWRITE
        else:
            policy = ExistingFilePolicy.FAIL

        # OVERWRITE_EXISTING uses the registry's recorded file_path verbatim
        # (in-place overwrite) wrapped in a ProjectFileDestination, so it is the
        # scenario the `policy` above governs. All other scenarios carry an
        # unresolved destination so the save_workflow situation macro resolves at
        # write time, preserving the seed-and-retry contract for unresolved
        # required `{x:NN}` slots — those keep the situation's own policy and
        # intentionally ignore `policy`. That's the documented boundary: this flag
        # guards against replacing another *registered* workflow, not against an
        # unregistered stray .py file sitting at a freshly-computed Save-As path.
        if save_target.destination is not None:
            destination = save_target.destination
        elif save_target.file_path is not None:
            destination = ProjectFileDestination(
                str(save_target.file_path),
                existing_file_policy=policy,
            )
        else:
            msg = (
                f"Save target for '{relative_file_path}' has neither a destination nor a file_path; "
                "this is a programming error in _determine_save_target."
            )
            return SaveWorkflowResultFailure(result_details=msg)

        logger.debug(
            "Save workflow: scenario=%s, file_name=%s, registry_key=%s, destination=%s, "
            "self_save=%s, overwrite_existing=%s, branched_from=%s",
            save_target.scenario.value,
            file_name,
            registry_key,
            destination.location,
            is_self_save,
            request.overwrite_existing,
            branched_from or "None",
        )

        # Serialize current flow and get shape
        top_level_flow_result = await self.engine.ahandle_request(GetTopLevelFlowRequest())
        if not isinstance(top_level_flow_result, GetTopLevelFlowResultSuccess):
            details = f"Attempted to save workflow '{relative_file_path}'. Failed when requesting top level flow."
            return SaveWorkflowResultFailure(result_details=details)
        top_level_flow_name = top_level_flow_result.flow_name

        serialized_flow_result = await self.engine.ahandle_request(
            SerializeFlowToCommandsRequest(flow_name=top_level_flow_name, include_create_flow_command=True)
        )
        if not isinstance(serialized_flow_result, SerializeFlowToCommandsResultSuccess):
            details = f"Attempted to save workflow '{relative_file_path}'. Failed when serializing flow."
            return SaveWorkflowResultFailure(result_details=details)
        commands = serialized_flow_result.serialized_flow_commands

        # Extract workflow shape if available; ignore failures
        try:
            workflow_shape_dict = self.engine.workflow_manager.extract_workflow_shape(workflow_name=registry_key)
            workflow_shape = WorkflowShape(
                inputs=workflow_shape_dict[WorkflowShapeType.INPUT],
                outputs=workflow_shape_dict[WorkflowShapeType.OUTPUT],
            )
        except ValueError:
            workflow_shape = None

        # Build save request inline (preserve existing display_name/description/image/is_template if present)
        existing = self._get_existing_metadata(registry_key)
        # Display name precedence (high to low):
        # 1. Caller-supplied request.display_name — explicit intent always wins.
        # 2. existing.display_name from the registry — preserves the human-readable label
        #    across re-saves and version bumps (one workflow named "a" with v001, v002, ...).
        # 3. The resolved local file_name from _determine_save_target — this is either the
        #    user's typed Save-As stem (so "a" stays "a" and doesn't become "a_v001"), or
        #    the sanitized display-name-derived stem for a first-save-of-unsaved-workflow
        #    (so the synthetic "unsaved:<uuid>" key never leaks to metadata.name). Read the
        #    local `file_name`, NOT `request.file_name` — the latter is un-normalized and
        #    can still be "unsaved:<uuid>" on the wire for the fresh-save case.
        # 4. Resolved file_name fallback inside _generate_workflow_metadata_from_commands
        #    (last-resort safety net for code paths that supply nothing).
        if request.display_name is not None:
            resolved_display_name = request.display_name
        elif existing.display_name is not None:
            resolved_display_name = existing.display_name
        elif file_name:
            resolved_display_name = file_name
        else:
            resolved_display_name = None

        save_file_result = self._save_workflow_file_inline(
            destination=destination,
            serialized_flow_commands=commands,
            file_name=file_name,
            creation_date=creation_date,
            display_name=resolved_display_name,
            image_path=request.image_path if request.image_path is not None else existing.image,
            description=existing.description,
            is_template=existing.is_template,
            branched_from=branched_from,
            workflow_shape=workflow_shape,
        )
        # _save_workflow_file_inline returns a SaveWorkflowFileFromSerializedFlowResult*
        # (its native result family). on_save_workflow_request's public contract
        # returns SaveWorkflowResult*. The check here translates between the two
        # failure types — it stays outside the helper because the helper is called
        # from two handlers with different outer result families.
        if not isinstance(save_file_result, SaveWorkflowFileFromSerializedFlowResultSuccess):
            details = (
                f"Attempted to save workflow '{relative_file_path}'. "
                f"Failed during file generation: {save_file_result.result_details}"
            )
            # Carry the typed reason across the family boundary so callers can tell a
            # refused overwrite (POLICY_NO_OVERWRITE) apart from a genuine I/O failure
            # and offer to retry with overwrite_existing=True.
            failure_reason = None
            if isinstance(save_file_result, SaveWorkflowFileFromSerializedFlowResultFailure):
                failure_reason = save_file_result.failure_reason
            return SaveWorkflowResultFailure(result_details=details, failure_reason=failure_reason)

        workflow_metadata = save_file_result.workflow_metadata

        # Reconcile registry key / relative_file_path with the actual written path
        # for macro-driven saves. CREATE_NEW + `{_index:03}` may produce
        # `foo_v001.py` from a `foo.py` request; the registry must key by what
        # ended up on disk, not what was asked for.
        if save_target.destination is not None:
            written_relative = self._workspace_relative_path(save_file_result.file_path, self.engine)
            if written_relative != relative_file_path:
                relative_file_path = written_relative
                registry_key = derive_registry_key(relative_file_path)

        # Handle the unsaved -> saved transition: if the current-context workflow is an
        # unsaved entry, swap its registry key to the path-derived key and update its
        # file_path. This preserves the workflow instance (so any external references
        # remain valid) while transitioning it to the "saved" state. Also walks the
        # ContextManager's workflow stack in-place so any active context referencing
        # the old unsaved key is updated to the new registry key.
        unsaved_source_key: str | None = None
        if (
            current_workflow_name is not None
            and current_workflow_name.startswith(self.engine.workflow_registry.UNSAVED_KEY_PREFIX)
            and self.engine.workflow_registry.has_workflow_with_name(current_workflow_name)
        ):
            unsaved_source_key = current_workflow_name

        registered_workflows = self.engine.workflow_registry.list_workflows()
        if unsaved_source_key is not None and unsaved_source_key != registry_key:
            # Rekey the unsaved entry to the path-derived key if the new key is not already
            # occupied by a separate entry. If the new key already exists (e.g. a saved
            # workflow with the same target path is already registered), fall back to
            # dropping the unsaved entry and updating the existing saved entry below.
            if registry_key in registered_workflows:
                self.engine.workflow_registry.delete_workflow_by_name(unsaved_source_key)
                self.engine.workflow_manager.variable_substitution.drop(unsaved_source_key)
            else:
                self.engine.workflow_registry.rekey_workflow(old_key=unsaved_source_key, new_key=registry_key)
                self.engine.workflow_manager.variable_substitution.rekey(unsaved_source_key, registry_key)
                rekeyed_workflow = self.engine.workflow_registry.get_workflow_by_name(registry_key)
                rekeyed_workflow.file_path = relative_file_path
            for workflow_context_state in self.engine.context_manager._workflow_stack:
                if workflow_context_state._name == unsaved_source_key:
                    workflow_context_state._name = registry_key
                    # The context also retains the workflow's path, and `workflow_dir` prefers
                    # it over a registry lookup. An unsaved context has no path; this save is
                    # where it gets one, so record it here or the builtin keeps falling back to
                    # the registry key -- the thing that goes stale on the next project switch.
                    workflow_context_state._file_path = str(save_file_result.file_path)
            registered_workflows = self.engine.workflow_registry.list_workflows()

        if registry_key not in registered_workflows:
            self.engine.workflow_registry.generate_new_workflow(
                registry_key=registry_key, metadata=workflow_metadata, file_path=relative_file_path
            )

        existing_workflow = self.engine.workflow_registry.get_workflow_by_name(registry_key)
        existing_workflow.metadata = workflow_metadata
        # Ensure file_path is populated even for pre-existing entries (defensive).
        if existing_workflow.file_path is None:
            existing_workflow.file_path = relative_file_path
        details = f"Successfully saved workflow to: {save_file_result.file_path}"
        return SaveWorkflowResultSuccess(
            file_path=save_file_result.file_path,
            workflow_name=registry_key,
            result_details=details,
        )

    def generate_unique_filename(self, base_name: str) -> str:
        """Generate a unique filename for a workflow, avoiding collisions.

        Uses the same logic as object_manager:
        1. If base name has no collision, use it as-is
        2. If collision exists and name ends in a number, find first free prefix + integer
        3. If collision exists and name doesn't end in a number, append _1, _2, etc.

        Candidates are probed where the ``save_workflow`` situation actually puts them rather
        than at ``<workspace>/<name>.py``: a project that redirects workflow saves would
        otherwise judge uniqueness against a directory it never writes to, and a creation
        carrying the situation's overwrite policy would then clobber whatever already sits at
        the real destination.

        Args:
            base_name: The desired base name for the workflow

        Returns:
            A unique filename whose save destination is free
        """
        if not self.workflow_destination_exists(base_name):
            return base_name

        pattern_match = re.search(r"\d+$", base_name)
        if pattern_match is not None:
            # Name ends in a number - strip it and find first free integer
            incremental_prefix = base_name[: pattern_match.start()]
        else:
            # Name doesn't end in a number - append underscore prefix
            incremental_prefix = f"{base_name}_"

        curr_idx = 1
        while True:
            candidate_name = f"{incremental_prefix}{curr_idx}"
            if not self.workflow_destination_exists(candidate_name):
                return candidate_name
            curr_idx += 1

    def workflow_destination_exists(self, file_name: str) -> bool:
        """Whether the ``save_workflow`` destination for ``file_name`` is already occupied.

        A macro that cannot resolve yet answers False: the only reason it can't is an
        unresolved required ``{x:NN}`` slot, which OSManager seeds to a free value during the
        write, so there is no single path to probe.
        """
        destination, _relative = self._build_workflow_save_path(f"{file_name}.py")
        try:
            resolved = destination.resolve()
        except FileLoadError:
            return False
        return Path(resolved).exists()

    @handles(SaveWorkflowFileFromSerializedFlowRequest)
    async def on_save_workflow_file_from_serialized_flow_request(
        self, request: SaveWorkflowFileFromSerializedFlowRequest
    ) -> ResultPayload:
        """Save a workflow file from serialized flow commands without registry overhead."""
        # Determine write destination
        if request.file_path:
            # Callers that pre-resolved a file path (rename, failed-workflow saver,
            # node-executor publishers) save exactly there via in-place overwrite.
            # File treats literal absolute paths as non-macros, so the write goes
            # straight through OSManager's sanitize-and-write branch.
            destination = ProjectFileDestination(
                request.file_path,
                existing_file_policy=ExistingFilePolicy.OVERWRITE,
            )
        else:
            # Resolve via the save_workflow situation (workspace-relative by default).
            destination = self._build_workflow_save_path(f"{request.file_name}.py").destination

        return self._save_workflow_file_inline(
            destination=destination,
            serialized_flow_commands=request.serialized_flow_commands,
            file_name=request.file_name,
            creation_date=request.creation_date,
            display_name=request.display_name,
            image_path=request.image_path,
            description=request.description,
            is_template=request.is_template,
            branched_from=request.branched_from,
            workflow_shape=request.workflow_shape,
        )

    @handles(SaveSubflowToWorkflowRequest)
    async def on_save_subflow_to_workflow(self, request: SaveSubflowToWorkflowRequest) -> ResultPayload:
        """Save a subflow back to its original workflow file."""
        registry_key = request.workflow_name

        if not self.engine.workflow_registry.has_workflow_with_name(registry_key):
            details = (
                f"Attempted to save subflow '{request.flow_name}'. Workflow '{registry_key}' not found in registry."
            )
            return SaveSubflowToWorkflowResultFailure(result_details=details)

        workflow = self.engine.workflow_registry.get_workflow_by_name(registry_key)
        if workflow.file_path is None:
            # Saving a subflow back into its parent requires that parent to have a file.
            # Unsaved workflows have no destination to write into.
            details = (
                f"Attempted to save subflow '{request.flow_name}' into workflow '{registry_key}'. "
                "Failed because the parent workflow is unsaved (no file on disk). "
                "Save the parent workflow before saving a subflow into it."
            )
            return SaveSubflowToWorkflowResultFailure(result_details=details)
        file_path = self.engine.workflow_registry.get_complete_file_path(workflow.file_path)
        file_name = Path(file_path).stem

        # Serialize the subflow.
        serialized_flow_result = await self.engine.ahandle_request(
            SerializeFlowToCommandsRequest(flow_name=request.flow_name, include_create_flow_command=True)
        )
        if not isinstance(serialized_flow_result, SerializeFlowToCommandsResultSuccess):
            details = f"Attempted to save subflow '{request.flow_name}' to '{file_path}'. Failed when serializing flow."
            return SaveSubflowToWorkflowResultFailure(result_details=details)
        commands = serialized_flow_result.serialized_flow_commands

        # Strip parent_flow_name so the saved file stands alone as a top-level workflow.
        # If the subflow is tracked as a referenced workflow, replace the self-referential
        # import command with a plain CreateFlowRequest for the standalone save.
        if isinstance(commands.flow_initialization_command, ImportWorkflowAsReferencedSubFlowRequest):
            commands.flow_initialization_command = CreateFlowRequest(
                flow_name=request.flow_name,
                parent_flow_name=None,
                set_as_new_context=False,
                metadata=commands.flow_initialization_command.imported_flow_metadata,
            )
        elif isinstance(commands.flow_initialization_command, CreateFlowRequest):
            commands.flow_initialization_command.parent_flow_name = None

        # Extract workflow shape from the specific subflow (not the top-level flow).
        try:
            workflow_shape_dict = self.engine.workflow_manager.extract_workflow_shape(
                workflow_name=registry_key, flow_name=request.flow_name
            )
            workflow_shape = WorkflowShape(
                inputs=workflow_shape_dict[WorkflowShapeType.INPUT],
                outputs=workflow_shape_dict[WorkflowShapeType.OUTPUT],
            )
        except ValueError:
            workflow_shape = None
            msg = f"The workflow {registry_key} is being saved without Start and End Flow parameters. It will no longer be a callable workflow."
            logger.warning(msg)

        # Preserve existing metadata from the registry.
        existing = self._get_existing_metadata(registry_key)
        resolved_display_name = existing.display_name

        # Delegate file generation and writing to the existing lower-level handler.
        save_file_request = SaveWorkflowFileFromSerializedFlowRequest(
            serialized_flow_commands=commands,
            file_name=file_name,
            file_path=file_path,
            display_name=resolved_display_name,
            description=existing.description,
            image_path=existing.image,
            is_template=existing.is_template,
            workflow_shape=workflow_shape,
        )
        save_file_result = await self.on_save_workflow_file_from_serialized_flow_request(save_file_request)
        if not isinstance(save_file_result, SaveWorkflowFileFromSerializedFlowResultSuccess):
            details = (
                f"Attempted to save subflow '{request.flow_name}' to '{file_path}'. "
                f"Failed during file generation: {save_file_result.result_details}"
            )
            return SaveSubflowToWorkflowResultFailure(result_details=details)

        workflow_metadata = save_file_result.workflow_metadata

        # Update the registry entry with the new metadata.
        workflow.metadata = workflow_metadata

        details = f"Successfully saved subflow '{request.flow_name}' to: {save_file_result.file_path}"
        return SaveSubflowToWorkflowResultSuccess(
            file_path=save_file_result.file_path,
            workflow_metadata=workflow_metadata,
            result_details=details,
        )

    def _build_workflow_save_path(
        self,
        file_name: str,
        sub_dirs: str | None = None,
        situation_name: str = BuiltInSituation.SAVE_WORKFLOW,
    ) -> WorkflowSavePath:
        """Build a workflow save destination via a named situation.

        Returns an unresolved ``ProjectFileDestination`` plus a registry-relative
        display string. The destination's macro is resolved inside
        ``OSManager.on_write_file_request`` so the seed-and-retry contract for
        unresolved required ``{x:NN}`` slots applies (see issue #4941).

        ``relative_file_path`` is computed from the user-supplied name and
        sub-directory directly; out-of-workspace handling and macro-form
        portability happen post-write via ``ProjectFileDestination._map_to_macro_file``.

        ``situation_name`` defaults to ``save_workflow`` (overwrite-in-place
        semantics). Pass ``create_versioned_workflow`` for the versioned-save
        flow that bumps the padded index every save (issue #4945).
        """
        extra_vars: dict[str, str | int] = {}
        if sub_dirs:
            extra_vars["sub_dirs"] = sub_dirs

        destination = ProjectFileDestination.from_situation(file_name, situation_name, **extra_vars)
        relative_file_path = str(Path(sub_dirs) / file_name) if sub_dirs else file_name
        return WorkflowSavePath(
            destination=destination,
            relative_file_path=relative_file_path,
        )

    def _resolve_named_save_path(
        self,
        requested_file_name: str,
        situation_name: str = BuiltInSituation.SAVE_WORKFLOW,
    ) -> NamedSavePath:
        """Resolve a user-supplied save name (possibly carrying a directory) to a save destination.

        A relative name like "episode/my_wf" splits into sub-directory + stem and routes
        through the workspace save situation. An absolute name like "/ext/my_wf" (produced
        when renaming an externally-registered workflow) is honored verbatim:
        ProjectFileDestination.from_situation bypasses the workspace macro for absolute
        filenames.

        ``situation_name`` selects which situation drives the save (default
        ``save_workflow``; pass ``create_versioned_workflow`` for versioned saves).
        """
        parts = FilenameParts.from_filename(f"{requested_file_name}.py")
        if parts.directory.is_absolute():
            destination, relative_file_path = self._build_workflow_save_path(
                f"{requested_file_name}.py", situation_name=situation_name
            )
        else:
            sub_dirs = str(parts.directory) if str(parts.directory) != "." else None
            destination, relative_file_path = self._build_workflow_save_path(
                f"{parts.stem}.py", sub_dirs=sub_dirs, situation_name=situation_name
            )
        return NamedSavePath(file_name=parts.stem, destination=destination, relative_file_path=relative_file_path)

    def _get_existing_metadata(self, file_name: str) -> _ExistingMetadata:
        """Return metadata for an existing workflow, or all-None if not present."""
        if not self.engine.workflow_registry.has_workflow_with_name(file_name):
            return _ExistingMetadata(None, None, None, None)
        try:
            existing = self.engine.workflow_registry.get_workflow_by_name(file_name)
        except Exception as err:
            logger.debug("Preserving existing metadata failed for workflow '%s': %s", file_name, err)
            return _ExistingMetadata(None, None, None, None)
        else:
            return _ExistingMetadata(
                display_name=existing.metadata.name,
                description=existing.metadata.description,
                image=existing.metadata.image,
                is_template=existing.metadata.is_template,
            )

    def _determine_save_target(  # noqa: C901, PLR0912, PLR0915
        self,
        requested_file_name: str | None,
        current_workflow_name: str | None,
        *,
        create_versioned: bool = False,
    ) -> SaveWorkflowTargetInfo:
        """Determine the target file path, name, and metadata for saving a workflow.

        Args:
            requested_file_name: The name the user wants to save as (can be None)
            current_workflow_name: The workflow currently loaded in context (can be None)
            create_versioned: When True, route every save through the
                ``create_versioned_workflow`` situation so each save produces a
                new versioned file (e.g. ``foo_v001.py``, ``foo_v002.py``, ...).
                When False (default), the standard scenarios apply
                (FIRST_SAVE / OVERWRITE_EXISTING / SAVE_AS / SAVE_FROM_TEMPLATE)
                via the ``save_workflow`` situation.

        Returns:
            SaveWorkflowTargetInfo with all information needed to save the workflow

        Raises:
            ValueError: If workflow registry lookups fail or produce inconsistent state
        """
        # An unsaved synthetic key ("unsaved:<uuid>") is a registry lookup key, not a
        # usable filename stem. Treat it as "no requested name" so the FIRST_SAVE path
        # derives the filename from the workflow's display-name metadata below.
        if requested_file_name and requested_file_name.startswith(self.engine.workflow_registry.UNSAVED_KEY_PREFIX):
            requested_file_name = None

        # Look up workflows in registry
        target_workflow = None
        if requested_file_name and self.engine.workflow_registry.has_workflow_with_name(requested_file_name):
            target_workflow = self.engine.workflow_registry.get_workflow_by_name(requested_file_name)

        current_workflow = None
        if current_workflow_name and self.engine.workflow_registry.has_workflow_with_name(current_workflow_name):
            current_workflow = self.engine.workflow_registry.get_workflow_by_name(current_workflow_name)

        # Pick the situation up-front: create_versioned diverts EVERY save through
        # create_versioned_workflow (with CREATE_NEW + a padded slot) so each save
        # bumps the version. Without it, the standard save_workflow situation applies.
        situation_name = (
            BuiltInSituation.CREATE_VERSIONED_WORKFLOW if create_versioned else BuiltInSituation.SAVE_WORKFLOW
        )
        self._warn_if_situation_policy_mismatches_intent(situation_name, create_versioned=create_versioned)

        # CREATE_VERSIONED short-circuits the OVERWRITE_EXISTING branch. Even when
        # the workflow is already in the registry with a saved file_path, a versioned
        # save re-resolves the macro so OSManager walks past existing versions and
        # produces the next one. The helper handles all three sub-cases (match,
        # no-match, unsaved) and returns the destination + display strings; the
        # macro layer is the single source of truth for "where does it go?".
        if create_versioned:
            file_name, destination, relative_file_path = self._resolve_versioned_save_target(
                situation_name=situation_name,
                requested_file_name=requested_file_name,
                current_workflow=current_workflow,
                target_workflow=target_workflow,
            )
            creation_date = (
                current_workflow.metadata.creation_date if current_workflow is not None else datetime.now(tz=UTC)
            )
            branched_from = current_workflow.metadata.branched_from if current_workflow is not None else None
            if (creation_date is None) or (creation_date == EPOCH_START):
                creation_date = datetime.now(tz=UTC)
            return SaveWorkflowTargetInfo(
                scenario=SaveWorkflowScenario.CREATE_VERSIONED,
                file_name=file_name,
                destination=destination,
                file_path=None,
                relative_file_path=relative_file_path,
                creation_date=creation_date,
                branched_from=branched_from,
            )

        # Determine scenario and build target info
        # Only treat as SAVE_FROM_TEMPLATE if this is a Griptape-provided template.
        # User-marked templates (is_template=True but is_griptape_provided=False) should be saved normally.
        target_is_griptape_template = (
            target_workflow and target_workflow.metadata.is_template and target_workflow.metadata.is_griptape_provided
        )
        current_is_griptape_template = (
            current_workflow
            and current_workflow.metadata.is_template
            and current_workflow.metadata.is_griptape_provided
        )
        destination: ProjectFileDestination | None = None
        file_path: Path | None = None
        if target_is_griptape_template or current_is_griptape_template:
            # Griptape-provided template workflows always create new copies with unique names.
            # Griptape-provided templates are always disk-backed, so file_path is guaranteed.
            scenario = SaveWorkflowScenario.SAVE_FROM_TEMPLATE
            template_workflow = target_workflow or current_workflow
            if template_workflow is None or template_workflow.file_path is None:
                msg = "Save From Template scenario requires a disk-backed template workflow."
                raise ValueError(msg)
            # Use the registry key as base name, independent of the display name in metadata.
            base_name = requested_file_name or derive_registry_key(template_workflow.file_path)
            file_name = self.generate_unique_filename(base_name)
            creation_date = datetime.now(tz=UTC)
            branched_from = None
            destination, relative_file_path = self._build_workflow_save_path(f"{file_name}.py")

        elif target_workflow and target_workflow.file_path is not None:
            # Requested name exists in registry as a saved workflow → overwrite it.
            # (If it were unsaved, we would instead treat this as first-save of the current
            # workflow; handled by the `elif requested_file_name and current_workflow` branch.)
            scenario = SaveWorkflowScenario.OVERWRITE_EXISTING
            # Use the registry key as the file name, independent of the display name in metadata.
            file_name = derive_registry_key(target_workflow.file_path)
            creation_date = target_workflow.metadata.creation_date
            branched_from = target_workflow.metadata.branched_from
            relative_file_path = target_workflow.file_path
            file_path = Path(self.engine.workflow_registry.get_complete_file_path(relative_file_path))

        elif requested_file_name and current_workflow:
            # Requested name doesn't exist but we have a current workflow → Save As.
            # A user-typed name like "episode/my_wf" splits into sub-directory + stem
            # and is authoritative: the requested name fully determines the save path.
            scenario = SaveWorkflowScenario.SAVE_AS
            creation_date = current_workflow.metadata.creation_date
            branched_from = current_workflow.metadata.branched_from
            file_name, destination, relative_file_path = self._resolve_named_save_path(requested_file_name)

        else:
            # No requested name or no current workflow → first save.
            # A user-typed name like "episode/my_wf" splits into sub-directory + stem;
            # auto-generated timestamp names have no directory component. When the caller
            # has no name in mind, prefer the current workflow's display-name metadata
            # (e.g. the auto-generated "workflow_25" for a freshly-created unsaved flow)
            # over a timestamp so the on-disk filename matches what the user sees.
            scenario = SaveWorkflowScenario.FIRST_SAVE
            if not requested_file_name and current_workflow is not None:
                candidate_name = (current_workflow.metadata.name or "").strip()
                sanitized = re.sub(r"[^A-Za-z0-9._/-]+", "_", candidate_name).strip("_/")
                raw_name = sanitized or datetime.now(tz=UTC).strftime("%d.%m_%H.%M")
            else:
                raw_name = requested_file_name or datetime.now(tz=UTC).strftime("%d.%m_%H.%M")
            creation_date = datetime.now(tz=UTC)
            branched_from = None
            file_name, destination, relative_file_path = self._resolve_named_save_path(raw_name)

        # Ensure creation date is valid (backcompat)
        if (creation_date is None) or (creation_date == EPOCH_START):
            creation_date = datetime.now(tz=UTC)

        return SaveWorkflowTargetInfo(
            scenario=scenario,
            file_name=file_name,
            destination=destination,
            file_path=file_path,
            relative_file_path=relative_file_path,
            creation_date=creation_date,
            branched_from=branched_from,
        )

    def _resolve_versioned_save_target(
        self,
        *,
        situation_name: str,
        requested_file_name: str | None,
        current_workflow: Workflow | None,
        target_workflow: Workflow | None,
    ) -> NamedSavePath:
        """Build the ``(file_name, destination, relative_file_path)`` triple for a versioned save.

        Priority order:

        1. Explicit ``requested_file_name`` *for a workflow we don't already
           know about* (true Save-As to a brand-new name). When the requested
           name maps to an existing registry entry — e.g. the UI re-sends the
           current workflow's key as ``file_name`` — treat that workflow as
           the source of truth and fall through to Step 2 so the macro
           reverse-match advances the version.
        2. Candidate workflow's existing ``file_path`` that matches the
           versioned situation's macro. The matched variables ride through
           the new ``MacroPath``; OSManager's collision-walk steps the
           padded slot forward on write.
        3. Candidate workflow's existing ``file_path`` that does NOT match.
           Use the file's stem as the base and route through the standard
           ``_resolve_named_save_path`` plumbing the same way a
           non-versioned SAVE_AS does. The versioned situation's macro
           decides where the new file lands.
        4. Candidate workflow with no ``file_path`` (unsaved). Sanitize
           ``metadata.name`` and route through ``_resolve_named_save_path``.
        5. Timestamp fallback when no candidate workflow exists.
        """
        # Step 1 only fires for a truly novel requested name. If the name
        # resolves to a workflow already in the registry (target_workflow is set),
        # that's the same identity we'd reverse-match from anyway, so drop into
        # Step 2 instead of starting a fresh "_v001" series under the old name.
        if requested_file_name and target_workflow is None:
            return self._resolve_named_save_path(requested_file_name, situation_name=situation_name)

        # target_workflow (set by Step 1 when the requested name maps to an existing
        # registry entry) beats current_workflow: the workflow the UI named is what
        # we want to reverse-match against, not whatever tab is focused. The prior
        # ordering (current_workflow first) caused the "UI re-sends registry key as
        # file_name" bug — see
        # test_create_versioned_with_requested_name_matching_existing_workflow_runs_match.
        candidate_workflow = target_workflow if target_workflow is not None else current_workflow
        if candidate_workflow is not None and candidate_workflow.file_path is not None:
            matched = self._try_match_versioned_destination(candidate_workflow.file_path, situation_name=situation_name)
            if matched is not None:
                return matched
            # The existing file isn't part of a sequence this situation
            # recognizes. Treat the file's stem like a user-typed name and
            # go through the standard SAVE_AS plumbing.
            stem = derive_registry_key(candidate_workflow.file_path)
            return self._resolve_named_save_path(stem, situation_name=situation_name)

        if candidate_workflow is not None:
            display_name = (candidate_workflow.metadata.name or "").strip()
            sanitized = re.sub(r"[^A-Za-z0-9._/-]+", "_", display_name).strip("_/")
            if sanitized:
                return self._resolve_named_save_path(sanitized, situation_name=situation_name)

        timestamp_name = datetime.now(tz=UTC).strftime("%d.%m_%H.%M")
        return self._resolve_named_save_path(timestamp_name, situation_name=situation_name)

    def _try_match_versioned_destination(self, file_path: str, *, situation_name: str) -> NamedSavePath | None:
        """Reverse-match ``file_path`` against the situation's macro; build a destination on success.

        Returns ``None`` only when the macro doesn't recognize the file —
        e.g. the file was created under a different situation, or lives
        outside the workspace. The caller treats that as "start a new
        versioned series from the file's stem" and falls through to the
        standard save plumbing.

        Raises ``ValueError`` when the active project is in a state where
        reverse-matching can't run at all — missing situation, no current
        project, or no resolvable ``workspace_dir`` builtin. These are
        configuration problems that the caller surfaces to the user.

        On match, the returned ``NamedSavePath`` carries a
        ``ProjectFileDestination`` whose ``MacroPath`` has every variable
        the macro identified — minus builtins, which ProjectManager
        re-derives at resolve time and rejects caller overrides for.
        """
        result = self.engine.handle_request(GetSituationRequest(situation_name=situation_name))
        if not isinstance(result, GetSituationResultSuccess):
            msg = (
                f"Attempted to build a versioned save destination. "
                f"Failed because situation '{situation_name}' was not found in the active project template."
            )
            raise ValueError(msg)  # noqa: TRY004 - missing situation is a config error, not a type error

        situation = result.situation
        try:
            parsed_macro = ParsedMacro(situation.macro)
        except MacroSyntaxError as err:
            msg = (
                f"Attempted to build a versioned save destination for '{file_path}'. "
                f"Failed because situation '{situation_name}' has an invalid macro '{situation.macro}': {err}"
            )
            raise ValueError(msg) from err
        # The match handler expects the path to match the macro template
        # end-to-end. Use the workflow registry's get_complete_file_path, the same
        # absolutize helper non-versioned saves use, so the anchor value
        # the macro sees here matches the rest of the save plumbing.
        #
        # Macro templates use forward-slash separators (the cross-platform
        # convention). On Windows the absolute path comes back with
        # backslashes; normalize to POSIX so the static-text comparison
        # between `{workspace_dir}` and the next segment lines up. The
        # match handler's auto-resolve path POSIX-normalizes the directory
        # builtins it injects, so both sides agree on separator regardless
        # of OS.
        absolute_path = Path(self.engine.workflow_registry.get_complete_file_path(file_path)).as_posix()

        match_result = self.engine.handle_request(
            AttemptMatchPathAgainstMacroRequest(
                parsed_macro=parsed_macro,
                file_path=absolute_path,
                known_variables={},
                auto_resolve_builtins=True,
            )
        )
        if not isinstance(match_result, AttemptMatchPathAgainstMacroResultSuccess):
            # The dispatcher caught an exception inside the handler and returned a
            # generic ResultPayloadFailure. Surface its result_details and exception
            # so users see the underlying cause instead of an opaque wrapper.
            inner = getattr(match_result, "result_details", None)
            exc = getattr(match_result, "exception", None)
            msg = f"Attempted to build a versioned save destination for '{file_path}'. Match handler failed: {inner}"
            if exc is not None:
                msg = f"{msg} (underlying: {type(exc).__name__}: {exc})"
            raise ValueError(msg)  # noqa: TRY004 - handler dispatch failure is a state error
        if match_result.extracted_variables is None:
            # The macro didn't match this file. Not an error — the caller
            # falls through to the standard "new versioned series from the
            # file stem" plumbing.
            return None
        extracted = match_result.extracted_variables

        # Drop builtins from the dict before re-feeding to MacroPath: the
        # ProjectManager re-derives those at resolve time and rejects
        # caller overrides that disagree with the runtime values.
        next_version_variables: MacroVariables = {
            name: value for name, value in extracted.items() if name not in BUILTIN_VARIABLES
        }

        macro_path = MacroPath(parsed_macro=parsed_macro, variables=next_version_variables)
        destination = ProjectFileDestination(
            macro_path,
            existing_file_policy=SITUATION_TO_FILE_POLICY.get(
                situation.policy.on_collision, ExistingFilePolicy.CREATE_NEW
            ),
            create_parents=situation.policy.create_dirs,
        )

        # file_name and relative_file_path are pre-write display strings the
        # registry keys by; the post-write reconciliation block in
        # on_save_workflow_request swaps in the actually-written path. Use
        # the matched file's existing path verbatim — by the time we resolve
        # post-write, the registry will be coherent with what's on disk.
        return NamedSavePath(
            file_name=Path(file_path).stem,
            destination=destination,
            relative_file_path=file_path,
        )

    def _warn_if_situation_policy_mismatches_intent(self, situation_name: str, *, create_versioned: bool) -> None:
        """Log a warning when a situation's policy doesn't match the caller's intent.

        - ``create_versioned=True`` expects an unresolved padded slot; warn when
          the chosen situation uses ``overwrite``.
        - ``create_versioned=False`` expects in-place overwrite; warn when
          ``save_workflow`` has been customized to ``create_new``.

        These mismatches are configuration smells, not hard errors:
        CREATE_NEW without a ``{x:NN}`` slot still works (OSManager's collision
        fallback synthesizes ``_1``, ``_2``, ...), and OVERWRITE with an
        ``{_index:NN}`` slot renders a literal ``_v000``. We surface the
        mismatch so the user can correct the situation if it doesn't reflect
        their intent.
        """
        result = self.engine.handle_request(GetSituationRequest(situation_name=situation_name))
        if not isinstance(result, GetSituationResultSuccess):
            return  # Missing situation surfaces as a load failure elsewhere; nothing useful to warn about here.

        on_collision = result.situation.policy.on_collision
        if create_versioned and on_collision != SituationFilePolicy.CREATE_NEW:
            logger.warning(
                "Versioned save requested but situation '%s' uses '%s' policy; saves may overwrite in place. "
                "Set the situation's policy to 'create_new' (with a padded `{_index:NN}` slot) for true versioning.",
                situation_name,
                on_collision.value,
            )
        elif not create_versioned and on_collision == SituationFilePolicy.CREATE_NEW:
            logger.warning(
                "Non-versioned save requested but situation '%s' uses 'create_new' policy; saves will produce "
                "auto-indexed files instead of overwriting in place. Set the situation's policy to 'overwrite' "
                "for in-place saves, or use create_versioned=True for explicit versioning.",
                situation_name,
            )

    def _save_workflow_file_inline(  # noqa: PLR0913
        self,
        *,
        destination: ProjectFileDestination,
        serialized_flow_commands: SerializedFlowCommands,
        file_name: str,
        creation_date: datetime | None,
        display_name: str | None,
        image_path: str | None,
        description: str | None,
        is_template: bool | None,
        branched_from: str | None,
        workflow_shape: WorkflowShape | None,
    ) -> ResultPayload:
        """Generate the workflow file content and write it to ``destination``.

        Shared by ``on_save_workflow_request`` and
        ``on_save_workflow_file_from_serialized_flow_request``. Callers with a
        pre-resolved Path wrap it as ``ProjectFileDestination(str(path), ...)``
        before calling this helper.
        """
        if creation_date is None:
            creation_date = datetime.now(tz=UTC)

        try:
            workflow_metadata = self.engine.workflow_manager.codegen.generate_workflow_metadata_from_commands(
                serialized_flow_commands=serialized_flow_commands,
                file_name=file_name,
                creation_date=creation_date,
                display_name=display_name,
                image_path=image_path,
                description=description,
                is_template=is_template,
                branched_from=branched_from,
                workflow_shape=workflow_shape,
            )
        except Exception as err:
            details = f"Attempted to save workflow file '{file_name}' from serialized flow commands. Failed during metadata generation: {err}"
            return SaveWorkflowFileFromSerializedFlowResultFailure(result_details=details)

        try:
            final_code_output = self.engine.workflow_manager.codegen.generate_workflow_file_content(
                serialized_flow_commands=serialized_flow_commands,
                workflow_metadata=workflow_metadata,
            )
        except Exception as err:
            details = f"Attempted to save workflow file '{file_name}' from serialized flow commands. Failed during content generation: {err}"
            return SaveWorkflowFileFromSerializedFlowResultFailure(result_details=details)

        write_result = self.write_workflow_file(destination, final_code_output, file_name)
        if not write_result.success:
            return SaveWorkflowFileFromSerializedFlowResultFailure(
                result_details=write_result.error_details,
                failure_reason=write_result.failure_reason,
            )

        # Prefer the post-write location from ``write_workflow_file`` — for
        # macro-driven saves this reflects the resolved-and-possibly-seeded
        # filename (e.g. ``..._v001.py``), not the unresolved template.
        if write_result.written_file is not None:
            try:
                # Re-resolve to get the absolute on-disk path the write actually
                # landed at (``_map_to_macro_file`` may have rewritten the
                # ``File`` to its portable macro form like ``{workspace_dir}/...``).
                final_file_path = write_result.written_file.resolve()
            except FileLoadError:
                # Re-resolution failed (project unloaded between the write and
                # this re-resolve, or the macro form references a directory that
                # disappeared). Fall back to the ``File.location`` string —
                # for non-macro paths it's the absolute path; for macro paths
                # it's the unresolved template, which is still a meaningful
                # human-readable answer for the success message.
                final_file_path = write_result.written_file.location
        else:
            final_file_path = destination.location

        details = f"Successfully saved workflow file at: {final_file_path}"
        return SaveWorkflowFileFromSerializedFlowResultSuccess(
            file_path=final_file_path,
            workflow_metadata=workflow_metadata,
            result_details=details,
        )

    @staticmethod
    def _workspace_relative_path(absolute_or_relative_path: str, engine: Engine) -> str:
        """Return the workspace-relative form of a path, or the absolute path if outside.

        Used post-write to reconcile registry state with the actual on-disk
        location (e.g. when CREATE_NEW seeded an index slot, the written file
        is ``foo_v001.py`` while the request asked for ``foo.py``).
        """
        path = Path(absolute_or_relative_path)
        workspace_path = engine.config_manager.workspace_path
        try:
            relative = canonicalize_for_identity(path).relative_to(canonicalize_for_identity(workspace_path))
        except ValueError:
            # TODO: store the macro form (e.g. "{workspace_dir}/foo.py") in the
            # registry so out-of-workspace save locations stay portable across
            # machines. Tracked in
            # https://github.com/griptape-ai/griptape-nodes/issues/2047.
            return str(path)
        return str(relative)

    @staticmethod
    def _probe_parent_for_disk_check(destination: ProjectFileDestination) -> Path | None:
        """Return the parent directory to use for the pre-write disk-space probe.

        Returns ``None`` when the destination's macro can't be resolved without
        seeding (we'd be duplicating OSManager's seed logic here). The actual
        write will surface a disk-full as IO_ERROR.
        """
        try:
            # Resolve only to learn the target *directory* for the disk-space probe.
            # The macro may still carry unresolved seed-eligible slots (e.g.
            # `{_index:03}`); those get seeded later inside OSManager during the
            # actual write. We don't want to duplicate that seed logic here just
            # to satisfy a best-effort capacity check.
            resolved = destination.resolve()
        except FileLoadError:
            # Macro couldn't resolve (typically because a required `{x:NN}` slot
            # is waiting for OSManager's seed-and-retry). Skip the proactive
            # check and let the write itself raise IO_ERROR if the volume is
            # actually full — the user still gets a clear failure, just without
            # the "X.X GB required" pre-flight message.
            return None
        return Path(resolved).parent

    @staticmethod
    def _format_workflow_write_error(file_name: str, failure_reason: FileIOFailureReason, details: str) -> str:
        """Build the user-facing error string for a workflow write failure."""
        match failure_reason:
            case FileIOFailureReason.IO_ERROR:
                error_msg = details
            case FileIOFailureReason.PERMISSION_DENIED:
                error_msg = f"Permission denied: {details}"
            case FileIOFailureReason.IS_DIRECTORY:
                error_msg = "Path is a directory, not a file"
            case FileIOFailureReason.ENCODING_ERROR:
                error_msg = f"Content encoding error: {details}"
            case FileIOFailureReason.POLICY_NO_OVERWRITE:
                error_msg = (
                    "A different workflow already exists at this location. "
                    "To replace it, save again with overwrite enabled."
                )
            case _:
                error_msg = details
        return f"Attempted to save workflow '{file_name}'. {error_msg}"
