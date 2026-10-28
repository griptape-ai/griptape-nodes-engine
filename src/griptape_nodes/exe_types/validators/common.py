"""Validators that aren't about one file: a list's length, and limits picked by another parameter."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, override

from griptape_nodes.exe_types.input_checks import check_count
from griptape_nodes.exe_types.validators.base import EMPTY_MEDIA, MediaInfo, PreRunValidator

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from griptape_nodes.exe_types.node_error import NodeError
    from griptape_nodes.exe_types.node_types import BaseNode

__all__ = ["DependsOn", "MaxCount"]


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
    def check(
        self,
        value: Any,
        *,
        subject: str,
        docs_url: str | None,
        media: MediaInfo = EMPTY_MEDIA,
        node: BaseNode | None = None,
        position: int | None = None,
    ) -> NodeError | None:
        return check_count(len(value), maximum=self.maximum, parameter=subject, noun=self.noun, docs_url=docs_url)


@dataclass(frozen=True, init=False)
class DependsOn(PreRunValidator):
    """Limits that depend on another parameter's value, such as the selected model.

    At the node's turn, the validators listed for the other parameter's current value apply, or
    `default` for a value not listed. A list can hold another `DependsOn`, for limits that depend
    on two settings; it picks in turn.

    ```python
    DependsOn(
        "model",
        {"pro": [SideRange(min=300), MaxFileSize(10 * MB)]},
        default=[SideRange(min=512)],
    )
    ```

    Args:
        parameter: The name of the parameter whose value picks the limits.
        choices: The validators for each value of that parameter.
        default: The validators for any other value.
    """

    parameter: str
    # Pairs rather than a dict, so the validator stays frozen and hashable.
    choices: tuple[tuple[str, tuple[PreRunValidator, ...]], ...]
    default: tuple[PreRunValidator, ...]

    def __init__(
        self,
        parameter: str,
        choices: Mapping[str, Sequence[PreRunValidator]],
        *,
        default: Sequence[PreRunValidator] = (),
    ) -> None:
        object.__setattr__(self, "parameter", parameter)
        object.__setattr__(self, "choices", tuple((value, tuple(validators)) for value, validators in choices.items()))
        object.__setattr__(self, "default", tuple(default))

    @override
    def check(
        self,
        value: Any,
        *,
        subject: str,
        docs_url: str | None,
        media: MediaInfo = EMPTY_MEDIA,
        node: BaseNode | None = None,
        position: int | None = None,
    ) -> NodeError | None:
        # The engine runs the validators `selected` picks; this one has nothing of its own to check.
        return None

    def selected(self, node: BaseNode) -> Sequence[PreRunValidator]:
        """The validators listed for the other parameter's current value, or `default`, each picking in turn."""
        picked: list[PreRunValidator] = []
        for validator in self._listed_for(node.get_parameter_value(self.parameter)):
            picked.extend(validator.selected(node))
        return picked

    def candidates(self) -> Sequence[PreRunValidator]:
        """Every validator this can pick, from every list, through any `DependsOn` inside it."""
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
