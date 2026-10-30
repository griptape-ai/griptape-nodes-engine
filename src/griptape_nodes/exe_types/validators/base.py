"""What a pre-run validator is: a limit on one input, of any kind.

A parameter declares `pre_run_validators=`; the engine runs them at the node's turn, just before
`validate_before_node_run`. A family of validators that needs more than the value subclasses
`PreRunValidator` in its own module.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, override

if TYPE_CHECKING:
    from collections.abc import Sequence

    from griptape_nodes.exe_types.core_types import Parameter
    from griptape_nodes.exe_types.node_error import NodeError
    from griptape_nodes.exe_types.node_types import BaseNode

__all__ = ["LIMITS_LINK_LABEL", "PreRunValidator", "require_validators_fit"]


# The label on the docs link every validator's error carries.
LIMITS_LINK_LABEL = "Provider limits"


class PreRunValidator(ABC):
    """A limit on one input, checked at the node's turn, before it runs.

    A validator on a list parameter checks each item, unless it sets `checks_whole_list`, in which
    case it gets the whole list. `first(n)` narrows any validator to a list's first `n` items, for a
    provider that reads only that many.
    """

    # True for a validator that judges a list parameter as a whole, such as how many items it holds.
    checks_whole_list: ClassVar[bool] = False

    @abstractmethod
    def check(self, value: Any, *, subject: str, docs_url: str | None, position: int | None = None) -> NodeError | None:
        """An error when `value` breaks this limit, or None when it is within it.

        A single input with no value, and each empty row of a list, is skipped before any validator
        runs, so `value` is never None; a validator with `checks_whole_list` still gets the list, even
        an empty one. Check that a required input is set in the node's `validate_before_node_run`.

        Args:
            value: The parameter's value; for a list, one item, or the whole list with `checks_whole_list`.
            subject: The start of the message naming the input, such as "Prompt 2 in 'prompts'".
                With `checks_whole_list`, the parameter's name as the UI shows it.
            docs_url: The node's page documenting its input limits, from `BaseNode.input_limits_url`.
            position: An item's 1-based position in a list parameter, or None for a single input or
                the whole list.
        """

    def selected(self, node: BaseNode) -> Sequence[PreRunValidator]:  # noqa: ARG002
        """The validators that apply on `node` now. A validator is itself; `IfParameter` picks a list."""
        return (self,)

    def candidates(self) -> Sequence[PreRunValidator]:
        """Every validator `selected` can return, so a parameter can refuse one that can't go on it."""
        return (self,)

    def require_fits(self, parameter: Parameter) -> None:
        """Raise when this validator can't check `parameter`, so a mistake shows when the node is built.

        The default refuses a validator that checks a whole list on a parameter holding a single
        value. A family that needs more of the parameter, such as a kind of value, adds its rules.

        Raises:
            TypeError: If this validator can't check `parameter`.
        """
        if self.checks_whole_list and not holds_list(parameter):
            msg = (
                f"Attempted to add {shown_name(self)} to parameter '{parameter.name}'. "
                "Failed because it checks a list, and the parameter holds a single value."
            )
            raise TypeError(msg)

    def first(self, count: int) -> PreRunValidator:
        """This validator, applied only to the first `count` items of a list parameter.

        On a single input, or on a validator that checks the whole list, it always applies.
        """
        return _FirstItems(self, count)


@dataclass(frozen=True)
class _FirstItems(PreRunValidator):
    """`inner`, run only on a list's first `count` items."""

    inner: PreRunValidator
    count: int

    def __post_init__(self) -> None:
        # Per instance, since it follows `inner`; the engine and the fit check read it from here.
        object.__setattr__(self, "checks_whole_list", self.inner.checks_whole_list)
        if self.count < 1:
            msg = (
                f"Attempted to apply {shown_name(self)} to the first {self.count} items. "
                "Failed because the count must be at least 1."
            )
            raise ValueError(msg)

    # A wrapped `IfParameter` picks, and each validator it picks or can pick is wrapped the same way.
    @override
    def selected(self, node: BaseNode) -> Sequence[PreRunValidator]:
        return [validator.first(self.count) for validator in self.inner.selected(node)]

    @override
    def candidates(self) -> Sequence[PreRunValidator]:
        return [validator.first(self.count) for validator in self.inner.candidates()]

    # The validator inside decides whether it fits, including any rules it adds of its own.
    @override
    def require_fits(self, parameter: Parameter) -> None:
        self.inner.require_fits(parameter)

    @override
    def check(self, value: Any, *, subject: str, docs_url: str | None, position: int | None = None) -> NodeError | None:
        if position is not None and position > self.count:
            return None
        return self.inner.check(value, subject=subject, docs_url=docs_url, position=position)


def holds_list(parameter: Parameter) -> bool:
    """Whether a parameter's value is a list, whose items its validators check one by one."""
    return parameter.type.strip().lower().startswith("list[")


def shown_name(validator: PreRunValidator) -> str:
    """The validator's class name for an error, naming the validator a wrapper such as `first` holds."""
    inner = getattr(validator, "inner", None)
    while isinstance(inner, PreRunValidator):
        validator = inner
        inner = getattr(validator, "inner", None)
    return type(validator).__name__


def require_validators_fit(parameter: Parameter) -> None:
    """Raise when a parameter declares a pre-run validator it can't be checked with.

    Each validator, and each one an `IfParameter` or wrapper can pick, checks itself with `require_fits`.

    Raises:
        TypeError: If any of them can't check `parameter`.
    """
    for declared in parameter.pre_run_validators:
        for validator in declared.candidates():
            validator.require_fits(parameter)
