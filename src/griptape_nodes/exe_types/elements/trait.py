"""The trait base class: a control attached to a parameter."""

from __future__ import annotations
import __future__

import contextlib
import inspect
import logging
import sys
from abc import ABC
from typing import TYPE_CHECKING, Any, ClassVar, Self, get_origin

import attrs

from griptape_nodes.exe_types.callback_binding import name_callback, resolve_callback
from griptape_nodes.exe_types.elements.base import BaseNodeElement
from griptape_nodes.exe_types.trait_state import CALLBACK_TYPE, as_saved_state_value, unsaveable_type

if TYPE_CHECKING:
    from collections.abc import Callable

    from griptape_nodes.exe_types.elements.parameter import Parameter
    from griptape_nodes.exe_types.node_types import BaseNode

logger = logging.getLogger("griptape_nodes")


# TODO: https://github.com/griptape-ai/griptape-nodes/issues/858


class Trait(ABC, BaseNodeElement):
    """A parameter control whose attrs fields define its saved contract.

    Normal fields are saved as data. ``metadata=BEHAVIOR`` fields are saved by owning-node
    method name. ``init=False`` fields are not saved.
    """

    @classmethod
    def __attrs_init_subclass__(cls) -> None:
        """Reject state that cannot round-trip through a saved workflow."""
        cls._reject_annotations_that_are_not_fields()
        # Forward references are checked by value while saving.
        with contextlib.suppress(NameError):
            attrs.resolve_types(cls)
        for attribute in cls._state_fields():
            unsaveable = unsaveable_type(attribute.type)
            if unsaveable == CALLBACK_TYPE:
                msg = (
                    f"Trait '{cls.__name__}' declares '{attribute.name}' as state, but its type is a callback. "
                    f"Declare it with metadata=BEHAVIOR so it is carried by method name instead of saved as data."
                )
                raise TypeError(msg)
            if unsaveable is not None:
                msg = (
                    f"Trait '{cls.__name__}' declares '{attribute.name}' as state, but a {unsaveable} cannot be "
                    f"written to a saved workflow. Trait state holds text, numbers, true/false, and lists or "
                    f"dictionaries of those. Convert it in the field, or declare it init=False if it is derived."
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
        migrated = type(self).migrate_state(state)
        interpreted = type(self)._construct(migrated)
        for attribute in self._state_fields():
            if self.saved_key(attribute) in migrated:
                setattr(self, attribute.name, getattr(interpreted, attribute.name))

    @classmethod
    def state_from_ui_options(cls, ui_options: dict[str, Any]) -> dict[str, Any]:  # noqa: ARG003
        """Map flat UI options to the mentioned state fields.

        The default ignores writes for traits with no state behind their rendered keys, which
        is why class creation does not require this to invert ``ui_options_for_trait``. A write
        to a rendered key no trait accepts is logged rather than dropped in silence.
        """
        return {}

    def callback_names(self, owner: BaseNode | None) -> dict[str, str]:
        """Return nameable behavior fields as owning-node method names."""
        names: dict[str, str] = {}
        for attribute in self._behavior_fields():
            callback = getattr(self, attribute.name, None)
            name = name_callback(callback, owner)
            if name is not None:
                names[self.saved_key(attribute)] = name
        return names

    def unnameable_callbacks(self, owner: BaseNode | None) -> list[str]:
        """Return behavior fields whose callbacks cannot be saved by name."""
        unnameable: list[str] = []
        for attribute in self._behavior_fields():
            callback = getattr(self, attribute.name, None)
            if callback is None:
                continue
            if name_callback(callback, owner) is None:
                unnameable.append(self.saved_key(attribute))
        return sorted(unnameable)

    def apply_callback_names(self, names: dict[str, str], owner: BaseNode | None) -> None:
        """Bind missing behavior fields to saved owning-node methods.

        Existing callbacks take precedence over saved names.
        """
        for attribute in self._behavior_fields():
            saved_key = self.saved_key(attribute)
            method_name = names.get(saved_key)
            if method_name is None:
                continue
            if getattr(self, attribute.name, None) is not None:
                continue
            described_as = f"the '{saved_key}' behavior of the '{type(self).__name__}' control"
            callback = resolve_callback(method_name, owner, described_as=described_as)
            if callback is not None:
                setattr(self, attribute.name, callback)

    @classmethod
    def migrate_state(cls, state: dict[str, Any]) -> dict[str, Any]:
        """Migrate saved field names or values before construction.

        Runs once per load, so an override may rename or convert unconditionally.
        """
        return state

    @classmethod
    def from_state(cls, state: dict[str, Any]) -> Self:
        """Construct a detached trait from saved state."""
        return cls._construct(cls.migrate_state(state))

    @classmethod
    def _construct(cls, migrated_state: dict[str, Any]) -> Self:
        """Construct from state already passed through ``migrate_state``."""
        with BaseNodeElement.detached():
            return cls(**migrated_state)

    @staticmethod
    def saved_key(attribute: attrs.Attribute) -> str:
        if attribute.alias is None:
            return attribute.name
        return attribute.alias

    @classmethod
    def _reject_annotations_that_are_not_fields(cls) -> None:
        """Reject bare annotations that type check as nonexistent constructor fields.

        Trait modules cannot postpone annotations because stringified ``ClassVar`` values
        cannot be distinguished from fields without evaluating possibly unbound names.
        """
        declared = {attribute.name for attribute in attrs.fields(cls)}
        for name, annotation in inspect.get_annotations(cls).items():
            # Bare ``ClassVar`` has no origin to read.
            if name in declared or annotation is ClassVar or get_origin(annotation) is ClassVar:
                continue
            if cls._uses_postponed_annotations():
                msg = (
                    f"Trait '{cls.__name__}' annotates '{name}' in a module with 'from __future__ import "
                    f"annotations', which turns every annotation into a string and hides whether it is a "
                    f"field. Remove that import from the trait's module."
                )
                raise TypeError(msg)
            msg = (
                f"Trait '{cls.__name__}' annotates '{name}' but never declares it. A trait's fields are its "
                f"saved state, so declare it with attrs.field(), mark it ClassVar if it is a constant, or "
                f"annotate it where it is assigned if it is neither."
            )
            raise TypeError(msg)

    @classmethod
    def _uses_postponed_annotations(cls) -> bool:
        """True when the trait's defining module wrote ``from __future__ import annotations``.

        That statement is a real import: it binds the name ``annotations`` in the module's
        namespace to this singleton, which nothing else binds.
        """
        module = sys.modules.get(cls.__module__)
        if module is None:
            return False
        return getattr(module, "annotations", None) is __future__.annotations

    @classmethod
    def state_keys(cls) -> list[str]:
        return [cls.saved_key(attribute) for attribute in cls._state_fields()]

    @classmethod
    def _state_fields(cls) -> list[attrs.Attribute]:
        return [
            attribute
            for attribute in attrs.fields(cls)
            if attribute.init and not attribute.metadata.get("wiring") and not attribute.metadata.get("behavior")
        ]

    @classmethod
    def _behavior_fields(cls) -> list[attrs.Attribute]:
        return [attribute for attribute in attrs.fields(cls) if attribute.metadata.get("behavior")]

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
