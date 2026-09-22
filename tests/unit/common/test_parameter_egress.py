"""What happens to a parameter value on its way out of the process.

Parking lives here rather than on the write path: a node's own dicts keep the real objects, so reading
back what you just assigned works, and a graph that never crosses a boundary never parks anything.
"""

import uuid
from typing import Any

from griptape.artifacts import TextArtifact
from griptape.drivers.prompt.openai import OpenAiChatPromptDriver

from griptape_nodes.exe_types.core_types import Parameter, ParameterList, ParameterMode
from griptape_nodes.exe_types.local_objects import (
    cache_outputs_for_egress,
    caches_its_values,
    is_reference,
    make_reference,
)
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
    return cache_outputs_for_egress(node.parameter_output_values, node=node)


class TestWhatCrosses:
    def test_an_object_becomes_a_key_and_stays_behind(self) -> None:
        node = _node()
        pipeline = Pipeline("flux")
        node.parameter_output_values["pipeline"] = pipeline

        sent = _send(node)

        assert is_reference(sent["pipeline"])
        assert node.parameter_output_values["pipeline"] is pipeline
        assert node.local_objects.get(sent["pipeline"]["key"]) is pipeline

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


class TestWhatTheFeatureDoesNotTouch:
    """Anything not declared is passed through exactly as before, whatever the transport makes of it.

    The cache is opt-in. Policing values the transport stringifies is a separate, pre-existing concern and
    not this feature's to enforce -- refusing them here broke graphs that worked, for values like UUID and
    Decimal that cross perfectly well.
    """

    def test_an_undeclared_unsendable_value_is_passed_through(self) -> None:
        node = _node()
        driver = OpenAiChatPromptDriver(model="gpt-4o", api_key="sk-test")
        node.parameter_output_values["steps"] = driver

        assert _send(node)["steps"] is driver

    def test_a_uuid_still_crosses(self) -> None:
        """The transport ships it as a string; refusing it was a regression this feature caused."""
        node = _node()
        identifier = uuid.uuid4()
        node.parameter_output_values["steps"] = identifier

        assert _send(node)["steps"] is identifier


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

    def test_a_container_output_is_not_cached(self) -> None:
        """A container is never itself held: its children carry the values and it has no release hook."""
        node = _LibraryNode(name="Batch", metadata={"library": "Diffusers"})
        latents = ParameterList(name="latents", output_type="Latent", tooltip="")
        node.add_parameter(latents)
        batch = [Pipeline("a"), Pipeline("b")]
        node.parameter_output_values["latents"] = batch

        assert _send(node)["latents"] is batch


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
    def test_a_plain_string_is_never_collected_as_something_to_release(self) -> None:
        """An API token is declared serializable=False and travels as data. It is not a reference.

        Deleting a node broadcasts what it owns to every worker, so a value mistaken for a reference here
        would put a secret on the wire to libraries that never saw it. The envelope is what prevents that:
        only a reference is ever collected, and a string cannot be one whatever it looks like. The previous
        design matched a pattern against strings and had to be kept deliberately narrow for this reason.
        """
        node = _node()
        node.parameter_output_values["pipeline"] = "sk-secret-123"

        assert node.local_objects.parked_keys_within("sk-secret-123") == set()
        assert node.local_objects.parked_keys_within({"token": "sk-secret-123"}) == set()
        # And the shape the old pattern would have matched.
        assert node.local_objects.parked_keys_within("Lib:Producer@abc12345.pipeline#deadbeef") == set()

    def test_a_reference_is_collected(self) -> None:
        """The other half: what the node does own is found, so deletion releases it."""
        node = _node()
        node.parameter_output_values["pipeline"] = Pipeline("flux")
        sent = _send(node)

        assert node.local_objects.parked_keys_within(sent["pipeline"]) == {sent["pipeline"]["key"]}


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

        assert is_reference(sent["pipeline"])
        assert node.local_objects.get(sent["pipeline"]["key"]) is driver

    def test_an_artifact_on_a_held_parameter_is_held(self) -> None:
        """The declaration decides, not whether cattrs happens to manage a round trip.

        An artifact a library defines itself unstructures without its payload, so treating "is an
        artifact" as "survives" dropped the object on the wire with no diagnostic.
        """
        node = _node()
        artifact = TextArtifact("hello")
        node.parameter_output_values["pipeline"] = artifact

        sent = _send(node)

        assert is_reference(sent["pipeline"])
        assert node.local_objects.get(sent["pipeline"]["key"]) is artifact

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

    def test_a_declared_container_keeps_its_persistence_meaning_but_is_not_held(self) -> None:
        """The flag still means "do not save this list", which is what it meant before any of this.

        What it cannot add on a container is holding, because a container builds its value from children
        and has nowhere to attach a release hook. An unsendable value in one is caught at the boundary
        with a message saying what to do instead.
        """
        node = _LibraryNode(name="Batch", metadata={"library": "Diffusers"})
        latents = ParameterList(name="latents", output_type="Latent", tooltip="")
        latents.serializable = False
        node.add_parameter(latents)

        assert latents.serializable is False
        assert caches_its_values(latents) is False

    def test_an_ordinary_container_is_fine(self) -> None:
        node = _LibraryNode(name="Batch", metadata={"library": "Diffusers"})
        node.add_parameter(ParameterList(name="latents", output_type="Latent", tooltip=""))

        assert node.get_parameter_by_name("latents") is not None


class TestReleasingWhileANodeRuns:
    """A hook frees what the object holds, so it must not run while a node is using the object.

    The store's lock cannot help: a consumer stops consulting the store the moment it has the object in
    hand, and then holds it for as long as it runs. So engine-initiated releases wait for the node to
    finish rather than freeing underneath it.
    """

    def test_a_release_waits_for_the_running_node(self) -> None:
        node = _node()
        released: list[str] = []
        node.local_objects.put(Pipeline("first"), key="cfg", on_drop=lambda value: released.append(value.label))
        manager = node.local_objects._manager()

        with manager.engine.event_manager.worker_node_execution_scope():
            node.local_objects.put(Pipeline("second"), key="cfg")
            assert released == []

        assert manager.drain_deferred_releases() == 1
        assert released == ["first"]

    def test_outside_execution_a_release_is_immediate(self) -> None:
        node = _node()
        released: list[str] = []
        node.local_objects.put(Pipeline("first"), key="cfg", on_drop=lambda value: released.append(value.label))

        node.local_objects.put(Pipeline("second"), key="cfg")

        assert released == ["first"]


class TestReadingTheDictDirectly:
    """A node body that indexes `parameter_values` gets the object, not the reference standing for it.

    Authors do read that dict directly -- hydration already materialises defaults into it for exactly that
    reason -- so a reference sitting there would hand them something that is not their object.
    """

    def test_a_held_reference_becomes_the_object(self) -> None:
        producer = _node()
        pipeline = Pipeline("flux")
        producer.parameter_output_values["pipeline"] = pipeline
        reference = _send(producer)["pipeline"]

        consumer = _node(name="Generate")
        consumer.parameter_values["steps"] = reference

        resolved = consumer.local_objects.resolve_what_is_here(consumer.parameter_values["steps"])

        assert resolved is pipeline

    def test_a_reference_from_another_process_is_left_alone(self) -> None:
        """The orchestrator cannot resolve one and still has to send it onward, so it must survive."""
        node = _node()
        elsewhere = make_reference(worker="another-worker", key="P@abc12345.pipe#deadbeef", source="P@abc12345")

        assert node.local_objects.resolve_what_is_here(elsewhere) is elsewhere

    def test_nothing_else_is_disturbed(self) -> None:
        node = _node()
        stored = ["a", 1, {"b": None}]

        assert node.local_objects.resolve_what_is_here(stored) is stored


class TestReleasingWhileAnotherNodeRuns:
    """The flag the deferral tests is a process-wide count, so the drain has to respect it too.

    Parallel resolution runs several nodes at once in one worker. Draining when the first of them exits
    frees an object a sibling may still be holding -- the failure the deferral exists to prevent, reached
    by a different door.
    """

    def test_the_drain_waits_for_the_last_node(self) -> None:
        node = _node()
        released: list[str] = []
        node.local_objects.put(Pipeline("first"), key="cfg", on_drop=lambda value: released.append(value.label))
        manager = node.local_objects._manager()
        events = manager.engine.event_manager

        with events.worker_node_execution_scope():  # node A
            with events.worker_node_execution_scope():  # node B, concurrent
                node.local_objects.put(Pipeline("second"), key="cfg")
                assert released == []
            # B has exited, A is still running: the drain refuses rather than freeing what A may hold.
            assert events.in_node_execution() is True
            assert manager.drain_deferred_releases() == 0
            assert released == []

        # A has finished too, so now it goes.
        assert manager.drain_deferred_releases() == 1
        assert released == ["first"]
