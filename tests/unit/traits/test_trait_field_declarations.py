"""A trait that writes its own __init__ must not declare dataclass fields.

@dataclass does not replace an __init__ found in the class body, so the generated one, and
every field default feeding it, is discarded. A field declared alongside a hand-written
constructor is therefore inert at best: ``field(default=x)`` leaves a class attribute that
masks the deadness, and ``field(default_factory=...)`` leaves nothing at all, so reading the
attribute raises unless the constructor happens to set it. AddParameterButton.type and
Slider._allowed_modes both shipped that way.
"""

import dataclasses
import importlib
import inspect
import pkgutil

import pytest

import griptape_nodes.traits
from griptape_nodes.exe_types.core_types import Trait


def _in_tree_traits() -> list[type[Trait]]:
    for module in pkgutil.iter_modules(griptape_nodes.traits.__path__):
        importlib.import_module(f"{griptape_nodes.traits.__name__}.{module.name}")

    found: dict[str, type[Trait]] = {}
    pending = [Trait]
    while pending:
        for subclass in pending.pop().__subclasses__():
            if subclass.__module__.startswith(griptape_nodes.traits.__name__):
                found[f"{subclass.__module__}.{subclass.__qualname__}"] = subclass
            pending.append(subclass)
    return [found[key] for key in sorted(found)]


def _own_field_names(trait_class: type[Trait]) -> list[str]:
    """Field names this class declares itself, with ClassVars and inherited fields excluded."""
    declared = set(inspect.get_annotations(trait_class))
    fields = {field.name for field in dataclasses.fields(trait_class)}
    return sorted(declared & fields)


@pytest.mark.parametrize("trait_class", _in_tree_traits(), ids=lambda cls: cls.__name__)
def test_a_hand_written_constructor_comes_with_no_field_declarations(trait_class: type[Trait]) -> None:
    """A field declared beside a hand-written __init__ is inert, so there should be none."""
    if "__init__" not in trait_class.__dict__:
        pytest.skip("uses the generated constructor, so its field defaults do run")

    assert _own_field_names(trait_class) == [], (
        f"{trait_class.__name__} declares its own __init__, so these field defaults never run. "
        f"Set the attributes in __init__ instead, annotating them there if the type matters."
    )
