from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, NamedTuple

from griptape_nodes.node_library.library_registry import (
    LibraryMetadata,
    LibrarySchema,
    NodeMetadata,
)
from griptape_nodes.retained_mode.events.base_events import (
    RequestPayload,
    ResultPayloadFailure,
    ResultPayloadSuccess,
    WorkflowAlteredMixin,
    WorkflowNotAlteredMixin,
)
from griptape_nodes.retained_mode.events.payload_registry import PayloadRegistry

if TYPE_CHECKING:
    from pathlib import Path

    from griptape_nodes.exe_types.core_types import Parameter

    # Circular import: library_events -> library_manager -> library_events
    from griptape_nodes.retained_mode.managers.fitness_problems.libraries import LibraryProblem
    from griptape_nodes.retained_mode.managers.library_manager import LibraryManager


class DiscoveredLibrary(NamedTuple):
    """Information about a discovered library.

    Attributes:
        path: Absolute path to the library JSON file or sandbox directory
        is_sandbox: True if this is a sandbox library (user-created nodes in workspace), False for regular libraries
        enabled: False when the entry is present in libraries_to_register but explicitly disabled.
            Disabled libraries are still discovered (so they appear in status output) but are not loaded.
    """

    path: Path
    is_sandbox: bool
    enabled: bool = True


@dataclass
@PayloadRegistry.register
class ListRegisteredLibrariesRequest(RequestPayload):
    """List all currently registered libraries.

    Use when: Displaying available libraries, checking library availability,
    building library selection UIs, debugging library registration.

    Results: ListRegisteredLibrariesResultSuccess (with library names) | ListRegisteredLibrariesResultFailure (system error)
    """


@dataclass
@PayloadRegistry.register
class ListRegisteredLibrariesResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Registered libraries listed successfully.

    Args:
        libraries: List of registered library names
    """

    libraries: list[str]


@dataclass
@PayloadRegistry.register
class ListRegisteredLibrariesResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Library listing failed. Common causes: registry not initialized, system error."""


@dataclass
@PayloadRegistry.register
class ListCapableLibraryEventHandlersRequest(RequestPayload):
    """List libraries capable of handling a specific event type.

    Use when: Finding libraries that can process specific events, implementing event routing,
    library capability discovery, debugging event handling.

    Results: ListCapableLibraryEventHandlersResultSuccess (with handler names) | ListCapableLibraryEventHandlersResultFailure (query error)
    """

    request_type: str


@dataclass
class LibraryEventHandlerDetails:
    """Presentation metadata for a single capable library event handler.

    Carries the optional, human-facing fields a library may register alongside its
    handler so a frontend can render the handler in a menu/dropdown without falling
    back to the raw library name. All presentation fields are optional; when they are
    None the frontend should preserve today's behavior (render the library name, no
    description, no icon).

    Args:
        library_name: Name of the library that registered the handler. Matches the
            corresponding entry in ListCapableLibraryEventHandlersResultSuccess.handlers.
        display_name: Optional human-readable name for the handler (e.g. a publishing
            target). None means fall back to library_name.
        description: Optional short description shown alongside the handler. None means
            no description.
        icon: Optional icon identifier — a Lucide icon name or a path/URL to an image
            the frontend renders as-is (not raw image data). None means no icon.
    """

    library_name: str
    display_name: str | None = None
    description: str | None = None
    icon: str | None = None


@dataclass
@PayloadRegistry.register
class ListCapableLibraryEventHandlersResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Event handlers listed successfully.

    Args:
        handlers: List of library names capable of handling the event type.
        handler_details: Per-handler presentation metadata, one entry per library in
            ``handlers`` (same order). Present so a frontend can render richer menu
            entries (display name, description, icon) for handlers whose registration
            supplied them. Handlers that registered no presentation metadata still get
            an entry with only ``library_name`` populated, so existing consumers that
            read only ``handlers`` are unaffected.
    """

    handlers: list[str]
    handler_details: list[LibraryEventHandlerDetails] = field(default_factory=list)


@dataclass
@PayloadRegistry.register
class ListCapableLibraryEventHandlersResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Event handlers listing failed. Common causes: invalid event type, registry error."""


@dataclass
@PayloadRegistry.register
class ListNodeTypesInLibraryRequest(RequestPayload):
    """List all node types available in a specific library.

    Use when: Discovering available nodes, building node creation UIs,
    validating node types, exploring library contents.

    Args:
        library: Name of the library to list node types for

    Results: ListNodeTypesInLibraryResultSuccess (with node types) | ListNodeTypesInLibraryResultFailure (library not found)
    """

    library: str


@dataclass
@PayloadRegistry.register
class ListNodeTypesInLibraryResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Node types in library listed successfully.

    Args:
        node_types: List of node type names available in the library
    """

    node_types: list[str]


@dataclass
@PayloadRegistry.register
class ListNodeTypesInLibraryResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Node types listing failed. Common causes: library not found, library not loaded."""


@dataclass
@PayloadRegistry.register
class GetNodeMetadataFromLibraryRequest(RequestPayload):
    """Get metadata for a specific node type from a library.

    Use when: Inspecting node capabilities, validating node types, building node creation UIs,
    getting parameter definitions, checking node requirements.

    Args:
        library: Name of the library containing the node type
        node_type: Name of the node type to get metadata for

    Results: GetNodeMetadataFromLibraryResultSuccess (with metadata) | GetNodeMetadataFromLibraryResultFailure (node not found)
    """

    library: str
    node_type: str


@dataclass
@PayloadRegistry.register
class GetNodeMetadataFromLibraryResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Node metadata retrieved successfully from library.

    Args:
        metadata: Complete node metadata including parameters, description, requirements
    """

    metadata: NodeMetadata


@dataclass
@PayloadRegistry.register
class GetNodeMetadataFromLibraryResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Node metadata retrieval failed. Common causes: library not found, node type not found, library not loaded."""


@dataclass
class ParameterDescription:
    """Schema of a single parameter on a node type, surfaced without instantiating the node into a flow.

    Args:
        name: Parameter name, used when wiring connections and setting values
        type: Canonical parameter type
        input_types: Accepted input types when used as an input
        output_type: Type produced when used as an output
        default_value: Default value for the parameter (may be None)
        tooltip: General tooltip text (string or structured list)
        tooltip_as_input/property/output: Mode-specific tooltips (None if unset)
        mode_allowed_input/property/output: Whether the parameter supports each mode
        settable: Whether the parameter can be set directly by the user
        ui_options: UI configuration options (includes display_name when set)
        parent_container_name: Name of the parent ParameterGroup, if any
    """

    name: str
    type: str
    input_types: list[str]
    output_type: str
    default_value: Any | None
    tooltip: str | list[dict]
    tooltip_as_input: str | list[dict] | None
    tooltip_as_property: str | list[dict] | None
    tooltip_as_output: str | list[dict] | None
    mode_allowed_input: bool
    mode_allowed_property: bool
    mode_allowed_output: bool
    settable: bool
    ui_options: dict | None
    parent_container_name: str | None

    @classmethod
    def from_parameter(cls, param: Parameter) -> ParameterDescription:
        """Build a ParameterDescription from a live Parameter instance.

        Projects from `Parameter.to_dict()` so this view stays in sync with the canonical
        serialization used elsewhere (e.g. workflow shape extraction).
        """
        param_dict = param.to_dict()

        return cls(
            name=param_dict["name"],
            type=param_dict["type"],
            input_types=list(param_dict["input_types"] or []),
            output_type=param_dict["output_type"] or "",
            default_value=param_dict["default_value"],
            tooltip=param_dict["tooltip"],
            tooltip_as_input=param_dict["tooltip_as_input"],
            tooltip_as_property=param_dict["tooltip_as_property"],
            tooltip_as_output=param_dict["tooltip_as_output"],
            mode_allowed_input=param_dict["mode_allowed_input"],
            mode_allowed_property=param_dict["mode_allowed_property"],
            mode_allowed_output=param_dict["mode_allowed_output"],
            settable=param_dict["settable"],
            ui_options=param_dict["ui_options"],
            parent_container_name=param_dict["parent_container_name"],
        )


@dataclass
@PayloadRegistry.register
class DescribeNodeTypeRequest(RequestPayload):
    """Describe a node type's metadata and parameter schema without adding a node to a flow.

    Use when: choosing between similar node types, building a node-creation UI, or inspecting
    parameter defaults, tooltips, and ui_options before committing to CreateNode. Avoids the
    create/inspect/delete cycle otherwise required to learn a node type's surface area.

    Note: the engine instantiates a throwaway node to read its parameters. Node types whose
    __init__ performs network or disk I/O will incur that cost here as well.

    Args:
        node_type: Name of the node type to describe
        library: Name of the library providing the node type. If omitted and exactly one
            library registers this node type, that library is used; otherwise the request fails.

    Results: DescribeNodeTypeResultSuccess | DescribeNodeTypeResultFailure
    """

    node_type: str
    library: str | None = None


@dataclass
@PayloadRegistry.register
class DescribeNodeTypeResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Node type described successfully.

    Args:
        library: Library that provided the node type
        node_type: Name of the node type
        metadata: Library-level node metadata (category, description, display_name, tags, icon, color, group)
        parameters: Parameter schemas in declaration order. Empty when the engine could not
            instantiate the probe node used to read parameter declarations (typically because the
            node's `__init__` performed I/O that failed). When that happens, the node-level
            `metadata` is still valid and `result_details` carries a WARNING-level entry naming
            the cause, so callers can distinguish a probe failure from a node that legitimately
            declares no parameters.
    """

    library: str
    node_type: str
    metadata: NodeMetadata
    parameters: list[ParameterDescription] = field(default_factory=list)


@dataclass
@PayloadRegistry.register
class DescribeNodeTypeResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Node type description failed. Common causes: library not registered, node type not found, node instantiation raised."""


@dataclass
@PayloadRegistry.register
class RegisterSandboxNodeFromSourceRequest(RequestPayload):
    """Register the BaseNode subclasses declared in a Python source file already on disk inside the sandbox library directory.

    Use when: An agent has just written a `.py` file into the configured sandbox directory
    (via `WriteFileRequest` or any other means) and wants the engine to import it and
    register its `BaseNode` subclasses without restarting. The engine never writes anything
    itself; it only imports and registers what is already at `file_path`. The new node types
    are immediately usable via `CreateNodeRequest` / `CreateNodesRequest`.

    The file persists on disk, so subsequent engine startups pick it up through the normal
    sandbox scan-and-load pipeline. Auto-generated metadata follows the same conventions the
    engine applies to files placed in the sandbox directory through the UI.

    Security note: the imported source runs inside the engine process with no isolation.
    Anyone who can reach this request, or who can write into the sandbox directory, can
    execute arbitrary Python. This mirrors the existing sandbox-directory behaviour, but
    makes that surface reachable via MCP.

    Args:
        file_path: Path to a `.py` file inside the configured sandbox library directory.
            Absolute paths must resolve under the sandbox directory; relative paths are
            resolved against it. The file must already exist on disk.
        replace_if_exists: When True, any node type of the same class name already registered
            in the Sandbox Library is unregistered before registering the new class. Existing
            node instances of that class in a running workflow are NOT migrated.

    Results: RegisterSandboxNodeFromSourceResultSuccess | RegisterSandboxNodeFromSourceResultFailure
    """

    file_path: str
    replace_if_exists: bool = True


@dataclass
@PayloadRegistry.register
class RegisterSandboxNodeFromSourceResultSuccess(WorkflowAlteredMixin, ResultPayloadSuccess):
    """Source file imported and at least one BaseNode subclass registered.

    Args:
        file_path: Absolute path to the .py file that was imported.
        library_name: Name of the library the class(es) were registered with (always the
            Sandbox Library for now).
        registered_class_names: Node class names that were registered by this call, in module
            declaration order.
        replaced_class_names: Subset of registered_class_names for which an existing
            registration was unregistered first (only meaningful when replace_if_exists=True).
    """

    file_path: str
    library_name: str
    registered_class_names: list[str] = field(default_factory=list)
    replaced_class_names: list[str] = field(default_factory=list)


@dataclass
@PayloadRegistry.register
class RegisterSandboxNodeFromSourceResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Sandbox node registration failed.

    Common causes: sandbox_library_directory not configured, file_path resolves outside the
    sandbox directory or has the wrong extension, file does not exist, Python import error
    (syntax error, missing dependency), no BaseNode subclass found in the file, or name
    collision when replace_if_exists=False.
    """


@dataclass
@PayloadRegistry.register
class LoadLibraryMetadataFromFileRequest(RequestPayload):
    """Request to load library metadata from a JSON file without loading node modules.

    This provides a lightweight way to get library schema information without the overhead
    of dynamically importing Python modules. Useful for metadata queries, validation,
    and library discovery operations.

    Args:
        file_path: Absolute path to the library JSON schema file to load.
    """

    file_path: str


@dataclass
@PayloadRegistry.register
class LoadLibraryMetadataFromFileResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Successful result from loading library metadata.

    Contains the validated library schema that can be used for metadata queries,
    node type discovery, and other operations that don't require the actual
    node classes to be loaded.

    Args:
        library_schema: The validated LibrarySchema object containing all metadata
                       about the library including nodes, categories, and settings.
        file_path: The file path from which the library metadata was loaded (resolved
                   absolute path on disk).
        registered_path: The user's verbatim `LibraryRegistration.path` from
                         `libraries_to_register` before workspace resolution / `~`-expansion
                         / symlink-following. Surfaced so the GUI can match library metadata
                         back to its `libraries_to_register` row using the exact key the user
                         sees in their config. None for libraries registered through other
                         channels (e.g. sandbox, ad-hoc loads).
        git_remote: The git remote URL if the library is in a git repository, None otherwise.
        git_ref: The current git reference (branch, tag, or commit) if the library is in a git repository, None otherwise.
        enabled: If the current library is enabled or disabled by the user at the time of the request.
    """

    library_schema: LibrarySchema
    file_path: str
    git_remote: str | None
    git_ref: str | None
    enabled: bool
    registered_path: str | None = None


@dataclass
@PayloadRegistry.register
class LoadLibraryMetadataFromFileResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Failed result from loading library metadata with detailed error information.

    Provides comprehensive error details including the specific failure type and
    a list of problems encountered during loading. This allows callers to understand
    exactly what went wrong and take appropriate action.

    Args:
        library_path: Path to the library file that failed to load.
        library_name: Name of the library if it could be extracted from the JSON,
                     None if the name couldn't be determined.
        status: The LibraryFitness enum indicating the type of failure
               (MISSING, UNUSABLE, etc.).
        problems: List of specific problems encountered during loading
                 (file not found, JSON parse errors, validation failures, etc.).
        library_version: Version of the library if it could be extracted from the raw JSON,
                        None if it couldn't be determined (e.g. invalid JSON). Surfaced so
                        status output can show the real version even when the schema failed to
                        validate.
    """

    library_path: str
    library_name: str | None
    status: LibraryManager.LibraryFitness
    problems: list[LibraryProblem]
    library_version: str | None = None


@dataclass
@PayloadRegistry.register
class LoadMetadataForAllLibrariesRequest(RequestPayload):
    """Request to load metadata for all libraries from configuration without loading node modules.

    This loads metadata from both:
    1. Library JSON files specified in configuration
    2. Sandbox library (dynamically generated from Python files)

    Provides a lightweight way to discover all available libraries and their schemas
    without the overhead of importing Python modules or registering them in the system.
    """


@dataclass
@PayloadRegistry.register
class LoadMetadataForAllLibrariesResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Successful result from loading metadata for all libraries.

    Contains metadata for all discoverable libraries from both configuration files
    and sandbox directory, with clear separation between successful loads and failures.

    Args:
        successful_libraries: List of successful library metadata loading results,
                             including both config-based libraries and sandbox library if applicable.
        failed_libraries: List of detailed failure results for libraries that couldn't be loaded,
                         including both config-based libraries and sandbox library if applicable.
    """

    successful_libraries: list[LoadLibraryMetadataFromFileResultSuccess]
    failed_libraries: list[LoadLibraryMetadataFromFileResultFailure]


@dataclass
@PayloadRegistry.register
class LoadMetadataForAllLibrariesResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Failed result from loading metadata for all libraries.

    This indicates a systemic failure (e.g., configuration access issues)
    rather than individual library loading failures, which are captured
    in the success result's failed_libraries list.
    """


@dataclass
@PayloadRegistry.register
class ScanSandboxDirectoryRequest(RequestPayload):
    """Scan sandbox directory and generate/update library metadata.

    This request triggers a scan of a sandbox directory,
    discovers Python files, and either creates a new library schema or
    merges with an existing griptape_nodes_library.json if present.

    Use when: Manually triggering sandbox refresh, testing sandbox setup,
    forcing regeneration of sandbox library metadata.

    Args:
        directory_path: Path to sandbox directory to scan (required).

    Results: ScanSandboxDirectoryResultSuccess | ScanSandboxDirectoryResultFailure
    """

    directory_path: str


@dataclass
@PayloadRegistry.register
class ScanSandboxDirectoryResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Sandbox directory scanned successfully.

    Args:
        library_schema: The generated or merged LibrarySchema
    """

    library_schema: LibrarySchema


@dataclass
@PayloadRegistry.register
class ScanSandboxDirectoryResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Sandbox directory scan failed.

    Common causes: directory doesn't exist, no Python files found, internal error.
    """


@dataclass
@PayloadRegistry.register
class RegisterLibraryFromFileRequest(RequestPayload):
    """Register a library by name or path, progressing through all lifecycle phases.

    This request handles the complete library loading lifecycle:
    DISCOVERED → METADATA_LOADED → EVALUATED → DEPENDENCIES_INSTALLED → LOADED

    The handler automatically creates LibraryInfo if not already tracked, making it suitable
    for both internal use (from load_all_libraries_from_config) and external use (scripts, tests, API).

    Use when: Loading custom libraries, adding new node types,
    registering development libraries, extending node capabilities.

    Args:
        library_name: Name of library to load (must match library JSON 'name' field). Either library_name OR file_path required (not both).
        file_path: Path to library JSON file. Either library_name OR file_path required (not both).
        perform_discovery_if_not_found: If True and library not found, trigger discovery (default: False)
        load_as_default_library: Whether to mark this library as the default (default: False)

    Results: RegisterLibraryFromFileResultSuccess (with library name) | RegisterLibraryFromFileResultFailure (load error)
    """

    library_name: str | None = None
    file_path: str | None = None
    perform_discovery_if_not_found: bool = False
    load_as_default_library: bool = False


@dataclass
@PayloadRegistry.register
class RegisterLibraryFromFileResultSuccess(WorkflowAlteredMixin, ResultPayloadSuccess):
    """Library registered successfully from file.

    Args:
        library_name: Name of the registered library
        was_already_loaded: True if the library was already loaded, False if it was loaded by this request
    """

    library_name: str
    was_already_loaded: bool = False


@dataclass
@PayloadRegistry.register
class RegisterLibraryFromFileResultFailure(ResultPayloadFailure):
    """Library registration from file failed. Common causes: file not found, invalid format, load error."""


@dataclass
@PayloadRegistry.register
class RegisterLibraryFromRequirementSpecifierRequest(RequestPayload):
    """Register a library from a requirement specifier (e.g., package name).

    Use when: Installing libraries from package managers, adding dependencies,
    registering third-party libraries, dynamic library loading.

    Results: RegisterLibraryFromRequirementSpecifierResultSuccess (with library name) | RegisterLibraryFromRequirementSpecifierResultFailure (install error)
    """

    requirement_specifier: str
    library_config_name: str = "griptape_nodes_library.json"


@dataclass
@PayloadRegistry.register
class RegisterLibraryFromRequirementSpecifierResultSuccess(WorkflowAlteredMixin, ResultPayloadSuccess):
    """Library registered successfully from requirement specifier.

    Args:
        library_name: Name of the registered library
    """

    library_name: str


@dataclass
@PayloadRegistry.register
class RegisterLibraryFromRequirementSpecifierResultFailure(ResultPayloadFailure):
    """Library registration from requirement specifier failed. Common causes: package not found, installation error, invalid specifier."""


@dataclass
@PayloadRegistry.register
class ListCategoriesInLibraryRequest(RequestPayload):
    """List all categories available in a library.

    Use when: Building category-based UIs, organizing node selection,
    browsing library contents, implementing filters.

    Results: ListCategoriesInLibraryResultSuccess (with categories) | ListCategoriesInLibraryResultFailure (library not found)
    """

    library: str


@dataclass
@PayloadRegistry.register
class ListCategoriesInLibraryResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Library categories listed successfully.

    Args:
        categories: List of category dictionaries with names, descriptions, and metadata
    """

    categories: list[dict]


@dataclass
@PayloadRegistry.register
class ListCategoriesInLibraryResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Library categories listing failed. Common causes: library not found, library not loaded."""


@dataclass
@PayloadRegistry.register
class GetLibraryMetadataRequest(RequestPayload):
    """Get metadata for a specific library.

    Use when: Inspecting library properties, displaying library information,
    checking library versions, validating library compatibility.

    Results: GetLibraryMetadataResultSuccess (with metadata) | GetLibraryMetadataResultFailure (library not found)
    """

    library: str


@dataclass
@PayloadRegistry.register
class GetLibraryMetadataResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Library metadata retrieved successfully.

    Args:
        metadata: Complete library metadata including version, description, dependencies
    """

    metadata: LibraryMetadata


@dataclass
@PayloadRegistry.register
class GetLibraryMetadataResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Library metadata retrieval failed. Common causes: library not found, library not loaded.

    Args:
        problems: Collated description of known fitness problems for this library, if any.
    """

    problems: str | None = None


@dataclass
class WidgetInfo:
    """Information about a custom UI widget for the frontend.

    This is included in library info responses so the frontend can
    dynamically load widgets from libraries.
    """

    name: str  # Widget name (e.g., "ColorGradientPicker")
    bundle_url: str  # Full URL where the widget bundle can be fetched
    description: str | None = None  # Optional description


# "Jumbo" event for getting all things say, a GUI might want w/r/t a Library.
@dataclass
@PayloadRegistry.register
class GetAllInfoForLibraryRequest(RequestPayload):
    """Get comprehensive information for a library in a single call.

    Use when: Populating library UIs, implementing library inspection,
    gathering complete library state, optimizing multiple info requests.

    Results: GetAllInfoForLibraryResultSuccess (with comprehensive info) | GetAllInfoForLibraryResultFailure (library not found)
    """

    library: str


@dataclass
@PayloadRegistry.register
class GetAllInfoForLibraryResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Comprehensive library information retrieved successfully.

    Args:
        library_metadata_details: Library metadata and version information
        category_details: All categories available in the library
        node_type_name_to_node_metadata_details: Complete node metadata for each node type
        widgets: Custom UI widgets provided by the library (if any)
    """

    library_metadata_details: GetLibraryMetadataResultSuccess
    category_details: ListCategoriesInLibraryResultSuccess
    node_type_name_to_node_metadata_details: dict[str, GetNodeMetadataFromLibraryResultSuccess]
    widgets: list[WidgetInfo] | None = None


@dataclass
@PayloadRegistry.register
class GetAllInfoForLibraryResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Comprehensive library information retrieval failed. Common causes: library not found, library not loaded, partial failure."""


# The "Jumbo-est" of them all. Grabs all info for all libraries in one fell swoop.
@dataclass
@PayloadRegistry.register
class GetAllInfoForAllLibrariesRequest(RequestPayload):
    """Get comprehensive information for all libraries in a single call.

    Use when: Populating complete library catalogs, implementing library browsers,
    gathering system-wide library state, optimizing bulk library operations.

    Results: GetAllInfoForAllLibrariesResultSuccess (with all library info) | GetAllInfoForAllLibrariesResultFailure (system error)
    """


@dataclass
@PayloadRegistry.register
class GetAllInfoForAllLibrariesResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Comprehensive information for all libraries retrieved successfully.

    Args:
        library_name_to_library_info: Complete information for each registered library
    """

    library_name_to_library_info: dict[str, GetAllInfoForLibraryResultSuccess]


@dataclass
@PayloadRegistry.register
class GetAllInfoForAllLibrariesResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Comprehensive information retrieval for all libraries failed. Common causes: registry not initialized, system error."""


@dataclass
@PayloadRegistry.register
class UnloadLibraryFromRegistryRequest(RequestPayload):
    """Unload a library from the registry.

    Use when: Removing unused libraries, cleaning up library registry,
    preparing for library updates, troubleshooting library issues.

    Args:
        library_name: Name of the library to unload from the registry

    Results: UnloadLibraryFromRegistryResultSuccess | UnloadLibraryFromRegistryResultFailure (library not found, unload error)
    """

    library_name: str


@dataclass
@PayloadRegistry.register
class UnloadLibraryFromRegistryResultSuccess(WorkflowAlteredMixin, ResultPayloadSuccess):
    """Library unloaded successfully from registry."""


@dataclass
@PayloadRegistry.register
class UnloadLibraryFromRegistryResultFailure(ResultPayloadFailure):
    """Library unload failed. Common causes: library not found, library in use, unload error."""


@dataclass
@PayloadRegistry.register
class ReloadAllLibrariesRequest(RequestPayload):
    """WARNING: This request will CLEAR ALL CURRENT WORKFLOW STATE!

    Reloading all libraries requires clearing all existing workflows, nodes, and execution state
    because there is no way to comprehensively erase references to old Python modules.
    All current work will be lost and must be recreated after the reload operation completes.

    Use this operation only when you need to pick up changes to library code during development
    or when library corruption requires a complete reset.
    """


@dataclass
@PayloadRegistry.register
class ReloadAllLibrariesResultSuccess(WorkflowAlteredMixin, ResultPayloadSuccess):
    """All libraries reloaded successfully. All workflow state has been cleared."""


@dataclass
@PayloadRegistry.register
class ReloadAllLibrariesResultFailure(ResultPayloadFailure):
    """Library reload failed. Common causes: library loading errors, system constraints, initialization failures."""


@dataclass
@PayloadRegistry.register
class DiscoverLibrariesRequest(RequestPayload):
    """Discover all libraries from configuration.

    Scans configured library paths and creates LibraryInfo entries in 'discovered' state.
    This does not load any library contents - just identifies what's available.

    Use when: Refreshing library catalog, checking for new libraries, initializing
    library tracking before selective loading.

    Results: DiscoverLibrariesResultSuccess | DiscoverLibrariesResultFailure
    """

    include_sandbox: bool = True  # Whether to include sandbox library in discovery


@dataclass
@PayloadRegistry.register
class DiscoverLibrariesResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Libraries discovered successfully."""

    libraries_discovered: list[DiscoveredLibrary]  # Discovered libraries in config order


@dataclass
@PayloadRegistry.register
class DiscoverLibrariesResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Library discovery failed."""


@dataclass
@PayloadRegistry.register
class EvaluateLibraryFitnessRequest(RequestPayload):
    """Evaluate a library's fitness (compatibility with current engine).

    Checks version compatibility and determines if the library can be loaded.
    Does not actually load Python modules - just validates compatibility.

    Args:
        schema: The loaded LibrarySchema from metadata loading

    Results: EvaluateLibraryFitnessResultSuccess | EvaluateLibraryFitnessResultFailure
    """

    schema: LibrarySchema


@dataclass
@PayloadRegistry.register
class EvaluateLibraryFitnessResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Library fitness evaluation successful.

    Returns fitness and any non-fatal problems (warnings).
    Caller manages their own lifecycle state.
    """

    fitness: LibraryManager.LibraryFitness
    problems: list[LibraryProblem]


@dataclass
@PayloadRegistry.register
class EvaluateLibraryFitnessResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Library fitness evaluation failed - library is not fit for this engine.

    Returns fitness and problems for caller to update their LibraryInfo.
    """

    fitness: LibraryManager.LibraryFitness
    problems: list[LibraryProblem]


@dataclass
@PayloadRegistry.register
class LoadLibrariesRequest(RequestPayload):
    """Load all libraries from configuration if they are not already loaded.

    This is a non-destructive operation that checks if libraries are already loaded
    and only performs the initial loading if needed. Unlike ReloadAllLibrariesRequest,
    this does NOT clear any workflow state.

    Use when: Ensuring libraries are loaded at workflow startup, initializing library
    system on demand, preparing library catalog without disrupting existing workflows.

    Results: LoadLibrariesResultSuccess | LoadLibrariesResultFailure (loading error)
    """


@dataclass
@PayloadRegistry.register
class LoadLibrariesResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Libraries loaded successfully (or were already loaded)."""


@dataclass
@PayloadRegistry.register
class LoadLibrariesResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Library loading failed. Common causes: library loading errors, configuration issues, initialization failures."""


@dataclass
@PayloadRegistry.register
class CheckLibraryUpdateRequest(RequestPayload):
    """Check if a library has updates available via git.

    Use when: Checking for library updates, displaying update status,
    validating library versions, implementing update notifications.

    Args:
        library_name: Name of the library to check for updates

    Results: CheckLibraryUpdateResultSuccess (with update info) | CheckLibraryUpdateResultFailure (library not found, not a git repo, check error)
    """

    library_name: str


@dataclass
@PayloadRegistry.register
class CheckLibraryUpdateResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Library update check completed successfully.

    Updates are detected based on either version changes or commit differences:
    - If remote version > local version: update available (semantic versioning)
    - If remote version < local version: no update (prevent regression)
    - If versions equal: compare commits; if different, update available

    Args:
        has_update: True if an update is available, False otherwise
        current_version: The current library version
        latest_version: The latest library version from remote
        git_remote: The git remote URL
        git_ref: The current git reference (branch, tag, or commit)
        local_commit: The local HEAD commit SHA (None if not a git repository)
        remote_commit: The remote HEAD commit SHA (None if not available)
        update_gated_by_age: True when an update exists but is withheld because the target commit
            is younger than the configured soak period (library.update_age_gating_enabled). When
            True, has_update is also True.
        target_commit_age_hours: Age in hours of the target commit at check time, or None when
            unknown (e.g. no update available or the commit timestamp could not be read).
        update_min_age_hours: The configured minimum age in hours, or None when age gating is disabled.
    """

    has_update: bool
    current_version: str | None
    latest_version: str | None
    git_remote: str | None
    git_ref: str | None
    local_commit: str | None
    remote_commit: str | None
    update_gated_by_age: bool = False
    target_commit_age_hours: float | None = None
    update_min_age_hours: float | None = None


@dataclass
@PayloadRegistry.register
class CheckLibraryUpdateResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Library update check failed. Common causes: library not found, not a git repository, git remote error, network error."""


@dataclass
@PayloadRegistry.register
class UpdateLibraryRequest(RequestPayload):
    """Update a library to the latest version using the appropriate git strategy.

    Automatically detects whether the library uses branch-based or tag-based workflow:
    - Branch-based: Uses git fetch + git reset --hard (forces local to match remote)
    - Tag-based: Uses git fetch --tags --force + git checkout (for moving tags like 'latest')

    Use when: Applying library updates, synchronizing with remote changes,
    updating library versions, implementing auto-update features.

    Args:
        library_name: Name of the library to update
        overwrite_existing: If True, discard any uncommitted local changes. If False, fail if uncommitted changes exist (default: False)

    Results: UpdateLibraryResultSuccess (with version info) | UpdateLibraryResultFailure (library not found, git error, update failure)
    """

    library_name: str
    overwrite_existing: bool = False


@dataclass
@PayloadRegistry.register
class UpdateLibraryResultSuccess(WorkflowAlteredMixin, ResultPayloadSuccess):
    """Library updated successfully.

    Args:
        old_version: The previous library version
        new_version: The new library version after update
    """

    old_version: str
    new_version: str


@dataclass
@PayloadRegistry.register
class UpdateLibraryResultFailure(ResultPayloadFailure):
    """Library update failed. Common causes: library not found, not a git repository, git pull error, uncommitted changes.

    Args:
        retryable: If True, the operation can be retried with overwrite_existing=True
        existing_path: When the failure is caused by uncommitted changes in the library directory,
            the absolute path of that directory. Provided as a structured field so clients do not
            have to parse it out of the human-readable error message (which is unreliable for paths
            containing ``:``, e.g. Windows drive letters).
        age_gated: True when the update was withheld because the target commit is younger than the
            configured soak period (library.update_age_gating_enabled). This is not a hard error;
            the update will succeed once the target commit is old enough.
    """

    retryable: bool = False
    existing_path: str | None = None
    age_gated: bool = False


@dataclass
@PayloadRegistry.register
class SwitchLibraryRefRequest(RequestPayload):
    """Switch a library to a different git branch or tag.

    Supports switching to both branches and tags (e.g., 'main', 'develop', 'latest', 'v1.0.0').

    Use when: Switching between branches for development, testing different versions,
    reverting to stable branches, checking out feature branches, or switching to specific tags.

    Args:
        library_name: Name of the library to switch
        ref_name: Name of the branch or tag to switch to

    Results: SwitchLibraryRefResultSuccess (with ref/version info) | SwitchLibraryRefResultFailure (library not found, git error, ref not found)
    """

    library_name: str
    ref_name: str


@dataclass
@PayloadRegistry.register
class SwitchLibraryRefResultSuccess(WorkflowAlteredMixin, ResultPayloadSuccess):
    """Library branch or tag switched successfully.

    Args:
        old_ref: The previous branch or tag name
        new_ref: The new branch or tag name after switch
        old_version: The previous library version
        new_version: The new library version after switch
    """

    old_ref: str
    new_ref: str
    old_version: str
    new_version: str


@dataclass
@PayloadRegistry.register
class SwitchLibraryRefResultFailure(ResultPayloadFailure):
    """Library ref switch failed. Common causes: library not found, not a git repository, ref not found, git checkout error."""


@dataclass
@PayloadRegistry.register
class DownloadLibraryRequest(RequestPayload):
    """Download a library from a git repository.

    Use when: Installing new libraries from git repositories, downloading third-party libraries,
    setting up development libraries, adding community libraries.

    Args:
        git_url: The git repository URL to clone
        branch_tag_commit: Optional branch, tag, or commit to checkout (defaults to default branch)
        target_directory_name: Optional name for the target directory (defaults to repository name)
        download_directory: Optional parent directory path for download (defaults to workspace/libraries)
        overwrite_existing: If True, delete existing directory before cloning (default: False)
        auto_register: If True, automatically register library after download (default: True)
        fail_on_exists: If True, fail with retryable error when directory exists and overwrite_existing=False.
                       If False, skip clone and register existing library (idempotent). (default: True)

    Results: DownloadLibraryResultSuccess (with library info) | DownloadLibraryResultFailure (clone error, directory exists)
    """

    git_url: str
    branch_tag_commit: str | None = None
    target_directory_name: str | None = None
    download_directory: str | None = None
    overwrite_existing: bool = False
    auto_register: bool = True
    fail_on_exists: bool = True


@dataclass
@PayloadRegistry.register
class DownloadLibraryResultSuccess(WorkflowAlteredMixin, ResultPayloadSuccess):
    """Library downloaded successfully.

    Args:
        library_name: Name of the library extracted from griptape_nodes_library.json
        library_path: Full path where the library was downloaded
    """

    library_name: str
    library_path: str


@dataclass
@PayloadRegistry.register
class DownloadLibraryResultFailure(ResultPayloadFailure):
    """Library download failed. Common causes: invalid git URL, network error, target directory already exists, no griptape_nodes_library.json found.

    Args:
        retryable: If True, the operation can be retried with overwrite_existing=True
        existing_path: When the failure is caused by an existing target directory, the absolute
            path of that directory. Provided as a structured field so clients do not have to
            parse it out of the human-readable error message.
    """

    retryable: bool = False
    existing_path: str | None = None


@dataclass
@PayloadRegistry.register
class InstallLibraryDependenciesRequest(RequestPayload):
    """Install dependencies for a library.

    Use when: Installing or reinstalling dependencies for a library,
    setting up a library's environment, updating dependencies after changes.

    This operation:
    1. Loads library metadata from the file
    2. Gets library dependencies from metadata
    3. Initializes the library's virtual environment
    4. Installs pip dependencies specified in the library metadata
    5. Always installs dependencies without version checks

    Args:
        library_file_path: Path to the library JSON file

    Results: InstallLibraryDependenciesResultSuccess | InstallLibraryDependenciesResultFailure
    """

    library_file_path: str


@dataclass
@PayloadRegistry.register
class InstallLibraryDependenciesResultSuccess(ResultPayloadSuccess):
    """Library dependencies installed successfully.

    Args:
        library_name: Name of the library whose dependencies were installed
        dependencies_installed: Number of dependencies that were installed
    """

    library_name: str
    dependencies_installed: int


@dataclass
@PayloadRegistry.register
class InstallLibraryDependenciesResultFailure(ResultPayloadFailure):
    """Library dependency installation failed. Common causes: library not found, no dependencies defined, venv initialization failed, pip install error."""


@dataclass
@PayloadRegistry.register
class SyncLibrariesRequest(RequestPayload):
    """Sync all libraries to latest versions and ensure dependencies are installed.

    Similar to `uv sync` - ensures workspace is in a consistent, up-to-date state.
    This operation:
    1. Downloads missing libraries from git URLs specified in config
    2. Gets all registered libraries (including newly downloaded)
    3. Checks each library for available updates
    4. Updates libraries that have updates available
    5. Installs/updates dependencies for all libraries
    6. Returns comprehensive summary of changes

    Use when: Updating workspace to latest versions, ensuring all libraries are
    up-to-date, setting up development environment, periodic maintenance.

    Args:
        overwrite_existing: If True, discard any uncommitted local changes when updating libraries. If False, fail if uncommitted changes exist (default: False)

    Results: SyncLibrariesResultSuccess (with summary) | SyncLibrariesResultFailure (sync errors)
    """

    overwrite_existing: bool = False


@dataclass
@PayloadRegistry.register
class SyncLibrariesResultSuccess(WorkflowAlteredMixin, ResultPayloadSuccess):
    """Libraries synced successfully.

    Args:
        libraries_downloaded: Number of libraries that were downloaded from git URLs
        libraries_checked: Number of libraries checked for updates
        libraries_updated: Number of libraries that were updated
        libraries_deferred: Number of libraries with an available update that was withheld by the
            age gate (soak period) and therefore not applied this sync
        update_summary: Dict mapping library names to their update info (old_version -> new_version, or status for downloads)
    """

    libraries_downloaded: int
    libraries_checked: int
    libraries_updated: int
    update_summary: dict[str, dict[str, str]]
    libraries_deferred: int = 0


@dataclass
@PayloadRegistry.register
class SyncLibrariesResultFailure(ResultPayloadFailure):
    """Library sync failed. Common causes: git errors, network errors, dependency installation failures."""


@dataclass
@PayloadRegistry.register
class InspectLibraryRepoRequest(RequestPayload):
    """Inspect a library's metadata from a git repository without downloading the full repository.

    Performs a sparse checkout to fetch only the library JSON file, which is efficient for
    previewing library information, checking compatibility, or validating git URLs before
    full download.

    Use when: Previewing library details, displaying library information in UI,
    validating library compatibility, checking library versions remotely.

    Args:
        git_url: Git repository URL (supports GitHub shorthand like "user/repo")
        ref: Branch, tag, or commit to inspect (defaults to "HEAD")

    Results: InspectLibraryRepoResultSuccess (with library metadata) | InspectLibraryRepoResultFailure (invalid URL, network error, no library JSON found)
    """

    git_url: str
    ref: str = "HEAD"


@dataclass
@PayloadRegistry.register
class InspectLibraryRepoResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Library repository inspection completed successfully.

    Args:
        library_schema: Complete library schema with all metadata (name, version, nodes, categories, dependencies, settings, etc.)
        commit_sha: Git commit SHA that was inspected
        git_url: Git URL that was inspected (normalized)
        ref: Git reference that was inspected
    """

    library_schema: LibrarySchema
    commit_sha: str
    git_url: str
    ref: str


@dataclass
@PayloadRegistry.register
class InspectLibraryRepoResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Library repository inspection failed. Common causes: invalid git URL, network error, no library JSON found, invalid JSON format."""


@dataclass
@PayloadRegistry.register
class GetLibrarySourceInfoRequest(RequestPayload):
    """Get filesystem paths for a registered library's source code.

    Use when: Locating library source files on disk, reading node source code.

    Args:
        library: Name of the registered library (e.g. "Griptape Nodes Library")

    Results: GetLibrarySourceInfoResultSuccess (with paths) | GetLibrarySourceInfoResultFailure (library not found)
    """

    library: str


@dataclass
@PayloadRegistry.register
class GetLibrarySourceInfoResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Library source info retrieved successfully.

    Args:
        library_name: Echo of the requested library name
        library_json_path: Absolute path to the library's griptape_nodes_library.json
        library_directory: Absolute path to the directory containing the JSON file
    """

    library_name: str
    library_json_path: str
    library_directory: str


@dataclass
@PayloadRegistry.register
class GetLibrarySourceInfoResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Library source info retrieval failed. Common causes: library not found, library not yet loaded."""


@dataclass
@PayloadRegistry.register
class GetEngineSourceInfoRequest(RequestPayload):
    """Get the filesystem path of the griptape_nodes engine source tree.

    Use when: Reading engine base class definitions (e.g. exe_types/node_types.py),
    inspecting engine internals, locating engine source code on disk.

    Results: GetEngineSourceInfoResultSuccess (with path) | GetEngineSourceInfoResultFailure (resolution error)
    """


@dataclass
@PayloadRegistry.register
class GetEngineSourceInfoResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Engine source info retrieved successfully.

    Args:
        package_directory: Absolute path to the griptape_nodes package root
    """

    package_directory: str


@dataclass
@PayloadRegistry.register
class GetEngineSourceInfoResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Engine source info retrieval failed. Common causes: package path could not be resolved."""


class LibraryProvisioningActionKind(StrEnum):
    """What provisioning will do to a single sourced library."""

    SKIP = "SKIP"  # installed version already satisfies the entry
    INSTALL = "INSTALL"  # not installed -> fresh install (non-destructive)
    OVERWRITE = "OVERWRITE"  # wrong version installed -> replace


@dataclass
class LibraryProvisioningAction:
    """The planned provisioning outcome for one libraries_to_download entry.

    Computed by a pure registry-read + PEP 440 compare so the preview and the
    real execution derive from the same decision. `destructive` is True ONLY for
    a git OVERWRITE, the path that deletes the local library directory before
    re-cloning; INSTALL/SKIP never are.

    Fields:
        library_name: Library name, matching the download entry's `name`.
        kind: SKIP / INSTALL / OVERWRITE.
        installed_version: Currently registered version, or None when not installed.
        pinned_version: The download entry's PEP 440 version specifier, or None for a source-only entry.
        git_url: The download entry's git source (url@ref form).
        git_ref: The branch/tag/commit parsed from `git_url`, when present.
        destructive: True only for a git OVERWRITE (deletes the local dir).
        reason: Human-readable explanation of the decision.
    """

    library_name: str
    kind: LibraryProvisioningActionKind
    installed_version: str | None
    pinned_version: str | None
    git_url: str | None
    git_ref: str | None
    destructive: bool
    reason: str


@dataclass
@PayloadRegistry.register
class PreviewProjectProvisioningRequest(RequestPayload):
    """Compute, without touching disk, what activating a project would provision.

    Use when: the UI wants to show the user which libraries_to_download entries
    will be installed or overwritten before committing to a project switch.
    Read-only: a registry read plus a PEP 440 compare, no clone/venv/delete work.
    Reads the target project's project-adjacent config without mutating the live config layers.

    Args:
        project_id: Identifier of an already-loaded project to preview.

    Results: PreviewProjectProvisioningResultSuccess | PreviewProjectProvisioningResultFailure
    """

    project_id: str


@dataclass
@PayloadRegistry.register
class PreviewProjectProvisioningResultSuccess(WorkflowNotAlteredMixin, ResultPayloadSuccess):
    """Provisioning plan computed.

    Args:
        actions: One action per libraries_to_download entry, in config order. Empty when the
            project declares no libraries to download/provision.
        engine_version_failure: Non-None when the project's pinned `requires_engine`
            cannot be satisfied by the running engine. The same text the live
            reconcile would surface, computed on the same merged config the preview
            reads, so the UI can warn before the user approves a plan that would
            fail the engine_version gate on activation.
    """

    actions: list[LibraryProvisioningAction]
    engine_version_failure: str | None = None


@dataclass
@PayloadRegistry.register
class PreviewProjectProvisioningResultFailure(WorkflowNotAlteredMixin, ResultPayloadFailure):
    """Provisioning plan could not be computed (project not loaded)."""
