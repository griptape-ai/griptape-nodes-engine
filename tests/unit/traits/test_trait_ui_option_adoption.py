"""A ui_options write from outside the engine reaches the trait that owns the key.

The editor and any workflow saved before trait state was carried in its own right both write
a trait's options straight into the parameter's ``ui_options``. Stored there, the value is
shadowed at read time and dropped at save time, so the write has to be routed to its owner.
"""

import logging

import attrs
import pytest

from griptape_nodes.exe_types.core_types import Parameter, Trait
from griptape_nodes.traits.button import Button
from griptape_nodes.traits.multi_options import MultiOptions
from griptape_nodes.traits.options import Options
from griptape_nodes.traits.slider import Slider


class TestAdoptingADropdown:
    def test_the_written_choices_land_on_the_trait(self) -> None:
        trait = Options(choices=["base-1"])
        parameter = Parameter(name="model", type="str", tooltip="t", traits={trait})

        parameter.adopt_ui_options({"simple_dropdown": ["base-1", "runtime-a"]})

        assert trait.choices == ["base-1", "runtime-a"]

    def test_the_written_choices_are_reported_back(self) -> None:
        parameter = Parameter(name="model", type="str", tooltip="t", traits={Options(choices=["base-1"])})

        parameter.adopt_ui_options({"simple_dropdown": ["base-1", "runtime-a"]})

        assert parameter.ui_options["simple_dropdown"] == ["base-1", "runtime-a"]

    def test_the_written_choices_are_saved_as_trait_state(self) -> None:
        parameter = Parameter(name="model", type="str", tooltip="t", traits={Options(choices=["base-1"])})

        parameter.adopt_ui_options({"simple_dropdown": ["base-1", "runtime-a"]})

        assert parameter.trait_states()[0]["trait_state"]["choices"] == ["base-1", "runtime-a"]

    def test_the_search_settings_are_adopted_too(self) -> None:
        trait = Options(choices=["a"])
        parameter = Parameter(name="model", type="str", tooltip="t", traits={trait})

        parameter.adopt_ui_options({"show_search": False, "search_filter": "gpt"})

        assert (trait.show_search, trait.search_filter) == (False, "gpt")

    def test_an_authored_option_alongside_it_is_still_stored(self) -> None:
        parameter = Parameter(name="model", type="str", tooltip="t", traits={Options(choices=["a"])})

        parameter.adopt_ui_options({"simple_dropdown": ["a", "b"], "hide": True})

        assert parameter.authored_ui_options() == {"hide": True}

    def test_a_converter_accepts_a_value_from_the_written_choices(self) -> None:
        # The point of adopting: the converter reads trait.choices, so a value the write made
        # legal has to stay legal rather than being rewritten to the first original choice.
        parameter = Parameter(name="model", type="str", tooltip="t", traits={Options(choices=["base-1"])})
        parameter.adopt_ui_options({"simple_dropdown": ["base-1", "runtime-a"]})

        converted = parameter.converters[0]("runtime-a")

        assert converted == "runtime-a"


class TestAdoptingSliderBounds:
    def test_the_written_bounds_land_on_the_trait(self) -> None:
        trait = Slider(min_val=1, max_val=50)
        parameter = Parameter(name="steps", type="int", tooltip="t", traits={trait})

        parameter.adopt_ui_options({"slider": {"min_val": 0, "max_val": 10}})

        assert (trait.min, trait.max) == (0, 10)

    def test_the_validator_moves_with_the_written_bounds(self) -> None:
        parameter = Parameter(name="steps", type="int", tooltip="t", traits={Slider(min_val=1, max_val=50)})

        parameter.adopt_ui_options({"slider": {"min_val": 0, "max_val": 10}})

        with pytest.raises(ValueError, match="out of range"):
            parameter.validators[0](parameter, 40)

    def test_one_written_bound_leaves_the_other_alone(self) -> None:
        # Slider takes both bounds as required arguments, so adopting only one has to be
        # merged over the trait's current state rather than handed to the constructor alone.
        trait = Slider(min_val=1, max_val=50)
        parameter = Parameter(name="steps", type="int", tooltip="t", traits={trait})

        parameter.adopt_ui_options({"slider": {"max_val": 10}})

        assert (trait.min, trait.max) == (1, 10)

    def test_a_slider_key_that_is_not_a_pair_of_bounds_is_ignored(self) -> None:
        trait = Slider(min_val=1, max_val=50)
        parameter = Parameter(name="steps", type="int", tooltip="t", traits={trait})

        parameter.adopt_ui_options({"slider": True})

        assert (trait.min, trait.max) == (1, 50)


class TestAdoptingAMultiSelect:
    def test_the_written_choices_land_on_the_trait(self) -> None:
        trait = MultiOptions(choices=["base-1"])
        parameter = Parameter(name="tags", type="list", tooltip="t", traits={trait})

        parameter.adopt_ui_options({"multi_options": {"choices": ["base-1", "runtime-a"]}})

        assert trait.choices == ["base-1", "runtime-a"]

    def test_the_other_nested_settings_are_adopted_too(self) -> None:
        trait = MultiOptions(choices=["a"])
        parameter = Parameter(name="tags", type="list", tooltip="t", traits={trait})

        parameter.adopt_ui_options({"multi_options": {"placeholder": "Pick tags", "icon_size": "large"}})

        assert (trait.placeholder, trait.icon_size) == ("Pick tags", "large")

    def test_a_nested_value_the_constructor_rejects_is_coerced_as_it_would_be(self) -> None:
        # MultiOptions snaps an unrecognized icon_size back to "small" when constructed, and
        # adoption goes through the constructor, so a bad written value cannot land raw.
        trait = MultiOptions(choices=["a"])
        parameter = Parameter(name="tags", type="list", tooltip="t", traits={trait})

        parameter.adopt_ui_options({"multi_options": {"icon_size": "enormous"}})

        assert trait.icon_size == "small"


class TestATraitWithNothingToAdopt:
    def test_a_write_to_its_key_changes_nothing(self) -> None:
        trait = Button(label="Go")
        parameter = Parameter(name="go", type="str", tooltip="t", traits={trait})

        parameter.adopt_ui_options({"button_label": "Written"})

        assert trait.label == "Go"

    def test_the_trait_still_owns_the_reported_value(self) -> None:
        parameter = Parameter(name="go", type="str", tooltip="t", traits={Button(label="Go")})

        parameter.adopt_ui_options({"button_label": "Written"})

        assert parameter.ui_options["button_label"] == "Go"

    def test_the_dropped_write_is_reported(self, caplog: pytest.LogCaptureFixture) -> None:
        """The write is neither applied nor saved, so silence loses it without a trace."""
        parameter = Parameter(name="go", type="str", tooltip="t", traits={Button(label="Go")})
        caplog.set_level(logging.WARNING, logger="griptape_nodes")

        parameter.adopt_ui_options({"button_label": "Written"})

        assert any(
            "'button_label'" in record.getMessage() and "'go'" in record.getMessage() for record in caplog.records
        )

    def test_a_write_matching_what_it_renders_is_not_reported(self, caplog: pytest.LogCaptureFixture) -> None:
        """The editor echoing back what it was given changes nothing."""
        parameter = Parameter(name="go", type="str", tooltip="t", traits={Button(label="Go")})
        caplog.set_level(logging.WARNING, logger="griptape_nodes")

        parameter.adopt_ui_options({"button_label": "Go"})

        assert caplog.records == []

    def test_a_write_to_a_key_no_trait_renders_is_not_reported(self, caplog: pytest.LogCaptureFixture) -> None:
        parameter = Parameter(name="go", type="str", tooltip="t", traits={Button(label="Go")})
        caplog.set_level(logging.WARNING, logger="griptape_nodes")

        parameter.adopt_ui_options({"hide": True})

        assert caplog.records == []

    def test_a_parameter_with_no_traits_stores_the_whole_write(self) -> None:
        parameter = Parameter(name="steps", type="int", tooltip="t")

        parameter.adopt_ui_options({"simple_dropdown": ["a", "b"], "hide": True})

        assert parameter.authored_ui_options() == {"simple_dropdown": ["a", "b"], "hide": True}


class TestNodeCodeWritesAdoptToo:
    """``update_ui_options`` and its siblings route through the same adoption as the editor.

    A node keeping a slider's bound in step with a loaded image (as an image crop node does)
    used to write straight into stored options: never read back, since the trait's own
    rendered bound always won, and never saved, since a trait-owned key is stripped from what
    gets stored.
    """

    def test_update_ui_options_moves_a_slider_bound(self) -> None:
        trait = Slider(min_val=0, max_val=100)
        parameter = Parameter(name="top", type="int", tooltip="t", traits={trait})

        parameter.update_ui_options({"slider": {"max_val": 512}})

        assert (trait.min, trait.max) == (0, 512)
        assert parameter.ui_options["slider"] == {"min_val": 0, "max_val": 512}

    def test_update_ui_options_key_moves_a_dropdown_choice_list(self) -> None:
        trait = Options(choices=["a"])
        parameter = Parameter(name="model", type="str", tooltip="t", traits={trait})

        parameter.update_ui_options_key("simple_dropdown", ["a", "b"])

        assert trait.choices == ["a", "b"]

    def test_a_non_trait_write_is_still_silent(self, caplog: pytest.LogCaptureFixture) -> None:
        parameter = Parameter(name="go", type="str", tooltip="t", traits={Button(label="Go")})
        caplog.set_level(logging.WARNING, logger="griptape_nodes")

        parameter.update_ui_options_key("hide", True)

        assert caplog.records == []
        assert parameter.authored_ui_options()["hide"] is True

    def test_remove_ui_options_key_still_removes_a_stored_key(self) -> None:
        parameter = Parameter(name="go", type="str", tooltip="t")
        parameter.update_ui_options_key("display_name", "Go")

        parameter.remove_ui_options_key("display_name")

        assert "display_name" not in parameter.authored_ui_options()

    def test_removing_a_trait_rendered_key_is_silent(self, caplog: pytest.LogCaptureFixture) -> None:
        """The key was never stored raw to begin with, so there is nothing to warn about."""
        parameter = Parameter(name="model", type="str", tooltip="t", traits={Options(choices=["a"])})
        caplog.set_level(logging.WARNING, logger="griptape_nodes")

        parameter.remove_ui_options_key("simple_dropdown")

        assert caplog.records == []
        assert parameter.ui_options["simple_dropdown"] == ["a"]


_ADOPTED_RANK = 3


class TestAdoptionFollowsTheDeclaredFields:
    """A trait adopts the fields it declares, so a field added later needs no second edit.

    Listing the keys by hand is how a trait's two directions drift apart: the field renders
    and then silently stops being adopted.
    """

    def test_a_field_a_subclass_adds_is_adopted(self) -> None:
        class RankedOptions(Options):
            rank: int = attrs.field(default=0)

        trait = RankedOptions(choices=["a"])
        parameter = Parameter(name="model", type="str", tooltip="t", traits={trait})

        parameter.adopt_ui_options({"rank": _ADOPTED_RANK})

        assert trait.rank == _ADOPTED_RANK

    def test_a_dropdown_adopts_every_field_it_declares(self) -> None:
        assert set(Options.state_keys()) == {"choices", "show_search", "search_filter", "allow_custom"}

    def test_slider_bounds_adopt_every_field_it_declares(self) -> None:
        assert set(Slider.state_keys()) == {"min_val", "max_val"}


_UNTOUCHED_LEVEL = 3


class _MisdeclaredTrait(Trait):
    """Names an argument its constructor does not take, as a third-party trait might."""

    def __init__(self, level: int = 1) -> None:
        super().__init__()
        self.level = level

    def ui_options_for_trait(self) -> dict:
        return {"misdeclared": self.level}

    def state_from_ui_options(self, ui_options: dict) -> dict:
        return {"nonexistent_argument": ui_options.get("misdeclared")}


class TestStateTheTraitWillNotAccept:
    """Adopted state its own constructor rejects leaves the trait as it was.

    Out of reach for the in-tree traits, which only ever name their own arguments. This is
    the contract for a third-party one: the mistake costs a control, not the whole load.
    """

    def test_the_trait_keeps_the_state_it_had(self) -> None:
        trait = _MisdeclaredTrait(level=_UNTOUCHED_LEVEL)
        parameter = Parameter(name="level", type="int", tooltip="t", traits={trait})

        parameter.adopt_ui_options({"misdeclared": 9})

        assert trait.level == _UNTOUCHED_LEVEL

    def test_a_warning_names_the_control_and_the_parameter(self, caplog: pytest.LogCaptureFixture) -> None:
        parameter = Parameter(name="level", type="int", tooltip="t", traits={_MisdeclaredTrait()})
        caplog.set_level(logging.WARNING, logger="griptape_nodes")

        parameter.adopt_ui_options({"misdeclared": 9})

        assert any(
            "_MisdeclaredTrait" in record.getMessage() and "'level'" in record.getMessage() for record in caplog.records
        )

    def test_the_rest_of_the_write_is_still_stored(self) -> None:
        parameter = Parameter(name="level", type="int", tooltip="t", traits={_MisdeclaredTrait()})

        parameter.adopt_ui_options({"misdeclared": 9, "hide": True})

        assert parameter.authored_ui_options()["hide"] is True


_ALLOWED_RANGE_LIMITED_LEVEL = 2


class _RangeLimitedTrait(Trait):
    """A validator rejects out-of-range state, as a third-party trait's own field might."""

    level: int = attrs.field(default=1, validator=attrs.validators.in_([1, _ALLOWED_RANGE_LIMITED_LEVEL, 3]))

    def ui_options_for_trait(self) -> dict:
        return {"ranged": self.level}

    @classmethod
    def state_from_ui_options(cls, ui_options: dict) -> dict:
        if "ranged" not in ui_options:
            return {}
        return {"level": ui_options["ranged"]}


class TestStateAValidatorRejects:
    """A constructor argument out of range raises ValueError, not TypeError.

    Adoption has to treat the two the same: neither is a mistake an artist can fix mid-load.
    """

    def test_the_trait_keeps_the_state_it_had(self) -> None:
        trait = _RangeLimitedTrait(level=_ALLOWED_RANGE_LIMITED_LEVEL)
        parameter = Parameter(name="level", type="int", tooltip="t", traits={trait})

        parameter.adopt_ui_options({"ranged": 99})

        assert trait.level == _ALLOWED_RANGE_LIMITED_LEVEL

    def test_a_warning_names_the_control_and_the_parameter(self, caplog: pytest.LogCaptureFixture) -> None:
        parameter = Parameter(name="level", type="int", tooltip="t", traits={_RangeLimitedTrait()})
        caplog.set_level(logging.WARNING, logger="griptape_nodes")

        parameter.adopt_ui_options({"ranged": 99})

        assert any(
            "_RangeLimitedTrait" in record.getMessage() and "'level'" in record.getMessage()
            for record in caplog.records
        )
