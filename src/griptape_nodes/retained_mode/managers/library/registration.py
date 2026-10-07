from __future__ import annotations

import asyncio
import logging
import subprocess
from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from packaging.requirements import InvalidRequirement, Requirement

from griptape_nodes.node_library.library_declarations import (
    LibraryDependencyDeclaration,
)
from griptape_nodes.node_library.library_registry import (
    Library,
    LibraryRegistry,
    LibrarySchema,
)
from griptape_nodes.retained_mode.beta_features import (
    LIBRARY_BETA_FEATURES_KEY,
    find_library_config_slug_collision,
    library_config_slug,
)
from griptape_nodes.retained_mode.engine import EngineScoped
from griptape_nodes.retained_mode.events.app_events import (
    ReportLibraryLoadedRequest,
)

# Runtime imports for ResultDetails since it's used at runtime
from griptape_nodes.retained_mode.events.base_events import ResultDetails
from griptape_nodes.retained_mode.events.config_events import (
    GetConfigCategoryRequest,
    GetConfigCategoryResultSuccess,
    SetConfigCategoryRequest,
    SetConfigCategoryResultSuccess,
)
from griptape_nodes.retained_mode.events.library_events import (
    DiscoverLibrariesRequest,
    DiscoverLibrariesResultSuccess,
    DownloadLibraryRequest,
    DownloadLibraryResultFailure,
    EvaluateLibraryFitnessRequest,
    EvaluateLibraryFitnessResultFailure,
    InstallLibraryDependenciesRequest,
    InstallLibraryDependenciesResultFailure,
    LoadLibraryMetadataFromFileRequest,
    LoadLibraryMetadataFromFileResultFailure,
    RegisterLibraryFromFileRequest,
    RegisterLibraryFromFileResultFailure,
    RegisterLibraryFromFileResultSuccess,
    RegisterLibraryFromRequirementSpecifierRequest,
    RegisterLibraryFromRequirementSpecifierResultFailure,
    RegisterLibraryFromRequirementSpecifierResultSuccess,
    UnloadLibraryFromRegistryRequest,
    UnloadLibraryFromRegistryResultFailure,
    UnloadLibraryFromRegistryResultSuccess,
)
from griptape_nodes.retained_mode.managers.fitness_problems.libraries import (
    AdvancedLibraryLoadFailureProblem,
    BetaFeatureSettingsCollisionProblem,
    CreateConfigCategoryProblem,
    DependencyInstallationFailedProblem,
    DuplicateLibraryProblem,
    LibraryDependencyProblem,
    LibraryProblem,
    ShadowedEnginePackagesProblem,
    UpdateConfigCategoryProblem,
)
from griptape_nodes.retained_mode.managers.library.common import LibraryFitness, LibraryInfo, LibraryLifecycleState
from griptape_nodes.retained_mode.managers.library.dependencies import parse_dependency_url
from griptape_nodes.retained_mode.managers.library.environment import describe_unmet_requirements
from griptape_nodes.retained_mode.managers.library.workers import resolve_executes_in_worker
from griptape_nodes.retained_mode.managers.os_manager import OSManager
from griptape_nodes.retained_mode.managers.settings import (
    LIBRARY_DEPENDENCY_INSTALL_BEHAVIOR_KEY,
    LibraryDependencyInstallBehavior,
)
from griptape_nodes.retained_mode.request_handlers import handles
from griptape_nodes.utils.dict_utils import merge_dicts, normalize_secrets_to_register
from griptape_nodes.utils.uv_utils import find_uv_bin

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.events.base_events import ResultPayload
    from griptape_nodes.retained_mode.managers.event_manager import EventManager

logger = logging.getLogger("griptape_nodes")


class RegisterLibraryPrerequisites(NamedTuple):
    """Prerequisites established for library loading."""

    library_info: LibraryInfo
    file_path: str


class LibraryRegistrar(EngineScoped):
    def __init__(self, event_manager: EventManager, *, engine: Engine | None = None) -> None:
        super().__init__(engine)
        event_manager.register_request_handlers(self)

    @handles(RegisterLibraryFromFileRequest)
    async def register_library_from_file_request(self, request: RegisterLibraryFromFileRequest) -> ResultPayload:
        """Register a library by name or path, progressing through all lifecycle phases.

        Supports loading by library_name OR file_path (mutually exclusive), with optional
        discovery integration. Creates LibraryInfo if not already tracked.

        A library that newly arrived also gets its declared workflow templates registered.

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
        # Even a library that failed partway may have reached the registry.
        self.engine.workflow_manager.note_library_set_changed()

        # FAILURE CHECK
        if isinstance(progression_result, RegisterLibraryFromFileResultFailure):
            return progression_result

        # Phase 3: Build the result from the library's fitness, and register what it ships.
        # At this point, library_name must be set (it's set during METADATA_LOADED phase)
        library_name = library_info.library_name
        if library_name is None:
            details = "Library loaded but library_name was not set during metadata loading"
            return RegisterLibraryFromFileResultFailure(result_details=details)

        result = self._build_register_library_result(library_info, library_name=library_name, file_path=file_path)
        if isinstance(result, RegisterLibraryFromFileResultSuccess):
            await self.engine.library_manager.register_workflows_for_library(library_info)
        return result

    def _build_register_library_result(
        self, library_info: LibraryInfo, *, library_name: str, file_path: str
    ) -> RegisterLibraryFromFileResultSuccess | RegisterLibraryFromFileResultFailure:
        """Turn a loaded library's fitness into the result its registration reports."""
        match library_info.fitness:
            case LibraryFitness.GOOD:
                details = f"Successfully loaded Library '{library_name}' from JSON file at {file_path}"
                result = RegisterLibraryFromFileResultSuccess(
                    library_name=library_name,
                    result_details=ResultDetails(message=details, level=logging.INFO),
                )
            case LibraryFitness.FLAWED:
                details = f"Successfully loaded Library JSON file from '{file_path}', but one or more nodes failed to load. Check the log for more details."
                result = RegisterLibraryFromFileResultSuccess(
                    library_name=library_name,
                    result_details=ResultDetails(message=details, level=logging.WARNING),
                )
            case LibraryFitness.UNUSABLE:
                details = f"Attempted to load Library JSON file from '{file_path}'. Failed because no nodes were loaded. Check the log for more details."
                result = RegisterLibraryFromFileResultFailure(result_details=details)
            case LibraryFitness.NOT_EVALUATED:
                # Worker-delegated libraries on the orchestrator: node imports are skipped
                # and fitness will be updated once the worker reports back via
                # ReportLibraryLoadedRequest. Workers are started asynchronously (either by
                # AppStartSessionRequest or by maybe_start_workers_for_existing_session)
                # so we must NOT block here -- doing so would prevent the orchestrator from
                # sending heartbeats to the worker process, causing it to self-terminate.
                details = f"Successfully registered Library '{library_name}' from '{file_path}'. Node loading is delegated to a worker process."
                result = RegisterLibraryFromFileResultSuccess(
                    library_name=library_name,
                    result_details=ResultDetails(message=details, level=logging.INFO),
                )
            case _:
                details = f"Attempted to load Library JSON file from '{file_path}'. Failed because an unknown/unexpected fitness '{library_info.fitness}' was returned."
                result = RegisterLibraryFromFileResultFailure(result_details=details)
        return result

    @handles(RegisterLibraryFromRequirementSpecifierRequest)
    async def register_library_from_requirement_specifier_request(
        self, request: RegisterLibraryFromRequirementSpecifierRequest
    ) -> ResultPayload:
        try:
            package_name = Requirement(request.requirement_specifier).name
            # Determine venv path for dependency installation
            venv_path = self.engine.library_manager.environment.get_library_venv_path(package_name, None)

            # A broken directory is recreated by init_library_venv, in which case dependencies
            # must be installed; a reused functional venv already has them.
            try:
                venv_init = await self.engine.library_manager.environment.init_library_venv(venv_path)
            except RuntimeError as e:
                details = f"Attempted to prepare the environment for library '{request.requirement_specifier}'. Failed due to: {e}"
                return RegisterLibraryFromRequirementSpecifierResultFailure(result_details=details)
            library_python_venv_path = venv_init.python_path

            if venv_init.reused:
                logger.debug(
                    "Skipping dependency installation for package '%s' - venv already exists at %s",
                    package_name,
                    venv_path,
                )
            elif self.engine.library_manager.environment.can_write_to_venv_location(library_python_venv_path):
                # Check disk space before installing dependencies
                config_manager = self.engine.config_manager
                min_space_gb = config_manager.get_config_value("minimum_disk_space_gb_libraries")
                if not OSManager.check_available_disk_space(Path(venv_path), min_space_gb):
                    error_msg = OSManager.format_disk_space_error(Path(venv_path))
                    details = f"Attempted to install library '{request.requirement_specifier}'. Failed when installing dependencies due to insufficient disk space (requires {min_space_gb} GB): {error_msg}"
                    return RegisterLibraryFromRequirementSpecifierResultFailure(result_details=details)

                uv_path = find_uv_bin()

                logger.info("Installing dependency '%s' with pip in venv at %s", package_name, venv_path)
                is_debug = config_manager.get_config_value("log_level").upper() == "DEBUG"
                await self.engine.library_manager.dependencies.install_under_engine_floors(
                    [
                        uv_path,
                        "pip",
                        "install",
                        request.requirement_specifier,
                        "--python",
                        str(library_python_venv_path),
                    ],
                    library_python_venv_path,
                    capture_output=not is_debug,
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

        register_result = await self.engine.ahandle_request(RegisterLibraryFromFileRequest(file_path=library_path))
        if isinstance(register_result, RegisterLibraryFromFileResultFailure):
            details = f"Attempted to install library '{request.requirement_specifier}'. Failed due to {register_result}"
            return RegisterLibraryFromRequirementSpecifierResultFailure(result_details=details)

        return RegisterLibraryFromRequirementSpecifierResultSuccess(
            library_name=request.requirement_specifier,
            result_details=f"Successfully registered library from requirement specifier: {request.requirement_specifier}",
        )

    @handles(UnloadLibraryFromRegistryRequest)
    def unload_library_from_registry_request(self, request: UnloadLibraryFromRegistryRequest) -> ResultPayload:
        try:
            LibraryRegistry.unregister_library(
                library_name=request.library_name, event_manager=self.engine.event_manager
            )
        except Exception as e:
            details = f"Attempted to unload library '{request.library_name}'. Failed due to {e}"
            return UnloadLibraryFromRegistryResultFailure(result_details=details)

        # Release anything this library was holding in THIS process, so it cannot come back holding
        # objects its previous code built.
        #
        # This process only. Reloading every library restarts the workers, which takes their copies with
        # them, but updating or switching the ref of a single library does not.
        dropped = self.engine.resource_manager.drop_objects_for_group(request.library_name)
        if dropped:
            logger.debug(
                "Released %d held object(s) belonging to library '%s' as it unloaded.",
                dropped,
                request.library_name,
            )

        # Clean up all stable module aliases for this library. Note first whether any of its node
        # modules had actually been imported: if so, this process is stuck with that code and the
        # library's next load cannot replace it.
        if self.engine.library_manager.module_loading._library_to_stable_modules.get(request.library_name):
            self.engine.library_manager._libraries_reloaded_after_import.add(request.library_name)
        self.engine.library_manager.module_loading.unregister_all_stable_module_aliases_for_library(
            request.library_name
        )

        # Nothing else takes a library's workflows out: the workspace rescan spares them.
        self.engine.workflow_manager.remove_library_workflows(request.library_name)
        self.engine.workflow_manager.note_library_set_changed()

        # Remove the library from our library info list. This prevents it from still showing
        # up in the table of attempted library loads. Remove ALL entries for this name, not
        # just the first: a duplicately-registered library (e.g. two on-disk copies, or a
        # filename variation left behind by a git operation) would otherwise keep a stale
        # entry alive, which desynchronizes the update and update-check paths.
        stale_paths = [
            file_path
            for file_path, library_info in self.engine.library_manager._library_file_path_to_info.items()
            if library_info.library_name == request.library_name
        ]
        for file_path in stale_paths:
            del self.engine.library_manager._library_file_path_to_info[file_path]
        # Whether a worker is available is keyed by library name over there, so it has to be dropped
        # with the record rather than outliving it.
        self.engine.library_manager._worker_manager.forget_library(request.library_name)
        details = f"Successfully unloaded (and unregistered) library '{request.library_name}'."
        return UnloadLibraryFromRegistryResultSuccess(result_details=details)

    async def _establish_register_library_prerequisites(  # noqa: C901, PLR0911, PLR0912 (prerequisite validation needs branches)
        self, request: RegisterLibraryFromFileRequest
    ) -> RegisterLibraryPrerequisites | RegisterLibraryFromFileResultSuccess | RegisterLibraryFromFileResultFailure:
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
            lib_info = self.engine.library_manager._library_file_path_to_info.get(file_path)

            # If we don't have LibraryInfo yet, load metadata to get the name
            if not lib_info or not lib_info.library_name:
                metadata_result = self.engine.library_manager.metadata_loading.load_library_metadata_from_file_request(
                    LoadLibraryMetadataFromFileRequest(file_path=file_path)
                )

                if isinstance(metadata_result, LoadLibraryMetadataFromFileResultFailure):
                    if lib_info is not None:
                        self._apply_metadata_load_failure(lib_info, metadata_result)
                    return RegisterLibraryFromFileResultFailure(result_details=metadata_result.result_details)

                library_name = metadata_result.library_schema.name

                # Entering METADATA_LOADED directly, so the DISCOVERED-phase writer that
                # normally resolves requires_worker never runs for this registration.
                # Resolve it here too, or a library whose manifest suggests worker mode
                # loads in-process when registered by file path (e.g. added to a running
                # engine from the editor) -- its advanced module would then import in the
                # orchestrator without the worker-managed dependency environment.
                requires_worker = self.engine.library_manager.workers.resolve_requires_worker(
                    lib_info.registered_path if lib_info else None,
                    metadata_result.library_schema.metadata.declarations,
                )

                # Update or create LibraryInfo
                executes_in_worker = resolve_executes_in_worker(
                    requires_worker=requires_worker, metadata=metadata_result.library_schema.metadata
                )
                if lib_info:
                    lib_info.library_name = library_name
                    lib_info.library_version = metadata_result.library_schema.metadata.library_version
                    lib_info.lifecycle_state = LibraryLifecycleState.METADATA_LOADED
                    lib_info.requires_worker = requires_worker
                    lib_info.executes_in_worker = executes_in_worker
                else:
                    # Create new LibraryInfo since it doesn't exist yet
                    lib_info = LibraryInfo(
                        lifecycle_state=LibraryLifecycleState.METADATA_LOADED,
                        library_path=file_path,
                        is_sandbox=False,
                        library_name=library_name,
                        library_version=metadata_result.library_schema.metadata.library_version,
                        fitness=LibraryFitness.NOT_EVALUATED,
                        problems=[],
                        requires_worker=requires_worker,
                        executes_in_worker=executes_in_worker,
                    )
                    self.engine.library_manager._library_file_path_to_info[file_path] = lib_info
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
        library_info = self.engine.library_manager.get_library_info_by_library_name(library_name)

        # If not found and discovery is allowed, try discovery
        if library_info is None and request.perform_discovery_if_not_found:
            discover_result = await self.engine.library_manager.discovery.discover_libraries_request(
                DiscoverLibrariesRequest()
            )
            if isinstance(discover_result, DiscoverLibrariesResultSuccess):
                library_info = self.engine.library_manager.get_library_info_by_library_name(library_name)

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
        return RegisterLibraryPrerequisites(library_info=library_info, file_path=file_path)

    async def _progress_library_through_lifecycle(  # noqa: C901, PLR0911, PLR0912, PLR0915 (lifecycle state machine needs branches/statements/returns)
        self,
        library_info: LibraryInfo,
        file_path: str,
        request: RegisterLibraryFromFileRequest,
    ) -> RegisterLibraryFromFileResultFailure | None:
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
                case LibraryLifecycleState.LOADED:
                    # Terminal state: inconsistent (marked LOADED but not in registry)
                    details = f"Library '{library_info.library_name}' marked as LOADED but not in registry"
                    self.engine.library_manager._library_file_path_to_info[library_info.library_path] = library_info
                    return RegisterLibraryFromFileResultFailure(result_details=details)

                case LibraryLifecycleState.FAILURE:
                    # Terminal state: failure
                    details = f"Library '{library_info.library_name}' is in FAILURE state and cannot be loaded"
                    self.engine.library_manager._library_file_path_to_info[library_info.library_path] = library_info
                    return RegisterLibraryFromFileResultFailure(result_details=details)

                case LibraryLifecycleState.DISABLED:
                    # Terminal state: the user has disabled this library in libraries_to_register.
                    # Loading was intentionally skipped.
                    details = (
                        f"Library at '{library_info.library_path}' is disabled in libraries_to_register "
                        f"and was not loaded"
                    )
                    self.engine.library_manager._library_file_path_to_info[library_info.library_path] = library_info
                    return RegisterLibraryFromFileResultFailure(result_details=details)

                case LibraryLifecycleState.DISCOVERED:
                    # DISCOVERED → METADATA_LOADED
                    # All libraries (including sandbox) load metadata from JSON file
                    metadata_result = (
                        self.engine.library_manager.metadata_loading.load_library_metadata_from_file_request(
                            LoadLibraryMetadataFromFileRequest(file_path=library_info.library_path)
                        )
                    )

                    if isinstance(metadata_result, LoadLibraryMetadataFromFileResultFailure):
                        self._apply_metadata_load_failure(library_info, metadata_result)
                        return RegisterLibraryFromFileResultFailure(result_details=metadata_result.result_details)

                    # Update library_info with metadata results
                    library_info.library_name = metadata_result.library_schema.name
                    library_info.library_version = metadata_result.library_schema.metadata.library_version
                    library_info.requires_worker = self.engine.library_manager.workers.resolve_requires_worker(
                        library_info.registered_path,
                        metadata_result.library_schema.metadata.declarations,
                    )
                    library_info.executes_in_worker = resolve_executes_in_worker(
                        requires_worker=library_info.requires_worker,
                        metadata=metadata_result.library_schema.metadata,
                    )
                    library_info.lifecycle_state = LibraryLifecycleState.METADATA_LOADED

                case LibraryLifecycleState.METADATA_LOADED:
                    # METADATA_LOADED → EVALUATED
                    # Need to load schema to pass to evaluate request
                    metadata_result = (
                        self.engine.library_manager.metadata_loading.load_library_metadata_from_file_request(
                            LoadLibraryMetadataFromFileRequest(file_path=library_info.library_path)
                        )
                    )

                    if isinstance(metadata_result, LoadLibraryMetadataFromFileResultFailure):
                        self._apply_metadata_load_failure(library_info, metadata_result)
                        return RegisterLibraryFromFileResultFailure(result_details=metadata_result.result_details)

                    evaluate_result = self.engine.library_manager.discovery.evaluate_library_fitness_request(
                        EvaluateLibraryFitnessRequest(schema=metadata_result.library_schema)
                    )
                    if isinstance(evaluate_result, EvaluateLibraryFitnessResultFailure):
                        library_info.fitness = evaluate_result.fitness
                        library_info.lifecycle_state = LibraryLifecycleState.FAILURE
                        library_info.problems.extend(evaluate_result.problems)
                        self.engine.library_manager._library_file_path_to_info[library_info.library_path] = library_info
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
                        requirements_check_result = self.engine.library_manager.environment.check_library_requirements(
                            library_requirements, library_data.name
                        )
                        if requirements_check_result is not None:
                            # A declared resource this machine does not have costs EXECUTION, not
                            # loading: a missing GPU does not stop the orchestrator importing node
                            # modules, drawing their parameters, or saving a workflow that uses them.
                            # Same rule as a dependency that will not install -- the capability gates
                            # the run, and the artist finds out when they run.
                            library_info.fitness = LibraryFitness.FLAWED
                            library_info.problems.append(requirements_check_result)
                            library_info.execution_unavailable_reason = describe_unmet_requirements(
                                requirements_check_result
                            )

                    library_info.lifecycle_state = LibraryLifecycleState.EVALUATED

                case LibraryLifecycleState.EVALUATED:
                    # EVALUATED -> DEPENDENCIES_INSTALLED or WORKER_DELEGATED
                    # Resolve library_dependencies before node imports: each dependency library must be
                    # fully loaded (including its venv added to sys.path via add_library_paths_to_sys_path)
                    # before this library's nodes are imported in the LOADED phase. Venvs are completely
                    # isolated - dependency packages are not accessible to this library's pip install
                    # subprocess.
                    dep_metadata_result = (
                        self.engine.library_manager.metadata_loading.load_library_metadata_from_file_request(
                            LoadLibraryMetadataFromFileRequest(file_path=library_info.library_path)
                        )
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
                            config_mgr = self.engine.config_manager
                            install_behavior = config_mgr.get_config_value(
                                LIBRARY_DEPENDENCY_INSTALL_BEHAVIOR_KEY,
                                default=LibraryDependencyInstallBehavior.ALWAYS,
                                cast_type=str,
                            )
                            for dep in griptape_library_deps:
                                parsed_dep = parse_dependency_url(dep.url)
                                repo_name = parsed_dep.repo_name
                                already_registered = any(
                                    (info.library_name == repo_name or repo_name in Path(info.library_path).parts)
                                    and info.lifecycle_state != LibraryLifecycleState.FAILURE
                                    and info.fitness
                                    not in (
                                        LibraryFitness.UNUSABLE,
                                        LibraryFitness.MISSING,
                                    )
                                    for info in self.engine.library_manager._library_file_path_to_info.values()
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
                                        library_info.fitness = LibraryFitness.FLAWED
                                    logger.debug(
                                        "Skipping download of library dependency '%s' (library_dependency_install_behavior=never)",
                                        dep.url,
                                    )
                                    continue
                                dep_result = await self.engine.library_manager.git_operations.download_library_request(
                                    DownloadLibraryRequest(
                                        git_url=parsed_dep.normalized_url,
                                        branch_tag_commit=parsed_dep.ref,
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
                                        library_info.fitness = LibraryFitness.UNUSABLE
                                        library_info.lifecycle_state = LibraryLifecycleState.FAILURE
                                        self.engine.library_manager._library_file_path_to_info[
                                            library_info.library_path
                                        ] = library_info
                                        details = f"Attempted to load Library '{library_info.library_name}'. Failed to load required library dependency '{dep.url}': {dep_result.result_details}"
                                        return RegisterLibraryFromFileResultFailure(result_details=details)
                                    logger.warning(
                                        "Optional library dependency '%s' failed to load: %s",
                                        dep.url,
                                        dep_result.result_details,
                                    )

                    # A worker-mode library still reaches the install, because the orchestrator has
                    # to build its EXECUTION environment: the worker receives that directory as
                    # PYTHONPATH and so cannot create it. Only the edit-time venv is the worker's,
                    # and `_this_process_owns_the_edit_venv` is what declines it here. The lifecycle
                    # still reports WORKER_DELEGATED and completes through LOADED, so the library is
                    # registered for the editor and for workflow loading.
                    delegated_to_worker = library_info.requires_worker and not self.engine.library_manager.is_worker
                    install_result = (
                        await self.engine.library_manager.dependencies.install_library_dependencies_request(
                            InstallLibraryDependenciesRequest(library_file_path=library_info.library_path)
                        )
                    )
                    if isinstance(install_result, InstallLibraryDependenciesResultFailure):
                        # Replaced, not appended: the lifecycle re-enters at EVALUATED on every
                        # reload while the LibraryInfo survives, and the display shows the
                        # OLDEST instance -- appending would keep reporting the first reason.
                        # Fitness stays with the dependency block above, which decides it.
                        library_info.problems = [
                            problem
                            for problem in library_info.problems
                            if not isinstance(problem, DependencyInstallationFailedProblem)
                        ]
                        library_info.problems.append(
                            DependencyInstallationFailedProblem(error_details=str(install_result.result_details))
                        )
                        self.engine.library_manager._library_file_path_to_info[library_info.library_path] = library_info
                        return RegisterLibraryFromFileResultFailure(result_details=install_result.result_details)

                    # Cleared on success for the same LibraryInfo-is-preserved reason the
                    # failure branch replaces rather than appends: a marker left over from a
                    # transient failure would keep refusing this library's worker spawns for
                    # every later session, reporting a problem that no longer exists.
                    library_info.problems = [
                        problem
                        for problem in library_info.problems
                        if not isinstance(problem, DependencyInstallationFailedProblem)
                    ]

                    # Checked on every load rather than only after an install that ran: an
                    # environment built by an older engine can hold an older copy of something
                    # this engine imports, and a dependency set that has not changed gives the
                    # installer nothing to re-resolve. Replaced and cleared for the same
                    # LibraryInfo-is-preserved reason as the marker above.
                    shadowed_packages = await self.engine.library_manager.dependencies.shadowed_engine_packages(
                        library_info.library_name, library_info.library_path
                    )
                    library_info.problems = [
                        problem
                        for problem in library_info.problems
                        if not isinstance(problem, ShadowedEnginePackagesProblem)
                    ]
                    if shadowed_packages:
                        library_info.problems.append(ShadowedEnginePackagesProblem(packages=shadowed_packages))

                    library_info.lifecycle_state = (
                        LibraryLifecycleState.WORKER_DELEGATED
                        if delegated_to_worker
                        else LibraryLifecycleState.DEPENDENCIES_INSTALLED
                    )

                case LibraryLifecycleState.DEPENDENCIES_INSTALLED | LibraryLifecycleState.WORKER_DELEGATED:
                    # DEPENDENCIES_INSTALLED or WORKER_DELEGATED → LOADED

                    if not library_info.is_sandbox:
                        # REGULAR LIBRARIES: Standard registration from JSON file
                        # Load metadata and create library
                        metadata_result = (
                            self.engine.library_manager.metadata_loading.load_library_metadata_from_file_request(
                                LoadLibraryMetadataFromFileRequest(file_path=library_info.library_path)
                            )
                        )

                        if isinstance(metadata_result, LoadLibraryMetadataFromFileResultFailure):
                            self._apply_metadata_load_failure(library_info, metadata_result)
                            return RegisterLibraryFromFileResultFailure(result_details=metadata_result.result_details)

                        library_data = metadata_result.library_schema
                        json_path = Path(file_path)
                        base_dir = json_path.parent.absolute()

                        # Add library directory and venv site-packages to sys.path
                        await self.engine.library_manager.environment.add_library_paths_to_sys_path(
                            library_data.name, file_path, base_dir
                        )

                        # Load the advanced library module if specified. Worker-delegated
                        # libraries skip this on the orchestrator, mirroring the venv/pip and
                        # node-import skips above and below: the module executes in this
                        # process, its third-party imports resolve against the library venv,
                        # and the orchestrator deliberately never created that venv -- so any
                        # manifest dependency it imports would raise ModuleNotFoundError and
                        # kill the registration. Nothing on the orchestrator invokes the
                        # advanced hooks for a worker library anyway (node loading, their only
                        # load-time caller, is skipped), and generate_new_library accepts
                        # advanced_library=None. The worker loads the module in its own
                        # process with the venv populated.
                        advanced_library_instance = None
                        skip_advanced_library = (
                            library_info.requires_worker and not self.engine.library_manager.is_worker
                        )
                        if skip_advanced_library and library_data.advanced_library_path:
                            logger.debug(
                                "Skipping Advanced Library load for worker-delegated library '%s' on the "
                                "orchestrator; the worker loads it in its own process.",
                                library_data.name,
                            )
                        elif library_data.advanced_library_path:
                            try:
                                advanced_library_instance = (
                                    self.engine.library_manager.module_loading.load_advanced_library_module(
                                        library_data=library_data,
                                        base_dir=base_dir,
                                    )
                                )
                            except Exception as err:
                                library_info.lifecycle_state = LibraryLifecycleState.FAILURE
                                library_info.fitness = LibraryFitness.UNUSABLE
                                library_info.problems.append(
                                    AdvancedLibraryLoadFailureProblem(
                                        advanced_library_path=library_data.advanced_library_path, error_message=str(err)
                                    )
                                )
                                self.engine.library_manager._library_file_path_to_info[file_path] = library_info
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
                            library_info.lifecycle_state = LibraryLifecycleState.FAILURE
                            library_info.fitness = LibraryFitness.UNUSABLE
                            library_info.problems.append(DuplicateLibraryProblem())
                            self.engine.library_manager._library_file_path_to_info[file_path] = library_info
                            details = f"Attempted to load Library JSON file from '{json_path}'. Failed because a Library '{library_data.name}' already exists. Error: {err}."
                            return RegisterLibraryFromFileResultFailure(result_details=details)

                        # Check the library's custom config settings
                        if library_data.settings is not None:
                            library_info.problems.extend(self._persist_library_settings(library_data))

                        library_info.problems.extend(
                            self._check_beta_feature_settings_collision(library_data.name, library)
                        )

                        # For worker-delegated libraries on the orchestrator, skip node module
                        # imports entirely -- importing them would pull heavy deps (torch, triton,
                        # etc.) into the orchestrator process.  The library is already registered
                        # in LibraryRegistry (for the editor and workflow loading); the worker
                        # process handles node loading and will report fitness via
                        # ReportLibraryLoadedRequest once it finishes.
                        if library_info.requires_worker and not self.engine.library_manager.is_worker:
                            library_info.fitness = LibraryFitness.NOT_EVALUATED
                            library_info.lifecycle_state = LibraryLifecycleState.WORKER_PENDING
                            self.engine.library_manager._library_file_path_to_info[file_path] = library_info
                        else:
                            # Attempt to load nodes from the library (modifies library_info in place).
                            await asyncio.to_thread(
                                self.engine.library_manager.module_loading.attempt_load_nodes_from_library,
                                library_data=library_data,
                                library=library,
                                base_dir=base_dir,
                                library_info=library_info,
                                lazy_loading=self.engine.library_manager.module_loading.should_lazy_load_nodes(),
                            )
                            self.engine.library_manager._library_file_path_to_info[file_path] = library_info

                        # A worker reports its load to the orchestrator, which never imported this
                        # library and so has no other account of how it went. Schemas travel only
                        # for a library the orchestrator cannot import: an exec-deps library already
                        # registered its real classes there, and probing every node class to build
                        # schemas it will discard costs a timeout apiece and can exceed 100 KB.
                        if (
                            self.engine.library_manager.is_worker
                            and library_info.lifecycle_state == LibraryLifecycleState.LOADED
                            and library_info.library_name
                        ):
                            node_schemas = (
                                await self.engine.library_manager.workers.serialize_library_node_schemas(
                                    library_info.library_name
                                )
                                if library_info.requires_worker
                                else None
                            )
                            await self.engine.library_manager.workers.report_library_loaded(
                                ReportLibraryLoadedRequest(
                                    library_name=library_info.library_name,
                                    fitness=library_info.fitness,
                                    problem_details=self.engine.library_manager.catalog.collate_problems_for_lib_info(
                                        library_info
                                    ),
                                    node_schemas=node_schemas,
                                )
                            )
                    else:
                        # SANDBOX LIBRARIES: Full processing here (discovery + registration)
                        # Load metadata from JSON file (already generated in DISCOVERED → METADATA_LOADED)
                        sandbox_directory = Path(library_info.library_path).parent
                        metadata_result = (
                            self.engine.library_manager.metadata_loading.load_library_metadata_from_file_request(
                                LoadLibraryMetadataFromFileRequest(file_path=library_info.library_path)
                            )
                        )

                        if isinstance(metadata_result, LoadLibraryMetadataFromFileResultFailure):
                            self._apply_metadata_load_failure(library_info, metadata_result)
                            return RegisterLibraryFromFileResultFailure(result_details=metadata_result.result_details)

                        # Add sandbox directory and venv site-packages to sys.path
                        library_data = metadata_result.library_schema
                        await self.engine.library_manager.environment.add_library_paths_to_sys_path(
                            library_data.name, library_info.library_path, sandbox_directory
                        )

                        # Discover real class names by importing files
                        await self.engine.library_manager.sandbox.attempt_generate_sandbox_library_from_schema(
                            library_schema=metadata_result.library_schema,
                            sandbox_directory=str(sandbox_directory),
                            library_info=library_info,
                        )
                        # Function handles registration and updates library_info with problems
                        # lifecycle_state set to LOADED by attempt_load_nodes_from_library

                    # Exit loop after final phase
                    break

                case _:
                    # Unexpected state
                    msg = f"Library '{library_info.library_name}' in unexpected lifecycle state: {current_state}"
                    raise ValueError(msg)

        # Success - progressed to LOADED state
        return None

    def _apply_metadata_load_failure(
        self,
        library_info: LibraryInfo,
        metadata_result: LoadLibraryMetadataFromFileResultFailure,
    ) -> None:
        """Mirror a metadata-load failure onto a LibraryInfo so it surfaces in status output.

        A failed metadata load (e.g. a top-level schema ValidationError) carries the real
        fitness, library name, version, and problem list. Without copying those onto the
        LibraryInfo, it stays at its pre-load defaults (NOT_EVALUATED fitness, no name, no
        version, empty problems) and renders as '*UNKNOWN* v*UNKNOWN* (PENDING) - No problems
        detected.' even though the load clearly failed. Record the failure's detail and mark
        the library FAILURE.
        """
        if library_info.library_name is None and metadata_result.library_name is not None:
            library_info.library_name = metadata_result.library_name
        if library_info.library_version is None and metadata_result.library_version is not None:
            library_info.library_version = metadata_result.library_version
        library_info.fitness = metadata_result.status
        library_info.problems.extend(metadata_result.problems)
        library_info.lifecycle_state = LibraryLifecycleState.FAILURE
        self.engine.library_manager._library_file_path_to_info[library_info.library_path] = library_info

    def _check_beta_feature_settings_collision(self, library_name: str, library: Library) -> list[LibraryProblem]:
        """Report another loaded library that would store its beta feature choices in the same place.

        Runs after registration, because the fitness check runs before any library is registered
        and so can't see the others. Only libraries that both declare beta features can collide.
        """
        if not library.get_beta_features():
            return []

        other_names_with_features = [
            name
            for name in LibraryRegistry.list_libraries()
            if name != library_name and LibraryRegistry.get_library(name).get_beta_features()
        ]
        other_name = find_library_config_slug_collision(library_name, other_names_with_features)
        if other_name is None:
            return []

        config_key = f"{LIBRARY_BETA_FEATURES_KEY}.{library_config_slug(library_name)}"
        return [BetaFeatureSettingsCollisionProblem(other_library_name=other_name, config_key=config_key)]

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
        config_mgr = self.engine.config_manager
        for library_data_setting in library_data.settings:
            get_category_request = GetConfigCategoryRequest(
                category=library_data_setting.category,
                failure_log_level=logging.DEBUG,
            )
            get_category_result = self.engine.handle_request(get_category_request)
            if not isinstance(get_category_result, GetConfigCategoryResultSuccess):
                # Create new category
                create_new_category_request = SetConfigCategoryRequest(
                    category=library_data_setting.category, contents=library_data_setting.contents
                )
                create_new_category_result = self.engine.handle_request(create_new_category_request)
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
            set_category_result = self.engine.handle_request(set_category_request)
            if not isinstance(set_category_result, SetConfigCategoryResultSuccess):
                problems.append(UpdateConfigCategoryProblem(category_name=library_data_setting.category))
                details = f"Failed attempting to update config category '{library_data_setting.category}' for library '{library_data.name}'."
                logger.error(details)

        return problems
