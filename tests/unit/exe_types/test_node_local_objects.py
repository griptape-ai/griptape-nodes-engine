"""What a library author sees when a value cannot cross a process boundary.

The contract is one declaration: mark the parameter `serializable=False`. A value that cannot be written
into a saved workflow cannot be sent to another process either, so the engine holds it where it was built
and passes a key in its place. Reads and writes stay ordinary -- assign the object, read the object -- and
these tests are written from that author's point of view.

Engine-side lifetime (who frees what, and when) lives in
tests/unit/retained_mode/managers/test_handle_lifetime.py.
"""

import pytest

from griptape_nodes.exe_types.core_types import Parameter, ParameterList, ParameterMode
from griptape_nodes.exe_types.local_objects import cache_outputs_for_egress, caches_its_values
from griptape_nodes.exe_types.node_types import BaseNode


class Pipeline:
    """Stands in for something that cannot cross a process boundary: a loaded model, a latent."""

    def __init__(self, label: str) -> None:
        self.label = label


class _ArrayLike:
    """Stands in for a tensor: truthiness raises above one element, and it is unhashable.

    numpy is not an engine dependency, and those are the two behaviours that break a value check and a
    map lookup respectively.
    """

    __hash__ = None  # type: ignore[assignment]

    def __init__(self, elements: int) -> None:
        self.elements = elements

    def __bool__(self) -> bool:
        if self.elements != 1:
            message = "The truth value of an array with more than one element is ambiguous."
            raise ValueError(message)
        return False


class _LibraryNode(BaseNode):
    def __init__(self, name: str, metadata=None) -> None:  # noqa: ANN001
        super().__init__(name=name, metadata=metadata or {"library": "Diffusers"})

    def process(self) -> None:
        return None


def _producer(name: str = "LoadPipeline", *, on_drop=None) -> _LibraryNode:  # noqa: ANN001
    """A node whose output holds a pipeline: the whole declaration is serializable=False."""
    node = _LibraryNode(name=name)
    node.add_parameter(
        Parameter(
            name="pipeline",
            output_type="Pipeline",
            tooltip="",
            serializable=False,
            allowed_modes={ParameterMode.OUTPUT},
            on_local_object_drop=on_drop,
        )
    )
    return node


def _consumer(name: str = "Generate") -> _LibraryNode:
    node = _LibraryNode(name=name)
    node.add_parameter(
        Parameter(
            name="pipeline",
            input_types=["Pipeline"],
            tooltip="",
            serializable=False,
            allowed_modes={ParameterMode.INPUT},
        )
    )
    return node


def _egress(node: _LibraryNode) -> dict:
    """The payload that leaves the process, which is where a value becomes a key."""
    return cache_outputs_for_egress(node.parameter_output_values, node=node)


def _hand_over(producer: _LibraryNode, consumer: _LibraryNode, param: str = "pipeline") -> None:
    """Deliver the producer's output to the consumer across a process boundary.

    The producer ran in a worker, so what reaches the consumer is whatever survived the trip: a key for
    the pipeline, the value itself for anything that is already data.
    """
    consumer.set_parameter_value(param, _egress(producer)[param])


class TestHandingAnObjectToTheNextNode:
    """The story the feature exists for: a pipeline reaches the next node without crossing the wire."""

    def test_the_consumer_reads_the_object_its_producer_assigned(self) -> None:
        producer, consumer = _producer(), _consumer()
        pipeline = Pipeline("flux")

        producer.parameter_output_values["pipeline"] = pipeline
        _hand_over(producer, consumer)

        assert consumer.get_parameter_value("pipeline") is pipeline

    def test_the_library_never_touches_a_key(self) -> None:
        """Assign an object, read an object. The key exists, but not in anything the author writes."""
        producer, consumer = _producer(), _consumer()

        producer.parameter_output_values["pipeline"] = Pipeline("flux")
        _hand_over(producer, consumer)

        # The producer keeps the object it assigned. Only what crossed became a key.
        assert isinstance(producer.parameter_output_values["pipeline"], Pipeline)
        assert isinstance(_egress(producer)["pipeline"], str)
        assert isinstance(consumer.get_parameter_value("pipeline"), Pipeline)

    def test_the_engine_sees_the_key_where_the_node_sees_the_object(self) -> None:
        """What travels to a worker, into a saved file, or to the editor is the key.

        A value only becomes an object at the point a node reads its own parameter.
        """
        producer, consumer = _producer(), _consumer()
        producer.parameter_output_values["pipeline"] = Pipeline("flux")
        _hand_over(producer, consumer)

        raw = consumer._get_raw_parameter_value("pipeline")

        assert isinstance(raw, str)
        # Namespaced by the worker holding it, not by the library: the cache belongs to the process.
        assert raw.startswith(f"{consumer.local_objects.owner}:")
        assert consumer.get_parameter_value("pipeline") is not raw


class TestTheProducerRunsAgain:
    def test_the_consumer_sees_the_new_object(self) -> None:
        producer, consumer = _producer(), _consumer()

        producer.parameter_output_values["pipeline"] = Pipeline("first")
        _hand_over(producer, consumer)
        assert consumer.get_parameter_value("pipeline").label == "first"

        producer.parameter_output_values.silent_clear()
        producer.parameter_output_values["pipeline"] = Pipeline("second")
        _hand_over(producer, consumer)

        assert consumer.get_parameter_value("pipeline").label == "second"

    def test_the_object_it_replaced_is_freed(self) -> None:
        """The release hook is where a library frees VRAM; dropping the reference would not."""
        released: list[str] = []
        producer = _producer(on_drop=lambda value: released.append(value.label))

        producer.parameter_output_values["pipeline"] = Pipeline("first")
        _egress(producer)
        producer.parameter_output_values.silent_clear()
        producer.parameter_output_values["pipeline"] = Pipeline("second")
        _egress(producer)

        assert released == ["first"]


class TestWhatTheAuthorSeesWhenSomethingIsWrong:
    """Each way of being wrong needs a different fix, so each gets its own answer."""

    def test_the_object_is_gone(self) -> None:
        """A key this worker minted whose object has since been released: re-running the producer is the fix."""
        producer, consumer = _producer(), _consumer()
        producer.parameter_output_values["pipeline"] = Pipeline("flux")
        key = _egress(producer)["pipeline"]
        consumer.set_parameter_value("pipeline", key)
        assert producer.local_objects.drop(key) is True

        with pytest.raises(RuntimeError) as caught:
            consumer.get_parameter_value("pipeline")

        message = str(caught.value)
        assert "no longer available" in message
        assert "parameter 'pipeline'" in message
        assert "Re-run whatever is connected to 'pipeline'." in message

    def test_a_key_from_another_process_does_not_say_re_run(self) -> None:
        """The object may be alive in the worker that made it, so this must not send anyone re-running.

        The orchestrator reading a worker's key is the everyday case: it instantiates the node classes
        too, so anything a library does outside `process` lands here.
        """
        consumer = _consumer()
        # A key from another process can only be here because a worker existed, which is the condition the
        # read's fast path latches on. Without one, no value in this process can name a cached object.
        consumer.local_objects._manager().engine.worker_manager._has_ever_had_a_worker = True
        consumer.set_parameter_value("pipeline", "some-other-worker:LoadPipeline@abc12345.pipeline#deadbeef")

        with pytest.raises(RuntimeError) as caught:
            consumer.get_parameter_value("pipeline")

        message = str(caught.value)
        assert "held in another process" in message
        assert "Re-run" not in message

    def test_nothing_is_connected(self) -> None:
        consumer = _consumer()

        assert consumer.get_parameter_value("pipeline") is None

    @pytest.mark.parametrize("wrong_value", [_ArrayLike(elements=4), _ArrayLike(elements=1), 42])
    def test_a_value_the_engine_never_held_is_returned_untouched(self, wrong_value: object) -> None:
        """A non-serializable parameter may hold something the engine never parked.

        Wiring the object in rather than its key is the likeliest mistake, so this must survive a tensor:
        asking whether a multi-element array is empty raises, and a one-element one answers falsy.
        """
        consumer = _consumer()
        consumer.parameter_values["pipeline"] = wrong_value

        assert consumer.get_parameter_value("pipeline") is wrong_value

    def test_two_libraries_sharing_a_worker_share_its_objects(self) -> None:
        """The cache belongs to the worker, and libraries co-hosted in it genuinely share a process.

        Refusing this would only hold while they happened to be co-hosted, and the object is right there.
        """
        other_library = _LibraryNode(name="TheirLoader", metadata={"library": "SomeoneElse"})
        other_library.add_parameter(Parameter(name="pipeline", output_type="Pipeline", tooltip="", serializable=False))
        theirs = Pipeline("theirs")
        other_library.parameter_output_values["pipeline"] = theirs

        consumer = _consumer()
        consumer.set_parameter_value("pipeline", _egress(other_library)["pipeline"])

        assert consumer.get_parameter_value("pipeline") is theirs

    def test_a_serializable_parameter_keeps_its_value(self) -> None:
        node = _LibraryNode(name="Settings")
        node.add_parameter(Parameter(name="steps", output_type="int", tooltip=""))
        steps = 20

        node.parameter_output_values["steps"] = steps

        assert node.parameter_output_values["steps"] == steps

    def test_a_container_is_not_held(self) -> None:
        """A container is never itself held, whatever it declares.

        Holding a whole list as one object would hand a downstream list one opaque key, and a container has
        nowhere to put a release hook -- its elements are ordinary parameters. The declaration still means
        what it always meant here, "do not persist this", so it is accepted; what it cannot add is holding,
        and the boundary says so if unsendable values in a container ever try to cross.
        """
        node = _LibraryNode(name="Batch")
        latents = ParameterList(name="latents", output_type="Latent", tooltip="")
        # ParameterList does not accept `serializable` through __init__; setting it directly is the only
        # way to reach the case.
        latents.serializable = False
        node.add_parameter(latents)
        batch = [Pipeline("a"), Pipeline("b")]

        node.parameter_output_values["latents"] = batch

        assert caches_its_values(latents) is False
        assert node.parameter_output_values["latents"] is batch


class TestCachingAnExpensiveResourceAcrossRuns:
    """`local_objects` is the one explicit API: a key the library can derive again.

    A worker rebuilds the node for every execution, so without this a 30-second pipeline load repeats on
    every run. The engine never releases these -- the library owns them.
    """

    def test_the_second_run_finds_what_the_first_built(self) -> None:
        node = _producer()
        key = node.local_objects.key_for("flux-config-hash")
        assert node.local_objects.get(key) is None

        built = Pipeline("flux")
        node.local_objects.put(built, key="flux-config-hash")

        next_run = _producer()
        assert next_run.local_objects.get(key) is built

    def test_rebuilding_under_the_same_key_frees_the_old_one(self) -> None:
        released: list[str] = []
        node = _producer()

        node.local_objects.put(Pipeline("v1"), key="cfg", on_drop=lambda value: released.append(value.label))
        node.local_objects.put(Pipeline("v2"), key="cfg")

        assert released == ["v1"]

    def test_a_clear_cache_node_frees_only_its_own_libraries_objects(self) -> None:
        mine = _producer()
        theirs = _producer(name="OtherLibLoader")
        theirs.metadata["library"] = "SomeoneElse"
        my_key = mine.local_objects.put(Pipeline("mine"), key="cfg")
        their_key = theirs.local_objects.put(Pipeline("theirs"), key="cfg")

        assert mine.local_objects.drop_all() == 1

        assert mine.local_objects.get(my_key) is None
        assert theirs.local_objects.get(their_key) is not None


class TestBothDictsCross:
    """Inputs and outputs both leave the process, so both get the pass.

    A write stores the real value either way: nothing is a key until it is about to travel, which is why
    a node can read back what it just assigned.
    """

    def test_a_write_stores_the_object_itself(self) -> None:
        node = _producer()
        node.add_parameter(Parameter(name="incoming", input_types=["Pipeline"], tooltip="", serializable=False))
        pipeline = Pipeline("flux")

        node.set_parameter_value("incoming", pipeline)

        assert node.parameter_values["incoming"] is pipeline
        assert node.get_parameter_value("incoming") is pipeline

    def test_an_input_is_never_cached(self) -> None:
        """Caching an input would mint a key the far side has nothing to resolve against.

        The object is in this process and the node runs in another, so no arrangement gets it there. The
        value is passed through as it always was rather than being turned into an unusable key.
        """
        node = _producer()
        node.add_parameter(Parameter(name="incoming", input_types=["Pipeline"], tooltip="", serializable=False))
        pipeline = Pipeline("flux")
        node.set_parameter_value("incoming", pipeline)

        assert node.parameter_values["incoming"] is pipeline

    def test_a_key_arriving_on_an_input_is_not_parked_again(self) -> None:
        """A consumer receives keys, and re-parking one would wrap the string as though it were an object."""
        producer, consumer = _producer(), _consumer()
        producer.parameter_output_values["pipeline"] = Pipeline("flux")
        key = _egress(producer)["pipeline"]

        consumer.set_parameter_value("pipeline", key)

        assert consumer._get_raw_parameter_value("pipeline") == key


class TestOutputtingACachedResourceKey:
    """The sanctioned way to hand a cached pipeline downstream is to output its key.

    The write path must recognise that key as one this library already holds and leave it alone. Wrapping
    it would park the string as though it were the object: the consumer reads a str where it wants a
    Pipeline, and the release hook runs against the key.
    """

    def test_the_consumer_resolves_the_cached_object(self) -> None:
        producer, consumer = _producer(), _consumer()
        pipeline = Pipeline("flux")
        cache_key = producer.local_objects.put(pipeline, key="flux-config-hash")

        producer.parameter_output_values["pipeline"] = cache_key
        _hand_over(producer, consumer)

        assert producer.parameter_output_values["pipeline"] == cache_key
        assert consumer.get_parameter_value("pipeline") is pipeline

    def test_the_release_hook_is_not_handed_the_key(self) -> None:
        """A real hook does `del value.unet`; against a str it raises into a swallowed log."""
        released: list[object] = []
        producer = _producer(on_drop=released.append)
        pipeline = Pipeline("flux")
        cache_key = producer.local_objects.put(pipeline, key="cfg")

        producer.parameter_output_values["pipeline"] = cache_key
        _egress(producer)
        producer.parameter_output_values.silent_clear()
        producer.parameter_output_values["pipeline"] = Pipeline("second")
        _egress(producer)

        assert all(not isinstance(value, str) for value in released)


class TestAParameterWithAnInputAndAnOutputValue:
    """A parameter can carry both, and only the output side is ever held.

    The engine resets input values after a run whose connection was torn down mid-execution, so that
    write must not disturb the object the node published from the same parameter.
    """

    def test_clearing_the_input_leaves_the_output_object_alone(self) -> None:
        released: list[str] = []
        node = _producer(on_drop=lambda value: released.append(value.label))
        node.add_parameter(
            Parameter(
                name="pipeline_in",
                input_types=["Pipeline"],
                tooltip="",
                serializable=False,
                allowed_modes={ParameterMode.INPUT},
            )
        )
        published = Pipeline("with lora")
        node.parameter_output_values["pipeline"] = published
        key = _egress(node)["pipeline"]

        # What reset_deferred_input_values does after a run whose connection was cut mid-execution.
        node.set_parameter_value("pipeline", None)

        assert released == []
        assert node.local_objects.get(key) is published

    def test_only_the_output_side_is_held(self) -> None:
        node = _producer()
        incoming, outgoing = Pipeline("incoming"), Pipeline("outgoing")

        node.set_parameter_value("pipeline", incoming)
        node.parameter_output_values["pipeline"] = outgoing
        out_key = _egress(node)["pipeline"]

        assert node.local_objects.get(out_key) is outgoing
        # The input value is still the object the node was handed; nothing minted a key for it.
        assert node.get_parameter_value("pipeline") is incoming
        assert node.parameter_values["pipeline"] is incoming


class TestAConsumerThatDeclaresNothing:
    """The shipped shape: only the producer declares the flag.

    `base_driver.py` in the standard library marks its `driver` output serializable=False, while the
    Agent node's `model` input declares nothing and simply expects the driver object. Translation is
    therefore a question about the value, not about the parameter reading it.
    """

    def _plain_consumer(self) -> _LibraryNode:
        node = _LibraryNode(name="Agent")
        node.add_parameter(
            Parameter(name="model", input_types=["str", "Pipeline"], tooltip="", allowed_modes={ParameterMode.INPUT})
        )
        return node

    def test_an_undeclared_input_still_reads_the_object(self) -> None:
        producer, consumer = _producer(), self._plain_consumer()
        pipeline = Pipeline("flux")

        producer.parameter_output_values["pipeline"] = pipeline
        consumer.set_parameter_value("model", _egress(producer)["pipeline"])

        assert consumer.get_parameter_value("model") is pipeline

    def test_the_undeclared_input_still_carries_the_key(self) -> None:
        """It has to stay JSON-safe: this dict is the payload of an ExecuteNodeRequest."""
        producer, consumer = _producer(), self._plain_consumer()

        producer.parameter_output_values["pipeline"] = Pipeline("flux")
        consumer.set_parameter_value("model", _egress(producer)["pipeline"])

        assert isinstance(consumer.parameter_values["model"], str)
        assert isinstance(consumer._get_raw_parameter_value("model"), str)

    def test_an_ordinary_string_on_an_undeclared_input_is_untouched(self) -> None:
        consumer = self._plain_consumer()
        consumer.set_parameter_value("model", "gpt-4o")

        assert consumer.get_parameter_value("model") == "gpt-4o"


class TestRemovingARowFromAContainer:
    """Deleting a connection into a ParameterList row copies the remaining rows along.

    Those rows can hold keys whose objects sit in a worker, where this process has nothing to resolve
    against. Resolving them here would raise out of a connection delete, and out of the finally that
    resets input values after a run.
    """

    def test_a_row_holding_a_worker_held_key_does_not_raise(self) -> None:
        consumer = _LibraryNode(name="Generate", metadata={"library": "Diffusers"})
        rows = ParameterList(name="pipelines", input_types=["Pipeline"], tooltip="")
        consumer.add_parameter(rows)
        first, second = rows.add_child_parameter(), rows.add_child_parameter()
        # Minted shape, but parked in another process: exactly what a worker-produced value looks like here.
        elsewhere = "Diffusers:LoadPipeline@aabbccdd.pipeline#11223344"
        consumer.set_parameter_value(first.name, elsewhere)
        consumer.set_parameter_value(second.name, elsewhere)

        consumer.remove_parameter_value(first.name)

        assert isinstance(consumer._get_raw_parameter_value("pipelines"), list)
