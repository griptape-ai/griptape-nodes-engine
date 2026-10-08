from __future__ import annotations

import importlib.abc
import importlib.machinery
import importlib.util
import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from griptape_nodes.exe_types.node_types import BaseNode
from griptape_nodes.exe_types.workflow_node import (
    WorkflowNode,
    WorkflowNodeDefinitionError,
    build_workflow_node_class,
)
from griptape_nodes.files.path_utils import canonicalize_for_identity_preserving_symlinks, resolve_workspace_path
from griptape_nodes.node_library.library_registry import (
    Library,
    LibraryRegistry,
    LibrarySchema,
    NodeDefinition,
    WorkflowNodeDefinition,
)
from griptape_nodes.node_library.workflow_registry import WorkflowMetadataError, read_workflow_metadata
from griptape_nodes.retained_mode.engine import EngineScoped
from griptape_nodes.retained_mode.managers.fitness_problems.libraries import (
    AfterLibraryCallbackProblem,
    BeforeLibraryCallbackProblem,
    NodeClassNotBaseNodeProblem,
    NodeClassNotFoundProblem,
    NodeModuleImportProblem,
    OldXdgLocationWarningProblem,
    PostDispatchHookRegistrationProblem,
    PostDispatchHooksWorkerIncompatibleProblem,
    RequestHandlerRegistrationProblem,
    RequestHandlersWorkerIncompatibleProblem,
    WorkflowNodeLoadProblem,
)
from griptape_nodes.retained_mode.managers.library.common import LibraryFitness, LibraryInfo, LibraryLifecycleState
from griptape_nodes.retained_mode.managers.settings import (
    LIBRARY_LAZY_NODE_LOADING_KEY,
)
from griptape_nodes.serialization.type_names import (
    DYNAMIC_MODULE_PREFIX,
    forget_stable_module_name,
    is_dynamic_module_name,
    register_stable_module_name,
)
from griptape_nodes.utils.engine_dirs import engine_data_dir

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from types import ModuleType

    from griptape_nodes.node_library.advanced_node_library import AdvancedNodeLibrary
    from griptape_nodes.retained_mode.engine import Engine

logger = logging.getLogger("griptape_nodes")


# Prefix for the stable, deterministic module namespaces that library node files are
# importable under. Anything under this prefix is a dynamically loaded library module
# whose import path only exists in-process once its library has been registered.
STABLE_NAMESPACE_PREFIX = "griptape_nodes.node_libraries."


class StableNamespaceImportFinder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    """Imports lazily registered library node modules on demand via their stable namespace.

    Saved workflows reference node-file classes through their stable namespace
    (``griptape_nodes.node_libraries.<lib>.<file>``), both as ``from`` imports emitted into the
    generated Python and in the ``$type`` tags of saved parameter values (and, in files saved by
    earlier engines, inside pickled ones). With eager node loading those modules
    are already aliased into ``sys.modules`` when the library registers, so the imports resolve.
    With lazy loading nothing is imported until a node class is first resolved, so opening a
    workflow before then would fail with ``No module named 'griptape_nodes.node_libraries'``.

    This finder fills that gap: importing a stable namespace triggers the same memoized module
    load that the deferred node-class loaders use, and the synthetic parent packages
    (``griptape_nodes.node_libraries`` and ``griptape_nodes.node_libraries.<lib>``, which do not
    exist on disk) are materialized as empty namespace packages so the dotted import chain
    resolves.
    """

    def __init__(self, module_loading: LibraryModuleLoading) -> None:
        self._module_loading = module_loading

    def find_spec(
        self,
        fullname: str,
        path: Sequence[str] | None = None,  # noqa: ARG002 (MetaPathFinder protocol)
        target: ModuleType | None = None,  # noqa: ARG002 (MetaPathFinder protocol)
    ) -> importlib.machinery.ModuleSpec | None:
        package_root = STABLE_NAMESPACE_PREFIX.rstrip(".")
        if fullname != package_root and not fullname.startswith(STABLE_NAMESPACE_PREFIX):
            return None

        pending_loaders = self._module_loading._pending_stable_module_loaders
        if fullname in pending_loaders:
            return importlib.util.spec_from_loader(fullname, self)

        # Synthesize the namespace-package parents of any known stable namespace so the import
        # machinery can walk the dotted chain down to the leaf loader above. Known namespaces
        # cover both pending (lazy, not yet imported) and already-loaded library modules.
        child_prefix = f"{fullname}."
        known_namespaces = (*pending_loaders, *self._module_loading._stable_to_dynamic_module_mapping)
        if fullname == package_root or any(namespace.startswith(child_prefix) for namespace in known_namespaces):
            return importlib.machinery.ModuleSpec(fullname, None, is_package=True)

        return None

    def create_module(self, spec: importlib.machinery.ModuleSpec) -> ModuleType:
        module_loader = self._module_loading._pending_stable_module_loaders[spec.name]
        return module_loader()

    def exec_module(self, module: ModuleType) -> None:
        """No-op: the module was fully executed by the memoized loader in create_module."""


def get_root_cause_from_exception(exception: BaseException) -> BaseException:
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


def get_node_class_from_module(module: ModuleType, class_name: str, file_path: Path | str) -> type[BaseNode]:
    """Extract and validate a BaseNode subclass from an already-imported module.

    Raises:
        AttributeError: If the class doesn't exist in the module
        TypeError: If the named object isn't a BaseNode-derived class
    """
    try:
        node_class = getattr(module, class_name)
    except AttributeError as err:
        msg = f"Class '{class_name}' not found in module '{file_path}'"
        raise AttributeError(msg) from err

    if not issubclass(node_class, BaseNode):
        msg = f"'{class_name}' must inherit from BaseNode"
        raise TypeError(msg)

    return node_class


class LibraryModuleLoading(EngineScoped):
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
    # Deferred module loaders for lazily registered node files, keyed by stable namespace.
    # Populated at (lazy) library registration time and consumed by StableNamespaceImportFinder
    # so a saved workflow can import `griptape_nodes.node_libraries.<lib>.<file>` before any
    # node class from that file has been resolved. An entry is dropped as soon as its module
    # actually loads (sys.modules satisfies imports from then on) or when its library unloads.
    _pending_stable_module_loaders: dict[str, Callable[[], ModuleType]]  # stable_namespace -> module loader
    _library_to_pending_stable_namespaces: dict[str, set[str]]  # library_name -> pending stable_namespaces
    # Meta-path finder that resolves stable namespaces for pending (lazy) node modules.
    _stable_namespace_finder: StableNamespaceImportFinder

    def __init__(self, engine: Engine | None = None) -> None:
        super().__init__(engine)
        self._dynamic_to_stable_module_mapping = {}
        self._stable_to_dynamic_module_mapping = {}
        self._library_to_stable_modules = {}
        self._pending_stable_module_loaders = {}
        self._library_to_pending_stable_namespaces = {}
        self._install_stable_namespace_finder()

    def unregister_all_stable_module_aliases_for_library(self, library_name: str) -> None:
        """Unregister all stable module aliases for a library during library unload/reload.

        Args:
            library_name: Name of the library to clean up
        """
        self._unregister_pending_stable_module_loaders_for_library(library_name)

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
                forget_stable_module_name(dynamic_module_name)
            self._stable_to_dynamic_module_mapping.pop(stable_namespace, None)

        # Clear the library's module set
        del self._library_to_stable_modules[library_key]
        details = f"Completed cleanup of stable aliases for library: '{library_name}'."
        logger.debug(details)

    def stable_module_names(self) -> set[str]:
        """Return the stable namespace of every registered library file, whether or not it has loaded yet."""
        return {*self._pending_stable_module_loaders, *self._stable_to_dynamic_module_mapping}

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
        return is_dynamic_module_name(module_name)

    def load_module_from_file(self, file_path: Path | str, library_name: str) -> ModuleType:
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
        module_name = f"{DYNAMIC_MODULE_PREFIX}{file_path.name.replace('.', '_')}_{hash(str(file_path))}"

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
                # Restore the old module and its alias, so its live classes keep their type names
                sys.modules[module_name] = old_module
                self._register_stable_module_alias(module_name, stable_namespace, old_module, library_name)
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

    def load_advanced_library_module(
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
            module = self.load_module_from_file(advanced_library_module_path, library_data.name)
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

    def should_lazy_load_nodes(self) -> bool:
        """Return whether node modules should be imported lazily for this process.

        Lazy loading is the default (fast startup); set ``library.lazy_node_loading`` to False
        to load eagerly so node import errors surface at startup while authoring. A worker always
        loads eagerly regardless of the setting: it imports every node anyway to serialize schemas
        back to the orchestrator, and eager load lets it report import problems.
        """
        if self.engine.library_manager.is_worker:
            return False
        return bool(self.engine.config_manager.get_config_value(LIBRARY_LAZY_NODE_LOADING_KEY, default=True))

    def attempt_load_nodes_from_library(  # noqa: PLR0912, PLR0915, C901
        self,
        library_data: LibrarySchema,
        library: Library,
        base_dir: Path,
        library_info: LibraryInfo,
        *,
        lazy_loading: bool = False,
    ) -> None:
        """Load nodes from library and update library_info in place.

        Args:
            library_data: Library schema with node definitions
            library: Library instance to register nodes with
            base_dir: Base directory for resolving relative paths
            library_info: LibraryInfo to update with problems and fitness
            lazy_loading: When False (default), each node's module is imported now so import
                errors surface here as library problems. When True, node types are registered
                with a deferred loader and their modules are imported on first use instead.
        """
        any_nodes_loaded_successfully = False

        # Check if library is in old engine data location
        old_xdg_libraries_path = engine_data_dir() / "libraries"
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

        # Process each node in the metadata. Lazy loading (the default) registers each type with
        # a deferred loader and imports the module on first use, keeping startup from paying the
        # import cost (often heavy deps like torch/diffusers) of node modules that are never used;
        # the tradeoff is that an import error is not reported until the node is first used (the
        # CreateNode handler then substitutes an Error Proxy). Eager loading (library.lazy_node_loading
        # = False, and always for the sandbox library) instead imports each node's module now so a
        # broken node surfaces as a library problem at startup, before it is placed on a canvas.
        # module_loaders memoizes each file's module import so classes sharing a file (whether
        # resolved eagerly here or lazily later) import it once rather than re-executing per class.
        module_loaders: dict[Path, Callable[[], ModuleType]] = {}
        for node_definition in library_data.nodes:
            # Resolve relative path to absolute path
            node_file_path = resolve_workspace_path(Path(node_definition.file_path), base_dir)

            if lazy_loading:
                node_registered = self._register_node_lazy(
                    node_definition, node_file_path, library, library_info, module_loaders
                )
            else:
                node_registered = self._register_node_eager(
                    node_definition, node_file_path, library, library_info, module_loaders
                )
            if node_registered:
                any_nodes_loaded_successfully = True

        # Nodes generated from saved workflow files. These need no Python module, so they are always
        # built now rather than lazily: reading a workflow's metadata header is cheap.
        for workflow_node_definition in library_data.workflow_nodes or []:
            if self._register_workflow_node(workflow_node_definition, base_dir, library, library_info):
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

        # Register request/response handlers declared by the library
        if advanced_library:
            try:
                # TODO: https://github.com/griptape-ai/griptape-nodes-engine/issues/4744 revisit per-entry error granularity
                handlers = advanced_library.get_request_handlers()
                if handlers and library_info.requires_worker:
                    library_info.problems.append(
                        RequestHandlersWorkerIncompatibleProblem(
                            library_name=library_data.name,
                            handler_count=len(handlers),
                        )
                    )
                    logger.warning(
                        "Library '%s' declares %d request handler(s) via get_request_handlers() but requires "
                        "worker mode. Handlers are only registered in the worker process and cannot be reached "
                        "from the orchestrator. See https://github.com/griptape-ai/griptape-nodes-engine/issues/4748",
                        library_data.name,
                        len(handlers),
                    )
                for request_type, handler in handlers:
                    event_manager = self.engine.event_manager
                    event_manager.assign_manager_to_request_type(request_type, handler)
                    library._registered_request_handler_types.append(request_type)
                if handlers:
                    logger.debug(
                        "Registered %d request handler(s) for library '%s'",
                        len(handlers),
                        library_data.name,
                    )
            except Exception as err:
                library_info.problems.append(RequestHandlerRegistrationProblem(error_message=str(err)))
                logger.error(
                    "Failed to register request handlers for library '%s': %s",
                    library_data.name,
                    err,
                )

        # Register post-dispatch hooks declared by the library
        if advanced_library:
            try:
                hooks = advanced_library.get_post_dispatch_hooks()
                if hooks and library_info.requires_worker:
                    library_info.problems.append(
                        PostDispatchHooksWorkerIncompatibleProblem(
                            library_name=library_data.name,
                            hook_count=len(hooks),
                        )
                    )
                    logger.warning(
                        "Library '%s' declares %d post-dispatch hook(s) via get_post_dispatch_hooks() but "
                        "requires worker mode. Hooks are only registered in the worker process and do not "
                        "observe requests handled by the orchestrator. "
                        "See https://github.com/griptape-ai/griptape-nodes-engine/issues/4748",
                        library_data.name,
                        len(hooks),
                    )
                for request_type, callback in hooks:
                    # Hooks match on the exact request type, so a key that is not a class can
                    # never equal `type(request)`: the hook would register cleanly and then
                    # never fire, with nothing in the log to explain why. Fail loudly instead,
                    # the same way the callable check below does.
                    if not isinstance(request_type, type):
                        msg = (
                            f"Attempted to register a post-dispatch hook from library "
                            f"'{library_data.name}'. Failed because '{request_type}' is not a request type. "
                            f"Each entry from get_post_dispatch_hooks() must pair a request type with a "
                            f"function to run."
                        )
                        raise TypeError(msg)  # noqa: TRY301
                    if not callable(callback):
                        msg = (
                            f"Attempted to register a post-dispatch hook for '{request_type.__name__}' "
                            f"from library '{library_data.name}'. Failed because the hook is not something "
                            f"that can be called. Each entry from get_post_dispatch_hooks() must pair a "
                            f"request type with a function to run."
                        )
                        raise TypeError(msg)  # noqa: TRY301
                    event_manager = self.engine.event_manager
                    event_manager.add_post_dispatch_hook(request_type, callback)
                    library._registered_post_dispatch_hooks.append((request_type, callback))
                if hooks:
                    logger.debug(
                        "Registered %d post-dispatch hook(s) for library '%s'",
                        len(hooks),
                        library_data.name,
                    )
            except Exception as err:
                library_info.problems.append(PostDispatchHookRegistrationProblem(error_message=str(err)))
                logger.error(
                    "Failed to register post-dispatch hooks for library '%s': %s",
                    library_data.name,
                    err,
                )

        # Update library_info fitness based on load successes and problem count
        if not any_nodes_loaded_successfully:
            library_info.fitness = LibraryFitness.UNUSABLE
        elif library_info.problems:
            # Success, but errors.
            library_info.fitness = LibraryFitness.FLAWED
        else:
            # Flawless victory.
            library_info.fitness = LibraryFitness.GOOD

        # Update lifecycle state to LOADED
        library_info.lifecycle_state = LibraryLifecycleState.LOADED

    def build_workflow_node_class_from_definition(
        self, workflow_node_definition: WorkflowNodeDefinition, base_dir: Path
    ) -> type[WorkflowNode] | WorkflowNodeLoadProblem:
        """Generate the node type a saved workflow file describes, or the problem that prevents it."""
        # Deliberately not `canonicalize_for_identity`: the workspace scan registers a linked
        # workflow under the link's path, and resolving it would key it by a full machine-specific
        # path instead.
        workflow_file_path = canonicalize_for_identity_preserving_symlinks(
            workflow_node_definition.workflow_path, base=base_dir
        )
        try:
            workflow_metadata = read_workflow_metadata(workflow_file_path)
        except WorkflowMetadataError as err:
            return WorkflowNodeLoadProblem(
                node_type=workflow_node_definition.node_type,
                workflow_path=str(workflow_file_path),
                error_message=str(err),
            )

        try:
            node_class = build_workflow_node_class(
                node_type=workflow_node_definition.node_type,
                workflow_file_path=workflow_file_path,
                workflow_metadata=workflow_metadata,
            )
        except WorkflowNodeDefinitionError as err:
            return WorkflowNodeLoadProblem(
                node_type=workflow_node_definition.node_type,
                workflow_path=str(workflow_file_path),
                error_message=str(err),
            )

        return node_class

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

        return f"{STABLE_NAMESPACE_PREFIX}{safe_library_name}.{safe_file_name}"

    def _install_stable_namespace_finder(self) -> None:
        """Install the meta-path finder that resolves stable namespaces for lazy node modules.

        Any finder left behind by a previously constructed LibraryManager (tests construct
        several per process) is removed first so exactly one finder, backed by this manager's
        state, is consulted.
        """
        sys.meta_path[:] = [finder for finder in sys.meta_path if not isinstance(finder, StableNamespaceImportFinder)]
        self._stable_namespace_finder = StableNamespaceImportFinder(self)
        sys.meta_path.append(self._stable_namespace_finder)

    def _register_pending_stable_module_loader(
        self, stable_namespace: str, library_name: str, module_loader: Callable[[], ModuleType]
    ) -> None:
        """Make a lazily registered node file importable via its stable namespace.

        The loader is the same memoized per-file loader the node-class loaders share, so an
        import through StableNamespaceImportFinder and a later class resolution reuse one
        module object (and the file's top-level code runs once).

        Args:
            stable_namespace: Stable namespace the file will be importable under
            library_name: Name of the owning library (for cleanup on unload)
            module_loader: Memoized zero-argument loader that imports the file's module
        """
        self._pending_stable_module_loaders[stable_namespace] = module_loader
        self._library_to_pending_stable_namespaces.setdefault(library_name, set()).add(stable_namespace)

    def _unregister_pending_stable_module_loaders_for_library(self, library_name: str) -> None:
        """Drop pending (never-imported) stable module loaders for a library on unload.

        Args:
            library_name: Name of the library to clean up
        """
        pending_namespaces = self._library_to_pending_stable_namespaces.pop(library_name, set())
        for stable_namespace in pending_namespaces:
            self._pending_stable_module_loaders.pop(stable_namespace, None)

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
        register_stable_module_name(dynamic_module_name, stable_namespace)

        # The module is importable through sys.modules now; retire its pending loader so the
        # meta-path finder can never serve a stale module object after this one is unloaded.
        self._pending_stable_module_loaders.pop(stable_namespace, None)
        self._library_to_pending_stable_namespaces.get(library_key, set()).discard(stable_namespace)

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
            forget_stable_module_name(dynamic_module_name)

            details = f"Unregistered stable alias: {stable_namespace}"
            logger.debug(details)

    def _make_memoized_module_loader(self, node_file_path: Path, library_name: str) -> Callable[[], ModuleType]:
        """Return a loader that imports a file's module at most once, caching the result.

        ``load_module_from_file`` re-executes a module that is already in ``sys.modules`` (its
        hot-reload path). Without memoization, resolving several node classes that share one file
        would re-run that file's top-level code once per class and bind the classes to different
        module objects. Memoizing per file makes the module import (and its top-level side
        effects) happen exactly once per library load, whether its classes are resolved eagerly in
        a burst or lazily at scattered points in the session. A fresh cache is created per
        ``attempt_load_nodes_from_library`` call, so a reload still re-imports the file once.
        """
        cache: dict[str, ModuleType] = {}

        def load_module() -> ModuleType:
            module = cache.get("module")
            if module is None:
                module = self.load_module_from_file(node_file_path, library_name)
                cache["module"] = module
            return module

        return load_module

    def _get_or_create_module_loader(
        self,
        node_file_path: Path,
        library_name: str,
        module_loaders: dict[Path, Callable[[], ModuleType]],
    ) -> Callable[[], ModuleType]:
        """Get the memoized module loader for a node file, creating and caching one if needed.

        All consumers of a file's module (node-class loaders and the stable-namespace import
        finder) must share one memoized loader so the file is imported exactly once per
        library load.
        """
        module_loader = module_loaders.get(node_file_path)
        if module_loader is None:
            module_loader = self._make_memoized_module_loader(node_file_path, library_name)
            module_loaders[node_file_path] = module_loader
        return module_loader

    def _make_node_class_loader(
        self,
        node_file_path: Path,
        class_name: str,
        library_name: str,
        module_loaders: dict[Path, Callable[[], ModuleType]],
    ) -> Callable[[], type[BaseNode]]:
        """Return a zero-argument loader that imports and returns a node class on demand.

        Node classes that share a file reuse a single memoized module loader from
        ``module_loaders`` (keyed by resolved file path), so the file's module is imported once
        even when its classes are resolved at different times. Binds its arguments as method
        parameters (not loop variables), so the closure is safe to build inside a per-node loop.
        """
        module_loader = self._get_or_create_module_loader(node_file_path, library_name, module_loaders)

        def load() -> type[BaseNode]:
            module = module_loader()
            return get_node_class_from_module(module, class_name, node_file_path)

        return load

    def _register_node_eager(
        self,
        node_definition: NodeDefinition,
        node_file_path: Path,
        library: Library,
        library_info: LibraryInfo,
        module_loaders: dict[Path, Callable[[], ModuleType]],
    ) -> bool:
        """Import a node's module now and register its class, recording any load problem.

        Returns True if the node type was registered, False if its module failed to import
        (a problem is appended to ``library_info`` in that case).
        """
        library_name = library.get_library_data().name
        try:
            node_class = self._make_node_class_loader(
                node_file_path, node_definition.class_name, library_name, module_loaders
            )()
        except ImportError as err:
            root_cause = get_root_cause_from_exception(err)
            library_info.problems.append(
                NodeModuleImportProblem(
                    class_name=node_definition.class_name,
                    file_path=str(node_file_path),
                    error_message=str(err),
                    root_cause=str(root_cause),
                )
            )
            logger.error(
                "Attempted to load node '%s' from '%s'. Failed because module could not be imported: %s",
                node_definition.class_name,
                node_file_path,
                err,
            )
            return False
        except AttributeError:
            library_info.problems.append(
                NodeClassNotFoundProblem(class_name=node_definition.class_name, file_path=str(node_file_path))
            )
            logger.error(
                "Attempted to load node '%s' from '%s'. Failed because class not found in module",
                node_definition.class_name,
                node_file_path,
            )
            return False
        except TypeError:
            library_info.problems.append(
                NodeClassNotBaseNodeProblem(class_name=node_definition.class_name, file_path=str(node_file_path))
            )
            logger.error(
                "Attempted to load node '%s' from '%s'. Failed because class doesn't inherit from BaseNode",
                node_definition.class_name,
                node_file_path,
            )
            return False

        library_problem = library.register_new_node_type(node_class, metadata=node_definition.metadata)
        if library_problem is not None:
            library_info.problems.append(library_problem)
        return True

    def _register_node_lazy(
        self,
        node_definition: NodeDefinition,
        node_file_path: Path,
        library: Library,
        library_info: LibraryInfo,
        module_loaders: dict[Path, Callable[[], ModuleType]],
    ) -> bool:
        """Register a node type with a deferred loader; its module imports on first use.

        Always returns True: registration itself does not import the module, so an import
        error cannot be detected here (it surfaces when the node is first used).
        """
        library_name = library.get_library_data().name
        loader = self._make_node_class_loader(node_file_path, node_definition.class_name, library_name, module_loaders)
        # Saved workflows import classes, and rebuild values, from this file via its stable namespace
        # (`griptape_nodes.node_libraries.<lib>.<file>`). With eager loading that namespace is in
        # sys.modules by now; with lazy loading it is not, so register a pending loader that the
        # StableNamespaceImportFinder resolves on first import of the namespace.
        stable_namespace = self._create_stable_namespace(library_name, node_file_path)
        module_loader = self._get_or_create_module_loader(node_file_path, library_name, module_loaders)
        self._register_pending_stable_module_loader(stable_namespace, library_name, module_loader)
        library_problem = library.register_lazy_node_type(
            node_definition.class_name, metadata=node_definition.metadata, loader=loader
        )
        if library_problem is not None:
            library_info.problems.append(library_problem)
        return True

    def _register_workflow_node(
        self,
        workflow_node_definition: WorkflowNodeDefinition,
        base_dir: Path,
        library: Library,
        library_info: LibraryInfo,
    ) -> bool:
        """Generate and register a node type from a saved workflow file.

        The workflow's metadata header supplies the saved input/output shape that becomes the node's
        parameters. Returns False (recording a library problem) when the header cannot be read or
        carries no shape.
        """
        node_class = self.build_workflow_node_class_from_definition(workflow_node_definition, base_dir)
        if isinstance(node_class, WorkflowNodeLoadProblem):
            library_info.problems.append(node_class)
            return False

        library_problem = library.register_new_node_type(node_class, metadata=workflow_node_definition.metadata)
        if library_problem is not None:
            library_info.problems.append(library_problem)
        return True
