from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from griptape_nodes.node_library.library_registry import Library, LibrarySchema
    from griptape_nodes.retained_mode.events.base_events import RequestPayload, ResultPayload


class AdvancedNodeLibrary:
    """Base class for advanced node libraries with callback support.

    Library modules can inherit from this class to provide custom initialization
    and cleanup logic that runs before and after node loading.

    Example usage:
        ```python
        # In your library's advanced library module file:
        from griptape_nodes.node_library.advanced_node_library import AdvancedNodeLibrary

        class MyLibrary(AdvancedNodeLibrary):
            def before_library_nodes_loaded(self, library_data, library):
                # Set up any prerequisites before nodes are loaded
                print(f"About to load nodes for {library_data.name}")

            def after_library_nodes_loaded(self, library_data, library):
                # Perform any cleanup or additional setup after nodes are loaded
                print(f"Finished loading {len(library.get_registered_nodes())} nodes")
        ```
    """

    def before_library_nodes_loaded(self, library_data: LibrarySchema, library: Library) -> None:
        """Called before any nodes are loaded from the library.

        This method is called after the library instance is created but before
        any individual node classes are dynamically loaded and registered.

        Args:
            library_data: The library schema containing metadata and node definitions
            library: The library instance that will contain the loaded nodes
        """

    def after_library_nodes_loaded(self, library_data: LibrarySchema, library: Library) -> None:
        """Called after all nodes have been loaded from the library.

        This method is called after all node classes have been successfully
        loaded and registered with the library.

        Args:
            library_data: The library schema containing metadata and node definitions
            library: The library instance containing the loaded nodes
        """

    def before_library_unregistered(self, library_data: LibrarySchema, library: Library) -> None:
        """Called before the library is unregistered from the engine.

        Called before the engine deregisters any event listeners, pre-dispatch hooks,
        or request handlers, and before the library is removed from LibraryRegistry.
        Use it to release external resources acquired during load — Python bindings,
        GPU contexts, background threads, connection pools, etc.

        Errors raised here are logged and swallowed; unregistration continues
        regardless so a failing teardown cannot leave the engine in a stuck state.

        Args:
            library_data: The library schema containing metadata and node definitions
            library: The library instance being unregistered
        """

    def get_request_handlers(
        self,
    ) -> list[
        tuple[
            type[RequestPayload],
            Callable[[RequestPayload], ResultPayload] | Callable[[RequestPayload], Awaitable[ResultPayload]],
        ]
    ]:
        """Return request/response handlers to register with the engine.

        Each entry is a (request_type, handler) pair. The library must own the
        request_type — it should be a RequestPayload subclass defined within
        this library's package. Each request type maps to exactly one handler
        engine-wide; attempting to register a type that already has a handler
        raises a ValueError, surfaced as a RequestHandlerRegistrationProblem.

        The engine registers all returned handlers after after_library_nodes_loaded()
        and deregisters them automatically when the library is unloaded.

        Both sync and async handler callables are supported.

        **Orchestrator process only.** Handlers registered via this method run in
        the orchestrator process. Libraries loaded in worker processes will not have
        their handlers forwarded to the orchestrator, so requests dispatched there
        will result in "No manager found". Cross-worker handler support is tracked
        in GH#4748.

        **Singleton handlers only.** This mechanism is for services where exactly
        one library is the provider (e.g. colour conversion, ML inference). For
        competing-provider scenarios — where multiple libraries can each handle the
        same request type and the caller selects one by name at dispatch time (e.g.
        ``PublishWorkflowRequest``) — use ``LibraryManager.on_register_event_handler()``
        in your ``after_library_nodes_loaded`` callback instead. Unification of the
        two registration systems is tracked as future work.

        **Introspection.** Once a library is loaded, other libraries or nodes can
        discover which request types it registered and inspect their field schemas
        using standard Python APIs::

            from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes
            import dataclasses, typing

            library = GriptapeNodes.LibraryRegistry().get_library("My Library Name")
            for request_type in library.get_registered_request_handler_types():
                hints = typing.get_type_hints(request_type)
                fields = dataclasses.fields(request_type)
                # hints: {field_name: type, ...}
                # fields: tuple of dataclasses.Field objects with name/default/metadata

        Example:
            def get_request_handlers(self):
                return [
                    (ConvertColorspaceRequest, self._handle_convert_colorspace),
                ]
        """
        return []
