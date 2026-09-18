"""The trait base class: a control attached to a parameter."""

from __future__ import annotations

import contextlib
import inspect
import logging
from abc import ABC
from typing import TYPE_CHECKING, Any, ClassVar, Self, get_origin

import attrs

from griptape_nodes.exe_types.elements.base import BEHAVIOR_KEY, WIRING_KEY, BaseNodeElement
from griptape_nodes.exe_types.trait_state import (
    as_saved_state_value,
    is_callback_annotation,
    list_shaped_container,
    unsaveable_type,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from griptape_nodes.exe_types.elements.parameter import Parameter

logger = logging.getLogger("griptape_nodes")


def _is_class_var(annotation: Any) -> bool:
    """Recognize a ``ClassVar`` written either as a type or as text.

    A module with ``from __future__ import annotations`` hands every annotation over as a
    string, so the textual form has to be read too. Matches the trailing name, covering
    ``ClassVar``, ``ClassVar[x]``, and ``typing.ClassVar[x]``.
    """
    if isinstance(annotation, str):
        head = annotation.partition("[")[0].strip()
        return head.rpartition(".")[2] == "ClassVar"
    return annotation is ClassVar or get_origin(annotation) is ClassVar


# TODO: https://github.com/griptape-ai/griptape-nodes/issues/858


class Trait(ABC, BaseNodeElement):
    """A parameter control whose attrs fields define its saved contract.

    Normal fields are saved as data. ``metadata=BEHAVIOR`` fields hold a callback the owning
    node supplies on every construction, so they are not saved. ``init=False`` fields are not
    saved either.
    """

    # Set by a trait rendering its options under one nested key, which ``state_from_ui_options``
    # then reads back without the trait writing that inverse itself.
    NESTED_UI_OPTIONS_KEY: ClassVar[str | None] = None

    @classmethod
    def __attrs_init_subclass__(cls) -> None:
        """Reject state that cannot round-trip through a saved workflow."""
        cls._reject_annotations_that_are_not_fields()
        # Forward references are checked by value while saving.
        with contextlib.suppress(NameError):
            attrs.resolve_types(cls)
        for attribute in cls._state_fields():
            cls._reject_unsaveable_field(attribute)

    @classmethod
    def _reject_unsaveable_field(cls, attribute: attrs.Attribute) -> None:
        if is_callback_annotation(attribute.type):
            msg = (
                f"Trait '{cls.__name__}' declares '{attribute.name}' as state, but its type is a callback. "
                f"Declare it with metadata=BEHAVIOR, which marks a callback the owning node supplies."
            )
            raise TypeError(msg)
        unsaveable = unsaveable_type(attribute.type)
        if unsaveable is not None:
            msg = (
                f"Trait '{cls.__name__}' declares '{attribute.name}' as state, but a {unsaveable} cannot be "
                f"written to a saved workflow. Trait state holds text, numbers, true/false, and lists or "
                f"dictionaries of those. Convert it in the field, or declare it init=False if it is derived."
            )
            raise TypeError(msg)
        flattened = list_shaped_container(attribute.type)
        if flattened is not None and attribute.converter is None:
            msg = (
                f"Trait '{cls.__name__}' declares '{attribute.name}' as a {flattened}, which a saved workflow "
                f"holds as a list, so the constructor is handed a list on load. Give the field "
                f"converter={flattened} to convert it back, or declare it as a list."
            )
            raise TypeError(msg)

    def to_dict(self) -> dict[str, Any]:
        updated = super().to_dict()
        updated["trait_ui_options"] = self.ui_options_for_trait()
        updated["trait_name"] = self.__class__.__name__
        updated["trait_display_options"] = self.display_options_for_trait()
        return updated

    def to_state(self) -> dict[str, Any]:
        """Return constructor arguments, omitting and warning about unsupported values."""
        state: dict[str, Any] = {}
        for attribute in self._state_fields():
            saved = as_saved_state_value(getattr(self, attribute.name))
            if saved.unsupported_type is not None:
                logger.warning(
                    "Trait '%s' holds a %s in '%s', which cannot be written to a saved workflow. Trait state "
                    "holds text, numbers, true/false, and lists or dictionaries of those. The parameter will "
                    "load without this trait's '%s'.",
                    type(self).__name__,
                    saved.unsupported_type,
                    self.saved_key(attribute),
                    self.saved_key(attribute),
                )
                continue
            state[self.saved_key(attribute)] = saved.value
        return state

    def apply_state(self, state: dict[str, Any]) -> None:
        """Apply saved fields through the constructor without replacing the trait.

        Only supplied fields are copied, preserving constructor wiring and defaults for fields
        absent from older files. Constructor converters and validators still apply.

        Raises:
            TypeError: If ``state`` is missing a field the constructor requires.
            ValueError: If ``state`` holds a value a field's validator rejects.
        """
        if not state:
            return
        interpreted = type(self).from_state(state)
        for attribute in self._state_fields():
            if self.saved_key(attribute) in state:
                setattr(self, attribute.name, getattr(interpreted, attribute.name))

    @classmethod
    def state_from_ui_options(cls, ui_options: dict[str, Any]) -> dict[str, Any]:
        """Map flat UI options to the state fields they mention.

        Reads the nested key a trait renders under, when it names one. A trait rendering keys
        with no state behind them adopts nothing, which is why class creation does not require
        this to invert ``ui_options_for_trait``. A write to a rendered key no trait accepts is
        logged rather than dropped in silence.
        """
        if cls.NESTED_UI_OPTIONS_KEY is None:
            return {}
        written = ui_options.get(cls.NESTED_UI_OPTIONS_KEY)
        if not isinstance(written, dict):
            return {}
        return {key: written[key] for key in cls.state_keys() if key in written}

    @classmethod
    def from_state(cls, state: dict[str, Any]) -> Self:
        """Construct a detached trait from saved state."""
        with BaseNodeElement.detached():
            return cls(**state)

    @staticmethod
    def saved_key(attribute: attrs.Attribute) -> str:
        if attribute.alias is None:
            return attribute.name
        return attribute.alias

    @classmethod
    def _reject_annotations_that_are_not_fields(cls) -> None:
        """Reject bare annotations that type check as nonexistent constructor fields."""
        declared = {attribute.name for attribute in attrs.fields(cls)}
        for name, annotation in inspect.get_annotations(cls).items():
            if name in declared or _is_class_var(annotation):
                continue
            msg = (
                f"Trait '{cls.__name__}' annotates '{name}' but never declares it. A trait's fields are its "
                f"saved state, so declare it with attrs.field(), mark it ClassVar if it is a constant, or "
                f"annotate it where it is assigned if it is neither."
            )
            raise TypeError(msg)

    @classmethod
    def state_keys(cls) -> list[str]:
        return [cls.saved_key(attribute) for attribute in cls._state_fields()]

    @classmethod
    def _state_fields(cls) -> list[attrs.Attribute]:
        return [
            attribute
            for attribute in attrs.fields(cls)
            if attribute.init and not attribute.metadata.get(WIRING_KEY) and not attribute.metadata.get(BEHAVIOR_KEY)
        ]

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
