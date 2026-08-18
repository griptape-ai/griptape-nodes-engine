"""Tests for the Engine object graph and how the current engine is resolved.

These pin the guarantees the refactor away from a process-wide singleton was for: engines
are independent, managers are wired to the engine that built them, and the current engine
can be rebound for a scope without leaking into the rest of the process.
"""

import asyncio
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
import static_ffmpeg.run
from xdg_base_dirs import xdg_data_home

from griptape_nodes.retained_mode.engine import (
    Engine,
    EngineScoped,
    current_engine,
    engine_scope,
    has_current_engine,
    reset_root_engine,
)
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes


def _engine_scoped_children(path: str, value: object) -> Iterator[tuple[str, EngineScoped]]:
    """Yield the engine-scoped objects held directly by one attribute value."""
    if isinstance(value, EngineScoped):
        yield path, value
    elif isinstance(value, (list, tuple, set)):
        for index, item in enumerate(value):
            if isinstance(item, EngineScoped):
                yield f"{path}[{index}]", item
    elif isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, EngineScoped):
                yield f"{path}[{key!r}]", item


def _unbound_engine_scoped_descendants(root: EngineScoped, engine: Engine, path: str) -> list[str]:
    """Report every engine-scoped object reachable from `root` that is not bound to `engine`."""
    unbound: list[str] = []
    visited: set[int] = set()
    pending: list[tuple[str, EngineScoped]] = [(path, root)]

    while pending:
        owner_path, owner = pending.pop()
        if id(owner) in visited:
            continue
        visited.add(id(owner))

        for attribute_name, value in vars(owner).items():
            for child_path, child in _engine_scoped_children(f"{owner_path}.{attribute_name}", value):
                if child._engine is not engine:
                    unbound.append(f"{child_path} ({type(child).__name__})")
                pending.append((child_path, child))

    return unbound


class TestEngineIndependence:
    def test_engines_do_not_share_managers(self) -> None:
        first = Engine()
        second = Engine()

        assert first.object_manager is not second.object_manager
        assert first.event_manager is not second.event_manager
        assert first.flow_manager is not second.flow_manager

    def test_managers_are_wired_to_their_own_engine(self) -> None:
        first = Engine()
        second = Engine()

        assert first.flow_manager.engine is first
        assert second.flow_manager.engine is second

    def test_every_engine_scoped_manager_is_bound(self) -> None:
        """No manager should be left resolving the engine through the module-level fallback."""
        engine = Engine()

        unbound = [
            name
            for name in dir(Engine)
            if name.endswith("_manager")
            and isinstance(manager := getattr(engine, name), EngineScoped)
            and manager._engine is not engine
        ]

        assert unbound == []

    def test_every_engine_scoped_object_owned_by_a_manager_is_bound(self) -> None:
        """Managers must inject their engine into the engine-scoped objects they build.

        The manager-level check stops at `Engine`'s own attributes, so a registry or helper
        constructed bare inside a manager's `__init__` still falls back to `current_engine()`
        and runs its whole subtree on whatever engine happens to be ambient at call time.
        """
        engine = Engine()
        unbound: list[str] = []

        for attribute_name in dir(Engine):
            if not attribute_name.endswith("_manager"):
                continue

            manager = getattr(engine, attribute_name)
            if not isinstance(manager, EngineScoped):
                continue

            unbound.extend(_unbound_engine_scoped_descendants(manager, engine, attribute_name))

        assert unbound == []

    def test_object_registered_in_one_engine_is_invisible_to_the_other(self) -> None:
        first = Engine()
        second = Engine()

        first.object_manager.add_object_by_name("shared_name", object())

        assert first.object_manager.has_object_with_name("shared_name")
        assert not second.object_manager.has_object_with_name("shared_name")


class TestEngineScopedFallback:
    def test_manager_built_without_an_engine_falls_back_to_the_current_engine(self) -> None:
        scoped = EngineScoped()

        assert scoped.engine is current_engine()

    def test_manager_built_with_an_engine_ignores_the_current_engine(self) -> None:
        explicit = Engine()
        scoped = EngineScoped(explicit)

        assert scoped.engine is explicit
        assert scoped.engine is not current_engine()


class TestEngineScope:
    def test_scope_binds_and_restores(self) -> None:
        root = current_engine()
        scoped = Engine()

        with engine_scope(scoped) as bound:
            assert bound is scoped
            assert current_engine() is scoped

        assert current_engine() is root

    def test_scope_without_an_argument_builds_a_fresh_engine(self) -> None:
        root = current_engine()

        with engine_scope() as bound:
            assert bound is not root
            assert current_engine() is bound

        assert current_engine() is root

    def test_scopes_nest_and_unwind_in_order(self) -> None:
        outer = Engine()
        inner = Engine()

        with engine_scope(outer):
            assert current_engine() is outer
            with engine_scope(inner):
                assert current_engine() is inner
            assert current_engine() is outer

    def test_scope_restores_when_the_body_raises(self) -> None:
        root = current_engine()

        def explode() -> None:
            msg = "boom"
            raise RuntimeError(msg)

        with pytest.raises(RuntimeError, match="boom"), engine_scope(Engine()):
            explode()

        assert current_engine() is root

    def test_facade_follows_the_scoped_engine(self) -> None:
        """The `GriptapeNodes` facade must resolve the scoped engine, not the root."""
        scoped = Engine()

        with engine_scope(scoped):
            assert GriptapeNodes() is scoped
            assert GriptapeNodes.get_instance() is scoped
            assert GriptapeNodes.FlowManager() is scoped.flow_manager
            assert GriptapeNodes.ObjectManager() is scoped.object_manager

    @pytest.mark.asyncio
    async def test_task_created_inside_the_scope_inherits_the_binding(self) -> None:
        """Asyncio tasks copy the context at creation, so work spawned in a scope stays bound."""
        scoped = Engine()

        async def read_engine() -> Engine:
            await asyncio.sleep(0)
            return current_engine()

        with engine_scope(scoped):
            task = asyncio.create_task(read_engine())
            observed = await task

        assert observed is scoped

    @pytest.mark.asyncio
    async def test_scope_survives_an_await_in_its_body(self) -> None:
        """Entering and exiting around an await must not trip the ContextVar token reset."""
        root = current_engine()
        scoped = Engine()

        with engine_scope(scoped):
            await asyncio.sleep(0)
            assert current_engine() is scoped

        assert current_engine() is root

    @pytest.mark.asyncio
    async def test_concurrent_tasks_can_hold_different_engines(self) -> None:
        """The point of a ContextVar: two concurrent flows of control, two engines."""
        first = Engine()
        second = Engine()

        async def observe(engine: Engine) -> Engine:
            with engine_scope(engine):
                await asyncio.sleep(0)
                return current_engine()

        observed = await asyncio.gather(observe(first), observe(second))

        assert observed == [first, second]


class TestRootEngine:
    def test_root_engine_is_cached(self) -> None:
        assert current_engine() is current_engine()

    def test_reset_root_engine_builds_a_fresh_one(self) -> None:
        first = current_engine()
        reset_root_engine()

        assert current_engine() is not first

    def test_has_current_engine_does_not_build_one(self) -> None:
        reset_root_engine()

        assert has_current_engine() is False

        current_engine()

        assert has_current_engine() is True

    def test_has_current_engine_is_true_inside_a_scope(self) -> None:
        reset_root_engine()

        with engine_scope(Engine()):
            assert has_current_engine() is True

    def test_racing_threads_share_one_root_engine(self) -> None:
        """The lazy build is locked, so a cold start under load cannot orphan an engine."""
        racer_count = 8
        reset_root_engine()
        observed: list[Engine] = []
        start = threading.Barrier(racer_count)

        def resolve() -> None:
            start.wait()
            observed.append(current_engine())

        threads = [threading.Thread(target=resolve) for _ in range(racer_count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert len(observed) == racer_count
        assert all(engine is observed[0] for engine in observed)


class TestFfmpegCacheRedirect:
    """Constructing an Engine must move ffmpeg off `static_ffmpeg`'s own package directory.

    That directory is read-only when the engine runs from a packaged app -- notably the Linux
    AppImage's FUSE mount, where it made every ffmpeg-dependent node fail with Errno 30.
    """

    @pytest.fixture(autouse=True)
    def _restore_static_ffmpeg_globals(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(static_ffmpeg.run, "SELF_DIR", static_ffmpeg.run.SELF_DIR)
        monkeypatch.setattr(static_ffmpeg.run, "LOCK_FILE", static_ffmpeg.run.LOCK_FILE)

    def test_redirects_away_from_the_package_directory(self) -> None:
        package_dir = Path(static_ffmpeg.run.__file__).parent

        Engine()

        assert not Path(static_ffmpeg.run.SELF_DIR).is_relative_to(package_dir)

    def test_defaults_to_xdg_data_home(self) -> None:
        Engine()

        assert Path(static_ffmpeg.run.SELF_DIR) == xdg_data_home() / "griptape_nodes" / "ffmpeg"

    def test_honors_config_env_var(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """`GTN_CONFIG_FFMPEG_DIRECTORY` is the hook a packaged app uses to supply its own binaries."""
        monkeypatch.setenv("GTN_CONFIG_FFMPEG_DIRECTORY", str(tmp_path))

        Engine()

        assert Path(static_ffmpeg.run.SELF_DIR) == tmp_path
