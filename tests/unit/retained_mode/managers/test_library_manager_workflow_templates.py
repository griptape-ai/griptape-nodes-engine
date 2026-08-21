"""Tests for registering and unregistering a library's `workflows` templates."""

from __future__ import annotations

import asyncio
import contextlib
import sys
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from griptape_nodes.node_library.library_registry import Library, LibraryMetadata, LibrarySchema
from griptape_nodes.node_library.workflow_registry import WorkflowMetadata, WorkflowRegistry
from griptape_nodes.retained_mode.events.app_events import LibraryWorkflowTemplatesChanged
from griptape_nodes.retained_mode.events.library_events import (
    LoadLibrariesRequest,
    LoadLibrariesResultSuccess,
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
    from griptape_nodes.retained_mode.events.base_events import ResultPayload

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


def _workflow_header() -> str:
    """The metadata header a workflow file needs before the engine will register it."""
    lines = [
        "# /// script",
        "# [tool.griptape-nodes]",
        '# name = "example"',
        f'# schema_version = "{WorkflowMetadata.LATEST_SCHEMA_VERSION}"',
        '# engine_version_created_with = "0.0.0"',
        "# node_libraries_referenced = []",
        "# is_template = true",
        "# ///",
    ]
    return "\n".join(lines) + "\n"


def _emitted_template_changes(event_manager: MagicMock) -> list[LibraryWorkflowTemplatesChanged]:
    """Pull the template-change payloads out of a mocked event manager's put_event calls."""
    payloads = [call.args[0].payload for call in event_manager.put_event.call_args_list]
    return [payload for payload in payloads if isinstance(payload, LibraryWorkflowTemplatesChanged)]


@pytest.fixture(autouse=True)
def _restore_sys_path() -> Iterator[None]:
    """Undo the `sys.path` entry registration adds for a library's directory.

    Every test here points a library at a `tmp_path` that pytest later deletes, so without
    this the rest of the session runs with dead directories on `sys.path` where they can
    shadow module resolution.
    """
    original_sys_path = list(sys.path)
    yield
    sys.path[:] = original_sys_path


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

        with patch(f"{LIBRARY_MANAGER_MODULE}.LibraryRegistry.get_library", return_value=library):
            griptape_nodes.library_manager._collect_workflow_files_for_library(_library_info(library_json))
            griptape_nodes.library_manager._collect_workflow_files_for_library(_library_info(library_json))

        assert sys.path.count(str(tmp_path)) == 1

    def test_returns_nothing_when_the_library_declares_no_workflows(
        self, griptape_nodes: Engine, tmp_path: Path
    ) -> None:
        library_json = tmp_path / "griptape_nodes_library.json"

        with patch(f"{LIBRARY_MANAGER_MODULE}.LibraryRegistry.get_library", return_value=_library(None)):
            collected = griptape_nodes.library_manager._collect_workflow_files_for_library(_library_info(library_json))

        assert collected == []

    def test_returns_none_when_the_library_is_not_registered(self, griptape_nodes: Engine, tmp_path: Path) -> None:
        """Distinct from the empty list above, and the caller relies on the difference.

        "Declares no templates" means the recorded keys are stale and should go; "could not read
        what it declares" means leave them alone.
        """
        library_json = tmp_path / "griptape_nodes_library.json"

        with patch(f"{LIBRARY_MANAGER_MODULE}.LibraryRegistry.get_library", side_effect=KeyError(LIBRARY_NAME)):
            collected = griptape_nodes.library_manager._collect_workflow_files_for_library(_library_info(library_json))

        assert collected is None


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

        assert not library_manager._library_to_workflow_keys.get(LIBRARY_NAME)
        assert _emitted_template_changes(event_manager) == []

    @pytest.mark.asyncio
    async def test_leaves_a_key_that_has_not_moved_alone(self, griptape_nodes: Engine, tmp_path: Path) -> None:
        """Reconciling twice must not remove and re-add a template that did not change.

        The stale sweep exists for keys the library would no longer produce. Sweeping every
        recorded key instead would work out to the same registry contents, but every listener
        would be told the template vanished and came back on every engine start and reload.
        """
        library_manager = griptape_nodes.library_manager
        library_manager._library_to_workflow_keys[LIBRARY_NAME] = ["example"]
        register = AsyncMock(return_value=WorkflowRegistrationResult(succeeded=[], failed=[]))
        event_manager = MagicMock()

        with (
            patch(f"{LIBRARY_MANAGER_MODULE}.LibraryRegistry.get_library", return_value=_library(["example.py"])),
            patch.object(griptape_nodes.workflow_manager, "registry_key_for_workflow_file", return_value="example"),
            patch.object(griptape_nodes.workflow_manager, "register_list_of_workflows", register),
            patch.object(griptape_nodes, "_event_manager", event_manager),
        ):
            await library_manager._register_workflow_files_for_library(_library_info(tmp_path / "lib.json"))

        assert library_manager._library_to_workflow_keys[LIBRARY_NAME] == ["example"]
        assert _emitted_template_changes(event_manager) == []

    @pytest.mark.asyncio
    async def test_unregisters_a_key_the_library_no_longer_produces(
        self, griptape_nodes: Engine, tmp_path: Path
    ) -> None:
        library_manager = griptape_nodes.library_manager
        library_manager._library_to_workflow_keys[LIBRARY_NAME] = ["stale/example"]
        register = AsyncMock(return_value=WorkflowRegistrationResult(succeeded=["fresh/example"], failed=[]))
        event_manager = MagicMock()

        with (
            patch(f"{LIBRARY_MANAGER_MODULE}.LibraryRegistry.get_library", return_value=_library(["example.py"])),
            patch.object(
                griptape_nodes.workflow_manager, "registry_key_for_workflow_file", return_value="fresh/example"
            ),
            patch.object(griptape_nodes.workflow_manager, "register_list_of_workflows", register),
            patch.dict(WorkflowRegistry._workflows, {"stale/example": MagicMock()}, clear=True),
            patch.object(griptape_nodes, "_event_manager", event_manager),
        ):
            await library_manager._register_workflow_files_for_library(_library_info(tmp_path / "lib.json"))

            assert "stale/example" not in WorkflowRegistry._workflows

        assert library_manager._library_to_workflow_keys[LIBRARY_NAME] == ["fresh/example"]
        removals = [change for change in _emitted_template_changes(event_manager) if not change.registered]
        additions = [change for change in _emitted_template_changes(event_manager) if change.registered]
        assert [change.workflow_names for change in removals] == [["stale/example"]]
        assert [change.workflow_names for change in additions] == [["fresh/example"]]

    @pytest.mark.asyncio
    async def test_bailing_out_never_unregisters_anything(self, griptape_nodes: Engine, tmp_path: Path) -> None:
        """Every early exit is above the stale sweep, so a bail-out is not a removal.

        This is the shape of the bug the sweep replaced: dropping the recorded keys before the
        registration that puts them back means anything that stops the registration -- a worker,
        a closed gate, an unreadable library -- silently unregisters the library's templates.
        """
        library_manager = griptape_nodes.library_manager
        library_manager._library_to_workflow_keys[LIBRARY_NAME] = ["templates/example"]
        event_manager = MagicMock()

        with (
            patch(f"{LIBRARY_MANAGER_MODULE}.LibraryRegistry.get_library", side_effect=KeyError(LIBRARY_NAME)),
            patch.dict(WorkflowRegistry._workflows, {"templates/example": MagicMock()}, clear=True),
            patch.object(griptape_nodes, "_event_manager", event_manager),
        ):
            await library_manager._register_workflow_files_for_library(_library_info(tmp_path / "lib.json"))

            assert "templates/example" in WorkflowRegistry._workflows

        assert library_manager._library_to_workflow_keys[LIBRARY_NAME] == ["templates/example"]
        assert _emitted_template_changes(event_manager) == []

    @pytest.mark.asyncio
    async def test_unregisters_everything_when_the_library_stops_declaring_templates(
        self, griptape_nodes: Engine, tmp_path: Path
    ) -> None:
        """An empty list is a real answer -- the templates are gone and listeners are told."""
        library_manager = griptape_nodes.library_manager
        library_manager._library_to_workflow_keys[LIBRARY_NAME] = ["templates/example"]
        event_manager = MagicMock()

        with (
            patch(f"{LIBRARY_MANAGER_MODULE}.LibraryRegistry.get_library", return_value=_library(None)),
            patch.dict(WorkflowRegistry._workflows, {"templates/example": MagicMock()}, clear=True),
            patch.object(griptape_nodes, "_event_manager", event_manager),
        ):
            await library_manager._register_workflow_files_for_library(_library_info(tmp_path / "lib.json"))

            assert "templates/example" not in WorkflowRegistry._workflows

        assert LIBRARY_NAME not in library_manager._library_to_workflow_keys
        changes = _emitted_template_changes(event_manager)
        assert len(changes) == 1
        assert changes[0].workflow_names == ["templates/example"]
        assert changes[0].registered is False

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
            await library_manager.reconcile_all_library_workflow_templates()

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
            await library_manager.reconcile_all_library_workflow_templates()

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

    def test_announces_a_template_that_was_already_gone(self, griptape_nodes: Engine) -> None:
        """Something else got there first, but the listener still has to hear about it.

        The recorded keys are exactly the ones registration announced. Staying quiet because the
        registry entry had already been deleted -- by `clear_user_workflows`, or by the user
        deleting the file -- leaves a listener that was told the template appeared with no
        matching removal.
        """
        library_manager = griptape_nodes.library_manager
        library_manager._library_to_workflow_keys[LIBRARY_NAME] = ["templates/example"]
        event_manager = MagicMock()

        with (
            patch.dict(WorkflowRegistry._workflows, {}, clear=True),
            patch.object(griptape_nodes, "_event_manager", event_manager),
        ):
            library_manager._unregister_workflow_files_for_library(LIBRARY_NAME)

        assert LIBRARY_NAME not in library_manager._library_to_workflow_keys
        changes = _emitted_template_changes(event_manager)
        assert len(changes) == 1
        assert changes[0].workflow_names == ["templates/example"]
        assert changes[0].registered is False

    def test_says_nothing_for_a_library_that_contributed_no_templates(self, griptape_nodes: Engine) -> None:
        library_manager = griptape_nodes.library_manager
        event_manager = MagicMock()

        with patch.object(griptape_nodes, "_event_manager", event_manager):
            library_manager._unregister_workflow_files_for_library(LIBRARY_NAME)

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

    Two guards, and the tests below cover both. The bulk-load count keeps the hook out of a
    batch load, so a template's library references are resolved against the full set of
    libraries rather than however many happened to load first. The `_libraries_loading_complete`
    check inside the registration itself is the backstop: registering awaits that event through
    `WorkflowManager.on_load_workflow_metadata_request`, so registering while it is closed would
    await an event only the enclosing load can set.
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
        library_manager._bulk_library_load_depth = 0
        register_one = AsyncMock(return_value=None)

        with self._stub_lifecycle(library_manager, _library_info(tmp_path / "lib.json"), register_one):
            result = await library_manager.register_library_from_file_request(
                RegisterLibraryFromFileRequest(file_path="/fake/lib.json")
            )

        assert result.succeeded()
        register_one.assert_awaited_once()

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
        library_manager._bulk_library_load_depth = 1
        register_one = AsyncMock(return_value=None)

        with self._stub_lifecycle(library_manager, _library_info(tmp_path / "lib.json"), register_one):
            result = await library_manager.register_library_from_file_request(
                RegisterLibraryFromFileRequest(file_path="/fake/lib.json")
            )

        assert result.succeeded()
        register_one.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_one_bulk_load_finishing_does_not_re_arm_the_hook_for_another(
        self, griptape_nodes: Engine, tmp_path: Path
    ) -> None:
        """`load_all_libraries_from_config` and `load_libraries_request` can overlap.

        Both await inside their loops and both are reachable from the request queue, so a plain
        boolean would let whichever finished first register templates underneath the other one.
        This drives the real bulk entry points rather than setting the count by hand: one is held
        open mid-load while the other runs to completion, and the hook has to stay down until the
        held one lets go too.
        """
        library_manager = griptape_nodes.library_manager
        library_manager._libraries_loading_complete.set()
        library_manager._bulk_library_load_depth = 0
        register_one = AsyncMock(return_value=None)
        reconcile_all = AsyncMock(return_value=None)
        held_open = asyncio.Event()

        async def block_until_released() -> ResultPayload:
            await held_open.wait()
            return LoadLibrariesResultSuccess(result_details="the bulk load held open by this test")

        async def register_one_library() -> bool:
            with self._stub_lifecycle(library_manager, _library_info(tmp_path / "lib.json"), register_one):
                result = await library_manager.register_library_from_file_request(
                    RegisterLibraryFromFileRequest(file_path="/fake/lib.json")
                )
            return result.succeeded()

        with (
            patch.object(library_manager, "_load_libraries_from_config_in_bulk", block_until_released),
            patch.object(library_manager, "reconcile_all_library_workflow_templates", reconcile_all),
        ):
            first_bulk_load = asyncio.create_task(library_manager.load_libraries_request(LoadLibrariesRequest()))
            # Let the task get as far as its own await so the count is genuinely raised by it.
            await asyncio.sleep(0)
            assert library_manager._bulk_library_load_depth == 1

            # A second bulk load starts and finishes entirely inside the first one, which is still
            # parked on `held_open`.
            with patch.object(
                library_manager,
                "_load_libraries_from_config_in_bulk",
                AsyncMock(return_value=LoadLibrariesResultSuccess(result_details="the second bulk load")),
            ):
                await library_manager.load_libraries_request(LoadLibrariesRequest())

            # The finished load must not have taken the count to zero underneath the one still
            # running, which is exactly what a boolean flag would have done.
            assert library_manager._bulk_library_load_depth == 1
            assert await register_one_library()
            register_one.assert_not_awaited()

            held_open.set()
            await first_bulk_load

        assert library_manager._bulk_library_load_depth == 0
        assert await register_one_library()
        register_one.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_skips_templates_for_an_unusable_library(self, griptape_nodes: Engine, tmp_path: Path) -> None:
        library_manager = griptape_nodes.library_manager
        library_manager._libraries_loading_complete.set()
        library_manager._bulk_library_load_depth = 0
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

        The bulk-load count is left at zero so the only thing standing between this and
        `on_load_workflow_metadata_request` is the `_libraries_loading_complete` check, and the
        real `_register_workflow_files_for_library` runs against a library that declares a
        template. Delete that check and this reaches an await on the event the test holds
        closed, so the regression shows up as a timeout rather than as a quietly different
        result.
        """
        library_manager = griptape_nodes.library_manager
        library_manager._libraries_loading_complete.clear()
        library_manager._bulk_library_load_depth = 0
        (tmp_path / "example.py").write_text(_workflow_header(), encoding="utf-8")

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
            patch.object(griptape_nodes.library_manager, "reconcile_all_library_workflow_templates", register_all),
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
    async def test_refresh_workflow_registry_re_registers_templates_after_clearing(
        self, griptape_nodes: Engine
    ) -> None:
        """A rescan clears the registry, so it has to put library templates back afterwards.

        Order is the whole point: re-registering before `clear_user_workflows` would hand the
        clear a fresh set of templates to delete. Asserting only that the call happened would
        pass either way, so this checks what had already run when it fired.
        """
        workflow_manager = griptape_nodes.workflow_manager
        observed = {}

        async def record_state() -> None:
            observed["cleared_first"] = clear_user_workflows.called
            observed["gate_closed"] = not workflow_manager._workflows_loading_complete.is_set()

        with (
            patch.object(WorkflowRegistry, "clear_user_workflows") as clear_user_workflows,
            patch.object(
                griptape_nodes.library_manager,
                "reconcile_all_library_workflow_templates",
                AsyncMock(side_effect=record_state),
            ),
            patch.object(workflow_manager, "_process_workflows_for_registration", AsyncMock(return_value=None)),
        ):
            await workflow_manager.refresh_workflow_registry(workflows_to_register=[])

        assert observed == {"cleared_first": True, "gate_closed": True}
        assert workflow_manager._workflows_loading_complete.is_set()


class TestTemplatesSurviveAWorkspaceChange:
    """End to end against the real WorkflowRegistry, no registration mocks.

    A template's registry key is workspace-relative while the file sits inside the workspace and
    absolute otherwise, and `clear_user_workflows` spares entries flagged `is_griptape_provided`.
    Together those let a rescan after a workspace change leave the old key behind and add a
    second one, so the template shows up twice with one copy pointing nowhere.
    """

    @pytest.mark.asyncio
    async def test_a_template_is_registered_under_exactly_one_key(self, griptape_nodes: Engine, tmp_path: Path) -> None:
        library_manager = griptape_nodes.library_manager
        config_manager = griptape_nodes.config_manager
        first_workspace = tmp_path / "first_workspace"
        library_dir = first_workspace / "libraries" / "test_lib"
        library_dir.mkdir(parents=True)
        (library_dir / "example.py").write_text(_workflow_header(), encoding="utf-8")
        library_info = _library_info(library_dir / "griptape_nodes_library.json")

        with (
            patch(f"{LIBRARY_MANAGER_MODULE}.LibraryRegistry.get_library", return_value=_library(["example.py"])),
            patch(f"{LIBRARY_MANAGER_MODULE}.LibraryRegistry.list_libraries", return_value=[LIBRARY_NAME]),
            patch.dict(library_manager._library_file_path_to_info, {library_info.library_path: library_info}),
            patch.dict(WorkflowRegistry._workflows, {}, clear=True),
            patch.object(type(config_manager), "workspace_path", first_workspace),
        ):
            await library_manager.reconcile_all_library_workflow_templates()
            registered_in_first_workspace = list(WorkflowRegistry._workflows)

            # The workspace moves but the library does not, which is what a project switch that
            # leaves library config alone does.
            with patch.object(type(config_manager), "workspace_path", tmp_path / "second_workspace"):
                await library_manager.reconcile_all_library_workflow_templates()
                registered_in_second_workspace = list(WorkflowRegistry._workflows)

        # Inside the workspace the key is relative to it; once the workspace moves away from the
        # library the same file registers under its absolute path instead.
        assert registered_in_first_workspace == ["libraries/test_lib/example"]
        assert registered_in_second_workspace == [str(library_dir / "example")]
        assert library_manager._library_to_workflow_keys[LIBRARY_NAME] == registered_in_second_workspace

    @pytest.mark.asyncio
    async def test_a_template_without_the_griptape_provided_flag_survives_a_rescan(
        self, griptape_nodes: Engine, tmp_path: Path
    ) -> None:
        """Through the real `refresh_workflow_registry`, with the real `clear_user_workflows`.

        The template's header sets `is_template` but not `is_griptape_provided`, so the clear at
        the top of a rescan deletes it. It comes back only because the rescan asks LibraryManager
        for the library's templates afterwards -- which is the point: a template follows the
        library that ships it rather than depending on a flag its author may have omitted.
        """
        library_manager = griptape_nodes.library_manager
        workflow_manager = griptape_nodes.workflow_manager
        config_manager = griptape_nodes.config_manager
        workspace = tmp_path / "workspace"
        library_dir = workspace / "libraries" / "test_lib"
        library_dir.mkdir(parents=True)
        (library_dir / "example.py").write_text(_workflow_header(), encoding="utf-8")
        library_info = _library_info(library_dir / "griptape_nodes_library.json")
        registry_key = "libraries/test_lib/example"

        with (
            patch(f"{LIBRARY_MANAGER_MODULE}.LibraryRegistry.get_library", return_value=_library(["example.py"])),
            patch(f"{LIBRARY_MANAGER_MODULE}.LibraryRegistry.list_libraries", return_value=[LIBRARY_NAME]),
            patch.dict(library_manager._library_file_path_to_info, {library_info.library_path: library_info}),
            patch.dict(WorkflowRegistry._workflows, {}, clear=True),
            patch.object(type(config_manager), "workspace_path", workspace),
        ):
            await library_manager.reconcile_all_library_workflow_templates()

            # Nothing spares this entry from the clear, so surviving the rescan cannot be an
            # accident of the flag.
            assert WorkflowRegistry.get_workflow_by_name(registry_key).metadata.is_griptape_provided is False

            # An empty list skips the workspace scan; the clear and the library re-registration
            # are the parts under test.
            await workflow_manager.refresh_workflow_registry(workflows_to_register=[])

            assert list(WorkflowRegistry._workflows) == [registry_key]

        assert library_manager._library_to_workflow_keys[LIBRARY_NAME] == [registry_key]
