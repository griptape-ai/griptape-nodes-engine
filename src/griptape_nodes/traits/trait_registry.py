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
    def resolve(cls, trait_name: str, trait_module: str) -> type[Trait] | None:
        """Resolve a saved trait entry to its class by importing the module it names.

        The module is the whole answer: a name alone cannot tell two libraries' same-named
        traits apart. A library trait's module is rewritten to that library's stable
        namespace before saving, and importing a stable namespace goes through the same
        machinery a saved workflow's own imports use, so a lazily loaded library resolves
        here too.

        Returns None when the module will not import, or holds no trait by that name. The
        caller reports it and loads the parameter without that trait.
        """
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

        A module that is simply gone passes quietly, because the caller already reports the
        trait it could not restore.

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
