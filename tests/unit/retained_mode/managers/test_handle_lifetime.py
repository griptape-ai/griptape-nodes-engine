"""When a held object is released, and when it must not be.

A matrix over the design rather than over the implementation: three kinds of parameter (ordinary, a handle
whose key the engine mints per assignment, a resource the library keyed itself) crossed with every event
that could end a key's life -- the producer runs again, the connection goes, the producer is deleted, the
consumer is deleted, workflow state is cleared.

The rule they pin: release when a fresher value exists, or when nothing refers to the key any more. Never
while a consumer still holds it. A key is a value, not an edge, so a consumer keeps its copy after the
connection goes and can still run with what it has.
"""

from unittest.mock import MagicMock

import pytest

from griptape_nodes.common.parameter_hydration import dehydrate_parameter_values
from griptape_nodes.exe_types.core_types import Parameter, ParameterList, ParameterMode
from griptape_nodes.exe_types.node_types import BaseNode, NodeResolutionState
from griptape_nodes.retained_mode.engine import Engine
from griptape_nodes.retained_mode.events.connection_events import (
    CreateConnectionRequest,
    CreateConnectionResultSuccess,
    DeleteConnectionRequest,
    DeleteConnectionResultSuccess,
)
from griptape_nodes.retained_mode.events.flow_events import CreateFlowRequest, CreateFlowResultSuccess
from griptape_nodes.retained_mode.events.node_events import (
    CreateNodeRequest,
    DeleteNodeRequest,
    DeleteNodeResultSuccess,
    SerializedParameterValueTracker,
)
from griptape_nodes.retained_mode.events.object_events import ClearAllObjectStateRequest
from griptape_nodes.retained_mode.events.parameter_events import (
    GetParameterValueRequest,
    GetParameterValueResultSuccess,
    SetParameterValueRequest,
    SetParameterValueResultFailure,
)

# A node built outside library registration shares this namespace.
OWNER = "<unregistered>"

# The ordinary output value carried alongside each handle, for contrast.
_STEPS = 20


class Held:
    def __init__(self, label: str) -> None:
        self.label = label


class _Producer(BaseNode):
    """A handle output whose key the engine mints, plus an ordinary output for contrast."""

    def __init__(self, name: str, metadata=None) -> None:  # noqa: ANN001
        super().__init__(name=name, metadata=metadata)
        self.released: list[str] = []
        self.add_parameter(
            Parameter(
                name="latent",
                output_type="Latent",
                serializable=False,
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
            Parameter(
                name="latent",
                input_types=["Latent"],
                serializable=False,
                tooltip="",
                allowed_modes={ParameterMode.INPUT},
            )
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


def _egress(node: BaseNode) -> dict:
    """The payload leaving the process, which is where a value that cannot travel becomes a key."""
    return dehydrate_parameter_values(node.parameter_output_values, node=node, are_outputs=True)


def _produce(producer: _Producer, consumer: _Consumer, label: str) -> str:
    """Run the producer once and deliver both values to the consumer across a process boundary."""
    producer.parameter_output_values["latent"] = Held(label)
    producer.parameter_output_values["steps"] = _STEPS
    sent = _egress(producer)
    key = sent["latent"]
    consumer.set_parameter_value("latent", key)
    consumer.set_parameter_value("steps", sent["steps"])
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
        assert consumer.get_parameter_value("latent").label == "second"

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
            input_types=["Latent"],
            serializable=False,
            tooltip="",
            allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
        )
        consumer.add_parameter(keeper)
        producer.parameter_output_values["latent"] = Held("latent")
        key = _egress(producer)["latent"]
        consumer.set_parameter_value("kept", key)

        engine.handle_request(DeleteNodeRequest(node_name="Producer"))

        assert producer.released == []
        assert consumer.parameter_values["kept"] == key
        assert consumer.get_parameter_value("kept").label == "latent"

    def test_the_object_goes_when_nothing_refers_to_the_key(self, engine: Engine, flow_name: str) -> None:
        producer = _add(engine, _Producer(name="Producer"), flow_name)
        producer.parameter_output_values["latent"] = Held("latent")
        key = _egress(producer)["latent"]

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
        resolved = producer.local_objects.get(_egress(producer)["latent"])
        assert resolved is not None
        assert resolved.label == "latent"


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
    def test_a_handle_key_is_never_written_into_a_saved_workflow(self, engine: Engine) -> None:
        """A key must not survive into a saved workflow, and the node must come back UNRESOLVED.

        Real tracker, real request: a key is a picklable string, so the hashing path would happily
        serialize it, and the reloaded workflow would hold a key into a process that no longer exists.
        """
        producer = _Producer(name="Producer")
        producer.parameter_output_values["latent"] = Held("latent")
        _egress(producer)
        parameter = producer.get_parameter_by_name("latent")
        assert parameter is not None
        # The author never set it: declaring the type handle[...] is what forced it off, which is why
        # to_dict and the serializer cannot disagree about this parameter.
        assert parameter.serializable is False

        tracker = SerializedParameterValueTracker()
        uuid_to_values: dict = {}
        create_node_request = CreateNodeRequest(
            node_type="_Producer", node_name="Producer", resolution=NodeResolutionState.RESOLVED.value
        )

        saved = engine.node_manager.handle_parameter_value_saving(
            parameter=parameter,
            node=producer,
            unique_parameter_uuid_to_values=uuid_to_values,
            serialized_parameter_value_tracker=tracker,
            create_node_request=create_node_request,
            workflow_manager=MagicMock(),
        )

        assert saved is None
        assert uuid_to_values == {}
        assert create_node_request.resolution == NodeResolutionState.UNRESOLVED.value

    def test_an_ordinary_value_still_serializes(self, engine: Engine) -> None:
        """The handle gate sits ahead of the hashing now, and must not have eaten everyone else's path."""
        producer = _Producer(name="Producer")
        producer.parameter_values["steps"] = _STEPS
        parameter = producer.get_parameter_by_name("steps")
        assert parameter is not None

        saved = engine.node_manager.handle_parameter_value_saving(
            parameter=parameter,
            node=producer,
            unique_parameter_uuid_to_values={},
            serialized_parameter_value_tracker=SerializedParameterValueTracker(),
            create_node_request=CreateNodeRequest(
                node_type="_Producer", node_name="Producer", resolution=NodeResolutionState.RESOLVED.value
            ),
            workflow_manager=MagicMock(),
        )

        assert saved is not None


class TestTheRealRunPathReleases:
    """The release cannot depend on the old key still being in the dict when the new one is written.

    The engine clears outputs before re-dispatching a node, through several paths. The slot carries the
    identity instead.
    """

    def test_clear_then_repark_releases_the_previous_object(self, graph: tuple) -> None:
        """parallel_resolution.silent_clear()s outputs before dispatch; control_flow calls clear_node()."""
        producer, consumer = graph
        _produce(producer, consumer, "first")

        producer.parameter_output_values.silent_clear()
        _produce(producer, consumer, "second")

        assert producer.released == ["first"]

    def test_clear_node_then_repark_releases_the_previous_object(self, graph: tuple) -> None:
        producer, consumer = graph
        _produce(producer, consumer, "first")

        producer.clear_node()
        _produce(producer, consumer, "second")

        assert producer.released == ["first"]

    def test_a_fresh_node_instance_reparking_the_same_slot_displaces(self, engine: Engine) -> None:  # noqa: ARG002 (the store the fixture builds is the subject)
        """Worker semantics: the previous run's object is displaced by slot, wherever it lives.

        The transient node is rebuilt every execution, but the store is process-level.
        """
        first_run = _Producer(name="Producer")
        first_run.parameter_output_values["latent"] = Held("first")
        _egress(first_run)

        # ExecuteNodeRequest carries dict(node.metadata), which is what makes the identity stable
        # across transient instances; a node with different metadata is a different node.
        second_run = _Producer(name="Producer", metadata=dict(first_run.metadata))
        second_run.parameter_output_values["latent"] = Held("second")
        _egress(second_run)

        # The hook captured at park time belongs to the first instance, and it is the displaced one.
        assert first_run.released == ["first"]
        assert second_run.released == []


class TestARelayDoesNotReleaseItsUpstream:
    def test_reparking_after_a_pass_through_leaves_the_producers_object_alone(
        self, engine: Engine, flow_name: str
    ) -> None:
        """Only the relay's own slot is displaced when it parks -- never the producer's.

        A relay's output value can be its upstream's key, and the producer's consumers are still live.
        """
        producer = _add(engine, _Producer(name="Producer"), flow_name)
        relay = _add(engine, _Producer(name="Relay"), flow_name)
        producer.parameter_output_values["latent"] = Held("upstream")
        upstream_key = _egress(producer)["latent"]

        relay.parameter_output_values["latent"] = upstream_key
        _egress(relay)
        relay.parameter_output_values.silent_clear()
        relay.parameter_output_values["latent"] = Held("relay-made")
        _egress(relay)

        assert producer.released == []
        assert _is_held(engine, upstream_key)
        still_held = producer.local_objects.get(upstream_key)
        assert still_held is not None
        assert still_held.label == "upstream"


class TestReferencesTheScanMustSee:
    """What counts as "something still refers to this key", beyond a bare value on another node."""

    def test_a_bystander_holding_a_list_value_does_not_break_the_delete(self, engine: Engine, flow_name: str) -> None:
        """List and dict parameter values are everywhere, and comparing sets of them raises.

        Without this the artist cannot delete any node in a graph that has one.
        """
        producer = _add(engine, _Producer(name="Producer"), flow_name)
        bystander = _add(engine, _Consumer(name="Bystander"), flow_name)
        bystander.add_parameter(Parameter(name="tags", input_types=["list"], tooltip=""))
        bystander.set_parameter_value("tags", ["a", "b"])
        producer.parameter_output_values["latent"] = Held("latent")
        _egress(producer)

        result = engine.handle_request(DeleteNodeRequest(node_name="Producer"))

        assert isinstance(result, DeleteNodeResultSuccess)
        assert producer.released == ["latent"]

    def test_a_key_inside_a_list_value_still_counts_as_a_reference(self, engine: Engine, flow_name: str) -> None:
        """A ParameterList consumer holds `[key]`, not `key`, and that object is just as live."""
        producer = _add(engine, _Producer(name="Producer"), flow_name)
        consumer = _add(engine, _Consumer(name="Consumer"), flow_name)
        consumer.add_parameter(Parameter(name="latents", input_types=["list"], tooltip=""))
        producer.parameter_output_values["latent"] = Held("latent")
        key = _egress(producer)["latent"]
        consumer.set_parameter_value("latents", [key])

        engine.handle_request(DeleteNodeRequest(node_name="Producer"))

        assert producer.released == []
        assert _is_held(engine, key)

    def test_a_key_inside_a_dict_value_still_counts_as_a_reference(self, engine: Engine, flow_name: str) -> None:
        producer = _add(engine, _Producer(name="Producer"), flow_name)
        consumer = _add(engine, _Consumer(name="Consumer"), flow_name)
        consumer.add_parameter(Parameter(name="bundle", input_types=["dict"], tooltip=""))
        producer.parameter_output_values["latent"] = Held("latent")
        key = _egress(producer)["latent"]
        consumer.set_parameter_value("bundle", {"latent": key})

        engine.handle_request(DeleteNodeRequest(node_name="Producer"))

        assert producer.released == []
        assert _is_held(engine, key)

    def test_an_undeclared_consumer_is_still_the_last_reference(self, engine: Engine, flow_name: str) -> None:
        """The normal consumer shape: only the producer declares the flag, so the consumer declares nothing.

        Gating candidate collection on the consumer's own declaration meant deleting the last thing
        referring to an object released nothing, and it waited for workflow teardown.
        """
        producer = _add(engine, _Producer(name="Producer"), flow_name)
        consumer = _add(engine, _Consumer(name="Consumer"), flow_name)
        consumer.add_parameter(
            Parameter(
                name="plain",
                input_types=["Latent"],
                tooltip="",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        producer.parameter_output_values["latent"] = Held("latent")
        key = _egress(producer)["latent"]
        consumer.set_parameter_value("plain", key)
        engine.handle_request(DeleteNodeRequest(node_name="Producer"))
        assert _is_held(engine, key)

        engine.handle_request(DeleteNodeRequest(node_name="Consumer"))

        assert not _is_held(engine, key)

    def test_a_key_nested_in_a_consumers_list_value_is_still_collected(self, engine: Engine, flow_name: str) -> None:
        """A ParameterList consumer holds `[key]`, so a scan that only looks at the value itself misses it."""
        producer = _add(engine, _Producer(name="Producer"), flow_name)
        consumer = _add(engine, _Consumer(name="Consumer"), flow_name)
        consumer.add_parameter(Parameter(name="rows", input_types=["list"], tooltip=""))
        producer.parameter_output_values["latent"] = Held("latent")
        key = _egress(producer)["latent"]
        consumer.set_parameter_value("rows", [key])
        engine.handle_request(DeleteNodeRequest(node_name="Producer"))
        assert _is_held(engine, key)

        engine.handle_request(DeleteNodeRequest(node_name="Consumer"))

        assert not _is_held(engine, key)

    def test_deleting_the_last_consumer_releases_what_only_it_held(self, engine: Engine, flow_name: str) -> None:
        """A PROPERTY consumer keeps its copy after its producer goes, and is then the only reference.

        Harvesting only outputs would leave a multi-gigabyte object resident with nothing able to name it.
        """
        producer = _add(engine, _Producer(name="Producer"), flow_name)
        consumer = _add(engine, _Consumer(name="Consumer"), flow_name)
        released: list[str] = []
        consumer.add_parameter(
            Parameter(
                name="kept",
                input_types=["Latent"],
                serializable=False,
                tooltip="",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
                on_local_object_drop=lambda value: released.append(value.label),
            )
        )
        producer.parameter_output_values["latent"] = Held("latent")
        key = _egress(producer)["latent"]
        consumer.set_parameter_value("kept", key)
        engine.handle_request(DeleteNodeRequest(node_name="Producer"))
        assert _is_held(engine, key)

        engine.handle_request(DeleteNodeRequest(node_name="Consumer"))

        assert not _is_held(engine, key)


class TestALibraryKeyIsNotTheEnginesToRelease:
    """The engine releases only what it parked. A library's own key stays the library's."""

    def test_displacing_a_library_key_from_a_handle_output_leaves_the_resource_alone(
        self, engine: Engine, flow_name: str
    ) -> None:
        """Handing a cached pipeline downstream means putting its key in a handle output.

        The key is stable by construction, so releasing it would drop that resource in every worker holding
        it, and any other node naming it would fail with "no longer available".
        """
        builder = _add(engine, _Producer(name="Producer"), flow_name)
        released: list[str] = []
        resource_key = builder.local_objects.put(
            Held("pipe-v1"), key="cfg-1", on_drop=lambda value: released.append(value.label)
        )

        builder.parameter_output_values["latent"] = resource_key
        _egress(builder)
        builder.parameter_output_values["latent"] = builder.local_objects.put(Held("pipe-v2"), key="cfg-2")

        assert released == []
        assert _is_held(engine, resource_key)

    def test_a_library_key_that_looks_engine_minted_is_still_refused(self, engine: Engine, flow_name: str) -> None:
        """Provenance is recorded on the entry, not inferred from the key's shape.

        A model id with a version dot and a short config hash is a realistic library key that matches the
        minted shape exactly.
        """
        builder = _add(engine, _Producer(name="Producer"), flow_name)
        released: list[str] = []
        lookalike = builder.local_objects.put(
            Held("shared"), key="sd-xl-1.0#a1b2c3d4", on_drop=lambda value: released.append(value.label)
        )

        assert builder.local_objects.is_parked_by_engine(lookalike) is False
        assert builder.local_objects.release_parked(lookalike) is False
        assert released == []
        assert _is_held(engine, lookalike)

    def test_deleting_the_node_leaves_a_library_key_it_output_alone(self, engine: Engine, flow_name: str) -> None:
        builder = _add(engine, _Producer(name="Producer"), flow_name)
        released: list[str] = []
        resource_key = builder.local_objects.put(
            Held("pipe"), key="cfg-1", on_drop=lambda value: released.append(value.label)
        )
        builder.parameter_output_values["latent"] = resource_key
        _egress(builder)

        engine.handle_request(DeleteNodeRequest(node_name="Producer"))

        assert released == []
        assert _is_held(engine, resource_key)


class TestAssigningNothing:
    def test_none_passes_through_and_frees_what_the_slot_held(self) -> None:
        """A run that produces nothing releases what the last run produced, and the consumer reads None."""
        producer = _Producer(name="Producer")
        producer.parameter_output_values["latent"] = Held("only-run")
        _egress(producer)

        producer.parameter_output_values.silent_clear()
        producer.parameter_output_values["latent"] = None
        _egress(producer)

        assert producer.released == ["only-run"]
        assert producer.parameter_output_values["latent"] is None


class TestOneObjectInSeveralEntries:
    """Displacing one entry must not tear down an object another entry still hands out."""

    def test_reparking_one_of_two_outputs_holding_the_same_object(self, engine: Engine, flow_name: str) -> None:
        producer = _add(engine, _Producer(name="Producer"), flow_name)
        producer.add_parameter(
            Parameter(
                name="latent_also",
                output_type="Latent",
                serializable=False,
                tooltip="",
                allowed_modes={ParameterMode.OUTPUT},
                on_local_object_drop=lambda value: producer.released.append(value.label),
            )
        )
        shared = Held("shared")
        producer.parameter_output_values["latent"] = shared
        producer.parameter_output_values["latent_also"] = shared
        _egress(producer)
        second_key = _egress(producer)["latent_also"]

        producer.parameter_output_values.silent_clear()
        producer.parameter_output_values["latent"] = Held("fresh")
        _egress(producer)

        assert producer.released == []
        assert engine.resource_manager.get_local_object(second_key, owner=OWNER) is shared

    def test_reparking_leaves_the_library_cached_copy_alone(self, engine: Engine, flow_name: str) -> None:
        """A library caches the pipeline under a config hash AND assigns it to a handle output.

        The sanctioned pattern is to output the cache key, but nothing enforces that, and displacing the
        parked entry must not free what `local_objects.get(cfg)` still returns.
        """
        producer = _add(engine, _Producer(name="Producer"), flow_name)
        released: list[str] = []
        pipe = Held("pipe-v1")
        cache_key = producer.local_objects.put(pipe, key="cfg-1", on_drop=lambda value: released.append(value.label))
        producer.parameter_output_values["latent"] = pipe

        producer.parameter_output_values.silent_clear()
        producer.parameter_output_values["latent"] = Held("pipe-v2")
        _egress(producer)

        assert released == []
        assert producer.released == []
        assert producer.local_objects.get(cache_key) is pipe


class TestAHookRunsOncePerObject:
    """Freeing is per object, not per entry: one object in several entries gets one teardown."""

    def _with_second_output(self, producer: _Producer) -> None:
        producer.add_parameter(
            Parameter(
                name="latent_also",
                output_type="Latent",
                serializable=False,
                tooltip="",
                allowed_modes={ParameterMode.OUTPUT},
                on_local_object_drop=lambda value: producer.released.append(value.label),
            )
        )

    def test_deleting_a_node_with_one_object_on_two_outputs(self, engine: Engine, flow_name: str) -> None:
        """A double `del model` + CUDA flush is a crash on a non-idempotent resource."""
        producer = _add(engine, _Producer(name="Producer"), flow_name)
        self._with_second_output(producer)
        shared = Held("shared")
        producer.parameter_output_values["latent"] = shared
        producer.parameter_output_values["latent_also"] = shared
        _egress(producer)

        engine.handle_request(DeleteNodeRequest(node_name="Producer"))

        assert producer.released == ["shared"]

    def test_workflow_teardown_with_one_object_on_two_outputs(self, engine: Engine, flow_name: str) -> None:
        producer = _add(engine, _Producer(name="Producer"), flow_name)
        self._with_second_output(producer)
        shared = Held("shared")
        producer.parameter_output_values["latent"] = shared
        producer.parameter_output_values["latent_also"] = shared
        _egress(producer)

        engine.clear_current_workflow_data()

        assert producer.released == ["shared"]

    def test_deleting_a_node_leaves_its_library_cached_copy_usable(self, engine: Engine, flow_name: str) -> None:
        """The parked entry goes with the node; the cache entry is the library's.

        It keeps handing the object out, so the teardown must not have run.
        """
        producer = _add(engine, _Producer(name="Producer"), flow_name)
        released: list[str] = []
        pipe = Held("pipe")
        cache_key = producer.local_objects.put(pipe, key="cfg-1", on_drop=lambda value: released.append(value.label))
        producer.parameter_output_values["latent"] = pipe

        engine.handle_request(DeleteNodeRequest(node_name="Producer"))

        assert producer.released == []
        assert released == []
        assert engine.resource_manager.get_local_object(cache_key, owner=OWNER) is pipe


class TestAnOrphanedEntryIsStillCollected:
    """The store, not the node's current parameter names, is the authority on what a node holds."""

    def test_removing_the_parameter_then_deleting_the_node_still_releases(self, engine: Engine, flow_name: str) -> None:
        """The parked entry must not outlive the node just because its parameter's name is gone.

        Dynamic-pipeline nodes remove parameters from after_value_set.
        """
        producer = _add(engine, _Producer(name="Producer"), flow_name)
        producer.parameter_output_values["latent"] = Held("orphaned")
        key = _egress(producer)["latent"]
        producer.remove_parameter_element_by_name("latent")

        engine.handle_request(DeleteNodeRequest(node_name="Producer"))

        assert producer.released == ["orphaned"]
        assert not _is_held(engine, key)

    def test_a_cancelled_run_between_clear_and_park_still_releases_on_delete(
        self, engine: Engine, flow_name: str
    ) -> None:
        producer = _add(engine, _Producer(name="Producer"), flow_name)
        producer.parameter_output_values["latent"] = Held("stranded")
        key = _egress(producer)["latent"]
        producer.parameter_output_values.silent_clear()

        engine.handle_request(DeleteNodeRequest(node_name="Producer"))

        assert producer.released == ["stranded"]
        assert not _is_held(engine, key)


class TestAnUnhookedEntryDoesNotSuppressAHookedOne:
    def test_the_hooked_entry_still_tears_the_object_down(self, engine: Engine) -> None:
        """An entry with no hook coming first in the batch must not swallow the second entry's teardown.

        Dedup is per object, but only a run hook marks it done.
        """
        manager = engine.resource_manager
        released: list[str] = []
        shared = Held("shared")
        manager.put_local_object(shared, owner=OWNER, source="S", key="first-no-hook")
        manager.put_local_object(
            shared, owner=OWNER, source="S", key="second-hooked", on_drop=lambda value: released.append(value.label)
        )

        manager.drop_all_local_objects()

        assert released == ["shared"]


class TestBatchTeardownIsOncePerObject:
    def test_an_owner_sweep_with_one_object_in_two_entries_runs_one_hook(self, engine: Engine) -> None:
        """The bulk paths' only exactly-once guard is the batch dedupe; drive one directly to pin it."""
        manager = engine.resource_manager
        released: list[str] = []
        shared = Held("shared")
        for key in ("a", "b"):
            manager.put_local_object(
                shared, owner=OWNER, source="S", key=key, on_drop=lambda value: released.append(value.label)
            )

        manager.drop_objects_for_owner(OWNER)

        assert released == ["shared"]


class TestTheEnginesOwnReadsNeverSeeTheObject:
    """Every path that moves, saves, or reports a value must see the key.

    This is what makes holding objects safe at all: `parameter_values` stays JSON-safe even while a node
    is executing, so a mid-process request that forwards to the orchestrator cannot stringify a pipeline.
    """

    def test_the_editor_is_told_the_key(self, engine: Engine, flow_name: str) -> None:
        """Parameter.to_event builds what the editor receives, and it is json-serialized on the way."""
        producer = _add(engine, _Producer(name="Producer"), flow_name)
        producer.parameter_output_values["latent"] = Held("pipeline")
        key = _egress(producer)["latent"]

        parameter = producer.get_parameter_by_name("latent")
        assert parameter is not None
        event = parameter.to_event(producer)

        assert event["value"] == key or event.get("value") is None

    def test_a_value_query_answers_with_the_key(self, engine: Engine, flow_name: str) -> None:
        """GetParameterValueRequest serves the editor and is forwarded from workers mid-execution."""
        consumer = _add(engine, _Consumer(name="Consumer"), flow_name)
        consumer.set_parameter_value("latent", f"{OWNER}:Producer@abc12345.latent#deadbeef")

        result = engine.handle_request(GetParameterValueRequest(parameter_name="latent", node_name="Consumer"))

        assert isinstance(result, GetParameterValueResultSuccess)
        assert result.value == f"{OWNER}:Producer@abc12345.latent#deadbeef"

    def test_clearing_outputs_reports_keys_not_objects(self, engine: Engine, flow_name: str) -> None:
        """clear() emits a change event per key, and those events reach the editor too."""
        producer = _add(engine, _Producer(name="Producer"), flow_name)
        producer.parameter_output_values["latent"] = Held("pipeline")
        _egress(producer)

        producer.parameter_output_values.clear()

        assert "latent" not in producer.parameter_output_values


class TestSavingAndPublishing:
    """A key means nothing in another process, so neither a save nor a publish may carry one.

    Both leave the node UNRESOLVED instead, and its producer re-runs on load. Publishing behaves the same
    way it did before held values existed: a `serializable=False` parameter was already skipped.
    """

    @pytest.mark.parametrize("publishing", [False, True])
    def test_a_held_value_is_never_written_out(self, engine: Engine, publishing: bool) -> None:  # noqa: FBT001
        producer = _Producer(name="Producer")
        producer.parameter_output_values["latent"] = Held("pipeline")
        _egress(producer)
        parameter = producer.get_parameter_by_name("latent")
        assert parameter is not None
        captured: dict = {}
        request = CreateNodeRequest(
            node_type="_Producer", node_name="Producer", resolution=NodeResolutionState.RESOLVED.value
        )

        saved = engine.node_manager.handle_parameter_value_saving(
            parameter=parameter,
            node=producer,
            unique_parameter_uuid_to_values=captured,
            serialized_parameter_value_tracker=SerializedParameterValueTracker(),
            create_node_request=request,
            workflow_manager=MagicMock(),
            serialize_all_parameter_values=publishing,
        )

        assert saved is None
        assert captured == {}
        assert request.resolution == NodeResolutionState.UNRESOLVED.value


class TestAKeyThatReachedASerializableParameter:
    """The save skip asks the store, not the parameter's flag, and this is why.

    A `serializable=False` parameter is skipped by its own flag. A key that arrives on an ordinary
    parameter -- wired through an `any` input, say -- has nothing stopping it, and a saved workflow would
    carry a reference into a process that no longer exists on a node marked RESOLVED.
    """

    def test_it_is_not_written_into_the_saved_workflow(self, engine: Engine, flow_name: str) -> None:
        producer = _add(engine, _Producer(name="Producer"), flow_name)
        producer.parameter_output_values["latent"] = Held("pipeline")
        key = _egress(producer)["latent"]

        passthrough = _add(engine, _Consumer(name="Passthrough"), flow_name)
        ordinary = Parameter(name="anything", input_types=["any"], tooltip="")
        passthrough.add_parameter(ordinary)
        passthrough.set_parameter_value("anything", key)
        assert ordinary.serializable is True

        request = CreateNodeRequest(
            node_type="_Consumer", node_name="Passthrough", resolution=NodeResolutionState.RESOLVED.value
        )
        captured: dict = {}
        saved = engine.node_manager.handle_parameter_value_saving(
            parameter=ordinary,
            node=passthrough,
            unique_parameter_uuid_to_values=captured,
            serialized_parameter_value_tracker=SerializedParameterValueTracker(),
            create_node_request=request,
            workflow_manager=MagicMock(),
        )

        assert saved is None
        assert captured == {}
        assert request.resolution == NodeResolutionState.UNRESOLVED.value

    def test_the_serializer_reads_the_stored_key(self, engine: Engine, flow_name: str) -> None:
        """The serialization helper reads values to hash them; it must see keys, never objects.

        Through an input value specifically: the helper takes output values from the dict directly, so only
        the input path goes through an accessor and can translate.
        """
        producer = _add(engine, _Producer(name="Producer"), flow_name)
        producer.parameter_output_values["latent"] = Held("pipeline")
        key = _egress(producer)["latent"]
        consumer = _add(engine, _Consumer(name="Consumer"), flow_name)
        consumer.set_parameter_value("latent", key)

        read = engine.node_manager._get_parameter_value_for_serialization(consumer, "latent")

        assert read == key


class TestDeliveringAValueToTheNextNode:
    def test_it_is_stored_as_a_key(self, engine: Engine, flow_name: str) -> None:
        """Resolution delivers values through SetParameterValueRequest.

        What lands in the consumer's parameter_values is the key: that dict is json-serialized on every
        worker dispatch, so an object there would go onto the wire.
        """
        producer = _add(engine, _Producer(name="Producer"), flow_name)
        consumer = _add(engine, _Consumer(name="Consumer"), flow_name)
        producer.parameter_output_values["latent"] = Held("pipeline")
        key = _egress(producer)["latent"]

        result = engine.handle_request(
            SetParameterValueRequest(parameter_name="latent", node_name="Consumer", value=key)
        )

        assert not isinstance(result, SetParameterValueResultFailure)
        assert consumer.parameter_values["latent"] == key
        resolved = consumer.get_parameter_value("latent")
        assert resolved is not None
        assert resolved.label == "pipeline"


class TestANonSerializableContainer:
    def test_it_cannot_be_declared_at_all(self, engine: Engine, flow_name: str) -> None:
        """A container cannot hold a value, so the declaration is refused where it is made.

        It used to be accepted and silently ignored, which meant the author found out at a process
        boundary, after a node had already produced something.
        """
        node = _add(engine, _Producer(name="Producer"), flow_name)
        latents = ParameterList(name="latents", output_type="Latent", tooltip="")
        latents.serializable = False

        with pytest.raises(ValueError, match="cannot hold a value that stays in this process"):
            node.add_parameter(latents)

    def test_an_ordinary_container_holding_objects_is_collected_by_nothing(
        self, engine: Engine, flow_name: str
    ) -> None:
        """Undeclared, a list of objects is nobody's to hold, and the delete scan finds no key in it."""
        node = _add(engine, _Producer(name="Producer"), flow_name)
        node.add_parameter(ParameterList(name="latents", output_type="Latent", tooltip=""))
        batch = [Held("a"), Held("b")]

        node.parameter_output_values["latents"] = batch

        assert node.parameter_output_values["latents"] is batch
        assert engine.node_manager._unreferenced_handle_keys(node) == []
