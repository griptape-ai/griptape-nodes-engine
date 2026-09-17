"""When a held object is released, and when it must not be.

A matrix over the design rather than over the implementation: three kinds of parameter (ordinary, a handle
whose key the engine mints per assignment, a resource the library keyed itself) crossed with every event
that could end a key's life -- the producer runs again, the connection goes, the producer is deleted, the
consumer is deleted, workflow state is cleared.

The rule they pin: release when a fresher value exists, or when nothing refers to the key any more. Never
while a consumer still holds it. A key is a value, not an edge, so a consumer keeps its copy after the
connection goes and can still run with what it has.
"""

import logging
from unittest.mock import MagicMock

import pytest

from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
from griptape_nodes.exe_types.node_types import BaseNode
from griptape_nodes.retained_mode.engine import Engine
from griptape_nodes.retained_mode.events.connection_events import (
    CreateConnectionRequest,
    CreateConnectionResultSuccess,
    DeleteConnectionRequest,
    DeleteConnectionResultSuccess,
)
from griptape_nodes.retained_mode.events.flow_events import CreateFlowRequest, CreateFlowResultSuccess
from griptape_nodes.retained_mode.events.node_events import (
    DeleteNodeRequest,
    DeleteNodeResultSuccess,
    SerializedParameterValueTracker,
)
from griptape_nodes.retained_mode.events.object_events import ClearAllObjectStateRequest

# A node built outside library registration shares this namespace.
OWNER = "<unregistered>"

# The ordinary output value carried alongside each handle, for contrast.
_STEPS = 20


class Held:
    def __init__(self, label: str) -> None:
        self.label = label


class _Producer(BaseNode):
    """A handle output whose key the engine mints, plus an ordinary output for contrast."""

    def __init__(self, name: str) -> None:
        super().__init__(name=name)
        self.released: list[str] = []
        self.add_parameter(
            Parameter(
                name="latent",
                output_type="handle[Latent]",
                tooltip="",
                allowed_modes={ParameterMode.OUTPUT},
                on_local_object_drop=lambda value: self.released.append(value.label),
            )
        )
        self.add_parameter(Parameter(name="steps", output_type="int", tooltip="", allowed_modes={ParameterMode.OUTPUT}))

    def process(self) -> None:
        return None


class _Consumer(BaseNode):
    def __init__(self, name: str) -> None:
        super().__init__(name=name)
        self.add_parameter(
            Parameter(name="latent", input_types=["handle[Latent]"], tooltip="", allowed_modes={ParameterMode.INPUT})
        )
        self.add_parameter(Parameter(name="steps", input_types=["int"], tooltip=""))

    def process(self) -> None:
        return None


@pytest.fixture
def flow_name(engine: Engine) -> str:
    """A flow to hang the nodes off, inside an active workflow."""
    engine.context_manager.push_workflow("wf")
    result = engine.handle_request(CreateFlowRequest(parent_flow_name=None))
    assert isinstance(result, CreateFlowResultSuccess)
    return result.flow_name


def _add[NodeT: BaseNode](engine: Engine, node: NodeT, flow_name: str) -> NodeT:
    """Register a hand-built node the way CreateNodeRequest does, with no library to create it from."""
    engine.flow_manager.get_flow_by_name(flow_name).add_node(node)
    engine.object_manager.add_object_by_name(node.name, node)
    engine.node_manager._name_to_parent_flow_name[node.name] = flow_name
    return node


def _connect(engine: Engine, parameter_name: str) -> None:
    result = engine.handle_request(
        CreateConnectionRequest(
            source_node_name="Producer",
            source_parameter_name=parameter_name,
            target_node_name="Consumer",
            target_parameter_name=parameter_name,
        )
    )
    assert isinstance(result, CreateConnectionResultSuccess)


def _disconnect(engine: Engine, parameter_name: str) -> None:
    result = engine.handle_request(
        DeleteConnectionRequest(
            source_node_name="Producer",
            source_parameter_name=parameter_name,
            target_node_name="Consumer",
            target_parameter_name=parameter_name,
        )
    )
    assert isinstance(result, DeleteConnectionResultSuccess)


def _is_held(engine: Engine, key: str) -> bool:
    return engine.resource_manager.get_local_object(key, owner=OWNER) is not None


@pytest.fixture
def graph(engine: Engine, flow_name: str) -> tuple[_Producer, _Consumer]:
    """Producer.latent -> Consumer.latent and Producer.steps -> Consumer.steps, both connected."""
    producer = _add(engine, _Producer(name="Producer"), flow_name)
    consumer = _add(engine, _Consumer(name="Consumer"), flow_name)
    _connect(engine, "latent")
    _connect(engine, "steps")
    return producer, consumer  # type: ignore[return-value]


def _produce(producer: _Producer, consumer: _Consumer, label: str) -> str:
    """Run the producer once and deliver both values to the consumer, as resolution would."""
    producer.parameter_output_values["latent"] = Held(label)
    producer.parameter_output_values["steps"] = _STEPS
    key = producer.parameter_output_values["latent"]
    consumer.set_parameter_value("latent", key)
    consumer.set_parameter_value("steps", producer.parameter_output_values["steps"])
    return key


class TestTheProducerRunsAgain:
    def test_the_key_changes_so_the_editor_hears_about_it(self, graph: tuple) -> None:
        producer, consumer = graph

        first = _produce(producer, consumer, "first")
        second = _produce(producer, consumer, "second")

        assert first != second

    def test_the_displaced_object_goes_even_though_the_consumer_held_that_key(
        self, engine: Engine, graph: tuple
    ) -> None:
        """The producer replaced it, so a fresher value exists and the consumer's copy was already stale."""
        producer, consumer = graph
        first = _produce(producer, consumer, "first")

        second = _produce(producer, consumer, "second")

        assert producer.released == ["first"]
        assert not _is_held(engine, first)
        assert consumer.parameter_values["latent"] == second
        assert consumer.resolve_handle("latent").label == "second"

    def test_the_ordinary_output_is_untouched(self, graph: tuple) -> None:
        producer, consumer = graph

        _produce(producer, consumer, "first")

        assert producer.parameter_output_values["steps"] == _STEPS
        assert consumer.parameter_values["steps"] == _STEPS


class TestTheProducerIsDeleted:
    def test_the_object_is_not_released_while_the_consumer_takes_it_as_an_input(
        self, engine: Engine, graph: tuple
    ) -> None:
        """Deleting the producer must not free what a consumer refers to.

        The case this design exists for.

        Reachability is judged before the connections come down, because deleting one clears an INPUT-only
        consumer's copy of the key and every key would then look unreferenced.

        The consumer's own value follows the ordinary INPUT-only rule and is cleared with the connection --
        handles are not special-cased there -- so nothing can resolve this object afterwards and it goes at
        the next workflow clear. What matters is that it was not freed underneath a live reference.
        """
        producer, consumer = graph
        key = _produce(producer, consumer, "latent")

        result = engine.handle_request(DeleteNodeRequest(node_name="Producer"))

        assert isinstance(result, DeleteNodeResultSuccess)
        assert producer.released == []
        assert _is_held(engine, key)
        assert consumer.parameter_values.get("latent") != key

    def test_a_property_consumer_can_still_resolve_it_after_the_producer_goes(
        self, engine: Engine, flow_name: str
    ) -> None:
        """A PROPERTY consumer keeps its value on disconnect, so it can still run with the last object.

        Exactly as any parameter allowing PROPERTY behaves; handles are not special-cased.
        """
        producer = _add(engine, _Producer(name="Producer"), flow_name)
        consumer = _add(engine, _Consumer(name="Consumer"), flow_name)
        keeper = Parameter(
            name="kept",
            input_types=["handle[Latent]"],
            tooltip="",
            allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
        )
        consumer.add_parameter(keeper)
        producer.parameter_output_values["latent"] = Held("latent")
        key = producer.parameter_output_values["latent"]
        consumer.set_parameter_value("kept", key)

        engine.handle_request(DeleteNodeRequest(node_name="Producer"))

        assert producer.released == []
        assert consumer.parameter_values["kept"] == key
        assert consumer.resolve_handle("kept").label == "latent"

    def test_the_object_goes_when_nothing_refers_to_the_key(self, engine: Engine, flow_name: str) -> None:
        producer = _add(engine, _Producer(name="Producer"), flow_name)
        producer.parameter_output_values["latent"] = Held("latent")
        key = producer.parameter_output_values["latent"]

        result = engine.handle_request(DeleteNodeRequest(node_name="Producer"))

        assert isinstance(result, DeleteNodeResultSuccess)
        assert producer.released == ["latent"]
        assert not _is_held(engine, key)

    def test_the_object_goes_once_the_last_consumer_has_gone_too(self, engine: Engine, graph: tuple) -> None:
        producer, consumer = graph
        _produce(producer, consumer, "latent")

        engine.handle_request(DeleteNodeRequest(node_name="Consumer"))
        engine.handle_request(DeleteNodeRequest(node_name="Producer"))

        assert producer.released == ["latent"]

    def test_deleting_a_node_holding_nothing_releases_nothing(self, engine: Engine, flow_name: str) -> None:
        producer = _add(engine, _Producer(name="Producer"), flow_name)

        result = engine.handle_request(DeleteNodeRequest(node_name="Producer"))

        assert isinstance(result, DeleteNodeResultSuccess)
        assert producer.released == []


class TestTheConsumerIsDeleted:
    def test_the_object_stays_because_the_producer_still_holds_its_own_output(
        self, engine: Engine, graph: tuple
    ) -> None:
        producer, consumer = graph
        key = _produce(producer, consumer, "latent")

        engine.handle_request(DeleteNodeRequest(node_name="Consumer"))

        assert producer.released == []
        assert _is_held(engine, key)
        assert producer.resolve_handle("latent").label == "latent"


class TestTheConnectionIsDeleted:
    def test_disconnecting_alone_releases_nothing(self, engine: Engine, graph: tuple) -> None:
        """Both nodes are still there, so the producer still refers to what it made."""
        producer, consumer = graph
        key = _produce(producer, consumer, "latent")

        _disconnect(engine, "latent")

        assert producer.released == []
        assert _is_held(engine, key)

    def test_an_input_only_handle_loses_its_value_like_any_input_only_parameter(
        self, engine: Engine, graph: tuple
    ) -> None:
        """Not special-cased for handles: this is what INPUT-only already means on disconnect.

        Once the consumer's copy has gone, deleting the producer releases the object, because nothing refers
        to the key any more.
        """
        producer, consumer = graph
        key = _produce(producer, consumer, "latent")

        _disconnect(engine, "latent")

        assert consumer.parameter_values.get("latent") != key
        engine.handle_request(DeleteNodeRequest(node_name="Producer"))
        assert producer.released == ["latent"]


class TestClearingWorkflowState:
    def test_everything_goes_however_it_was_keyed(self, engine: Engine, graph: tuple) -> None:
        producer, consumer = graph
        _produce(producer, consumer, "latent")
        resource_key = producer.local_objects.put(Held("pipeline"), key="cfg-hash")

        engine.handle_request(ClearAllObjectStateRequest(i_know_what_im_doing=True))

        assert producer.released == ["latent"]
        assert not _is_held(engine, resource_key)


class TestALibraryKeyedResource:
    """`local_objects.put(key=...)` is the library's own slot, not tied to any parameter's value."""

    def test_deleting_the_node_leaves_it_alone(self, engine: Engine, flow_name: str) -> None:
        """No parameter refers to it, so no parameter event ends its life.

        The library holds it until it drops it or the workflow is cleared, which is the point of keying it.
        """
        producer = _add(engine, _Producer(name="Producer"), flow_name)
        released: list[str] = []
        key = producer.local_objects.put(
            Held("pipeline"), key="cfg-hash", on_drop=lambda value: released.append(value.label)
        )

        engine.handle_request(DeleteNodeRequest(node_name="Producer"))

        assert released == []
        assert _is_held(engine, key)

    def test_rebuilding_under_the_same_key_releases_the_old_one(self, engine: Engine, flow_name: str) -> None:
        producer = _add(engine, _Producer(name="Producer"), flow_name)
        released: list[str] = []
        producer.local_objects.put(Held("first"), key="cfg-hash", on_drop=lambda value: released.append(value.label))

        producer.local_objects.put(Held("second"), key="cfg-hash")

        assert released == ["first"]


class TestSaving:
    def test_a_handle_value_is_skipped_without_a_warning(
        self, engine: Engine, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Skipped silently, unlike a value that failed to serialize.

        There is nothing for a library to fix, and the author did not have to remember `serializable=False`.
        """
        producer = _Producer(name="Producer")
        producer.parameter_output_values["latent"] = Held("latent")
        parameter = producer.get_parameter_by_name("latent")
        assert parameter is not None
        assert parameter.serializable is True

        tracker = MagicMock()
        tracker.get_tracker_state.return_value = SerializedParameterValueTracker.TrackerState.NOT_SERIALIZABLE
        caplog.clear()
        caplog.set_level(logging.WARNING, logger="griptape_nodes")

        saved = engine.node_manager.handle_parameter_value_saving(
            parameter=parameter,
            node=producer,
            unique_parameter_uuid_to_values={},
            serialized_parameter_value_tracker=tracker,
            create_node_request=MagicMock(),
            workflow_manager=MagicMock(),
        )

        assert saved is None
        assert [record for record in caplog.records if "Attempted to serialize" in record.message] == []

    def test_an_ordinary_value_still_warns_when_it_will_not_serialize(
        self, engine: Engine, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The handle skip must not have silenced the genuine failure it sits beside."""
        producer = _Producer(name="Producer")
        producer.parameter_output_values["steps"] = _STEPS
        parameter = producer.get_parameter_by_name("steps")
        assert parameter is not None

        tracker = MagicMock()
        tracker.get_tracker_state.return_value = SerializedParameterValueTracker.TrackerState.NOT_SERIALIZABLE
        caplog.clear()
        caplog.set_level(logging.WARNING, logger="griptape_nodes")

        engine.node_manager.handle_parameter_value_saving(
            parameter=parameter,
            node=producer,
            unique_parameter_uuid_to_values={},
            serialized_parameter_value_tracker=tracker,
            create_node_request=MagicMock(),
            workflow_manager=MagicMock(),
        )

        assert [record for record in caplog.records if "Attempted to serialize" in record.message] != []
