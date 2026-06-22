from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import logging
import os
import platform
import shutil
import subprocess
import sys
import sysconfig
from collections import defaultdict
from dataclasses import dataclass, field
from enum import StrEnum
from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING, Any, Generic, NamedTuple, TypeVar, cast

import anyio
from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion
from packaging.version import Version as PackagingVersion
from pydantic import ValidationError
from rich.align import Align
from rich.box import HEAVY_EDGE
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from semver import Version
from xdg_base_dirs import xdg_data_home

from griptape_nodes.common.strict_mode import (
    STRICT_MODE,
    StrictModeScopeKind,
)
from griptape_nodes.common.strict_mode_checks import RULES
from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
from griptape_nodes.exe_types.node_types import BaseNode
from griptape_nodes.files.path_utils import canonicalize_for_identity, canonicalize_for_io, resolve_workspace_path
from griptape_nodes.node_library.library_declarations import (
    LibraryDeclaration,
    LibraryDependencyDeclaration,
    WorkerCompatibility,
    WorkerMode,
    WorkerModeCompatibility,
    requires_worker_process,
)
from griptape_nodes.node_library.library_registry import (
    CategoryDefinition,
    Library,
    LibraryMetadata,
    LibraryNameAndVersion,
    LibraryRegistry,
    LibrarySchema,
    NodeDefinition,
    NodeMetadata,
)
from griptape_nodes.node_library.library_validation import (
    detect_retired_node_declarations,
    validate_library_declarations,
)
from griptape_nodes.retained_mode.events.app_events import (
    AppInitializationComplete,
    AppSessionStartedEvent,
    EngineInitializationProgress,
    GetEngineVersionRequest,
    GetEngineVersionResultSuccess,
    InitializationPhase,
    InitializationStatus,
    LibraryLoadedNotification,
    WorkerNodeSchema,
    WorkerParameterSchema,
)

# Runtime imports for ResultDetails since it's used at runtime
from griptape_nodes.retained_mode.events.base_events import AppEvent, ResultDetail, ResultDetails, ResultPayloadFailure
from griptape_nodes.retained_mode.events.config_events import (
    GetConfigCategoryRequest,
    GetConfigCategoryResultSuccess,
    SetConfigCategoryRequest,
    SetConfigCategoryResultSuccess,
)
from griptape_nodes.retained_mode.events.library_events import (
    CheckLibraryUpdateRequest,
    CheckLibraryUpdateResultFailure,
    CheckLibraryUpdateResultSuccess,
    DescribeNodeTypeRequest,
    DescribeNodeTypeResultFailure,
    DescribeNodeTypeResultSuccess,
    DiscoveredLibrary,
    DiscoverLibrariesRequest,
    DiscoverLibrariesResultFailure,
    DiscoverLibrariesResultSuccess,
    DownloadLibraryRequest,
    DownloadLibraryResultFailure,
    DownloadLibraryResultSuccess,
    EvaluateLibraryFitnessRequest,
    EvaluateLibraryFitnessResultFailure,
    EvaluateLibraryFitnessResultSuccess,
    GetAllInfoForAllLibrariesRequest,
    GetAllInfoForAllLibrariesResultFailure,
    GetAllInfoForAllLibrariesResultSuccess,
    GetAllInfoForLibraryRequest,
    GetAllInfoForLibraryResultFailure,
    GetAllInfoForLibraryResultSuccess,
    GetEngineSourceInfoRequest,
    GetEngineSourceInfoResultFailure,
    GetEngineSourceInfoResultSuccess,
    GetLibraryMetadataRequest,
    GetLibraryMetadataResultFailure,
    GetLibraryMetadataResultSuccess,
    GetLibrarySourceInfoRequest,
    GetLibrarySourceInfoResultFailure,
    GetLibrarySourceInfoResultSuccess,
    GetNodeMetadataFromLibraryRequest,
    GetNodeMetadataFromLibraryResultFailure,
    GetNodeMetadataFromLibraryResultSuccess,
    InspectLibraryRepoRequest,
    InspectLibraryRepoResultFailure,
    InspectLibraryRepoResultSuccess,
    InstallLibraryDependenciesRequest,
    InstallLibraryDependenciesResultFailure,
    InstallLibraryDependenciesResultSuccess,
    LibraryProvisioningAction,
    LibraryProvisioningActionKind,
    ListCapableLibraryEventHandlersRequest,
    ListCapableLibraryEventHandlersResultFailure,
    ListCapableLibraryEventHandlersResultSuccess,
    ListCategoriesInLibraryRequest,
    ListCategoriesInLibraryResultFailure,
    ListCategoriesInLibraryResultSuccess,
    ListNodeTypesInLibraryRequest,
    ListNodeTypesInLibraryResultFailure,
    ListNodeTypesInLibraryResultSuccess,
    ListRegisteredLibrariesRequest,
    ListRegisteredLibrariesResultSuccess,
    LoadLibrariesRequest,
    LoadLibrariesResultFailure,
    LoadLibrariesResultSuccess,
    LoadLibraryMetadataFromFileRequest,
    LoadLibraryMetadataFromFileResultFailure,
    LoadLibraryMetadataFromFileResultSuccess,
    LoadMetadataForAllLibrariesRequest,
    LoadMetadataForAllLibrariesResultSuccess,
    ParameterDescription,
    PreviewProjectProvisioningRequest,
    PreviewProjectProvisioningResultFailure,
    PreviewProjectProvisioningResultSuccess,
    RegisterLibraryFromFileRequest,
    RegisterLibraryFromFileResultFailure,
    RegisterLibraryFromFileResultSuccess,
    RegisterLibraryFromRequirementSpecifierRequest,
    RegisterLibraryFromRequirementSpecifierResultFailure,
    RegisterLibraryFromRequirementSpecifierResultSuccess,
    RegisterSandboxNodeFromSourceRequest,
    RegisterSandboxNodeFromSourceResultFailure,
    RegisterSandboxNodeFromSourceResultSuccess,
    ReloadAllLibrariesRequest,
    ReloadAllLibrariesResultFailure,
    ReloadAllLibrariesResultSuccess,
    ScanSandboxDirectoryRequest,
    ScanSandboxDirectoryResultFailure,
    ScanSandboxDirectoryResultSuccess,
    SwitchLibraryRefRequest,
    SwitchLibraryRefResultFailure,
    SwitchLibraryRefResultSuccess,
    SyncLibrariesRequest,
    SyncLibrariesResultFailure,
    SyncLibrariesResultSuccess,
    UnloadLibraryFromRegistryRequest,
    UnloadLibraryFromRegistryResultFailure,
    UnloadLibraryFromRegistryResultSuccess,
    UpdateLibraryRequest,
    UpdateLibraryResultFailure,
    UpdateLibraryResultSuccess,
    WidgetInfo,
)
from griptape_nodes.retained_mode.events.object_events import ClearAllObjectStateRequest
from griptape_nodes.retained_mode.events.os_events import (
    DeleteFileRequest,
    DeleteFileResultFailure,
    WriteFileRequest,
)
from griptape_nodes.retained_mode.events.payload_registry import PayloadRegistry
from griptape_nodes.retained_mode.events.resource_events import (
    GetResourceInstanceStatusRequest,
    GetResourceInstanceStatusResultSuccess,
    ListCompatibleResourceInstancesRequest,
    ListCompatibleResourceInstancesResultSuccess,
)
from griptape_nodes.retained_mode.events.worker_events import StartWorkerRequest
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
from griptape_nodes.retained_mode.managers.fitness_problems.libraries import (
    AdvancedLibraryLoadFailureProblem,
    AfterLibraryCallbackProblem,
    BeforeLibraryCallbackProblem,
    CreateConfigCategoryProblem,
    DuplicateLibraryProblem,
    EngineVersionErrorProblem,
    IncompatibleRequirementsProblem,
    InvalidVersionStringProblem,
    LibraryDependencyProblem,
    LibraryJsonDecodeProblem,
    LibraryLoadExceptionProblem,
    LibraryNotFoundProblem,
    LibraryProblem,
    LibrarySchemaExceptionProblem,
    LibrarySchemaValidationProblem,
    NodeClassNotBaseNodeProblem,
    NodeClassNotFoundProblem,
    NodeModuleImportProblem,
    OldXdgLocationWarningProblem,
    SandboxDirectoryMissingProblem,
    UpdateConfigCategoryProblem,
)
from griptape_nodes.retained_mode.managers.os_manager import OSManager
from griptape_nodes.retained_mode.managers.project_manager import SYSTEM_DEFAULTS_KEY
from griptape_nodes.retained_mode.managers.settings import (
    LIBRARIES_TO_DOWNLOAD_KEY,
    LIBRARIES_TO_REGISTER_KEY,
    LIBRARY_DEPENDENCY_INSTALL_BEHAVIOR_KEY,
    REQUIRES_ENGINE_KEY,
    WORKER_HEARTBEAT_STARTUP_GRACE_KEY,
    LibraryDependencyInstallBehavior,
    LibraryDownload,
    LibraryRegistration,
)
from griptape_nodes.utils.async_utils import subprocess_run
from griptape_nodes.utils.dict_utils import get_dot_value, merge_dicts, normalize_secrets_to_register
from griptape_nodes.utils.file_utils import find_file_in_directory, find_files_recursive
from griptape_nodes.utils.git_utils import (
    GitCloneError,
    GitPullError,
    GitRefError,
    GitRemoteError,
    GitRepositoryError,
    clone_repository,
    extract_repo_name_from_url,
    get_current_ref,
    get_git_remote,
    get_local_commit_sha,
    is_git_url,
    normalize_github_url,
    parse_git_url_with_ref,
    sparse_checkout_library_json,
    switch_branch_or_tag,
    update_library_git,
)
from griptape_nodes.utils.library_utils import (
    LIBRARY_GIT_URLS,
    clone_and_get_library_version,
    extract_library_path,
    filter_old_xdg_library_paths,
    is_monorepo,
    normalize_library_downloads,
    normalize_library_registrations,
)
from griptape_nodes.utils.uv_utils import find_uv_bin, is_venv_functional, venv_python_path
from griptape_nodes.utils.version_utils import (
    engine_version_failure_detail,
    get_complete_version_string,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from types import ModuleType

    from griptape_nodes.node_library.advanced_node_library import AdvancedNodeLibrary
    from griptape_nodes.retained_mode.events.base_events import Payload, RequestPayload, ResultPayload
    from griptape_nodes.retained_mode.managers.event_manager import EventManager
    from griptape_nodes.retained_mode.managers.worker_manager import WorkerManager

logger = logging.getLogger("griptape_nodes")
console = Console()

# Directories to exclude when scanning for Python source files (in addition to any directory starting with '.')
EXCLUDED_SCAN_DIRECTORIES = frozenset({"venv", "__pycache__"})

TRegisteredEventData = TypeVar("TRegisteredEventData")


class LibraryGitOperationContext(NamedTuple):
    """Context information for git operations on a library."""

    library: Library
    old_version: str
    library_file_path: str
    library_dir: Path


class LibraryUpdateInfo(NamedTuple):
    """Information about a library pending update."""

    library_name: str
    old_version: str
    new_version: str


class LibraryUpdateResult(NamedTuple):
    """Result of updating a single library."""

    library_name: str
    old_version: str
    new_version: str
    result: ResultPayload


class LibraryManager:
    SANDBOX_LIBRARY_NAME = "Sandbox Library"
    LIBRARY_CONFIG_FILENAME = "griptape_nodes_library.json"
    LIBRARY_CONFIG_GLOB_PATTERN = "griptape[_-]nodes[_-]library.json"

    # Sandbox library constants
    UNRESOLVED_SANDBOX_CLASS_NAME = "<NOT YET RESOLVED>"
    SANDBOX_CATEGORY_NAME = "Griptape Nodes Sandbox"

    _library_file_path_to_info: dict[str, LibraryInfo]

    class LibraryLifecycleState(StrEnum):
        """Lifecycle states for library loading."""

        FAILURE = "failure"
        DISCOVERED = "discovered"
        METADATA_LOADED = "metadata_loaded"
        EVALUATED = "evaluated"
        DEPENDENCIES_INSTALLED = "dependencies_installed"
        WORKER_DELEGATED = "worker_delegated"
        WORKER_PENDING = "worker_pending"
        LOADED = "loaded"
        DISABLED = "disabled"

    class LibraryFitness(StrEnum):
        """Fitness of the library that was attempted to be loaded."""

        GOOD = "GOOD"  # No errors detected during loading. Registered.
        FLAWED = "FLAWED"  # Some errors detected, but recoverable. Registered.
        UNUSABLE = "UNUSABLE"  # Errors detected and not recoverable. Not registered.
        MISSING = "MISSING"  # File not found. Not registered.
        NOT_EVALUATED = "NOT_EVALUATED"  # Library has not been evaluated yet.

    @dataclass
    class RegisteredEventHandler(Generic[TRegisteredEventData]):
        """Information regarding an event handler from a registered library.

        The generic type parameter TRegisteredEventData allows each event type
        to specify its own structured additional data.
        """

        handler: Callable[[RequestPayload], ResultPayload]
        library_data: LibrarySchema
        event_data: TRegisteredEventData | None = None

    @dataclass
    class LibraryInfo:
        """Information about a library that was attempted to be loaded.

        Tracks the lifecycle state (where we are in the loading process) and fitness (health/quality).
        Includes the file path and any problems encountered during loading.

        Attributes:
            lifecycle_state: Current phase of the library loading lifecycle (DISCOVERED → METADATA_LOADED →
                           EVALUATED → DEPENDENCIES_INSTALLED → LOADED, or EVALUATED → WORKER_DELEGATED →
                           WORKER_PENDING → LOADED once the worker confirms, or FAILURE at any phase)
            fitness: Health/quality assessment of the library (GOOD, FLAWED, UNUSABLE, NOT_EVALUATED)
            library_path: Absolute path to the library JSON file or sandbox directory
            is_sandbox: True if this is a sandbox library (user-created nodes in workspace), False for regular libraries
            library_name: Name of the library from metadata (None until METADATA_LOADED phase)
            library_version: Schema version from metadata (None until METADATA_LOADED phase)
            problems: List of issues encountered during any phase (version incompatibilities, node load failures, etc.)
                     Problems accumulate across lifecycle phases and determine final fitness level.
        """

        lifecycle_state: LibraryManager.LibraryLifecycleState
        fitness: LibraryManager.LibraryFitness
        # Resolved absolute path to the library JSON file. Used as the dict key in
        # `_library_file_path_to_info` and for filesystem operations.
        library_path: str
        is_sandbox: bool
        library_name: str | None = None
        library_version: str | None = None
        # The path string the user wrote in `libraries_to_register` before workspace
        # resolution / `~`-expansion / symlink-following. Surfaced to the GUI so the
        # settings panel can match library metadata back to its config row using the
        # exact key the user sees in their config. None for sandbox libraries
        # (registered through workspace discovery, not via `libraries_to_register`).
        registered_path: str | None = None
        problems: list[LibraryProblem] = field(default_factory=list)
        # Mirrors LibraryRegistration.enabled from the user's libraries_to_register config.
        # True for sandbox, ad-hoc, and bare-string entries; False only when the user
        # explicitly set enabled=false on the config object form.
        enabled: bool = True
        # True when the library's declarations resolve to launching in a worker
        # process (compatible per ``WorkerModeCompatibility`` and suggested per
        # ``SuggestedWorkerMode``). Set whenever metadata is first successfully
        # parsed (discovery or lifecycle progression). Absence of the relevant
        # declarations falls through to False.
        requires_worker: bool = False
        # Set when the library enters WORKER_PENDING state. The orchestrator waits on this
        # event before returning RegisterLibraryFromFileResultSuccess so callers see the real
        # fitness once the worker has loaded and reported back.
        worker_ready: asyncio.Event | None = field(default=None, repr=False)

    class RegisterLibraryPrerequisites(NamedTuple):
        """Prerequisites established for library loading."""

        library_info: LibraryManager.LibraryInfo
        file_path: str

    class DiscoveredLibraryEntry(NamedTuple):
        """Internal pairing of a discovered library with the user's original config entry path.

        `registration` carries the resolved on-disk path used to load the library.
        `registered_path` is the verbatim string from `LibraryRegistration.path` (before
        workspace resolution / `~`-expansion / symlink-following). Surfacing the raw config
        path lets the GUI match library metadata back to its `libraries_to_register` row
        by the same key the user sees in their config.

        For directory entries that expand into multiple library files, every discovered
        child shares the parent directory's `registered_path`.
        """

        registration: LibraryRegistration
        registered_path: str

    class ResolvedDiscoveryPath(NamedTuple):
        """A config `libraries_to_register` entry resolved to a concrete on-disk path.

        `path` is the file or directory to scan for manifests: the
        workspace-resolved path for a path-backed entry, or the provisioned
        manifest located by name for a git-sourced entry. `registered_path`
        is the key the GUI uses to map metadata back to the user's config row.
        """

        path: Path
        registered_path: str

    # Stable module namespace mappings for workflow serialization
    # These mappings ensure that dynamically loaded modules can be reliably imported
    # in generated workflow code by providing stable, predictable import paths.
    #
    # Example mappings:
    # dynamic to stable module mapping:
    #     "gtn_dynamic_module_image_to_video_py_123456789": "griptape_nodes.node_libraries.runwayml_library.image_to_video"
    #
    # stable to dynamic module mapping:
    #     "griptape_nodes.node_libraries.runwayml_library.image_to_video": "gtn_dynamic_module_image_to_video_py_123456789"
    #
    # library to stable modules:
    #     "RunwayML Library": {"griptape_nodes.node_libraries.runwayml_library.image_to_video", "griptape_nodes.node_libraries.runwayml_library.text_to_image"},
    #     "Sandbox Library": {"griptape_nodes.node_libraries.sandbox.my_custom_node"}
    #
    _dynamic_to_stable_module_mapping: dict[str, str]  # dynamic_module_name -> stable_namespace
    _stable_to_dynamic_module_mapping: dict[str, str]  # stable_namespace -> dynamic_module_name
    _library_to_stable_modules: dict[str, set[str]]  # library_name -> set of stable_namespaces
    # Callbacks invoked immediately before all libraries are reloaded.
    _pre_reload_callbacks: list[Callable[[], Awaitable[None]]]

    def __init__(self, event_manager: EventManager, *, worker_manager: WorkerManager) -> None:
        self._library_file_path_to_info = {}
        self._dynamic_to_stable_module_mapping = {}
        self._stable_to_dynamic_module_mapping = {}
        self._library_to_stable_modules = {}
        self._library_event_handler_mappings: dict[
            type[Payload], dict[str, LibraryManager.RegisteredEventHandler[Any]]
        ] = {}
        self._libraries_loading_complete = asyncio.Event()
        self._libraries_loading_complete.set()  # Not loading initially; load_all_libraries_from_config will clear/set this
        # True for the duration of the engine's initialization sequence (library + workflow
        # loading) driven by on_app_initialization_complete. Reported on the engine heartbeat so
        # a client connecting mid-startup can render a loading state instead of an empty list.
        self._is_initializing: bool = False
        self._pre_reload_callbacks: list[Callable[[], Awaitable[None]]] = []
        # True when this process is a dedicated worker
        self._is_worker: bool = False
        # The libraries this process is restricted to loading (set on workers).
        self._target_library_names: list[str] | None = None

        event_manager.assign_manager_to_request_type(
            ListRegisteredLibrariesRequest, self.on_list_registered_libraries_request
        )
        event_manager.assign_manager_to_request_type(
            ListCapableLibraryEventHandlersRequest, self.on_list_capable_event_handlers
        )
        event_manager.assign_manager_to_request_type(
            ListNodeTypesInLibraryRequest, self.on_list_node_types_in_library_request
        )
        event_manager.assign_manager_to_request_type(
            GetNodeMetadataFromLibraryRequest,
            self.get_node_metadata_from_library_request,
        )
        event_manager.assign_manager_to_request_type(
            DescribeNodeTypeRequest,
            self.describe_node_type_request,
        )
        event_manager.assign_manager_to_request_type(
            LoadLibraryMetadataFromFileRequest,
            self.load_library_metadata_from_file_request,
        )
        event_manager.assign_manager_to_request_type(
            RegisterLibraryFromFileRequest,
            self.register_library_from_file_request,
        )
        event_manager.assign_manager_to_request_type(
            RegisterLibraryFromRequirementSpecifierRequest, self.register_library_from_requirement_specifier_request
        )
        event_manager.assign_manager_to_request_type(
            ListCategoriesInLibraryRequest,
            self.list_categories_in_library_request,
        )
        event_manager.assign_manager_to_request_type(
            GetLibraryMetadataRequest,
            self.get_library_metadata_request,
        )
        event_manager.assign_manager_to_request_type(GetAllInfoForLibraryRequest, self.get_all_info_for_library_request)
        event_manager.assign_manager_to_request_type(
            GetAllInfoForAllLibrariesRequest, self.on_get_all_info_for_all_libraries_request
        )
        event_manager.assign_manager_to_request_type(
            LoadMetadataForAllLibrariesRequest, self.load_metadata_for_all_libraries_request
        )
        event_manager.assign_manager_to_request_type(ScanSandboxDirectoryRequest, self.scan_sandbox_directory_request)
        event_manager.assign_manager_to_request_type(
            RegisterSandboxNodeFromSourceRequest, self.register_sandbox_node_from_source_request
        )
        event_manager.assign_manager_to_request_type(
            UnloadLibraryFromRegistryRequest, self.unload_library_from_registry_request
        )
        event_manager.assign_manager_to_request_type(ReloadAllLibrariesRequest, self.reload_libraries_request)
        event_manager.assign_manager_to_request_type(LoadLibrariesRequest, self.load_libraries_request)
        event_manager.assign_manager_to_request_type(CheckLibraryUpdateRequest, self.check_library_update_request)
        event_manager.assign_manager_to_request_type(UpdateLibraryRequest, self.update_library_request)
        event_manager.assign_manager_to_request_type(SwitchLibraryRefRequest, self.switch_library_ref_request)
        event_manager.assign_manager_to_request_type(DownloadLibraryRequest, self.download_library_request)
        event_manager.assign_manager_to_request_type(
            InstallLibraryDependenciesRequest, self.install_library_dependencies_request
        )
        event_manager.assign_manager_to_request_type(SyncLibrariesRequest, self.sync_libraries_request)
        event_manager.assign_manager_to_request_type(InspectLibraryRepoRequest, self.inspect_library_repo_request)
        event_manager.assign_manager_to_request_type(
            GetLibrarySourceInfoRequest, self.on_get_library_source_info_request
        )
        event_manager.assign_manager_to_request_type(GetEngineSourceInfoRequest, self.on_get_engine_source_info_request)
        event_manager.assign_manager_to_request_type(
            PreviewProjectProvisioningRequest, self.on_preview_project_provisioning_request
        )

        event_manager.add_listener_to_app_event(
            LibraryLoadedNotification,
            self._on_library_loaded_notification,
        )
        event_manager.add_listener_to_app_event(
            AppInitializationComplete,
            self.on_app_initialization_complete,
        )
        event_manager.add_listener_to_app_event(
            AppSessionStartedEvent,
            self._on_session_started,
        )

        worker_manager.register_worker_evicted_callback(self.on_worker_evicted)
        self._pre_reload_callbacks.append(worker_manager.reset_workers)

    def is_initializing(self) -> bool:
        """Return True while the engine is running its initialization sequence.

        Covers the full on_app_initialization_complete / reload flow (library and workflow
        loading), not just the library phase. Reported on the engine heartbeat.
        """
        return self._is_initializing

    def register_pre_reload_callback(self, callback: Callable[[], Awaitable[None]]) -> None:
        """Register a callback invoked immediately before all libraries are reloaded.

        Callbacks fire after all libraries have been unloaded, before
        load_all_libraries_from_config runs. Use this to clean up state
        (e.g. terminate worker processes) that must be reset for the reload to
        succeed.
        """
        self._pre_reload_callbacks.append(callback)

    async def _on_library_loaded_notification(self, notification: LibraryLoadedNotification) -> None:
        """Update LibraryInfo fitness and state when a library load outcome is reported."""
        library_info = self.get_library_info_by_library_name(notification.library_name)
        if library_info is None:
            logger.warning(
                "Received LibraryLoadedNotification for unknown library '%s'.",
                notification.library_name,
            )
            return
        library_info.fitness = LibraryManager.LibraryFitness(notification.fitness)
        library_info.lifecycle_state = LibraryManager.LibraryLifecycleState.LOADED
        if notification.problem_details:
            logger.warning(
                "Worker reported problems loading library '%s': %s",
                notification.library_name,
                notification.problem_details,
            )
        # Register stub node classes from the worker-reported schemas so the orchestrator
        # can display nodes in the sidebar and recreate them during workflow loading.
        # Skip on the worker itself -- it already has the real node classes registered.
        if notification.node_schemas and not self._is_worker:
            self._register_nodes_from_worker_schemas(notification.library_name, notification.node_schemas)
        # Unblock any code awaiting this library's worker_ready event.
        if library_info.worker_ready is not None:
            library_info.worker_ready.set()

    @property
    def is_worker(self) -> bool:
        """True when this process was started as a dedicated worker.

        Set by ``LibrariesInitializeStartRequest`` once the worker bootstrap
        has identified the process role. Callers outside this manager that
        need the role (e.g. node-execution strict-mode attribution, request
        forwarding decisions) should consult this accessor rather than
        reaching into ``_is_worker``.
        """
        return self._is_worker

    def get_worker_for_library(self, library_name: str | None) -> tuple[str, str] | None:
        """Return (worker_engine_id, worker_request_topic) for the worker serving library_name, or None.

        Raises RuntimeError if the library requires a dedicated worker but none is registered yet.
        Returns None if no worker is registered and none is required.
        """
        if library_name:
            library_info = self.get_library_info_by_library_name(library_name)
            if library_info and library_info.requires_worker:
                wm = GriptapeNodes.WorkerManager()
                if wm:
                    worker = wm.get_worker_for_key(library_name)
                    if worker:
                        return worker
                    msg = (
                        f"Library '{library_name}' requires a dedicated worker process "
                        "that is not yet registered. The worker may still be starting up."
                    )
                    raise RuntimeError(msg)
                msg = (
                    f"Library '{library_name}' requires a dedicated worker process. "
                    "The Worker Manager is not available."
                )
                raise RuntimeError(msg)
        return None

    async def _start_workers(self) -> None:
        """Issue StartWorkerRequest for every library that requires a dedicated worker.

        Sets each matching library back to WORKER_PENDING and asks WorkerManager to
        spawn a subprocess.  Used on session start (both initial and subsequent) so
        that worker creation is always tied to an active session.
        """
        for library_info in self._library_file_path_to_info.values():
            if library_info.requires_worker and library_info.library_name and not self._is_worker:
                library_info.lifecycle_state = LibraryManager.LibraryLifecycleState.WORKER_PENDING
                # Create (or reset) the worker_ready event for this spawn.
                library_info.worker_ready = asyncio.Event()
                await GriptapeNodes.ahandle_request(StartWorkerRequest(library_name=library_info.library_name))

    def on_worker_evicted(self, worker_engine_id: str, library_name: str | None) -> None:
        """Called when a worker is evicted by the orchestrator heartbeat monitor.

        Transitions WORKER_PENDING libraries to FAILURE so downstream code and the UI
        can reflect that the worker did not successfully confirm its library load.
        The library remains registered in LibraryRegistry so node stubs stay visible.
        """
        if not library_name:
            return
        library_info = self.get_library_info_by_library_name(library_name)
        if library_info is None:
            return
        if library_info.lifecycle_state == LibraryManager.LibraryLifecycleState.WORKER_PENDING:
            library_info.lifecycle_state = LibraryManager.LibraryLifecycleState.FAILURE
            library_info.fitness = LibraryManager.LibraryFitness.UNUSABLE
            # Unblock any code awaiting this library's worker_ready event.
            if library_info.worker_ready is not None:
                library_info.worker_ready.set()
            logger.warning(
                "Worker '%s' evicted before confirming load of library '%s'; library marked as FAILURE.",
                worker_engine_id,
                library_name,
            )

    def print_library_load_status(self) -> None:
        library_file_paths = self.get_libraries_attempted_to_load()
        library_infos = []
        for library_file_path in library_file_paths:
            library_info = self.get_library_info_for_attempted_load(library_file_path)
            library_infos.append(library_info)

        console = Console()

        # Check if the list is empty
        if not library_infos:
            # Display a message indicating no libraries are available
            empty_message = Text("No library information available", style="italic")
            panel = Panel(empty_message, title="Library Information", border_style="blue")
            console.print(panel)
            return

        # Create a table with two columns and row dividers
        table = Table(show_header=True, box=HEAVY_EDGE, show_lines=True, expand=True)
        table.add_column("Library", style="green", ratio=1)
        table.add_column("Problems", style="yellow", ratio=1)

        # Status emojis mapping
        status_emoji = {
            LibraryManager.LibraryFitness.GOOD: "[green]OK[/green]",
            LibraryManager.LibraryFitness.FLAWED: "[yellow]![/yellow]",
            LibraryManager.LibraryFitness.UNUSABLE: "[red]X[/red]",
            LibraryManager.LibraryFitness.MISSING: "[red]?[/red]",
            LibraryManager.LibraryFitness.NOT_EVALUATED: "[cyan]...[/cyan]",
        }

        # Status text mapping (colored)
        status_text = {
            LibraryManager.LibraryFitness.GOOD: "[green](GOOD)[/green]",
            LibraryManager.LibraryFitness.FLAWED: "[yellow](FLAWED)[/yellow]",
            LibraryManager.LibraryFitness.UNUSABLE: "[red](UNUSABLE)[/red]",
            LibraryManager.LibraryFitness.MISSING: "[red](MISSING)[/red]",
            LibraryManager.LibraryFitness.NOT_EVALUATED: "[cyan](PENDING)[/cyan]",
        }

        # Add rows for each library info
        for lib_info in library_infos:
            # Library column with emoji, name, version, colored status, and file path underneath
            is_disabled = lib_info.lifecycle_state == LibraryManager.LibraryLifecycleState.DISABLED
            if is_disabled:
                emoji = "[dim]-[/dim]"
                colored_status = "[dim](DISABLED)[/dim]"
            else:
                emoji = status_emoji.get(lib_info.fitness, "ERROR: Unknown/Unexpected Library Status")
                colored_status = status_text.get(lib_info.fitness, "(UNKNOWN)")
            name = lib_info.library_name or "*UNKNOWN*"

            library_version = lib_info.library_version
            if library_version:
                version_str = str(library_version)
            else:
                version_str = "*UNKNOWN*"

            file_path = lib_info.library_path
            library_name_with_details = Text.from_markup(
                f"{emoji} - {name} v{version_str} {colored_status}\n[cyan dim]{file_path}[/cyan dim]"
            )
            library_name_with_details.overflow = "fold"

            # Problems column - collate by type then format
            collated = self.collate_problems_for_lib_info(lib_info)
            problems = collated if collated is not None else "No problems detected."

            # Add the row to the table
            table.add_row(library_name_with_details, problems)

        # Create a panel containing the table
        panel = Panel(table, title="Library Information", border_style="blue")

        # Display the panel
        console.print(panel)

    def get_libraries_attempted_to_load(self) -> list[str]:
        return list(self._library_file_path_to_info.keys())

    def get_library_info_for_attempted_load(self, library_file_path: str) -> LibraryInfo:
        return self._library_file_path_to_info[library_file_path]

    def get_library_info_by_library_name(self, library_name: str) -> LibraryInfo | None:
        for library_info in self._library_file_path_to_info.values():
            if library_info.library_name == library_name:
                return library_info
        return None

    def resolve_transitive_library_deps(
        self,
        initial: list[LibraryNameAndVersion],
    ) -> list[LibraryNameAndVersion]:
        """Expand an initial library set by following each library's library_dependencies.

        BFS walks declared library_dependencies until no new libraries are found.
        Unregistered deps are logged and skipped. Cycle-safe via a visited set.
        """
        resolved: dict[str, LibraryNameAndVersion] = {ref.library_name: ref for ref in initial}
        queue = list(initial)

        while queue:
            library_ref = queue.pop(0)
            try:
                library_data = LibraryRegistry.get_library(library_ref.library_name).get_library_data()
            except KeyError:
                logger.warning(
                    "Library '%s' not found in registry during transitive dep resolution, skipping",
                    library_ref.library_name,
                )
                continue

            if not library_data.metadata:
                continue
            lib_deps = [
                d for d in (library_data.metadata.declarations or []) if isinstance(d, LibraryDependencyDeclaration)
            ]
            if not lib_deps:
                continue

            for dep in lib_deps:
                repo_name = extract_repo_name_from_url(dep.url)
                dep_info = self.get_library_info_by_library_name(repo_name)
                if dep_info is None:
                    logger.warning(
                        "Library dependency '%s' (resolved as '%s') is not registered; skipping",
                        dep.url,
                        repo_name,
                    )
                    continue
                dep_library_name = dep_info.library_name
                if dep_library_name is None:
                    logger.warning("Library dependency '%s' has no library_name; skipping", dep.url)
                    continue
                if dep_library_name not in resolved:
                    lib_nav = LibraryNameAndVersion(
                        library_name=dep_library_name,
                        library_version=dep_info.library_version or "unknown",
                    )
                    resolved[dep_library_name] = lib_nav
                    queue.append(lib_nav)

        return list(resolved.values())

    def collate_problems_for_lib_info(self, lib_info: LibraryInfo) -> str | None:
        """Return a collated display string for a LibraryInfo's problems, or None if there are none."""
        if not lib_info.problems:
            return None

        # Group problems by type
        problems_by_type: defaultdict[type, list] = defaultdict(list)
        for problem in lib_info.problems:
            problems_by_type[type(problem)].append(problem)

        # Collate each group
        collated_strings = []
        for problem_class, instances in problems_by_type.items():
            collated_display = problem_class.collate_problems_for_display(instances)
            collated_strings.append(collated_display)

        if len(collated_strings) == 1:
            return collated_strings[0]

        # Number the problems when there's more than one
        return "\n".join([f"{j + 1}. {problem}" for j, problem in enumerate(collated_strings)])

    def get_collated_problems_for_library(self, library_name: str) -> str | None:
        """Return a collated display string for a library's fitness problems, or None if not found or no problems."""
        library_info = self.get_library_info_by_library_name(library_name)
        if library_info is None:
            return None
        return self.collate_problems_for_lib_info(library_info)

    def on_register_event_handler(
        self,
        request_type: type[RequestPayload],
        handler: Callable[[RequestPayload], ResultPayload],
        library_data: LibrarySchema,
        event_data: object | None = None,
    ) -> None:
        """Register an event handler for a specific request type from a library.

        Args:
            request_type: The type of request payload this handler processes
            handler: The callable handler function
            library_data: Schema data for the library registering this handler
            event_data: Optional structured data specific to this event type
        """
        if self._library_event_handler_mappings.get(request_type) is None:
            self._library_event_handler_mappings[request_type] = {}
        self._library_event_handler_mappings[request_type][library_data.name] = LibraryManager.RegisteredEventHandler(
            handler=handler, library_data=library_data, event_data=event_data
        )

    def get_registered_event_handlers(self, request_type: type[Payload]) -> dict[str, RegisteredEventHandler[Any]]:
        """Get all registered event handlers for a specific request type."""
        return self._library_event_handler_mappings.get(request_type, {})

    def on_list_capable_event_handlers(self, request: ListCapableLibraryEventHandlersRequest) -> ResultPayload:
        """Get all registered event handlers for a specific request type."""
        request_type = PayloadRegistry.get_type(request.request_type)
        if request_type is None:
            details = f"Request type '{request.request_type}' is not registered in the PayloadRegistry."
            return ListCapableLibraryEventHandlersResultFailure(exception=KeyError(details), result_details=details)
        handler_mappings = self.get_registered_event_handlers(request_type)
        return ListCapableLibraryEventHandlersResultSuccess(
            handlers=list(handler_mappings.keys()),
            result_details=f"Successfully listed {len(handler_mappings)} capable library event handlers",
        )

    async def on_list_registered_libraries_request(self, _request: ListRegisteredLibrariesRequest) -> ResultPayload:
        await self._libraries_loading_complete.wait()
        # Make a COPY of the list
        snapshot_list = LibraryRegistry.list_libraries()
        event_copy = snapshot_list.copy()

        details = "Successfully retrieved the list of registered libraries."

        result = ListRegisteredLibrariesResultSuccess(
            libraries=event_copy,
            result_details=details,
        )
        return result

    async def on_get_library_source_info_request(self, request: GetLibrarySourceInfoRequest) -> ResultPayload:
        """Return the filesystem paths for a registered library's source files.

        Waits for all libraries to finish loading before resolving the request,
        ensuring the library registry is in a consistent state.

        The response provides two path variants for the named library:
        - ``library_json_path``: the absolute path to the library's
          ``griptape_nodes_library.json`` manifest file.
        - ``library_directory``: the absolute path to the directory that contains the
          manifest file (i.e. the library root folder).

        Args:
            request: A :class:`GetLibrarySourceInfoRequest` carrying the ``library``
              field — the registered name of the library whose source paths are being
              queried (e.g. ``"Griptape Nodes Library"``).

        Returns:
            :class:`GetLibrarySourceInfoResultSuccess` containing ``library_name``,
              ``library_json_path``, and ``library_directory`` when the library is
              found.

            :class:`GetLibrarySourceInfoResultFailure` when no library with the
              requested name has been registered.
        """
        await self._libraries_loading_complete.wait()
        lib_info = self.get_library_info_by_library_name(request.library)
        if lib_info is None:
            return GetLibrarySourceInfoResultFailure(
                result_details=f"Library '{request.library}' not found.",
            )
        library_dir = str(Path(lib_info.library_path).parent.absolute())
        return GetLibrarySourceInfoResultSuccess(
            library_name=request.library,
            library_json_path=lib_info.library_path,
            library_directory=library_dir,
            result_details=f"Source info for library '{request.library}'.",
        )

    def on_get_engine_source_info_request(self, _request: GetEngineSourceInfoRequest) -> ResultPayload:
        """Return the filesystem path of the installed ``griptape_nodes`` package.

        Resolves the location of the ``griptape_nodes`` package on disk. The returned
        directory is the package root.

        This is useful for tools that need to read engine source files directly, for
        example to inspect base-class definitions in ``exe_types/node_types.py`` or
        locate built-in node implementations.

        Args:
            _request: A :class:`GetEngineSourceInfoRequest` (no fields required; the
                argument is accepted for handler-dispatch consistency).

        Returns:
            :class:`GetEngineSourceInfoResultSuccess` containing ``package_directory`` —
                the absolute path to the ``griptape_nodes`` package root directory.
        """
        spec = importlib.util.find_spec("griptape_nodes")
        if spec is None or spec.origin is None:
            return GetEngineSourceInfoResultFailure(
                result_details="Attempted to resolve engine source path. Failed because the griptape_nodes package spec could not be located.",
            )
        package_dir = str(Path(spec.origin).parent.absolute())
        return GetEngineSourceInfoResultSuccess(
            package_directory=package_dir,
            result_details="Engine source info.",
        )

    def on_list_node_types_in_library_request(self, request: ListNodeTypesInLibraryRequest) -> ResultPayload:
        # Does this library exist?
        try:
            library = LibraryRegistry.get_library(name=request.library)
        except KeyError:
            details = f"Attempted to list node types in a Library named '{request.library}'. Failed because no Library with that name was registered."

            result = ListNodeTypesInLibraryResultFailure(result_details=details)
            return result

        # Cool, get a copy of the list.
        snapshot_list = library.get_registered_nodes()
        event_copy = snapshot_list.copy()

        details = f"Successfully retrieved the list of node types in the Library named '{request.library}'."

        result = ListNodeTypesInLibraryResultSuccess(
            node_types=event_copy,
            result_details=details,
        )
        return result

    def get_library_metadata_request(self, request: GetLibraryMetadataRequest) -> ResultPayload:
        # Does this library exist?
        try:
            library = LibraryRegistry.get_library(name=request.library)
        except KeyError:
            details = f"Attempted to get metadata for Library '{request.library}'. Failed because no Library with that name was registered."
            problems = self.get_collated_problems_for_library(request.library)
            result = GetLibraryMetadataResultFailure(result_details=details, problems=problems)
            return result

        # Get the metadata off of it.
        metadata = library.get_metadata()
        details = f"Successfully retrieved metadata for Library '{request.library}'."

        result = GetLibraryMetadataResultSuccess(metadata=metadata, result_details=details)
        return result

    def load_library_metadata_from_file_request(  # noqa: PLR0911, PLR0915, C901
        self, request: LoadLibraryMetadataFromFileRequest
    ) -> LoadLibraryMetadataFromFileResultSuccess | LoadLibraryMetadataFromFileResultFailure:
        """Load library metadata from a JSON file without loading the actual node modules.

        This method provides a lightweight way to get library schema information
        without the overhead of dynamically importing Python modules.
        """
        file_path = request.file_path

        # Convert to Path object if it's a string
        json_path = Path(file_path)

        # Check if the file exists
        if not json_path.exists():
            details = f"Attempted to load Library JSON file. Failed because no file could be found at the specified path: {json_path}"
            return LoadLibraryMetadataFromFileResultFailure(
                library_path=file_path,
                library_name=None,
                status=LibraryManager.LibraryFitness.MISSING,
                problems=[LibraryNotFoundProblem(library_path=str(json_path))],
                result_details=details,
            )

        # Load the JSON
        try:
            with json_path.open("r", encoding="utf-8") as f:
                library_json = json.load(f)
        except json.JSONDecodeError:
            details = f"Attempted to load Library JSON file. Failed because the file at path '{json_path}' was improperly formatted."
            return LoadLibraryMetadataFromFileResultFailure(
                library_path=file_path,
                library_name=None,
                status=LibraryManager.LibraryFitness.UNUSABLE,
                problems=[LibraryJsonDecodeProblem()],
                result_details=details,
            )
        except Exception as err:
            details = f"Attempted to load Library JSON file from location '{json_path}'. Failed because an exception occurred: {err}"
            return LoadLibraryMetadataFromFileResultFailure(
                library_path=file_path,
                library_name=None,
                status=LibraryManager.LibraryFitness.UNUSABLE,
                problems=[LibraryLoadExceptionProblem(error_message=str(err))],
                result_details=details,
            )

        # Try to extract library name from JSON for better error reporting
        library_name = library_json.get("name") if isinstance(library_json, dict) else None

        # Surface retired declaration types with migration guidance before the
        # discriminated-union validator rejects them with an opaque message.
        if isinstance(library_json, dict):
            retired_problems = detect_retired_node_declarations(library_json)
            if retired_problems:
                details = (
                    f"Attempted to load Library JSON file from '{json_path}'. "
                    f"Failed because it uses node declaration types removed in a newer schema. "
                    f"Count: {len(retired_problems)}."
                )
                return LoadLibraryMetadataFromFileResultFailure(
                    library_path=file_path,
                    library_name=library_name,
                    status=LibraryManager.LibraryFitness.UNUSABLE,
                    problems=retired_problems,
                    result_details=details,
                )

        # Do you comport, my dude
        try:
            library_data = LibrarySchema.model_validate(library_json)
        except ValidationError as err:
            # Do some more hardcore error handling.
            problems = []
            for error in err.errors():
                loc = " -> ".join(map(str, error["loc"]))
                msg = error["msg"]
                error_type = error["type"]
                problem = LibrarySchemaValidationProblem(location=loc, error_type=error_type, message=msg)
                problems.append(problem)
            details = f"Attempted to load Library JSON file. Failed because the file at path '{json_path}' failed to match the library schema due to: {err}"
            return LoadLibraryMetadataFromFileResultFailure(
                library_path=file_path,
                library_name=library_name,
                status=LibraryManager.LibraryFitness.UNUSABLE,
                problems=problems,
                result_details=details,
            )
        except Exception as err:
            details = f"Attempted to load Library JSON file. Failed because the file at path '{json_path}' failed to match the library schema due to: {err}"
            return LoadLibraryMetadataFromFileResultFailure(
                library_path=file_path,
                library_name=library_name,
                status=LibraryManager.LibraryFitness.UNUSABLE,
                problems=[LibrarySchemaExceptionProblem(error_message=str(err))],
                result_details=details,
            )

        # Make sure the version string is copacetic.
        library_version = library_data.metadata.library_version
        if library_version is None:
            details = f"Attempted to load Library '{library_data.name}' JSON file from '{json_path}'. Failed because version string '{library_data.metadata.library_version}' wasn't valid. Must be in major.minor.patch format."
            return LoadLibraryMetadataFromFileResultFailure(
                library_path=file_path,
                library_name=library_data.name,
                status=LibraryManager.LibraryFitness.UNUSABLE,
                problems=[InvalidVersionStringProblem(version_string=str(library_data.metadata.library_version))],
                result_details=details,
            )

        # Resolve cross-references between declarations (catalog model ids,
        # node-level model_usage references, etc.). Any problem blocks the load.
        declaration_problems = validate_library_declarations(library_data)
        if declaration_problems:
            details = (
                f"Attempted to load Library '{library_data.name}' JSON file from '{json_path}'. "
                f"Failed because declarative references did not resolve. "
                f"Count: {len(declaration_problems)}."
            )
            return LoadLibraryMetadataFromFileResultFailure(
                library_path=file_path,
                library_name=library_data.name,
                status=LibraryManager.LibraryFitness.UNUSABLE,
                problems=list(declaration_problems),
                result_details=details,
            )

        # Get git remote and ref if this library is in a git repository
        library_dir = json_path.parent.absolute()
        try:
            git_remote = get_git_remote(library_dir)
        except GitRemoteError as e:
            logger.debug("Failed to get git remote for %s: %s", library_dir, e)
            git_remote = None

        try:
            git_ref = get_current_ref(library_dir)
        except GitRefError as e:
            logger.debug("Failed to get git ref for %s: %s", library_dir, e)
            git_ref = None

        existing_info = self._library_file_path_to_info.get(file_path)
        enabled = existing_info.enabled if existing_info is not None else True
        details = f"Successfully loaded library metadata from JSON file at {json_path}"
        return LoadLibraryMetadataFromFileResultSuccess(
            library_schema=library_data,
            file_path=file_path,
            git_remote=git_remote,
            git_ref=git_ref,
            enabled=enabled,
            result_details=details,
        )

    async def load_metadata_for_all_libraries_request(
        self,
        request: LoadMetadataForAllLibrariesRequest,  # noqa: ARG002
    ) -> ResultPayload:
        """Load metadata for all libraries from configuration without loading node modules.

        This loads metadata from both library JSON files specified in configuration
        and generates sandbox library metadata by scanning Python files without importing them.
        """
        successful_libraries = []
        failed_libraries = []

        # Discover library files for metadata loading
        library_files = await self._discover_library_files()

        # Load metadata for all discovered library files (including disabled ones,
        # so their names/versions can be displayed in status output).
        for discovered in library_files:
            metadata_request = LoadLibraryMetadataFromFileRequest(file_path=discovered.registration.path)
            metadata_result = self.load_library_metadata_from_file_request(metadata_request)

            if isinstance(metadata_result, LoadLibraryMetadataFromFileResultSuccess):
                # Stamp the user's verbatim registered_path onto the response so the GUI
                # can map this metadata back to the matching `libraries_to_register` row
                # without re-implementing the engine's path resolution logic.
                metadata_result.registered_path = discovered.registered_path
                successful_libraries.append(metadata_result)
            else:
                failed_libraries.append(cast("LoadLibraryMetadataFromFileResultFailure", metadata_result))

        # Generate sandbox library metadata if configured
        sandbox_library_dir = self._get_sandbox_directory()
        if sandbox_library_dir:
            # Try to load existing JSON first - only scan if load fails
            sandbox_json_path = sandbox_library_dir / LibraryManager.LIBRARY_CONFIG_FILENAME
            sandbox_result = self.load_library_metadata_from_file_request(
                LoadLibraryMetadataFromFileRequest(file_path=str(sandbox_json_path))
            )

            # If load failed, it either didn't exist or was malformed. Try scanning, which will generate a fresh one.
            if isinstance(sandbox_result, LoadLibraryMetadataFromFileResultFailure):
                scan_result = self.scan_sandbox_directory_request(
                    ScanSandboxDirectoryRequest(directory_path=str(sandbox_library_dir))
                )
                # Map scan result to load result for consistency
                if isinstance(scan_result, ScanSandboxDirectoryResultSuccess):
                    sandbox_result = LoadLibraryMetadataFromFileResultSuccess(
                        library_schema=scan_result.library_schema,
                        file_path=str(sandbox_json_path),
                        git_remote=None,
                        git_ref=None,
                        enabled=True,
                        result_details=scan_result.result_details,
                    )
                # else: Keep the load failure result

            if isinstance(sandbox_result, LoadLibraryMetadataFromFileResultSuccess):
                successful_libraries.append(sandbox_result)
            else:
                failed_libraries.append(sandbox_result)

        details = (
            f"Successfully loaded metadata for {len(successful_libraries)} libraries, {len(failed_libraries)} failed"
        )
        return LoadMetadataForAllLibrariesResultSuccess(
            successful_libraries=successful_libraries,
            failed_libraries=failed_libraries,
            result_details=details,
        )

    def _generate_sandbox_library_metadata(
        self,
        sandbox_directory: Path,
    ) -> LoadLibraryMetadataFromFileResultSuccess | LoadLibraryMetadataFromFileResultFailure | None:
        """Generate sandbox library metadata by scanning Python files without importing them.

        Args:
            sandbox_directory: Path to sandbox directory to scan.

        Returns None if no files are found.
        """
        sandbox_library_dir_as_posix = sandbox_directory.as_posix()

        if not sandbox_directory.exists():
            details = "Sandbox directory does not exist. If you wish to create a Sandbox directory to develop custom nodes: in the Griptape Nodes editor, go to Settings -> Libraries and navigate to the Sandbox Settings."
            return LoadLibraryMetadataFromFileResultFailure(
                library_path=sandbox_library_dir_as_posix,
                library_name=LibraryManager.SANDBOX_LIBRARY_NAME,
                status=LibraryManager.LibraryFitness.MISSING,
                problems=[SandboxDirectoryMissingProblem()],
                result_details=ResultDetails(message=details, level=logging.INFO),
            )

        sandbox_node_candidates = self._find_files_in_dir(directory=sandbox_directory, extension=".py")
        if not sandbox_node_candidates:
            logger.debug(
                "No candidate files found in sandbox directory '%s'. Creating empty sandbox library metadata.",
                sandbox_directory,
            )
            # Continue with empty list - create valid schema with 0 nodes
            sandbox_node_candidates = []

        # Try to load existing library JSON for smart merging
        json_path = sandbox_directory / LibraryManager.LIBRARY_CONFIG_FILENAME
        metadata_result = self.load_library_metadata_from_file_request(
            LoadLibraryMetadataFromFileRequest(file_path=str(json_path))
        )

        existing_schema = None
        if isinstance(metadata_result, LoadLibraryMetadataFromFileResultSuccess):
            existing_schema = metadata_result.library_schema
            logger.debug("Loaded existing sandbox library JSON from '%s'", json_path)
        else:
            logger.debug(
                "No existing sandbox library JSON or failed to load from '%s': %s. Will generate fresh schema.",
                json_path,
                metadata_result.result_details,
            )

        if existing_schema is not None:
            # Smart merge: preserve existing customizations, add new files, remove deleted files
            logger.debug(
                "Merging existing sandbox library JSON with discovered files in sandbox directory '%s'",
                sandbox_directory,
            )
            node_definitions = self._merge_sandbox_nodes(
                existing_schema=existing_schema,
                discovered_files=sandbox_node_candidates,
                sandbox_directory=sandbox_directory,
            )

            if not node_definitions:
                logger.debug(
                    "No valid node files found after merge in sandbox directory '%s'. Creating empty sandbox library metadata.",
                    sandbox_directory,
                )
                # Continue with empty list - create valid schema with 0 nodes
                node_definitions = []

            # Preserve existing library metadata
            library_name = existing_schema.name
            library_metadata = existing_schema.metadata
            categories = existing_schema.categories
            widgets = existing_schema.widgets

            # Update schema version to latest
            library_schema_version = LibrarySchema.LATEST_SCHEMA_VERSION

        else:
            # No existing JSON or it failed to load - generate fresh schema
            logger.debug(
                "Generating fresh sandbox library schema for sandbox directory '%s'",
                sandbox_directory,
            )

            node_definitions = self._create_placeholder_node_definitions(sandbox_node_candidates, sandbox_directory)

            # Create default metadata
            sandbox_category = CategoryDefinition(
                title="Sandbox",
                description=f"Nodes loaded from the {LibraryManager.SANDBOX_LIBRARY_NAME}.",
                color="#c7621a",
                icon="Folder",
            )

            engine_version = GriptapeNodes().handle_engine_version_request(request=GetEngineVersionRequest())
            if not isinstance(engine_version, GetEngineVersionResultSuccess):
                details = "Could not get engine version for sandbox library generation."
                return LoadLibraryMetadataFromFileResultFailure(
                    library_path=sandbox_library_dir_as_posix,
                    library_name=LibraryManager.SANDBOX_LIBRARY_NAME,
                    status=LibraryManager.LibraryFitness.UNUSABLE,
                    problems=[EngineVersionErrorProblem()],
                    result_details=details,
                )

            engine_version_str = f"{engine_version.major}.{engine_version.minor}.{engine_version.patch}"
            library_metadata = LibraryMetadata(
                author="Author needs to be specified when library is published.",
                description="Nodes loaded from the sandbox library.",
                library_version=engine_version_str,
                engine_version=engine_version_str,
                tags=["sandbox"],
                is_griptape_nodes_searchable=False,
            )
            categories = [
                {self.SANDBOX_CATEGORY_NAME: sandbox_category},
            ]
            library_name = LibraryManager.SANDBOX_LIBRARY_NAME
            library_schema_version = LibrarySchema.LATEST_SCHEMA_VERSION
            widgets = None  # Fresh schemas have no widgets defined yet

        # Create the library schema (now using variables set by either path)
        library_schema = LibrarySchema(
            name=library_name,
            library_schema_version=library_schema_version,
            metadata=library_metadata,
            categories=categories,
            nodes=node_definitions,
            widgets=widgets,
        )

        # Sandbox libraries are never git repositories - always set to None
        git_remote = None
        git_ref = None

        details = f"Successfully generated sandbox library metadata with {len(node_definitions)} nodes from {sandbox_directory}"
        return LoadLibraryMetadataFromFileResultSuccess(
            library_schema=library_schema,
            file_path=str(sandbox_directory),
            git_remote=git_remote,
            git_ref=git_ref,
            enabled=True,
            result_details=details,
        )

    def _create_placeholder_node_definitions(
        self,
        sandbox_node_candidates: list[Path],
        sandbox_directory: Path,
    ) -> list[NodeDefinition]:
        """Create placeholder node definitions for sandbox files that haven't been imported yet.

        Args:
            sandbox_node_candidates: List of Python files found in sandbox directory
            sandbox_directory: Path to sandbox directory for computing relative paths

        Returns:
            List of placeholder NodeDefinitions
        """
        node_definitions = []
        for candidate in sandbox_node_candidates:
            class_name = self.UNRESOLVED_SANDBOX_CLASS_NAME
            file_name = candidate.name

            node_metadata = NodeMetadata(
                category=self.SANDBOX_CATEGORY_NAME,
                description=f"'{file_name}' may contain one or more nodes defined in this candidate file.",
                display_name=file_name,
                icon="square-dashed",
                color=None,
            )
            node_definition = NodeDefinition(
                class_name=class_name,
                file_path=str(candidate.relative_to(sandbox_directory)),
                metadata=node_metadata,
            )
            node_definitions.append(node_definition)
        return node_definitions

    def _merge_sandbox_nodes(
        self,
        existing_schema: LibrarySchema,
        discovered_files: list[Path],
        sandbox_directory: Path,
    ) -> list[NodeDefinition]:
        """Merge existing node definitions with newly discovered files.

        Args:
            existing_schema: Previously saved library schema
            discovered_files: List of .py files found in sandbox directory
            sandbox_directory: Path to sandbox directory for computing relative paths

        Returns:
            Merged list of NodeDefinitions
        """
        # Create mapping of discovered files for quick lookup (use absolute resolved paths)
        discovered_file_paths = {str(canonicalize_for_identity(f)): f for f in discovered_files}

        # Keep existing nodes that still have corresponding files
        merged_nodes = []
        existing_file_paths = set()

        for existing_node in existing_schema.nodes:
            # Resolve the file path to absolute for comparison
            try:
                existing_file_path = str(canonicalize_for_identity(existing_node.file_path))
            except Exception as e:
                logger.warning(
                    "Could not resolve path for existing node '%s' at '%s': %s. Skipping.",
                    existing_node.class_name,
                    existing_node.file_path,
                    e,
                )
                continue

            # Keep node if file still exists
            if existing_file_path in discovered_file_paths:
                merged_nodes.append(existing_node)
                existing_file_paths.add(existing_file_path)
                logger.debug(
                    "Preserved existing sandbox node definition: %s (%s)",
                    existing_node.class_name,
                    existing_node.file_path,
                )
            else:
                logger.debug(
                    "Removing sandbox node '%s' - file no longer exists: %s",
                    existing_node.class_name,
                    existing_node.file_path,
                )

        # Add new files as placeholder nodes
        for discovered_file in discovered_files:
            discovered_file_path = str(canonicalize_for_identity(discovered_file))

            if discovered_file_path not in existing_file_paths:
                # Create placeholder node definition for new file
                class_name = self.UNRESOLVED_SANDBOX_CLASS_NAME
                file_name = discovered_file.name

                node_metadata = NodeMetadata(
                    category=self.SANDBOX_CATEGORY_NAME,
                    description=f"'{file_name}' may contain one or more nodes defined in this candidate file.",
                    display_name=file_name,
                    icon="square-dashed",
                    color=None,
                )
                node_definition = NodeDefinition(
                    class_name=class_name,
                    file_path=str(discovered_file.relative_to(sandbox_directory)),
                    metadata=node_metadata,
                )
                merged_nodes.append(node_definition)
                logger.debug(
                    "Added new placeholder sandbox node: %s (%s)",
                    file_name,
                    discovered_file.relative_to(sandbox_directory),
                )

        return merged_nodes

    def _get_sandbox_directory(self) -> Path | None:
        """Get the configured sandbox directory path.

        Returns:
            Path to sandbox directory if configured and exists, None otherwise.
        """
        config_mgr = GriptapeNodes.ConfigManager()
        sandbox_library_subdir = config_mgr.get_config_value("sandbox_library_directory")
        if not sandbox_library_subdir:
            return None

        sandbox_library_dir = resolve_workspace_path(Path(sandbox_library_subdir), config_mgr.workspace_path)
        if not sandbox_library_dir.exists():
            return None

        return sandbox_library_dir

    def scan_sandbox_directory_request(
        self,
        request: ScanSandboxDirectoryRequest,
    ) -> ScanSandboxDirectoryResultSuccess | ScanSandboxDirectoryResultFailure:
        """Handle ScanSandboxDirectoryRequest.

        Scans specified sandbox directory and generates/merges library metadata.
        """
        sandbox_directory = Path(request.directory_path)

        # Generate/merge library metadata
        result = self._generate_sandbox_library_metadata(sandbox_directory=sandbox_directory)

        # Note: result should never be None after Step 1 fix, but handle defensively
        if result is None:
            details = f"Internal error: _generate_sandbox_library_metadata returned None for {sandbox_directory}"
            return ScanSandboxDirectoryResultFailure(result_details=ResultDetails(message=details, level=logging.ERROR))

        if isinstance(result, LoadLibraryMetadataFromFileResultFailure):
            # Failure during generation
            return ScanSandboxDirectoryResultFailure(result_details=result.result_details)

        # Success
        return ScanSandboxDirectoryResultSuccess(
            library_schema=result.library_schema,
            result_details=ResultDetails(
                message=f"Scanned sandbox directory: {len(result.library_schema.nodes)} node definitions",
                level=logging.INFO,
            ),
        )

    def get_node_metadata_from_library_request(self, request: GetNodeMetadataFromLibraryRequest) -> ResultPayload:
        # Does this library exist?
        try:
            library = LibraryRegistry.get_library(name=request.library)
        except KeyError:
            details = f"Attempted to get node metadata for a node type '{request.node_type}' in a Library named '{request.library}'. Failed because no Library with that name was registered."
            result = GetNodeMetadataFromLibraryResultFailure(result_details=details)
            return result

        # Does the node type exist within the library?
        try:
            metadata = library.get_node_metadata(node_type=request.node_type)
        except KeyError:
            details = f"Attempted to get node metadata for a node type '{request.node_type}' in a Library named '{request.library}'. Failed because no node type of that name could be found in the Library."
            result = GetNodeMetadataFromLibraryResultFailure(result_details=details)
            return result

        details = f"Successfully retrieved node metadata for a node type '{request.node_type}' in a Library named '{request.library}'."

        result = GetNodeMetadataFromLibraryResultSuccess(
            metadata=metadata,
            result_details=details,
        )
        return result

    def register_sandbox_node_from_source_request(  # noqa: C901, PLR0911
        self, request: RegisterSandboxNodeFromSourceRequest
    ) -> ResultPayload:
        """Import a Python source file from the sandbox dir and register its BaseNode subclasses.

        Leverages existing engine primitives end to end:
          * `_get_sandbox_directory` resolves the configured path.
          * `_load_module_from_file` imports the source (with the existing hot-reload
            semantics when replacing an iterating draft).
          * `Library.register_new_node_type` attaches the class to the Sandbox Library, and
            `Library.unregister_node_type` removes any prior registration first when
            `replace_if_exists=True`.

        The handler does not write `request.file_path`; the caller is expected to have placed
        the file in the sandbox directory already (e.g. via `WriteFileRequest`). The file
        stays on disk, so the normal sandbox scan-and-load pipeline picks it up on the next
        engine start. We intentionally do not update the sandbox's
        `griptape_nodes_library.json` here: startup's own merge step (`_merge_sandbox_nodes`)
        discovers files that exist on disk but are absent from the manifest, and the loader
        resolves their class names and writes the manifest back for us.
        """
        # Resolve and validate the sandbox directory. Agents cannot register nodes on a
        # system that has not opted in to a sandbox.
        sandbox_dir = self._get_sandbox_directory()
        if sandbox_dir is None:
            details = (
                "Attempted to register a sandbox node from source. Failed because "
                "`sandbox_library_directory` is not configured (or the configured path does "
                "not exist). Set it in Settings -> Libraries -> Sandbox Settings first."
            )
            return RegisterSandboxNodeFromSourceResultFailure(result_details=details)

        # Canonicalize the requested path against the sandbox dir. Relative paths anchor to
        # the sandbox; absolute paths stay where they are. We then verify the result lives
        # under the canonical sandbox dir so callers can never reach outside it via `..` or
        # absolute paths to other locations.
        sandbox_root = canonicalize_for_identity(sandbox_dir)
        file_path = canonicalize_for_io(request.file_path, base=sandbox_dir)
        file_identity = canonicalize_for_identity(request.file_path, base=sandbox_dir)
        if not file_identity.is_relative_to(sandbox_root):
            details = (
                f"Attempted to register a sandbox node with file_path={request.file_path!r}. "
                f"Failed because the resolved path '{file_identity}' is not inside the "
                f"sandbox directory '{sandbox_root}'."
            )
            return RegisterSandboxNodeFromSourceResultFailure(result_details=details)
        if file_path.suffix != ".py":
            details = (
                f"Attempted to register a sandbox node with file_path={request.file_path!r}. "
                "Failed because file_path must point at a `.py` file so the sandbox loader "
                "can pick it up."
            )
            return RegisterSandboxNodeFromSourceResultFailure(result_details=details)
        if not file_path.is_file():
            details = (
                f"Attempted to register a sandbox node with file_path={request.file_path!r}. "
                f"Failed because no file exists at the resolved path '{file_path}'. Write "
                "the source file into the sandbox directory before calling this request."
            )
            return RegisterSandboxNodeFromSourceResultFailure(result_details=details)

        # Import the module. `_load_module_from_file` handles both first-load and hot-reload
        # (re-importing an existing module with fresh source), which is exactly what an agent
        # iterating on a draft needs.
        try:
            module = self._load_module_from_file(file_path, LibraryManager.SANDBOX_LIBRARY_NAME)
        except ImportError as err:
            details = f"Attempted to register a sandbox node from '{file_path}'. Failed at import time: {err}"
            return RegisterSandboxNodeFromSourceResultFailure(result_details=details)

        # The Sandbox Library must already be registered. It is created as part of normal
        # engine startup when the sandbox directory is configured; if it is missing here, the
        # user hasn't run through the sandbox setup at all.
        try:
            sandbox_library = LibraryRegistry.get_library(LibraryManager.SANDBOX_LIBRARY_NAME)
        except KeyError:
            details = (
                "Attempted to register a sandbox node, but the Sandbox Library is not "
                "registered in the engine. Ensure the sandbox directory has been initialized "
                "(it is scanned once at engine startup) before calling this request."
            )
            return RegisterSandboxNodeFromSourceResultFailure(result_details=details)

        # Discover BaseNode subclasses defined in this module. The filter matches the one in
        # `_attempt_load_nodes_from_sandbox_library_using_existing_schema` so that files
        # registered via MCP and files scanned at startup surface identically.
        registered_class_names: list[str] = []
        replaced_class_names: list[str] = []
        for class_name, obj in vars(module).items():
            if not (
                isinstance(obj, type)
                and issubclass(obj, BaseNode)
                and type(obj) is not BaseNode
                and obj.__module__ == module.__name__
            ):
                continue

            if sandbox_library.has_node_type(class_name):
                if not request.replace_if_exists:
                    details = (
                        f"Attempted to register node type '{class_name}' from '{file_path}'. "
                        "Failed because a node type with that name is already registered in "
                        "the Sandbox Library and replace_if_exists=False."
                    )
                    return RegisterSandboxNodeFromSourceResultFailure(result_details=details)
                sandbox_library.unregister_node_type(class_name)
                replaced_class_names.append(class_name)

            metadata = NodeMetadata(
                category=self.SANDBOX_CATEGORY_NAME,
                description=f"'{class_name}' (loaded from the {LibraryManager.SANDBOX_LIBRARY_NAME}).",
                display_name=class_name,
            )
            sandbox_library.register_new_node_type(obj, metadata)
            registered_class_names.append(class_name)

        if not registered_class_names:
            details = (
                f"Imported '{file_path}' successfully, but it does not declare any BaseNode "
                "subclasses (must be `class X(BaseNode):` defined in this file, not "
                "re-exported from another module). Nothing was registered."
            )
            return RegisterSandboxNodeFromSourceResultFailure(result_details=details)

        summary = (
            f"Registered {len(registered_class_names)} node type(s) from '{file_path}' "
            f"into the {LibraryManager.SANDBOX_LIBRARY_NAME} "
            f"(replaced: {len(replaced_class_names)})."
        )
        return RegisterSandboxNodeFromSourceResultSuccess(
            file_path=str(file_path),
            library_name=LibraryManager.SANDBOX_LIBRARY_NAME,
            registered_class_names=registered_class_names,
            replaced_class_names=replaced_class_names,
            result_details=summary,
        )

    def describe_node_type_request(self, request: DescribeNodeTypeRequest) -> ResultPayload:
        # Resolve the library for this node type. When no library is supplied, we rely on
        # LibraryRegistry to pick the unique library that provides it.
        try:
            library = LibraryRegistry.get_library_for_node_type(
                node_type=request.node_type, specific_library_name=request.library
            )
        except KeyError as err:
            details = f"Attempted to describe node type '{request.node_type}'. Failed when looking up its library because: {err}"
            return DescribeNodeTypeResultFailure(result_details=details)

        library_name = library.get_library_data().name

        # Make sure the node type really is registered in this library before we go further.
        try:
            node_metadata = library.get_node_metadata(node_type=request.node_type)
        except KeyError:
            details = f"Attempted to describe node type '{request.node_type}' in Library '{library_name}'. Failed because the Library has no node type with that name."
            return DescribeNodeTypeResultFailure(result_details=details)

        # Instantiate a throwaway node so we can read the parameters its __init__ declares.
        # The node is never added to a flow or the ObjectManager, so it is garbage-collected
        # when this method returns.
        #
        # Nodes whose __init__ performs I/O (network calls, auth checks, disk reads) can raise.
        # In that case we still return a success payload with the library-level metadata and a
        # WARNING entry in result_details, so callers can present the node at all instead of
        # getting an opaque failure for every such node type.
        node_class = library.get_node_class(request.node_type)
        probe_name = f"__describe_node_type_probe__{request.node_type}"
        try:
            # Wrap in ``LibraryRegistry.constructing_node()`` so the
            # parameter-mutation detector skips this ephemeral probe's
            # declarative ``add_parameter`` calls (this construction
            # bypasses ``LibraryRegistry.create_node``).
            #
            # Pass the node's library and type so declarative ``__init__`` logic
            # that resolves against the library -- e.g. ``get_declared_models``
            # populating a model dropdown from the ``model_catalog`` -- works
            # during the probe just as it does under ``create_node``.
            with LibraryRegistry.constructing_node():
                probe_node = node_class(
                    name=probe_name,
                    metadata={"library": library_name, "node_type": request.node_type},
                )
        except Exception as err:
            probe_error = f"{type(err).__name__}: {err}"
            return DescribeNodeTypeResultSuccess(
                library=library_name,
                node_type=request.node_type,
                metadata=node_metadata,
                parameters=[],
                result_details=ResultDetails(
                    ResultDetail(
                        level=logging.INFO,
                        message=(
                            f"Described node type '{request.node_type}' in Library '{library_name}' "
                            "with library metadata only."
                        ),
                    ),
                    ResultDetail(
                        level=logging.WARNING,
                        message=f"Parameter probe failed because: {probe_error}",
                    ),
                ),
            )

        parameters = [ParameterDescription.from_parameter(param) for param in probe_node.parameters]

        details = f"Successfully described node type '{request.node_type}' in Library '{library_name}'."
        return DescribeNodeTypeResultSuccess(
            library=library_name,
            node_type=request.node_type,
            metadata=node_metadata,
            parameters=parameters,
            result_details=details,
        )

    def list_categories_in_library_request(self, request: ListCategoriesInLibraryRequest) -> ResultPayload:
        # Does this library exist?
        try:
            library = LibraryRegistry.get_library(name=request.library)
        except KeyError:
            details = f"Attempted to get categories in a Library named '{request.library}'. Failed because no Library with that name was registered."
            result = ListCategoriesInLibraryResultFailure(result_details=details)
            return result

        categories = library.get_categories()
        result = ListCategoriesInLibraryResultSuccess(
            categories=categories, result_details=f"Successfully retrieved categories for library '{request.library}'."
        )
        return result

    async def register_library_from_file_request(self, request: RegisterLibraryFromFileRequest) -> ResultPayload:  # noqa: PLR0911 (result determination needs multiple returns)
        """Register a library by name or path, progressing through all lifecycle phases.

        Supports loading by library_name OR file_path (mutually exclusive), with optional
        discovery integration. Creates LibraryInfo if not already tracked.

        Args:
            request: RegisterLibraryFromFileRequest containing library_name OR file_path,
                    perform_discovery_if_not_found, and load_as_default_library

        Returns:
            RegisterLibraryFromFileResultSuccess if loaded, RegisterLibraryFromFileResultFailure otherwise
        """
        # Phase 1: Establish prerequisites
        prereq_result = await self._establish_register_library_prerequisites(request)

        # FAILURE CHECK FIRST
        if isinstance(prereq_result, RegisterLibraryFromFileResultFailure):
            return prereq_result

        # SUCCESS CHECK (library already loaded)
        if isinstance(prereq_result, RegisterLibraryFromFileResultSuccess):
            return prereq_result

        # Extract prerequisites
        library_info = prereq_result.library_info
        file_path = prereq_result.file_path

        # Phase 2: Progress through lifecycle phases
        progression_result = await self._progress_library_through_lifecycle(
            library_info=library_info, file_path=file_path, request=request
        )

        # FAILURE CHECK
        if isinstance(progression_result, RegisterLibraryFromFileResultFailure):
            return progression_result

        # Phase 3: Return appropriate result based on fitness
        # At this point, library_name must be set (it's set during METADATA_LOADED phase)
        if library_info.library_name is None:
            details = "Library loaded but library_name was not set during metadata loading"
            return RegisterLibraryFromFileResultFailure(result_details=details)

        match library_info.fitness:
            case LibraryManager.LibraryFitness.GOOD:
                details = f"Successfully loaded Library '{library_info.library_name}' from JSON file at {file_path}"
                return RegisterLibraryFromFileResultSuccess(
                    library_name=library_info.library_name,
                    result_details=ResultDetails(message=details, level=logging.INFO),
                )
            case LibraryManager.LibraryFitness.FLAWED:
                details = f"Successfully loaded Library JSON file from '{file_path}', but one or more nodes failed to load. Check the log for more details."
                return RegisterLibraryFromFileResultSuccess(
                    library_name=library_info.library_name,
                    result_details=ResultDetails(message=details, level=logging.WARNING),
                )
            case LibraryManager.LibraryFitness.UNUSABLE:
                details = f"Attempted to load Library JSON file from '{file_path}'. Failed because no nodes were loaded. Check the log for more details."
                return RegisterLibraryFromFileResultFailure(result_details=details)
            case LibraryManager.LibraryFitness.NOT_EVALUATED:
                # Worker-delegated libraries on the orchestrator: node imports are skipped
                # and fitness will be updated once the worker reports back via
                # LibraryLoadedNotification. Workers are started asynchronously (either by
                # AppStartSessionRequest or by _maybe_start_workers_for_existing_session)
                # so we must NOT block here -- doing so would prevent the orchestrator from
                # sending heartbeats to the worker process, causing it to self-terminate.
                details = f"Successfully registered Library '{library_info.library_name}' from '{file_path}'. Node loading is delegated to a worker process."
                return RegisterLibraryFromFileResultSuccess(
                    library_name=library_info.library_name,
                    result_details=ResultDetails(message=details, level=logging.INFO),
                )
            case _:
                details = f"Attempted to load Library JSON file from '{file_path}'. Failed because an unknown/unexpected fitness '{library_info.fitness}' was returned."
                return RegisterLibraryFromFileResultFailure(result_details=details)

    async def _establish_register_library_prerequisites(  # noqa: C901, PLR0911, PLR0912 (prerequisite validation needs branches)
        self, request: RegisterLibraryFromFileRequest
    ) -> (
        LibraryManager.RegisterLibraryPrerequisites
        | RegisterLibraryFromFileResultSuccess
        | RegisterLibraryFromFileResultFailure
    ):
        """Validate request and establish library identity.

        Returns:
            RegisterLibraryPrerequisites: Ready for lifecycle progression
            RegisterLibraryFromFileResultSuccess: Library already loaded (early exit)
            RegisterLibraryFromFileResultFailure: Validation or lookup failed
        """
        # Validate request has either library_name or file_path (but not both)
        if not request.library_name and not request.file_path:
            return RegisterLibraryFromFileResultFailure(
                result_details="Attempted to register a library. Failed because neither library name nor file path were specified."
            )

        if request.library_name and request.file_path:
            return RegisterLibraryFromFileResultFailure(
                result_details="Attempted to register a library. Failed because both library name and file path were specified."
            )

        library_name = request.library_name
        file_path = request.file_path

        # If file_path provided but not library_name, load metadata to get the name
        if file_path and not library_name:
            lib_info = self._library_file_path_to_info.get(file_path)

            # If we don't have LibraryInfo yet, load metadata to get the name
            if not lib_info or not lib_info.library_name:
                metadata_result = self.load_library_metadata_from_file_request(
                    LoadLibraryMetadataFromFileRequest(file_path=file_path)
                )

                if isinstance(metadata_result, LoadLibraryMetadataFromFileResultFailure):
                    return RegisterLibraryFromFileResultFailure(result_details=metadata_result.result_details)

                library_name = metadata_result.library_schema.name

                # Update or create LibraryInfo
                if lib_info:
                    lib_info.library_name = library_name
                    lib_info.library_version = metadata_result.library_schema.metadata.library_version
                    lib_info.lifecycle_state = LibraryManager.LibraryLifecycleState.METADATA_LOADED
                else:
                    # Create new LibraryInfo since it doesn't exist yet
                    lib_info = LibraryManager.LibraryInfo(
                        lifecycle_state=LibraryManager.LibraryLifecycleState.METADATA_LOADED,
                        library_path=file_path,
                        is_sandbox=False,
                        library_name=library_name,
                        library_version=metadata_result.library_schema.metadata.library_version,
                        fitness=LibraryManager.LibraryFitness.NOT_EVALUATED,
                        problems=[],
                    )
                    self._library_file_path_to_info[file_path] = lib_info
            else:
                library_name = lib_info.library_name

        # At this point, library_name must be set (either from request or from metadata)
        if not library_name:
            return RegisterLibraryFromFileResultFailure(result_details="Failed to determine library name")

        # Check if already loaded in registry
        try:
            LibraryRegistry.get_library(name=library_name)
            return RegisterLibraryFromFileResultSuccess(
                library_name=library_name,
                was_already_loaded=True,
                result_details=f"Library '{library_name}' already loaded",
            )
        except KeyError:
            pass  # Not loaded, continue

        # Look up LibraryInfo by library_name (supports lazy loading)
        library_info = self.get_library_info_by_library_name(library_name)

        # If not found and discovery is allowed, try discovery
        if library_info is None and request.perform_discovery_if_not_found:
            discover_result = await self.discover_libraries_request(DiscoverLibrariesRequest())
            if isinstance(discover_result, DiscoverLibrariesResultSuccess):
                library_info = self.get_library_info_by_library_name(library_name)

        # If still not found, fail
        if library_info is None:
            details = f"Library '{library_name}' not found"
            if request.perform_discovery_if_not_found:
                details += " (discovery was attempted)"
            return RegisterLibraryFromFileResultFailure(result_details=details)

        file_path = library_info.library_path

        # Check if already loaded in registry (by name if we have it)
        if library_info.library_name:
            try:
                LibraryRegistry.get_library(name=library_info.library_name)
            except KeyError:
                # Library not in registry, continue with loading
                pass
            else:
                # Already loaded and good to go
                return RegisterLibraryFromFileResultSuccess(
                    library_name=library_info.library_name,
                    was_already_loaded=True,
                    result_details=f"Library '{library_info.library_name}' already loaded",
                )

        # Prerequisites established - ready for lifecycle progression
        return LibraryManager.RegisterLibraryPrerequisites(library_info=library_info, file_path=file_path)

    async def _progress_library_through_lifecycle(  # noqa: C901, PLR0911, PLR0912, PLR0915 (lifecycle state machine needs branches/statements/returns)
        self,
        library_info: LibraryManager.LibraryInfo,
        file_path: str,
        request: RegisterLibraryFromFileRequest,
    ) -> None | RegisterLibraryFromFileResultFailure:
        """Progress library through lifecycle states until LOADED.

        Advances library_info through states: DISCOVERED → METADATA_LOADED →
        EVALUATED → DEPENDENCIES_INSTALLED → LOADED, or EVALUATED → WORKER_DELEGATED →
        WORKER_PENDING (worker confirmation pending, not LOADED yet).

        Modifies library_info in place as it progresses through states.

        Returns:
            None: Successfully progressed to LOADED state
            RegisterLibraryFromFileResultFailure: Failed during progression
        """
        while True:
            current_state = library_info.lifecycle_state

            match current_state:
                case LibraryManager.LibraryLifecycleState.LOADED:
                    # Terminal state: inconsistent (marked LOADED but not in registry)
                    details = f"Library '{library_info.library_name}' marked as LOADED but not in registry"
                    self._library_file_path_to_info[library_info.library_path] = library_info
                    return RegisterLibraryFromFileResultFailure(result_details=details)

                case LibraryManager.LibraryLifecycleState.FAILURE:
                    # Terminal state: failure
                    details = f"Library '{library_info.library_name}' is in FAILURE state and cannot be loaded"
                    self._library_file_path_to_info[library_info.library_path] = library_info
                    return RegisterLibraryFromFileResultFailure(result_details=details)

                case LibraryManager.LibraryLifecycleState.DISABLED:
                    # Terminal state: the user has disabled this library in libraries_to_register.
                    # Loading was intentionally skipped.
                    details = (
                        f"Library at '{library_info.library_path}' is disabled in libraries_to_register "
                        f"and was not loaded"
                    )
                    self._library_file_path_to_info[library_info.library_path] = library_info
                    return RegisterLibraryFromFileResultFailure(result_details=details)

                case LibraryManager.LibraryLifecycleState.DISCOVERED:
                    # DISCOVERED → METADATA_LOADED
                    # All libraries (including sandbox) load metadata from JSON file
                    metadata_result = self.load_library_metadata_from_file_request(
                        LoadLibraryMetadataFromFileRequest(file_path=library_info.library_path)
                    )

                    if isinstance(metadata_result, LoadLibraryMetadataFromFileResultFailure):
                        self._library_file_path_to_info[library_info.library_path] = library_info
                        return RegisterLibraryFromFileResultFailure(result_details=metadata_result.result_details)

                    # Update library_info with metadata results
                    library_info.library_name = metadata_result.library_schema.name
                    library_info.library_version = metadata_result.library_schema.metadata.library_version
                    library_info.requires_worker = self._resolve_requires_worker(
                        library_info.registered_path,
                        metadata_result.library_schema.metadata.declarations,
                    )
                    library_info.lifecycle_state = LibraryManager.LibraryLifecycleState.METADATA_LOADED

                case LibraryManager.LibraryLifecycleState.METADATA_LOADED:
                    # METADATA_LOADED → EVALUATED
                    # Need to load schema to pass to evaluate request
                    metadata_result = self.load_library_metadata_from_file_request(
                        LoadLibraryMetadataFromFileRequest(file_path=library_info.library_path)
                    )

                    if isinstance(metadata_result, LoadLibraryMetadataFromFileResultFailure):
                        self._library_file_path_to_info[library_info.library_path] = library_info
                        return RegisterLibraryFromFileResultFailure(result_details=metadata_result.result_details)

                    evaluate_result = self.evaluate_library_fitness_request(
                        EvaluateLibraryFitnessRequest(schema=metadata_result.library_schema)
                    )
                    if isinstance(evaluate_result, EvaluateLibraryFitnessResultFailure):
                        library_info.fitness = evaluate_result.fitness
                        library_info.lifecycle_state = LibraryManager.LibraryLifecycleState.FAILURE
                        library_info.problems.extend(evaluate_result.problems)
                        self._library_file_path_to_info[library_info.library_path] = library_info
                        return RegisterLibraryFromFileResultFailure(result_details=evaluate_result.result_details)

                    # Update library_info with evaluation results
                    library_info.fitness = evaluate_result.fitness
                    library_info.problems.extend(evaluate_result.problems)

                    # Check if library requirements are met by the current system
                    library_data = metadata_result.library_schema
                    library_requirements = (
                        library_data.metadata.resources.required
                        if library_data.metadata.resources is not None
                        else None
                    )
                    if library_requirements is not None:
                        requirements_check_result = self._check_library_requirements(
                            library_requirements, library_data.name
                        )
                        if requirements_check_result is not None:
                            library_info.fitness = LibraryManager.LibraryFitness.UNUSABLE
                            library_info.problems.append(requirements_check_result)
                            library_info.lifecycle_state = LibraryManager.LibraryLifecycleState.FAILURE
                            self._library_file_path_to_info[library_info.library_path] = library_info
                            details = f"Library '{library_data.name}' requirements not met: {library_requirements}"
                            return RegisterLibraryFromFileResultFailure(result_details=details)

                    library_info.lifecycle_state = LibraryManager.LibraryLifecycleState.EVALUATED

                case LibraryManager.LibraryLifecycleState.EVALUATED:
                    # EVALUATED -> DEPENDENCIES_INSTALLED or WORKER_DELEGATED
                    # Resolve library_dependencies before node imports: each dependency library must be
                    # fully loaded (including its venv added to sys.path via _add_library_paths_to_sys_path)
                    # before this library's nodes are imported in the LOADED phase. Venvs are completely
                    # isolated - dependency packages are not accessible to this library's pip install
                    # subprocess.
                    dep_metadata_result = self.load_library_metadata_from_file_request(
                        LoadLibraryMetadataFromFileRequest(file_path=library_info.library_path)
                    )
                    if not isinstance(dep_metadata_result, LoadLibraryMetadataFromFileResultFailure):
                        dep_schema = dep_metadata_result.library_schema
                        griptape_library_deps = [
                            d
                            for d in (dep_schema.metadata.declarations or [])
                            if isinstance(d, LibraryDependencyDeclaration)
                        ] or None

                        # Download and install any griptape libraries this library depends on.
                        if griptape_library_deps:
                            config_mgr = GriptapeNodes.ConfigManager()
                            install_behavior = config_mgr.get_config_value(
                                LIBRARY_DEPENDENCY_INSTALL_BEHAVIOR_KEY,
                                default=LibraryDependencyInstallBehavior.ALWAYS,
                                cast_type=str,
                            )
                            for dep in griptape_library_deps:
                                parsed = parse_git_url_with_ref(dep.url)
                                normalized_url = normalize_github_url(parsed.url)
                                repo_name = extract_repo_name_from_url(normalized_url)
                                already_registered = any(
                                    (info.library_name == repo_name or repo_name in Path(info.library_path).parts)
                                    and info.lifecycle_state != LibraryManager.LibraryLifecycleState.FAILURE
                                    and info.fitness
                                    not in (
                                        LibraryManager.LibraryFitness.UNUSABLE,
                                        LibraryManager.LibraryFitness.MISSING,
                                    )
                                    for info in self._library_file_path_to_info.values()
                                )
                                if already_registered:
                                    logger.debug(
                                        "Library dependency '%s' is already registered, skipping download",
                                        dep.url,
                                    )
                                    continue
                                if install_behavior == LibraryDependencyInstallBehavior.NEVER:
                                    if dep.required:
                                        library_info.problems.append(
                                            LibraryDependencyProblem(
                                                dependency_name=dep.url,
                                                error_message="Automatic dependency installation is disabled (library_dependency_install_behavior=never).",
                                            )
                                        )
                                        library_info.fitness = LibraryManager.LibraryFitness.FLAWED
                                    logger.debug(
                                        "Skipping download of library dependency '%s' (library_dependency_install_behavior=never)",
                                        dep.url,
                                    )
                                    continue
                                dep_result = await self.download_library_request(
                                    DownloadLibraryRequest(
                                        git_url=normalized_url,
                                        branch_tag_commit=parsed.ref,
                                        fail_on_exists=False,
                                        auto_register=True,
                                    )
                                )
                                if isinstance(dep_result, DownloadLibraryResultFailure):
                                    if dep.required:
                                        library_info.problems.append(
                                            LibraryDependencyProblem(
                                                dependency_name=dep.url,
                                                error_message=str(dep_result.result_details),
                                            )
                                        )
                                        library_info.fitness = LibraryManager.LibraryFitness.UNUSABLE
                                        library_info.lifecycle_state = LibraryManager.LibraryLifecycleState.FAILURE
                                        self._library_file_path_to_info[library_info.library_path] = library_info
                                        details = f"Attempted to load Library '{library_info.library_name}'. Failed to load required library dependency '{dep.url}': {dep_result.result_details}"
                                        return RegisterLibraryFromFileResultFailure(result_details=details)
                                    logger.warning(
                                        "Optional library dependency '%s' failed to load: %s",
                                        dep.url,
                                        dep_result.result_details,
                                    )

                    # On the orchestrator (_is_worker is False), skip venv creation and pip
                    # install for libraries that require a dedicated worker. The lifecycle still
                    # completes through LOADED so the library is registered in LibraryRegistry
                    # (needed for the editor and workflow loading). The worker installs its own
                    # deps in its own process.
                    if library_info.requires_worker and not self._is_worker:
                        library_info.lifecycle_state = LibraryManager.LibraryLifecycleState.WORKER_DELEGATED
                    else:
                        install_result = await self.install_library_dependencies_request(
                            InstallLibraryDependenciesRequest(library_file_path=library_info.library_path)
                        )
                        if isinstance(install_result, InstallLibraryDependenciesResultFailure):
                            self._library_file_path_to_info[library_info.library_path] = library_info
                            return RegisterLibraryFromFileResultFailure(result_details=install_result.result_details)

                        library_info.lifecycle_state = LibraryManager.LibraryLifecycleState.DEPENDENCIES_INSTALLED

                case (
                    LibraryManager.LibraryLifecycleState.DEPENDENCIES_INSTALLED
                    | LibraryManager.LibraryLifecycleState.WORKER_DELEGATED
                ):
                    # DEPENDENCIES_INSTALLED or WORKER_DELEGATED → LOADED

                    if not library_info.is_sandbox:
                        # REGULAR LIBRARIES: Standard registration from JSON file
                        # Load metadata and create library
                        metadata_result = self.load_library_metadata_from_file_request(
                            LoadLibraryMetadataFromFileRequest(file_path=library_info.library_path)
                        )

                        if isinstance(metadata_result, LoadLibraryMetadataFromFileResultFailure):
                            self._library_file_path_to_info[library_info.library_path] = library_info
                            return RegisterLibraryFromFileResultFailure(result_details=metadata_result.result_details)

                        library_data = metadata_result.library_schema
                        json_path = Path(file_path)
                        base_dir = json_path.parent.absolute()

                        # Add library directory and venv site-packages to sys.path
                        await self._add_library_paths_to_sys_path(library_data.name, file_path, base_dir)

                        # Load the advanced library module if specified
                        advanced_library_instance = None
                        if library_data.advanced_library_path:
                            try:
                                advanced_library_instance = self._load_advanced_library_module(
                                    library_data=library_data,
                                    base_dir=base_dir,
                                )
                            except Exception as err:
                                library_info.lifecycle_state = LibraryManager.LibraryLifecycleState.FAILURE
                                library_info.fitness = LibraryManager.LibraryFitness.UNUSABLE
                                library_info.problems.append(
                                    AdvancedLibraryLoadFailureProblem(
                                        advanced_library_path=library_data.advanced_library_path, error_message=str(err)
                                    )
                                )
                                self._library_file_path_to_info[file_path] = library_info
                                details = f"Attempted to load Library '{library_data.name}' from '{json_path}'. Failed to load Advanced Library module: {err}"
                                return RegisterLibraryFromFileResultFailure(result_details=details)

                        # Create or get the library
                        try:
                            library = LibraryRegistry.generate_new_library(
                                library_data=library_data,
                                mark_as_default_library=request.load_as_default_library,
                                advanced_library=advanced_library_instance,
                            )
                        except KeyError as err:
                            # Library already exists
                            library_info.lifecycle_state = LibraryManager.LibraryLifecycleState.FAILURE
                            library_info.fitness = LibraryManager.LibraryFitness.UNUSABLE
                            library_info.problems.append(DuplicateLibraryProblem())
                            self._library_file_path_to_info[file_path] = library_info
                            details = f"Attempted to load Library JSON file from '{json_path}'. Failed because a Library '{library_data.name}' already exists. Error: {err}."
                            return RegisterLibraryFromFileResultFailure(result_details=details)

                        # Check the library's custom config settings
                        if library_data.settings is not None:
                            library_info.problems.extend(self._persist_library_settings(library_data))

                        # For worker-delegated libraries on the orchestrator, skip node module
                        # imports entirely -- importing them would pull heavy deps (torch, triton,
                        # etc.) into the orchestrator process.  The library is already registered
                        # in LibraryRegistry (for the editor and workflow loading); the worker
                        # process handles node loading and will report fitness via
                        # LibraryLoadedNotification once it finishes.
                        if library_info.requires_worker and not self._is_worker:
                            library_info.fitness = LibraryManager.LibraryFitness.NOT_EVALUATED
                            library_info.lifecycle_state = LibraryManager.LibraryLifecycleState.WORKER_PENDING
                            self._library_file_path_to_info[file_path] = library_info
                        else:
                            # Attempt to load nodes from the library (modifies library_info in place)
                            await asyncio.to_thread(
                                self._attempt_load_nodes_from_library,
                                library_data=library_data,
                                library=library,
                                base_dir=base_dir,
                                library_info=library_info,
                            )
                            self._library_file_path_to_info[file_path] = library_info

                        # On a worker process, broadcast the notification so app.py can relay it
                        # to the orchestrator over the transport layer. Include serialized node
                        # schemas so the orchestrator can register stub classes without importing
                        # the library.
                        if (
                            self._is_worker
                            and library_info.lifecycle_state == LibraryManager.LibraryLifecycleState.LOADED
                            and library_info.library_name
                        ):
                            node_schemas = await self._serialize_library_node_schemas(library_info.library_name)
                            await GriptapeNodes.abroadcast_app_event(
                                LibraryLoadedNotification(
                                    library_name=library_info.library_name,
                                    fitness=library_info.fitness,
                                    problem_details=self.collate_problems_for_lib_info(library_info),
                                    node_schemas=node_schemas,
                                )
                            )
                    else:
                        # SANDBOX LIBRARIES: Full processing here (discovery + registration)
                        # Load metadata from JSON file (already generated in DISCOVERED → METADATA_LOADED)
                        sandbox_directory = Path(library_info.library_path).parent
                        metadata_result = self.load_library_metadata_from_file_request(
                            LoadLibraryMetadataFromFileRequest(file_path=library_info.library_path)
                        )

                        if isinstance(metadata_result, LoadLibraryMetadataFromFileResultFailure):
                            self._library_file_path_to_info[library_info.library_path] = library_info
                            return RegisterLibraryFromFileResultFailure(result_details=metadata_result.result_details)

                        # Add sandbox directory and venv site-packages to sys.path
                        library_data = metadata_result.library_schema
                        await self._add_library_paths_to_sys_path(
                            library_data.name, library_info.library_path, sandbox_directory
                        )

                        # Discover real class names by importing files
                        await self._attempt_generate_sandbox_library_from_schema(
                            library_schema=metadata_result.library_schema,
                            sandbox_directory=str(sandbox_directory),
                            library_info=library_info,
                        )
                        # Function handles registration and updates library_info with problems
                        # lifecycle_state set to LOADED by _attempt_load_nodes_from_library

                    # Exit loop after final phase
                    break

                case _:
                    # Unexpected state
                    msg = f"Library '{library_info.library_name}' in unexpected lifecycle state: {current_state}"
                    raise ValueError(msg)

        # Success - progressed to LOADED state
        return None

    def _persist_library_settings(self, library_data: LibrarySchema) -> list[LibraryProblem]:
        """Inject a library's declared settings into the user config, returning any problems.

        For each declared setting category: when the category does not yet exist,
        write the library's contents as-is. When it does, persist only what THIS
        library declares merged onto the GLOBAL user-config layer for the category.

        The existing category is read from the `user_config` layer, NOT the merged
        config. The merged config folds in the active project's
        project/workspace/env layers (e.g. libraries_to_download, requires_engine);
        writing that back through SetConfigCategory (which lands in the global user
        config) would leak those per-project values into every other project's
        startup. Reading user_config keeps the write scoped to what is genuinely
        global plus the library's own declared settings.
        """
        if library_data.settings is None:
            return []

        problems: list[LibraryProblem] = []
        config_mgr = GriptapeNodes.ConfigManager()
        for library_data_setting in library_data.settings:
            get_category_request = GetConfigCategoryRequest(
                category=library_data_setting.category,
                failure_log_level=logging.DEBUG,
            )
            get_category_result = GriptapeNodes.handle_request(get_category_request)
            if not isinstance(get_category_result, GetConfigCategoryResultSuccess):
                # Create new category
                create_new_category_request = SetConfigCategoryRequest(
                    category=library_data_setting.category, contents=library_data_setting.contents
                )
                create_new_category_result = GriptapeNodes.handle_request(create_new_category_request)
                if not isinstance(create_new_category_result, SetConfigCategoryResultSuccess):
                    problems.append(CreateConfigCategoryProblem(category_name=library_data_setting.category))
                    details = f"Failed attempting to create new config category '{library_data_setting.category}' for library '{library_data.name}'."
                    logger.error(details)
                continue

            # Normalize secrets_to_register before merge (handles list/dict format mismatch)
            library_contents = dict(library_data_setting.contents)
            existing_contents = dict(
                config_mgr.get_config_value(
                    library_data_setting.category,
                    config_source="user_config",
                    default={},
                )
            )
            if "secrets_to_register" in library_contents:
                library_contents["secrets_to_register"] = normalize_secrets_to_register(
                    library_contents["secrets_to_register"]
                )
            if "secrets_to_register" in existing_contents:
                existing_contents["secrets_to_register"] = normalize_secrets_to_register(
                    existing_contents["secrets_to_register"]
                )
            # Merge with existing category
            existing_category_contents = merge_dicts(
                library_contents,
                existing_contents,
                add_keys=True,
                merge_lists=True,
            )
            set_category_request = SetConfigCategoryRequest(
                category=library_data_setting.category, contents=existing_category_contents
            )
            set_category_result = GriptapeNodes.handle_request(set_category_request)
            if not isinstance(set_category_result, SetConfigCategoryResultSuccess):
                problems.append(UpdateConfigCategoryProblem(category_name=library_data_setting.category))
                details = f"Failed attempting to update config category '{library_data_setting.category}' for library '{library_data.name}'."
                logger.error(details)

        return problems

    async def register_library_from_requirement_specifier_request(
        self, request: RegisterLibraryFromRequirementSpecifierRequest
    ) -> ResultPayload:
        try:
            package_name = Requirement(request.requirement_specifier).name
            # Determine venv path for dependency installation
            venv_path = self._get_library_venv_path(package_name, None)

            # Check if a functional venv already exists; a broken directory will be
            # recreated by _init_library_venv, in which case dependencies must be installed.
            venv_already_exists = is_venv_functional(venv_path)

            # Only install dependencies if conditions are met
            try:
                library_python_venv_path = await self._init_library_venv(venv_path)
            except RuntimeError as e:
                details = f"Attempted to install library '{request.requirement_specifier}'. Failed when creating the virtual environment: {e}"
                return RegisterLibraryFromRequirementSpecifierResultFailure(result_details=details)

            if venv_already_exists:
                logger.debug(
                    "Skipping dependency installation for package '%s' - venv already exists at %s",
                    package_name,
                    venv_path,
                )
            elif self._can_write_to_venv_location(library_python_venv_path):
                # Check disk space before installing dependencies
                config_manager = GriptapeNodes.ConfigManager()
                min_space_gb = config_manager.get_config_value("minimum_disk_space_gb_libraries")
                if not OSManager.check_available_disk_space(Path(venv_path), min_space_gb):
                    error_msg = OSManager.format_disk_space_error(Path(venv_path))
                    details = f"Attempted to install library '{request.requirement_specifier}'. Failed when installing dependencies due to insufficient disk space (requires {min_space_gb} GB): {error_msg}"
                    return RegisterLibraryFromRequirementSpecifierResultFailure(result_details=details)

                uv_path = find_uv_bin()

                logger.info("Installing dependency '%s' with pip in venv at %s", package_name, venv_path)
                is_debug = config_manager.get_config_value("log_level").upper() == "DEBUG"
                await subprocess_run(
                    [
                        uv_path,
                        "pip",
                        "install",
                        request.requirement_specifier,
                        "--python",
                        str(library_python_venv_path),
                    ],
                    check=True,
                    capture_output=not is_debug,
                    text=True,
                )
            else:
                logger.debug(
                    "Skipping dependency installation for package '%s' - venv location at %s is not writable",
                    package_name,
                    venv_path,
                )
        except subprocess.CalledProcessError as e:
            details = f"Attempted to install library '{request.requirement_specifier}'. Failed: return code={e.returncode}, stdout={e.stdout}, stderr={e.stderr}"
            return RegisterLibraryFromRequirementSpecifierResultFailure(result_details=details)
        except InvalidRequirement as e:
            details = f"Attempted to install library '{request.requirement_specifier}'. Failed due to invalid requirement specifier: {e}"
            return RegisterLibraryFromRequirementSpecifierResultFailure(result_details=details)

        library_path = str(files(package_name).joinpath(request.library_config_name))

        register_result = await GriptapeNodes.ahandle_request(RegisterLibraryFromFileRequest(file_path=library_path))
        if isinstance(register_result, RegisterLibraryFromFileResultFailure):
            details = f"Attempted to install library '{request.requirement_specifier}'. Failed due to {register_result}"
            return RegisterLibraryFromRequirementSpecifierResultFailure(result_details=details)

        return RegisterLibraryFromRequirementSpecifierResultSuccess(
            library_name=request.requirement_specifier,
            result_details=f"Successfully registered library from requirement specifier: {request.requirement_specifier}",
        )

    async def _init_library_venv(self, library_venv_path: Path) -> Path:
        """Initialize a virtual environment for the library.

        If a functional virtual environment already exists at the path, it is reused.
        If a directory exists at the path but is not a functional venv (e.g. missing
        ``pyvenv.cfg`` or Python executable, or referencing a Python interpreter that
        has since been removed), it is deleted and recreated.

        Args:
            library_venv_path: Path to the virtual environment directory

        Returns:
            Path to the Python executable in the virtual environment

        Raises:
            RuntimeError: If the virtual environment cannot be created.
        """
        python_version = platform.python_version()

        if is_venv_functional(library_venv_path):
            logger.debug("Reusing existing virtual environment at %s", library_venv_path)
            return venv_python_path(library_venv_path)

        if await anyio.Path(library_venv_path).exists():
            logger.warning(
                "Existing path at %s is not a functional virtual environment; recreating it", library_venv_path
            )
            try:
                await asyncio.to_thread(shutil.rmtree, library_venv_path, onexc=OSManager.remove_readonly)
            except OSError as e:
                msg = f"Failed to remove broken virtual environment at {library_venv_path}: {e}"
                raise RuntimeError(msg) from e

        # Check disk space before creating virtual environment
        config_manager = GriptapeNodes.ConfigManager()
        min_space_gb = config_manager.get_config_value("minimum_disk_space_gb_libraries")
        if not OSManager.check_available_disk_space(library_venv_path.parent, min_space_gb):
            error_msg = OSManager.format_disk_space_error(library_venv_path.parent)
            logger.error(
                "Attempted to create virtual environment (requires %.1f GB). Failed: %s", min_space_gb, error_msg
            )
            error_message = f"Disk space error creating virtual environment (requires {min_space_gb} GB): {error_msg}"
            raise RuntimeError(error_message)

        try:
            uv_path = find_uv_bin()
            logger.info("Creating virtual environment at %s with Python %s", library_venv_path, python_version)
            is_debug = config_manager.get_config_value("log_level").upper() == "DEBUG"
            await subprocess_run(
                [uv_path, "venv", str(library_venv_path), "--python", python_version],
                check=True,
                capture_output=not is_debug,
                text=True,
            )
        except subprocess.CalledProcessError as e:
            msg = f"Failed to create virtual environment at {library_venv_path} with Python {python_version}: return code={e.returncode}, stdout={e.stdout}, stderr={e.stderr}"
            raise RuntimeError(msg) from e
        logger.debug("Created virtual environment at %s", library_venv_path)

        return venv_python_path(library_venv_path)

    def _check_library_requirements(
        self, requirements: dict[str, Any], library_name: str
    ) -> IncompatibleRequirementsProblem | None:
        """Check if the current system meets the library's resource requirements.

        Args:
            requirements: Dictionary of requirements in the format used by resource_instance.Requirements
            library_name: Name of the library being checked (for logging)

        Returns:
            IncompatibleRequirementsProblem if requirements are not met, None if they are met
        """
        logger.debug("Checking requirements for library '%s': %s", library_name, requirements)

        os_keys = {"platform", "arch", "version"}
        compute_keys = {"compute"}

        os_requirements = {k: v for k, v in requirements.items() if k in os_keys}
        compute_requirements = {k: v for k, v in requirements.items() if k in compute_keys}

        if os_requirements:
            list_request = ListCompatibleResourceInstancesRequest(
                resource_type_name="OSResourceType",
                requirements=os_requirements,
                include_locked=True,
            )
            result = GriptapeNodes.handle_request(list_request)

            if isinstance(result, ListCompatibleResourceInstancesResultSuccess) and not result.instance_ids:
                system_capabilities = self._get_system_capabilities()
                logger.warning(
                    "Library '%s' OS requirements not met. Required: %s, System: %s",
                    library_name,
                    os_requirements,
                    system_capabilities,
                )
                return IncompatibleRequirementsProblem(
                    requirements=requirements,
                    system_capabilities=system_capabilities,
                )

        if compute_requirements:
            list_request = ListCompatibleResourceInstancesRequest(
                resource_type_name="ComputeResourceType",
                requirements=compute_requirements,
                include_locked=True,
            )
            result = GriptapeNodes.handle_request(list_request)

            if isinstance(result, ListCompatibleResourceInstancesResultSuccess) and not result.instance_ids:
                system_capabilities = self._get_system_capabilities()
                logger.warning(
                    "Library '%s' compute requirements not met. Required: %s, System: %s",
                    library_name,
                    compute_requirements,
                    system_capabilities,
                )
                return IncompatibleRequirementsProblem(
                    requirements=requirements,
                    system_capabilities=system_capabilities,
                )

        return None

    def _get_system_capabilities(self) -> dict[str, Any]:
        """Get the current system's capabilities for error reporting.

        Returns:
            Dictionary of combined OS and compute capabilities or empty dict if unavailable
        """
        capabilities: dict[str, Any] = {}

        os_list_request = ListCompatibleResourceInstancesRequest(
            resource_type_name="OSResourceType",
            requirements=None,
            include_locked=True,
        )
        os_result = GriptapeNodes.handle_request(os_list_request)

        if isinstance(os_result, ListCompatibleResourceInstancesResultSuccess) and os_result.instance_ids:
            status_request = GetResourceInstanceStatusRequest(instance_id=os_result.instance_ids[0])
            status_result = GriptapeNodes.handle_request(status_request)
            if isinstance(status_result, GetResourceInstanceStatusResultSuccess):
                capabilities.update(status_result.status.capabilities)

        compute_list_request = ListCompatibleResourceInstancesRequest(
            resource_type_name="ComputeResourceType",
            requirements=None,
            include_locked=True,
        )
        compute_result = GriptapeNodes.handle_request(compute_list_request)

        if isinstance(compute_result, ListCompatibleResourceInstancesResultSuccess) and compute_result.instance_ids:
            status_request = GetResourceInstanceStatusRequest(instance_id=compute_result.instance_ids[0])
            status_result = GriptapeNodes.handle_request(status_request)
            if isinstance(status_result, GetResourceInstanceStatusResultSuccess):
                capabilities.update(status_result.status.capabilities)

        return capabilities

    def _get_library_venv_path(self, library_name: str, library_file_path: str | None = None) -> Path:
        """Get the path to the virtual environment directory for a library.

        Args:
            library_name: Name of the library
            library_file_path: Optional path to the library JSON file

        Returns:
            Path to the virtual environment directory
        """
        clean_library_name = library_name.replace(" ", "_").strip()

        if library_file_path is not None:
            # Create venv relative to the library.json file
            library_dir = Path(library_file_path).parent.absolute()
            return library_dir / ".venv"

        # Create venv relative to the xdg data home
        return xdg_data_home() / "griptape_nodes" / "libraries" / clean_library_name / ".venv"

    async def _add_library_paths_to_sys_path(self, library_name: str, library_file_path: str, base_dir: Path) -> None:
        """Add a library's directory and venv site-packages to sys.path.

        Args:
            library_name: Name of the library (for venv lookup)
            library_file_path: Path to the library JSON file (for venv lookup)
            base_dir: Library base directory to add for relative imports
        """
        sys.path.insert(0, str(base_dir))

        venv_path = self._get_library_venv_path(library_name, library_file_path)
        if await anyio.Path(venv_path).exists():
            site_packages = str(
                Path(
                    sysconfig.get_path(
                        "purelib",
                        vars={"base": str(venv_path), "platbase": str(venv_path)},
                    )
                )
            )
            sys.path.insert(0, site_packages)
            logger.debug("Added library '%s' venv to sys.path: %s", library_name, site_packages)

    def _can_write_to_venv_location(self, venv_python_path: Path) -> bool:
        """Check if we can write to the venv location (either create it or modify existing).

        Args:
            venv_python_path: Path to the python executable in the virtual environment

        Returns:
            True if we can write to the location, False otherwise
        """
        # On Windows, permission checks are hard. Assume we can write
        if OSManager.is_windows():
            return True

        venv_path = venv_python_path.parent.parent

        # If venv doesn't exist, check if parent directory is writable
        if not venv_path.exists():
            parent_dir = venv_path.parent
            try:
                return parent_dir.exists() and os.access(parent_dir, os.W_OK)
            except (OSError, AttributeError) as e:
                logger.debug("Could not check parent directory permissions for %s: %s", parent_dir, e)
                return False

        # If venv exists, check if we can write to it
        try:
            return os.access(venv_path, os.W_OK)
        except (OSError, AttributeError) as e:
            logger.debug("Could not check venv write permissions for %s: %s", venv_path, e)
            return False

    def unload_library_from_registry_request(self, request: UnloadLibraryFromRegistryRequest) -> ResultPayload:
        try:
            LibraryRegistry.unregister_library(library_name=request.library_name)
        except Exception as e:
            details = f"Attempted to unload library '{request.library_name}'. Failed due to {e}"
            return UnloadLibraryFromRegistryResultFailure(result_details=details)

        # Clean up all stable module aliases for this library
        self._unregister_all_stable_module_aliases_for_library(request.library_name)

        # Remove the library from our library info list. This prevents it from still showing
        # up in the table of attempted library loads.
        lib_info = self.get_library_info_by_library_name(request.library_name)
        if lib_info:
            del self._library_file_path_to_info[lib_info.library_path]
        details = f"Successfully unloaded (and unregistered) library '{request.library_name}'."
        return UnloadLibraryFromRegistryResultSuccess(result_details=details)

    def get_all_info_for_all_libraries_request(self, request: GetAllInfoForAllLibrariesRequest) -> ResultPayload:  # noqa: ARG002
        libraries = LibraryRegistry.list_libraries()

        try:
            # Create a mapping of library name to all its info.
            library_name_to_all_info = {}

            for library_name in libraries:
                library_all_info_request = GetAllInfoForLibraryRequest(library=library_name)
                library_all_info_result = self.get_all_info_for_library_request(library_all_info_request)

                if not library_all_info_result.succeeded():
                    details = f"Attempted to get all info for all libraries, but failed when getting all info for library named '{library_name}'."
                    return GetAllInfoForAllLibrariesResultFailure(result_details=details)

                library_all_info_success = cast("GetAllInfoForLibraryResultSuccess", library_all_info_result)

                library_name_to_all_info[library_name] = library_all_info_success
        except Exception as err:
            details = f"Attempted to get all info for all libraries. Encountered error {err}."
            return GetAllInfoForAllLibrariesResultFailure(result_details=details)

        # We're home free now
        details = "Successfully retrieved all info for all libraries."
        result = GetAllInfoForAllLibrariesResultSuccess(
            library_name_to_library_info=library_name_to_all_info, result_details=details
        )
        return result

    async def on_get_all_info_for_all_libraries_request(
        self, request: GetAllInfoForAllLibrariesRequest
    ) -> ResultPayload:
        """Async handler for GetAllInfoForAllLibrariesRequest that waits for library loading to complete."""
        await self._libraries_loading_complete.wait()
        return await asyncio.to_thread(self.get_all_info_for_all_libraries_request, request)

    def get_all_info_for_library_request(self, request: GetAllInfoForLibraryRequest) -> ResultPayload:  # noqa: PLR0911, PLR0912, PLR0915, C901
        # Does this library exist?
        try:
            library = LibraryRegistry.get_library(name=request.library)
        except KeyError:
            details = f"Attempted to get all library info for a Library named '{request.library}'. Failed because no Library with that name was registered."
            result = GetAllInfoForLibraryResultFailure(result_details=details)
            return result

        library_metadata_request = GetLibraryMetadataRequest(library=request.library)
        library_metadata_result = self.get_library_metadata_request(library_metadata_request)

        if not library_metadata_result.succeeded():
            details = f"Attempted to get all library info for a Library named '{request.library}'. Failed attempting to get the library's metadata."
            return GetAllInfoForLibraryResultFailure(result_details=details)

        list_categories_request = ListCategoriesInLibraryRequest(library=request.library)
        list_categories_result = self.list_categories_in_library_request(list_categories_request)

        if not list_categories_result.succeeded():
            details = f"Attempted to get all library info for a Library named '{request.library}'. Failed attempting to get the list of categories in the library."
            return GetAllInfoForLibraryResultFailure(result_details=details)

        node_type_list_request = ListNodeTypesInLibraryRequest(library=request.library)
        node_type_list_result = self.on_list_node_types_in_library_request(node_type_list_request)

        if not node_type_list_result.succeeded():
            details = f"Attempted to get all library info for a Library named '{request.library}'. Failed attempting to get the list of node types in the library."
            return GetAllInfoForLibraryResultFailure(result_details=details)

        # Cast everyone to their success counterparts.
        try:
            library_metadata_result_success = cast("GetLibraryMetadataResultSuccess", library_metadata_result)
            list_categories_result_success = cast("ListCategoriesInLibraryResultSuccess", list_categories_result)
            node_type_list_result_success = cast("ListNodeTypesInLibraryResultSuccess", node_type_list_result)
        except Exception as err:
            details = (
                f"Attempted to get all library info for a Library named '{request.library}'. Encountered error: {err}."
            )
            return GetAllInfoForLibraryResultFailure(result_details=details)

        # Now build the map of node types to metadata.
        node_type_name_to_node_metadata_details = {}
        for node_type_name in node_type_list_result_success.node_types:
            node_metadata_request = GetNodeMetadataFromLibraryRequest(library=request.library, node_type=node_type_name)
            node_metadata_result = self.get_node_metadata_from_library_request(node_metadata_request)

            if not node_metadata_result.succeeded():
                details = f"Attempted to get all library info for a Library named '{request.library}'. Failed attempting to get the metadata for a node type called '{node_type_name}'."
                return GetAllInfoForLibraryResultFailure(result_details=details)

            try:
                node_metadata_result_success = cast("GetNodeMetadataFromLibraryResultSuccess", node_metadata_result)
            except Exception as err:
                details = f"Attempted to get all library info for a Library named '{request.library}'. Encountered error: {err}."
                return GetAllInfoForLibraryResultFailure(result_details=details)

            # Put it into the map.
            node_type_name_to_node_metadata_details[node_type_name] = node_metadata_result_success

        # Build widget info list if the library has widgets
        widgets_info: list[WidgetInfo] | None = None
        library_data = library.get_library_data()
        if library_data.widgets:
            logger.debug(
                "Library '%s' has %d widget(s), building widget info",
                request.library,
                len(library_data.widgets),
            )
            # Get the static server base URL for constructing absolute bundle URLs
            static_server_base_url = GriptapeNodes.StaticFilesManager().static_server_base_url
            # Get the library directory so we can hash each bundle file
            library_info_for_path = self.get_library_info_by_library_name(request.library)
            library_dir = Path(library_info_for_path.library_path).parent if library_info_for_path is not None else None
            widgets_info = []
            for widget_def in library_data.widgets:
                # Construct the full URL for this widget
                # The frontend will fetch from: {static_server_base_url}/api/libraries/{library_name}/widgets/{path}
                base_url = f"{static_server_base_url}/api/libraries/{request.library}/widgets/{widget_def.path}"
                # Append a content hash so browsers re-fetch when the bundle file changes
                try:
                    if library_dir is not None:
                        content_hash = hashlib.sha256((library_dir / widget_def.path).read_bytes()).hexdigest()[:8]
                        bundle_url = f"{base_url}?v={content_hash}"
                    else:
                        bundle_url = base_url
                except OSError:
                    bundle_url = base_url
                logger.debug(
                    "Widget '%s' from library '%s': bundle_url=%s",
                    widget_def.name,
                    request.library,
                    bundle_url,
                )
                widgets_info.append(
                    WidgetInfo(
                        name=widget_def.name,
                        bundle_url=bundle_url,
                        description=widget_def.description,
                    )
                )

        details = f"Successfully got all library info for a Library named '{request.library}'."
        result = GetAllInfoForLibraryResultSuccess(
            library_metadata_details=library_metadata_result_success,
            category_details=list_categories_result_success,
            node_type_name_to_node_metadata_details=node_type_name_to_node_metadata_details,
            widgets=widgets_info,
            result_details=details,
        )
        return result

    def _create_stable_namespace(self, library_name: str, file_path: Path) -> str:
        """Create a stable namespace for a dynamic module.

        Args:
            library_name: Name of the library
            file_path: Path to the Python file

        Returns:
            Stable namespace string like 'griptape_nodes.node_libraries.runwayml_library.image_to_video'
        """
        # Convert library name to safe module name
        safe_library_name = library_name.lower().replace(" ", "_").replace("-", "_")
        # Remove invalid characters
        safe_library_name = "".join(c for c in safe_library_name if c.isalnum() or c == "_")

        # Convert file path to safe module name
        safe_file_name = file_path.stem.replace("-", "_")

        return f"griptape_nodes.node_libraries.{safe_library_name}.{safe_file_name}"

    def _register_stable_module_alias(
        self, dynamic_module_name: str, stable_namespace: str, module: ModuleType, library_name: str
    ) -> None:
        """Register a stable alias for a dynamic module in sys.modules.

        Args:
            dynamic_module_name: Original dynamic module name
            stable_namespace: Stable namespace to alias to
            module: The loaded module
            library_name: Name of the library
        """
        # Update our mapping
        self._dynamic_to_stable_module_mapping[dynamic_module_name] = stable_namespace
        self._stable_to_dynamic_module_mapping[stable_namespace] = dynamic_module_name

        # Track library-to-modules mapping for bulk cleanup
        library_key = library_name
        if library_key not in self._library_to_stable_modules:
            self._library_to_stable_modules[library_key] = set()
        self._library_to_stable_modules[library_key].add(stable_namespace)

        # Register the stable alias in sys.modules
        sys.modules[stable_namespace] = module

        details = f"Registered stable alias: {stable_namespace} -> {dynamic_module_name} (library: {library_key})"
        logger.debug(details)

    def _unregister_stable_module_alias(self, dynamic_module_name: str) -> None:
        """Unregister a stable alias for a dynamic module during hot reload.

        Args:
            dynamic_module_name: Original dynamic module name
        """
        if dynamic_module_name in self._dynamic_to_stable_module_mapping:
            stable_namespace = self._dynamic_to_stable_module_mapping[dynamic_module_name]

            # Remove from sys.modules if it exists
            if stable_namespace in sys.modules:
                del sys.modules[stable_namespace]

            # Remove from library tracking
            for library_modules in self._library_to_stable_modules.values():
                library_modules.discard(stable_namespace)

            # Remove from our mappings
            del self._dynamic_to_stable_module_mapping[dynamic_module_name]
            del self._stable_to_dynamic_module_mapping[stable_namespace]

            details = f"Unregistered stable alias: {stable_namespace}"
            logger.debug(details)

    def _unregister_all_stable_module_aliases_for_library(self, library_name: str) -> None:
        """Unregister all stable module aliases for a library during library unload/reload.

        Args:
            library_name: Name of the library to clean up
        """
        library_key = library_name
        if library_key not in self._library_to_stable_modules:
            return

        stable_namespaces = self._library_to_stable_modules[library_key].copy()
        details = f"Unregistering {len(stable_namespaces)} stable aliases for library: {library_name}"
        logger.debug(details)

        for stable_namespace in stable_namespaces:
            # Remove from sys.modules if it exists
            if stable_namespace in sys.modules:
                del sys.modules[stable_namespace]

            # Find and remove from dynamic mapping
            dynamic_module_name = self._stable_to_dynamic_module_mapping.get(stable_namespace)
            if dynamic_module_name:
                self._dynamic_to_stable_module_mapping.pop(dynamic_module_name, None)
            self._stable_to_dynamic_module_mapping.pop(stable_namespace, None)

        # Clear the library's module set
        del self._library_to_stable_modules[library_key]
        details = f"Completed cleanup of stable aliases for library: '{library_name}'."
        logger.debug(details)

    def get_stable_namespace_for_dynamic_module(self, dynamic_module_name: str) -> str | None:
        """Get the stable namespace for a dynamic module name.

        This method is used during workflow serialization to convert dynamic module names
        (like "gtn_dynamic_module_image_to_video_py_123456789") to stable namespace imports
        (like "griptape_nodes.node_libraries.runwayml_library.image_to_video").

        Args:
            dynamic_module_name: The dynamic module name to look up

        Returns:
            The stable namespace string, or None if not found

        Example:
            >>> manager.get_stable_namespace_for_dynamic_module("gtn_dynamic_module_image_to_video_py_123456789")
            "griptape_nodes.node_libraries.runwayml_library.image_to_video"
        """
        return self._dynamic_to_stable_module_mapping.get(dynamic_module_name)

    def is_dynamic_module(self, module_name: str) -> bool:
        """Check if a module name represents a dynamically loaded module.

        Args:
            module_name: The module name to check

        Returns:
            True if this is a dynamic module name, False otherwise

        Example:
            >>> manager.is_dynamic_module("gtn_dynamic_module_image_to_video_py_123456789")
            True
            >>> manager.is_dynamic_module("griptape.artifacts")
            False
        """
        return module_name.startswith("gtn_dynamic_module_")

    @staticmethod
    def _get_root_cause_from_exception(exception: BaseException) -> BaseException:
        """Walk the exception chain to find the root cause.

        Args:
            exception: The exception to walk

        Returns:
            The root cause exception (the innermost exception in the chain)
        """
        current = exception
        while current.__cause__ is not None:
            current = current.__cause__
        return current

    @staticmethod
    def _check_engine_version_compatibility(required_engine_version: str) -> tuple[bool, str]:
        """Check if a required engine version is compatible with the current engine.

        Args:
            required_engine_version: The engine version required by the library.

        Returns:
            A tuple of (is_compatible, current_engine_version).
            is_compatible is True if required_engine_version <= current_engine_version.
            If version comparison fails, returns (True, current_engine_version) to allow the operation.
        """
        engine_version_result = GriptapeNodes.handle_request(GetEngineVersionRequest())
        if not isinstance(engine_version_result, GetEngineVersionResultSuccess):
            logger.warning("Failed to get engine version for compatibility check, allowing operation to proceed")
            return True, ""

        current_engine_version = (
            f"{engine_version_result.major}.{engine_version_result.minor}.{engine_version_result.patch}"
        )

        if not required_engine_version:
            return True, current_engine_version

        try:
            required_ver = Version.parse(required_engine_version)
            current_ver = Version.parse(current_engine_version)
            is_compatible = required_ver <= current_ver
        except ValueError:
            # If version parsing fails, assume compatible
            return True, current_engine_version
        else:
            return is_compatible, current_engine_version

    def _load_module_from_file(self, file_path: Path | str, library_name: str) -> ModuleType:
        """Dynamically load a module from a Python file with support for hot reloading.

        Args:
            file_path: Path to the Python file
            library_name: Name of the library

        Returns:
            The loaded module

        Raises:
            ImportError: If the module cannot be imported
        """
        # Ensure file_path is a Path object
        file_path = Path(file_path)

        # Generate a unique module name
        module_name = f"gtn_dynamic_module_{file_path.name.replace('.', '_')}_{hash(str(file_path))}"

        # Create stable namespace
        stable_namespace = self._create_stable_namespace(library_name, file_path)

        # Check if this module is already loaded
        if module_name in sys.modules:
            # For dynamically loaded modules, we need to re-create the module
            # with a fresh spec rather than using importlib.reload

            # Unregister old stable alias
            self._unregister_stable_module_alias(module_name)

            # Remove the old module from sys.modules
            old_module = sys.modules.pop(module_name)

            # Create a fresh spec and module
            spec = importlib.util.spec_from_file_location(module_name, file_path)
            if spec is None or spec.loader is None:
                msg = f"Could not load module specification from {file_path}"
                raise ImportError(msg)

            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module

            try:
                # Execute the module with the new code
                spec.loader.exec_module(module)
                # Register new stable alias
                self._register_stable_module_alias(module_name, stable_namespace, module, library_name)
                details = f"Hot reloaded module: {module_name} from {file_path}"
                logger.debug(details)
            except Exception as e:
                # Restore the old module in case of failure
                sys.modules[module_name] = old_module
                msg = f"Error reloading module {module_name} from {file_path}: {e}"
                raise ImportError(msg) from e

        # Load it for the first time
        else:
            # Load the module specification
            spec = importlib.util.spec_from_file_location(module_name, file_path)
            if spec is None or spec.loader is None:
                msg = f"Could not load module specification from {file_path}"
                raise ImportError(msg)

            # Create the module
            module = importlib.util.module_from_spec(spec)

            # Add to sys.modules to handle recursive imports
            sys.modules[module_name] = module

            # Execute the module
            try:
                spec.loader.exec_module(module)
                # Register stable alias
                self._register_stable_module_alias(module_name, stable_namespace, module, library_name)
            except Exception as err:
                msg = f"Module at '{file_path}' failed to load with error: {err}"
                raise ImportError(msg) from err

        return module

    def _load_class_from_file(self, file_path: Path | str, class_name: str, library_name: str) -> type[BaseNode]:
        """Dynamically load a class from a Python file with support for hot reloading.

        Args:
            file_path: Path to the Python file
            class_name: Name of the class to load
            library_name: Name of the library

        Returns:
            The loaded class

        Raises:
            ImportError: If the module cannot be imported
            AttributeError: If the class doesn't exist in the module
            TypeError: If the loaded class isn't a BaseNode-derived class
        """
        try:
            module = self._load_module_from_file(file_path, library_name)
        except ImportError as err:
            msg = f"Attempted to load class '{class_name}'. Error: {err}"
            raise ImportError(msg) from err

        # Get the class
        try:
            node_class = getattr(module, class_name)
        except AttributeError as err:
            msg = f"Class '{class_name}' not found in module '{file_path}'"
            raise AttributeError(msg) from err

        # Verify it's a BaseNode subclass
        if not issubclass(node_class, BaseNode):
            msg = f"'{class_name}' must inherit from BaseNode"
            raise TypeError(msg)

        return node_class

    async def _load_and_track_library(self, lib_path: str, index: int, total: int) -> None:
        """Load a single library and emit the corresponding progress event."""
        # Emit the LOADING event BEFORE registering: register_library_from_file_request installs
        # the library's dependencies (a slow pip/uv step), so emitting after it would leave the
        # GUI with no progress signal during the longest part of startup. The library name is
        # populated during discovery; fall back to a path-derived name if it isn't set yet.
        pre_register_info = self._library_file_path_to_info.get(lib_path)
        pending_library_name = (
            pre_register_info.library_name
            if pre_register_info and pre_register_info.library_name
            else Path(lib_path).stem
        )
        GriptapeNodes.EventManager().put_event(
            AppEvent(
                payload=EngineInitializationProgress(
                    phase=InitializationPhase.LIBRARIES,
                    item_name=pending_library_name,
                    status=InitializationStatus.LOADING,
                    current=index,
                    total=total,
                    is_worker=self._is_worker,
                )
            )
        )

        load_result = await self.register_library_from_file_request(
            RegisterLibraryFromFileRequest(
                file_path=lib_path,
                load_as_default_library=False,
            )
        )

        if isinstance(load_result, RegisterLibraryFromFileResultFailure):
            logger.warning("Failed to load library at '%s': %s", lib_path, load_result.result_details)
            error_message = (
                load_result.result_details.result_details[0].message
                if isinstance(load_result.result_details, ResultDetails)
                else str(load_result.result_details)
            )
            GriptapeNodes.EventManager().put_event(
                AppEvent(
                    payload=EngineInitializationProgress(
                        phase=InitializationPhase.LIBRARIES,
                        item_name=lib_path,
                        status=InitializationStatus.FAILED,
                        current=index,
                        total=total,
                        error=error_message,
                        is_worker=self._is_worker,
                    )
                )
            )
        elif isinstance(load_result, RegisterLibraryFromFileResultSuccess):
            GriptapeNodes.EventManager().put_event(
                AppEvent(
                    payload=EngineInitializationProgress(
                        phase=InitializationPhase.LIBRARIES,
                        item_name=load_result.library_name,
                        status=InitializationStatus.COMPLETE,
                        current=index,
                        total=total,
                        is_worker=self._is_worker,
                    )
                )
            )

    async def load_all_libraries_from_config(self, target_library_names: list[str] | None = None) -> list[str]:
        """Reconcile sourced libraries, then discover and load every enabled library.

        Reconcile runs first (engine_version gate + provision of git-sourced
        entries) so freshly provisioned libraries are present on disk for the
        discovery pass below. Reconcile failure details are returned, not raised:
        the boot caller logs and continues so a bad pin cannot brick startup,
        while the interactive reload caller turns non-empty details into a
        failure result. Library discovery/loading always proceeds regardless so
        the engine comes up with whatever libraries it can.

        Returns the reconcile failure details (empty list on success).
        """
        # Recreate the event bound to the current event loop. Calling .clear() on an event
        # created by a previous asyncio.run() call raises RuntimeError when awaited from
        # the new loop (asyncio.Event objects are bound to the loop they were created on).
        self._libraries_loading_complete = asyncio.Event()

        reconcile_failures = await self._reconcile_libraries_from_config()

        # Discover all available libraries (config + sandbox)
        discover_result = await self.discover_libraries_request(DiscoverLibrariesRequest())
        if isinstance(discover_result, DiscoverLibrariesResultFailure):
            logger.error("Failed to discover libraries: %s", discover_result.result_details)
            self._libraries_loading_complete.set()
            return reconcile_failures

        # Build list of library paths to load
        libraries_to_load = []
        for discovered_lib in discover_result.libraries_discovered:
            lib_path = str(discovered_lib.path)
            lib_info = self._library_file_path_to_info.get(lib_path)

            if lib_info and lib_info.lifecycle_state != LibraryManager.LibraryLifecycleState.DISABLED:
                libraries_to_load.append(lib_path)

        if not libraries_to_load:
            logger.info("No libraries found in configuration.")
            self._libraries_loading_complete.set()
            return reconcile_failures

        # Calculate total libraries for progress tracking
        total_libraries = len(libraries_to_load)

        for current_library_index, lib_path in enumerate(libraries_to_load, start=1):
            # When running as a dedicated library worker, skip libraries that don't match the target.
            # library_name is already populated in _library_file_path_to_info from the discovery phase.
            lib_info = self._library_file_path_to_info.get(lib_path)
            if target_library_names is not None and (
                lib_info is None or lib_info.library_name not in target_library_names
            ):
                continue

            await self._load_and_track_library(lib_path, current_library_index, total_libraries)

        # Remove any missing libraries AFTER we've loaded them for the user.
        user_libraries_section = LIBRARIES_TO_REGISTER_KEY
        self._remove_missing_libraries_from_config(config_category=user_libraries_section)

        self._libraries_loading_complete.set()
        return reconcile_failures

    async def on_preview_project_provisioning_request(
        self, request: PreviewProjectProvisioningRequest
    ) -> PreviewProjectProvisioningResultSuccess | PreviewProjectProvisioningResultFailure:
        """Compute the library provisioning plan for a loaded project, read-only.

        Sync and side-effect-free. To match what activation will actually do, it
        reconstructs the same effective config the live reconcile reads:
        ProjectManager resolves the project (canonically, like
        on_set_current_project_request) plus its workspace dir, then ConfigManager
        merges every layer (defaults -> user -> project-adjacent -> workspace ->
        override -> env) without mutating live state. Reading the project-adjacent
        file alone diverges whenever a higher-priority layer sets
        `libraries_to_download`/`engine_version`. It also runs the same
        engine_version gate (`engine_version_failure_detail`) on that merged config
        so the preview can warn before the user approves a plan that activation
        would reject. The GUI calls this before committing to a switch so the user
        can approve or refuse the changes. System defaults are previewable too:
        switching to them merges defaults -> user -> env (no project-adjacent or
        workspace-file layer), and that merged config can still carry a user-config
        library pin or engine_version, so it gets the same plan + gate. A non-loaded
        file-backed project (or one with no adjacent config dir) is a Failure.
        """
        config_mgr = GriptapeNodes.ConfigManager()

        # System defaults is a synthetic id, not a path, so match it verbatim before
        # any canonicalization (mirroring on_set_current_project_request). Its activation
        # reads no project-adjacent or workspace-file config layer, so the preview must
        # not either, or it would drift from what the reconcile actually provisions.
        if request.project_id == SYSTEM_DEFAULTS_KEY:
            merged = config_mgr.compute_system_defaults_provisioning_config()
        else:
            dirs = GriptapeNodes.ProjectManager().resolve_provisioning_config_dirs(request.project_id)
            if dirs is None:
                return PreviewProjectProvisioningResultFailure(
                    result_details=f"Attempted to preview provisioning for project '{request.project_id}'. "
                    f"Failed because the project is not loaded or has no project-adjacent config directory",
                )
            merged = config_mgr.compute_project_provisioning_config(
                dirs.project_dir, dirs.workspace_dir, apply_override=dirs.apply_override
            )

        engine_version_failure = engine_version_failure_detail(get_dot_value(merged, REQUIRES_ENGINE_KEY, default=None))

        raw_libraries = get_dot_value(merged, LIBRARIES_TO_DOWNLOAD_KEY, default=[])
        downloads = normalize_library_downloads(raw_libraries)

        # Probe the installed versions against the TARGET project's libraries dir, not the
        # live one, so the plan matches what activation would reconcile in that workspace.
        # Both keys come from Settings defaults, so they are always present in `merged`.
        libraries_path = resolve_workspace_path(
            Path(get_dot_value(merged, "libraries_directory")),
            Path(get_dot_value(merged, "workspace_directory")),
        )

        actions = await asyncio.gather(
            *(self._plan_one_library_provisioning(download, libraries_path) for download in downloads)
        )
        destructive_count = sum(1 for action in actions if action.destructive)
        change_count = sum(1 for action in actions if action.kind != LibraryProvisioningActionKind.SKIP)
        return PreviewProjectProvisioningResultSuccess(
            actions=actions,
            engine_version_failure=engine_version_failure,
            result_details=f"Computed provisioning plan for project '{request.project_id}': "
            f"{change_count} change(s), {destructive_count} destructive",
        )

    async def _reconcile_libraries_from_config(self) -> list[str]:
        """Enforce the engine_version gate and provision libraries_to_download from config.

        The project (its adjacent config, already merged into the live config by
        the time this runs) is the source of truth for the libraries it needs:
        each `libraries_to_download` entry is provisioned to match its version,
        which may overwrite a wrong installed copy. A library listed only in
        `libraries_to_register` is left to normal discovery and is never
        overwritten. The engine_version gate runs first, before any disk
        mutation, so a version mismatch blocks provisioning entirely rather than
        half-applying it.

        Returns a list of failure detail strings (empty on success). Callers
        decide whether to log-and-continue (boot) or fail (interactive reload).
        """
        engine_version_failure = self._check_engine_version()
        if engine_version_failure is not None:
            return [engine_version_failure]

        config_mgr = GriptapeNodes.ConfigManager()
        raw_libraries = config_mgr.get_config_value(LIBRARIES_TO_DOWNLOAD_KEY, default=[])
        downloads = normalize_library_downloads(raw_libraries)

        failures: list[str] = []
        for download in downloads:
            failure = await self._provision_one_library(download)
            if failure is not None:
                failures.append(failure)

        return failures

    def _check_engine_version(self) -> str | None:
        """Return a failure detail when the running engine fails the configured spec.

        Reads the merged `requires_engine` config key and delegates the PEP 440
        compare to `engine_version_failure_detail`. No key means no constraint.
        """
        spec_string = GriptapeNodes.ConfigManager().get_config_value(REQUIRES_ENGINE_KEY, default=None)
        return engine_version_failure_detail(spec_string)

    async def _plan_one_library_provisioning(
        self, download: LibraryDownload, libraries_path: Path | None = None
    ) -> LibraryProvisioningAction:
        """Decide what provisioning will do to one download entry, reading only.

        A manifest read for the installed version (under `libraries_directory`)
        plus a PEP 440 compare. The preview lists this output and the real
        provisioning path re-runs it at execution time; both read the same
        on-disk state, which survives the reload's registry unload, so the two
        cannot drift. Branch order mirrors `_provision_one_library`: the
        already-satisfied SKIP first, then the git clone/overwrite.

        `libraries_path` is the TARGET project's resolved libraries directory.
        The preview passes it so the installed-version probe reads the workspace
        the switch would land in, not the currently-active one; activation runs
        after the config layers switch, so its reconcile leaves this None and
        resolves from the (now-target) live config.

        `destructive` is True ONLY for a git OVERWRITE, matching the
        `overwrite_existing = installed_version is not None` decision in
        `_provision_git_library` that triggers the local directory delete.

        A LibraryDownload always carries a `git_url`; `name` is optional. When
        `name` is absent the installed copy is located by its repo-name directory
        (`_installed_manifest_path_for_download`), the same place the download
        handler lands it, so a `version` pin is enforced without requiring `name`.
        The action's `library_name` falls back to the repo name for display.
        """
        library_name = download.name if download.name is not None else extract_repo_name_from_url(download.git_url)
        installed_version = await self._installed_download_version(download, libraries_path)
        parsed = parse_git_url_with_ref(download.git_url)
        satisfied = self._registration_satisfied_by_installed(download, installed_version)
        if satisfied:
            return LibraryProvisioningAction(
                library_name=library_name,
                kind=LibraryProvisioningActionKind.SKIP,
                installed_version=installed_version,
                pinned_version=download.version,
                git_url=parsed.url,
                git_ref=parsed.ref,
                destructive=False,
                reason=f"Installed version {installed_version} already satisfies the entry",
            )

        if installed_version is None:
            kind = LibraryProvisioningActionKind.INSTALL
            reason = f"Not installed; will clone from {download.git_url}"
        else:
            kind = LibraryProvisioningActionKind.OVERWRITE
            reason = (
                f"Installed version {installed_version} does not satisfy the entry; "
                f"will delete the local library directory and re-clone from {download.git_url}"
            )
        return LibraryProvisioningAction(
            library_name=library_name,
            kind=kind,
            installed_version=installed_version,
            pinned_version=download.version,
            git_url=parsed.url,
            git_ref=parsed.ref,
            destructive=kind == LibraryProvisioningActionKind.OVERWRITE,
            reason=reason,
        )

    async def _provision_one_library(self, download: LibraryDownload) -> str | None:
        """Provision a single git-sourced library, skipping when already satisfied.

        Computes the plan with the same pure decision function the preview uses
        (`_plan_one_library_provisioning`), then clones/overwrites via the
        download handler, which recomputes its own `overwrite_existing` so the
        wire payload is unchanged. Returns a failure detail string, or None on
        success/skip. A LibraryDownload always carries a `git_url`.
        """
        action = await self._plan_one_library_provisioning(download)
        if action.kind == LibraryProvisioningActionKind.SKIP:
            return None

        return await self._provision_git_library(
            download, git_url=download.git_url, installed_version=action.installed_version
        )

    async def _provision_git_library(
        self, download: LibraryDownload, *, git_url: str, installed_version: str | None
    ) -> str | None:
        """Download a git-sourced entry, overwriting only a wrong installed version.

        The download handler skips its clone when the target directory exists,
        which would silently keep a stale checkout. So overwrite_existing is set
        only when a wrong version is already installed; a fresh install lets the
        handler land the library normally.

        When overwriting, the destructive delete must target the directory the
        installed library actually lives in. The handler's default guess is
        `libraries_path/<git-repo-name>`, which is correct for any library this
        engine downloaded. An explicit `name` overrides that for a library
        installed under a differently-named directory: the installed manifest is
        resolved by `name` and its parent directory is passed so the delete hits
        the stale dir and the re-clone does not orphan the old copy. Without a
        `name` (the common case) both hints stay None and the handler's repo-name
        default applies. A fresh install (installed_version is None) also leaves
        both hints None.
        """
        parsed = parse_git_url_with_ref(git_url)
        overwrite_existing = installed_version is not None

        download_directory: str | None = None
        target_directory_name: str | None = None
        if overwrite_existing and download.name is not None:
            manifest_path = await self._installed_library_manifest_path(download.name)
            if manifest_path is not None:
                download_directory = str(manifest_path.parent.parent)
                target_directory_name = manifest_path.parent.name

        download_request = DownloadLibraryRequest(
            git_url=parsed.url,
            branch_tag_commit=parsed.ref,
            auto_register=False,
            overwrite_existing=overwrite_existing,
            fail_on_exists=False,
            download_directory=download_directory,
            target_directory_name=target_directory_name,
        )
        download_result = await GriptapeNodes.ahandle_request(download_request)
        if not isinstance(download_result, DownloadLibraryResultSuccess):
            library_label = download.name if download.name is not None else extract_repo_name_from_url(git_url)
            return f"Failed to provision library '{library_label}' from '{git_url}': {download_result.result_details}"
        return None

    @staticmethod
    async def _installed_library_manifest_path(library_name: str, libraries_path: Path | None = None) -> Path | None:
        """Return the on-disk manifest path for a provisioned library by name, or None.

        Scans the manifests under `libraries_directory` (where reconcile clones
        git-sourced libraries) rather than the in-memory `LibraryRegistry`,
        because the reload path
        unregisters every library before reconcile runs (see
        `reload_libraries_request`). Returns the first manifest whose `name`
        matches; None when the directory is unconfigured or missing, or no
        manifest matches.

        `libraries_path` lets the provisioning preview probe the TARGET project's
        libraries directory rather than the live one. When None it resolves from
        the live config, which is correct for the real reconcile (it runs after
        activation has switched the config layers to the target).

        This is the single source of truth for the provisioning planner
        (`_installed_library_version`, which decides SKIP/INSTALL/OVERWRITE) and
        the overwrite path (`_provision_git_library`, which deletes the manifest's
        directory before re-cloning), guaranteeing the file the planner reasoned
        about is exactly the file overwrite targets.
        """
        if libraries_path is None:
            config_mgr = GriptapeNodes.ConfigManager()
            libraries_dir_setting = config_mgr.get_config_value("libraries_directory")
            if not libraries_dir_setting:
                return None
            libraries_path = resolve_workspace_path(Path(libraries_dir_setting), config_mgr.workspace_path)

        for manifest_path in await find_files_recursive(libraries_path, LibraryManager.LIBRARY_CONFIG_GLOB_PATTERN):
            try:
                content = manifest_path.read_text(encoding="utf-8")
                manifest = json.loads(content)
            except (OSError, json.JSONDecodeError):
                continue
            if manifest.get("name") == library_name:
                return manifest_path

        return None

    @staticmethod
    async def _installed_library_version(library_name: str) -> str | None:
        """Return the on-disk version of a library by manifest name, or None when absent.

        Locates the provisioned manifest via `_installed_library_manifest_path`
        (the shared resolver), then reads `metadata.library_version`. None when no
        manifest matches or the version is absent/unreadable.
        """
        manifest_path = await LibraryManager._installed_library_manifest_path(library_name)
        return LibraryManager._library_version_from_manifest(manifest_path)

    @staticmethod
    async def _installed_manifest_path_for_download(
        download: LibraryDownload, libraries_path: Path | None = None
    ) -> Path | None:
        """Return the on-disk manifest path for a download entry, or None when absent.

        Locates the installed copy the same way the download handler lands it:
        by the repo-name directory `libraries_directory/<repo-name>/`. This keeps
        the provisioning version-check consistent with the clone/skip/overwrite
        logic, so a `version` pin works without requiring `name`. An explicit
        `name` overrides the directory match for a library installed under a
        differently-named directory, resolving by manifest name instead. None when
        the directory is unconfigured/missing or no manifest is found.

        `libraries_path` lets the provisioning preview probe the TARGET project's
        libraries directory; when None it resolves from the live config (correct
        for the real reconcile, which runs post-activation).
        """
        if download.name is not None:
            return await LibraryManager._installed_library_manifest_path(download.name, libraries_path)

        if libraries_path is None:
            config_mgr = GriptapeNodes.ConfigManager()
            libraries_dir_setting = config_mgr.get_config_value("libraries_directory")
            if not libraries_dir_setting:
                return None
            libraries_path = resolve_workspace_path(Path(libraries_dir_setting), config_mgr.workspace_path)

        repo_name = extract_repo_name_from_url(download.git_url)
        repo_directory = libraries_path / repo_name
        return find_file_in_directory(repo_directory, LibraryManager.LIBRARY_CONFIG_GLOB_PATTERN)

    @staticmethod
    async def _installed_download_version(download: LibraryDownload, libraries_path: Path | None = None) -> str | None:
        """Return the on-disk version for a download entry, or None when absent.

        Resolves the installed manifest via `_installed_manifest_path_for_download`
        (repo-name directory, or `name` override), then reads
        `metadata.library_version`. `libraries_path` threads the TARGET project's
        libraries directory through for the preview; None resolves from live config.
        """
        manifest_path = await LibraryManager._installed_manifest_path_for_download(download, libraries_path)
        return LibraryManager._library_version_from_manifest(manifest_path)

    @staticmethod
    def _library_version_from_manifest(manifest_path: Path | None) -> str | None:
        """Read `metadata.library_version` from a manifest path, or None when unreadable/absent."""
        if manifest_path is None:
            return None

        try:
            content = manifest_path.read_text(encoding="utf-8")
            manifest = json.loads(content)
        except (OSError, json.JSONDecodeError):
            return None

        metadata = manifest.get("metadata")
        if not isinstance(metadata, dict):
            return None
        version = metadata.get("library_version")
        return str(version) if version is not None else None

    @staticmethod
    def _registration_satisfied_by_installed(download: LibraryDownload, installed_version: str | None) -> bool:
        """Decide whether the installed library already satisfies the entry.

        Nothing installed is never satisfied. An entry without a version spec is
        satisfied by any installed version (source-only entry). Otherwise the
        installed version must fall within the PEP 440 specifier; a malformed
        spec or version is treated as unsatisfied so provisioning re-runs.
        """
        if installed_version is None:
            return False
        if download.version is None:
            return True
        try:
            specifier_set = SpecifierSet(download.version)
            return PackagingVersion(installed_version) in specifier_set
        except (InvalidSpecifier, InvalidVersion):
            return False

    async def _ensure_libraries_from_config(self) -> None:
        """Ensure libraries from git URLs specified in config are downloaded.

        This method:
        1. Reads libraries_to_download from config
        2. Downloads any missing libraries concurrently
        3. Logs summary of successful/failed operations

        Supports URL format with @ref suffix (e.g., "https://github.com/user/repo@stable").
        Libraries are registered later by load_all_libraries_from_config().
        """
        config_mgr = GriptapeNodes.ConfigManager()
        raw_downloads = config_mgr.get_config_value(LIBRARIES_TO_DOWNLOAD_KEY, default=[])
        git_urls = [download.git_url for download in normalize_library_downloads(raw_downloads)]

        if not git_urls:
            logger.debug("No libraries to download from config")
            return

        logger.debug("Starting download of %d libraries from config", len(git_urls))

        # Use shared download method
        results = await self._download_libraries_from_git_urls(git_urls)

        # Count successes, skipped, and failures
        successful = sum(1 for r in results.values() if r["success"])
        skipped = sum(1 for r in results.values() if r.get("skipped"))
        failed = len(results) - successful - skipped

        if failed:
            logger.info(
                "Completed automatic library downloads: %d successful, %d skipped, %d failed",
                successful,
                skipped,
                failed,
            )
        else:
            logger.debug(
                "Completed automatic library downloads: %d successful, %d skipped",
                successful,
                skipped,
            )

    async def _download_libraries_from_git_urls(
        self,
        git_urls_with_refs: list[str],
    ) -> dict[str, dict[str, Any]]:
        """Download multiple libraries from git URLs concurrently.

        Args:
            git_urls_with_refs: List of git URLs with optional @ref suffix (e.g., "url@v1.0")

        Returns:
            Dictionary mapping git_url_with_ref to result info:
            {
                "url@ref": {
                    "success": bool,
                    "library_name": str | None,
                    "error": str | None,
                    "skipped": bool (optional, True if already exists),
                }
            }
        """
        config_mgr = GriptapeNodes.ConfigManager()
        libraries_dir_setting = config_mgr.get_config_value("libraries_directory")

        if not libraries_dir_setting:
            logger.warning("Cannot download libraries: libraries_directory not configured")
            return {}

        libraries_path = resolve_workspace_path(Path(libraries_dir_setting), config_mgr.workspace_path)

        async def download_one(git_url_with_ref: str) -> tuple[str, dict[str, Any]]:
            """Download a single library if not already present."""
            # Parse URL to extract git URL and optional ref
            git_url, ref = parse_git_url_with_ref(git_url_with_ref)
            target_directory_name = extract_repo_name_from_url(git_url)
            target_path = libraries_path / target_directory_name

            # Skip if already exists
            if target_path.exists():
                logger.debug("Library at '%s' already exists, skipping", target_path)
                return git_url_with_ref, {
                    "success": False,
                    "library_name": None,
                    "error": None,
                    "skipped": True,
                }

            logger.info("Downloading library from '%s'", git_url_with_ref)
            download_result = await GriptapeNodes.ahandle_request(
                DownloadLibraryRequest(
                    git_url=git_url,
                    branch_tag_commit=ref,
                    fail_on_exists=False,
                    auto_register=False,
                )
            )

            if isinstance(download_result, DownloadLibraryResultSuccess):
                logger.info("Downloaded library '%s'", download_result.library_name)
                return git_url_with_ref, {
                    "success": True,
                    "library_name": download_result.library_name,
                    "error": None,
                }

            error = str(download_result.result_details)
            logger.warning("Failed to download '%s': %s", git_url_with_ref, error)
            return git_url_with_ref, {
                "success": False,
                "library_name": None,
                "error": error,
            }

        # Download all concurrently
        async with asyncio.TaskGroup() as tg:
            tasks = [tg.create_task(download_one(url)) for url in git_urls_with_refs]

        # Collect results
        return dict(task.result() for task in tasks)

    async def on_app_initialization_complete(self, payload: AppInitializationComplete) -> None:
        # Bracket the whole init sequence so the heartbeat can report is_initializing to clients
        # that connect mid-startup. finally guarantees the flag clears even if init raises.
        self._is_initializing = True
        try:
            await self._run_app_initialization(payload)
        finally:
            self._is_initializing = False

    async def _run_app_initialization(self, payload: AppInitializationComplete) -> None:
        if payload.skip_library_loading:
            # Register all secrets even in headless mode
            GriptapeNodes.SecretsManager().register_all_secrets()

            # Still need to tell WorkflowManager to register workflows
            # Pass the specific workflows if provided, otherwise it will scan workspace
            await GriptapeNodes.WorkflowManager().refresh_workflow_registry(
                workflows_to_register=payload.workflows_to_register
            )
            return

        # Automatically migrate old XDG library paths from config
        # TODO: Remove https://github.com/griptape-ai/griptape-nodes/issues/3348
        self._migrate_old_xdg_library_paths()

        # App just got init'd. First download any missing libraries from git URLs.
        await self._ensure_libraries_from_config()

        # Now load all libraries from config (including newly downloaded ones).
        # When running as a dedicated library worker, restrict loading to those libraries.
        self._is_worker = payload.is_worker
        self._target_library_names = payload.libraries_to_register if payload.is_worker else None
        reconcile_failures = await self.load_all_libraries_from_config(target_library_names=self._target_library_names)
        # Soft boot: log reconcile failures and continue so the engine still starts and the
        # user can switch to a working project. Interactive activation hard-fails instead
        # (see reload_libraries_request).
        if reconcile_failures:
            logger.warning(
                "Library reconcile reported %d problem(s) at startup; continuing so the engine can start:\n%s",
                len(reconcile_failures),
                "\n".join(reconcile_failures),
            )

        # When the orchestrator restarts into an already-active session, the GUI will not
        # send AppStartSessionRequest again, so workers must be started here.
        await self._maybe_start_workers_for_existing_session()

        # Wait for workers only when restarting into an existing session (workers were just
        # spawned above). On a fresh boot there is no session yet -- workers start later
        # when AppStartSessionRequest arrives and will finish before the user can open a
        # workflow. Awaiting here on fresh boot would wait the full worker-startup grace
        # period for workers that haven't started yet.
        if GriptapeNodes.get_session_id():
            await self._await_pending_workers()

        # Print library status after workers have had a chance to report back so worker
        # libraries show their real fitness rather than NOT_EVALUATED.
        if not self._is_worker:
            self.print_library_load_status()

        # Register all secrets now that libraries are loaded and settings are merged
        GriptapeNodes.SecretsManager().register_all_secrets()

        # We have to load all libraries before we attempt to load workflows.

        # This will (attempts to) load all workflows specified by LIBRARIES. User workflows are loaded later.
        library_workflow_files_to_register = await self._collect_library_workflow_files()
        await GriptapeNodes.WorkflowManager().register_list_of_workflows(library_workflow_files_to_register)

        # Go tell the Workflow Manager that it's turn is now.
        await GriptapeNodes.WorkflowManager().refresh_workflow_registry()

        # Only print the engine ready banner for the orchestrator — not for dedicated library workers.
        self._maybe_print_engine_ready_banner(is_worker=self._is_worker)

    async def _collect_library_workflow_files(self) -> list[str]:
        """Collect workflow file paths declared by all registered libraries.

        Returns absolute paths to workflow files, adding each library's base directory
        to sys.path so relative imports work when the workflow is loaded.
        """
        workflow_files: list[str] = []
        library_result = await GriptapeNodes.ahandle_request(ListRegisteredLibrariesRequest(broadcast_result=False))
        if not isinstance(library_result, ListRegisteredLibrariesResultSuccess):
            return workflow_files
        for library_name in library_result.libraries:
            try:
                library = LibraryRegistry.get_library(name=library_name)
            except KeyError:
                logger.error("Could not find library '%s'", library_name)
                continue
            library_data = library.get_library_data()
            if not library_data.workflows:
                continue
            # Workflows are stored relative to the library JSON; find the library's path.
            for library_info in self._library_file_path_to_info.values():
                if library_info.library_name == library_name:
                    library_path = Path(library_info.library_path)
                    base_dir = library_path.parent.absolute()
                    # Add the directory to the Python path to allow for relative imports.
                    sys.path.insert(0, str(base_dir))
                    workflow_files.extend(str(base_dir / workflow) for workflow in library_data.workflows)
                    break
        return workflow_files

    async def _on_session_started(self, _event: AppSessionStartedEvent) -> None:
        """Spawn workers for all libraries that require one now that a session is active.

        Two cases:
        1. Fresh start: libraries finished loading before a session existed, so their
           worker spawns were blocked on _session_ready_event. WorkerManager.set_session_ready()
           unblocks them, but _start_workers() catches any that slipped through.
        2. Session restart: workers were terminated by AppEndSession and need to be
           re-spawned now that a new session is available.
        """
        if self._is_worker:
            return
        await self._start_workers()

    async def _maybe_start_workers_for_existing_session(self) -> None:
        """Start workers if the orchestrator restarted into an already-active session.

        In a normal fresh start the GUI sends AppStartSessionRequest which triggers worker
        spawning via _on_session_started. When the engine restarts mid-session the GUI does
        not send that request, so this method handles the case at the end of library
        initialization.
        """
        if self._is_worker or not GriptapeNodes.get_session_id():
            return
        worker_manager = GriptapeNodes.WorkerManager()
        if worker_manager is not None:
            worker_manager.set_session_ready()
            await self._start_workers()

    async def _await_pending_workers(self, wait_seconds: float | None = None) -> None:
        """Wait for all WORKER_PENDING libraries to report back via LibraryLoadedNotification.

        On timeout, marks remaining pending libraries as FAILURE/UNUSABLE so the rest of
        initialization can continue. Per-library worker_ready events are set by
        _on_library_loaded_notification when the worker sends its LibraryLoadedNotification.

        When wait_seconds is None, reads the worker heartbeat startup grace from config so
        the orchestrator ceiling stays aligned with the worker self-timeout; first-time
        installs of large libraries can easily exceed the default heartbeat timeout.
        """
        pending_events = [
            info.worker_ready
            for info in self._library_file_path_to_info.values()
            if info.worker_ready is not None and not info.worker_ready.is_set()
        ]
        if not pending_events:
            return

        if wait_seconds is None:
            config_mgr = GriptapeNodes.ConfigManager()
            wait_seconds = config_mgr.get_config_value(
                WORKER_HEARTBEAT_STARTUP_GRACE_KEY, default=600.0, cast_type=float
            )

        timed_out = False
        try:
            with anyio.fail_after(wait_seconds):
                await asyncio.gather(*[e.wait() for e in pending_events])
        except TimeoutError:
            timed_out = True

        if timed_out:
            for info in self._library_file_path_to_info.values():
                if (
                    info.worker_ready is not None
                    and not info.worker_ready.is_set()
                    and info.lifecycle_state == LibraryManager.LibraryLifecycleState.WORKER_PENDING
                ):
                    info.lifecycle_state = LibraryManager.LibraryLifecycleState.FAILURE
                    info.fitness = LibraryManager.LibraryFitness.UNUSABLE
                    info.worker_ready.set()
                    logger.warning(
                        "Worker for library '%s' timed out after %s seconds; marked as FAILURE.",
                        info.library_name,
                        wait_seconds,
                    )

    def _register_nodes_from_worker_schemas(self, library_name: str, node_schemas: list[WorkerNodeSchema]) -> None:
        """Register stub node classes on the orchestrator from worker-reported schemas.

        Creates a minimal dynamic class for each node type so that LibraryRegistry can
        instantiate nodes (for workflow loading, sidebar display, etc.) without importing
        the worker library's Python modules.
        """
        try:
            library = LibraryRegistry.get_library(library_name)
        except KeyError:
            logger.warning("Cannot register worker node schemas: library '%s' not found in registry.", library_name)
            return

        # Build a lookup from class_name -> NodeMetadata using the library JSON schema.
        library_data = library.get_library_data()
        metadata_by_class: dict[str, NodeMetadata] = {
            node_def.class_name: node_def.metadata for node_def in library_data.nodes
        }

        for node_schema in node_schemas:
            metadata = metadata_by_class.get(node_schema.class_name)
            if metadata is None:
                logger.warning(
                    "Worker reported node '%s' for library '%s' but it has no metadata entry; skipping.",
                    node_schema.class_name,
                    library_name,
                )
                continue

            stub_class = self._make_worker_stub_class(node_schema.class_name, node_schema.parameters)
            library_problem = library.register_new_node_type(stub_class, metadata=metadata)
            if library_problem is not None:
                logger.warning(
                    "Problem registering worker stub for node '%s': %s",
                    node_schema.class_name,
                    library_problem,
                )

        # Register widgets declared by the library.
        if library_data.widgets:
            for widget_def in library_data.widgets:
                widget_problem = LibraryRegistry.register_widget_from_library(library_name, widget_def.name)
                if widget_problem is not None:
                    logger.warning(
                        "Problem registering widget '%s' from library '%s': %s",
                        widget_def.name,
                        library_name,
                        widget_problem,
                    )

    @staticmethod
    def _make_worker_stub_class(class_name: str, param_schemas: list[WorkerParameterSchema]) -> type[BaseNode]:
        """Create a dynamic BaseNode subclass from worker-reported parameter schemas.

        The stub registers the correct parameters so the GUI can display them and
        workflow loading can restore saved values. Execution always runs on the worker,
        so process() is a no-op.
        """

        def stub_init(self: BaseNode, name: str, metadata: dict | None = None) -> None:  # type: ignore[override]
            BaseNode.__init__(self, name=name, metadata=metadata)
            for schema in param_schemas:
                allowed_modes = set()
                if schema.mode_allowed_input:
                    allowed_modes.add(ParameterMode.INPUT)
                if schema.mode_allowed_property:
                    allowed_modes.add(ParameterMode.PROPERTY)
                if schema.mode_allowed_output:
                    allowed_modes.add(ParameterMode.OUTPUT)
                self.add_parameter(
                    Parameter(
                        name=schema.name,
                        default_value=schema.default_value,
                        type=schema.type or None,
                        input_types=schema.input_types or None,
                        output_type=schema.output_type or None,
                        tooltip=schema.tooltip,
                        tooltip_as_input=schema.tooltip_as_input,
                        tooltip_as_property=schema.tooltip_as_property,
                        tooltip_as_output=schema.tooltip_as_output,
                        allowed_modes=allowed_modes,
                        settable=schema.settable,
                        serializable=schema.serializable,
                        user_defined=schema.user_defined,
                        private=schema.private,
                        ui_options=schema.ui_options,
                    )
                )

        def stub_process(_: BaseNode) -> None:
            pass

        return type(class_name, (BaseNode,), {"__init__": stub_init, "process": stub_process})

    def _maybe_print_engine_ready_banner(self, *, is_worker: bool) -> None:
        if is_worker:
            return

        engine_version = get_complete_version_string()

        # Get current session ID
        session_id = GriptapeNodes.get_session_id()
        session_info = f" | Session: {session_id[:8]}..." if session_id else " | No Session"

        # Get user and organization
        user = GriptapeNodes.UserManager().user
        user_info = f" | User: {user.email if user else 'Not available'}"

        user_organization = GriptapeNodes.UserManager().user_organization
        org_info = f" | Org: {user_organization.name if user_organization else 'Not available'}"

        nodes_app_url = os.getenv("GRIPTAPE_NODES_UI_BASE_URL", "https://nodes.griptape.ai")
        message = Panel(
            Align.center(
                f"[bold green]Engine is ready to receive events[/bold green]\n"
                f"[bold blue]Return to: [link={nodes_app_url}]{nodes_app_url}[/link] to access the Workflow Editor[/bold blue]",
                vertical="middle",
            ),
            title="Griptape Nodes Engine Started",
            subtitle=f"[green]Version: {engine_version}{session_info}{user_info}{org_info}[/green]",
            border_style="green",
            padding=(1, 4),
        )
        console.print(message)

    def _load_advanced_library_module(
        self,
        library_data: LibrarySchema,
        base_dir: Path,
    ) -> AdvancedNodeLibrary | None:
        """Load the advanced library module and return an instance.

        Args:
            library_data: The library schema data
            base_dir: Base directory containing the library files

        Returns:
            An instance of the AdvancedNodeLibrary class from the module, or None if not specified

        Raises:
            ImportError: If the module cannot be loaded
            AttributeError: If no AdvancedNodeLibrary subclass is found
            TypeError: If the found class cannot be instantiated
        """
        from griptape_nodes.node_library.advanced_node_library import AdvancedNodeLibrary

        if not library_data.advanced_library_path:
            return None

        # Resolve relative path to absolute path
        advanced_library_module_path = resolve_workspace_path(Path(library_data.advanced_library_path), base_dir)

        # Load the module (supports hot reloading)
        try:
            module = self._load_module_from_file(advanced_library_module_path, library_data.name)
        except Exception as err:
            msg = f"Failed to load Advanced Library module from '{advanced_library_module_path}': {err}"
            raise ImportError(msg) from err

        # Find an AdvancedNodeLibrary subclass in the module
        advanced_library_class = None
        for obj in vars(module).values():
            if (
                isinstance(obj, type)
                and issubclass(obj, AdvancedNodeLibrary)
                and obj is not AdvancedNodeLibrary
                and obj.__module__ == module.__name__
            ):
                advanced_library_class = obj
                break

        if not advanced_library_class:
            msg = f"No AdvancedNodeLibrary subclass found in Advanced Library module '{advanced_library_module_path}'"
            raise AttributeError(msg)

        # Create an instance
        try:
            advanced_library_instance = advanced_library_class()
        except Exception as err:
            msg = f"Failed to instantiate AdvancedNodeLibrary class '{advanced_library_class.__name__}': {err}"
            raise TypeError(msg) from err

        # Validate the instance
        if not isinstance(advanced_library_instance, AdvancedNodeLibrary):
            msg = f"Created instance is not an AdvancedNodeLibrary subclass: {type(advanced_library_instance)}"
            raise TypeError(msg)

        return advanced_library_instance

    def _attempt_load_nodes_from_library(  # noqa: PLR0912, PLR0915, C901
        self,
        library_data: LibrarySchema,
        library: Library,
        base_dir: Path,
        library_info: LibraryInfo,
    ) -> None:
        """Load nodes from library and update library_info in place.

        Args:
            library_data: Library schema with node definitions
            library: Library instance to register nodes with
            base_dir: Base directory for resolving relative paths
            library_info: LibraryInfo to update with problems and fitness
        """
        any_nodes_loaded_successfully = False

        # Check if library is in old XDG location
        old_xdg_libraries_path = xdg_data_home() / "griptape_nodes" / "libraries"
        library_path_obj = Path(library_info.library_path)
        try:
            # Check if the library path is relative to the old XDG location
            if library_path_obj.is_relative_to(old_xdg_libraries_path):
                library_info.problems.append(OldXdgLocationWarningProblem(old_path=str(library_path_obj)))
                logger.warning(
                    "Library '%s' is located in old XDG data directory: %s. "
                    "Starting with version 0.65.0, libraries are managed in your workspace directory. "
                    "To migrate: run 'gtn init' (CLI) or go to App Settings and click 'Re-run Setup Wizard' (desktop app).",
                    library_data.name,
                    library_info.library_path,
                )
        except ValueError:
            # is_relative_to() raises ValueError if paths are on different drives
            # In this case, library is definitely not in the old XDG location
            pass

        # Call the before_library_nodes_loaded callback if available
        advanced_library = library.get_advanced_library()
        if advanced_library:
            try:
                advanced_library.before_library_nodes_loaded(library_data, library)
                details = f"Successfully called before_library_nodes_loaded callback for library '{library_data.name}'"
                logger.debug(details)
            except Exception as err:
                library_info.problems.append(BeforeLibraryCallbackProblem(error_message=str(err)))
                details = (
                    f"Failed to call before_library_nodes_loaded callback for library '{library_data.name}': {err}"
                )
                logger.error(details)

        # Process each node in the metadata
        for node_definition in library_data.nodes:
            # Resolve relative path to absolute path
            node_file_path = resolve_workspace_path(Path(node_definition.file_path), base_dir)

            try:
                # Dynamically load the module containing the node class
                node_class = self._load_class_from_file(node_file_path, node_definition.class_name, library_data.name)
            except ImportError as err:
                root_cause = self._get_root_cause_from_exception(err)
                library_info.problems.append(
                    NodeModuleImportProblem(
                        class_name=node_definition.class_name,
                        file_path=str(node_file_path),
                        error_message=str(err),
                        root_cause=str(root_cause),
                    )
                )
                details = f"Attempted to load node '{node_definition.class_name}' from '{node_file_path}'. Failed because module could not be imported: {err}"
                logger.error(details)
                continue  # SKIP IT
            except AttributeError:
                library_info.problems.append(
                    NodeClassNotFoundProblem(class_name=node_definition.class_name, file_path=str(node_file_path))
                )
                details = f"Attempted to load node '{node_definition.class_name}' from '{node_file_path}'. Failed because class not found in module"
                logger.error(details)
                continue  # SKIP IT
            except TypeError:
                library_info.problems.append(
                    NodeClassNotBaseNodeProblem(class_name=node_definition.class_name, file_path=str(node_file_path))
                )
                details = f"Attempted to load node '{node_definition.class_name}' from '{node_file_path}'. Failed because class doesn't inherit from BaseNode"
                logger.error(details)
                continue  # SKIP IT

            # Register the node type with the library
            library_problem = library.register_new_node_type(node_class, metadata=node_definition.metadata)
            if library_problem is not None:
                library_info.problems.append(library_problem)

            # If we got here, at least one node came in.
            any_nodes_loaded_successfully = True

        # Register widgets and check for duplicates
        if library_data.widgets:
            for widget_def in library_data.widgets:
                widget_problem = LibraryRegistry.register_widget_from_library(
                    library_name=library_data.name, widget_name=widget_def.name
                )
                if widget_problem is not None:
                    library_info.problems.append(widget_problem)

        # Call the after_library_nodes_loaded callback if available
        if advanced_library:
            try:
                advanced_library.after_library_nodes_loaded(library_data, library)
                details = f"Successfully called after_library_nodes_loaded callback for library '{library_data.name}'"
                logger.debug(details)
            except Exception as err:
                library_info.problems.append(AfterLibraryCallbackProblem(error_message=str(err)))
                details = f"Failed to call after_library_nodes_loaded callback for library '{library_data.name}': {err}"
                logger.error(details)

        # Update library_info fitness based on load successes and problem count
        if not any_nodes_loaded_successfully:
            library_info.fitness = LibraryManager.LibraryFitness.UNUSABLE
        elif library_info.problems:
            # Success, but errors.
            library_info.fitness = LibraryManager.LibraryFitness.FLAWED
        else:
            # Flawless victory.
            library_info.fitness = LibraryManager.LibraryFitness.GOOD

        # Update lifecycle state to LOADED
        library_info.lifecycle_state = LibraryManager.LibraryLifecycleState.LOADED

    # Per-node timeout for the schema probe. Node __init__ methods that make
    # synchronous handle_request calls can deadlock against async handlers that
    # await init-time events (e.g. WorkflowManager._workflows_loading_complete),
    # so each probe runs in a worker thread with this ceiling.
    _SCHEMA_PROBE_TIMEOUT_S: float = 10.0
    # Sentinel name passed to the throwaway node instance built for schema
    # discovery. The instance is discarded after its parameters are read.
    _SCHEMA_PROBE_NODE_NAME: str = "__schema_probe__"

    def _report_parameter_behavior_losses(self, probe: BaseNode) -> None:
        """Report parameter-behaviors-dropped-in-schema for any probe parameter that has live behaviors.

        ``WorkerParameterSchema`` only carries the scalar-shaped fields of a
        ``Parameter``. Converters, validators, and traits cannot be
        serialized across the worker boundary and therefore will not run on
        the orchestrator stub. If a parameter has any of these attached,
        report it so the author sees a named warning during library load.
        """
        rule = RULES["parameter-behaviors-dropped-in-schema"]
        for param in probe.parameters:
            dropped: list[str] = []
            if param.has_directly_attached_converters:
                dropped.append("converters")
            if param.has_directly_attached_validators:
                dropped.append("validators")
            if param.has_traits:
                dropped.append("traits")
            if not dropped:
                continue
            STRICT_MODE.report(
                rule_id=rule.rule_id,
                message=rule.render(parameter_name=param.name, dropped_attributes=", ".join(dropped)),
            )

    async def _serialize_library_node_schemas(self, library_name: str) -> list[WorkerNodeSchema]:
        """Serialize node parameter schemas for a loaded library.

        Called on the worker process after library nodes are loaded. Probes each
        registered node type to extract its parameter layout, so the orchestrator
        can create stub classes without importing the library's Python modules.
        """
        try:
            library = LibraryRegistry.get_library(library_name)
        except KeyError:
            logger.warning("Cannot serialize schemas: library '%s' not found in registry.", library_name)
            return []

        node_schemas: list[WorkerNodeSchema] = []
        for class_name in library.get_registered_nodes():
            # The is-constructing flag set inside create_node propagates into
            # the asyncio.to_thread worker via contextvars.copy_context().
            probe = None
            with STRICT_MODE.open_scope(
                kind=StrictModeScopeKind.LOAD_PROBE,
                subject=class_name,
                library_name=library_name,
                is_worker=self.is_worker,
            ) as scope:
                try:
                    probe = await asyncio.wait_for(
                        asyncio.to_thread(
                            LibraryRegistry.create_node,
                            node_type=class_name,
                            name=self._SCHEMA_PROBE_NODE_NAME,
                            specific_library_name=library_name,
                        ),
                        timeout=self._SCHEMA_PROBE_TIMEOUT_S,
                    )
                except TimeoutError:
                    logger.warning(
                        "Schema probe for node class '%s' in library '%s' timed out after %.1fs; "
                        "skipping. The node's __init__ likely makes a blocking call that cannot "
                        "complete during library load.",
                        class_name,
                        library_name,
                        self._SCHEMA_PROBE_TIMEOUT_S,
                    )
                    continue
                except Exception:
                    logger.debug("Could not probe node class '%s' for schema serialization.", class_name, exc_info=True)
                    continue
                # Run the parameter-behavior-drop detector inside the scope so
                # warnings attach to the same LOAD_PROBE scope that owns the
                # probe attempt.
                self._report_parameter_behavior_losses(probe)

            # Drop the class only when a correctness-class rule fired.
            # Severity is a logging concern; correctness is the lifecycle
            # signal ("this class is broken enough to exclude from the
            # schema"). Gating on severity would also drop the class for
            # any future ergonomics rule whose worker_escalation flag is
            # left at the True default.
            blocking_rule_ids = {rid for rid, r in RULES.items() if r.correctness}
            if any(v.rule_id in blocking_rule_ids for v in scope.violations):
                continue

            param_schemas: list[WorkerParameterSchema] = []
            for param in probe.parameters:
                allowed_modes = param.allowed_modes
                param_schemas.append(
                    WorkerParameterSchema(
                        name=param.name,
                        type=param._type or "",
                        input_types=list(param._input_types or []),
                        output_type=param._output_type or "",
                        default_value=self._try_json_serialize(param.default_value),
                        tooltip=param.tooltip,
                        tooltip_as_input=param.tooltip_as_input,
                        tooltip_as_property=param.tooltip_as_property,
                        tooltip_as_output=param.tooltip_as_output,
                        mode_allowed_input=ParameterMode.INPUT in allowed_modes,
                        mode_allowed_property=ParameterMode.PROPERTY in allowed_modes,
                        mode_allowed_output=ParameterMode.OUTPUT in allowed_modes,
                        user_defined=param.user_defined,
                        settable=param.settable,
                        serializable=param.serializable,
                        private=param.private,
                        ui_options=self._try_json_serialize(param.ui_options) if param.ui_options else None,
                    )
                )
            node_schemas.append(WorkerNodeSchema(class_name=class_name, parameters=param_schemas))

        return node_schemas

    @staticmethod
    def _try_json_serialize(value: Any) -> Any:
        """Return value if it is JSON-serializable, otherwise return None."""
        try:
            json.dumps(value)
        except (TypeError, ValueError):
            return None
        else:
            return value

    async def _attempt_generate_sandbox_library_from_schema(  # noqa: C901
        self,
        library_schema: LibrarySchema,
        sandbox_directory: str,
        library_info: LibraryInfo,
    ) -> None:
        """Generate sandbox library using an existing schema, loading actual node classes."""
        sandbox_library_dir = Path(sandbox_directory)

        problems = []

        # Get the file paths from the schema's node definitions to load actual classes
        actual_node_definitions = []
        for node_def in library_schema.nodes:
            # Resolve relative path from schema against sandbox directory
            candidate_path = sandbox_library_dir / node_def.file_path
            try:
                module = self._load_module_from_file(candidate_path, LibraryManager.SANDBOX_LIBRARY_NAME)
            except Exception as err:
                root_cause = self._get_root_cause_from_exception(err)
                problems.append(
                    NodeModuleImportProblem(
                        class_name=f"<Sandbox node in '{node_def.file_path}'>",
                        file_path=str(candidate_path),
                        error_message=str(err),
                        root_cause=str(root_cause),
                    )
                )
                details = f"Attempted to load module in sandbox library '{candidate_path}'. Failed because an exception occurred: {err}."
                logger.warning(details)
                continue  # SKIP IT

            # Peek inside for any BaseNodes.
            for class_name, obj in vars(module).items():
                if (
                    isinstance(obj, type)
                    and issubclass(obj, BaseNode)
                    and type(obj) is not BaseNode
                    and obj.__module__ == module.__name__
                ):
                    details = f"Found node '{class_name}' in sandbox library '{candidate_path}'."
                    logger.debug(details)

                    # Look for existing node definition to preserve user-edited metadata
                    existing_node = None
                    for existing_node_def in library_schema.nodes:
                        if (
                            existing_node_def.file_path == str(candidate_path)
                            and existing_node_def.class_name == class_name
                        ):
                            existing_node = existing_node_def
                            break

                    if existing_node:
                        # PRESERVE existing metadata - user may have customized it
                        node_metadata = existing_node.metadata
                        logger.debug("Preserving existing metadata for node '%s'", class_name)
                    else:
                        # NEW node - create default metadata
                        node_metadata = NodeMetadata(
                            category=self.SANDBOX_CATEGORY_NAME,
                            description=f"'{class_name}' (loaded from the {LibraryManager.SANDBOX_LIBRARY_NAME}).",
                            display_name=class_name,
                        )
                        logger.debug("Creating new metadata for node '%s'", class_name)

                    node_definition = NodeDefinition(
                        class_name=class_name,
                        file_path=node_def.file_path,  # Keep original relative path from schema
                        metadata=node_metadata,
                    )
                    actual_node_definitions.append(node_definition)

        if not actual_node_definitions:
            # The sandbox directory exists but currently holds no files that declare a
            # BaseNode subclass. Previously the loader bailed here and left the Sandbox
            # Library unregistered, which made it impossible to add the first node via
            # `RegisterSandboxNodeFromSourceRequest` (and similar incremental tools) without
            # first seeding a throwaway file by hand. We now fall through and register the
            # library with zero nodes so it is a valid target for subsequent registrations.
            logger.debug(
                "No nodes found in sandbox library '%s'. Registering empty library so it can be populated incrementally.",
                sandbox_library_dir,
            )

        # Use the existing schema but replace nodes with actual discovered ones
        library_data = LibrarySchema(
            name=library_schema.name,
            library_schema_version=library_schema.library_schema_version,
            metadata=library_schema.metadata,
            categories=library_schema.categories,
            nodes=actual_node_definitions,
            widgets=library_schema.widgets,
        )

        # Save the schema with real class names back to disk
        json_path = sandbox_library_dir / LibraryManager.LIBRARY_CONFIG_FILENAME
        write_succeeded = self._write_library_schema_to_json(library_data, json_path)
        if write_succeeded:
            logger.debug(
                "Saved sandbox library schema with %d discovered nodes to '%s'",
                len(actual_node_definitions),
                json_path,
            )

        # Register the library.
        # Create or get the library
        try:
            # Try to create a new library
            library = LibraryRegistry.generate_new_library(
                library_data=library_data,
                mark_as_default_library=True,
            )

        except KeyError as err:
            # Library already exists - update existing library_info
            library_info.lifecycle_state = LibraryManager.LibraryLifecycleState.FAILURE
            library_info.fitness = LibraryManager.LibraryFitness.UNUSABLE
            library_info.problems.append(DuplicateLibraryProblem())

            details = f"Attempted to load Library JSON file from '{sandbox_library_dir}'. Failed because a Library '{library_data.name}' already exists. Error: {err}."
            logger.error(details)
            return

        # Add any problems encountered during node discovery to library_info
        library_info.problems.extend(problems)

        # Load nodes into the library (modifies library_info in place)
        # Note: library_info is passed as parameter from lifecycle handler
        await asyncio.to_thread(
            self._attempt_load_nodes_from_library,
            library_data=library_data,
            library=library,
            base_dir=sandbox_library_dir,
            library_info=library_info,
        )

    def _find_files_in_dir(self, directory: Path, extension: str) -> list[Path]:
        """Find all files with given extension in directory, excluding common non-source directories."""
        ret_val = []
        for root, dirs, files_found in os.walk(directory):
            # Modify dirs in-place to skip excluded directories
            # Also skip any directory starting with '.'
            dirs[:] = [d for d in dirs if d not in EXCLUDED_SCAN_DIRECTORIES and not d.startswith(".")]

            for file in files_found:
                if file.endswith(extension):
                    file_path = Path(root) / file
                    ret_val.append(file_path)
        return ret_val

    def _write_library_schema_to_json(self, library_schema: LibrarySchema, json_path: Path) -> bool:
        """Write library schema to JSON file using WriteFileRequest.

        Args:
            library_schema: The library schema to write
            json_path: Path where the JSON file should be written

        Returns:
            True if write succeeded, False otherwise
        """
        write_request = WriteFileRequest(
            file_path=str(json_path),
            content=library_schema.model_dump_json(indent=2),
            encoding="utf-8",
        )
        write_result = GriptapeNodes.handle_request(write_request)

        if write_result.failed():
            logger.error("Failed to write library schema to '%s': %s", json_path, write_result.result_details)
            return False

        return True

    def _resolve_requires_worker(
        self,
        registered_path: str | None,
        declarations: list[LibraryDeclaration],
    ) -> bool:
        """Apply per-entry worker_mode_override iff the library is worker-compatible; otherwise honor the manifest.

        The override lives on the matching `LibraryRegistration` entry in
        `libraries_to_register` (keyed by the user's verbatim path, mirroring how `enabled`
        works). Lookup uses the user's *registered* path -- the same string they typed in
        config -- not the resolved absolute path, because the engine's path resolution can
        diverge between sides (workspace-relative, `~`-expansion, symlink-following).

        Returns the load-time `requires_worker` bool used by the lifecycle state machine.
        Centralized here so both writer sites stay in sync.
        """
        manifest_default = requires_worker_process(declarations)

        capability = next((d for d in declarations if isinstance(d, WorkerModeCompatibility)), None)
        is_incompatible = capability is not None and capability.compatibility is WorkerCompatibility.INCOMPATIBLE
        if is_incompatible or not registered_path:
            return manifest_default

        config_mgr = GriptapeNodes.ConfigManager()
        raw_entries = config_mgr.get_config_value(LIBRARIES_TO_REGISTER_KEY) or []
        target_path_lower = registered_path.lower()
        for entry in raw_entries:
            entry_path = extract_library_path(entry)
            if not entry_path or entry_path.lower() != target_path_lower:
                continue
            override_raw: Any = None
            if isinstance(entry, LibraryRegistration):
                override_raw = entry.worker_mode_override
            elif isinstance(entry, dict):
                override_raw = entry.get("worker_mode_override")
            if override_raw is None:
                return manifest_default
            try:
                effective_mode = WorkerMode(override_raw)
            except ValueError:
                logger.debug(
                    "Ignoring invalid worker_mode_override %r on libraries_to_register entry %r; falling back to manifest suggested mode.",
                    override_raw,
                    registered_path,
                )
                return manifest_default
            return effective_mode is WorkerMode.WORKER

        return manifest_default

    def _remove_missing_libraries_from_config(self, config_category: str) -> None:
        # Now remove all libraries that were missing from the user's config.
        config_mgr = GriptapeNodes.ConfigManager()
        libraries_to_register_category = config_mgr.get_config_value(config_category)

        paths_to_remove = set()
        for library_path, library_info in self._library_file_path_to_info.items():
            if library_info.fitness == LibraryManager.LibraryFitness.MISSING:
                # Remove this file path from the config.
                paths_to_remove.add(library_path.lower())

        if paths_to_remove and libraries_to_register_category:
            libraries_to_register_category = [
                library
                for library in libraries_to_register_category
                if extract_library_path(library).lower() not in paths_to_remove
            ]
            config_mgr.set_config_value(config_category, libraries_to_register_category)

    def _migrate_old_xdg_library_paths(self) -> None:
        """Automatically removes old XDG library paths and adds git URLs to download list.

        This method removes library paths that were stored in the old XDG data home location
        (~/.local/share/griptape_nodes/libraries/) from the libraries_to_register configuration,
        and automatically adds the corresponding git URLs to libraries_to_download to ensure
        the libraries are re-downloaded. This migration happens automatically on app startup,
        so users don't need to run gtn init.
        """
        config_mgr = GriptapeNodes.ConfigManager()

        # Get both config lists
        register_key = LIBRARIES_TO_REGISTER_KEY
        download_key = LIBRARIES_TO_DOWNLOAD_KEY

        libraries_to_register = config_mgr.get_config_value(register_key)
        libraries_to_download = config_mgr.get_config_value(download_key) or []

        if not libraries_to_register:
            return

        # filter_old_xdg_library_paths operates on bare path strings; extract paths from
        # any object-shaped entries, run the filter, then rebuild the list preserving each
        # surviving entry's original shape (so disabled entries keep their `enabled: false`).
        path_to_entry: dict[str, Any] = {}
        for entry in libraries_to_register:
            entry_path = extract_library_path(entry)
            if entry_path:
                path_to_entry[entry_path] = entry

        filtered_paths, removed_library_names = filter_old_xdg_library_paths(list(path_to_entry))

        # If any paths were removed
        paths_removed = len(path_to_entry) - len(filtered_paths)
        if paths_removed > 0:
            filtered_libraries = [path_to_entry[p] for p in filtered_paths]
            # Update libraries_to_register
            config_mgr.set_config_value(register_key, filtered_libraries)

            # Add corresponding git URLs to libraries_to_download
            updated_downloads = self._add_git_urls_for_removed_libraries(
                libraries_to_download,
                removed_library_names,
            )

            urls_added = len(updated_downloads) - len(libraries_to_download)
            if urls_added > 0:
                config_mgr.set_config_value(download_key, updated_downloads)

            logger.info(
                "Automatically migrated library configuration: removed %d old XDG path(s), added %d git URL(s) to download",
                paths_removed,
                urls_added,
            )

    def _add_git_urls_for_removed_libraries(
        self,
        current_downloads: list[Any],
        removed_library_names: set[str],
    ) -> list[Any]:
        """Add git URLs for removed libraries if not already present.

        Args:
            current_downloads: Current libraries_to_download entries (bare git URL strings or object form)
            removed_library_names: Set of library names that were removed (e.g., "griptape_nodes_library")

        Returns:
            Updated list with new git URLs added (deduplicated), preserving existing entry shapes
        """
        if not removed_library_names:
            return current_downloads

        # Get current repository names for deduplication. Entries may be bare strings or
        # object form, so normalize to git URLs before deriving repo names.
        current_repo_names = {
            extract_repo_name_from_url(download.git_url) for download in normalize_library_downloads(current_downloads)
        }

        new_downloads = current_downloads.copy()

        for lib_name in removed_library_names:
            if lib_name not in LIBRARY_GIT_URLS:
                continue

            git_url = LIBRARY_GIT_URLS[lib_name]
            repo_name = extract_repo_name_from_url(git_url)

            # Only add if not already present
            if repo_name not in current_repo_names:
                new_downloads.append(git_url)
                current_repo_names.add(repo_name)

        return new_downloads

    async def reload_libraries_request(self, request: ReloadAllLibrariesRequest) -> ResultPayload:
        # Bracket the reload like on_app_initialization_complete so the heartbeat reports
        # is_initializing during a mid-session reload too. finally clears it even on failure.
        self._is_initializing = True
        try:
            return await self._run_reload_libraries(request)
        finally:
            self._is_initializing = False

    async def _run_reload_libraries(self, request: ReloadAllLibrariesRequest) -> ResultPayload:  # noqa: ARG002
        # Start with a clean slate.
        clear_all_request = ClearAllObjectStateRequest(i_know_what_im_doing=True)
        clear_all_result = await GriptapeNodes.ahandle_request(clear_all_request)
        if not clear_all_result.succeeded():
            details = "Failed to clear the existing object state when preparing to reload all libraries."
            return ReloadAllLibrariesResultFailure(result_details=details)

        # Unload all libraries now.
        all_libraries_request = ListRegisteredLibrariesRequest(broadcast_result=False)
        all_libraries_result = await GriptapeNodes.ahandle_request(all_libraries_request)
        if not isinstance(all_libraries_result, ListRegisteredLibrariesResultSuccess):
            details = "When preparing to reload all libraries, failed to get registered libraries."
            logger.error(details)
            return ReloadAllLibrariesResultFailure(result_details=details)

        for library_name in all_libraries_result.libraries:
            unload_library_request = UnloadLibraryFromRegistryRequest(library_name=library_name)
            unload_library_result = GriptapeNodes.handle_request(unload_library_request)
            if not unload_library_result.succeeded():
                details = f"When preparing to reload all libraries, failed to unload library '{library_name}'."
                logger.error(details)
                return ReloadAllLibrariesResultFailure(result_details=details)

        # Notify pre-reload callbacks (e.g. to terminate worker processes) before
        # load_all_libraries_from_config runs so that workers can be cleanly restarted.
        for callback in self._pre_reload_callbacks:
            try:
                await callback()
            except Exception as e:
                logger.warning("Pre-reload callback raised an exception: %s", e)

        # Load (or reload, which should trigger a hot reload) all libraries.
        # Pass _target_library_names so workers reload only their designated libraries.
        reconcile_failures = await self.load_all_libraries_from_config(target_library_names=self._target_library_names)

        # Re-spawn workers for libraries that require them; reset_workers terminated them above.
        await self._maybe_start_workers_for_existing_session()

        # Wait for worker-delegated libraries to finish loading before returning. The GUI
        # opens the current workflow immediately on receiving the result, so all node types
        # must be registered before we respond.
        await self._await_pending_workers()

        # Print after workers have reported back so their real fitness is shown.
        self.print_library_load_status()

        # Hard activation: a reload is interactive (project switch / explicit reload), so a
        # reconcile failure (bad engine_version gate or a sourced library that could not be
        # provisioned) surfaces to the caller. ProjectManager turns this into a
        # SetCurrentProjectResultFailure the GUI can show.
        if reconcile_failures:
            details = "Reloaded libraries but reconcile reported problem(s): " + "; ".join(reconcile_failures)
            return ReloadAllLibrariesResultFailure(result_details=details)

        details = (
            "Successfully reloaded all libraries. All object state was cleared and previous libraries were unloaded."
        )
        return ReloadAllLibrariesResultSuccess(result_details=ResultDetails(message=details, level=logging.INFO))

    def _create_library_info_entry(
        self,
        file_path_str: str,
        *,
        is_sandbox: bool,
        enabled: bool = True,
        registered_path: str | None = None,
    ) -> None:
        """Create a LibraryInfo entry for a discovered library.

        Loads metadata if possible and creates the entry in the appropriate lifecycle state.
        When `enabled` is False, the entry is created in the DISABLED terminal state and
        is skipped by load_all_libraries_from_config.

        `registered_path` is the user's verbatim `LibraryRegistration.path` from
        `libraries_to_register` before workspace resolution; the GUI uses it to map
        library metadata back to the user's config row. None for sandbox libraries
        (registered through workspace discovery, not via `libraries_to_register`).

        If an entry already exists for this path, it is preserved unless the requested
        `enabled` flag disagrees with the existing lifecycle state (DISABLED vs. anything
        else). In that case the stale entry is dropped so a fresh one can be created,
        which is what lets a refresh pick up libraries the user has just toggled in
        libraries_to_register.
        """
        existing = self._library_file_path_to_info.get(file_path_str)
        if existing is not None:
            existing_is_disabled = existing.lifecycle_state == LibraryManager.LibraryLifecycleState.DISABLED
            requested_is_disabled = not enabled
            if existing_is_disabled == requested_is_disabled:
                # Already in the right state; keep the existing entry as-is.
                return
            # The user toggled enabled in libraries_to_register; drop the stale entry so
            # the block below recreates it with the new lifecycle.
            del self._library_file_path_to_info[file_path_str]

        metadata_result = self.load_library_metadata_from_file_request(
            LoadLibraryMetadataFromFileRequest(file_path=file_path_str)
        )

        library_name = None
        library_version = None
        requires_worker = False
        lifecycle_state = LibraryManager.LibraryLifecycleState.DISCOVERED

        if isinstance(metadata_result, LoadLibraryMetadataFromFileResultSuccess):
            library_name = metadata_result.library_schema.name
            library_version = metadata_result.library_schema.metadata.library_version
            requires_worker = self._resolve_requires_worker(
                registered_path,
                metadata_result.library_schema.metadata.declarations,
            )
            lifecycle_state = LibraryManager.LibraryLifecycleState.METADATA_LOADED

        if not enabled:
            lifecycle_state = LibraryManager.LibraryLifecycleState.DISABLED

        self._library_file_path_to_info[file_path_str] = LibraryManager.LibraryInfo(
            lifecycle_state=lifecycle_state,
            fitness=LibraryManager.LibraryFitness.NOT_EVALUATED,
            library_path=file_path_str,
            is_sandbox=is_sandbox,
            enabled=enabled,
            library_name=library_name,
            library_version=library_version,
            registered_path=registered_path,
            requires_worker=requires_worker,
        )

    async def discover_libraries_request(
        self,
        request: DiscoverLibrariesRequest,
    ) -> DiscoverLibrariesResultSuccess | DiscoverLibrariesResultFailure:
        """Discover libraries from config and track them in discovered state.

        Scans configured library paths and creates LibraryInfo entries in DISCOVERED state.
        """
        try:
            config_library_entries = await self._discover_library_files()
        except Exception as e:
            logger.exception("Failed to discover library files")
            return DiscoverLibrariesResultFailure(
                result_details=f"Failed to discover library files: {e}",
            )

        discovered_libraries = []
        seen_libraries = set()

        # Process sandbox library first if requested
        if request.include_sandbox:
            sandbox_library_dir = self._get_sandbox_directory()
            if sandbox_library_dir:
                # Generate/update the sandbox library JSON file
                metadata_result = self.scan_sandbox_directory_request(
                    ScanSandboxDirectoryRequest(directory_path=str(sandbox_library_dir))
                )

                # If generation succeeded, write JSON and add the sandbox library
                if isinstance(metadata_result, ScanSandboxDirectoryResultSuccess):
                    sandbox_json_path = sandbox_library_dir / LibraryManager.LIBRARY_CONFIG_FILENAME
                    sandbox_json_path_str = str(sandbox_json_path)

                    # Write the schema to JSON so it exists for lifecycle phases
                    write_succeeded = self._write_library_schema_to_json(
                        metadata_result.library_schema, sandbox_json_path
                    )
                    if write_succeeded:
                        logger.debug(
                            "Wrote sandbox library schema with %d nodes to '%s' during discovery",
                            len(metadata_result.library_schema.nodes),
                            sandbox_json_path,
                        )
                    # Continue anyway if write failed - lifecycle will fail gracefully

                    # Add to discovered libraries with is_sandbox=True
                    if sandbox_json_path not in seen_libraries:
                        seen_libraries.add(sandbox_json_path)
                        discovered_libraries.append(DiscoveredLibrary(path=sandbox_json_path, is_sandbox=True))

                    # Create LibraryInfo entry for the sandbox library
                    self._create_library_info_entry(sandbox_json_path_str, is_sandbox=True)

        # Add all regular libraries from config
        for discovered in config_library_entries:
            entry = discovered.registration
            file_path = Path(entry.path)
            file_path_str = entry.path

            # Add to discovered libraries with is_sandbox=False
            if file_path not in seen_libraries:
                seen_libraries.add(file_path)
                discovered_libraries.append(DiscoveredLibrary(path=file_path, is_sandbox=False, enabled=entry.enabled))

            # Create LibraryInfo entry for the library
            self._create_library_info_entry(
                file_path_str,
                is_sandbox=False,
                enabled=entry.enabled,
                registered_path=discovered.registered_path,
            )

        # Success path at the end
        return DiscoverLibrariesResultSuccess(
            result_details=f"Discovered {len(discovered_libraries)} libraries",
            libraries_discovered=discovered_libraries,
        )

    def evaluate_library_fitness_request(
        self, request: EvaluateLibraryFitnessRequest
    ) -> EvaluateLibraryFitnessResultSuccess | EvaluateLibraryFitnessResultFailure:
        """Evaluate library fitness using version compatibility checks.

        Extracts version checking logic from _attempt_load_nodes_from_library.
        Checks engine version compatibility without loading Python modules.
        """
        schema = request.schema
        problems: list[LibraryProblem] = []

        # Check for version-based compatibility issues
        version_issues = GriptapeNodes.VersionCompatibilityManager().check_library_version_compatibility(schema)
        has_disqualifying_issues = False

        for issue in version_issues:
            problems.append(issue.problem)
            if issue.severity == LibraryManager.LibraryFitness.UNUSABLE:
                has_disqualifying_issues = True

        if has_disqualifying_issues:
            return EvaluateLibraryFitnessResultFailure(
                result_details=f"Library '{schema.name}' has version compatibility issues",
                fitness=LibraryManager.LibraryFitness.UNUSABLE,
                problems=problems,
            )

        # Determine fitness based on whether we have any non-disqualifying issues
        fitness = LibraryManager.LibraryFitness.FLAWED if problems else LibraryManager.LibraryFitness.GOOD

        return EvaluateLibraryFitnessResultSuccess(
            result_details=f"Library '{schema.name}' is compatible",
            fitness=fitness,
            problems=problems,
        )

    async def load_libraries_request(self, request: LoadLibrariesRequest) -> ResultPayload:  # noqa: ARG002, C901, PLR0912
        """Load all libraries from configuration (backward compatibility wrapper).

        This is the legacy entry point that loads all configured libraries.
        New code should use LoadLibraryRequest to load specific libraries instead.
        """
        # First, discover all available libraries
        discover_result = await self.discover_libraries_request(DiscoverLibrariesRequest())
        if isinstance(discover_result, DiscoverLibrariesResultFailure):
            return LoadLibrariesResultFailure(result_details=f"Discovery failed: {discover_result.result_details}")

        # Build list of library paths to load, preserving is_sandbox flag
        libraries_to_load = []
        for discovered_lib in discover_result.libraries_discovered:
            lib_path = str(discovered_lib.path)
            lib_info = self._library_file_path_to_info.get(lib_path)

            # Update is_sandbox if library_info exists and discovery says it's sandbox
            if lib_info and discovered_lib.is_sandbox:
                lib_info.is_sandbox = True

            if lib_info and lib_info.lifecycle_state != LibraryManager.LibraryLifecycleState.DISABLED:
                libraries_to_load.append(lib_path)

        if not libraries_to_load:
            details = "No libraries found in configuration."
            return LoadLibrariesResultSuccess(result_details=ResultDetails(message=details, level=logging.INFO))

        # Load each discovered library by path
        loaded_count = 0
        failed_libraries = []
        total_libraries = len(libraries_to_load)

        for current_library_index, lib_path in enumerate(libraries_to_load, start=1):
            # Emit the LOADING event BEFORE registering: register_library_from_file_request
            # installs the library's dependencies (a slow pip/uv step), so emitting after it
            # would leave the GUI with no progress signal during the longest part of startup.
            # The library name isn't resolved until metadata loads, so fall back to a name
            # derived from the path; the COMPLETE/FAILED event below reports the resolved name.
            pre_register_info = self._library_file_path_to_info.get(lib_path)
            pending_library_name = (
                pre_register_info.library_name
                if pre_register_info and pre_register_info.library_name
                else Path(lib_path).stem
            )
            GriptapeNodes.EventManager().put_event(
                AppEvent(
                    payload=EngineInitializationProgress(
                        phase=InitializationPhase.LIBRARIES,
                        item_name=pending_library_name,
                        status=InitializationStatus.LOADING,
                        current=current_library_index,
                        total=total_libraries,
                        is_worker=self._is_worker,
                    )
                )
            )

            load_result = await self.register_library_from_file_request(
                RegisterLibraryFromFileRequest(
                    file_path=lib_path,
                    load_as_default_library=False,
                )
            )

            # Get library_name from result for progress events (use path as fallback for failures)
            if isinstance(load_result, RegisterLibraryFromFileResultSuccess):
                library_name = load_result.library_name
            else:
                library_name = lib_path

            # Check if library was already loaded (skip the COMPLETE event so reloads stay quiet).
            if isinstance(load_result, RegisterLibraryFromFileResultSuccess) and load_result.was_already_loaded:
                # Library was already loaded - already counted, nothing more to emit.
                loaded_count += 1
                continue

            if isinstance(load_result, RegisterLibraryFromFileResultSuccess):
                loaded_count += 1

                # Emit success event
                GriptapeNodes.EventManager().put_event(
                    AppEvent(
                        payload=EngineInitializationProgress(
                            phase=InitializationPhase.LIBRARIES,
                            item_name=library_name,
                            status=InitializationStatus.COMPLETE,
                            current=current_library_index,
                            total=total_libraries,
                            is_worker=self._is_worker,
                        )
                    )
                )
            else:
                failed_libraries.append(library_name)
                logger.warning("Failed to load library '%s': %s", library_name, load_result.result_details)

                # Emit failure event
                error_message = (
                    load_result.result_details.result_details[0].message
                    if isinstance(load_result.result_details, ResultDetails)
                    else str(load_result.result_details)
                )
                GriptapeNodes.EventManager().put_event(
                    AppEvent(
                        payload=EngineInitializationProgress(
                            phase=InitializationPhase.LIBRARIES,
                            item_name=library_name,
                            status=InitializationStatus.FAILED,
                            current=current_library_index,
                            total=total_libraries,
                            error=error_message,
                            is_worker=self._is_worker,
                        )
                    )
                )

        if loaded_count == 0 and len(failed_libraries) > 0:
            return LoadLibrariesResultFailure(
                result_details=f"Failed to load any libraries. Failed: {', '.join(failed_libraries)}"
            )

        message = f"Loaded {loaded_count} libraries"
        if failed_libraries:
            message += f". Failed: {', '.join(failed_libraries)}"

        return LoadLibrariesResultSuccess(result_details=ResultDetails(message=message, level=logging.INFO))

    async def _discover_library_files(self) -> list[LibraryManager.DiscoveredLibraryEntry]:
        """Discover library JSON files from config and workspace recursively.

        Returns:
            List of DiscoveredLibraryEntry pairing each discovered LibraryRegistration
            with the user's original `LibraryRegistration.path` string from config (before
            workspace resolution). Directory entries expand to one entry per discovered
            library file; every child inherits the parent's `registered_path`.
        """
        config_mgr = GriptapeNodes.ConfigManager()
        user_libraries_section = LIBRARIES_TO_REGISTER_KEY

        discovered_entries: list[LibraryManager.DiscoveredLibraryEntry] = []
        seen_paths: set[Path] = set()

        async def process_path(path: Path, *, enabled: bool, registered_path: str) -> None:
            """Process a path, handling both files and directories."""
            if await anyio.Path(path).is_dir():
                # Recursively find library files. find_files_recursive skips hidden
                # directories and bounds recursion depth so a deep or symlink-looped
                # tree can't stall the boot scan.
                for lib_path in await find_files_recursive(path, LibraryManager.LIBRARY_CONFIG_GLOB_PATTERN):
                    if lib_path not in seen_paths:
                        seen_paths.add(lib_path)
                        discovered_entries.append(
                            LibraryManager.DiscoveredLibraryEntry(
                                registration=LibraryRegistration(path=str(lib_path), enabled=enabled),
                                registered_path=registered_path,
                            )
                        )
            elif path.suffix == ".json" and path not in seen_paths:
                seen_paths.add(path)
                discovered_entries.append(
                    LibraryManager.DiscoveredLibraryEntry(
                        registration=LibraryRegistration(path=str(path), enabled=enabled),
                        registered_path=registered_path,
                    )
                )

        # Add from config
        config_libraries = config_mgr.get_config_value(user_libraries_section, default=[])
        for entry in normalize_library_registrations(config_libraries):
            resolved = self._resolve_discovery_path(entry, config_mgr.workspace_path)
            if resolved is not None:
                await process_path(resolved.path, enabled=entry.enabled, registered_path=resolved.registered_path)

        # Add provisioned git-sourced libraries. Each libraries_to_download entry is
        # cloned into the workspace libraries_directory by reconcile; discovery
        # resolves it to its installed manifest there so it loads scoped to the
        # workspace that declares it, without ever being written into the global
        # libraries_to_register config (which would leak it into every project).
        download_libraries = config_mgr.get_config_value(LIBRARIES_TO_DOWNLOAD_KEY, default=[])
        for download in normalize_library_downloads(download_libraries):
            manifest_path = await self._installed_manifest_path_for_download(download)
            if manifest_path is not None:
                await process_path(manifest_path, enabled=True, registered_path=str(manifest_path))

        return discovered_entries

    @staticmethod
    def _resolve_discovery_path(entry: LibraryRegistration, workspace_path: Path) -> ResolvedDiscoveryPath | None:
        """Resolve a `libraries_to_register` entry to a concrete on-disk path to scan.

        A register entry names an already-present local library by `path`, which
        resolves against the workspace. Libraries pinned to a git source live in
        `libraries_to_download` and are resolved separately in
        `_discover_library_files` by locating their provisioned manifest under the
        workspace libraries directory, so they never need a `libraries_to_register`
        entry. Returns None when the path does not exist on disk.
        """
        # TODO: Update to check on project manager for workspace path. https://github.com/griptape-ai/griptape-nodes/issues/4396
        library_path = resolve_workspace_path(Path(entry.path), workspace_path)
        if not library_path.exists():
            return None
        return LibraryManager.ResolvedDiscoveryPath(path=library_path, registered_path=entry.path)

    async def check_library_update_request(self, request: CheckLibraryUpdateRequest) -> ResultPayload:  # noqa: C901, PLR0911, PLR0912, PLR0915
        """Check if a library has updates available via git."""
        library_name = request.library_name

        # Check if the library exists
        try:
            library = LibraryRegistry.get_library(name=library_name)
        except KeyError:
            details = f"Attempted to check for updates for Library '{library_name}'. Failed because no Library with that name was registered."
            return CheckLibraryUpdateResultFailure(result_details=details)

        # Find the library file path
        library_file_path = None
        for file_path, library_info in self._library_file_path_to_info.items():
            if library_info.library_name == library_name:
                library_file_path = file_path
                break

        if library_file_path is None:
            details = f"Attempted to check for updates for Library '{library_name}'. Failed because no file path could be found for this library."
            return CheckLibraryUpdateResultFailure(result_details=details)

        # Get the library directory (parent of the JSON file)
        library_dir = Path(library_file_path).parent.absolute()

        # Check if library is in a monorepo (multiple libraries in same git repository)
        if await asyncio.to_thread(is_monorepo, library_dir):
            details = (
                f"Library '{library_name}' is in a monorepo with multiple libraries. Updates must be managed manually."
            )
            logger.info(details)
            # Get git info for the response
            git_remote = await asyncio.to_thread(get_git_remote, library_dir)
            git_ref = await asyncio.to_thread(get_current_ref, library_dir)
            current_version = library.get_metadata().library_version
            return CheckLibraryUpdateResultSuccess(
                has_update=False,
                current_version=current_version,
                latest_version=current_version,
                git_remote=git_remote,
                git_ref=git_ref,
                local_commit=None,
                remote_commit=None,
                result_details=details,
            )

        # Check if the library directory is a git repository and get remote URL and ref
        try:
            git_remote = await asyncio.to_thread(get_git_remote, library_dir)
            if git_remote is None:
                details = f"Library '{library_name}' is not a git repository or has no remote configured."
                return CheckLibraryUpdateResultFailure(result_details=details)
        except GitRemoteError as e:
            details = f"Failed to get git remote for Library '{library_name}': {e}"
            return CheckLibraryUpdateResultFailure(result_details=details)

        try:
            git_ref = await asyncio.to_thread(get_current_ref, library_dir)
        except GitRefError as e:
            details = f"Failed to get current git reference for Library '{library_name}': {e}"
            return CheckLibraryUpdateResultFailure(result_details=details)

        # Get current library version
        current_version = library.get_metadata().library_version
        if current_version is None:
            details = f"Library '{library_name}' has no version information."
            return CheckLibraryUpdateResultFailure(result_details=details)

        # Get local commit SHA
        local_commit = await asyncio.to_thread(get_local_commit_sha, library_dir)

        # Clone remote and get latest version and commit SHA (using current ref or HEAD if detached)
        try:
            ref_to_check = git_ref or "HEAD"
            version_info = await asyncio.to_thread(clone_and_get_library_version, git_remote, ref_to_check)
            latest_version = version_info.library_version
            remote_commit = version_info.commit_sha
        except GitCloneError as e:
            details = f"Failed to retrieve latest version from git remote for Library '{library_name}': {e}"
            return CheckLibraryUpdateResultFailure(result_details=details)

        # Determine if update is available using version comparison and commit comparison
        try:
            current_ver = Version.parse(current_version)
            latest_ver = Version.parse(latest_version)

            # Update detection logic:
            # 1. If remote version > local version -> update available (semantic versioning)
            if latest_ver > current_ver:
                has_update = True
                update_reason = "version increased"
            # 2. If remote version < local version -> no update (prevent regression)
            elif latest_ver < current_ver:
                has_update = False
                update_reason = "version decreased (regression blocked)"
            # 3. If versions equal -> check commits
            elif local_commit is not None and remote_commit is not None and local_commit != remote_commit:
                has_update = True
                update_reason = "commits differ (same version)"
            else:
                has_update = False
                update_reason = "versions and commits match"

        except ValueError as e:
            details = f"Failed to parse version strings for Library '{library_name}': {e}"
            return CheckLibraryUpdateResultFailure(result_details=details)

        # Check engine version compatibility
        library_required_engine_version = version_info.engine_version
        is_compatible, current_engine_version = self._check_engine_version_compatibility(
            library_required_engine_version
        )

        if not is_compatible:
            details = (
                f"Cannot update Library '{library_name}'. "
                f"The update requires engine version {library_required_engine_version} "
                f"but the current engine version is {current_engine_version}. "
                f"Please update your engine first."
            )
            return CheckLibraryUpdateResultFailure(result_details=details)

        details = f"Successfully checked for updates for Library '{library_name}'. Current version: {current_version}, Latest version: {latest_version}, Has update: {has_update} ({update_reason})"
        logger.info(details)

        return CheckLibraryUpdateResultSuccess(
            has_update=has_update,
            current_version=current_version,
            latest_version=latest_version,
            git_remote=git_remote,
            git_ref=git_ref,
            local_commit=local_commit,
            remote_commit=remote_commit,
            result_details=details,
        )

    async def _validate_and_prepare_library_for_git_operation(
        self,
        library_name: str,
        failure_result_class: type[ResultPayloadFailure],
        operation_description: str,
    ) -> LibraryGitOperationContext | ResultPayloadFailure:
        """Validate library exists and prepare for git operation.

        Args:
            library_name: Name of the library to validate
            failure_result_class: Class to use for failure results (e.g., UpdateLibraryResultFailure)
            operation_description: Description of operation for error messages (e.g., "update", "switch branch/tag for")

        Returns:
            On success: LibraryGitOperationContext with library info
            On failure: ResultPayloadFailure instance
        """
        # Check if the library exists
        try:
            library = LibraryRegistry.get_library(name=library_name)
        except KeyError:
            details = f"Attempted to {operation_description} Library '{library_name}'. Failed because no Library with that name was registered."
            return failure_result_class(result_details=details)

        # Get current version
        old_version = library.get_metadata().library_version
        if old_version is None:
            details = f"Library '{library_name}' has no version information."
            return failure_result_class(result_details=details)

        # Find the library file path
        library_file_path = None
        for file_path, library_info in self._library_file_path_to_info.items():
            if library_info.library_name == library_name:
                library_file_path = file_path
                break

        if library_file_path is None:
            details = f"Attempted to {operation_description} Library '{library_name}'. Failed because no file path could be found for this library."
            return failure_result_class(result_details=details)

        # Get the library directory (parent of the JSON file)
        library_dir = Path(library_file_path).parent.absolute()

        return LibraryGitOperationContext(
            library=library,
            old_version=old_version,
            library_file_path=library_file_path,
            library_dir=library_dir,
        )

    async def _reload_library_after_git_operation(
        self,
        library_name: str,
        library_file_path: str,
        *,
        failure_result_class: type[ResultPayloadFailure],
    ) -> str | ResultPayloadFailure:
        """Reload library after git operation.

        Args:
            library_name: Name of the library to reload
            library_file_path: Path to the library JSON file
            failure_result_class: Class to use for failure results

        Returns:
            On success: new_version (str, may be "unknown")
            On failure: ResultPayloadFailure instance
        """
        # Unload the library
        unload_result = GriptapeNodes.handle_request(UnloadLibraryFromRegistryRequest(library_name=library_name))
        if not unload_result.succeeded():
            details = f"Failed to unload Library '{library_name}' after git operation."
            return failure_result_class(result_details=details)

        # Search for the library JSON file using flexible pattern to handle filename variations
        # (after git operations, the filename might change between griptape-nodes-library.json and griptape_nodes_library.json)
        library_dir = Path(library_file_path).parent
        actual_library_file = find_file_in_directory(library_dir, "griptape[-_]nodes[-_]library.json")

        if actual_library_file is None:
            details = (
                f"Failed to find library JSON file in {library_dir} after git operation for Library '{library_name}'."
            )
            return failure_result_class(result_details=details)

        # Use the found file path for reloading
        actual_library_file_path = str(actual_library_file)

        # Create LibraryInfo for tracking this library reload
        lib_info = LibraryManager.LibraryInfo(
            lifecycle_state=LibraryManager.LibraryLifecycleState.DISCOVERED,
            library_path=actual_library_file_path,
            is_sandbox=False,
            library_name=library_name,
            fitness=LibraryManager.LibraryFitness.NOT_EVALUATED,
            problems=[],
        )
        # Store lib_info in dict so register handler can find it
        self._library_file_path_to_info[actual_library_file_path] = lib_info

        # Reload the library from file
        reload_result = await GriptapeNodes.ahandle_request(
            RegisterLibraryFromFileRequest(file_path=actual_library_file_path)
        )
        if not isinstance(reload_result, RegisterLibraryFromFileResultSuccess):
            details = f"Failed to reload Library '{library_name}' after git operation."
            return failure_result_class(result_details=details)

        # Get new version after reload
        try:
            updated_library = LibraryRegistry.get_library(name=library_name)
            new_version = updated_library.get_metadata().library_version
            if new_version is None:
                new_version = "unknown"
        except KeyError:
            new_version = "unknown"

        return new_version

    async def update_library_request(self, request: UpdateLibraryRequest) -> ResultPayload:
        """Update a library to the latest version using the appropriate git strategy.

        Automatically detects whether the library uses branch-based or tag-based workflow:
        - Branch-based: Uses git fetch + git reset --hard (forces local to match remote)
        - Tag-based: Uses git fetch --tags --force + git checkout
        """
        library_name = request.library_name

        # Validate library and prepare for git operation
        validation_result = await self._validate_and_prepare_library_for_git_operation(
            library_name=library_name,
            failure_result_class=UpdateLibraryResultFailure,
            operation_description="update",
        )
        if isinstance(validation_result, ResultPayloadFailure):
            return validation_result

        old_version = validation_result.old_version
        library_file_path = validation_result.library_file_path
        library_dir = validation_result.library_dir

        # Check if library is in a monorepo (multiple libraries in same git repository)
        if await asyncio.to_thread(is_monorepo, library_dir):
            details = f"Cannot update Library '{library_name}'. Repository contains multiple libraries and must be updated manually."
            return UpdateLibraryResultFailure(result_details=details)

        # Perform git update (auto-detects branch vs tag workflow)
        try:
            await asyncio.to_thread(
                update_library_git,
                library_dir,
                overwrite_existing=request.overwrite_existing,
            )
        except (GitPullError, GitRepositoryError) as e:
            error_msg = str(e).lower()

            # Check if error is retryable (uncommitted changes)
            retryable = "uncommitted changes" in error_msg or "unstaged changes" in error_msg

            details = f"Failed to update Library '{library_name}': {e}"
            return UpdateLibraryResultFailure(
                result_details=details,
                retryable=retryable,
                existing_path=str(library_dir) if retryable else None,
            )

        # Reload library
        reload_result = await self._reload_library_after_git_operation(
            library_name=library_name,
            library_file_path=library_file_path,
            failure_result_class=UpdateLibraryResultFailure,
        )
        if isinstance(reload_result, ResultPayloadFailure):
            return reload_result

        new_version = reload_result

        details = f"Successfully updated Library '{library_name}' from version {old_version} to {new_version}."
        return UpdateLibraryResultSuccess(
            old_version=old_version,
            new_version=new_version,
            result_details=details,
        )

    async def switch_library_ref_request(self, request: SwitchLibraryRefRequest) -> ResultPayload:
        """Switch a library to a different git branch or tag."""
        library_name = request.library_name
        ref_name = request.ref_name

        # Validate library and prepare for git operation
        validation_result = await self._validate_and_prepare_library_for_git_operation(
            library_name=library_name,
            failure_result_class=SwitchLibraryRefResultFailure,
            operation_description="switch branch/tag for",
        )
        if isinstance(validation_result, ResultPayloadFailure):
            return validation_result

        old_version = validation_result.old_version
        library_file_path = validation_result.library_file_path
        library_dir = validation_result.library_dir

        # Get current ref (branch or tag) before switch
        try:
            old_ref = await asyncio.to_thread(get_current_ref, library_dir)
            if old_ref is None:
                details = f"Library '{library_name}' is not on a branch/tag or is not a git repository."
                return SwitchLibraryRefResultFailure(result_details=details)
        except GitRefError as e:
            details = f"Failed to get current branch/tag for Library '{library_name}': {e}"
            return SwitchLibraryRefResultFailure(result_details=details)

        # Perform git ref switch (branch or tag)
        try:
            await asyncio.to_thread(switch_branch_or_tag, library_dir, ref_name)
        except (GitRefError, GitRepositoryError) as e:
            details = f"Failed to switch to '{ref_name}' for Library '{library_name}': {e}"
            return SwitchLibraryRefResultFailure(result_details=details)

        # Reload library
        reload_result = await self._reload_library_after_git_operation(
            library_name=library_name,
            library_file_path=library_file_path,
            failure_result_class=SwitchLibraryRefResultFailure,
        )
        if isinstance(reload_result, ResultPayloadFailure):
            return reload_result

        new_version = reload_result

        # Get new ref (branch or tag) after switch
        try:
            new_ref = await asyncio.to_thread(get_current_ref, library_dir)
            if new_ref is None:
                new_ref = "unknown"
        except GitRefError:
            new_ref = "unknown"

        details = f"Successfully switched Library '{library_name}' from '{old_ref}' (version {old_version}) to '{new_ref}' (version {new_version})."
        return SwitchLibraryRefResultSuccess(
            old_ref=old_ref,
            new_ref=new_ref,
            old_version=old_version,
            new_version=new_version,
            result_details=details,
        )

    async def download_library_request(self, request: DownloadLibraryRequest) -> ResultPayload:  # noqa: PLR0911, PLR0912, PLR0915, C901
        """Download a library from a git repository."""
        git_url = normalize_github_url(request.git_url)
        branch_tag_commit = request.branch_tag_commit
        target_directory_name = request.target_directory_name
        download_directory = request.download_directory

        # Determine the parent directory for the download
        config_mgr = GriptapeNodes.ConfigManager()

        if download_directory is not None:
            # Use custom download directory if provided
            libraries_path = Path(download_directory)
        else:
            # Use default from config
            libraries_dir_setting = config_mgr.get_config_value("libraries_directory")
            if not libraries_dir_setting:
                details = "Cannot download library: libraries_directory setting is not configured."
                return DownloadLibraryResultFailure(result_details=details)
            libraries_path = resolve_workspace_path(Path(libraries_dir_setting), config_mgr.workspace_path)

        # Ensure parent directory exists
        await anyio.Path(libraries_path).mkdir(parents=True, exist_ok=True)

        # Determine target directory name
        if target_directory_name is None:
            # Extract from git URL (e.g., "https://github.com/user/repo.git" -> "repo")
            target_directory_name = git_url.rstrip("/").split("/")[-1]
            target_directory_name = target_directory_name.removesuffix(".git")

        # Construct full target path
        target_path = libraries_path / target_directory_name

        # Check if target directory already exists
        skip_clone = False
        if await anyio.Path(target_path).exists():
            if request.overwrite_existing:
                # Delete existing directory before cloning
                delete_request = DeleteFileRequest(path=str(target_path), workspace_only=False)
                delete_result = await GriptapeNodes.ahandle_request(delete_request)

                if isinstance(delete_result, DeleteFileResultFailure):
                    details = f"Cannot delete existing directory at {target_path}: {delete_result.result_details}"
                    return DownloadLibraryResultFailure(result_details=details)

                logger.info("Deleted existing directory at %s for overwrite", target_path)
            else:
                # Check fail_on_exists flag
                if request.fail_on_exists:
                    # Fail with retryable error for interactive CLI
                    details = f"Cannot download library: target directory already exists at {target_path}"
                    return DownloadLibraryResultFailure(
                        result_details=details,
                        retryable=True,
                        existing_path=str(target_path),
                    )

                # Skip cloning since directory already exists, but continue with registration
                skip_clone = True
                logger.debug(
                    "Library directory already exists at %s, skipping download but will proceed with registration",
                    target_path,
                )

        # Clone the repository (unless skipping because it already exists)
        if skip_clone:
            logger.debug("Using existing library directory at %s", target_path)
        else:
            try:
                await asyncio.to_thread(clone_repository, git_url, target_path, branch_tag_commit)
            except GitCloneError as e:
                details = f"Failed to clone repository from {git_url} to {target_path}: {e}"
                return DownloadLibraryResultFailure(result_details=details)

        # Recursively search for griptape_nodes_library.json file
        library_json_path = find_file_in_directory(target_path, "griptape[-_]nodes[-_]library.json")
        if library_json_path is None:
            details = f"Downloaded library from {git_url} but no library JSON file found in {target_path}"
            return DownloadLibraryResultFailure(result_details=details)

        try:
            content = await anyio.Path(library_json_path).read_text(encoding="utf-8")
            library_data = json.loads(content)
        except json.JSONDecodeError as e:
            details = f"Failed to parse griptape_nodes_library.json from downloaded library: {e}"
            return DownloadLibraryResultFailure(result_details=details)

        # Extract library name
        library_name = library_data.get("name")
        if library_name is None:
            details = "Downloaded library has no 'name' field in griptape_nodes_library.json"
            return DownloadLibraryResultFailure(result_details=details)

        # Automatically register the downloaded library (unless disabled for startup downloads)
        if request.auto_register:
            # Create LibraryInfo for tracking this downloaded library
            lib_info = LibraryManager.LibraryInfo(
                lifecycle_state=LibraryManager.LibraryLifecycleState.DISCOVERED,
                library_path=str(library_json_path),
                is_sandbox=False,
                library_name=library_name,
                fitness=LibraryManager.LibraryFitness.NOT_EVALUATED,
                problems=[],
            )
            # Store lib_info in dict so register handler can find it
            self._library_file_path_to_info[str(library_json_path)] = lib_info

            register_request = RegisterLibraryFromFileRequest(file_path=str(library_json_path))
            register_result = await GriptapeNodes.ahandle_request(register_request)
            if not register_result.succeeded():
                return DownloadLibraryResultFailure(
                    result_details=f"Library '{library_name}' downloaded but failed to register: {register_result.result_details}"
                )
            logger.info("Library '%s' registered successfully", library_name)

        # Persist the path to libraries_to_register only when registering now. The
        # write lands in the GLOBAL user config (set_config_value -> user config),
        # so a project-reconcile download (auto_register=False) must NOT touch it:
        # the project's own libraries_to_download is the per-activation source of
        # truth, and persisting its clone path here would leak that library into
        # every other project's startup registration. Reconcile-downloaded
        # libraries instead reach discovery directly from libraries_to_download
        # (see _discover_library_files), so they load scoped to the workspace that
        # declares them without any global config write.
        if request.auto_register:
            libraries_to_register = config_mgr.get_config_value(LIBRARIES_TO_REGISTER_KEY, default=[])
            library_json_str = str(library_json_path)
            existing_paths = {extract_library_path(entry) for entry in libraries_to_register}
            if library_json_str not in existing_paths:
                libraries_to_register.append(library_json_str)
                config_mgr.set_config_value(LIBRARIES_TO_REGISTER_KEY, libraries_to_register)
                logger.info("Added library '%s' to config for auto-registration on startup", library_name)

        if skip_clone:
            details = f"Library '{library_name}' already exists at {target_path} and has been registered"
        else:
            details = f"Successfully downloaded library '{library_name}' from {git_url} to {target_path}"
        return DownloadLibraryResultSuccess(
            library_name=library_name,
            library_path=str(library_json_path),
            result_details=details,
        )

    async def install_library_dependencies_request(self, request: InstallLibraryDependenciesRequest) -> ResultPayload:  # noqa: PLR0911
        """Install dependencies for a library."""
        library_file_path = request.library_file_path

        # Load library metadata from file
        metadata_request = LoadLibraryMetadataFromFileRequest(file_path=library_file_path)
        metadata_result = self.load_library_metadata_from_file_request(metadata_request)

        if not isinstance(metadata_result, LoadLibraryMetadataFromFileResultSuccess):
            details = f"Failed to load library metadata from {library_file_path}: {metadata_result.result_details}"
            return InstallLibraryDependenciesResultFailure(result_details=details)

        library_data = metadata_result.library_schema
        library_name = library_data.name
        library_metadata = library_data.metadata

        pip_dependencies = []
        pip_install_flags = []
        if library_metadata.dependencies:
            pip_dependencies = library_metadata.dependencies.pip_dependencies or []
            pip_install_flags = library_metadata.dependencies.pip_install_flags or []

        # Always initialize the venv, even if there are no dependencies to install.
        # Advanced library hooks (before_library_nodes_loaded) expect the venv to exist.
        venv_path = self._get_library_venv_path(library_name, library_file_path)

        try:
            library_venv_python_path = await self._init_library_venv(venv_path)
        except RuntimeError as e:
            details = f"Failed to initialize venv for library '{library_name}': {e}"
            return InstallLibraryDependenciesResultFailure(result_details=details)

        if not self._can_write_to_venv_location(library_venv_python_path):
            details = f"Venv location for library '{library_name}' at {venv_path} is not writable"
            logger.warning(details)
            return InstallLibraryDependenciesResultFailure(result_details=details)

        # Check disk space
        config_manager = GriptapeNodes.ConfigManager()
        min_space_gb = config_manager.get_config_value("minimum_disk_space_gb_libraries")
        if not OSManager.check_available_disk_space(Path(venv_path), min_space_gb):
            error_msg = OSManager.format_disk_space_error(Path(venv_path))
            details = f"Insufficient disk space for dependencies (requires {min_space_gb} GB) for library '{library_name}': {error_msg}"
            return InstallLibraryDependenciesResultFailure(result_details=details)

        if not pip_dependencies:
            details = f"Library '{library_name}' has no dependencies to install"
            logger.info(details)
            return InstallLibraryDependenciesResultSuccess(
                library_name=library_name, dependencies_installed=0, result_details=details
            )

        # Install dependencies
        logger.info("Installing %d dependencies for library '%s'", len(pip_dependencies), library_name)
        is_debug = config_manager.get_config_value("log_level").upper() == "DEBUG"

        try:
            await subprocess_run(
                [
                    sys.executable,
                    "-m",
                    "uv",
                    "pip",
                    "install",
                    *pip_dependencies,
                    *pip_install_flags,
                    "--python",
                    str(library_venv_python_path),
                ],
                check=True,
                capture_output=not is_debug,
                text=True,
            )
        except subprocess.CalledProcessError as e:
            details = f"Failed to install dependencies for library '{library_name}': return code={e.returncode}, stderr={e.stderr}"
            return InstallLibraryDependenciesResultFailure(result_details=details)

        details = f"Installed {len(pip_dependencies)} dependencies for library '{library_name}'"
        logger.info(details)
        return InstallLibraryDependenciesResultSuccess(
            library_name=library_name, dependencies_installed=len(pip_dependencies), result_details=details
        )

    async def sync_libraries_request(self, request: SyncLibrariesRequest) -> ResultPayload:  # noqa: C901, PLR0915
        """Sync all libraries to latest versions and ensure dependencies are installed."""
        # Phase 1: Download missing libraries from both config keys
        config_mgr = GriptapeNodes.ConfigManager()

        # Collect git URLs from both config keys
        download_config = config_mgr.get_config_value(LIBRARIES_TO_DOWNLOAD_KEY, default=[])
        register_config = config_mgr.get_config_value(LIBRARIES_TO_REGISTER_KEY, default=[])
        # libraries_to_download entries carry the git URL (bare string or object form).
        git_urls_from_download = [download.git_url for download in normalize_library_downloads(download_config)]
        # Disabled entries are still synced; disabling only affects loading.
        git_urls_from_register = [path for entry in register_config if is_git_url(path := extract_library_path(entry))]

        # Combine and deduplicate
        all_git_urls = list(set(git_urls_from_download + git_urls_from_register))

        # Use shared download method
        update_summary = {}
        libraries_downloaded = 0

        if all_git_urls:
            logger.info("Found %d git URLs, downloading missing libraries", len(all_git_urls))
            download_results = await self._download_libraries_from_git_urls(all_git_urls)

            # Process results for summary
            for git_url, result in download_results.items():
                if result["success"]:
                    libraries_downloaded += 1
                    update_summary[result["library_name"]] = {
                        "status": "downloaded",
                        "git_url": git_url,
                    }
                elif result.get("error"):
                    logger.warning("Download failed for '%s': %s", git_url, result["error"])

        logger.info("Downloaded %d new libraries", libraries_downloaded)

        # Phase 2: Load libraries to ensure newly downloaded ones are registered
        logger.info("Loading libraries to register newly downloaded ones")
        load_request = LoadLibrariesRequest()
        load_result = await GriptapeNodes.ahandle_request(load_request)

        if not isinstance(load_result, LoadLibrariesResultSuccess):
            logger.warning("Failed to load libraries after download: %s", load_result.result_details)
            # Continue anyway - we can still update previously registered libraries

        # Phase 3: Check and update all registered libraries
        # Get all registered libraries
        list_result = await GriptapeNodes.ahandle_request(ListRegisteredLibrariesRequest(broadcast_result=False))
        if not isinstance(list_result, ListRegisteredLibrariesResultSuccess):
            details = "Failed to list registered libraries for sync"
            return SyncLibrariesResultFailure(result_details=details)

        libraries_to_check = list_result.libraries

        logger.info("Checking %d registered libraries for updates", len(libraries_to_check))

        # Check all libraries for updates concurrently using task group
        async def check_library_for_update(library_name: str) -> tuple[str, ResultPayload]:
            """Check a single library for updates."""
            logger.info("Checking library '%s' for updates", library_name)
            check_result = await GriptapeNodes.ahandle_request(
                CheckLibraryUpdateRequest(library_name=library_name, failure_log_level=logging.DEBUG)
            )
            return library_name, check_result

        # Gather all check results concurrently
        check_results: dict[str, ResultPayload] = {}
        async with asyncio.TaskGroup() as tg:
            tasks = [tg.create_task(check_library_for_update(lib)) for lib in libraries_to_check]

        # Collect results from completed tasks
        for task in tasks:
            library_name, result = task.result()
            check_results[library_name] = result

        # Process check results and determine which libraries need updates
        libraries_checked = len(libraries_to_check)
        libraries_updated = 0
        libraries_to_update: list[LibraryUpdateInfo] = []

        for library_name, check_result in check_results.items():
            if not isinstance(check_result, CheckLibraryUpdateResultSuccess):
                logger.warning(
                    "Failed to check for updates for library '%s', skipping: %s",
                    library_name,
                    str(check_result.result_details),
                )
                continue

            if not check_result.has_update:
                logger.info("Library '%s' is up to date (version %s)", library_name, check_result.current_version)
                continue

            # Library has an update available
            old_version = check_result.current_version or "unknown"
            new_version = check_result.latest_version or "unknown"
            logger.info("Library '%s' has update available: %s -> %s", library_name, old_version, new_version)
            libraries_to_update.append(
                LibraryUpdateInfo(library_name=library_name, old_version=old_version, new_version=new_version)
            )

        # Update libraries concurrently using task group
        async def update_library(library_name: str, old_version: str, new_version: str) -> LibraryUpdateResult:
            """Update a single library."""
            logger.info("Updating library '%s' from %s to %s", library_name, old_version, new_version)
            update_result = await GriptapeNodes.ahandle_request(
                UpdateLibraryRequest(
                    library_name=library_name,
                    overwrite_existing=request.overwrite_existing,
                )
            )
            return LibraryUpdateResult(
                library_name=library_name,
                old_version=old_version,
                new_version=new_version,
                result=update_result,
            )

        # Gather all update results concurrently
        async with asyncio.TaskGroup() as tg:
            update_tasks = [
                tg.create_task(update_library(info.library_name, info.old_version, info.new_version))
                for info in libraries_to_update
            ]

        # Collect update results
        for task in update_tasks:
            result = task.result()
            library_name = result.library_name
            old_version = result.old_version
            new_version = result.new_version
            update_result = result.result

            if not isinstance(update_result, UpdateLibraryResultSuccess):
                logger.error("Failed to update library '%s': %s", library_name, update_result.result_details)
                update_summary[library_name] = {
                    "old_version": old_version,
                    "new_version": old_version,
                    "status": "failed",
                    "error": update_result.result_details,
                }
                continue

            libraries_updated += 1
            update_summary[library_name] = {
                "old_version": update_result.old_version,
                "new_version": update_result.new_version,
                "status": "updated",
            }
            logger.info(
                "Successfully updated library '%s' from %s to %s",
                library_name,
                update_result.old_version,
                update_result.new_version,
            )

        # Build result details
        details = f"Downloaded {libraries_downloaded} libraries. Checked {libraries_checked} libraries. {libraries_updated} updated."
        logger.info(details)
        return SyncLibrariesResultSuccess(
            libraries_downloaded=libraries_downloaded,
            libraries_checked=libraries_checked,
            libraries_updated=libraries_updated,
            update_summary=update_summary,
            result_details=details,
        )

    async def inspect_library_repo_request(self, request: InspectLibraryRepoRequest) -> ResultPayload:
        """Inspect a library's metadata from a git repository without downloading the full repository."""
        git_url = request.git_url
        ref = request.ref

        # Normalize GitHub shorthand to full URL
        normalized_url = normalize_github_url(git_url)
        logger.info("Inspecting library metadata from '%s' (ref: %s)", normalized_url, ref)

        # Perform sparse checkout to get library JSON
        try:
            library_version, commit_sha, library_data_raw = sparse_checkout_library_json(normalized_url, ref)
        except GitCloneError as e:
            details = f"Failed to inspect library from {normalized_url}: {e}"
            logger.error(details)
            return InspectLibraryRepoResultFailure(result_details=details)

        # Validate and create LibrarySchema
        try:
            library_schema = LibrarySchema(**library_data_raw)
        except Exception as e:
            details = f"Invalid library schema from {normalized_url}: {e}"
            logger.error(details)
            return InspectLibraryRepoResultFailure(result_details=details)

        # Return success with full library metadata
        details = f"Successfully inspected library '{library_schema.name}' (version {library_version}) from {normalized_url} at commit {commit_sha[:7]}"
        logger.info(details)
        return InspectLibraryRepoResultSuccess(
            library_schema=library_schema,
            commit_sha=commit_sha,
            git_url=normalized_url,
            ref=ref,
            result_details=details,
        )
