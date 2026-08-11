import pytest  # type: ignore[reportMissingImports]

from griptape_nodes.exe_types.core_types import Parameter
from griptape_nodes.traits.suggestions import Suggestion, Suggestions


def _parameter_with(trait: Suggestions) -> Parameter:
    param = Parameter(name="model", input_types=["str"], type="str", output_type="str", tooltip="test")
    param.add_trait(trait)
    return param


class TestSuggestionsUiOptions:
    def test_publishes_plain_strings_as_rows(self) -> None:
        param = _parameter_with(Suggestions(choices=["gpt-4.1", "claude-sonnet-4-5"]))

        assert param.ui_options["suggestions"] == [{"name": "gpt-4.1"}, {"name": "claude-sonnet-4-5"}]

    def test_publishes_row_decoration_only_when_set(self) -> None:
        param = _parameter_with(
            Suggestions(choices=[Suggestion("gpt-4.1", label="GPT-4.1", subtitle="OpenAI"), "claude-sonnet-4-5"])
        )

        assert param.ui_options["suggestions"] == [
            {"name": "gpt-4.1", "label": "GPT-4.1", "subtitle": "OpenAI"},
            {"name": "claude-sonnet-4-5"},
        ]

    def test_publishes_an_empty_list_when_there_are_no_choices(self) -> None:
        param = _parameter_with(Suggestions())

        assert param.ui_options["suggestions"] == []

    def test_a_runtime_update_reaches_the_editor(self) -> None:
        trait = Suggestions(choices=["gpt-4.1"])
        param = _parameter_with(trait)

        trait.choices = ["gemini-2.5-pro"]

        assert param.ui_options["suggestions"] == [{"name": "gemini-2.5-pro"}]

    def test_does_not_claim_the_dropdown_key(self) -> None:
        """The editor renders a dropdown when simple_dropdown is present, which is the wrong widget here."""
        param = _parameter_with(Suggestions(choices=["gpt-4.1"]))

        assert "simple_dropdown" not in param.ui_options


class TestSuggestionsDoesNotConstrain:
    def test_adds_no_converter(self) -> None:
        param = _parameter_with(Suggestions(choices=["gpt-4.1"]))

        assert param.converters == []

    def test_adds_no_validator(self) -> None:
        param = _parameter_with(Suggestions(choices=["gpt-4.1"]))

        assert param.validators == []


class TestSuggestionsRejectsBadRows:
    def test_rejects_a_blank_string(self) -> None:
        with pytest.raises(ValueError, match="suggestion 2 is blank"):
            Suggestions(choices=["gpt-4.1", "   "])

    def test_rejects_a_row_with_a_blank_name(self) -> None:
        with pytest.raises(ValueError, match="suggestion 1 has a blank name"):
            Suggestions(choices=[Suggestion("", label="GPT-4.1")])

    def test_rejects_a_row_that_is_neither_text_nor_a_suggestion(self) -> None:
        with pytest.raises(TypeError, match="suggestion 1 is a dict"):
            Suggestions(choices=[{"name": "gpt-4.1"}])  # type: ignore[list-item]
