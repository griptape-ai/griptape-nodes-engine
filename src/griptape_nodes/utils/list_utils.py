"""Helpers for working with lists."""

from __future__ import annotations

from typing import Any


def flatten_list(items: list[Any], depth: int = 0) -> list[Any]:
    """Flatten nested lists into one list, keeping item order.

    A depth of 0 flattens every level; a positive depth flattens that many levels.
    Only lists are expanded, so strings, dicts and other values stay whole items.
    """
    flat: list[Any] = []
    for item in items:
        if isinstance(item, list) and depth != 1:
            flat.extend(flatten_list(item, max(depth - 1, 0)))
        elif isinstance(item, list):
            flat.extend(item)
        else:
            flat.append(item)
    return flat
