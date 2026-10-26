from __future__ import annotations

from contextlib import contextmanager
from enum import StrEnum
from typing import TYPE_CHECKING

from griptape_nodes.retained_mode.events.app_events import AppInitializationComplete, EngineReadyEvent
from griptape_nodes.retained_mode.events.base_events import AppEvent

if TYPE_CHECKING:
    from collections.abc import Iterator

    from griptape_nodes.retained_mode.engine import Engine


class EngineLifecycleState(StrEnum):
    STARTING = "starting"
    READY = "ready"
    RELOADING = "reloading"


class EngineLifecycle:
    """The engine's startup sequence and whether the engine is currently initializing.

    Owned by `Engine`, which wires it to `AppInitializationComplete`. It sequences steps across
    managers, so it lives beside the engine rather than among them.
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        # Starts STARTING because the engine publishes its heartbeat and registry record before
        # AppInitializationComplete arrives, and a client that reads "not initializing" then
        # treats a request timeout as a dead engine instead of waiting.
        self._state = EngineLifecycleState.STARTING

    @property
    def state(self) -> EngineLifecycleState:
        return self._state

    def is_initializing(self) -> bool:
        """Return True until startup finishes, and during a library reload.

        Covers the full startup sequence (library and workflow loading), not just the library
        phase. Reported on the engine heartbeat.
        """
        return self._state is not EngineLifecycleState.READY

    @contextmanager
    def reloading(self) -> Iterator[None]:
        """Report the engine as initializing for the duration of a mid-session library reload.

        The state returns to READY even if the reload raises, so a failure cannot leave clients
        waiting on an engine that will never finish.
        """
        self._state = EngineLifecycleState.RELOADING
        try:
            yield
        finally:
            self._state = EngineLifecycleState.READY

    async def on_app_initialization_complete(self, payload: AppInitializationComplete) -> None:
        # The state already starts STARTING; setting it again covers a repeated broadcast.
        # finally guarantees the state reaches READY even if startup raises.
        self._state = EngineLifecycleState.STARTING
        try:
            await self._run_startup(payload)
        finally:
            self._state = EngineLifecycleState.READY

    def announce_ready(self, *, is_initial_start: bool) -> None:
        """Signal readiness so the application layer can render its library status table.

        Consumers (the app's "engine ready" banner, the GUI, the desktop app) react to the same
        event. Only the orchestrator announces readiness; dedicated library workers do not.
        Presentation is owned by the app, not the engine. The statuses are the orchestrator's own
        verdicts, derived from loading each library's real node modules here.
        """
        library_manager = self._engine.library_manager
        if library_manager.is_worker:
            return
        self._engine.event_manager.put_event(
            AppEvent(
                payload=EngineReadyEvent(
                    libraries=library_manager.collect_library_load_statuses(),
                    is_initial_start=is_initial_start,
                )
            )
        )

    async def _run_startup(self, payload: AppInitializationComplete) -> None:
        engine = self._engine
        if payload.skip_library_loading:
            # Register all secrets even in headless mode
            engine.secrets_manager.register_all_secrets()

            # Still need to tell WorkflowManager to register workflows
            # Pass the specific workflows if provided, otherwise it will scan workspace
            await engine.workflow_manager.refresh_workflow_registry(workflows_to_register=payload.workflows_to_register)
            return

        library_manager = engine.library_manager
        await library_manager.load_libraries_for_startup(payload)

        # When the orchestrator restarts into an already-active session, the GUI will not
        # send AppStartSessionRequest again, so workers must be started here.
        await library_manager.workers.maybe_start_workers_for_existing_session()

        # Register all secrets now that libraries are loaded and settings are merged
        engine.secrets_manager.register_all_secrets()

        # We have to load all libraries before we attempt to load workflows. The refresh registers
        # library-declared workflows first, then user workflows.
        await engine.workflow_manager.refresh_workflow_registry()

        self.announce_ready(is_initial_start=True)
