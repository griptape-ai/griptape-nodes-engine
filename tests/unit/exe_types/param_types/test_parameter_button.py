"""ParameterButton's href is stored as the Button trait's ``button_link``."""

from griptape_nodes.exe_types.param_types.parameter_button import ParameterButton
from griptape_nodes.traits.button import Button


def _click(trait: Button) -> str | None:
    callback = trait.on_click_callback
    assert callback is not None
    result = callback(trait, trait.get_button_details())
    assert result is not None
    return getattr(result.response, "href", None)


class TestHrefRoutesToTheLink:
    def test_href_reads_back_off_the_trait(self) -> None:
        parameter = ParameterButton(name="docs", href="https://docs.example.test")

        assert parameter.href == "https://docs.example.test"

    def test_href_survives_a_save(self) -> None:
        parameter = ParameterButton(name="docs", href="https://docs.example.test")

        state = parameter.trait_states()[0]["trait_state"]

        assert state["button_link"] == "https://docs.example.test"

    def test_a_click_follows_a_restored_link(self) -> None:
        parameter = ParameterButton(name="docs", href="https://docs.example.test")
        trait = parameter._get_button_trait()

        trait.apply_state({"button_link": "https://restored.example.test"})

        assert _click(trait) == "https://restored.example.test"

    def test_clearing_href_removes_the_link(self) -> None:
        parameter = ParameterButton(name="docs", href="https://docs.example.test")

        parameter.href = None

        assert parameter.href is None

    def test_a_click_opens_the_href(self) -> None:
        parameter = ParameterButton(name="docs", href="https://docs.example.test")

        assert _click(parameter._get_button_trait()) == "https://docs.example.test"

    def test_on_click_wins_over_href(self) -> None:
        clicks: list[str] = []
        parameter = ParameterButton(
            name="both",
            href="https://docs.example.test",
            on_click=lambda button, details: clicks.append("handler"),  # noqa: ARG005
        )
        trait = parameter._get_button_trait()

        callback = trait.on_click_callback
        assert callback is not None
        callback(trait, trait.get_button_details())

        assert clicks == ["handler"]
        assert parameter.href == "https://docs.example.test"

    def test_href_replaces_an_on_click_set_after_construction(self) -> None:
        """Setting href is how a button already wired to ``on_click`` switches to a link."""
        parameter = ParameterButton(name="docs", on_click=lambda button, details: None)  # noqa: ARG005

        parameter.href = "https://docs.example.test"

        assert parameter.href == "https://docs.example.test"
        assert _click(parameter._get_button_trait()) == "https://docs.example.test"
