"""Tests for the Engine object graph and how the current engine is resolved.

These pin the guarantees the refactor away from a process-wide singleton was for: engines
are independent, managers are wired to the engine that built them, and the current engine
can be rebound for a scope without leaking into the rest of the process.
"""

import asyncio
import threading
from collections.abc import Iterator

import pytest

from griptape_nodes.exe_types.core_types import Parameter
from griptape_nodes.exe_types.node_types import BaseNode, NodeResolutionState
from griptape_nodes.node_library.library_registry import (
    LibraryMetadata,
    LibraryRegistry,
    LibrarySchema,
    NodeMetadata,
)
from griptape_nodes.retained_mode.engine import (
    Engine,
    EngineScoped,
    current_engine,
    engine_scope,
    has_current_engine,
    reset_root_engine,
)
from griptape_nodes.retained_mode.events.app_events import AppEndSessionRequest, AppEndSessionResultSuccess
from griptape_nodes.retained_mode.events.base_events import (
    EventResultSuccess,
    ExecutionEvent,
    ExecutionGriptapeNodeEvent,
    GriptapeNodeEvent,
    ProgressEvent,
)
from griptape_nodes.retained_mode.events.execution_events import ControlFlowCancelledEvent
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes


class _BareNode(BaseNode):
    """Concrete BaseNode used to exercise engine binding without a library round-trip."""

    def process(self) -> None:
        return None


class _ParamDeclaringNode(BaseNode):
    """Concrete BaseNode that declares a parameter from `__init__`, the way real nodes do.

    `add_parameter` emits an `AlterElementEvent` unconditionally, so a node built through this
    class exercises whether that construction-time emission resolves to the engine constructing
    it or falls back to the process engine.
    """

    def __init__(self, name: str, metadata: dict | None = None) -> None:
        super().__init__(name=name, metadata=metadata)
        self.add_parameter(Parameter(name="value", type="str", tooltip="test"))

    def process(self) -> None:
        return None


_PARAM_DECLARING_LIBRARY_NAME = "node-construction-engine-binding-test-library"


def _register_param_declaring_node_library() -> None:
    """Register `_ParamDeclaringNode` under a throwaway library so `create_node` can build it."""
    schema = LibrarySchema(
        name=_PARAM_DECLARING_LIBRARY_NAME,
        library_schema_version=LibrarySchema.LATEST_SCHEMA_VERSION,
        metadata=LibraryMetadata(
            author="test",
            description="node construction engine binding test library",
            library_version="1.0.0",
            engine_version="1.0.0",
            tags=[],
        ),
        categories=[],
        nodes=[],
    )
    library = LibraryRegistry.generate_new_library(library_data=schema)
    library.register_new_node_type(
        _ParamDeclaringNode,
        NodeMetadata(category="test", description="probe", display_name="Probe"),
    )


def _identify(engine: Engine, name: str) -> Engine:
    """Give `engine` a distinguishable engine and session id.

    Engines built in one process read the same identity files, so they start out with
    identical ids. These tests care about which engine stamped an event, so each one
    needs an id of its own.
    """
    engine.engine_identity_manager.active_engine_id = f"engine-{name}"
    engine.session_manager.active_session_id = f"session-{name}"
    return engine


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


class TestNodeEngineBinding:
    """A node emits onto its own engine's queue, not the process root's.

    A node executing under engine B must never put its events on engine A's queue.
    `LibraryRegistry` binds the owning engine at creation to guarantee this.
    """

    def test_node_falls_back_to_the_process_engine_when_unbound(self) -> None:
        """Directly constructed nodes (tests, subclass `__init__` bodies) still resolve an engine."""
        node = _BareNode(name="unbound")

        assert node._engine is None
        assert node.engine is current_engine()

    def test_bound_node_reports_its_own_engine(self) -> None:
        engine = Engine()
        node = _BareNode(name="bound")
        node._engine = engine

        assert node.engine is engine
        assert node.engine is not current_engine()

    @pytest.mark.asyncio
    async def test_node_emits_onto_its_own_engines_queue(self) -> None:
        """Two engines, two nodes: neither node's events land on the other's queue."""
        first = Engine()
        second = Engine()
        first.event_manager.initialize_queue(asyncio.Queue())
        second.event_manager.initialize_queue(asyncio.Queue())

        first_node = _BareNode(name="first_node")
        first_node._engine = first
        second_node = _BareNode(name="second_node")
        second_node._engine = second

        # make_node_unresolved only emits when the current state is in the trigger set.
        first_node.state = NodeResolutionState.RESOLVED
        second_node.state = NodeResolutionState.RESOLVED

        first_node.make_node_unresolved({NodeResolutionState.RESOLVED})
        second_node.make_node_unresolved({NodeResolutionState.RESOLVED})

        first_event = first.event_manager.event_queue.get_nowait()
        second_event = second.event_manager.event_queue.get_nowait()

        assert first_event.wrapped_event.payload.node_name == "first_node"
        assert second_event.wrapped_event.payload.node_name == "second_node"
        assert first.event_manager.event_queue.empty()
        assert second.event_manager.event_queue.empty()

    def test_create_node_binds_the_node_to_the_supplied_engine(self) -> None:
        """`create_node` binds a supplied engine; a node created without one falls back to the process engine."""
        LibraryRegistry._clear()
        _register_param_declaring_node_library()
        engine = Engine()
        try:
            node = LibraryRegistry.create_node(
                node_type=_ParamDeclaringNode.__name__,
                name="bound-via-create-node",
                specific_library_name=_PARAM_DECLARING_LIBRARY_NAME,
                engine=engine,
            )

            assert node.engine is engine

            unbound_node = LibraryRegistry.create_node(
                node_type=_ParamDeclaringNode.__name__,
                name="unbound-via-create-node",
                specific_library_name=_PARAM_DECLARING_LIBRARY_NAME,
            )

            assert unbound_node._engine is None
            assert unbound_node.engine is current_engine()
        finally:
            LibraryRegistry._clear()

    def test_nested_constructing_node_without_an_engine_inherits_the_outer_one(self) -> None:
        """Omitting `engine` on a nested `constructing_node()` inherits the outer call's engine.

        `LibraryRegistry.constructing_node()` is the tool for any construction site that
        bypasses `create_node`, so a node built from inside another node's `__init__` (a helper
        or reference node, say) has to keep resolving to the outer engine rather than falling
        back to the process engine just because the inner call didn't repeat it.
        """
        LibraryRegistry._clear()
        _register_param_declaring_node_library()
        engine = Engine()
        try:
            with LibraryRegistry.constructing_node(engine=engine), LibraryRegistry.constructing_node():
                node = _ParamDeclaringNode(name="nested")

            assert node.engine is engine
        finally:
            LibraryRegistry._clear()

    @pytest.mark.asyncio
    async def test_construction_time_parameter_events_reach_the_creating_engine(self) -> None:
        """A node's own `__init__` emits parameter events onto the engine creating it.

        `add_parameter` fires an `AlterElementEvent` unconditionally, and declaring parameters
        from `__init__` is how nodes are normally written, so this covers the construction
        window rather than just the post-construction `node.engine` getter.
        """
        LibraryRegistry._clear()
        _register_param_declaring_node_library()
        creating_engine = Engine()
        other_engine = Engine()
        creating_engine.event_manager.initialize_queue(asyncio.Queue())
        other_engine.event_manager.initialize_queue(asyncio.Queue())
        try:
            node = LibraryRegistry.create_node(
                node_type=_ParamDeclaringNode.__name__,
                name="declares-a-parameter",
                specific_library_name=_PARAM_DECLARING_LIBRARY_NAME,
                engine=creating_engine,
            )

            # add_parameter's own emit and add_child's emit (from add_node_element) both fire for
            # a single add_parameter call, so drain everything rather than assume exactly one event.
            emitted_events = []
            while not creating_engine.event_manager.event_queue.empty():
                emitted_events.append(creating_engine.event_manager.event_queue.get_nowait())

            assert node.engine is creating_engine
            assert len(emitted_events) > 0
            assert all(
                event.wrapped_event.payload.element_details["node_name"] == "declares-a-parameter"
                for event in emitted_events
            )
            assert other_engine.event_manager.event_queue.empty()
        finally:
            LibraryRegistry._clear()


class TestEventIdentity:
    """Events must be attributed to the engine that emitted them, not to whoever booted last.

    An event carries no identity at construction; the emitting engine's `EventManager` stamps
    it on the way out, and constructing a second engine must never restamp events already
    attributed to the first.
    """

    def test_event_starts_with_no_identity(self) -> None:
        """Nothing may be attributed to an engine until an engine actually claims it."""
        event = ExecutionEvent(payload=ControlFlowCancelledEvent())

        assert event.engine_id is None
        assert event.session_id is None

    def test_each_engine_stamps_its_own_identity(self) -> None:
        first = _identify(Engine(), "a")
        second = _identify(Engine(), "b")

        first_event = ExecutionEvent(payload=ControlFlowCancelledEvent())
        second_event = ExecutionEvent(payload=ControlFlowCancelledEvent())

        first.event_manager.stamp_event_identity(first_event)
        second.event_manager.stamp_event_identity(second_event)

        assert (first_event.engine_id, first_event.session_id) == ("engine-a", "session-a")
        assert (second_event.engine_id, second_event.session_id) == ("engine-b", "session-b")

    def test_building_a_second_engine_does_not_restamp_the_first(self) -> None:
        """Constructing a second engine must not reattribute events already stamped by the first."""
        first = _identify(Engine(), "a")
        event = ExecutionEvent(payload=ControlFlowCancelledEvent())
        first.event_manager.stamp_event_identity(event)

        _identify(Engine(), "b")

        assert (event.engine_id, event.session_id) == ("engine-a", "session-a")

    def test_stamping_does_not_overwrite_an_existing_identity(self) -> None:
        """A forwarded event keeps its origin instead of being claimed by the relay."""
        relay = _identify(Engine(), "relay")
        event = ExecutionEvent(payload=ControlFlowCancelledEvent())
        event.engine_id = "engine-origin"
        event.session_id = "session-origin"

        relay.event_manager.stamp_event_identity(event)

        assert (event.engine_id, event.session_id) == ("engine-origin", "session-origin")

    def test_stamping_reaches_the_wrapped_execution_event(self) -> None:
        """The transport publishes `wrapped_event`, so the inner event is what needs identity."""
        engine = _identify(Engine(), "a")
        inner = ExecutionEvent(payload=ControlFlowCancelledEvent())
        wrapper = ExecutionGriptapeNodeEvent(wrapped_event=inner)

        engine.event_manager.stamp_event_identity(wrapper)

        assert (inner.engine_id, inner.session_id) == ("engine-a", "session-a")

    def test_stamping_reaches_the_wrapped_result_event(self) -> None:
        engine = _identify(Engine(), "a")
        request = AppEndSessionRequest()
        inner = EventResultSuccess(
            request=request,
            result=AppEndSessionResultSuccess(session_id="session-a", result_details="Session ended"),
        )
        wrapper = GriptapeNodeEvent(wrapped_event=inner)

        engine.event_manager.stamp_event_identity(wrapper)

        assert (inner.engine_id, inner.session_id) == ("engine-a", "session-a")

    def test_non_event_items_on_the_queue_are_left_alone(self) -> None:
        """`ProgressEvent` is a plain dataclass with no identity fields; stamping must not raise."""
        engine = _identify(Engine(), "a")
        progress = ProgressEvent(value=1, node_name="node", parameter_name="param")

        engine.event_manager.stamp_event_identity(progress)

        assert not hasattr(progress, "engine_id")

    @pytest.mark.asyncio
    async def test_queued_events_are_stamped_on_the_way_out(self) -> None:
        """`put_event` is the chokepoint for everything the engine emits through its queue.

        Async because `initialize_queue` defers queue creation until a loop is running.
        """
        engine = _identify(Engine(), "a")
        engine.event_manager.initialize_queue()
        event = ExecutionGriptapeNodeEvent(wrapped_event=ExecutionEvent(payload=ControlFlowCancelledEvent()))

        engine.event_manager.put_event(event)

        assert engine.event_manager.event_queue.get_nowait() is event
        assert (event.engine_id, event.session_id) == ("engine-a", "session-a")
        assert (event.wrapped_event.engine_id, event.wrapped_event.session_id) == ("engine-a", "session-a")


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
