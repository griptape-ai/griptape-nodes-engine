"""A trait declares its fields, and the element metaclass turns them into its constructor.

``BaseNodeElement`` is an attrs class, so a trait subclass declares ``attrs.field()`` attributes
and gets a constructor that sets them and the element wiring both. What must not happen is a
declaration that a save cannot hold or a load cannot replay, and the checks below are the ones
that cannot be expressed as a per-trait test: they hold for every trait in the tree.
"""

import importlib
import pkgutil
from typing import ClassVar

import attrs
import pytest

import griptape_nodes.traits
from griptape_nodes.exe_types.core_types import WIRING, Trait, default_element_id


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

_DECLARED_THRESHOLD = 9
_DECLARED_LEVEL = 3


def test_the_element_base_carries_the_metaclass() -> None:
    """The premise the rest of this file rests on: a trait is an attrs class without saying so."""
    assert attrs.has(Trait)


@pytest.mark.parametrize("trait_class", IN_TREE_TRAITS, ids=lambda cls: cls.__name__)
def test_a_trait_is_an_attrs_class_without_declaring_it(trait_class: type[Trait]) -> None:
    """No trait carries a decorator. The metaclass applies attrs, so none can opt out."""
    assert attrs.has(trait_class)


@pytest.mark.parametrize("trait_class", IN_TREE_TRAITS, ids=lambda cls: cls.__name__)
def test_a_trait_module_does_not_stringify_its_annotations(trait_class: type[Trait]) -> None:
    """``from __future__ import annotations`` hides a ClassVar behind a string the checks cannot read."""
    module = importlib.import_module(trait_class.__module__)

    assert "annotations" not in vars(module)


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

        assert Constant.DEFAULTS == ["a"]

    def test_an_unsubscripted_class_constant_is_allowed(self) -> None:
        """A bare ``ClassVar`` has no origin to read, so it has to be recognized on its own."""

        class BareConstant(Trait):
            DEFAULTS: ClassVar = ["a"]

        assert BareConstant.DEFAULTS == ["a"]

    def test_a_stringified_class_constant_is_refused(self) -> None:
        """What a trait module under ``from __future__ import annotations`` would hit."""
        with pytest.raises(TypeError, match="from __future__ import annotations"):

            class Stringified(Trait):
                DEFAULTS: "ClassVar[list[str]]" = ["a"]  # noqa: RUF012

    def test_a_declared_field_is_allowed(self) -> None:
        class Declared(Trait):
            threshold: int = attrs.field(default=5)

        assert Declared(threshold=9).threshold == _DECLARED_THRESHOLD

    def test_an_inherited_field_may_be_re_annotated(self) -> None:
        class Fixed(Trait):
            element_id: str = attrs.field(default="Fixed", converter=default_element_id, metadata=WIRING)

        assert Fixed().element_id == "Fixed"


class TestWhoGetsAGeneratedConstructor:
    """Declaring a field is what asks for a constructor.

    The metaclass reads that off the value ``attrs.field()`` returns. An attrs release that
    changed that value would otherwise stop generating constructors quietly, leaving every
    trait taking its parent's arguments instead of its own.
    """

    def test_a_declared_field_becomes_a_keyword_argument(self) -> None:
        class Declared(Trait):
            threshold: int = attrs.field(default=5)

        assert Declared(threshold=9).threshold == _DECLARED_THRESHOLD

    def test_a_declared_field_is_keyword_only(self) -> None:
        class Declared(Trait):
            threshold: int = attrs.field(default=5)

        with pytest.raises(TypeError):
            Declared(9)  # type: ignore[misc]

    def test_a_hand_written_constructor_is_kept(self) -> None:
        class HandWritten(Trait):
            def __init__(self, level: int = 1) -> None:
                super().__init__()
                self.level = level

        assert HandWritten(_DECLARED_LEVEL).level == _DECLARED_LEVEL

    def test_a_subclass_declaring_nothing_inherits_that_constructor(self) -> None:
        """A generated constructor here would take the parent's fields and drop its arguments."""

        class HandWritten(Trait):
            def __init__(self, level: int = 1) -> None:
                super().__init__()
                self.level = level

        class Inheriting(HandWritten):
            pass

        assert Inheriting.__init__ is HandWritten.__init__
