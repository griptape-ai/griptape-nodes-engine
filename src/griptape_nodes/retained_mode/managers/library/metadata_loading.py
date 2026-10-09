from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, cast

from pydantic import ValidationError

from griptape_nodes.node_library.library_registry import (
    LibraryRegistry,
    LibrarySchema,
)
from griptape_nodes.node_library.library_validation import (
    detect_retired_node_declarations,
    validate_library_declarations,
)
from griptape_nodes.retained_mode.engine import EngineScoped
from griptape_nodes.retained_mode.events.library_events import (
    LoadLibraryMetadataFromFileRequest,
    LoadLibraryMetadataFromFileResultFailure,
    LoadLibraryMetadataFromFileResultSuccess,
    LoadMetadataForAllLibrariesRequest,
    LoadMetadataForAllLibrariesResultSuccess,
    ScanSandboxDirectoryRequest,
    ScanSandboxDirectoryResultSuccess,
)
from griptape_nodes.retained_mode.managers.fitness_problems.libraries import (
    InvalidVersionStringProblem,
    LibraryJsonDecodeProblem,
    LibraryLoadExceptionProblem,
    LibraryNotFoundProblem,
    LibrarySchemaExceptionProblem,
    LibrarySchemaValidationProblem,
)
from griptape_nodes.retained_mode.managers.library.common import LIBRARY_CONFIG_FILENAME, LibraryFitness
from griptape_nodes.retained_mode.request_handlers import handles
from griptape_nodes.utils.git_utils import (
    get_git_info,
)

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.events.base_events import ResultPayload
    from griptape_nodes.retained_mode.managers.event_manager import EventManager


def is_library_name_registered(library_name: str) -> bool:
    """Whether a library of this name is in the registry right now.

    Metadata is derived from config while the registry holds what actually loaded, so the
    two can disagree until libraries reload. Keyed on name because the requests a client
    makes off the back of metadata (CheckLibraryUpdate, GetAllInfoForLibrary) are
    name-keyed: if the name resolves, those calls succeed regardless of which copy loaded.
    """
    return library_name in LibraryRegistry.list_libraries()


class LibraryMetadataLoading(EngineScoped):
    def __init__(self, event_manager: EventManager, *, engine: Engine | None = None) -> None:
        super().__init__(engine)
        event_manager.register_request_handlers(self)

    @handles(LoadLibraryMetadataFromFileRequest)
    def load_library_metadata_from_file_request(  # noqa: PLR0911, C901
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
                status=LibraryFitness.MISSING,
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
                status=LibraryFitness.UNUSABLE,
                problems=[LibraryJsonDecodeProblem()],
                result_details=details,
            )
        except Exception as err:
            details = f"Attempted to load Library JSON file from location '{json_path}'. Failed because an exception occurred: {err}"
            return LoadLibraryMetadataFromFileResultFailure(
                library_path=file_path,
                library_name=None,
                status=LibraryFitness.UNUSABLE,
                problems=[LibraryLoadExceptionProblem(error_message=str(err))],
                result_details=details,
            )

        # Try to extract library name from JSON for better error reporting
        library_name = library_json.get("name") if isinstance(library_json, dict) else None

        # Extract the declared version straight from the raw JSON too, so a library whose
        # schema fails to validate can still report its version in status output instead of
        # showing '*UNKNOWN*'.
        raw_metadata = library_json.get("metadata") if isinstance(library_json, dict) else None
        raw_version = raw_metadata.get("library_version") if isinstance(raw_metadata, dict) else None
        raw_library_version = raw_version if isinstance(raw_version, str) else None

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
                    status=LibraryFitness.UNUSABLE,
                    problems=retired_problems,
                    library_version=raw_library_version,
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
                status=LibraryFitness.UNUSABLE,
                problems=problems,
                library_version=raw_library_version,
                result_details=details,
            )
        except Exception as err:
            details = f"Attempted to load Library JSON file. Failed because the file at path '{json_path}' failed to match the library schema due to: {err}"
            return LoadLibraryMetadataFromFileResultFailure(
                library_path=file_path,
                library_name=library_name,
                status=LibraryFitness.UNUSABLE,
                problems=[LibrarySchemaExceptionProblem(error_message=str(err))],
                library_version=raw_library_version,
                result_details=details,
            )

        # Make sure the version string is copacetic.
        library_version = library_data.metadata.library_version
        if library_version is None:
            details = f"Attempted to load Library '{library_data.name}' JSON file from '{json_path}'. Failed because version string '{library_data.metadata.library_version}' wasn't valid. Must be in major.minor.patch format."
            return LoadLibraryMetadataFromFileResultFailure(
                library_path=file_path,
                library_name=library_data.name,
                status=LibraryFitness.UNUSABLE,
                problems=[InvalidVersionStringProblem(version_string=str(library_data.metadata.library_version))],
                library_version=raw_library_version,
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
                status=LibraryFitness.UNUSABLE,
                problems=list(declaration_problems),
                library_version=raw_library_version,
                result_details=details,
            )

        # Use get_git_info (not get_git_remote + get_current_ref) to open the repo once
        # instead of three times — this is called for every library on every metadata load.
        library_dir = json_path.parent.absolute()
        git_remote, git_ref = get_git_info(library_dir)

        existing_info = self.engine.library_manager._library_file_path_to_info.get(file_path)
        enabled = existing_info.enabled if existing_info is not None else True
        details = f"Successfully loaded library metadata from JSON file at {json_path}"
        return LoadLibraryMetadataFromFileResultSuccess(
            library_schema=library_data,
            file_path=file_path,
            git_remote=git_remote,
            git_ref=git_ref,
            enabled=enabled,
            is_registered=is_library_name_registered(library_data.name),
            result_details=details,
        )

    @handles(LoadMetadataForAllLibrariesRequest)
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
        library_files = await self.engine.library_manager.discovery.discover_library_files()
        environment_mode = self.engine.library_manager.managed_environment.provisioned_by_environment()

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
                # is_registered is answered by library name, so a configured copy the environment
                # refused would read as loaded whenever the environment provides a library of the
                # same name. Only the environment's own entry can be the one that loaded.
                if environment_mode and not discovered.from_environment:
                    metadata_result.is_registered = False
                self._stamp_load_outcome(metadata_result)
                successful_libraries.append(metadata_result)
            else:
                failed_libraries.append(cast("LoadLibraryMetadataFromFileResultFailure", metadata_result))

        # Generate sandbox library metadata if configured. Not when the sandbox is turned off: it
        # never loads then, and scanning it writes its manifest.
        sandbox_library_dir = None
        if self.engine.library_manager.managed_environment.sandbox_enabled():
            sandbox_library_dir = self.engine.library_manager.sandbox.get_sandbox_directory()
        if sandbox_library_dir:
            # Try to load existing JSON first - only scan if load fails
            sandbox_json_path = sandbox_library_dir / LIBRARY_CONFIG_FILENAME
            sandbox_result = self.load_library_metadata_from_file_request(
                LoadLibraryMetadataFromFileRequest(file_path=str(sandbox_json_path))
            )

            # If load failed, it either didn't exist or was malformed. Try scanning, which will generate a fresh one.
            if isinstance(sandbox_result, LoadLibraryMetadataFromFileResultFailure):
                scan_result = self.engine.library_manager.sandbox.scan_sandbox_directory_request(
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
                        is_registered=is_library_name_registered(scan_result.library_schema.name),
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

    def _stamp_load_outcome(self, metadata_result: LoadLibraryMetadataFromFileResultSuccess) -> None:
        """Copy the engine's record of the last load attempt for this file onto its metadata.

        A manifest that parses says nothing about whether the library then loaded: one whose
        dependency install failed reads exactly like one added since the last refresh, unless the
        record of the attempt travels with it.
        """
        library_info = self.engine.library_manager._library_file_path_to_info.get(metadata_result.file_path)
        if library_info is None:
            return
        metadata_result.lifecycle_state = library_info.lifecycle_state.value
        metadata_result.fitness = library_info.fitness.value
        metadata_result.problems = self.engine.library_manager.catalog.collate_problems_for_lib_info(library_info)
        metadata_result.execution_env_failure = library_info.execution_env_failure
