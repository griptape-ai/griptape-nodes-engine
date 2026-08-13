import pytest  # type: ignore[reportMissingImports]

from griptape_nodes.exe_types.core_types import Parameter
from griptape_nodes.traits.options import Options

CHOICES = ["red", "green", "blue"]


def _parameter_with(trait: Options) -> Parameter:
    param = Parameter(name="color", input_types=["str"], type="str", output_type="str", tooltip="test")
    param.add_trait(trait)
    return param


def _convert(param: Parameter, value: object) -> object:
    for converter in param.converters:
        value = converter(value)
    return value


def _validate(param: Parameter, value: object) -> None:
    for validator in param.validators:
        validator(param, value)


class TestConstrainedChoices:
    def test_snaps_unknown_value_to_first_choice(self) -> None:
        param = _parameter_with(Options(choices=CHOICES))

        assert _convert(param, "chartreuse") == "red"

    def test_keeps_known_value(self) -> None:
        param = _parameter_with(Options(choices=CHOICES))

        assert _convert(param, "green") == "green"

    def test_rejects_unknown_value(self) -> None:
        param = _parameter_with(Options(choices=CHOICES))

        with pytest.raises(ValueError, match="Choice not allowed"):
            _validate(param, "chartreuse")


class TestCustomChoices:
    def test_keeps_unknown_value(self) -> None:
        param = _parameter_with(Options(choices=CHOICES, allow_custom=True))

        assert _convert(param, "chartreuse") == "chartreuse"

    def test_accepts_unknown_value(self) -> None:
        param = _parameter_with(Options(choices=CHOICES, allow_custom=True))

        _validate(param, "chartreuse")

    def test_adds_no_converter_or_validator(self) -> None:
        """Nothing consults the choices, which is why they can be replaced at run time."""
        param = _parameter_with(Options(choices=CHOICES, allow_custom=True))

        assert param.converters == []
        assert param.validators == []

    def test_keeps_unknown_value_when_choices_are_empty(self) -> None:
        """Constrained mode has no first choice to fall back on; custom mode never needs one."""
        param = _parameter_with(Options(choices=[], allow_custom=True))

        assert _convert(param, "chartreuse") == "chartreuse"
        _validate(param, "chartreuse")

    def test_survives_a_choices_update(self) -> None:
        """Updating choices at runtime rewrites ui_options, which must not drop the flag."""
        trait = Options(choices=CHOICES, allow_custom=True)
        param = _parameter_with(trait)

        trait.choices = ["cyan", "magenta"]

        assert param.ui_options["allow_custom"] is True
        assert _convert(param, "chartreuse") == "chartreuse"


class TestPublishedUiOptions:
    def test_omits_the_flag_by_default(self) -> None:
        """Absent rather than False, so an existing dropdown's saved ui_options are unchanged."""
        param = _parameter_with(Options(choices=CHOICES))

        assert "allow_custom" not in param.ui_options

    def test_publishes_the_flag_when_set(self) -> None:
        param = _parameter_with(Options(choices=CHOICES, allow_custom=True))

        assert param.ui_options["allow_custom"] is True

    def test_still_publishes_the_choices(self) -> None:
        """The editor filters the typeahead from the same key the dropdown reads."""
        param = _parameter_with(Options(choices=CHOICES, allow_custom=True))

        assert param.ui_options["simple_dropdown"] == CHOICES
