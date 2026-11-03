"""Validators for any kind of value: a list's length."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, override

from griptape_nodes.exe_types.validators._checks import check_count
from griptape_nodes.exe_types.validators.base import PreRunValidator

if TYPE_CHECKING:
    from griptape_nodes.exe_types.node_error import NodeError
    from griptape_nodes.exe_types.validators.context import CheckContext

__all__ = ["MaxCount"]


@dataclass(frozen=True)
class MaxCount(PreRunValidator):
    """The most items a list parameter accepts.

    Args:
        maximum: The most items accepted.
        noun: The plural of what is counted, such as "prompts", for the message.
    """

    checks_whole_list = True

    maximum: int
    noun: str = "items"

    @override
    def check(self, value: Any, ctx: CheckContext) -> NodeError | None:
        return check_count(
            len(value), maximum=self.maximum, parameter=ctx.subject, noun=self.noun, docs_url=ctx.docs_url
        )
