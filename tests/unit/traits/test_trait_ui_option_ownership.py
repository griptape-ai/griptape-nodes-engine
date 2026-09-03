"""Trait-derived UI options must stay owned by the trait, never copied into stored state."""

from griptape_nodes.exe_types.core_types import Parameter
from griptape_nodes.traits.options import Options
from griptape_nodes.traits.slider import Slider


def _slider_parameter() -> Parameter:
    return Parameter(name="steps", type="int", tooltip="Sampling steps", traits={Slider(min_val=1, max_val=50)})


class TestTraitUIOptionOwnership:
    def test_trait_options_are_reported_but_not_stored(self) -> None:
        parameter = _slider_parameter()

        assert parameter.ui_options["slider"] == {"min_val": 1, "max_val": 50}
        assert parameter.authored_ui_options() == {}

    def test_an_unrelated_ui_tweak_does_not_capture_trait_options(self) -> None:
        parameter = _slider_parameter()

        parameter.hide = True

        assert "slider" not in parameter.authored_ui_options()
        assert parameter.ui_options["slider"] == {"min_val": 1, "max_val": 50}

    def test_a_trait_change_still_reaches_the_ui_after_a_ui_tweak(self) -> None:
        # The regression: hiding the parameter used to freeze a copy of the slider's options
        # in stored state, so later changes to the trait updated the validator but not the UI.
        slider = Slider(min_val=1, max_val=50)
        parameter = Parameter(name="steps", type="int", tooltip="Sampling steps", traits={slider})
        parameter.hide = True

        slider.max = 10

        assert parameter.ui_options["slider"] == {"min_val": 1, "max_val": 10}

    def test_a_trait_owns_its_keys_over_stored_values(self) -> None:
        parameter = Parameter(
            name="steps",
            type="int",
            tooltip="Sampling steps",
            ui_options={"slider": {"min_val": 1, "max_val": 50}},
            traits={Slider(min_val=1, max_val=10)},
        )

        assert parameter.ui_options["slider"] == {"min_val": 1, "max_val": 10}

    def test_options_reports_choices_without_writing_them_to_stored_options(self) -> None:
        parameter = Parameter(
            name="model", type="str", tooltip="Pick a model", traits={Options(choices=["sdxl", "sd3"])}
        )

        assert parameter.ui_options["simple_dropdown"] == ["sdxl", "sd3"]
        assert "simple_dropdown" not in parameter.authored_ui_options()
