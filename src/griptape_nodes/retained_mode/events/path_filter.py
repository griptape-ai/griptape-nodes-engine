"""Dot-path field projection for WebSocket result payloads.

Used by EventResult.dict() to prune the unstructured result dict before broadcast.
Frontend callers set RequestPayload.fields to shrink the wire payload; safe_unstructure
still materializes the full result in memory — only transmission is reduced.

Path mini-language
------------------
- "a.b.c"  — keep field b.c nested inside a
- "*"       — wildcard: apply subtree to every value in a dict with arbitrary keys
              (e.g. dict[str, WorkflowMetadata] keyed by file path)
- Prefix-wins: if both "workflows" and "workflows.name" are requested, "workflows" wins.
- Unmatched paths at any depth emit a WARNING (typo vs version skew).
- Mixing "*" with named siblings at the same level emits a WARNING; named keys are dropped.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def build_path_tree(paths: list[str]) -> dict:
    """Convert a list of dot-paths into a nested projection tree."""
    # ["a.b", "a.c.d"] -> {"a": {"b": {}, "c": {"d": {}}}}
    # Empty {} at a leaf = keep the value whole.
    #
    # Prefix-wins rule: if "workflows" (keep-whole) and "workflows.name" (narrow) are both
    # requested, "workflows" wins regardless of order. Implemented two ways:
    #   - Skip a child path if any ancestor is already a {} leaf (broad path was added first).
    #   - Always assign the leaf with = {} rather than setdefault, so a later broad path
    #     overwrites children that a narrower path already built.
    tree: dict = {}
    for path in paths:
        parts = path.split(".")
        node = tree
        skip = False
        for part in parts[:-1]:
            if node.get(part) == {}:
                # An ancestor is already a keep-whole leaf; this child path is dominated.
                skip = True
                break
            # setdefault: create the branch if missing, or return the existing one so shared
            # prefixes like ["a.b", "a.c"] converge on the same "a" node.
            node = node.setdefault(part, {})
        if not skip:
            # Unconditional assignment so a broad path ("workflows") that arrives after a
            # narrow one ("workflows.name") still wins by replacing the subtree with {}.
            node[parts[-1]] = {}
    return tree


def _filter_list(items: list, subtree: dict, _path: str = "") -> list:
    return [apply_path_tree(item, subtree, _path) if isinstance(item, dict) else item for item in items]


def _apply_wildcard_tree(data: dict, subtree: dict, _path: str = "") -> dict:
    # Applies subtree to every value in data, used when the dict's keys are arbitrary
    # (e.g. workflow file paths) rather than fixed field names. See apply_path_tree.
    if not subtree:
        return dict(data)
    filtered = {}
    for k, v in data.items():
        item_path = f"{_path}.{k}" if _path else k
        filtered[k] = (
            _filter_list(v, subtree, item_path) if isinstance(v, list) else apply_path_tree(v, subtree, item_path)
        )
    return filtered


def apply_path_tree(data: Any, tree: dict, _path: str = "") -> Any:
    """Apply a projection tree to a dict, returning only the requested fields."""
    # _path tracks the dot-path to the current node for warning messages; callers omit it.
    if not isinstance(data, dict):
        return data

    if "*" in tree:
        # "*" must be the only key at its level — mixing it with named keys is contradictory
        # ("*" is for dicts with arbitrary keys; named siblings would be silently dropped).
        named_siblings = [k for k in tree if k != "*"]
        if named_siblings:
            logger.warning(
                "fields filter: '*' mixed with named keys %s at the same level — named keys are ignored. "
                "Use '*' only when all keys in the dict are arbitrary (e.g. file paths).",
                named_siblings,
            )
        return _apply_wildcard_tree(data, tree["*"], _path)

    # Named-key traversal: keep only fields present in the tree, drop everything else.
    result = {}
    for key, subtree in tree.items():
        full_path = f"{_path}.{key}" if _path else key
        if key not in data:
            logger.warning("fields filter: '%s' not found in result — typo or version skew?", full_path)
            continue
        value = data[key]
        if not subtree:
            result[key] = value  # leaf — keep value whole
        elif isinstance(value, list):
            result[key] = _filter_list(value, subtree, full_path)
        else:
            result[key] = apply_path_tree(value, subtree, full_path)
    return result
