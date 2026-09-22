"""The trait base class: a control attached to a parameter."""

from __future__ import annotations

from abc import ABC
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Self

from griptape_nodes.exe_types.elements.base import BaseNodeElement

if TYPE_CHECKING:
    from collections.abc import Callable

    from griptape_nodes.exe_types.elements.parameter import Parameter


# TODO: https://github.com/griptape-ai/griptape-nodes/issues/858


@dataclass(eq=False)
class Trait(ABC, BaseNodeElement):
    def __hash__(self) -> int:
        # Use a unique, immutable attribute for hashing
        return hash(self.element_id)

    def __eq__(self, other: object) -> bool:
        if not (isinstance(other, Trait)):
            return False
        return self.to_dict() == other.to_dict()

    def to_dict(self) -> dict[str, Any]:
        updated = super().to_dict()
        updated["trait_ui_options"] = self.ui_options_for_trait()
        updated["trait_name"] = self.__class__.__name__
        updated["trait_display_options"] = self.display_options_for_trait()
        return updated

    def to_state(self) -> dict[str, Any]:
        """Return state that must survive a workflow save."""
        return {}

    def apply_state(self, state: dict[str, Any]) -> None:
        """Apply state from a saved workflow."""
        if state:
            msg = f"Trait '{type(self).__name__}' does not accept saved state."
            raise ValueError(msg)

    @classmethod
    def from_state(cls, state: dict[str, Any]) -> Self:
        """Build a trait from saved state when the node did not build one.

        Passes the state to the constructor. Override when ``to_state`` keys are not
        constructor arguments.
        """
        return cls(**state)

    @classmethod
    def state_from_ui_options(cls, _ui_options: dict[str, Any]) -> dict[str, Any]:
        """Return trait state represented by an inbound UI option write."""
        return {}

    def ui_options_for_trait(self) -> dict:
        """Returns a list of UI options for the parameter as a list of strings or dictionaries."""
        return {}

    def display_options_for_trait(self) -> dict:
        """Returns a list of display options for the parameter as a dictionary."""
        return {}

    def converters_for_trait(self) -> list[Callable[[Any], Any]]:
        """Returns a list of methods to be applied as a convertor."""
        return []

    def validators_for_trait(self) -> list[Callable[[Parameter, Any]]]:
        """Returns a list of methods to be applied as a validator."""
        return []


def instantiate_trait(trait: type[Trait] | Trait) -> Trait:
    """Return the trait a caller passed, building one if the caller passed the class."""
    if isinstance(trait, Trait):
        return trait
    return trait()
