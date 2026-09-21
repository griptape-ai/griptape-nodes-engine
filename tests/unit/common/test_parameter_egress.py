"""What happens to a parameter value on its way out of the process.

Parking lives here rather than on the write path: a node's own dicts keep the real objects, so reading
back what you just assigned works, and a graph that never crosses a boundary never parks anything.
"""

from typing import Any

import pytest
from griptape.artifacts import TextArtifact
from griptape.drivers.prompt.openai import OpenAiChatPromptDriver

from griptape_nodes.common.parameter_hydration import dehydrate_parameter_values
from griptape_nodes.exe_types.core_types import Parameter, ParameterList, ParameterMode
from griptape_nodes.exe_types.node_types import BaseNode

_STEPS = 30


class Pipeline:
    """Stands in for anything that cannot be represented as data."""

    def __init__(self, label: str) -> None:
        self.label = label


class _LibraryNode(BaseNode):
    def process(self) -> None:
        pass


def _node(name: str = "LoadPipeline", *, on_drop: Any = None) -> _LibraryNode:
    node = _LibraryNode(name=name, metadata={"library": "Diffusers"})
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
    node.add_parameter(Parameter(name="steps", output_type="int", tooltip=""))
    return node


def _send(node: _LibraryNode) -> dict:
    return dehydrate_parameter_values(node.parameter_output_values, node=node, are_outputs=True)


class TestWhatCrosses:
    def test_an_object_becomes_a_key_and_stays_behind(self) -> None:
        node = _node()
        pipeline = Pipeline("flux")
        node.parameter_output_values["pipeline"] = pipeline

        sent = _send(node)

        assert isinstance(sent["pipeline"], str)
        assert node.parameter_output_values["pipeline"] is pipeline
        assert node.local_objects.get(sent["pipeline"]) is pipeline

    def test_ordinary_data_is_sent_as_data(self) -> None:
        node = _node()
        node.parameter_output_values["steps"] = _STEPS

        assert _send(node)["steps"] == _STEPS

    def test_a_dict_keyed_by_number_travels(self) -> None:
        """json.dumps coerces non-string keys, so refusing them would fail a node that already ran."""
        node = _node()
        node.parameter_output_values["steps"] = {0: "a.png", 1: "b.png"}

        assert _send(node)["steps"] == {0: "a.png", 1: "b.png"}

    def test_an_artifact_passes_through_untouched(self) -> None:
        """Artifacts only look unsendable until cattrs turns them into dicts, so they are not parked.

        This pass substitutes keys and nothing else; the transport is what unstructures.
        """
        node = _node()
        artifact = TextArtifact("hello")
        node.parameter_output_values["steps"] = artifact

        assert _send(node)["steps"] is artifact

    def test_sendable_data_on_an_unpersistable_parameter_is_not_parked(self) -> None:
        """An API token is declared serializable=False for persistence, and it travels perfectly well.

        Parking it would send a key the far side has nothing to resolve against.
        """
        node = _node()
        node.parameter_output_values["pipeline"] = "sk-secret-123"

        assert _send(node)["pipeline"] == "sk-secret-123"


class TestWhenTheAuthorDidNotSaySo:
    def test_an_unsendable_value_on_an_ordinary_parameter_raises(self) -> None:
        """The transport coerces with str(), so silence here ships a repr to the other side."""
        node = _node()
        node.parameter_output_values["steps"] = Pipeline("flux")

        with pytest.raises(TypeError) as caught:
            _send(node)

        message = str(caught.value)
        assert "'steps'" in message
        assert "serializable=False" in message


class TestSendingTwice:
    def test_one_object_keeps_one_key(self) -> None:
        node = _node()
        node.parameter_output_values["pipeline"] = Pipeline("flux")

        assert _send(node)["pipeline"] == _send(node)["pipeline"]

    def test_a_run_that_produces_nothing_frees_the_last_one(self) -> None:
        released: list[str] = []
        node = _node(on_drop=lambda value: released.append(value.label))
        node.parameter_output_values["pipeline"] = Pipeline("first")
        _send(node)

        node.parameter_output_values.silent_clear()
        node.parameter_output_values["pipeline"] = None
        _send(node)

        assert released == ["first"]


class TestAListOfHeldValues:
    """A ParameterList fed by several producers carries several keys, one per row.

    `get_parameter_list_value` is how a node reads one, so a key nested in the list has to come back as
    an object or the node is handed strings.
    """

    def test_a_consumer_reads_objects_out_of_the_list(self) -> None:
        producer = _node()
        producer.parameter_output_values["pipeline"] = Pipeline("flux")
        key = _send(producer)["pipeline"]

        consumer = _LibraryNode(name="Generate", metadata={"library": "Diffusers"})
        pipelines = ParameterList(name="pipelines", input_types=["Pipeline"], tooltip="")
        consumer.add_parameter(pipelines)
        consumer.set_parameter_value(pipelines.add_child_parameter().name, key)

        assert [type(item).__name__ for item in consumer.get_parameter_list_value("pipelines")] == ["Pipeline"]
        assert consumer.get_parameter_value("pipelines")[0].label == "flux"

    def test_a_key_in_a_dict_value_comes_back_too(self) -> None:
        producer = _node()
        producer.parameter_output_values["pipeline"] = Pipeline("flux")
        key = _send(producer)["pipeline"]

        consumer = _node(name="Generate")
        consumer.set_parameter_value("steps", {"base": key, "count": _STEPS})

        read = consumer.get_parameter_value("steps")
        assert read["base"].label == "flux"
        assert read["count"] == _STEPS

    def test_a_container_output_of_unsendable_values_says_what_to_do(self) -> None:
        """The container is never itself held, so the ordinary remedy would send the author in a circle."""
        node = _LibraryNode(name="Batch", metadata={"library": "Diffusers"})
        latents = ParameterList(name="latents", output_type="Latent", tooltip="")
        node.add_parameter(latents)
        node.parameter_output_values["latents"] = [Pipeline("a"), Pipeline("b")]

        with pytest.raises(TypeError) as caught:
            _send(node)

        message = str(caught.value)
        assert "cannot be kept here as a whole" in message
        assert "ordinary parameter marked serializable=False" in message


class TestReadingAnOrdinaryContainer:
    """The walk that resolves nested keys must not change what an ordinary list or dict read gives back.

    `get_parameter_value` is the most-used node-facing call, and a node that mutates the list it read in
    place is relying on getting the stored object rather than a copy of it.
    """

    def test_a_list_with_nothing_held_is_the_stored_object(self) -> None:
        node = _node()
        node.add_parameter(Parameter(name="items", output_type="list", tooltip=""))
        stored = [1, 2, {"a": 3}]
        node.parameter_values["items"] = stored

        got = node.get_parameter_value("items")

        assert got is stored
        assert got[2] is stored[2]

    def test_two_rows_sharing_one_list_both_resolve(self) -> None:
        """parallel_resolution delivers an upstream output by reference, so two rows can be one object."""
        producer = _node()
        producer.parameter_output_values["pipeline"] = Pipeline("flux")
        key = _send(producer)["pipeline"]

        consumer = _node(name="Generate")
        consumer.add_parameter(Parameter(name="rows", output_type="list", tooltip=""))
        shared = [key]
        consumer.parameter_values["rows"] = [shared, shared]

        read = consumer.get_parameter_value("rows")

        assert [type(row[0]).__name__ for row in read] == ["Pipeline", "Pipeline"]
        assert read[0] is read[1]

    def test_a_self_referential_list_terminates(self) -> None:
        producer = _node()
        producer.parameter_output_values["pipeline"] = Pipeline("flux")
        key = _send(producer)["pipeline"]

        consumer = _node(name="Generate")
        consumer.add_parameter(Parameter(name="rows", output_type="list", tooltip=""))
        cyclic: list = [key]
        cyclic.append(cyclic)
        consumer.parameter_values["rows"] = cyclic

        assert isinstance(consumer.get_parameter_value("rows")[0], Pipeline)


class TestReleasingOnDelete:
    def test_a_plain_string_is_not_broadcast_as_a_key(self) -> None:
        """An API token is declared serializable=False and travels as data; it is not a key.

        The orchestrator holds no entries, so anything reaching the broadcast with no local record is
        assumed to be parked in a worker and sent to all of them.
        """
        node = _node()
        manager = node.local_objects._manager()
        manager._pending_worker_releases.clear()
        # Snapshotted at the moment the fan-out fires, because scheduling drains the queue: the return
        # value was already False without the shape check, so the queue contents are the only evidence.
        queued_when_sent: list[list[str]] = []
        original = manager.engine.worker_manager.schedule_pending_local_object_releases
        manager.engine.worker_manager.schedule_pending_local_object_releases = lambda: queued_when_sent.append(
            list(manager._pending_worker_releases)
        )
        try:
            node.local_objects.release_parked("sk-secret-123")
        finally:
            manager.engine.worker_manager.schedule_pending_local_object_releases = original

        assert queued_when_sent == []


class TestWhatCountsAsArrivingIntact:
    """Parking asks whether the value arrives as itself, not whether it becomes JSON.

    cattrs unstructures any attrs or dataclass object into a dict of its fields, and the inbound mirror
    only reconstitutes artifacts, so "encodable" would let a driver through as a plain dict.
    """

    def test_a_driver_is_held_even_though_it_encodes(self) -> None:
        node = _node()
        driver = OpenAiChatPromptDriver(model="gpt-4o", api_key="sk-test")
        node.parameter_output_values["pipeline"] = driver

        sent = _send(node)

        assert isinstance(sent["pipeline"], str)
        assert node.local_objects.get(sent["pipeline"]) is driver

    def test_an_artifact_on_a_held_parameter_is_held(self) -> None:
        """The declaration decides, not whether cattrs happens to manage a round trip.

        An artifact a library defines itself unstructures without its payload, so treating "is an
        artifact" as "survives" dropped the object on the wire with no diagnostic.
        """
        node = _node()
        artifact = TextArtifact("hello")
        node.parameter_output_values["pipeline"] = artifact

        sent = _send(node)

        assert isinstance(sent["pipeline"], str)
        assert node.local_objects.get(sent["pipeline"]) is artifact

    def test_an_artifact_on_an_ordinary_parameter_still_travels(self) -> None:
        """Undeclared, the pre-existing transport handles it: out as a dict, hydrated back on arrival."""
        node = _node()
        artifact = TextArtifact("hello")
        node.parameter_output_values["steps"] = artifact

        assert _send(node)["steps"] is artifact

    def test_a_driver_on_an_ordinary_parameter_is_not_refused(self) -> None:
        """The transport can encode it, so the raise is not the right answer -- only parking was missing."""
        node = _node()
        node.parameter_output_values["steps"] = OpenAiChatPromptDriver(model="gpt-4o", api_key="sk-test")

        assert _send(node)["steps"] is not None


class TestAKeyNestedInsideASavedValue:
    """The save and metadata guards have to see a key inside a list or dict, not just one that is the value.

    A container carries its children's values, so a key arrives nested as a matter of course. Saving one
    writes a dead reference into the workflow file on a node marked RESOLVED, with nothing to re-run it.
    """

    def test_a_key_inside_a_list_is_seen(self) -> None:
        node = _node()
        node.parameter_output_values["pipeline"] = Pipeline("flux")
        key = _send(node)["pipeline"]

        assert node.local_objects.contains_a_parked_object([key]) is True
        assert node.local_objects.contains_a_parked_object({"a": key}) is True
        assert node.local_objects.contains_a_parked_object({"a": [{"b": key}]}) is True

    def test_ordinary_values_are_not_flagged(self) -> None:
        node = _node()

        assert node.local_objects.contains_a_parked_object(["a", 1, None]) is False
        assert node.local_objects.contains_a_parked_object({"a": "hello"}) is False


class TestTheSaveGuardOnAwkwardValues:
    """The guard runs on the save path, so a value it cannot walk takes out the save itself."""

    def test_a_self_referential_value_does_not_recurse_forever(self) -> None:
        node = _node()
        cyclic: list = ["a"]
        cyclic.append(cyclic)

        assert node.local_objects.contains_a_parked_object(cyclic) is False

    def test_shared_substructure_is_visited_once(self) -> None:
        """Acyclic but shared: without a visited set this is exponential in the nesting depth."""
        node = _node()
        previous: Any = "leaf"
        for _ in range(40):
            previous = {"a": previous, "b": previous}

        assert node.local_objects.contains_a_parked_object(previous) is False

    def test_a_key_shared_across_branches_is_still_found(self) -> None:
        node = _node()
        node.parameter_output_values["pipeline"] = Pipeline("flux")
        key = _send(node)["pipeline"]
        shared = [key]

        assert node.local_objects.contains_a_parked_object({"a": shared, "b": shared}) is True


class TestDeclaringItOnAContainer:
    """A container cannot hold a value, so saying so when the parameter is added beats saying it mid-run.

    `ParameterContainer.is_process_local` is always False, so the declaration had no effect whatsoever and
    the author learned that only when a node had already produced something at a process boundary.
    """

    def test_adding_a_declared_container_is_refused(self) -> None:
        node = _LibraryNode(name="Batch", metadata={"library": "Diffusers"})
        latents = ParameterList(name="latents", output_type="Latent", tooltip="")
        latents.serializable = False

        with pytest.raises(ValueError, match="cannot hold a value that stays in this process") as caught:
            node.add_parameter(latents)

        assert "ordinary parameter marked serializable=False" in str(caught.value)

    def test_an_ordinary_container_is_fine(self) -> None:
        node = _LibraryNode(name="Batch", metadata={"library": "Diffusers"})
        node.add_parameter(ParameterList(name="latents", output_type="Latent", tooltip=""))

        assert node.get_parameter_by_name("latents") is not None
