"""Tests for the node-facing side of the process-local object store.

Two surfaces: a `handle[...]` parameter, where assigning an object parks it and the engine owns the
key, and `node.local_objects`, where a library names its own key for a resource it reuses across runs.
"""

import pytest

from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
from griptape_nodes.exe_types.node_types import BaseNode


class _Holder(BaseNode):
    """Concrete BaseNode used to exercise the local-object surfaces."""

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


def _with_handle_output(node: _Holder, name: str = "pipeline", **kwargs) -> Parameter:
    parameter = Parameter(name=name, output_type="handle[Pipeline]", tooltip="", **kwargs)
    node.add_parameter(parameter)
    return parameter


class TestHandleParameter:
    def test_assigning_an_object_stores_a_key_and_holds_the_object(self, node: _Holder) -> None:
        """The library assigns the object; only the key travels onward."""
        _with_handle_output(node)
        held = Held("pipeline")

        node.parameter_output_values["pipeline"] = held

        key = node.parameter_output_values["pipeline"]
        assert isinstance(key, str)
        assert key.startswith("Lib A:")
        assert node.resolve_handle("pipeline") is held

    def test_a_second_run_gets_a_different_key(self, node: _Holder) -> None:
        """A stable key would leave the value unchanged, and the editor is told only about changes."""
        _with_handle_output(node)

        node.parameter_output_values["pipeline"] = Held("first")
        first_key = node.parameter_output_values["pipeline"]
        node.parameter_output_values["pipeline"] = Held("second")
        second_key = node.parameter_output_values["pipeline"]

        assert first_key != second_key

    def test_the_displaced_object_is_released(self, node: _Holder) -> None:
        """Replacing the value is what makes the old object unreachable, so it is freed then."""
        released: list[str] = []
        _with_handle_output(node, on_local_object_drop=lambda value: released.append(value.label))

        node.parameter_output_values["pipeline"] = Held("first")
        node.parameter_output_values["pipeline"] = Held("second")

        assert released == ["first"]
        assert node.resolve_handle("pipeline").label == "second"

    def test_a_key_assigned_back_is_passed_through(self, node: _Holder) -> None:
        """A node that changes an object in place and outputs it again must not park it twice.

        Two entries for one object means two release hooks, either of which frees it under the other.
        """
        released: list[str] = []
        _with_handle_output(node, on_local_object_drop=lambda value: released.append(value.label))
        held = Held("pipeline")

        node.parameter_output_values["pipeline"] = held
        key = node.parameter_output_values["pipeline"]
        node.parameter_output_values["pipeline"] = key

        assert node.parameter_output_values["pipeline"] == key
        assert released == []
        assert node.resolve_handle("pipeline") is held

    def test_an_ordinary_parameter_is_untouched(self, node: _Holder) -> None:
        node.add_parameter(Parameter(name="count", output_type="int", tooltip=""))
        steps = 42

        node.parameter_output_values["count"] = steps

        assert node.parameter_output_values["count"] == steps

    def test_a_handle_parameter_reports_that_it_holds_one(self, node: _Holder) -> None:
        parameter = _with_handle_output(node)

        assert parameter.holds_local_object is True
        assert Parameter(name="plain", output_type="str", tooltip="").holds_local_object is False

    def test_a_handle_can_declare_property_without_breaking(self, node: _Holder) -> None:
        """No construction guard: the engine clears a handle on disconnect whatever it declares."""
        parameter = _with_handle_output(node, allowed_modes={ParameterMode.PROPERTY, ParameterMode.OUTPUT})

        node.parameter_output_values["pipeline"] = Held("x")

        assert parameter.holds_local_object is True
        assert node.resolve_handle("pipeline").label == "x"


class TestResolveHandle:
    def test_another_node_reads_what_this_one_produced(self, node: _Holder) -> None:
        """The normal case: a producer assigns, a consumer resolves, both in one process."""
        _with_handle_output(node)
        held = Held("pipeline")
        node.parameter_output_values["pipeline"] = held

        consumer = _Holder(name="Runtime", metadata={"library": "Lib A"})
        consumer.add_parameter(Parameter(name="pipeline", input_types=["handle[Pipeline]"], tooltip=""))
        consumer.set_parameter_value("pipeline", node.parameter_output_values["pipeline"])

        assert consumer.resolve_handle("pipeline") is held

    def test_says_what_to_do_when_the_object_is_gone(self, node: _Holder) -> None:
        node.add_parameter(Parameter(name="pipeline", input_types=["handle[Pipeline]"], tooltip=""))
        node.set_parameter_value("pipeline", "Lib A:long-gone")

        with pytest.raises(RuntimeError) as caught:
            node.resolve_handle("pipeline")

        message = str(caught.value)
        assert "no longer available" in message
        assert "parameter 'pipeline'" in message
        assert "Re-run whatever is connected to 'pipeline'." in message

    def test_an_unwired_input_is_told_that_nothing_is_connected(self, node: _Holder) -> None:
        """An unwired input reads as None, and needs a different remedy from a wrong value."""
        node.add_parameter(Parameter(name="pipeline", input_types=["handle[Pipeline]"], tooltip=""))

        with pytest.raises(RuntimeError) as caught:
            node.resolve_handle("pipeline")

        message = str(caught.value)
        assert "nothing is connected to it" in message
        assert "not a reference to a held object" not in message

    @pytest.mark.parametrize("wrong_value", [_ArrayLike(elements=4), _ArrayLike(elements=1), 42, object()])
    def test_a_value_that_is_not_a_key_reports_that(self, node: _Holder, wrong_value: object) -> None:
        """The mistake this catches is wiring the object in place of its key, so it must survive a tensor."""
        node.add_parameter(Parameter(name="pipeline", input_types=["handle[Pipeline]"], tooltip=""))
        node.parameter_values["pipeline"] = wrong_value

        with pytest.raises(RuntimeError) as caught:
            node.resolve_handle("pipeline")

        message = str(caught.value)
        assert "not a reference to a held object" in message
        assert "nothing is connected" not in message

    def test_a_key_from_another_library_says_so_instead_of_re_run(self, node: _Holder) -> None:
        """A key from another library can never resolve here, so re-running would go on forever."""
        other = _Holder(name="Other", metadata={"library": "Lib B"})
        _with_handle_output(other)
        other.parameter_output_values["pipeline"] = Held("theirs")

        node.add_parameter(Parameter(name="pipeline", input_types=["handle[Pipeline]"], tooltip=""))
        node.set_parameter_value("pipeline", other.parameter_output_values["pipeline"])

        with pytest.raises(RuntimeError) as caught:
            node.resolve_handle("pipeline")

        message = str(caught.value)
        assert "different node library" in message
        assert "Re-run" not in message


class TestResourceScope:
    """`local_objects` is for a resource the library reuses across runs, keyed by something it can derive."""

    def test_put_then_get_under_a_derived_key(self, node: _Holder) -> None:
        held = Held("pipeline")
        key = node.local_objects.put(held, key="cfg-hash")

        assert node.local_objects.get(key) is held
        assert node.local_objects.key_for("cfg-hash") == key

    def test_key_is_scoped_to_the_nodes_library(self, node: _Holder) -> None:
        assert node.local_objects.put(Held("x"), key="cfg").startswith("Lib A:")

    def test_a_node_without_a_library_still_works(self) -> None:
        """A node built outside library registration must not land in a real library's namespace."""
        orphan = _Holder(name="Loose", metadata={})

        key = orphan.local_objects.put(Held("x"), key="cfg")

        assert orphan.local_objects.get(key) is not None
        assert not key.startswith("Lib A:")

    def test_drop_one(self, node: _Holder) -> None:
        key = node.local_objects.put(Held("x"), key="cfg")

        assert node.local_objects.drop(key) is True
        assert node.local_objects.get(key) is None

    @pytest.mark.parametrize("wrong_value", [_ArrayLike(elements=4), ["not", "a", "key"], None])
    def test_dropping_something_that_is_not_a_key_releases_nothing(self, node: _Holder, wrong_value: object) -> None:
        """An unhashable value would otherwise raise `TypeError` out of the map lookup."""
        assert node.local_objects.drop(wrong_value) is False  # type: ignore[arg-type]

    def test_drop_all_is_scoped_to_this_library(self, node: _Holder) -> None:
        """This is what a clear-cache node calls, and it must not reach another library's objects."""
        other = _Holder(name="Other", metadata={"library": "Lib B"})
        mine = node.local_objects.put(Held("mine"), key="cfg")
        theirs = other.local_objects.put(Held("theirs"), key="cfg")

        dropped = node.local_objects.drop_all()

        assert dropped == 1
        assert node.local_objects.get(mine) is None
        assert other.local_objects.get(theirs) is not None

    def test_cannot_drop_an_object_owned_by_another_library(self, node: _Holder) -> None:
        """A handle can arrive from another library as a parameter value; dropping it must be refused."""
        other = _Holder(name="Other", metadata={"library": "Lib B"})
        theirs = other.local_objects.put(Held("theirs"), key="cfg")

        assert node.local_objects.drop(theirs) is False
        assert other.local_objects.get(theirs) is not None

    def test_drop_all_runs_release_hooks(self, node: _Holder) -> None:
        released: list[str] = []
        node.local_objects.put(Held("gpu"), key="cfg", on_drop=lambda value: released.append(value.label))

        node.local_objects.drop_all()

        assert released == ["gpu"]

    def test_reusing_a_key_releases_what_it_displaced(self, node: _Holder) -> None:
        """Rebuilding under an unchanged hash must not strand the old object."""
        released: list[str] = []
        node.local_objects.put(Held("first"), key="cfg", on_drop=lambda value: released.append(value.label))
        node.local_objects.put(Held("second"), key="cfg")

        assert released == ["first"]


class TestSurvivesNodeDiscard:
    def test_a_new_node_reads_what_another_produced(self, node: _Holder) -> None:
        """A worker discards the node each execution, so the next instance must find what the last put."""
        _with_handle_output(node)
        held = Held("pipeline")
        node.parameter_output_values["pipeline"] = held
        key = node.parameter_output_values["pipeline"]

        next_execution = _Holder(name="Runtime", metadata={"library": "Lib A"})
        next_execution.add_parameter(Parameter(name="pipeline", input_types=["handle[Pipeline]"], tooltip=""))
        next_execution.set_parameter_value("pipeline", key)

        assert next_execution.resolve_handle("pipeline") is held
