from __future__ import annotations

import importlib
import logging
import sys
from typing import TYPE_CHECKING

from griptape_nodes.exe_types.core_types import Trait

if TYPE_CHECKING:
    from types import ModuleType

logger = logging.getLogger("griptape_nodes")


def resolve_trait(trait_name: str, trait_module: str) -> type[Trait] | None:
    """Resolve a saved trait by module and class name."""
    module = sys.modules.get(trait_module)
    if module is None:
        module = _import_trait_module(trait_module, trait_name)
    if module is None:
        return None
    candidate = getattr(module, trait_name, None)
    if not isinstance(candidate, type) or not issubclass(candidate, Trait):
        return None
    return candidate


def _import_trait_module(trait_module: str, trait_name: str) -> ModuleType | None:
    """Import a trait module without failing the workflow load."""
    try:
        return importlib.import_module(trait_module)
    except Exception as error:
        if isinstance(error, ModuleNotFoundError) and _requested_module_is_missing(trait_module, error.name):
            return None
        logger.warning(
            "Attempted to restore the '%s' trait from '%s'. Loading that module failed (%s), "
            "so the parameter will load without that trait. This usually means the library "
            "providing it is broken or partly installed.",
            trait_name,
            trait_module,
            error,
        )
        return None


def _requested_module_is_missing(trait_module: str, missing_module: str | None) -> bool:
    if missing_module is None:
        return False
    return trait_module == missing_module or trait_module.startswith(f"{missing_module}.")
