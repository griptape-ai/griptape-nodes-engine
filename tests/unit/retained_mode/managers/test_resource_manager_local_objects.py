"""Tests for the process-local object cache on ResourceManager.

The cache exists so a library can pass something unserializable between nodes: the object stays in the
process that built it and a key travels as the parameter value. These tests pin the parts that are easy
to get wrong -- key namespacing, what a miss says, and the release hook -- rather than the dict.
"""

import asyncio
import sys
import threading
import time
from collections.abc import Iterator
from unittest.mock import AsyncMock, patch

import pytest

from griptape_nodes.app.worker_routing import DropAllLocalObjectsRequest
from griptape_nodes.exe_types.core_types import ParameterType, ParameterTypeBuiltin
from griptape_nodes.node_library.library_registry import LibraryRegistry
from griptape_nodes.retained_mode.engine import Engine
from griptape_nodes.retained_mode.events.library_events import (
    UnloadLibraryFromRegistryRequest,
    UnloadLibraryFromRegistryResultSuccess,
)
from griptape_nodes.retained_mode.events.object_events import ClearAllObjectStateRequest
from griptape_nodes.retained_mode.managers.resource_manager import LocalObjectEntry

_SECOND_READ_TIMEOUT_SECONDS = 0.25


class Held:
    """Stands in for something that cannot cross a process boundary."""

    def __init__(self, label: str) -> None:
        self.label = label


class _DeleteAfterSecondRead(dict[str, LocalObjectEntry]):
    """A map that holds a delete until a second reader has been through it.

    Getting both threads inside the lookup is not enough: whether the second one reads before the
    first deletes is still a coin flip, and the same-key drop test caught an unsynchronized drop only
    about one run in eight. Delaying the delete until a second read lands makes the interleaving
    certain. The timeout is what lets the synchronized version, where that second read cannot happen
    until the delete is done, finish at all.
    """

    def __init__(self, source: dict[str, LocalObjectEntry]) -> None:
        super().__init__(source)
        self._reads_seen = 0
        self._reads = threading.Condition()

    def get(self, key: str, default: LocalObjectEntry | None = None) -> LocalObjectEntry | None:
        with self._reads:
            self._reads_seen += 1
            self._reads.notify_all()
        return super().get(key, default)

    def __delitem__(self, key: str) -> None:
        with self._reads:
            self._reads.wait_for(lambda: self._reads_seen >= 2, timeout=_SECOND_READ_TIMEOUT_SECONDS)  # noqa: PLR2004
        super().__delitem__(key)


class TestPutAndGet:
    def test_round_trip(self, engine: Engine) -> None:
        manager = engine.resource_manager
        held = Held("pipeline")

        key = manager.put_local_object(held, owner_library="Lib A", producing_node="Builder")

        assert manager.get_local_object(key, owner_library="Lib A") is held

    def test_miss_returns_none(self, engine: Engine) -> None:
        assert engine.resource_manager.get_local_object("Lib A:nope", owner_library="Lib A") is None

    def test_key_is_namespaced_by_owner(self, engine: Engine) -> None:
        """Two libraries choosing the same suffix must not collide.

        One worker can serve several libraries, and a library picking its own suffix (a config hash,
        say) cannot know what another library's scheme looks like.
        """
        manager = engine.resource_manager
        a = Held("a")
        b = Held("b")

        key_a = manager.put_local_object(a, owner_library="Lib A", producing_node="N", key="same-hash")
        key_b = manager.put_local_object(b, owner_library="Lib B", producing_node="N", key="same-hash")

        assert key_a != key_b
        assert manager.get_local_object(key_a, owner_library="Lib A") is a
        assert manager.get_local_object(key_b, owner_library="Lib B") is b

    def test_reading_another_library_s_key_misses(self, engine: Engine) -> None:
        """The rule lives here, not only in the node helper that usually calls this.

        Libraries reach this manager through the documented facade, so a read that ignored ownership
        would work for as long as both libraries happened to share a process and stop the moment either
        moved to a worker: the same graph failing later, with a message saying it was never possible.
        """
        manager = engine.resource_manager
        key = manager.put_local_object(Held("theirs"), owner_library="Lib A", producing_node="N")

        assert manager.get_local_object(key, owner_library="Lib B") is None
        assert manager.get_local_object(key, owner_library="Lib A") is not None

    def test_supplied_key_is_reusable(self, engine: Engine) -> None:
        """Putting the same key twice replaces the entry, which is what recipe-style reuse needs."""
        manager = engine.resource_manager
        first = Held("first")
        second = Held("second")

        key = manager.put_local_object(first, owner_library="Lib A", producing_node="N", key="cfg")
        again = manager.put_local_object(second, owner_library="Lib A", producing_node="N", key="cfg")

        assert again == key
        assert manager.get_local_object(key, owner_library="Lib A") is second

    def test_replacing_releases_what_it_displaced(self, engine: Engine) -> None:
        """Reuse must not strand the object it replaced.

        Dropping the last reference does not free what the object was holding, so rebuilding under the
        same config hash would otherwise leak that memory on every rebuild.
        """
        manager = engine.resource_manager
        released: list[str] = []

        manager.put_local_object(
            Held("first"),
            owner_library="Lib A",
            producing_node="N",
            key="cfg",
            on_drop=lambda value: released.append(value.label),
        )
        manager.put_local_object(Held("second"), owner_library="Lib A", producing_node="N", key="cfg")

        assert released == ["first"]

    def test_re_registering_the_same_object_does_not_release_it(self, engine: Engine) -> None:
        """Displacement is by identity, not by presence.

        Re-registering the object already at that key is the obvious way to write the reuse this API
        recommends. Running the release hook there would tear down the value that is now live, and the
        next reader would get a released object with nothing logged.
        """
        manager = engine.resource_manager
        released: list[str] = []
        held = Held("pipeline")

        key = manager.put_local_object(
            held,
            owner_library="Lib A",
            producing_node="N",
            key="cfg",
            on_drop=lambda value: released.append(value.label),
        )
        manager.put_local_object(
            held,
            owner_library="Lib A",
            producing_node="N",
            key="cfg",
            on_drop=lambda value: released.append(value.label),
        )

        assert released == []
        assert manager.get_local_object(key, owner_library="Lib A") is held

    def test_a_first_put_displaces_nothing(self, engine: Engine) -> None:
        manager = engine.resource_manager
        released: list[str] = []

        manager.put_local_object(
            Held("only"),
            owner_library="Lib A",
            producing_node="N",
            key="cfg",
            on_drop=lambda value: released.append(value.label),
        )

        assert released == []

    def test_different_producing_nodes_get_different_slots(self, engine: Engine) -> None:
        """Residency scales with the number of producing nodes, which is what makes it bounded.

        Two latent-producing nodes coexist; the same node running twice does not (see
        TestKeyDerivation).
        """
        manager = engine.resource_manager

        first = manager.put_local_object(Held("a"), owner_library="Lib A", producing_node="Node A")
        second = manager.put_local_object(Held("b"), owner_library="Lib A", producing_node="Node B")

        assert first != second
        assert manager.get_local_object(first, owner_library="Lib A") is not None
        assert manager.get_local_object(second, owner_library="Lib A") is not None


class TestDrop:
    def test_drop_removes_and_reports(self, engine: Engine) -> None:
        manager = engine.resource_manager
        key = manager.put_local_object(Held("x"), owner_library="Lib A", producing_node="N")

        assert manager.drop_local_object(key) is True
        assert manager.get_local_object(key, owner_library="Lib A") is None
        assert manager.drop_local_object(key) is False

    def test_drop_runs_the_release_hook(self, engine: Engine) -> None:
        """Dropping the reference does not free what the object holds, so the hook must run."""
        manager = engine.resource_manager
        released: list[str] = []
        held = Held("gpu")

        key = manager.put_local_object(
            held,
            owner_library="Lib A",
            producing_node="N",
            on_drop=lambda value: released.append(value.label),
        )
        manager.drop_local_object(key)

        assert released == ["gpu"]

    def test_refuses_a_key_owned_by_another_library(self, engine: Engine) -> None:
        """A library may only release what it put.

        Releasing someone else's object runs their teardown under them and leaves their own still-valid
        keys reporting it as gone, which reads as a crash that never happened.
        """
        manager = engine.resource_manager
        released: list[str] = []
        key = manager.put_local_object(
            Held("theirs"),
            owner_library="Lib A",
            producing_node="N",
            on_drop=lambda value: released.append(value.label),
        )

        assert manager.drop_local_object(key, owner_library="Lib B") is False
        assert manager.get_local_object(key, owner_library="Lib A") is not None
        assert released == []

    def test_owner_may_drop_its_own(self, engine: Engine) -> None:
        manager = engine.resource_manager
        key = manager.put_local_object(Held("mine"), owner_library="Lib A", producing_node="N")

        assert manager.drop_local_object(key, owner_library="Lib A") is True

    def test_a_raising_hook_still_removes_the_entry(self, engine: Engine) -> None:
        """The entry is popped before teardown runs, deliberately.

        If a failing teardown left the entry in place, nothing would retry it and the object would stay
        resident for the life of the process.
        """
        manager = engine.resource_manager

        def explode(_value: object) -> None:
            error = "teardown failed"
            raise RuntimeError(error)

        key = manager.put_local_object(Held("x"), owner_library="Lib A", producing_node="N", on_drop=explode)

        assert manager.drop_local_object(key) is True
        assert manager.get_local_object(key, owner_library="Lib A") is None


class TestDropForLibrary:
    def test_drops_only_that_library(self, engine: Engine) -> None:
        manager = engine.resource_manager
        mine = manager.put_local_object(Held("mine"), owner_library="Lib A", producing_node="N")
        theirs = manager.put_local_object(Held("theirs"), owner_library="Lib B", producing_node="N")

        dropped = manager.drop_local_objects_for_library("Lib A")

        assert dropped == 1
        assert manager.get_local_object(mine, owner_library="Lib A") is None
        assert manager.get_local_object(theirs, owner_library="Lib B") is not None

    def test_runs_every_release_hook(self, engine: Engine) -> None:
        manager = engine.resource_manager
        released: list[str] = []
        for label in ("one", "two"):
            manager.put_local_object(
                Held(label),
                owner_library="Lib A",
                producing_node="N",
                on_drop=lambda value: released.append(value.label),
            )

        manager.drop_local_objects_for_library("Lib A")

        assert sorted(released) == ["one", "two"]

    def test_one_raising_hook_does_not_strand_the_rest(self, engine: Engine) -> None:
        """A library reload clears many entries at once; one bad teardown must not abort the sweep."""
        manager = engine.resource_manager
        released: list[str] = []

        def explode(_value: object) -> None:
            error = "teardown failed"
            raise RuntimeError(error)

        manager.put_local_object(Held("bad"), owner_library="Lib A", producing_node="Node A", on_drop=explode)
        manager.put_local_object(
            Held("good"),
            owner_library="Lib A",
            producing_node="Node B",
            on_drop=lambda value: released.append(value.label),
        )
        entries_put = 2

        dropped = manager.drop_local_objects_for_library("Lib A")

        assert dropped == entries_put
        assert released == ["good"]


class TestPresence:
    def test_a_sentinel_distinguishes_absent_from_falsy(self, engine: Engine) -> None:
        """A library may legitimately hold a falsy value.

        One lookup against a sentinel rather than a presence check followed by a read, so a concurrent
        drop cannot land in between.
        """
        manager = engine.resource_manager
        missing = object()
        key = manager.put_local_object(None, owner_library="Lib A", producing_node="N")

        assert manager.get_local_object(key, owner_library="Lib A", default=missing) is None
        assert manager.get_local_object("Lib A:gone", owner_library="Lib A", default=missing) is missing

    def test_default_is_none_when_not_given(self, engine: Engine) -> None:
        assert engine.resource_manager.get_local_object("Lib A:gone", owner_library="Lib A") is None


class TestKeyDerivation:
    def test_derived_key_matches_what_put_returns(self, engine: Engine) -> None:
        """A caller that supplied a suffix must be able to look it up again.

        `key` goes in as a suffix and comes back namespaced, so without this the library silently misses
        its own entry and rebuilds the model every execution.
        """
        manager = engine.resource_manager
        put_key = manager.put_local_object(Held("x"), owner_library="Lib A", producing_node="N", key="cfg")

        assert manager.local_object_key("cfg", owner_library="Lib A") == put_key

    def test_default_key_is_the_producing_node(self, engine: Engine) -> None:
        """A random default would have no owner able to release it, so a re-run would strand the old one."""
        manager = engine.resource_manager
        first = manager.put_local_object(Held("a"), owner_library="Lib A", producing_node="Loader")
        second = manager.put_local_object(Held("b"), owner_library="Lib A", producing_node="Loader")

        assert first == second
        assert first == manager.local_object_key("Loader", owner_library="Lib A")

    def test_a_re_run_releases_what_it_replaced(self, engine: Engine) -> None:
        """The point of keying on the node: residency is bounded by producing nodes, not executions."""
        manager = engine.resource_manager
        released: list[str] = []

        for label in ("first", "second", "third"):
            manager.put_local_object(
                Held(label),
                owner_library="Lib A",
                producing_node="Loader",
                on_drop=lambda value: released.append(value.label),
            )

        assert released == ["first", "second"]


class TestHandleParameterType:
    """`handle` is used parameterised, so the existing generic rules do the validation."""

    def test_same_kind_connects(self) -> None:
        assert ParameterType.are_types_compatible("handle[DiffusionPipeline]", "handle[DiffusionPipeline]")

    def test_different_kinds_are_refused(self) -> None:
        """The reason to parameterise: a latent must not be wirable into a pipeline input."""
        assert not ParameterType.are_types_compatible("handle[Latent]", "handle[DiffusionPipeline]")

    def test_bare_handle_accepts_any_kind(self) -> None:
        assert ParameterType.are_types_compatible("handle[DiffusionPipeline]", "handle")

    def test_handle_any_accepts_any_kind(self) -> None:
        assert ParameterType.are_types_compatible("handle[Latent]", "handle[any]")

    def test_a_handle_is_not_a_string(self) -> None:
        """The key is carried as a string, but the parameter must not accept arbitrary strings."""
        assert not ParameterType.are_types_compatible("str", "handle[Latent]")

    def test_handle_resolves_as_a_builtin(self) -> None:
        """Two structures must stay in sync, and nothing enforces it.

        `ParameterTypeBuiltin` lists the type and `ParameterType._builtin_aliases` is what
        `attempt_get_builtin` reads. A member missing from the aliases is the one builtin that gets no
        name normalisation, so `Handle[X]` would reach serialisation with the author's casing and any
        code treating `attempt_get_builtin(t) is None` as "library-defined" would misclassify it.
        """
        assert ParameterType.attempt_get_builtin("handle") is ParameterTypeBuiltin.HANDLE
        assert ParameterType.attempt_get_builtin("Handle") is ParameterTypeBuiltin.HANDLE

    def test_every_builtin_has_an_alias(self) -> None:
        """The invariant behind the test above, so a future addition cannot repeat the omission."""
        unaliased = [
            member.name
            for member in ParameterTypeBuiltin
            if ParameterType.attempt_get_builtin(member.value) is not member
        ]
        assert unaliased == []


class TestCapabilityMapIsSeparate:
    def test_local_objects_are_invisible_to_the_capability_map(self, engine: Engine) -> None:
        """The capability query gates whether a library may execute, so it must not see held objects.

        Keeping the two maps apart is the one rule this design cannot break.
        """
        manager = engine.resource_manager
        before = len(manager._capability_instances)

        manager.put_local_object(Held("x"), owner_library="Lib A", producing_node="N")

        assert len(manager._capability_instances) == before


class TestLibraryUnloadClears:
    """Unload is the hook, and reload goes through it: it unloads every library before loading again.

    A library must not come back holding objects its previous code built.
    """

    def test_unload_releases_only_the_unloaded_library(self, engine: Engine) -> None:
        manager = engine.resource_manager
        released: list[str] = []
        mine = manager.put_local_object(
            Held("mine"),
            owner_library="MyLib",
            producing_node="N",
            on_drop=lambda value: released.append(value.label),
        )
        theirs = manager.put_local_object(Held("theirs"), owner_library="OtherLib", producing_node="N")

        with (
            patch.object(LibraryRegistry, "unregister_library"),
            patch.object(engine.library_manager, "_unregister_all_stable_module_aliases_for_library"),
        ):
            result = engine.library_manager.unload_library_from_registry_request(
                UnloadLibraryFromRegistryRequest(library_name="MyLib")
            )

        assert isinstance(result, UnloadLibraryFromRegistryResultSuccess)
        assert manager.get_local_object(mine, owner_library="MyLib") is None
        assert released == ["mine"]
        assert manager.get_local_object(theirs, owner_library="OtherLib") is not None


class TestWorkflowStateClearReleasesObjects:
    """Clearing workflow state deletes every node, and every key lived in a node's parameter value.

    So the objects become unreachable while still holding what they hold. Nothing persists across
    workflows, which is why this clears everything rather than distinguishing how the key was chosen.
    """

    def test_tearing_down_a_workflow_releases_every_library(self, engine: Engine) -> None:
        manager = engine.resource_manager
        released: list[str] = []
        for library, label in (("Lib A", "a"), ("Lib B", "b")):
            manager.put_local_object(
                Held(label),
                owner_library=library,
                producing_node="N",
                on_drop=lambda value: released.append(value.label),
            )

        engine.context_manager.push_workflow("wf")
        engine.clear_current_workflow_data()

        assert sorted(released) == ["a", "b"]
        assert manager.drop_all_local_objects() == 0

    def test_clearing_object_state_broadcasts_to_workers(self, engine: Engine) -> None:
        """The local drop alone is nearly pointless.

        A library declaring execution dependencies holds its objects in a worker, and workflow teardown
        happens on the orchestrator, so without the broadcast the process with the gigabytes keeps them.

        Awaited from the async handler rather than scheduled from the sync teardown: a fan-out created
        on a transient loop is destroyed when that loop closes, losing the message silently.
        """
        engine.context_manager.push_workflow("wf")

        with patch.object(engine.worker_manager, "broadcast_drop_all_local_objects", AsyncMock()) as broadcast:
            engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))

        assert broadcast.await_count == 1

    def test_a_failed_teardown_still_tells_the_workers(self, engine: Engine) -> None:
        """The orchestrator has already released whatever it got to before the failure.

        Nothing retries this request, and the callers abort the load or reload it was part of, so a
        worker that was never told keeps its objects for the life of the process.
        """
        engine.context_manager.push_workflow("wf")

        with (
            patch.object(engine, "clear_current_workflow_data", side_effect=RuntimeError("teardown blew up")),
            patch.object(engine.worker_manager, "broadcast_drop_all_local_objects", AsyncMock()) as broadcast,
        ):
            result = engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))

        assert result.failed()
        assert broadcast.await_count == 1

    def test_a_failing_broadcast_does_not_break_the_teardown(self, engine: Engine) -> None:
        """The notification is best-effort; what it reports on is not.

        A send fails most readily against a dying worker, which is exactly when a workflow is closing.
        Letting that propagate out of the teardown would replace the real result with a transport error
        and skip the state clearing that follows -- and in `on_delete_workflows_request`, leave the
        workflow in the registry with its contents already destroyed.
        """
        engine.context_manager.push_workflow("wf")
        worker_manager = engine.worker_manager

        with (
            patch.object(worker_manager, "broadcast_to_workers", AsyncMock(side_effect=RuntimeError("no broker"))),
            patch.object(worker_manager, "_transport", object()),
            patch.object(worker_manager, "_workers", {"w1": object()}),
        ):
            result = engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))

        assert result.succeeded()

    def test_the_broadcast_actually_reaches_the_transport(self, engine: Engine) -> None:
        """Mocking the broadcast method proves the call, never that a message is sent.

        This goes one layer deeper so a fire-and-forget regression, which loses the message without
        raising, cannot pass.
        """
        worker_manager = engine.worker_manager
        with (
            patch.object(worker_manager, "broadcast_to_workers", AsyncMock()) as fan_out,
            patch.object(worker_manager, "_transport", object()),
            patch.object(worker_manager, "_workers", {"w1": object()}),
        ):
            asyncio.run(worker_manager.broadcast_drop_all_local_objects())

        assert fan_out.await_count == 1
        assert fan_out.await_args is not None
        sent = fan_out.await_args.args[0]
        assert isinstance(sent.request, DropAllLocalObjectsRequest)

    def test_clearing_all_object_state_reaches_the_same_teardown(self, engine: Engine) -> None:
        """The handler path still releases, even though the hook moved off it.

        The hook sits on the teardown rather than on this handler, because deleting the open workflow
        reaches the teardown directly without coming through here.
        """
        manager = engine.resource_manager
        released: list[str] = []
        manager.put_local_object(
            Held("x"),
            owner_library="Lib A",
            producing_node="N",
            on_drop=lambda value: released.append(value.label),
        )
        engine.context_manager.push_workflow("wf")

        engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))

        assert released == ["x"]

    def test_a_refused_clear_releases_nothing(self, engine: Engine) -> None:
        """The guard rejects the request before any teardown, so objects must survive it."""
        manager = engine.resource_manager
        key = manager.put_local_object(Held("x"), owner_library="Lib A", producing_node="N")

        engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=False))

        assert manager.get_local_object(key, owner_library="Lib A") is not None


class TestConcurrentAccess:
    """The map is reachable from real threads, so mutation must not corrupt it or raise.

    Node bodies that yield a callable run via `async_utils.to_thread`, and parallel resolution runs
    several node tasks at once, so a clear-cache node and a producing node can be inside these methods
    simultaneously. This pins map integrity only; the held object's lifetime is not protected, which is
    stated in the manager and deliberately out of this change.
    """

    @pytest.fixture(autouse=True)
    def _preempt_aggressively(self) -> Iterator[None]:
        """Without this, both tests below pass with the lock removed.

        Each critical section is shorter than the 5ms default switch interval, so the interpreter
        never preempts inside one and an unsynchronized map is never caught.
        """
        previous = sys.getswitchinterval()
        sys.setswitchinterval(1e-6)
        try:
            yield
        finally:
            sys.setswitchinterval(previous)

    def test_putting_while_clearing_neither_raises_nor_corrupts(self, engine: Engine) -> None:
        manager = engine.resource_manager
        errors: list[BaseException] = []
        stop = threading.Event()

        def keep_putting() -> None:
            try:
                index = 0
                while not stop.is_set():
                    manager.put_local_object(Held(str(index)), owner_library="Lib A", producing_node=f"N{index % 20}")
                    index += 1
            except BaseException as exc:
                errors.append(exc)

        def keep_clearing() -> None:
            try:
                while not stop.is_set():
                    manager.drop_local_objects_for_library("Lib A")
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=keep_putting), threading.Thread(target=keep_clearing)]
        for thread in threads:
            thread.start()
        time.sleep(0.25)
        stop.set()
        for thread in threads:
            thread.join(timeout=5)

        assert errors == []

    def test_concurrent_drops_of_one_key_release_exactly_once(self, engine: Engine) -> None:
        """Two threads dropping the same key must not both run its release hook."""
        manager = engine.resource_manager
        released: list[str] = []
        release_lock = threading.Lock()

        def record(value: Held) -> None:
            with release_lock:
                released.append(value.label)

        key = manager.put_local_object(Held("once"), owner_library="Lib A", producing_node="N", on_drop=record)
        # The lookup and the delete are adjacent bytecodes, so no switch interval preempts between them
        # and an unsynchronized drop passes. Ordering the delete after a second read is what forces the
        # interleaving this test is about.
        manager._local_objects = _DeleteAfterSecondRead(manager._local_objects)
        barrier = threading.Barrier(2)
        reported: list[bool] = []
        errors: list[BaseException] = []

        def drop() -> None:
            barrier.wait()
            try:
                reported.append(manager.drop_local_object(key))
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=drop) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        # The hook count alone cannot see the failure: an unsynchronized drop raises out of the second
        # thread's delete having released exactly once, so the caller learns of a teardown that did happen
        # as a crash, and both threads claiming True would hide a double release from a future refactor.
        assert errors == []
        assert sorted(reported) == [False, True]
        assert released == ["once"]


@pytest.mark.parametrize("bad_key", ["", "no-namespace", "Lib A:", ":suffix"])
def test_malformed_keys_miss_rather_than_raise(engine: Engine, bad_key: str) -> None:
    """A key arrives from a parameter value, so it can be anything. A lookup must not raise."""
    assert engine.resource_manager.get_local_object(bad_key, owner_library="Lib A") is None
    assert engine.resource_manager.drop_local_object(bad_key) is False
