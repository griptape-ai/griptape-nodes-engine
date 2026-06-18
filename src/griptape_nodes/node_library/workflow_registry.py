from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar, NamedTuple

from pydantic import BaseModel, Field, field_serializer, field_validator

from griptape_nodes.files.path_utils import resolve_workspace_path
from griptape_nodes.node_library.library_registry import (
    LibraryNameAndVersion,
)
from griptape_nodes.utils.metaclasses import SingletonMeta

logger = logging.getLogger("griptape_nodes")


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
    LATEST_SCHEMA_VERSION: ClassVar[str] = "0.19.2"

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
    is_private: bool | None = False
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


class WorkflowRegistry(metaclass=SingletonMeta):
    class _RegistryKey:
        """Private class for workflow construction."""

    # Prefix used for synthetic registry keys for unsaved (in-memory) workflows.
    # These keys collide with no possible file-path-derived key because derive_registry_key
    # strips the extension and normalizes separators, but never emits a "unsaved:" literal.
    UNSAVED_KEY_PREFIX: ClassVar[str] = "unsaved:"

    _workflows: ClassVar[dict[str, Workflow]] = {}
    _registry_key: _RegistryKey = _RegistryKey()

    # Create a new workflow with everything we'd need.
    @classmethod
    def generate_new_workflow(
        cls,
        registry_key: str,
        metadata: WorkflowMetadata,
        file_path: str | None = None,
    ) -> Workflow:
        """Register a workflow under `registry_key` with the given metadata.

        The registry stores the workflow by the caller-supplied `registry_key`; the key
        just happens to be file-path-derived for saved workflows, but the registry does
        not require that. Callers deriving a key from a file path should pass it through
        `derive_registry_key` first.

        `file_path` is optional: provide it for saved workflows (backed by a file on
        disk; existence is verified at construction time); omit it for unsaved in-memory
        entries. Unsaved keys must start with `UNSAVED_KEY_PREFIX`.
        """
        instance = cls()
        if registry_key in instance._workflows:
            msg = f"Workflow with registry key '{registry_key}' already registered."
            raise KeyError(msg)
        is_unsaved_key = registry_key.startswith(cls.UNSAVED_KEY_PREFIX)
        if is_unsaved_key and file_path is not None:
            msg = f"Unsaved registry key '{registry_key}' cannot be paired with a file_path."
            raise ValueError(msg)
        if not is_unsaved_key and file_path is None:
            msg = f"Saved registry key '{registry_key}' requires a file_path."
            raise ValueError(msg)
        if file_path is None:
            workflow = Workflow(registry_key=instance._registry_key, metadata=metadata, file_path=None)
        else:
            workflow = Workflow.from_disk(registry_key=instance._registry_key, file_path=file_path, metadata=metadata)
        instance._workflows[registry_key] = workflow
        return workflow

    @classmethod
    def ensure_unsaved(cls, key: str, display_name: str) -> Workflow:
        """Idempotently register an unsaved workflow under `key`.

        Returns the existing entry if `key` is already registered; otherwise constructs
        a default WorkflowMetadata and registers a new in-memory workflow. `key` must
        start with `UNSAVED_KEY_PREFIX`; `display_name` is only consulted on first
        registration.
        """
        if not key.startswith(cls.UNSAVED_KEY_PREFIX):
            msg = f"Unsaved registry key '{key}' must start with '{cls.UNSAVED_KEY_PREFIX}'."
            raise ValueError(msg)
        if cls.has_workflow_with_name(key):
            return cls.get_workflow_by_name(key)
        metadata = WorkflowMetadata(
            name=display_name,
            schema_version=WorkflowMetadata.LATEST_SCHEMA_VERSION,
            engine_version_created_with="",
            node_libraries_referenced=[],
            creation_date=datetime.now(UTC),
        )
        return cls.generate_new_workflow(registry_key=key, metadata=metadata, file_path=None)

    @classmethod
    def get_workflow_by_name(cls, name: str) -> Workflow:
        instance = cls()
        if name not in instance._workflows:
            msg = f"Failed to get Workflow. Workflow with name '{name}' has not been registered."
            raise KeyError(msg)
        return instance._workflows[name]

    @classmethod
    def has_workflow_with_name(cls, name: str) -> bool:
        instance = cls()
        return name in instance._workflows

    @classmethod
    def list_workflows(cls) -> dict[str, dict]:
        instance = cls()
        return {key: instance._workflows[key].get_workflow_metadata() for key in instance._workflows}

    @classmethod
    def get_complete_file_path(cls, relative_file_path: str) -> str:
        from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

        # Resolve path using utility function
        config_mgr = GriptapeNodes.ConfigManager()
        workspace_path = config_mgr.workspace_path
        resolved_path = resolve_workspace_path(Path(relative_file_path), workspace_path)
        return str(resolved_path)

    @classmethod
    def delete_workflow_by_name(cls, name: str) -> Workflow:
        instance = cls()
        if name not in instance._workflows:
            msg = f"Failed to delete Workflow. Workflow with name '{name}' has not been registered."
            raise KeyError(msg)
        return instance._workflows.pop(name)

    @classmethod
    def clear_user_workflows(cls) -> None:
        """Remove all non-library workflows from the registry.

        Library-provided workflows (is_griptape_provided=True) are preserved.
        Called before re-registering workflows so that a workspace change takes effect cleanly.
        """
        instance = cls()
        keys_to_remove = [
            key for key, workflow in instance._workflows.items() if not workflow.metadata.is_griptape_provided
        ]
        for key in keys_to_remove:
            del instance._workflows[key]

    @classmethod
    def rekey_workflow(cls, old_key: str, new_key: str) -> None:
        """Re-key a workflow in the registry from old_key to new_key."""
        instance = cls()
        if old_key not in instance._workflows:
            msg = f"Failed to rekey Workflow. Workflow with key '{old_key}' has not been registered."
            raise KeyError(msg)
        workflow = instance._workflows.pop(old_key)
        instance._workflows[new_key] = workflow

    @classmethod
    def get_branches_of_workflow(cls, workflow_name: str) -> list[str]:
        """Get all workflows that are branches of the specified workflow."""
        instance = cls()
        branches = []
        for name, workflow in instance._workflows.items():
            if workflow.metadata.branched_from == workflow_name:
                branches.append(name)
        return branches


class Workflow:
    """A workflow card to be ran.

    A workflow has two possible states:
    - **Saved**: backed by a file on disk. `file_path` is a string (relative or absolute).
      Created via `Workflow.from_disk`.
    - **Unsaved**: in-memory only. `file_path is None`. Created via
      `WorkflowRegistry.generate_new_workflow` with `file_path=None`. Transitions to
      saved when `SaveWorkflowRequest` is handled for this workflow's registry key.
    """

    metadata: WorkflowMetadata
    file_path: str | None

    def __init__(
        self,
        registry_key: WorkflowRegistry._RegistryKey,
        metadata: WorkflowMetadata,
        file_path: str | None,
    ) -> None:
        if not isinstance(registry_key, WorkflowRegistry._RegistryKey):
            msg = "Workflows can only be created through WorkflowRegistry"
            raise TypeError(msg)

        self.metadata = metadata
        self.file_path = file_path

    @classmethod
    def from_disk(
        cls,
        registry_key: WorkflowRegistry._RegistryKey,
        metadata: WorkflowMetadata,
        file_path: str,
    ) -> Workflow:
        """Construct a Workflow backed by an existing file on disk.

        Verifies the file exists at construction time (preserving the pre-existing invariant
        that saved Workflow entries always point at a real file). Unsaved entries bypass
        this check by constructing the Workflow directly with `file_path=None`.
        """
        complete_path = WorkflowRegistry.get_complete_file_path(relative_file_path=file_path)
        if not Path(complete_path).is_file():
            msg = f"File path '{complete_path}' does not exist."
            raise ValueError(msg)
        return cls(registry_key=registry_key, metadata=metadata, file_path=file_path)

    @property
    def is_saved(self) -> bool:
        """True if this workflow is backed by a file on disk."""
        return self.file_path is not None

    @property
    def is_synced(self) -> bool:
        """Check if this workflow is in the synced workflows directory.

        Unsaved workflows are never synced (there is no file to place anywhere).
        """
        if self.file_path is None:
            return False

        from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

        config_mgr = GriptapeNodes.ConfigManager()
        synced_directory = config_mgr.get_config_value("synced_workflows_directory")

        # Get the full path to the synced workflows directory
        synced_path = config_mgr.get_full_path(synced_directory)

        # Get the complete file path for this workflow
        complete_file_path = WorkflowRegistry.get_complete_file_path(self.file_path)

        # Check if the workflow file is within the synced directory
        return Path(complete_file_path).is_relative_to(synced_path)

    def get_workflow_metadata(self) -> dict:
        # Convert from the Pydantic schema.
        ret_val = {**self.metadata.model_dump()}

        # The schema doesn't have the file path in it, because it is baked into the file itself.
        # Customers of this function need that, so let's stuff it in.
        ret_val["file_path"] = self.file_path
        ret_val["is_synced"] = self.is_synced
        ret_val["is_saved"] = self.is_saved
        return ret_val
