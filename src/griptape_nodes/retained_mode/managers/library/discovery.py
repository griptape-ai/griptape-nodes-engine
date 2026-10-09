from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

import anyio

from griptape_nodes.files.path_utils import canonicalize_for_identity, resolve_workspace_path
from griptape_nodes.node_library.library_declarations import (
    LifecycleStageLibraryProperty,
)
from griptape_nodes.retained_mode.engine import EngineScoped
from griptape_nodes.retained_mode.events.app_events import (
    EngineInitializationProgress,
    InitializationPhase,
    InitializationStatus,
)

# Runtime imports for ResultDetails since it's used at runtime
from griptape_nodes.retained_mode.events.base_events import AppEvent, ResultDetails
from griptape_nodes.retained_mode.events.library_events import (
    DiscoveredLibrary,
    DiscoverLibrariesRequest,
    DiscoverLibrariesResultFailure,
    DiscoverLibrariesResultSuccess,
    EvaluateLibraryFitnessRequest,
    EvaluateLibraryFitnessResultFailure,
    EvaluateLibraryFitnessResultSuccess,
    LoadLibrariesRequest,
    LoadLibrariesResultFailure,
    LoadLibrariesResultSuccess,
    LoadLibraryMetadataFromFileRequest,
    LoadLibraryMetadataFromFileResultSuccess,
    RegisterLibraryFromFileRequest,
    RegisterLibraryFromFileResultSuccess,
    ScanSandboxDirectoryRequest,
    ScanSandboxDirectoryResultSuccess,
)
from griptape_nodes.retained_mode.managers.authorization_checkpoint import (
    AuthorizationCheckpoint,
    CheckpointAction,
    CheckpointAttribute,
    CheckpointSubjectType,
)
from griptape_nodes.retained_mode.managers.external_environment import (
    LIBRARY_PATHS_ENV_VAR,
    library_paths_from_environment,
)
from griptape_nodes.retained_mode.managers.fitness_problems.libraries import (
    LibraryProblem,
    NodePermissionDeniedProblem,
    PermissionDeniedProblem,
)
from griptape_nodes.retained_mode.managers.library.common import (
    LIBRARY_CONFIG_FILENAME,
    LIBRARY_CONFIG_GLOB_PATTERN,
    LibraryFitness,
    LibraryInfo,
    LibraryLifecycleState,
)
from griptape_nodes.retained_mode.managers.library.workers import resolve_executes_in_worker
from griptape_nodes.retained_mode.managers.settings import (
    LIBRARIES_TO_DOWNLOAD_KEY,
    LIBRARIES_TO_REGISTER_KEY,
    LibraryRegistration,
)
from griptape_nodes.retained_mode.request_handlers import handles
from griptape_nodes.utils.file_utils import find_files_recursive
from griptape_nodes.utils.git_utils import (
    extract_repo_name_from_url,
)
from griptape_nodes.utils.library_utils import (
    LIBRARY_GIT_URLS,
    extract_library_path,
    filter_old_xdg_library_paths,
    normalize_library_downloads,
    normalize_library_registrations,
)

if TYPE_CHECKING:
    from griptape_nodes.node_library.library_registry import (
        LibrarySchema,
    )
    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.events.base_events import ResultPayload
    from griptape_nodes.retained_mode.managers.event_manager import EventManager

logger = logging.getLogger("griptape_nodes")


class DiscoveredLibraryEntry(NamedTuple):
    """Internal pairing of a discovered library with the user's original config entry path.

    `registration` carries the resolved on-disk path used to load the library.
    `registered_path` is the verbatim string from `LibraryRegistration.path` (before
    workspace resolution / `~`-expansion / symlink-following). Surfacing the raw config
    path lets the GUI match library metadata back to its `libraries_to_register` row
    by the same key the user sees in their config.

    For directory entries that expand into multiple library files, every discovered
    child shares the parent directory's `registered_path`.

    `from_environment` is True for a library listed in `GTN_LIBRARY_PATHS`, whose
    `registered_path` is then the verbatim entry from that variable.
    """

    registration: LibraryRegistration
    registered_path: str
    from_environment: bool = False


class ResolvedDiscoveryPath(NamedTuple):
    """A config `libraries_to_register` entry resolved to a concrete on-disk path.

    `path` is the file or directory to scan for manifests: the
    workspace-resolved path for a path-backed entry, or the provisioned
    manifest located by name for a git-sourced entry. `registered_path`
    is the key the GUI uses to map metadata back to the user's config row.
    """

    path: Path
    registered_path: str


def library_checkpoint_attributes(schema: LibrarySchema) -> dict[str, Any]:
    """Resolve the facts an authorization hook may gate a library load on.

    `id` is the library name (so a policy can match a specific library);
    `lifecycle_stage` is the library's declared stage when one is present.
    The engine supplies what it has resolved and does not know which a policy
    will read.
    """
    attributes: dict[str, Any] = {CheckpointAttribute.ID: schema.name}
    stage = next(
        (
            declaration.stage
            for declaration in schema.metadata.declarations
            if isinstance(declaration, LifecycleStageLibraryProperty)
        ),
        None,
    )
    if stage is not None:
        attributes[CheckpointAttribute.LIFECYCLE_STAGE] = stage.value
    return attributes


def resolve_discovery_path(entry: LibraryRegistration, workspace_path: Path) -> ResolvedDiscoveryPath | None:
    """Resolve a `libraries_to_register` entry to a concrete on-disk path to scan.

    A register entry names an already-present local library by `path`, which
    resolves against the workspace. Libraries pinned to a git source live in
    `libraries_to_download` and are resolved separately in
    `discover_library_files` by locating their provisioned manifest under the
    workspace libraries directory, so they never need a `libraries_to_register`
    entry. Returns None when the path does not exist on disk.
    """
    # TODO: Update to check on project manager for workspace path. https://github.com/griptape-ai/griptape-nodes/issues/4396
    library_path = resolve_workspace_path(Path(entry.path), workspace_path)
    if not library_path.exists():
        return None
    return ResolvedDiscoveryPath(path=library_path, registered_path=entry.path)


class LibraryDiscovery(EngineScoped):
    def __init__(self, event_manager: EventManager, *, engine: Engine | None = None) -> None:
        super().__init__(engine)
        event_manager.register_request_handlers(self)

    def remove_missing_libraries_from_config(self, config_category: str) -> None:
        # Now remove all libraries that were missing from the user's config.
        config_mgr = self.engine.config_manager
        libraries_to_register_category = config_mgr.get_config_value(config_category)

        paths_to_remove = set()
        for library_path, library_info in self.engine.library_manager._library_file_path_to_info.items():
            if library_info.fitness == LibraryFitness.MISSING:
                # Remove this file path from the config.
                paths_to_remove.add(library_path.lower())

        if paths_to_remove and libraries_to_register_category:
            libraries_to_register_category = [
                library
                for library in libraries_to_register_category
                if extract_library_path(library).lower() not in paths_to_remove
            ]
            config_mgr.set_config_value(config_category, libraries_to_register_category)

    def migrate_old_xdg_library_paths(self) -> None:
        """Automatically removes old XDG library paths and adds git URLs to download list.

        This method removes library paths that were stored in the old XDG data home location
        (~/.local/share/griptape_nodes/libraries/) from the libraries_to_register configuration,
        and automatically adds the corresponding git URLs to libraries_to_download to ensure
        the libraries are re-downloaded. This migration happens automatically on app startup,
        so users don't need to run gtn init.
        """
        config_mgr = self.engine.config_manager

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

    async def discover_libraries_request(
        self,
        request: DiscoverLibrariesRequest,
    ) -> DiscoverLibrariesResultSuccess | DiscoverLibrariesResultFailure:
        """Discover libraries from config and track them in discovered state.

        Scans configured library paths and creates LibraryInfo entries in DISCOVERED state.
        """
        try:
            config_library_entries = await self.discover_library_files()
        except Exception as e:
            logger.exception("Failed to discover library files")
            return DiscoverLibrariesResultFailure(
                result_details=f"Failed to discover library files: {e}",
            )

        discovered_libraries = []
        seen_libraries = set()
        managed = self.engine.library_manager.managed_environment
        environment_mode = managed.provisioned_by_environment()
        managed.environment_library_paths = managed.environment_paths_from(config_library_entries)

        # A sandbox that is turned off is never scanned: scanning writes its manifest into the
        # workspace. In environment mode it is reported as not provided rather than vanishing.
        sandbox_on = managed.sandbox_enabled()
        if request.include_sandbox and not sandbox_on and environment_mode:
            sandbox_library_dir = self.engine.library_manager.sandbox.get_sandbox_directory()
            if sandbox_library_dir:
                managed.create_not_provided_library_info_entry(
                    str(sandbox_library_dir / LIBRARY_CONFIG_FILENAME),
                    is_sandbox=True,
                    enabled=True,
                    registered_path=None,
                )

        # Process sandbox library first if requested
        if request.include_sandbox and sandbox_on:
            sandbox_json_path = self.discover_sandbox_library()
            if sandbox_json_path is not None and sandbox_json_path not in seen_libraries:
                seen_libraries.add(sandbox_json_path)
                discovered_libraries.append(DiscoveredLibrary(path=sandbox_json_path, is_sandbox=True))

        # Add all regular libraries from config
        for discovered in config_library_entries:
            entry = discovered.registration
            file_path = Path(entry.path)
            file_path_str = entry.path

            # A configured library the environment does not provide is recorded with the reason and
            # left out of the discovered list, so nothing tries to load it.
            if environment_mode and not discovered.from_environment:
                managed.create_not_provided_library_info_entry(
                    file_path_str,
                    is_sandbox=False,
                    enabled=entry.enabled,
                    registered_path=discovered.registered_path,
                )
                continue

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

        Extracts version checking logic from attempt_load_nodes_from_library.
        Checks engine version compatibility without loading Python modules.
        """
        schema = request.schema
        problems: list[LibraryProblem] = []

        # Check for version-based compatibility issues
        version_issues = self.engine.version_compatibility_manager.check_library_version_compatibility(schema)
        has_disqualifying_issues = False

        for issue in version_issues:
            problems.append(issue.problem)
            if issue.severity == LibraryFitness.UNUSABLE:
                has_disqualifying_issues = True

        if has_disqualifying_issues:
            return EvaluateLibraryFitnessResultFailure(
                result_details=f"Library '{schema.name}' has version compatibility issues",
                fitness=LibraryFitness.UNUSABLE,
                problems=problems,
            )

        # License-policy checkpoint: ask any registered authorization hook (the
        # app installs one) whether this library may load past its metadata
        # stage. A denial is rendered as a fitness problem and marks the library
        # UNUSABLE, so it is not registered and the GUI shows every missing
        # permission on the failure icon. With no hook installed this allows.
        denial = self.engine.event_manager.evaluate_authorization_checkpoint(
            AuthorizationCheckpoint(
                action=CheckpointAction.LOAD_LIBRARY,
                subject_type=CheckpointSubjectType.LIBRARY,
                subject_id=schema.name,
                attributes=library_checkpoint_attributes(schema),
            )
        )
        if denial is not None:
            problems.append(PermissionDeniedProblem(library_name=schema.name, messages=denial.messages()))
            return EvaluateLibraryFitnessResultFailure(
                result_details=f"Library '{schema.name}' is not permitted by the license policy",
                fitness=LibraryFitness.UNUSABLE,
                problems=problems,
            )

        # Per-node license-policy preview. A library may be permitted to load while
        # still declaring node types the policy forbids (by lifecycle stage or the
        # arbitrary-code flag). Surface each denied node type as a library problem
        # now -- so the GUI failure icon lists them and what to ask an admin for --
        # without blocking the library, which stays usable for its permitted nodes.
        # Instantiating a denied node later substitutes an Error Proxy via the same
        # checkpoint. Runs on the schema, so no library module is imported here.
        node_denials = self.engine.node_manager.evaluate_schema_node_instantiation_denials(
            schema, event_manager=self.engine.event_manager
        )
        for node_type, node_denial in node_denials.items():
            problems.append(NodePermissionDeniedProblem(node_type=node_type, messages=node_denial.messages()))

        # Determine fitness based on whether we have any non-disqualifying issues
        fitness = LibraryFitness.FLAWED if problems else LibraryFitness.GOOD

        return EvaluateLibraryFitnessResultSuccess(
            result_details=f"Library '{schema.name}' is compatible",
            fitness=fitness,
            problems=problems,
        )

    @handles(LoadLibrariesRequest)
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
            lib_info = self.engine.library_manager._library_file_path_to_info.get(lib_path)

            # Update is_sandbox if library_info exists and discovery says it's sandbox
            if lib_info and discovered_lib.is_sandbox:
                lib_info.is_sandbox = True

            if lib_info and lib_info.lifecycle_state != LibraryLifecycleState.DISABLED:
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
            pre_register_info = self.engine.library_manager._library_file_path_to_info.get(lib_path)
            pending_library_name = (
                pre_register_info.library_name
                if pre_register_info and pre_register_info.library_name
                else Path(lib_path).stem
            )
            self.engine.event_manager.put_event(
                AppEvent(
                    payload=EngineInitializationProgress(
                        phase=InitializationPhase.LIBRARIES,
                        item_name=pending_library_name,
                        status=InitializationStatus.LOADING,
                        current=current_library_index,
                        total=total_libraries,
                        is_worker=self.engine.library_manager.is_worker,
                    )
                )
            )

            with self.engine.library_manager.track_load_progress(
                lib_path, current=current_library_index, total=total_libraries
            ):
                load_result = await self.engine.library_manager.registration.register_library_from_file_request(
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
                self.engine.event_manager.put_event(
                    AppEvent(
                        payload=EngineInitializationProgress(
                            phase=InitializationPhase.LIBRARIES,
                            item_name=library_name,
                            status=InitializationStatus.COMPLETE,
                            current=current_library_index,
                            total=total_libraries,
                            is_worker=self.engine.library_manager.is_worker,
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
                self.engine.event_manager.put_event(
                    AppEvent(
                        payload=EngineInitializationProgress(
                            phase=InitializationPhase.LIBRARIES,
                            item_name=library_name,
                            status=InitializationStatus.FAILED,
                            current=current_library_index,
                            total=total_libraries,
                            error=error_message,
                            is_worker=self.engine.library_manager.is_worker,
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

    async def discover_library_files(self) -> list[DiscoveredLibraryEntry]:  # noqa: C901 (environment, config, and download sources each branch)
        """Discover library JSON files from config and workspace recursively.

        Returns:
            List of DiscoveredLibraryEntry pairing each discovered LibraryRegistration
            with the user's original `LibraryRegistration.path` string from config (before
            workspace resolution). Directory entries expand to one entry per discovered
            library file; every child inherits the parent's `registered_path`.
        """
        config_mgr = self.engine.config_manager
        user_libraries_section = LIBRARIES_TO_REGISTER_KEY

        discovered_entries: list[DiscoveredLibraryEntry] = []
        seen_paths: set[Path] = set()

        async def process_path(
            path: Path, *, enabled: bool, registered_path: str, from_environment: bool = False
        ) -> None:
            """Process a path, handling both files and directories."""
            if await anyio.Path(path).is_dir():
                # Recursively find library files. find_files_recursive skips hidden
                # directories and bounds recursion depth so a deep or symlink-looped
                # tree can't stall the boot scan.
                for lib_path in await find_files_recursive(
                    path,
                    LIBRARY_CONFIG_GLOB_PATTERN,
                    max_depth=self.engine.config_manager.discovery_max_depth,
                ):
                    if lib_path not in seen_paths:
                        seen_paths.add(lib_path)
                        discovered_entries.append(
                            DiscoveredLibraryEntry(
                                registration=LibraryRegistration(path=str(lib_path), enabled=enabled),
                                registered_path=registered_path,
                                from_environment=from_environment,
                            )
                        )
            elif path.suffix == ".json" and path not in seen_paths:
                seen_paths.add(path)
                discovered_entries.append(
                    DiscoveredLibraryEntry(
                        registration=LibraryRegistration(path=str(path), enabled=enabled),
                        registered_path=registered_path,
                        from_environment=from_environment,
                    )
                )

        # Libraries the environment provides come first, so a libraries_to_register entry naming the
        # same manifest is the duplicate, not the environment's copy. Read from the engine's startup
        # environment, not a project's: a project template must not change which libraries the
        # environment provides.
        startup_environ = self.engine.project_manager.get_pre_project_environ()
        for environment_path in library_paths_from_environment(startup_environ):
            resolved = resolve_discovery_path(LibraryRegistration(path=environment_path), config_mgr.workspace_path)
            if resolved is None:
                logger.warning(
                    "Ignoring '%s' in %s: there is no library at that path.", environment_path, LIBRARY_PATHS_ENV_VAR
                )
                continue
            await process_path(
                resolved.path, enabled=True, registered_path=resolved.registered_path, from_environment=True
            )

        # Add from config
        config_libraries = config_mgr.get_config_value(user_libraries_section, default=[])
        for entry in normalize_library_registrations(config_libraries):
            resolved = resolve_discovery_path(entry, config_mgr.workspace_path)
            if resolved is not None:
                await process_path(resolved.path, enabled=entry.enabled, registered_path=resolved.registered_path)

        # Nothing is downloaded when the environment provides the libraries, so there is no
        # provisioned copy to find. The libraries_to_register entries above are still discovered,
        # so each can be reported as not provided rather than silently vanishing.
        if self.engine.library_manager.managed_environment.provisioned_by_environment():
            return discovered_entries

        # Add provisioned git-sourced libraries. Each libraries_to_download entry is
        # cloned into the workspace libraries_directory by reconcile; discovery
        # resolves it to its installed manifest there so it loads scoped to the
        # workspace that declares it, without ever being written into the global
        # libraries_to_register config (which would leak it into every project).
        libraries_root = config_mgr.resolved_libraries_root()
        download_libraries = config_mgr.get_config_value(LIBRARIES_TO_DOWNLOAD_KEY, default=[])
        for download in normalize_library_downloads(download_libraries):
            manifest_path = await self.engine.library_manager.provisioning.installed_manifest_path_for_download(
                download, libraries_root
            )
            if manifest_path is not None:
                await process_path(manifest_path, enabled=True, registered_path=str(manifest_path))

        return discovered_entries

    async def log_unregistered_libraries(self, discovered_libraries: list[DiscoveredLibrary]) -> None:
        """Log a hint for each library manifest under the libraries root that discovery did not pick up.

        Nothing under `libraries_directory` loads on its own: a library loads only from a
        `libraries_to_register` entry or a `libraries_to_download` entry. A manifest copied
        into that folder by hand is otherwise skipped silently.

        Not reported: manifests in the top-level folder of a download entry, and in the
        sandbox library, which loads separately. A top-level folder that is a git clone may
        be another project's `libraries_to_download` install in the shared libraries root,
        so its hint says so.

        Called once per actual load (boot, reload), on the orchestrator only, rather than
        from `discover_library_files` itself: that helper also backs lazy per-request lookups
        and metadata refreshes, which would otherwise re-log the same hint many times a session.
        """
        config_mgr = self.engine.config_manager
        libraries_root = config_mgr.resolved_libraries_root()
        if not await anyio.Path(libraries_root).is_dir():
            return

        download_directories: list[Path] = []
        download_libraries = config_mgr.get_config_value(LIBRARIES_TO_DOWNLOAD_KEY, default=[])
        for download in normalize_library_downloads(download_libraries):
            manifest_path = await self.engine.library_manager.provisioning.installed_manifest_path_for_download(
                download, libraries_root
            )
            if manifest_path is not None:
                download_directories.append(manifest_path.parent)

        discovered_paths = {canonicalize_for_identity(library.path) for library in discovered_libraries}
        download_identities = [canonicalize_for_identity(directory) for directory in download_directories]
        sandbox_directory = self.engine.library_manager.sandbox.get_sandbox_directory()
        sandbox_identity = None
        if sandbox_directory is not None:
            sandbox_identity = canonicalize_for_identity(sandbox_directory)

        for manifest_path in await find_files_recursive(
            libraries_root,
            LIBRARY_CONFIG_GLOB_PATTERN,
            max_depth=self.engine.config_manager.discovery_max_depth,
        ):
            identity = canonicalize_for_identity(manifest_path)
            if identity in discovered_paths:
                continue
            if sandbox_identity is not None and identity.is_relative_to(sandbox_identity):
                continue
            # Judge ownership by the unresolved top-level folder so a symlinked library still counts as under the root.
            top_directory = libraries_root / manifest_path.relative_to(libraries_root).parts[0]
            top_identity = canonicalize_for_identity(top_directory)
            if any(download.is_relative_to(top_identity) for download in download_identities):
                continue
            message = (
                "Found library '%s' in the libraries directory, but it is not registered, so it was not loaded. "
                "To load it, use Add Library or add its path to libraries_to_register in your config."
            )
            if await anyio.Path(top_directory / ".git").exists():
                message += " If another project downloads it through libraries_to_download, no action is needed."
            logger.info(message, manifest_path)

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

    def discover_sandbox_library(self) -> Path | None:
        """Scan the sandbox directory, write its manifest, and record it. Returns the manifest path.

        None when no sandbox directory is configured or the scan fails. Once the scan succeeds, a
        record left from a discovery that refused the sandbox is replaced, so enabling the sandbox
        takes effect without a restart.
        """
        sandbox = self.engine.library_manager.sandbox
        sandbox_library_dir = sandbox.get_sandbox_directory()
        if sandbox_library_dir is None:
            return None

        # Generate/update the sandbox library JSON file
        metadata_result = sandbox.scan_sandbox_directory_request(
            ScanSandboxDirectoryRequest(directory_path=str(sandbox_library_dir))
        )
        if not isinstance(metadata_result, ScanSandboxDirectoryResultSuccess):
            return None

        sandbox_json_path = sandbox_library_dir / LIBRARY_CONFIG_FILENAME
        sandbox_json_path_str = str(sandbox_json_path)

        # Write the schema to JSON so it exists for lifecycle phases
        write_succeeded = sandbox.write_library_schema_to_json(metadata_result.library_schema, sandbox_json_path)
        if write_succeeded:
            logger.debug(
                "Wrote sandbox library schema with %d nodes to '%s' during discovery",
                len(metadata_result.library_schema.nodes),
                sandbox_json_path,
            )
        # Continue anyway if write failed - lifecycle will fail gracefully

        library_infos = self.engine.library_manager._library_file_path_to_info
        existing = library_infos.get(sandbox_json_path_str)
        managed = self.engine.library_manager.managed_environment
        if existing is not None and managed.is_not_provided_by_environment(existing):
            del library_infos[sandbox_json_path_str]

        # Create LibraryInfo entry for the sandbox library
        self._create_library_info_entry(sandbox_json_path_str, is_sandbox=True)
        return sandbox_json_path

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
        existing = self.engine.library_manager._library_file_path_to_info.get(file_path_str)
        if existing is not None:
            existing_is_disabled = existing.lifecycle_state == LibraryLifecycleState.DISABLED
            requested_is_disabled = not enabled
            if existing_is_disabled == requested_is_disabled:
                # Already in the right state; keep the existing entry as-is.
                return
            # The user toggled enabled in libraries_to_register; drop the stale entry so
            # the block below recreates it with the new lifecycle.
            del self.engine.library_manager._library_file_path_to_info[file_path_str]

        metadata_result = self.engine.library_manager.metadata_loading.load_library_metadata_from_file_request(
            LoadLibraryMetadataFromFileRequest(file_path=file_path_str)
        )

        library_name = None
        library_version = None
        executes_in_worker = False
        lifecycle_state = LibraryLifecycleState.DISCOVERED

        if isinstance(metadata_result, LoadLibraryMetadataFromFileResultSuccess):
            library_name = metadata_result.library_schema.name
            library_version = metadata_result.library_schema.metadata.library_version
            executes_in_worker = resolve_executes_in_worker(metadata=metadata_result.library_schema.metadata)
            lifecycle_state = LibraryLifecycleState.METADATA_LOADED
            if enabled:
                self.engine.library_manager.workers.log_legacy_worker_mode_advisory(
                    library_name=library_name,
                    registered_path=registered_path,
                    declarations=metadata_result.library_schema.metadata.declarations,
                    executes_in_worker=executes_in_worker,
                )

        if not enabled:
            lifecycle_state = LibraryLifecycleState.DISABLED

        self.engine.library_manager._library_file_path_to_info[file_path_str] = LibraryInfo(
            lifecycle_state=lifecycle_state,
            fitness=LibraryFitness.NOT_EVALUATED,
            library_path=file_path_str,
            is_sandbox=is_sandbox,
            enabled=enabled,
            library_name=library_name,
            library_version=library_version,
            registered_path=registered_path,
            executes_in_worker=executes_in_worker,
        )
