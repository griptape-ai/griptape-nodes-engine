import json
import logging
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError, ValidationInfo, field_validator
from pydantic import Field as PydanticField

from griptape_nodes.common.project_templates import PerPlatformProjectPath
from griptape_nodes.node_library.library_declarations import WorkerMode
from griptape_nodes.retained_mode.beta_features import BETA_FEATURES_KEY, LIBRARY_BETA_FEATURES_KEY

LIBRARIES_TO_REGISTER_KEY = "app_events.on_app_initialization_complete.libraries_to_register"
LIBRARIES_TO_DOWNLOAD_KEY = "app_events.on_app_initialization_complete.libraries_to_download"
WORKFLOWS_TO_REGISTER_KEY = "app_events.on_app_initialization_complete.workflows_to_register"
SECRETS_TO_REGISTER_KEY = "app_events.on_app_initialization_complete.secrets_to_register"
MODELS_TO_DOWNLOAD_KEY = "app_events.on_app_initialization_complete.models_to_download"
PROJECTS_TO_REGISTER_KEY = "app_events.on_app_initialization_complete.projects_to_register"
REQUIRES_ENGINE_KEY = "app_events.on_app_initialization_complete.requires_engine"
PROJECT_WORKSPACES_KEY = "project_workspaces"
EVENTS_TO_ECHO_KEY = "app_events.events_to_echo_as_retained_mode"
WORKER_HEARTBEAT_INTERVAL_KEY = "worker.heartbeat_interval_s"
WORKER_HEARTBEAT_TIMEOUT_KEY = "worker.heartbeat_timeout_s"
WORKER_LIBRARY_LOAD_TIMEOUT_KEY = "worker.library_load_timeout_s"
WORKER_COMMAND_PREFIX_KEY = "worker.command_prefix"
DISCOVERY_MAX_DEPTH_KEY = "discovery_max_depth"
# The `Settings.libraries_directory` field below, named here so every reader of it -- the live
# libraries root, the provisioning preview, the offline libraries-root resolver, and the packager --
# agrees on both the key and what a missing value means.
LIBRARIES_DIRECTORY_KEY = "libraries_directory"
DEFAULT_LIBRARIES_DIRECTORY = "libraries"
LIBRARY_DEPENDENCY_INSTALL_BEHAVIOR_KEY = "library.dependency_install_behavior"
LIBRARY_PROVISIONED_BY_KEY = "library.provisioned_by"
LIBRARY_SANDBOX_ENABLED_KEY = "library.sandbox_enabled"
LIBRARY_MINIMUM_RELEASE_AGE_KEY = "library.minimum_release_age"
LIBRARY_LAZY_NODE_LOADING_KEY = "library.lazy_node_loading"
LOG_TO_FILE_KEY = "logging.log_to_file"
LOG_DIRECTORY_KEY = "logging.log_directory"
LOG_RETENTION_DAYS_KEY = "logging.log_retention_days"
SESSION_LOG_BUFFER_LINES_KEY = "logging.session_log_buffer_lines"
# Validation context flag ConfigManager sets when checking a single GTN_CONFIG_ variable. Env vars
# are always strings, so validators that need a typed value convert under this flag, and a value
# that can't be converted fails validation so the variable is reported as a bad value instead of
# silently becoming a default. Beta feature entries, `worker.command_prefix`,
# `library.provisioned_by`, and `library.sandbox_enabled` read it.
FROM_ENV_CONTEXT = "from_env"

logger = logging.getLogger("griptape_nodes")

_BOOL_ADAPTER = TypeAdapter(bool)
# (config key, repr of value) pairs already warned about. Settings is validated on every config
# reload, so without this one bad entry would log the same warning many times per session.
_reported_invalid_settings: set[tuple[str, str]] = set()


def _validate_beta_feature_map(map_key: str, v: Any, *, from_env: bool) -> dict[str, bool]:
    """Keep the true/false entries of one beta feature map and drop the rest with a warning.

    Args:
        map_key: Dot-notation key of the map, used in warnings (e.g. "beta_features").
        v: The raw value found under that key.
        from_env: Whether the value came from a GTN_CONFIG_ variable. Its strings are converted
            to booleans, and a value that can't be converted, or isn't a map, raises so the env
            loader reports the variable as invalid.
    """
    if not isinstance(v, dict) and from_env:
        msg = f"{map_key} must be a map of feature ids to true or false, got {v!r}"
        raise ValueError(msg)

    if not isinstance(v, dict):
        _warn_once(
            (map_key, repr(v)),
            f"Ignoring {map_key}: expected a map of feature ids to true or false, got {v!r}. "
            "Every feature in it uses its default.",
        )
        return {}

    if from_env:
        return {feature_id: _env_value_to_bool(f"{map_key}.{feature_id}", value) for feature_id, value in v.items()}

    valid: dict[str, bool] = {}
    for feature_id, value in v.items():
        if not isinstance(value, bool):
            _warn_once(
                (f"{map_key}.{feature_id}", repr(value)),
                f"Ignoring {map_key}.{feature_id}: expected true or false, got {value!r}. "
                "The feature uses its default.",
            )
            continue
        valid[feature_id] = value
    return valid


def _env_value_to_bool(config_key: str, value: Any) -> bool:
    """Convert a beta feature GTN_CONFIG_ string to a boolean, raising when it isn't one."""
    try:
        return _BOOL_ADAPTER.validate_python(value)
    except ValidationError as e:
        msg = f"{config_key} must be true or false, got {value!r}"
        raise ValueError(msg) from e


def _warn_once(report_key: tuple[str, str], message: str, *, level: int = logging.WARNING) -> None:
    """Log a settings warning the first time this (config key, value) pair is seen."""
    if report_key in _reported_invalid_settings:
        return

    _reported_invalid_settings.add(report_key)
    logger.log(level, message)


class Category(BaseModel):
    """A category with name and optional description."""

    name: str
    description: str | None = None

    def __str__(self) -> str:
        return self.name


# Predefined categories to avoid repetition
FILE_SYSTEM = Category(name="File System", description="Directories and file paths for the application")
APPLICATION_EVENTS = Category(name="Application Events", description="Configuration for application lifecycle events")
API_KEYS = Category(name="API Keys", description="API keys and authentication credentials")
EXECUTION = Category(name="Execution", description="Workflow execution and processing settings")
STORAGE = Category(name="Storage", description="Data storage and persistence configuration")
SYSTEM_REQUIREMENTS = Category(name="System Requirements", description="System resource requirements and limits")
MCP_SERVERS = Category(name="MCP Servers", description="Model Context Protocol server configurations")
PROJECTS = Category(name="Projects", description="Project template configurations and registrations")
STATIC_SERVER = Category(name="Static Server", description="Static file server configuration for serving media assets")
ARTIFACTS = Category(name="Artifacts", description="Settings for artifact providers and preview generation")
AGENT = Category(name="Agent", description="Agent behavior and system prompt")
LIBRARIES = Category(name="Libraries", description="Settings for library management and dependency installation")
BETA_FEATURES = Category(name="Beta Features", description="Experimental features that can be turned on or off")
LOGGING = Category(name="Logging", description="Where engine logs are kept and how much history is retained")


def Field(category: str | Category = "General", **kwargs) -> Any:
    """Enhanced Field with default category that can be overridden.

    A caller-supplied `json_schema_extra` dict keeps its own keys and gains the category unless it
    already names one.
    """
    json_schema_extra = dict(kwargs.get("json_schema_extra") or {})
    if "category" not in json_schema_extra:
        # Convert Category to dict or use string directly
        if isinstance(category, Category):
            category_dict = {"name": category.name}
            if category.description:
                category_dict["description"] = category.description
            json_schema_extra["category"] = category_dict
        else:
            json_schema_extra["category"] = category
    kwargs["json_schema_extra"] = json_schema_extra
    return PydanticField(**kwargs)


class WorkflowExecutionMode(StrEnum):
    """Execution type for node processing."""

    SEQUENTIAL = "sequential"
    PARALLEL = "parallel"


class LogLevel(StrEnum):
    """Logging level for the application."""

    CRITICAL = "CRITICAL"
    ERROR = "ERROR"
    WARNING = "WARNING"
    INFO = "INFO"
    DEBUG = "DEBUG"


class MCPServerConfig(BaseModel):
    """Configuration for a single MCP server."""

    name: str = Field(description="Unique name/identifier for the MCP server")
    enabled: bool = Field(default=True, description="Whether this MCP server is enabled")
    transport: str = Field(default="stdio", description="Transport type: stdio, sse, streamable_http, or websocket")

    # StdioConnection fields
    command: str | None = Field(default=None, description="Command to start the MCP server (required for stdio)")
    args: list[str] = Field(default_factory=list, description="Arguments to pass to the MCP server command (stdio)")
    env: dict[str, str] = Field(default_factory=dict, description="Environment variables for the MCP server (stdio)")
    cwd: str | None = Field(default=None, description="Working directory for the MCP server (stdio)")
    encoding: str = Field(default="utf-8", description="Text encoding for stdio communication")
    encoding_error_handler: str = Field(default="strict", description="Encoding error handler for stdio")

    # HTTP-based connection fields (sse, streamable_http, websocket)
    url: str | None = Field(
        default=None, description="URL for HTTP-based connections (sse, streamable_http, websocket)"
    )
    headers: dict[str, str] | None = Field(default=None, description="HTTP headers for HTTP-based connections")
    timeout: float | None = Field(default=None, description="HTTP timeout in seconds")
    sse_read_timeout: float | None = Field(default=None, description="SSE read timeout in seconds")
    terminate_on_close: bool = Field(
        default=True, description="Whether to terminate session on close (streamable_http)"
    )

    # Common fields
    description: str | None = Field(default=None, description="Optional description of what this MCP server provides")
    capabilities: list[str] = Field(default_factory=list, description="List of capabilities this MCP server provides")
    rules: str | None = Field(default=None, description="Optional rules for this MCP server as a single string.")

    def __str__(self) -> str:
        return f"{self.name} ({'enabled' if self.enabled else 'disabled'})"


class LibraryRegistration(BaseModel):
    """A library entry in libraries_to_register with optional metadata.

    Bare path strings remain valid in the config; this object form is used when
    additional fields (such as `enabled`) need to be set per entry. Each entry
    names an already-present local library by `path`;
    version-pinned remote sources are declared separately in `libraries_to_download`.
    """

    model_config = ConfigDict(extra="forbid")

    path: str = Field(description="Path to a griptape_nodes_library.json file or a directory scanned recursively.")
    enabled: bool = Field(
        default=True,
        description="When False, the library remains in config but is not loaded on startup.",
    )
    worker_mode_override: WorkerMode | None = Field(
        default=None,
        description=(
            "Accepted for backward compatibility; no longer affects where a library runs. "
            "A library's nodes execute in a worker when it declares pip_dependencies_exec."
        ),
    )


class LibraryDownload(BaseModel):
    """A library entry in libraries_to_download that the engine provisions to a version.

    Bare git-URL strings remain valid in the config; this object form is used
    when a version pin (or an explicit manifest `name`) is needed. The engine
    downloads the library and, when the installed version does not satisfy the
    pin, overwrites the local copy so the owning project gets the version it
    declares. Only libraries listed here may be overwritten by project
    activation; a library that is merely registered (libraries_to_register) is
    never overwritten.
    """

    model_config = ConfigDict(extra="forbid")

    git_url: str = Field(
        description=(
            "Git source in the engine's `url@ref` form: a full URL or `user/repo` shorthand, "
            "with an optional `@branch|tag|commit` suffix "
            "(e.g. 'griptape-ai/griptape-nodes-library-standard@v2.0')."
        ),
    )
    version: str | None = Field(
        default=None,
        description=(
            "PEP 440 version specifier the installed library must satisfy (e.g. '>=1.2,<2'). None pins by source only."
        ),
    )
    name: str | None = Field(
        default=None,
        description=(
            "Library name, matching the library's manifest `name`. When set, the installed "
            "version is matched by name to decide whether a re-download is needed."
        ),
    )


class AppInitializationComplete(BaseModel):
    libraries_to_download: list[str | LibraryDownload] = Field(
        default_factory=list,
        description="Libraries to automatically download when the engine starts, into libraries_directory. Each entry is either a bare git URL string or an object with `git_url` plus an optional PEP 440 `version` pin and manifest `name`. Git URLs support full URLs or GitHub shorthand (e.g., 'user/repo'). Optionally specify a branch, tag, or commit with @ref syntax (e.g., 'user/repo@stable' or 'https://github.com/user/repo@v1.0.0'). If no ref is specified, uses the repository's default branch. The engine provisions each entry to its pinned version and may overwrite a wrong installed version; libraries listed only in libraries_to_register are never overwritten.",
    )
    libraries_to_register: list[str | LibraryRegistration] = Field(
        default_factory=list,
        description=(
            "Libraries the engine loads on startup. Each entry can be a path to a single "
            "griptape_nodes_library.json file or a folder containing one or more libraries. "
            "Use the toggle to enable or skip a library. Where a library's nodes execute is "
            "not a setting; a library declaring pip_dependencies_exec executes them in a worker."
        ),
    )
    workflows_to_register: list[str] = Field(default_factory=list)
    secrets_to_register: list[str] | dict[str, str] = Field(
        default_factory=lambda: {"HF_TOKEN": "", "GT_CLOUD_API_KEY": ""},
        description="Core secrets to register. Can be a list of secret names (default to empty values) or a dict mapping names to default values. Library-specific secrets are registered automatically from library settings.",
    )
    models_to_download: list[str] = Field(default_factory=list)
    projects_to_register: list[str | PerPlatformProjectPath] = Field(
        category=PROJECTS,
        default_factory=list,
        description=(
            "List of project entries to load at startup. "
            "Each entry may be either: "
            "(1) a single path string (supports `${ENV_VAR}` and `~` expansion), or "
            "(2) a per-platform mapping with optional `linux`, `darwin`, `windows`, and `default` keys "
            "for cross-platform deployments where the same project resolves to different paths on each OS. "
            "A path entry may point to a single griptape-nodes-project.yml file, or to a directory that is "
            "recursively scanned for all griptape-nodes-project.yml files (each loaded as a registered template). "
            "Directory entries are kept verbatim and re-scanned each startup; the discovered files are not "
            "expanded into individual entries. "
            "Per-platform entries with no key matching the active platform and no `default` are skipped with a warning."
        ),
    )
    requires_engine: str | None = Field(
        category=PROJECTS,
        default=None,
        description=(
            "PEP 440 version specifier the running engine must satisfy (e.g. '>=0.5,<0.6'). "
            "A mismatch blocks project activation. Typically set in a project-adjacent config so the "
            "project becomes the source of truth for the engine version it runs against."
        ),
    )


class AppEvents(BaseModel):
    on_app_initialization_complete: AppInitializationComplete = Field(default_factory=AppInitializationComplete)
    events_to_echo_as_retained_mode: list[str] = Field(
        default_factory=lambda: [
            "CreateConnectionRequest",
            "DeleteConnectionRequest",
            "CreateFlowRequest",
            "DeleteFlowRequest",
            "CreateNodeRequest",
            "DeleteNodeRequest",
            "AddParameterToNodeRequest",
            "RemoveParameterFromNodeRequest",
            "SetParameterValueRequest",
            "AlterParameterDetailsRequest",
            "SetConfigValueRequest",
            "SetConfigCategoryRequest",
            "DeleteWorkflowRequest",
            "ResolveNodeRequest",
            "ExecuteNodeRequest",
            "StartFlowRequest",
            "CancelFlowRequest",
            "UnresolveFlowRequest",
            "SingleExecutionStepRequest",
            "SingleNodeStepRequest",
            "ContinueExecutionStepRequest",
            "SetLockNodeStateRequest",
        ]
    )


class WorkerSettings(BaseModel):
    heartbeat_interval_s: float = Field(
        default=5.0,
        description="Interval in seconds between worker heartbeat challenges sent by the orchestrator.",
    )
    heartbeat_timeout_s: float = Field(
        default=15.0,
        description=(
            "Seconds without a heartbeat response before a worker is evicted. A worker also shuts "
            "itself down after this much orchestrator silence, but never sooner than 30 seconds, so "
            "that an orchestrator too busy to challenge is not mistaken for one that exited."
        ),
    )
    library_load_timeout_s: float = Field(
        default=600.0,
        description=(
            "Seconds a worker may take to load its library. Bounds how long running a node waits "
            "for its library's worker to finish loading, and how long a project switch waits for "
            "each worker to adopt it. "
            "First-time installs of large libraries (e.g. torch, diffusers) can easily exceed "
            "two minutes. Does not affect heartbeats; see worker.heartbeat_timeout_s for those."
        ),
    )
    command_prefix: list[str] = Field(
        default_factory=list,
        json_schema_extra={"env_var_format": "json_list"},
        description=(
            "Words placed in front of the command that starts a library's worker process, so the worker "
            "runs inside an environment another tool prepares (for example a package manager that "
            "resolves the library's packages). Empty (the default) starts workers directly. Each word "
            "may contain {library_request}, {library_name}, {engine_version}, and {python_version}, "
            "filled per worker: {library_request} is the library's entry in the "
            "GTN_LIBRARY_WORKER_REQUESTS environment variable (a word that is exactly {library_request} "
            "becomes one word per space-separated part of that entry), {library_name} is the library's "
            "name, {engine_version} is this engine's version, and {python_version} is the Python it runs "
            "on (major.minor). The worker always runs this engine's own Python interpreter, so the "
            "environment the prefix prepares must be for that Python. When a word uses {library_request} and the library has no entry, the "
            "worker starts without the prefix, except when library.provisioned_by is 'environment', "
            "where the worker is not started and the library reports why. The environment variable "
            'takes a JSON list, e.g. GTN_CONFIG_WORKER__COMMAND_PREFIX=\'["env-tool", "run", '
            '"{library_request}", "--"]\'.'
        ),
    )

    @field_validator("command_prefix", mode="before")
    @classmethod
    def validate_command_prefix(cls, v: Any, info: ValidationInfo) -> Any:
        """Parse the JSON list a GTN_CONFIG_WORKER__COMMAND_PREFIX variable carries.

        Env vars are always strings and a list has no other string form, so under
        `FROM_ENV_CONTEXT` a string is read as JSON. A blank one means no prefix.
        A string from a config file is left for the list type to reject, as for any list setting.
        """
        from_env = bool(info.context and info.context.get(FROM_ENV_CONTEXT))
        if not from_env or not isinstance(v, str):
            return v
        if not v.strip():
            return []
        try:
            return json.loads(v)
        except json.JSONDecodeError as e:
            msg = f"{WORKER_COMMAND_PREFIX_KEY} must be a JSON list of strings, got {v!r}"
            raise ValueError(msg) from e


class AgentSettings(BaseModel):
    system_prompt: str = Field(
        default="",
        description="Additional text appended to the agent's built-in system prompt. Use to customize tone, preferred patterns, or domain context.",
    )


class LibraryDependencyInstallBehavior(StrEnum):
    ALWAYS = "always"
    NEVER = "never"


class LibraryProvisioner(StrEnum):
    ENGINE = "engine"
    ENVIRONMENT = "environment"


class LibrarySettings(BaseModel):
    provisioned_by: LibraryProvisioner = Field(
        default=LibraryProvisioner.ENGINE,
        description=(
            "What provides libraries and their Python dependencies. 'engine' (the default) has the engine "
            "download libraries, build a virtual environment for each one, and install its dependencies. "
            "'environment' is for an engine started inside an environment another tool has already "
            "prepared: the engine loads only the libraries listed in the GTN_LIBRARY_PATHS environment "
            "variable, never builds virtual environments, never downloads, updates, or installs "
            "libraries, and marks every other configured library (libraries_to_register entries, and the "
            "sandbox library unless library.sandbox_enabled is true) as not provided by the environment. "
            "A library dependency is then satisfied only by a library the environment provides. Any other "
            "value is treated as 'environment' and reported as an error, so a misspelled value never "
            "downloads or builds."
        ),
    )
    sandbox_enabled: bool | None = Field(
        default=None,
        description=(
            "Whether the sandbox library (sandbox_library_directory) is scanned and loaded, and sandbox "
            "nodes can be added. Unset (the default) means on when library.provisioned_by is 'engine' "
            "and off when it is 'environment'. True turns it on in either mode: in environment mode no "
            "virtual environment is built for it, so everything its nodes import must already be in "
            "the environment, and every other library the environment does not provide is still "
            "refused. False turns it off in either mode."
        ),
    )
    dependency_install_behavior: LibraryDependencyInstallBehavior = Field(
        default=LibraryDependencyInstallBehavior.ALWAYS,
        description=(
            "Controls automatic installation of library dependencies declared in library manifests. "
            "'always' downloads and installs them on registration. "
            "'never' skips installation and marks the library as degraded if required dependencies are missing."
        ),
    )
    lazy_node_loading: bool = Field(
        default=True,
        description=(
            "When True (the default), a node's Python module is imported lazily the first time a node "
            "of that type is created (or the type is otherwise resolved, such as when introspected) "
            "rather than at startup, which speeds up engine startup for libraries with many or heavy "
            "nodes. The tradeoff is that a broken node's import error is not reported until that type is "
            "first created. When False, the engine imports every node's Python module at startup, so an "
            "import error surfaces immediately as a library problem, before the node is placed on a "
            "canvas; set this while authoring nodes if you want that check. Nodes in the sandbox library "
            "are always loaded eagerly regardless of this setting."
        ),
    )
    minimum_release_age: float = Field(
        default=0.0,
        description=(
            "Minimum age, in hours, of the target release before a library update is applied. When 0 (the "
            "default), updates apply as soon as they are available. When greater than 0, an update is "
            "withheld until the commit it would move to is at least this many hours old, guarding against "
            "automatically adopting a freshly-pushed release before there is time to catch and yank a bad "
            "one. If the target commit's age cannot be determined (e.g. the remote timestamp is unreadable), "
            "the update is allowed (fail-open) and a warning is logged, so a metadata hiccup never "
            "permanently blocks updates. Age is measured from the target commit's git committer timestamp, "
            "which is not necessarily when the release was published: rebased, cherry-picked, backdated "
            "(GIT_COMMITTER_DATE), or force-moved tags can report an age that differs from the actual publish "
            "time."
        ),
    )

    @field_validator("provisioned_by", mode="before")
    @classmethod
    def validate_provisioned_by(cls, v: Any, info: ValidationInfo) -> LibraryProvisioner:  # noqa: ARG003 (both sources fail closed the same way)
        """Accept any letter case, and fail closed on anything else.

        A value that is neither 'engine' nor 'environment', from a config file or a
        GTN_CONFIG_LIBRARY__PROVISIONED_BY variable, is treated as 'environment' and reported as an
        error. Falling back to 'engine' instead would have a launcher's misspelled 'environment'
        download, build, and prune exactly what the environment was meant to provide, after one
        warning that is easy to miss because GTN_LIBRARY_PATHS libraries still load either way.
        """
        if isinstance(v, LibraryProvisioner):
            return v
        if isinstance(v, str):
            try:
                return LibraryProvisioner(v.strip().lower())
            except ValueError:
                pass
        _warn_once(
            (LIBRARY_PROVISIONED_BY_KEY, repr(v)),
            f"{LIBRARY_PROVISIONED_BY_KEY} is {v!r}, which is neither 'engine' nor 'environment'. Treating it as "
            "'environment' so nothing is downloaded, built, or installed: only libraries listed in GTN_LIBRARY_PATHS "
            "load. Fix the value to use the engine's own provisioning.",
            level=logging.ERROR,
        )
        return LibraryProvisioner.ENVIRONMENT

    @field_validator("sandbox_enabled", mode="before")
    @classmethod
    def validate_sandbox_enabled(cls, v: Any, info: ValidationInfo) -> bool | None:
        """Accept true or false in any letter case, and keep a bad value from resetting the whole config.

        From a GTN_CONFIG_LIBRARY__SANDBOX_ENABLED variable an unrecognized value raises, so the env
        loader reports the variable and ignores it. From a config file it falls back to unset (the
        mode's default) with a warning.
        """
        from_env = bool(info.context and info.context.get(FROM_ENV_CONTEXT))
        if v is None or isinstance(v, bool):
            return v
        if isinstance(v, str) and v.strip().lower() in ("true", "false"):
            return v.strip().lower() == "true"
        if from_env:
            msg = f"{LIBRARY_SANDBOX_ENABLED_KEY} must be true or false, got {v!r}"
            raise ValueError(msg)
        _warn_once(
            (LIBRARY_SANDBOX_ENABLED_KEY, repr(v)),
            f"Ignoring {LIBRARY_SANDBOX_ENABLED_KEY}: expected true or false, got {v!r}. Using the default.",
        )
        return None

    @field_validator("dependency_install_behavior", mode="before")
    @classmethod
    def validate_dependency_install_behavior(cls, v: Any) -> LibraryDependencyInstallBehavior:
        if isinstance(v, str):
            try:
                return LibraryDependencyInstallBehavior(v.lower())
            except ValueError:
                return LibraryDependencyInstallBehavior.ALWAYS
        elif isinstance(v, LibraryDependencyInstallBehavior):
            return v
        return LibraryDependencyInstallBehavior.ALWAYS


class LoggingSettings(BaseModel):
    """Settings for engine log capture, used when reporting a problem."""

    log_to_file: bool = Field(
        category=LOGGING,
        default=True,
        description="Write engine logs to a file as well as to the console. Each engine process writes its own file, rolling over at 10 MB and keeping 5 rollovers, so the total size per process is capped. Turn this off if you only ever need the logs from the session that is running right now.",
    )
    log_directory: str = Field(
        category=LOGGING,
        default="",
        description="Absolute path to the directory holding engine log files. Like ffmpeg_directory, this is never interpreted relative to the workspace: logs belong to the machine, not to a workspace, so every workspace and project shares one location. A relative value is ignored with a warning. Empty (the default) means the `logs` folder in the engine state directory: `<XDG_STATE_HOME>/griptape_nodes`, or the path in `GTN_ENGINE_STATE_DIR` when that is set.",
    )
    log_retention_days: int = Field(
        category=LOGGING,
        default=7,
        description="Delete engine log files that have not been written to for this many days. Checked when the engine starts, and again whenever a logging setting changes. The log file the engine is currently writing is never deleted, however old it is. Set to 0 to keep log files forever.",
    )
    session_log_buffer_lines: int = Field(
        category=LOGGING,
        default=5000,
        description="How many of the most recent log lines the engine keeps in memory for the current session, so a problem report includes what just happened without you having to reproduce it. These lines carry whatever log_level allows, so raise log_level to DEBUG before reproducing a problem if you need debug detail in the report. Set to 0 to disable, which means a problem report can only include whatever reached the log files.",
    )


class Settings(BaseModel):
    model_config = ConfigDict(extra="allow")

    workspace_directory: str = Field(
        category=FILE_SYSTEM,
        default=str(Path().cwd() / "GriptapeNodes"),
        description="Root directory for projects, workflows, and generated assets. Defaults to a GriptapeNodes folder under the current working directory. The other File System paths (libraries_directory, static_files_directory, sandbox_library_directory, synced_workflows_directory) are interpreted relative to this directory unless they are set to absolute paths.",
    )
    static_files_directory: str = Field(
        category=FILE_SYSTEM,
        default="staticfiles",
        description="Path to the static files directory, relative to the workspace directory.",
    )
    sandbox_library_directory: str = Field(
        category=FILE_SYSTEM,
        default="sandbox_library",
        description="Path to the sandbox library directory (useful while developing nodes). Relative paths are interpreted relative to the workspace directory. Absolute paths are used as-is. `~` and environment variables later in the path are expanded; a value that starts with `$` is looked up as a secret name instead.",
    )
    libraries_directory: str = Field(
        category=FILE_SYSTEM,
        default="libraries",
        description="Path to the directory where libraries from libraries_to_download are installed; those load automatically. Anything else placed here is not loaded until its path is added to libraries_to_register (Add Library in the editor). Relative paths are interpreted relative to the workspace directory. Absolute paths are used as-is. A project may override this location via the project-template `libraries_dir` field (inheritable down the parent-project chain), which takes precedence over this value so a child project can share its parent's library install location.",
    )
    ffmpeg_directory: str = Field(
        category=FILE_SYSTEM,
        default="",
        description="Absolute path to the directory holding the ffmpeg/ffprobe binaries the engine downloads on first use. Unlike the other directory settings, this is never interpreted relative to the workspace: the ffmpeg cache belongs to the machine, not to a workspace, so it is shared across every workspace and project. A relative value is ignored with a warning. Empty (the default) means the `ffmpeg` folder in the engine data directory: `<XDG_DATA_HOME>/griptape_nodes`, or the path in `GTN_ENGINE_DATA_DIR` when that is set. To supply your own binaries instead of downloading, point this at a directory containing `bin/<platform>/` holding ffmpeg, ffprobe, and an empty `installed.crumb` file - static-ffmpeg treats that marker as proof of a completed install, and re-downloads over the binaries whenever it is missing.",
    )
    app_events: AppEvents = Field(
        category=APPLICATION_EVENTS,
        default_factory=AppEvents,
    )
    log_level: LogLevel = Field(
        category=EXECUTION,
        default=LogLevel.INFO,
        description="Logging verbosity for the engine. One of CRITICAL, ERROR, WARNING, INFO, or DEBUG, from least to most verbose.",
    )
    logging: LoggingSettings = Field(
        category=LOGGING,
        default_factory=LoggingSettings,
    )
    workflow_execution_mode: WorkflowExecutionMode = Field(
        category=EXECUTION,
        default=WorkflowExecutionMode.SEQUENTIAL,
        description="Workflow execution mode for node processing. SEQUENTIAL mode uses ParallelResolutionMachine with max_nodes_in_parallel=1 to execute nodes one at a time. PARALLEL mode uses the configured max_nodes_in_parallel value.",
    )

    @field_validator("workflow_execution_mode", mode="before")
    @classmethod
    def validate_workflow_execution_mode(cls, v: Any) -> WorkflowExecutionMode:
        """Convert string values to WorkflowExecutionMode enum."""
        if isinstance(v, str):
            try:
                return WorkflowExecutionMode(v.lower())
            except ValueError:
                # Return default if invalid string
                return WorkflowExecutionMode.SEQUENTIAL
        elif isinstance(v, WorkflowExecutionMode):
            return v
        else:
            # Return default for any other type
            return WorkflowExecutionMode.SEQUENTIAL

    @field_validator("log_level", mode="before")
    @classmethod
    def validate_log_level(cls, v: Any) -> LogLevel:
        """Convert string values to LogLevel enum."""
        if isinstance(v, str):
            try:
                return LogLevel(v.upper())
            except ValueError:
                # Return default if invalid string
                return LogLevel.INFO
        elif isinstance(v, LogLevel):
            return v
        else:
            # Return default for any other type
            return LogLevel.INFO

    max_nodes_in_parallel: int | None = Field(
        category=EXECUTION,
        default=5,
        description="Maximum number of nodes executing at a time for parallel execution.",
    )
    worker: WorkerSettings = Field(
        category=EXECUTION,
        default_factory=WorkerSettings,
    )
    storage_backend: Literal["local", "gtc"] = Field(
        category=STORAGE,
        default="local",
        description="Backend used to persist workflow data and generated assets. 'local' stores files on the local filesystem under the workspace; 'gtc' uses Griptape Cloud storage.",
    )
    auto_inject_workflow_metadata: bool = Field(
        category=STORAGE,
        default=True,
        description="Automatically inject workflow metadata into saved files with supported formats",
    )
    minimum_disk_space_gb_libraries: float = Field(
        category=SYSTEM_REQUIREMENTS,
        default=10.0,
        description="Minimum disk space in GB required for library installation and virtual environment operations",
    )
    minimum_disk_space_gb_workflows: float = Field(
        category=SYSTEM_REQUIREMENTS,
        default=1.0,
        description="Minimum disk space in GB required for saving workflows",
    )
    discovery_max_depth: int = Field(
        category=SYSTEM_REQUIREMENTS,
        default=5,
        description=(
            "Maximum directory depth the engine walks when a registered entry points at a directory "
            "to recursively discover files (e.g. project files under projects_to_register). Bounds boot-time "
            "scans against pathologically deep trees and symlink loops. 0 scans only the top-level directory; "
            "each nested level adds 1."
        ),
    )
    synced_workflows_directory: str = Field(
        category=FILE_SYSTEM,
        default="synced_workflows",
        description="Path to the synced workflows directory, relative to the workspace directory.",
    )
    thread_storage_backend: Literal["local"] = Field(
        category=STORAGE,
        default="local",
        description="Storage backend for conversation threads. Only 'local' (filesystem) is supported; "
        "Griptape Cloud support was removed in the Pydantic AI migration.",
    )

    @field_validator("thread_storage_backend", mode="before")
    @classmethod
    def validate_thread_storage_backend(cls, v: Any) -> str:
        """Coerce legacy/unknown backends (e.g. the removed 'gtc') to 'local'.

        Persisted configs from before Griptape Cloud thread storage was removed
        carry ``thread_storage_backend: "gtc"``. Without this, validating the
        whole config fails and the user's entire config is reset to defaults.
        """
        if v == "local":
            return v
        return "local"

    enable_workspace_file_watching: bool = Field(
        category=FILE_SYSTEM,
        default=True,
        description="Enable file watching for synced workflows directory",
    )
    mcp_servers: list[MCPServerConfig] = Field(
        category=MCP_SERVERS,
        default_factory=list,
        description="List of Model Context Protocol server configurations",
    )
    static_server_base_url: str | None = Field(
        category=STATIC_SERVER,
        default=None,
        description="Base URL for the static server. Leave unset to derive it from the server's host/port (including the OS-assigned port when the configured port is unavailable). Set this only to override the derived URL, e.g. when fronting the server with a tunnel (ngrok, cloudflare) or reverse proxy.",
    )
    artifacts: dict[str, Any] = Field(
        category=ARTIFACTS,
        default_factory=dict,
        description="Control how previews are generated for images and other media files",
    )
    project_file: str | None = Field(
        category=PROJECTS,
        default=None,
        description="Path to the project file (griptape-nodes-project.yml) to load initially when the engine starts. When set, overrides the default location of <workspace_directory>/griptape-nodes-project.yml. If the specified path does not exist, falls back to the workspace default. The sentinel value '<system-defaults>' means the engine deliberately stays on system defaults and suppresses the workspace-default fallback (so a workspace griptape-nodes-project.yml is not auto-discovered); this is what the engine persists when it is intentionally on system defaults.",
    )
    project_workspaces: dict[str, str] = Field(
        category=PROJECTS,
        default_factory=dict,
        description="Mapping of project identifiers to workspace directory overrides. A key may be either a project ID or a project file path: it is first matched against loaded project IDs, and if none match, treated as a project file path. When a project is loaded, if it matches a key here, the corresponding value is used as the workspace directory instead of the project-adjacent config or auto-default.",
    )
    agent: AgentSettings = Field(
        category=AGENT,
        default_factory=AgentSettings,
    )
    library: LibrarySettings = Field(
        category=LIBRARIES,
        default_factory=LibrarySettings,
    )
    beta_features: dict[str, bool] = Field(
        category=BETA_FEATURES,
        default_factory=dict,
        description="Experimental features turned on or off, keyed by feature id. The editor's Beta settings page writes these. A feature missing from this map uses its default. Any key is accepted, so editor-only features never need an engine release. The `enabled` key is the global switch: `false` turns every editor, engine, and library beta feature off without changing their own values.",
    )

    library_beta_features: dict[str, dict[str, bool]] = Field(
        category=BETA_FEATURES,
        default_factory=dict,
        description="Experimental features defined by node libraries, turned on or off. Keyed by the library's name in lowercase with spaces and punctuation replaced by underscores, then by feature id. A feature missing from this map uses its default.",
    )

    @field_validator("beta_features", mode="before")
    @classmethod
    def validate_beta_features(cls, v: Any, info: ValidationInfo) -> dict[str, bool]:
        """Drop entries that aren't true or false instead of failing the whole config.

        The map is free-form and hand-editable, so the engine cannot vouch for its contents.
        Without this, one entry such as `"maybe"` fails Settings validation and `load_configs`
        resets the user's entire config to defaults.

        Only a real boolean counts, which is the same rule `is_beta_enabled` and the editor apply.
        A string like `"true"` in a config file is dropped with a warning rather than converted,
        because the merged config keeps raw values and readers would treat it as unset anyway.

        A `GTN_CONFIG_BETA_FEATURES__<ID>` variable is the exception. It is validated under
        `FROM_ENV_CONTEXT`, which converts its string to a boolean and raises when
        it can't, so the env loader reports the variable as having an invalid value.
        """
        from_env = bool(info.context and info.context.get(FROM_ENV_CONTEXT))
        return _validate_beta_feature_map(BETA_FEATURES_KEY, v, from_env=from_env)

    @field_validator("library_beta_features", mode="before")
    @classmethod
    def validate_library_beta_features(cls, v: Any, info: ValidationInfo) -> dict[str, dict[str, bool]]:
        """Apply the `beta_features` rules to each library's map, one library at a time."""
        from_env = bool(info.context and info.context.get(FROM_ENV_CONTEXT))
        if not isinstance(v, dict) and from_env:
            msg = f"{LIBRARY_BETA_FEATURES_KEY} must be a map of library names to feature maps, got {v!r}"
            raise ValueError(msg)

        if not isinstance(v, dict):
            _warn_once(
                (LIBRARY_BETA_FEATURES_KEY, repr(v)),
                f"Ignoring {LIBRARY_BETA_FEATURES_KEY}: expected a map of library names to feature maps, got {v!r}. "
                "Every library beta feature uses its default.",
            )
            return {}

        return {
            library_key: _validate_beta_feature_map(
                f"{LIBRARY_BETA_FEATURES_KEY}.{library_key}", features, from_env=from_env
            )
            for library_key, features in v.items()
        }
