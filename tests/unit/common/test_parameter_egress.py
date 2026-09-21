"""What happens to a parameter value on its way out of the process.

Parking lives here rather than on the write path: a node's own dicts keep the real objects, so reading
back what you just assigned works, and a graph that never crosses a boundary never parks anything.
"""

from typing import Any

import pytest
from griptape.artifacts import TextArtifact

from griptape_nodes.common.parameter_hydration import dehydrate_parameter_values
from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
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
