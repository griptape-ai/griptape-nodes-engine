from __future__ import annotations

import json
import logging
import re
import tomllib
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, NamedTuple

from pydantic import BaseModel, Field, ValidationError, field_serializer, field_validator

from griptape_nodes.files.path_utils import (
    canonicalize_for_identity,
    canonicalize_for_identity_preserving_symlinks,
    derive_registry_key,
    relative_to_keeping_or_following_links,
    resolve_workspace_path,
)
from griptape_nodes.node_library.library_registry import (
    LibraryNameAndVersion,
)

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.managers.config_manager import ConfigManager

logger = logging.getLogger("griptape_nodes")


def new_workflow_id() -> str:
    """Mint a workflow id. It lasts while the engine runs, and is never written to the workflow's file."""
    return uuid.uuid4().hex


# Name of the inline metadata block that carries a workflow's metadata header.
WORKFLOW_METADATA_BLOCK_NAME = "script"

# PEP 723-style inline metadata block: a "# /// <name>" opener, comment-prefixed body lines,
# and a "# ///" closer.
_METADATA_BLOCK_PATTERN = re.compile(r"(?m)^# /// (?P<type>[a-zA-Z0-9-]+)$\s(?P<content>(^#(| .*)$\s)+)^# ///$")

_TOOL_TABLE = "tool"
_GRIPTAPE_NODES_TABLE = "griptape-nodes"
# How the required table is spelled in messages and in the editor's workflow-load report.
METADATA_TABLE_PATH = f"[{_TOOL_TABLE}.{_GRIPTAPE_NODES_TABLE}]"


class WorkflowMetadataError(Exception):
    """Raised when a workflow file's metadata header cannot be read.

    Callers that only need to know it failed catch this. Callers that report per-stage detail, such
    as the editor's workflow-load report, catch the subclasses below, which carry the values that
    report displays.
    """


class WorkflowMetadataFileError(WorkflowMetadataError):
    """The workflow file itself could not be read."""


class WorkflowMetadataSectionCountError(WorkflowMetadataError):
    """The file does not carry exactly one metadata header."""

    def __init__(self, message: str, *, section_name: str, count: int) -> None:
        super().__init__(message)
        self.section_name = section_name
        self.count = count


class WorkflowMetadataTomlError(WorkflowMetadataError):
    """The metadata header is not valid TOML."""

    def __init__(self, message: str, *, error_message: str) -> None:
        super().__init__(message)
        self.error_message = error_message


class WorkflowMetadataMissingTableError(WorkflowMetadataError):
    """The metadata header carries no `[tool.griptape-nodes]` table."""

    def __init__(self, message: str, *, section_path: str) -> None:
        super().__init__(message)
        self.section_path = section_path


class WorkflowMetadataSchemaError(WorkflowMetadataError):
    """The `[tool.griptape-nodes]` table does not match `WorkflowMetadata`."""

    def __init__(self, message: str, *, section_path: str, error_message: str) -> None:
        super().__init__(message)
        self.section_path = section_path
        self.error_message = error_message


class LibraryNameAndNodeType(NamedTuple):
    library_name: str
    node_type: str


# Type aliases for clarity
type NodeName = str
type ParameterName = str
type ParameterAttribute = str
type ParameterMinimalDict = dict[ParameterAttribute, Any]
type NodeParametersMapping = dict[NodeName, dict[ParameterName, ParameterMinimalDict]]


class WorkflowShape(BaseModel):
    """This structure reflects the input and output shapes extracted from StartNodes and EndNodes inside of the workflow.

    A workflow may have multiple StartNodes and multiple EndNodes, each contributing their parameters
    to the overall workflow shape.

    Structure is:
    - inputs: {start_node_name: {param_name: param_minimal_dict}}
    - outputs: {end_node_name: {param_name: param_minimal_dict}}
    """

    inputs: NodeParametersMapping = Field(default_factory=dict)
    outputs: NodeParametersMapping = Field(default_factory=dict)


class WorkflowMetadata(BaseModel):
    LATEST_SCHEMA_VERSION: ClassVar[str] = "0.21.0"

    name: str
    schema_version: str
    engine_version_created_with: str
    node_libraries_referenced: list[LibraryNameAndVersion]
    node_types_used: set[LibraryNameAndNodeType] = Field(default_factory=set)
    workflows_referenced: list[str] | None = None
    description: str | None = None
    image: str | None = None
    is_griptape_provided: bool | None = False
    is_template: bool | None = False
    # Hidden from the GUI workflow picker. Set on library-internal workflows (e.g. integration tests)
    # that ship inside a library directory but should never surface to users.
    is_internal: bool | None = False
    creation_date: datetime | None = Field(default=None)
    last_modified_date: datetime | None = Field(default=None)
    branched_from: str | None = Field(default=None)
    workflow_shape: WorkflowShape | None = Field(default=None)

    @field_serializer("node_types_used")
    def serialize_node_types_used(self, node_types_used: set[LibraryNameAndNodeType]) -> list[list[str]]:
        """Serialize node_types_used as list of tuples for TOML compatibility.

        Sets and NamedTuples are not directly supported by TOML, so we convert the set
        to a list of lists (each inner list represents [library_name, node_type]).
        """
        return [[nt.library_name, nt.node_type] for nt in sorted(node_types_used)]

    @field_validator("node_types_used", mode="before")
    @classmethod
    def validate_node_types_used(cls, value: Any) -> set[LibraryNameAndNodeType]:
        """Deserialize node_types_used from list of lists during TOML loading.

        When loading workflow metadata from TOML files, the node_types_used field
        is stored as a list of [library_name, node_type] pairs that needs to be
        converted back to a set of LibraryNameAndNodeType objects. This validator
        handles the expected input formats:
        - List of lists (from TOML deserialization)
        - Set of LibraryNameAndNodeType (from direct Python construction)
        - Empty list (for workflows with no nodes)
        """
        if isinstance(value, set):
            return value
        if isinstance(value, list):
            return {LibraryNameAndNodeType(library_name=item[0], node_type=item[1]) for item in value}
        msg = f"Expected list or set for node_types_used, got {type(value)}"
        raise ValueError(msg)

    @field_serializer("workflow_shape")
    def serialize_workflow_shape(self, workflow_shape: WorkflowShape | None) -> str | None:
        """Serialize WorkflowShape as JSON string to avoid TOML serialization issues.

        The WorkflowShape contains deeply nested dictionaries with None values that are
        meaningful data (e.g., default_value: None). TOML's nested table format creates
        unreadable output and tomlkit fails on None values in nested structures.
        JSON preserves None as null and keeps the data compact and readable.
        """
        if workflow_shape is None:
            return None
        # Use json.dumps to preserve None values as null, which TOML can handle
        return json.dumps(workflow_shape.model_dump(), separators=(",", ":"))

    @field_validator("workflow_shape", mode="before")
    @classmethod
    def validate_workflow_shape(cls, value: Any) -> WorkflowShape | None:
        """Deserialize WorkflowShape from JSON string during TOML loading.

        When loading workflow metadata from TOML files, the workflow_shape field
        is stored as a JSON string that needs to be converted back to a WorkflowShape
        object. This validator handles the expected input formats:
        - JSON strings (from TOML deserialization)
        - WorkflowShape objects (from direct Python construction)
        - None values (workflows without Start/End nodes)

        If JSON deserialization fails, logs a warning and returns None for graceful
        degradation, consistent with other metadata parsing failures in this codebase.
        """
        if value is None:
            return None
        if isinstance(value, WorkflowShape):
            return value
        if isinstance(value, str):
            try:
                data = json.loads(value)
                return WorkflowShape(**data)
            except (json.JSONDecodeError, TypeError, ValueError) as e:
                logger.error("Failed to deserialize workflow_shape from JSON: %s", e)
                return None
        # Unexpected type - let Pydantic's normal validation handle it
        return value


def find_metadata_blocks(workflow_content: str, block_name: str) -> list[re.Match[str]]:
    """Return every inline metadata block in `workflow_content` whose name is `block_name`."""
    return [match for match in _METADATA_BLOCK_PATTERN.finditer(workflow_content) if match.group("type") == block_name]


def strip_metadata_comment_prefixes(metadata_block: re.Match[str]) -> str:
    """Recover the raw TOML from a metadata block by dropping the comment marker off each line."""
    return "".join(
        line[2:] if line.startswith("# ") else line[1:]
        for line in metadata_block.group("content").splitlines(keepends=True)
    )


def read_workflow_metadata(workflow_file_path: Path) -> WorkflowMetadata:
    """Read the metadata header out of a workflow file.

    Pure file parsing: no registry lookups, no engine requests, and no waiting on library
    registration. Callers that run while libraries are still loading (the library loader itself,
    for instance) can use this directly; `LoadWorkflowMetadata`'s handler wraps it with the file
    checks, dependency checks, and per-stage problem reporting the editor needs.

    Raises:
        WorkflowMetadataFileError: The file could not be read.
        WorkflowMetadataSectionCountError: The file does not carry exactly one metadata header.
        WorkflowMetadataTomlError: The header is not valid TOML.
        WorkflowMetadataMissingTableError: The header has no `[tool.griptape-nodes]` table.
        WorkflowMetadataSchemaError: That table does not match the expected schema.
    """
    try:
        workflow_content = workflow_file_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as err:
        msg = (
            f"Attempted to read workflow metadata from '{workflow_file_path}'. "
            f"Failed because the file could not be read: {err}"
        )
        raise WorkflowMetadataFileError(msg) from err

    matches = find_metadata_blocks(workflow_content, WORKFLOW_METADATA_BLOCK_NAME)
    if len(matches) != 1:
        msg = (
            f"Attempted to read workflow metadata from '{workflow_file_path}'. Failed because the file has "
            f"{len(matches)} '{WORKFLOW_METADATA_BLOCK_NAME}' metadata sections, and exactly 1 is required. "
            "Open the workflow in the editor and save it to regenerate the header."
        )
        raise WorkflowMetadataSectionCountError(msg, section_name=WORKFLOW_METADATA_BLOCK_NAME, count=len(matches))

    metadata_toml = strip_metadata_comment_prefixes(matches[0])

    # tomllib, not tomlkit: this is a read-only path, and tomlkit builds a formatting-preserving
    # document model that costs ~20x more per header. Only the save path needs tomlkit, to keep the
    # formatting of headers it rewrites.
    try:
        toml_document = tomllib.loads(metadata_toml)
    except tomllib.TOMLDecodeError as err:
        msg = (
            f"Attempted to read workflow metadata from '{workflow_file_path}'. "
            f"Failed because the header is not valid TOML: {err}"
        )
        raise WorkflowMetadataTomlError(msg, error_message=str(err)) from err

    try:
        tool_section = toml_document[_TOOL_TABLE][_GRIPTAPE_NODES_TABLE]
    except (KeyError, TypeError) as err:
        msg = (
            f"Attempted to read workflow metadata from '{workflow_file_path}'. Failed because the header has no "
            f"'{METADATA_TABLE_PATH}' table."
        )
        raise WorkflowMetadataMissingTableError(msg, section_path=METADATA_TABLE_PATH) from err

    try:
        return WorkflowMetadata.model_validate(tool_section)
    except ValidationError as err:
        msg = (
            f"Attempted to read workflow metadata from '{workflow_file_path}'. Failed because the "
            f"'{METADATA_TABLE_PATH}' table does not match the expected schema: {err}"
        )
        raise WorkflowMetadataSchemaError(msg, section_path=METADATA_TABLE_PATH, error_message=str(err)) from err


class WorkflowRegistryError(ValueError):
    """A registry change was refused because it would break one of the registry's invariants."""


class WorkflowPathTakenError(WorkflowRegistryError):
    """Another entry already holds the path, or the name derived from it."""

    def __init__(self, message: str, *, existing: Workflow) -> None:
        super().__init__(message)
        self.existing = existing


class DuplicateWorkflowIdError(WorkflowRegistryError):
    """A workflow id given to register a workflow under is already registered."""

    def __init__(self, message: str, *, existing: Workflow) -> None:
        super().__init__(message)
        self.existing = existing


class _IndexKeys(NamedTuple):
    """Where an entry sits in the path and name indexes."""

    file_path: str | None
    name: str
    path_keys: list[str]
    alias_names: list[str]


class _WorkflowRegistry:
    """Workflows known to one engine, keyed by workflow id.

    Owned by `Engine` and reached through `engine.workflow_registry`. Node libraries, which have
    no engine reference, use the `WorkflowRegistry` classmethods.

    The registry mints a workflow's id when the workflow is registered, and the id lasts while the
    engine runs. It is never written to the workflow's file, so another engine process gives the
    same file an id of its own. A rescan keeps each file's id because it refreshes entries in
    place: `refresh_file` keeps the entry a file already has, and `remove_saved_workflows_except`
    drops only the entries the rescan did not find.

    Names and paths are indexes into the id-keyed entries, kept here and nowhere else.
    `set_file_path` is the only way an entry's path changes, so the indexes cannot drift from
    the entries. Lookups never register anything.
    """

    # Prefix of the legacy name of an unsaved (in-memory) workflow: "unsaved:<workflow id>".
    # No path-derived name starts with it: derive_registry_key strips the extension and
    # normalizes separators, but never emits an "unsaved:" literal.
    UNSAVED_KEY_PREFIX: ClassVar[str] = "unsaved:"

    def __init__(self, config_manager: ConfigManager) -> None:
        self._config_manager = config_manager
        self._by_id: dict[str, Workflow] = {}
        # Canonical absolute path -> id. Holds every path an entry is known by: its path as named
        # (links kept), the same path with links followed, and the paths of its aliases.
        self._id_by_path: dict[str, str] = {}
        # Legacy name -> id, "unsaved:<id>" included.
        self._id_by_name: dict[str, str] = {}

    def get(self, workflow_id: str) -> Workflow:
        """Return the entry with this id.

        Raises:
            KeyError: No workflow has that id.
        """
        workflow = self._by_id.get(workflow_id)
        if workflow is None:
            msg = f"Attempted to get workflow '{workflow_id}'. Failed because no workflow has that id."
            raise KeyError(msg)
        return workflow

    def find_by_id(self, workflow_id: str) -> Workflow | None:
        """Return the entry with this id, or None."""
        return self._by_id.get(workflow_id)

    def find_by_name(self, name: str) -> Workflow | None:
        """Return the entry with this legacy name ("unsaved:<id>" included), or None."""
        workflow_id = self._id_by_name.get(name)
        if workflow_id is None:
            return None
        return self._by_id[workflow_id]

    def unsaved_name_workflow_id(self, name: str) -> str | None:
        """The workflow id in an "unsaved:<id>" name. None for any other name, or an id not in its hex form."""
        if not name.startswith(self.UNSAVED_KEY_PREFIX):
            return None
        candidate = name.removeprefix(self.UNSAVED_KEY_PREFIX)
        try:
            workflow_id = uuid.UUID(hex=candidate).hex
        except ValueError:
            return None
        if workflow_id != candidate:
            return None
        return workflow_id

    def find_by_path(self, path: str | Path) -> Workflow | None:
        """Return the entry for the file at `path`, or None. Accepts any path the entry is known by."""
        for path_key in self._path_keys(self.canonical_path(path)):
            workflow_id = self._id_by_path.get(path_key)
            if workflow_id is not None:
                return self._by_id[workflow_id]
        return None

    def register_file(self, path: str | Path, metadata: WorkflowMetadata) -> Workflow:
        """Register the workflow file at `path` (workspace-relative or absolute) under a new id.

        A path the registry already holds is refused; a rescan uses `refresh_file`, which keeps
        it. A link to a file the registry already holds under another path becomes an alias of
        that entry, which is returned.

        Raises:
            ValueError: The file is not on disk.
            WorkflowPathTakenError: Another entry holds the path, or the name derived from it.
        """
        absolute_path = self.canonical_path(path)
        if not absolute_path.is_file():
            msg = f"File path '{absolute_path}' does not exist."
            raise ValueError(msg)

        holder = self.find_by_path(absolute_path)
        if holder is not None and absolute_path in holder.known_paths:
            msg = f"Attempted to register the workflow at '{absolute_path}'. Failed because '{holder.name}' is already registered there."
            raise WorkflowPathTakenError(msg, existing=holder)

        # The registry knows the file under another path, through a link: one workflow known by
        # one more path.
        if holder is not None:
            self._add_alias(holder, absolute_path)
            return holder

        workflow = Workflow(
            registry=self, workflow_id=new_workflow_id(), metadata=metadata, absolute_path=absolute_path
        )
        self._add(workflow)
        return workflow

    def refresh_file(self, path: str | Path, metadata: WorkflowMetadata) -> Workflow:
        """Register the workflow file at `path` as a rescan finds it, keeping the entry it already has.

        A file the registry does not hold is registered with a new id. A file it holds, under
        this path or through a link, keeps its entry and its id, and takes `metadata`, freshly
        read from its header.

        Raises:
            ValueError: The file is not on disk.
            WorkflowPathTakenError: The file is new, and another entry holds the name derived from it.
        """
        absolute_path = self.canonical_path(path)
        holder = self.find_by_path(absolute_path)
        if holder is None:
            return self.register_file(absolute_path, metadata)
        if not absolute_path.is_file():
            msg = f"File path '{absolute_path}' does not exist."
            raise ValueError(msg)

        holder.metadata = metadata
        return holder

    def register_unsaved(self, display_name: str, *, workflow_id: str | None = None) -> Workflow:
        """Register a new unsaved (in-memory) workflow, named "unsaved:<id>".

        `workflow_id` is minted unless given.

        Raises:
            DuplicateWorkflowIdError: `workflow_id` is already registered.
        """
        if workflow_id is None:
            workflow_id = new_workflow_id()
        existing = self._by_id.get(workflow_id)
        if existing is not None:
            msg = f"Attempted to register an unsaved workflow. Failed because '{existing.name}' already has the workflow id '{workflow_id}'."
            raise DuplicateWorkflowIdError(msg, existing=existing)

        metadata = WorkflowMetadata(
            name=display_name,
            schema_version=WorkflowMetadata.LATEST_SCHEMA_VERSION,
            engine_version_created_with="",
            node_libraries_referenced=[],
            creation_date=datetime.now(UTC),
        )
        workflow = Workflow(registry=self, workflow_id=workflow_id, metadata=metadata, absolute_path=None)
        self._add(workflow)
        return workflow

    def set_file_path(self, workflow: Workflow, new_path: str | Path) -> None:
        """Move `workflow` to the file at `new_path`. The only way an entry's path changes.

        Its id stays the same; its legacy name follows the path. An unsaved workflow becomes a
        saved one. Aliases of the old path are dropped, since they named the old file.

        Raises:
            KeyError: `workflow` is not this registry's entry.
            ValueError: The file is not on disk.
            WorkflowPathTakenError: Another entry holds the path, or the name derived from it.
        """
        if self._by_id.get(workflow.workflow_id) is not workflow:
            msg = f"Attempted to move workflow '{workflow.name}'. Failed because it is not registered."
            raise KeyError(msg)
        absolute_path = self.canonical_path(new_path)
        if not absolute_path.is_file():
            msg = f"File path '{absolute_path}' does not exist."
            raise ValueError(msg)

        holder = self.find_by_path(absolute_path)
        if holder is None:
            holder = self.find_by_name(derive_registry_key(self._registry_form(absolute_path)))
        if holder is not None and holder is not workflow:
            msg = f"Attempted to move workflow '{workflow.name}' to '{absolute_path}'. Failed because '{holder.name}' is registered there."
            raise WorkflowPathTakenError(msg, existing=holder)

        self._remove_from_indexes(workflow)
        workflow._absolute_path = absolute_path
        workflow._alias_paths = []
        self._add_to_indexes(workflow)

    def remove(self, workflow_id: str) -> Workflow:
        """Remove the entry with this id and return it. A file registered at its path later gets an id of its own.

        Raises:
            KeyError: No workflow has that id.
        """
        workflow = self.get(workflow_id)
        self._remove_from_indexes(workflow)
        del self._by_id[workflow_id]
        return workflow

    def workflow_ids(self) -> set[str]:
        """The ids of every registered workflow, saved or not."""
        return set(self._by_id)

    def reindex_workflows(self) -> None:
        """Derive every entry's path keys and legacy name again, ahead of a rescan.

        A legacy name is derived against the workspace, which may have changed (a project
        switch), and a path key follows links, which may point elsewhere now. Two entries can
        only collide when a link was retargeted outside the engine, giving one entry another
        entry's file. Unsaved entries are indexed first, then saved ones whose path keys did not
        change, then the rest. A saved entry that collides is removed; the rescan registers its
        file again if it is still a workflow of its own.
        """
        previous_path_keys: dict[str, set[str]] = {}
        for path_key, workflow_id in self._id_by_path.items():
            previous_path_keys.setdefault(workflow_id, set()).add(path_key)
        self._id_by_path.clear()
        self._id_by_name.clear()

        unsaved_workflows = []
        unchanged_workflows = []
        changed_workflows = []
        for workflow in self._by_id.values():
            if not workflow.is_saved:
                unsaved_workflows.append(workflow)
            elif set(self._index_keys(workflow).path_keys) == previous_path_keys.get(workflow.workflow_id):
                unchanged_workflows.append(workflow)
            else:
                changed_workflows.append(workflow)

        # Unsaved entries hold no path and are named by their id, so they never collide.
        for workflow in unsaved_workflows:
            self._add_to_indexes(workflow)
        for workflow in [*unchanged_workflows, *changed_workflows]:
            try:
                self._add_to_indexes(workflow)
            except WorkflowPathTakenError as err:
                logger.debug("Dropped workflow '%s' from the registry ahead of a rescan: %s", workflow.name, err)
                del self._by_id[workflow.workflow_id]

    def remove_saved_workflows_except(self, kept_workflow_ids: set[str]) -> None:
        """Remove every saved entry whose id is not in `kept_workflow_ids`.

        A rescan calls this with the ids of the entries it found, so a file deleted, moved, or
        made unreadable outside the engine drops out. Unsaved entries stay.
        """
        for workflow in list(self._by_id.values()):
            if not workflow.is_saved:
                continue
            if workflow.workflow_id in kept_workflow_ids:
                continue
            self._remove_from_indexes(workflow)
            del self._by_id[workflow.workflow_id]

    # Create a new workflow with everything we'd need.
    def generate_new_workflow(
        self,
        registry_key: str,
        metadata: WorkflowMetadata,
        file_path: str | None = None,
    ) -> Workflow:
        """Register a workflow with the given metadata, refusing a `registry_key` already registered.

        With `file_path`, registers that file, and `registry_key` must be the name its path gives
        it. A file the registry holds under another path, through a link, takes `metadata` and
        answers to `registry_key` from then on. Without `file_path`, registers the unsaved
        workflow named `registry_key`, which must be "unsaved:<id>".

        Raises:
            KeyError: A workflow is already registered under `registry_key`.
            ValueError: The file is not on disk, another entry holds its path, `registry_key` is
                not the name the path gives it, or `registry_key` is not an unsaved name.
        """
        if file_path is not None:
            path_name = derive_registry_key(self._registry_form(self.canonical_path(file_path)))
            if path_name != registry_key:
                msg = f"Attempted to register workflow '{registry_key}' at '{file_path}'. Failed because a workflow is named after its file, and that file names it '{path_name}'."
                raise WorkflowRegistryError(msg)
        if self.find_by_name(registry_key) is not None:
            msg = f"Workflow with registry key '{registry_key}' already registered."
            raise KeyError(msg)

        if file_path is not None:
            workflow = self.register_file(file_path, metadata)
        else:
            workflow = self.ensure_unsaved(registry_key, metadata.name)
        workflow.metadata = metadata
        return workflow

    def ensure_unsaved(self, key: str, display_name: str) -> Workflow:
        """Idempotently register an unsaved workflow under `key`, which must be "unsaved:<id>".

        Returns the existing entry if `key` is already registered. `display_name` is only
        consulted on first registration.

        Raises:
            ValueError: `key` is not "unsaved:" followed by a workflow id.
        """
        existing = self.find_by_name(key)
        if existing is not None:
            return existing
        workflow_id = self.unsaved_name_workflow_id(key)
        if workflow_id is None:
            msg = f"Unsaved registry key '{key}' must be '{self.UNSAVED_KEY_PREFIX}' followed by a workflow id (32 hex digits)."
            raise ValueError(msg)
        return self.register_unsaved(display_name, workflow_id=workflow_id)

    def get_workflow_by_name(self, name: str) -> Workflow:
        workflow = self.find_by_name(name)
        if workflow is None:
            msg = f"Failed to get Workflow. Workflow with name '{name}' has not been registered."
            raise KeyError(msg)
        return workflow

    def has_workflow_with_name(self, name: str) -> bool:
        return self.find_by_name(name) is not None

    def list_workflows(self) -> dict[str, dict]:
        # Resolve paths once here and pass them down so get_workflow_metadata() skips
        # the is_synced property, which resolves them per workflow.
        synced_path = self.get_synced_workflows_path()
        workspace_path = self._config_manager.workspace_path

        return {
            workflow.name: workflow.get_workflow_metadata(synced_path=synced_path, workspace_path=workspace_path)
            for workflow in self._by_id.values()
        }

    def get_complete_file_path(self, relative_file_path: str) -> str:
        workspace_path = self._config_manager.workspace_path
        resolved_path = resolve_workspace_path(Path(relative_file_path), workspace_path)
        return str(resolved_path)

    def get_synced_workflows_path(self) -> Path:
        synced_directory = self._config_manager.get_config_value("synced_workflows_directory")
        return self._config_manager.get_full_path(synced_directory)

    def delete_workflow_by_name(self, name: str) -> Workflow:
        workflow = self.find_by_name(name)
        if workflow is None:
            msg = f"Failed to delete Workflow. Workflow with name '{name}' has not been registered."
            raise KeyError(msg)
        return self.remove(workflow.workflow_id)

    def clear_user_workflows(self) -> None:
        """Remove all non-library workflows from the registry.

        Library-provided workflows (is_griptape_provided=True) are preserved.
        """
        for workflow in list(self._by_id.values()):
            if not workflow.metadata.is_griptape_provided:
                self.remove(workflow.workflow_id)

    def rekey_workflow(self, old_key: str, new_key: str) -> None:
        """Move the workflow named `old_key` to the file `new_key` names, keeping its file extension.

        A workflow's name follows its file, so the file must already be at its new path.

        Raises:
            KeyError: No workflow has the name `old_key`.
            ValueError: The workflow is unsaved, or no file is at the new path.
        """
        workflow = self.get_workflow_by_name(old_key)
        if workflow.absolute_path is None:
            msg = f"Failed to rekey Workflow. Workflow '{old_key}' is unsaved, so it has no file to name it by."
            raise ValueError(msg)
        self.set_file_path(workflow, f"{new_key}{workflow.absolute_path.suffix}")

    def get_branches_of_workflow(self, workflow_name: str) -> list[str]:
        """Get all workflows that are branches of the specified workflow."""
        return [workflow.name for workflow in self._by_id.values() if workflow.metadata.branched_from == workflow_name]

    def _add(self, workflow: Workflow) -> None:
        self._add_to_indexes(workflow)
        self._by_id[workflow.workflow_id] = workflow

    def _add_alias(self, workflow: Workflow, alias_path: Path) -> None:
        self._remove_from_indexes(workflow)
        workflow._alias_paths.append(alias_path)
        self._add_to_indexes(workflow)

    def _add_to_indexes(self, workflow: Workflow) -> None:
        """Index `workflow` by its paths and names, refusing any another entry holds."""
        index_keys = self._index_keys(workflow)
        for path_key in index_keys.path_keys:
            holder_id = self._id_by_path.get(path_key)
            if holder_id is not None and holder_id != workflow.workflow_id:
                holder = self._by_id[holder_id]
                msg = f"Attempted to register workflow '{index_keys.name}'. Failed because '{holder.name}' is registered at '{path_key}'."
                raise WorkflowPathTakenError(msg, existing=holder)
        for name in [index_keys.name, *index_keys.alias_names]:
            holder_id = self._id_by_name.get(name)
            if holder_id is not None and holder_id != workflow.workflow_id:
                holder = self._by_id[holder_id]
                msg = f"Attempted to register workflow '{name}'. Failed because another workflow is already registered under that name."
                raise WorkflowPathTakenError(msg, existing=holder)

        workflow._file_path = index_keys.file_path
        workflow._name = index_keys.name
        for path_key in index_keys.path_keys:
            self._id_by_path[path_key] = workflow.workflow_id
        for name in [index_keys.name, *index_keys.alias_names]:
            self._id_by_name[name] = workflow.workflow_id

    def _remove_from_indexes(self, workflow: Workflow) -> None:
        for path_key in [key for key, holder_id in self._id_by_path.items() if holder_id == workflow.workflow_id]:
            del self._id_by_path[path_key]
        for name in [key for key, holder_id in self._id_by_name.items() if holder_id == workflow.workflow_id]:
            del self._id_by_name[name]

    def _index_keys(self, workflow: Workflow) -> _IndexKeys:
        if workflow.absolute_path is None:
            return _IndexKeys(
                file_path=None,
                name=f"{self.UNSAVED_KEY_PREFIX}{workflow.workflow_id}",
                path_keys=[],
                alias_names=[],
            )

        file_path = self._registry_form(workflow.absolute_path)
        path_keys = self._path_keys(workflow.absolute_path)
        alias_names = []
        for alias_path in workflow._alias_paths:
            path_keys.extend(self._path_keys(alias_path))
            alias_names.append(derive_registry_key(self._registry_form(alias_path)))
        return _IndexKeys(
            file_path=file_path,
            name=derive_registry_key(file_path),
            path_keys=path_keys,
            alias_names=alias_names,
        )

    def _registry_form(self, absolute_path: Path) -> str:
        """The form `Workflow.file_path` reports: workspace-relative inside the workspace, else absolute."""
        relative_path = relative_to_keeping_or_following_links(absolute_path, self._config_manager.workspace_path)
        if relative_path is None:
            return str(absolute_path)
        return str(relative_path)

    def canonical_path(self, path: str | Path) -> Path:
        """The absolute form the registry knows a workflow file by: workspace-anchored, links kept as named."""
        return canonicalize_for_identity_preserving_symlinks(path, base=self._config_manager.workspace_path)

    @staticmethod
    def _path_keys(absolute_path: Path) -> list[str]:
        """The keys a path is indexed under: as named, and with links followed."""
        kept = str(absolute_path)
        followed = str(canonicalize_for_identity(absolute_path))
        if followed == kept:
            return [kept]
        return [kept, followed]


# The engine version that removes the deprecated `WorkflowRegistry` classmethods.
_DEPRECATED_METHODS_REMOVAL_VERSION = "0.110.0"


def _log_deprecated(method_name: str, replacement: str) -> None:
    logger.warning(
        "WorkflowRegistry.%s() is deprecated and will be removed in version %s. Use %s instead.",
        method_name,
        _DEPRECATED_METHODS_REMOVAL_VERSION,
        replacement,
    )


class WorkflowRegistry:
    """The current engine's workflow registry, for node libraries.

    Kept for node libraries that call these classmethods. Each one forwards to
    `current_engine().workflow_registry`. Engine-internal code uses `engine.workflow_registry`.
    """

    UNSAVED_KEY_PREFIX: ClassVar[str] = _WorkflowRegistry.UNSAVED_KEY_PREFIX

    @classmethod
    def generate_new_workflow(
        cls,
        registry_key: str,
        metadata: WorkflowMetadata,
        file_path: str | None = None,
    ) -> Workflow:
        _log_deprecated("generate_new_workflow", "RegisterWorkflowRequest")
        return _current_registry().generate_new_workflow(
            registry_key=registry_key, metadata=metadata, file_path=file_path
        )

    @classmethod
    def ensure_unsaved(cls, key: str, display_name: str) -> Workflow:
        _log_deprecated("ensure_unsaved", "SetWorkflowContextRequest")
        return _current_registry().ensure_unsaved(key=key, display_name=display_name)

    @classmethod
    def get_workflow_by_name(cls, name: str) -> Workflow:
        return _current_registry().get_workflow_by_name(name)

    @classmethod
    def has_workflow_with_name(cls, name: str) -> bool:
        return _current_registry().has_workflow_with_name(name)

    @classmethod
    def list_workflows(cls) -> dict[str, dict]:
        return _current_registry().list_workflows()

    @classmethod
    def get_complete_file_path(cls, relative_file_path: str) -> str:
        return _current_registry().get_complete_file_path(relative_file_path)

    @classmethod
    def delete_workflow_by_name(cls, name: str) -> Workflow:
        _log_deprecated("delete_workflow_by_name", "DeleteWorkflowRequest")
        return _current_registry().delete_workflow_by_name(name)

    @classmethod
    def clear_user_workflows(cls) -> None:
        _log_deprecated("clear_user_workflows", "RefreshWorkflowRegistryRequest")
        # Test suites call this right after reset_root_engine(). Building an engine here would boot
        # it before their config patches apply, and a fresh engine has nothing to clear anyway.
        # Deferred import: see _current_registry.
        from griptape_nodes.retained_mode.engine import has_current_engine

        if not has_current_engine():
            return
        _current_registry().clear_user_workflows()

    @classmethod
    def rekey_workflow(cls, old_key: str, new_key: str) -> None:
        _log_deprecated("rekey_workflow", "MoveWorkflowRequest or RenameWorkflowRequest")
        _current_registry().rekey_workflow(old_key, new_key)

    @classmethod
    def get_branches_of_workflow(cls, workflow_name: str) -> list[str]:
        return _current_registry().get_branches_of_workflow(workflow_name)


def _current_registry() -> _WorkflowRegistry:
    # Deferred import: the engine module imports the event payloads, which import this module.
    from griptape_nodes.retained_mode.engine import current_engine

    return current_engine().workflow_registry


class Workflow:
    """A workflow card to be ran.

    A workflow has two possible states:
    - **Saved**: backed by a file on disk. `absolute_path` is the file; `file_path` is the same
      file in the registry's form (workspace-relative inside the workspace, else absolute).
    - **Unsaved**: in-memory only. `file_path` and `absolute_path` are None. Becomes saved when
      the registry's `set_file_path` gives it a file.

    `workflow_id` never changes while the engine runs. Only the registry changes the path, so its
    indexes stay right.
    """

    metadata: WorkflowMetadata

    def __init__(
        self,
        registry: _WorkflowRegistry,
        workflow_id: str,
        metadata: WorkflowMetadata,
        absolute_path: Path | None,
    ) -> None:
        if not isinstance(registry, _WorkflowRegistry):
            msg = "Workflows can only be created through a workflow registry"
            raise TypeError(msg)

        self._registry = registry
        self._workflow_id = workflow_id
        self.metadata = metadata
        self._absolute_path = absolute_path
        # Other paths to the same file (os.path.samefile), so `find_by_path` answers for each.
        self._alias_paths: list[Path] = []
        # Path in the registry's form, and the legacy name: the registry sets both when it indexes the entry.
        self._file_path: str | None = None
        self._name = ""

    @property
    def workflow_id(self) -> str:
        return self._workflow_id

    @property
    def name(self) -> str:
        """The legacy registry name: the file path without its extension, or "unsaved:<id>"."""
        return self._name

    @property
    def file_path(self) -> str | None:
        """The file, workspace-relative inside the workspace and absolute outside it. None while unsaved."""
        return self._file_path

    @property
    def absolute_path(self) -> Path | None:
        """The file's absolute path, with links kept as named. None while unsaved."""
        return self._absolute_path

    @property
    def known_paths(self) -> list[Path]:
        """Every path this workflow's file is registered by: its own and its aliases'."""
        if self._absolute_path is None:
            return []
        return [self._absolute_path, *self._alias_paths]

    @property
    def is_saved(self) -> bool:
        """True if this workflow is backed by a file on disk."""
        return self._absolute_path is not None

    @property
    def is_synced(self) -> bool:
        """Check if this workflow is in the synced workflows directory.

        Unsaved workflows are never synced (there is no file to place anywhere).
        """
        if self.file_path is None:
            return False

        synced_path = self._registry.get_synced_workflows_path()
        complete_file_path = self._registry.get_complete_file_path(self.file_path)
        return Path(complete_file_path).is_relative_to(synced_path)

    def get_workflow_metadata(self, synced_path: Path | None = None, workspace_path: Path | None = None) -> dict:
        # Convert from the Pydantic schema.
        ret_val = {**self.metadata.model_dump()}

        # The schema doesn't have the file path in it, because it is baked into the file itself.
        # Customers of this function need that, so let's stuff it in.
        ret_val["workflow_id"] = self.workflow_id
        ret_val["file_path"] = self.file_path
        ret_val["is_saved"] = self.is_saved

        if synced_path is not None and workspace_path is not None and self.file_path is not None:
            # Pre-computed paths supplied by list_workflows() so they are resolved once, not per
            # workflow. Falls back to the property for standalone callers.
            complete_file_path = resolve_workspace_path(Path(self.file_path), workspace_path)
            ret_val["is_synced"] = complete_file_path.is_relative_to(synced_path)
        else:
            ret_val["is_synced"] = self.is_synced

        return ret_val
