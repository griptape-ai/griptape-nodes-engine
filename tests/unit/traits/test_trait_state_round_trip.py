"""Trait.to_state()/from_state() carry a trait's constructor arguments through JSON."""

import json
import logging
from dataclasses import dataclass
from typing import Any

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


@dataclass(eq=False)
class _InheritedConstructorBase(Trait):
    """A trait meant to be subclassed, contributing one constructor argument."""

    low: Any = 0

    def __init__(self, *, low: Any = 0) -> None:
        super().__init__()
        self.low = low

    @classmethod
    def get_trait_keys(cls) -> list[str]:
        return ["inherited_base"]


@dataclass(eq=False)
class _ForwardingSubclass(_InheritedConstructorBase):
    """Declares one argument of its own and forwards the rest to its base."""

    high: Any = 10

    def __init__(self, *, high: Any = 10, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.high = high

    @classmethod
    def get_trait_keys(cls) -> list[str]:
        return ["forwarding"]


@dataclass(eq=False)
class _NarrowingSubclass(_InheritedConstructorBase):
    """Fixes its base's argument instead of forwarding it, so it takes none of its own."""

    def __init__(self) -> None:
        super().__init__(low=7)

    @classmethod
    def get_trait_keys(cls) -> list[str]:
        return ["narrowing"]


@dataclass(eq=False)
class _NoConstructorTrait(Trait):
    """Declares no __init__, so @dataclass generates one over inherited element fields."""

    @classmethod
    def get_trait_keys(cls) -> list[str]:
        return ["no_constructor"]


class TestInheritedConstructorArguments:
    """State comes from every constructor the authors wrote, not just the nearest one."""

    def test_a_forwarded_base_argument_is_saved(self) -> None:
        trait = _ForwardingSubclass(high=99, low=5)

        assert trait.to_state() == {"high": 99, "low": 5}

    def test_a_forwarded_base_argument_survives_a_round_trip(self) -> None:
        source = _ForwardingSubclass(high=99, low=5)

        restored = _ForwardingSubclass.from_state(source.to_state())

        assert restored.high == source.high
        assert restored.low == source.low
        assert restored.to_state() == source.to_state()

    def test_a_base_argument_the_subclass_cannot_forward_is_not_saved(self) -> None:
        # Without **kwargs there is no way to pass 'low' back in, so saving it would
        # produce state from_state() could not replay.
        trait = _NarrowingSubclass()

        assert trait.to_state() == {}
        # The constructor fixes 'low', so restoring reproduces it without carrying it.
        assert _NarrowingSubclass.from_state(trait.to_state()).low == trait.low


class TestGeneratedConstructorsAreNotState:
    """A @dataclass-generated __init__ says nothing about what the author considers state."""

    def test_a_trait_declaring_no_constructor_has_no_state(self) -> None:
        assert _NoConstructorTrait().to_state() == {}

    def test_engine_internals_never_appear_in_state(self) -> None:
        state = _NoConstructorTrait().to_state()

        for internal in ("_children", "_parent", "element_id", "element_type"):
            assert internal not in state

    def test_compare_declares_no_constructor_and_stays_empty(self) -> None:
        assert Compare().to_state() == {}
