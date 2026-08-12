"""Dot-path field projection for WebSocket result payloads.

Used by EventResult.dict() to prune the unstructured result dict before broadcast.
Frontend callers set RequestPayload.fields to shrink the wire payload; safe_unstructure
still materializes the full result in memory — only transmission is reduced.

Path mini-language
------------------
- "a.b.c"  — keep field b.c nested inside a
- "*"       — wildcard: apply subtree to every value in a dict with arbitrary keys
              (e.g. dict[str, WorkflowMetadata] keyed by file path). Use "*" — not a
              concrete key — for such maps: referencing a specific key (e.g.
              "situations.save_file") on an arbitrary-key map warns "not found" whenever
              that key is legitimately absent, since keys are indistinguishable from typos.
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


def apply_path_tree(data: Any, tree: dict) -> Any:
    """Apply a projection tree to a dict, returning only the requested fields.

    Unmatched paths (typos or version skew) are collected during traversal and each
    distinct path is logged once. This matters under wildcards and lists: a single typo
    like ``workflows.*.nme`` against N values would otherwise emit N identical warnings.
    """
    warnings: set[str] = set()
    result = _apply(data, tree, "", warnings)
    for message in sorted(warnings):
        logger.warning("%s", message)
    return result


def _apply(data: Any, tree: dict, path: str, warnings: set[str]) -> Any:
    # path tracks the dot-path to the current node for warning messages. warnings accumulates
    # distinct warning strings so the public wrapper can emit each exactly once.
    if not isinstance(data, dict):
        return data

    if "*" in tree:
        # "*" must be the only key at its level — mixing it with named keys is contradictory
        # ("*" is for dicts with arbitrary keys; named siblings would be silently dropped).
        named_siblings = [k for k in tree if k != "*"]
        if named_siblings:
            level = path or "(root)"
            warnings.add(
                f"fields filter: '*' mixed with named keys {named_siblings} at '{level}' — named keys are "
                f"ignored. Use '*' only when all keys in the dict are arbitrary (e.g. file paths)."
            )
        return _apply_wildcard(data, tree["*"], path, warnings)

    # Named-key traversal: keep only fields present in the tree, drop everything else.
    result = {}
    for key, subtree in tree.items():
        full_path = f"{path}.{key}" if path else key
        if key not in data:
            warnings.add(f"fields filter: '{full_path}' not found in result — typo or version skew?")
            continue
        value = data[key]
        if not subtree:
            result[key] = value  # leaf — keep value whole
        elif isinstance(value, list):
            result[key] = _filter_list(value, subtree, full_path, warnings)
        else:
            result[key] = _apply(value, subtree, full_path, warnings)
    return result


def _apply_wildcard(data: dict, subtree: dict, path: str, warnings: set[str]) -> dict:
    # Applies subtree to every value in data, used when the dict's keys are arbitrary
    # (e.g. workflow file paths) rather than fixed field names. See apply_path_tree.
    if not subtree:
        return dict(data)
    # Report unmatched paths under the wildcard using the "*" spelling (e.g. "workflows.*.nme")
    # rather than each resolved key, so one typo across N values dedupes to a single warning.
    wildcard_path = f"{path}.*" if path else "*"
    filtered = {}
    for k, v in data.items():
        filtered[k] = (
            _filter_list(v, subtree, wildcard_path, warnings)
            if isinstance(v, list)
            else _apply(v, subtree, wildcard_path, warnings)
        )
    return filtered


def _filter_list(items: list, subtree: dict, path: str, warnings: set[str]) -> list:
    return [_apply(item, subtree, path, warnings) if isinstance(item, dict) else item for item in items]
