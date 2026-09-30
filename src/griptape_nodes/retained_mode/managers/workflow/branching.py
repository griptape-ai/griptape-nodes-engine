"""Branching, merging, resetting, templating, and comparing workflow files."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, NamedTuple

from griptape_nodes.files.path_utils import (
    derive_registry_key,
)
from griptape_nodes.node_library.workflow_registry import (
    Workflow,
    WorkflowMetadata,
)
from griptape_nodes.retained_mode.engine import EngineScoped
from griptape_nodes.retained_mode.events.base_events import ResultDetail, ResultDetails
from griptape_nodes.retained_mode.events.workflow_events import (
    BranchWorkflowRequest,
    BranchWorkflowResultFailure,
    BranchWorkflowResultSuccess,
    CompareWorkflowsRequest,
    CompareWorkflowsResultFailure,
    CompareWorkflowsResultSuccess,
    CreateWorkflowFromTemplateRequest,
    CreateWorkflowFromTemplateResultFailure,
    CreateWorkflowFromTemplateResultSuccess,
    MergeWorkflowBranchRequest,
    MergeWorkflowBranchResultFailure,
    MergeWorkflowBranchResultSuccess,
    ResetWorkflowBranchRequest,
    ResetWorkflowBranchResultFailure,
    ResetWorkflowBranchResultSuccess,
)
from griptape_nodes.retained_mode.request_handlers import handles

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.events.base_events import ResultPayload
    from griptape_nodes.retained_mode.managers.event_manager import EventManager


logger = logging.getLogger("griptape_nodes")


class _BranchNaming(NamedTuple):
    """The two distinct names a new branch needs.

    `registry_key` is the workspace-relative file path minus its extension, used to key the
    registry. `display_name` is the human-readable title stored as `metadata.name`. Conflating
    them is what made branches show up in the editor as file paths.
    """

    registry_key: str
    display_name: str


class WorkflowBranching(EngineScoped):
    def __init__(self, event_manager: EventManager, *, engine: Engine | None = None) -> None:
        super().__init__(engine)
        event_manager.register_request_handlers(self)

    @handles(BranchWorkflowRequest)
    def on_branch_workflow_request(self, request: BranchWorkflowRequest) -> ResultPayload:  # noqa: PLR0911
        """Create a branch (copy) of an existing workflow with branch tracking."""
        try:
            # Validate source workflow exists
            source_workflow = self.engine.workflow_registry.get_workflow_by_name(request.workflow_name)
        except KeyError:
            details = f"Failed to branch workflow '{request.workflow_name}' because it does not exist"
            return BranchWorkflowResultFailure(result_details=details)

        # Branch copies the source workflow file and produces a new file on disk.
        # Unsaved workflows have no source file to copy from; the user must save first
        # so the branch has a concrete starting point.
        source_file_path_rel = source_workflow.file_path
        if source_file_path_rel is None:
            details = (
                f"Cannot branch unsaved workflow '{request.workflow_name}' because it has no file on disk. "
                "Save the workflow before branching it."
            )
            return BranchWorkflowResultFailure(result_details=details)

        # A caller-supplied display name has to actually say something; a blank label would leave the
        # branch looking nameless everywhere the editor shows a workflow title.
        requested_display_name = request.branched_workflow_display_name
        if requested_display_name is not None and not requested_display_name.strip():
            details = (
                f"Attempted to branch workflow '{request.workflow_name}' with an empty display name. "
                "Provide a display name with at least one non-whitespace character, or leave it unset "
                "to name the branch after the workflow it came from."
            )
            return BranchWorkflowResultFailure(result_details=details)

        branch_naming = self._resolve_branch_naming(request=request, source_workflow=source_workflow)
        branch_name = branch_naming.registry_key
        branch_display_name = branch_naming.display_name

        # Refuse a name that is already taken, in either namespace that can claim it. This is the
        # only collision guard a caller-supplied `branched_workflow_name` passes through -- the
        # counter walk inside _resolve_branch_naming runs only when the caller named nothing --
        # so it has to be the same predicate, or a supplied name lands on an existing branch and
        # the situation's overwrite policy replaces it.
        if self._branch_name_taken(branch_name):
            details = (
                f"Attempted to branch workflow '{request.workflow_name}' as '{branch_name}'. "
                "Failed because a workflow is already saved under that name."
            )
            return BranchWorkflowResultFailure(result_details=details)

        try:
            # Create branch metadata by copying source metadata
            branch_metadata = WorkflowMetadata(
                # The display name, not the registry key: `branch_name` is a file path.
                name=branch_display_name,
                schema_version=source_workflow.metadata.schema_version,
                engine_version_created_with=source_workflow.metadata.engine_version_created_with,
                node_libraries_referenced=source_workflow.metadata.node_libraries_referenced.copy(),
                node_types_used=source_workflow.metadata.node_types_used.copy(),
                workflows_referenced=source_workflow.metadata.workflows_referenced.copy()
                if source_workflow.metadata.workflows_referenced
                else None,
                description=source_workflow.metadata.description,
                image=source_workflow.metadata.image,
                is_griptape_provided=False,  # Branches are always user-created
                is_template=False,
                creation_date=datetime.now(tz=UTC),
                last_modified_date=source_workflow.metadata.last_modified_date,
                branched_from=request.workflow_name,
            )

            # Read source workflow content and replace metadata header
            source_file_path = self.engine.workflow_registry.get_complete_file_path(source_file_path_rel)
            if not Path(source_file_path).exists():
                details = f"Failed to branch workflow '{request.workflow_name}': File path '{source_file_path}' does not exist. The workflow may have been moved or the workspace configuration may have changed."
                return BranchWorkflowResultFailure(result_details=details)

            source_content = Path(source_file_path).read_text(encoding="utf-8")

            # Replace the metadata header with branch metadata
            branch_content = self.engine.workflow_manager.codegen.replace_workflow_metadata_header(
                source_content, branch_metadata
            )
            if branch_content is None:
                details = f"Failed to replace metadata header for branch workflow '{branch_name}'"
                return BranchWorkflowResultFailure(result_details=details)

            # Write the branch file to disk BEFORE registering it (the registry requires the
            # file to exist), through the save_workflow situation so a branch lands where the
            # project puts workflows rather than at the workspace root.
            created = self.engine.workflow_manager.saver.create_workflow_file(branch_name, branch_content)
            if not created.success:
                details = f"Failed to branch workflow '{request.workflow_name}': {created.error_details}"
                return BranchWorkflowResultFailure(result_details=details)

            # Key by the path actually written, and report that key: callers open the branch by
            # the name they get back.
            branch_registry_key = derive_registry_key(created.relative_file_path)
            self.engine.workflow_registry.generate_new_workflow(
                registry_key=branch_registry_key,
                metadata=branch_metadata,
                file_path=created.relative_file_path,
            )

            details = f"Successfully branched workflow '{request.workflow_name}' as '{branch_name}'"
            return BranchWorkflowResultSuccess(
                branched_workflow_name=branch_registry_key,
                original_workflow_name=request.workflow_name,
                result_details=ResultDetails(message=details, level=logging.INFO),
            )

        except Exception as e:
            details = f"Failed to branch workflow '{request.workflow_name}': {e!s}"
            import traceback

            traceback.print_exc()
            return BranchWorkflowResultFailure(result_details=details)

    @handles(CreateWorkflowFromTemplateRequest)
    def on_create_workflow_from_template_request(self, request: CreateWorkflowFromTemplateRequest) -> ResultPayload:  # noqa: PLR0911
        """Create a new workflow file from a template (Griptape-provided or user-provided)."""
        try:
            template_workflow = self.engine.workflow_registry.get_workflow_by_name(request.template_name)
        except KeyError:
            details = (
                f"Attempted to create workflow from template '{request.template_name}'. "
                "Failed because template workflow was not found in registry."
            )
            return CreateWorkflowFromTemplateResultFailure(result_details=details)

        if not template_workflow.metadata.is_template:
            details = (
                f"Attempted to create workflow from template '{request.template_name}'. "
                "Failed because workflow is not marked as a template (is_template must be True)."
            )
            return CreateWorkflowFromTemplateResultFailure(result_details=details)

        template_file_path_rel = template_workflow.file_path
        if template_file_path_rel is None:
            details = (
                f"Attempted to create workflow from template '{request.template_name}'. "
                "Failed because the template is unsaved (has no file on disk)."
            )
            return CreateWorkflowFromTemplateResultFailure(result_details=details)

        source_file_path = self.engine.workflow_registry.get_complete_file_path(template_file_path_rel)
        if not Path(source_file_path).is_file():
            details = (
                f"Attempted to create workflow from template '{request.template_name}'. "
                f"Failed because template file path '{source_file_path}' does not exist."
            )
            return CreateWorkflowFromTemplateResultFailure(result_details=details)

        base_name = request.file_name or Path(template_file_path_rel).stem
        new_file_name = self.engine.workflow_manager.saver.generate_unique_filename(base_name)

        new_metadata = WorkflowMetadata(
            name=new_file_name,
            schema_version=template_workflow.metadata.schema_version,
            engine_version_created_with=template_workflow.metadata.engine_version_created_with,
            node_libraries_referenced=template_workflow.metadata.node_libraries_referenced.copy(),
            node_types_used=template_workflow.metadata.node_types_used.copy(),
            workflows_referenced=template_workflow.metadata.workflows_referenced.copy()
            if template_workflow.metadata.workflows_referenced
            else None,
            description=template_workflow.metadata.description,
            image=template_workflow.metadata.image,
            is_griptape_provided=False,
            is_template=False,
            creation_date=datetime.now(tz=UTC),
            last_modified_date=template_workflow.metadata.last_modified_date,
            branched_from=None,
        )

        source_content = Path(source_file_path).read_text(encoding="utf-8")
        new_content = self.engine.workflow_manager.codegen.replace_workflow_metadata_header(
            source_content, new_metadata
        )
        if new_content is None:
            details = (
                f"Attempted to create workflow from template '{request.template_name}'. "
                f"Failed because metadata header replacement failed for '{new_file_name}'."
            )
            return CreateWorkflowFromTemplateResultFailure(result_details=details)

        created = self.engine.workflow_manager.saver.create_workflow_file(new_file_name, new_content)
        if not created.success:
            details = f"Attempted to create workflow from template '{request.template_name}'. {created.error_details}"
            return CreateWorkflowFromTemplateResultFailure(result_details=details)

        # Key by the path actually written, and hand that key back: the caller puts the new
        # workflow into context by this name, so it has to be the name the registry holds.
        registry_key = derive_registry_key(created.relative_file_path)
        self.engine.workflow_registry.generate_new_workflow(
            registry_key=registry_key,
            metadata=new_metadata,
            file_path=created.relative_file_path,
        )

        details = f"Successfully created workflow '{new_file_name}' from template '{request.template_name}'"
        return CreateWorkflowFromTemplateResultSuccess(
            workflow_name=registry_key,
            file_path=created.absolute_path,
            result_details=ResultDetails(message=details, level=logging.INFO),
        )

    @handles(MergeWorkflowBranchRequest)
    def on_merge_workflow_branch_request(self, request: MergeWorkflowBranchRequest) -> ResultPayload:  # noqa: PLR0911
        """Merge a branch back into its source workflow, removing the branch when complete."""
        try:
            # Validate branch workflow exists
            branch_workflow = self.engine.workflow_registry.get_workflow_by_name(request.workflow_name)
        except KeyError as e:
            details = f"Failed to merge workflow branch because it does not exist: {e!s}"
            return MergeWorkflowBranchResultFailure(result_details=details)

        # Get source workflow name from branch metadata
        source_workflow_name = branch_workflow.metadata.branched_from
        if not source_workflow_name:
            details = f"Failed to merge workflow branch '{request.workflow_name}' because it has no source workflow"
            return MergeWorkflowBranchResultFailure(result_details=details)

        # Validate source workflow exists
        try:
            source_workflow = self.engine.workflow_registry.get_workflow_by_name(source_workflow_name)
        except KeyError:
            details = f"Failed to merge workflow branch '{request.workflow_name}' because source workflow '{source_workflow_name}' does not exist"
            return MergeWorkflowBranchResultFailure(result_details=details)

        # Merge rewrites both files on disk; both sides must be saved. The branch relation
        # is only ever established between saved workflows today, but narrow defensively.
        branch_file_path_rel = branch_workflow.file_path
        source_file_path_rel = source_workflow.file_path
        if branch_file_path_rel is None or source_file_path_rel is None:
            details = (
                f"Failed to merge workflow branch '{request.workflow_name}' because "
                "either the branch or its source is unsaved (no file on disk)."
            )
            return MergeWorkflowBranchResultFailure(result_details=details)

        try:
            # Create updated metadata for source workflow - update timestamp
            merged_metadata = WorkflowMetadata(
                # Carry the source's existing display name across. `source_workflow_name` came from
                # the branch's `branched_from`, which holds a registry key (a file path), so using it
                # here would overwrite the source's correct title with a path and persist that to disk.
                # A merge changes the source's contents, never what it is called.
                name=source_workflow.metadata.name,
                schema_version=source_workflow.metadata.schema_version,
                engine_version_created_with=source_workflow.metadata.engine_version_created_with,
                node_libraries_referenced=source_workflow.metadata.node_libraries_referenced.copy(),
                node_types_used=source_workflow.metadata.node_types_used.copy(),
                workflows_referenced=source_workflow.metadata.workflows_referenced.copy()
                if source_workflow.metadata.workflows_referenced
                else None,
                description=source_workflow.metadata.description,
                image=source_workflow.metadata.image,
                is_griptape_provided=source_workflow.metadata.is_griptape_provided,
                is_template=source_workflow.metadata.is_template,
                is_internal=source_workflow.metadata.is_internal,
                creation_date=source_workflow.metadata.creation_date,
                last_modified_date=datetime.now(tz=UTC),
                branched_from=source_workflow.metadata.branched_from,  # Preserve original source chain
            )

            # Read branch content and replace metadata header with merged metadata
            branch_content_file_path = self.engine.workflow_registry.get_complete_file_path(branch_file_path_rel)
            branch_content = Path(branch_content_file_path).read_text(encoding="utf-8")

            # Replace the metadata header with merged metadata
            merged_content = self.engine.workflow_manager.codegen.replace_workflow_metadata_header(
                branch_content, merged_metadata
            )
            if merged_content is None:
                details = f"Failed to replace metadata header for merged workflow '{source_workflow_name}'"
                return MergeWorkflowBranchResultFailure(result_details=details)

            # Write the updated content to the source workflow file
            source_file_path = self.engine.workflow_registry.get_complete_file_path(source_file_path_rel)
            Path(source_file_path).write_text(merged_content, encoding="utf-8")

            # Update the registry with new metadata for the source workflow
            source_workflow.metadata = merged_metadata

            # Remove the branch workflow from registry and delete file
            result_messages = []
            try:
                self.engine.workflow_registry.delete_workflow_by_name(request.workflow_name)
                self.engine.workflow_manager.variable_substitution.drop(request.workflow_name)
                # TODO: Replace with DeleteFileRequest https://github.com/griptape-ai/griptape-nodes/issues/3765
                Path(branch_content_file_path).unlink()
                cleanup_message = f"Deleted branch workflow file and registry entry for '{request.workflow_name}'"
                result_messages.append(ResultDetail(message=cleanup_message, level=logging.INFO))
            except Exception as delete_error:
                warning_message = (
                    f"Failed to fully clean up branch workflow '{request.workflow_name}': {delete_error!s}"
                )
                result_messages.append(ResultDetail(message=warning_message, level=logging.WARNING))
                # Continue anyway - the merge was successful even if cleanup failed

            success_message = f"Successfully merged branch workflow '{request.workflow_name}' into source workflow '{source_workflow_name}'"
            result_messages.append(ResultDetail(message=success_message, level=logging.INFO))

            return MergeWorkflowBranchResultSuccess(
                merged_workflow_name=source_workflow_name, result_details=ResultDetails(*result_messages)
            )

        except Exception as e:
            details = f"Failed to merge branch workflow '{request.workflow_name}' into source workflow '{source_workflow_name}': {e!s}"
            return MergeWorkflowBranchResultFailure(result_details=details)

    @handles(ResetWorkflowBranchRequest)
    def on_reset_workflow_branch_request(self, request: ResetWorkflowBranchRequest) -> ResultPayload:  # noqa: PLR0911
        """Reset a branch to match its source workflow, discarding branch changes."""
        try:
            # Validate branch workflow exists
            branch_workflow = self.engine.workflow_registry.get_workflow_by_name(request.workflow_name)
        except KeyError as e:
            details = f"Failed to reset workflow branch because it does not exist: {e!s}"
            return ResetWorkflowBranchResultFailure(result_details=details)

        # Get source workflow name from branch metadata
        source_workflow_name = branch_workflow.metadata.branched_from
        if not source_workflow_name:
            details = f"Failed to reset workflow branch '{request.workflow_name}' because it has no source workflow"
            return ResetWorkflowBranchResultFailure(result_details=details)

        # Validate source workflow exists
        try:
            source_workflow = self.engine.workflow_registry.get_workflow_by_name(source_workflow_name)
        except KeyError:
            details = f"Failed to reset workflow branch '{request.workflow_name}' because source workflow '{source_workflow_name}' does not exist"
            return ResetWorkflowBranchResultFailure(result_details=details)

        # Reset rewrites the branch file from the source file; both sides must be saved.
        branch_file_path_rel = branch_workflow.file_path
        source_file_path_rel = source_workflow.file_path
        if branch_file_path_rel is None or source_file_path_rel is None:
            details = (
                f"Failed to reset workflow branch '{request.workflow_name}' because "
                "either the branch or its source is unsaved (no file on disk)."
            )
            return ResetWorkflowBranchResultFailure(result_details=details)

        try:
            # Read content from the source workflow (what we're resetting the branch to)
            source_content_file_path = self.engine.workflow_registry.get_complete_file_path(source_file_path_rel)
            source_content = Path(source_content_file_path).read_text(encoding="utf-8")

            # Create updated metadata for branch workflow - preserve branch relationship and source timestamp
            reset_metadata = WorkflowMetadata(
                # Keep the branch's own display name. A reset discards the branch's *content* changes;
                # its identity fields below (creation_date, branched_from, is_template) are likewise
                # kept from the branch, and its title belongs with them. `request.workflow_name` is a
                # registry key, so using it would overwrite the branch's title with a path on disk.
                name=branch_workflow.metadata.name,
                schema_version=source_workflow.metadata.schema_version,
                engine_version_created_with=source_workflow.metadata.engine_version_created_with,
                node_libraries_referenced=source_workflow.metadata.node_libraries_referenced.copy(),
                node_types_used=source_workflow.metadata.node_types_used.copy(),
                workflows_referenced=source_workflow.metadata.workflows_referenced.copy()
                if source_workflow.metadata.workflows_referenced
                else None,
                description=source_workflow.metadata.description,
                image=source_workflow.metadata.image,
                is_griptape_provided=branch_workflow.metadata.is_griptape_provided,
                is_template=branch_workflow.metadata.is_template,
                is_internal=branch_workflow.metadata.is_internal,
                creation_date=branch_workflow.metadata.creation_date,
                last_modified_date=source_workflow.metadata.last_modified_date,
                branched_from=source_workflow_name,  # Preserve branch relationship
            )

            # Replace the metadata header with reset metadata
            reset_content = self.engine.workflow_manager.codegen.replace_workflow_metadata_header(
                source_content, reset_metadata
            )
            if reset_content is None:
                details = f"Failed to replace metadata header for reset branch workflow '{request.workflow_name}'"
                return ResetWorkflowBranchResultFailure(result_details=details)

            # Write the updated content to the branch workflow file
            branch_content_file_path = self.engine.workflow_registry.get_complete_file_path(branch_file_path_rel)
            Path(branch_content_file_path).write_text(reset_content, encoding="utf-8")

            # Update the registry with new metadata for the branch workflow
            branch_workflow.metadata = reset_metadata

        except Exception as e:
            details = f"Failed to reset branch workflow '{request.workflow_name}' to source workflow '{source_workflow_name}': {e!s}"
            return ResetWorkflowBranchResultFailure(result_details=details)
        else:
            details = f"Successfully reset branch workflow '{request.workflow_name}' to match source workflow '{source_workflow_name}'"
            return ResetWorkflowBranchResultSuccess(
                reset_workflow_name=request.workflow_name,
                result_details=ResultDetails(message=details, level=logging.INFO),
            )

    @handles(CompareWorkflowsRequest)
    def on_compare_workflows_request(self, request: CompareWorkflowsRequest) -> ResultPayload:
        """Compare two workflows to determine if one is ahead, behind, or up-to-date relative to the other."""
        try:
            # Get the workflow to evaluate
            workflow = self.engine.workflow_registry.get_workflow_by_name(request.workflow_name)
        except KeyError:
            details = f"Failed to compare workflow '{request.workflow_name}' because it does not exist"
            return CompareWorkflowsResultFailure(result_details=details)

        # Use the provided compare_workflow_name
        compare_workflow_name = request.compare_workflow_name

        # Try to get the source workflow
        try:
            source_workflow = self.engine.workflow_registry.get_workflow_by_name(compare_workflow_name)
        except KeyError:
            # Source workflow no longer exists
            details = f"Source workflow '{compare_workflow_name}' for '{request.workflow_name}' no longer exists"
            return CompareWorkflowsResultSuccess(
                workflow_name=request.workflow_name,
                compare_workflow_name=compare_workflow_name,
                status="no_source",
                workflow_last_modified=workflow.metadata.last_modified_date.isoformat()
                if workflow.metadata.last_modified_date
                else None,
                source_last_modified=None,
                details=details,
                result_details="Workflow comparison completed successfully.",
            )

        # Compare last modified dates
        workflow_last_modified = workflow.metadata.last_modified_date
        source_last_modified = source_workflow.metadata.last_modified_date

        # Handle missing timestamps
        if workflow_last_modified is None or source_last_modified is None:
            details = f"Cannot compare timestamps - workflow: {workflow_last_modified}, source: {source_last_modified}"
            logger.warning(details)
            return CompareWorkflowsResultSuccess(
                workflow_name=request.workflow_name,
                compare_workflow_name=compare_workflow_name,
                status="diverged",
                workflow_last_modified=workflow_last_modified.isoformat() if workflow_last_modified else None,
                source_last_modified=source_last_modified.isoformat() if source_last_modified else None,
                details=details,
                result_details="Workflow comparison completed successfully.",
            )

        # Compare timestamps to determine status
        if workflow_last_modified == source_last_modified:
            status = "up_to_date"
            details = f"Workflow '{request.workflow_name}' is up-to-date with source '{compare_workflow_name}'"
        elif workflow_last_modified > source_last_modified:
            status = "ahead"
            details = f"Workflow '{request.workflow_name}' is ahead of source '{compare_workflow_name}' (local changes)"
        else:
            status = "behind"
            details = (
                f"Workflow '{request.workflow_name}' is behind source '{compare_workflow_name}' (source has updates)"
            )

        return CompareWorkflowsResultSuccess(
            workflow_name=request.workflow_name,
            compare_workflow_name=compare_workflow_name,
            status=status,
            workflow_last_modified=workflow_last_modified.isoformat(),
            source_last_modified=source_last_modified.isoformat(),
            details=details,
            result_details="Workflow comparison completed successfully.",
        )

    def _resolve_branch_naming(self, *, request: BranchWorkflowRequest, source_workflow: Workflow) -> _BranchNaming:
        """Pick the registry key and display name for a new branch of ``source_workflow``.

        The two are resolved together because the label depends on how the key was chosen: an
        auto-generated key contributes its ``_branch_<n>`` counter to the label, while a
        caller-supplied key does not. ``request.branched_workflow_display_name``, when given, wins
        over either derivation; ``on_branch_workflow_request`` has already rejected a blank one.
        """
        branch_counter = None
        branch_registry_key = request.branched_workflow_name
        if branch_registry_key is None:
            branch_counter = 1
            branch_registry_key = f"{request.workflow_name}_branch_{branch_counter}"
            while self._branch_name_taken(branch_registry_key):
                branch_counter += 1
                branch_registry_key = f"{request.workflow_name}_branch_{branch_counter}"

        requested_display_name = request.branched_workflow_display_name
        if requested_display_name is not None:
            return _BranchNaming(registry_key=branch_registry_key, display_name=requested_display_name.strip())

        derived_display_name = self._derive_branch_display_name(
            source_display_name=source_workflow.metadata.name,
            source_registry_key=request.workflow_name,
            branch_registry_key=branch_registry_key,
            branch_counter=branch_counter,
        )
        return _BranchNaming(registry_key=branch_registry_key, display_name=derived_display_name)

    def _branch_name_taken(self, branch_registry_key: str) -> bool:
        """Whether a candidate branch name is unavailable, in either namespace that can claim it.

        A branch is registered under the path ``save_workflow`` wrote it to, so a name can be
        free as a registry key and still resolve onto a file that is already there -- the two
        never meet when the source is keyed workspace-relative and the destination is outside
        the workspace. Walking the counter on the registry alone would keep offering the same
        name, and the situation's overwrite policy would replace the earlier branch with it.
        """
        if self.engine.workflow_registry.has_workflow_with_name(branch_registry_key):
            return True
        return self.engine.workflow_manager.saver.workflow_destination_exists(branch_registry_key)

    def _derive_branch_display_name(
        self,
        *,
        source_display_name: str | None,
        source_registry_key: str,
        branch_registry_key: str,
        branch_counter: int | None,
    ) -> str:
        """Compute the human-readable label (``metadata.name``) for a new branch.

        A registry key is a file path; ``metadata.name`` is a title. Using the key as the label makes
        a branch of "Shot 010 Comp" read as "shots/sh010/comp_branch_1" everywhere the editor shows a
        workflow name, and the deeper the folder structure the worse it reads.

        * ``branch_counter`` is set when the caller auto-generated the branch key as
          ``<source key>_branch_<counter>``. The label mirrors that counter so it stays in step with
          the key: "Shot 010 Comp (branch 1)".
        * ``branch_counter`` is None when the caller supplied ``branched_workflow_name`` themselves.
          They chose the key, so its final path segment is the most faithful label available.
        """
        if branch_counter is None:
            return PurePosixPath(branch_registry_key).name

        source_label = (source_display_name or "").strip()
        if source_label:
            # Keep only the final segment. A path-shaped source label is exactly the bug this
            # derivation exists to fix -- branches created before it carry their full registry key as
            # their name -- so deriving from one verbatim would carry the path forward.
            source_label = PurePosixPath(source_label).name.strip()
        if not source_label:
            # A blank (or purely separator) display name means the source's own metadata is corrupt.
            # A label off the file path beats propagating emptiness onto the branch.
            source_label = PurePosixPath(source_registry_key).name
        return f"{source_label} (branch {branch_counter})"
