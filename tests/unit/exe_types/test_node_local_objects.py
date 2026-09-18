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


def _hand_over(producer: _LibraryNode, consumer: _LibraryNode, param: str = "pipeline") -> None:
    """Deliver the producer's output to the consumer, the way resolution does.

    Reads the output dict directly, as parallel_resolution does: what is there is the key.
    """
    consumer.set_parameter_value(param, producer.parameter_output_values[param])


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

        assert isinstance(producer.parameter_output_values["pipeline"], str)
        assert isinstance(consumer.get_parameter_value("pipeline"), Pipeline)

    def test_the_engine_sees_the_key_where_the_node_sees_the_object(self) -> None:
        """What travels to a worker, into a saved file, or to the editor is the key.

        A value only becomes an object at the point a node reads its own parameter.
        """
        producer, consumer = _producer(), _consumer()
        producer.parameter_output_values["pipeline"] = Pipeline("flux")
        _hand_over(producer, consumer)

        raw = consumer.get_raw_parameter_value("pipeline")

        assert isinstance(raw, str)
        assert raw.startswith("Diffusers:")
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
        producer.parameter_output_values.silent_clear()
        producer.parameter_output_values["pipeline"] = Pipeline("second")

        assert released == ["first"]


class TestWhatTheAuthorSeesWhenSomethingIsWrong:
    """Each way of being wrong needs a different fix, so each gets its own answer."""

    def test_the_object_is_gone(self) -> None:
        """A reopened workflow holds keys into a process that no longer exists."""
        consumer = _consumer()
        consumer.set_parameter_value("pipeline", "Diffusers:LoadPipeline@abc12345.pipeline#deadbeef")

        with pytest.raises(RuntimeError) as caught:
            consumer.get_parameter_value("pipeline")

        message = str(caught.value)
        assert "no longer available" in message
        assert "parameter 'pipeline'" in message
        assert "Re-run whatever is connected to 'pipeline'." in message

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

    def test_a_key_from_another_library_says_so(self) -> None:
        """A key never resolves outside the library that made it, whatever process they share."""
        other_library = _LibraryNode(name="TheirLoader", metadata={"library": "SomeoneElse"})
        other_library.add_parameter(Parameter(name="pipeline", output_type="Pipeline", tooltip="", serializable=False))
        other_library.parameter_output_values["pipeline"] = Pipeline("theirs")

        consumer = _consumer()
        consumer.set_parameter_value("pipeline", other_library.parameter_output_values["pipeline"])

        with pytest.raises(RuntimeError) as caught:
            consumer.get_parameter_value("pipeline")

        message = str(caught.value)
        assert "different node library" in message
        assert "Re-run" not in message

    def test_streaming_into_a_held_parameter_is_refused(self) -> None:
        """Appending a chunk to a key would make a string that still looks like a key.

        The write would pass it through and free the object under everyone holding the real key, on the
        first chunk, silently.
        """
        producer = _producer()
        producer.parameter_output_values["pipeline"] = Pipeline("streamed")

        with pytest.raises(RuntimeError, match="cannot be streamed into"):
            producer.append_value_to_parameter("pipeline", "chunk")


class TestOrdinaryParametersAreUntouched:
    def test_a_serializable_parameter_keeps_its_value(self) -> None:
        node = _LibraryNode(name="Settings")
        node.add_parameter(Parameter(name="steps", output_type="int", tooltip=""))
        steps = 20

        node.parameter_output_values["steps"] = steps

        assert node.parameter_output_values["steps"] == steps

    def test_a_container_is_not_parked(self) -> None:
        """Holding a whole list as one object would hand a downstream list one opaque key.

        A container has nowhere to put a release hook either; its elements are ordinary parameters.
        """
        node = _LibraryNode(name="Batch")
        latents = ParameterList(name="latents", output_type="Latent", tooltip="")
        # ParameterList does not accept `serializable` through __init__; setting it directly is the only
        # way to reach the case, and is_process_local must still answer False for a container.
        latents.serializable = False
        node.add_parameter(latents)
        assert latents.is_process_local is False
        batch = [Pipeline("a"), Pipeline("b")]

        node.parameter_output_values["latents"] = batch

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
