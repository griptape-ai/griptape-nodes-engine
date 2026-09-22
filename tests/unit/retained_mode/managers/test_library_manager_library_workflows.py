"""Tests for registering and unregistering the workflows a library declares."""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from griptape_nodes.node_library.library_registry import Library, LibraryMetadata, LibrarySchema
from griptape_nodes.node_library.workflow_registry import (
    WORKSPACE_WORKFLOW_SOURCE,
    WorkflowMetadata,
    WorkflowRegistry,
    WorkflowSource,
)
from griptape_nodes.retained_mode.events.app_events import WorkflowsChanged
from griptape_nodes.retained_mode.events.library_events import (
    CheckLibraryUpdateRequest,
    CheckLibraryUpdateResultSuccess,
    DiscoveredLibrary,
    DiscoverLibrariesRequest,
    DiscoverLibrariesResultSuccess,
    DownloadLibraryRequest,
    ListRegisteredLibrariesRequest,
    ListRegisteredLibrariesResultSuccess,
    LoadLibrariesRequest,
    LoadLibrariesResultSuccess,
    RegisterLibraryFromFileRequest,
    RegisterLibraryFromFileResultFailure,
    RegisterLibraryFromFileResultSuccess,
    SyncLibrariesRequest,
    SyncLibrariesResultSuccess,
    UnloadLibraryFromRegistryRequest,
    UnloadLibraryFromRegistryResultSuccess,
    UpdateLibraryRequest,
    UpdateLibraryResultFailure,
    UpdateLibraryResultSuccess,
)
from griptape_nodes.retained_mode.managers.library_manager import LibraryManager
from griptape_nodes.retained_mode.managers.workflow_manager import WorkflowRegistrationResult

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator
    from pathlib import Path

    from griptape_nodes.retained_mode.engine import Engine
    from griptape_nodes.retained_mode.events.base_events import ResultPayload

LIBRARY_MANAGER_MODULE = "griptape_nodes.retained_mode.managers.library_manager"
LIBRARY_NAME = "TestLib"


def _library_info(
    library_path: Path, *, is_sandbox: bool = False, library_name: str = LIBRARY_NAME
) -> LibraryManager.LibraryInfo:
    return LibraryManager.LibraryInfo(
        lifecycle_state=LibraryManager.LibraryLifecycleState.LOADED,
        fitness=LibraryManager.LibraryFitness.GOOD,
        library_path=str(library_path),
        is_sandbox=is_sandbox,
        library_name=library_name,
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


def _workflow_header(name: str = "example") -> str:
    """The metadata header a workflow file needs before the engine will register it."""
    lines = [
        "# /// script",
        "# [tool.griptape-nodes]",
        f'# name = "{name}"',
        f'# schema_version = "{WorkflowMetadata.LATEST_SCHEMA_VERSION}"',
        '# engine_version_created_with = "0.0.0"',
        "# node_libraries_referenced = []",
        "# is_template = true",
        "# ///",
    ]
    return "\n".join(lines) + "\n"


def _emitted_workflow_changes(event_manager: MagicMock) -> list[WorkflowsChanged]:
    """Pull the workflow-change payloads out of a mocked event manager's put_event calls."""
    payloads = [call.args[0].payload for call in event_manager.put_event.call_args_list]
    return [payload for payload in payloads if isinstance(payload, WorkflowsChanged)]


def _library_entry() -> MagicMock:
    """A registry entry standing in for one this library contributed."""
    return MagicMock(source=WorkflowSource.for_library(LIBRARY_NAME))


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
        """The source is what ties the entries to the library, so it has to reach the registry."""
        register = AsyncMock(return_value=WorkflowRegistrationResult(succeeded=["example"], failed=[]))
        event_manager = MagicMock()

        with (
            patch(f"{LIBRARY_MANAGER_MODULE}.LibraryRegistry.get_library", return_value=_library(["example.py"])),
            patch.object(engine.workflow_manager, "register_list_of_workflows", register),
            patch.object(engine, "_event_manager", event_manager),
        ):
            await engine.library_manager.register_workflows_for_library(_library_info(tmp_path / "lib.json"))

        register.assert_awaited_once_with(
            [str(tmp_path / "example.py")], source=WorkflowSource.for_library(LIBRARY_NAME)
        )
        changes = _emitted_workflow_changes(event_manager)
        assert len(changes) == 1
        assert changes[0].source == WorkflowSource.for_library(LIBRARY_NAME)
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
        mine = _library_entry()
        theirs = MagicMock(source=WorkflowSource.for_library("OtherLib"))
        users = MagicMock(source=WORKSPACE_WORKFLOW_SOURCE)

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
        assert changes[0].source == WorkflowSource.for_library(LIBRARY_NAME)
        assert changes[0].workflow_names == ["lib/example"]
        assert changes[0].registered is False

    def test_says_nothing_for_a_library_that_contributed_none(self, engine: Engine) -> None:
        event_manager = MagicMock()

        with (
            patch.dict(
                WorkflowRegistry._workflows,
                {"user_workflow": MagicMock(source=WORKSPACE_WORKFLOW_SOURCE)},
                clear=True,
            ),
            patch.object(engine, "_event_manager", event_manager),
        ):
            engine.library_manager._unregister_workflows_for_library(LIBRARY_NAME)

        assert _emitted_workflow_changes(event_manager) == []

    def test_leaves_alone_a_library_this_engine_never_registered(self, engine: Engine) -> None:
        """The mirror of the guard on the register side, and for the same reason.

        `WorkflowRegistry` is process-global and a library's entries are identified by its name
        alone, so in a process running more than one Engine an unguarded delete would take the
        other engine's entries for a library this one has never seen.
        """
        event_manager = MagicMock()

        with (
            patch.dict(engine.library_manager._library_file_path_to_info, {}, clear=True),
            patch.dict(WorkflowRegistry._workflows, {"lib/example": _library_entry()}, clear=True),
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
            patch.dict(WorkflowRegistry._workflows, {"lib/example": _library_entry()}, clear=True),
        ):
            result = engine.library_manager.unload_library_from_registry_request(
                UnloadLibraryFromRegistryRequest(library_name=LIBRARY_NAME)
            )

            assert result.succeeded()
            assert "lib/example" not in WorkflowRegistry._workflows


class TestTheRequestHandlerAndTheBatchLoopUseSeparateDoors:
    """One library arriving on request registers its workflows; one out of a whole set does not.

    They used to share `register_library_from_file_request`, which is why it could not tell which
    it was serving and had to work it out from the loading gate. Splitting the lifecycle work into
    `_register_library_from_file` lets each caller say plainly which it is: the handler is the
    mid-session door and registers, the batch loops take the inner one and leave it to the pass
    that runs when their set is complete.
    """

    @contextlib.contextmanager
    def _stub_lifecycle(
        self,
        library_manager: LibraryManager,
        library_info: LibraryManager.LibraryInfo,
        register_one: AsyncMock,
    ) -> Iterator[None]:
        """Patch out everything before the fitness match, so only what follows it is exercised.

        `register_one` stands in for `register_workflows_for_library`, to assert on whether the
        call under test reaches it.
        """
        prerequisites = LibraryManager.RegisterLibraryPrerequisites(
            library_info=library_info, file_path=library_info.library_path
        )
        with (
            patch.object(
                library_manager,
                "_establish_register_library_prerequisites",
                AsyncMock(return_value=prerequisites),
            ),
            patch.object(library_manager, "_progress_library_through_lifecycle", AsyncMock(return_value=None)),
            patch.object(library_manager, "register_workflows_for_library", register_one),
        ):
            yield

    @pytest.mark.parametrize(
        ("fitness", "expected_result", "expect_workflows_registered"),
        [
            (LibraryManager.LibraryFitness.GOOD, RegisterLibraryFromFileResultSuccess, True),
            (LibraryManager.LibraryFitness.FLAWED, RegisterLibraryFromFileResultSuccess, True),
            (LibraryManager.LibraryFitness.NOT_EVALUATED, RegisterLibraryFromFileResultSuccess, True),
            (LibraryManager.LibraryFitness.UNUSABLE, RegisterLibraryFromFileResultFailure, False),
        ],
    )
    @pytest.mark.asyncio
    async def test_the_handler_registers_for_every_fitness_it_reports_success_for(
        self,
        engine: Engine,
        tmp_path: Path,
        fitness: LibraryManager.LibraryFitness,
        expected_result: type[ResultPayload],
        expect_workflows_registered: bool,  # noqa: FBT001 (pytest fills parametrized args positionally)
    ) -> None:
        """Not just the healthy verdict.

        `FLAWED` means some of the library's nodes failed to load and `NOT_EVALUATED` means node
        loading is deferred to a worker. Either way the library is registered and its templates
        belong in the picker: registering one parses the file's TOML header and never imports a
        node class, so there is nothing to wait for. `UNUSABLE` is the one that gets no further.
        """
        library_manager = engine.library_manager
        library_info = _library_info(tmp_path / "lib.json")
        library_info.fitness = fitness
        register_one = AsyncMock(return_value=None)

        with (
            self._stub_lifecycle(library_manager, library_info, register_one),
            patch(f"{LIBRARY_MANAGER_MODULE}.LibraryRegistry.list_libraries", return_value=[LIBRARY_NAME]),
            patch.dict(
                library_manager._library_file_path_to_info, {library_info.library_path: library_info}, clear=True
            ),
        ):
            result = await library_manager.register_library_from_file_request(
                RegisterLibraryFromFileRequest(file_path="/fake/lib.json")
            )

        assert isinstance(result, expected_result)
        assert register_one.await_count == (1 if expect_workflows_registered else 0)

    @pytest.mark.asyncio
    async def test_the_inner_callee_the_batch_loops_take_registers_nothing(
        self, engine: Engine, tmp_path: Path
    ) -> None:
        """What `_load_and_track_library` and `_load_every_discovered_library` call.

        Register here and a library loading partway through a batch would resolve its workflows'
        `node_libraries_referenced` against a half-loaded registry, and would deadlock besides:
        reading a workflow header waits on the very gate the enclosing load holds closed.
        """
        library_manager = engine.library_manager
        library_info = _library_info(tmp_path / "lib.json")
        register_one = AsyncMock(return_value=None)

        with self._stub_lifecycle(library_manager, library_info, register_one):
            result = await library_manager._register_library_from_file(
                RegisterLibraryFromFileRequest(file_path="/fake/lib.json")
            )

        assert isinstance(result, RegisterLibraryFromFileResultSuccess)
        register_one.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_an_already_loaded_library_is_not_re_registered(self, engine: Engine, tmp_path: Path) -> None:
        """Opening a workflow re-requests every library it names, and most are already in.

        Registration is idempotent, so this is about cost rather than correctness: each pass would
        otherwise re-read the metadata header of every template every one of those libraries ships.
        """
        library_manager = engine.library_manager
        library_info = _library_info(tmp_path / "lib.json")
        register_one = AsyncMock(return_value=None)
        already_loaded = RegisterLibraryFromFileResultSuccess(
            library_name=LIBRARY_NAME, was_already_loaded=True, result_details="already loaded"
        )

        with (
            patch.object(
                library_manager, "_establish_register_library_prerequisites", AsyncMock(return_value=already_loaded)
            ),
            patch.object(library_manager, "register_workflows_for_library", register_one),
            patch.dict(
                library_manager._library_file_path_to_info, {library_info.library_path: library_info}, clear=True
            ),
        ):
            result = await library_manager.register_library_from_file_request(
                RegisterLibraryFromFileRequest(file_path="/fake/lib.json")
            )

        assert result is already_loaded
        register_one.assert_not_awaited()


class TestTheWholeSetRegistersAfterTheLoad:
    """A load of every library registers their workflows in one pass once the set is complete.

    Two reasons it cannot happen per library on the way through. A workflow resolves its
    `node_libraries_referenced` against `LibraryRegistry` as it stands when it registers, so one
    naming a sibling still to load would be reported as depending on a library that is not
    installed when it is merely not installed *yet* -- and nothing recomputes that, because the
    workspace rescan skips registered-library roots. And registering reads each workflow's
    metadata header through `WorkflowManager.on_load_workflow_metadata_request`, which waits on
    the loading gate the load itself holds closed.
    """

    @pytest.mark.asyncio
    async def test_the_pass_runs_after_the_gate_reopens(self, engine: Engine, tmp_path: Path) -> None:
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
    async def test_the_real_pass_does_not_hang_behind_the_gate(self, engine: Engine, tmp_path: Path) -> None:
        """Guards the hazard, not just the ordering the test above reads off.

        Nothing runs the unmocked pass in the tests above, so move it back inside the gate and they
        keep passing. Here the real `register_workflows_for_all_libraries` runs against a library
        declaring a real workflow file, so that move shows up as this timing out instead.
        """
        library_manager = engine.library_manager
        library_json = tmp_path / "griptape_nodes_library.json"
        (tmp_path / "example.py").write_text(_workflow_header(), encoding="utf-8")

        with (
            patch.object(library_manager, "_reconcile_libraries_from_config", AsyncMock(return_value=[])),
            patch.object(
                library_manager,
                "discover_libraries_request",
                AsyncMock(return_value=DiscoverLibrariesResultSuccess(libraries_discovered=[], result_details="none")),
            ),
            patch.dict(
                library_manager._library_file_path_to_info, {str(library_json): _library_info(library_json)}, clear=True
            ),
            patch(f"{LIBRARY_MANAGER_MODULE}.LibraryRegistry.list_libraries", return_value=[LIBRARY_NAME]),
            patch(f"{LIBRARY_MANAGER_MODULE}.LibraryRegistry.get_library", return_value=_library(["example.py"])),
            patch.dict(WorkflowRegistry._workflows, {}, clear=True),
        ):
            await asyncio.wait_for(library_manager.load_all_libraries_from_config(), timeout=10)

            registered = list(WorkflowRegistry._workflows.values())

        # The key itself is absolute here, because tmp_path sits outside the workspace. What this
        # asserts is that the pass ran to completion and recorded the library as the owner.
        assert [workflow.source for workflow in registered] == [WorkflowSource.for_library(LIBRARY_NAME)]

    @pytest.mark.asyncio
    async def test_an_early_exit_still_opens_the_gate_and_still_registers(self, engine: Engine) -> None:
        """The load returns early when it finds nothing to load, and must not leave the gate shut.

        A closed gate outlives the load: `on_load_workflow_metadata_request` waits on it, so every
        later attempt to open a workflow would hang rather than merely find the list short. The
        pass runs either way; with nothing loaded it has nothing to register.
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
        register_all.assert_awaited_once_with()

    @pytest.mark.asyncio
    async def test_a_raising_load_opens_the_gate_and_skips_the_pass(self, engine: Engine) -> None:
        """An incomplete library set is not worth registering against.

        Whatever owns the failed load -- a reload, or boot -- runs its own once it recovers. The
        gate still has to reopen, for the same reason as above.
        """
        library_manager = engine.library_manager
        register_all = AsyncMock(return_value=None)

        with (
            patch.object(
                library_manager, "_reconcile_libraries_from_config", AsyncMock(side_effect=RuntimeError("boom"))
            ),
            patch.object(library_manager, "register_workflows_for_all_libraries", register_all),
            pytest.raises(RuntimeError, match="boom"),
        ):
            await library_manager.load_all_libraries_from_config()

        assert library_manager._libraries_loading_complete.is_set()
        register_all.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_load_libraries_request_runs_the_pass_after_its_own_loop(self, engine: Engine) -> None:
        """The other whole-set load: a registered handler any client can send.

        `SyncLibrariesRequest` reaches it in its Phase 2, so it loads several libraries in a pass
        exactly the way boot does and owes the same one pass afterwards.
        """
        library_manager = engine.library_manager
        calls: list[str] = []

        async def load_every_library() -> LoadLibrariesResultSuccess:
            calls.append("load")
            return LoadLibrariesResultSuccess(result_details="loaded")

        async def register_all() -> None:
            calls.append("register")

        with (
            patch.object(library_manager, "_load_every_discovered_library", AsyncMock(side_effect=load_every_library)),
            patch.object(library_manager, "register_workflows_for_all_libraries", AsyncMock(side_effect=register_all)),
        ):
            result = await library_manager.load_libraries_request(LoadLibrariesRequest())

        assert result.succeeded()
        assert calls == ["load", "register"]

    @pytest.mark.asyncio
    async def test_the_loop_takes_the_door_that_does_not_register(self, engine: Engine, tmp_path: Path) -> None:
        """The other half of the split, from the batch side.

        The pass afterwards is only safe because nothing inside the loop registers. Point the loop
        back at `register_library_from_file_request` and every library would register partway
        through the load -- against a half-loaded registry, behind a gate the load holds closed.
        """
        library_manager = engine.library_manager
        library_json = tmp_path / "griptape_nodes_library.json"
        arrival = RegisterLibraryFromFileResultSuccess(library_name=LIBRARY_NAME, result_details="registered")
        register_one_library = AsyncMock(return_value=arrival)
        mid_session_door = AsyncMock(return_value=arrival)

        with (
            patch.object(library_manager, "_register_library_from_file", register_one_library),
            patch.object(library_manager, "register_library_from_file_request", mid_session_door),
            patch.dict(library_manager._library_file_path_to_info, {}, clear=True),
        ):
            await library_manager._load_and_track_library(str(library_json), index=1, total=1)

        register_one_library.assert_awaited_once()
        mid_session_door.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_discover_libraries_request_is_the_shape_these_tests_assume(self, engine: Engine) -> None:
        """Pins the collaborator the tests above stub out.

        They stand in a `DiscoverLibrariesResultSuccess` for the real discovery call, so if that
        call ever stops answering with one they would keep passing against a shape the engine no
        longer produces.
        """
        result = await engine.library_manager.discover_libraries_request(DiscoverLibrariesRequest())

        assert isinstance(result, DiscoverLibrariesResultSuccess)


class TestEachMidSessionArrivalGoesThroughTheRegisteringDoor:
    """A library arriving on its own registers its own workflows, and gets there via the handler.

    Every other library is already loaded on these paths, so a workflow's
    `node_libraries_referenced` resolves against the full set and there is no whole-set pass to
    wait for. Two handlers bring one library in mid-session and both reach it the same way: they
    dispatch `RegisterLibraryFromFileRequest`, and registering the templates is what that handler
    does. So neither registers anything itself, and neither may take the inner
    `_register_library_from_file` shortcut the batch loops use. `TestTheConcurrentSyncBatch` below
    covers the one caller that drives these doors several at a time.

    These tests run the real handler behind the mocked dispatch for exactly that reason -- asserting
    only that some request went out would pass just as happily if the path called the inner one.
    """

    def _register_through_the_real_handler(
        self, library_manager: LibraryManager, register_one: AsyncMock
    ) -> Callable[[object], Awaitable[object]]:
        """Build an `ahandle_request` side effect that routes registrations to the real handler.

        The lifecycle work below the handler is stubbed out -- there is no library on disk here --
        leaving the part under test: whether reaching this handler registers the workflows.
        `register_one` stands in for `register_workflows_for_library`.
        """
        arrival = RegisterLibraryFromFileResultSuccess(library_name=LIBRARY_NAME, result_details="registered")

        async def dispatch(request: object) -> object:
            if isinstance(request, RegisterLibraryFromFileRequest):
                with (
                    patch.object(library_manager, "_register_library_from_file", AsyncMock(return_value=arrival)),
                    patch.object(library_manager, "register_workflows_for_library", register_one),
                ):
                    return await library_manager.register_library_from_file_request(request)
            msg = f"Unexpected request: {type(request).__name__}"
            raise AssertionError(msg)

        return dispatch

    @pytest.mark.asyncio
    async def test_a_git_reload_puts_the_libraries_workflows_back(self, engine: Engine, tmp_path: Path) -> None:
        """Both `update_library_request` and `switch_library_ref_request` land here.

        The unload at the top of the reload takes this library's entries out of the registry, so
        the reload has to put back whatever the updated library now declares -- otherwise an update
        silently empties its own templates out of the picker.
        """
        library_manager = engine.library_manager
        library_json = tmp_path / "griptape_nodes_library.json"
        library_json.write_text("{}", encoding="utf-8")
        register_one = AsyncMock(return_value=None)

        with (
            patch.object(
                engine,
                "handle_request",
                MagicMock(return_value=UnloadLibraryFromRegistryResultSuccess(result_details="unloaded")),
            ),
            patch(f"{LIBRARY_MANAGER_MODULE}.find_file_in_directory", return_value=library_json),
            patch.object(
                engine,
                "ahandle_request",
                AsyncMock(side_effect=self._register_through_the_real_handler(library_manager, register_one)),
            ),
            patch(f"{LIBRARY_MANAGER_MODULE}.LibraryRegistry.get_library", return_value=_library(["example.py"])),
        ):
            result = await library_manager._reload_library_after_git_operation(
                library_name=LIBRARY_NAME,
                library_file_path=str(library_json),
                failure_result_class=UpdateLibraryResultFailure,
            )

        assert result == "1.0.0"
        register_one.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_download_registers_the_library_it_brought_in(self, engine: Engine, tmp_path: Path) -> None:
        """`auto_register` means the templates belong in the picker without a restart."""
        library_manager = engine.library_manager
        library_json = tmp_path / "griptape_nodes_library.json"
        register_one = AsyncMock(return_value=None)

        downloaded = AsyncMock()
        downloaded.mkdir = AsyncMock(return_value=None)
        # exists() -> True takes the skip_clone path, so no git clone runs.
        downloaded.exists = AsyncMock(return_value=True)
        downloaded.read_text = AsyncMock(return_value=json.dumps({"name": LIBRARY_NAME}))

        with (
            patch(f"{LIBRARY_MANAGER_MODULE}.anyio.Path", return_value=downloaded),
            patch(f"{LIBRARY_MANAGER_MODULE}.find_file_in_directory", return_value=str(library_json)),
            patch.object(
                engine,
                "ahandle_request",
                AsyncMock(side_effect=self._register_through_the_real_handler(library_manager, register_one)),
            ),
            patch.object(engine.config_manager, "get_config_value", MagicMock(return_value=[])),
            patch.object(engine.config_manager, "set_config_value", MagicMock(return_value=None)),
            patch.dict(library_manager._library_file_path_to_info, {}, clear=True),
        ):
            result = await library_manager.download_library_request(
                DownloadLibraryRequest(
                    git_url="https://example.invalid/lib.git",
                    download_directory=str(tmp_path),
                    auto_register=True,
                    fail_on_exists=False,
                )
            )

        assert result.succeeded(), result.result_details
        register_one.assert_awaited_once()


class TestTheConcurrentSyncBatch:
    """Sync is the one batch built out of mid-session doors: it drives `UpdateLibraryRequest`.

    Each update reloads one library and registers its workflows itself, which is right for a
    library arriving alone and wrong for several at once -- so this batch cannot use the gate to
    hold them back (it would deadlock), and has to repair the result afterwards instead.
    """

    @pytest.mark.asyncio
    async def test_sync_leaves_the_gate_open_for_the_updates_it_drives(self, engine: Engine) -> None:
        """Sync drives `UpdateLibraryRequest` per library, and each one registers its own.

        So the gate has to stay open across the whole sync. Close it around the update pass and
        every update inside would suspend on the event only that pass can set -- registering reads
        workflow metadata through `on_load_workflow_metadata_request`, which waits on it. The check
        pass has the same requirement for its own reason: `check_library_update_request` waits on
        the gate too.
        """
        library_manager = engine.library_manager
        observed = {}

        async def dispatch(request: object) -> object:
            if isinstance(request, LoadLibrariesRequest):
                return LoadLibrariesResultSuccess(result_details="loaded")
            if isinstance(request, ListRegisteredLibrariesRequest):
                return ListRegisteredLibrariesResultSuccess(libraries=[LIBRARY_NAME], result_details="one library")
            if isinstance(request, CheckLibraryUpdateRequest):
                observed["gate_open_during_the_check_pass"] = library_manager._libraries_loading_complete.is_set()
                return CheckLibraryUpdateResultSuccess(
                    has_update=True,
                    current_version="1.0.0",
                    latest_version="2.0.0",
                    git_remote="https://example.invalid/lib.git",
                    git_ref="main",
                    local_commit="aaaaaaa",
                    remote_commit="bbbbbbb",
                    result_details="update available",
                )
            if isinstance(request, UpdateLibraryRequest):
                observed["gate_open_during_the_update_pass"] = library_manager._libraries_loading_complete.is_set()
                return UpdateLibraryResultSuccess(old_version="1.0.0", new_version="2.0.0", result_details="updated")
            msg = f"Unexpected request: {type(request).__name__}"
            raise AssertionError(msg)

        register_all = AsyncMock(return_value=None)

        with (
            patch.object(engine.config_manager, "get_config_value", MagicMock(return_value=[])),
            patch.object(engine, "ahandle_request", AsyncMock(side_effect=dispatch)),
            patch.object(library_manager, "register_workflows_for_all_libraries", register_all),
        ):
            result = await library_manager.sync_libraries_request(SyncLibrariesRequest())

        assert isinstance(result, SyncLibrariesResultSuccess)
        assert result.libraries_updated == 1
        assert observed == {"gate_open_during_the_check_pass": True, "gate_open_during_the_update_pass": True}
        # Sync does not touch the whole set: the updates it drives each reload one library, and
        # the pass afterwards rebuilds exactly those. See the test below.
        register_all.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_sync_rebuilds_the_updated_libraries_workflows_after_the_batch(self, engine: Engine) -> None:
        """The updates run concurrently, so each one registers while siblings are mid-unload.

        A workflow naming a sibling that is between its own unload and reload is recorded as
        depending on a library that is not installed, and nothing recomputes that. So sync takes
        the updated libraries' entries out and puts them back once every update has finished --
        out and back in, because re-registering skips a key already in the registry.
        """
        library_manager = engine.library_manager
        other_library = "OtherLib"
        updating = [LIBRARY_NAME, other_library]
        sequence: list[str] = []

        async def dispatch(request: object) -> object:
            if isinstance(request, LoadLibrariesRequest):
                return LoadLibrariesResultSuccess(result_details="loaded")
            if isinstance(request, ListRegisteredLibrariesRequest):
                return ListRegisteredLibrariesResultSuccess(libraries=updating, result_details="two libraries")
            if isinstance(request, CheckLibraryUpdateRequest):
                return CheckLibraryUpdateResultSuccess(
                    has_update=True,
                    current_version="1.0.0",
                    latest_version="2.0.0",
                    git_remote="https://example.invalid/lib.git",
                    git_ref="main",
                    local_commit="aaaaaaa",
                    remote_commit="bbbbbbb",
                    result_details="update available",
                )
            if isinstance(request, UpdateLibraryRequest):
                sequence.append(f"update:{request.library_name}")
                return UpdateLibraryResultSuccess(old_version="1.0.0", new_version="2.0.0", result_details="updated")
            msg = f"Unexpected request: {type(request).__name__}"
            raise AssertionError(msg)

        def unregister(library_name: str) -> None:
            sequence.append(f"out:{library_name}")

        async def register(library_name: str) -> None:
            sequence.append(f"in:{library_name}")

        with (
            patch.object(engine.config_manager, "get_config_value", MagicMock(return_value=[])),
            patch.object(engine, "ahandle_request", AsyncMock(side_effect=dispatch)),
            patch.object(library_manager, "_unregister_workflows_for_library", MagicMock(side_effect=unregister)),
            patch.object(library_manager, "register_workflows_for_registered_library", AsyncMock(side_effect=register)),
        ):
            result = await library_manager.sync_libraries_request(SyncLibrariesRequest())

        assert isinstance(result, SyncLibrariesResultSuccess)
        assert result.libraries_updated == len(updating)
        # The updates go first, in whichever order the task group finishes them. Every rebuild
        # lands after all of them, which is the point: no library is mid-unload by then.
        updates, rebuilds = sequence[: len(updating)], sequence[len(updating) :]
        assert sorted(updates) == sorted(f"update:{name}" for name in updating)
        assert rebuilds == [f"out:{LIBRARY_NAME}", f"in:{LIBRARY_NAME}", f"out:{other_library}", f"in:{other_library}"]

    @pytest.mark.asyncio
    async def test_sync_rebuilds_nothing_when_no_library_updated(self, engine: Engine) -> None:
        """Nothing registered during the batch, so there is no mid-batch verdict to redo.

        Rebuilding anyway would take every library's workflows out of the picker and put them back
        on every sync, announcing a removal and a re-add to clients for no change.
        """
        library_manager = engine.library_manager
        unregister = MagicMock(return_value=None)
        register = AsyncMock(return_value=None)

        async def dispatch(request: object) -> object:
            if isinstance(request, LoadLibrariesRequest):
                return LoadLibrariesResultSuccess(result_details="loaded")
            if isinstance(request, ListRegisteredLibrariesRequest):
                return ListRegisteredLibrariesResultSuccess(libraries=[LIBRARY_NAME], result_details="one library")
            if isinstance(request, CheckLibraryUpdateRequest):
                return CheckLibraryUpdateResultSuccess(
                    has_update=False,
                    current_version="1.0.0",
                    latest_version="1.0.0",
                    git_remote="https://example.invalid/lib.git",
                    git_ref="main",
                    local_commit="aaaaaaa",
                    remote_commit="aaaaaaa",
                    result_details="up to date",
                )
            msg = f"Unexpected request: {type(request).__name__}"
            raise AssertionError(msg)

        with (
            patch.object(engine.config_manager, "get_config_value", MagicMock(return_value=[])),
            patch.object(engine, "ahandle_request", AsyncMock(side_effect=dispatch)),
            patch.object(library_manager, "_unregister_workflows_for_library", unregister),
            patch.object(library_manager, "register_workflows_for_registered_library", register),
        ):
            result = await library_manager.sync_libraries_request(SyncLibrariesRequest())

        assert isinstance(result, SyncLibrariesResultSuccess)
        assert result.libraries_updated == 0
        unregister.assert_not_called()
        register.assert_not_awaited()


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
            assert registered.source == WorkflowSource.for_library(LIBRARY_NAME)

            # An empty list skips the workspace scan; the clear is the part under test.
            await workflow_manager.refresh_workflow_registry(workflows_to_register=[])

            assert list(WorkflowRegistry._workflows) == [registry_key]

    @pytest.mark.asyncio
    async def test_the_scan_skips_installed_library_roots_but_still_walks_sandbox_ones(
        self, engine: Engine, tmp_path: Path
    ) -> None:
        """Libraries live under the workspace by default, so the scan walks straight into them.

        Claiming an installed library's files would register them a second time as the workspace's,
        and that copy would outlive the library. Sandbox libraries are the deliberate exception:
        they are the directory an author is actively editing, so their workflows have to appear
        from the scan rather than waiting on a library reload to publish them.
        """
        workflow_manager = engine.workflow_manager
        config_manager = engine.config_manager
        workspace = tmp_path / "workspace"

        installed_dir = workspace / "libraries" / "installed_lib"
        installed_dir.mkdir(parents=True)
        (installed_dir / "shipped.py").write_text(_workflow_header("shipped"), encoding="utf-8")
        installed_info = _library_info(installed_dir / "griptape_nodes_library.json", library_name="InstalledLib")

        sandbox_dir = workspace / "libraries" / "sandbox_lib"
        sandbox_dir.mkdir(parents=True)
        (sandbox_dir / "in_development.py").write_text(_workflow_header("in_development"), encoding="utf-8")
        sandbox_info = _library_info(
            sandbox_dir / "griptape_nodes_library.json", is_sandbox=True, library_name="SandboxLib"
        )

        with (
            patch.dict(
                engine.library_manager._library_file_path_to_info,
                {installed_info.library_path: installed_info, sandbox_info.library_path: sandbox_info},
                clear=True,
            ),
            patch.dict(WorkflowRegistry._workflows, {}, clear=True),
            patch.object(type(config_manager), "workspace_path", workspace),
        ):
            await workflow_manager.refresh_workflow_registry(workflows_to_register=[str(workspace)])
            registered = sorted(WorkflowRegistry._workflows)

        assert registered == ["libraries/sandbox_lib/in_development"]

    @pytest.mark.asyncio
    async def test_a_library_registering_a_directory_still_claims_its_own_files(
        self, engine: Engine, tmp_path: Path
    ) -> None:
        """Skipping library roots is the workspace scan's business, not every caller's.

        A library hands over explicit file paths today, and those never reach the directory walk
        that consults the exclusion roots. Handing over its own directory is the case that would:
        the library's own root is an exclusion candidate, so without the source check a library
        would exclude its own files and registering its workflows would silently do nothing.
        """
        workflow_manager = engine.workflow_manager
        config_manager = engine.config_manager
        workspace = tmp_path / "workspace"
        library_dir = workspace / "libraries" / "test_lib"
        library_dir.mkdir(parents=True)
        (library_dir / "example.py").write_text(_workflow_header(), encoding="utf-8")
        library_info = _library_info(library_dir / "griptape_nodes_library.json")

        with (
            patch.dict(
                engine.library_manager._library_file_path_to_info,
                {library_info.library_path: library_info},
                clear=True,
            ),
            patch.dict(WorkflowRegistry._workflows, {}, clear=True),
            patch.object(type(config_manager), "workspace_path", workspace),
        ):
            await workflow_manager._process_workflows_for_registration(
                [str(library_dir)], source=WorkflowSource.for_library(LIBRARY_NAME)
            )
            registered = list(WorkflowRegistry._workflows)

        assert registered == ["libraries/test_lib/example"]


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
        other_engines_workflow = MagicMock(source=WorkflowSource.for_library("AnotherEnginesLib"))

        with (
            patch.dict(library_manager._library_file_path_to_info, {}, clear=True),
            patch(f"{LIBRARY_MANAGER_MODULE}.LibraryRegistry.list_libraries", return_value=["AnotherEnginesLib"]),
            patch.dict(WorkflowRegistry._workflows, {"theirs/example": other_engines_workflow}, clear=True),
            patch.object(library_manager, "register_workflows_for_library", register_one),
        ):
            await library_manager.rekey_workflows_for_all_libraries()

            assert list(WorkflowRegistry._workflows) == ["theirs/example"]

        register_one.assert_not_awaited()
