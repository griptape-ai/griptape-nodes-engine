from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, ClassVar, NamedTuple

from pydantic import BaseModel, Field, field_validator, model_validator

from griptape_nodes.node_library.library_declarations import (
    LibraryDeclaration,
    ModelCatalogLibraryProperty,
    NodeDeclaration,
    SuggestedWorkerMode,
    WorkerCompatibility,
    WorkerMode,
    WorkerModeCompatibility,
    find_model_catalog,
    resolve_node_models,
)
from griptape_nodes.retained_mode.managers.fitness_problems.libraries.duplicate_node_registration_problem import (
    DuplicateNodeRegistrationProblem,
)
from griptape_nodes.retained_mode.managers.fitness_problems.libraries.duplicate_widget_registration_problem import (
    DuplicateWidgetRegistrationProblem,
)
from griptape_nodes.retained_mode.managers.resource_components.resource_instance import (
    Requirements,
)
from griptape_nodes.utils.metaclasses import SingletonMeta

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from griptape_nodes.exe_types.node_types import BaseNode
    from griptape_nodes.node_library.advanced_node_library import AdvancedNodeLibrary
    from griptape_nodes.node_library.library_declarations import ResolvedModel
    from griptape_nodes.retained_mode.managers.fitness_problems.libraries.library_problem import LibraryProblem

logger = logging.getLogger("griptape_nodes")

_constructing_node: ContextVar[bool] = ContextVar("_library_registry_constructing_node", default=False)


class LibraryNameAndVersion(NamedTuple):
    library_name: str
    library_version: str


class Dependencies(BaseModel):
    """Pip packages that need to be installed for this library."""

    pip_dependencies: list[str] | None = None
    pip_install_flags: list[str] | None = None


class ResourceRequirements(BaseModel):
    """Resource requirements for a library.

    Specifies what system resources (OS, compute backends) the library needs.
    Example: {"platform": (["linux", "windows"], "has_any"), "arch": "x86_64", "compute": (["cuda", "cpu"], "has_all")}
    """

    required: Requirements | None = None

    @field_validator("required", mode="before")
    @classmethod
    def convert_lists_to_tuples(cls, v: Any) -> Any:
        """Convert list values to tuples for requirements loaded from JSON.

        JSON arrays become Python lists, but the Requirements type expects tuples
        for (value, comparator) pairs.
        """
        if v is None:
            return None

        if not isinstance(v, dict):
            return v

        converted = {}
        comparator_tuple_length = 2
        for key, value in v.items():
            # Check if value is a list with exactly 2 elements where second is a string (comparator)
            if isinstance(value, list) and len(value) == comparator_tuple_length and isinstance(value[1], str):
                converted[key] = tuple(value)
            else:
                converted[key] = value
        return converted


class LibraryMetadata(BaseModel):
    """Metadata that explains details about the library, including versioning and search details."""

    author: str
    description: str
    library_version: str
    engine_version: str
    tags: list[str]
    dependencies: Dependencies | None = None
    # If True, this library will be surfaced to Griptape Nodes customers when listing Node Libraries available to them.
    is_griptape_nodes_searchable: bool = True
    # Resource requirements for this library. If None, library is assumed to work on any platform.
    resources: ResourceRequirements | None = None
    # Declarative properties / capabilities for this library. Applies to all nodes in the library.
    # See griptape_nodes.node_library.library_declarations for the supported types,
    # including WorkerModeCompatibility for orchestrator/worker hosting.
    declarations: list[LibraryDeclaration] = Field(default_factory=list)

    @model_validator(mode="after")
    def _reject_incompatible_with_suggested_worker(self) -> LibraryMetadata:
        # A library declared INCOMPATIBLE with worker hosting must not also
        # suggest WORKER as its launch mode. The two declarations live on
        # the same metadata block, so the cross-axis check belongs here --
        # the individual declaration models are independent and can't see
        # each other.
        capability = next((d for d in self.declarations if isinstance(d, WorkerModeCompatibility)), None)
        if capability is None or capability.compatibility is not WorkerCompatibility.INCOMPATIBLE:
            return self
        suggested = next(
            (d for d in self.declarations if isinstance(d, SuggestedWorkerMode)),
            None,
        )
        if suggested is not None and suggested.mode is WorkerMode.WORKER:
            msg = (
                "Library declares WorkerModeCompatibility(compatibility=INCOMPATIBLE) but also "
                "declares SuggestedWorkerMode(mode=WORKER); the two are contradictory."
            )
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _reject_multiple_model_catalogs(self) -> LibraryMetadata:
        # Node references and the duplicate-id check assume a single catalog
        # (see library_validation._find_model_catalog). Two catalogs would let
        # the second one's models go unseen, so reject the ambiguity here where
        # all declarations are visible together.
        catalog_count = sum(1 for d in self.declarations if isinstance(d, ModelCatalogLibraryProperty))
        if catalog_count > 1:
            msg = (
                f"Library declares {catalog_count} 'model_catalog' declarations; at most one is allowed. "
                f"Merge the providers into a single 'model_catalog'."
            )
            raise ValueError(msg)
        return self


class IconVariant(BaseModel):
    """Icon variant for light and dark themes."""

    light: str
    dark: str


class NodeDeprecationMetadata(BaseModel):
    """Metadata about a deprecated node."""

    deprecation_message: str | None = None
    removal_version: str | None = None


class NodeMetadata(BaseModel):
    """Metadata about each node within the library, which informs where in the hierarchy it sits, details on usage, and tags to assist search."""

    category: str
    description: str
    display_name: str
    tags: list[str] | None = None
    icon: str | IconVariant | None = None
    color: str | None = None
    group: str | None = None
    deprecation: NodeDeprecationMetadata | None = None
    is_node_group: bool | None = None
    # Declarative properties / capabilities for this node.
    # See griptape_nodes.node_library.library_declarations for the supported types.
    declarations: list[NodeDeclaration] = Field(default_factory=list)


class CategoryDefinition(BaseModel):
    """Defines categories within a library, which influences how nodes are organized within an editor."""

    title: str
    description: str
    color: str
    icon: str
    group: str | None = None


class NodeDefinition(BaseModel):
    """Defines a node within a library, including class name and file name and metadata about the node."""

    class_name: str
    file_path: str
    metadata: NodeMetadata


class Setting(BaseModel):
    """Defines a library-specific setting, which will automatically be injected into the user's Configuration."""

    category: str  # Name of the category in the config
    contents: dict[str, Any]  # The actual settings content
    description: str | None = None  # Optional description for the setting
    json_schema: dict[str, Any] | None = Field(
        default=None, alias="schema"
    )  # JSON schema for the setting (including enums)


class WidgetDefinition(BaseModel):
    """Defines a custom UI widget provided by the library.

    Widgets are pre-built ES module bundles that the frontend
    can dynamically load to render custom parameter UI.
    """

    name: str  # Widget name (e.g., "ColorGradientPicker")
    path: str  # Relative path to widget JS file (e.g., "widgets/ColorGradientPicker.js")
    description: str | None = None  # Optional description for documentation


class LibrarySchema(BaseModel):
    """Schema for a library definition file.

    The schema that defines the structure of a Griptape Nodes library,
    including the nodes and workflows it contains, as well as metadata about the
    library itself.
    """

    LATEST_SCHEMA_VERSION: ClassVar[str] = "0.10.0"

    name: str
    library_schema_version: str
    metadata: LibraryMetadata
    categories: list[dict[str, CategoryDefinition]]
    nodes: list[NodeDefinition]
    workflows: list[str] | None = None
    scripts: list[str] | None = None
    settings: list[Setting] | None = None
    is_default_library: bool | None = None
    advanced_library_path: str | None = None
    widgets: list[WidgetDefinition] | None = None


class LibraryRegistry(metaclass=SingletonMeta):
    """Singleton registry to manage many libraries."""

    _libraries: ClassVar[dict[str, Library]] = {}
    _node_aliases: ClassVar[dict[str, Library]] = {}
    _collision_node_names_to_library_names: ClassVar[dict[str, list[str]]] = {}
    # Track registered widgets per library: {library_name: set(widget_names)}
    _registered_widgets: ClassVar[dict[str, set[str]]] = {}

    @classmethod
    def _clear(cls) -> None:
        """Drop every registered library and its tracking state.

        Used by tests to reset the singleton between cases. Centralizes the store
        list here so renaming a `ClassVar` updates this one method rather than
        silently degrading callers that would otherwise clear stores by name.
        """
        cls._libraries.clear()
        cls._node_aliases.clear()
        cls._collision_node_names_to_library_names.clear()
        cls._registered_widgets.clear()

    @classmethod
    def generate_new_library(
        cls,
        library_data: LibrarySchema,
        *,
        mark_as_default_library: bool = False,
        advanced_library: AdvancedNodeLibrary | None = None,
    ) -> Library:
        instance = cls()

        if library_data.name in instance._libraries:
            msg = f"Library '{library_data.name}' already registered."
            raise KeyError(msg)
        library = Library(
            library_data=library_data, is_default_library=mark_as_default_library, advanced_library=advanced_library
        )
        instance._libraries[library_data.name] = library
        return library

    @classmethod
    def unregister_library(cls, library_name: str) -> None:
        instance = cls()

        if library_name not in instance._libraries:
            msg = f"Library '{library_name}' was requested to be unregistered, but it wasn't registered in the first place."
            raise KeyError(msg)

        library = instance._libraries[library_name]
        advanced_library = library.get_advanced_library()

        # Teardown hook — called before any deregistration.
        if advanced_library:
            try:
                advanced_library.before_library_unregistered(library.get_library_data(), library)
            except Exception as err:
                logger.error(
                    "Failed to call before_library_unregistered for library '%s': %s",
                    library_name,
                    err,
                )
                # Continue — a failing teardown must not prevent unregistration.

        # Deregister tracked handlers. Lazy import required: library_registry is part of the
        # import chain griptape_nodes → workflow_registry → library_registry, so a top-level
        # import of GriptapeNodes here would create a circular dependency.
        from griptape_nodes.retained_mode.griptape_nodes import (
            GriptapeNodes,
        )  # circular: griptape_nodes → workflow_registry → library_registry

        event_manager = GriptapeNodes.EventManager()

        if library._registered_app_event_listeners:
            for event_type, listener in library._registered_app_event_listeners:
                event_manager.remove_listener_for_app_event(event_type, listener)
            library._registered_app_event_listeners.clear()

        if library._registered_pre_dispatch_hooks:
            for hook in library._registered_pre_dispatch_hooks:
                event_manager.remove_pre_dispatch_hook(hook)
            library._registered_pre_dispatch_hooks.clear()

        if library._registered_request_handler_types:
            for request_type in library._registered_request_handler_types:
                event_manager.remove_manager_from_request_type(request_type)
            library._registered_request_handler_types.clear()

        # Clean up registered widgets for this library
        cls.unregister_widgets_for_library(library_name)

        # Now delete the library from the registry.
        del instance._libraries[library_name]

    @classmethod
    def get_library(cls, name: str) -> Library:
        instance = cls()
        if name not in instance._libraries:
            msg = f"Library '{name}' not found"
            raise KeyError(msg)
        return instance._libraries[name]

    @classmethod
    def list_libraries(cls) -> list[str]:
        instance = cls()

        # Put the default libraries first.
        default_libraries = [k for k, v in instance._libraries.items() if v.is_default_library()]
        other_libraries = [k for k, v in instance._libraries.items() if not v.is_default_library()]
        sorted_list = default_libraries + other_libraries
        return sorted_list

    @classmethod
    def register_node_type_from_library(cls, library: Library, node_class_name: str) -> LibraryProblem | None:
        """Register a node type from a library. Returns a LibraryProblem if registration fails."""
        # Does a node class of this name already exist?
        library_collisions = LibraryRegistry.get_libraries_with_node_type(node_class_name)
        if library_collisions:
            library_data = library.get_library_data()
            if library_data.name in library_collisions:
                logger.error(
                    "Attempted to register node class '%s' from library '%s', but a node with that name from that library was already registered",
                    node_class_name,
                    library_data.name,
                )
                return DuplicateNodeRegistrationProblem(class_name=node_class_name, library_name=library_data.name)

        return None

    @classmethod
    def register_widget_from_library(
        cls, library_name: str, widget_name: str
    ) -> DuplicateWidgetRegistrationProblem | None:
        """Register a widget from a library. Returns a LibraryProblem if registration fails."""
        instance = cls()

        # Initialize the set for this library if needed
        if library_name not in instance._registered_widgets:
            instance._registered_widgets[library_name] = set()

        # Check if widget is already registered for this library
        if widget_name in instance._registered_widgets[library_name]:
            logger.error(
                "Attempted to register widget '%s' from library '%s', but a widget with that name from that library was already registered",
                widget_name,
                library_name,
            )
            return DuplicateWidgetRegistrationProblem(widget_name=widget_name, library_name=library_name)

        # Register the widget
        instance._registered_widgets[library_name].add(widget_name)
        return None

    @classmethod
    def unregister_widgets_for_library(cls, library_name: str) -> None:
        """Unregister all widgets for a library (used during library unload)."""
        instance = cls()
        if library_name in instance._registered_widgets:
            del instance._registered_widgets[library_name]

    @classmethod
    def get_libraries_with_node_type(cls, node_type: str) -> list[str]:
        instance = cls()
        libraries = []
        for library_name, library in instance._libraries.items():
            if library.has_node_type(node_type):
                libraries.append(library_name)
        return libraries

    @classmethod
    def get_library_for_node_type(cls, node_type: str, specific_library_name: str | None = None) -> Library:
        instance = cls()

        if specific_library_name is None:
            # Find its library.
            libraries_with_node_type = LibraryRegistry.get_libraries_with_node_type(node_type)
            if len(libraries_with_node_type) == 1:
                specific_library_name = libraries_with_node_type[0]
                dest_library = instance.get_library(specific_library_name)
            elif len(libraries_with_node_type) > 1:
                msg = f"Attempted to create a node of type '{node_type}' with no library name specified. The following libraries have nodes in them with the same name: {libraries_with_node_type}. In order to disambiguate, specify the library this node should come from."
                raise KeyError(msg)
            else:
                msg = f"No node type '{node_type}' could be found in any of the libraries registered."
                raise KeyError(msg)
        else:
            # See if the library exists.
            dest_library = instance.get_library(specific_library_name)

        return dest_library

    @classmethod
    def create_node(
        cls,
        node_type: str,
        name: str,
        metadata: dict[Any, Any] | None = None,
        specific_library_name: str | None = None,
    ) -> BaseNode:
        instance = cls()

        dest_library = instance.get_library_for_node_type(
            node_type=node_type, specific_library_name=specific_library_name
        )

        with cls.constructing_node():
            return dest_library.create_node(node_type=node_type, name=name, metadata=metadata)

    @classmethod
    @contextmanager
    def constructing_node(cls) -> Iterator[None]:
        """Mark the enclosed block as a node ``__init__`` running on the calling task.

        Sets the same task-local flag that ``create_node`` sets. Use at
        any direct construction site that bypasses ``create_node``
        (e.g. ``type(node)(name=...)`` or ``node_class(name=...)`` for
        an ephemeral probe / reference node), so:

        - the parameter-mutation-during-aprocess detector skips the
          declarative ``add_parameter`` calls inside the constructed
          node's ``__init__`` (otherwise it would fire once per
          parameter declared by the helper instance), and
        - the reentrant-bus-in-init detector still fires for the
          right reason if the constructed node's ``__init__`` issues
          a bus request.
        """
        token = _constructing_node.set(True)
        try:
            yield
        finally:
            _constructing_node.reset(token)

    @classmethod
    def is_constructing_node(cls) -> bool:
        """Return True if a node ``__init__`` is currently running on the calling task.

        The reentrant-bus-in-init and parameter-mutation-during-aprocess
        strict-mode detectors consult this so they can fire from outside
        ``LibraryRegistry`` without owning their own depth counter. The
        flag is set by ``create_node`` and by ``constructing_node()``.
        """
        return _constructing_node.get()

    @classmethod
    def get_all_library_schemas(cls) -> dict[str, dict]:
        """Get schemas from all loaded libraries.

        Returns:
            Dictionary mapping category names to their JSON Schema dicts
        """
        instance = cls()
        schemas = {}

        # Get explicit schemas from loaded libraries
        for library in instance._libraries.values():
            library_data = library.get_library_data()
            if library_data.settings:
                for setting in library_data.settings:
                    if setting.json_schema:
                        schemas[setting.category] = {
                            "type": "object",
                            "properties": setting.json_schema,
                            "title": setting.description or f"{setting.category.title()} Settings",
                        }
                    else:
                        # Create fallback schema for settings without explicit schemas
                        schemas[setting.category] = {
                            "type": "object",
                            "title": setting.description or f"{setting.category.title()} Settings",
                        }

        return schemas


class Library:
    """A collection of nodes curated by library author.

    Handles registration and creation of nodes.
    """

    _library_data: LibrarySchema
    _is_default_library: bool
    # Maintain fast lookups for node class name to class and to its metadata.
    _node_types: dict[str, type[BaseNode]]
    _node_metadata: dict[str, NodeMetadata]
    _advanced_library: AdvancedNodeLibrary | None
    # Tracks handlers registered on behalf of this library so they can be
    # deregistered automatically when the library is unloaded.
    _registered_app_event_listeners: list[tuple[type, Callable]]
    _registered_pre_dispatch_hooks: list[Callable]
    _registered_request_handler_types: list[type]

    def __init__(
        self,
        library_data: LibrarySchema,
        *,
        is_default_library: bool = False,
        advanced_library: AdvancedNodeLibrary | None = None,
    ) -> None:
        self._library_data = library_data

        # If they didn't make it explicit, allow an override.
        if self._library_data.is_default_library is None:
            self._library_data.is_default_library = is_default_library

        self._is_default_library = self._library_data.is_default_library

        self._node_types = {}
        self._node_metadata = {}
        self._advanced_library = advanced_library
        self._registered_app_event_listeners = []
        self._registered_pre_dispatch_hooks = []
        self._registered_request_handler_types = []

    def get_registered_app_event_listeners(self) -> list[tuple[type, Callable]]:
        return list(self._registered_app_event_listeners)

    def get_registered_pre_dispatch_hooks(self) -> list[Callable]:
        return list(self._registered_pre_dispatch_hooks)

    def get_registered_request_handler_types(self) -> list[type]:
        """Return the request payload types whose handlers this library has registered.

        Tracked for two purposes:
        - **Teardown**: the engine calls this during ``unregister_library`` to remove
          all handlers automatically when the library is unloaded.
        - **Introspection**: other libraries or nodes can call this to discover what
          request types this library exposes, then use ``dataclasses.fields()`` and
          ``typing.get_type_hints()`` on each type to inspect its field schema.

        Returns a copy; mutating the returned list has no effect.
        """
        return list(self._registered_request_handler_types)

    def register_new_node_type(self, node_class: type[BaseNode], metadata: NodeMetadata) -> LibraryProblem | None:
        """Register a new node type in this library. Returns a LibraryProblem if registration fails, or None if all clear."""
        # We only need to register the name of the node within the library.
        node_class_as_str = node_class.__name__

        # Let the registry know.
        library_problem = LibraryRegistry.register_node_type_from_library(
            library=self, node_class_name=node_class_as_str
        )

        self._node_types[node_class_as_str] = node_class
        self._node_metadata[node_class_as_str] = metadata
        return library_problem

    def unregister_node_type(self, node_class_name: str) -> None:
        """Remove a single node type from this library.

        Exists to support incremental re-registration (e.g. an agent iterates on a sandbox
        node's source code during a session). Does not touch existing node instances of this
        class that are already living in a flow; callers are responsible for deleting and
        recreating them if they want the new class to take effect.
        """
        if node_class_name not in self._node_types:
            msg = (
                f"Node type '{node_class_name}' was requested to be unregistered from library "
                f"'{self._library_data.name}', but it wasn't registered in the first place."
            )
            raise KeyError(msg)
        del self._node_types[node_class_name]
        self._node_metadata.pop(node_class_name, None)

    def get_library_data(self) -> LibrarySchema:
        return self._library_data

    def get_models_for_node_type(self, node_type: str) -> list[ResolvedModel]:
        """Resolve the catalog models a node type is declared to use.

        Returns the models referenced by the node's ``model_usage`` /
        ``model_provider_usage`` declarations, resolved against this library's
        ``model_catalog`` declaration. Returns an empty list when the node
        declares no model usage or the library declares no catalog.

        Raises:
            KeyError: if ``node_type`` is not registered in this library.
        """
        node_metadata = self._node_metadata.get(node_type)
        if node_metadata is None:
            msg = f"Node type '{node_type}' not found in library '{self._library_data.name}'"
            raise KeyError(msg)
        catalog = find_model_catalog(self._library_data.metadata.declarations)
        if catalog is None:
            return []
        return resolve_node_models(catalog, node_metadata.declarations)

    def create_node(
        self,
        node_type: str,
        name: str,
        metadata: dict[Any, Any] | None = None,
    ) -> BaseNode:
        """Create a new node instance of the specified type."""
        node_class = self._node_types.get(node_type)
        if not node_class:
            msg = f"Node type '{node_type}' not found in library '{self._library_data.name}'"
            raise KeyError(msg)
        # Inject the metadata ABOUT the node from the Library
        # into the node's metadata blob.
        if metadata is None:
            metadata = {}
        # Dump to a JSON-safe dict so downstream consumers (and the workflow
        # serializer in particular) only ever see plain literals — no Pydantic
        # models, no StrEnum members. Without this, a NodeMetadata carrying a
        # LifecycleStageNodeProperty would leak through to the generated workflow
        # as a Python repr (e.g. `<LifecycleStage.BETA: 'BETA'>`), which is not
        # valid Python.
        library_node_metadata_model = self._node_metadata.get(node_type)
        if library_node_metadata_model is None:
            metadata["library_node_metadata"] = {}
        else:
            metadata["library_node_metadata"] = library_node_metadata_model.model_dump(mode="json")
        metadata["library"] = self._library_data.name
        metadata["node_type"] = node_type
        node = node_class(name=name, metadata=metadata)
        return node

    def get_registered_nodes(self) -> list[str]:
        """Get a list of all registered node types."""
        return list(self._node_types.keys())

    def has_node_type(self, node_type: str) -> bool:
        return node_type in self._node_types

    def get_node_metadata(self, node_type: str) -> NodeMetadata:
        if node_type not in self._node_metadata:
            raise KeyError(self._library_data.name, node_type)
        return self._node_metadata[node_type]

    def get_node_class(self, node_type: str) -> type[BaseNode]:
        """Return the BaseNode subclass registered under `node_type`.

        For callers that need the class itself, e.g. classmethod checks like
        `allow_outgoing_connection_by_class`, rather than an instance produced
        by `create_node`.
        """
        if node_type not in self._node_types:
            raise KeyError(self._library_data.name, node_type)
        return self._node_types[node_type]

    def get_categories(self) -> list[dict[str, CategoryDefinition]]:
        return self._library_data.categories

    def is_default_library(self) -> bool:
        return self._is_default_library

    def get_metadata(self) -> LibraryMetadata:
        return self._library_data.metadata

    def get_advanced_library(self) -> AdvancedNodeLibrary | None:
        """Get the advanced library instance for this library.

        Returns:
            The AdvancedNodeLibrary instance, or None if not set
        """
        return self._advanced_library

    def get_nodes_by_base_type(self, base_type: type) -> list[str]:
        """Get all node types in this library that are subclasses of the specified base type.

        Args:
            base_type: The base class to filter by (e.g., StartNode, ControlNode)

        Returns:
            List of node type names that extend the base type
        """
        matching_nodes = []
        for node_type, node_class in self._node_types.items():
            if issubclass(node_class, base_type):
                matching_nodes.append(node_type)
        return matching_nodes


def get_declared_models(node: BaseNode) -> list[ResolvedModel]:
    """Resolve the catalog models a node is declared to use.

    Reads the ``library`` and ``node_type`` that ``Library.create_node`` injects
    into the node's metadata, looks up that library, and resolves the node's
    ``model_usage`` / ``model_provider_usage`` declarations against its
    ``model_catalog``. A node calls this to build its model dropdown from the
    catalog, passing only ``self`` -- it never restates its own library/type,
    nothing is stored on the node, and nothing is serialized.

    The catalog is library-local, so this is an in-process lookup that resolves
    correctly in both the orchestrator and a worker subprocess, including from
    ``__init__``. Each returned ``ResolvedModel`` carries the model descriptor
    (``model.display_name``, ``model.provider_model_id``) the node needs to map
    a dropdown selection back to the provider's model id.

    Returns an empty list when the node declares no model usage, its library
    declares no catalog, or the library/type cannot be resolved (e.g. a node
    constructed outside the normal library path).
    """
    library_name = node.metadata.get("library")
    node_type = node.metadata.get("node_type")
    if not isinstance(library_name, str) or not isinstance(node_type, str):
        return []
    try:
        library = LibraryRegistry.get_library(name=library_name)
        return library.get_models_for_node_type(node_type)
    except KeyError:
        return []
