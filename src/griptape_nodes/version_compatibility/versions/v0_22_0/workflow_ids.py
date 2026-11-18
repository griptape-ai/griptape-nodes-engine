"""Links to other workflows by id, for workflows saved before schema 0.22.0, which link by registry name."""

from __future__ import annotations

import ast
import logging
from typing import TYPE_CHECKING, NamedTuple

import semver

from griptape_nodes.retained_mode.managers.version_compatibility_manager import WorkflowMigration

if TYPE_CHECKING:
    from griptape_nodes.node_library.workflow_registry import WorkflowMetadata

logger = logging.getLogger("griptape_nodes")

_IDS_SCHEMA_VERSION = semver.VersionInfo(0, 22, 0)
_IMPORT_REQUEST_NAME = "ImportWorkflowAsReferencedSubFlowRequest"


class _SourceEdit(NamedTuple):
    """Text in a workflow file's code to replace, by character offsets."""

    start: int
    end: int
    text: str


class WorkflowIdsMigration(WorkflowMigration):
    """Point an older workflow's links to other workflows at their ids.

    Before schema 0.22.0 a workflow named other workflows by registry name: the workflow it is a
    branch of (`branched_from`), the workflows it imports as referenced subflows
    (`workflows_referenced`), and those imports in its code
    (`ImportWorkflowAsReferencedSubFlowRequest(workflow_name=...)`). A name follows the other
    workflow's file, so a rename or move broke the link. A name no registered workflow has is left
    as it is.
    """

    def applies_to_workflow(self, workflow_metadata: WorkflowMetadata) -> bool:
        try:
            schema_version = semver.VersionInfo.parse(workflow_metadata.schema_version)
        except ValueError:
            return False
        return schema_version < _IDS_SCHEMA_VERSION

    def migrate_metadata(self, workflow_metadata: WorkflowMetadata) -> WorkflowMetadata:
        updates: dict[str, object] = {}
        if workflow_metadata.branched_from is not None:
            updates["branched_from"] = self._id_for_name(workflow_metadata.branched_from)
        if workflow_metadata.workflows_referenced:
            referenced = [self._id_for_name(name) for name in workflow_metadata.workflows_referenced]
            updates["workflows_referenced"] = list(dict.fromkeys(referenced))
        return workflow_metadata.model_copy(update=updates)

    def migrate_source(self, workflow_source: str) -> str:
        try:
            tree = ast.parse(workflow_source)
        except SyntaxError:
            # The run reports the file's syntax error itself.
            return workflow_source
        line_starts = _line_starts(workflow_source)
        edits = [edit for node in ast.walk(tree) if (edit := self._import_edit(node, workflow_source, line_starts))]
        migrated = workflow_source
        for edit in sorted(edits, key=lambda edit: edit.start, reverse=True):
            migrated = migrated[: edit.start] + edit.text + migrated[edit.end :]
        return migrated

    def _import_edit(self, node: ast.AST, workflow_source: str, line_starts: list[int]) -> _SourceEdit | None:
        """The edit that names the workflow an import call names by registry name by its id, or None."""
        name_keyword = _import_name_keyword(node)
        if name_keyword is None or name_keyword.end_lineno is None or name_keyword.end_col_offset is None:
            return None
        workflow = self.engine.workflow_registry.find_by_name(name_keyword.value.value)  # type: ignore[attr-defined]
        if workflow is None:
            return None
        start = _offset(workflow_source, line_starts, name_keyword.lineno, name_keyword.col_offset)
        end = _offset(workflow_source, line_starts, name_keyword.end_lineno, name_keyword.end_col_offset)
        return _SourceEdit(start=start, end=end, text=f"workflow_id={workflow.workflow_id!r}")

    def _id_for_name(self, name: str) -> str:
        workflow = self.engine.workflow_registry.find_by_name(name)
        if workflow is None:
            logger.debug("Workflow '%s' is named by a saved link, but no workflow has that name.", name)
            return name
        return workflow.workflow_id


def _import_name_keyword(node: ast.AST) -> ast.keyword | None:
    """The `workflow_name="..."` argument of an import call that names no `workflow_id`, or None."""
    if not isinstance(node, ast.Call) or _called_name(node.func) != _IMPORT_REQUEST_NAME:
        return None
    keywords = {keyword.arg: keyword for keyword in node.keywords}
    name_keyword = keywords.get("workflow_name")
    if "workflow_id" in keywords or name_keyword is None:
        return None
    if not isinstance(name_keyword.value, ast.Constant) or not isinstance(name_keyword.value.value, str):
        return None
    return name_keyword


def _called_name(func: ast.expr) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _line_starts(source: str) -> list[int]:
    """The character offset each line of `source` starts at."""
    starts = [0]
    for line in source.splitlines(keepends=True):
        starts.append(starts[-1] + len(line))
    return starts


def _offset(source: str, line_starts: list[int], lineno: int, col_offset: int) -> int:
    """The character offset of an AST position, whose column counts UTF-8 bytes."""
    line_start = line_starts[lineno - 1]
    line = source[line_start : line_starts[lineno]]
    return line_start + len(line.encode("utf-8")[:col_offset].decode("utf-8"))
