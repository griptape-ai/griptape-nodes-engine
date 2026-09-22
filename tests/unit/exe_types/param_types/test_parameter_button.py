"""ParameterButton's href routes to Button.button_link instead of a second, unsaveable callback."""

import pytest

from griptape_nodes.exe_types.param_types.parameter_button import ParameterButton


class TestHrefRoutesToTheLink:
    def test_href_reads_back_off_the_trait(self) -> None:
        parameter = ParameterButton(name="docs", href="https://docs.example.test")

        assert parameter.href == "https://docs.example.test"

    def test_href_survives_a_save(self) -> None:
        parameter = ParameterButton(name="docs", href="https://docs.example.test")

        state = parameter.trait_states()[0]["trait_state"]

        assert state["button_link"] == "https://docs.example.test"

    def test_href_needs_no_callback(self) -> None:
        """An href is the Button's own link feature, not a second handler-producing path."""
        parameter = ParameterButton(name="docs", href="https://docs.example.test")

        assert parameter._get_button_trait()._on_click_handler is None

    def test_clearing_href_removes_the_link(self) -> None:
        parameter = ParameterButton(name="docs", href="https://docs.example.test")

        parameter.href = None

        assert parameter.href is None

    def test_a_click_opens_the_href(self) -> None:
        parameter = ParameterButton(name="docs", href="https://docs.example.test")
        trait = parameter._get_button_trait()
        callback = trait.on_click_callback

        assert callback is not None
        result = callback(trait, trait.get_button_details())
        assert result is not None
        href = getattr(result.response, "href", None)
        assert href == "https://docs.example.test"

    def test_href_and_on_click_together_are_rejected(self) -> None:
        """Both name one click action; Button's own constructor enforces there is only one."""
        with pytest.raises(ValueError, match="Cannot specify both"):
            ParameterButton(name="bad", href="https://docs.example.test", on_click=lambda button, details: None)  # noqa: ARG005

    def test_href_replaces_an_on_click_set_after_construction(self) -> None:
        """Setting href is how a button already wired to ``on_click`` switches to a link."""
        parameter = ParameterButton(name="docs", on_click=lambda button, details: None)  # noqa: ARG005

        parameter.href = "https://docs.example.test"

        assert parameter.href == "https://docs.example.test"
        trait = parameter._get_button_trait()
        callback = parameter.on_click_callback
        assert callback is not None
        result = callback(trait, trait.get_button_details())
        assert result is not None
        assert getattr(result.response, "href", None) == "https://docs.example.test"
