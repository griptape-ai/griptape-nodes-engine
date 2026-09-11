"""Trait.to_state()/from_state() carry a trait's constructor arguments through JSON."""

import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from griptape_nodes.exe_types.core_types import BaseNodeElement, ParameterGroup, Trait
from griptape_nodes.traits.add_param_button import AddParameterButton
from griptape_nodes.traits.clamp import Clamp
from griptape_nodes.traits.compare import Compare
from griptape_nodes.traits.minmax import MinMax
from griptape_nodes.traits.multi_options import MultiOptions
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

    def test_a_value_no_saved_file_can_hold_is_omitted_and_warned_about(self, caplog: pytest.LogCaptureFixture) -> None:
        trait = _UnsaveableValueTrait(label="x", root=Path("/tmp/somewhere"))  # noqa: S108

        with caplog.at_level(logging.WARNING, logger="griptape_nodes"):
            state = trait.to_state()

        assert state == {"label": "x"}
        assert "root" in caplog.text
        assert "PosixPath" in caplog.text


class _UnsaveableValueTrait(Trait):
    """Stands in for a trait whose constructor takes something no data format holds."""

    def __init__(self, *, label: str = "hi", root: Path | None = None) -> None:
        super().__init__(element_id="_UnsaveableValueTrait")
        self.label = label
        self.root = root

    def ui_options_for_trait(self) -> dict:
        return {}

    @classmethod
    def get_trait_keys(cls) -> list[str]:
        return ["unsaveable_value"]


class _ExtensionsTrait(Trait):
    """Stands in for a trait whose constructor takes a set, as a file picker's extensions are."""

    def __init__(self, *, extensions: set[str] | list[str] | None = None) -> None:
        super().__init__(element_id="_ExtensionsTrait")
        self.extensions = extensions

    def ui_options_for_trait(self) -> dict:
        return {}

    @classmethod
    def get_trait_keys(cls) -> list[str]:
        return ["extensions"]


class TestStateIsWhatADataFormatCanHold:
    """State goes into a saved artifact, so a container with no data form becomes one that has."""

    def test_a_set_is_saved_as_a_list(self) -> None:
        trait = _ExtensionsTrait(extensions={".mp4", ".avi"})

        assert trait.to_state() == {"extensions": [".avi", ".mp4"]}

    def test_the_constructor_is_handed_that_list_on_load(self) -> None:
        """A trait wanting a set builds one in its constructor, which is where coercion belongs."""
        source = _ExtensionsTrait(extensions={".mp4", ".avi"})

        rebuilt = _ExtensionsTrait.from_state(json.loads(json.dumps(source.to_state())))

        assert rebuilt.extensions == [".avi", ".mp4"]
        assert rebuilt.to_state() == source.to_state()


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


class TestApplyStateGoesThroughTheConstructor:
    """Restoring in place must interpret state the same way building fresh does."""

    def test_a_constructor_coercion_applies_in_place(self) -> None:
        # MultiOptions snaps an unrecognized icon_size back to "small". A raw assignment
        # would keep "huge", so the same saved file produced two different traits depending
        # on whether the node's __init__ had already built one.
        trait = MultiOptions(choices=["a"])

        trait.apply_state({"choices": ["a"], "icon_size": "huge"})

        assert trait.icon_size == "small"
        assert trait.icon_size == MultiOptions.from_state({"choices": ["a"], "icon_size": "huge"}).icon_size

    def test_a_key_the_saved_state_omits_keeps_what_init_built(self) -> None:
        # A file saved before the trait gained an argument says nothing about it. Live code
        # should win there, so the node's own constructor value stands rather than being
        # reset to the trait's default.
        trait = MultiOptions(choices=["a"], placeholder="Pick one")

        trait.apply_state({"choices": ["b"]})

        assert trait.choices == ["b"]
        assert trait.placeholder == "Pick one"
        assert MultiOptions.from_state({"choices": ["b"]}).placeholder == "Select options..."

    def test_an_aliased_argument_still_lands_on_its_attribute(self) -> None:
        trait = Slider(min_val=0, max_val=1)

        trait.apply_state({"min_val": 2, "max_val": 8})

        assert (trait.min, trait.max) == (2, 8)

    def test_state_the_constructor_cannot_satisfy_raises(self) -> None:
        # The caller reports this. Applying what fits and leaving the rest would produce a
        # trait that is neither what was saved nor what __init__ built.
        trait = Slider(min_val=0, max_val=1)

        with pytest.raises(TypeError):
            trait.apply_state({"min_val": 2})

    def test_a_failed_apply_leaves_the_trait_as_it_was(self) -> None:
        trait = Slider(min_val=0, max_val=1)

        with pytest.raises(TypeError):
            trait.apply_state({"min_val": 2})

        assert trait.min == 0
        assert trait.max == 1


@contextmanager
def _recording_elements(built: list[str]) -> Iterator[None]:
    """Record the class name of every node element constructed inside the block."""
    original_init = BaseNodeElement.__init__

    def recording_init(self: BaseNodeElement, **kwargs: Any) -> None:
        built.append(type(self).__name__)
        original_init(self, **kwargs)

    with patch.object(BaseNodeElement, "__init__", recording_init):
        yield


class TestTheThrowawayIsNeverObservable:
    """apply_state reads its values off an instance the constructor built, then discards it."""

    def test_it_is_not_adopted_by_an_open_element_context(self) -> None:
        # An element built inside a `with` block is attached to it, which is what lets node
        # code declare a parameter's children by nesting. Wrong for an instance built only to
        # be read: the group would grow a phantom trait.
        trait = Slider(min_val=0, max_val=1)

        with ParameterGroup(name="g") as group:
            trait.apply_state({"min_val": 2, "max_val": 8})

        assert group.children == []

    def test_state_with_nothing_in_it_builds_nothing(self) -> None:
        # A trait declaring no constructor arguments has no state for a throwaway to
        # interpret. Building one anyway runs its constructor, and AddParameterButton's
        # attaches a Button child.
        built: list[str] = []
        trait = AddParameterButton()

        with _recording_elements(built):
            trait.apply_state({})

        assert built == []
