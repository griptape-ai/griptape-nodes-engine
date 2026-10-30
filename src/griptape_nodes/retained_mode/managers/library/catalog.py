from __future__ import annotations

import asyncio
import copy
import hashlib
import importlib.abc
import importlib.machinery
import importlib.util
import logging
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

from griptape_nodes.node_library.library_registry import (
    LibraryRegistry,
    LibraryRegistryError,
    WidgetDefinition,
)
from griptape_nodes.retained_mode.engine import EngineScoped

# Runtime imports for ResultDetails since it's used at runtime
from griptape_nodes.retained_mode.events.base_events import ResultDetail, ResultDetails
from griptape_nodes.retained_mode.events.library_events import (
    DescribeNodeTypeRequest,
    DescribeNodeTypeResultFailure,
    DescribeNodeTypeResultSuccess,
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
    LibraryEventHandlerDetails,
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
    ParameterDescription,
    WidgetInfo,
)
from griptape_nodes.retained_mode.events.payload_registry import PayloadRegistry
from griptape_nodes.retained_mode.managers.fitness_problems.libraries import (
    NodeModuleImportProblem,
)
from griptape_nodes.retained_mode.managers.library.node_type_specs import NodeTypeSpecCache, trim_parameters
from griptape_nodes.retained_mode.request_handlers import handles

if TYPE_CHECKING:
    from collections.abc import Sequence

    from griptape_nodes.exe_types.core_types import Parameter
    from griptape_nodes.node_library.library_registry import Library, NodeMetadata
    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.events.base_events import ResultPayload
    from griptape_nodes.retained_mode.managers.event_manager import EventManager
    from griptape_nodes.retained_mode.managers.library.common import LibraryInfo

logger = logging.getLogger("griptape_nodes")


class LibraryCatalog(EngineScoped):
    def __init__(self, event_manager: EventManager, *, engine: Engine | None = None) -> None:
        super().__init__(engine)
        self._node_type_specs = NodeTypeSpecCache()
        event_manager.register_request_handlers(self)

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
        library_info = self.engine.library_manager.get_library_info_by_library_name(library_name)
        if library_info is None:
            return None
        return self.collate_problems_for_lib_info(library_info)

    def get_library_name_for_node_type(self, node_type: str) -> str | None:
        """The library that provides a node type, or None when it cannot be pinned to one.

        A node type several libraries provide has no single owner. A node type no library provides
        may still be one whose module failed to import, which registers nothing but does record the
        failure against the library that owns the node file.

        Args:
            node_type: Node type to find the providing library for
        """
        try:
            library = LibraryRegistry.get_library_for_node_type(node_type)
        except LibraryRegistryError:
            return self._library_name_reporting_node_import_failure(node_type)
        return library.get_library_data().name

    def explain_stale_module_failure(self, library_name: str) -> str | None:
        """An artist-facing explanation for an import failure caused by a mid-session reload.

        Returns None when this library has not been reloaded after its modules were imported, in
        which case an import failure means the library itself is broken and needs no restart.

        Args:
            library_name: Name of the library whose node failed to load
        """
        if not self._was_reloaded_after_its_modules_were_imported(library_name):
            return None

        return (
            f"Library '{library_name}' was reloaded after this engine had already loaded its nodes, "
            f"so the engine is still running the code it loaded first. Restart the engine to pick up "
            f"the new code."
        )

    def explain_restart_after_reload(self, library_name: str) -> str | None:
        """The restart explanation to report after reloading a library, or None when it took cleanly.

        Only speaks up when the reload actually failed to import a node module. A library whose
        nodes took the new code needs no restart, and warning on every reload would train artists
        to ignore the message.

        Args:
            library_name: Name of the library that was just reloaded
        """
        if not self._library_has_node_import_problems(library_name):
            return None
        return self.explain_stale_module_failure(library_name)

    @handles(ListCapableLibraryEventHandlersRequest)
    def on_list_capable_event_handlers(self, request: ListCapableLibraryEventHandlersRequest) -> ResultPayload:
        """Get all registered event handlers for a specific request type."""
        request_type = PayloadRegistry.get_type(request.request_type)
        if request_type is None:
            details = f"Request type '{request.request_type}' is not registered in the PayloadRegistry."
            return ListCapableLibraryEventHandlersResultFailure(exception=KeyError(details), result_details=details)
        handler_mappings = self.engine.library_manager.get_registered_event_handlers(request_type)
        # Surface any presentation metadata a library registered alongside its handler
        # (currently PublishWorkflowRegisteredEventData's display_name/description/icon)
        # so a frontend can render richer menu entries. Read the fields defensively:
        # event_data may be None or a type without these attributes, in which case the
        # handler still gets an entry keyed only by its library name.
        handler_details = [
            LibraryEventHandlerDetails(
                library_name=library_name,
                display_name=getattr(registered.event_data, "display_name", None),
                description=getattr(registered.event_data, "description", None),
                icon=getattr(registered.event_data, "icon", None),
            )
            for library_name, registered in handler_mappings.items()
        ]
        return ListCapableLibraryEventHandlersResultSuccess(
            handlers=list(handler_mappings.keys()),
            handler_details=handler_details,
            result_details=f"Successfully listed {len(handler_mappings)} capable library event handlers",
        )

    @handles(ListRegisteredLibrariesRequest)
    async def on_list_registered_libraries_request(self, _request: ListRegisteredLibrariesRequest) -> ResultPayload:
        await self.engine.library_manager._libraries_loading_complete.wait()
        # Make a COPY of the list
        snapshot_list = LibraryRegistry.list_libraries()
        event_copy = snapshot_list.copy()

        details = "Successfully retrieved the list of registered libraries."

        result = ListRegisteredLibrariesResultSuccess(
            libraries=event_copy,
            result_details=details,
        )
        return result

    @handles(GetLibrarySourceInfoRequest)
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
        await self.engine.library_manager._libraries_loading_complete.wait()
        lib_info = self.engine.library_manager.get_library_info_by_library_name(request.library)
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

    @handles(GetEngineSourceInfoRequest)
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

    @handles(ListNodeTypesInLibraryRequest)
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

        # Registered metadata only, so node modules that deferred registration has not imported stay unimported.
        node_metadata = None
        if request.include_metadata:
            node_metadata = {node_type: library.get_node_metadata(node_type) for node_type in event_copy}

        details = f"Successfully retrieved the list of node types in the Library named '{request.library}'."

        result = ListNodeTypesInLibraryResultSuccess(
            node_types=event_copy,
            node_metadata=node_metadata,
            result_details=details,
        )
        return result

    @handles(GetLibraryMetadataRequest)
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

    @handles(GetNodeMetadataFromLibraryRequest)
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

    @handles(DescribeNodeTypeRequest)
    def describe_node_type_request(self, request: DescribeNodeTypeRequest) -> ResultPayload:
        resolved = self._resolve_node_type(request.node_type, request.library)
        if isinstance(resolved, _NodeTypeLookupFailure):
            return DescribeNodeTypeResultFailure(result_details=resolved.details)

        # A request that leaves something out is for planning, so it is served from the cache. One
        # with everything included is read fresh, for dropdowns built from the machine it runs on.
        includes_everything = (
            request.include_metadata
            and request.include_tooltips
            and request.include_ui_options
            and request.include_exec_pins
        )
        if includes_everything:
            parameters = self._fresh_parameters(resolved)
        else:
            parameters = self._cached_parameters(resolved)
        if isinstance(parameters, _ParameterProbeFailure):
            return self._described_without_parameters(resolved, parameters, include_metadata=request.include_metadata)

        metadata = None
        if request.include_metadata:
            metadata = resolved.metadata
        trimmed = trim_parameters(
            parameters,
            include_tooltips=request.include_tooltips,
            include_ui_options=request.include_ui_options,
            include_exec_pins=request.include_exec_pins,
        )
        details = f"Successfully described node type '{request.node_type}' in Library '{resolved.library_name}'."
        return DescribeNodeTypeResultSuccess(
            library=resolved.library_name,
            node_type=request.node_type,
            metadata=metadata,
            parameters=trimmed.parameters,
            exec_pins=trimmed.exec_pins,
            result_details=details,
        )

    @handles(ListCategoriesInLibraryRequest)
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

    async def get_all_info_for_all_libraries_request(self, request: GetAllInfoForAllLibrariesRequest) -> ResultPayload:  # noqa: ARG002
        libraries = LibraryRegistry.list_libraries()

        try:
            # Each library's info is independent, and the per-library handler blocks on
            # reading widget bundles, so gather rather than walking them one at a time.
            library_all_info_results = await asyncio.gather(
                *(
                    self.get_all_info_for_library_request(GetAllInfoForLibraryRequest(library=library_name))
                    for library_name in libraries
                )
            )
        except Exception as err:
            details = f"Attempted to get all info for all libraries. Encountered error {err}."
            return GetAllInfoForAllLibrariesResultFailure(result_details=details)

        # Create a mapping of library name to all its info.
        library_name_to_all_info = {}
        for library_name, library_all_info_result in zip(libraries, library_all_info_results, strict=True):
            if not library_all_info_result.succeeded():
                details = f"Attempted to get all info for all libraries, but failed when getting all info for library named '{library_name}'."
                return GetAllInfoForAllLibrariesResultFailure(result_details=details)

            library_name_to_all_info[library_name] = cast("GetAllInfoForLibraryResultSuccess", library_all_info_result)

        # We're home free now
        details = "Successfully retrieved all info for all libraries."
        result = GetAllInfoForAllLibrariesResultSuccess(
            library_name_to_library_info=library_name_to_all_info, result_details=details
        )
        return result

    @handles(GetAllInfoForAllLibrariesRequest)
    async def on_get_all_info_for_all_libraries_request(
        self, request: GetAllInfoForAllLibrariesRequest
    ) -> ResultPayload:
        """Registered entry point: hold the caller until any in-flight reload finishes."""
        await self.engine.library_manager._libraries_loading_complete.wait()
        return await self.get_all_info_for_all_libraries_request(request)

    @handles(GetAllInfoForLibraryRequest)
    async def on_get_all_info_for_library_request(self, request: GetAllInfoForLibraryRequest) -> ResultPayload:
        """Registered entry point: hold the caller until any in-flight reload finishes.

        Without the wait, a request arriving mid-reload reports the library as
        unregistered even though the reload re-registers it moments later.

        The gate is held here rather than inside the handler so an internal caller cannot
        wait on a reload it is running inside.
        """
        await self.engine.library_manager._libraries_loading_complete.wait()
        return await self.get_all_info_for_library_request(request)

    async def get_all_info_for_library_request(self, request: GetAllInfoForLibraryRequest) -> ResultPayload:  # noqa: PLR0911
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

        # Build widget info list if the library has widgets. Hashing reads every bundle
        # off disk, the only blocking work in this handler, so it runs in a thread.
        widgets_info: list[WidgetInfo] | None = None
        library_data = library.get_library_data()
        if library_data.widgets:
            widgets_info = await asyncio.to_thread(self._build_widget_info, request.library, library_data.widgets)

        details = f"Successfully got all library info for a Library named '{request.library}'."
        result = GetAllInfoForLibraryResultSuccess(
            library_metadata_details=library_metadata_result_success,
            category_details=list_categories_result_success,
            node_type_name_to_node_metadata_details=node_type_name_to_node_metadata_details,
            widgets=widgets_info,
            result_details=details,
        )
        return result

    def _described_without_parameters(
        self, resolved: _ResolvedNodeType, failure: _ParameterProbeFailure, *, include_metadata: bool
    ) -> DescribeNodeTypeResultSuccess:
        """A description without parameters, for a node type whose parameters could not be read.

        Still a success: the WARNING entry tells callers why the parameters are missing, and the
        library metadata is there when the request asked for it.
        """
        metadata = None
        summary = "without its parameters"
        if include_metadata:
            metadata = resolved.metadata
            summary = "with library metadata only"
        return DescribeNodeTypeResultSuccess(
            library=resolved.library_name,
            node_type=resolved.node_type,
            metadata=metadata,
            parameters=[],
            result_details=ResultDetails(
                ResultDetail(
                    level=logging.INFO,
                    message=f"Described node type '{resolved.node_type}' in Library '{resolved.library_name}' {summary}.",
                ),
                ResultDetail(level=logging.WARNING, message=failure.describe_warning),
            ),
        )

    def _fresh_parameters(self, resolved: _ResolvedNodeType) -> list[ParameterDescription] | _ParameterProbeFailure:
        """A node type's parameter descriptions, read from a new probe instance."""
        probe = self._probe_parameters(resolved)
        if isinstance(probe, _ParameterProbeFailure):
            return probe
        return [ParameterDescription.from_parameter(param) for param in probe]

    def _cached_parameters(self, resolved: _ResolvedNodeType) -> list[ParameterDescription] | _ParameterProbeFailure:
        """A node type's parameter descriptions, from the cache when it holds them.

        Returns a copy, so a caller changing a description (a default value, say) cannot change
        what later requests receive.
        """
        parameters = self._node_type_specs.get(resolved.library_name, resolved.node_type)
        if parameters is None:
            built = self._build_cached_parameters(resolved)
            if isinstance(built, _ParameterProbeFailure):
                return built
            parameters = built
        return copy.deepcopy(parameters)

    def _build_cached_parameters(
        self, resolved: _ResolvedNodeType
    ) -> list[ParameterDescription] | _ParameterProbeFailure:
        """Probe a node type, describe its parameters, and keep the descriptions in the cache.

        Failures are not cached: the cause (a missing dependency, a failed network call, a missing
        API key) may be fixed without any library reloading.
        """
        # Read before probing: descriptions built across a registration change are not kept.
        generation = LibraryRegistry.get_generation()
        probe = self._probe_parameters(resolved)
        if isinstance(probe, _ParameterProbeFailure):
            return probe

        # Reading a parameter runs library code (trait ui_options, for one), and cached descriptions
        # are copied out on every request, so a default that cannot be copied has to fail here, as a warning.
        try:
            parameters = copy.deepcopy([ParameterDescription.from_parameter(param) for param in probe])
        except Exception as err:
            read_error = f"{type(err).__name__}: {err}"
            return _ParameterProbeFailure(describe_warning=f"Parameter read failed because: {read_error}")

        self._node_type_specs.put(resolved.library_name, resolved.node_type, parameters, generation=generation)
        return parameters

    def _resolve_node_type(
        self, node_type: str, library_name: str | None
    ) -> _ResolvedNodeType | _NodeTypeLookupFailure:
        """Find the library and metadata for a node type.

        Args:
            node_type: Name of the node type
            library_name: Library to look in. When None, the one library that registers the node type.
        """
        # When no library is supplied, rely on LibraryRegistry to pick the unique library that provides it.
        try:
            library = LibraryRegistry.get_library_for_node_type(node_type=node_type, specific_library_name=library_name)
        except KeyError as err:
            details = (
                f"Attempted to describe node type '{node_type}'. Failed when looking up its library because: {err}"
            )
            return _NodeTypeLookupFailure(details=details)

        resolved_library_name = library.get_library_data().name

        # Make sure the node type really is registered in this library before going further.
        try:
            node_metadata = library.get_node_metadata(node_type=node_type)
        except KeyError:
            details = f"Attempted to describe node type '{node_type}' in Library '{resolved_library_name}'. Failed because the Library has no node type with that name."
            return _NodeTypeLookupFailure(details=details)

        return _ResolvedNodeType(
            library=library,
            library_name=resolved_library_name,
            node_type=node_type,
            metadata=node_metadata,
        )

    def _probe_parameters(self, resolved: _ResolvedNodeType) -> list[Parameter] | _ParameterProbeFailure:
        """The parameters a node type's __init__ declares, read from a throwaway instance.

        The node is never added to a flow or the ObjectManager, so it is garbage-collected once its
        parameters are no longer referenced.

        Nodes whose __init__ performs I/O (network calls, auth checks, disk reads) can raise, and
        resolving the class imports the node's module, which deferred registration put off until
        first use, so a broken module raises here rather than at load. Either is returned as a
        failure so callers can still present the node type's library metadata.
        """
        try:
            node_class = resolved.library.get_node_class(resolved.node_type)
        except (ImportError, AttributeError, TypeError) as err:
            import_error = f"{type(err).__name__}: {err}"
            return _ParameterProbeFailure(describe_warning=f"Node module failed to import: {import_error}")

        probe_name = f"__describe_node_type_probe__{resolved.node_type}"
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
            with LibraryRegistry.constructing_node(throwaway=True):
                probe_node = node_class(
                    name=probe_name,
                    metadata={"library": resolved.library_name, "node_type": resolved.node_type},
                )
        except Exception as err:
            probe_error = f"{type(err).__name__}: {err}"
            return _ParameterProbeFailure(describe_warning=f"Parameter probe failed because: {probe_error}")

        return probe_node.parameters

    def _library_name_reporting_node_import_failure(self, node_type: str) -> str | None:
        """The library that recorded an import failure for a node type, or None when none did.

        A node type whose module failed to import registers nothing, so the recorded failure is the
        only thing that can name its library.

        Args:
            node_type: Class name of the node type to look for
        """
        for library_info in self.engine.library_manager._library_file_path_to_info.values():
            for problem in library_info.problems:
                if isinstance(problem, NodeModuleImportProblem) and problem.class_name == node_type:
                    return library_info.library_name
        return None

    def _was_reloaded_after_its_modules_were_imported(self, library_name: str) -> bool:
        """Whether this library was reloaded in this session after its node modules had loaded.

        See `_libraries_reloaded_after_import` for why that leaves this process on the old code.

        Args:
            library_name: Name of the library to ask about
        """
        return library_name in self.engine.library_manager._libraries_reloaded_after_import

    def _library_has_node_import_problems(self, library_name: str) -> bool:
        """Whether a library's last load recorded any node module that failed to import.

        Args:
            library_name: Name of the library to ask about
        """
        library_info = self.engine.library_manager.get_library_info_by_library_name(library_name)
        if library_info is None:
            return False
        return any(isinstance(problem, NodeModuleImportProblem) for problem in library_info.problems)

    def _build_widget_info(self, library_name: str, widget_defs: Sequence[WidgetDefinition]) -> list[WidgetInfo]:
        """Resolve each widget bundle to a cache-busting URL.

        Blocking: reads and hashes every bundle file. Callers run this off the event loop.
        """
        logger.debug("Library '%s' has %d widget(s), building widget info", library_name, len(widget_defs))
        # Get the static server base URL for constructing absolute bundle URLs
        static_server_base_url = self.engine.static_files_manager.static_server_base_url
        # Get the library directory so we can hash each bundle file
        library_info_for_path = self.engine.library_manager.get_library_info_by_library_name(library_name)
        library_dir = Path(library_info_for_path.library_path).parent if library_info_for_path is not None else None

        widgets_info = []
        for widget_def in widget_defs:
            # Construct the full URL for this widget
            # The frontend will fetch from: {static_server_base_url}/api/libraries/{library_name}/widgets/{path}
            base_url = f"{static_server_base_url}/api/libraries/{library_name}/widgets/{widget_def.path}"
            # Append a content hash so browsers re-fetch when the bundle file changes
            try:
                if library_dir is not None:
                    content_hash = hashlib.sha256((library_dir / widget_def.path).read_bytes()).hexdigest()[:8]
                    bundle_url = f"{base_url}?v={content_hash}"
                else:
                    bundle_url = base_url
            except OSError:
                bundle_url = base_url
            logger.debug("Widget '%s' from library '%s': bundle_url=%s", widget_def.name, library_name, bundle_url)
            widgets_info.append(
                WidgetInfo(
                    name=widget_def.name,
                    bundle_url=bundle_url,
                    description=widget_def.description,
                )
            )
        return widgets_info


@dataclass
class _ResolvedNodeType:
    """A node type found in a registered library."""

    library: Library
    library_name: str
    node_type: str
    metadata: NodeMetadata


@dataclass
class _NodeTypeLookupFailure:
    """A node type no registered library could be pinned down as providing."""

    details: str


@dataclass
class _ParameterProbeFailure:
    """A node type whose parameters could not be read, because its module or __init__ raised.

    Args:
        describe_warning: The WARNING DescribeNodeTypeRequest reports
    """

    describe_warning: str
