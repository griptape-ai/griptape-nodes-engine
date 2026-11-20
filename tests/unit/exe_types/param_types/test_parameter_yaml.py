import datetime

import pytest

from griptape_nodes.exe_types.core_types import InvalidParameterValueError
from griptape_nodes.exe_types.param_types.parameter_yaml import ParameterYaml

PROSE_WITH_COLON = "CHAR: Annie\nSTYLE:\n  - EDIT: should feel tactile and handled: soft cotton, rough canvas\n"


def _rejection(value: str) -> str:
    parameter = ParameterYaml(name="yaml")
    with pytest.raises(InvalidParameterValueError, match="Invalid YAML") as exc_info:
        parameter._convert_to_yaml(value)
    return str(exc_info.value)


def _stdlib_date_error(year: int, month: int, day: int) -> str:
    # The wording varies by Python version, so tests compare against the stdlib rather than a literal.
    try:
        datetime.date(year, month, day)
    except ValueError as e:
        return str(e)
    msg = f"{year}-{month}-{day} is a valid date"
    raise AssertionError(msg)


class TestInvalidYamlMessage:
    def test_colon_in_a_value_suggests_quoting_or_a_block_scalar(self) -> None:
        message = _rejection(PROSE_WITH_COLON)

        assert "A colon followed by a space starts a new key" in message
        assert "wrap the value in quotes" in message
        assert "key: |" in message

    def test_prose_continued_on_the_next_line_gets_the_colon_hint(self) -> None:
        message = _rejection("EDIT: should feel tactile\n  and handled: soft cotton\n")

        assert "wrap the value in quotes" in message

    def test_value_with_double_quotes_suggests_only_a_block_scalar(self) -> None:
        message = _rejection('EDIT: she\'s "hi" and handled: soft cotton\n')

        assert "key: |" in message
        assert "wrap the value in quotes" not in message

    def test_invalid_date_is_reported_as_invalid_yaml(self) -> None:
        message = _rejection("shot: 2024-02-30\n")

        assert _stdlib_date_error(2024, 2, 30) in message

    def test_names_and_quotes_the_line(self) -> None:
        message = _rejection(PROSE_WITH_COLON)

        assert message.startswith(
            "Invalid YAML on line 3: - EDIT: should feel tactile and handled: soft cotton, rough canvas\n\n"
        )

    def test_other_errors_skip_the_colon_hint(self) -> None:
        message = _rejection("a: [1, 2")

        assert "Invalid YAML on line 1: a: [1, 2" in message
        assert "wrap the value in quotes" not in message

    def test_misindented_key_skips_the_colon_hint(self) -> None:
        message = _rejection("a: b\n  c: d")

        assert "Mapping values are not allowed here." in message
        assert "wrap the value in quotes" not in message

    @pytest.mark.parametrize(
        "value",
        [
            'CHAR: Annie\nSTYLE:\n  - EDIT: "should feel tactile and handled: soft cotton"\n',
            "CHAR: Annie\nSTYLE:\n  - EDIT: |\n      should feel tactile and handled: soft cotton\n",
        ],
    )
    def test_the_suggested_fixes_parse(self, value: str) -> None:
        parameter = ParameterYaml(name="yaml")

        assert parameter._convert_to_yaml(value) == value
