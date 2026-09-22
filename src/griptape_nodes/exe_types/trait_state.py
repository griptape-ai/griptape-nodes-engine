"""Plain-data representations of saved trait state."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, NamedTuple, Self

SAVEABLE_SCALARS = (type(None), bool, int, float, str)


class SavedStateValue(NamedTuple):
    value: Any
    unsupported_type: str | None


def as_saved_state_value(value: Any) -> SavedStateValue:
    """Convert sets and tuples to lists; report values with no data representation."""
    if isinstance(value, SAVEABLE_SCALARS):
        return SavedStateValue(value=value, unsupported_type=None)
    if isinstance(value, dict):
        return _as_saved_mapping(value)
    if isinstance(value, (list, tuple, set, frozenset)):
        return _as_saved_sequence(value)
    return SavedStateValue(value=None, unsupported_type=type(value).__name__)


def _as_saved_mapping(value: dict) -> SavedStateValue:
    """Reject non-text mapping keys, which cannot round-trip."""
    saved: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            return SavedStateValue(value=None, unsupported_type=f"dictionary keyed by {type(key).__name__}")
        saved_item = as_saved_state_value(item)
        if saved_item.unsupported_type is not None:
            return saved_item
        saved[key] = saved_item.value
    return SavedStateValue(value=saved, unsupported_type=None)


def _as_saved_sequence(value: list | tuple | set | frozenset) -> SavedStateValue:
    """Convert sequences to lists.

    Sort string sets so unchanged state produces stable saves across processes.
    """
    items = value
    if isinstance(value, (set, frozenset)) and all(isinstance(item, str) for item in value):
        items = sorted(value)
    saved: list[Any] = []
    for item in items:
        saved_item = as_saved_state_value(item)
        if saved_item.unsupported_type is not None:
            return saved_item
        saved.append(saved_item.value)
    return SavedStateValue(value=saved, unsupported_type=None)


def state_from_rendered_keys(written: Any, renames: dict[str, str]) -> dict[str, Any]:
    """Map rendered UI option keys back to trait state keys, skipping any not written."""
    if not isinstance(written, dict):
        return {}
    return {state_key: written[ui_key] for ui_key, state_key in renames.items() if ui_key in written}


def changed_trait_states(built: list[dict[str, Any]], current: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop each state key that still matches the same trait in ``built``.

    ``built`` is what the node's own code constructs. Leaving an unchanged key out of the save
    lets a library release that changes it reach existing workflows. An entry with no
    counterpart keeps its full state, since nothing else would supply it.
    """
    unmatched = list(built)
    changed: list[dict[str, Any]] = []
    for entry in current:
        counterpart = _take_counterpart(unmatched, entry)
        if counterpart is None:
            changed.append(entry)
            continue
        built_state = counterpart["trait_state"]
        trait_state = {
            key: value
            for key, value in entry["trait_state"].items()
            if key not in built_state or built_state[key] != value
        }
        changed.append({**entry, "trait_state": trait_state})
    return changed


def _take_counterpart(candidates: list[dict[str, Any]], entry: dict[str, Any]) -> dict[str, Any] | None:
    """Take the first candidate with the same identity, consuming it so two entries cannot share one."""
    identity = _trait_identity(entry)
    for candidate in candidates:
        if _trait_identity(candidate) == identity:
            candidates.remove(candidate)
            return candidate
    return None


def _trait_identity(entry: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in entry.items() if key != "trait_state"}


@dataclass(frozen=True)
class TraitStateEntry:
    """Saved trait identity and state."""

    trait_name: str
    trait_state: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, entry: dict[str, Any]) -> Self | None:
        """Return ``None`` when the entry names no trait."""
        trait_name = entry.get("trait_name")
        if not isinstance(trait_name, str):
            return None
        trait_state = entry.get("trait_state")
        if not isinstance(trait_state, dict):
            trait_state = {}
        return cls(trait_name=trait_name, trait_state=trait_state)

    def to_dict(self) -> dict[str, Any]:
        return {"trait_name": self.trait_name, "trait_state": self.trait_state}
