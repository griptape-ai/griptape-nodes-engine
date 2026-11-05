"""Tests for the checks any validator shares: counting a list, and naming an input in a message."""

from __future__ import annotations

from griptape_nodes.exe_types.node_error import NodeError
from griptape_nodes.exe_types.validators._checks import check_count, input_subject

DOCS = "https://example.com/limits"


class TestCheckCount:
    def test_allows_the_maximum(self) -> None:
        assert check_count(3, maximum=3, parameter="prompts") is None

    def test_rejects_more_than_the_maximum(self) -> None:
        error = check_count(5, maximum=3, parameter="prompts", noun="prompts", docs_url=DOCS)
        assert isinstance(error, NodeError)
        assert str(error) == "5 prompts are connected to 'prompts' and the limit is 3. Remove 2."
        assert error.links is not None
        assert error.links[0].url == DOCS


class TestInputSubject:
    def test_names_a_position_in_a_list(self) -> None:
        assert input_subject("prompts", noun="prompt", position=2) == "Prompt 2 in 'prompts'"

    def test_names_a_single_input(self) -> None:
        assert input_subject("Prompt", noun="input") == "The input in 'Prompt'"

    def test_keeps_a_nouns_own_capitals_at_a_position(self) -> None:
        assert input_subject("icons", noun="SVG icon", position=2) == "SVG icon 2 in 'icons'"
        assert input_subject("loras", noun="LoRA file", position=1) == "LoRA file 1 in 'loras'"
