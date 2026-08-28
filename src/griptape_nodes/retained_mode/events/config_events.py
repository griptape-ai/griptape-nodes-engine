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

# Fixed vocabulary for which config layer supplies a value's effective content, lowest to
# highest priority. Matches ConfigManager.load_configs' merge order exactly: "default" is
# the Settings model's own defaults, "env" is GTN_CONFIG_ environment variables. Shared
# between ConfigValueSource and ConfigLayer so a caller matching one against the other
# (e.g. is this key's source among GetConfigLayersResultSuccess.layers) compares equal
# strings.
ConfigLayerName = Literal["default", "user", "project", "workspace", "env"]


class ConfigValueSource(BaseModel):
    """Identifies which config layer currently supplies a value's effective content.

    Only "default" and "user" are writable by `SetConfigValueRequest` /
    `SetConfigCategoryRequest` today: a value sourced from "project", "workspace", or
    "env" is a value a settings-editor write cannot change, because a higher-priority
    layer keeps re-supplying its own value on every `load_configs()` remerge. See
    `ConfigManager.shadowed_by`.
    """

    layer: ConfigLayerName
    path: str | None = Field(
        default=None,
        description=(
            "Absolute path to the config file backing this layer. None for 'default' "
            "(no file backs the Settings model's own defaults) and for 'env' (see env_var "
            "instead). May be None for 'project'/'workspace' too when no project is active."
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
    """One layer of the config stack, in isolation: this layer's own contents, unmerged.

    Used by `GetConfigLayersRequest` to answer "which layer set this key" and "did this
    layer's file even parse" without requiring the caller to already know the answer --
    unlike `ConfigManager.merged_config`, which only ever shows the WINNING value.
    """

    layer: ConfigLayerName
    path: str | None = Field(
        default=None,
        description="Absolute path to this layer's config file. None for 'default' and 'env'.",
    )
    present: bool = Field(
        description=(
            "Whether this layer currently has any effect: the file exists for a file layer, "
            "or at least one GTN_CONFIG_ variable is set for 'env'. 'default' is always True."
        )
    )
    parse_error: str | None = Field(
        default=None,
        description=(
            "Set only when this layer's file EXISTS but failed to parse as JSON. A missing "
            "file is not an error and leaves this None with present=False."
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
        source: Which config layer currently supplies this value.
        editable: Whether a `SetConfigValueRequest` for this same key can actually change
            the effective value (True when `source.layer` is "default" or "user"; False
            when a higher-priority project/workspace/env layer would keep overriding it).
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

    A success here means the write reached disk; it does NOT mean the value took
    effect. Check `applied` and `shadowed_by` before assuming so -- a project or
    workspace config layer (or a GTN_CONFIG_ environment variable) can outrank the
    write, in which case it is stored but has no visible effect until that layer changes.

    One key is exempt from that guarantee: `workspace_directory` can also be pinned by a
    runtime per-project override, which is not one of the layers shadowing is computed
    from. A write to it while a project pins the workspace can report `applied` True with
    an `effective_value` that still differs from what was written.

    Args:
        applied: Whether the requested value is now the effective (merged) value. False
            means some higher-priority layer still supplies a different value; see
            `shadowed_by`. A dict value is judged leaf by leaf, so False means at least
            one leaf it wrote is shadowed, not necessarily all of them.
        effective_value: What `GetConfigValueRequest` for this same key would return right
            now, after this write. Equal to the requested value when `applied` is True.
        shadowed_by: The layer that won instead, when `applied` is False. None when
            `applied` is True. `result_details` names which key that layer supplies.
    """

    applied: bool = True
    effective_value: Any = None
    shadowed_by: ConfigValueSource | None = None


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
            dot path FROM THE CONFIG ROOT (not relative to `category`), e.g.
            "app_events.on_app_initialization_complete.libraries_to_register" even when
            `category` was "app_events.on_app_initialization_complete". Root-relative keys
            let a caller look a key up the same way no matter which category it was
            fetched through. A dict value is a sub-category and is recursed into; a list (or
            any other non-dict) value is one leaf entry, never split per item.
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

    See `SetConfigValueResultSuccess` for what `applied`/`effective_value`/`shadowed_by`
    mean; the same shadowing risk applies here. They are only computed when `category` is
    a single named key (the request routes through `ConfigManager.set_config_value` for
    that key): a full-config replacement (`category` is None or "") rewrites the whole
    user layer at once, has no single key to check, and leaves these three at their
    defaults.

    Shadowing is judged per leaf of `contents`, so a higher-priority layer that defines a
    DIFFERENT key under the same category does not make this write shadowed. `applied`
    False therefore means at least one of the written leaves is shadowed; the rest may
    well have taken effect. `result_details` names the first shadowed leaf.

    Args:
        applied: See `SetConfigValueResultSuccess.applied`. Always True (default) for a
            full-config replacement.
        effective_value: See `SetConfigValueResultSuccess.effective_value`, for the whole
            category. Always None (default) for a full-config replacement.
        shadowed_by: See `SetConfigValueResultSuccess.shadowed_by`. Always None (default)
            for a full-config replacement.
    """

    applied: bool = True
    effective_value: Any = None
    shadowed_by: ConfigValueSource | None = None


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
    shows provenance (which file, or which GTN_CONFIG_ variable, currently owns a value),
    or writing an environment/support report. Unlike `GetConfigCategoryRequest`, which
    only ever returns the MERGED (winning) values, this returns each layer's own contents
    separately -- including a layer whose file exists but failed to parse, which today
    only ever reaches a log line.

    Results: GetConfigLayersResultSuccess (with layers, lowest to highest priority)
    """


@dataclass
@PayloadRegistry.register
class GetConfigLayersResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Config layer stack retrieved successfully.

    Args:
        layers: Every config layer, ordered lowest to highest priority. Always exactly
            five entries, in this fixed order: default, user, project, workspace, env.
            `project`/`workspace` report `path=None`, `present=False` when no project is
            currently active.
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
