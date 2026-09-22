"""Trait-owned UI-option writes must update the trait because they are not stored."""

import logging
from typing import Any

import pytest

from griptape_nodes.exe_types.core_types import Parameter, Trait
from griptape_nodes.traits.button import Button
from griptape_nodes.traits.color_picker import ColorPicker
from griptape_nodes.traits.file_system_picker import FileSystemPicker
from griptape_nodes.traits.multi_options import MultiOptions
from griptape_nodes.traits.numbers_selector import NumbersSelector
from griptape_nodes.traits.options import Options
from griptape_nodes.traits.slider import Slider
from griptape_nodes.traits.widget import Widget


class TestAdoptingADropdown:
    def test_the_written_choices_land_on_the_trait(self) -> None:
        trait = Options(choices=["base-1"])
        parameter = Parameter(name="model", type="str", tooltip="t", traits={trait})

        parameter.ui_options = {"simple_dropdown": ["base-1", "runtime-a"]}

        assert trait.choices == ["base-1", "runtime-a"]

    def test_the_written_choices_are_reported_back(self) -> None:
        parameter = Parameter(name="model", type="str", tooltip="t", traits={Options(choices=["base-1"])})

        parameter.ui_options = {"simple_dropdown": ["base-1", "runtime-a"]}

        assert parameter.ui_options["simple_dropdown"] == ["base-1", "runtime-a"]

    def test_the_written_choices_are_saved_as_trait_state(self) -> None:
        parameter = Parameter(name="model", type="str", tooltip="t", traits={Options(choices=["base-1"])})

        parameter.ui_options = {"simple_dropdown": ["base-1", "runtime-a"]}

        assert parameter.trait_states()[0]["trait_state"]["choices"] == ["base-1", "runtime-a"]

    def test_the_search_settings_are_adopted_too(self) -> None:
        trait = Options(choices=["a"])
        parameter = Parameter(name="model", type="str", tooltip="t", traits={trait})

        parameter.ui_options = {"show_search": False, "search_filter": "gpt"}

        assert (trait.show_search, trait.search_filter) == (False, "gpt")

    def test_an_authored_option_alongside_it_is_still_stored(self) -> None:
        parameter = Parameter(name="model", type="str", tooltip="t", traits={Options(choices=["a"])})

        parameter.ui_options = {"simple_dropdown": ["a", "b"], "hide": True}

        assert parameter.authored_ui_options() == {"hide": True}

    def test_a_converter_accepts_a_value_from_the_written_choices(self) -> None:
        parameter = Parameter(name="model", type="str", tooltip="t", traits={Options(choices=["base-1"])})
        parameter.ui_options = {"simple_dropdown": ["base-1", "runtime-a"]}

        converted = parameter.converters[0]("runtime-a")

        assert converted == "runtime-a"


class TestAdoptingSliderBounds:
    def test_the_written_bounds_land_on_the_trait(self) -> None:
        trait = Slider(min_val=1, max_val=50)
        parameter = Parameter(name="steps", type="int", tooltip="t", traits={trait})

        parameter.ui_options = {"slider": {"min_val": 0, "max_val": 10}}

        assert (trait.min, trait.max) == (0, 10)

    def test_the_validator_moves_with_the_written_bounds(self) -> None:
        parameter = Parameter(name="steps", type="int", tooltip="t", traits={Slider(min_val=1, max_val=50)})

        parameter.ui_options = {"slider": {"min_val": 0, "max_val": 10}}

        with pytest.raises(ValueError, match="must be between 0 and 10"):
            parameter.validators[0](parameter, 40)

    def test_one_written_bound_leaves_the_other_alone(self) -> None:
        # Slider takes both bounds as required arguments, so adopting only one has to be
        # merged over the trait's current state rather than handed to the constructor alone.
        trait = Slider(min_val=1, max_val=50)
        parameter = Parameter(name="steps", type="int", tooltip="t", traits={trait})

        parameter.ui_options = {"slider": {"max_val": 10}}

        assert (trait.min, trait.max) == (1, 10)

    def test_written_soft_limits_land_on_the_trait(self) -> None:
        trait = Slider(min_val=1, max_val=50)
        parameter = Parameter(name="steps", type="int", tooltip="t", traits={trait})

        parameter.ui_options = {"slider": {"min_val": 1, "max_val": 50, "soft_limits": True}}

        assert trait.soft_limits is True
        assert parameter.validators == []

    def test_a_slider_key_that_is_not_a_pair_of_bounds_is_ignored(self) -> None:
        trait = Slider(min_val=1, max_val=50)
        parameter = Parameter(name="steps", type="int", tooltip="t", traits={trait})

        parameter.ui_options = {"slider": True}

        assert (trait.min, trait.max) == (1, 50)


class TestAdoptingAMultiSelect:
    def test_the_written_choices_land_on_the_trait(self) -> None:
        trait = MultiOptions(choices=["base-1"])
        parameter = Parameter(name="tags", type="list", tooltip="t", traits={trait})

        parameter.ui_options = {"multi_options": {"choices": ["base-1", "runtime-a"]}}

        assert trait.choices == ["base-1", "runtime-a"]

    def test_the_other_nested_settings_are_adopted_too(self) -> None:
        trait = MultiOptions(choices=["a"])
        parameter = Parameter(name="tags", type="list", tooltip="t", traits={trait})

        parameter.ui_options = {"multi_options": {"placeholder": "Pick tags", "icon_size": "large"}}

        assert (trait.placeholder, trait.icon_size) == ("Pick tags", "large")

    def test_a_nested_value_the_constructor_rejects_is_coerced_as_it_would_be(self) -> None:
        # MultiOptions snaps an unrecognized icon_size back to "small" when constructed, and
        # adoption goes through the constructor, so a bad written value cannot land raw.
        trait = MultiOptions(choices=["a"])
        parameter = Parameter(name="tags", type="list", tooltip="t", traits={trait})

        parameter.ui_options = {"multi_options": {"icon_size": "enormous"}}

        assert trait.icon_size == "small"


class TestAdoptingKeysAnOlderSaveStored:
    """A workflow saved before trait state was its own field stored these keys raw."""

    @pytest.mark.parametrize(
        ("trait", "written", "attribute", "expected"),
        [
            (Button(label="Go"), {"button_label": "Stop"}, "label", "Stop"),
            (Button(label="Go", icon="play"), {"iconPosition": "right"}, "icon_position", "right"),
            (FileSystemPicker(), {"fileSystemPicker": {"allowFiles": True}}, "allow_files", True),
            (ColorPicker(format="hex"), {"color_picker": {"format": "rgb"}}, "format", "rgb"),
            (NumbersSelector({"x": 1}), {"numbers_selector": {"step": 5}}, "step", 5),
            (Widget("editor", "widgets"), {"widget": "viewer"}, "name", "viewer"),
        ],
    )
    def test_the_stored_value_lands_on_the_trait(
        self, trait: Trait, written: dict[str, Any], attribute: str, expected: Any
    ) -> None:
        parameter = Parameter(name="p", type="str", tooltip="t", traits={trait})

        parameter.ui_options = written

        assert getattr(trait, attribute) == expected

    def test_a_button_label_is_reported_back(self) -> None:
        parameter = Parameter(name="go", type="str", tooltip="t", traits={Button(label="Go")})

        parameter.ui_options = {"button_label": "Stop"}

        assert parameter.ui_options["button_label"] == "Stop"


class _RenderOnlyTrait(Trait):
    """Renders a key with no way to read it back, as a third-party trait might."""

    def __init__(self, label: str) -> None:
        super().__init__()
        self.label = label

    @classmethod
    def get_trait_keys(cls) -> list[str]:
        return []

    def ui_options_for_trait(self) -> dict:
        return {"badge_label": self.label}


class TestATraitWithNothingToAdopt:
    def test_a_write_to_its_key_changes_nothing(self) -> None:
        trait = _RenderOnlyTrait(label="Go")
        parameter = Parameter(name="go", type="str", tooltip="t", traits={trait})

        parameter.ui_options = {"badge_label": "Written"}

        assert trait.label == "Go"

    def test_the_trait_still_owns_the_reported_value(self) -> None:
        parameter = Parameter(name="go", type="str", tooltip="t", traits={_RenderOnlyTrait(label="Go")})

        parameter.ui_options = {"badge_label": "Written"}

        assert parameter.ui_options["badge_label"] == "Go"

    def test_the_dropped_write_is_reported(self, caplog: pytest.LogCaptureFixture) -> None:
        """The write is neither applied nor saved, so silence loses it without a trace."""
        parameter = Parameter(name="go", type="str", tooltip="t", traits={_RenderOnlyTrait(label="Go")})
        caplog.set_level(logging.WARNING, logger="griptape_nodes")

        parameter.ui_options = {"badge_label": "Written"}

        assert any(
            "'badge_label'" in record.getMessage() and "'go'" in record.getMessage() for record in caplog.records
        )

    def test_a_write_matching_what_it_renders_is_not_reported(self, caplog: pytest.LogCaptureFixture) -> None:
        """The editor echoing back what it was given changes nothing."""
        parameter = Parameter(name="go", type="str", tooltip="t", traits={_RenderOnlyTrait(label="Go")})
        caplog.set_level(logging.WARNING, logger="griptape_nodes")

        parameter.ui_options = {"badge_label": "Go"}

        assert caplog.records == []

    def test_a_write_to_a_key_no_trait_renders_is_not_reported(self, caplog: pytest.LogCaptureFixture) -> None:
        parameter = Parameter(name="go", type="str", tooltip="t", traits={_RenderOnlyTrait(label="Go")})
        caplog.set_level(logging.WARNING, logger="griptape_nodes")

        parameter.ui_options = {"hide": True}

        assert caplog.records == []

    def test_a_parameter_with_no_traits_stores_the_whole_write(self) -> None:
        parameter = Parameter(name="steps", type="int", tooltip="t")

        parameter.ui_options = {"simple_dropdown": ["a", "b"], "hide": True}

        assert parameter.authored_ui_options() == {"simple_dropdown": ["a", "b"], "hide": True}


class TestNodeCodeWritesAdoptToo:
    """Node-code writes follow the same adoption path as editor writes."""

    def test_update_ui_options_moves_a_slider_bound(self) -> None:
        trait = Slider(min_val=0, max_val=100)
        parameter = Parameter(name="top", type="int", tooltip="t", traits={trait})

        parameter.update_ui_options({"slider": {"max_val": 512}})

        assert (trait.min, trait.max) == (0, 512)
        assert parameter.ui_options["slider"] == {"min_val": 0, "max_val": 512}

    def test_assigning_ui_options_moves_a_dropdown_choice_list(self) -> None:
        trait = Options(choices=["a"])
        parameter = Parameter(name="model", type="str", tooltip="t", traits={trait})

        parameter.ui_options = {**parameter.ui_options, "simple_dropdown": ["a", "b"]}

        assert trait.choices == ["a", "b"]

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

    def test_removing_a_trait_rendered_key_is_reported(self, caplog: pytest.LogCaptureFixture) -> None:
        """A trait renders the key regardless, so the removal can never take effect."""
        parameter = Parameter(name="model", type="str", tooltip="t", traits={Options(choices=["a"])})
        caplog.set_level(logging.WARNING, logger="griptape_nodes")

        parameter.remove_ui_options_key("simple_dropdown")

        assert len(caplog.records) == 1
        assert parameter.ui_options["simple_dropdown"] == ["a"]


class TestAdoptionUsesTheTraitProtocol:
    def test_a_dropdown_adopts_every_state_key(self) -> None:
        assert set(Options(choices=[]).to_state()) == {"choices", "show_search", "search_filter", "allow_custom"}

    def test_slider_bounds_are_saved(self) -> None:
        assert set(Slider(min_val=0, max_val=1).to_state()) == {"min_val", "max_val", "soft_limits"}


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
    """Invalid adopted state leaves the trait unchanged instead of failing the load."""

    def test_the_trait_keeps_the_state_it_had(self) -> None:
        trait = _MisdeclaredTrait(level=_UNTOUCHED_LEVEL)
        parameter = Parameter(name="level", type="int", tooltip="t", traits={trait})

        parameter.ui_options = {"misdeclared": 9}

        assert trait.level == _UNTOUCHED_LEVEL

    def test_a_warning_names_the_control_and_the_parameter(self, caplog: pytest.LogCaptureFixture) -> None:
        parameter = Parameter(name="level", type="int", tooltip="t", traits={_MisdeclaredTrait()})
        caplog.set_level(logging.WARNING, logger="griptape_nodes")

        parameter.ui_options = {"misdeclared": 9}

        assert any(
            "_MisdeclaredTrait" in record.getMessage() and "'level'" in record.getMessage() for record in caplog.records
        )

    def test_the_rest_of_the_write_is_still_stored(self) -> None:
        parameter = Parameter(name="level", type="int", tooltip="t", traits={_MisdeclaredTrait()})

        parameter.ui_options = {"misdeclared": 9, "hide": True}

        assert parameter.authored_ui_options()["hide"] is True


_ALLOWED_RANGE_LIMITED_LEVEL = 2


class _RangeLimitedTrait(Trait):
    """A third-party trait that validates restored state."""

    def __init__(self, level: int = 1) -> None:
        super().__init__()
        self.level = level

    def to_state(self) -> dict:
        return {"level": self.level}

    def apply_state(self, state: dict) -> None:
        if "level" not in state:
            return
        level = state["level"]
        if level not in (1, _ALLOWED_RANGE_LIMITED_LEVEL, 3):
            msg = "level must be between 1 and 3"
            raise ValueError(msg)
        self.level = level

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

        parameter.ui_options = {"ranged": 99}

        assert trait.level == _ALLOWED_RANGE_LIMITED_LEVEL

    def test_a_warning_names_the_control_and_the_parameter(self, caplog: pytest.LogCaptureFixture) -> None:
        parameter = Parameter(name="level", type="int", tooltip="t", traits={_RangeLimitedTrait()})
        caplog.set_level(logging.WARNING, logger="griptape_nodes")

        parameter.ui_options = {"ranged": 99}

        assert any(
            "_RangeLimitedTrait" in record.getMessage() and "'level'" in record.getMessage()
            for record in caplog.records
        )
