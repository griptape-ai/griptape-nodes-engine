from __future__ import annotations

import importlib
import logging
import sys
from typing import TYPE_CHECKING

from griptape_nodes.exe_types.core_types import Trait

if TYPE_CHECKING:
    from types import ModuleType

logger = logging.getLogger("griptape_nodes")


class TraitRegistry:
    """Finds the trait class a saved workflow named."""

    @classmethod
    def resolve(cls, trait_name: str, trait_module: str | None = None) -> type[Trait] | None:
        """Resolve a saved trait name back to its class.

        Prefers ``trait_module``: importing it and reading the named attribute finds the
        right class even when two libraries ship a trait of the same name. Falls back to
        walking every already-imported ``Trait`` subclass by name alone, for an entry saved
        before the module was recorded, or whose module no longer imports.
        """
        resolved = cls._resolve_from_module(trait_name, trait_module)
        if resolved is not None:
            return resolved
        return cls._resolve_by_name_walk(trait_name)

    @classmethod
    def _resolve_from_module(cls, trait_name: str, trait_module: str | None) -> type[Trait] | None:
        """Import ``trait_module`` and return its ``trait_name`` attribute, if that names a Trait."""
        if trait_module is None:
            return None
        module = sys.modules.get(trait_module)
        if module is None:
            module = cls._import_trait_module(trait_module, trait_name)
        if module is None:
            return None
        candidate = getattr(module, trait_name, None)
        if not isinstance(candidate, type) or not issubclass(candidate, Trait):
            return None
        return candidate

    @classmethod
    def _import_trait_module(cls, trait_module: str, trait_name: str) -> ModuleType | None:
        """Import a saved trait's module, or return None when it cannot be loaded.

        A missing module is the ordinary case for a workflow saved before the module was
        recorded, or by a library since renamed, so it passes quietly to the name walk.

        Any other failure means the module exists but its own code raised. Catching broadly
        is deliberate: this executes a library author's module-level code, which can fail in
        any way their file can fail, and a trait that will not load costs the parameter one
        control while letting the failure escape would cost the artist the whole workflow.
        """
        try:
            return importlib.import_module(trait_module)
        except ImportError:
            return None
        except Exception as error:
            logger.warning(
                "Attempted to restore the '%s' trait from '%s'. Loading that module failed (%s), "
                "so the parameter will load without that trait. This usually means the library "
                "providing it is broken or partly installed.",
                trait_name,
                trait_module,
                error,
            )
            return None

    @classmethod
    def _resolve_by_name_walk(cls, trait_name: str) -> type[Trait] | None:
        """Find every already-imported ``Trait`` subclass named ``trait_name`` and return one.

        A last resort: name alone cannot tell two distinct classes apart, so a collision is
        logged rather than silently resolved to whichever one happened to be found first.
        """
        candidates = sorted(
            {subclass for subclass in cls._all_trait_subclasses(Trait) if subclass.__name__ == trait_name},
            key=lambda candidate: f"{candidate.__module__}.{candidate.__qualname__}",
        )
        if not candidates:
            return None
        if len(candidates) > 1:
            found = ", ".join(f"{candidate.__module__}.{candidate.__name__}" for candidate in candidates)
            logger.warning(
                "Attempted to resolve the saved trait '%s' by name alone, because its module "
                "was not saved or no longer imports. Found more than one trait class named "
                "'%s': %s. The trait restored may not be the one that was saved.",
                trait_name,
                trait_name,
                found,
            )
        return candidates[0]

    @classmethod
    def _all_trait_subclasses(cls, klass: type[Trait]) -> set[type[Trait]]:
        """Return every subclass of ``klass``, direct or indirect."""
        subclasses: set[type[Trait]] = set()
        for subclass in klass.__subclasses__():
            subclasses.add(subclass)
            subclasses.update(cls._all_trait_subclasses(subclass))
        return subclasses
