import pytest

from griptape_nodes.exe_types.param_types.parameter_yaml import ParameterYaml

PROSE_WITH_COLON = "CHAR: Annie\nSTYLE:\n  - EDIT: should feel tactile and handled: soft cotton, rough canvas\n"


def _rejection(value: str) -> str:
    parameter = ParameterYaml(name="yaml")
    with pytest.raises(ValueError, match="not valid YAML") as exc_info:
        parameter._convert_to_yaml(value)
    return str(exc_info.value)


class TestInvalidYamlMessage:
    def test_colon_in_a_value_suggests_quoting_or_a_block_scalar(self) -> None:
        message = _rejection(PROSE_WITH_COLON)

        assert "a value contains a colon followed by a space" in message
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

        assert "day is out of range for month" in message

    def test_names_the_line_and_column(self) -> None:
        message = _rejection(PROSE_WITH_COLON)

        assert "line 3, column 42" in message
        assert "Line 3: - EDIT: should feel tactile and handled: soft cotton, rough canvas" in message

    def test_other_errors_skip_the_colon_hint(self) -> None:
        message = _rejection("a: [1, 2")

        assert "line 1, column 9" in message
        assert "wrap the value in quotes" not in message

    def test_misindented_key_skips_the_colon_hint(self) -> None:
        message = _rejection("a: b\n  c: d")

        assert "mapping values are not allowed here" in message
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
