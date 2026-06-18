from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, field_validator
from pydantic import Field as PydanticField

from griptape_nodes.common.project_templates import PerPlatformProjectPath
from griptape_nodes.node_library.library_declarations import WorkerMode

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
WORKER_HEARTBEAT_STARTUP_GRACE_KEY = "worker.heartbeat_startup_grace_s"
DISCOVERY_MAX_DEPTH_KEY = "discovery_max_depth"


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
AGENT = Category(name="Agent", description="Agent behavior and instructions")


def Field(category: str | Category = "General", **kwargs) -> Any:
    """Enhanced Field with default category that can be overridden."""
    if "json_schema_extra" not in kwargs:
        # Convert Category to dict or use string directly
        if isinstance(category, Category):
            category_dict = {"name": category.name}
            if category.description:
                category_dict["description"] = category.description
            kwargs["json_schema_extra"] = {"category": category_dict}
        else:
            kwargs["json_schema_extra"] = {"category": category}
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
    additional fields (such as `enabled` or `worker_mode_override`) need to be
    set per entry. Each entry names an already-present local library by `path`;
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
            "Per-library override of the launch mode declared in the library's manifest. "
            "ORCHESTRATOR or WORKER. Only honored when the manifest declares "
            "WorkerModeCompatibility.COMPATIBLE; ignored for INCOMPATIBLE libraries. "
            "None reverts to the manifest's SuggestedWorkerMode."
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
            "Use the toggle to enable or skip a library, and pick whether it runs alongside "
            "the engine or in its own isolated process when the library supports it."
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
        description="Seconds without a heartbeat response before a worker is evicted.",
    )
    heartbeat_startup_grace_s: float = Field(
        default=600.0,
        description=(
            "Grace period in seconds after worker spawn before heartbeat timeouts are enforced. "
            "Workers need time to install venv deps and import modules before they can respond. "
            "First-time installs of large libraries (e.g. torch, diffusers) can easily exceed "
            "two minutes; this also bounds how long the orchestrator waits for worker libraries "
            "to load before marking them as FAILURE."
        ),
    )


class AgentSettings(BaseModel):
    instructions: str = Field(
        default="",
        description="Additional instructions appended to the agent's built-in system prompt. Use to customize tone, preferred patterns, or domain context.",
    )


class Settings(BaseModel):
    model_config = ConfigDict(extra="allow")

    workspace_directory: str = Field(
        category=FILE_SYSTEM,
        default=str(Path().cwd() / "GriptapeNodes"),
    )
    static_files_directory: str = Field(
        category=FILE_SYSTEM,
        default="staticfiles",
        description="Path to the static files directory, relative to the workspace directory.",
    )
    sandbox_library_directory: str = Field(
        category=FILE_SYSTEM,
        default="sandbox_library",
        description="Path to the sandbox library directory (useful while developing nodes). Relative paths are interpreted relative to the workspace directory. Absolute paths are used as-is.",
    )
    libraries_directory: str = Field(
        category=FILE_SYSTEM,
        default="libraries",
        description="Path to directory for downloaded libraries. All griptape_nodes_library.json files found recursively will be auto-discovered on startup. Relative paths are interpreted relative to the workspace directory. Absolute paths are used as-is.",
    )
    app_events: AppEvents = Field(
        category=APPLICATION_EVENTS,
        default_factory=AppEvents,
    )
    log_level: LogLevel = Field(category=EXECUTION, default=LogLevel.INFO)
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
    storage_backend: Literal["local", "gtc"] = Field(category=STORAGE, default="local")
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
        description="Path to the project file (griptape-nodes-project.yml) to load initially when the engine starts. When set, overrides the default location of <workspace_directory>/griptape-nodes-project.yml. If the specified path does not exist, falls back to the workspace default.",
    )
    project_workspaces: dict[str, str] = Field(
        category=PROJECTS,
        default_factory=dict,
        description="Mapping of project file paths to workspace directory overrides. When a project is loaded, if its resolved path matches a key here, the corresponding value is used as the workspace directory instead of the project-adjacent config or auto-default.",
    )
    agent: AgentSettings = Field(
        category=AGENT,
        default_factory=AgentSettings,
    )
