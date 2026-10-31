"""Tests for `MaxCount` and `IfParameter` on their own."""

from __future__ import annotations

import pytest

from griptape_nodes.exe_types.core_types import ParameterList, ParameterMode
from griptape_nodes.exe_types.param_types.parameter_bool import ParameterBool
from griptape_nodes.exe_types.param_types.parameter_string import ParameterString
from griptape_nodes.exe_types.validators.common import IfParameter, MaxCount
from tests.unit.exe_types.mocks import MockNode

DOCS = "https://example.com/limits"


class TestMaxCount:
    def test_allows_the_maximum(self) -> None:
        assert MaxCount(3).check([1, 2, 3], subject="prompts", docs_url=None) is None

    def test_refuses_more_than_the_maximum(self) -> None:
        error = MaxCount(2, noun="prompts").check([1, 2, 3], subject="prompts", docs_url=DOCS)
        assert str(error) == "3 prompts are connected to 'prompts' and the limit is 2. Remove 1."
        assert error is not None
        assert error.links
        assert error.links[0].url == DOCS


class TestIfParameter:
    PRO = (MaxCount(4),)
    DEFAULT = (MaxCount(1), MaxCount(2, noun="prompts"))

    def _node(self, model: str | None) -> MockNode:
        node = MockNode(name="node")
        node.add_parameter(ParameterString(name="model"))
        if model is not None:
            node.set_parameter_value("model", model)
        return node

    def test_picks_the_list_for_the_current_value(self) -> None:
        by_model = IfParameter("model", {"pro": self.PRO}, default=self.DEFAULT)
        assert list(by_model.selected(self._node("pro"))) == list(self.PRO)

    @pytest.mark.parametrize("model", ["other", None])
    def test_falls_back_to_the_default(self, model: str | None) -> None:
        by_model = IfParameter("model", {"pro": self.PRO}, default=self.DEFAULT)
        assert list(by_model.selected(self._node(model))) == list(self.DEFAULT)

    def test_has_no_default_unless_given(self) -> None:
        assert list(IfParameter("model", {"pro": self.PRO}).selected(self._node("other"))) == []

    def test_lists_every_validator_it_can_pick(self) -> None:
        by_model = IfParameter("model", {"pro": self.PRO}, default=self.DEFAULT)
        assert list(by_model.candidates()) == [*self.PRO, *self.DEFAULT]

    def test_a_nested_if_parameter_picks_in_turn(self) -> None:
        node = self._node("pro")
        node.add_parameter(ParameterString(name="size"))
        node.set_parameter_value("size", "large")
        nested = IfParameter(
            "model",
            {"pro": [MaxCount(9), IfParameter("size", {"large": self.PRO}, default=self.DEFAULT)]},
        )
        assert list(nested.selected(node)) == [MaxCount(9), *self.PRO]
        node.set_parameter_value("size", "small")
        assert list(nested.selected(node)) == [MaxCount(9), *self.DEFAULT]

    def test_lists_every_validator_reachable_through_nesting(self) -> None:
        nested = IfParameter("model", {"pro": [IfParameter("size", {"large": self.PRO})]}, default=self.DEFAULT)
        assert list(nested.candidates()) == [*self.PRO, *self.DEFAULT]

    @pytest.mark.parametrize(("strict", "expected"), [(True, []), (False, [MaxCount(3)])])
    def test_picks_by_a_value_that_is_not_text(self, strict: bool, expected: list) -> None:  # noqa: FBT001
        node = MockNode(name="node")
        node.add_parameter(ParameterBool(name="strict"))
        node.set_parameter_value("strict", strict)
        by_strict = IfParameter("strict", {True: []}, default=[MaxCount(3)])
        assert list(by_strict.selected(node)) == expected

    @pytest.mark.parametrize(("examples", "expected"), [([], list(PRO)), (["a cat"], list(DEFAULT))])
    def test_key_turns_the_value_into_what_is_looked_up(self, examples: list[str], expected: list) -> None:
        node = MockNode(name="node")
        node.add_parameter(
            ParameterList(
                name="examples",
                tooltip="",
                input_types=["str"],
                type="str",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        node.set_parameter_value("examples", examples)
        by_examples = IfParameter("examples", {False: self.PRO}, default=self.DEFAULT, key=bool)
        assert list(by_examples.selected(node)) == expected

    def test_checks_nothing_itself(self) -> None:
        by_model = IfParameter("model", {"pro": self.PRO})
        assert by_model.check("x", subject="s", docs_url=None) is None

    def test_is_frozen_and_hashable(self) -> None:
        first = IfParameter("model", {"pro": self.PRO}, default=self.DEFAULT)
        second = IfParameter("model", {"pro": list(self.PRO)}, default=list(self.DEFAULT))
        assert first == second
        assert hash(first) == hash(second)
        with pytest.raises(AttributeError):
            first.parameter = "size"  # pyright: ignore[reportAttributeAccessIssue]
