"""Tests for the BaseNode surface over the process-local object cache.

The node supplies ownership and the key's default suffix, so these pin that plumbing and the error a
consumer gets when the object is gone.
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


class _ArrayLike:
    """Stands in for a tensor: truthiness raises above one element, and it is unhashable.

    numpy is not an engine dependency, and those are the two behaviours that break a key check and a map
    lookup respectively.
    """

    __hash__ = None  # type: ignore[assignment]

    def __init__(self, elements: int) -> None:
        self.elements = elements

    def __bool__(self) -> bool:
        if self.elements != 1:
            message = "The truth value of an array with more than one element is ambiguous."
            raise ValueError(message)
        return False


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
        """A node built outside library registration must not land in a real library's namespace."""
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
        """The producer cannot be named on a miss, but the consumer knows which input it was reading."""
        with pytest.raises(RuntimeError) as excinfo:
            node.require_local_object("Lib A:long-gone", parameter_name="pipeline")

        message = str(excinfo.value)
        assert "parameter 'pipeline'" in message
        assert "node 'Builder'" in message
        assert "Re-run whatever is connected to 'pipeline'." in message

    def test_a_key_from_another_library_says_so_instead_of_re_run(self, node: _Holder) -> None:
        """A key from another library can never resolve here, so re-running would go on forever."""
        other = _Holder(name="Other", metadata={"library": "Lib B"})
        theirs = other.put_local_object(Held("theirs"))
        other.drop_local_object(theirs)

        with pytest.raises(RuntimeError) as excinfo:
            node.require_local_object(theirs, parameter_name="pipeline")

        message = str(excinfo.value)
        assert "different node library" in message
        assert "cannot be passed between libraries" in message
        assert "Re-run" not in message

    @pytest.mark.parametrize("wrong_value", [_ArrayLike(elements=4), _ArrayLike(elements=1), 42, object()])
    def test_a_value_that_is_not_a_key_reports_that(self, node: _Holder, wrong_value: object) -> None:
        """The mistake this catches is wiring the object in place of its key, so it must survive a tensor."""
        with pytest.raises(RuntimeError) as caught:
            node.require_local_object(wrong_value, parameter_name="pipeline")  # type: ignore[arg-type]

        message = str(caught.value)
        assert "not a reference to a held object" in message
        assert "nothing is connected" not in message

    def test_an_unwired_input_is_told_that_nothing_is_connected(self, node: _Holder) -> None:
        """An unwired input reads as None, and needs a different remedy from a wrong value."""
        with pytest.raises(RuntimeError) as caught:
            node.require_local_object(None, parameter_name="pipeline")  # type: ignore[arg-type]

        message = str(caught.value)
        assert "nothing is connected to it" in message
        assert "not a reference to a held object" not in message

    def test_a_held_falsy_value_is_not_treated_as_missing(self, node: _Holder) -> None:
        """A plain None return cannot tell absent from falsy, hence the sentinel lookup."""
        key = node.put_local_object(None)

        assert node.require_local_object(key) is None


class TestNodeDrop:
    def test_drop_one(self, node: _Holder) -> None:
        key = node.put_local_object(Held("x"))

        assert node.drop_local_object(key) is True
        assert node.get_local_object(key) is None

    @pytest.mark.parametrize("wrong_value", [_ArrayLike(elements=4), ["not", "a", "key"], None])
    def test_dropping_something_that_is_not_a_key_releases_nothing(self, node: _Holder, wrong_value: object) -> None:
        """An unhashable value would otherwise raise `TypeError` out of the map lookup."""
        assert node.drop_local_object(wrong_value) is False  # type: ignore[arg-type]

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
        """A worker discards the node each execution, so the next instance must find what the last put."""
        held = Held("pipeline")
        key = node.put_local_object(held)

        next_execution = _Holder(name="Runtime", metadata={"library": "Lib A"})

        assert next_execution.get_local_object(key) is held
