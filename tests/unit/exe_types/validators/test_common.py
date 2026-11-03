"""Tests for `MaxCount`, and for `IfParameter` and the narrowing rules as `_pick_validators` walks them."""

from __future__ import annotations

import pytest

from griptape_nodes.exe_types.core_types import ParameterList, ParameterMode
from griptape_nodes.exe_types.param_types.parameter_bool import ParameterBool
from griptape_nodes.exe_types.param_types.parameter_string import ParameterString
from griptape_nodes.exe_types.validators.base import IfParameter, PreRunRule, PreRunValidator, _pick_validators
from griptape_nodes.exe_types.validators.common import MaxCount
from griptape_nodes.exe_types.validators.context import CheckContext
from griptape_nodes.exe_types.validators.runner import _read_setting
from tests.unit.exe_types.mocks import MockNode

DOCS = "https://example.com/limits"


def _ctx(subject: str, docs_url: str | None) -> CheckContext:
    parameter = ParameterString(name="prompts")
    return CheckContext(parameter=parameter, value=None, subject=subject, docs_url=docs_url)


def _picked_on(node: MockNode, rule: PreRunRule) -> list[PreRunValidator]:
    """The validators `rule` picks on `node` now, as the runner resolves them."""

    def branches(choice: IfParameter) -> tuple[PreRunRule, ...]:
        return choice._listed_for(_read_setting(node, choice.parameter, for_parameter="prompts"))

    return [picked.validator for picked in _pick_validators(rule, branches)]


def _every_branch(rule: PreRunRule) -> list[PreRunValidator]:
    """Every validator `rule` can pick, as the build-time fit check walks it."""
    return [picked.validator for picked in _pick_validators(rule, IfParameter._every_branch)]


class TestMaxCount:
    def test_allows_the_maximum(self) -> None:
        assert MaxCount(3).check([1, 2, 3], _ctx("prompts", None)) is None

    def test_refuses_more_than_the_maximum(self) -> None:
        error = MaxCount(2, noun="prompts").check([1, 2, 3], _ctx("prompts", DOCS))
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
        assert _picked_on(self._node("pro"), by_model) == list(self.PRO)

    @pytest.mark.parametrize("model", ["other", None])
    def test_falls_back_to_the_default(self, model: str | None) -> None:
        by_model = IfParameter("model", {"pro": self.PRO}, default=self.DEFAULT)
        assert _picked_on(self._node(model), by_model) == list(self.DEFAULT)

    def test_has_no_default_unless_given(self) -> None:
        assert _picked_on(self._node("other"), IfParameter("model", {"pro": self.PRO})) == []

    def test_lists_every_validator_it_can_pick(self) -> None:
        by_model = IfParameter("model", {"pro": self.PRO}, default=self.DEFAULT)
        assert _every_branch(by_model) == [*self.PRO, *self.DEFAULT]

    def test_a_nested_if_parameter_picks_in_turn(self) -> None:
        node = self._node("pro")
        node.add_parameter(ParameterString(name="size"))
        node.set_parameter_value("size", "large")
        nested = IfParameter(
            "model",
            {"pro": [MaxCount(9), IfParameter("size", {"large": self.PRO}, default=self.DEFAULT)]},
        )
        assert _picked_on(node, nested) == [MaxCount(9), *self.PRO]
        node.set_parameter_value("size", "small")
        assert _picked_on(node, nested) == [MaxCount(9), *self.DEFAULT]

    def test_lists_every_validator_reachable_through_nesting(self) -> None:
        nested = IfParameter("model", {"pro": [IfParameter("size", {"large": self.PRO})]}, default=self.DEFAULT)
        assert _every_branch(nested) == [*self.PRO, *self.DEFAULT]

    @pytest.mark.parametrize(("strict", "expected"), [(True, []), (False, [MaxCount(3)])])
    def test_picks_by_a_value_that_is_not_text(self, strict: bool, expected: list) -> None:  # noqa: FBT001
        node = MockNode(name="node")
        node.add_parameter(ParameterBool(name="strict"))
        node.set_parameter_value("strict", strict)
        by_strict = IfParameter("strict", {True: []}, default=[MaxCount(3)])
        assert _picked_on(node, by_strict) == expected

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
        assert _picked_on(node, by_examples) == expected

    def test_is_a_rule_and_not_a_validator(self) -> None:
        by_model = IfParameter("model", {"pro": self.PRO})
        assert isinstance(by_model, PreRunRule)
        assert not isinstance(by_model, PreRunValidator)

    def test_a_misspelled_parameter_is_refused_instead_of_picking_the_default(self) -> None:
        by_model = IfParameter("model_name_typo", {"pro": self.PRO}, default=self.DEFAULT)
        with pytest.raises(ValueError, match="no parameter called 'model_name_typo'"):
            _picked_on(self._node("pro"), by_model)

    def test_is_frozen_and_hashable(self) -> None:
        first = IfParameter("model", {"pro": self.PRO}, default=self.DEFAULT)
        second = IfParameter("model", {"pro": list(self.PRO)}, default=list(self.DEFAULT))
        assert first == second
        assert hash(first) == hash(second)
        with pytest.raises(AttributeError):
            first.parameter = "size"  # pyright: ignore[reportAttributeAccessIssue]


class TestPickValidators:
    """The one walk of a rule tree: picks resolve wherever they sit, and narrowing carries down."""

    def _node(self) -> MockNode:
        node = MockNode(name="node")
        node.add_parameter(ParameterString(name="model"))
        node.set_parameter_value("model", "pro")
        return node

    def test_resolves_an_if_parameter_inside_a_narrowing_rule(self) -> None:
        nested = IfParameter("model", {"pro": [MaxCount(1)]}).first(2).when(lambda _value, _ctx: True)
        assert _picked_on(self._node(), nested) == [MaxCount(1)]

    def test_resolves_an_if_parameter_nested_in_another(self) -> None:
        nested = IfParameter("model", {"pro": [IfParameter("model", {"pro": [MaxCount(1)]})]})
        assert _picked_on(self._node(), nested) == [MaxCount(1)]

    def test_carries_first_and_conditions_down_to_each_validator(self) -> None:
        def is_long(value: object, _ctx: CheckContext) -> bool:
            return len(str(value)) > 3  # noqa: PLR2004

        rule = IfParameter("model", {"pro": [MaxCount(1), MaxCount(2)]}).first(3).first(2).unless(is_long)
        picked = _pick_validators(rule, IfParameter._every_branch)
        assert [entry.first for entry in picked] == [2, 2]
        assert [entry.conditions for entry in picked] == [((is_long, False),), ((is_long, False),)]

    def test_refuses_something_that_is_not_a_rule(self) -> None:
        with pytest.raises(TypeError, match="pre_run_validators takes"):
            _pick_validators("MaxCount(1)", IfParameter._every_branch)  # pyright: ignore[reportArgumentType]

    def test_first_refuses_a_count_below_one(self) -> None:
        with pytest.raises(ValueError, match="at least 1"):
            MaxCount(1).first(0)
