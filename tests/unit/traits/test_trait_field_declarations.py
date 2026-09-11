"""A trait initializes the element it is, whichever way it declares its constructor.

``BaseNodeElement`` is not a dataclass, so nothing generates a constructor that quietly
skips it. A trait either writes an ``__init__`` that calls ``super().__init__()``, or
inherits one. What must not happen is a constructor that runs without the base attributes
being set, which is how a trait ends up with no ``element_id`` and fails only later, at the
point something tries to hash it or send it to the editor.
"""

import dataclasses
import importlib
import pkgutil

import pytest

import griptape_nodes.traits
from griptape_nodes.exe_types.core_types import BaseNodeElement, Trait


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


IN_TREE_TRAITS = _in_tree_traits()


def test_the_element_base_is_not_a_dataclass() -> None:
    """The premise the rest of this file rests on, and what keeps trait constructors clean.

    A generated constructor on the base would publish ``_children``, ``_parent``, and the
    rest of the element wiring as arguments to every trait.
    """
    assert not dataclasses.is_dataclass(BaseNodeElement)


@pytest.mark.parametrize("trait_class", IN_TREE_TRAITS, ids=lambda cls: cls.__name__)
def test_a_trait_is_not_a_dataclass(trait_class: type[Trait]) -> None:
    """A generated constructor would set the trait's fields and never reach the base one.

    The trait would come out with no ``element_id``, failing later at whatever first reads
    it. Write ``__init__`` and call ``super().__init__()``.
    """
    assert not dataclasses.is_dataclass(trait_class)


@pytest.mark.parametrize("trait_class", IN_TREE_TRAITS, ids=lambda cls: cls.__name__)
def test_a_trait_constructs_with_its_element_attributes_set(trait_class: type[Trait]) -> None:
    """Default-constructible traits must come out as usable elements."""
    try:
        trait = trait_class()
    except TypeError:
        pytest.skip("requires constructor arguments; covered by the round-trip tests")

    assert trait.element_id
    assert trait.element_type
    assert trait.name
    assert trait.get_badge() is None
    assert hash(trait) == hash(trait.element_id)


@pytest.mark.parametrize("trait_class", IN_TREE_TRAITS, ids=lambda cls: cls.__name__)
def test_a_trait_does_not_report_element_wiring_as_state(trait_class: type[Trait]) -> None:
    """State is the trait's own constructor arguments, never the base element's.

    ``name`` is exempt: it is the one base attribute a trait may reasonably declare as a
    constructor argument of its own, and ``Widget`` does.
    """
    element_wiring = {"element_id", "element_type", "parent_group_name", "_children", "_parent"}

    assert element_wiring.isdisjoint(trait_class._state_parameter_names())
