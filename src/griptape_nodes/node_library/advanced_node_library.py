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
        post-dispatch hooks, or request handlers, and before the library is removed
        from LibraryRegistry.
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

            from griptape_nodes.node_library.library_registry import LibraryRegistry
            import dataclasses, typing

            library = LibraryRegistry.get_library("My Library Name")
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

    def get_post_dispatch_hooks(
        self,
    ) -> list[
        tuple[
            type[RequestPayload],
            Callable[[RequestPayload, ResultPayload], None]
            | Callable[[RequestPayload, ResultPayload], Awaitable[None]],
        ]
    ]:
        """Return post-dispatch hooks to register with the engine.

        Each entry is a (request_type, callback) pair. After the engine's own
        handler for that request type produces a result, the callback is invoked
        with (request, result). Unlike get_request_handlers(), this does not claim
        the request type — any number of libraries may hook the same type, and the
        engine's handler still runs normally.

        The engine registers all returned hooks after after_library_nodes_loaded()
        and deregisters them automatically when the library is unloaded.

        Both sync and async callbacks are supported. Sync callbacks run in a worker
        thread; async callbacks run on the engine's event loop and must not block it.

        **Notification only.** The callback's return value is ignored and it cannot
        alter the result or fail the operation. A callback that raises is logged and
        otherwise ignored.

        **Usually detached.** Whenever the engine has a live event loop the hook is
        scheduled as a detached task, so the result reaches the client without waiting
        for it. Some paths have no such loop — CLI commands, bootstrap workflow runs,
        worker threads — and there the hook runs inline and blocks the caller until it
        returns, on a transient loop rather than the engine's. Keep hooks quick, or move
        slow work off-process, if they may fire on those paths.

        **Both outcomes.** The callback is invoked for successes and failures alike,
        including failures produced by an exception escaping the handler. Branch on
        the result type to filter.

        **Exact type matching.** A hook registered for a request type fires only for
        that exact type, not for its subclasses.

        **Read-only arguments.** Do not mutate the request or the result: both are
        still referenced by the result event the engine is about to serialize. Fields
        marked ``omit_from_result`` have already been cleared on the request the hook
        receives.

        **Do not issue engine requests from a hook.** The engine's operation-depth and
        node-execution state is process-wide, so a request issued from a hook can
        perturb an in-flight operation. Do external work (HTTP, file writes) instead.

        **Orchestrator process only.** Hooks are registered on the event manager of
        whichever process loads the library, so a worker-mode library's hooks never
        observe requests handled by the orchestrator. Cross-worker hook support is
        tracked in GH#4748.

        **Not durable.** In-flight hooks are abandoned at process exit. Do not use
        them where delivery must be guaranteed.

        Example:
            def get_post_dispatch_hooks(self):
                return [
                    (SaveWorkflowRequest, self._on_workflow_saved),
                ]
        """
        return []
