"""A trait declares its fields, and the element metaclass turns them into its constructor.

``BaseNodeElement`` is an attrs class, so a trait subclass declares ``attrs.field()`` attributes
and gets a constructor that sets them and the element wiring both. What must not happen is a
declaration that a save cannot hold or a load cannot replay, and the checks below are the ones
that cannot be expressed as a per-trait test: they hold for every trait in the tree.
"""

import importlib
import pkgutil
from collections.abc import Callable
from pathlib import Path
from typing import Any, ClassVar

import attrs
import pytest

import griptape_nodes.traits
from griptape_nodes.exe_types.core_types import BEHAVIOR, WIRING, Trait, default_element_id


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

ELEMENT_WIRING = ("element_id", "element_type", "parent_group_name", "_children", "_parent")


def test_the_element_base_carries_the_metaclass() -> None:
    """The premise the rest of this file rests on: a trait is an attrs class without saying so."""
    assert attrs.has(Trait)


@pytest.mark.parametrize("trait_class", IN_TREE_TRAITS, ids=lambda cls: cls.__name__)
def test_a_trait_is_an_attrs_class_without_declaring_it(trait_class: type[Trait]) -> None:
    """No trait carries a decorator. The metaclass applies attrs, so none can opt out."""
    assert attrs.has(trait_class)


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


@pytest.mark.parametrize("trait_class", IN_TREE_TRAITS, ids=lambda cls: cls.__name__)
def test_a_trait_does_not_report_element_wiring_as_state(trait_class: type[Trait]) -> None:
    """State is what the trait declared, never what the element base did.

    ``Widget`` is the one trait that re-declares ``name`` as its own field, which is a
    deliberate override: which widget to render is state. Everything else stays wiring.
    """
    state_keys = trait_class.state_keys()

    assert set(ELEMENT_WIRING).isdisjoint(state_keys)


@pytest.mark.parametrize("trait_class", IN_TREE_TRAITS, ids=lambda cls: cls.__name__)
def test_a_trait_is_identified_rather_than_compared(trait_class: type[Trait]) -> None:
    """Two traits of one class are two controls, even when they hold the same values.

    attrs would generate value equality, which would collapse a pair of identical buttons into
    one wherever traits travel in a set.
    """
    try:
        first, second = trait_class(), trait_class()
    except TypeError:
        pytest.skip("requires constructor arguments")

    assert first != second
    assert len({first, second}) == 2  # noqa: PLR2004


class TestTheContractIsCheckedWhenTheClassIsBuilt:
    """A declaration that could never round-trip costs a library its import, not an artist's save."""

    def test_a_callback_declared_as_state_is_refused(self) -> None:
        with pytest.raises(TypeError, match="metadata=BEHAVIOR"):

            class Handler(Trait):
                on_ping: Callable | None = attrs.field(default=None)

    def test_a_state_type_no_saved_workflow_can_hold_is_refused(self) -> None:
        with pytest.raises(TypeError, match="Path cannot be written"):

            class Rooted(Trait):
                root: Path | None = attrs.field(default=None)

    def test_an_unsaveable_type_inside_a_container_is_refused(self) -> None:
        with pytest.raises(TypeError, match="Path cannot be written"):

            class ManyRoots(Trait):
                roots: list[Path] = attrs.field(factory=list)

    def test_a_saveable_declaration_is_accepted(self) -> None:
        class Fine(Trait):
            label: str | None = attrs.field(default=None)
            counts: dict[str, int] = attrs.field(factory=dict)
            on_ping: Any = attrs.field(default=None, metadata=BEHAVIOR)

        assert Fine.state_keys() == ["label", "counts"]

    def test_a_derived_field_is_neither_state_nor_a_constructor_argument(self) -> None:
        class Derived(Trait):
            label: str = attrs.field(default="")
            _cached: object = attrs.field(default=None, init=False)

        assert Derived.state_keys() == ["label"]
        assert Derived(label="x")._cached is None


class TestAnAnnotationThatIsNotAFieldIsRefused:
    """A field is opt-in, but a type checker reads every annotation as one.

    Without this, ``threshold: int = 5`` type-checks as a constructor argument and raises an
    unexpected-keyword error the first time anyone builds the trait.
    """

    def test_a_bare_annotated_attribute_is_refused(self) -> None:
        with pytest.raises(TypeError, match="annotates 'threshold' but never declares it"):

            class Bare(Trait):
                threshold: int = 5

    def test_an_annotation_with_no_value_is_refused(self) -> None:
        with pytest.raises(TypeError, match=r"attrs\.field"):

            class Undeclared(Trait):
                threshold: int

    def test_a_class_constant_is_allowed(self) -> None:
        class Constant(Trait):
            DEFAULTS: ClassVar[list[str]] = ["a"]

        assert Constant.state_keys() == []

    def test_a_declared_field_is_allowed(self) -> None:
        class Declared(Trait):
            threshold: int = attrs.field(default=5)

        assert Declared(threshold=9).to_state() == {"threshold": 9}

    def test_an_inherited_field_may_be_re_annotated(self) -> None:
        class Fixed(Trait):
            element_id: str = attrs.field(default="Fixed", converter=default_element_id, metadata=WIRING)

        assert Fixed().element_id == "Fixed"
