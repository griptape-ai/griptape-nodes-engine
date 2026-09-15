"""Reading the controls out of a workflow saved before trait state was carried on its own.

Such a file recorded a dropdown or a slider only as the flat ``ui_options`` keys the trait
rendered. For a parameter the node declares that is a shortfall the node's ``__init__`` covers.
For one created at run time it is the only record there is.
"""

import pytest

from griptape_nodes.exe_types.core_types import Parameter, ParameterMode, Trait
from griptape_nodes.traits.legacy_ui_options import reconstruct_traits_from_ui_options
from griptape_nodes.traits.multi_options import MultiOptions
from griptape_nodes.traits.options import Options
from griptape_nodes.traits.slider import Slider


def _parameter(**ui_options) -> Parameter:
    return Parameter(
        name="p",
        type="str",
        tooltip="t",
        allowed_modes={ParameterMode.PROPERTY},
        ui_options=dict(ui_options),
    )


class TestRebuildingAControl:
    def test_a_dropdown_comes_back_with_its_choices(self) -> None:
        parameter = _parameter(simple_dropdown=["a", "b", "c"], show_search=True)

        rebuilt = reconstruct_traits_from_ui_options(parameter)

        assert [type(trait) for trait in rebuilt] == [Options]
        assert parameter.find_elements_by_type(Options)[0].choices == ["a", "b", "c"]

    def test_the_choices_start_meaning_something_again(self) -> None:
        """The point of rebuilding it. Stored keys alone render a dropdown that validates nothing."""
        parameter = _parameter(simple_dropdown=["a", "b", "c"])

        reconstruct_traits_from_ui_options(parameter)

        assert [converter("zzz") for converter in parameter.converters] == ["a"]
        assert len(parameter.validators) == 1
        with pytest.raises(ValueError, match="Choice not allowed"):
            parameter.validators[0](parameter, "zzz")

    def test_a_slider_comes_back_with_its_bounds(self) -> None:
        parameter = _parameter(slider={"min_val": 2, "max_val": 8})

        reconstruct_traits_from_ui_options(parameter)

        slider = parameter.find_elements_by_type(Slider)[0]
        assert (slider.min, slider.max) == (2, 8)

    def test_a_multi_select_comes_back_with_its_choices(self) -> None:
        parameter = _parameter(multi_options={"choices": ["x", "y"], "placeholder": "Pick"})

        reconstruct_traits_from_ui_options(parameter)

        multi = parameter.find_elements_by_type(MultiOptions)[0]
        assert multi.choices == ["x", "y"]
        assert multi.placeholder == "Pick"

    def test_a_dropdown_spelled_the_way_it_was_before_the_trait_existed(self) -> None:
        """``enum_choices`` was the SimpleDropdown option key. The editor still reads it."""
        parameter = _parameter(enum_choices=["x", "y"])

        reconstruct_traits_from_ui_options(parameter)

        assert parameter.find_elements_by_type(Options)[0].choices == ["x", "y"]

    def test_what_comes_back_is_saved_as_trait_state_from_now_on(self) -> None:
        parameter = _parameter(simple_dropdown=["a", "b"], hide=True)

        reconstruct_traits_from_ui_options(parameter)

        assert parameter.trait_states()[0]["trait_state"]["choices"] == ["a", "b"]
        assert parameter.ui_options["hide"] is True


class TestLeavingAParameterAlone:
    def test_a_secondary_key_on_its_own_invents_nothing(self) -> None:
        """``show_search`` belongs to a dropdown but does not imply one."""
        parameter = _parameter(show_search=True, hide=True)

        assert reconstruct_traits_from_ui_options(parameter) == []
        assert parameter.find_elements_by_type(Trait) == []

    def test_a_parameter_with_no_control_keys_is_untouched(self) -> None:
        parameter = _parameter(hide=True, display_name="Model")

        assert reconstruct_traits_from_ui_options(parameter) == []

    def test_a_control_the_node_already_built_is_not_duplicated(self) -> None:
        """Live code owns the trait. The stored keys describe it, not a second one beside it."""
        parameter = Parameter(
            name="p",
            tooltip="t",
            traits={Options(choices=["live"])},
            ui_options={"simple_dropdown": ["stale"]},
        )

        assert reconstruct_traits_from_ui_options(parameter) == []
        assert len(parameter.find_elements_by_type(Options)) == 1

    def test_options_that_no_longer_fit_the_control_are_reported(self, caplog: pytest.LogCaptureFixture) -> None:
        """A slider needs both bounds. Half of one is a file from a version that spelled it differently."""
        parameter = _parameter(slider={"min_val": 2})

        assert reconstruct_traits_from_ui_options(parameter) == []
        assert "Slider" in caplog.text
        assert parameter.name in caplog.text
