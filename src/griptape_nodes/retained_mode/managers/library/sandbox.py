from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from itertools import groupby
from pathlib import Path
from typing import TYPE_CHECKING

from griptape_nodes.exe_types.node_types import BaseNode
from griptape_nodes.files.path_utils import (
    canonicalize_for_identity,
    canonicalize_for_identity_preserving_symlinks,
    canonicalize_for_io,
    relative_to_keeping_or_following_links,
    resolve_workspace_path,
)
from griptape_nodes.node_library.library_registry import (
    CategoryDefinition,
    LibraryMetadata,
    LibraryRegistry,
    LibrarySchema,
    NodeDefinition,
    NodeMetadata,
    WorkflowNodeDefinition,
)
from griptape_nodes.node_library.workflow_registry import (
    WorkflowMetadata,
    WorkflowMetadataError,
    WorkflowMetadataSectionCountError,
    read_workflow_metadata,
)
from griptape_nodes.retained_mode.engine import EngineScoped
from griptape_nodes.retained_mode.events.app_events import (
    GetEngineVersionRequest,
    GetEngineVersionResultSuccess,
)

# Runtime imports for ResultDetails since it's used at runtime
from griptape_nodes.retained_mode.events.base_events import ResultDetail, ResultDetails
from griptape_nodes.retained_mode.events.library_events import (
    LoadLibraryMetadataFromFileRequest,
    LoadLibraryMetadataFromFileResultFailure,
    LoadLibraryMetadataFromFileResultSuccess,
    RegisterSandboxNodeFromSourceRequest,
    RegisterSandboxNodeFromSourceResultFailure,
    RegisterSandboxNodeFromSourceResultSuccess,
    ScanSandboxDirectoryRequest,
    ScanSandboxDirectoryResultFailure,
    ScanSandboxDirectoryResultSuccess,
)
from griptape_nodes.retained_mode.events.os_events import (
    WriteFileRequest,
)
from griptape_nodes.retained_mode.managers.fitness_problems.libraries import (
    DuplicateLibraryProblem,
    DuplicateNodeRegistrationProblem,
    EngineVersionErrorProblem,
    NodeModuleImportProblem,
    SandboxDirectoryMissingProblem,
    WorkflowNodeLoadProblem,
)
from griptape_nodes.retained_mode.managers.library.common import (
    LIBRARY_CONFIG_FILENAME,
    LibraryFitness,
    LibraryInfo,
    LibraryLifecycleState,
)
from griptape_nodes.retained_mode.managers.library.metadata_loading import is_library_name_registered
from griptape_nodes.retained_mode.managers.library.module_loading import get_root_cause_from_exception
from griptape_nodes.retained_mode.request_handlers import handles

if TYPE_CHECKING:
    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.events.base_events import ResultPayload
    from griptape_nodes.retained_mode.managers.event_manager import EventManager

logger = logging.getLogger("griptape_nodes")


SANDBOX_LIBRARY_NAME = "Sandbox Library"

# Sandbox library constants
UNRESOLVED_SANDBOX_CLASS_NAME = "<NOT YET RESOLVED>"
SANDBOX_CATEGORY_NAME = "Griptape Nodes Sandbox"

# Icon for the workflow-backed nodes that saved workflows in the sandbox become
SUBFLOW_NODE_ICON = "Layers"

# Prepended when a workflow's name cannot start a node type on its own (e.g. "3d_scan").
SUBFLOW_NODE_TYPE_FALLBACK_PREFIX = "Subflow"

# Directories to exclude when scanning for Python source files (in addition to any directory starting with '.')
EXCLUDED_SCAN_DIRECTORIES = frozenset({"venv", "__pycache__"})


@dataclass
class SandboxCandidates:
    """The sandbox's scanned files.

    Split into node source to import and saved workflows to build as nodes.
    """

    node_source_definitions: list[NodeDefinition] = field(default_factory=list)
    workflow_node_definitions: list[WorkflowNodeDefinition] = field(default_factory=list)
    problems: list[WorkflowNodeLoadProblem] = field(default_factory=list)


class LibrarySandbox(EngineScoped):
    def __init__(self, event_manager: EventManager, *, engine: Engine | None = None) -> None:
        super().__init__(engine)
        event_manager.register_request_handlers(self)

    def get_sandbox_directory(self) -> Path | None:
        """Get the configured sandbox directory path.

        Returns:
            Path to sandbox directory if configured and exists, None otherwise.
        """
        config_mgr = self.engine.config_manager
        sandbox_library_subdir = config_mgr.get_config_value("sandbox_library_directory")
        if not sandbox_library_subdir:
            return None

        # Links are kept, not resolved: the workspace scans name files by the link's path, so
        # resolving a linked sandbox would name the same files a second way.
        sandbox_library_dir = canonicalize_for_identity_preserving_symlinks(
            sandbox_library_subdir, base=config_mgr.workspace_path
        )
        if not sandbox_library_dir.exists():
            # Log the setting and the path it became, to expose a typo or an unset variable.
            # A leading `$` never gets here: the config manager reads `$MY_VAR/sub` as a secret
            # named `MY_VAR_SUB`.
            logger.debug(
                "The sandbox library directory '%s' does not exist at '%s', so no sandbox is loaded.",
                sandbox_library_subdir,
                sandbox_library_dir,
            )
            return None

        return sandbox_library_dir

    @handles(ScanSandboxDirectoryRequest)
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

    @handles(RegisterSandboxNodeFromSourceRequest)
    def register_sandbox_node_from_source_request(  # noqa: C901, PLR0911
        self, request: RegisterSandboxNodeFromSourceRequest
    ) -> ResultPayload:
        """Register node types from a `.py` file in the sandbox dir.

        A file with a workflow header is a saved workflow. It is never imported, and becomes one
        workflow-backed node built the same way the sandbox load builds it. Any other file is
        Python node source and is handled with existing engine primitives end to end:
          * `get_sandbox_directory` resolves the configured path.
          * `load_module_from_file` imports the source (with the existing hot-reload
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
        sandbox_dir = self.get_sandbox_directory()
        if sandbox_dir is None:
            details = (
                "Attempted to register a sandbox node from source. Failed because "
                "`sandbox_library_directory` is not configured (or the configured path does "
                "not exist). Set it in Settings -> Libraries -> Sandbox Settings first."
            )
            return RegisterSandboxNodeFromSourceResultFailure(result_details=details)

        # Canonicalize the requested path against the sandbox dir. Relative paths anchor to
        # the sandbox; absolute paths stay where they are. We then verify the result lives
        # under the sandbox dir so callers can never reach outside it (links kept, then
        # followed; see the helper).
        sandbox_root = canonicalize_for_identity_preserving_symlinks(sandbox_dir)
        file_path = canonicalize_for_io(request.file_path, base=sandbox_dir)
        file_identity = canonicalize_for_identity_preserving_symlinks(request.file_path, base=sandbox_dir)
        # Unlike the paths around it, the import uses the resolved path (as the sandbox load and
        # library loader do): the module name comes from the path, so one file is one module.
        resolved_file = resolve_workspace_path(file_identity, sandbox_dir)
        if relative_to_keeping_or_following_links(request.file_path, sandbox_dir, base=sandbox_dir) is None:
            details = (
                f"Attempted to register a sandbox node with file_path={request.file_path!r}. "
                f"Failed because the path '{file_identity}' is not inside the "
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
            # Display file_identity, not file_path: on Windows the latter now carries the
            # \\?\ long-path prefix (canonicalize_for_io applies it unconditionally), which
            # is confusing in a user-facing message. file_identity is the un-prefixed form.
            details = (
                f"Attempted to register a sandbox node with file_path={request.file_path!r}. "
                f"Failed because no file exists at the path '{file_identity}'. Write "
                "the source file into the sandbox directory before calling this request."
            )
            return RegisterSandboxNodeFromSourceResultFailure(result_details=details)

        # Saved workflows are never imported (see `_partition_sandbox_candidates`). The link is
        # kept in the path so the workflow is keyed as the workspace scan keys it.
        workflow_header = self._read_sandbox_workflow_header(file_identity)
        if workflow_header is not None:
            return self._register_sandbox_workflow_node_from_source(
                workflow_header, file_identity, sandbox_dir, replace_if_exists=request.replace_if_exists
            )

        # Import the module. `load_module_from_file` handles both first-load and hot-reload
        # (re-importing an existing module with fresh source), which is exactly what an agent
        # iterating on a draft needs.
        try:
            module = self.engine.library_manager.module_loading.load_module_from_file(
                resolved_file, SANDBOX_LIBRARY_NAME
            )
        except ImportError as err:
            # Display file_identity (un-prefixed); file_path may carry the \\?\ prefix on Windows.
            details = f"Attempted to register a sandbox node from '{file_identity}'. Failed at import time: {err}"
            return RegisterSandboxNodeFromSourceResultFailure(result_details=details)

        # The Sandbox Library must already be registered. It is created as part of normal
        # engine startup when the sandbox directory is configured; if it is missing here, the
        # user hasn't run through the sandbox setup at all.
        try:
            sandbox_library = LibraryRegistry.get_library(SANDBOX_LIBRARY_NAME)
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
                        f"Attempted to register node type '{class_name}' from '{file_identity}'. "
                        "Failed because a node type with that name is already registered in "
                        "the Sandbox Library and replace_if_exists=False."
                    )
                    return RegisterSandboxNodeFromSourceResultFailure(result_details=details)
                sandbox_library.unregister_node_type(class_name)
                replaced_class_names.append(class_name)

            metadata = NodeMetadata(
                category=SANDBOX_CATEGORY_NAME,
                description=f"'{class_name}' (loaded from the {SANDBOX_LIBRARY_NAME}).",
                display_name=class_name,
            )
            sandbox_library.register_new_node_type(obj, metadata)
            registered_class_names.append(class_name)

        if not registered_class_names:
            details = (
                f"Imported '{file_identity}' successfully, but it does not declare any BaseNode "
                "subclasses (must be `class X(BaseNode):` defined in this file, not "
                "re-exported from another module). Nothing was registered."
            )
            return RegisterSandboxNodeFromSourceResultFailure(result_details=details)

        summary = (
            f"Registered {len(registered_class_names)} node type(s) from '{file_identity}' "
            f"into the {SANDBOX_LIBRARY_NAME} "
            f"(replaced: {len(replaced_class_names)})."
        )
        return RegisterSandboxNodeFromSourceResultSuccess(
            file_path=str(file_identity),
            library_name=SANDBOX_LIBRARY_NAME,
            registered_class_names=registered_class_names,
            replaced_class_names=replaced_class_names,
            result_details=summary,
        )

    async def attempt_generate_sandbox_library_from_schema(  # noqa: C901
        self,
        library_schema: LibrarySchema,
        sandbox_directory: str,
        library_info: LibraryInfo,
    ) -> None:
        """Generate sandbox library using an existing schema, loading actual node classes."""
        sandbox_library_dir = Path(sandbox_directory)

        problems = []

        # Saved workflows are set aside before anything is imported.
        candidates = self._partition_sandbox_candidates(library_schema, sandbox_library_dir)
        problems.extend(candidates.problems)

        # Get the file paths from the schema's node definitions to load actual classes
        actual_node_definitions = []
        for node_def in candidates.node_source_definitions:
            # Resolve relative path from schema against sandbox directory
            # Resolved, unlike the sandbox paths around it: one file is one module.
            candidate_path = resolve_workspace_path(Path(node_def.file_path), sandbox_library_dir)
            try:
                module = self.engine.library_manager.module_loading.load_module_from_file(
                    candidate_path, SANDBOX_LIBRARY_NAME
                )
            except Exception as err:
                root_cause = get_root_cause_from_exception(err)
                problems.append(
                    NodeModuleImportProblem(
                        class_name=f"<Sandbox node in '{node_def.file_path}'>",
                        file_path=str(candidate_path),
                        error_message=str(err),
                        root_cause=str(root_cause),
                    )
                )
                details = f"Attempted to load module in sandbox library '{candidate_path}'. Failed because an exception occurred: {err}."
                # The library report lists this problem; logging it too repeats it.
                logger.debug(details)
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
                            category=SANDBOX_CATEGORY_NAME,
                            description=f"'{class_name}' (loaded from the {SANDBOX_LIBRARY_NAME}).",
                            display_name=class_name,
                        )
                        logger.debug("Creating new metadata for node '%s'", class_name)

                    node_definition = NodeDefinition(
                        class_name=class_name,
                        file_path=node_def.file_path,  # Keep original relative path from schema
                        metadata=node_metadata,
                    )
                    actual_node_definitions.append(node_definition)

        if not actual_node_definitions and not candidates.workflow_node_definitions:
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
            workflow_nodes=candidates.workflow_node_definitions,
            widgets=library_schema.widgets,
        )

        # Save the schema with real class names back to disk
        json_path = sandbox_library_dir / LIBRARY_CONFIG_FILENAME
        write_succeeded = self.write_library_schema_to_json(library_data, json_path)
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
            library_info.lifecycle_state = LibraryLifecycleState.FAILURE
            library_info.fitness = LibraryFitness.UNUSABLE
            library_info.problems.append(DuplicateLibraryProblem())

            details = f"Attempted to load Library JSON file from '{sandbox_library_dir}'. Failed because a Library '{library_data.name}' already exists. Error: {err}."
            logger.error(details)
            return

        # Add any problems encountered during node discovery to library_info
        library_info.problems.extend(problems)

        # Load nodes into the library (modifies library_info in place)
        # Note: library_info is passed as parameter from lifecycle handler.
        # Sandbox nodes always load eagerly (not gated on library.lazy_node_loading): they are
        # being actively authored, so import errors should surface immediately, not on first use.
        await asyncio.to_thread(
            self.engine.library_manager.module_loading.attempt_load_nodes_from_library,
            library_data=library_data,
            library=library,
            base_dir=sandbox_library_dir,
            library_info=library_info,
            lazy_loading=False,
        )

    def write_library_schema_to_json(self, library_schema: LibrarySchema, json_path: Path) -> bool:
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
        write_result = self.engine.handle_request(write_request)

        if write_result.failed():
            logger.error("Failed to write library schema to '%s': %s", json_path, write_result.result_details)
            return False

        return True

    def _register_sandbox_workflow_node_from_source(
        self,
        workflow_header: WorkflowMetadata | WorkflowNodeLoadProblem,
        workflow_path: Path,
        sandbox_dir: Path,
        *,
        replace_if_exists: bool,
    ) -> RegisterSandboxNodeFromSourceResultSuccess | RegisterSandboxNodeFromSourceResultFailure:
        """Register a saved sandbox workflow as a workflow-backed node, without importing it.

        Built as the sandbox load builds it, and before any same-named node is removed, so a workflow
        that cannot become a node leaves the existing one in place.
        """
        if isinstance(workflow_header, WorkflowNodeLoadProblem):
            details = (
                f"Attempted to register the saved workflow at '{workflow_path}' as a sandbox node. "
                f"Failed because its workflow header could not be read: {workflow_header.error_message}"
            )
            return RegisterSandboxNodeFromSourceResultFailure(result_details=details)

        try:
            sandbox_library = LibraryRegistry.get_library(SANDBOX_LIBRARY_NAME)
        except KeyError:
            details = (
                "Attempted to register a sandbox node, but the Sandbox Library is not "
                "registered in the engine. Ensure the sandbox directory has been initialized "
                "(it is scanned once at engine startup) before calling this request."
            )
            return RegisterSandboxNodeFromSourceResultFailure(result_details=details)

        workflow_node_definition = self._create_sandbox_workflow_node_definition(workflow_header, str(workflow_path))
        node_type = workflow_node_definition.node_type
        node_class = self.engine.library_manager.module_loading.build_workflow_node_class_from_definition(
            workflow_node_definition, sandbox_dir
        )
        if isinstance(node_class, WorkflowNodeLoadProblem):
            details = (
                f"Attempted to register the saved workflow at '{workflow_path}' as node type '{node_type}'. "
                f"Failed because the workflow cannot become a node: {node_class.error_message}"
            )
            return RegisterSandboxNodeFromSourceResultFailure(result_details=details)

        already_registered = sandbox_library.has_node_type(node_type)
        if already_registered and not replace_if_exists:
            details = (
                f"Attempted to register the saved workflow at '{workflow_path}' as node type '{node_type}'. "
                "Failed because a node type with that name is already registered in the Sandbox Library "
                "and replace_if_exists=False."
            )
            return RegisterSandboxNodeFromSourceResultFailure(result_details=details)

        replaced_class_names: list[str] = []
        if already_registered:
            sandbox_library.unregister_node_type(node_type)
            replaced_class_names.append(node_type)
        library_problem = sandbox_library.register_new_node_type(node_class, workflow_node_definition.metadata)

        problem_details = ""
        if library_problem is not None:
            # A duplicate is removed or refused above, so none is expected. If one appears, the new
            # type replaced it: record it (as the load path does) and succeed with a warning. Any
            # other problem type is a guard for future kinds: the node type may or may not have
            # been stored, so fail and say so.
            problem_details = type(library_problem).collate_problems_for_display([library_problem])
            sandbox_library_info = self.engine.library_manager.get_library_info_by_library_name(SANDBOX_LIBRARY_NAME)
            if sandbox_library_info is None:
                logger.warning(
                    "Attempted to record a problem registering the saved workflow at '%s' as node type '%s'. "
                    "Failed because the %s has no load entry to record it in. The problem: %s",
                    workflow_path,
                    node_type,
                    SANDBOX_LIBRARY_NAME,
                    problem_details,
                )
            else:
                sandbox_library_info.problems.append(library_problem)

        if library_problem is not None and not isinstance(library_problem, DuplicateNodeRegistrationProblem):
            details = (
                f"Attempted to register the saved workflow at '{workflow_path}' as node type '{node_type}'. "
                f"Failed because the {SANDBOX_LIBRARY_NAME} reported a problem: "
                f"{problem_details} The node type may have been registered anyway. "
                f"Check whether node type '{node_type}' is listed in the {SANDBOX_LIBRARY_NAME} "
                "before using it, or retry with replace_if_exists=True."
            )
            return RegisterSandboxNodeFromSourceResultFailure(result_details=details)

        summary = (
            f"Registered the saved workflow at '{workflow_path}' as node type '{node_type}' "
            f"in the {SANDBOX_LIBRARY_NAME} (replaced: {len(replaced_class_names)})."
        )
        result_details: ResultDetails | str = summary
        if library_problem is not None:
            warning = (
                f"Attempted to register the saved workflow at '{workflow_path}' as node type '{node_type}'. "
                f"The node type may have been registered, but the {SANDBOX_LIBRARY_NAME} "
                f"reported a problem: {problem_details}"
            )
            result_details = ResultDetails(
                ResultDetail(level=logging.INFO, message=summary),
                ResultDetail(level=logging.WARNING, message=warning),
            )

        return RegisterSandboxNodeFromSourceResultSuccess(
            file_path=str(workflow_path),
            library_name=SANDBOX_LIBRARY_NAME,
            registered_class_names=[node_type],
            replaced_class_names=replaced_class_names,
            result_details=result_details,
        )

    def _partition_sandbox_candidates(
        self, library_schema: LibrarySchema, sandbox_directory: Path
    ) -> SandboxCandidates:
        """Split the sandbox's scanned files into node source and saved workflows, without importing any.

        Saved workflows are never imported, since that would run their graph-construction code. Their
        entries are rebuilt from the header on every load, so a rename is never frozen at first scan.
        """
        candidates = SandboxCandidates()
        for node_def in library_schema.nodes:
            workflow_header = self._read_sandbox_workflow_header(sandbox_directory / node_def.file_path)
            if isinstance(workflow_header, WorkflowNodeLoadProblem):
                candidates.problems.append(workflow_header)
            elif workflow_header is None:
                candidates.node_source_definitions.append(node_def)
            else:
                candidates.workflow_node_definitions.append(
                    self._create_sandbox_workflow_node_definition(workflow_header, node_def.file_path)
                )
        return candidates

    def _read_sandbox_workflow_header(self, candidate_path: Path) -> WorkflowMetadata | WorkflowNodeLoadProblem | None:
        """Read a sandbox file's workflow header, telling Python node source apart from saved workflows.

        Returns None for a file with no workflow header (Python node source), the header for a saved
        workflow, or a problem for a file whose header is present but cannot be read.
        """
        try:
            workflow_metadata = read_workflow_metadata(candidate_path)
        except WorkflowMetadataError as err:
            if isinstance(err, WorkflowMetadataSectionCountError) and err.count == 0:
                return None
            return WorkflowNodeLoadProblem(
                node_type=candidate_path.stem, workflow_path=str(candidate_path), error_message=str(err)
            )

        return workflow_metadata

    def _create_sandbox_workflow_node_definition(
        self, workflow_metadata: WorkflowMetadata, workflow_path: str
    ) -> WorkflowNodeDefinition:
        """Describe a saved sandbox workflow as a node, named and described by the workflow itself."""
        description = workflow_metadata.description
        if not description:
            description = f"Runs the '{workflow_metadata.name}' workflow."

        return WorkflowNodeDefinition(
            node_type=node_type_for_subflow_workflow_name(workflow_metadata.name),
            workflow_path=workflow_path,
            metadata=NodeMetadata(
                category=SANDBOX_CATEGORY_NAME,
                description=description,
                display_name=workflow_metadata.name,
                icon=SUBFLOW_NODE_ICON,
            ),
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
                library_name=SANDBOX_LIBRARY_NAME,
                status=LibraryFitness.MISSING,
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
        json_path = sandbox_directory / LIBRARY_CONFIG_FILENAME
        metadata_result = self.engine.library_manager.metadata_loading.load_library_metadata_from_file_request(
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
                description=f"Nodes loaded from the {SANDBOX_LIBRARY_NAME}.",
                color="#c7621a",
                icon="Folder",
            )

            engine_version = self.engine.handle_engine_version_request(request=GetEngineVersionRequest())
            if not isinstance(engine_version, GetEngineVersionResultSuccess):
                details = "Could not get engine version for sandbox library generation."
                return LoadLibraryMetadataFromFileResultFailure(
                    library_path=sandbox_library_dir_as_posix,
                    library_name=SANDBOX_LIBRARY_NAME,
                    status=LibraryFitness.UNUSABLE,
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
                {SANDBOX_CATEGORY_NAME: sandbox_category},
            ]
            library_name = SANDBOX_LIBRARY_NAME
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
            is_registered=is_library_name_registered(library_schema.name),
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
            class_name = UNRESOLVED_SANDBOX_CLASS_NAME
            file_name = candidate.name

            node_metadata = NodeMetadata(
                category=SANDBOX_CATEGORY_NAME,
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
                class_name = UNRESOLVED_SANDBOX_CLASS_NAME
                file_name = discovered_file.name

                node_metadata = NodeMetadata(
                    category=SANDBOX_CATEGORY_NAME,
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

    def _find_files_in_dir(self, directory: Path, extension: str) -> list[Path]:
        """Find all files with given extension in directory, excluding common non-source directories.

        Follows links to folders and keeps each link in the returned paths. A folder reached again
        (link loop, second link) is skipped, so which spelling survives depends on walk order.
        """
        ret_val = []
        visited_directories: set[Path] = set()
        for root, dirs, files_found in os.walk(directory, followlinks=True):
            # Compare real locations so a link back up the tree ends the walk.
            real_root = canonicalize_for_identity(root)
            if real_root in visited_directories:
                dirs[:] = []
                continue
            visited_directories.add(real_root)

            # Modify dirs in-place to skip excluded directories
            # Also skip any directory starting with '.'
            dirs[:] = [d for d in dirs if d not in EXCLUDED_SCAN_DIRECTORIES and not d.startswith(".")]

            for file in files_found:
                if file.endswith(extension):
                    file_path = Path(root) / file
                    ret_val.append(file_path)
        return ret_val


def node_type_for_subflow_workflow_name(workflow_name: str) -> str:
    """Derive a valid node type name from a saved workflow's name.

    Runs of letters and digits (any language) are capitalized and joined: ``shout_workflow``
    becomes ``ShoutWorkflow``. A name that cannot start a type gains a prefix rather than being
    rejected: ``3d_scan`` becomes ``Subflow3dScan``.
    """
    words = [
        "".join(characters)
        for is_word_character, characters in groupby(workflow_name, key=_can_appear_in_node_type)
        if is_word_character
    ]
    node_type = "".join(f"{word[0].upper()}{word[1:]}" for word in words)
    if not node_type or not node_type[0].isidentifier():
        return f"{SUBFLOW_NODE_TYPE_FALLBACK_PREFIX}{node_type}"
    return node_type


def _can_appear_in_node_type(character: str) -> bool:
    """Whether a character from a workflow's name can be carried into its node type name.

    Letters and digits in any language are kept; "½" counts as a number but is not valid in a class name.
    """
    return character.isalnum() and f"a{character}".isidentifier()
