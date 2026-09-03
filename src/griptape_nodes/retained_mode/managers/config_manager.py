import copy
import json
import logging
import os
from pathlib import Path
from typing import Any, Literal, NamedTuple

from pydantic import ValidationError
from xdg_base_dirs import xdg_config_home

from griptape_nodes.files.path_utils import resolve_workspace_path
from griptape_nodes.node_library.library_registry import LibraryRegistry
from griptape_nodes.retained_mode.engine import Engine, EngineScoped
from griptape_nodes.retained_mode.events.app_events import ConfigChanged
from griptape_nodes.retained_mode.events.artifact_events import (
    GetArtifactSchemasRequest,
    GetArtifactSchemasResultSuccess,
)
from griptape_nodes.retained_mode.events.base_events import ResultPayload
from griptape_nodes.retained_mode.events.config_events import (
    ConfigLayer,
    ConfigLayerName,
    ConfigValueSource,
    ConfigWriteUnappliedReason,
    GetConfigCategoryRequest,
    GetConfigCategoryResultFailure,
    GetConfigCategoryResultSuccess,
    GetConfigLayersRequest,
    GetConfigLayersResultSuccess,
    GetConfigPathRequest,
    GetConfigPathResultSuccess,
    GetConfigSchemaRequest,
    GetConfigSchemaResultFailure,
    GetConfigSchemaResultSuccess,
    GetConfigValueRequest,
    GetConfigValueResultFailure,
    GetConfigValueResultSuccess,
    GetWorkspaceRequest,
    GetWorkspaceResultSuccess,
    ResetConfigRequest,
    ResetConfigResultFailure,
    ResetConfigResultSuccess,
    SetConfigCategoryRequest,
    SetConfigCategoryResultFailure,
    SetConfigCategoryResultSuccess,
    SetConfigValueRequest,
    SetConfigValueResultFailure,
    SetConfigValueResultSuccess,
)
from griptape_nodes.retained_mode.events.os_events import (
    ExistingFilePolicy,
    FileIOFailureReason,
    GetFileInfoRequest,
    GetFileInfoResultFailure,
    RenameFileRequest,
    RenameFileResultFailure,
    WriteFileRequest,
    WriteFileResultFailure,
)
from griptape_nodes.retained_mode.managers.event_manager import EventManager
from griptape_nodes.retained_mode.managers.settings import (
    DEFAULT_LIBRARIES_DIRECTORY,
    DISCOVERY_MAX_DEPTH_KEY,
    LIBRARIES_DIRECTORY_KEY,
    WORKFLOWS_TO_REGISTER_KEY,
    Settings,
)
from griptape_nodes.utils.dict_utils import get_dot_value, merge_dicts, set_dot_value
from griptape_nodes.utils.file_utils import DEFAULT_MAX_SEARCH_DEPTH

logger = logging.getLogger("griptape_nodes")

USER_CONFIG_PATH = xdg_config_home() / "griptape_nodes" / "griptape_nodes_config.json"

# Sentinel distinguishing "this layer's dict has no entry for this key" from "this layer's
# dict has an entry whose value happens to be None" (e.g. `project_file: str | None`).
# A plain `None` default can't do this job: `None` is also a legitimate stored value.
_KEY_NOT_IN_LAYER = object()

# The only layers a settings write can control. `set_config_value` writes the user config
# file, and "default" means nothing has overridden the Settings model, so a key sourced
# from either is a key the user owns. Any other winning layer (project, workspace, runtime,
# env) re-supplies its own value on every load_configs(), making a user-layer write invisible.
_WRITABLE_LAYERS: frozenset[ConfigLayerName] = frozenset({"default", "user"})

# Environment variable the engine PUBLISHES (it is never read as config input) carrying the absolute
# directory libraries install under when no project declares a libraries_dir. Deliberately outside
# the GTN_CONFIG_ prefix: that prefix is the highest-priority config INPUT layer, so publishing there
# would pin the resolved path above every config file and every project override.
DEFAULT_LIBRARIES_ROOT_ENV_VAR = "GTN_DEFAULT_LIBRARIES_ROOT"

# Prefix marking an environment variable as a config input, and the separator that splits the
# remainder into a nested key path. A single underscore cannot serve as the separator because
# setting names contain underscores themselves (`storage_backend`), so `WORKER_HEARTBEAT_TIMEOUT_S`
# would be ambiguous.
ENV_VAR_PREFIX = "GTN_CONFIG_"
ENV_VAR_PATH_SEPARATOR = "__"


class EnvVarOverride(NamedTuple):
    """One GTN_CONFIG_ variable, with the config key and coerced value it resolves to."""

    env_var_name: str
    config_key: str
    raw_value: str
    value: Any


# Outcomes of coercing an environment variable that no real config value can collide with.
_REJECTED_BAD_VALUE = object()
_REJECTED_UNKNOWN_KEY = object()


class LoadedConfigFile(NamedTuple):
    """One config file's parsed contents plus why it failed to parse, if it did.

    `contents` is `{}` when the file is missing or unparsable. `parse_error` is None
    unless the file exists and failed to parse, so a caller can tell "no such file"
    apart from "file is broken" without stat-ing the path a second time.
    """

    contents: dict
    parse_error: str | None


class ConfigWriteOutcome(NamedTuple):
    """Whether a completed user-layer write is the value now in effect.

    A write always lands in the user config file; whether it takes effect depends on more than
    where it landed. `unapplied_key` names the first key that did not take effect, which for a
    category write is a leaf under it rather than the category itself, and `reason` says why.
    `shadowed_by` is set only for `reason == "shadowed"`. See `ConfigManager._write_outcome`.
    """

    applied: bool
    effective_value: Any
    unapplied_key: str | None
    shadowed_by: ConfigValueSource | None
    reason: ConfigWriteUnappliedReason | None = None


class _ShadowedLeaf(NamedTuple):
    """One written key that a higher-priority layer keeps supplying, and the layer doing it."""

    key: str
    source: ConfigValueSource


class _UnappliedLeaf(NamedTuple):
    """One written key whose value is not the one now in effect, and why.

    `source` is set only when `reason` is "shadowed"; the other reasons name no layer, because
    no config layer is responsible for them.
    """

    key: str
    reason: ConfigWriteUnappliedReason
    source: ConfigValueSource | None = None


class _MergedConfigRejection(NamedTuple):
    """The keys that made the merged config fail `Settings` validation.

    Recorded by `load_configs` when it discards the merge and falls back to defaults, which is
    otherwise visible only as a log line. Held so a write's outcome can name the setting that is
    actually broken: it is frequently NOT the key the user just wrote, and without it the only
    honest thing a message can say is that something somewhere is wrong.
    """

    keys: tuple[str, ...]


def _offending_keys(error: ValidationError) -> tuple[str, ...]:
    """Dot paths of the config keys a `Settings` validation error blames, in report order.

    Pydantic reports each error's location as a tuple of path segments; joining them yields the
    same dot notation the config API takes, so the key can be shown to a user or fed back into
    `GetConfigValueRequest`. Errors with an empty location (whole-model failures) are skipped,
    since they name no key a user could go and fix.

    Args:
        error: The validation error raised by `Settings.model_validate`.
    """
    keys = []
    for detail in error.errors():
        location = detail.get("loc") or ()
        if not location:
            continue
        keys.append(".".join(str(segment) for segment in location))
    return tuple(keys)


class _LayerProbe(NamedTuple):
    """One layer to check when resolving where a key's effective value comes from."""

    layer: ConfigLayerName
    values: dict
    path: Path | None


class ConfigManager(EngineScoped):
    """A class to manage application configuration and file pathing.

    This class handles loading and saving configuration from multiple sources with the following precedence:
    1. Default configuration from Settings model (lowest priority)
    2. User global configuration from ~/.config/griptape_nodes/griptape_nodes_config.json
    3. Project-adjacent configuration from <project_dir>/griptape_nodes_config.json
    4. Workspace configuration from <workspace_dir>/griptape_nodes_config.json
    5. Runtime workspace pin from the active project (workspace_directory only; see
       set_workspace_override)
    6. Environment variables with GTN_CONFIG_ prefix (highest priority)

    Environment variables starting with GTN_CONFIG_ are converted to config keys by removing the prefix
    and converting to lowercase (e.g., GTN_CONFIG_FOO=bar becomes {"foo": "bar"}), with a double
    underscore separating nested keys (GTN_CONFIG_WORKER__HEARTBEAT_TIMEOUT_S=30 becomes
    {"worker": {"heartbeat_timeout_s": 30.0}}). That flow is input-only: nothing here writes a
    GTN_CONFIG_ variable back out.

    The one variable this manager PUBLISHES is GTN_DEFAULT_LIBRARIES_ROOT, written once at
    construction so project templates can reference the engine's default libraries root. See
    _publish_default_libraries_root.

    Supports categorized configuration using dot notation (e.g., 'category.subcategory.key')
    to organize related configuration items.

    Attributes:
        default_config (dict): The default configuration loaded from the Settings model.
        user_config (dict): The user configuration loaded from the config file.
        project_config (dict): The project-adjacent configuration loaded when a project is set.
        workspace_config (dict): The workspace configuration loaded when a workspace is resolved.
        env_config (dict): The configuration loaded from GTN_CONFIG_ environment variables.
        merged_config (dict): The merged configuration, combining all sources in precedence order.
    """

    def __init__(self, event_manager: EventManager | None = None, *, engine: Engine | None = None) -> None:
        """Initialize the ConfigManager.

        Args:
            event_manager: The EventManager instance to use for event handling.
            engine: The Engine this manager belongs to.
        """
        super().__init__(engine)
        self._project_config_path: Path | None = None
        self._workspace_config_path: Path | None = None
        self._workspace_dir_override: str | None = None
        # Whether the active workspace pin merely restates a value the config stack already
        # supplies (see set_workspace_override). Such a pin is not its own layer: the file the
        # value came from is the editable owner, and reporting "runtime" would lock a field the
        # user can in fact change.
        self._workspace_pin_supplied_by_config: bool = False
        # Set by load_configs when the merged result fails Settings validation and the whole merge
        # is discarded. None means the live merge is valid.
        self._merged_config_rejection: _MergedConfigRejection | None = None
        self._libraries_root_override: str | None = None
        # (variable name, value) pairs already reported as ignored. The env layer is re-read on
        # every load_configs() and on each read_env_config() call, so without this a single bad
        # variable warns repeatedly for the life of this manager. Other ConfigManagers built
        # elsewhere in the process keep their own accounting.
        self._reported_invalid_env_vars: set[tuple[str, str]] = set()
        # Parse error for the most recent load of each file layer, keyed by layer name and
        # set by load_configs() via _load_file_layer. None means either the file doesn't
        # exist or it parsed fine; config_layers() distinguishes those two cases with
        # `present`. Deliberately left alone by compute_project_provisioning_config /
        # compute_system_defaults_provisioning_config's own _load_config_from_file calls,
        # which preview some other project's config and would otherwise clobber the live
        # layer's error with a preview's.
        self._layer_parse_errors: dict[ConfigLayerName, str | None] = {}
        self.load_configs()

        # Once per engine process, before any project YAML is read. See the method docstring for why
        # this cannot live in load_configs().
        self._publish_default_libraries_root()

        self._set_log_level(self.merged_config.get("log_level", logging.INFO))

        # Store event manager reference for broadcasting config change events
        self._event_manager = event_manager

        if event_manager is not None:
            # Register all our listeners.
            event_manager.assign_manager_to_request_type(
                GetConfigCategoryRequest, self.on_handle_get_config_category_request
            )
            event_manager.assign_manager_to_request_type(
                SetConfigCategoryRequest, self.on_handle_set_config_category_request
            )
            event_manager.assign_manager_to_request_type(GetConfigValueRequest, self.on_handle_get_config_value_request)
            event_manager.assign_manager_to_request_type(SetConfigValueRequest, self.on_handle_set_config_value_request)
            event_manager.assign_manager_to_request_type(GetConfigPathRequest, self.on_handle_get_config_path_request)
            event_manager.assign_manager_to_request_type(
                GetConfigLayersRequest, self.on_handle_get_config_layers_request
            )
            event_manager.assign_manager_to_request_type(GetWorkspaceRequest, self.on_handle_get_workspace_request)
            event_manager.assign_manager_to_request_type(
                GetConfigSchemaRequest, self.on_handle_get_config_schema_request
            )
            event_manager.assign_manager_to_request_type(ResetConfigRequest, self.on_handle_reset_config_request)

    @property
    def workspace_path(self) -> Path:
        """Get the base file path from the configuration.

        Returns:
            Path object representing the base file path.
        """
        return Path(self._workspace_path).resolve()

    @workspace_path.setter
    def workspace_path(self, path: str | Path) -> None:
        """Set the base file path in the configuration.

        Args:
            path: The path to set as the base file path.
        """
        self._workspace_path = str(Path(path).expanduser().resolve())

    def set_workspace_override(self, path: Path | None, *, supplied_by_config: bool = False) -> None:
        """Set a runtime workspace directory override.

        This override takes precedence over config-file-based workspace_directory
        values (default, user, project, workspace configs) but is still overridden
        by the GTN_CONFIG_WORKSPACE_DIRECTORY environment variable.

        Used by ProjectManager to apply project_workspaces mappings and
        auto-default-to-project-dir behavior. Also updates workspace_path immediately
        so callers see the correct value before the next load_configs() call.

        Args:
            path: The workspace directory override, or None to clear it.
            supplied_by_config: True when `path` is the value a config layer already supplies,
                which `ProjectManager` does on an ordinary activation by reading
                `workspace_directory` out of the user (or default) layer and pinning it back.
                Such a pin changes nothing about who owns the setting, so `value_source` keeps
                reporting the file layer and a write to `workspace_directory` keeps applying.
                Leave False for a pin whose value has no config-layer origin: a project
                template's `workspace_dir`, a `project_workspaces` mapping, or an inherited
                ancestor workspace. Those are reported as the "runtime" layer.
        """
        if path is None:
            self._workspace_dir_override = None
            self._workspace_pin_supplied_by_config = False
        else:
            resolved = str(Path(path).expanduser().resolve())
            self._workspace_dir_override = resolved
            self._workspace_pin_supplied_by_config = supplied_by_config
            self._workspace_path = resolved

    def set_libraries_root_override(self, path: Path | None) -> None:
        """Set a runtime override for where libraries install and resolve.

        When set, it is the absolute root used by resolved_libraries_root() in place
        of the workspace-relative libraries_directory. Used by ProjectManager to apply
        a project's own (or inherited) libraries_dir so a child project can share its
        parent's library install location. Pass None to clear it, restoring the
        workspace-relative default.

        Args:
            path: The absolute libraries root override, or None to clear it.
        """
        if path is None:
            self._libraries_root_override = None
        else:
            self._libraries_root_override = str(Path(path).expanduser().resolve())

    def _resolve_configured_workspace_directory(self, *, include_runtime_override: bool) -> str:
        """Resolve workspace_directory across the config layers, in load_configs' precedence order.

        Single source of truth for how workspace_directory is layered (highest priority first):
        env var, then the runtime override (only when `include_runtime_override`), then workspace
        config, project-adjacent config, user config, and finally the Settings default. load_configs
        uses this WITH the override to set the active workspace_path; configured_global_workspace_path
        uses it WITHOUT, to get the engine's workspace independent of any single project's pin. Keeping
        both on this one helper is what stops the two precedences from drifting. The Settings default
        always populates default_config, so a value is always returned.
        """
        # env is applied last in load_configs, so it is highest priority; the runtime override sits
        # just below env and above the config files.
        if (env_value := get_dot_value(self.env_config, "workspace_directory", None)) is not None:
            return env_value
        if include_runtime_override and self._workspace_dir_override is not None:
            return self._workspace_dir_override
        for layer in (self.workspace_config, self.project_config, self.user_config):
            if (configured := get_dot_value(layer, "workspace_directory", None)) is not None:
                return configured
        return self.default_config["workspace_directory"]

    def configured_global_workspace_path(self) -> Path:
        """Return the workspace_directory as configured, EXCLUDING the runtime per-project override.

        The runtime `_workspace_dir_override` is the only layer that pins the active workspace to a
        self-contained project's own folder (workspace_dir "./"); everything else (a
        GTN_CONFIG_WORKSPACE_DIRECTORY env var, a project-adjacent or workspace `workspace_directory`,
        the user/default config) describes where the ENGINE's workspace lives.

        This is the base for the unset-libraries_dir fallback: libraries follow an engine that
        relocates its whole workspace via env/adjacent config, but do NOT follow a single project's
        self-contained workspace_dir pin (so a v1 self-contained project's unset libraries still land
        in the shared workspace `libraries/`, matching pre-v1 behavior).
        """
        return Path(self._resolve_configured_workspace_directory(include_runtime_override=False)).expanduser().resolve()

    def resolved_libraries_root(self) -> Path:
        """Return the absolute directory under which libraries install and resolve.

        When a libraries-root override is set (from a project's own or inherited
        libraries_dir), it is returned verbatim. Otherwise the fallback resolves the
        workspace-relative libraries_directory config value against the GLOBAL configured
        workspace (configured_global_workspace_path), NOT the active per-project workspace.
        This keeps libraries in the shared global-workspace `libraries/` folder even for a
        self-contained project whose workspace_dir pins the active workspace to its own folder,
        preserving the pre-v1 shared-libraries behavior. A project opts into a project-local
        libraries dir by declaring an explicit libraries_dir (which sets the override above).
        """
        if self._libraries_root_override is not None:
            return Path(self._libraries_root_override)
        return self.default_libraries_root(self.get_config_value(LIBRARIES_DIRECTORY_KEY))

    def default_libraries_root(self, libraries_directory: str | None) -> Path:
        """Resolve a workspace-relative libraries_directory value against the GLOBAL workspace.

        The no-libraries_dir-declared fallback, factored out so every caller computes it the same
        way: the live path above, the provisioning preview, and the offline resolver that reports
        each project's effective libraries root on the project listing. Previously each open-coded
        `resolve_workspace_path(Path(...), configured_global_workspace_path())`, which is exactly
        the kind of duplication that lets a preview or a UI hint drift from what activation does.

        The base is deliberately configured_global_workspace_path() -- the ENGINE's workspace,
        excluding the runtime per-project override -- so this answer does NOT depend on which
        project happens to be active. That is what makes it safe to compute for a project that is
        not loaded. `libraries_directory` is the caller's business: it must come from the TARGET
        project's config layers, not the merged view of whatever project is open.

        A missing or empty value falls back to DEFAULT_LIBRARIES_DIRECTORY here rather than at each
        call site: a caller that forgot its own default would otherwise hand us None and resolve the
        libraries root to the workspace itself (`Path("")` is `Path(".")`), quietly installing
        libraries on top of the user's workspace.

        Args:
            libraries_directory: The `libraries_directory` config value (absolute, or relative to
                the global workspace). Anything that is not a non-empty string means "not
                configured" -- None and "" say so deliberately, and a non-string says it by
                accident. The annotation names what callers SHOULD pass, not the full set of what
                arrives: the value comes from user-editable config JSON, where nothing enforces the
                type, so `get_config_value` can hand over an int or a list.

        Returns:
            The absolute directory libraries install and resolve under.
        """
        # The isinstance half is load-bearing, not a redundant belt on the annotation: a config file
        # with `"libraries_directory": 1` reaches here as an int, and Path(1) raises. Widening this
        # to a string check alone would restore that crash.
        if not isinstance(libraries_directory, str) or not libraries_directory:
            libraries_directory = DEFAULT_LIBRARIES_DIRECTORY
        return resolve_workspace_path(Path(libraries_directory), self.configured_global_workspace_path())

    def _publish_default_libraries_root(self) -> None:
        """Publish the default libraries root to os.environ as GTN_DEFAULT_LIBRARIES_ROOT.

        Exists so a project template can write `libraries_dir: "${GTN_DEFAULT_LIBRARIES_ROOT}/shared"`
        instead of hardcoding an absolute path per machine. `resolve_project_path_field` expands shell
        variables in that field, and env vars are the only substitution it accepts -- `{macro}` tokens
        are refused there because building the macro bag needs the very values those fields produce.

        Publishes `default_libraries_root`, NOT `resolved_libraries_root`. The latter returns
        `_libraries_root_override` when a project declares or inherits a libraries_dir, so publishing
        it would make the variable's value depend on the field that reads it. The default is derived
        from the ENGINE's workspace and is therefore an answer that exists before any project loads.

        Called once from __init__, deliberately not from load_configs(), which re-runs on every
        project activation -- by which point the override may be set, reintroducing that circularity.
        The consequence to keep in mind: this reflects the config layers present at boot (env vars,
        user config, defaults), so a project-adjacent or workspace config that re-points
        libraries_directory at activation time is NOT retroactively published here.

        The value must be in place before any project YAML is read, because a project naming an unset
        variable is refused at load time and becomes unusable. __init__ runs when the Engine builds
        its managers, well before projects load in on_app_initialization_complete.

        An existing value is overwritten rather than preserved. This is a published value, so a stale
        one left in the shell would make the variable lie about where this engine installs libraries;
        the supported ways to move that location are the libraries_directory config value and a
        project's libraries_dir.
        """
        default_root = self.default_libraries_root(self.get_config_value(LIBRARIES_DIRECTORY_KEY))
        os.environ[DEFAULT_LIBRARIES_ROOT_ENV_VAR] = str(default_root)
        logger.debug("Published %s=%s", DEFAULT_LIBRARIES_ROOT_ENV_VAR, default_root)

    def clear_project_layers(self) -> None:
        """Drop all per-activation config state so the next activation starts clean.

        Resets the workspace override and the project-adjacent / workspace config-file
        paths. Without this, switching projects (or rolling back to one) inherits the
        prior project's config-file layer and workspace override. Callers remerge via
        load_configs()/load_project_config()/load_workspace_config() right after.
        """
        self._workspace_dir_override = None
        self._workspace_pin_supplied_by_config = False
        self._libraries_root_override = None
        self._project_config_path = None
        self._workspace_config_path = None

    @property
    def config_files(self) -> list[Path]:
        """Get a list of config files in ascending order of priority.

        The last file shown has the highest priority and overrides
        any settings found in earlier files.

        Returns:
            List of Path objects representing the config files.
        """
        possible_config_files: list[Path] = [USER_CONFIG_PATH]

        if self._project_config_path is not None:
            possible_config_files.append(self._project_config_path)

        if self._workspace_config_path is not None:
            possible_config_files.append(self._workspace_config_path)

        return [config_file for config_file in possible_config_files if config_file.exists()]

    @property
    def discovery_max_depth(self) -> int:
        """Get the operator-configured recursion depth ceiling for recursive file discovery.

        Overridable via the `GTN_CONFIG_DISCOVERY_MAX_DEPTH` env var; falls back to
        DEFAULT_MAX_SEARCH_DEPTH when unset.

        Returns:
            The `discovery_max_depth` setting.
        """
        return self.get_config_value(DISCOVERY_MAX_DEPTH_KEY, default=DEFAULT_MAX_SEARCH_DEPTH, cast_type=int)

    def value_source(self, key: str) -> ConfigValueSource:
        """Return which config layer currently supplies `key`'s effective value.

        Walks the same layers `load_configs` merges, in the same priority order (env
        highest, then the runtime workspace pin, workspace, project, user, default lowest),
        and returns the first (highest-priority) layer whose own dict contains `key` at all.
        That layer wins the merge for `key` whatever any lower layer holds, so it is reported
        as the source even if, say, the project layer's value happens to equal the user
        layer's. Every key resolves to at least "default", since
        `Settings().model_dump()` always populates `default_config`.

        Args:
            key: Dot-notation key, e.g. "libraries_directory" or
                "app_events.on_app_initialization_complete.libraries_to_register". Split on
                ".", so a key whose own name contains a dot cannot be addressed this way; the
                leaf walks use `_value_source_at` with an explicit path instead.
        """
        return self._value_source_at(tuple(key.split(".")))

    def _value_source_at(self, path: tuple[str, ...]) -> ConfigValueSource:
        """`value_source` for an already-split key path.

        Takes the path as segments rather than a dot string so a segment may itself contain
        dots. `project_workspaces` is keyed by project file paths, so joining its keys into a
        dot string and re-splitting them addresses a nesting level that exists in no layer.

        Args:
            path: Key segments from the config root, e.g. ("nuke", "port").
        """
        for probe in self._layer_probes():
            if self._layer_value_at(probe.values, path) is _KEY_NOT_IN_LAYER:
                continue
            if probe.layer == "env":
                return ConfigValueSource(layer=probe.layer, env_var=f"GTN_CONFIG_{'_'.join(path).upper()}")
            layer_path = str(probe.path) if probe.path is not None else None
            return ConfigValueSource(layer=probe.layer, path=layer_path)
        return ConfigValueSource(layer="default")

    def _layer_value_at(self, values: dict, path: tuple[str, ...]) -> Any:
        """Return the value at `path` in one layer's own dict, or `_KEY_NOT_IN_LAYER`.

        Descends by successive dict lookups instead of `get_dot_value`, so a segment
        containing a dot is looked up verbatim. Distinguishes "absent" from "present and
        None", which is what makes provenance correct for an optional setting.

        Args:
            values: One layer's own parsed contents.
            path: Key segments from the config root.
        """
        current: Any = values
        for segment in path:
            if not isinstance(current, dict) or segment not in current:
                return _KEY_NOT_IN_LAYER
            current = current[segment]
        return current

    def _layer_probes(self) -> list[_LayerProbe]:
        """Return the file/env/runtime layers to check for a key, HIGHEST priority first.

        The reverse of `load_configs`' merge order, minus "default", which every key falls
        back to and which has no dict of its own to probe. Kept as one ordered list so
        `value_source` cannot drift out of step with the merge it is describing.

        The runtime pin is expressed as a one-key dict rather than a special case, so it is
        probed by the same `get_dot_value` walk as a file layer. It supplies only
        `workspace_directory`, and only while a project activation holds it.
        """
        return [
            _LayerProbe(layer="env", values=self.env_config, path=None),
            _LayerProbe(layer="runtime", values=self._runtime_pin_values(), path=None),
            _LayerProbe(layer="workspace", values=self.workspace_config, path=self._workspace_config_path),
            _LayerProbe(layer="project", values=self.project_config, path=self._project_config_path),
            _LayerProbe(layer="user", values=self.user_config, path=USER_CONFIG_PATH),
        ]

    def _runtime_pin_values(self) -> dict[str, Any]:
        """Return the runtime layer's own contents: the active project's workspace pin, if any.

        `set_workspace_override` records this in memory during project activation (from a
        project template's `workspace_dir`, a `project_workspaces` mapping, or an inherited
        ancestor workspace), and `load_configs` applies it straight onto `merged_config` above
        the config files and below env. No file holds it, so a settings write cannot reach it --
        which is why it has to be a reportable layer rather than an unexplained loss.

        Empty when the pin merely restates what a config layer already supplies, which is what
        an ordinary activation does. That pin is not a distinct owner: the user's own config
        supplied the value and a write to it still decides what the next activation pins.
        """
        if self._workspace_dir_override is None or self._workspace_pin_supplied_by_config:
            return {}
        return {"workspace_directory": self._workspace_dir_override}

    def shadowed_by(self, key: str) -> ConfigValueSource | None:
        """Return the layer shadowing `key` from the user's own edits, or None if not shadowed.

        "Not shadowed" means `value_source(key)` is "default" or "user" -- the two layers a
        `set_config_value` write can actually control. Any other winning layer (project,
        workspace, runtime, env) means a user-layer write to `key` is currently invisible: it
        lands on disk (see `set_config_value`) but the merged config still reports the higher
        layer's value on every subsequent `load_configs()`. This is what makes
        `SetConfigValueRequest` report success for a write that has no visible effect.

        Args:
            key: Dot-notation key, same as `value_source`.
        """
        return self._shadowed_by_at(tuple(key.split(".")))

    def _shadowed_by_at(self, path: tuple[str, ...]) -> ConfigValueSource | None:
        """`shadowed_by` for an already-split key path. See `_value_source_at`."""
        source = self._value_source_at(path)
        if source.layer in _WRITABLE_LAYERS:
            return None
        return source

    def category_sources(self, category: str | None) -> dict[str, ConfigValueSource]:
        """Return `value_source` for every leaf key under `category`, keyed by full dot path.

        Keys are relative to the CONFIG ROOT, not to `category`: requesting
        "app_events.on_app_initialization_complete" returns keys like
        "app_events.on_app_initialization_complete.libraries_to_register", not just
        "libraries_to_register", so a caller can look a key up the same way regardless of
        which category it was fetched through.

        A dict value is treated as a sub-category and is recursed into. A list (or any
        other non-dict) value is one leaf entry -- a list is never split into per-item
        entries, since whichever layer set the entire list is the source for all of it.

        A leaf whose own name contains a dot (`project_workspaces` is keyed by project file
        paths) gets the right source, because provenance is resolved from the key path rather
        than from the joined string. Its returned key is still ambiguous to a caller
        re-splitting on ".", which is inherent to a dot-keyed return shape.

        Args:
            category: Dot-notation category, or None/"" for the whole merged config.
        """
        if category is None or category == "":
            contents: Any = self.merged_config
            root: tuple[str, ...] = ()
        else:
            contents = self.get_config_value(category, should_load_env_var_if_detected=False)
            root = tuple(category.split("."))

        sources: dict[str, ConfigValueSource] = {}
        if isinstance(contents, dict):
            self._collect_leaf_sources(contents, root, sources)
        return sources

    def _collect_leaf_sources(self, node: dict, path: tuple[str, ...], out: dict[str, ConfigValueSource]) -> None:
        """Recursion helper for `category_sources`. Mutates `out` in place."""
        for key, value in node.items():
            leaf_path = (*path, key)
            if isinstance(value, dict):
                self._collect_leaf_sources(value, leaf_path, out)
            else:
                out[".".join(leaf_path)] = self._value_source_at(leaf_path)

    def config_layers(self) -> list[ConfigLayer]:
        """Return every config layer in isolation, lowest to highest priority.

        Unlike `merged_config`, each returned layer's `values` is that layer's own parsed
        contents, not merged with any other layer -- this is what lets a caller see which
        layer a key came from, and whether a layer's file exists but failed to parse
        (`parse_error`), e.g. for `gtn self info` or a support/environment report. Always
        exactly six entries, in this fixed order: default, user, project, workspace, runtime,
        env. `project`/`workspace` report `path=None`, `present=False` when no project is
        currently active (`_project_config_path`/`_workspace_config_path` unset).

        `present` means "this layer contributes to the merge", not merely "its file
        exists". That distinction matters for one real case: when the workspace dir is the
        project dir, both layers name the same file and `load_configs` deliberately loads
        it once, as `project`. The `workspace` entry then keeps its `path` (so a caller can
        see which file it would have been) but reports `present=False` with empty `values`,
        matching the skip rather than implying the file is applied twice.

        `runtime` is the one layer no file backs: it carries the active project's workspace
        pin, so its `path` is always None and its `values` hold at most `workspace_directory`.
        """
        workspace_config_path = self._workspace_layer_path()
        runtime_pin_values = self._runtime_pin_values()
        return [
            ConfigLayer(
                layer="default",
                path=None,
                present=True,
                parse_error=None,
                values=self.default_config,
            ),
            ConfigLayer(
                layer="user",
                path=str(USER_CONFIG_PATH),
                present=USER_CONFIG_PATH.exists(),
                parse_error=self._layer_parse_errors.get("user"),
                values=self.user_config,
            ),
            ConfigLayer(
                layer="project",
                path=str(self._project_config_path) if self._project_config_path is not None else None,
                present=self._project_config_path is not None and self._project_config_path.exists(),
                parse_error=self._layer_parse_errors.get("project"),
                values=self.project_config,
            ),
            ConfigLayer(
                layer="workspace",
                path=str(self._workspace_config_path) if self._workspace_config_path is not None else None,
                present=workspace_config_path is not None and workspace_config_path.exists(),
                parse_error=self._layer_parse_errors.get("workspace"),
                values=self.workspace_config,
            ),
            ConfigLayer(
                layer="runtime",
                path=None,
                present=bool(runtime_pin_values),
                parse_error=None,
                values=runtime_pin_values,
            ),
            ConfigLayer(
                layer="env",
                path=None,
                present=bool(self.env_config),
                parse_error=None,
                values=self.env_config,
            ),
        ]

    def _workspace_layer_path(self) -> Path | None:
        """Return the file the workspace layer loads, or None when it contributes nothing.

        None means either no workspace is resolved, or the workspace dir is the project dir,
        so both layers name the same file. `load_configs` loads that file once, as `project`,
        and this is the single place that decides so, keeping the load and `config_layers`'
        `present` report from disagreeing about whether the workspace layer applies.
        """
        if self._workspace_config_path == self._project_config_path:
            return None
        return self._workspace_config_path

    def _load_config_from_env_vars(self) -> dict[str, Any]:
        """Load configuration values from GTN_CONFIG_ environment variables.

        Environment variables starting with GTN_CONFIG_ are converted to config keys.
        GTN_CONFIG_FOO=bar becomes {"foo": "bar"}
        GTN_CONFIG_STORAGE_BACKEND=gtc becomes {"storage_backend": "gtc"}

        A double underscore separates nested keys, so
        GTN_CONFIG_WORKER__HEARTBEAT_TIMEOUT_S=30 becomes {"worker": {"heartbeat_timeout_s": 30.0}}.
        Because the whole name is lowercased, this only reaches settings whose keys are lowercase:
        case-sensitive trees such as `nodes.<Library>.<SECRET>` stay config-file-only.

        Returns:
            Dictionary containing config values from environment variables
        """
        env_config: dict[str, Any] = {}
        for override in self._collect_env_var_overrides():
            set_dot_value(env_config, override.config_key, override.value)
            logger.debug("Loaded config from env var: %s -> %s", override.env_var_name, override.config_key)

        return env_config

    def _collect_env_var_overrides(self) -> list[EnvVarOverride]:
        """Resolve the GTN_CONFIG_ variables to apply, reporting each one ignored along the way.

        Four things get a variable ignored: a name with an empty path segment, a value the
        setting's type rejects, a path that names no setting, and a path that is a strict prefix
        of another surviving path.

        The prefix case is last because it only has to separate overrides that are each valid on
        their own. Exporting both GTN_CONFIG_ARTIFACTS__IMAGE and
        GTN_CONFIG_ARTIFACTS__IMAGE__PREVIEW_FORMAT asks for `artifacts.image` to be a value and a
        section at once, and `dict[str, Any]` accepts either, so something has to break the tie:
        `set_dot_value` would otherwise honor whichever `os.environ` yielded last. The deeper path
        wins because it carries strictly more of what was asked for. An override that fails
        validation is not competing for anything, so it must be dropped before the tie is broken;
        judging overlap first would let a typo like GTN_CONFIG_AGENT__SYSTEM_PROMPT__OOPS take a
        correct neighbour down with it.

        Returns:
            The overrides to apply, in environment order.
        """
        coerced = []
        for env_var_name, raw_value in os.environ.items():
            if not env_var_name.startswith(ENV_VAR_PREFIX):
                continue
            config_key = env_var_name.removeprefix(ENV_VAR_PREFIX).lower().replace(ENV_VAR_PATH_SEPARATOR, ".")
            if "" in config_key.split("."):
                # A trailing, doubled, or leading separator. Nothing rejects it later: an empty key
                # is a perfectly good key under a mapping or an undeclared tree, so it would
                # validate, apply garbage at the highest-priority layer, and count as a longer path
                # that discards its own correct prefix.
                self._report_ignored_env_var(
                    env_var_name, raw_value, "its name has an empty path segment, so the configured value stands"
                )
                continue
            value = self._coerce_env_var_value(config_key, raw_value)
            if value is _REJECTED_BAD_VALUE:
                self._report_ignored_env_var(
                    env_var_name,
                    raw_value,
                    f"'{raw_value}' is not a valid value for the '{config_key}' setting, "
                    "so the configured value stands",
                )
                continue
            if value is _REJECTED_UNKNOWN_KEY:
                self._report_ignored_env_var(
                    env_var_name, raw_value, f"there is no '{config_key}' setting, so it sets nothing"
                )
                continue
            coerced.append(
                EnvVarOverride(env_var_name=env_var_name, config_key=config_key, raw_value=raw_value, value=value)
            )

        applicable = []
        for override in coerced:
            deeper_keys = sorted(
                other.config_key for other in coerced if other.config_key.startswith(f"{override.config_key}.")
            )
            if deeper_keys:
                self._report_ignored_env_var(
                    override.env_var_name,
                    override.raw_value,
                    f"'{override.config_key}' is also the start of a longer path "
                    f"({', '.join(deeper_keys)}), which still applies",
                )
                continue
            applicable.append(override)

        return applicable

    def _coerce_env_var_value(self, config_key: str, raw_value: str) -> Any:
        """Coerce one environment variable's string to the type the Settings model declares for it.

        Environment variables are always strings, so a bool, number, or enum setting would otherwise
        reach read sites as text -- and "false" is truthy. Validating a single-key delta against
        Settings (every field has a default, so a partial dict validates) both converts the value and
        contains the damage from a bad one to that key.

        A key the schema does not declare survives validation but not the dump, because a nested
        model ignores sub-keys it does not know. Free-form keys never reach that state: an entry in a
        mapping-valued setting and a path riding Settings' `extra="allow"` are both retained through
        the dump. So an absent key means the path names nothing, as `worker.heartbeat_timeout` does
        by dropping the `_s`.

        Args:
            config_key: The dot-notation key the variable maps to.
            raw_value: The variable's string value.

        Returns:
            The coerced value, _REJECTED_BAD_VALUE when the model rejects the value, or
            _REJECTED_UNKNOWN_KEY when the path names no setting.
        """
        candidate = set_dot_value({}, config_key, raw_value)

        try:
            validated = Settings.model_validate(candidate)
        except ValidationError:
            return _REJECTED_BAD_VALUE

        return get_dot_value(validated.model_dump(), config_key, _REJECTED_UNKNOWN_KEY)

    def _report_ignored_env_var(self, env_var_name: str, raw_value: str, reason: str) -> None:
        """Warn that an environment override was ignored, once per variable and value.

        Args:
            env_var_name: The full environment variable name.
            raw_value: The value it carried.
            reason: Why it was ignored and what happens instead, phrased to complete
                "Ignoring <var>: <reason>.". The consequence differs per reason, so it belongs
                here rather than in a shared closing sentence: a dropped prefix is not falling
                back to anything, since the longer path that displaced it still writes underneath.
        """
        report_key = (env_var_name, raw_value)
        if report_key in self._reported_invalid_env_vars:
            return

        self._reported_invalid_env_vars.add(report_key)
        logger.warning("Ignoring environment variable %s: %s.", env_var_name, reason)

    def _load_config_from_file(self, path: Path, label: str) -> LoadedConfigFile:
        """Read and parse a JSON config file.

        A parse failure is logged at ERROR and also returned as `parse_error`, which lets a
        caller (currently only `_load_file_layer`, for the three live layers) attribute the
        failure to a specific layer for `config_layers()`.
        """
        if not path.exists():
            logger.debug("No %s config file loaded", label)
            return LoadedConfigFile(contents={}, parse_error=None)
        try:
            return LoadedConfigFile(contents=json.loads(path.read_text(encoding="utf-8")), parse_error=None)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            error = f"{type(e).__name__}: {e}"
            logger.error("Error parsing %s config file: %s", label, e)
            return LoadedConfigFile(contents={}, parse_error=error)

    def _load_file_layer(self, layer: ConfigLayerName, path: Path | None, label: str) -> dict:
        """Load one file-backed config layer and record its parse error under `layer`.

        A None `path` means the layer has nothing to contribute -- no project is active, or
        a workspace layer that resolves to the project's own file. That is not an error: the
        layer loads empty and its recorded parse error is cleared, so a layer that failed to
        parse for a previously active project cannot linger past the switch.

        Args:
            layer: Which layer this is, used as the parse-error key `config_layers()` reads.
            path: The file backing this layer, or None when it has none.
            label: Human-readable layer name for the log lines.

        Returns:
            This layer's own parsed contents, unmerged. `{}` when there is no file, the file
            is missing, or it failed to parse.
        """
        if path is None:
            self._layer_parse_errors[layer] = None
            return {}

        loaded = self._load_config_from_file(path, label)
        self._layer_parse_errors[layer] = loaded.parse_error
        return loaded.contents

    def load_configs(self) -> None:
        """Load and merge configs from all sources in priority order.

        Sets default_config, user_config, project_config, workspace_config, env_config,
        and merged_config attributes. Priority order (later entries win):
        defaults → user → project-adjacent → workspace → env vars.
        """
        self.default_config = Settings().model_dump()
        merged_config = self.default_config

        self.user_config = self._load_file_layer("user", USER_CONFIG_PATH, "user")
        merged_config = merge_dicts(merged_config, self.user_config)

        self.project_config = self._load_file_layer("project", self._project_config_path, "project-adjacent")
        merged_config = merge_dicts(merged_config, self.project_config)

        # The workspace layer loads nothing when it resolves to the project's own file; see
        # _workspace_layer_path.
        self.workspace_config = self._load_file_layer("workspace", self._workspace_layer_path(), "workspace")
        merged_config = merge_dicts(merged_config, self.workspace_config)

        # Apply runtime workspace override (from ProjectManager's project_workspaces lookup
        # or auto-default-to-project-dir). Sits above config files but below env vars.
        if self._workspace_dir_override is not None:
            merged_config["workspace_directory"] = self._workspace_dir_override

        self.env_config = self._load_config_from_env_vars()
        if self.env_config:
            merged_config = merge_dicts(merged_config, self.env_config)
            logger.debug("Merged config from environment variables: %s", list(self.env_config.keys()))

        # Re-assign workspace path in case env var or project config overrides it. Uses the shared
        # precedence resolver (WITH the runtime override) so the active workspace and
        # configured_global_workspace_path() can never disagree on layer ordering.
        self.workspace_path = self._resolve_configured_workspace_directory(include_runtime_override=True)

        # Validate the full config against the Settings model.
        try:
            Settings.model_validate(merged_config)
            self.merged_config = merged_config
            self._merged_config_rejection = None
        except ValidationError as e:
            logger.error("Error validating config file: %s", e)
            self.merged_config = self.default_config
            self._merged_config_rejection = _MergedConfigRejection(keys=_offending_keys(e))

    def load_project_config(self, project_dir: Path) -> None:
        """Load the project-adjacent config from the given project directory and remerge all configs.

        Reads griptape_nodes_config.json from project_dir (if it exists) and stores it as
        the project_config layer. Then rebuilds the merged config with the updated layer order:
        default → user → project_config → workspace_config → env vars.

        Args:
            project_dir: Directory containing the project YAML file. Looks for
                griptape_nodes_config.json in this directory.
        """
        self._project_config_path = project_dir / "griptape_nodes_config.json"
        self.load_configs()

    def load_workspace_config(self, workspace_dir: Path) -> None:
        """Load the workspace config from the given workspace directory and remerge all configs.

        Reads griptape_nodes_config.json from workspace_dir (if it exists) and stores it as
        the workspace_config layer. When workspace_dir matches the project directory, the file
        is the same as the project-adjacent config and the workspace layer is skipped to avoid
        loading it twice. Rebuilds the merged config with the updated layer order:
        default → user → project_config → workspace_config → env vars.

        Args:
            workspace_dir: The resolved workspace directory. Looks for
                griptape_nodes_config.json in this directory.
        """
        self._workspace_config_path = workspace_dir / "griptape_nodes_config.json"
        self.load_configs()

    def compute_project_provisioning_config(
        self, project_dir: Path, workspace_dir: Path, *, apply_override: bool
    ) -> dict:
        """Return the merged config a project WOULD activate with, mutating nothing.

        Mirrors load_configs()'s layer order (defaults -> user -> project-adjacent ->
        workspace -> workspace override -> env vars) for the given project and
        workspace directories, reading files fresh into a local dict. The
        provisioning preview uses this so its plan reflects the same effective
        `libraries_to_register` / `requires_engine` that _reconcile_libraries_from_config
        reads from the live merged config after activation - instead of the
        project-adjacent file alone, which diverges when a higher-priority layer
        (a separate-dir workspace config, env vars, or the user config) sets those keys.

        `workspace_dir` and `apply_override` come from ProjectManager.decide_workspace,
        the same decision the live activation applies. The override is applied here only
        when `apply_override` is True (the project_workspaces mapping, parent-chain
        inheritance, and global-default branches), exactly as _activate_project calls
        set_workspace_override; for an env/project-adjacent workspace_directory it is False
        so the workspace config layer can re-point workspace_directory, matching the live
        path. When applied, the value is resolved the same way set_workspace_override
        resolves it (expanduser + resolve), so the merged workspace_directory matches the
        live merged config byte-for-byte.

        Args:
            project_dir: Directory holding the project YAML and its adjacent config.
            workspace_dir: The resolved workspace directory for this project.
            apply_override: Whether activation would pin workspace_directory to
                workspace_dir via set_workspace_override.
        """
        merged = Settings().model_dump()

        if USER_CONFIG_PATH.exists():
            user_config = self._load_config_from_file(USER_CONFIG_PATH, "user").contents
            merged = merge_dicts(merged, user_config)

        project_config_path = project_dir / "griptape_nodes_config.json"
        project_config = self._load_config_from_file(project_config_path, "project-adjacent").contents
        merged = merge_dicts(merged, project_config)

        # Skip the workspace layer when it resolves to the project-adjacent file
        # (workspace dir == project dir for self-contained projects), matching load_configs.
        workspace_config_path = workspace_dir / "griptape_nodes_config.json"
        if workspace_config_path != project_config_path:
            workspace_config = self._load_config_from_file(workspace_config_path, "workspace").contents
            merged = merge_dicts(merged, workspace_config)

        # Apply the runtime workspace override conditionally, mirroring _activate_project:
        # only the project_workspaces, parent-chain inheritance, and global-default branches
        # pin it (apply_override), and the value is resolved exactly as set_workspace_override
        # would so preview and live agree. It sits above config files but below env vars.
        if apply_override:
            merged["workspace_directory"] = str(Path(workspace_dir).expanduser().resolve())

        env_config = self._load_config_from_env_vars()
        if env_config:
            merged = merge_dicts(merged, env_config)

        return merged

    def compute_system_defaults_provisioning_config(self) -> dict:
        """Return the merged config system defaults WOULD activate with, mutating nothing.

        Mirrors what _activate_project does for SYSTEM_DEFAULTS_KEY: clear_project_layers()
        drops the project-adjacent and workspace config-file layers and the workspace
        override, then load_configs() merges defaults -> user -> env vars only. The
        provisioning preview uses this so a switch to "Default Project" shows the same
        `libraries_to_register` / `requires_engine` that _reconcile_libraries_from_config
        reads from the live merged config after activation. Unlike
        compute_project_provisioning_config, it reads no project-adjacent or workspace
        griptape_nodes_config.json, because the system-defaults activation path reads
        neither.
        """
        merged = Settings().model_dump()

        if USER_CONFIG_PATH.exists():
            user_config = self._load_config_from_file(USER_CONFIG_PATH, "user").contents
            merged = merge_dicts(merged, user_config)

        env_config = self._load_config_from_env_vars()
        if env_config:
            merged = merge_dicts(merged, env_config)

        return merged

    def reset_user_config(self) -> None:
        """Reset the user configuration to the default values.

        An exception is made for `workflows_to_register` since resetting it gives the appearance of the user losing their workflows.
        """
        # TODO: https://github.com/griptape-ai/griptape-nodes/issues/1241 need a better way to annotate fields to ignore.
        workflows_to_register = self.get_config_value(WORKFLOWS_TO_REGISTER_KEY)
        USER_CONFIG_PATH.write_text(
            json.dumps(
                {
                    "app_events": {
                        "on_app_initialization_complete": {
                            "workflows_to_register": workflows_to_register,
                        }
                    }
                },
                indent=2,
            )
        )
        self._workspace_dir_override = None
        self._workspace_pin_supplied_by_config = False
        self._libraries_root_override = None
        self.load_configs()

    def delete_user_workflow(self, workflow_file_name: str) -> None:
        default_workflows = self.get_config_value(WORKFLOWS_TO_REGISTER_KEY)
        if default_workflows:
            default_workflows = [
                saved_workflow
                for saved_workflow in default_workflows
                if (saved_workflow.lower() != workflow_file_name.lower())
            ]
            self.set_config_value(WORKFLOWS_TO_REGISTER_KEY, default_workflows)

    def get_full_path(self, relative_path: str) -> Path:
        """Get a full path by combining the base path with a relative path.

        Args:
            relative_path: A path relative to the base path.

        Returns:
            Path object representing the full path.
        """
        workspace_path = self.workspace_path
        return workspace_path / relative_path

    def _coerce_to_type(self, value: Any, cast_type: type) -> Any:
        """Coerce a value to the specified type.

        This is particularly useful for environment variables which are always strings.

        Args:
            value: The value to coerce.
            cast_type: The type to coerce to (bool, int, float, or str).

        Returns:
            The coerced value.
        """
        if cast_type is bool:
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                return value.lower() not in ("false", "0", "no", "")
            return bool(value)
        if cast_type is int:
            return int(value)
        if cast_type is float:
            return float(value)
        # str is a no-op
        return value

    def get_config_value(
        self,
        key: str,
        *,
        should_load_env_var_if_detected: bool = True,
        config_source: Literal[
            "user_config", "project_config", "workspace_config", "default_config", "merged_config"
        ] = "merged_config",
        default: Any | None = None,
        cast_type: type[bool] | type[int] | type[float] | type[str] | None = None,
    ) -> Any:
        """Get a value from the configuration.

        If `should_load_env_var_if_detected` is True (default), and the value starts with a $, it will be pulled from the environment variables.

        Args:
            key: The configuration key to get. Can use dot notation for nested keys (e.g., 'category.subcategory.key').
                 If the key refers to a category (dictionary), returns the entire category.
            should_load_env_var_if_detected: If True, and the value starts with a $, it will be pulled from the environment variables.
            config_source: The source of the configuration to use. Can be 'user_config', 'project_config', 'default_config', or 'merged_config'.
            default: The default value to return if the key is not found in the configuration.
            cast_type: Optional type to coerce the value to (bool, int, float, or str). Useful for environment
                       variables which are always strings (e.g., "false" -> False when cast_type=bool).

        Returns:
            The value associated with the key, or the entire category if key points to a dict.
        """
        config_source_map = {
            "user_config": self.user_config,
            "project_config": self.project_config,
            "workspace_config": self.workspace_config,
            "merged_config": self.merged_config,
            "default_config": self.default_config,
        }
        config = config_source_map.get(config_source, self.merged_config)
        value = get_dot_value(config, key, default)

        if value is None:
            msg = f"Config key '{key}' not found in config file."
            logger.debug(msg)
            return None

        if should_load_env_var_if_detected and isinstance(value, str) and value.startswith("$"):
            value = self.engine.secrets_manager.get_secret(value[1:])

        if cast_type is not None:
            value = self._coerce_to_type(value, cast_type)

        return value

    def read_config_file(self, path: Path) -> dict:
        """Read and parse a single JSON config file in isolation, mutating nothing.

        Returns the raw parsed dict (empty when the file is missing or unparsable),
        without merging it into the live config layers. Used to inspect a
        project-adjacent config (e.g. for a provisioning preview or a read-only
        workspace-dir decision) for a project other than the active one.

        Args:
            path: The config file to read.
        """
        return self._load_config_from_file(path, label=str(path)).contents

    def read_env_config(self) -> dict[str, Any]:
        """Return the config layer derived from GTN_CONFIG_ environment variables, mutating nothing.

        Public read-only view of the env-var layer for callers (e.g. a provisioning
        preview's read-only workspace-dir decision) that need to inspect it without
        triggering a full load_configs().
        """
        return self._load_config_from_env_vars()

    def read_config_file_value(self, path: Path, key: str, *, default: Any | None = None) -> Any:
        """Read a single dot-notation key from a config file without merging it into the live config.

        Reads and parses the JSON at `path` in isolation, then pulls `key` from it.
        Used to inspect a project-adjacent config (e.g. for a provisioning preview)
        without disturbing the active config layers. Returns `default` when the
        file is missing/unparsable or the key is absent.

        Args:
            path: The config file to read.
            key: Dot-notation key (e.g. 'category.subcategory.key').
            default: Value to return when the key is not present.
        """
        config = self.read_config_file(path)
        return get_dot_value(config, key, default)

    def set_config_value(self, key: str, value: Any, *, should_set_env_var_if_detected: bool = True) -> bool:
        """Set a value in the configuration.

        Args:
            key: The configuration key to set. Can use dot notation for nested keys (e.g., 'category.subcategory.key').
            value: The value to associate with the key.
            should_set_env_var_if_detected: If True, and the value starts with a $, it will be set in the environment variables.

        Returns:
            True if the change was persisted to disk; False if the underlying
            ``_write_user_config_delta`` call failed. Callers that surface a
            result payload to a request handler should propagate the failure
            instead of reporting success on a stale write.
        """
        # Capture old value before making changes (for event emission)
        old_value = self.get_config_value(key, should_load_env_var_if_detected=False)

        delta = set_dot_value({}, key, value)
        if key == "log_level":
            self._set_log_level(value)
        elif key == "workspace_directory":
            self.workspace_path = value
        self.user_config = merge_dicts(self.merged_config, delta)
        write_succeeded = self._write_user_config_delta(delta)

        if should_set_env_var_if_detected and isinstance(value, str) and value.startswith("$"):
            value = self.engine.secrets_manager.set_secret(value[1:], "")

        # We need to fully reload the user config because we need to regenerate the merged config.
        # Also eventually need to reload registered workflows.
        # TODO: https://github.com/griptape-ai/griptape-nodes/issues/437
        self.load_configs()
        logger.debug("Config value '%s' set to '%s'", key, value)

        # Broadcast a domain event on success only. Listeners (in production:
        # WorkerManager) take it from here -- this manager has no knowledge of
        # who consumes the event. Failed writes are logged inside
        # ``_write_user_config_delta``; no event fires so listeners cannot act
        # on a state that does not exist on disk.
        if write_succeeded and self._event_manager is not None:
            event = ConfigChanged(key=key, old_value=old_value, new_value=value)
            self._event_manager.broadcast_app_event(event)

        return write_succeeded

    def on_handle_get_config_category_request(self, request: GetConfigCategoryRequest) -> ResultPayload:
        if request.category is None or request.category == "":
            # Return the whole shebang. Start with the defaults and then layer on the user config.
            contents = self.merged_config
            result_details = "Successfully returned the entire config dictionary."
            return GetConfigCategoryResultSuccess(
                contents=contents, sources=self.category_sources(None), result_details=result_details
            )

        # See if we got something valid.
        find_results = self.get_config_value(request.category)
        if find_results is None:
            result_details = f"Attempted to get config details for category '{request.category}'. Failed because no such category could be found."
            return GetConfigCategoryResultFailure(result_details=result_details)

        if not isinstance(find_results, dict):
            result_details = f"Attempted to get config details for category '{request.category}'. Failed because this was was not a dictionary."
            return GetConfigCategoryResultFailure(result_details=result_details)

        result_details = f"Successfully returned the config dictionary for section '{request.category}'."
        return GetConfigCategoryResultSuccess(
            contents=find_results,
            sources=self.category_sources(request.category),
            result_details=result_details,
        )

    def on_handle_set_config_category_request(self, request: SetConfigCategoryRequest) -> ResultPayload:
        # Validate the value is a dict
        if not isinstance(request.contents, dict):
            result_details = f"Attempted to set config details for category '{request.category}'. Failed because the contents provided were not a dictionary."
            return SetConfigCategoryResultFailure(result_details=result_details)

        # Get old value before changing for event emission
        old_value = None
        if request.category and request.category != "":
            old_value = self.get_config_value(request.category)

        if request.category is None or request.category == "":
            # Assign the whole shebang.
            write_succeeded = self._write_user_config_delta(request.contents)
            if not write_succeeded:
                result_details = (
                    "Attempted to assign the entire config dictionary. Failed because the user config "
                    "file could not be written; see prior logs for the underlying I/O error."
                )
                return SetConfigCategoryResultFailure(result_details=result_details)

            result_details = "Successfully assigned the entire config dictionary."

            # Domain event on success only -- listeners (e.g. WorkerManager)
            # decide what to do with it.
            if self._event_manager is not None:
                event = ConfigChanged(
                    key="",
                    old_value=old_value,
                    new_value=request.contents,
                )
                self._event_manager.broadcast_app_event(event)

            # A full-config replacement has no single dot-key to check for shadowing: it
            # rewrites the whole user layer at once. applied/effective_value/shadowed_by
            # are only meaningful for a single key (see the non-empty-category branch
            # below), so they are left at their neutral defaults here.
            return SetConfigCategoryResultSuccess(result_details=result_details)

        write_succeeded = self.set_config_value(key=request.category, value=request.contents)
        if not write_succeeded:
            result_details = (
                f"Attempted to set config category '{request.category}'. Failed because the user config "
                "file could not be written; see prior logs for the underlying I/O error."
            )
            return SetConfigCategoryResultFailure(result_details=result_details)

        outcome = self._write_outcome(request.category, request.contents)

        result_details = f"Successfully assigned the config dictionary for section '{request.category}'."

        return SetConfigCategoryResultSuccess(
            result_details=self._append_shadow_note(result_details, outcome),
            applied=outcome.applied,
            effective_value=outcome.effective_value,
            shadowed_by=outcome.shadowed_by,
            reason=outcome.reason,
        )

    def on_handle_get_config_value_request(self, request: GetConfigValueRequest) -> ResultPayload:
        if request.category_and_key == "":
            result_details = "Attempted to get config value but no category or key was specified."
            return GetConfigValueResultFailure(result_details=result_details)

        # See if we got something valid.
        find_results = self.get_config_value(request.category_and_key)
        if find_results is None:
            result_details = f"Attempted to get config value for category.key '{request.category_and_key}'. Failed because no such category.key could be found."
            return GetConfigValueResultFailure(result_details=result_details)

        source = self.value_source(request.category_and_key)
        result_details = f"Successfully returned the config value for section '{request.category_and_key}'."
        return GetConfigValueResultSuccess(
            value=find_results,
            source=source,
            editable=self._first_shadowed_leaf(request.category_and_key, find_results) is None,
            result_details=result_details,
        )

    def on_handle_get_config_path_request(self, request: GetConfigPathRequest) -> ResultPayload:  # noqa: ARG002
        result_details = "Successfully returned the config path."
        return GetConfigPathResultSuccess(config_path=str(USER_CONFIG_PATH), result_details=result_details)

    def on_handle_get_config_layers_request(self, request: GetConfigLayersRequest) -> ResultPayload:  # noqa: ARG002
        result_details = "Successfully returned the config layer stack."
        return GetConfigLayersResultSuccess(layers=self.config_layers(), result_details=result_details)

    def on_handle_get_workspace_request(self, request: GetWorkspaceRequest) -> ResultPayload:  # noqa: ARG002
        result_details = "Successfully returned the absolute workspace path."
        return GetWorkspaceResultSuccess(workspace_path=str(self.workspace_path), result_details=result_details)

    def on_handle_get_config_schema_request(self, request: GetConfigSchemaRequest) -> ResultPayload:  # noqa: ARG002
        """Handle request to get the configuration schema with current values and library settings.

        This method returns a clean structure with four main components:
        1. base_schema: Core settings schema from Pydantic Settings model with categories
        2. library_schemas: Library-specific schemas from definition files (preserves enums)
        3. artifact_schemas: Dynamically generated artifact provider schemas (enums, types, defaults)
        4. current_values: All current configuration values from merged config

        The approach separates concerns for frontend flexibility and simplicity.
        Library settings with explicit schemas (including enums) are preserved, while
        libraries without schemas get simple object types.
        """
        try:
            # Get base settings schema and current values
            base_schema = Settings.model_json_schema()
            current_values = self.merged_config.copy()

            # Get library schemas
            library_schemas = LibraryRegistry.get_all_library_schemas()

            # Get artifact schemas (dynamically generated from registered providers/generators)
            schemas_request = GetArtifactSchemasRequest()
            schemas_result = self.engine.handle_request(schemas_request)

            if not isinstance(schemas_result, GetArtifactSchemasResultSuccess):
                result_details = f"Failed to retrieve artifact schemas: {schemas_result.result_details}"
                return GetConfigSchemaResultFailure(result_details=result_details)

            artifact_schemas = schemas_result.schemas

            # Return clean structure
            schema_with_defaults = {
                "base_schema": base_schema,
                "library_schemas": library_schemas,
                "artifact_schemas": artifact_schemas,
                "current_values": current_values,
            }

            result_details = "Successfully returned the configuration schema with default values, library settings, and artifact schemas."
            return GetConfigSchemaResultSuccess(schema=schema_with_defaults, result_details=result_details)
        except Exception as e:
            result_details = f"Failed to generate configuration schema: {e}"
            return GetConfigSchemaResultFailure(result_details=result_details)

    def on_handle_reset_config_request(self, request: ResetConfigRequest) -> ResultPayload:  # noqa: ARG002
        try:
            self.reset_user_config()
            self._set_log_level(str(self.merged_config["log_level"]))

            result_details = "Successfully reset user configuration."
            # Reset is a full replacement; emit the same shape of ConfigChanged
            # that ``on_handle_set_config_category_request`` does for category=None,
            # so listeners cannot tell the two paths apart.
            if self._event_manager is not None:
                event = ConfigChanged(key="", old_value=None, new_value=self.merged_config)
                self._event_manager.broadcast_app_event(event)
            return ResetConfigResultSuccess(result_details=result_details)
        except Exception as e:
            result_details = f"Attempted to reset user configuration but failed: {e}."
            return ResetConfigResultFailure(result_details=result_details)

    def _get_diff(self, old_value: Any, new_value: Any) -> dict[Any, Any]:
        """Generate a diff between the old and new values."""
        if isinstance(old_value, dict) and isinstance(new_value, dict):
            diff = {
                key: (old_value.get(key), new_value.get(key))
                for key in new_value
                if old_value.get(key) != new_value.get(key)
            }
        elif isinstance(old_value, list) and isinstance(new_value, list):
            diff = {
                str(i): (old, new) for i, (old, new) in enumerate(zip(old_value, new_value, strict=False)) if old != new
            }

            # Handle added or removed elements
            if len(old_value) > len(new_value):
                for i in range(len(new_value), len(old_value)):
                    diff[str(i)] = (old_value[i], None)
            elif len(new_value) > len(old_value):
                for i in range(len(old_value), len(new_value)):
                    diff[str(i)] = (None, new_value[i])
        else:
            diff = {"old": old_value, "new": new_value}
        return diff

    def _format_diff(self, diff: dict[Any, Any]) -> str:
        """Format the diff dictionary into a readable string."""
        formatted_lines = []
        for key, (old, new) in diff.items():
            if old is None:
                formatted_lines.append(f"[{key}]: ADDED: '{new}'")
            elif new is None:
                formatted_lines.append(f"[{key}]: REMOVED: '{old}'")
            else:
                formatted_lines.append(f"[{key}]:\n\tFROM: '{old}'\n\t  TO: '{new}'")
        return "\n".join(formatted_lines)

    def _write_outcome(self, key: str, value: Any) -> ConfigWriteOutcome:
        """Report whether a just-completed user-layer write is the value now in effect.

        Called by the `Set*` handlers once the write is known to have reached disk. A write
        lands in the user layer but can still fail to take effect three ways, checked in this
        order because the earlier ones are the more specific explanation:

        1. A higher-priority layer (project, workspace, runtime, env) also defines what was
           written and keeps winning the merge. `shadowed_by` names it.
        2. The open project pins `workspace_directory` to the value the config stack supplied
           it, so every remerge restores that value. The write decides what the next activation
           pins rather than what is in effect now.
        3. The merged result failed `Settings` validation, so `load_configs` discarded it and
           fell back to defaults.

        Only the first names a layer; `reason` distinguishes all three so the note can say
        something true about each.

        Ownership is judged before values are compared, because writing the value a higher
        layer already holds is still not a write that took effect: the user's copy is inert and
        the next remerge keeps reporting the other layer's. Comparing values alone would call
        that success.

        Judged on the keys the caller actually wrote, which for a dict `value` means its
        leaves rather than `key` itself -- writing `{"port": 8080}` to category "nuke" is
        unaffected by a project layer that defines only "nuke.executable".

        Reads the value back without env-var `$`-expansion, matching how the handlers read
        `old_value`, so the reported value is the raw stored one rather than a resolved secret.

        Args:
            key: Dot-notation key that was written.
            value: The value written to `key`, descended into when it is a dict.
        """
        effective_value = self.get_config_value(key, should_load_env_var_if_detected=False)
        unapplied_leaf = self._first_unapplied_leaf(key, value)
        if unapplied_leaf is None:
            return ConfigWriteOutcome(
                applied=True, effective_value=effective_value, unapplied_key=None, shadowed_by=None
            )
        return ConfigWriteOutcome(
            applied=False,
            effective_value=effective_value,
            unapplied_key=unapplied_leaf.key,
            shadowed_by=unapplied_leaf.source,
            reason=unapplied_leaf.reason,
        )

    def _first_unapplied_leaf(self, key: str, value: Any) -> _UnappliedLeaf | None:
        """Return the first written key whose value is not the one now in effect, or None.

        Shadowing is reported in preference to divergence: when a higher layer owns the key,
        that layer is the actionable explanation, and the two causes overlap in the common case.

        Args:
            key: Dot-notation key `value` was written to.
            value: The written value, descended into when it is a dict.
        """
        root = tuple(key.split("."))

        shadowed_leaf = self._first_shadowed_leaf_at(root, value)
        if shadowed_leaf is not None:
            return _UnappliedLeaf(key=shadowed_leaf.key, reason="shadowed", source=shadowed_leaf.source)

        diverged_path = self._first_diverged_leaf_at(root, value)
        if diverged_path is not None:
            return _UnappliedLeaf(key=".".join(diverged_path), reason=self._divergence_reason(diverged_path))

        return None

    def _divergence_reason(self, path: tuple[str, ...]) -> ConfigWriteUnappliedReason:
        """Why `path`'s merged value differs from what was written, when no layer shadows it.

        A discarded merge is asked about first, because it explains every divergence and the pin
        explains none of them while it holds: `load_configs` replaced the merge with defaults, so
        the pin is not in `merged_config` at all. Deciding pin-versus-discard by comparing the
        merged value against the pin cannot separate the two, since branch 5 derives the pin from
        the `default` layer whenever the user config has no `workspace_directory` -- the ordinary
        state after a `gtn init` without `--workspace-directory` -- and a discarded merge IS the
        default layer, so the two compare equal.

        Args:
            path: Key segments of the leaf whose merged value diverged.
        """
        if self._merged_config_rejection is not None:
            return "rejected"
        if self._is_pinned_by_workspace_override(path):
            return "pinned"
        # Unreachable: with a valid merge and nothing shadowing, the written value is the merged
        # value unless the pin replaced it. `_append_shadow_note` still words this honestly.
        return "rejected"

    def _is_pinned_by_workspace_override(self, path: tuple[str, ...]) -> bool:
        """Whether the runtime workspace pin is what keeps `path` from taking effect.

        Only `workspace_directory` can be pinned, and only a config-supplied pin can reach here:
        a pin with no config origin is reported as the "runtime" layer, so it is caught as
        shadowing before divergence is ever checked. Both preconditions are asserted rather than
        assumed, so this stays correct if the caller order changes.

        No value comparison is needed: the caller has already excluded a discarded merge, and
        while the merge stands `load_configs` assigns the pin over the merged value, so a held
        config-supplied pin is necessarily the value in effect.

        Args:
            path: Key segments of the leaf whose merged value diverged.
        """
        if path != ("workspace_directory",):
            return False
        return self._workspace_dir_override is not None and self._workspace_pin_supplied_by_config

    def _first_shadowed_leaf(self, key: str, value: Any) -> _ShadowedLeaf | None:
        """Return the first written key a higher-priority layer shadows, or None if none is.

        Args:
            key: Dot-notation key `value` was written to.
            value: The written value.
        """
        return self._first_shadowed_leaf_at(tuple(key.split(".")), value)

    def _first_shadowed_leaf_at(self, path: tuple[str, ...], value: Any) -> _ShadowedLeaf | None:
        """`_first_shadowed_leaf` for an already-split key path.

        A dict `value` is descended into so each leaf is judged on its own path, matching the
        leaf-level granularity `category_sources` reports on the read side. A non-dict value --
        and an empty dict, which names no leaves -- is one leaf: `path` itself. Descending
        extends the path by a segment rather than joining into a dot string, so a leaf whose
        own name contains a dot is still probed at the level it actually sits on.

        Also used by the read path to decide whether a whole category is editable, where
        `value` is the category's current contents rather than something being written.

        "First" is in the caller's own key order, so the reported key is the one nearest the
        top of what they wrote. One shadowed leaf is enough to make the write's outcome
        partial, so this stops at the first rather than collecting all of them.

        Args:
            path: Key segments `value` was written to.
            value: The written value.
        """
        if not isinstance(value, dict) or not value:
            shadowed = self._shadowed_by_at(path)
            if shadowed is None:
                return None
            return _ShadowedLeaf(key=".".join(path), source=shadowed)

        for leaf_name, leaf_value in value.items():
            shadowed_leaf = self._first_shadowed_leaf_at((*path, leaf_name), leaf_value)
            if shadowed_leaf is not None:
                return shadowed_leaf
        return None

    def _first_diverged_leaf_at(self, path: tuple[str, ...], value: Any) -> tuple[str, ...] | None:
        """Return the path of the first written leaf whose merged value differs from what was written.

        Catches the write no layer shadows but that still did not land, meaning `Settings`
        validation rejected the merged result and `load_configs` fell back to defaults. Reads
        each leaf back on its own rather than comparing whole dicts, because a category's
        merged value legitimately holds keys the caller never wrote.

        An empty dict returns None: a write that names no leaves cannot have diverged, and
        `merge_dicts` leaves the category untouched. `_first_shadowed_leaf_at` treats the same
        input as one leaf, because writing nothing into a category a higher layer owns is still
        a write that layer outranks.

        Args:
            path: Key segments `value` was written to.
            value: The written value, descended into when it is a dict.
        """
        if isinstance(value, dict) and not value:
            return None

        if not isinstance(value, dict):
            if self._layer_value_at(self.merged_config, path) == value:
                return None
            return path

        for leaf_name, leaf_value in value.items():
            diverged_path = self._first_diverged_leaf_at((*path, leaf_name), leaf_value)
            if diverged_path is not None:
                return diverged_path
        return None

    def _append_shadow_note(self, result_details: str, outcome: ConfigWriteOutcome) -> str:
        """Return `result_details` with an explanation appended when the write did not take effect.

        Returned unchanged when the write applied. Otherwise the message a user sees (in a
        toast, a log, a CLI response) names the key that did not change and says why, instead
        of reporting a bare success. Naming the key matters for a category write, where the
        rest of the category may well have taken effect.

        Args:
            result_details: The success message describing the write.
            outcome: The outcome from `_write_outcome`.
        """
        if outcome.applied:
            return result_details

        if outcome.reason == "pinned":
            return (
                f"{result_details} NOTE: '{outcome.unapplied_key}' is still "
                f"'{outcome.effective_value}' because the open project pins its workspace. The "
                "new value is saved and takes effect the next time a project is opened."
            )

        if outcome.reason == "rejected":
            return self._rejection_note(result_details, outcome)

        if outcome.reason == "shadowed" and outcome.shadowed_by is not None:
            location = outcome.shadowed_by.path or outcome.shadowed_by.env_var or "an unknown source"
            return (
                f"{result_details} NOTE: '{outcome.unapplied_key}' is supplied by a higher-priority "
                f"'{outcome.shadowed_by.layer}' layer ({location}), so that value does not change "
                "until that layer does."
            )

        # Defensive tail: every reason above is accounted for, so this is unreachable today. It
        # still says the write did not land, because a future reason arriving here silently
        # unexplained would be the same bare success this reporting exists to remove.
        return (
            f"{result_details} NOTE: '{outcome.unapplied_key}' is still "
            f"'{outcome.effective_value}'. The value was saved but did not take effect, for a "
            "reason the engine did not record."
        )

    def _rejection_note(self, result_details: str, outcome: ConfigWriteOutcome) -> str:
        """Explain a write that reached disk while the merged config was failing validation.

        The broken setting is frequently NOT the one just written: one invalid value anywhere
        makes `load_configs` discard the entire merge, so an unrelated write reads back as
        unapplied. Blaming the written value sends the user to re-type a value that was fine.

        Says the merge fell back to built-in defaults rather than that the previous configuration
        was kept, because that is what happens: every other user, project and workspace setting
        stops applying for the session while still sitting on disk.

        Args:
            result_details: The success message describing the write.
            outcome: The outcome from `_write_outcome`.
        """
        rejection = self._merged_config_rejection
        if rejection is None:
            return (
                f"{result_details} NOTE: '{outcome.unapplied_key}' is still "
                f"'{outcome.effective_value}'. The value was saved but did not take effect, for a "
                "reason the engine did not record."
            )

        if outcome.unapplied_key in rejection.keys:
            blame = f"The value saved for '{outcome.unapplied_key}' is not one this setting accepts"
        elif rejection.keys:
            named = ", ".join(f"'{key}'" for key in rejection.keys)
            blame = f"The value saved for {named} is not valid, which is a different setting"
        else:
            blame = "The saved configuration is not valid"

        return (
            f"{result_details} NOTE: '{outcome.unapplied_key}' is still "
            f"'{outcome.effective_value}'. {blame}, so the engine could not apply the "
            "configuration and fell back to built-in defaults for every setting until it is fixed."
        )

    def on_handle_set_config_value_request(self, request: SetConfigValueRequest) -> ResultPayload:
        if request.category_and_key == "":
            result_details = "Attempted to set config value but no category or key was specified."
            return SetConfigValueResultFailure(result_details=result_details)

        # Fetch the existing value (don't go to the env vars directly; we want the key)
        old_value = self.get_config_value(request.category_and_key, should_load_env_var_if_detected=False)

        # Make a copy of the existing value if it is a dict or list
        if isinstance(old_value, (dict, list)):
            old_value_copy = copy.deepcopy(old_value)
        else:
            old_value_copy = old_value

        # Set the new value
        write_succeeded = self.set_config_value(key=request.category_and_key, value=request.value)
        if not write_succeeded:
            result_details = (
                f"Attempted to set config value '{request.category_and_key}'. Failed because the user "
                "config file could not be written; see prior logs for the underlying I/O error."
            )
            return SetConfigValueResultFailure(result_details=result_details)

        # The write landed in the user layer; whether it's actually the value now in
        # effect depends on whether a higher-priority layer (project/workspace/env) also
        # defines what was written.
        outcome = self._write_outcome(request.category_and_key, request.value)

        # For container types, indicate the change with a diff
        if isinstance(request.value, (dict, list)):
            if old_value_copy is not None:
                diff = self._get_diff(old_value_copy, request.value)
                formatted_diff = self._format_diff(diff)
                if formatted_diff:
                    result_details = f"Successfully updated {type(request.value).__name__} at '{request.category_and_key}'. Changes:\n{formatted_diff}"
                else:
                    result_details = f"Successfully updated {type(request.value).__name__} at '{request.category_and_key}'. No changes detected."
            else:
                result_details = f"Successfully updated {type(request.value).__name__} at '{request.category_and_key}'"
        else:
            result_details = f"Successfully assigned the config value for '{request.category_and_key}':\n\tFROM '{old_value_copy}'\n\tTO: '{request.value}'"

        return SetConfigValueResultSuccess(
            result_details=self._append_shadow_note(result_details, outcome),
            applied=outcome.applied,
            effective_value=outcome.effective_value,
            shadowed_by=outcome.shadowed_by,
            reason=outcome.reason,
        )

    def _write_user_config_delta(self, user_config_delta: dict) -> bool:  # noqa: C901, PLR0911, PLR0912, PLR0915
        """Write user configuration delta to config file with atomic read-modify-write.

        This method performs an atomic read-modify-write operation on the user config file:
        1. Checks if config file exists, creates if missing
        2. Reads current config with file locking (prevents concurrent write corruption)
        3. Merges the delta with current config
        4. Writes merged config back with file locking
        5. Reloads all configs to reflect changes

        Uses OSManager request types (GetFileInfoRequest, ReadFileRequest, WriteFileRequest)
        for centralized file I/O with automatic file locking, structured error handling,
        and audit trail capabilities.

        Args:
            user_config_delta: Configuration changes to merge with existing config.
                              Uses dot notation keys (e.g., {"nodes.max_depth": 10})

        Returns:
            True if the merged config was written to disk; False if any step
            (file info, create, read, write) failed. Callers must gate
            worker fan-out on this so workers don't reload from a file that
            wasn't actually updated.
        """
        os_manager = self.engine.os_manager
        config_path_str = str(USER_CONFIG_PATH)

        # Step 1: Check if config file exists
        info_request = GetFileInfoRequest(path=config_path_str, workspace_only=False)
        info_result = os_manager.on_get_file_info_request(info_request)

        # Handle failures getting file info
        if isinstance(info_result, GetFileInfoResultFailure):
            logger.error(
                "Attempted to check if user config exists at '%s'. Failed due to: %s",
                config_path_str,
                info_result.result_details,
            )
            return False

        # Step 2: Create config file if it doesn't exist
        if info_result.file_entry is None:
            logger.info("User config file does not exist at '%s', creating with empty config", config_path_str)

            # Create empty config with proper JSON formatting
            empty_config = json.dumps({}, indent=2)

            create_request = WriteFileRequest(
                file_path=config_path_str,
                content=empty_config,
                encoding="utf-8",
                existing_file_policy=ExistingFilePolicy.FAIL,  # Should not exist, fail if it does
                create_parents=True,  # Create parent directories if missing
                skip_metadata_injection=True,
            )
            create_result = os_manager.on_write_file_request(create_request)

            if isinstance(create_result, WriteFileResultFailure):
                logger.error(
                    "Attempted to create user config file at '%s'. Failed due to: %s",
                    config_path_str,
                    create_result.result_details,
                )
                return False

        # Step 3: Read current config directly from disk.
        #
        # We intentionally bypass the ReadFileRequest handler here. The enclosing writes
        # already use os_manager.on_write_file_request directly (sync), so the read matches
        # that bootstrap-path style and avoids coupling config load to event-loop state.
        try:
            file_content = Path(config_path_str).read_text(encoding="utf-8")
        except FileNotFoundError:
            logger.error(
                "Attempted to read user config at '%s'. File not found despite creation attempt.",
                config_path_str,
            )
            return False
        except PermissionError as e:
            logger.error(
                "Attempted to read user config at '%s'. Permission denied: %s",
                config_path_str,
                e,
            )
            return False
        except UnicodeDecodeError as e:
            logger.error(
                "Attempted to read user config at '%s'. Encoding error: %s",
                config_path_str,
                e,
            )
            return False
        except OSError as e:
            logger.error(
                "Attempted to read user config at '%s'. Failed with: %s",
                config_path_str,
                e,
            )
            return False

        # Step 4: Parse JSON from file content
        try:
            current_config = json.loads(file_content)
        except json.JSONDecodeError as e:
            # Config file is corrupted - back it up and start fresh
            backup_path_str = str(USER_CONFIG_PATH.with_suffix(".bak"))

            logger.warning(
                "User config file at '%s' contained invalid JSON. Attempting to back up to '%s'. Parse error: %s",
                config_path_str,
                backup_path_str,
                str(e),
            )

            # Use RenameFileRequest to back up corrupted file
            rename_request = RenameFileRequest(
                old_path=config_path_str,
                new_path=backup_path_str,
                workspace_only=False,
            )
            rename_result = os_manager.on_rename_file_request(rename_request)

            if isinstance(rename_result, RenameFileResultFailure):
                logger.error(
                    "Failed to back up corrupted config from '%s' to '%s': %s. Using empty config.",
                    config_path_str,
                    backup_path_str,
                    rename_result.result_details,
                )
            else:
                logger.info("Successfully backed up corrupted config to '%s'", backup_path_str)

            # Use empty config regardless of backup success
            current_config = {}

        # Step 5: Merge delta with current config
        merged_config = merge_dicts(current_config, user_config_delta)

        # Step 6: Write merged config back with file locking (atomic write)
        write_request = WriteFileRequest(
            file_path=config_path_str,
            content=json.dumps(merged_config, indent=2),
            encoding="utf-8",
            existing_file_policy=ExistingFilePolicy.OVERWRITE,
            create_parents=True,
        )
        write_result = os_manager.on_write_file_request(write_request)

        # Handle write failures
        if isinstance(write_result, WriteFileResultFailure):
            match write_result.failure_reason:
                case FileIOFailureReason.PERMISSION_DENIED:
                    logger.error(
                        "Attempted to write merged config to '%s'. Permission denied: %s",
                        config_path_str,
                        write_result.result_details,
                    )
                case FileIOFailureReason.DISK_FULL:
                    logger.error(
                        "Attempted to write merged config to '%s'. Disk full: %s",
                        config_path_str,
                        write_result.result_details,
                    )
                case FileIOFailureReason.FILE_LOCKED:
                    logger.error(
                        "Attempted to write merged config to '%s'. File is locked by another process: %s",
                        config_path_str,
                        write_result.result_details,
                    )
                case FileIOFailureReason.IS_DIRECTORY:
                    logger.error(
                        "Attempted to write merged config to '%s'. Path is a directory, not a file: %s",
                        config_path_str,
                        write_result.result_details,
                    )
                case FileIOFailureReason.ENCODING_ERROR:
                    logger.error(
                        "Attempted to write merged config to '%s'. Encoding error: %s",
                        config_path_str,
                        write_result.result_details,
                    )
                case _:
                    logger.error(
                        "Attempted to write merged config to '%s'. Failed with: %s",
                        config_path_str,
                        write_result.result_details,
                    )
            return False

        # Success path: Reload configs to reflect the changes
        logger.debug("Successfully wrote user config delta to '%s', reloading configs", config_path_str)
        return True

    def _set_log_level(self, level: str) -> None:
        """Set the log level for the logger.

        Args:
            level: The log level to set (e.g., 'DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL').
        """
        try:
            level_upper = level.upper()
            log_level = getattr(logging, level_upper)
            logger.setLevel(log_level)
        except (ValueError, AttributeError):
            logger.error("Invalid log level %s. Defaulting to INFO.", level)
            logger.setLevel(logging.INFO)
