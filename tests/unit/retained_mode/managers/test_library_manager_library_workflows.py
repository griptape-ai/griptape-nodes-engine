"""Tests for registering and unregistering the workflows a library declares.

Registering one only parses its metadata header, and never waits on the libraries loading gate, so a
library can register its workflows at any point in a load. Whether a workflow's libraries are
installed is judged later, when someone asks, against the library set as it is then.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from griptape_nodes.node_library.library_registry import Library, LibraryMetadata, LibrarySchema
from griptape_nodes.node_library.workflow_registry import WorkflowMetadata
from griptape_nodes.retained_mode.events.app_events import WorkflowRegistryChanged
from griptape_nodes.retained_mode.events.base_events import AppEvent
from griptape_nodes.retained_mode.events.library_events import (
    DownloadLibraryRequest,
    RegisterLibraryFromFileRequest,
    RegisterLibraryFromFileResultFailure,
    RegisterLibraryFromFileResultSuccess,
    UnloadLibraryFromRegistryRequest,
    UnloadLibraryFromRegistryResultSuccess,
    UpdateLibraryResultFailure,
)
from griptape_nodes.retained_mode.managers.library_manager import LibraryManager
from griptape_nodes.retained_mode.managers.workflow_manager import WorkflowManager, WorkflowRegistrationResult

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


def _registry_changes_announced(put_event: MagicMock) -> int:
    """Count the `WorkflowRegistryChanged` events a mocked `put_event` was handed."""
    events = [call.args[0] for call in put_event.call_args_list]
    app_events = [event for event in events if isinstance(event, AppEvent)]
    return sum(1 for event in app_events if isinstance(event.payload, WorkflowRegistryChanged))


def _library_entry(file_path: str = "lib/example.py") -> MagicMock:
    """A registry entry standing in for one this library contributed."""
    return MagicMock(library_name=LIBRARY_NAME, file_path=file_path)


@contextlib.contextmanager
def _stub_library_lifecycle(
    library_manager: LibraryManager,
    library_info: LibraryManager.LibraryInfo,
    register_one: AsyncMock | None = None,
) -> Iterator[None]:
    """Patch out everything before the fitness match, so only what follows it is exercised.

    There is no library on disk in these tests. Pass `register_one` to stand in for
    `register_workflows_for_library` and assert on whether the call under test reaches it; leave it
    out to let the real registration run.
    """
    prerequisites = LibraryManager.RegisterLibraryPrerequisites(
        library_info=library_info, file_path=library_info.library_path
    )
    with (
        patch.object(
            library_manager, "_establish_register_library_prerequisites", AsyncMock(return_value=prerequisites)
        ),
        patch.object(library_manager, "_progress_library_through_lifecycle", AsyncMock(return_value=None)),
    ):
        if register_one is None:
            yield
        else:
            with patch.object(library_manager, "register_workflows_for_library", register_one):
                yield


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

        Adding it again here would mean a second, undocumented owner of the process's import path, and
        a library pointed at a `tmp_path` pytest later deletes would leave a dead directory shadowing
        module resolution.
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
    async def test_registers_them_under_the_library_name(self, engine: Engine, tmp_path: Path) -> None:
        """The library name is what ties the entries to the library, so it has to reach the registry."""
        register = AsyncMock()

        with (
            patch(f"{LIBRARY_MANAGER_MODULE}.LibraryRegistry.get_library", return_value=_library(["example.py"])),
            patch.object(engine.workflow_manager, "register_list_of_workflows", register),
        ):
            await engine.library_manager.register_workflows_for_library(_library_info(tmp_path / "lib.json"))

        register.assert_awaited_once_with([str(tmp_path / "example.py")], library_name=LIBRARY_NAME)

    @pytest.mark.asyncio
    async def test_keys_them_by_their_absolute_path(self, engine: Engine, tmp_path: Path) -> None:
        """Even inside the workspace, so a project switch that moves the workspace leaves them resolving.

        A workspace-relative key would resolve against the new workspace to a file that is not there,
        and a library's workflows are not rescanned when the workspace moves.
        """
        library_manager = engine.library_manager
        config_manager = engine.config_manager
        workspace = tmp_path / "workspace"
        library_dir = workspace / "libraries" / "test_lib"
        library_dir.mkdir(parents=True)
        (library_dir / "example.py").write_text(_workflow_header(), encoding="utf-8")

        with (
            patch(f"{LIBRARY_MANAGER_MODULE}.LibraryRegistry.get_library", return_value=_library(["example.py"])),
            patch.dict(engine.workflow_registry._workflows, {}, clear=True),
            patch.object(type(config_manager), "workspace_path", workspace),
        ):
            await library_manager.register_workflows_for_library(
                _library_info(library_dir / "griptape_nodes_library.json")
            )
            registered = list(engine.workflow_registry._workflows)

        # `as_posix` rather than `str` because `derive_registry_key` normalizes separators to
        # forward slashes, so on Windows the key is "C:/.../test_lib/example" and never the
        # backslashed spelling.
        assert registered == [(library_dir / "example").as_posix()]

    @pytest.mark.asyncio
    async def test_does_not_wait_on_a_closed_loading_gate(self, engine: Engine, tmp_path: Path) -> None:
        """A whole-set load registers each library's workflows while it holds the gate closed.

        Registering only parses the header, which is not gated. Route it back through the gated
        metadata handler and this hangs instead of registering.
        """
        library_manager = engine.library_manager
        (tmp_path / "example.py").write_text(_workflow_header(), encoding="utf-8")
        library_manager._close_libraries_loading_gate()

        try:
            with (
                patch(f"{LIBRARY_MANAGER_MODULE}.LibraryRegistry.get_library", return_value=_library(["example.py"])),
                patch.dict(engine.workflow_registry._workflows, {}, clear=True),
            ):
                await asyncio.wait_for(
                    library_manager.register_workflows_for_library(_library_info(tmp_path / "lib.json")), timeout=10
                )
                registered = [workflow.library_name for workflow in engine.workflow_registry._workflows.values()]
        finally:
            library_manager._libraries_loading_complete.set()

        assert registered == [LIBRARY_NAME]

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


class TestRegistryChangesAreAnnounced:
    """Clients showing the workflow list hear about a change once, and only when there was one."""

    @pytest.mark.asyncio
    async def test_registering_new_workflows_announces_once(self, engine: Engine, tmp_path: Path) -> None:
        (tmp_path / "first.py").write_text(_workflow_header("first"), encoding="utf-8")
        (tmp_path / "second.py").write_text(_workflow_header("second"), encoding="utf-8")
        put_event = MagicMock()

        with (
            patch.dict(engine.workflow_registry._workflows, {}, clear=True),
            patch.object(engine.event_manager, "put_event", put_event),
        ):
            await engine.workflow_manager.register_list_of_workflows(
                [str(tmp_path / "first.py"), str(tmp_path / "second.py")], library_name=LIBRARY_NAME
            )

        assert _registry_changes_announced(put_event) == 1

    @pytest.mark.asyncio
    async def test_registering_an_unchanged_set_says_nothing(self, engine: Engine, tmp_path: Path) -> None:
        """Re-registering an already-registered set is the normal case, not a change."""
        (tmp_path / "example.py").write_text(_workflow_header(), encoding="utf-8")
        workflow_files = [str(tmp_path / "example.py")]
        put_event = MagicMock()

        with patch.dict(engine.workflow_registry._workflows, {}, clear=True):
            await engine.workflow_manager.register_list_of_workflows(workflow_files, library_name=LIBRARY_NAME)
            with patch.object(engine.event_manager, "put_event", put_event):
                await engine.workflow_manager.register_list_of_workflows(workflow_files, library_name=LIBRARY_NAME)

        assert _registry_changes_announced(put_event) == 0

    @pytest.mark.asyncio
    async def test_a_workspace_rescan_announces(self, engine: Engine) -> None:
        put_event = MagicMock()

        with patch.object(engine.event_manager, "put_event", put_event):
            await engine.workflow_manager.refresh_workflow_registry(workflows_to_register=[])

        assert _registry_changes_announced(put_event) == 1


class TestRemoveLibraryWorkflows:
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

    def test_removes_the_library_workflows_and_announces_it(self, engine: Engine) -> None:
        workflow_manager = engine.workflow_manager
        event_manager = MagicMock()
        mine = _library_entry()
        theirs = MagicMock(library_name="OtherLib")
        users = MagicMock(library_name=None)

        with (
            patch.dict(
                engine.workflow_registry._workflows,
                {"lib/example": mine, "other/example": theirs, "user_workflow": users},
                clear=True,
            ),
            patch.object(engine, "_event_manager", event_manager),
        ):
            workflow_manager.remove_library_workflows(LIBRARY_NAME)

            assert sorted(engine.workflow_registry._workflows) == ["other/example", "user_workflow"]

        assert _registry_changes_announced(event_manager.put_event) == 1

    def test_says_nothing_for_a_library_that_contributed_none(self, engine: Engine) -> None:
        event_manager = MagicMock()

        with (
            patch.dict(
                engine.workflow_registry._workflows,
                {"user_workflow": MagicMock(library_name=None)},
                clear=True,
            ),
            patch.object(engine, "_event_manager", event_manager),
        ):
            engine.workflow_manager.remove_library_workflows(LIBRARY_NAME)

        assert _registry_changes_announced(event_manager.put_event) == 0

    def test_unloading_a_library_removes_its_workflows(self, engine: Engine) -> None:
        """Nothing else does: a workspace rescan deliberately spares library-owned entries.

        Without this, an install -> uninstall -> reinstall cycle piles up stale entries and an
        unloaded library keeps offering workflows for the life of the process.
        """
        with (
            patch(f"{LIBRARY_MANAGER_MODULE}.LibraryRegistry.unregister_library"),
            patch.dict(engine.workflow_registry._workflows, {"lib/example": _library_entry()}, clear=True),
        ):
            result = engine.library_manager.unload_library_from_registry_request(
                UnloadLibraryFromRegistryRequest(library_name=LIBRARY_NAME)
            )

            assert result.succeeded()
            assert "lib/example" not in engine.workflow_registry._workflows

    def test_forgets_the_verdicts_of_the_workflows_it_took_out(self, engine: Engine) -> None:
        """Their entries are gone, so nothing can ask about them and nothing would clean them up.

        The verdicts are kept per file, and the file paths only reach here through the removal: from a
        library name alone there is no way back to them.
        """
        workflow_manager = engine.workflow_manager
        info_key = workflow_manager._build_workflow_info_key("lib/example.py")
        verdict = WorkflowManager.WorkflowInfo(
            status=WorkflowManager.WorkflowStatus.GOOD, workflow_path="lib/example.py", workflow_name="example"
        )

        with (
            patch.dict(engine.workflow_registry._workflows, {"lib/example": _library_entry()}, clear=True),
            patch.dict(workflow_manager._workflow_file_path_to_info, {info_key: verdict}, clear=True),
        ):
            engine.workflow_manager.remove_library_workflows(LIBRARY_NAME)

            assert info_key not in workflow_manager._workflow_file_path_to_info

    def test_unloading_marks_every_verdict_stale(self, engine: Engine) -> None:
        """Workflows from other libraries may name this one, and recorded it as installed."""
        workflow_manager = engine.workflow_manager
        generation_before = workflow_manager._library_set_generation

        with patch(f"{LIBRARY_MANAGER_MODULE}.LibraryRegistry.unregister_library"):
            result = engine.library_manager.unload_library_from_registry_request(
                UnloadLibraryFromRegistryRequest(library_name=LIBRARY_NAME)
            )

        assert result.succeeded()
        assert workflow_manager._library_set_generation > generation_before


class TestRegisteringALibraryRegistersItsWorkflows:
    """Every library that newly arrives goes through one door, and that door registers.

    No caller has to know whether it is bringing in one library or one of a set.
    """

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

        `FLAWED` means some of the library's nodes failed to load and `NOT_EVALUATED` means node loading
        is deferred to a worker. Either way the library is registered and its templates belong in the
        picker: registering one parses the file's TOML header and never imports a node class. `UNUSABLE`
        is the one that gets no further.
        """
        library_manager = engine.library_manager
        library_info = _library_info(tmp_path / "lib.json")
        library_info.fitness = fitness
        register_one = AsyncMock(return_value=None)

        with (
            _stub_library_lifecycle(library_manager, library_info, register_one),
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

    @pytest.mark.parametrize(
        "progression_result",
        [None, RegisterLibraryFromFileResultFailure(result_details="failed partway")],
    )
    @pytest.mark.asyncio
    async def test_loading_marks_every_verdict_stale(
        self, engine: Engine, tmp_path: Path, progression_result: ResultPayload | None
    ) -> None:
        """Workflows judged before this library arrived may have recorded it as missing.

        Even a library that failed partway through may have reached the registry, so a failure counts.
        """
        library_manager = engine.library_manager
        library_info = _library_info(tmp_path / "lib.json")
        prerequisites = LibraryManager.RegisterLibraryPrerequisites(
            library_info=library_info, file_path=library_info.library_path
        )
        generation_before = engine.workflow_manager._library_set_generation

        with (
            patch.object(
                library_manager, "_establish_register_library_prerequisites", AsyncMock(return_value=prerequisites)
            ),
            patch.object(
                library_manager, "_progress_library_through_lifecycle", AsyncMock(return_value=progression_result)
            ),
            patch.object(library_manager, "register_workflows_for_library", AsyncMock(return_value=None)),
        ):
            await library_manager.register_library_from_file_request(
                RegisterLibraryFromFileRequest(file_path="/fake/lib.json")
            )

        assert engine.workflow_manager._library_set_generation > generation_before

    @pytest.mark.asyncio
    async def test_an_already_loaded_library_leaves_the_verdicts_alone(self, engine: Engine) -> None:
        """Nothing about the library set changed, so every cached verdict still holds."""
        library_manager = engine.library_manager
        already_loaded = RegisterLibraryFromFileResultSuccess(
            library_name=LIBRARY_NAME, was_already_loaded=True, result_details="already loaded"
        )
        generation_before = engine.workflow_manager._library_set_generation

        with patch.object(
            library_manager, "_establish_register_library_prerequisites", AsyncMock(return_value=already_loaded)
        ):
            await library_manager.register_library_from_file_request(
                RegisterLibraryFromFileRequest(file_path="/fake/lib.json")
            )

        assert engine.workflow_manager._library_set_generation == generation_before


class TestEachMidSessionArrivalRegistersItsWorkflows:
    """A library arriving on its own registers its own workflows, and gets there via the handler.

    Two handlers bring one library in mid-session and both dispatch `RegisterLibraryFromFileRequest`,
    so neither registers anything itself.

    These tests run the real handler behind the mocked dispatch: asserting only that some request
    went out would pass just as happily if it never reached the registration.
    """

    def _register_through_the_real_handler(
        self, library_manager: LibraryManager, library_info: LibraryManager.LibraryInfo, register_one: AsyncMock
    ) -> Callable[[object], Awaitable[object]]:
        """Build a dispatch side effect that routes registrations to the real handler.

        The lifecycle work below the handler is stubbed out -- there is no library on disk here --
        leaving the part under test: whether reaching this handler registers the workflows.
        `register_one` stands in for `register_workflows_for_library`.
        """

        async def dispatch(request: object) -> object:
            if isinstance(request, RegisterLibraryFromFileRequest):
                with _stub_library_lifecycle(library_manager, library_info, register_one):
                    return await library_manager.register_library_from_file_request(request)
            if isinstance(request, UnloadLibraryFromRegistryRequest):
                return UnloadLibraryFromRegistryResultSuccess(result_details="unloaded")
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
        dispatch = self._register_through_the_real_handler(library_manager, _library_info(library_json), register_one)

        with (
            patch(f"{LIBRARY_MANAGER_MODULE}.find_file_in_directory", return_value=library_json),
            patch.object(engine, "ahandle_request", AsyncMock(side_effect=dispatch)),
            patch.object(
                engine,
                "handle_request",
                MagicMock(return_value=UnloadLibraryFromRegistryResultSuccess(result_details="unloaded")),
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
        dispatch = self._register_through_the_real_handler(library_manager, _library_info(library_json), register_one)

        downloaded = AsyncMock()
        downloaded.mkdir = AsyncMock(return_value=None)
        # exists() -> True takes the skip_clone path, so no git clone runs.
        downloaded.exists = AsyncMock(return_value=True)
        downloaded.read_text = AsyncMock(return_value=json.dumps({"name": LIBRARY_NAME}))

        with (
            patch(f"{LIBRARY_MANAGER_MODULE}.anyio.Path", return_value=downloaded),
            patch(f"{LIBRARY_MANAGER_MODULE}.find_file_in_directory", return_value=str(library_json)),
            patch.object(engine, "ahandle_request", AsyncMock(side_effect=dispatch)),
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

    @pytest.mark.asyncio
    async def test_an_arrival_mid_load_registers_without_hanging_the_load(self, engine: Engine, tmp_path: Path) -> None:
        """An arrival nothing asked for, run for real against a closed gate.

        A declared library dependency that is missing from disk is downloaded and registered from inside
        another library's lifecycle, so it reaches this handler even when that lifecycle is running
        inside a whole-set load.
        """
        library_manager = engine.library_manager
        library_json = tmp_path / "griptape_nodes_library.json"
        (tmp_path / "example.py").write_text(_workflow_header(), encoding="utf-8")
        library_info = _library_info(library_json)
        library_manager._close_libraries_loading_gate()

        try:
            with (
                _stub_library_lifecycle(library_manager, library_info),
                patch.dict(
                    library_manager._library_file_path_to_info, {library_info.library_path: library_info}, clear=True
                ),
                patch(f"{LIBRARY_MANAGER_MODULE}.LibraryRegistry.get_library", return_value=_library(["example.py"])),
                patch.dict(engine.workflow_registry._workflows, {}, clear=True),
            ):
                result = await asyncio.wait_for(
                    library_manager.register_library_from_file_request(
                        RegisterLibraryFromFileRequest(file_path=str(library_json))
                    ),
                    timeout=10,
                )
                registered = [workflow.library_name for workflow in engine.workflow_registry._workflows.values()]
        finally:
            library_manager._libraries_loading_complete.set()

        assert result.succeeded(), result.result_details
        assert registered == [LIBRARY_NAME]


class TestLibraryWorkflowsSurviveAWorkspaceRescan:
    """End to end against the real WorkflowRegistry, no registration mocks."""

    @pytest.mark.asyncio
    async def test_a_workflow_without_the_griptape_provided_flag_survives_a_rescan(
        self, engine: Engine, tmp_path: Path
    ) -> None:
        """Through the real `refresh_workflow_registry`, with the real clear at the top of it.

        The workflow's header sets `is_template` but not `is_griptape_provided`, so nothing in the file
        spares it. It survives because the registry knows the library contributed it: a workflow follows
        the library that ships it rather than a flag its author may have omitted.
        """
        library_manager = engine.library_manager
        workflow_manager = engine.workflow_manager
        config_manager = engine.config_manager
        workspace = tmp_path / "workspace"
        library_dir = workspace / "libraries" / "test_lib"
        library_dir.mkdir(parents=True)
        (library_dir / "example.py").write_text(_workflow_header(), encoding="utf-8")
        library_info = _library_info(library_dir / "griptape_nodes_library.json")
        registry_key = (library_dir / "example").as_posix()

        with (
            patch(f"{LIBRARY_MANAGER_MODULE}.LibraryRegistry.get_library", return_value=_library(["example.py"])),
            patch.dict(library_manager._library_file_path_to_info, {library_info.library_path: library_info}),
            patch.dict(engine.workflow_registry._workflows, {}, clear=True),
            patch.object(type(config_manager), "workspace_path", workspace),
        ):
            await library_manager.register_workflows_for_library(library_info)

            registered = engine.workflow_registry.get_workflow_by_name(registry_key)
            assert registered.metadata.is_griptape_provided is False
            assert registered.library_name == LIBRARY_NAME

            # An empty list skips the workspace scan; the clear is the part under test.
            await workflow_manager.refresh_workflow_registry(workflows_to_register=[])

            assert list(engine.workflow_registry._workflows) == [registry_key]

    @pytest.mark.asyncio
    async def test_the_scan_skips_installed_library_roots_but_still_walks_sandbox_ones(
        self, engine: Engine, tmp_path: Path
    ) -> None:
        """Libraries live under the workspace by default, so the scan walks straight into them.

        Claiming an installed library's files would register them a second time as the workspace's, and
        that copy would outlive the library. Sandbox libraries are the deliberate exception: they are
        the directory an author is actively editing, so their workflows have to appear without waiting
        on a library reload.
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
            patch.dict(engine.workflow_registry._workflows, {}, clear=True),
            patch.object(type(config_manager), "workspace_path", workspace),
        ):
            await workflow_manager.refresh_workflow_registry(workflows_to_register=[str(workspace)])
            registered = sorted(engine.workflow_registry._workflows)

        assert registered == ["libraries/sandbox_lib/in_development"]

    @pytest.mark.asyncio
    async def test_a_library_registering_a_directory_still_claims_its_own_files(
        self, engine: Engine, tmp_path: Path
    ) -> None:
        """Skipping library roots is the workspace scan's business, not every caller's.

        A library hands over explicit file paths today, and those never reach the directory walk that
        consults the exclusion roots. Handing over its own directory is the case that would: without the
        check on the contributing library it would exclude its own files and register nothing.
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
            patch.dict(engine.workflow_registry._workflows, {}, clear=True),
            patch.object(type(config_manager), "workspace_path", workspace),
        ):
            await workflow_manager._process_workflows_for_registration([str(library_dir)], library_name=LIBRARY_NAME)
            registered = list(engine.workflow_registry._workflows)

        assert registered == [(library_dir / "example").as_posix()]
