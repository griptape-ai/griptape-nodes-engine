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


class TestAnEchoedMergedViewCannotBecomeStoredState:
    """The editor is handed the merged view, so writing it back must not store trait keys."""

    def test_echoing_the_merged_view_back_stores_only_authored_options(self) -> None:
        parameter = _slider_parameter()
        parameter.hide = True

        # What the editor received, with one authored option flipped and sent back.
        echoed = parameter.to_dict()["ui_options"]
        echoed["hide"] = False
        parameter.ui_options = echoed

        assert parameter.authored_ui_options() == {"hide": False}

    def test_the_trait_still_owns_its_keys_after_an_echo(self) -> None:
        slider = Slider(min_val=1, max_val=50)
        parameter = Parameter(name="steps", type="int", tooltip="Sampling steps", traits={slider})

        parameter.ui_options = parameter.to_dict()["ui_options"]
        slider.max = 10

        # Would report the echoed copy's max of 50 if the echo had been stored.
        assert parameter.ui_options["slider"] == {"min_val": 1, "max_val": 10}

    def test_a_parameter_with_no_traits_stores_what_it_is_given(self) -> None:
        parameter = Parameter(name="steps", type="int", tooltip="Sampling steps")

        parameter.ui_options = {"hide": True, "display_name": "Steps"}

        assert parameter.authored_ui_options() == {"hide": True, "display_name": "Steps"}


class TestClearingAUIOption:
    """Unsetting an option goes through the same authored view as setting one."""

    def test_clearing_one_option_does_not_capture_trait_options(self) -> None:
        parameter = _slider_parameter()
        parameter.display_name = "Steps"

        parameter.display_name = None

        assert parameter.authored_ui_options() == {}
        assert parameter.ui_options["slider"] == {"min_val": 1, "max_val": 50}

    def test_a_trait_change_still_reaches_the_ui_after_a_clear(self) -> None:
        slider = Slider(min_val=1, max_val=50)
        parameter = Parameter(name="steps", type="int", tooltip="Sampling steps", traits={slider})
        parameter.display_name = "Steps"
        parameter.display_name = None

        slider.max = 10

        assert parameter.ui_options["slider"] == {"min_val": 1, "max_val": 10}

    def test_clearing_leaves_the_other_authored_options_alone(self) -> None:
        parameter = _slider_parameter()
        parameter.hide = True
        parameter.display_name = "Steps"

        parameter.display_name = None

        assert parameter.authored_ui_options() == {"hide": True}

    def test_clearing_an_option_that_was_never_set_is_harmless(self) -> None:
        parameter = _slider_parameter()

        parameter.display_name = None

        assert parameter.authored_ui_options() == {}
