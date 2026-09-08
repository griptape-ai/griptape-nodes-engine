"""Trait.to_state()/from_state() carry a trait's constructor arguments through JSON."""

import json
import logging

import pytest

from griptape_nodes.exe_types.core_types import Trait
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


class _UndeclaredCallbackTrait(Trait):
    """Stands in for a third-party trait that forgot to declare STATE_EXCLUDE."""

    def __init__(self, *, label: str = "hi", on_ping: object = None) -> None:
        super().__init__(element_id="_UndeclaredCallbackTrait")
        self.label = label
        self.on_ping = on_ping

    def ui_options_for_trait(self) -> dict:
        return {}

    @classmethod
    def get_trait_keys(cls) -> list[str]:
        return ["undeclared_callback"]


class _UnaliasedAttributeTrait(Trait):
    """Stands in for a third-party trait that forgot to declare STATE_ALIASES."""

    def __init__(self, *, threshold: int = 0) -> None:
        super().__init__(element_id="_UnaliasedAttributeTrait")
        self._level = threshold  # stored under a different name, no STATE_ALIASES declared

    def ui_options_for_trait(self) -> dict:
        return {}

    @classmethod
    def get_trait_keys(cls) -> list[str]:
        return ["unaliased_attribute"]


class TestMisdeclaredTraitStateDegradesInsteadOfFailingTheSave:
    """A trait author's mistake costs that one key, not the whole workflow save."""

    def test_an_undeclared_callback_is_omitted_and_warned_about(self, caplog: pytest.LogCaptureFixture) -> None:
        trait = _UndeclaredCallbackTrait(label="x", on_ping=lambda: None)

        with caplog.at_level(logging.WARNING, logger="griptape_nodes"):
            state = trait.to_state()

        assert state == {"label": "x"}
        assert "on_ping" in caplog.text
        assert "STATE_EXCLUDE" in caplog.text

    def test_an_unaliased_attribute_is_omitted_and_warned_about(self, caplog: pytest.LogCaptureFixture) -> None:
        trait = _UnaliasedAttributeTrait(threshold=5)

        with caplog.at_level(logging.WARNING, logger="griptape_nodes"):
            state = trait.to_state()

        assert state == {}
        assert "threshold" in caplog.text
        assert "STATE_ALIASES" in caplog.text
