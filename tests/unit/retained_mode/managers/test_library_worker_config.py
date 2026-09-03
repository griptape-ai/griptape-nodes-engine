"""Tests for library worker configuration."""

from __future__ import annotations

import asyncio
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from griptape_nodes.node_library.library_declarations import LibraryDependencyDeclaration
from griptape_nodes.node_library.library_registry import Dependencies, LibraryMetadata
from griptape_nodes.retained_mode.events.app_events import LibraryLoadedNotification
from griptape_nodes.retained_mode.managers.fitness_problems.libraries import (
    IncompatibleRequirementsProblem,
)
from griptape_nodes.retained_mode.managers.library_manager import LibraryManager


def _make_metadata(**kwargs: Any) -> LibraryMetadata:
    return LibraryMetadata(
        author="test",
        description="test library",
        library_version="1.0.0",
        engine_version="1.0.0",
        tags=[],
        **kwargs,
    )


def _make_library_manager() -> LibraryManager:
    return LibraryManager(event_manager=MagicMock(), worker_manager=MagicMock())


class TestLibraryInfoRequiresWorker:
    def test_defaults_to_false(self) -> None:
        info = LibraryManager.LibraryInfo(
            lifecycle_state=LibraryManager.LibraryLifecycleState.DISCOVERED,
            fitness=LibraryManager.LibraryFitness.NOT_EVALUATED,
            library_path="/some/path.json",
            is_sandbox=False,
        )

        assert info.requires_worker is False

    def test_can_be_set_true(self) -> None:
        info = LibraryManager.LibraryInfo(
            lifecycle_state=LibraryManager.LibraryLifecycleState.LOADED,
            fitness=LibraryManager.LibraryFitness.GOOD,
            library_path="/some/path.json",
            is_sandbox=False,
            requires_worker=True,
        )

        assert info.requires_worker is True


class TestGetWorkerForLibrary:
    def test_returns_none_for_none_library_name(self) -> None:
        mgr = _make_library_manager()

        result = mgr.get_worker_for_library(None)

        assert result is None

    def test_returns_worker_when_registered(self) -> None:
        mgr = _make_library_manager()
        worker_engine_id = "eng-xyz"
        worker_request_topic = "sessions/s/workers/eng-xyz/request"
        lib_info = LibraryManager.LibraryInfo(
            lifecycle_state=LibraryManager.LibraryLifecycleState.LOADED,
            fitness=LibraryManager.LibraryFitness.GOOD,
            library_path="/some/path.json",
            is_sandbox=False,
            library_name="my_lib",
            requires_worker=True,
            executes_in_worker=True,
        )

        cast("MagicMock", mgr._worker_manager).get_worker_for_key.return_value = (
            worker_engine_id,
            worker_request_topic,
        )
        mgr._library_file_path_to_info["/some/path.json"] = lib_info
        result = mgr.get_worker_for_library("my_lib")

        assert result == (worker_engine_id, worker_request_topic)

    def test_returns_none_when_no_worker_and_not_required(self) -> None:
        mgr = _make_library_manager()
        lib_info = LibraryManager.LibraryInfo(
            lifecycle_state=LibraryManager.LibraryLifecycleState.LOADED,
            fitness=LibraryManager.LibraryFitness.GOOD,
            library_path="/some/path.json",
            is_sandbox=False,
            library_name="my_lib",
            requires_worker=False,
        )

        cast("MagicMock", mgr._worker_manager).get_worker_for_key.return_value = None
        mgr._library_file_path_to_info["/some/path.json"] = lib_info
        result = mgr.get_worker_for_library("my_lib")

        assert result is None

    def test_raises_when_library_requires_worker_but_none_registered(self) -> None:
        mgr = _make_library_manager()
        lib_info = LibraryManager.LibraryInfo(
            lifecycle_state=LibraryManager.LibraryLifecycleState.LOADED,
            fitness=LibraryManager.LibraryFitness.GOOD,
            library_path="/some/path.json",
            is_sandbox=False,
            library_name="my_lib",
            requires_worker=True,
            executes_in_worker=True,
        )

        cast("MagicMock", mgr._worker_manager).get_worker_for_key.return_value = None
        mgr._library_file_path_to_info["/some/path.json"] = lib_info

        with pytest.raises(RuntimeError, match="requires a dedicated worker"):
            mgr.get_worker_for_library("my_lib")


class TestOnLibraryLoadedNotification:
    def _make_lib_info(self, library_name: str) -> LibraryManager.LibraryInfo:
        """A legacy worker-mode library awaiting its worker's verdict.

        `requires_worker` is what puts a library in WORKER_PENDING in the first place (see
        `_start_workers`), so a fixture in that state without the flag describes something the
        engine never produces.
        """
        return LibraryManager.LibraryInfo(
            lifecycle_state=LibraryManager.LibraryLifecycleState.WORKER_PENDING,
            fitness=LibraryManager.LibraryFitness.NOT_EVALUATED,
            library_path="/some/path.json",
            is_sandbox=False,
            library_name=library_name,
            requires_worker=True,
            executes_in_worker=True,
        )

    def _make_exec_deps_lib_info(self, library_name: str) -> LibraryManager.LibraryInfo:
        """An execution-dependency library that already loaded its real nodes locally."""
        return LibraryManager.LibraryInfo(
            lifecycle_state=LibraryManager.LibraryLifecycleState.LOADED,
            fitness=LibraryManager.LibraryFitness.FLAWED,
            library_path="/some/exec-deps.json",
            is_sandbox=False,
            library_name=library_name,
            requires_worker=False,
            executes_in_worker=True,
        )

    @pytest.mark.asyncio
    @pytest.mark.asyncio
    async def test_updates_fitness_and_lifecycle_to_loaded(self) -> None:
        mgr = _make_library_manager()
        lib_info = self._make_lib_info("my_lib")
        mgr._library_file_path_to_info["/some/path.json"] = lib_info

        await mgr._on_library_loaded_notification(LibraryLoadedNotification(library_name="my_lib", fitness="GOOD"))

        assert lib_info.lifecycle_state == LibraryManager.LibraryLifecycleState.LOADED
        assert lib_info.fitness == LibraryManager.LibraryFitness.GOOD

    @pytest.mark.asyncio
    @pytest.mark.asyncio
    async def test_a_worker_does_not_overwrite_a_locally_derived_fitness(self) -> None:
        """An exec-deps library's fitness is the orchestrator's own finding, not the worker's.

        It loaded real node classes here, so its FLAWED verdict came from doing that -- an
        edit-time dependency that failed, a node module that would not import. The worker only
        knows whether ITS copy came up. Taking the worker's answer would paint over a broken node
        that is sitting on the canvas, and the reason would not travel with it.
        """
        mgr = _make_library_manager()
        lib_info = self._make_exec_deps_lib_info("exec_deps_lib")
        mgr._library_file_path_to_info["/some/exec-deps.json"] = lib_info

        await mgr._on_library_loaded_notification(
            LibraryLoadedNotification(library_name="exec_deps_lib", fitness="GOOD")
        )

        assert lib_info.fitness == LibraryManager.LibraryFitness.FLAWED
        assert lib_info.lifecycle_state == LibraryManager.LibraryLifecycleState.LOADED

    @pytest.mark.asyncio
    @pytest.mark.asyncio
    async def test_accepts_flawed_fitness(self) -> None:
        mgr = _make_library_manager()
        lib_info = self._make_lib_info("my_lib")
        mgr._library_file_path_to_info["/some/path.json"] = lib_info

        await mgr._on_library_loaded_notification(
            LibraryLoadedNotification(library_name="my_lib", fitness="FLAWED", problem_details="some issue")
        )

        assert lib_info.lifecycle_state == LibraryManager.LibraryLifecycleState.LOADED
        assert lib_info.fitness == LibraryManager.LibraryFitness.FLAWED

    @pytest.mark.asyncio
    @pytest.mark.asyncio
    async def test_does_nothing_for_unknown_library(self) -> None:
        mgr = _make_library_manager()

        await mgr._on_library_loaded_notification(LibraryLoadedNotification(library_name="unknown_lib", fitness="GOOD"))


class TestRegisterPreReloadCallback:
    def test_callback_is_appended(self) -> None:
        mgr = _make_library_manager()
        callback = MagicMock()

        mgr.register_pre_reload_callback(callback)

        assert callback in mgr._pre_reload_callbacks

    def test_multiple_callbacks_registered_in_order(self) -> None:
        mgr = _make_library_manager()
        baseline = list(mgr._pre_reload_callbacks)
        first, second = MagicMock(), MagicMock()

        mgr.register_pre_reload_callback(first)
        mgr.register_pre_reload_callback(second)

        assert mgr._pre_reload_callbacks == [*baseline, first, second]


class TestResolveExecutesInWorker:
    """Execution placement is a fact about dependencies, derived not declared."""

    def _metadata(self, *, exec_deps: list[str] | None) -> LibraryMetadata:
        dependencies = None
        if exec_deps is not None:
            dependencies = Dependencies(pip_dependencies=["pillow"], pip_dependencies_exec=exec_deps)
        return LibraryMetadata(
            author="t",
            description="d",
            library_version="1.0.0",
            engine_version="0.0.0",
            tags=[],
            dependencies=dependencies,
        )

    def test_exec_dependencies_require_a_worker(self) -> None:
        result = LibraryManager._resolve_executes_in_worker(
            requires_worker=False, metadata=self._metadata(exec_deps=["torch"])
        )
        assert result is True

    def test_no_dependencies_section_means_no_worker(self) -> None:
        result = LibraryManager._resolve_executes_in_worker(
            requires_worker=False, metadata=self._metadata(exec_deps=None)
        )
        assert result is False

    def test_empty_exec_dependencies_mean_no_worker(self) -> None:
        result = LibraryManager._resolve_executes_in_worker(
            requires_worker=False, metadata=self._metadata(exec_deps=[])
        )
        assert result is False

    def test_legacy_worker_mode_still_executes_in_a_worker(self) -> None:
        """The two reasons are independent: declaring worker mode is sufficient alone."""
        result = LibraryManager._resolve_executes_in_worker(
            requires_worker=True, metadata=self._metadata(exec_deps=None)
        )
        assert result is True


class TestExecuteWaitsForTheWorkerLibraryLoad:
    """Routing must gate on the worker's LibraryLoadedNotification, not on registration.

    A worker registers at process start, BEFORE it has loaded its library -- with eager
    execution-module imports, seconds before. A node routed in that window failed in the
    worker with "Library 'X' not found". The old worker system never had this window:
    stub nodes did not exist until the worker confirmed the load, so nothing could execute.
    """

    def _spawned_info(self, *, loaded: bool) -> LibraryManager.LibraryInfo:
        info = LibraryManager.LibraryInfo(
            lifecycle_state=LibraryManager.LibraryLifecycleState.LOADED,
            fitness=LibraryManager.LibraryFitness.GOOD,
            library_path="/some/path.json",
            is_sandbox=False,
            library_name="Lib",
            executes_in_worker=True,
        )
        info.worker_ready = asyncio.Event()
        if loaded:
            info.worker_ready.set()
        return info

    @pytest.mark.asyncio
    async def test_the_wait_releases_when_the_notification_arrives(self) -> None:
        mgr = _make_library_manager()
        info = self._spawned_info(loaded=False)
        mgr._library_file_path_to_info["/some/path.json"] = info
        order: list[str] = []

        async def executes() -> None:
            await mgr.wait_for_worker_library_load("Lib")
            order.append("routed")

        async def worker_finishes_loading() -> None:
            order.append("loaded")
            await mgr._on_library_loaded_notification(LibraryLoadedNotification(library_name="Lib", fitness="GOOD"))

        await asyncio.gather(executes(), worker_finishes_loading())

        assert order == ["loaded", "routed"], "execution routed before the worker reported the library loaded"

    @pytest.mark.asyncio
    async def test_an_already_loaded_library_does_not_wait(self) -> None:
        mgr = _make_library_manager()
        mgr._library_file_path_to_info["/some/path.json"] = self._spawned_info(loaded=True)

        await mgr.wait_for_worker_library_load("Lib")

    @pytest.mark.asyncio
    async def test_a_library_with_no_spawned_worker_does_not_wait(self) -> None:
        mgr = _make_library_manager()
        info = self._spawned_info(loaded=False)
        info.worker_ready = None
        mgr._library_file_path_to_info["/some/path.json"] = info

        await mgr.wait_for_worker_library_load("Lib")

    @pytest.mark.asyncio
    async def test_the_timeout_names_the_library_and_the_ceiling(self) -> None:
        mgr = _make_library_manager()
        mgr._library_file_path_to_info["/some/path.json"] = self._spawned_info(loaded=False)
        mgr._engine = MagicMock()  # type: ignore[assignment]
        mgr._engine.config_manager.get_config_value.return_value = 0.01

        with pytest.raises(RuntimeError, match="Lib"):
            await mgr.wait_for_worker_library_load("Lib")

    @pytest.mark.asyncio
    async def test_an_eviction_during_the_wait_releases_it(self) -> None:
        """The waiter must not hold on for the full grace for a worker that is already gone."""
        mgr = _make_library_manager()
        info = self._spawned_info(loaded=False)
        mgr._library_file_path_to_info["/some/path.json"] = info
        order: list[str] = []

        async def executes() -> None:
            await mgr.wait_for_worker_library_load("Lib")
            order.append("released")

        async def worker_dies() -> None:
            order.append("evicted")
            mgr.on_worker_evicted("worker-1", "Lib")

        await asyncio.gather(executes(), worker_dies())

        assert order == ["evicted", "released"]
        assert info.lifecycle_state is LibraryManager.LibraryLifecycleState.LOADED, (
            "an exec-deps library must stay editable through an eviction"
        )


class TestOnWorkerEvicted:
    """What an evicted worker leaves behind, per library kind.

    An exec-dependencies library loaded real node classes on the orchestrator before its worker
    ever spawned, so losing the worker must cost EXECUTION and nothing else: still LOADED, still
    editable, with a reason the next run can report. A legacy worker-mode library has only stubs
    until its worker confirms, so losing the worker is a load failure. Neither was covered, and
    the difference is the whole point of the split.
    """

    def _info(self, *, requires_worker: bool, lifecycle: LibraryManager.LibraryLifecycleState) -> Any:
        return LibraryManager.LibraryInfo(
            lifecycle_state=lifecycle,
            fitness=LibraryManager.LibraryFitness.GOOD,
            library_path="/some/path.json",
            is_sandbox=False,
            library_name="Lib",
            requires_worker=requires_worker,
            executes_in_worker=True,
        )

    def test_exec_deps_library_stays_loaded_and_records_why(self) -> None:
        manager = _make_library_manager()
        info = self._info(requires_worker=False, lifecycle=LibraryManager.LibraryLifecycleState.LOADED)
        manager._library_file_path_to_info["/some/path.json"] = info

        manager.on_worker_evicted("worker-1", "Lib")

        assert info.lifecycle_state is LibraryManager.LibraryLifecycleState.LOADED
        assert info.fitness is LibraryManager.LibraryFitness.GOOD
        assert info.execution_unavailable_reason is not None
        assert "stopped responding" in info.execution_unavailable_reason

    def test_legacy_worker_pending_library_becomes_failure(self) -> None:
        manager = _make_library_manager()
        info = self._info(requires_worker=True, lifecycle=LibraryManager.LibraryLifecycleState.WORKER_PENDING)
        manager._library_file_path_to_info["/some/path.json"] = info

        manager.on_worker_evicted("worker-1", "Lib")

        assert info.lifecycle_state is LibraryManager.LibraryLifecycleState.FAILURE
        assert info.fitness is LibraryManager.LibraryFitness.UNUSABLE

    def test_unknown_library_name_is_a_no_op(self) -> None:
        manager = _make_library_manager()
        manager.on_worker_evicted("worker-1", "Never Registered")
        manager.on_worker_evicted("worker-1", None)


class TestSpawnSkipForUnmetRequirements:
    """A pointless spawn is skipped -- but only where the nodes already exist locally.

    An exec-dependencies library loaded real node classes on the orchestrator, so a worker it can
    never use costs a whole execution environment -- torch, gigabytes -- for nothing. A legacy
    worker-mode library is the opposite: the orchestrator skips its node modules entirely and its
    classes arrive as stubs from the worker, so skipping the spawn would leave it with no node
    types at all.
    """

    def _manager(self, *, requires_worker: bool, unmet: bool) -> LibraryManager:
        manager = _make_library_manager()
        manager._engine = MagicMock()  # type: ignore[assignment]
        manager._engine.ahandle_request = AsyncMock()  # type: ignore[union-attr]
        info = LibraryManager.LibraryInfo(
            lifecycle_state=LibraryManager.LibraryLifecycleState.LOADED,
            fitness=LibraryManager.LibraryFitness.GOOD,
            library_path="/some/path.json",
            is_sandbox=False,
            library_name="Lib",
            requires_worker=requires_worker,
            executes_in_worker=True,
        )
        if unmet:
            info.problems = [
                IncompatibleRequirementsProblem(
                    requirements={"compute": (["cuda"], "has_any")},
                    system_capabilities={"compute": ["cpu"]},
                )
            ]
            info.execution_unavailable_reason = "it needs compute cuda, and this machine has cpu."
        manager._library_file_path_to_info["/some/path.json"] = info
        return manager

    @pytest.mark.asyncio
    async def test_exec_deps_library_with_unmet_requirements_is_not_spawned(self) -> None:
        manager = self._manager(requires_worker=False, unmet=True)

        await manager._start_workers()

        cast("MagicMock", manager._engine).ahandle_request.assert_not_awaited()
        info = manager._library_file_path_to_info["/some/path.json"]
        assert info.execution_unavailable_reason is not None, "the local refusal must survive"

    @pytest.mark.asyncio
    async def test_legacy_worker_library_still_spawns_even_when_unmet(self) -> None:
        """Its nodes come from the worker, so no spawn means no node types at all."""
        manager = self._manager(requires_worker=True, unmet=True)

        await manager._start_workers()

        cast("MagicMock", manager._engine).ahandle_request.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_the_spawn_a_legacy_library_still_gets_does_not_clear_its_refusal(self) -> None:
        """execution_unavailable_reason is the only gate get_worker_for_library has.

        This library's spawn is deliberately not skipped, so clearing the reason on a fresh attempt
        would let execution dispatch to a worker that cannot load it. An unmet requirement is a
        standing fact about the machine, not an account of a previous attempt.
        """
        manager = self._manager(requires_worker=True, unmet=True)

        await manager._start_workers()

        info = manager._library_file_path_to_info["/some/path.json"]
        assert info.execution_unavailable_reason is not None

    @pytest.mark.asyncio
    async def test_a_library_with_met_requirements_spawns(self) -> None:
        manager = self._manager(requires_worker=False, unmet=False)

        await manager._start_workers()

        cast("MagicMock", manager._engine).ahandle_request.assert_awaited_once()
        # Nothing standing in the way, so a stale account of a previous attempt is cleared.
        assert manager._library_file_path_to_info["/some/path.json"].execution_unavailable_reason is None


class TestLibraryDependencyResolution:
    """A dependency declaration names a REPO; the registry is keyed by library NAME.

    `griptape-nodes-library-openexr` publishes itself as `OpenEXR Library`, so matching the repo
    name against library names missed it -- and a miss only warns and skips, so the whole
    library-dependency mechanism was a silent no-op for any library not named after its repo.
    Provisioning installs each download under a repo-name directory, which is where the repo name
    actually appears.
    """

    def _register(self, manager: LibraryManager, *, path: str, library_name: str) -> None:
        manager._library_file_path_to_info[path] = LibraryManager.LibraryInfo(
            lifecycle_state=LibraryManager.LibraryLifecycleState.LOADED,
            fitness=LibraryManager.LibraryFitness.GOOD,
            library_path=path,
            is_sandbox=False,
            library_name=library_name,
        )

    def test_repo_name_resolves_via_the_install_directory(self) -> None:
        manager = _make_library_manager()
        self._register(
            manager,
            path="/libs/griptape-nodes-library-openexr/griptape-nodes-library.json",
            library_name="OpenEXR Library",
        )

        info = manager._library_info_for_repo_name("griptape-nodes-library-openexr")

        assert info is not None
        assert info.library_name == "OpenEXR Library"

    def test_library_name_still_resolves_when_it_matches_the_repo(self) -> None:
        manager = _make_library_manager()
        self._register(manager, path="/libs/whatever/griptape-nodes-library.json", library_name="some-repo-name")

        info = manager._library_info_for_repo_name("some-repo-name")

        assert info is not None

    def test_unknown_repo_name_resolves_to_none(self) -> None:
        manager = _make_library_manager()
        self._register(manager, path="/libs/other/griptape-nodes-library.json", library_name="Other Library")

        assert manager._library_info_for_repo_name("griptape-nodes-library-openexr") is None


class TestExpandTargetsWithLibraryDependencies:
    """A worker must load the libraries its library declares, or a feature works only while editing.

    CorridorKey's OCIO path reaches into the OpenEXR library. The orchestrator loaded it, the
    worker did not, so selecting an OCIO colour space succeeded on the canvas and failed on run.
    The declaration names a REPO (`griptape-nodes-library-openexr`) while the registry is keyed by
    library NAME (`OpenEXR Library`), which is why resolution has to go through the install path.
    """

    def _manager_with(self, monkeypatch: pytest.MonkeyPatch, libraries: dict[str, Any]) -> LibraryManager:
        """A manager whose discovery found `libraries`: {library_name: (path, declarations)}."""
        manager = _make_library_manager()
        by_path: dict[str, Any] = {}
        for name, (path, declarations) in libraries.items():
            manager._library_file_path_to_info[path] = LibraryManager.LibraryInfo(
                lifecycle_state=LibraryManager.LibraryLifecycleState.DISCOVERED,
                fitness=LibraryManager.LibraryFitness.NOT_EVALUATED,
                library_path=path,
                is_sandbox=False,
                library_name=name,
            )
            by_path[path] = declarations

        def fake_load(request: Any) -> Any:
            schema = MagicMock()
            schema.metadata = _make_metadata(declarations=by_path[request.file_path])
            result = MagicMock()
            result.library_schema = schema
            return result

        monkeypatch.setattr(manager, "load_library_metadata_from_file_request", fake_load)
        return manager

    def test_declared_dependency_reaches_the_worker(self, monkeypatch: pytest.MonkeyPatch) -> None:
        manager = self._manager_with(
            monkeypatch,
            {
                "Consumer Library": (
                    "/libs/consumer/griptape-nodes-library.json",
                    [LibraryDependencyDeclaration(url="https://github.com/o/griptape-nodes-library-openexr.git")],
                ),
                "OpenEXR Library": ("/libs/griptape-nodes-library-openexr/griptape-nodes-library.json", []),
            },
        )

        expanded = manager._expand_targets_with_library_dependencies(["Consumer Library"])

        assert expanded == ["Consumer Library", "OpenEXR Library"]

    def test_dependencies_are_followed_transitively(self, monkeypatch: pytest.MonkeyPatch) -> None:
        manager = self._manager_with(
            monkeypatch,
            {
                "A": (
                    "/libs/a/griptape-nodes-library.json",
                    [LibraryDependencyDeclaration(url="https://github.com/o/griptape-nodes-library-b.git")],
                ),
                "B Library": (
                    "/libs/griptape-nodes-library-b/griptape-nodes-library.json",
                    [LibraryDependencyDeclaration(url="https://github.com/o/griptape-nodes-library-c.git")],
                ),
                "C Library": ("/libs/griptape-nodes-library-c/griptape-nodes-library.json", []),
            },
        )

        assert manager._expand_targets_with_library_dependencies(["A"]) == ["A", "B Library", "C Library"]

    def test_a_library_with_no_declarations_gains_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Over-broad expansion would put every library in every worker, undoing the isolation."""
        manager = self._manager_with(
            monkeypatch,
            {
                "Solo Library": ("/libs/solo/griptape-nodes-library.json", []),
                "Unrelated Library": ("/libs/griptape-nodes-library-unrelated/griptape-nodes-library.json", []),
            },
        )

        assert manager._expand_targets_with_library_dependencies(["Solo Library"]) == ["Solo Library"]

    def test_an_uninstalled_dependency_is_skipped_not_fatal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Declarations are optional in practice; refusing to start would be the worse failure."""
        manager = self._manager_with(
            monkeypatch,
            {
                "Consumer Library": (
                    "/libs/consumer/griptape-nodes-library.json",
                    [LibraryDependencyDeclaration(url="https://github.com/o/griptape-nodes-library-absent.git")],
                ),
            },
        )

        assert manager._expand_targets_with_library_dependencies(["Consumer Library"]) == ["Consumer Library"]

    @pytest.mark.asyncio
    async def test_the_worker_load_path_applies_the_expansion(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Guards the call site, not just the method.

        The expansion is only useful if the worker's load path runs it. Testing the method alone
        left removing the call invisible, so this pins that load_all_libraries_from_config feeds
        its target list through it -- and that an orchestrator (no target list) is left alone.
        """
        manager = _make_library_manager()
        seen: list[list[str] | None] = []

        def fake_expand(targets: list[str]) -> list[str]:
            seen.append(targets)
            return [*targets, "Pulled In Library"]

        monkeypatch.setattr(manager, "_expand_targets_with_library_dependencies", fake_expand)
        monkeypatch.setattr(manager, "_reconcile_libraries_from_config", AsyncMock(return_value=[]))
        # Discovery returning nothing ends the load early, which is all this test needs: the
        # expansion runs before any library is touched.
        monkeypatch.setattr(
            manager, "discover_libraries_request", AsyncMock(return_value=MagicMock(libraries_discovered=[]))
        )

        await manager.load_all_libraries_from_config(target_library_names=["Worker Library"])
        assert seen == [["Worker Library"]], "the worker load path did not expand its target list"

        seen.clear()
        await manager.load_all_libraries_from_config(target_library_names=None)
        assert seen == [], "the orchestrator has no target list and must not be expanded"
