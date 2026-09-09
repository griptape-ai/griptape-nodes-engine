"""TraitRegistry.resolve prefers a saved trait's module over guessing by name alone."""

from __future__ import annotations

import logging
import sys
from types import ModuleType
from typing import TYPE_CHECKING

import pytest

from griptape_nodes.exe_types.core_types import Trait
from griptape_nodes.traits.slider import Slider
from griptape_nodes.traits.trait_registry import TraitRegistry

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path


class _CollisionTraitA(Trait):
    """One of two distinct trait classes renamed below to share a class name."""

    def __init__(self) -> None:
        super().__init__(element_id="_CollisionTraitA")

    def ui_options_for_trait(self) -> dict:
        return {}

    @classmethod
    def get_trait_keys(cls) -> list[str]:
        return ["collision_a"]


class _CollisionTraitB(Trait):
    """The other of the two, standing in for a second library's trait of the same name."""

    def __init__(self) -> None:
        super().__init__(element_id="_CollisionTraitB")

    def ui_options_for_trait(self) -> dict:
        return {}

    @classmethod
    def get_trait_keys(cls) -> list[str]:
        return ["collision_b"]


# Renamed to collide: both now report the class name "SharedTraitName", from different
# modules, the way two libraries shipping a same-named trait would.
_CollisionTraitA.__name__ = "SharedTraitName"
_CollisionTraitA.__qualname__ = "SharedTraitName"
_CollisionTraitA.__module__ = "tests.fake.collision_module_a"
_CollisionTraitB.__name__ = "SharedTraitName"
_CollisionTraitB.__qualname__ = "SharedTraitName"
_CollisionTraitB.__module__ = "tests.fake.collision_module_b"


@pytest.fixture(autouse=True)
def _fake_collision_modules() -> Generator[None]:
    """Register the two fake modules the collision classes claim to live in."""
    module_a = ModuleType(_CollisionTraitA.__module__)
    setattr(module_a, "SharedTraitName", _CollisionTraitA)  # noqa: B010
    module_b = ModuleType(_CollisionTraitB.__module__)
    setattr(module_b, "SharedTraitName", _CollisionTraitB)  # noqa: B010
    sys.modules[_CollisionTraitA.__module__] = module_a
    sys.modules[_CollisionTraitB.__module__] = module_b
    yield
    sys.modules.pop(_CollisionTraitA.__module__, None)
    sys.modules.pop(_CollisionTraitB.__module__, None)


class TestResolveByModule:
    def test_resolves_an_in_tree_trait_through_its_module(self) -> None:
        resolved = TraitRegistry.resolve("Slider", "griptape_nodes.traits.slider")

        assert resolved is Slider

    def test_the_module_disambiguates_two_classes_sharing_a_name(self) -> None:
        resolved = TraitRegistry.resolve("SharedTraitName", _CollisionTraitB.__module__)

        assert resolved is _CollisionTraitB

    def test_a_module_that_no_longer_imports_falls_back_to_the_name_walk(self) -> None:
        resolved = TraitRegistry.resolve("Slider", "griptape_nodes.traits.a_module_that_was_removed")

        assert resolved is Slider


class TestResolveByNameWalkAmbiguity:
    """No module to disambiguate with, so name alone must pick from the collision."""

    def test_a_collision_is_resolved_but_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger="griptape_nodes"):
            resolved = TraitRegistry.resolve("SharedTraitName")

        # Deterministic: sorted by module, so module_a wins over module_b every time.
        assert resolved is _CollisionTraitA
        assert _CollisionTraitA.__module__ in caplog.text
        assert _CollisionTraitB.__module__ in caplog.text

    def test_a_class_with_no_collision_logs_nothing(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger="griptape_nodes"):
            resolved = TraitRegistry.resolve("Slider")

        assert resolved is Slider
        assert caplog.text == ""


class TestABrokenTraitModuleDoesNotFailTheLoad:
    """Resolving executes a library's module code, which must not take the workflow down."""

    def test_a_module_that_raises_on_import_falls_back_and_warns(
        self, caplog: pytest.LogCaptureFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "exploding_trait_module.py").write_text('raise RuntimeError("library blew up on import")')
        monkeypatch.syspath_prepend(str(tmp_path))

        with caplog.at_level(logging.WARNING, logger="griptape_nodes"):
            resolved = TraitRegistry.resolve("Slider", "exploding_trait_module")

        # Fell back to the name walk rather than propagating the library's RuntimeError.
        assert resolved is Slider
        assert "library blew up on import" in caplog.text

    def test_a_missing_module_falls_back_quietly(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger="griptape_nodes"):
            resolved = TraitRegistry.resolve("Slider", "griptape_nodes.traits.never_existed")

        assert resolved is Slider
        assert caplog.text == ""


class TestUnresolvableTraitDegradesInsteadOfRaising:
    def test_an_unknown_trait_name_returns_none(self) -> None:
        resolved = TraitRegistry.resolve("NoSuchTraitAnywhere")

        assert resolved is None

    def test_an_unknown_trait_name_with_an_unimportable_module_also_returns_none(self) -> None:
        resolved = TraitRegistry.resolve("NoSuchTraitAnywhere", "griptape_nodes.traits.does_not_exist")

        assert resolved is None
