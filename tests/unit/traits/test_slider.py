import re
from typing import Any

import pytest  # type: ignore[reportMissingImports]

from griptape_nodes.exe_types.core_types import Parameter
from griptape_nodes.exe_types.param_types.parameter_float import ParameterFloat
from griptape_nodes.traits.slider import Slider

MIN_VAL = -10.0
MAX_VAL = 10.0
OUT_OF_RANGE_ERROR = re.escape(f"must be between {MIN_VAL} and {MAX_VAL}")


def run_validators(param: Parameter, value: Any) -> None:
    """Apply every validator the parameter exposes, trait-derived ones included."""
    for validator in param.validators:
        validator(param, value)


class TestSliderHardLimits:
    """Default behavior: min/max are enforced and out-of-range values raise."""

    @pytest.fixture
    def trait(self) -> Slider:
        return Slider(min_val=MIN_VAL, max_val=MAX_VAL)

    @pytest.fixture
    def param(self) -> ParameterFloat:
        return ParameterFloat(name="defocus", slider=True, min_val=MIN_VAL, max_val=MAX_VAL)

    def test_attaches_a_validator(self, trait: Slider) -> None:
        assert len(trait.validators_for_trait()) == 1

    def test_in_range_value_passes(self, trait: Slider, param: ParameterFloat) -> None:
        validate = trait.validators_for_trait()[0]

        validate(param, 5.0)

    @pytest.mark.parametrize("value", [10.5, -10.5, 1000.0])
    def test_out_of_range_value_raises(self, trait: Slider, param: ParameterFloat, value: float) -> None:
        validate = trait.validators_for_trait()[0]

        with pytest.raises(ValueError, match=OUT_OF_RANGE_ERROR):
            validate(param, value)

    def test_error_message_names_the_parameter_and_the_value(self, trait: Slider, param: ParameterFloat) -> None:
        validate = trait.validators_for_trait()[0]

        with pytest.raises(ValueError, match=re.escape("Attempted to set 'defocus' to 42.0")):
            validate(param, 42.0)

    def test_ui_options_omit_the_soft_limits_key(self, trait: Slider) -> None:
        assert trait.ui_options_for_trait() == {"slider": {"min_val": MIN_VAL, "max_val": MAX_VAL}}


class TestSliderSoftLimits:
    """soft_limits=True: min/max only size the track, so any value is accepted."""

    @pytest.fixture
    def trait(self) -> Slider:
        return Slider(min_val=MIN_VAL, max_val=MAX_VAL, soft_limits=True)

    def test_attaches_no_validator(self, trait: Slider) -> None:
        assert trait.validators_for_trait() == []

    def test_ui_options_advertise_soft_limits(self, trait: Slider) -> None:
        assert trait.ui_options_for_trait() == {"slider": {"min_val": MIN_VAL, "max_val": MAX_VAL, "soft_limits": True}}

    def test_range_is_still_reported_for_the_widget(self, trait: Slider) -> None:
        assert trait.min == MIN_VAL
        assert trait.max == MAX_VAL


class TestParameterNumberSoftLimits:
    """soft_limits is plumbed from ParameterInt/ParameterFloat down to the Slider trait."""

    def test_out_of_range_value_is_accepted(self) -> None:
        param = ParameterFloat(name="defocus", slider=True, min_val=MIN_VAL, max_val=MAX_VAL, soft_limits=True)

        run_validators(param, 250.0)

    def test_out_of_range_value_is_rejected_by_default(self) -> None:
        param = ParameterFloat(name="defocus", slider=True, min_val=MIN_VAL, max_val=MAX_VAL)

        with pytest.raises(ValueError, match=OUT_OF_RANGE_ERROR):
            run_validators(param, 250.0)

    def test_soft_limits_property_reflects_the_trait(self) -> None:
        soft = ParameterFloat(name="soft", slider=True, min_val=0.0, max_val=1.0, soft_limits=True)
        hard = ParameterFloat(name="hard", slider=True, min_val=0.0, max_val=1.0)

        assert soft.soft_limits is True
        assert hard.soft_limits is False

    def test_soft_limits_survives_a_min_max_change(self) -> None:
        param = ParameterFloat(name="defocus", slider=True, min_val=0.0, max_val=1.0, soft_limits=True)

        param.max_val = 100.0

        assert param.soft_limits is True
        assert param.validators == []

    def test_soft_limits_can_be_toggled_at_runtime(self) -> None:
        param = ParameterFloat(name="defocus", slider=True, min_val=0.0, max_val=1.0)

        param.soft_limits = True

        assert param.soft_limits is True
        run_validators(param, 250.0)

    def test_soft_limits_requires_a_slider(self) -> None:
        with pytest.raises(ValueError, match="soft_limits only applies to sliders"):
            ParameterFloat(name="defocus", min_val=MIN_VAL, max_val=MAX_VAL, soft_limits=True)

    def test_enabling_soft_limits_without_a_slider_raises(self) -> None:
        param = ParameterFloat(name="defocus", min_val=MIN_VAL, max_val=MAX_VAL)

        with pytest.raises(ValueError, match="Cannot enable soft_limits without a slider"):
            param.soft_limits = True
