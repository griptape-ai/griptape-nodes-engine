from griptape.artifacts import BaseArtifact, TextArtifact

from griptape_nodes.common.parameter_hydration import hydrate_parameter_values
from griptape_nodes.serialization.values import UndecodedValue


class TestHydrateParameterValues:
    def test_rehydrates_single_artifact_dict(self) -> None:
        original = TextArtifact("hello")
        values = {"out": original.to_dict()}

        hydrated = hydrate_parameter_values(values)

        assert isinstance(hydrated["out"], BaseArtifact)
        assert isinstance(hydrated["out"], TextArtifact)
        assert hydrated["out"].value == "hello"

    def test_passes_through_real_artifact(self) -> None:
        artifact = TextArtifact("hello")
        values = {"out": artifact}

        hydrated = hydrate_parameter_values(values)

        assert hydrated["out"] is artifact

    def test_passes_through_non_artifact_dict(self) -> None:
        values = {"cfg": {"foo": "bar", "count": 3}}

        hydrated = hydrate_parameter_values(values)

        assert hydrated["cfg"] == {"foo": "bar", "count": 3}

    def test_passes_through_scalars_and_none(self) -> None:
        values = {"s": "hi", "n": 3, "f": 1.5, "b": True, "none": None}

        hydrated = hydrate_parameter_values(values)

        assert hydrated == values

    def test_walks_list_of_artifact_dicts(self) -> None:
        a = TextArtifact("a")
        b = TextArtifact("b")
        values = {"items": [a.to_dict(), b.to_dict()]}

        hydrated = hydrate_parameter_values(values)

        assert isinstance(hydrated["items"], list)
        assert all(isinstance(item, TextArtifact) for item in hydrated["items"])
        assert [item.value for item in hydrated["items"]] == ["a", "b"]

    def test_malformed_artifact_dict_falls_back(self) -> None:
        bogus = {"type": "NotARealArtifactType", "value": "whatever"}
        values = {"x": bogus}

        hydrated = hydrate_parameter_values(values)

        assert hydrated["x"] == bogus

    def test_leaves_undecoded_value_alone(self) -> None:
        """UndecodedValue is a dict subclass; it must not be run through from_dict."""
        undecoded = UndecodedValue({"type": "TextArtifact", "value": "hello"}, reason="library not loaded")
        values = {"out": undecoded}

        hydrated = hydrate_parameter_values(values)

        assert hydrated["out"] is undecoded
