"""Validators that aren't about one file: a list's length, and limits picked by another parameter."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, override

from griptape_nodes.exe_types.validators._checks import check_count
from griptape_nodes.exe_types.validators.base import PreRunValidator

if TYPE_CHECKING:
    from collections.abc import Callable, Hashable, Mapping, Sequence

    from griptape_nodes.exe_types.node_error import NodeError
    from griptape_nodes.exe_types.node_types import BaseNode

__all__ = ["IfParameter", "MaxCount"]


@dataclass(frozen=True)
class MaxCount(PreRunValidator):
    """The most items a list parameter accepts.

    Args:
        maximum: The most items accepted.
        noun: The plural of what is counted, such as "images", for the message.
    """

    checks_whole_list = True

    maximum: int
    noun: str = "items"

    @override
    def check(self, value: Any, *, subject: str, docs_url: str | None, position: int | None = None) -> NodeError | None:
        return check_count(len(value), maximum=self.maximum, parameter=subject, noun=self.noun, docs_url=docs_url)


@dataclass(frozen=True, init=False)
class IfParameter(PreRunValidator):
    """Limits that depend on another parameter's value, such as the selected model.

    At the node's turn, the validators listed for the other parameter's current value apply, or
    `default` for a value not listed. A list can hold another `IfParameter`, for limits that depend
    on two settings; it picks in turn. This is how a limit depends on a setting; a limit that
    depends on the file itself uses `MediaValidator.when` or `unless`.

    ```python
    IfParameter(
        "model",
        {"pro": [SideRange(min=300), MaxFileSize(10 * MB)]},
        default=[SideRange(min=512)],
    )
    # A switch, and whether a list has items:
    IfParameter("upscale", {True: []}, default=[SideRange(min=512)])
    IfParameter("reference_images", {False: [AllowedFormats("image/png")]}, key=bool)
    ```

    Args:
        parameter: The name of the parameter whose value picks the limits.
        choices: The validators for each value of that parameter, or of `key` applied to it.
        default: The validators for any other value.
        key: Turns the parameter's value into the value looked up in `choices`, such as `bool`
            for whether a list has items. The value is looked up as it is when None.
    """

    parameter: str
    # Pairs rather than a dict, so the validator stays frozen and hashable.
    choices: tuple[tuple[Hashable, tuple[PreRunValidator, ...]], ...]
    default: tuple[PreRunValidator, ...]
    key: Callable[[Any], Hashable] | None

    def __init__(
        self,
        parameter: str,
        choices: Mapping[Hashable, Sequence[PreRunValidator]],
        *,
        default: Sequence[PreRunValidator] = (),
        key: Callable[[Any], Hashable] | None = None,
    ) -> None:
        object.__setattr__(self, "parameter", parameter)
        object.__setattr__(self, "choices", tuple((value, tuple(validators)) for value, validators in choices.items()))
        object.__setattr__(self, "default", tuple(default))
        object.__setattr__(self, "key", key)

    @override
    def check(self, value: Any, *, subject: str, docs_url: str | None, position: int | None = None) -> NodeError | None:
        # The engine runs the validators `selected` picks; this one has nothing of its own to check.
        return None

    def selected(self, node: BaseNode) -> Sequence[PreRunValidator]:
        """The validators listed for the other parameter's current value, or `default`, each picking in turn."""
        picked: list[PreRunValidator] = []
        current = node.get_parameter_value(self.parameter)
        if self.key is not None:
            current = self.key(current)
        for validator in self._listed_for(current):
            picked.extend(validator.selected(node))
        return picked

    def candidates(self) -> Sequence[PreRunValidator]:
        """Every validator this can pick, from every list, through any `IfParameter` inside it."""
        everything: list[PreRunValidator] = []
        lists = [validators for _, validators in self.choices]
        lists.append(self.default)
        for validators in lists:
            for validator in validators:
                everything.extend(validator.candidates())
        return everything

    def _listed_for(self, current: Any) -> tuple[PreRunValidator, ...]:
        """The list for the other parameter's value, or `default`."""
        for value, validators in self.choices:
            if current == value:
                return validators
        return self.default
