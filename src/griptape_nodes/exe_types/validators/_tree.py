"""The one walk of a parameter's rule tree, shared by the runner and the build-time fit check.

`pick_validators` follows a rule down to the validators it reaches, carrying the `first` and
`when`/`unless` narrowing on the path to each. Which branches an `IfParameter` leads to is the
caller's: at the node's turn, the list for the other parameter's current value (`listed_for`); when
the parameter is built, every list (`every_branch`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from griptape_nodes.exe_types.validators.base import (
    Condition,
    Conditional,
    FirstItems,
    IfParameter,
    PreRunRule,
    PreRunValidator,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from griptape_nodes.exe_types.core_types import Parameter
    from griptape_nodes.exe_types.validators.context import CheckContext


@dataclass(frozen=True)
class PickedValidator:
    """A validator a rule reached, with the narrowing on its path from the declared rule.

    Args:
        validator: The validator to run.
        first: The most items of a list it checks, from `first` on its path, or None for all of them.
        conditions: Each `when` (True) or `unless` (False) on its path, outermost first.
    """

    validator: PreRunValidator
    first: int | None = None
    conditions: tuple[tuple[Condition, bool], ...] = ()

    def applies(self, value: Any, ctx: CheckContext) -> bool:
        """Whether the validator checks `value`: within `first`, and every condition agreeing."""
        if self.first is not None and ctx.position is not None and ctx.position > self.first:
            return False
        return all(condition(value, ctx) == applies_when for condition, applies_when in self.conditions)


def listed_for(rule: IfParameter, current: Any) -> tuple[PreRunRule, ...]:
    """The rules `rule` lists for the other parameter's value `current`, or its `default`."""
    if rule.key is not None:
        current = rule.key(current)
    for value, rules in rule.choices:
        if current == value:
            return rules
    return rule.default


def every_branch(rule: IfParameter) -> tuple[PreRunRule, ...]:
    """The rules of every choice in `rule` and its `default`, for checking all of them when the parameter is built."""
    rules = [branch for _, listed in rule.choices for branch in listed]
    rules.extend(rule.default)
    return tuple(rules)


def pick_validators(rule: PreRunRule, branches: Callable[[IfParameter], Sequence[PreRunRule]]) -> list[PickedValidator]:
    """Every validator `rule` reaches, with the narrowing on the path to it.

    Raises:
        TypeError: If the rule, or one it holds, isn't a validator or a rule from `base`.
    """
    return _walk(rule, branches, first=None, conditions=())


def _walk(
    rule: PreRunRule,
    branches: Callable[[IfParameter], Sequence[PreRunRule]],
    *,
    first: int | None,
    conditions: tuple[tuple[Condition, bool], ...],
) -> list[PickedValidator]:
    """`pick_validators` below one rule, carrying the narrowing on the path so far."""
    match rule:
        case PreRunValidator():
            return [PickedValidator(validator=rule, first=first, conditions=conditions)]
        case FirstItems(rule=inner, count=count):
            narrowed = count if first is None else min(first, count)
            return _walk(inner, branches, first=narrowed, conditions=conditions)
        case Conditional(rule=inner, condition=condition, applies_when=applies_when):
            return _walk(inner, branches, first=first, conditions=(*conditions, (condition, applies_when)))
        case IfParameter():
            picked: list[PickedValidator] = []
            for branch in branches(rule):
                picked.extend(_walk(branch, branches, first=first, conditions=conditions))
            return picked
        case _:
            msg = (
                f"Attempted to check an input with {rule!r}. Failed because pre_run_validators takes "
                "validators, IfParameter, and what first, when and unless return."
            )
            raise TypeError(msg)


def require_validators_fit(parameter: Parameter) -> None:
    """Raise when a parameter declares a rule it can't be checked with.

    Walks every branch of every declared rule and asks each validator reached whether it fits.

    Raises:
        TypeError: If a declared item isn't a rule, or a validator it reaches can't check `parameter`.
    """
    for rule in parameter.pre_run_validators:
        for picked in pick_validators(rule, every_branch):
            picked.validator.require_fits(parameter)
