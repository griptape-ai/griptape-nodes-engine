from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field

from griptape_nodes.retained_mode.events.base_events import (
    RequestPayload,
    ResultPayloadFailure,
    ResultPayloadSuccess,
    WorkflowNotAlteredMixin,
)
from griptape_nodes.retained_mode.events.payload_registry import PayloadRegistry

# Config layers in ConfigManager.load_configs' merge order, lowest to highest priority.
# "default" is the Settings model's own defaults, "env" is GTN_CONFIG_ variables.
ConfigLayerName = Literal["default", "user", "project", "workspace", "runtime", "env"]

# Why a write that reached disk is not the value now in effect:
#   shadowed -- a higher-priority layer also defines the key and keeps winning the merge.
#   pinned   -- the open project pins `workspace_directory`; the write applies on the next open.
#   rejected -- the merged result failed Settings validation, so defaults are in effect instead.
ConfigWriteUnappliedReason = Literal["shadowed", "pinned", "rejected"]


class ConfigValueSource(BaseModel):
    """Which config layer currently supplies a value's effective content.

    Only "default" and "user" are writable by `SetConfigValueRequest` /
    `SetConfigCategoryRequest`. Any other layer re-supplies its own value on every
    `load_configs()` remerge, so a settings-editor write cannot change it. See
    `ConfigManager.shadowed_by`.
    """

    layer: ConfigLayerName
    path: str | None = Field(
        default=None,
        description=(
            "Absolute path to the config file backing this layer. None for 'default', "
            "'env' (see env_var instead), 'runtime' (pinned in memory by a project "
            "activation), and for 'project'/'workspace' when no project is active."
        ),
    )
    env_var: str | None = Field(
        default=None,
        description=(
            "The GTN_CONFIG_ environment variable name supplying this value, e.g. "
            "'GTN_CONFIG_LIBRARIES_DIRECTORY'. Set only when layer == 'env'."
        ),
    )


class ConfigLayer(BaseModel):
    """One layer of the config stack: its own contents, unmerged."""

    layer: ConfigLayerName
    path: str | None = Field(
        default=None,
        description="Absolute path to this layer's config file. None for 'default', 'runtime' and 'env'.",
    )
    present: bool = Field(
        description=(
            "Whether this layer currently has any effect: the file exists for a file layer, "
            "at least one GTN_CONFIG_ variable is set for 'env', or a project activation has "
            "pinned a workspace for 'runtime'. 'default' is always True."
        )
    )
    parse_error: str | None = Field(
        default=None,
        description=(
            "Set only when this layer's file exists but failed to parse as JSON. A missing "
            "file leaves this None with present=False."
        ),
    )
    values: dict[str, Any] = Field(
        default_factory=dict,
        description="This layer's own parsed contents, unmerged with any other layer.",
    )


@dataclass
@PayloadRegistry.register
class GetConfigValueRequest(RequestPayload):
    """Get a specific configuration value.

    Use when: Reading application settings, checking node configurations, retrieving user preferences,
    accessing environment-specific values. Key format: "category.key" or "category.subcategory.key".

    Args:
        category_and_key: Configuration key in format "category.key" or "category.subcategory.key"

    Results: GetConfigValueResultSuccess (with value) | GetConfigValueResultFailure (key not found)
    """

    category_and_key: str


@dataclass
@PayloadRegistry.register
class GetConfigValueResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Configuration value retrieved successfully.

    Args:
        value: The configuration value (can be any type)
        source: Which config layer currently supplies this value. For a category (dict) key
            this names the highest-priority layer that mentions the category at all; use
            `GetConfigCategoryResultSuccess.sources` for per-leaf provenance.
        editable: Whether a `SetConfigValueRequest` for this key is the user's to make. Judged
            leaf by leaf, so a project layer pinning "nuke.executable" does not lock
            "nuke.port". True does not promise the next write takes effect immediately:
            `workspace_directory` is editable while the open project pins it, and a write to it
            applies on the next project open. See `SetConfigValueResultSuccess.reason`.
    """

    value: Any
    source: ConfigValueSource = field(default_factory=lambda: ConfigValueSource(layer="default"))
    editable: bool = True


@dataclass
@PayloadRegistry.register
class GetConfigValueResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Configuration value retrieval failed. Common causes: key not found, invalid category format."""


@dataclass
@PayloadRegistry.register
class SetConfigValueRequest(RequestPayload):
    """Set a specific configuration value.

    Use when: Updating application settings, configuring node behavior, storing user preferences,
    setting environment-specific values. Key format: "category.key" or "category.subcategory.key".

    Args:
        category_and_key: Configuration key in format "category.key" or "category.subcategory.key"
        value: Value to set for the configuration key

    Results: SetConfigValueResultSuccess | SetConfigValueResultFailure (invalid key, value error)
    """

    category_and_key: str
    value: Any


@dataclass
@PayloadRegistry.register
class SetConfigValueResultSuccess(ResultPayloadSuccess):
    """Configuration value set successfully.

    Success means the write reached disk, not that the value took effect. Check `applied`.

    Args:
        applied: Whether the requested value is now the effective (merged) value. A dict value
            is judged leaf by leaf, so False means at least one written leaf did not take
            effect, not necessarily all of them.
        effective_value: What `GetConfigValueRequest` for this key returns after this write.
            Equal to the requested value when `applied` is True.
        shadowed_by: The layer that won instead. Set only when `reason` is "shadowed".
        reason: Why the write is not in effect, or None when `applied` is True. Prefer this
            over inferring from `shadowed_by`, which is null for two of the three reasons.
            `result_details` carries the same distinction as user-facing prose.
    """

    applied: bool = True
    effective_value: Any = None
    shadowed_by: ConfigValueSource | None = None
    reason: ConfigWriteUnappliedReason | None = None


@dataclass
@PayloadRegistry.register
class SetConfigValueResultFailure(ResultPayloadFailure):
    """Configuration value setting failed. Common causes: invalid key format, value validation error."""


@dataclass
@PayloadRegistry.register
class GetConfigCategoryRequest(RequestPayload):
    """Get all configuration values within a category.

    Use when: Retrieving multiple related settings, displaying configuration sections in UIs,
    backing up/restoring configuration groups, bulk configuration operations.

    Args:
        category: Name of the configuration category (None for all categories)

    Results: GetConfigCategoryResultSuccess (with contents dict) | GetConfigCategoryResultFailure (category not found)
    """

    category: str | None = None


@dataclass
@PayloadRegistry.register
class GetConfigCategoryResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Configuration category retrieved successfully.

    Args:
        contents: Dictionary of key-value pairs within the category
        sources: `ConfigValueSource` for every leaf key under this category, keyed by full
            dot path FROM THE CONFIG ROOT, not relative to `category`: fetching
            "app_events.on_app_initialization_complete" returns keys like
            "app_events.on_app_initialization_complete.libraries_to_register". A dict value is
            a sub-category and is recursed into; a list (or any other non-dict) value is one
            leaf entry, never split per item.
    """

    contents: dict[str, Any]
    sources: dict[str, ConfigValueSource] = field(default_factory=dict)


@dataclass
@PayloadRegistry.register
class GetConfigCategoryResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Configuration category retrieval failed. Common causes: category not found, invalid category name."""


@dataclass
@PayloadRegistry.register
class SetConfigCategoryRequest(RequestPayload):
    """Set multiple configuration values within a category.

    Use when: Bulk updating configuration settings, restoring configuration sections,
    applying configuration templates, batch configuration operations.

    Args:
        contents: Dictionary of key-value pairs to set in the category
        category: Name of the configuration category (None for default)

    Results: SetConfigCategoryResultSuccess | SetConfigCategoryResultFailure (invalid category, value errors)
    """

    contents: dict[str, Any]
    category: str | None = None


@dataclass
@PayloadRegistry.register
class SetConfigCategoryResultSuccess(ResultPayloadSuccess):
    """Configuration category updated successfully.

    See `SetConfigValueResultSuccess` for what the four outcome fields mean. They are only
    computed when `category` names a single key; a full-config replacement (`category` is
    None or "") rewrites the whole user layer at once and leaves them at their defaults.

    Whether the write took effect is judged per leaf of `contents`, so a higher-priority
    layer that defines a different key under the same category does not make this write
    unapplied. `result_details` names the first leaf that did not take effect.

    Args:
        applied: See `SetConfigValueResultSuccess.applied`.
        effective_value: See `SetConfigValueResultSuccess.effective_value`, for the whole
            category.
        shadowed_by: See `SetConfigValueResultSuccess.shadowed_by`.
        reason: See `SetConfigValueResultSuccess.reason`.
    """

    applied: bool = True
    effective_value: Any = None
    shadowed_by: ConfigValueSource | None = None
    reason: ConfigWriteUnappliedReason | None = None


@dataclass
@PayloadRegistry.register
class SetConfigCategoryResultFailure(ResultPayloadFailure):
    """Configuration category update failed. Common causes: invalid category name, value validation errors."""


@dataclass
@PayloadRegistry.register
class GetWorkspaceRequest(RequestPayload):
    """Get the absolute path to the configured workspace directory.

    Use when: Resolving relative FILE_SYSTEM-category settings (e.g. `sandbox_library_directory`,
    `static_files_directory`, `libraries_directory`) into absolute paths, deciding where to write
    files the engine is supposed to see, displaying workspace info to users.

    The returned path has `~` expanded and symlinks resolved, so it is safe to use directly with
    `Path` / `os.path` operations.

    Results: GetWorkspaceResultSuccess (with absolute workspace path)
    """


@dataclass
@PayloadRegistry.register
class GetWorkspaceResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Workspace path retrieved successfully.

    Args:
        workspace_path: Absolute path to the workspace directory.
    """

    workspace_path: str


@dataclass
@PayloadRegistry.register
class GetConfigPathRequest(RequestPayload):
    """Get the path to the configuration file.

    Use when: Locating configuration files, debugging configuration issues,
    implementing configuration backup/restore, displaying configuration info to users.

    Results: GetConfigPathResultSuccess (with path) | GetConfigPathResultFailure (path not available)
    """


@dataclass
@PayloadRegistry.register
class GetConfigPathResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Configuration path retrieved successfully.

    Args:
        config_path: Path to the configuration file (None if using default/memory config)
    """

    config_path: str | None = None


@dataclass
@PayloadRegistry.register
class GetConfigPathResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Configuration path retrieval failed. Common causes: configuration not initialized, access denied."""


@dataclass
@PayloadRegistry.register
class GetConfigLayersRequest(RequestPayload):
    """Get the full config layer stack, in isolation, lowest to highest priority.

    Use when: diagnosing why a setting doesn't take effect, building a settings UI that
    shows provenance, or writing an environment/support report. Unlike
    `GetConfigCategoryRequest`, which returns only the merged (winning) values, this returns
    each layer's own contents separately, including a layer whose file failed to parse.

    Results: GetConfigLayersResultSuccess (with layers, lowest to highest priority)
    """


@dataclass
@PayloadRegistry.register
class GetConfigLayersResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Config layer stack retrieved successfully.

    Args:
        layers: Every config layer, ordered lowest to highest priority. Always exactly six
            entries: default, user, project, workspace, runtime, env. `project`/`workspace`
            report `path=None`, `present=False` when no project is active. `runtime` carries
            the active project's workspace pin and never has a path.
    """

    layers: list[ConfigLayer]


@dataclass
@PayloadRegistry.register
class ResetConfigRequest(RequestPayload):
    """Reset configuration to default values.

    Use when: Recovering from configuration errors, restoring default settings,
    clearing user customizations, troubleshooting configuration issues.

    Results: ResetConfigResultSuccess | ResetConfigResultFailure (reset error)
    """


@dataclass
@PayloadRegistry.register
class ResetConfigResultSuccess(ResultPayloadSuccess):
    """Configuration reset successfully to default values."""


@dataclass
@PayloadRegistry.register
class ResetConfigResultFailure(ResultPayloadFailure):
    """Configuration reset failed. Common causes: file system errors, permission issues, initialization errors."""


@dataclass
@PayloadRegistry.register
class GetConfigSchemaRequest(RequestPayload):
    """Get the JSON schema for the configuration model.

    Use when: Frontend needs to understand field types, enums, and validation rules
    for rendering appropriate UI components (dropdowns, text inputs, etc.).

    Results: GetConfigSchemaResultSuccess (with schema) | GetConfigSchemaResultFailure (schema generation error)
    """


@dataclass
@PayloadRegistry.register
class GetConfigSchemaResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Configuration schema retrieved successfully.

    Args:
        schema: The JSON schema for the configuration model
    """

    schema: dict[str, Any]


@dataclass
@PayloadRegistry.register
class GetConfigSchemaResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Configuration schema retrieval failed. Common causes: schema generation error, model validation issues."""
