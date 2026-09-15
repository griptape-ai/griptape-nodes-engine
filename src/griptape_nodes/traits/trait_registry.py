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
        """Resolve a trait by module and class name.

        The module distinguishes same-named traits from different libraries. Return ``None``
        when the module or class is unavailable.
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
        """Import a trait module without failing the workflow load.

        Missing modules are reported by the caller. Other failures are logged here because
        arbitrary library module code may raise any exception.
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
