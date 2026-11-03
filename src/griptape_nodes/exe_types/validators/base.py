"""What a parameter's `pre_run_validators=` holds: validators, and the rules that narrow them.

A `PreRunValidator` is a limit on one input, checked by its `check(value, ctx)`. The rest of a rule
is a tree the engine walks: `IfParameter` picks validators by another parameter's value, and
`first`, `when` and `unless` narrow a rule to a list's first items or to values a condition holds
for (`FirstItems` and `Conditional`). `_tree` holds the one walk of that tree, which the runner and
the build-time fit check share.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from collections.abc import Hashable, Mapping, Sequence

    from griptape_nodes.exe_types.core_types import Parameter
    from griptape_nodes.exe_types.node_error import NodeError
    from griptape_nodes.exe_types.validators.context import CheckContext

__all__ = [
    "LIMITS_LINK_LABEL",
    "Condition",
    "IfParameter",
    "PreRunRule",
    "PreRunValidator",
]


# The label on the docs link every validator's error carries.
LIMITS_LINK_LABEL = "Provider limits"

# Whether a narrowed rule applies to one value, from the value and what the engine can read of it.
# `CheckContext` is quoted: the alias is evaluated when something reads it, and `context` imports this
# module, so a runtime import here would be circular.
type Condition = Callable[[Any, "CheckContext"], bool]


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
        return FirstItems(self, count)

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
        return Conditional(self, condition, applies_when=True)

    def unless(self, condition: Condition) -> PreRunRule:
        """This rule, applied only to a value `condition` doesn't hold for. See `when`."""
        return Conditional(self, condition, applies_when=False)


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


@dataclass(frozen=True)
class FirstItems(PreRunRule):
    """`rule`, applied only to a list's first `count` items. What `first` returns; the engine walks it."""

    rule: PreRunRule
    count: int


@dataclass(frozen=True)
class Conditional(PreRunRule):
    """`rule`, applied only when `condition` holds for the value (or, with `applies_when=False`, doesn't).

    What `when` and `unless` return; the engine walks it.
    """

    rule: PreRunRule
    condition: Condition
    applies_when: bool


def holds_list(parameter: Parameter) -> bool:
    """Whether a parameter's value is a list, whose items its validators check one by one.

    For the engine's validator modules; not exported.
    """
    return parameter.type.strip().lower().startswith("list[")
