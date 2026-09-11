"""The saved form of a trait: which class it is, the state that rebuilds it, its callbacks.

A saved workflow carries traits as data, not as code. Nothing here holds a class object or a
callable, and every value is something a plain data format can express, so the same entries
survive whether the file that carries them is generated Python or a serialized artifact.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, NamedTuple, Self

# What a saved value may be once containers are unwrapped. A saved artifact is data, so a
# value that is not one of these has no representation in it.
SAVEABLE_SCALARS = (type(None), bool, int, float, str)


class SavedStateValue(NamedTuple):
    """One trait state value, prepared for a saved artifact."""

    value: Any
    # Name of the type that has no saved form, or None when the value is fine. Named rather
    # than a bare flag so the warning can tell an author what they handed us.
    unsupported_type: str | None


def as_saved_state_value(value: Any) -> SavedStateValue:
    """Convert one state value into its saved form, or report the type that has none.

    A set or a tuple becomes a list, because a data format has neither. That is a real
    change for the trait: what a constructor is handed on load is a list, so a trait wanting
    a set should build one there. Anything with no data form at all, an arbitrary object or a
    callable, is reported instead of guessed at, and the caller drops that one value.
    """
    if isinstance(value, SAVEABLE_SCALARS):
        return SavedStateValue(value=value, unsupported_type=None)
    if isinstance(value, dict):
        return _as_saved_mapping(value)
    if isinstance(value, (list, tuple, set, frozenset)):
        return _as_saved_sequence(value)
    return SavedStateValue(value=None, unsupported_type=type(value).__name__)


def _as_saved_mapping(value: dict) -> SavedStateValue:
    """Convert a dict, whose keys can only be text: that is all a saved mapping has."""
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
    """Convert any sequence to a list, sorting a set so repeated saves of it match.

    A set has no order of its own, and string hashing differs run to run, so saving one
    unsorted would rewrite the file with the same contents shuffled.
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
    """One trait as a save records it.

    The single declaration of that shape. Requests carry the ``to_dict`` form rather than
    these objects, because what a save writes has to be plain data, but every producer and
    reader goes through here so the keys are named in one place.

    ``trait_module`` is what tells two libraries' same-named traits apart, so an entry
    without one names no particular class. It is optional only because a hand-written entry
    can omit it; a save always records it.
    """

    trait_name: str
    trait_module: str | None = None
    trait_state: dict[str, Any] = field(default_factory=dict)
    # Parameter name to the name of a method on the owning node.
    trait_callbacks: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, entry: dict[str, Any]) -> Self | None:
        """Read one saved entry, or None when it names no trait at all.

        A missing state or callback mapping reads as empty: an entry that says nothing about
        either is asking for whatever the node's own code builds.
        """
        trait_name = entry.get("trait_name")
        if not isinstance(trait_name, str):
            return None
        trait_module = entry.get("trait_module")
        if not isinstance(trait_module, str):
            trait_module = None
        trait_state = entry.get("trait_state")
        if not isinstance(trait_state, dict):
            trait_state = {}
        trait_callbacks = entry.get("trait_callbacks")
        if not isinstance(trait_callbacks, dict):
            trait_callbacks = {}
        return cls(
            trait_name=trait_name,
            trait_module=trait_module,
            trait_state=trait_state,
            trait_callbacks=trait_callbacks,
        )

    def to_dict(self) -> dict[str, Any]:
        """Render the form a save writes. Omits the callbacks key when there are none."""
        entry: dict[str, Any] = {
            "trait_name": self.trait_name,
            "trait_module": self.trait_module,
            "trait_state": self.trait_state,
        }
        if self.trait_callbacks:
            entry["trait_callbacks"] = self.trait_callbacks
        return entry
