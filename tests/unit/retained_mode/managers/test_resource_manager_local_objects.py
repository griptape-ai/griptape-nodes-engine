"""Tests for the process-local object cache on ResourceManager.

They pin the parts that are easy to get wrong -- key namespacing, what a miss says, the release hook,
and the lock -- rather than the dict.
"""

import asyncio
import sys
import threading
import time
from collections.abc import Iterator
from unittest.mock import AsyncMock, patch

import pytest

from griptape_nodes.app.worker_routing import DropAllLocalObjectsRequest, DropLocalObjectsRequest
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

    Without this the same-key drop test caught an unsynchronized drop about one run in eight, because
    whether the second thread reads before the first deletes is a coin flip. The timeout is what lets the
    synchronized version, where that second read cannot happen, finish at all.
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

        key = manager.put_local_object(held, owner="Lib A", source="Builder")

        assert manager.get_local_object(key, owner="Lib A") is held

    def test_miss_returns_none(self, engine: Engine) -> None:
        assert engine.resource_manager.get_local_object("Lib A:nope", owner="Lib A") is None

    def test_key_is_namespaced_by_owner(self, engine: Engine) -> None:
        """One worker serves several libraries, and one picking a suffix cannot see another's scheme."""
        manager = engine.resource_manager
        a = Held("a")
        b = Held("b")

        key_a = manager.put_local_object(a, owner="Lib A", source="N", key="same-hash")
        key_b = manager.put_local_object(b, owner="Lib B", source="N", key="same-hash")

        assert key_a != key_b
        assert manager.get_local_object(key_a, owner="Lib A") is a
        assert manager.get_local_object(key_b, owner="Lib B") is b

    def test_reading_another_library_s_key_misses(self, engine: Engine) -> None:
        """An unscoped read here would work only while both libraries happened to share a process."""
        manager = engine.resource_manager
        key = manager.put_local_object(Held("theirs"), owner="Lib A", source="N")

        assert manager.get_local_object(key, owner="Lib B") is None
        assert manager.get_local_object(key, owner="Lib A") is not None

    def test_supplied_key_is_reusable(self, engine: Engine) -> None:
        """Putting the same key twice replaces the entry, which is what recipe-style reuse needs."""
        manager = engine.resource_manager
        first = Held("first")
        second = Held("second")

        key = manager.put_local_object(first, owner="Lib A", source="N", key="cfg")
        again = manager.put_local_object(second, owner="Lib A", source="N", key="cfg")

        assert again == key
        assert manager.get_local_object(key, owner="Lib A") is second

    def test_replacing_releases_what_it_displaced(self, engine: Engine) -> None:
        """Rebuilding under the same config hash would otherwise leak the old object's memory each time."""
        manager = engine.resource_manager
        released: list[str] = []

        manager.put_local_object(
            Held("first"),
            owner="Lib A",
            source="N",
            key="cfg",
            on_drop=lambda value: released.append(value.label),
        )
        manager.put_local_object(Held("second"), owner="Lib A", source="N", key="cfg")

        assert released == ["first"]

    def test_re_registering_the_same_object_does_not_release_it(self, engine: Engine) -> None:
        """Re-registering the object already at a key must not tear down what is now the live value."""
        manager = engine.resource_manager
        released: list[str] = []
        held = Held("pipeline")

        key = manager.put_local_object(
            held,
            owner="Lib A",
            source="N",
            key="cfg",
            on_drop=lambda value: released.append(value.label),
        )
        manager.put_local_object(
            held,
            owner="Lib A",
            source="N",
            key="cfg",
            on_drop=lambda value: released.append(value.label),
        )

        assert released == []
        assert manager.get_local_object(key, owner="Lib A") is held

    def test_a_first_put_displaces_nothing(self, engine: Engine) -> None:
        manager = engine.resource_manager
        released: list[str] = []

        manager.put_local_object(
            Held("only"),
            owner="Lib A",
            source="N",
            key="cfg",
            on_drop=lambda value: released.append(value.label),
        )

        assert released == []

    def test_different_sources_get_different_slots(self, engine: Engine) -> None:
        """Residency scales with producing nodes, not with runs: two nodes coexist, one node twice does not."""
        manager = engine.resource_manager

        first = manager.put_local_object(Held("a"), owner="Lib A", source="Node A")
        second = manager.put_local_object(Held("b"), owner="Lib A", source="Node B")

        assert first != second
        assert manager.get_local_object(first, owner="Lib A") is not None
        assert manager.get_local_object(second, owner="Lib A") is not None


class TestDrop:
    def test_drop_removes_and_reports(self, engine: Engine) -> None:
        manager = engine.resource_manager
        key = manager.put_local_object(Held("x"), owner="Lib A", source="N")

        assert manager.drop_local_object(key) is True
        assert manager.get_local_object(key, owner="Lib A") is None
        assert manager.drop_local_object(key) is False

    def test_drop_runs_the_release_hook(self, engine: Engine) -> None:
        """Dropping the reference does not free what the object holds, so the hook must run."""
        manager = engine.resource_manager
        released: list[str] = []
        held = Held("gpu")

        key = manager.put_local_object(
            held,
            owner="Lib A",
            source="N",
            on_drop=lambda value: released.append(value.label),
        )
        manager.drop_local_object(key)

        assert released == ["gpu"]

    def test_refuses_a_key_owned_by_another_library(self, engine: Engine) -> None:
        """Releasing another owner's object runs their teardown under them."""
        manager = engine.resource_manager
        released: list[str] = []
        key = manager.put_local_object(
            Held("theirs"),
            owner="Lib A",
            source="N",
            on_drop=lambda value: released.append(value.label),
        )

        assert manager.drop_local_object(key, owner="Lib B") is False
        assert manager.get_local_object(key, owner="Lib A") is not None
        assert released == []

    def test_owner_may_drop_its_own(self, engine: Engine) -> None:
        manager = engine.resource_manager
        key = manager.put_local_object(Held("mine"), owner="Lib A", source="N")

        assert manager.drop_local_object(key, owner="Lib A") is True

    def test_a_raising_hook_still_removes_the_entry(self, engine: Engine) -> None:
        """The entry is popped before teardown runs, deliberately.

        If a failing teardown left the entry in place, nothing would retry it and the object would stay
        resident for the life of the process.
        """
        manager = engine.resource_manager

        def explode(_value: object) -> None:
            error = "teardown failed"
            raise RuntimeError(error)

        key = manager.put_local_object(Held("x"), owner="Lib A", source="N", on_drop=explode)

        assert manager.drop_local_object(key) is True
        assert manager.get_local_object(key, owner="Lib A") is None


class TestDropForLibrary:
    def test_drops_only_that_library(self, engine: Engine) -> None:
        manager = engine.resource_manager
        mine = manager.put_local_object(Held("mine"), owner="Lib A", source="N")
        theirs = manager.put_local_object(Held("theirs"), owner="Lib B", source="N")

        dropped = manager.drop_objects_for_owner("Lib A")

        assert dropped == 1
        assert manager.get_local_object(mine, owner="Lib A") is None
        assert manager.get_local_object(theirs, owner="Lib B") is not None

    def test_runs_every_release_hook(self, engine: Engine) -> None:
        manager = engine.resource_manager
        released: list[str] = []
        for label in ("one", "two"):
            manager.put_local_object(
                Held(label),
                owner="Lib A",
                source="N",
                on_drop=lambda value: released.append(value.label),
            )

        manager.drop_objects_for_owner("Lib A")

        assert sorted(released) == ["one", "two"]

    def test_one_raising_hook_does_not_strand_the_rest(self, engine: Engine) -> None:
        """A library reload clears many entries at once; one bad teardown must not abort the sweep."""
        manager = engine.resource_manager
        released: list[str] = []

        def explode(_value: object) -> None:
            error = "teardown failed"
            raise RuntimeError(error)

        manager.put_local_object(Held("bad"), owner="Lib A", source="Node A", on_drop=explode)
        manager.put_local_object(
            Held("good"),
            owner="Lib A",
            source="Node B",
            on_drop=lambda value: released.append(value.label),
        )
        entries_put = 2

        dropped = manager.drop_objects_for_owner("Lib A")

        assert dropped == entries_put
        assert released == ["good"]


class TestPresence:
    def test_a_sentinel_distinguishes_absent_from_falsy(self, engine: Engine) -> None:
        """One sentinel lookup leaves no window for a drop between a presence check and a read."""
        manager = engine.resource_manager
        missing = object()
        key = manager.put_local_object(None, owner="Lib A", source="N")

        assert manager.get_local_object(key, owner="Lib A", default=missing) is None
        assert manager.get_local_object("Lib A:gone", owner="Lib A", default=missing) is missing

    def test_default_is_none_when_not_given(self, engine: Engine) -> None:
        assert engine.resource_manager.get_local_object("Lib A:gone", owner="Lib A") is None


class TestKeyDerivation:
    def test_derived_key_matches_what_put_returns(self, engine: Engine) -> None:
        """A caller that supplied a suffix must be able to look it up again.

        `key` goes in as a suffix and comes back namespaced, so without this the library silently misses
        its own entry and rebuilds the model every execution.
        """
        manager = engine.resource_manager
        put_key = manager.put_local_object(Held("x"), owner="Lib A", source="N", key="cfg")

        assert manager.local_object_key("cfg", owner="Lib A") == put_key

    def test_default_key_is_the_source(self, engine: Engine) -> None:
        """A random default would have no owner able to release it, so a re-run would strand the old one."""
        manager = engine.resource_manager
        first = manager.put_local_object(Held("a"), owner="Lib A", source="Loader")
        second = manager.put_local_object(Held("b"), owner="Lib A", source="Loader")

        assert first == second
        assert first == manager.local_object_key("Loader", owner="Lib A")

    def test_a_re_run_releases_what_it_replaced(self, engine: Engine) -> None:
        """The point of keying on the node: residency is bounded by producing nodes, not executions."""
        manager = engine.resource_manager
        released: list[str] = []

        for label in ("first", "second", "third"):
            manager.put_local_object(
                Held(label),
                owner="Lib A",
                source="Loader",
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

        manager.put_local_object(Held("x"), owner="Lib A", source="N")

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
            owner="MyLib",
            source="N",
            on_drop=lambda value: released.append(value.label),
        )
        theirs = manager.put_local_object(Held("theirs"), owner="OtherLib", source="N")

        with (
            patch.object(LibraryRegistry, "unregister_library"),
            patch.object(engine.library_manager, "_unregister_all_stable_module_aliases_for_library"),
        ):
            result = engine.library_manager.unload_library_from_registry_request(
                UnloadLibraryFromRegistryRequest(library_name="MyLib")
            )

        assert isinstance(result, UnloadLibraryFromRegistryResultSuccess)
        assert manager.get_local_object(mine, owner="MyLib") is None
        assert released == ["mine"]
        assert manager.get_local_object(theirs, owner="OtherLib") is not None


class TestWorkflowStateClearReleasesObjects:
    """Clearing workflow state deletes every node, and so every key, whoever chose it."""

    def test_tearing_down_a_workflow_releases_every_library(self, engine: Engine) -> None:
        manager = engine.resource_manager
        released: list[str] = []
        for library, label in (("Lib A", "a"), ("Lib B", "b")):
            manager.put_local_object(
                Held(label),
                owner=library,
                source="N",
                on_drop=lambda value: released.append(value.label),
            )

        engine.context_manager.push_workflow("wf")
        engine.clear_current_workflow_data()

        assert sorted(released) == ["a", "b"]
        assert manager.drop_all_local_objects() == 0

    def test_clearing_object_state_broadcasts_to_workers(self, engine: Engine) -> None:
        """Teardown runs on the orchestrator, so without the broadcast the worker keeps its objects."""
        engine.context_manager.push_workflow("wf")

        with patch.object(engine.worker_manager, "broadcast_drop_all_local_objects", AsyncMock()) as broadcast:
            engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))

        assert broadcast.await_count == 1

    def test_a_failed_teardown_still_tells_the_workers(self, engine: Engine) -> None:
        """Nothing retries this, so a worker that was never told keeps its objects for the process's life."""
        engine.context_manager.push_workflow("wf")

        with (
            patch.object(engine, "clear_current_workflow_data", side_effect=RuntimeError("teardown blew up")),
            patch.object(engine.worker_manager, "broadcast_drop_all_local_objects", AsyncMock()) as broadcast,
        ):
            result = engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))

        assert result.failed()
        assert broadcast.await_count == 1

    def test_a_failing_broadcast_does_not_break_the_teardown(self, engine: Engine) -> None:
        """A failed notification must not fail the teardown it reports on.

        A send fails most readily against a dying worker, which is when a workflow is closing. Letting it
        propagate would replace the real result with a transport error, skip the clearing that follows,
        and in `on_delete_workflows_request` leave a registered workflow with no contents.
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
        """One layer deeper than mocking the broadcast, so losing the message without raising cannot pass."""
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
        """The hook sits on the teardown, which this handler is only one route into."""
        manager = engine.resource_manager
        released: list[str] = []
        manager.put_local_object(
            Held("x"),
            owner="Lib A",
            source="N",
            on_drop=lambda value: released.append(value.label),
        )
        engine.context_manager.push_workflow("wf")

        engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))

        assert released == ["x"]

    def test_a_refused_clear_releases_nothing(self, engine: Engine) -> None:
        """The guard rejects the request before any teardown, so objects must survive it."""
        manager = engine.resource_manager
        key = manager.put_local_object(Held("x"), owner="Lib A", source="N")

        engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=False))

        assert manager.get_local_object(key, owner="Lib A") is not None


class TestConcurrentAccess:
    """The map is reachable from real threads, so mutation must not corrupt it or raise.

    Node bodies yielding a callable run via `async_utils.to_thread` and parallel resolution runs several
    at once, so a clear-cache node and a producing node can be inside these methods together. Map
    integrity only: the held object's lifetime is not protected.
    """

    @pytest.fixture(autouse=True)
    def _preempt_aggressively(self) -> Iterator[None]:
        """Both tests below pass with the lock removed at the 5ms default, which never preempts inside one."""
        previous = sys.getswitchinterval()
        sys.setswitchinterval(1e-6)
        try:
            yield
        finally:
            sys.setswitchinterval(previous)

    def test_putting_while_clearing_neither_raises_nor_corrupts(self, engine: Engine) -> None:
        manager = engine.resource_manager
        errors: list[Exception] = []
        stop = threading.Event()

        def keep_putting() -> None:
            try:
                index = 0
                while not stop.is_set():
                    manager.put_local_object(Held(str(index)), owner="Lib A", source=f"N{index % 20}")
                    index += 1
            except Exception as exc:
                errors.append(exc)

        def keep_clearing() -> None:
            try:
                while not stop.is_set():
                    manager.drop_objects_for_owner("Lib A")
            except Exception as exc:
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

        key = manager.put_local_object(Held("once"), owner="Lib A", source="N", on_drop=record)
        # The lookup and the delete are adjacent bytecodes, so no switch interval preempts between them
        # and an unsynchronized drop passes. Ordering the delete after a second read is what forces the
        # interleaving this test is about.
        manager._local_objects = _DeleteAfterSecondRead(manager._local_objects)
        barrier = threading.Barrier(2)
        reported: list[bool] = []
        errors: list[Exception] = []

        def drop() -> None:
            barrier.wait()
            try:
                reported.append(manager.drop_local_object(key))
            except Exception as exc:
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


class TestReleaseKeyEverywhere:
    """What the engine calls when a handle stops being referenced: release here, tell the workers."""

    @pytest.fixture(autouse=True)
    def _with_registered_workers(self, engine: Engine) -> Iterator[None]:
        """Workers exist but no loop is running, which is how a sync release path arrives here.

        The keys stay queued for the awaited drain rather than being sent, which is what these assert on.
        With no workers registered the queue is drained on the spot instead, since there is nobody to tell
        and it must not grow for the life of the process -- covered separately below.
        """
        worker_manager = engine.worker_manager
        with (
            patch.object(worker_manager, "_transport", object()),
            patch.object(worker_manager, "_workers", {"w1": object()}),
        ):
            yield

    def test_releases_locally_and_queues_the_key(self, engine: Engine) -> None:
        manager = engine.resource_manager
        released: list[str] = []
        key = manager.put_local_object(
            Held("pipeline"),
            owner="Lib A",
            source="N",
            key="cfg",
            on_drop=lambda value: released.append(value.label),
        )

        assert manager.release_key_everywhere(key, owner="Lib A") is True
        assert released == ["pipeline"]
        assert manager.get_local_object(key, owner="Lib A") is None
        assert manager.drain_pending_worker_releases() == [key]

    def test_a_key_this_process_never_held_is_still_queued(self, engine: Engine) -> None:
        """The object is usually in a worker, so the local result says nothing about who holds it."""
        manager = engine.resource_manager

        assert manager.release_key_everywhere("Lib A:elsewhere", owner="Lib A") is False
        assert manager.drain_pending_worker_releases() == ["Lib A:elsewhere"]

    def test_draining_empties_the_queue(self, engine: Engine) -> None:
        manager = engine.resource_manager
        manager.release_key_everywhere("Lib A:one", owner="Lib A")

        assert manager.drain_pending_worker_releases() == ["Lib A:one"]
        assert manager.drain_pending_worker_releases() == []

    def test_the_broadcast_carries_the_drained_keys_to_the_transport(self, engine: Engine) -> None:
        """Mocking the broadcast method proves a call, never that a message is sent."""
        manager = engine.resource_manager
        worker_manager = engine.worker_manager
        manager.release_key_everywhere("Lib A:one", owner="Lib A")
        manager.release_key_everywhere("Lib A:two", owner="Lib A")

        with (
            patch.object(worker_manager, "broadcast_to_workers", AsyncMock()) as fan_out,
            patch.object(worker_manager, "_transport", object()),
            patch.object(worker_manager, "_workers", {"w1": object()}),
        ):
            asyncio.run(worker_manager.broadcast_pending_local_object_releases())

        assert fan_out.await_count == 1
        assert fan_out.await_args is not None
        sent = fan_out.await_args.args[0]
        assert isinstance(sent.request, DropLocalObjectsRequest)
        assert sent.request.keys == ["Lib A:one", "Lib A:two"]

    def test_no_workers_means_no_message_and_no_growing_queue(self, engine: Engine) -> None:
        """A worker process has no workers of its own, and its queue must not grow for the process's life."""
        manager = engine.resource_manager
        worker_manager = engine.worker_manager

        with patch.object(worker_manager, "_workers", {}):
            manager.release_key_everywhere("Lib A:one", owner="Lib A")

        assert manager.drain_pending_worker_releases() == []

    def test_a_failing_broadcast_does_not_raise(self, engine: Engine) -> None:
        """A send fails most readily against a dying worker, and the release already happened here."""
        engine.resource_manager.release_key_everywhere("Lib A:one", owner="Lib A")
        worker_manager = engine.worker_manager

        with (
            patch.object(worker_manager, "broadcast_to_workers", AsyncMock(side_effect=RuntimeError("no broker"))),
            patch.object(worker_manager, "_transport", object()),
            patch.object(worker_manager, "_workers", {"w1": object()}),
        ):
            asyncio.run(worker_manager.broadcast_pending_local_object_releases())


@pytest.mark.parametrize("bad_key", ["", "no-namespace", "Lib A:", ":suffix"])
def test_malformed_keys_miss_rather_than_raise(engine: Engine, bad_key: str) -> None:
    """A key arrives from a parameter value, so it can be anything. A lookup must not raise."""
    assert engine.resource_manager.get_local_object(bad_key, owner="Lib A") is None
    assert engine.resource_manager.drop_local_object(bad_key) is False
