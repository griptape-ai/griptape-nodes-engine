"""Trait.to_state()/from_state() carry a trait's constructor arguments through JSON."""

import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import patch

import attrs
import pytest

from griptape_nodes.exe_types.core_types import BaseNodeElement, ParameterGroup, Trait
from griptape_nodes.traits.add_param_button import AddParameterButton
from griptape_nodes.traits.clamp import Clamp
from griptape_nodes.traits.compare import Compare
from griptape_nodes.traits.minmax import MinMax
from griptape_nodes.traits.multi_options import MultiOptions
from griptape_nodes.traits.slider import Slider
from griptape_nodes.traits.widget import Widget


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


class TestTraitWithNoDeclaredState:
    """Compare declares no fields, so it has nothing to save."""

    def test_state_is_empty(self) -> None:
        assert Compare.state_keys() == []

    def test_to_state_is_empty(self) -> None:
        assert Compare().to_state() == {}

    def test_from_state_rebuilds_with_no_arguments(self) -> None:
        rebuilt = Compare.from_state({})

        assert isinstance(rebuilt, Compare)


class _UnsaveableItemTrait(Trait):
    """A field a save cannot hold in full: the declared type passes, the contents do not.

    ``list[Any]`` is the escape hatch the class-creation check cannot judge, so this is the one
    case left for a save-time warning.
    """

    items: list[Any] = attrs.field(factory=list)


class _ExtensionsTrait(Trait):
    """Stands in for a trait taking a set, as a file picker's extensions are."""

    extensions: set[str] | list[str] | None = attrs.field(default=None)


class TestAValueNoSavedFileCanHoldIsOmittedAndWarnedAbout:
    """One value's worth of loss, not the artist's whole save."""

    def test_the_warning_names_the_field_and_the_type(self, caplog: pytest.LogCaptureFixture) -> None:
        # Path is PosixPath or WindowsPath depending on the platform, so name it from the value.
        root = Path("/tmp/somewhere")  # noqa: S108
        trait = _UnsaveableItemTrait(items=[root])

        with caplog.at_level(logging.WARNING, logger="griptape_nodes"):
            state = trait.to_state()

        assert state == {}
        assert "items" in caplog.text
        assert type(root).__name__ in caplog.text


class TestStateIsWhatADataFormatCanHold:
    """State goes into a saved artifact, so a container with no data form becomes one that has."""

    def test_a_set_is_saved_as_a_list(self) -> None:
        trait = _ExtensionsTrait(extensions={".mp4", ".avi"})

        assert trait.to_state() == {"extensions": [".avi", ".mp4"]}

    def test_the_constructor_is_handed_that_list_on_load(self) -> None:
        """A trait wanting a set converts in the field, which is where coercion belongs."""
        source = _ExtensionsTrait(extensions={".mp4", ".avi"})

        rebuilt = _ExtensionsTrait.from_state(json.loads(json.dumps(source.to_state())))

        assert rebuilt.extensions == [".avi", ".mp4"]
        assert rebuilt.to_state() == source.to_state()


class _InheritedStateBase(Trait):
    """A trait meant to be subclassed, contributing one field."""

    low: Any = attrs.field(default=0)


class _InheritingSubclass(_InheritedStateBase):
    """Declares one field of its own. attrs collects its base's too, with nothing to restate."""

    high: Any = attrs.field(default=10)


class _FixingSubclass(_InheritedStateBase):
    """Fixes its base's field instead of taking an argument for it, so it is no longer state."""

    low: Any = attrs.field(default=7, init=False)


class _NoDeclaredStateTrait(Trait):
    """Declares no fields, so it has no state."""


class TestInheritedFields:
    """A subclass gets its base's fields, and can fix one rather than pass it on."""

    def test_a_base_field_is_saved_without_being_restated(self) -> None:
        trait = _InheritingSubclass(high=99, low=5)

        assert trait.to_state() == {"high": 99, "low": 5}

    def test_a_base_field_survives_a_round_trip(self) -> None:
        source = _InheritingSubclass(high=99, low=5)

        restored = _InheritingSubclass.from_state(source.to_state())

        assert restored.high == source.high
        assert restored.low == source.low
        assert restored.to_state() == source.to_state()

    def test_a_base_field_the_subclass_fixed_is_not_saved(self) -> None:
        # The constructor takes no argument for 'low', so saving it would produce state
        # from_state() could not replay.
        trait = _FixingSubclass()

        assert trait.to_state() == {}
        # The field fixes 'low', so restoring reproduces it without carrying it.
        assert _FixingSubclass.from_state(trait.to_state()).low == trait.low


class TestATraitDeclaringNoStateSavesNothing:
    """State is what a trait declares, so one that declares nothing carries nothing."""

    def test_state_is_empty(self) -> None:
        assert _NoDeclaredStateTrait().to_state() == {}

    def test_engine_internals_never_appear_in_state(self) -> None:
        state = _NoDeclaredStateTrait().to_state()

        for internal in ("_children", "_parent", "element_id", "element_type"):
            assert internal not in state


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


class TestWidgetReadsItsOlderSavedKey:
    """Widget's name moved off the element's own ``name`` and onto a field of its own.

    A file saved before that wrote the widget's name as ``name``, so ``migrate_state`` reads it
    back rather than leaving those parameters with a control that renders nothing.
    """

    def test_the_older_key_still_loads(self) -> None:
        rebuilt = Widget.from_state({"name": "editor", "library": "my-lib"})

        assert (rebuilt.widget_name, rebuilt.library) == ("editor", "my-lib")

    def test_it_is_saved_under_the_current_key(self) -> None:
        rebuilt = Widget.from_state({"name": "editor", "library": "my-lib"})

        assert rebuilt.to_state() == {"widget_name": "editor", "library": "my-lib"}

    def test_the_element_name_stays_engine_wiring(self) -> None:
        rebuilt = Widget.from_state({"name": "editor", "library": "my-lib"})

        assert rebuilt.name != "editor"

    def test_it_applies_in_place_too(self) -> None:
        widget = Widget(widget_name="old", library="my-lib")

        widget.apply_state({"name": "editor", "library": "my-lib"})

        assert widget.widget_name == "editor"

    def test_the_current_key_wins_when_a_file_holds_both(self) -> None:
        rebuilt = Widget.from_state({"name": "stale", "widget_name": "editor", "library": "my-lib"})

        assert rebuilt.widget_name == "editor"


_MIGRATED_LEVEL = 4
_MIGRATED_SECONDS = 120


class _RenamedField(Trait):
    """Renames a saved key the way a trait author would, without guarding against a rerun."""

    level: int = attrs.field(default=0)

    @classmethod
    def migrate_state(cls, state: dict[str, Any]) -> dict[str, Any]:
        migrated = dict(state)
        migrated["level"] = migrated.pop("threshold")
        return migrated


class _ScaledField(Trait):
    """Converts a saved value's units, which double-applying would corrupt rather than raise."""

    seconds: float = attrs.field(default=0)

    @classmethod
    def migrate_state(cls, state: dict[str, Any]) -> dict[str, Any]:
        return {"seconds": state["seconds"] * 60}


class TestAMigrationRunsOncePerLoad:
    """Otherwise every migration has to be written to survive being run on its own output.

    ``apply_state`` builds a throwaway to interpret the state it was handed, and that
    throwaway must not migrate a second time.
    """

    def test_a_rename_applied_to_an_existing_trait_does_not_rerun(self) -> None:
        trait = _RenamedField(level=1)

        trait.apply_state({"threshold": 4})

        assert trait.level == _MIGRATED_LEVEL

    def test_a_rename_loaded_into_a_fresh_trait_does_not_rerun(self) -> None:
        assert _RenamedField.from_state({"threshold": 4}).level == _MIGRATED_LEVEL

    def test_a_unit_conversion_is_applied_once(self) -> None:
        trait = _ScaledField(seconds=0)

        trait.apply_state({"seconds": 2})

        assert trait.seconds == _MIGRATED_SECONDS
