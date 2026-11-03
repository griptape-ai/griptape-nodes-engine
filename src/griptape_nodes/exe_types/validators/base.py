"""What a parameter's `pre_run_validators=` holds: validators, and the rules that narrow them.

A `PreRunValidator` is a limit on one input, checked by its `check(value, ctx)`. The rest of a rule
is a tree the engine walks: `IfParameter` picks validators by another parameter's value, and
`first`, `when` and `unless` narrow a rule to a list's first items or to values a condition holds
for. `pick_validators` is the one walk: the runner uses it to find what applies at the node's turn,
and `require_validators_fit` to check every branch when the parameter is built.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from collections.abc import Callable, Hashable, Mapping, Sequence

    from griptape_nodes.exe_types.core_types import Parameter
    from griptape_nodes.exe_types.node_error import NodeError
    from griptape_nodes.exe_types.validators.context import CheckContext

__all__ = [
    "LIMITS_LINK_LABEL",
    "Condition",
    "IfParameter",
    "PreRunRule",
    "PreRunValidator",
    "require_validators_fit",
]


# The label on the docs link every validator's error carries.
LIMITS_LINK_LABEL = "Provider limits"

# Whether a narrowed rule applies to one value, from the value and what the engine can read of it.
type Condition = Callable[[Any, CheckContext], bool]


class PreRunRule(ABC):  # noqa: B024  # a marker for what `pre_run_validators=` takes; the engine walks the kinds
    """Anything a parameter's `pre_run_validators=` takes: a validator, or a rule that narrows one.

    `first`, `when` and `unless` work on any rule, including an `IfParameter`, and return a new one.
    """

    def first(self, count: int) -> PreRunRule:
        """This rule, applied only to the first `count` items of a list parameter.

        On a single input, or on a validator that checks the whole list, it always applies.

        Raises:
            ValueError: If `count` is below 1.
        """
        if count < 1:
            msg = f"Attempted to apply a validator to the first {count} items. Failed because the count must be at least 1."
            raise ValueError(msg)
        return _FirstItems(self, count)

    def when(self, condition: Condition) -> PreRunRule:
        """This rule, applied only to a value `condition` holds for.

        A condition is a function given the value and its `CheckContext`, returning True or False:

        ```python
        def is_png(value, ctx) -> bool:
            return ctx.media.mime_type == "image/png"

        MaxFileSize(5 * MB).when(is_png)
        ```

        It reads what it touches, as a check does. A limit that depends on another parameter's value
        is an `IfParameter`.
        """
        return _Conditional(self, condition, applies_when=True)

    def unless(self, condition: Condition) -> PreRunRule:
        """This rule, applied only to a value `condition` doesn't hold for. See `when`."""
        return _Conditional(self, condition, applies_when=False)


class PreRunValidator(PreRunRule):
    """A limit on one input, checked at the node's turn, before it runs.

    A validator on a list parameter checks each item, unless it sets `checks_whole_list`, in which
    case it gets the whole list.
    """

    # True for a validator that judges a list parameter as a whole, such as how many items it holds.
    checks_whole_list: ClassVar[bool] = False

    @abstractmethod
    def check(self, value: Any, ctx: CheckContext) -> NodeError | None:
        """An error when `value` breaks this limit, or None when it is within it.

        A single input with no value, and each empty row of a list, is skipped before any validator
        runs, so `value` is never None; a validator with `checks_whole_list` still gets the list, even
        an empty one. Check that a required input is set in the node's `validate_before_node_run`.

        Args:
            value: The parameter's value; for a list, one item, or the whole list with `checks_whole_list`.
            ctx: Names the input (`ctx.subject`), links the limits page (`ctx.docs_url`), and reads
                what a check needs of a media file (`ctx.media`).
        """

    def require_fits(self, parameter: Parameter) -> None:
        """Raise when this validator can't check `parameter`, so a mistake shows when the node is built.

        The default refuses a validator that checks a whole list on a parameter holding a single
        value. A family that needs more of the parameter, such as a kind of media, adds its rules.

        Raises:
            TypeError: If this validator can't check `parameter`.
        """
        if self.checks_whole_list and not holds_list(parameter):
            msg = (
                f"Attempted to add {type(self).__name__} to parameter '{parameter.name}'. "
                "Failed because it checks a list, and the parameter holds a single value."
            )
            raise TypeError(msg)


@dataclass(frozen=True, init=False)
class IfParameter(PreRunRule):
    """Limits that depend on another parameter's value, such as the selected model.

    At the node's turn, the rules listed for the other parameter's current value apply, or `default`
    for a value not listed. A list can hold another `IfParameter`, for limits that depend on two
    settings. The parameter must be on the node: a name it doesn't have fails the node's checks.

    ```python
    IfParameter("model", {"pro": [MaxCount(4)]}, default=[MaxCount(1)])
    # A switch, and whether a list has items:
    IfParameter("strict", {True: [MaxCount(1)]}, default=[MaxCount(3)])
    IfParameter("examples", {False: [MaxCount(1)]}, key=bool)
    ```

    Args:
        parameter: The name of the parameter whose value picks the limits.
        choices: The rules for each value of that parameter, or of `key` applied to it.
        default: The rules for any other value.
        key: Turns the parameter's value into the value looked up in `choices`, such as `bool`
            for whether a list has items. The value is looked up as it is when None.
    """

    parameter: str
    # Pairs rather than a dict, so the rule stays frozen and hashable.
    choices: tuple[tuple[Hashable, tuple[PreRunRule, ...]], ...]
    default: tuple[PreRunRule, ...]
    key: Callable[[Any], Hashable] | None

    def __init__(
        self,
        parameter: str,
        choices: Mapping[Hashable, Sequence[PreRunRule]],
        *,
        default: Sequence[PreRunRule] = (),
        key: Callable[[Any], Hashable] | None = None,
    ) -> None:
        object.__setattr__(self, "parameter", parameter)
        object.__setattr__(self, "choices", tuple((value, tuple(rules)) for value, rules in choices.items()))
        object.__setattr__(self, "default", tuple(default))
        object.__setattr__(self, "key", key)

    def listed_for(self, current: Any) -> tuple[PreRunRule, ...]:
        """The rules for the other parameter's value `current`, or `default`."""
        if self.key is not None:
            current = self.key(current)
        for value, rules in self.choices:
            if current == value:
                return rules
        return self.default

    def every_branch(self) -> tuple[PreRunRule, ...]:
        """The rules of every choice and `default`, for checking all of them when the parameter is built."""
        rules = [rule for _, listed in self.choices for rule in listed]
        rules.extend(self.default)
        return tuple(rules)


@dataclass(frozen=True)
class _FirstItems(PreRunRule):
    """`rule`, applied only to a list's first `count` items."""

    rule: PreRunRule
    count: int


@dataclass(frozen=True)
class _Conditional(PreRunRule):
    """`rule`, applied only when `condition` holds for the value (or, with `applies_when=False`, doesn't)."""

    rule: PreRunRule
    condition: Condition
    applies_when: bool


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


def pick_validators(rule: PreRunRule, branches: Callable[[IfParameter], Sequence[PreRunRule]]) -> list[PickedValidator]:
    """Every validator `rule` reaches, with the narrowing on the path to it.

    `branches` says which rules an `IfParameter` leads to: at the node's turn, the list for the
    other parameter's value; when the parameter is built, every list.

    Raises:
        TypeError: If the rule, or one it holds, isn't a validator or a rule from this module.
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
        case _FirstItems(rule=inner, count=count):
            narrowed = count if first is None else min(first, count)
            return _walk(inner, branches, first=narrowed, conditions=conditions)
        case _Conditional(rule=inner, condition=condition, applies_when=applies_when):
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


def holds_list(parameter: Parameter) -> bool:
    """Whether a parameter's value is a list, whose items its validators check one by one."""
    return parameter.type.strip().lower().startswith("list[")


def require_validators_fit(parameter: Parameter) -> None:
    """Raise when a parameter declares a rule it can't be checked with.

    Walks every branch of every declared rule and asks each validator reached whether it fits.

    Raises:
        TypeError: If a declared item isn't a rule, or a validator it reaches can't check `parameter`.
    """
    for rule in parameter.pre_run_validators:
        for picked in pick_validators(rule, IfParameter.every_branch):
            picked.validator.require_fits(parameter)
