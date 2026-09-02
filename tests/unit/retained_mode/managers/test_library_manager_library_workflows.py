"""Tests for registering and unregistering the workflows a library declares."""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from griptape_nodes.node_library.library_registry import Library, LibraryMetadata, LibrarySchema
from griptape_nodes.node_library.workflow_registry import WorkflowMetadata, WorkflowRegistry
from griptape_nodes.retained_mode.events.app_events import LibraryWorkflowsChanged
from griptape_nodes.retained_mode.events.library_events import (
    DiscoveredLibrary,
    DiscoverLibrariesRequest,
    DiscoverLibrariesResultSuccess,
    RegisterLibraryFromFileRequest,
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


def _emitted_workflow_changes(event_manager: MagicMock) -> list[LibraryWorkflowsChanged]:
    """Pull the library-workflow payloads out of a mocked event manager's put_event calls."""
    payloads = [call.args[0].payload for call in event_manager.put_event.call_args_list]
    return [payload for payload in payloads if isinstance(payload, LibraryWorkflowsChanged)]


class TestCollectWorkflowFilesForLibrary:
    def test_resolves_paths_against_the_library_directory(self, engine: Engine, tmp_path: Path) -> None:
        library_json = tmp_path / "griptape_nodes_library.json"
        library = _library(["workflows/example.py", "other.py"])

        with patch(f"{LIBRARY_MANAGER_MODULE}.LibraryRegistry.get_library", return_value=library):
            collected = engine.library_manager._collect_workflow_files_for_library(_library_info(library_json))

        assert collected == [
            str(tmp_path / "workflows/example.py"),
            str(tmp_path / "other.py"),
        ]

    def test_leaves_sys_path_alone(self, engine: Engine, tmp_path: Path) -> None:
        """Loading the library already put its directory on `sys.path`.

        Adding it again here would mean a second, undocumented owner of the process's import
        path -- and every test pointing a library at a `tmp_path` pytest later deletes would
        leave a dead directory behind to shadow module resolution.
        """
        library_json = tmp_path / "griptape_nodes_library.json"
        sys_path = MagicMock()

        with (
            patch(f"{LIBRARY_MANAGER_MODULE}.LibraryRegistry.get_library", return_value=_library(["example.py"])),
            patch(f"{LIBRARY_MANAGER_MODULE}.sys.path", sys_path),
        ):
            engine.library_manager._collect_workflow_files_for_library(_library_info(library_json))

        sys_path.insert.assert_not_called()
        sys_path.append.assert_not_called()

    def test_returns_nothing_when_the_library_declares_no_workflows(self, engine: Engine, tmp_path: Path) -> None:
        library_json = tmp_path / "griptape_nodes_library.json"

        with patch(f"{LIBRARY_MANAGER_MODULE}.LibraryRegistry.get_library", return_value=_library(None)):
            collected = engine.library_manager._collect_workflow_files_for_library(_library_info(library_json))

        assert collected == []

    def test_returns_nothing_when_the_library_is_not_registered(self, engine: Engine, tmp_path: Path) -> None:
        library_json = tmp_path / "griptape_nodes_library.json"

        with patch(f"{LIBRARY_MANAGER_MODULE}.LibraryRegistry.get_library", side_effect=KeyError(LIBRARY_NAME)):
            collected = engine.library_manager._collect_workflow_files_for_library(_library_info(library_json))

        assert collected == []

    def test_returns_nothing_for_a_library_with_no_name(self, engine: Engine, tmp_path: Path) -> None:
        """A nameless library cannot be looked up, and could not own its entries anyway."""
        library_info = _library_info(tmp_path / "lib.json")
        library_info.library_name = None

        collected = engine.library_manager._collect_workflow_files_for_library(library_info)

        assert collected == []


class TestRegisterWorkflowsForLibrary:
    @pytest.mark.asyncio
    async def test_registers_them_under_the_library_name_and_announces_them(
        self, engine: Engine, tmp_path: Path
    ) -> None:
        """The name is what makes the entries the library's, so it has to reach the registry."""
        register = AsyncMock(return_value=WorkflowRegistrationResult(succeeded=["example"], failed=[]))
        event_manager = MagicMock()

        with (
            patch(f"{LIBRARY_MANAGER_MODULE}.LibraryRegistry.get_library", return_value=_library(["example.py"])),
            patch.object(engine.workflow_manager, "register_list_of_workflows", register),
            patch.object(engine, "_event_manager", event_manager),
        ):
            await engine.library_manager.register_workflows_for_library(_library_info(tmp_path / "lib.json"))

        register.assert_awaited_once_with([str(tmp_path / "example.py")], library_name=LIBRARY_NAME)
        changes = _emitted_workflow_changes(event_manager)
        assert len(changes) == 1
        assert changes[0].library_name == LIBRARY_NAME
        assert changes[0].workflow_names == ["example"]
        assert changes[0].registered is True

    @pytest.mark.asyncio
    async def test_says_nothing_when_no_workflow_landed(self, engine: Engine, tmp_path: Path) -> None:
        """Re-registering an already-registered set is the normal case, not a change.

        The registration pass runs again on every reload and after every full library load, so
        announcing an unchanged set would tell listeners the workflows appeared each time.
        """
        register = AsyncMock(return_value=WorkflowRegistrationResult(succeeded=[], failed=["example.py"]))
        event_manager = MagicMock()

        with (
            patch(f"{LIBRARY_MANAGER_MODULE}.LibraryRegistry.get_library", return_value=_library(["example.py"])),
            patch.object(engine.workflow_manager, "register_list_of_workflows", register),
            patch.object(engine, "_event_manager", event_manager),
        ):
            await engine.library_manager.register_workflows_for_library(_library_info(tmp_path / "lib.json"))

        assert _emitted_workflow_changes(event_manager) == []

    @pytest.mark.asyncio
    async def test_does_not_register_when_the_library_declares_nothing(self, engine: Engine, tmp_path: Path) -> None:
        register = AsyncMock(return_value=WorkflowRegistrationResult(succeeded=[], failed=[]))

        with (
            patch(f"{LIBRARY_MANAGER_MODULE}.LibraryRegistry.get_library", return_value=_library(None)),
            patch.object(engine.workflow_manager, "register_list_of_workflows", register),
        ):
            await engine.library_manager.register_workflows_for_library(_library_info(tmp_path / "lib.json"))

        register.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_does_nothing_on_a_worker(self, engine: Engine, tmp_path: Path) -> None:
        """A worker imports node classes for the orchestrator and never serves workflow lists."""
        library_manager = engine.library_manager
        library_manager._is_worker = True
        register = AsyncMock(return_value=WorkflowRegistrationResult(succeeded=["example"], failed=[]))

        with (
            patch(f"{LIBRARY_MANAGER_MODULE}.LibraryRegistry.get_library", return_value=_library(["example.py"])),
            patch.object(engine.workflow_manager, "register_list_of_workflows", register),
        ):
            await library_manager.register_workflows_for_library(_library_info(tmp_path / "lib.json"))

        register.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_does_nothing_for_a_library_with_no_name(self, engine: Engine, tmp_path: Path) -> None:
        register = AsyncMock(return_value=WorkflowRegistrationResult(succeeded=["example"], failed=[]))
        library_info = _library_info(tmp_path / "lib.json")
        library_info.library_name = None

        with patch.object(engine.workflow_manager, "register_list_of_workflows", register):
            await engine.library_manager.register_workflows_for_library(library_info)

        register.assert_not_awaited()


class TestRegisterWorkflowsForAllLibraries:
    """The pass that runs once a full library load reopens the loading gate."""

    @pytest.mark.asyncio
    async def test_covers_registered_libraries_only(self, engine: Engine, tmp_path: Path) -> None:
        """A library that failed to load has an info entry but is absent from the registry."""
        library_manager = engine.library_manager
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
            patch.object(library_manager, "register_workflows_for_library", register_one),
        ):
            await library_manager.register_workflows_for_all_libraries()

        register_one.assert_awaited_once_with(loaded_info)

    @pytest.mark.asyncio
    async def test_registers_a_duplicated_library_once(self, engine: Engine, tmp_path: Path) -> None:
        """Two on-disk copies of one name must not both contribute workflows."""
        library_manager = engine.library_manager
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
            patch.object(library_manager, "register_workflows_for_library", register_one),
        ):
            await library_manager.register_workflows_for_all_libraries()

        register_one.assert_awaited_once_with(live_info)

    @pytest.mark.asyncio
    async def test_skips_a_library_this_engine_never_registered(self, engine: Engine) -> None:
        """`LibraryRegistry` is process-global, so it can list a library another Engine registered.

        Their workflows are that engine's business, and this one has no `LibraryInfo` for them to
        resolve the declared paths against anyway.
        """
        library_manager = engine.library_manager
        register_one = AsyncMock(return_value=None)

        with (
            patch.dict(library_manager._library_file_path_to_info, {}, clear=True),
            patch(f"{LIBRARY_MANAGER_MODULE}.LibraryRegistry.list_libraries", return_value=["AnotherEnginesLib"]),
            patch.object(library_manager, "register_workflows_for_library", register_one),
        ):
            await library_manager.register_workflows_for_all_libraries()

        register_one.assert_not_awaited()


class TestUnregisterWorkflowsForLibrary:
    @pytest.fixture(autouse=True)
    def _engine_knows_the_library(self, engine: Engine, tmp_path: Path) -> Iterator[None]:
        """The library is one this engine loaded, which is the only way unloading it is reached."""
        library_info = _library_info(tmp_path / "griptape_nodes_library.json")
        with patch.dict(
            engine.library_manager._library_file_path_to_info,
            {library_info.library_path: library_info},
            clear=True,
        ):
            yield

    def test_removes_the_library_workflows_and_announces_them(self, engine: Engine) -> None:
        library_manager = engine.library_manager
        event_manager = MagicMock()
        mine = MagicMock(library_name=LIBRARY_NAME)
        theirs = MagicMock(library_name="OtherLib")
        users = MagicMock(library_name=None)

        with (
            patch.dict(
                WorkflowRegistry._workflows,
                {"lib/example": mine, "other/example": theirs, "user_workflow": users},
                clear=True,
            ),
            patch.object(engine, "_event_manager", event_manager),
        ):
            library_manager._unregister_workflows_for_library(LIBRARY_NAME)

            assert sorted(WorkflowRegistry._workflows) == ["other/example", "user_workflow"]

        changes = _emitted_workflow_changes(event_manager)
        assert len(changes) == 1
        assert changes[0].library_name == LIBRARY_NAME
        assert changes[0].workflow_names == ["lib/example"]
        assert changes[0].registered is False

    def test_says_nothing_for_a_library_that_contributed_none(self, engine: Engine) -> None:
        event_manager = MagicMock()

        with (
            patch.dict(WorkflowRegistry._workflows, {"user_workflow": MagicMock(library_name=None)}, clear=True),
            patch.object(engine, "_event_manager", event_manager),
        ):
            engine.library_manager._unregister_workflows_for_library(LIBRARY_NAME)

        assert _emitted_workflow_changes(event_manager) == []

    def test_leaves_alone_a_library_this_engine_never_registered(self, engine: Engine) -> None:
        """The mirror of the guard on the register side, and for the same reason.

        `WorkflowRegistry` is process-global and its entries record only the contributing
        library's name, so in a process running more than one Engine an unguarded delete would
        take the other engine's entries for a library this one has never seen.
        """
        event_manager = MagicMock()

        with (
            patch.dict(engine.library_manager._library_file_path_to_info, {}, clear=True),
            patch.dict(WorkflowRegistry._workflows, {"lib/example": MagicMock(library_name=LIBRARY_NAME)}, clear=True),
            patch.object(engine, "_event_manager", event_manager),
        ):
            engine.library_manager._unregister_workflows_for_library(LIBRARY_NAME)

            assert list(WorkflowRegistry._workflows) == ["lib/example"]

        assert _emitted_workflow_changes(event_manager) == []

    def test_unloading_a_library_removes_its_workflows(self, engine: Engine) -> None:
        """Nothing else does: a workspace rescan deliberately spares library-owned entries.

        Without this, an install -> uninstall -> reinstall cycle piles up stale entries and an
        unloaded library keeps offering workflows for the life of the process.
        """
        with (
            patch(f"{LIBRARY_MANAGER_MODULE}.LibraryRegistry.unregister_library"),
            patch.dict(WorkflowRegistry._workflows, {"lib/example": MagicMock(library_name=LIBRARY_NAME)}, clear=True),
        ):
            result = engine.library_manager.unload_library_from_registry_request(
                UnloadLibraryFromRegistryRequest(library_name=LIBRARY_NAME)
            )

            assert result.succeeded()
            assert "lib/example" not in WorkflowRegistry._workflows


class TestRegistrationHookGating:
    """Every path that brings a library in funnels through `register_library_from_file_request`.

    So that is where the hook lives, gated on `_libraries_loading_complete`. Registering a
    workflow awaits that event through `WorkflowManager.on_load_workflow_metadata_request`, so
    doing it while the gate is closed would await an event only the enclosing load can set.
    """

    @contextlib.contextmanager
    def _stub_lifecycle(
        self,
        library_manager: LibraryManager,
        library_info: LibraryManager.LibraryInfo,
        register_one: AsyncMock | None = None,
    ) -> Iterator[None]:
        """Patch out everything before the fitness match so only the hook's gating is exercised.

        Pass `register_one` to stand in for `register_workflows_for_library` and assert on whether
        it was awaited; leave it out to let the real one run.
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
                stack.enter_context(patch.object(library_manager, "register_workflows_for_library", register_one))
            yield

    @pytest.mark.parametrize(
        "fitness",
        [
            LibraryManager.LibraryFitness.GOOD,
            LibraryManager.LibraryFitness.FLAWED,
            LibraryManager.LibraryFitness.NOT_EVALUATED,
        ],
    )
    @pytest.mark.asyncio
    async def test_registers_workflows_for_a_mid_session_install(
        self, engine: Engine, tmp_path: Path, fitness: LibraryManager.LibraryFitness
    ) -> None:
        """Every fitness the handler reports success for, not just the healthy one.

        `FLAWED` means some of the library's nodes failed to load and `NOT_EVALUATED` means node
        loading is deferred to a worker. Either way the library is registered and its workflows
        belong in the list: registering one parses the file's TOML header and never imports a node
        class, so there is nothing to wait for. `UNUSABLE` is the fitness that does not get here,
        covered below.
        """
        library_manager = engine.library_manager
        library_manager._libraries_loading_complete.set()
        library_info = _library_info(tmp_path / "lib.json")
        library_info.fitness = fitness
        register_one = AsyncMock(return_value=None)

        with self._stub_lifecycle(library_manager, library_info, register_one):
            result = await library_manager.register_library_from_file_request(
                RegisterLibraryFromFileRequest(file_path="/fake/lib.json")
            )

        assert result.succeeded()
        register_one.assert_awaited_once_with(library_info)

    @pytest.mark.asyncio
    async def test_skips_workflows_for_an_unusable_library(self, engine: Engine, tmp_path: Path) -> None:
        library_manager = engine.library_manager
        library_manager._libraries_loading_complete.set()
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
    async def test_registering_with_the_gate_closed_does_not_hang(self, engine: Engine, tmp_path: Path) -> None:
        """Guards the hazard itself, not just the `if` that avoids it.

        The real `register_workflows_for_library` runs against a library that declares a workflow.
        Delete the gate check and this reaches an await on the event the test holds closed, so the
        regression shows up as a timeout rather than as a quietly different result.
        """
        library_manager = engine.library_manager
        library_manager._libraries_loading_complete.clear()
        (tmp_path / "example.py").write_text(_workflow_header(), encoding="utf-8")

        with (
            self._stub_lifecycle(library_manager, _library_info(tmp_path / "lib.json")),
            patch(f"{LIBRARY_MANAGER_MODULE}.LibraryRegistry.get_library", return_value=_library(["example.py"])),
            patch.dict(WorkflowRegistry._workflows, {}, clear=True),
        ):
            result = await asyncio.wait_for(
                library_manager.register_library_from_file_request(
                    RegisterLibraryFromFileRequest(file_path="/fake/lib.json")
                ),
                timeout=10,
            )

            assert result.succeeded()
            assert list(WorkflowRegistry._workflows) == []


class TestBootPairsRegistrationWithTheGate:
    """`load_all_libraries_from_config` is the only thing that closes the loading gate.

    Which makes it the only place that has to make up for the hook it suppresses. Pairing the
    registration pass with the `.set()` that unblocks it keeps the two together instead of
    leaving each caller to remember a follow-up call.
    """

    @pytest.mark.asyncio
    async def test_registers_the_whole_set_after_opening_the_gate(self, engine: Engine, tmp_path: Path) -> None:
        library_manager = engine.library_manager
        library_json = tmp_path / "griptape_nodes_library.json"
        library_info = _library_info(library_json)
        observed = {}

        async def load_one_library(*_args: object) -> None:
            observed["gate_closed_during_the_load"] = not library_manager._libraries_loading_complete.is_set()

        async def register_all() -> None:
            observed["gate_open_when_registering"] = library_manager._libraries_loading_complete.is_set()

        with (
            patch.object(library_manager, "_reconcile_libraries_from_config", AsyncMock(return_value=[])),
            patch.object(
                library_manager,
                "discover_libraries_request",
                AsyncMock(
                    return_value=DiscoverLibrariesResultSuccess(
                        libraries_discovered=[DiscoveredLibrary(path=library_json, is_sandbox=False)],
                        result_details="one discovered library",
                    )
                ),
            ),
            patch.dict(library_manager._library_file_path_to_info, {str(library_json): library_info}, clear=True),
            patch.object(library_manager, "_load_and_track_library", AsyncMock(side_effect=load_one_library)),
            patch.object(library_manager, "_remove_missing_libraries_from_config", MagicMock(return_value=None)),
            patch.object(library_manager, "register_workflows_for_all_libraries", AsyncMock(side_effect=register_all)),
        ):
            await library_manager.load_all_libraries_from_config()

        assert observed == {"gate_closed_during_the_load": True, "gate_open_when_registering": True}

    @pytest.mark.asyncio
    async def test_a_discovery_failure_still_opens_the_gate(self, engine: Engine) -> None:
        """The early exits skip the registration pass, so they must not leave the gate shut.

        A closed gate outlives the load: `on_load_workflow_metadata_request` waits on it, so
        every later attempt to open a workflow would hang rather than merely find the list short.
        """
        library_manager = engine.library_manager
        register_all = AsyncMock(return_value=None)

        with (
            patch.object(library_manager, "_reconcile_libraries_from_config", AsyncMock(return_value=[])),
            patch.object(
                library_manager,
                "discover_libraries_request",
                AsyncMock(return_value=DiscoverLibrariesResultSuccess(libraries_discovered=[], result_details="none")),
            ),
            patch.dict(library_manager._library_file_path_to_info, {}, clear=True),
            patch.object(library_manager, "register_workflows_for_all_libraries", register_all),
        ):
            await library_manager.load_all_libraries_from_config()

        assert library_manager._libraries_loading_complete.is_set()
        register_all.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_discover_libraries_request_is_the_shape_this_test_assumes(self, engine: Engine) -> None:
        """Pins the collaborator the two tests above stub out.

        They stand in a `DiscoverLibrariesResultSuccess` for the real discovery call, so if that
        call ever stops answering with one they would keep passing against a shape the engine no
        longer produces.
        """
        result = await engine.library_manager.discover_libraries_request(DiscoverLibrariesRequest())

        assert isinstance(result, DiscoverLibrariesResultSuccess)


class TestLibraryWorkflowsSurviveAWorkspaceRescan:
    """End to end against the real WorkflowRegistry, no registration mocks."""

    @pytest.mark.asyncio
    async def test_a_workflow_is_registered_under_exactly_one_key(self, engine: Engine, tmp_path: Path) -> None:
        """A registry key is workspace-relative inside the workspace and absolute outside it.

        So the same library file registers under a different key once the workspace moves away
        from it. Registering the set again must not leave the old key behind next to the new one,
        which would show the workflow twice with one copy pointing nowhere.
        """
        library_manager = engine.library_manager
        config_manager = engine.config_manager
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
            await library_manager.register_workflows_for_all_libraries()
            registered_in_first_workspace = list(WorkflowRegistry._workflows)

            # The workspace moves but the library does not, which is what a project switch that
            # leaves library config alone does. Unloading the library is what takes its entries
            # away, and that is what a reload does before loading them again.
            library_manager._unregister_workflows_for_library(LIBRARY_NAME)
            with patch.object(type(config_manager), "workspace_path", tmp_path / "second_workspace"):
                await library_manager.register_workflows_for_all_libraries()
                registered_in_second_workspace = list(WorkflowRegistry._workflows)

        # `as_posix` rather than `str` because `derive_registry_key` normalizes separators to
        # forward slashes, so on Windows the key is "C:/.../test_lib/example" and never the
        # backslashed spelling.
        assert registered_in_first_workspace == ["libraries/test_lib/example"]
        assert registered_in_second_workspace == [(library_dir / "example").as_posix()]

    @pytest.mark.asyncio
    async def test_a_workflow_without_the_griptape_provided_flag_survives_a_rescan(
        self, engine: Engine, tmp_path: Path
    ) -> None:
        """Through the real `refresh_workflow_registry`, with the real clear at the top of it.

        The workflow's header sets `is_template` but not `is_griptape_provided`, so nothing in
        the file spares it. It survives purely because the registry knows the library contributed
        it -- which is the point: a workflow follows the library that ships it rather than
        depending on a flag its author may have omitted.
        """
        library_manager = engine.library_manager
        workflow_manager = engine.workflow_manager
        config_manager = engine.config_manager
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
            await library_manager.register_workflows_for_all_libraries()

            registered = WorkflowRegistry.get_workflow_by_name(registry_key)
            assert registered.metadata.is_griptape_provided is False
            assert registered.library_name == LIBRARY_NAME

            # An empty list skips the workspace scan; the clear is the part under test.
            await workflow_manager.refresh_workflow_registry(workflows_to_register=[])

            assert list(WorkflowRegistry._workflows) == [registry_key]


class TestRekeyWorkflowsForAllLibraries:
    """Surviving the rescan is only half of it: the surviving key has to still resolve."""

    @pytest.mark.asyncio
    async def test_moving_the_workspace_re_derives_the_key(self, engine: Engine, tmp_path: Path) -> None:
        """A project switch that leaves library config alone does not reload libraries.

        So nothing rebuilds their keys, and a key derived against the old workspace resolves
        against the new one to a file that is not there.
        """
        library_manager = engine.library_manager
        config_manager = engine.config_manager
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
            await library_manager.register_workflows_for_all_libraries()
            assert list(WorkflowRegistry._workflows) == ["libraries/test_lib/example"]

            # The library does not move, so it is now outside the workspace and its key is
            # absolute rather than workspace-relative.
            with patch.object(type(config_manager), "workspace_path", tmp_path / "second_workspace"):
                await library_manager.rekey_workflows_for_all_libraries()

                # Exactly one, not the new key beside the stale one: registering alone would
                # skip the key already there and add the re-derived one next to it.
                assert list(WorkflowRegistry._workflows) == [(library_dir / "example").as_posix()]

    @pytest.mark.asyncio
    async def test_skips_a_library_this_engine_never_registered(self, engine: Engine) -> None:
        """Same reason as the register side: `LibraryRegistry` is process-global."""
        library_manager = engine.library_manager
        register_one = AsyncMock(return_value=None)
        other_engines_workflow = MagicMock(library_name="AnotherEnginesLib")

        with (
            patch.dict(library_manager._library_file_path_to_info, {}, clear=True),
            patch(f"{LIBRARY_MANAGER_MODULE}.LibraryRegistry.list_libraries", return_value=["AnotherEnginesLib"]),
            patch.dict(WorkflowRegistry._workflows, {"theirs/example": other_engines_workflow}, clear=True),
            patch.object(library_manager, "register_workflows_for_library", register_one),
        ):
            await library_manager.rekey_workflows_for_all_libraries()

            assert list(WorkflowRegistry._workflows) == ["theirs/example"]

        register_one.assert_not_awaited()
