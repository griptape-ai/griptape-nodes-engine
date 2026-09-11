"""TraitRegistry.resolve finds a saved trait through the module it was saved with."""

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
        assert TraitRegistry.resolve("SharedTraitName", _CollisionTraitA.__module__) is _CollisionTraitA
        assert TraitRegistry.resolve("SharedTraitName", _CollisionTraitB.__module__) is _CollisionTraitB


class TestABrokenTraitModuleDoesNotFailTheLoad:
    """Resolving executes a library's module code, which must not take the workflow down."""

    def test_a_module_that_raises_on_import_warns_instead_of_propagating(
        self, caplog: pytest.LogCaptureFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "exploding_trait_module.py").write_text('raise RuntimeError("library blew up on import")')
        monkeypatch.syspath_prepend(str(tmp_path))

        with caplog.at_level(logging.WARNING, logger="griptape_nodes"):
            resolved = TraitRegistry.resolve("Slider", "exploding_trait_module")

        assert resolved is None
        assert "library blew up on import" in caplog.text

    def test_a_missing_module_resolves_to_nothing_quietly(self, caplog: pytest.LogCaptureFixture) -> None:
        """The caller reports the trait it could not restore, so this stays quiet."""
        with caplog.at_level(logging.WARNING, logger="griptape_nodes"):
            resolved = TraitRegistry.resolve("Slider", "griptape_nodes.traits.never_existed")

        assert resolved is None
        assert caplog.text == ""


class TestUnresolvableTraitDegradesInsteadOfRaising:
    def test_a_name_the_module_does_not_hold_returns_none(self) -> None:
        resolved = TraitRegistry.resolve("NoSuchTraitAnywhere", "griptape_nodes.traits.slider")

        assert resolved is None

    def test_a_name_that_holds_something_other_than_a_trait_returns_none(self) -> None:
        resolved = TraitRegistry.resolve("Parameter", "griptape_nodes.exe_types.core_types")

        assert resolved is None
