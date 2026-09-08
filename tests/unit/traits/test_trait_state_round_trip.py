"""Trait.to_state()/from_state() carry a trait's constructor arguments through JSON."""

import json

import pytest

from griptape_nodes.traits.clamp import Clamp
from griptape_nodes.traits.compare import Compare
from griptape_nodes.traits.minmax import MinMax
from griptape_nodes.traits.slider import Slider


class TestAliasedConstructorState:
    """Slider, Clamp, and MinMax take min_val/max_val but store min/max."""

    @pytest.mark.parametrize("trait_class", [Slider, Clamp, MinMax])
    def test_state_is_keyed_by_the_constructor_argument_name(self, trait_class: type) -> None:
        trait = trait_class(min_val=1, max_val=9)

        state = trait.to_state()

        assert state == {"min_val": 1, "max_val": 9}

    @pytest.mark.parametrize("trait_class", [Slider, Clamp, MinMax])
    def test_state_survives_a_json_round_trip(self, trait_class: type) -> None:
        source = trait_class(min_val=1, max_val=9)

        wire = json.dumps(source.to_state())
        rebuilt = trait_class.from_state(json.loads(wire))

        assert rebuilt.min == source.min
        assert rebuilt.max == source.max
        assert rebuilt.to_state() == source.to_state()


class TestTraitWithNoHandWrittenInit:
    """Compare declares no __init__ of its own, so it has no constructor state to save."""

    def test_state_parameter_names_is_empty(self) -> None:
        assert Compare._state_parameter_names() == []

    def test_to_state_is_empty(self) -> None:
        assert Compare().to_state() == {}

    def test_from_state_rebuilds_with_no_arguments(self) -> None:
        rebuilt = Compare.from_state({})

        assert isinstance(rebuilt, Compare)
