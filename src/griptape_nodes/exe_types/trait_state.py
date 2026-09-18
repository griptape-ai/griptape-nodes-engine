"""Plain-data representations of saved trait state."""

from __future__ import annotations

from collections.abc import Callable as CallableABC
from dataclasses import dataclass, field
from types import UnionType
from typing import Any, Literal, NamedTuple, Self, Union, get_args, get_origin

SAVEABLE_SCALARS = (type(None), bool, int, float, str)

SAVEABLE_CONTAINERS = (list, dict, set, frozenset, tuple)

CALLBACK_TYPE = "callback"


def unsaveable_type(annotation: Any) -> str | None:
    """Return an unsaveable type named by an annotation.

    ``Any`` and unresolved forward references are checked by value during saving.
    """
    if annotation is None or annotation is Any or isinstance(annotation, str):
        return None
    origin = get_origin(annotation)
    if origin is not None:
        return _unsaveable_parameterized(annotation, origin)
    return _unsaveable_plain(annotation)


def _unsaveable_parameterized(annotation: Any, origin: Any) -> str | None:
    if origin is Literal:
        for value in get_args(annotation):
            if not isinstance(value, SAVEABLE_SCALARS):
                return type(value).__name__
        return None
    if origin in (Union, UnionType):
        return _first_unsaveable(get_args(annotation))
    if origin is CallableABC:
        return CALLBACK_TYPE
    outer = unsaveable_type(origin)
    if outer is not None:
        return outer
    return _first_unsaveable(get_args(annotation))


def _unsaveable_plain(annotation: Any) -> str | None:
    if not isinstance(annotation, type):
        return None
    if issubclass(annotation, CallableABC):
        return CALLBACK_TYPE
    if not issubclass(annotation, (*SAVEABLE_SCALARS, *SAVEABLE_CONTAINERS)):
        return annotation.__name__
    return None


def _first_unsaveable(members: tuple[Any, ...]) -> str | None:
    """Skip the ellipsis in ``tuple[X, ...]``."""
    for member in members:
        if member is Ellipsis:
            continue
        found = unsaveable_type(member)
        if found is not None:
            return found
    return None


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


@dataclass(frozen=True)
class TraitStateEntry:
    """Saved trait identity and state.

    ``trait_module`` distinguishes same-named traits from different libraries, and is what
    finds the class when no instance is attached to carry the state.
    """

    trait_name: str
    trait_module: str | None = None
    trait_state: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, entry: dict[str, Any]) -> Self | None:
        """Return ``None`` when the entry names no trait."""
        trait_name = entry.get("trait_name")
        if not isinstance(trait_name, str):
            return None
        trait_module = entry.get("trait_module")
        if not isinstance(trait_module, str):
            trait_module = None
        trait_state = entry.get("trait_state")
        if not isinstance(trait_state, dict):
            trait_state = {}
        return cls(trait_name=trait_name, trait_module=trait_module, trait_state=trait_state)

    def to_dict(self) -> dict[str, Any]:
        return {"trait_name": self.trait_name, "trait_module": self.trait_module, "trait_state": self.trait_state}
