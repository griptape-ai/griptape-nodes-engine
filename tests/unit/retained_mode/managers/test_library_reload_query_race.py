"""Regression tests for the "refresh libraries produces more errors" race.

Reloading libraries is not atomic. `_run_reload_libraries` empties the registry
(ClearAllObjectState, then an unload loop) and only afterwards calls
`load_all_libraries_from_config`, which re-registers libraries one at a time. In the
reported session that window was ~29s wide: the reload began at 14:27:16 and finished at
14:27:45, and the editor's update sweep landed inside it at 14:27:26-31, producing one
hard failure per library that had not yet been re-registered:

    ERROR  Attempted to get all library info for a Library named 'Sendgrid Library'.
           Failed because no Library with that name was registered.
    ERROR  Attempted to check for updates for Library 'Topaz Labs'.
           Failed because no Library with that name was registered.

Every one of those libraries registered successfully seconds later, so the failures were
purely an artifact of being asked mid-reload.

These tests pin two properties. Every GUI-facing query awaits
`_libraries_loading_complete` rather than answering from a half-empty registry. And the
gate closes before the unload loop, so it covers the window where the registry is empty --
but no earlier than the enumeration that precedes it, which waits on the same gate and
would otherwise deadlock the reload against itself.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest

from griptape_nodes.node_library.library_registry import (
    LibraryMetadata,
    LibraryRegistry,
    LibrarySchema,
)
from griptape_nodes.retained_mode.events.library_events import (
    CheckLibraryUpdateRequest,
    GetAllInfoForAllLibrariesRequest,
    GetAllInfoForLibraryRequest,
    GetAllInfoForLibraryResultFailure,
    ReloadAllLibrariesRequest,
    ReloadAllLibrariesResultFailure,
    UnloadLibraryFromRegistryRequest,
)

if TYPE_CHECKING:
    from collections.abc import Generator

    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.events.base_events import RequestPayload, ResultPayload

# A library the engine had registered before the reload and re-registers after it.
# Mirrors 'Sendgrid Library' / 'Topaz Labs' from the log: fine before, fine after,
# "not registered" only while the reload is in flight.
LIBRARY_NAME = "Sendgrid Library"


def _schema(name: str) -> LibrarySchema:
    return LibrarySchema(
        name=name,
        library_schema_version=LibrarySchema.LATEST_SCHEMA_VERSION,
        metadata=LibraryMetadata(
            author="test",
            description="library that is mid-reload",
            library_version="0.1.0",
            engine_version="0.98.0",
            tags=[],
        ),
        categories=[],
        nodes=[],
    )


class TestQueriesDuringLibraryReload:
    """GUI-facing library queries wait for the reload instead of failing hard."""

    @pytest.fixture(autouse=True)
    def _clean_registry(self) -> Generator[None, None, None]:
        LibraryRegistry._clear()
        yield
        LibraryRegistry._clear()

    @staticmethod
    def _enter_reload_window(engine: Engine) -> None:
        """Put the manager in the state `_run_reload_libraries` creates mid-flight.

        The registry has been emptied by the unload loop and `load_all_libraries_from_config`
        has not finished re-registering, so the loading gate is closed.
        """
        library_manager = engine.library_manager
        LibraryRegistry._clear()
        library_manager._libraries_loading_complete = asyncio.Event()

    @pytest.mark.asyncio
    async def test_check_library_update_waits_for_the_reload(self, engine: Engine) -> None:
        """An update check mid-reload waits instead of reporting the library missing."""
        library_manager = engine.library_manager
        LibraryRegistry.generate_new_library(library_data=_schema(LIBRARY_NAME))

        # Before the reload the library answers normally: it is in the registry.
        assert LIBRARY_NAME in set(LibraryRegistry.list_libraries())

        self._enter_reload_window(engine)

        pending = asyncio.create_task(
            library_manager.check_library_update_request(CheckLibraryUpdateRequest(library_name=LIBRARY_NAME))
        )
        await asyncio.sleep(0)
        assert not pending.done(), "the update check should wait on the loading gate, not fail"

        # The reload re-registers the library and reopens the gate.
        LibraryRegistry.generate_new_library(library_data=_schema(LIBRARY_NAME))
        library_manager._libraries_loading_complete.set()

        result = await pending

        # It no longer claims the library was never registered. (It still fails, because the
        # temp library is not a git repo -- a different, honest reason.)
        assert "no Library with that name was registered" not in str(result.result_details)

    @pytest.mark.asyncio
    async def test_both_get_all_info_entry_points_wait_on_the_gate(self, engine: Engine) -> None:
        """The per-library and all-libraries entry points now agree about the reload window.

        The gate lives on the `on_*` entry points, not in the handlers they call, so an
        internal caller cannot end up waiting on a reload it is already running inside.
        """
        library_manager = engine.library_manager
        LibraryRegistry.generate_new_library(library_data=_schema(LIBRARY_NAME))

        self._enter_reload_window(engine)

        singular = asyncio.create_task(
            library_manager.on_get_all_info_for_library_request(GetAllInfoForLibraryRequest(library=LIBRARY_NAME))
        )
        plural = asyncio.create_task(
            library_manager.on_get_all_info_for_all_libraries_request(GetAllInfoForAllLibrariesRequest())
        )
        await asyncio.sleep(0)
        assert not singular.done(), "the per-library handler is expected to wait on the loading gate"
        assert not plural.done(), "the all-libraries handler is expected to wait on the loading gate"

        # Finish the reload the way load_all_libraries_from_config does: re-register the
        # library, then reopen the gate. Both queries then see the rebuilt registry.
        LibraryRegistry.generate_new_library(library_data=_schema(LIBRARY_NAME))
        library_manager._libraries_loading_complete.set()
        singular_result = await singular
        plural_result = await plural

        assert not isinstance(singular_result, GetAllInfoForLibraryResultFailure)
        assert plural_result.succeeded()

    @pytest.mark.asyncio
    async def test_reload_closes_the_gate_before_emptying_the_registry(self, engine: Engine) -> None:
        """The gate must cover the whole reload, including the unload loop.

        `_run_reload_libraries` empties the registry (ClearAllObjectState + the unload loop)
        before `load_all_libraries_from_config` runs. If the gate only closed there, the
        registry would already be empty while the gate still read "loaded", and a handler
        awaiting it would sail through onto an empty registry. So the gate closes first.
        """
        library_manager = engine.library_manager
        LibraryRegistry.generate_new_library(library_data=_schema(LIBRARY_NAME))

        observed: dict[str, object] = {}

        async def spy_load_all_libraries_from_config(*_args: object, **_kwargs: object) -> list[str]:
            # Sampled at the moment reload hands off to the loading phase.
            observed["gate_was_open"] = library_manager._libraries_loading_complete.is_set()
            observed["registry_was_empty"] = set(LibraryRegistry.list_libraries()) == set()
            return []

        with (
            patch.object(
                library_manager, "load_all_libraries_from_config", side_effect=spy_load_all_libraries_from_config
            ),
            patch.object(library_manager, "_maybe_start_workers_for_existing_session", AsyncMock()),
            patch.object(library_manager, "_await_pending_workers", AsyncMock()),
        ):
            await library_manager._run_reload_libraries(ReloadAllLibrariesRequest())

        assert observed["registry_was_empty"] is True, "the unload loop should have emptied the registry"
        assert observed["gate_was_open"] is False, (
            "the gate must already be closed by the time the registry is empty, so queries "
            "wait for the rebuild instead of seeing a registry with nothing in it"
        )

    @pytest.mark.asyncio
    async def test_reload_reopens_the_gate_even_when_loading_raises(self, engine: Engine) -> None:
        """A failed rebuild must not leave the gate closed forever.

        Otherwise every gated query would hang for the life of the process.
        """
        library_manager = engine.library_manager
        failure = RuntimeError("discovery blew up")

        async def boom(*_args: object, **_kwargs: object) -> list[str]:
            raise failure

        with (
            patch.object(library_manager, "_reconcile_libraries_from_config", side_effect=boom),
            pytest.raises(RuntimeError, match="discovery blew up"),
        ):
            await library_manager.load_all_libraries_from_config()

        assert library_manager._libraries_loading_complete.is_set()

    @pytest.mark.asyncio
    async def test_reload_reopens_the_gate_when_it_bails_before_loading(self, engine: Engine) -> None:
        """A reload that fails between closing the gate and loading must not wedge queries.

        The gate closes just before the unload loop, so an early return from that loop would
        otherwise leave every gated query waiting for the life of the process.
        """
        library_manager = engine.library_manager
        LibraryRegistry.generate_new_library(library_data=_schema(LIBRARY_NAME))

        failed_unload = ReloadAllLibrariesResultFailure(result_details="unload failed")
        real_handle_request = engine.handle_request

        def refuse_unload(request: RequestPayload) -> ResultPayload:
            if isinstance(request, UnloadLibraryFromRegistryRequest):
                return failed_unload
            return real_handle_request(request)

        with (
            patch.object(library_manager, "load_all_libraries_from_config", AsyncMock(return_value=[])) as load_all,
            patch.object(library_manager.engine, "handle_request", side_effect=refuse_unload),
        ):
            result = await library_manager._run_reload_libraries(ReloadAllLibrariesRequest())

        assert isinstance(result, ReloadAllLibrariesResultFailure)
        load_all.assert_not_called()
        assert library_manager._libraries_loading_complete.is_set(), (
            "a reload that bailed before loading must reopen the gate it closed"
        )

    @pytest.mark.asyncio
    async def test_gated_handler_would_have_returned_the_right_answer(self, engine: Engine) -> None:
        """Waiting is the correct behavior: the awaited query is answerable once the reload ends.

        This is the payoff the gate rests on. A handler that waits and then returns a failure
        would satisfy the waiting assertions above but defeat the point, so assert the answer.
        """
        library_manager = engine.library_manager
        self._enter_reload_window(engine)

        pending = asyncio.create_task(
            library_manager.on_get_all_info_for_all_libraries_request(GetAllInfoForAllLibrariesRequest())
        )
        await asyncio.sleep(0)
        assert not pending.done()

        # The reload re-registers the library, then opens the gate.
        LibraryRegistry.generate_new_library(library_data=_schema(LIBRARY_NAME))
        library_manager._libraries_loading_complete.set()

        result = await pending
        assert result.succeeded()
        assert LIBRARY_NAME in set(LibraryRegistry.list_libraries())
