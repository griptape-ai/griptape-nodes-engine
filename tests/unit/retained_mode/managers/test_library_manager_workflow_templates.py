"""Tests for registering and unregistering a library's `workflows` templates."""

from __future__ import annotations

import asyncio
import contextlib
import sys
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from griptape_nodes.node_library.library_registry import Library, LibraryMetadata, LibrarySchema
from griptape_nodes.node_library.workflow_registry import WorkflowRegistry
from griptape_nodes.retained_mode.events.app_events import LibraryWorkflowTemplatesChanged
from griptape_nodes.retained_mode.events.library_events import (
    RegisterLibraryFromFileRequest,
    ReloadAllLibrariesRequest,
    UnloadLibraryFromRegistryRequest,
)
from griptape_nodes.retained_mode.managers.library_manager import LibraryManager
from griptape_nodes.retained_mode.managers.workflow_manager import WorkflowRegistrationResult

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from griptape_nodes.retained_mode.engine import Engine

LIBRARY_MANAGER_MODULE = "griptape_nodes.retained_mode.managers.library_manager"
LIBRARY_NAME = "TestLib"


def _library_info(library_path: Path) -> LibraryManager.LibraryInfo:
    return LibraryManager.LibraryInfo(
        lifecycle_state=LibraryManager.LibraryLifecycleState.LOADED,
        fitness=LibraryManager.LibraryFitness.GOOD,
        library_path=str(library_path),
        is_sandbox=False,
        library_name=LIBRARY_NAME,
        library_version="1.0.0",
    )


def _library(workflows: list[str] | None) -> Library:
    schema = LibrarySchema(
        name=LIBRARY_NAME,
        library_schema_version=LibrarySchema.LATEST_SCHEMA_VERSION,
        metadata=LibraryMetadata(
            author="Test",
            description="Test",
            library_version="1.0.0",
            engine_version="0.0.0",
            tags=[],
        ),
        categories=[],
        nodes=[],
        workflows=workflows,
    )
    return Library(library_data=schema)


def _emitted_template_changes(event_manager: MagicMock) -> list[LibraryWorkflowTemplatesChanged]:
    """Pull the template-change payloads out of a mocked event manager's put_event calls."""
    payloads = [call.args[0].payload for call in event_manager.put_event.call_args_list]
    return [payload for payload in payloads if isinstance(payload, LibraryWorkflowTemplatesChanged)]


class TestCollectWorkflowFilesForLibrary:
    def test_resolves_paths_against_the_library_directory(self, griptape_nodes: Engine, tmp_path: Path) -> None:
        library_json = tmp_path / "griptape_nodes_library.json"
        library = _library(["workflows/example.py", "other.py"])

        with patch(f"{LIBRARY_MANAGER_MODULE}.LibraryRegistry.get_library", return_value=library):
            collected = griptape_nodes.library_manager._collect_workflow_files_for_library(_library_info(library_json))

        assert collected == [
            str(tmp_path / "workflows/example.py"),
            str(tmp_path / "other.py"),
        ]

    def test_adds_the_library_directory_to_sys_path_only_once(self, griptape_nodes: Engine, tmp_path: Path) -> None:
        """Repeat registrations (reinstall, reload) must not grow sys.path without bound."""
        library_json = tmp_path / "griptape_nodes_library.json"
        library = _library(["workflows/example.py"])
        original_sys_path = list(sys.path)

        try:
            with patch(f"{LIBRARY_MANAGER_MODULE}.LibraryRegistry.get_library", return_value=library):
                griptape_nodes.library_manager._collect_workflow_files_for_library(_library_info(library_json))
                griptape_nodes.library_manager._collect_workflow_files_for_library(_library_info(library_json))

            assert sys.path.count(str(tmp_path)) == 1
        finally:
            sys.path[:] = original_sys_path

    def test_returns_nothing_when_the_library_declares_no_workflows(
        self, griptape_nodes: Engine, tmp_path: Path
    ) -> None:
        library_json = tmp_path / "griptape_nodes_library.json"

        with patch(f"{LIBRARY_MANAGER_MODULE}.LibraryRegistry.get_library", return_value=_library(None)):
            collected = griptape_nodes.library_manager._collect_workflow_files_for_library(_library_info(library_json))

        assert collected == []

    def test_returns_nothing_when_the_library_is_not_registered(self, griptape_nodes: Engine, tmp_path: Path) -> None:
        library_json = tmp_path / "griptape_nodes_library.json"

        with patch(f"{LIBRARY_MANAGER_MODULE}.LibraryRegistry.get_library", side_effect=KeyError(LIBRARY_NAME)):
            collected = griptape_nodes.library_manager._collect_workflow_files_for_library(_library_info(library_json))

        assert collected == []


class TestRegisterWorkflowFilesForLibrary:
    @pytest.mark.asyncio
    async def test_records_registered_keys_and_announces_them(self, griptape_nodes: Engine, tmp_path: Path) -> None:
        library_manager = griptape_nodes.library_manager
        register = AsyncMock(return_value=WorkflowRegistrationResult(succeeded=["templates/example"], failed=[]))
        event_manager = MagicMock()

        with (
            patch(f"{LIBRARY_MANAGER_MODULE}.LibraryRegistry.get_library", return_value=_library(["example.py"])),
            patch.object(griptape_nodes.workflow_manager, "register_list_of_workflows", register),
            patch.object(griptape_nodes, "_event_manager", event_manager),
        ):
            await library_manager._register_workflow_files_for_library(_library_info(tmp_path / "lib.json"))

        assert library_manager._library_to_workflow_keys[LIBRARY_NAME] == ["templates/example"]
        changes = _emitted_template_changes(event_manager)
        assert len(changes) == 1
        assert changes[0].library_name == LIBRARY_NAME
        assert changes[0].workflow_names == ["templates/example"]
        assert changes[0].registered is True

    @pytest.mark.asyncio
    async def test_ignores_paths_that_were_already_registered(self, griptape_nodes: Engine, tmp_path: Path) -> None:
        """A template someone else already owns must survive this library being unloaded."""
        library_manager = griptape_nodes.library_manager
        register = AsyncMock(return_value=WorkflowRegistrationResult(succeeded=[], failed=["already_there.py"]))
        event_manager = MagicMock()

        with (
            patch(f"{LIBRARY_MANAGER_MODULE}.LibraryRegistry.get_library", return_value=_library(["already_there.py"])),
            patch.object(griptape_nodes.workflow_manager, "register_list_of_workflows", register),
            patch.object(griptape_nodes, "_event_manager", event_manager),
        ):
            await library_manager._register_workflow_files_for_library(_library_info(tmp_path / "lib.json"))

        assert library_manager._library_to_workflow_keys[LIBRARY_NAME] == []
        assert _emitted_template_changes(event_manager) == []

    @pytest.mark.asyncio
    async def test_does_nothing_on_a_worker(self, griptape_nodes: Engine, tmp_path: Path) -> None:
        library_manager = griptape_nodes.library_manager
        library_manager._is_worker = True
        register = AsyncMock(return_value=WorkflowRegistrationResult(succeeded=["templates/example"], failed=[]))

        with (
            patch(f"{LIBRARY_MANAGER_MODULE}.LibraryRegistry.get_library", return_value=_library(["example.py"])),
            patch.object(griptape_nodes.workflow_manager, "register_list_of_workflows", register),
        ):
            await library_manager._register_workflow_files_for_library(_library_info(tmp_path / "lib.json"))

        register.assert_not_awaited()
        assert LIBRARY_NAME not in library_manager._library_to_workflow_keys


class TestRegisterAllLibraryWorkflowFiles:
    """The bulk path used by engine start and reload-all."""

    @pytest.mark.asyncio
    async def test_covers_registered_libraries_only(self, griptape_nodes: Engine, tmp_path: Path) -> None:
        """A library that failed to load has an info entry but is absent from the registry."""
        library_manager = griptape_nodes.library_manager
        loaded_info = _library_info(tmp_path / "loaded" / "lib.json")
        failed_info = LibraryManager.LibraryInfo(
            lifecycle_state=LibraryManager.LibraryLifecycleState.FAILURE,
            fitness=LibraryManager.LibraryFitness.UNUSABLE,
            library_path=str(tmp_path / "failed" / "lib.json"),
            is_sandbox=False,
            library_name="BrokenLib",
        )
        register_one = AsyncMock(return_value=None)

        with (
            patch.dict(
                library_manager._library_file_path_to_info,
                {loaded_info.library_path: loaded_info, failed_info.library_path: failed_info},
                clear=True,
            ),
            patch(f"{LIBRARY_MANAGER_MODULE}.LibraryRegistry.list_libraries", return_value=[LIBRARY_NAME]),
            patch.object(library_manager, "_register_workflow_files_for_library", register_one),
        ):
            await library_manager._register_all_library_workflow_files()

        register_one.assert_awaited_once_with(loaded_info)

    @pytest.mark.asyncio
    async def test_registers_a_duplicated_library_once(self, griptape_nodes: Engine, tmp_path: Path) -> None:
        """Two on-disk copies of one name must not both contribute templates."""
        library_manager = griptape_nodes.library_manager
        live_info = _library_info(tmp_path / "live" / "lib.json")
        duplicate_info = LibraryManager.LibraryInfo(
            lifecycle_state=LibraryManager.LibraryLifecycleState.FAILURE,
            fitness=LibraryManager.LibraryFitness.UNUSABLE,
            library_path=str(tmp_path / "duplicate" / "lib.json"),
            is_sandbox=False,
            library_name=LIBRARY_NAME,
        )
        register_one = AsyncMock(return_value=None)

        with (
            patch.dict(
                library_manager._library_file_path_to_info,
                {duplicate_info.library_path: duplicate_info, live_info.library_path: live_info},
                clear=True,
            ),
            patch(f"{LIBRARY_MANAGER_MODULE}.LibraryRegistry.list_libraries", return_value=[LIBRARY_NAME]),
            patch.object(library_manager, "_register_workflow_files_for_library", register_one),
        ):
            await library_manager._register_all_library_workflow_files()

        register_one.assert_awaited_once_with(live_info)


class TestUnregisterWorkflowFilesForLibrary:
    def test_removes_the_libraries_templates_and_announces_them(self, griptape_nodes: Engine) -> None:
        library_manager = griptape_nodes.library_manager
        library_manager._library_to_workflow_keys[LIBRARY_NAME] = ["templates/example"]
        event_manager = MagicMock()

        with (
            patch.dict(
                WorkflowRegistry._workflows,
                {"templates/example": MagicMock(), "user_workflow": MagicMock()},
                clear=True,
            ),
            patch.object(griptape_nodes, "_event_manager", event_manager),
        ):
            library_manager._unregister_workflow_files_for_library(LIBRARY_NAME)

            assert "templates/example" not in WorkflowRegistry._workflows
            assert "user_workflow" in WorkflowRegistry._workflows

        assert LIBRARY_NAME not in library_manager._library_to_workflow_keys
        changes = _emitted_template_changes(event_manager)
        assert len(changes) == 1
        assert changes[0].workflow_names == ["templates/example"]
        assert changes[0].registered is False

    def test_tolerates_a_template_that_is_already_gone(self, griptape_nodes: Engine) -> None:
        library_manager = griptape_nodes.library_manager
        library_manager._library_to_workflow_keys[LIBRARY_NAME] = ["templates/example"]
        event_manager = MagicMock()

        with (
            patch.dict(WorkflowRegistry._workflows, {}, clear=True),
            patch.object(griptape_nodes, "_event_manager", event_manager),
        ):
            library_manager._unregister_workflow_files_for_library(LIBRARY_NAME)

        assert LIBRARY_NAME not in library_manager._library_to_workflow_keys
        assert _emitted_template_changes(event_manager) == []

    def test_unloading_a_library_removes_its_templates(self, griptape_nodes: Engine) -> None:
        library_manager = griptape_nodes.library_manager
        library_manager._library_to_workflow_keys[LIBRARY_NAME] = ["templates/example"]

        with (
            patch(f"{LIBRARY_MANAGER_MODULE}.LibraryRegistry.unregister_library"),
            patch.dict(WorkflowRegistry._workflows, {"templates/example": MagicMock()}, clear=True),
        ):
            result = library_manager.unload_library_from_registry_request(
                UnloadLibraryFromRegistryRequest(library_name=LIBRARY_NAME)
            )

            assert result.succeeded()
            assert "templates/example" not in WorkflowRegistry._workflows


class TestRegistrationHookGating:
    """The hook in `register_library_from_file_request` must stay out of bulk library loads.

    Two reasons, and the tests below cover both. `WorkflowManager.on_load_workflow_metadata_request`
    waits on `_libraries_loading_complete`, which `load_all_libraries_from_config` holds closed
    while it loads each library through this handler -- registering there would await an event
    only the enclosing loop can set. The legacy `load_libraries_request` loop leaves that event
    alone, but registering per library there would resolve a template's library references
    against a partially loaded set.
    """

    @contextlib.contextmanager
    def _stub_lifecycle(
        self,
        library_manager: LibraryManager,
        library_info: LibraryManager.LibraryInfo,
        register_one: AsyncMock | None = None,
    ) -> Iterator[None]:
        """Patch out everything before the fitness match so only the hook's gating is exercised.

        Pass `register_one` to stand in for `_register_workflow_files_for_library` and assert on
        whether it was awaited; leave it out to let the real one run.
        """
        prerequisites = LibraryManager.RegisterLibraryPrerequisites(
            library_info=library_info, file_path=library_info.library_path
        )
        with contextlib.ExitStack() as stack:
            stack.enter_context(
                patch.object(
                    library_manager,
                    "_establish_register_library_prerequisites",
                    AsyncMock(return_value=prerequisites),
                )
            )
            stack.enter_context(
                patch.object(library_manager, "_progress_library_through_lifecycle", AsyncMock(return_value=None))
            )
            if register_one is not None:
                stack.enter_context(patch.object(library_manager, "_register_workflow_files_for_library", register_one))
            yield

    @pytest.mark.asyncio
    async def test_registers_templates_for_a_mid_session_install(self, griptape_nodes: Engine, tmp_path: Path) -> None:
        library_manager = griptape_nodes.library_manager
        library_manager._libraries_loading_complete.set()
        library_manager._bulk_library_load_in_progress = False
        register_one = AsyncMock(return_value=None)

        with self._stub_lifecycle(library_manager, _library_info(tmp_path / "lib.json"), register_one):
            result = await library_manager.register_library_from_file_request(
                RegisterLibraryFromFileRequest(file_path="/fake/lib.json")
            )

        assert result.succeeded()
        register_one.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_skips_templates_while_libraries_are_still_loading(
        self, griptape_nodes: Engine, tmp_path: Path
    ) -> None:
        library_manager = griptape_nodes.library_manager
        library_manager._libraries_loading_complete.clear()
        register_one = AsyncMock(return_value=None)

        with self._stub_lifecycle(library_manager, _library_info(tmp_path / "lib.json"), register_one):
            result = await library_manager.register_library_from_file_request(
                RegisterLibraryFromFileRequest(file_path="/fake/lib.json")
            )

        assert result.succeeded()
        register_one.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_templates_during_a_bulk_load_that_leaves_the_gate_open(
        self, griptape_nodes: Engine, tmp_path: Path
    ) -> None:
        """The legacy `load_libraries_request` loop does not close `_libraries_loading_complete`.

        Inferring "am I in a bulk load?" from that event alone would let this loop register each
        library's templates as it went, checking them against a partially loaded library set.
        """
        library_manager = griptape_nodes.library_manager
        library_manager._libraries_loading_complete.set()
        library_manager._bulk_library_load_in_progress = True
        register_one = AsyncMock(return_value=None)

        with self._stub_lifecycle(library_manager, _library_info(tmp_path / "lib.json"), register_one):
            result = await library_manager.register_library_from_file_request(
                RegisterLibraryFromFileRequest(file_path="/fake/lib.json")
            )

        assert result.succeeded()
        register_one.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_templates_for_an_unusable_library(self, griptape_nodes: Engine, tmp_path: Path) -> None:
        library_manager = griptape_nodes.library_manager
        library_manager._libraries_loading_complete.set()
        library_manager._bulk_library_load_in_progress = False
        library_info = _library_info(tmp_path / "lib.json")
        library_info.fitness = LibraryManager.LibraryFitness.UNUSABLE
        register_one = AsyncMock(return_value=None)

        with self._stub_lifecycle(library_manager, library_info, register_one):
            result = await library_manager.register_library_from_file_request(
                RegisterLibraryFromFileRequest(file_path="/fake/lib.json")
            )

        assert not result.succeeded()
        register_one.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_registering_with_the_gate_closed_does_not_hang(self, griptape_nodes: Engine, tmp_path: Path) -> None:
        """Guards the hazard itself, not just the `if` that avoids it.

        The real `_register_workflow_files_for_library` runs here against a library that declares
        a template. If the gate were ever dropped, this reaches
        `on_load_workflow_metadata_request`, which awaits the `_libraries_loading_complete` event
        this test holds closed, and the call never returns -- so a regression shows up as a
        timeout rather than as a silently different result.
        """
        library_manager = griptape_nodes.library_manager
        library_manager._libraries_loading_complete.clear()
        library_manager._bulk_library_load_in_progress = True
        (tmp_path / "example.py").write_text("# /// script\n# ///\n", encoding="utf-8")

        with (
            self._stub_lifecycle(library_manager, _library_info(tmp_path / "lib.json")),
            patch(f"{LIBRARY_MANAGER_MODULE}.LibraryRegistry.get_library", return_value=_library(["example.py"])),
        ):
            result = await asyncio.wait_for(
                library_manager.register_library_from_file_request(
                    RegisterLibraryFromFileRequest(file_path="/fake/lib.json")
                ),
                timeout=10,
            )

        assert result.succeeded()
        assert LIBRARY_NAME not in library_manager._library_to_workflow_keys


class TestBulkRegistrationIsWiredUp:
    """The whole no-deadlock design rests on the bulk call running after each batch load.

    Without these, deleting either call site drops every library template with no failing test.
    """

    @pytest.mark.asyncio
    async def test_reload_re_registers_templates(self, griptape_nodes: Engine) -> None:
        register_all = AsyncMock(return_value=None)

        with (
            patch.object(griptape_nodes.library_manager, "_register_all_library_workflow_files", register_all),
            patch.object(griptape_nodes.library_manager, "load_all_libraries_from_config", AsyncMock(return_value=[])),
            patch.object(
                griptape_nodes.library_manager,
                "_maybe_start_workers_for_existing_session",
                AsyncMock(return_value=None),
            ),
            patch.object(griptape_nodes.library_manager, "_await_pending_workers", AsyncMock(return_value=None)),
        ):
            result = await griptape_nodes.library_manager._run_reload_libraries(ReloadAllLibrariesRequest())

        assert result.succeeded()
        register_all.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_refresh_workflow_registry_re_registers_templates(self, griptape_nodes: Engine) -> None:
        """A rescan clears the registry, so it has to put library templates back before returning."""
        register_all = AsyncMock(return_value=None)

        with (
            patch.object(griptape_nodes.library_manager, "_register_all_library_workflow_files", register_all),
            patch.object(
                griptape_nodes.workflow_manager, "_process_workflows_for_registration", AsyncMock(return_value=None)
            ),
            patch.object(WorkflowRegistry, "clear_user_workflows"),
        ):
            await griptape_nodes.workflow_manager.refresh_workflow_registry(workflows_to_register=[])

        register_all.assert_awaited_once()
