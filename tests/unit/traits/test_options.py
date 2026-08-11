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


class TestOptionsConstrained:
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


class TestOptionsUserCreated:
    def test_keeps_unknown_value(self) -> None:
        param = _parameter_with(Options(choices=CHOICES, allow_user_created_options=True))

        assert _convert(param, "chartreuse") == "chartreuse"

    def test_keeps_known_value(self) -> None:
        param = _parameter_with(Options(choices=CHOICES, allow_user_created_options=True))

        assert _convert(param, "green") == "green"

    def test_accepts_unknown_value(self) -> None:
        param = _parameter_with(Options(choices=CHOICES, allow_user_created_options=True))

        _validate(param, "chartreuse")

    def test_keeps_unknown_value_when_choices_are_empty(self) -> None:
        """Constrained mode has no first choice to fall back on; user-created mode never needs one."""
        param = _parameter_with(Options(choices=[], allow_user_created_options=True))

        assert _convert(param, "chartreuse") == "chartreuse"
        _validate(param, "chartreuse")

    def test_survives_a_choices_update(self) -> None:
        """Updating choices at runtime rewrites ui_options, which must not drop the flag."""
        trait = Options(choices=CHOICES, allow_user_created_options=True)
        param = _parameter_with(trait)

        trait.choices = ["cyan", "magenta"]

        assert param.ui_options["allow_user_created_options"] is True
        assert _convert(param, "chartreuse") == "chartreuse"


class TestOptionsUiOptions:
    def test_defaults_to_constrained(self) -> None:
        param = _parameter_with(Options(choices=CHOICES))

        assert param.ui_options["allow_user_created_options"] is False

    def test_publishes_the_flag(self) -> None:
        param = _parameter_with(Options(choices=CHOICES, allow_user_created_options=True))

        assert param.ui_options["allow_user_created_options"] is True

    def test_a_manual_ui_option_wins_over_the_constructor(self) -> None:
        """Parameter.ui_options lets a manual entry override a trait's, and that is what the editor renders.

        The converter has to follow it, otherwise the engine snaps back a value the widget offered.
        """
        param = _parameter_with(Options(choices=CHOICES))
        param.ui_options = {"allow_user_created_options": True}

        assert _convert(param, "chartreuse") == "chartreuse"
        _validate(param, "chartreuse")

    def test_a_manual_ui_option_can_re_constrain_the_parameter(self) -> None:
        param = _parameter_with(Options(choices=CHOICES, allow_user_created_options=True))
        param.ui_options = {"allow_user_created_options": False}

        assert _convert(param, "chartreuse") == "red"
        with pytest.raises(ValueError, match="Choice not allowed"):
            _validate(param, "chartreuse")
