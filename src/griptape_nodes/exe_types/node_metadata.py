"""What in a node's metadata counts as a change to the workflow."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping


def without_first_size(before: Mapping[str, Any], after: Mapping[str, Any]) -> dict[str, Any]:
    """``after``, a node's metadata, without the size it adds to ``before``, if ``before`` has none.

    The editor records a node's size the first time it draws a node that has none, at the size it
    would draw it anyway. So adding a size is no change to the workflow, and two metadata that
    differ by only that are the same workflow.
    """
    if "size" in before:
        return dict(after)
    return {key: value for key, value in after.items() if key != "size"}
