"""Tests for the BaseNode surface over the process-local object cache.

A node is the only place a library reaches this from, and the node is what supplies ownership and the
producing-node name. These tests pin that plumbing, and the error a consumer gets when the object is
gone, because that message is the whole reason libraries stop improvising recovery paths.
"""

import pytest

from griptape_nodes.exe_types.node_types import BaseNode


class _Holder(BaseNode):
    """Concrete BaseNode used to exercise the local-object helpers."""

    def __init__(self, name: str, metadata=None) -> None:  # noqa: ANN001
        super().__init__(name=name, metadata=metadata)

    def process(self) -> None:
        return None


class Held:
    def __init__(self, label: str) -> None:
        self.label = label


@pytest.fixture
def node() -> _Holder:
    """A node belonging to a library, which is what supplies key scoping."""
    return _Holder(name="Builder", metadata={"library": "Lib A"})


class TestNodeRoundTrip:
    def test_put_then_get(self, node: _Holder) -> None:
        held = Held("pipeline")

        key = node.put_local_object(held)

        assert node.get_local_object(key) is held

    def test_key_is_scoped_to_the_nodes_library(self, node: _Holder) -> None:
        key = node.put_local_object(Held("x"))

        assert key.startswith("Lib A:")

    def test_a_node_without_a_library_still_works(self) -> None:
        """An unregistered node still gets somewhere to put things.

        A node built outside library registration, in a test or a sandbox script, must not land in a
        real library's namespace.
        """
        orphan = _Holder(name="Loose", metadata={})

        key = orphan.put_local_object(Held("x"))

        assert orphan.get_local_object(key) is not None
        assert not key.startswith("Lib A:")


class TestRequireLocalObject:
    def test_returns_the_object_when_held(self, node: _Holder) -> None:
        held = Held("pipeline")
        key = node.put_local_object(held)

        assert node.require_local_object(key) is held

    def test_another_node_can_read_what_this_one_put(self, node: _Holder) -> None:
        """The normal case: a producer puts, a consumer requires, both in one process."""
        held = Held("pipeline")
        key = node.put_local_object(held)

        consumer = _Holder(name="Runtime", metadata={"library": "Lib A"})

        assert consumer.require_local_object(key) is held

    def test_raises_and_says_what_to_do_when_missing(self, node: _Holder) -> None:
        with pytest.raises(RuntimeError) as excinfo:
            node.require_local_object("Lib A:long-gone")

        message = str(excinfo.value)
        assert "no longer available" in message
        assert "Re-run the node that produces it." in message

    def test_names_the_parameter_to_look_at_when_given_one(self, node: _Holder) -> None:
        """The message points at the input rather than at the producing node.

        Naming the producer is impossible on a miss, because its record went with the entry. The
        consumer does know which of its own inputs it was reading.
        """
        with pytest.raises(RuntimeError) as excinfo:
            node.require_local_object("Lib A:long-gone", parameter_name="pipeline")

        message = str(excinfo.value)
        assert "parameter 'pipeline'" in message
        assert "node 'Builder'" in message
        assert "Re-run whatever is connected to 'pipeline'." in message

    def test_a_key_from_another_library_says_so_instead_of_re_run(self, node: _Holder) -> None:
        """Telling someone to re-run a cross-library handle would have them do it forever.

        A key is namespaced by its creating library, and the value never leaves the process that built
        it, so a key from elsewhere can never resolve here no matter how many times anything re-runs.
        """
        other = _Holder(name="Other", metadata={"library": "Lib B"})
        theirs = other.put_local_object(Held("theirs"))
        other.drop_local_object(theirs)

        with pytest.raises(RuntimeError) as excinfo:
            node.require_local_object(theirs, parameter_name="pipeline")

        message = str(excinfo.value)
        assert "different node library" in message
        assert "cannot be passed between libraries" in message
        assert "Re-run" not in message

    def test_a_held_falsy_value_is_not_treated_as_missing(self, node: _Holder) -> None:
        """A held falsy value is not mistaken for a missing one.

        A plain `None` return cannot distinguish absent from falsy, which is why `require_local_object`
        does one lookup against a sentinel instead.
        """
        key = node.put_local_object(None)

        assert node.require_local_object(key) is None


class TestNodeDrop:
    def test_drop_one(self, node: _Holder) -> None:
        key = node.put_local_object(Held("x"))

        assert node.drop_local_object(key) is True
        assert node.get_local_object(key) is None

    def test_drop_all_is_scoped_to_this_library(self, node: _Holder) -> None:
        """This is what a clear-cache node calls, and it must not reach another library's objects."""
        other = _Holder(name="Other", metadata={"library": "Lib B"})
        mine = node.put_local_object(Held("mine"))
        theirs = other.put_local_object(Held("theirs"))

        dropped = node.drop_all_local_objects()

        assert dropped == 1
        assert node.get_local_object(mine) is None
        assert other.get_local_object(theirs) is not None

    def test_cannot_drop_an_object_owned_by_another_library(self, node: _Holder) -> None:
        """A handle can arrive from another library as a parameter value; dropping it must be refused."""
        other = _Holder(name="Other", metadata={"library": "Lib B"})
        theirs = other.put_local_object(Held("theirs"))

        assert node.drop_local_object(theirs) is False
        assert other.get_local_object(theirs) is not None

    def test_drop_all_runs_release_hooks(self, node: _Holder) -> None:
        released: list[str] = []
        node.put_local_object(Held("gpu"), on_drop=lambda value: released.append(value.label))

        node.drop_all_local_objects()

        assert released == ["gpu"]


class TestSurvivesNodeDiscard:
    def test_a_new_node_reads_what_another_put(self, node: _Holder) -> None:
        """The cache outlives the node that filled it.

        This is the point of the design: a worker discards the node after every execution, so a second
        node instance standing in for the next execution must still find what the first one put.
        """
        held = Held("pipeline")
        key = node.put_local_object(held)

        next_execution = _Holder(name="Runtime", metadata={"library": "Lib A"})

        assert next_execution.get_local_object(key) is held
